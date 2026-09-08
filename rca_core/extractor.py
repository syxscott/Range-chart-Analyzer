"""Range-chart extraction core.

Calls the MiniMax M3 (Anthropic-compatible) vision API using only the
Python standard library (urllib) so the app runs with zero third-party
install. Pillow is used only when available, to downscale huge images.

The public ``extract_range_chart`` never raises: it returns an
``ExtractResult`` with an ``ok`` flag and an ``error_key`` for the UI to
translate. Mirrors the JS ``extractRangeChart`` contract.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import re
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

from .image_hash import compute_image_sha256_from_b64
from .json_utils import safe_json_loads
from .llm import ApiFormat, LlmProvider, call_llm_api
from .prompt import (
    ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
    CHART_LANG_HINT,
    COLUMNAR_SECTION_SYSTEM_PROMPT,
    PHYLOGENETIC_TREE_SYSTEM_PROMPT,
    RANGE_CHART_SYSTEM_PROMPT,
    prompt_version_for_mode,
)

DEFAULT_ENDPOINT = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_MAX_TOKENS = 4000
# M5: explicit min/max bounds for clamp_max_tokens — defends against
# user typing absurd values (negative, millions) in the GUI / API.
MIN_MAX_TOKENS = 1
# Cap must match the web UI's max token slider: index.html:65 has
# max="32000" and js/config.js:17 has maxMaxTokens: 32000 (the JS clamp
# at js/config.js:34 also uses 32000). The previous 100000 was a
# core/UI inconsistency that let the server accept values the UI could
# never produce; align the core to the system-wide 32000 cap.
MAX_MAX_TOKENS = 32000
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_MAX_EDGE = 4000


def clamp_max_tokens(value):
    """Coerce a user-supplied max_tokens into [MIN, MAX]."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS
    return max(MIN_MAX_TOKENS, min(v, MAX_MAX_TOKENS))


# Bug-18 fix: explicit bounds for timeout_sec. Previously the GUI passed
# whatever the user typed into the settings box without clamping, so a
# value like 10000 (≈2.7 h) would tie up a worker thread indefinitely.
# The server.py path already clamped to [10, 300]; the GUI path didn't.
MIN_TIMEOUT_SEC = 10
MAX_TIMEOUT_SEC = 300


def clamp_timeout_sec(value):
    """Coerce a user-supplied timeout_sec into [MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC]."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SEC
    return max(MIN_TIMEOUT_SEC, min(v, MAX_TIMEOUT_SEC))


# L-3 fix: explicit bounds for max_edge. Previously max_edge=0 would bypass
# the resize check entirely (treated as falsy). Now we clamp to a valid range.
MIN_MAX_EDGE = 0   # 0 means "disabled / no resize"
MAX_MAX_EDGE = 10000


def clamp_max_edge(value):
    """Coerce a user-supplied max_edge into [MIN_MAX_EDGE, MAX_MAX_EDGE].

    0 disables resize; positive values cap the longest image edge.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_EDGE
    return max(MIN_MAX_EDGE, min(v, MAX_MAX_EDGE))


# P1-5 (REVIEW-2026-07-25): occurrence mode enum for species rows.
# Replaces the old boolean `reworked` field with a string classification
# of how the taxon occurred in the section.
VALID_OCCURRENCE_MODES = frozenset({
    "unknown",
    "in_situ",
    "reworked",
    "transported",
    "cavity_fill",
    "bioturbated",
    "derived",
    "lag_deposit",
})

VALID_ENDPOINT_KINDS = frozenset({
    "unknown",
    "observed",
    "projected",
    "truncated",
})


def _build_request_meta(provider, mode, max_tokens, image_sha256, prompt_version):
    """Build the per-request metadata dict persisted into ExtractResult.

    P1-2 + D6 (REVIEW-2026-07-27): a record must carry enough context
    for a 5-year audit to reconstruct "what produced this row". Any
    field that affects reproducibility (model, sampling, prompt text)
    goes here. ``image_sha256`` is included so the metadata and the
    fingerprint can be cross-referenced even when source_path is
    absent (clipboard paste).
    """
    meta: dict[str, Any] = {
        "mode": mode,
        "max_tokens": max_tokens,
        "prompt_version": prompt_version,
        "image_sha256": image_sha256,
    }
    if provider is not None:
        meta["model"] = provider.model
        meta["endpoint"] = provider.endpoint
        meta["api_format"] = provider.api_format.value
        # temperature / seed live in extra_body (LLM providers carry
        # them as opaque overrides). Read them out so the metadata
        # field reflects them as first-class fields.
        eb = provider.extra_body or {}
        if "temperature" in eb:
            meta["temperature"] = eb["temperature"]
        if "seed" in eb:
            meta["seed"] = eb["seed"]
    return meta


@dataclass
class ExtractResult:
    ok: bool = False
    data: dict[str, Any] | None = None
    error_key: str | None = None
    status: int | None = None
    raw: str = ""
    truncated: bool = False
    # H7: upstream error body (decoded, truncated). Surfaced to the GUI so
    # 5xx debugging has signal beyond the status code.
    error_body: str = ""
    # M2: how many multi-run attempts failed (count is 0 when runs == 1).
    partial_failures: int = 0
    # Token usage: ``{input_tokens, output_tokens, cache_read_tokens,
    # cache_creation_tokens, estimated}``. Empty dict when no API call
    # was made (e.g. image-b64 missing).
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    # Warning message for partial-success states (e.g. model hit the
    # max_tokens ceiling so the returned JSON may be truncated). Kept
    # distinct from `error_body` (which describes transport / API
    # errors) and from `truncated` (a boolean) so the frontend can show
    # a clear "result may be incomplete" banner without flipping `ok`
    # to False (which would discard otherwise-usable data).
    warning: str = ""
    # P1-1 / P1-2 (REVIEW-2026-07-27): mandatory image fingerprint and
    # per-request metadata. ``image_sha256`` survives clipboard-paste /
    # no-source-path cases so a 5-year audit can prove "this record
    # came from THAT image". ``request_meta`` carries the full sampling
    # context (model, max_tokens, temperature, seed, prompt_sha256, …)
    # populated by ``extract_*`` so the server / GUI can persist a
    # complete reproducible provenance record.
    image_sha256: str = ""
    request_meta: dict[str, Any] = field(default_factory=dict)
    # UI-REVIEW-2026-09-07 (auto mode): the concrete chart type an "auto"
    # extraction resolved to ("range_chart" / "zonation_chart" / …) and how
    # it was decided. Empty for explicitly-chosen modes.
    mode_used: str = ""
    mode_source: str = ""  # "text" | "vision" | "default" | ""


def _enhance_image_pil(img: "Image.Image") -> "Image.Image":
    """Pillow-only image enhancement for thin lines and small text.

    Applies a light unsharp mask + contrast boost. This is intentionally
    conservative — we do NOT upsample here (that is done by the caller via
    max_edge) and we avoid heavy denoising that could erase faint range
    lines. For stronger enhancement (3× upsample + NLMeans denoising +
    CLAHE) the optional cv2 path in `_enhance_image_cv2` is used when
    available.
    """
    try:
        from PIL import ImageEnhance, ImageFilter  # type: ignore
    except Exception:
        return img
    # Unsharp mask sharpens thin lines and small italic species names.
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=3))
    # Gentle contrast boost helps faint pencil lines stand out.
    img = ImageEnhance.Contrast(img).enhance(1.15)
    return img


def _enhance_image_cv2(img: "Image.Image") -> "Image.Image":
    """Optional OpenCV-based enhancement (3× upsample + NLMeans denoise).

    Falls back to the Pillow path when cv2 is not installed. The caller
    decides which path to use via the ``enhance`` parameter.
    """
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return _enhance_image_pil(img)
    arr = np.array(img)
    # 3× upsample — helps the VLM read small rotated text.
    h, w = arr.shape[:2]
    arr = cv2.resize(arr, (w * 3, h * 3), interpolation=cv2.INTER_LANCZOS4)
    # NLMeans denoising — removes scan noise without erasing thin lines.
    if len(arr.shape) == 3:
        arr = cv2.fastNlMeansDenoisingColored(arr, None, 10, 10, 7, 21)
    else:
        arr = cv2.fastNlMeansDenoising(arr, None, 10, 7, 21)
    # CLAHE contrast enhancement for faint lines.
    if len(arr.shape) == 3:
        lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        arr = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    return Image.fromarray(arr)  # type: ignore


def load_image_b64(path: str, max_edge: int = DEFAULT_MAX_EDGE,
                   enhance: bool = False):
    """Read an image, optionally downscale so its long edge <= max_edge.

    Returns ``(base64, media_type, width, height, resized, decode_error)``.
    Falls back to the raw bytes when Pillow is unavailable. The new
    ``decode_error`` flag is True when the file could not be decoded as
    an image (corrupt PNG header, etc.) — callers can use this to surface
    a friendlier error instead of showing a 0×0 thumbnail.

    When ``enhance=True`` the image is pre-processed before downscale to
    improve VLM recognition of thin range lines and small italic species
    names. Uses the Pillow-only path by default; the stronger cv2 path
    (3× upsample + NLMeans + CLAHE) is used when ``enhance='cv2'`` and
    cv2 is installed.

    Bug-15 fix: the previous version returned ``width=0, height=0`` for
    both "Pillow missing" and "decode failed", making the two cases
    indistinguishable. We now set ``decode_error=True`` when Pillow is
    present but cannot decode.
    """
    with open(path, "rb") as f:
        raw = f.read()
    mime, _ = mimetypes.guess_type(path)
    if mime is None:
        mime = "image/png"

    try:
        from PIL import Image  # type: ignore
    except Exception:
        # Pillow missing entirely — we still try to upload the bytes;
        # the upstream API will reject them if it can't decode.
        return base64.b64encode(raw).decode("ascii"), mime, 0, 0, False, False

    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
    except Exception:
        # Bug-15 fix: Pillow present but decode failed (corrupt file,
        # wrong format). Distinguish from "no Pillow" so the UI can show
        # a real error instead of a 0×0 image.
        return base64.b64encode(raw).decode("ascii"), mime, 0, 0, False, True

    # FIX (enhance): pre-process the image to boost VLM recognition of thin
    # lines and small text. Default off — the user opts in via the UI.
    if enhance:
        if enhance == "cv2":
            img = _enhance_image_cv2(img)
        else:
            img = _enhance_image_pil(img)
        w, h = img.size  # re-read size (cv2 path may have upsampled)

    long_edge = max(w, h)
    if max_edge and long_edge > max_edge:
        scale = max_edge / long_edge
        nw, nh = int(round(w * scale)), int(round(h * scale))
        img = img.resize((nw, nh), Image.LANCZOS)
        out = io.BytesIO()
        # Prefer lossless PNG for downscaled charts so the small italic
        # species names stay sharp. JPEG re-compression blurs dense text
        # and is a known cause of OCR misreads. Only keep JPEG when the
        # source is already JPEG AND the resized image is large enough
        # that a lossless PNG would be excessively big.
        resized_is_large = (nw * nh) > (2500 * 2500)
        if mime == "image/jpeg" and resized_is_large:
            fmt = "JPEG"
            img = img.convert("RGB")
            img.save(out, format=fmt, quality=95)
        else:
            fmt = "PNG"
            img.save(out, format=fmt)
        data = out.getvalue()
        out_mime = "image/png" if fmt == "PNG" else "image/jpeg"
        return base64.b64encode(data).decode("ascii"), out_mime, nw, nh, True, False
    return base64.b64encode(raw).decode("ascii"), mime, w, h, False, False


_KNOWN_RANGE_CHART_KEYS = (
    "sections", "species_ranges", "biozones", "other_fossils", "confidence",
)
_KNOWN_SECTION_KEYS = (
    "name", "age_range", "formations", "formation_thickness_m", "coordinates",
)
_KNOWN_SPECIES_KEYS = (
    "species", "section", "range_top", "range_base", "biozone",
    "author", "year",
    # HIGH fix: author_year is the combined string the prompt requests.
    "author_year", "reworked",
    # P0-3 (REVIEW-2026-07-25): the four fields below are NOW first-class
    # row keys written explicitly by _normalize_species_into, so they must
    # NOT be in _KNOWN_SPECIES_KEYS — otherwise _carry_extras would treat
    # any same-named dict keys emitted by the LLM as known and skip them
    # in the merge field pass (causing double-write via _extras).
    # Note: any leftover occurrences in the source dict are still captured
    # by _carry_extras through the "in _extras" branch — but since they are
    # also written to row[...] explicitly above, the row value wins.
)
_KNOWN_BIOZONE_KEYS = ("name", "section", "age", "thickness_m", "zone_type")

# MEDIUM fix: iron-rule markers for zone labels misclassified as species.
# Per prompt.py:80, names ending in Zone / Zonule / assemblage go into
# ``biozones``, NEVER into ``species_ranges``. We post-normalize to flag any
# slip-through so the operator can see it instead of silently exporting a
# fabricated FAD/LAD for a non-taxon.
_IRON_RULE_ZONE_RE = re.compile(
    r"\b(zone|zonule|assemblage|oppel|interval|lineage|range|acme)\b",
    re.IGNORECASE,
)


def _classify_array_item(item: dict[str, Any]) -> str | None:
    """Classify an item from a top-level array to the appropriate key.

    Returns the root key name (e.g. "sections", "species_ranges") or None if
    the item cannot be classified. Uses distinguishing keys to differentiate
    between section, species_range, biozone, and other_fossils entries.
    """
    if not isinstance(item, dict):
        return None
    # P0-4: explicit zone_type wins over all heuristics.
    zt = item.get("zone_type")
    if isinstance(zt, str):
        zt_lower = zt.strip().lower()
        if zt_lower in {"biozone", "zone", "assemblage_zone", "interval_zone",
                       "lineage_zone", "acme_zone", "oppel_zone", "range_zone",
                       "subzone", "zonule"}:
            return "biozones"
        if zt_lower in {"species_range", "taxon_range", "fad_lad"}:
            return "species_ranges"
        if zt_lower in {"section", "measured_section", "locality"}:
            return "sections"
    # Species ranges have "species" (the primary identifier) and range bounds.
    if "species" in item or ("range_top" in item and "range_base" in item):
        return "species_ranges"
    # P1-8 (REVIEW-2026-07-25): biozone identification now requires the
    # NAME to look like a zone/assemblage label (per prompt.py:80, 195
    # iron-rule markers) OR the item to declare ``zone_type``. Otherwise
    # a {name, age} item is likely a SECTION whose model confused
    # ``age_range`` with ``age`` — misclassifying it would silently move
    # a stratigraphic section into the biozones table, breaking all
    # biozone/range coupling validation downstream.
    name_str = (item.get("name") or "").strip()
    is_zone_label = bool(_IRON_RULE_ZONE_RE.search(name_str))
    if "name" in item and "age" in item and is_zone_label:
        return "biozones"
    # Sections have "name" and typically "age_range" or "formations".
    # ``age`` (without the ``_range`` suffix) is also accepted as a section
    # age signal — the prompt asks for ``age_range`` but lenient parsing
    # is the safer default.
    if "name" in item and ("age_range" in item or "formations" in item
                           or "age" in item):
        return "sections"
    # Fallback: if it has "name" but doesn't match biozone pattern, treat as section.
    if "name" in item:
        return "sections"
    return None


def _carry_extras(item: dict[str, Any], known: tuple[str, ...], out: dict[str, Any]) -> None:
    """H8: any non-known key the model emitted is preserved under a single
    ``_extras`` dict so downstream consumers (CSV/JSON export) can see it.

    P0-3 fix: also skip keys that are already top-level fields in ``out``.
    When the caller pre-populates ``out`` with explicit keys (e.g. endpoint_kind,
    occurrence_mode, range_top_bed), those fields must NOT also appear in
    _extras even if the raw item also carries them.
    """
    extras = {k: v for k, v in item.items() if k not in known and k not in out}
    if not extras:
        return
    existing = out.get("_extras")
    if isinstance(existing, dict):
        # Merge - existing keys (e.g. wrapper_key) win for collisions so
        # the structural hint from the caller takes precedence.
        merged = dict(existing)
        merged.update(extras)
        out["_extras"] = merged
    else:
        out["_extras"] = extras


def _normalize_section_into(sec: dict[str, Any],
                             target: list[dict[str, Any]]) -> None:
    """Build a section row from a raw dict and append it to ``target``.

    MEDIUM fix: the _array_root handler previously appended the raw dict
    without coercion, so string-where-list-was-expected fields and missing
    keys slipped through unchanged. Routing every item through this helper
    (and the sibling species/biozone helpers) guarantees a uniform shape.
    """
    def s(v):
        return "" if v is None else str(v)

    formations = sec.get("formations")
    if isinstance(formations, list):
        formations_out = [str(x).strip() for x in formations
                          if isinstance(x, str) and str(x).strip()]
    elif isinstance(formations, str) and formations.strip():
        formations_out = [formations.strip()]
    else:
        formations_out = []
    row = {
        "name": s(sec.get("name")),
        "age_range": s(sec.get("age_range")),
        "formations": formations_out,
        "formation_thickness_m": s(sec.get("formation_thickness_m")),
        "coordinates": s(sec.get("coordinates")),
    }
    _carry_extras(sec, _KNOWN_SECTION_KEYS, row)
    target.append(row)


def _normalize_occurrence_mode(sp: dict[str, Any]) -> str:
    """Return a scientifically explicit occurrence classification.

    Missing or invalid modern enum values remain ``unknown`` rather than being
    promoted to the positive assertion ``in_situ``. The legacy
    ``reworked: bool`` field is still mapped for backward compatibility because
    both boolean values carry an explicit assertion from older saved results.
    """
    raw = sp.get("occurrence_mode")
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in VALID_OCCURRENCE_MODES:
            return normalized
    reworked = sp.get("reworked")
    if isinstance(reworked, bool):
        return "reworked" if reworked else "in_situ"
    return "unknown"


def _normalize_endpoint_kind(value: Any) -> str:
    """Normalize an endpoint classification without inventing observation."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_ENDPOINT_KINDS:
            return normalized
    return "unknown"


def _normalize_optional_int(value: Any) -> int | None:
    """Return an exact integer index, or ``None`` when it is not integral."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else None
    return None


def _normalize_confidence(value: Any) -> float | None:
    """Normalize an optional per-row confidence to the closed interval [0, 1]."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


def _normalize_species_into(sp: dict[str, Any],
                            target: list[dict[str, Any]]) -> None:
    """Build a species_ranges row from a raw dict and append it."""
    def s(v):
        return "" if v is None else str(v)

    row = {
        "species": s(sp.get("species")),
        "section": s(sp.get("section")),
        "range_top": s(sp.get("range_top")),
        "range_base": s(sp.get("range_base")),
        "biozone": s(sp.get("biozone")),
        "author": s(sp.get("author", "")),
        "year": s(sp.get("year", "")),
        # HIGH fix: author_year is the combined string the prompt requests.
        "author_year": s(sp.get("author_year") or ""),
        # P0-3 (REVIEW-2026-07-25): prompt.py asks for these four scientific
        # metadata fields per species row. They were previously listed in
        # _KNOWN_SPECIES_KEYS so _carry_extras would skip them — silently
        # dropping observed/projected/truncated endpoint classification,
        # reworked flag, and exact bed labels. Promote them to first-class
        # row keys and ALSO drop them from _KNOWN_SPECIES_KEYS so _carry_extras
        # does not double-write them.
        "range_top_bed": s(sp.get("range_top_bed", "")),
        "range_base_bed": s(sp.get("range_base_bed", "")),
        "range_top_idx": _normalize_optional_int(sp.get("range_top_idx")),
        "range_base_idx": _normalize_optional_int(sp.get("range_base_idx")),
        "endpoint_kind": _normalize_endpoint_kind(sp.get("endpoint_kind")),
        # Missing modern classifications remain unknown. Explicit legacy
        # reworked booleans retain their historical compatibility mapping.
        "occurrence_mode": _normalize_occurrence_mode(sp),
        "confidence": _normalize_confidence(sp.get("confidence")),
        # H-8 fix: prompt explicitly asks for a note field for degraded
        # determinations; it was never written to the output row.
        "note": s(sp.get("note", "")),
    }
    _carry_extras(sp, _KNOWN_SPECIES_KEYS, row)
    # P0-4: defensive — if species name looks like a zone, flag & strip.
    sp_name = row["species"]
    if sp_name and _IRON_RULE_ZONE_RE.search(sp_name):
        row["note"] = (row["note"] + " [zone-mislabel-warning]").strip()
    target.append(row)


def _normalize_biozone_into(bz: dict[str, Any],
                            target: list[dict[str, Any]]) -> None:
    """Build a biozones row from a raw dict and append it.

    P0-4: enforces zone_type field. Infers zone_type from name keywords
    when not explicitly provided: assemblage, acme, lineage, interval, zonule,
    subzone, oppel, range zone.
    """
    def s(v):
        return "" if v is None else str(v)

    name = s(bz.get("name")).strip()
    # P0-4: infer zone_type from name keywords when not explicitly provided.
    inferred_zt = "biozone"
    nl = name.lower()
    if "assemblage" in nl or "ass." in nl:
        inferred_zt = "assemblage_zone"
    elif "acme" in nl:
        inferred_zt = "acme_zone"
    elif "lineage" in nl:
        inferred_zt = "lineage_zone"
    elif "interval" in nl:
        inferred_zt = "interval_zone"
    elif re.search(r"\bzonule\b", nl):
        inferred_zt = "zonule"
    elif re.search(r"\bsubzone\b", nl):
        inferred_zt = "subzone"
    elif "oppel" in nl:
        inferred_zt = "oppel_zone"
    elif "range zone" in nl or "taxon-range" in nl:
        inferred_zt = "range_zone"
    row = {
        "name": name,
        "section": s(bz.get("section", "")),
        "age": s(bz.get("age", "")),
        "thickness_m": s(bz.get("thickness_m", "")),
        "zone_type": s(bz.get("zone_type") or inferred_zt),
    }
    _carry_extras(bz, _KNOWN_BIOZONE_KEYS, row)
    target.append(row)


def normalize_result(parsed):
    """Coerce the parsed JSON into the strict result shape.

    H8: extra top-level / row-level keys the model emits are not silently
    discarded - they are attached under ``_extras`` so the operator sees
    what was extracted. This avoids losing data the caller assumes is
    captured by the schema.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to the
    appropriate keys (sections, species_ranges, biozones, other_fossils).
    """
    # MEDIUM fix: normalize_result crashes on non-dict input. Guard so callers don't get AttributeError.
    if not isinstance(parsed, dict):
        return {
            "sections": [],
            "species_ranges": [],
            "biozones": [],
            "other_fossils": [],
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }
    def s(v):
        return "" if v is None else str(v)

    out = {
        "sections": [],
        "species_ranges": [],
        "biozones": [],
        "other_fossils": [],
        "confidence": 0.0,
    }
    root_warnings = []

    # MEDIUM fix (truncated rescue): if the JSON parser rescued a partial
    # / inner object that does not match any of the documented range-chart
    # root keys, surface a warning so the operator is not silently given
    # an empty ok=True result.
    RANGE_CHART_ROOTS = {"sections", "species_ranges", "biozones",
                         "other_fossils", "confidence"}
    if (isinstance(parsed, dict) and parsed
            and not RANGE_CHART_ROOTS.intersection(parsed.keys())
            and "_array_root" not in parsed):
        root_warnings.append("truncated_or_unrecognized_payload")

    # H3-fix: unwrap _array_root wrapper and distribute items to known keys.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                # Non-dict items (e.g. bare strings) go to other_fossils.
                if isinstance(item, str) and item.strip():
                    out["other_fossils"].append(item.strip())
                continue
            key = _classify_array_item(item)
            if key is None:
                # Unclassifiable dicts: attach as top-level extras under
                # a generated key so nothing is silently dropped.
                out.setdefault("_unclassified", []).append(item)
            else:
                # MEDIUM fix: normalize the unwrapped item using the same
                # shape as the regular list, so string-instead-of-list
                # fields (formations, etc.) and missing keys are handled
                # uniformly.
                if key == "sections":
                    _normalize_section_into(item, out["sections"])
                elif key == "species_ranges":
                    _normalize_species_into(item, out["species_ranges"])
                elif key == "biozones":
                    _normalize_biozone_into(item, out["biozones"])
                else:
                    out.setdefault(key, []).append(item)

    # MEDIUM fix (dict-shaped arrays): if a named array is actually a dict
    # (single-object model emission), iterate the values instead of string
    # keys so the record is not silently lost. The wrapper key is preserved
    # under _extras.wrapper_key for traceability.
    def _coerce_list_or_dict(raw, kind):
        if isinstance(raw, dict):
            for wrapper_key, inner in raw.items():
                if not isinstance(inner, dict):
                    continue
                inner2 = dict(inner)
                # Fall back: when the inner record has no primary identifier
                # for its kind (no ``name`` for sections/biozones, no
                # ``species`` for species_ranges), use the wrapper key as the
                # primary identifier. This recovers the most common
                # dict-shaped payload (``{'Pingdingshan': {...}}``) without
                # losing the record.
                if kind in ("sections", "biozones") and not inner2.get("name"):
                    inner2["name"] = wrapper_key
                elif kind == "species_ranges" and not inner2.get("species"):
                    inner2["species"] = wrapper_key
                # Drop any pre-existing _extras before re-running the
                # normalizer so we don't end up with a nested ``_extras``
                # field. The normalizer will rebuild it cleanly with
                # ``wrapper_key`` preserved via the _carry_extras merge.
                inner2.pop("_extras", None)
                if kind == "sections":
                    _normalize_section_into(inner2, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(inner2, out["species_ranges"])
                elif kind == "biozones":
                    _normalize_biozone_into(inner2, out["biozones"])
                # After normalization, attach wrapper_key to the LAST
                # appended row. We can't put it on the source dict (it's a
                # 'known' marker the normalizer would treat as unknown-key
                # extras and re-attach), so we add it to the row directly.
                if out[kind]:
                    row = out[kind][-1]
                    extras = row.get("_extras")
                    if not isinstance(extras, dict):
                        extras = {}
                        row["_extras"] = extras
                    extras["wrapper_key"] = wrapper_key
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if kind == "sections":
                    _normalize_section_into(item, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(item, out["species_ranges"])
                elif kind == "biozones":
                    _normalize_biozone_into(item, out["biozones"])

    _coerce_list_or_dict(parsed.get("sections"), "sections")
    _coerce_list_or_dict(parsed.get("species_ranges"), "species_ranges")
    _coerce_list_or_dict(parsed.get("biozones"), "biozones")

    # MEDIUM fix (iron rule): post-normalize pass that flags any species
    # whose name reads like a zone label. We do not MOVE the row (which
    # would be silently destructive) - we flag it so the operator sees
    # the slip-through and decides.
    for sp in out["species_ranges"]:
        name = (sp.get("species") or "").strip()
        if name and _IRON_RULE_ZONE_RE.search(name):
            sp["_warning"] = "iron_rule_zone_label"
            if "iron_rule_zone_label" not in root_warnings:
                root_warnings.append("iron_rule_zone_label")

    of = parsed.get("other_fossils") or []
    # Fix B-3: handle case where model returns a string instead of a list.
    if isinstance(of, list):
        out["other_fossils"] = [s(x) for x in of if isinstance(x, str) and s(x).strip()]
    elif isinstance(of, str) and of.strip():
        out["other_fossils"] = [of.strip()]
    else:
        out["other_fossils"] = []
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))
    # LOW fix: drop _array_root (and its _note companion) from top-level
    # extras - the per-item rows are already distributed, so the raw
    # payload would be a duplicate.
    extras_src = {k: v for k, v in parsed.items()
                  if k not in _KNOWN_RANGE_CHART_KEYS}
    extras_src.pop("_array_root", None)
    extras_src.pop("_note", None)
    if extras_src:
        out["_extras"] = extras_src
    if root_warnings:
        out["_warnings"] = root_warnings
    return out


def extract_range_chart(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Range-chart extraction. Never raises.

    When ``provider`` is given, it drives the API format / auth / endpoint and
    the ``base_url`` / ``api_key`` / ``model`` kwargs are ignored. When None
    (legacy callers), an Anthropic-format provider is built from the kwargs so
    old behaviour is preserved byte-for-byte.

    FIX-BUG: all numeric parameters are clamped to valid ranges so that
    callers who bypass the GUI/CLI wrappers cannot pass absurd values.
    """
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    # FIX-BUG: clamp all numeric parameters to valid ranges
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    # P1-1 (REVIEW-2026-07-27): compute image fingerprint early, before any
    # processing, so clipboard paste / canvas-extracted / re-uploaded images
    # have a verifiable bytes-level identity even when source_path is absent.
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the geological information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    # LOW fix: honor the never-raises contract even when the provider has a
    # malformed extra_body / extra_headers (which raises TypeError/ValueError
    # inside llm.py's body.update / headers.update). Without this guard a
    # hand-edited providers.json would propagate out of extract_range_chart
    # and break the server's single-run path (server.py:606 has no try/except).
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=RANGE_CHART_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    # Truncation is partial-success — the model returned something but
    # the JSON may be cut off mid-structure. Surface as `warning` (and
    # keep `truncated=True`) so the frontend can show a clear "result
    # may be incomplete" banner without treating the call as a hard
    # failure. The previous version only set the boolean `truncated`
    # and left the operator to guess what to do about it.
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status_with_body(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_result(parsed)
    except Exception as exc:
        # Defensive: the ``extract_range_chart`` contract promises never to
        # raise, so an unexpected failure inside the normalizer (all of its
        # branches are guarded today, but future edits could regress) must be
        # surfaced as a hard error result rather than propagating up the stack
        # to a caller that assumes the promise holds (e.g. the server's
        # single-run path at server.py does not wrap extract() in try/except).
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            # Sprint B (REVIEW-2026-09-04): the fingerprint was computed but
            # dropped on this path, so failed runs could not be correlated
            # with their source image in history / provenance.
            image_sha256=image_sha256,
        )
    # MEDIUM fix: truncated VLM output rescued as an inner object produces
    # ok=True with empty arrays (no recognizable root keys). When the
    # normalizer surfaced a ``truncated_or_unrecognized_payload`` warning we
    # flip ok=False so the operator isn't silently given an empty extraction.
    if (data.get("_warnings")
            and "truncated_or_unrecognized_payload" in data["_warnings"]):
        return ExtractResult(
            ok=False, error_key="err.parse",
            raw=raw_text, truncated=truncated,
            latency_ms=latency_ms, usage=usage or {},
            warning=warning + " | rescued inner object: unusable",
            data=data,
            # Sprint B (REVIEW-2026-09-04): attach the image fingerprint here
            # too, same reason as the "normalize failed" path above.
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "range_chart", max_tokens, image_sha256,
            prompt_version_for_mode("range_chart"),
        ),
    )


def _error_from_status(status: int | None, err_body: str = "", latency_ms: int = 0,
                       *, image_sha256: str = "") -> ExtractResult:
    """Translate an HTTP status code into an ExtractResult.

    H6: when ``call_llm_api`` returns ``status=None`` it means the request
    never made it to a server (DNS, refused connection, timeout, etc.).
    Surface that as ``err.network`` rather than the generic ``err.http``
    so the user sees a meaningful diagnostic.
    H7: attach the upstream error body for 5xx debugging.
    ``latency_ms`` is the measured wall-clock time of the failed call so the
    Usage page can account for failed requests too.
    """
    if status is None:
        return ExtractResult(
            ok=False, error_key="err.network", status=None, error_body=err_body,
            latency_ms=latency_ms, image_sha256=image_sha256,
        )
    key = "err.http"
    if status == 401:
        key = "err.401"
    elif status == 403:
        key = "err.403"
    elif status == 429:
        key = "err.429"
    return ExtractResult(
        ok=False, error_key=key, status=status, error_body=err_body,
        latency_ms=latency_ms, image_sha256=image_sha256,
    )


# Backward-compat alias used in the success path. Today's code always calls
# the _with_body variant; keeping this name avoids renaming in every caller.
_error_from_status_with_body = _error_from_status


_KNOWN_COLUMNAR_SECTION_KEYS = (
    "id", "group", "lithology_blocks", "age_units", "samples",
    "coordinates_text", "thickness_m", "confidence_by_section",
)
_KNOWN_BLOCK_KEYS = ("pattern", "range_top_idx", "range_base_idx")
_KNOWN_UNIT_KEYS = ("label", "range_top_idx", "range_base_idx")
_KNOWN_SAMPLE_KEYS = ("bed_idx", "fossil_marker", "ref")
_KNOWN_LEGEND_KEYS = ("marker", "pattern", "meaning")
_KNOWN_CROSS_KEYS = ("from_section", "from_bed_idx", "to_section", "to_bed_idx")
_KNOWN_COLUMNAR_ROOT_KEYS = (
    "sections", "fossil_legend", "lithology_legend", "cross_beds",
    "overall_confidence", "confidence",
)


def normalize_columnar_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed columnar-section JSON into the strict result shape.

    H8: extra keys the model emits are preserved under ``_extras``.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to
    the appropriate keys (sections, fossil_legend, lithology_legend, cross_beds).
    """
    # H3-fix: unwrap _array_root wrapper and distribute items to known keys.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                continue
            # Columnar sections have "lithology_blocks" or "age_units" as
            # distinguishing features.
            if "lithology_blocks" in item or "age_units" in item or "id" in item:
                parsed.setdefault("sections", []).append(item)
            # Legend items have "marker" or "pattern" + "meaning".
            elif "meaning" in item and ("marker" in item or "pattern" in item):
                if "marker" in item:
                    parsed.setdefault("fossil_legend", []).append(item)
                else:
                    parsed.setdefault("lithology_legend", []).append(item)
            # Cross-bed entries have from_section/to_section.
            elif "from_section" in item or "from_bed_idx" in item:
                parsed.setdefault("cross_beds", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        return "" if v is None else str(v)

    def fi(v: Any) -> tuple[int | None, bool]:
        """Convert a bed-index field to ``(int | None, lossy)``.

        LOW fix: previously a numeric string like ``"8.0"`` or a float
        ``8.5`` was silently nulled (resp. floored to 8) without warning.

        Sprint B (REVIEW-2026-09-04): the docstring promised a row-level
        ``_warning`` for the float→int truncation path, but the flag was
        never surfaced. ``fi`` now returns a second element: ``True`` when
        the value had to go through float coercion (``"8.5"`` -> 8,
        ``8.5`` -> 8, and any numeric string ``int()`` rejects), and the
        callers below attach a row-level ``_warning`` so the operator sees
        a flagged value instead of a silent data loss.

        Return contract:
          ``(None, False)``   unparseable / empty / bool input
          ``(v, False)``      clean int conversion (int or int-string)
          ``(int(v), True)``  lossy truncation path taken
        """
        if v is None or v == "":
            return None, False
        if isinstance(v, bool):
            # bool is an int subclass — treat True/False as 1/0 is
            # surprising; return None instead so the caller can flag it.
            return None, False
        if isinstance(v, int):
            return v, False
        if isinstance(v, float):
            # Sprint B (REVIEW-2026-09-04): int() on a float TRUNCATES
            # silently (8.5 -> 8, and 9.5 -> 9) without ever raising, so
            # the old code's float fallback below never saw floats. Flag
            # every float as the lossy path it is.
            try:
                return int(v), True
            except (TypeError, ValueError, OverflowError):
                return None, False
        try:
            return int(v), False
        except (TypeError, ValueError):
            pass
        # Fall back to float coercion (handles "8.5" -> 8, "8.0" -> 8).
        # This is the lossy path — signal it to the caller.
        try:
            return int(float(v)), True
        except (TypeError, ValueError):
            return None, False

    def norm_blocks(items):
        out = []
        for b in items or []:
            if not isinstance(b, dict):
                continue
            raw_top = b.get("range_top_idx")
            raw_base = b.get("range_base_idx")
            top_idx, top_lossy = fi(raw_top)
            base_idx, base_lossy = fi(raw_base)
            # B-3 fix: enforce top (younger/higher) >= base (older/lower).
            # The prompt says "1-indexed from bottom (oldest=1), top >= base".
            # If the model emitted them reversed, swap and flag so the UI
            # can surface a warning without discarding the data.
            swapped = False
            if top_idx is not None and base_idx is not None and top_idx < base_idx:
                top_idx, base_idx = base_idx, top_idx
                swapped = True
            row = {
                "pattern": s(b.get("pattern")),
                "range_top_idx": top_idx,
                "range_base_idx": base_idx,
            }
            warnings: list[str] = []
            # LOW fix: flag when fi() silently coerced (numeric-string or
            # float) so the operator can audit the conversion instead of
            # seeing a clean None or floored integer.
            # Sprint B (REVIEW-2026-09-04): fi() also reports the lossy
            # float-truncation path ("8.5" -> 8) — surface it here as the
            # row-level ``_warning`` the fi() docstring always promised.
            if top_idx is None and raw_top not in (None, ""):
                warnings.append("range_top_idx_unparseable")
            elif top_lossy:
                warnings.append("range_top_idx_truncated")
            if base_idx is None and raw_base not in (None, ""):
                warnings.append("range_base_idx_unparseable")
            elif base_lossy:
                warnings.append("range_base_idx_truncated")
            if swapped:
                warnings.append("index_order_swap")
            if warnings:
                row["_warning"] = warnings[0] if len(warnings) == 1 else warnings
            _carry_extras(b, _KNOWN_BLOCK_KEYS, row)
            out.append(row)
        return out

    def norm_units(items):
        out = []
        for u in items or []:
            if not isinstance(u, dict):
                continue
            top_idx, top_lossy = fi(u.get("range_top_idx"))
            base_idx, base_lossy = fi(u.get("range_base_idx"))
            # B-3 fix: same swap-logic for age_units.
            swapped = False
            if top_idx is not None and base_idx is not None and top_idx < base_idx:
                top_idx, base_idx = base_idx, top_idx
                swapped = True
            row = {
                "label": s(u.get("label")),
                "range_top_idx": top_idx,
                "range_base_idx": base_idx,
            }
            # Sprint B (REVIEW-2026-09-04): mirror norm_blocks — report the
            # lossy float-truncation path via row-level ``_warning``.
            warnings: list[str] = []
            if swapped:
                warnings.append("index_order_swap")
            if top_lossy:
                warnings.append("range_top_idx_truncated")
            if base_lossy:
                warnings.append("range_base_idx_truncated")
            if warnings:
                row["_warning"] = warnings[0] if len(warnings) == 1 else warnings
            _carry_extras(u, _KNOWN_UNIT_KEYS, row)
            out.append(row)
        return out

    def norm_samples(items):
        out = []
        for s_item in items or []:
            if not isinstance(s_item, dict):
                continue
            bed_idx, bed_lossy = fi(s_item.get("bed_idx"))
            row = {
                "bed_idx": bed_idx,
                "fossil_marker": s(s_item.get("fossil_marker")),
                "ref": s(s_item.get("ref")),
            }
            # Sprint B (REVIEW-2026-09-04): lossy float truncation must be
            # visible here too (row-level ``_warning`` per the fi() contract).
            if bed_lossy:
                row["_warning"] = "bed_idx_truncated"
            _carry_extras(s_item, _KNOWN_SAMPLE_KEYS, row)
            out.append(row)
        return out

    def norm_legend(items):
        out = []
        warning = None
        # Fix B-5: if items is a string (not a list), don't iterate over chars.
        # Issue-1 fix: add _warning flag instead of silently discarding.
        if isinstance(items, str):
            warning = "legend_input_is_string"
            items = []
        for x in items or []:
            # Fix B-5: skip non-dict items (including strings) explicitly.
            # Previously strings would be silently skipped; now we explicitly
            # check and skip non-dict items without iterating over them.
            if not isinstance(x, dict):
                continue
            # fossil_legend uses marker+meaning; lithology_legend uses
            # pattern+meaning. Carry both so neither legend's primary
            # column is silently dropped into _extras (which the exporter
            # never reads) and rendered blank.
            row = {
                "marker": s(x.get("marker")),
                "pattern": s(x.get("pattern")),
                "meaning": s(x.get("meaning")),
            }
            _carry_extras(x, _KNOWN_LEGEND_KEYS, row)
            out.append(row)
        return out, warning

    def norm_cross(items):
        out = []
        for x in items or []:
            if not isinstance(x, dict):
                continue
            from_bed_idx, from_lossy = fi(x.get("from_bed_idx"))
            to_bed_idx, to_lossy = fi(x.get("to_bed_idx"))
            row = {
                "from_section": s(x.get("from_section")),
                "from_bed_idx": from_bed_idx,
                "to_section": s(x.get("to_section")),
                "to_bed_idx": to_bed_idx,
            }
            # Sprint B (REVIEW-2026-09-04): lossy float truncation warnings.
            trunc = [name for name, lossy in (
                ("from_bed_idx_truncated", from_lossy),
                ("to_bed_idx_truncated", to_lossy),
            ) if lossy]
            if trunc:
                row["_warning"] = trunc[0] if len(trunc) == 1 else trunc
            _carry_extras(x, _KNOWN_CROSS_KEYS, row)
            out.append(row)
        return out

    sections = []
    for sec in parsed.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        try:
            conf_v = float(sec.get("confidence_by_section", 0.0))
        except (TypeError, ValueError):
            conf_v = 0.0
        row = {
            "id": s(sec.get("id")),
            "group": s(sec.get("group")),
            "lithology_blocks": norm_blocks(sec.get("lithology_blocks")),
            "age_units": norm_units(sec.get("age_units")),
            "samples": norm_samples(sec.get("samples")),
            "coordinates_text": s(sec.get("coordinates_text")),
            "thickness_m": s(sec.get("thickness_m")),
            "confidence_by_section": max(0.0, min(1.0, conf_v)),
        }
        _carry_extras(sec, _KNOWN_COLUMNAR_SECTION_KEYS, row)
        sections.append(row)

    try:
        # Models sometimes emit `confidence` at the root instead of the
        # documented `overall_confidence`; fall back so the value isn't
        # silently zeroed (which would also distort aggregate's mean).
        overall = float(parsed.get("overall_confidence", parsed.get("confidence", 0.0)))
    except (TypeError, ValueError):
        overall = 0.0
    overall = max(0.0, min(1.0, overall))

    # Issue-1 fix: unpack tuple return from norm_legend (returns (list, warning))
    fossil_legend, fossil_legend_warn = norm_legend(parsed.get("fossil_legend"))
    lithology_legend, lithology_legend_warn = norm_legend(parsed.get("lithology_legend"))
    # Collect warnings to surface at root level
    legend_warnings = [w for w in (fossil_legend_warn, lithology_legend_warn) if w]

    out: dict[str, Any] = {
        "sections": sections,
        "fossil_legend": fossil_legend,
        "lithology_legend": lithology_legend,
        "cross_beds": norm_cross(parsed.get("cross_beds")),
        "confidence": overall,
    }
    if legend_warnings:
        out["_warnings"] = legend_warnings
    root_extras = {k: v for k, v in parsed.items() if k not in _KNOWN_COLUMNAR_ROOT_KEYS}
    if root_extras:
        out["_extras"] = root_extras
    return out


def extract_columnar_section(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Columnar-section extraction. Same contract as extract_range_chart."""
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    # FIX-BUG: clamp all numeric parameters to valid ranges
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    # P1-1 (REVIEW-2026-07-27): mandatory image fingerprint.
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the columnar-section information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    # LOW fix: never-raises contract - guard call_llm_api against malformed
    # provider config (extra_body / extra_headers may not be dicts).
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=COLUMNAR_SECTION_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_columnar_result(parsed)
    except Exception as exc:
        # Never-raises contract (see extract_range_chart for rationale).
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "columnar_section", max_tokens, image_sha256,
            prompt_version_for_mode("columnar_section"),
        ),
    )


# Dispatch table — single entry point for both modes.
_MODE_DISPATCH = {
    "range_chart": extract_range_chart,
    "columnar_section": extract_columnar_section,
    "abundance_diagram": None,  # bound below after the function is defined
    "phylogenetic_tree": None,  # bound below after the function is defined
}


_KNOWN_ABUNDANCE_ROOT_KEYS = ("sites", "abundances", "zones", "confidence")
_KNOWN_SITE_KEYS = ("name", "location", "age_range", "depth_unit")
_KNOWN_ABUNDANCE_KEYS = (
    "taxon", "site", "level", "depth", "abundance", "abundance_unit",
)
_KNOWN_ZONE_KEYS = ("name", "age", "level_range")


def normalize_abundance_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed abundance-diagram JSON into the strict result shape.

    H8: extra keys the model emits are preserved under ``_extras``. The shape
    mirrors range-chart (all-string rows) so the majority-vote merge machinery
    in aggregate.py works with no new code path.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to
    the appropriate keys (sites, abundances, zones).
    """
    # H3-fix: unwrap _array_root wrapper and distribute items to known keys.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                continue
            # Sites have "name" and typically "location" or "depth_unit".
            if "name" in item and ("location" in item or "depth_unit" in item or "age_range" in item):
                parsed.setdefault("sites", []).append(item)
            # Abundances have "taxon", "site", "level", "depth", "abundance".
            elif "taxon" in item or ("abundance" in item and "level" in item):
                parsed.setdefault("abundances", []).append(item)
            # Zones have "name" and "age" (and typically "level_range").
            elif "age" in item and "name" in item:
                parsed.setdefault("zones", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        return "" if v is None else str(v)

    out: dict[str, Any] = {
        "sites": [],
        "abundances": [],
        "zones": [],
        "confidence": 0.0,
    }
    for site in (parsed.get("sites") if isinstance(parsed.get("sites"), list) else []):
        if not isinstance(site, dict):
            continue
        row = {
            "name": s(site.get("name")),
            "location": s(site.get("location")),
            "age_range": s(site.get("age_range")),
            "depth_unit": s(site.get("depth_unit")),
        }
        _carry_extras(site, _KNOWN_SITE_KEYS, row)
        out["sites"].append(row)
    for ab in (parsed.get("abundances") if isinstance(parsed.get("abundances"), list) else []):
        if not isinstance(ab, dict):
            continue
        row = {
            "taxon": s(ab.get("taxon")),
            "site": s(ab.get("site")),
            "level": s(ab.get("level")),
            "depth": s(ab.get("depth")),
            "abundance": s(ab.get("abundance")),
            "abundance_unit": s(ab.get("abundance_unit")),
        }
        _carry_extras(ab, _KNOWN_ABUNDANCE_KEYS, row)
        out["abundances"].append(row)
    for z in (parsed.get("zones") if isinstance(parsed.get("zones"), list) else []):
        if not isinstance(z, dict):
            continue
        row = {
            "name": s(z.get("name")),
            "age": s(z.get("age")),
            "level_range": s(z.get("level_range")),
        }
        _carry_extras(z, _KNOWN_ZONE_KEYS, row)
        out["zones"].append(row)
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))
    top_extras = {k: v for k, v in parsed.items() if k not in _KNOWN_ABUNDANCE_ROOT_KEYS}
    if top_extras:
        out["_extras"] = top_extras
    return out


def extract_abundance_diagram(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Abundance-diagram extraction. Same contract as extract_range_chart."""
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    # FIX-BUG: clamp all numeric parameters to valid ranges
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    # P1-1 (REVIEW-2026-07-27): mandatory image fingerprint.
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the abundance-diagram information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    # LOW fix: never-raises contract - guard call_llm_api against malformed
    # provider config (extra_body / extra_headers may not be dicts).
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_abundance_result(parsed)
    except Exception as exc:
        # Never-raises contract (see extract_range_chart for rationale).
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "abundance_diagram", max_tokens, image_sha256,
            prompt_version_for_mode("abundance_diagram"),
        ),
    )


def _normalize_phylogenetic_tree_into(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a phylogenetic tree response.

    Schema:
      {
        "metadata": {
          "title": str,
          "extraction_timestamp": str,  # ISO 8601
          "tree_type": str,  # "cladogram" | "phylogram" | "dendrogram"
          "scale": str,  # e.g. "Ma" / "substitutions/site"
          "rooted": bool,
          "source": str
        },
        "root_ids": list[str],
        "nodes": [{
          "id": str,
          "parent": str | None,  # null for roots
          "name": str,           # taxon or internal node label
          "is_leaf": bool,
          "branch_length": float | None,
          "node_age_ma": float | None,  # optional, for time-calibrated trees
          "support": float | None,      # bootstrap 0..100
          "metadata": dict  # any extra fields (preserved)
        }],
        "confidence": float
      }

    Invariants enforced:
      - root_ids is non-empty
      - Every non-root node's parent is in node ids
      - Each root's parent is None (not just the first)
      - is_leaf == (children_count == 0) by reverse check
      - support in [0, 100] or None
    """
    # H3-fix pattern: unwrap _array_root wrapper (top-level array from
    # safe_json_loads wrapped as {_array_root:[...]}).
    if "_array_root" in raw and isinstance(raw["_array_root"], list):
        for item in raw["_array_root"]:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("nodes"), list):
                raw = item
                break

    def s(v):
        return "" if v is None else str(v)

    def fv(v):
        """Coerce to float or None."""
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    nodes_in = raw.get("nodes") or []
    if not isinstance(nodes_in, list):
        nodes_in = []

    # Build id→node lookup and compute children counts (reverse check for is_leaf).
    id_to_node: dict[str, dict[str, Any]] = {}
    children_count: dict[str, int] = {}
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if nid:
            id_to_node[nid] = n
            children_count[nid] = 0

    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        parent = n.get("parent")
        if parent is not None:
            pid = str(parent)
            if pid in children_count:
                children_count[pid] = children_count.get(pid, 0) + 1

    # Sprint B (REVIEW-2026-09-04): normalise root_ids to strings ONCE, up
    # front. Previously one check used str(rid) while the membership test
    # below compared a str node id against the RAW list — so
    # {"root_ids": [1], "nodes": [{"id": "1", "parent": null, ...}]}
    # mis-classified its only root as a non-root node and raised
    # "Non-root node 1 must have a parent".
    root_ids = [str(r) for r in (raw.get("root_ids") or [])]
    if not root_ids:
        raise ValueError("root_ids is empty")

    # Validate all root_ids reference actual nodes.
    for rid in root_ids:
        if rid not in id_to_node:
            raise ValueError(f"root_ids contains unknown node id: {rid}")

    nodes_out = []
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if not nid:
            continue

        parent_val = n.get("parent")

        # Invariant: non-root nodes must have a parent in node ids.
        if nid not in root_ids:
            if parent_val is None:
                raise ValueError(f"Non-root node {nid} must have a parent")
            if str(parent_val) not in id_to_node:
                raise ValueError(f"Node {nid} references parent {parent_val} not in node ids")
        else:
            # Root nodes must have parent == None.
            if parent_val is not None:
                raise ValueError(f"Root node {nid} must have parent == None, got {parent_val}")

        # Reverse-check is_leaf: a node is_leaf iff it has no children.
        # Sprint B (REVIEW-2026-09-04): the correction used to be written to
        # a dead local (``extra_flag``) that was never read, so the flag
        # vanished and the audit trail was lost. The corrected value is now
        # marked on the row via its ``metadata`` dict — the same channel the
        # unknown-key extras flow through — so downstream consumers can see
        # that the model's is_leaf claim was overridden.
        is_leaf_input = bool(n.get("is_leaf"))
        actual_is_leaf = children_count.get(nid, 0) == 0
        leaf_corrected = is_leaf_input != actual_is_leaf
        is_leaf = actual_is_leaf

        support_raw = n.get("support")
        support = fv(support_raw)
        if support is not None and not (0.0 <= support <= 100.0):
            raise ValueError(f"support must be in [0, 100] or None, got {support}")

        row = {
            "id": nid,
            "parent": str(parent_val) if parent_val is not None else None,
            "name": s(n.get("name")),
            "is_leaf": is_leaf,
            "branch_length": fv(n.get("branch_length")),
            "node_age_ma": fv(n.get("node_age_ma")),
            "support": support,
        }
        # Carry extras for unknown keys (preserve depth_range_m, sequence_count,
        # support_confidence, depth_confidence, etc.)
        extras = {k: v for k, v in n.items()
                   if k not in ("id", "parent", "name", "is_leaf",
                                "branch_length", "node_age_ma", "support")}
        if leaf_corrected:
            extras["_is_leaf_corrected"] = True
        if extras:
            row["metadata"] = extras
        nodes_out.append(row)

    metadata_raw = raw.get("metadata") or {}
    # Preserve raw fields not in the new schema (taxon_group, root_name,
    # total_nodes) so existing tests and downstream consumers that read
    # those fields continue to work.
    NEW_META_KEYS = {"title", "extraction_timestamp", "tree_type", "scale",
                     "rooted", "source"}
    metadata: dict[str, Any] = {
        "title": s(metadata_raw.get("title", "")),
        "extraction_timestamp": s(metadata_raw.get("extraction_timestamp", "")),
        "tree_type": s(metadata_raw.get("tree_type", "")),
        "scale": s(metadata_raw.get("scale", "")),
        "rooted": bool(metadata_raw.get("rooted", True)),
        "source": s(metadata_raw.get("source", metadata_raw.get("image_source", ""))),
    }
    for k, v in metadata_raw.items():
        if k not in NEW_META_KEYS:
            metadata[k] = v

    legend_raw = raw.get("legend")
    legend = dict(legend_raw) if isinstance(legend_raw, dict) else {}

    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    root_extras = {k: v for k, v in raw.items()
                   if k not in ("metadata", "nodes", "root_ids", "legend",
                                "confidence", "_array_root")}
    out: dict[str, Any] = {
        "metadata": metadata,
        # root_ids is already str-normalised up front (Sprint B REVIEW-2026-09-04).
        "root_ids": root_ids,
        "nodes": nodes_out,
        "legend": legend,
        "confidence": conf,
    }
    if root_extras:
        out["_extras"] = root_extras
    return out


# Public alias.
normalize_phylogenetic_tree_result = _normalize_phylogenetic_tree_into


def _quote_newick_label(label):
    """Return a Newick-safe representation of ``label``.

    Newick tokens that need quoting: ``( ) [ ] ; ,``. We also quote
    when the label contains whitespace, a leading/trailing space,
    or a literal ``:`` (which would otherwise be ambiguous with the
    ``name:branch_length`` separator — the parser splits on the
    FIRST ``:`` and treats anything after as the branch length, so
    without quoting ``"A:B"`` becomes the tuple ``(name=A,
    branch_length="B")``). Embedded single quotes are doubled per
    the Newick format spec.
    """
    if not label:
        return "''"
    needs_quote = any(ch in label for ch in '(),[];:')
    if not needs_quote and label.strip() == label and ' ' not in label:
        return label
    return "'" + label.replace("'", "''") + "'"

def _build_newick_node(node_id: str, id_to_children: dict[str, list[str]],
                       nodes_dict: dict[str, dict[str, Any]]) -> str:
    """Recursively build the Newick subtree for ``node_id``."""
    children = id_to_children.get(node_id, [])
    if not children:
        # Leaf: name:branch_length
        n = nodes_dict.get(node_id, {})
        name = n.get("name", "")
        bl = n.get("branch_length")
        bl_str = f":{bl}" if bl is not None else ""
        # Phase M fix: the previous escaping replaced `(`, `)`, `:`
        # with `_`, which loses semantic information (e.g. "(A)"
        # becomes "_A_"). Use the Newick-standard single-quote
        # quoting: wrap the name in single quotes when it contains
        # any of the structural characters, with embedded single
        # quotes doubled per the Newick spec.
        safe_name = _quote_newick_label(name)
        return f"{safe_name}{bl_str}"
    else:
        # Internal node: (children)support:branch_length
        child_parts = [_build_newick_node(cid, id_to_children, nodes_dict) for cid in children]
        support = nodes_dict.get(node_id, {}).get("support")
        support_str = f"{support}" if support is not None else ""
        bl = nodes_dict.get(node_id, {}).get("branch_length")
        bl_str = f":{bl}" if bl is not None else ""
        child_newick = ",".join(child_parts)
        return f"({child_newick}){support_str}{bl_str}"


def to_newick(tree: dict[str, Any]) -> str:
    """Convert a normalized phylogenetic tree to Newick format.

    Newick spec: ((A:0.1,B:0.2)95:0.5,C:0.3);
    - Internal nodes: (children)support:branch_length
    - Leaf nodes: name:branch_length
    - support omitted if None; branch_length omitted if None
    - Multiple roots joined by commas (forest) at top level

    Sprint B (REVIEW-2026-09-04): nodes unreachable from any root (dangling
    parent chain, e.g. a node whose parent is not itself rooted) are still
    omitted from the output, but the drop is no longer silent — a
    ``RuntimeWarning`` is emitted naming the dropped ids, following the
    module's user-visible warning convention. Use ``warnings.catch_warnings``
    in callers that want to treat it as an error.
    """
    nodes = tree.get("nodes") or []
    root_ids = tree.get("root_ids") or []

    # Build id→dict lookup and parent→children map.
    nodes_dict: dict[str, dict[str, Any]] = {}
    for n in nodes:
        if isinstance(n, dict):
            nid = str(n.get("id") or "")
            if nid:
                nodes_dict[nid] = n

    id_to_children: dict[str, list[str]] = {rid: [] for rid in root_ids}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        pid = n.get("parent")
        if pid is not None:
            pid_str = str(pid)
            if pid_str not in id_to_children:
                id_to_children[pid_str] = []
            id_to_children[pid_str].append(str(n.get("id") or ""))

    # Sprint B (REVIEW-2026-09-04): detect nodes unreachable from any root
    # BEFORE serializing, so the silent data loss becomes a visible warning.
    reachable: set[str] = set()
    stack = [rid for rid in root_ids if rid in nodes_dict]
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(id_to_children.get(cur, []))
    unreachable = sorted(nid for nid in nodes_dict if nid not in reachable)
    if unreachable:
        warnings.warn(
            f"to_newick: {len(unreachable)} node(s) unreachable from "
            f"root_ids {list(root_ids)} were dropped from the Newick "
            f"output: {unreachable}",
            RuntimeWarning,
            stacklevel=2,
        )

    parts = [_build_newick_node(rid, id_to_children, nodes_dict) for rid in root_ids]
    return "({});".format(",".join(parts))


def extract_phylogenetic_tree(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Phylogenetic-tree extraction. Same contract as extract_range_chart."""
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    # FIX-BUG: clamp all numeric parameters to valid ranges
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    # P1-1 (REVIEW-2026-07-27): mandatory image fingerprint.
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the phylogenetic-tree information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    # Never-raises contract: guard call_llm_api against malformed provider
    # config (extra_body / extra_headers may not be dicts).
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=PHYLOGENETIC_TREE_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    # Never-raises contract: a defensive guard so any future regression in
    # downstream normalization (or unexpected type from the model) cannot
    # propagate up to a caller that relies on the promise.
    if not isinstance(parsed, dict):
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, usage=usage or {},
            latency_ms=latency_ms,
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = _normalize_phylogenetic_tree_into(parsed)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "phylogenetic_tree", max_tokens, image_sha256,
            prompt_version_for_mode("phylogenetic_tree"),
        ),
    )


# ============================================================================
# NEW CHART TYPES: Chemical Stratigraphy
# ============================================================================

_KNOWN_CHEMICAL_STRAT_ROOT_KEYS = ("metadata", "data_points", "events", "intervals", "confidence")
_KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS = (
    "sample_id", "depth_m", "age_ma", "stage", "values", "lithology", "fossil_horizon", "note"
)
_KNOWN_CHEMICAL_STRAT_EVENT_KEYS = ("type", "depth_m", "age_ma", "name", "magnitude", "description")
_KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS = (
    "name", "top_depth_m", "base_depth_m", "top_age_ma", "base_age_ma",
    "characteristic_values", "lithology"
)


def normalize_chemical_stratigraphy_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed chemical stratigraphy JSON into the strict result shape.

    FIX-NEW: new chart type for isotopic curves, elemental data.
    H8: extra keys the model emits are preserved under ``_extras``.
    """
    if not isinstance(parsed, dict):
        return {
            "metadata": {},
            "data_points": [],
            "events": [],
            "intervals": [],
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }

    def s(v):
        return "" if v is None else str(v)

    out = {
        "metadata": {},
        "data_points": [],
        "events": [],
        "intervals": [],
        "confidence": 0.0,
    }

    # Normalize metadata
    meta = parsed.get("metadata") or {}
    if isinstance(meta, dict):
        out["metadata"] = {
            "section_name": s(meta.get("section_name", "")),
            "location": s(meta.get("location", "")),
            "latitude": s(meta.get("latitude", "")),
            "longitude": s(meta.get("longitude", "")),
            "age_range": s(meta.get("age_range", "")),
            "curve_types": meta.get("curve_types") if isinstance(meta.get("curve_types"), list) else [],
        }

    # Normalize data_points
    for pt in parsed.get("data_points") or []:
        if not isinstance(pt, dict):
            continue
        values_raw = pt.get("values") or {}
        values = {}
        if isinstance(values_raw, dict):
            for k, v in values_raw.items():
                values[str(k)] = s(v)
        row = {
            "sample_id": s(pt.get("sample_id")),
            "depth_m": s(pt.get("depth_m")),
            "age_ma": s(pt.get("age_ma")),
            "stage": s(pt.get("stage")),
            "values": values,
            "lithology": s(pt.get("lithology")),
            "fossil_horizon": s(pt.get("fossil_horizon")),
            "note": s(pt.get("note")),
        }
        _carry_extras(pt, _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS, row)
        out["data_points"].append(row)

    # Normalize events
    for ev in parsed.get("events") or []:
        if not isinstance(ev, dict):
            continue
        row = {
            "type": s(ev.get("type")),
            "depth_m": s(ev.get("depth_m")),
            "age_ma": s(ev.get("age_ma")),
            "name": s(ev.get("name")),
            "magnitude": s(ev.get("magnitude")),
            "description": s(ev.get("description")),
        }
        _carry_extras(ev, _KNOWN_CHEMICAL_STRAT_EVENT_KEYS, row)
        out["events"].append(row)

    # Normalize intervals
    for iv in parsed.get("intervals") or []:
        if not isinstance(iv, dict):
            continue
        row = {
            "name": s(iv.get("name")),
            "top_depth_m": s(iv.get("top_depth_m")),
            "base_depth_m": s(iv.get("base_depth_m")),
            "top_age_ma": s(iv.get("top_age_ma")),
            "base_age_ma": s(iv.get("base_age_ma")),
            "characteristic_values": s(iv.get("characteristic_values")),
            "lithology": s(iv.get("lithology")),
        }
        _carry_extras(iv, _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS, row)
        out["intervals"].append(row)

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))

    extras_src = {k: v for k, v in parsed.items() if k not in _KNOWN_CHEMICAL_STRAT_ROOT_KEYS}
    if extras_src:
        out["_extras"] = extras_src
    return out


def extract_chemical_stratigraphy(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Chemical stratigraphy extraction. Same contract as extract_range_chart."""
    from .prompt import CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT

    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the chemical stratigraphy information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_chemical_stratigraphy_result(parsed)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "chemical_stratigraphy", max_tokens, image_sha256,
            prompt_version_for_mode("chemical_stratigraphy"),
        ),
    )


# ============================================================================
# NEW CHART TYPES: Paleogeographic Map
# ============================================================================

_KNOWN_PALEOMAP_ROOT_KEYS = (
    "metadata", "continents", "oceans_seas", "tectonic_features",
    "biogeographic_realms", "fossil_sites", "paleolatitude_indicators", "confidence"
)

# Sprint B (REVIEW-2026-09-04): per-row known keys so the H8 ``_carry_extras``
# contract holds for sub-rows too — previously any extra key the model emitted
# inside a continents/oceans_seas/... row was silently discarded.
_KNOWN_PALEOMAP_CONTINENT_KEYS = ("name", "type", "coordinates", "paleolatitude", "note")
_KNOWN_PALEOMAP_SEA_KEYS = ("name", "type", "coordinates", "note")
_KNOWN_PALEOMAP_TECTONIC_KEYS = ("name", "type", "coordinates", "direction", "description")
_KNOWN_PALEOMAP_REALM_KEYS = ("name", "type", "coordinates", "characteristic_fauna")
_KNOWN_PALEOMAP_SITE_KEYS = ("name", "lat_lon", "age", "fossils", "marker_type")
_KNOWN_PALEOMAP_INDICATOR_KEYS = ("type", "coordinates")


def normalize_paleomap_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed paleogeographic map JSON into the strict result shape.

    FIX-NEW: new chart type for paleogeographic maps.
    """
    if not isinstance(parsed, dict):
        return {
            "metadata": {},
            "continents": [],
            "oceans_seas": [],
            "tectonic_features": [],
            "biogeographic_realms": [],
            "fossil_sites": [],
            "paleolatitude_indicators": [],
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }

    def s(v):
        return "" if v is None else str(v)

    out = {
        "metadata": {},
        "continents": [],
        "oceans_seas": [],
        "tectonic_features": [],
        "biogeographic_realms": [],
        "fossil_sites": [],
        "paleolatitude_indicators": [],
        "confidence": 0.0,
    }

    # Normalize metadata
    meta = parsed.get("metadata") or {}
    if isinstance(meta, dict):
        out["metadata"] = {
            "time_slice": s(meta.get("time_slice", "")),
            "approximate_age_ma": s(meta.get("approximate_age_ma", "")),
            "map_title": s(meta.get("map_title", "")),
            "projection": s(meta.get("projection", "")),
            "scale": s(meta.get("scale", "")),
            "source": s(meta.get("source", "")),
        }

    def norm_coords(val):
        """Normalize coordinates to list of [lat, lon] pairs."""
        if isinstance(val, list):
            result = []
            for item in val:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    try:
                        result.append([float(item[0]), float(item[1])])
                    except (TypeError, ValueError):
                        result.append([s(item[0]), s(item[1])])
            return result
        return []

    # Normalize continents
    for cont in parsed.get("continents") or []:
        if not isinstance(cont, dict):
            continue
        row = {
            "name": s(cont.get("name")),
            "type": s(cont.get("type")),
            "coordinates": norm_coords(cont.get("coordinates")),
            "paleolatitude": s(cont.get("paleolatitude")),
            "note": s(cont.get("note")),
        }
        # H8 (Sprint B REVIEW-2026-09-04): preserve unknown row keys.
        _carry_extras(cont, _KNOWN_PALEOMAP_CONTINENT_KEYS, row)
        out["continents"].append(row)

    # Normalize oceans/seas
    for sea in parsed.get("oceans_seas") or []:
        if not isinstance(sea, dict):
            continue
        row = {
            "name": s(sea.get("name")),
            "type": s(sea.get("type")),
            "coordinates": norm_coords(sea.get("coordinates")),
            "note": s(sea.get("note")),
        }
        _carry_extras(sea, _KNOWN_PALEOMAP_SEA_KEYS, row)
        out["oceans_seas"].append(row)

    # Normalize tectonic features
    for feat in parsed.get("tectonic_features") or []:
        if not isinstance(feat, dict):
            continue
        row = {
            "name": s(feat.get("name")),
            "type": s(feat.get("type")),
            "coordinates": norm_coords(feat.get("coordinates")),
            "direction": s(feat.get("direction")),
            "description": s(feat.get("description")),
        }
        _carry_extras(feat, _KNOWN_PALEOMAP_TECTONIC_KEYS, row)
        out["tectonic_features"].append(row)

    # Normalize biogeographic realms
    for realm in parsed.get("biogeographic_realms") or []:
        if not isinstance(realm, dict):
            continue
        row = {
            "name": s(realm.get("name")),
            "type": s(realm.get("type")),
            "coordinates": norm_coords(realm.get("coordinates")),
            "characteristic_fauna": s(realm.get("characteristic_fauna")),
        }
        _carry_extras(realm, _KNOWN_PALEOMAP_REALM_KEYS, row)
        out["biogeographic_realms"].append(row)

    # Normalize fossil sites
    for site in parsed.get("fossil_sites") or []:
        if not isinstance(site, dict):
            continue
        row = {
            "name": s(site.get("name")),
            "lat_lon": s(site.get("lat_lon")),
            "age": s(site.get("age")),
            "fossils": s(site.get("fossils")),
            "marker_type": s(site.get("marker_type")),
        }
        _carry_extras(site, _KNOWN_PALEOMAP_SITE_KEYS, row)
        out["fossil_sites"].append(row)

    # Normalize paleolatitude indicators
    for ind in parsed.get("paleolatitude_indicators") or []:
        if not isinstance(ind, dict):
            continue
        row = {
            "type": s(ind.get("type")),
            "coordinates": norm_coords(ind.get("coordinates")),
        }
        _carry_extras(ind, _KNOWN_PALEOMAP_INDICATOR_KEYS, row)
        out["paleolatitude_indicators"].append(row)

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))

    extras_src = {k: v for k, v in parsed.items() if k not in _KNOWN_PALEOMAP_ROOT_KEYS}
    if extras_src:
        out["_extras"] = extras_src
    return out


def extract_paleomap(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Paleogeographic map extraction. Same contract as extract_range_chart."""
    from .prompt import PALEOMAP_SYSTEM_PROMPT

    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the paleogeographic map information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=PALEOMAP_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_paleomap_result(parsed)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "paleomap", max_tokens, image_sha256,
            prompt_version_for_mode("paleomap"),
        ),
    )


# ============================================================================
# NEW CHART TYPES: Scatter Plot / Biplot
# ============================================================================

_KNOWN_SCATTER_PLOT_ROOT_KEYS = (
    "metadata", "groups", "points", "outliers", "statistics", "confidence"
)

# Sprint B (REVIEW-2026-09-04): per-row known keys for the H8 ``_carry_extras``
# contract on scatter sub-rows (groups / points / outliers).
_KNOWN_SCATTER_GROUP_KEYS = ("name", "color", "marker", "n_points_visible", "description")
_KNOWN_SCATTER_POINT_KEYS = ("x", "y", "z", "group", "label", "note")
_KNOWN_SCATTER_OUTLIER_KEYS = ("x", "y", "group", "reason")


def normalize_scatter_plot_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed scatter plot JSON into the strict result shape.

    FIX-NEW: new chart type for scatter plots and biplots.
    """
    if not isinstance(parsed, dict):
        return {
            "metadata": {},
            "groups": [],
            "points": [],
            "outliers": [],
            "statistics": {},
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }

    def s(v):
        return "" if v is None else str(v)

    out = {
        "metadata": {},
        "groups": [],
        "points": [],
        "outliers": [],
        "statistics": {},
        "confidence": 0.0,
    }

    # Normalize metadata
    meta = parsed.get("metadata") or {}
    if isinstance(meta, dict):
        out["metadata"] = {
            "title": s(meta.get("title", "")),
            "x_axis_label": s(meta.get("x_axis_label", "")),
            "y_axis_label": s(meta.get("y_axis_label", "")),
            "z_axis_label": s(meta.get("z_axis_label", "")),
            "x_unit": s(meta.get("x_unit", "")),
            "y_unit": s(meta.get("y_unit", "")),
            "n_points": meta.get("n_points") if isinstance(meta.get("n_points"), int) else None,
            "grouping_variable": s(meta.get("grouping_variable", "")),
        }

    # Normalize groups
    for grp in parsed.get("groups") or []:
        if not isinstance(grp, dict):
            continue
        row = {
            "name": s(grp.get("name")),
            "color": s(grp.get("color")),
            "marker": s(grp.get("marker")),
            "n_points_visible": grp.get("n_points_visible") if isinstance(grp.get("n_points_visible"), int) else None,
            "description": s(grp.get("description")),
        }
        # H8 (Sprint B REVIEW-2026-09-04): preserve unknown row keys.
        _carry_extras(grp, _KNOWN_SCATTER_GROUP_KEYS, row)
        out["groups"].append(row)

    # Normalize points (limit to first 500 for very large outputs)
    for pt in (parsed.get("points") or [])[:500]:
        if not isinstance(pt, dict):
            continue
        row = {
            "x": s(pt.get("x")),
            "y": s(pt.get("y")),
            "z": s(pt.get("z")),
            "group": s(pt.get("group")),
            "label": s(pt.get("label")),
            "note": s(pt.get("note")),
        }
        _carry_extras(pt, _KNOWN_SCATTER_POINT_KEYS, row)
        out["points"].append(row)

    # Normalize outliers
    for ot in parsed.get("outliers") or []:
        if not isinstance(ot, dict):
            continue
        row = {
            "x": s(ot.get("x")),
            "y": s(ot.get("y")),
            "group": s(ot.get("group")),
            "reason": s(ot.get("reason")),
        }
        _carry_extras(ot, _KNOWN_SCATTER_OUTLIER_KEYS, row)
        out["outliers"].append(row)

    # Normalize statistics
    stats = parsed.get("statistics") or {}
    if isinstance(stats, dict):
        out["statistics"] = {
            "correlation": s(stats.get("correlation")),
            "regression_line": s(stats.get("regression_line")),
            "r_squared": s(stats.get("r_squared")),
            "p_value": s(stats.get("p_value")),
        }

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))

    extras_src = {k: v for k, v in parsed.items() if k not in _KNOWN_SCATTER_PLOT_ROOT_KEYS}
    if extras_src:
        out["_extras"] = extras_src
    return out


def extract_scatter_plot(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Scatter plot extraction. Same contract as extract_range_chart."""
    from .prompt import SCATTER_PLOT_SYSTEM_PROMPT

    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the scatter plot information as the strict JSON contract."
    )
    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=SCATTER_PLOT_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_scatter_plot_result(parsed)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "scatter_plot", max_tokens, image_sha256,
            prompt_version_for_mode("scatter_plot"),
        ),
    )


_MODE_DISPATCH["abundance_diagram"] = extract_abundance_diagram
_MODE_DISPATCH["phylogenetic_tree"] = extract_phylogenetic_tree
_MODE_DISPATCH["chemical_stratigraphy"] = extract_chemical_stratigraphy
_MODE_DISPATCH["paleomap"] = extract_paleomap
_MODE_DISPATCH["scatter_plot"] = extract_scatter_plot

# ---------------------------------------------------------------------------
# ZONATION / BIOSTRATIGRAPHIC CORRELATION CHART (2026-09-05, radiolarian
# biochronology figures: columns of named zones correlated across regions /
# against ammonoid-conodont zones and stages — e.g. Gorican et al. 2018).
# ---------------------------------------------------------------------------
_KNOWN_ZONATION_ROOT_KEYS = (
    "zonations", "zones", "correlations", "confidence",
)
_KNOWN_ZONATIONS_KEYS = ("name", "region", "framework", "reference")
_KNOWN_ZONATION_ZONE_KEYS = (
    "name", "zonation", "rank", "age_span", "base_age", "top_age",
    "stage", "defined_by", "note",
)
_KNOWN_CORRELATION_KEYS = (
    "from_zone", "to_zone", "from_zonation", "to_zonation", "basis", "note",
)


def normalize_zonation_chart_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed zonation-chart JSON into the strict result shape.

    H8: extra keys the model emits are preserved under ``_extras`` per row.
    All rows are string-typed, mirroring range-chart so the majority-vote
    merge machinery in aggregate.py works with no new code path.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to the
    appropriate keys (zonations, zones, correlations).
    """
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                parsed.setdefault("_unclassified", []).append(item)
                continue
            # Correlations carry from_zone / to_zone.
            if "from_zone" in item or "to_zone" in item:
                parsed.setdefault("correlations", []).append(item)
            # Zone rows carry a name plus rank / zonation / age_span markers.
            elif "name" in item and (
                "rank" in item or "zonation" in item or "age_span" in item
                or "defined_by" in item or "base_age" in item
            ):
                parsed.setdefault("zones", []).append(item)
            # Zonation columns carry name + region / framework / reference.
            elif "name" in item and (
                "region" in item or "framework" in item or "reference" in item
            ):
                parsed.setdefault("zonations", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        return "" if v is None else str(v)

    out: dict[str, Any] = {
        "zonations": [],
        "zones": [],
        "correlations": [],
        "confidence": 0.0,
    }
    if not isinstance(parsed, dict):
        return out
    for z in (parsed.get("zonations") if isinstance(parsed.get("zonations"), list) else []):
        if not isinstance(z, dict):
            continue
        row = {
            "name": s(z.get("name")),
            "region": s(z.get("region")),
            "framework": s(z.get("framework")),
            "reference": s(z.get("reference")),
        }
        _carry_extras(z, _KNOWN_ZONATIONS_KEYS, row)
        out["zonations"].append(row)
    for z in (parsed.get("zones") if isinstance(parsed.get("zones"), list) else []):
        if not isinstance(z, dict):
            continue
        row = {
            "name": s(z.get("name")),
            "zonation": s(z.get("zonation")),
            "rank": s(z.get("rank")),
            "age_span": s(z.get("age_span")),
            "base_age": s(z.get("base_age")),
            "top_age": s(z.get("top_age")),
            "stage": s(z.get("stage")),
            "defined_by": s(z.get("defined_by")),
            "note": s(z.get("note")),
        }
        _carry_extras(z, _KNOWN_ZONATION_ZONE_KEYS, row)
        out["zones"].append(row)
    for c in (parsed.get("correlations") if isinstance(parsed.get("correlations"), list) else []):
        if not isinstance(c, dict):
            continue
        row = {
            "from_zone": s(c.get("from_zone")),
            "to_zone": s(c.get("to_zone")),
            "from_zonation": s(c.get("from_zonation")),
            "to_zonation": s(c.get("to_zonation")),
            "basis": s(c.get("basis")),
            "note": s(c.get("note")),
        }
        _carry_extras(c, _KNOWN_CORRELATION_KEYS, row)
        out["correlations"].append(row)
    try:
        out["confidence"] = max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    extras_src = {k: v for k, v in parsed.items() if k not in _KNOWN_ZONATION_ROOT_KEYS}
    if extras_src:
        out["_extras"] = extras_src
    return out


def extract_zonation_chart(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Zonation / correlation chart extraction. Same contract as
    extract_range_chart."""
    from .prompt import ZONATION_CHART_SYSTEM_PROMPT

    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    max_tokens = clamp_max_tokens(max_tokens)
    timeout_sec = clamp_timeout_sec(timeout_sec)
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Extract the biozonation / correlation chart information as the "
        "strict JSON contract."
    )
    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=ZONATION_CHART_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
            image_sha256=image_sha256,
        )
    try:
        data = normalize_zonation_chart_result(parsed)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "zonation_chart", max_tokens, image_sha256,
            prompt_version_for_mode("zonation_chart"),
        ),
    )


_MODE_DISPATCH["zonation_chart"] = extract_zonation_chart



# ---------------------------------------------------------------------------
# VISION CHART-TYPE CLASSIFIER + AUTO MODE (UI-REVIEW-2026-09-07).
#
# "auto" used to mean "keyword-match the caption / filename, else fall
# back to range_chart" — a caption-less abundance or zonation figure was
# silently extracted with the WRONG prompt. The upgraded auto path:
#   1. text heuristic on caption + filename (cheap, trusted when it hits);
#   2. vision classification of the image itself (cheap small-token call);
#   3. range_chart as the final fallback.
# ---------------------------------------------------------------------------

KNOWN_CHART_TYPES = frozenset({
    "range_chart", "columnar_section", "abundance_diagram",
    "phylogenetic_tree", "zonation_chart", "chemical_stratigraphy",
    "paleomap", "scatter_plot",
})

_CLASSIFY_MIN_CONFIDENCE = 0.5


def normalize_chart_classification(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the classifier JSON into {"chart_type", "reason", "confidence"}.

    Unknown / missing chart_type degrades to "unknown" rather than raising
    — a classification failure must fall back to the text heuristic /
    range_chart, never abort the extraction."""
    if not isinstance(parsed, dict):
        return {"chart_type": "unknown", "reason": "", "confidence": 0.0}
    chart_type = str(parsed.get("chart_type", "") or "").strip().lower()
    if chart_type not in KNOWN_CHART_TYPES:
        chart_type = "unknown"
    try:
        conf = max(0.0, min(1.0, float(parsed.get("confidence", 0.0) or 0.0)))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "chart_type": chart_type,
        "reason": "" if parsed.get("reason") is None else str(parsed.get("reason")),
        "confidence": conf,
    }


def classify_chart_image(
    *,
    api_key: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> ExtractResult:
    """Classify the chart TYPE of an image (never extracts data).

    Uses a dedicated small-token prompt; the result ``data`` carries
    ``{"chart_type", "reason", "confidence"}``. Cached like any other
    extraction (mode="chart_classify"), so a multi-run auto job classifies
    the identical image once."""
    from .prompt import CHART_CLASSIFY_SYSTEM_PROMPT

    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    timeout_sec = clamp_timeout_sec(timeout_sec)
    image_sha256 = compute_image_sha256_from_b64(image_b64)
    p = provider or LlmProvider(
        name="Legacy Anthropic-compatible",
        api_format=ApiFormat.ANTHROPIC,
        endpoint=base_url,
        api_key=api_key,
        model=model,
    )
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + "Classify the chart type as the strict JSON contract."
    )
    # UI-REVIEW-2026-09-08 (E2E perf): classification of the SAME image is
    # deterministic in intent, so cache it like an extraction. Fail-open:
    # any cache trouble just falls through to the network call.
    ckey = None
    try:
        from .cache import get_cache
        from .prompt import prompt_version_for_mode as _pvm
        _cache = get_cache()
        ckey = _cache.make_key(
            endpoint=p.endpoint if p else "",
            model=p.model if p else "",
            api_format=p.api_format.value if p else "",
            prompt_version=_pvm("chart_classify"),
            mode="chart_classify",
            image_b64=image_b64,
        )
        cached = _cache.get(ckey)
        if isinstance(cached, dict) and "chart_type" in cached:
            return ExtractResult(
                ok=True, data=cached, raw="(cached classification)",
                image_sha256=image_sha256,
                request_meta={"cached": True,
                              "mode": "chart_classify"},
            )
    except Exception:
        ckey = None

    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = call_llm_api(
            provider=p,
            system_prompt=CHART_CLASSIFY_SYSTEM_PROMPT,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_prompt,
            max_tokens=clamp_max_tokens(500),
            timeout_sec=timeout_sec,
            capture_error_body=True,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return ExtractResult(
            ok=False, error_key="err.extract", raw="", latency_ms=latency_ms,
            warning=f"call_llm_api failed: {type(exc).__name__}: {exc}",
            image_sha256=image_sha256,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms, image_sha256=image_sha256)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms, usage=usage or {},
            image_sha256=image_sha256,
        )
    data = normalize_chart_classification(parsed)
    if ckey is not None:
        try:
            from .cache import get_cache
            get_cache().put(ckey, data)
        except Exception:
            pass
    return ExtractResult(
        ok=True,
        data=data,
        raw=raw_text, truncated=truncated, usage=usage or {},
        latency_ms=latency_ms, image_sha256=image_sha256,
        request_meta=_build_request_meta(
            p, "chart_classify", clamp_max_tokens(500), image_sha256,
            prompt_version_for_mode("chart_classify"),
        ),
    )


def resolve_auto_mode(
    caption: str,
    filename: str,
    image_b64: str,
    media_type: str,
    api_key: str = "",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    provider: LlmProvider | None = None,
    progress_callback=None,
) -> tuple[str, ExtractResult | None]:
    """Resolve ``"auto"`` to a concrete chart type.

    Returns ``(mode, classify_result)``. ``classify_result`` is None when
    the text heuristic matched (no vision call was needed); otherwise it is
    the (possibly failed) vision classification whose ``chart_type`` and
    ``reason`` informed the decision. Never raises — any failure falls
    back to ``range_chart``."""
    from .chart_mode import auto_detect_chart_mode_ex

    text_mode, matched = auto_detect_chart_mode_ex(
        f"{caption or ''} {filename or ''}")
    if matched:
        return text_mode, None
    # Vision fallback. A classification failure here must not kill the
    # extraction — fall back to range_chart. The synthetic failed result
    # (ok=False) preserves the "vision was attempted" provenance: callers
    # derive mode_source from `classify_result is not None`, so returning
    # None here would misreport a vision-default as a text match.
    try:
        cls = classify_chart_image(
            api_key=api_key, image_b64=image_b64, media_type=media_type,
            caption=caption, provider=provider,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        cls = ExtractResult(
            ok=False, error_key="err.classify",
            data={"chart_type": "unknown", "reason": str(exc), "confidence": 0.0},
        )
    if cls.ok and isinstance(cls.data, dict):
        chart_type = cls.data.get("chart_type") or "unknown"
        conf = float(cls.data.get("confidence") or 0.0)
        if (chart_type in KNOWN_CHART_TYPES and chart_type != "unknown"
                and conf >= _CLASSIFY_MIN_CONFIDENCE):
            return chart_type, cls
    return "range_chart", cls


def _is_silent_miss(data: dict[str, Any]) -> bool:
    """True when a successful extraction returned an all-empty payload
    with ~zero confidence AND no explanatory note - i.e. a sampling flake
    worth one retry (E2E fig: oa_004 recovered 36 rows on re-run).

    Honest degradations attach a note explaining WHY the figure is not
    readable ("not a paleogeographic map"); those are final answers, not
    flakes, and must not be retried."""
    if not isinstance(data, dict):
        return False
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf > 0.1:
        return False
    extras = data.get("_extras")
    note = data.get("note")
    if isinstance(extras, dict):
        note = note or extras.get("note")
    if isinstance(note, str) and note.strip():
        return False  # the model explained itself - honour the verdict
    return not any(
        isinstance(v, list) and len(v) > 0 for v in data.values()
    )


def extract(
    *,
    mode: str,
    image_b64: str,
    media_type: str,
    caption: str = "",
    chart_lang: str = "auto",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    provider: LlmProvider | None = None,
    # Legacy flat kwargs (kept optional for backward compat). Ignored when
    # ``provider`` is supplied; used only to fall back to an Anthropic
    # default when no provider was provided.
    api_key: str = "",
    base_url: str = DEFAULT_ENDPOINT,
    model: str = DEFAULT_MODEL,
    progress_callback=None,
) -> ExtractResult:
    """Unified entry point. mode ∈ {"range_chart", "columnar_section",
    "abundance_diagram", "phylogenetic_tree"}.

    When ``provider`` is given it drives the API format / auth / endpoint and
    the flat legacy kwargs (``base_url`` / ``api_key`` / ``model``) are ignored
    — matching the per-mode functions' contract. This lets callers such as
    ``server.py`` and ``gui.py`` pass a single source of truth.

    All flat kwargs now have defaults so callers using only the provider
    path (e.g. server.py) don't have to send sentinel empty values.

    ``progress_callback``, if given, is called with stage strings
    ``"submitting"``, ``"uploading"``, ``"thinking"`` to allow the UI to
    show granular extraction progress.
    """
    # UI-REVIEW-2026-09-07 (auto mode): resolve "auto" through the
    # two-stage pipeline (text heuristic, then vision classification) and
    # stamp which chart type actually drove the extraction.
    mode_source = ""
    classify_result = None
    if mode == "auto":
        mode, classify_result = resolve_auto_mode(
            caption=caption,
            filename="",
            image_b64=image_b64,
            media_type=media_type,
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider=provider,
            progress_callback=progress_callback,
        )
        mode_source = "vision" if classify_result is not None else "text"

    fn = _MODE_DISPATCH.get(mode)
    if fn is None:
        return ExtractResult(ok=False, error_key="err.http", raw=f"unknown mode: {mode}")
    result = fn(
        api_key=api_key,
        image_b64=image_b64,
        media_type=media_type,
        caption=caption,
        chart_lang=chart_lang,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        provider=provider,
        progress_callback=progress_callback,
    )
    # UI-REVIEW-2026-09-08 (E2E oa_004): silent misses recover on re-run.
    # When the model returns a payload whose every content array is empty
    # at ~zero confidence WITHOUT an explanatory note, that is a sampling
    # flake, not an honest degradation - retry once with the same prompt.
    # Honest degradations carry a note ("not a paleogeographic map") and
    # are NOT retried: a second call with the same prompt would return the
    # same answer and double the cost for nothing.
    if result.ok and _is_silent_miss(result.data):
        try:
            retry = fn(
                api_key=api_key,
                image_b64=image_b64,
                media_type=media_type,
                caption=caption,
                chart_lang=chart_lang,
                base_url=base_url,
                model=model,
                max_tokens=max_tokens,
                timeout_sec=timeout_sec,
                provider=provider,
                progress_callback=progress_callback,
            )
        except Exception:
            retry = None
        if retry is not None and retry.ok and not _is_silent_miss(retry.data):
            retry.warning = ((retry.warning + "; ") if retry.warning else "")                 + "empty first attempt retried once"
            result = retry
    # UI-REVIEW-2026-09-07: surface which chart type the auto resolver
    # picked so UIs / history records can show "detected: zonation_chart".
    if mode_source:
        result.mode_used = mode
        result.mode_source = mode_source
    return result
