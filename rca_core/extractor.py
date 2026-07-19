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
import time
from dataclasses import dataclass, field
from typing import Any

from .json_utils import safe_json_loads
from .llm import ApiFormat, LlmProvider, call_llm_api
from .prompt import (
    ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
    CHART_LANG_HINT,
    COLUMNAR_SECTION_SYSTEM_PROMPT,
    RANGE_CHART_SYSTEM_PROMPT,
)

DEFAULT_ENDPOINT = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_MAX_TOKENS = 4000
# M5: explicit min/max bounds for clamp_max_tokens — defends against
# user typing absurd values (negative, millions) in the GUI / API.
MIN_MAX_TOKENS = 1
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


def load_image_b64(path: str, max_edge: int = DEFAULT_MAX_EDGE):
    """Read an image, optionally downscale so its long edge <= max_edge.

    Returns ``(base64, media_type, width, height, resized, decode_error)``.
    Falls back to the raw bytes when Pillow is unavailable. The new
    ``decode_error`` flag is True when the file could not be decoded as
    an image (corrupt PNG header, etc.) — callers can use this to surface
    a friendlier error instead of showing a 0×0 thumbnail.

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
)
_KNOWN_BIOZONE_KEYS = ("name", "section", "age", "thickness_m")


def _carry_extras(item: dict[str, Any], known: tuple[str, ...], out: dict[str, Any]) -> None:
    """H8: any non-known key the model emitted is preserved under a single
    ``_extras`` dict so downstream consumers (CSV/JSON export) can see it."""
    extras = {k: v for k, v in item.items() if k not in known}
    if extras:
        out["_extras"] = extras


def normalize_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed JSON into the strict result shape.

    H8: extra top-level / row-level keys the model emits are not silently
    discarded — they're attached under ``_extras`` so the operator sees
    what was extracted. This avoids losing data the caller assumes is
    captured by the schema.
    """
    def s(v: Any) -> str:
        return "" if v is None else str(v)

    out: dict[str, Any] = {
        "sections": [],
        "species_ranges": [],
        "biozones": [],
        "other_fossils": [],
        "confidence": 0.0,
    }
    for sec in parsed.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        formations = sec.get("formations")
        row = {
            "name": s(sec.get("name")),
            "age_range": s(sec.get("age_range")),
            "formations": [s(x) for x in formations] if isinstance(formations, list) else [],
            "formation_thickness_m": s(sec.get("formation_thickness_m")),
            "coordinates": s(sec.get("coordinates")),
        }
        _carry_extras(sec, _KNOWN_SECTION_KEYS, row)
        out["sections"].append(row)
    for sp in parsed.get("species_ranges") or []:
        if not isinstance(sp, dict):
            continue
        row = {
            "species": s(sp.get("species")),
            "section": s(sp.get("section")),
            "range_top": s(sp.get("range_top")),
            "range_base": s(sp.get("range_base")),
            "biozone": s(sp.get("biozone")),
        }
        _carry_extras(sp, _KNOWN_SPECIES_KEYS, row)
        out["species_ranges"].append(row)
    for bz in parsed.get("biozones") or []:
        if not isinstance(bz, dict):
            continue
        row = {
            "name": s(bz.get("name")),
            "section": s(bz.get("section")),
            "age": s(bz.get("age")),
            "thickness_m": s(bz.get("thickness_m")),
        }
        _carry_extras(bz, _KNOWN_BIOZONE_KEYS, row)
        out["biozones"].append(row)
    of = parsed.get("other_fossils") or []
    if isinstance(of, list):
        out["other_fossils"] = [s(x) for x in of if s(x).strip()]
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = max(0.0, min(1.0, conf))
    # H8: top-level extras (anything outside the named lists + confidence).
    top_extras = {k: v for k, v in parsed.items() if k not in _KNOWN_RANGE_CHART_KEYS}
    if top_extras:
        out["_extras"] = top_extras
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
) -> ExtractResult:
    """Range-chart extraction. Never raises.

    When ``provider`` is given, it drives the API format / auth / endpoint and
    the ``base_url`` / ``api_key`` / ``model`` kwargs are ignored. When None
    (legacy callers), an Anthropic-format provider is built from the kwargs so
    old behaviour is preserved byte-for-byte.
    """
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
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
    raw_text, truncated, status, err_body, usage = call_llm_api(
        provider=p,
        system_prompt=RANGE_CHART_SYSTEM_PROMPT,
        image_b64=image_b64,
        media_type=media_type,
        user_text=user_prompt,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        capture_error_body=True,
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
        return _error_from_status_with_body(status, err_body, latency_ms)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
        )
    return ExtractResult(
        ok=True, data=normalize_result(parsed), raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
    )


def _error_from_status(status: int | None, err_body: str = "", latency_ms: int = 0) -> ExtractResult:
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
            latency_ms=latency_ms,
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
        latency_ms=latency_ms,
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
    """

    def s(v: Any) -> str:
        return "" if v is None else str(v)

    def fi(v: Any) -> int | None:
        try:
            if v is None or v == "":
                return None
            return int(v)
        except (TypeError, ValueError):
            return None

    def norm_blocks(items):
        out = []
        for b in items or []:
            if not isinstance(b, dict):
                continue
            row = {
                "pattern": s(b.get("pattern")),
                "range_top_idx": fi(b.get("range_top_idx")),
                "range_base_idx": fi(b.get("range_base_idx")),
            }
            _carry_extras(b, _KNOWN_BLOCK_KEYS, row)
            out.append(row)
        return out

    def norm_units(items):
        out = []
        for u in items or []:
            if not isinstance(u, dict):
                continue
            row = {
                "label": s(u.get("label")),
                "range_top_idx": fi(u.get("range_top_idx")),
                "range_base_idx": fi(u.get("range_base_idx")),
            }
            _carry_extras(u, _KNOWN_UNIT_KEYS, row)
            out.append(row)
        return out

    def norm_samples(items):
        out = []
        for s_item in items or []:
            if not isinstance(s_item, dict):
                continue
            row = {
                "bed_idx": fi(s_item.get("bed_idx")),
                "fossil_marker": s(s_item.get("fossil_marker")),
                "ref": s(s_item.get("ref")),
            }
            _carry_extras(s_item, _KNOWN_SAMPLE_KEYS, row)
            out.append(row)
        return out

    def norm_legend(items):
        out = []
        for x in items or []:
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
        return out

    def norm_cross(items):
        out = []
        for x in items or []:
            if not isinstance(x, dict):
                continue
            row = {
                "from_section": s(x.get("from_section")),
                "from_bed_idx": fi(x.get("from_bed_idx")),
                "to_section": s(x.get("to_section")),
                "to_bed_idx": fi(x.get("to_bed_idx")),
            }
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

    out: dict[str, Any] = {
        "sections": sections,
        "fossil_legend": norm_legend(parsed.get("fossil_legend")),
        "lithology_legend": norm_legend(parsed.get("lithology_legend")),
        "cross_beds": norm_cross(parsed.get("cross_beds")),
        "confidence": overall,
    }
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
) -> ExtractResult:
    """Columnar-section extraction. Same contract as extract_range_chart."""
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
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
    raw_text, truncated, status, err_body, usage = call_llm_api(
        provider=p,
        system_prompt=COLUMNAR_SECTION_SYSTEM_PROMPT,
        image_b64=image_b64,
        media_type=media_type,
        user_text=user_prompt,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        capture_error_body=True,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
        )
    return ExtractResult(
        ok=True, data=normalize_columnar_result(parsed), raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
    )


# Dispatch table — single entry point for both modes.
_MODE_DISPATCH = {
    "range_chart": extract_range_chart,
    "columnar_section": extract_columnar_section,
    "abundance_diagram": None,  # bound below after the function is defined
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
    """
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
) -> ExtractResult:
    """Abundance-diagram extraction. Same contract as extract_range_chart."""
    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
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
    raw_text, truncated, status, err_body, usage = call_llm_api(
        provider=p,
        system_prompt=ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
        image_b64=image_b64,
        media_type=media_type,
        user_text=user_prompt,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        capture_error_body=True,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    warning = ("Result may be truncated (model hit max_tokens). "
               "Try raising the max_tokens setting and re-running.")
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms)
    try:
        parsed = safe_json_loads(raw_text)
    except ValueError:
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, latency_ms=latency_ms,
            usage=usage or {},
            warning=warning if truncated else "",
        )
    return ExtractResult(
        ok=True, data=normalize_abundance_result(parsed), raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
    )


_MODE_DISPATCH["abundance_diagram"] = extract_abundance_diagram


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
) -> ExtractResult:
    """Unified entry point. mode ∈ {"range_chart", "columnar_section",
    "abundance_diagram"}.

    When ``provider`` is given it drives the API format / auth / endpoint and
    the flat legacy kwargs (``base_url`` / ``api_key`` / ``model``) are ignored
    — matching the per-mode functions' contract. This lets callers such as
    ``server.py`` and ``gui.py`` pass a single source of truth.

    All flat kwargs now have defaults so callers using only the provider
    path (e.g. server.py) don't have to send sentinel empty values.
    """
    fn = _MODE_DISPATCH.get(mode)
    if fn is None:
        return ExtractResult(ok=False, error_key="err.http", raw=f"unknown mode: {mode}")
    return fn(
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
    )
