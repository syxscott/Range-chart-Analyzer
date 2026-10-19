"""Range-chart extraction core.

Calls the MiniMax M3 (Anthropic-compatible) vision API using only the
Python standard library (urllib) so the app runs with zero third-party
install. Pillow is used only when available, to downscale huge images.

The public ``extract_range_chart`` never raises: it returns an
``ExtractResult`` with an ``ok`` flag and an ``error_key`` for the UI to
translate. Mirrors the JS ``extractRangeChart`` contract.
"""

from __future__ import annotations

from .modes import MODES as _MODE_SPECS

import base64
import io
import json
import logging
import math
import mimetypes
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Iterable

#: Module logger. Bound here rather than created at first use because the
#: grid_read fallback path logs from inside its own `except` handler: a logger
#: that did not exist there would turn a handled failure into a NameError, and
#: the one thing that handler exists to prevent is exactly that.
log = logging.getLogger(__name__)

# AUDIT-2026-09-29: the two enhance helpers annotate their argument and return
# as `"Image.Image"`, but Pillow was imported FUNCTION-LOCALLY everywhere in
# this module, so `Image` was never a name in the module namespace.
# `from __future__ import annotations` made that invisible at runtime, and
# `ruff --select F821` was the only thing that ever looked. It bites in two
# places that are easy to miss: typing.get_type_hints() on either helper
# raises NameError (verified, not assumed), and any type checker reports the
# annotation as undefined. Same shape as the missing `Optional` import that
# rca_core/quality.py carried until this review wave.
#
# Not a TYPE_CHECKING block: that would satisfy the type checker and leave
# get_type_hints() still raising, which is the half that actually bites at
# runtime. The fallback to Any keeps the annotations resolvable when Pillow
# really is absent, so the module still imports -- the same "rca_core works
# without Pillow" contract deskew.py states.
try:
    from PIL import Image
except Exception:  # pragma: no cover - exercised only without Pillow
    # `Image = Any` is NOT enough: the annotations say "Image.Image", and
    # Any.Image does not exist, so get_type_hints() would trade a NameError
    # for an AttributeError. A one-attribute stand-in keeps both spellings
    # resolvable and lands on Any.
    class _ImageStub:  # noqa: N801 - stands in for the PIL module
        Image = Any

    Image = _ImageStub  # type: ignore[misc,assignment]

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
# BORROW-2026-09-20 (A): the coverage contract (response_kind + reason_codes).
from .reason_codes import (
    LOW_CONFIDENCE_CODE,
    RESPONSE_EXTRACTED,
    RESPONSE_NOT_DRAWN,
    has_value,
    normalize_codes,
    normalize_response_kind,
)

DEFAULT_ENDPOINT = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M3"
# REVIEW-2026-10-15 (measured): 4000 silently truncated ordinary figures.
# A 22-taxon x 18-locality occurrence matrix -- an entirely routine input for
# this tool -- needs 4400-5400 output tokens as one JSON record per filled cell.
# At 4000 the model was cut off mid-answer: it returned 52 of the 93 filled
# cells and never emitted the last row at all, while `stop_reason ==
# "max_tokens"` only surfaced as a warning string the operator may never read.
# max_tokens is a CEILING, not a reservation, so a higher default costs nothing
# unless the answer is actually that long.
DEFAULT_MAX_TOKENS = 16000
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
    # REVIEW-2026-09-10: OverflowError is not a TypeError/ValueError
    # subclass, and JSON 1e400 parses to float('inf') -> int() raises.
    except (TypeError, ValueError, OverflowError):
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
    except (TypeError, ValueError, OverflowError):  # see clamp_max_tokens
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
    except (TypeError, ValueError, OverflowError):  # see clamp_max_tokens
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
        # REVIEW-2026-09-10: extra_body is applied to the request body AFTER
        # the clamped max_tokens / model are set (llm.py `body.update(...)`),
        # so a provider with extra_body={"max_tokens": ..., "model": ...} sends
        # different values on the wire than this record claims - and this
        # record is the 5-year audit artifact. Record the actual wire values
        # so the two cannot disagree.
        if "max_tokens" in eb:
            meta["max_tokens"] = eb["max_tokens"]
            meta["max_tokens_source"] = "provider.extra_body"
        if "model" in eb:
            meta["model"] = eb["model"]
            meta["model_source"] = "provider.extra_body"
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
    # REVIEW-2026-09-20 #9: ``Image`` was only bound by the *fallback* import
    # path above, so this line raised NameError whenever cv2 really was
    # installed - the whole cv2 enhancement path was a dead end. Import
    # Pillow here so every return of this function has the name available.
    from PIL import Image  # type: ignore
    return Image.fromarray(arr)  # type: ignore


# REVIEW-2026-09-20 #10: ceiling for a single source image. The old
# ``f.read()`` had no limit, so pointing the extractor at a video / ISO made
# the process allocate gigabytes before the API ever saw a request. Oversized
# files are reported through the existing ``decode_error`` channel.
MAX_IMAGE_BYTES = 50 * 1024 * 1024


def load_image_b64(path: str, max_edge: int = DEFAULT_MAX_EDGE,
                   enhance: bool = False, deskew: bool = False):
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

    BORROW-2026-09-20: ``deskew=True`` first straightens the whole image
    (scanned / photographed plates are routinely a fraction of a degree
    off, which systematically breaks any grid-aligned reading) via
    ``rca_core.deskew``. Like enhancement it happens before downscale,
    and any failure keeps the original picture — it is an opt-in
    refinement, never a hard dependency.

    Bug-15 fix: the previous version returned ``width=0, height=0`` for
    both "Pillow missing" and "decode failed", making the two cases
    indistinguishable. We now set ``decode_error=True`` when Pillow is
    present but cannot decode.
    """
    # REVIEW-2026-09-20 #11: clamp_max_edge existed but was never wired into
    # any entry point, so a caller-supplied junk / negative max_edge reached
    # the resize maths unchecked (a negative edge produced a nonsense scale,
    # a string raised TypeError). 0 still means "no resize"; the default
    # (DEFAULT_MAX_EDGE) passes through unchanged, so no existing caller's
    # semantics move.
    max_edge = clamp_max_edge(max_edge)
    # REVIEW-2026-09-20 #10: refuse to slurp an oversized file (see
    # MAX_IMAGE_BYTES). Reported through decode_error, the channel callers
    # already use for "this is not a usable image".
    try:
        size_on_disk = os.path.getsize(path)
    except OSError:
        size_on_disk = -1
    if size_on_disk > MAX_IMAGE_BYTES:
        return "", "image/jpeg", 0, 0, False, True
    with open(path, "rb") as f:
        # The +1 lets the length check below catch files whose reported size
        # was a lie (stream, symlink race) without reading them whole.
        raw = f.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        return "", "image/jpeg", 0, 0, False, True
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

    # AUDIT-2026-09-27 [item 3.1] (found by running the real pipeline over
    # figures cut from the 2020 radiolarian literature, not by a test):
    # a phone photo carries its rotation in EXIF Orientation, and Pillow does
    # NOT apply it on open. ``rca_core.history.make_thumbnail_with_size`` has
    # called ``ImageOps.exif_transpose`` since REVIEW-2026-09-20 (finding 9),
    # so the HISTORY copy was stored upright — while this function sent the
    # same file to the model sideways, and the Extract page previewed the
    # sideways version too. Verified with a 400x200 JPEG tagged
    # Orientation=6: model received 400x200, history stored 100x200. The
    # same figure was upright in review and sideways in extraction, which
    # is precisely the input the VLM reads worst.
    # Applied BEFORE the w/h read below and before any pixel work, so the
    # reported dimensions, the max_edge downscale and the deskew detector all
    # see the orientation the model will actually get. A corrupt EXIF block
    # must not cost us the image, so this is best-effort like the one in
    # history.py.
    _exif_transposed = False
    try:
        from PIL import ImageOps  # type: ignore
        _before = img
        img = ImageOps.exif_transpose(img) or img
        if img is not _before:
            w, h = img.size
            _exif_transposed = True
    except Exception:
        pass

    # FIX (enhance): pre-process the image to boost VLM recognition of thin
    # lines and small text. Default off — the user opts in via the UI.
    # REVIEW-2026-09-20 #12: everything from here touches PIXEL data, and PIL
    # only raises OSError on a truncated file at that point (i.e. past the
    # Image.open guard above), which used to escape this function entirely.
    # Decode what exists, and report any hard failure through decode_error.
    try:
        from PIL import ImageFile  # type: ignore
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    except Exception:
        pass

    def _raw_with_error():
        return base64.b64encode(raw).decode("ascii"), mime, 0, 0, False, True

    modified = False
    # A real EXIF rotation must ALSO defeat the original-bytes fast path
    # further down. Without this the function reported the transposed size
    # while still uploading the untransposed original — caught by the
    # verification run immediately after the fix was written.
    if _exif_transposed:
        modified = True
    try:
        if deskew:
            try:
                from .deskew import deskew_image
                # FIX-2026-09-22 (audit item 9): ``deskew_image`` returns the
                # SAME object (angle 0.0) when the detected skew is below
                # ``min_angle`` — setting ``modified = True`` unconditionally
                # defeated the original-bytes fast path below and re-encoded
                # every deskew-enabled screenshot lossy-PNG at ~+40% size.
                # The identity check is robust to a future rotation that
                # expands the canvas (it then always returns a new object).
                _pre_deskew = img
                img, _angle = deskew_image(img)
                if img is not _pre_deskew:
                    w, h = img.size
                    modified = True
            except Exception:
                pass
        if enhance:
            if enhance == "cv2":
                img = _enhance_image_cv2(img)
            else:
                img = _enhance_image_pil(img)
            w, h = img.size  # re-read size (cv2 path may have upsampled)
            modified = True

        long_edge = max(w, h)
        resized = False
        if max_edge and long_edge > max_edge:
            scale = max_edge / long_edge
            nw, nh = int(round(w * scale)), int(round(h * scale))
            img = img.resize((nw, nh), Image.LANCZOS)
            w, h = nw, nh
            modified = True
            resized = True
        if not modified:
            # Nothing touched the picture: upload the original bytes verbatim.
            return base64.b64encode(raw).decode("ascii"), mime, w, h, False, False
        out = io.BytesIO()
        # Prefer lossless PNG for downscaled charts so the small italic
        # species names stay sharp. JPEG re-compression blurs dense text
        # and is a known cause of OCR misreads. Only keep JPEG when the
        # source is already JPEG AND the resized image is large enough
        # that a lossless PNG would be excessively big.
        #
        # REVIEW-2026-09-20 #2: this re-encode used to live INSIDE the resize
        # branch only, so an enhanced image that needed no downscale fell
        # through to "return the original bytes" - the enhancement was
        # computed and then thrown away. Anything modified is encoded here
        # now; ``resized`` below still reports only whether a resize happened.
        resized_is_large = (w * h) > (2500 * 2500)
        if mime == "image/jpeg" and resized_is_large:
            fmt = "JPEG"
            img = img.convert("RGB")
            img.save(out, format=fmt, quality=95)
        else:
            fmt = "PNG"
            img.save(out, format=fmt)
        data = out.getvalue()
        out_mime = "image/png" if fmt == "PNG" else "image/jpeg"
        return base64.b64encode(data).decode("ascii"), out_mime, w, h, resized, False
    except Exception:
        return _raw_with_error()


_KNOWN_RANGE_CHART_KEYS = (
    "sections", "species_ranges", "biozones", "other_fossils", "confidence",
    # BORROW-2026-09-20 (B): the optional figure-level axis calibration is a
    # hoisted root key, not an _extras leftover.
    "axis_calibration",
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
    # AUDIT-2026-10-02: explicit ASCII lookarounds instead of `\b`, for the
    # same reason as the zone_type ladder below. Python's `\b` is Unicode-aware
    # on str patterns and js/minimax.js#_RCA_IRON_RULE_ZONE_RE's is not, so for
    # "图zone" the browser matched and this side did not -- and this is the
    # highest-stakes instance of that split, because the rule exists so a
    # zone-like name is never filed as a taxon. Measured on the `range_chart`
    # parity group: with the Unicode-aware spelling the desktop emitted NO
    # `iron_rule_zone_label` warning and no "[zone-mislabel-warning]" note,
    # while the browser emitted both, so a zone label was silently accepted as
    # a species on one endpoint and flagged on the other -- i.e. the
    # "fabricated FAD/LAD for a non-taxon" the comment above describes.
    #
    # A CJK glyph is not a continuation of an ASCII identifier, so ASCII is
    # the right notion of "adjacent identifier character" here. "zozone" and
    # "zoness" are still rejected on both engines, and a space, a hyphen and a
    # dot are still boundaries both engines agree on (rc_iron_rule_boundaries
    # pins that half).
    r"(?<![A-Za-z0-9_])(zone|zonule|assemblage|oppel|interval|lineage|range|acme)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# AUDIT-2026-10-15 (D1): a column header printed VERTICALLY over two lines is
# returned as two rows.
#
# Measured on Li Xin et al. 2018a Fig. 4 (`_figtest/r10_lixin_col_result.json`,
# shipped extract over the real plate): the 24 taxon headers are rotated 90 and
# wrapped, so one column reads
#
#     Bekoma? cf. demissa
#     robusta Nishimura
#
# and the model returned FOUR `species_ranges` rows for the two columns that
# share that stem --
#
#     Bekoma? cf. demissa   ZN1-16 .. ZN1-20
#     robustus Nishimura    ZN1-20 .. ZN1-21
#     Bekoma? cf. demissa   ZN1-15 .. ZN1-15
#     demissa Foreman       ZN1-15 .. ZN1-16
#
# "robustus Nishimura" and "demissa Foreman" are not printed anywhere on the
# plate: each is the second PRINTED LINE of a header whose first line is
# already a row of its own. They exported as taxa with FAD/LAD endpoints, i.e.
# the normaliser turned a line-wrap artefact into two fabricated occurrence
# records (2 of 20 rows; 3 of 20 names were not complete labels).
#
# The name-shape vocabulary below is what separates the cases. Tokens are
# whitespace-separated, so this deliberately does not try to parse
# punctuation-heavy forms -- it only has to answer "is this the tail of a
# wrapped header", "is this a bare epithet", "is this a bare authority" and
# "is this a genus and nothing else".
_NAME_OPEN_NOMINATURE = frozenset({"sp.", "spp.", "sp", "ssp."})
_NAME_QUALIFIERS = frozenset({
    "cf.", "aff.", "subsp.", "var.", "s.l.", "s.str.", "sensu", "nov.",
    "n.sp.", "emend.", "et", "al.",
})


def _name_label_parts(name: Any) -> tuple[list[str], str, bool]:
    """Split a taxon label into ``(name tokens, authority, open_nomenclature)``.

    ``name tokens`` keeps the genus and every epithet that is part of the NAME,
    in print order. A trailing authority is peeled off -- parenthesised
    ("... (Nishimura)") or bare ("... carinatatum Ehrenberg") -- because an
    authority is not an epithet and must not make a name look binomial.

    The authority test is positional, not lexical: it can only START at the
    second token, since the first token is the genus and a genus is always
    capitalised. That is what keeps "Orbula sp." (one name token plus an
    open-nomenclature abbreviation) apart from "Bekoma Foreman" (a genus plus
    an authority), and it needs no authority lexicon, so it cannot mistake a
    capitalised surname for a species epithet.

    ``open_nomenclature`` reports whether an explicit ``sp.``/``spp.`` marked
    the label genus-level. Such a label is complete ON PURPOSE, so it must
    never be treated as a truncated one.
    """
    text = _stringify_scalar(name).strip()
    if not text:
        return [], "", False
    tokens = text.split()
    authority = ""
    # Parenthesised authority anywhere in the label ("... (Nishimura)").
    while tokens and tokens[-1].endswith(")") and tokens[-1].count("("):
        authority = (tokens[-1] + " " + authority).strip()
        tokens.pop()
    # Bare authority: a capitalised token from position 1 onwards, together
    # with the abbreviation run that commonly follows it ("Li and Matsuoka sp.
    # nov.").
    if len(tokens) >= 2 and tokens[1][:1].isupper():
        authority = " ".join(tokens[1:]).strip()
        tokens = tokens[:1]
    # Trailing qualifier run ("... cf.", "... aff.").
    while len(tokens) > 1 and tokens[-1].lower() in _NAME_QUALIFIERS:
        tokens.pop()
    open_nom = any(t.lower() in _NAME_OPEN_NOMINATURE for t in tokens[1:])
    return tokens, authority, open_nom


def _is_ascii_lowercase_start(token: str) -> bool:
    """True when *token* begins with an ASCII lowercase letter.

    The wrap repair reasons about Latin binomials ("robustus", "demissa"), and
    ASCII is the right notion of "identifier character" here for the same
    reason the iron rule and the zone_type ladder above spell their lookarounds
    explicitly: a CJK or Greek letter has no case, so ``str.islower()`` reads it
    as "not capitalised" and a genus like "图zone" would test as a bare epithet.
    """
    return bool(token[:1]) and "a" <= token[0] <= "z"


def _is_wrapped_header_fragment(name: Any) -> bool:
    """True when *name* is ``<epithet> <Authority>`` -- a wrapped header's tail.

    The authority is REQUIRED, and that is the sharpest line in this whole
    repair. A lone lowercase word is not enough evidence: it is exactly what
    the committed parity fixture ``rc_iron_rule_boundaries`` feeds the iron
    rule as a NON-zone ("zozone", "zoness"), so acting on the name alone would
    delete rows a pinned contract says must survive. Pairing an epithet with
    the capitalised surname that follows it is what makes the shape a
    fragment: no taxon is named "robustus Nishimura", but "robustus" and
    "Nishimura" are both real pieces of one wrapped column header.

    The remaining token must start with an ASCII lowercase letter, because a
    genus is capitalised by convention in every one of these payloads, so
    "Orbula sp." / "Reophax spp." and the bare genus "Reophax" are not
    fragments.
    """
    tokens, authority, open_nom = _name_label_parts(name)
    if open_nom or len(tokens) != 1 or not authority:
        return False
    return _is_ascii_lowercase_start(tokens[0])


def _is_bare_epithet(name: Any) -> bool:
    """True when *name* reduces to a lone Latin epithet.

    Weaker than :func:`_is_wrapped_header_fragment` on its own -- a lone
    lowercase word is also what the iron-rule fixtures use for a label that is
    not a zone. So this is only ever consulted as the TAIL of a repair, where
    the preceding row has already been proven to be a genus and nothing else
    and therefore has no other epithet the word could belong to.
    """
    tokens, _authority, open_nom = _name_label_parts(name)
    if open_nom or len(tokens) != 1:
        return False
    return _is_ascii_lowercase_start(tokens[0])


def _is_wrappable_header_base(name: Any) -> bool:
    """True when *name* is a genus and nothing else -- provably incomplete.

    A label with no epithet and no ``sp.``/``spp.`` is a genus on its own, so
    a bare epithet printed immediately after it can only be that genus's
    species: the pairing is forced by the plate, not guessed. "Orbula sp." is
    excluded on purpose -- the abbreviation says the genus is the whole
    determination, so nothing follows it.
    """
    tokens, _authority, open_nom = _name_label_parts(name)
    if open_nom or len(tokens) != 1:
        return False
    return bool(tokens[0][:1].isupper())


def _is_bare_authority(name: Any) -> bool:
    """True when *name* is a single unpunctuated capitalised word.

    That is the shape of an authority that lost its genus ("Foreman",
    "Nishimura") -- and it is AMBIGUOUS, because it is also exactly what a
    genus-level determination looks like. It is therefore never used to flag or
    drop a row: the committed parity fixture ``rc_string_row_biozones`` reads
    ``{"species": "Beta"}`` off a bare string and is a legitimate label, so any
    flag on this shape is a false positive by construction, and deleting on a
    coin-flip is the destructive half of this repository's rule that an
    unsupported row is worse than a missing one. It is used in exactly one
    place: :func:`_guard_wrapped_header_rows` refuses to build a repair on a
    base this ambiguous, so the one decision it drives can only ever refuse.

    A genus followed by ``?`` or ``sp.`` is punctuated or abbreviated and
    returns False.
    """
    tokens, authority, open_nom = _name_label_parts(name)
    if open_nom or authority or len(tokens) != 1:
        return False
    token = tokens[0]
    return bool(token[:1].isupper()) and token.isalpha()


def _row_is_iron_rule_rejected(row: dict[str, Any]) -> bool:
    """True when the iron-rule pass already rejected *row*'s name.

    A label it rejected is a zone label, not a taxon name, so it can be
    neither the tail of a wrapped header nor the genus one completes --
    "Postouwia Zone" reads as genus-plus-authority to the name parser and
    would otherwise swallow the fragment that follows it. The two rules answer
    different questions and the older, fixture-pinned one wins.

    Reads ``_warning`` in both shapes :func:`_add_row_flag` writes (one flag as
    a bare string, several as a list).
    """
    existing = row.get("_warning")
    if isinstance(existing, list):
        return "iron_rule_zone_label" in existing
    return existing == "iron_rule_zone_label"


def _guard_wrapped_header_rows(rows: list[dict[str, Any]],
                                root_warnings: list[str],
                                ) -> list[str]:
    """Repair or drop the rows a two-line column header was split into.

    Returns the text of every row that was removed WITHOUT being rejoined, for
    the caller to preserve under ``_extras`` -- the same "warn, and keep the
    raw text" contract ``_fold_axis_calibration`` uses for a calibration block
    it cannot use. A tail consumed by a repair is not reported, because its
    text is still on the plate, in the row that now carries it.

    Repair fires only when the preceding row is a genus and nothing else, so
    which row the fragment belongs to is decided by the plate rather than by
    list position alone (see :func:`_is_wrappable_header_base`). Everything
    else is dropped, because on the measured figure the two candidate columns
    carry the SAME stem ("Bekoma? cf. demissa" twice) and their endpoint
    readings straddle both columns' bars -- there is no evidence in the
    payload that picks one, and inventing a FAD/LAD would be worse than the
    missing row.

    Three ways a row can be neither dropped nor repaired, all of which leave
    it untouched: the iron rule already rejected it as a non-taxon; it is the
    first row, so there is no base to test it against (a lone epithet is not
    enough on its own); or its predecessor was itself consumed by an earlier
    repair, leaving no base.
    """
    dropped: list[str] = []
    joined = False
    doomed: set[int] = set()
    for i, row in enumerate(rows):
        name = _stringify_scalar(row.get("species"))
        if _row_is_iron_rule_rejected(row) or i == 0 or (i - 1) in doomed:
            continue
        base = rows[i - 1]
        base_name = _stringify_scalar(base.get("species"))
        repairable = (not _row_is_iron_rule_rejected(base)
                      and _is_wrappable_header_base(base_name)
                      and not _is_bare_authority(base_name))
        if repairable:
            if not _is_bare_epithet(name):
                continue
        elif not _is_wrapped_header_fragment(name):
            continue
        else:
            dropped.append(name)
            doomed.add(i)
            continue
        base["species"] = f"{base_name} {name}".strip()
        _add_row_flag(base, "wrapped_header_joined")
        joined = True
        doomed.add(i)
    if not doomed:
        return dropped
    rows[:] = [r for i, r in enumerate(rows) if i not in doomed]
    if joined and "wrapped_header_row_joined" not in root_warnings:
        root_warnings.append("wrapped_header_row_joined")
    if "wrapped_header_fragment_dropped" not in root_warnings:
        root_warnings.append("wrapped_header_fragment_dropped")
    return dropped


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


def _coerce_bool_flag(value: Any, default: bool = True) -> bool:
    """Coerce a model-emitted flag to a real bool.

    REVIEW-2026-09-10: ``bool(value)`` inverts the meaning of the string
    ``"false"`` (non-empty string -> True) and turns ``None`` into False.
    Strings are parsed explicitly; anything unrecognisable falls back to
    ``default`` rather than to a silently-wrong value. Mirrors the JS
    mirror's ``metaRaw.rooted !== false`` intent (absent/null -> default).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("false", "0", "no", "n", "off", "none", "null"):
        return False
    if text in ("true", "1", "yes", "y", "on", ""):
        return True
    return default


def _row_from_string(item: str, kind: str) -> dict[str, Any] | None:
    """Coerce a bare-string list entry into the mode's row shape.

    REVIEW-2026-09-10: ``{"sections": ["Pingdingshan"]}`` / ``{"biozones":
    ["Clarkina postbitteri Zone"]}`` / ``{"species_ranges": ["Clarkina
    yini"]}`` previously dropped every entry silently (both engines). The
    natural reading is one row whose primary identifier is the string, so
    the record is preserved (and flagged by the caller) instead of lost.
    Returns None when the string is blank.
    """
    text = str(item).strip()
    if not text:
        return None
    if kind in ("sections", "biozones", "sites", "zones", "zonations"):
        return {"name": text}
    if kind == "species_ranges":
        return {"species": text}
    # AUDIT-2026-09-27 P1: the paleomap / scatter / chemical tables were
    # reached only through `_dict_rows`, which skipped bare strings outright,
    # so a whole table could vanish with ok=True and NO warning. Only tables
    # that actually have a single natural identifier are listed: a scatter
    # point or a chemical data point needs a coordinate pair to mean anything,
    # so a bare string there is junk, exactly like an abundance row.
    if kind in ("continents", "oceans_seas", "tectonic_features",
                "biogeographic_realms", "fossil_sites", "groups",
                "events", "intervals"):
        return {"name": text}
    if kind == "paleolatitude_indicators":
        return {"type": text}
    if kind == "data_points":
        return {"sample_id": text}
    # Deliberately NOT abundances: a taxon row needs a level/abundance to mean
    # anything, so a bare string there is far more likely to be junk than a
    # record — tests_core pins that it is dropped, and coercing it would
    # invent a taxon with no associated measurement. Same reasoning for
    # scatter ``points`` / ``outliers`` and for ``correlations`` / ``nodes``,
    # which need a second field to identify anything.
    return None


def _single_record_from_dict(raw: Any, kind: str | None) -> dict | None:
    """Return *raw* as ONE record, or None if it is a wrapper-keyed mapping.

    FIX-2026-10-01 (HIGH). A dict-shaped named array has two distinct shapes
    and the old code only understood one:

        (a) a wrapper-keyed mapping  {"Pingdingshan": {...}, "Gaoyuzhuang": {...}}
        (b) a SINGLE record returned where the schema asked for an array
            {"species": "Clarkina yini", "range_base": "Bed 1", ...}

    Both arrive as a ``dict``, so the recovery loop for (a) ran on (b), every
    value was a scalar, every one hit ``if not isinstance(inner, dict): continue``,
    and the ENTIRE table came back empty -- with ``ok=True``, the model's
    declared confidence preserved, no warning, no retry, and one billed API
    call. Measured across all eight modes: sections, species_ranges, sites,
    zones, data_points and points all emptied; ``chemical_stratigraphy``
    additionally FABRICATED an empty row when one value happened to be a dict.

    Discriminator: in shape (b) the kind's primary identifier is a KEY of
    ``raw``; in shape (a) it is not. A value that is itself a dict means we are
    looking at shape (a) even when the wrapper key happens to equal the
    identifier name, so that case is excluded too.
    """
    if not isinstance(raw, dict) or not kind or not raw:
        return None
    id_key = _PRIMARY_ID_KEYS.get(kind)
    if not id_key or id_key not in raw:
        return None
    if isinstance(raw.get(id_key), dict):
        return None  # wrapper-keyed mapping that happens to use the id name
    return dict(raw)


def _iter_rows(raw: Any, kind: str, warnings: list[str] | None = None):
    """Yield dict rows from a list- or dict-shaped named array.

    Generalises the range-chart ``_coerce_list_or_dict`` recovery for the
    other chart modes:
      * list  -> dict items pass through; bare strings are coerced to a row
        whose primary identifier is the string (REVIEW-2026-09-10: they used
        to be skipped with no warning at all).
      * dict  -> each ``{wrapper_key: {...}}`` value is yielded, with the
        wrapper key injected as the primary identifier when that identifier
        is *absent* (a present-but-empty value is the model honestly saying
        "unreadable" and must NOT be overwritten - review finding).
    """
    def _flag(tag: str) -> None:
        if warnings is not None and tag not in warnings:
            warnings.append(tag)

    if isinstance(raw, dict):
        # REVIEW-2026-09-20 #5: a dict-shaped named array is always a repair,
        # even when every record carries its own identifier and no wrapper key
        # had to be injected - the previous code only flagged the injection,
        # so the shape change went unreported.
        _flag("dict_shaped_array")
        # FIX-2026-10-01: a single record returned where an array was asked
        # for. Without this the loop below treated the record's own field
        # values as wrapper entries, every one was a scalar, and the table
        # came back empty. See _single_record_from_dict.
        single = _single_record_from_dict(raw, kind)
        if single is not None:
            yield single
            return
        for wrapper_key, inner in raw.items():
            if not isinstance(inner, dict):
                continue
            row = dict(inner)
            id_key = _PRIMARY_ID_KEYS.get(kind)
            # FIX-2026-10-01: do not invent an identifier from a bare index.
            # A dict-shaped `correlations` keyed by position ({"0": {...},
            # "1": {...}}) is the common emission, and "0"/"1" is not a
            # biozone name -- injecting it produced a FABRICATED `from_zone`
            # that exported to JSON/CSV/XLSX and was indistinguishable from a
            # real zone on the plate. The same shape reached `species`
            # (finding A3) and was only half-fixed: "key absent" is the right
            # test, but an index is not a name either. Leave the field empty
            # (the model honestly said it could not read it) and flag the
            # repair so the operator knows a row lost its identifier.
            if id_key and id_key not in row and not str(wrapper_key).strip().isdigit():
                row[id_key] = str(wrapper_key)
            elif id_key and id_key not in row:
                _flag("identifier_not_recovered_from_index")
            yield row
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item
            elif isinstance(item, str):
                row = _row_from_string(item, kind)
                if row is not None:
                    yield row
                    _flag("string_row_coerced")


def _dict_rows(raw: Any, kind: str | None = None, warnings: list[str] | None = None):
    """Yield dict rows from a list- OR dict-shaped named array.

    REVIEW-2026-09-10: every non-range-chart normalizer did
    ``for x in (parsed.get(k) or [])`` and skipped non-dicts, so a
    dict-shaped emission (``{"sections": {"Ki-1": {...}}}``) produced [] and
    silently discarded every record. Iterating a dict's values recovers them.

    AUDIT-2026-09-27 P1: ``kind`` (optional, and off by default so the existing
    call sites are untouched) additionally coerces a bare-string entry into a
    single-field row and records ``string_row_coerced``, which is what
    ``_iter_rows`` already did for range_chart / abundance / zonation. Without
    it, ``{"continents": ["Laurasia", "Pangaea"]}`` came back as six empty
    tables with ``ok=True`` and no warning at all - the project's own
    definition of silent data loss. Tables with no natural single-field row
    (abundances, scatter points/outliers, correlations, nodes) still skip bare
    strings; see ``_row_from_string``.
    """
    def _flag(tag: str) -> None:
        if warnings is not None and tag not in warnings:
            warnings.append(tag)

    if isinstance(raw, dict):
        # An empty dict is not a record and not a mapping: it carried no
        # records before this branch existed and must still carry none. (The
        # "no dict values" rule below would otherwise turn ``{}`` into one
        # blank row -- found by the edge-case sweep, not by the suites.)
        if not raw:
            return
        # FIX-2026-10-01: a single record returned where an array was asked for
        # has no dict values at all ({"sample_id": "S1", "depth_m": 1.0}), so
        # the loop below yielded nothing and the table came back empty with no
        # warning. A wrapper-keyed mapping necessarily has dict values (each
        # one is a record), so "no dict values" is a kind-INDEPENDENT
        # discriminator between the two shapes and needs no per-kind key map.
        # The record is still a shape repair, so it is flagged the same way.
        has_dict_value = any(isinstance(v, dict) for v in raw.values())
        if not has_dict_value:
            _flag("dict_shaped_array")
            yield dict(raw)
            return
        for value in raw.values():
            if isinstance(value, dict):
                yield value
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item
            elif isinstance(item, str) and kind:
                row = _row_from_string(item, kind)
                if row is not None:
                    yield row
                    _flag("string_row_coerced")


_PRIMARY_ID_KEYS = {
    # REVIEW-2026-09-20 #5: with no entry for "abundances" a dict-shaped
    # abundance field ({"Pinus": {"level": ..., "abundance": ...}}, which the
    # abundance prompt explicitly asks for) lost its taxon to the wrapper key
    # and normalised into an empty-taxon junk row.
    "abundances": "taxon",
    "sections": "name",
    "biozones": "name",
    "species_ranges": "species",
    "sites": "name",
    "zones": "name",
    "zonations": "name",
    "correlations": "from_zone",
    "nodes": "id",
    "data_points": "sample_id",
}


def _merge_other_fossils(existing: Any, raw: Any) -> list[str]:
    """Combine already-collected labels with a parsed ``other_fossils`` field.

    REVIEW-2026-09-20 #1: ``normalize_result`` collected bare strings from an
    ``_array_root`` payload into ``out["other_fossils"]`` and then assigned
    ``_other_fossils_from(parsed["other_fossils"])`` over the top of that
    list, so the salvaged labels disappeared as soon as the payload also
    carried an ``other_fossils`` field. Merge instead: order preserved, exact
    duplicates skipped.
    """
    merged: list[str] = []
    seen: set = set()
    already = [x for x in (existing or []) if isinstance(x, str)]
    for text in already + _other_fossils_from(raw):
        t = (text or "").strip()
        if t and t not in seen:
            seen.add(t)
            merged.append(t)
    return merged


def _other_fossils_from(raw: Any) -> list[str]:
    """Normalise ``other_fossils`` to a list of display strings.

    REVIEW-2026-09-10: dict-shaped entries were dropped by the Python
    normalizer while the browser (js/minimax.js M1 fix) already lifted
    ``label``/``species``/``taxon``/``name`` - so a fossil record with a
    label was visible in the browser and absent from the server/CSV/XLSX.
    Both shapes are now accepted on both sides.
    """
    def _lift(item: Any) -> str:
        if item is None:
            return ""
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            for key in ("label", "species", "taxon", "name", "fossil", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            # No recognisable label: keep the record as JSON text rather
            # than dropping it (_stringify_scalar renders dicts as "").
            #
            # AUDIT-2026-09-29: the `extras` dict built here was assigned and
            # never read (ruff F841) -- a leftover from an earlier design that
            # folded unrecognised keys into a sidecar. The behaviour below is
            # the intended one, so the dead binding is simply gone.
            try:
                return json.dumps(item, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                return _stringify_scalar(item)
        return _stringify_scalar(item)

    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, dict):
        # {"ammonoid": {...}} / {"1": "string"} - iterate the values.
        out: list[str] = []
        for value in raw.values():
            text = _lift(value)
            if text:
                out.append(text)
        return out
    if isinstance(raw, list):
        out = []
        for item in raw:
            text = _lift(item)
            if text:
                out.append(text)
        return out
    return []


def _stringify_scalar(value: Any) -> str:
    """Stringify only scalars; containers return "" instead of a Python repr.

    REVIEW-2026-09-10: ``str({"name": "x"})`` produced
    ``"{'name': 'x'}"`` inside scientific identifier fields (species,
    fossil_marker), i.e. a fabricated-looking value. Containers are handled
    by the callers (lifted or carried into ``_extras``); here they collapse
    to an empty string rather than leaking a repr.

    FIX-2026-10-01: a LIST of scalars is joined rather than emptied. The
    empty-string branch assumed every container would be lifted or rescued by
    ``_carry_extras``, but that only rescues UNKNOWN keys -- and these are
    KNOWN ones (``fossils`` in _KNOWN_PALEOMAP_SITE_KEYS, ``fossil_marker``
    in _KNOWN_SAMPLE_KEYS, ``characteristic_fauna``), so the whole value
    vanished. ``{"fossils": ["radiolaria", "algae"]}`` exported as an empty
    cell, which reads as the affirmative claim "this site records no fossil
    assemblage" -- a worse outcome than dropping the row, because it is a
    false statement about the figure rather than a gap. A comma-joined list
    keeps the information and stays a single scalar cell. A dict still
    returns "" (no faithful scalar rendering, and nested records are lifted by
    the callers).
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [str(v).strip() for v in value
                 if v is not None and not isinstance(v, (dict, list, tuple, set))
                 and str(v).strip()]
        return ", ".join(parts)
    if isinstance(value, dict):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _pop_array_root_extras(extras: dict[str, Any]) -> dict[str, Any]:
    """Drop ``_array_root`` / ``_note`` from a top-level ``_extras`` payload.

    REVIEW-2026-09-10: only the range-chart normalizer stripped these, so
    every other mode duplicated the whole raw payload into the result JSON
    (and inflated quality.py's ``_extras``-ratio check).
    """
    extras.pop("_array_root", None)
    extras.pop("_note", None)
    return extras


def _extracted_any(out: dict[str, Any]) -> bool:
    """True when a normalizer salvaged at least one record / non-empty field.

    Used by the "unusable payload" guard (REVIEW-2026-09-20 #7): a rescued
    fragment that still produced rows is worth keeping, one that produced
    nothing but noise is not.

    FIX-2026-10-17 (P1-2): METADATA IS NOT CONTENT. The loop used to accept
    any non-empty value, and several normalizers emit bookkeeping of their own
    (``warnings`` / ``_warnings``, ``confidence``, ``cell_status``). So a
    payload in which the model produced NOTHING but the normalizer had
    something to say about it counted as "extracted", which is the exact
    opposite of what this predicate is for — and ``_payload_mismatch`` opens
    with ``if _extracted_any(data): return ""``, so the guard it gates could
    not fire at all. Measured on the real occurrence-matrix normalizer:

        occurrences == []                       # every cell read failed
        warnings == ['dropped 1 row(s) for blank cells; ...']
        _payload_mismatch(out, truncated=True) -> ''      # should be an error
        _payload_mismatch(normalize_result({}), truncated=True)
            -> 'truncated output rescued no usable records'   # no bookkeeping

    The list is explicit rather than a prefix rule, and it deliberately does
    NOT contain ``_extras``: a non-empty ``_extras`` is the model handing us
    SOMETHING (``{"totally_unrelated_key": 1}`` parks there for the other
    eight modes, and tests/test_frontend_parity_fixtures_2026_09_20.py pins
    that as a successful extraction), so treating it as bookkeeping inverted a
    documented contract and broke 12 tests when tried.
    """
    for key, value in out.items():
        if key in _NON_CONTENT_KEYS:
            continue
        if isinstance(value, (list, dict)):
            if len(value) > 0:
                return True
        elif value not in (None, "", 0, 0.0, False):
            return True
    return False


#: Keys whose presence says nothing about whether any DATA came out.
#: Read by ``_extracted_any`` only; the normalizers themselves are unchanged.
#: ``_extras`` is intentionally ABSENT — see ``_extracted_any``'s docstring.
_NON_CONTENT_KEYS: frozenset[str] = frozenset({
    # Diagnostic prose the normalizer wrote about its own work.
    "_warnings", "warnings", "note", "_note",
    # Scalars every mode fills in regardless of content.
    "confidence", "confidence_by_section", "confidence_detail",
    # Markers, not records.
    "cell_status", "truncated",
})


def _unwrap_array_root_into(parsed: dict[str, Any], out: dict[str, Any],
                            spec: tuple[tuple[str, tuple[str, ...], Any], ...],
                            warnings: list[str] | None = None) -> None:
    """Distribute a list-shaped root (``_array_root``) into the named arrays.

    REVIEW-2026-09-20 #6: every normalizer but the chemical one unwrapped the
    ``{"_array_root": [...]}`` shape that json_utils produces when the model
    emits a bare array; the chemical mode threw the whole payload away
    (``parsed.get("events")`` was empty), so a perfectly good set of isotope
    points produced an empty table.

    ``spec`` is ``(target_key, distinguishing_keys, target_list)``. The
    distinguishing keys are matched FIRST: ``_classify_array_item`` is a
    range-chart heuristic that maps any dict with a ``name`` onto "sections",
    which would mislabel another mode's rows. Modes whose rows have no stable
    signature pass an empty tuple and get the shared classifier.

    REVIEW-2026-09-20 #7: json_utils only records a truncation flag inside
    ``_extras["_json_recovery"]``, which no consumer reads, so a truncated
    emission that rescued a single INNER row (one data point of a curve) came
    back as ``ok=True`` with everything else empty. The truncation is mirrored
    onto the normalized data and, when nothing could be salvaged, flagged.
    """
    items = parsed.get("_array_root")
    if not isinstance(items, list):
        return
    extras = parsed.get("_extras")
    if isinstance(extras, dict) and extras.get("_json_recovery"):
        out["truncated"] = True
    keys_in_spec = {key for key, _probe, _target in spec}
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = None
        for key, probe, _target in spec:
            if probe and all(k in item for k in probe):
                kind = key
                break
        if kind is None:
            classified = _classify_array_item(item)
            if classified in keys_in_spec:
                kind = classified
        if kind is None:
            # Nothing identifies the row for THIS mode: keep it under
            # ``_unclassified`` rather than dropping it silently.
            out.setdefault("_unclassified", []).append(item)
            continue
        for key, _probe, target in spec:
            if key == kind:
                target.append(item)
                break
    if warnings is not None and not _extracted_any(out):
        _append_warning(warnings, "truncated_or_unrecognized_payload")


def _append_warning(warnings: list[str], tag: str) -> None:
    if tag not in warnings:
        warnings.append(tag)


def _rehydrate_array_root(parsed: dict[str, Any], spec_keys: tuple[tuple[str, tuple[str, ...]], ...],
                          warnings: list[str] | None = None) -> bool:
    """Distribute ``_array_root`` back into ``parsed`` so the normalizer sees it.

    AUDIT-2026-09-27 P0-1: ``_unwrap_array_root_into`` appends the RAW items to
    the target list, which is what the chemical mode wants (it has no
    per-table loop to re-normalize them). The paleomap and scatter normalizers
    DO have per-table loops that emit coerced rows, so the raw items have to
    travel back through ``parsed`` or they would bypass coercion. Handing each
    table a FRESH bucket and only publishing non-empty ones keeps the tables
    that the model answered normally completely untouched, and - critically -
    avoids handing a normalizer's own output list back to its own reader.

    Returns True when an array root was present and distributed.
    """
    items = parsed.get("_array_root")
    if not isinstance(items, list) or not items:
        return False
    buckets: dict[str, list] = {}
    out: dict[str, Any] = {}
    for key, probe in spec_keys:
        buckets.setdefault(key, [])
        out[key] = buckets[key]
    _unwrap_array_root_into(parsed, out, tuple(
        (key, probe, buckets[key]) for key, probe in spec_keys), warnings)
    published = False
    for key, rows in buckets.items():
        if rows:
            parsed[key] = rows
            published = True
    return published


def _payload_mismatch(data: dict[str, Any], result_truncated: bool) -> str:
    """Non-empty reason when the normalized payload is unusable, else "".

    Deliberately narrow (REVIEW-2026-09-20 #7): an empty result WITH an
    explanatory note is an honest degradation the prompt asks for ("do not
    invent data") and stays ``ok=True``; so is a truncated response that still
    produced rows. Only "nothing was extracted AND the root keys do not match
    this mode's contract" (or a truncation that rescued nothing) is an error.
    """
    if not isinstance(data, dict):
        return ""
    if _extracted_any(data):
        return ""
    # FIX-2026-10-17 (P3-4): read BOTH spellings. Eight normalizers write
    # ``_warnings``; this one used to write ``warnings`` (fixed above), and a
    # payload assembled by an older caller or a re-imported JSON can still
    # carry the bare key. Reading only ``_warnings`` meant the
    # ``truncated_or_unrecognized_payload`` tag below was invisible whenever
    # it arrived under the other name, so a rescued inner object was reported
    # as a normal empty result.
    raw_tags = data.get("_warnings")
    if not isinstance(raw_tags, list):
        raw_tags = data.get("warnings")
    tags = raw_tags if isinstance(raw_tags, list) else []
    if "truncated_or_unrecognized_payload" in tags:
        return "rescued inner object: unusable"
    if result_truncated or "truncated" in tags or data.get("truncated"):
        return "truncated output rescued no usable records"
    note = data.get("note")
    if isinstance(note, str) and note.strip():
        return ""
    return ""


def _mode_root_keys(mode_key: str) -> tuple[str, ...]:
    """The documented root keys of a mode, resolved lazily.

    Two shapes come from rca_core/modes.py:

    * ``root_keys_attr`` - the name of a ``_KNOWN_*_ROOT_KEYS`` tuple declared
      NEXT TO ITS NORMALIZER, i.e. after this helper, so the lookup goes
      through ``globals()`` instead of an import-time dict
      (REVIEW-2026-09-20 #7).
    * ``root_keys_inline`` - modes whose key list is a short literal with no
      constant worth naming (phylogenetic_tree, and the two modes added
      2026-10-15).
    """
    spec = _MODE_SPECS.get(mode_key)
    if spec is not None and spec.root_keys_inline:
        return tuple(spec.root_keys_inline)
    names = {
        s.name: s.root_keys_attr
        for s in _MODE_SPECS.values() if s.root_keys_attr
    }
    return tuple(globals().get(names.get(mode_key) or "", ()) or ())


def _usage_int(usage: dict[str, Any] | None, key: str) -> int | None:
    """One integer out of a provider usage block, or None.

    Providers disagree on the spelling (``output_tokens`` vs
    ``completion_tokens``) and any of them may omit the block entirely when the
    call was served from cache or failed midway, so every read is defensive.
    A reported 0 is a real reading and is returned as 0, not as None.
    """
    if not isinstance(usage, dict):
        return None
    if key in usage:
        raw = usage[key]
    elif key == "output_tokens":
        raw = usage.get("completion_tokens")
    else:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _truncation_warning(usage: dict[str, Any] | None, max_tokens: int) -> str:
    """The one truncation sentence, shared by all eleven extractors.

    FIX-2026-10-16: this string was copy-pasted into eleven places and every
    copy said the same two sentences with no numbers in them. That is not
    merely terse - it made a recoverable failure look fatal and an
    unrecoverable one look trivial, because the user could not tell 16000 from
    32000 or see whether the run had already been pushed to the ceiling.

    The concrete case: ``r10_bjorklund`` reported ``output_tokens=16058`` and
    the run was read as "max_tokens is capped, raising it will not help".
    It was not. 16058 was the DEFAULT (DEFAULT_MAX_TOKENS=16000); the clamp
    ceiling is MAX_MAX_TOKENS=32000, and re-running at 32000 recovered the
    result. A warning that cannot be acted on is worse than none, because it
    teaches the reader that the situation is hopeless.

    Reporting all three numbers makes the sentence checkable: what was used,
    what this run allowed, and what the program would permit.
    """
    out_tokens = _usage_int(usage, "output_tokens")
    return (
        "Result may be truncated: the model used %s of the %d max_tokens "
        "allowed for this run (the program ceiling is %d). Raising max_tokens "
        "and re-running recovers the missing rows."
        % ("an unreported number" if out_tokens is None else out_tokens,
           max_tokens, MAX_MAX_TOKENS)
    )


def _merge_usage(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the token usage of several model calls into one block.

    The server adds `input_tokens` / `output_tokens` across the runs of a
    multi-run batch to report what a batch cost, so a path that makes more than
    one call has to report the SUM. Reporting only the last call under-bills
    the run; reporting none at all under-bills it by the whole two-stage
    sequence, which is what the first version of the grid_read path did.
    """
    out: dict[str, Any] = {}
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if key == "estimated":
                out[key] = bool(out.get(key)) or bool(value)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                out[key] = value
                continue
            out[key] = (out.get(key) or 0) + value
    return out


def _grid_read_raw(raws: list[dict[str, Any]]) -> str:
    """The model replies of a two-stage run, as one JSON document.

    The audit trail stores `result.raw` so a researcher can go back to the
    exact model reply later. A two-stage run produced SEVERAL replies, and
    storing none of them (the first version stored "") left the new default's
    runs unreparseable. A wrapper object is used rather than a concatenation
    so the stored text is still valid JSON: the single-call paths store the
    model's bare JSON, and nothing in the codebase re-parses `result.raw`
    automatically, so a clearly-labelled object is both lossless and readable.
    """
    if not raws:
        return ""
    try:
        return json.dumps({"grid_read": raws}, ensure_ascii=False)
    except Exception:
        return ""


def _ok_result(*, p: LlmProvider, mode_key: str, data: dict[str, Any],
               raw_text: str, truncated: bool, usage: dict[str, Any] | None,
               latency_ms: int, image_sha256: str, max_tokens: int,
               parsed: Any = None) -> ExtractResult:
    """Build the success result shared by every ``extract_*`` mode.

    REVIEW-2026-09-20 #7: the "unusable payload" guard existed only on the
    range-chart path; the other seven modes reported a confident-looking empty
    table with ``ok=True`` whenever the model returned an unrelated or
    truncated object. Routing every mode's success return through here gives
    all eight the same contract, and keeps the honest-empty-result degradation
    (an explanatory note) working.
    """
    warning = _truncation_warning(usage, max_tokens)
    request_meta = _build_request_meta(
        p, mode_key, max_tokens, image_sha256,
        prompt_version_for_mode(mode_key))
    # ``truncated`` is an internal marker set by _unwrap_array_root_into.
    rescued_truncated = bool(data.get("truncated"))
    data.pop("truncated", None)
    known = _mode_root_keys(mode_key)
    root_unrelated = bool(
        isinstance(parsed, dict) and parsed and known
        and not (set(known) & set(parsed.keys()))
        and "_array_root" not in parsed
        and not _extracted_any(data))
    reason = _payload_mismatch(data, truncated or rescued_truncated)
    if not reason and root_unrelated:
        reason = "payload root keys match no field of this chart type"
    if reason:
        if root_unrelated:
            _append_warning(
                data.setdefault("_warnings", []),
                "truncated_or_unrecognized_payload")
        return ExtractResult(
            ok=False, error_key="err.parse", raw=raw_text,
            truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
            warning=warning + " | " + reason, data=data,
            image_sha256=image_sha256, request_meta=request_meta)
    return ExtractResult(
        ok=True, data=data, raw=raw_text,
        truncated=truncated, usage=usage or {}, latency_ms=latency_ms,
        warning=warning if truncated else "",
        image_sha256=image_sha256,
        request_meta=request_meta,
    )


def _normalize_section_into(sec: dict[str, Any],
                             target: list[dict[str, Any]]) -> None:
    """Build a section row from a raw dict and append it to ``target``.

    MEDIUM fix: the _array_root handler previously appended the raw dict
    without coercion, so string-where-list-was-expected fields and missing
    keys slipped through unchanged. Routing every item through this helper
    (and the sibling species/biozone helpers) guarantees a uniform shape.
    """
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

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


def _confidence_clamped(value: float) -> float:
    """Clamp a confidence to [0, 1]; NaN becomes 0.0.

    AUDIT-2026-09-30: this is the single gate for every ROOT confidence in this
    module, and it exists because the JS mirror already had one.

    Python's min()/max() do not propagate NaN -- ``min(1.0, nan)`` is 1.0,
    because ``nan < 1.0`` is False -- so a model that emitted
    ``"confidence": "NaN"`` was reported as a PERFECT 1.0 in every mode
    (13 hand-copied unguarded clamps across all 10 normalizers, found by AST
    enumeration rather than grep). js/minimax.js routes all eight of its root
    call sites through ONE function, ``rcaConfidenceClamped``, which guards
    ``Number.isNaN(n) -> return 0.0``; the same payload showed 0 in the
    browser and 1.0 on the desktop.

    Only NaN is remapped. +Infinity still clamps to 1.0 and -Infinity to 0.0,
    which is what BOTH engines already did and what the JS helper does too --
    writing ``if not math.isfinite(v): return 0.0`` here would silently create
    a NEW divergence on +Infinity, so the guard is deliberately isnan, not
    isfinite.

    Honest reporting is also the project's own stance: tests_real_corpus.py
    INV5 treats "empty arrays + confidence 0" as a correct refusal. A NaN is
    the same kind of "the model could not say", and 1.0 asserts the opposite.
    """
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value))


def _normalize_confidence(value: Any) -> float | None:
    """Normalize an optional per-row confidence to the closed interval [0, 1].

    AUDIT-2026-09-30: NaN returns None, not 0.0 -- the ROW variant of the
    contract. js/minimax.js rcaOptionalConfidence (the mirror of this
    function, used for ``species_ranges``/``confidence``) ends with
    ``if (n === null || Number.isNaN(n)) return null``, so an unstateable
    per-row confidence makes the browser OMIT the key while Python used to
    write a confident 1.0. Same root cause as _confidence_clamped, different
    fallback because this one is nullable.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return _confidence_clamped(parsed)


# ---------------------------------------------------------------------------
# BORROW-2026-09-20 (A) — coverage contract (``response_kind`` + ``reason_codes``)
# BORROW-2026-09-20 (B) — PlotLift 0-999 normalised positions -> ``geometry``
#
# Both are strictly ADDITIVE: the keys below only appear on rows whose model
# emission actually carried them, so every existing consumer (exporter,
# editable, history, the JS tables) sees the shapes it saw before.
#
# (A) ``response_kind`` is the three-state answer the prompts now demand —
# ``extracted`` / ``not_drawn`` / ``uncertain`` — and ``reason_codes`` the
# machine-readable why.  The distinction that matters scientifically is
# ``not_drawn`` ("the author drew a dash: this taxon is absent here", a
# POSITIVE observation) versus a silent omission (the model skipped the cell),
# which is a gap.  See rca_core/reason_codes.py.
#
# (B) ``pos_0_999`` fields are the model's independent pixel read of the SAME
# boundary it also transcribed as text ("Bed 9").  Converting them needs an
# axis calibration, which is optional; without one the raw 0-999 integer is
# kept as evidence only.  It NEVER overwrites the semantic value — that is the
# self-echo guard: derived numbers stay in the sidecar ``geometry`` block, and
# a block that contradicts the transcribed value is thrown away instead of
# being averaged into it.
# ---------------------------------------------------------------------------

#: Bare field name for a row's single vertical position.
POS_SCALE_KEY = "pos_0_999"
#: Suffix for a per-boundary position (``range_top_pos_0_999``, ``x_pos_0_999``…).
POS_SUFFIX = "_pos_0_999"
POS_MIN = 0
POS_MAX = 999
#: Version + scale tag written into every ``geometry`` block so a future
#: consumer (rca_core/geometry.py) can tell what the numbers mean.
GEOMETRY_VERSION = 1
GEOMETRY_SCALE = "pos_0_999"
#: How far outside the calibrated axis domain a converted position may sit
#: (fraction of the axis span) before the whole geometry block is discarded.
GEOMETRY_TOLERANCE = 0.15
#: Axis names the calibration block may carry.
GEOMETRY_AXIS_NAMES = ("vertical", "x", "y")

#: Row-level source keys the coverage contract consumes. They are added to
#: every ``_carry_extras`` known-key tuple so a model-emitted ``response_kind``
#: lands on the row as ``response_kind`` and never as a duplicate under
#: ``_extras`` (BORROW-2026-09-20 A).
_CONTRACT_SOURCE_KEYS = ("response_kind", "reason_codes", "reason_code")

#: geometry field -> axis name. Deliberately NOT a catch-all: an unknown
#: ``<prefix>_pos_0_999`` field maps to the axis named ``<prefix>`` only when
#: the result actually calibrates that axis, so a bar-length read can never
#: silently borrow the vertical (stratigraphic) axis and produce a plausible
#: but wrong engineering value.
_GEOMETRY_AXIS_BY_FIELD = {"x_pos_0_999": "x", "y_pos_0_999": "y"}
#: Fields that ARE the vertical axis by contract (per the prompt wording).
_GEOMETRY_VERTICAL_FIELDS = frozenset({
    "pos_0_999", "depth_pos_0_999", "level_pos_0_999", "age_pos_0_999",
    "range_top_pos_0_999", "range_base_pos_0_999",
    "top_pos_0_999", "base_pos_0_999",
})


def _geometry_axis_for(field: str,
                       axes: dict[str, dict[str, Any]] | None
                       ) -> str | None:
    """Which calibrated axis a ``*pos_0_999`` field belongs to (or None)."""
    if field in _GEOMETRY_AXIS_BY_FIELD:
        return _GEOMETRY_AXIS_BY_FIELD[field]
    if field in _GEOMETRY_VERTICAL_FIELDS or field == POS_SCALE_KEY:
        return "vertical"
    if field.endswith(POS_SUFFIX):
        prefix = field[: -len(POS_SUFFIX)]
        if prefix and (axes or {}).get(prefix) is not None:
            return prefix
    return None


#: geometry field -> row fields holding the same boundary as a readable value.
#: Used ONLY as a contradiction test, never as a source for the geometry.
_GEOMETRY_SEMANTIC_KEYS = {
    "pos_0_999": ("depth", "depth_m", "age_ma", "y", "level"),
    "y_pos_0_999": ("y",),
    "x_pos_0_999": ("x",),
    "range_top_pos_0_999": ("range_top_idx", "range_top"),
    "range_base_pos_0_999": ("range_base_idx", "range_base"),
    "depth_pos_0_999": ("depth", "depth_m"),
    "age_pos_0_999": ("age_ma", "age"),
    "level_pos_0_999": ("level", "depth", "depth_m"),
    # FIX-2026-09-22 (audit item 5): the chemical-stratigraphy interval rows
    # use top_pos_0_999 / base_pos_0_999 (both are in _GEOMETRY_VERTICAL_FIELDS)
    # against top_depth_m/top_age_ma and base_depth_m/base_age_ma.  Without the
    # pairing the anti-self-echo contradiction test silently never ran for that
    # mode, so a geometry block contradicting the transcribed interval was kept.
    "top_pos_0_999": ("top_depth_m", "top_age_ma"),
    "base_pos_0_999": ("base_depth_m", "base_age_ma"),
}


def _add_row_flag(row: dict[str, Any], tag: str) -> None:
    """Attach a ``_warning`` to a row using the extractor's own convention.

    Single flag -> string, several -> list (same shape
    ``aggregate._add_row_warning`` writes, so every consumer reads one format).
    """
    existing = row.get("_warning")
    if existing in (None, ""):
        row["_warning"] = tag
        return
    flags = list(existing) if isinstance(existing, list) else [existing]
    if tag not in flags:
        flags.append(tag)
    row["_warning"] = flags[0] if len(flags) == 1 else flags


def _to_float_opt(value: Any) -> float | None:
    """Best-effort float for a scientific value; non-numeric text -> ``None``.

    AUDIT-2026-09-30: js/viz.js#rcaVizNum is documented as the mirror of this
    function, and the two disagreed on 11 of 42 probed inputs. The worst was
    ``"260 Ma"`` -- the most ordinary age notation there is -- which this side
    refused while the browser read 260 and drew the endpoint. That is not a
    rounding difference: ``_axis_domain`` returns None when EITHER end fails
    to parse, so a model that wrote ``{"at_0": "0 Ma", "at_999": "120 Ma"}``
    produced a calibrated chart in the browser and an UNcalibrated one on the
    desktop, with the row's point geometry dropped for want of a domain.

    The mirror was also the stricter side on plain numeric text: ``"1.2e2"``,
    ``"1e5"``, ``".5"`` and ``"5."`` all parse here and returned null there.
    The browser's mirror now tries ``Number()`` first for the same reason.

    So each side now accepts what the other already accepted, and nothing
    more: the number grammar is Python's, and the unit suffix is the mirror's
    explicit ``ma | m.a. | megaa`` list, which is age notation this module
    legitimately meets in ``at_0`` / ``top_age_ma`` positions.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # AUDIT-2026-09-30: a non-finite number is not a number, and the
        # mirror already said so -- js/viz.js#rcaVizNum returns null for NaN
        # and +-Infinity because it tests isFinite. Here they went straight
        # through `float(value)`, and this function feeds the plot-frame
        # maths: one NaN endpoint poisons _axis_domain, the whole calibration
        # is dropped, and a NaN that does survive into a position breaks the
        # render rather than degrading it. Same class as the confidence-clamp
        # and the phylo metadata defects, one layer down.
        f = float(value)
        return f if math.isfinite(f) else None
    if not isinstance(value, str):
        return None
    # FIX-2026-10-01: a comma is only a DECIMAL separator when it is not a
    # thousands separator. The old line was a blind `.replace(",", ".")`, so
    # "1,000" became "1.000" and float() read it as 1.0 -- measured on both
    # engines (`_to_float_opt("1,000")` -> 1.0, and js/viz.js#rcaVizNum gave the
    # same 1). Since a range_base/range_top_idx carrying "1,000" then plotted
    # Bed 1000 as Bed 1, one row spanned the entire stratigraphic axis and the
    # domain was truncated, with no warning anywhere. The rule is the standard
    # one: a comma/underscore/space followed by EXACTLY three digits (and not
    # another digit) is a group separator; any comma left over is a European
    # decimal point, which is why the replace was there in the first place.
    # "1,5" -> 1.5 and "12,5" -> 12.5 are unchanged.
    # Ambiguity accepted and documented: "3,141" is read as 3141 (a group), not
    # as a European decimal with three places. Both engines apply the identical
    # rule, so they stay in parity.
    text = re.sub(r"([,_  ])(\d{3})(?!\d)", r"\2", value.strip())
    text = text.replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        # "260 Ma" / "120 m.a." / "1e5 Ma" / "1_000 Ma": float() rejects the
        # unit suffix, and the mirror accepts it. Numbers with an exponent or
        # an underscore separator are spelled the way float() already takes
        # them, so they only need the suffix stripped.
        m = re.fullmatch(
            r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:_\d+)*)"
            r"\s*(?:ma|m\.a\.|megaa)?\s*",
            text, re.IGNORECASE)
        return float(m.group(1)) if m else None


def normalize_pos_0_999(value: Any) -> int | None:
    """Strictly parse a PlotLift position: an INTEGER in [0, 999], else None.

    Deliberately unforgiving — ``"712"`` is fine, ``712.5`` / ``1000`` /
    ``-1`` / ``True`` are not. A rejected value must not be silently clamped
    into a plausible-looking position.
    """
    if value is None or isinstance(value, bool):
        return None
    parsed: float | None = None
    if isinstance(value, int):
        parsed = float(value)
    elif isinstance(value, float):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    if parsed is None or not float(parsed).is_integer():
        return None
    pos = int(parsed)
    if pos < POS_MIN or pos > POS_MAX:
        return None
    return pos


def _axis_domain(raw: Any) -> dict[str, Any] | None:
    """Normalise one axis calibration entry to ``{at_0, at_999, unit}``.

    Accepted shapes (all optional, model-emitted)::

        {"vertical": {"at_0": 1, "at_999": 24, "unit": "bed"}}
        {"vertical": {"bottom": 1, "top": 24}}
        {"vertical": [1, 24]}

    ``at_0`` is the value at the BOTTOM (oldest) edge of the plot frame and
    ``at_999`` the value at the TOP (youngest) edge, i.e. exactly the ends the
    0-999 scale is defined against.  Returns None unless both ends are numeric.
    """
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        low, high = (_to_float_opt(raw[0]), _to_float_opt(raw[1]))
        return {"at_0": low, "at_999": high, "unit": ""} if (
            low is not None and high is not None) else None
    if not isinstance(raw, dict):
        return None
    low = high = None
    for key in ("at_0", "bottom", "base", "oldest", "min", "start", "0"):
        if key in raw:
            low = _to_float_opt(raw.get(key))
            if low is not None:
                break
    for key in ("at_999", "top", "ceiling", "youngest", "max", "end", "999"):
        if key in raw:
            high = _to_float_opt(raw.get(key))
            if high is not None:
                break
    if low is None or high is None:
        return None
    unit = raw.get("unit")
    # FE-FIX-2026-09-22 (item 7): bool is an int subclass, so True used to
    # sail through the isinstance gate and export the unit "True" - while
    # the JS mirror (js/minimax.js rcaAxisDomain) emitted "". Both engines
    # now REJECT a bool unit to "" (the auditor's cleaner of the two
    # conventions; a boolean is not a unit of measurement).
    if isinstance(unit, bool):
        unit = ""
    return {"at_0": low, "at_999": high,
            "unit": str(unit).strip() if isinstance(unit, (str, int, float)) else ""}


def _axis_calibration_blocks(payload: Any) -> list[dict[str, Any]]:
    """Every raw ``axis_calibration`` block a payload carries, in match order.

    Root, then ``metadata``, then ``_extras`` — models put figure-level blocks
    in whichever of the three they prefer. Non-dict / empty blocks are dropped
    (there is nothing to read out of them).
    """
    blocks: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return blocks
    for holder in (payload, payload.get("metadata"), payload.get("_extras")):
        if not isinstance(holder, dict):
            continue
        candidate = holder.get("axis_calibration")
        if isinstance(candidate, dict) and candidate:
            blocks.append(candidate)
    return blocks


def axis_domains_from(payload: Any) -> dict[str, dict[str, Any]]:
    """Collect the optional ``axis_calibration`` block from a parsed result.

    Looks at the root, ``metadata`` and ``_extras`` — models put figure-level
    blocks in whichever of the three they prefer.  Unknown axis names are kept
    verbatim so a future axis (``"thickness"``) needs no code change here.
    """
    out: dict[str, dict[str, Any]] = {}
    for candidate in _axis_calibration_blocks(payload):
        for name, raw in candidate.items():
            domain = _axis_domain(raw)
            if domain and str(name) not in out:
                out[str(name)] = domain
    return out


def axis_calibration_unusable(payload: Any,
                              axes: dict[str, dict[str, Any]] | None = None,
                              ) -> bool:
    """True when the model claimed an axis calibration that cannot be honoured.

    FIX-2026-09-22 (audit item 7): two shapes used to vanish without a trace —
    the raw block was swallowed by the mode root-key whitelist (so it never
    reached ``_extras``) and no warning said why the geometry stayed raw:

    * a block exists but no axis survives :func:`_axis_domain`, e.g.
      ``{"vertical": {"at_0": 1}}`` where only one end is numeric;
    * every surviving axis is degenerate (``at_0 == at_999``), where the linear
      map would collapse the whole plot onto a single value (see
      :func:`_geometry_point`).
    """
    if not _axis_calibration_blocks(payload):
        return False
    if axes is None:
        axes = axis_domains_from(payload)
    if not axes:
        return True
    try:
        return all(float(d["at_0"]) == float(d["at_999"]) for d in axes.values())
    except (TypeError, KeyError, ValueError):
        return True


def _fold_axis_calibration(parsed: Any,
                           axes: dict[str, dict[str, Any]],
                           out: dict[str, Any],
                           extras_src: dict[str, Any],
                           warnings: list[str],
                           ) -> None:
    """Hoist a usable calibration; warn + keep the raw block when it is not.

    Shared by the four contract modes (range chart, abundance, chemical
    stratigraphy, scatter) so the root shape is defined once:

    * usable  -> ``out["axis_calibration"] = axes``;
    * unusable -> no hoist, an ``axis_calibration_unusable`` root warning and
      the raw block preserved under ``extras_src["axis_calibration"]`` so the
      operator still sees what the model claimed (audit item 7).
    """
    if axis_calibration_unusable(parsed, axes):
        if "axis_calibration_unusable" not in warnings:
            warnings.append("axis_calibration_unusable")
        raw = parsed.get("axis_calibration") if isinstance(parsed, dict) else None
        if isinstance(raw, dict) and raw:
            extras_src["axis_calibration"] = raw
        return
    if axes:
        out["axis_calibration"] = axes


def pos_to_axis_value(pos: int, domain: dict[str, Any] | None) -> float | None:
    """Linearly map a 0-999 frame position onto a calibrated axis."""
    if not domain or pos is None:
        return None
    # FIX-2026-09-22 (audit item 8): ``bool`` is an ``int`` subclass, so a
    # model-emitted ``true`` used to map as position 1 (and ``false`` as 0)
    # and produced a plausible-looking calibrated value.  A boolean is not a
    # position; same rejection as :func:`normalize_pos_0_999`.
    if isinstance(pos, bool) or not isinstance(pos, (int, float)):
        return None
    low, high = domain.get("at_0"), domain.get("at_999")
    if isinstance(low, bool) or isinstance(high, bool):
        return None
    if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
        return None
    return low + (float(pos) / float(POS_MAX)) * (float(high) - float(low))


def _geometry_point(field: str,
                    raw: Any,
                    src_row: dict[str, Any],
                    axes: dict[str, dict[str, Any]] | None
                    ) -> tuple[dict[str, Any] | None, bool, str | None]:
    """One ``*pos_0_999`` field -> ``(entry | None, rejected, axis name)``.

    ``rejected`` is True only for a value that CANNOT be trusted: a non-integer
    or out-of-scale position, or a converted value that contradicts the row's
    own transcribed boundary by more than :data:`GEOMETRY_TOLERANCE` of the
    axis span.  The check against the transcribed value is skipped when the two
    numbers are demonstrably on different scales (the semantic value lies
    outside the calibrated domain — e.g. bed indices against a metre axis), so
    a unit mismatch can never trigger a false rejection.
    """
    pos = normalize_pos_0_999(raw)
    if pos is None:
        return None, True, None
    axis = _geometry_axis_for(field, axes)
    domain = (axes or {}).get(axis) if axis else None
    if domain is None:
        domain = (axes or {}).get(field)
    entry: dict[str, Any] = {"pos": pos, "axis": axis or "uncalibrated"}
    if domain is None:
        # No calibration: the 0-999 integer survives as evidence, nothing more.
        return entry, False, None
    value = pos_to_axis_value(pos, domain)
    if value is None:
        return None, True, None
    lo = min(float(domain["at_0"]), float(domain["at_999"]))
    hi = max(float(domain["at_0"]), float(domain["at_999"]))
    span = hi - lo
    if span <= 0:
        # FIX-2026-09-22 (audit item 4): ``{at_0: 0, at_999: 0}`` is a legal
        # shape for _axis_domain but the linear map degenerates — every
        # position became 0.0 with ``calibrated: true``, and the contradiction
        # guards below (both keyed on ``span > 0``) never ran, so a row whose
        # transcribed depth said otherwise was accepted silently.  A
        # zero-span calibration carries no information: keep the raw 0-999
        # integer as evidence, mark the point rejected (=> low_confidence code)
        # and never write a value.
        return entry, True, None
    tol = GEOMETRY_TOLERANCE * span
    if not (lo - tol <= value <= hi + tol):
        return None, True, None
    for sem_key in _GEOMETRY_SEMANTIC_KEYS.get(field, ()):
        sem = _to_float_opt(src_row.get(sem_key))
        if sem is None or not (lo <= sem <= hi):
            continue
        if abs(value - sem) > tol:
            return None, True, None
        break
    entry["value"] = round(value, 6)
    if domain.get("unit"):
        entry["unit"] = domain["unit"]
    return entry, False, axis


def geometry_from_row(src: dict[str, Any],
                      axes: dict[str, dict[str, Any]] | None = None,
                      *,
                      row: dict[str, Any] | None = None
                      ) -> tuple[dict[str, Any] | None, bool, list[str]]:
    """Build the optional ``geometry`` sidecar for one row.

    Returns ``(geometry | None, rejected, consumed_keys)``:

    * ``geometry`` — ``{"version", "scale", "calibrated", "points", "axes"}``
      with one entry per ``*pos_0_999`` field: ``{"pos": int, "axis": str,
      "value": float?}``.  ``value`` is the engineering value derived from the
      axis calibration; it is absent when the result carries no calibration for
      that axis, in which case the raw 0-999 integer survives as evidence only.
    * ``rejected`` — True when at least one position was unusable, so the
      caller can add a ``low_confidence`` reason code.
    * ``consumed_keys`` — the ``*pos_0_999`` keys folded into the block, so the
      caller keeps them out of ``_extras`` instead of duplicating them.

    A bad point is dropped, the good ones in the same row survive; if nothing
    survives there is no ``geometry`` key at all.  The guard rails are
    one-directional on purpose: geometry can be dropped, it can never change a
    semantic value (the anti-self-echo rule).
    """
    src_row = row if isinstance(row, dict) else (src if isinstance(src, dict) else {})
    points: dict[str, Any] = {}
    used_axes: dict[str, Any] = {}
    consumed: list[str] = []
    rejected = False
    if not isinstance(src, dict):
        return None, False, consumed
    # AUDIT-2026-09-29 (ruff F402): the loop variable was named `field`,
    # shadowing dataclasses.field -- imported at the top of this module and
    # used by every dataclass here, including ExtractResult right above. Inside
    # this loop `field` is a column name, so nothing broke: there is no
    # field(...) call in these 16 lines. But a shadowed name that is "harmless
    # right now" is exactly the one that bites when someone later adds a
    # dataclass or a default_factory inside this function, and the failure
    # would be `'str' object is not callable` far from the cause. Renamed to
    # `name`, which is what it is.
    for name, raw in src.items():
        if not isinstance(name, str):
            continue
        if not (name == POS_SCALE_KEY or name.endswith(POS_SUFFIX)):
            continue
        consumed.append(name)
        entry, bad, axis = _geometry_point(name, raw, src_row, axes)
        if bad:
            rejected = True
        if entry is None:
            continue
        if axis and entry.get("value") is not None and axis not in used_axes:
            domain = (axes or {}).get(axis) or (axes or {}).get(name)
            if domain is not None:
                used_axes[axis] = domain
        points[name] = entry
    if not points:
        return None, rejected, consumed
    geometry: dict[str, Any] = {
        "version": GEOMETRY_VERSION,
        "scale": GEOMETRY_SCALE,
        "calibrated": bool(used_axes),
        "points": points,
    }
    if used_axes:
        geometry["axes"] = used_axes
    return geometry, rejected, consumed


def apply_coverage_contract(src: dict[str, Any],
                            row: dict[str, Any],
                            *,
                            extra_codes: Iterable[str] = ()) -> None:
    """Write the optional coverage fields onto a normalized row.

    Tolerant of every shape the model emits (list / comma string / dict) and
    silent when it emitted nothing at all, so legacy results keep their exact
    old key set.  A ``not_drawn`` claim that comes with a readable value is a
    contradiction the merge layer already resolves in favour of "drawn" — so
    the parser does too, and flags it instead of storing both.
    """
    if not isinstance(src, dict) or not isinstance(row, dict):
        return
    kind = normalize_response_kind(src.get("response_kind"))
    codes = normalize_codes(src.get("reason_codes") or src.get("reason_code"))
    for code in extra_codes:
        slug = normalize_codes([code])[0] if normalize_codes([code]) else None
        if slug and slug not in codes:
            codes.append(slug)
    if kind == RESPONSE_NOT_DRAWN and has_value(row):
        # "not drawn" plus a value = it WAS drawn (or the value is a real 0,
        # which is an answer, not an absence). extracted wins; flag the lie.
        kind = RESPONSE_EXTRACTED
        _add_row_flag(row, "response_kind_conflict")
    if kind:
        row["response_kind"] = kind
    if codes:
        row["reason_codes"] = codes


def attach_row_contract(src: dict[str, Any],
                        row: dict[str, Any],
                        *,
                        axes: dict[str, dict[str, Any]] | None = None,
                        ) -> tuple[str, ...]:
    """One call site per row normalizer: geometry + coverage, additively.

    Folds every ``*pos_0_999`` field of ``src`` into ``row["geometry"]`` (only
    when at least one position survives), writes the coverage contract, and
    returns the extra known-key tuple the caller must hand to
    ``_carry_extras`` so nothing is duplicated under ``_extras``.

    Used by all four continuous/table modes so the contract is defined once
    (BORROW-2026-09-20 A+B).
    """
    geometry, rejected, consumed = geometry_from_row(src, axes, row=row)
    if geometry is not None:
        row["geometry"] = geometry
    apply_coverage_contract(
        src, row, extra_codes=[LOW_CONFIDENCE_CODE] if rejected else ())
    return tuple(consumed)


def _normalize_species_into(sp: dict[str, Any],
                            target: list[dict[str, Any]],
                            *,
                            axes: dict[str, dict[str, Any]] | None = None,
                            ) -> None:
    """Build a species_ranges row from a raw dict and append it."""
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    # REVIEW-2026-09-10: species rows carried their bed indices through
    # _normalize_optional_int with no order check, unlike the columnar path
    # (norm_blocks) which swaps an inverted pair and flags it. An inverted
    # (range_top_idx < range_base_idx) pair was therefore exported as a valid
    # range and nothing downstream caught it - quality.py's FAD/LAD check
    # reads the *string* fields, not the index fields.
    _base_idx = _normalize_optional_int(sp.get("range_base_idx"))
    _top_idx = _normalize_optional_int(sp.get("range_top_idx"))
    _idx_warnings: list[str] = []
    if _top_idx is not None and _base_idx is not None and _top_idx < _base_idx:
        _top_idx, _base_idx = _base_idx, _top_idx
        _idx_warnings.append("index_order_swap")

    # AUDIT-2026-10-15: the indices above are ordered, but the TEXT endpoints
    # were taken verbatim from the model, so the two could disagree — and the
    # text is what the export writes.
    #
    # Measured on Diaz & Velez 2018 Fig. 5 (33 taxa, real extraction):
    # 21 of 28 rows carried
    #     range_base="A5 (606 m)"  range_base_idx=1
    #     range_top ="A1 (607 m)"  range_top_idx =5
    # A1 is the DEEPEST sample (607 m), so it is the OLD end and belongs in
    # range_base; the numeric pair was right and the text pair was transposed.
    # The workbook therefore shipped the older end labelled as the younger one,
    # and quality.py's FAD/LAD check read the text, so it raised 21
    # `range_top_lt_base` warnings while `to_xlsx` still wrote the wrong values
    # — `validate_export_invariants` reported ok=True because
    # `range_base_le_range_top` parses endpoints through the strict
    # `parse_bed`, which returns None for "A1 (607 m)" and so never reached
    # its own comparison.
    #
    # Repair when the two representations CONTRADICT each other: the labels
    # carry their own ordinal (A1 -> 1, A5 -> 5), and when that order disagrees
    # with the index order the text is the transposed half. Swap text + bed
    # labels together and keep the indices, so all three agree afterwards.
    # When the labels cannot be read, or the two orders merely agree, nothing
    # is touched — a wrong "repair" would be worse than the original defect.
    _range_top = s(sp.get("range_top"))
    _range_base = s(sp.get("range_base"))
    _top_bed = s(sp.get("range_top_bed", ""))
    _base_bed = s(sp.get("range_base_bed", ""))
    if _base_idx is not None and _top_idx is not None and _base_idx != _top_idx:
        from .bed_parser import label_ordinal, ordinal_kind
        _ord_base = label_ordinal(_range_base or _base_bed)
        _ord_top = label_ordinal(_range_top or _top_bed)
        _kinds = {ordinal_kind(_range_base or _base_bed),
                  ordinal_kind(_range_top or _top_bed)}
        if (_ord_base is not None and _ord_top is not None
                and _ord_base != _ord_top
                and len(_kinds) == 1 and None not in _kinds
                # opposite signs: labels say one order, indices the other
                and (_ord_base > _ord_top) != (_base_idx > _top_idx)):
            _range_top, _range_base = _range_base, _range_top
            _top_bed, _base_bed = _base_bed, _top_bed
            _idx_warnings.append("endpoint_label_index_swap")

    row = {
        "species": s(sp.get("species")),
        "section": s(sp.get("section")),
        "range_top": _range_top,
        "range_base": _range_base,
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
        "range_top_bed": _top_bed,
        "range_base_bed": _base_bed,
        "range_top_idx": _top_idx,
        "range_base_idx": _base_idx,
        "endpoint_kind": _normalize_endpoint_kind(sp.get("endpoint_kind")),
        # Missing modern classifications remain unknown. Explicit legacy
        # reworked booleans retain their historical compatibility mapping.
        "occurrence_mode": _normalize_occurrence_mode(sp),
        "confidence": _normalize_confidence(sp.get("confidence")),
        # H-8 fix: prompt explicitly asks for a note field for degraded
        # determinations; it was never written to the output row.
        "note": s(sp.get("note", "")),
    }
    # BORROW-2026-09-20 (A+B): geometry + coverage contract, additively.
    consumed = attach_row_contract(sp, row, axes=axes)
    _carry_extras(sp, _KNOWN_SPECIES_KEYS + _CONTRACT_SOURCE_KEYS + consumed, row)
    # P0-4: defensive — if species name looks like a zone, flag & strip.
    sp_name = row["species"]
    if sp_name and _IRON_RULE_ZONE_RE.search(sp_name):
        row["note"] = (row["note"] + " [zone-mislabel-warning]").strip()
    if _idx_warnings:
        # Same single-string / list convention the block rows use, so
        # rcaWarningFlags / _warning_flags consumers read it uniformly.
        # FIX-2026-09-22 (audit item 2 family): additive, because
        # attach_row_contract above may already have flagged this very row
        # (response_kind_conflict) and a plain assignment destroyed it.
        for _w in _idx_warnings:
            _add_row_flag(row, _w)
    target.append(row)


def _normalize_biozone_into(bz: dict[str, Any],
                            target: list[dict[str, Any]]) -> None:
    """Build a biozones row from a raw dict and append it.

    P0-4: enforces zone_type field. Infers zone_type from name keywords
    when not explicitly provided: assemblage, acme, lineage, interval, zonule,
    subzone, oppel, range zone.
    """
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

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
    # AUDIT-2026-10-02: the only two boundary-guarded branches of this ladder
    # (the other seven are plain substring tests and cannot diverge). The guard
    # is written as explicit ASCII lookarounds because Python's `\b` is
    # Unicode-aware on str patterns and ECMAScript's is not: a CJK / Greek /
    # Cyrillic letter touching the keyword is a word character here and not
    # there, so js/minimax.js#rcaNormalizeBiozoneInto inferred "zonule" for
    # "图zonule" and this side fell through to the "biozone" default -- the
    # same figure taking a different zone_type, and so a different biozone
    # row, on the two endpoints. Measured on the `range_chart` parity group.
    #
    # Direction, as with the explicit-age guard: a CJK glyph is not a
    # continuation of an ASCII identifier, so ASCII is the right notion of
    # "adjacent identifier character" here. "zozonule" / "zonules" are still
    # rejected on both engines, and a space or hyphen is still a boundary both
    # engines agree on.
    elif re.search(r"(?<![A-Za-z0-9_])zonule(?![A-Za-z0-9_])", nl):
        inferred_zt = "zonule"
    elif re.search(r"(?<![A-Za-z0-9_])subzone(?![A-Za-z0-9_])", nl):
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
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    out = {
        "sections": [],
        "species_ranges": [],
        "biozones": [],
        "other_fossils": [],
        "confidence": 0.0,
    }
    root_warnings = []

    # BORROW-2026-09-20 (B): one figure-level axis calibration per result. The
    # rows' *pos_0_999 reads are converted against it; absent it they stay raw
    # evidence. Read once here so every normalizer shares the same domains.
    axes = axis_domains_from(parsed)

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
                    _normalize_species_into(item, out["species_ranges"], axes=axes)
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
            # FIX-2026-10-01: flag the shape change here too. This is the only
            # mode that does not route through _iter_rows, so it was the only
            # one that silently repaired a dict-shaped array: the rows came
            # back (name recovered from the wrapper key) and _warnings stayed
            # None, while abundance / zonation / columnar all reported
            # `dict_shaped_array` for the identical shape. The operator could
            # not tell the result had been shape-repaired -- the only trace was
            # _extras.wrapper_key, which CSV/XLSX export does not read.
            if "dict_shaped_array" not in root_warnings:
                root_warnings.append("dict_shaped_array")
            # FIX-2026-10-01: a single record returned where an array was asked
            # for used to empty the whole table silently (ok=True, confidence
            # preserved, no warning, one billed call). See
            # _single_record_from_dict.
            single = _single_record_from_dict(raw, kind)
            if single is not None:
                inner2 = single
                inner2.pop("_extras", None)
                if kind == "sections":
                    _normalize_section_into(inner2, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(inner2, out["species_ranges"], axes=axes)
                elif kind == "biozones":
                    _normalize_biozone_into(inner2, out["biozones"])
                return
            for wrapper_key, inner in raw.items():
                if not isinstance(inner, dict):
                    continue
                inner2 = dict(inner)
                # Fall back: when the inner record has NO primary identifier
                # key at all (no ``name`` for sections/biozones, no
                # ``species`` for species_ranges), use the wrapper key as the
                # primary identifier. This recovers the most common
                # dict-shaped payload (``{'Pingdingshan': {...}}``) without
                # losing the record.
                #
                # REVIEW-2026-09-10: the test must be "key absent", not
                # "value falsy". A record that carries ``species: ""`` is the
                # model following the prompt's instruction to leave an
                # unreadable name empty - overwriting it with the wrapper key
                # fabricated a taxon name (e.g. ``{"unclear": {"species": ""}}``
                # became species "unclear", ``{"0": {...}}`` became "0").
                if kind in ("sections", "biozones") and "name" not in inner2:
                    if not str(wrapper_key).strip().isdigit():
                        inner2["name"] = wrapper_key
                    else:
                        # FIX-2026-10-01: a bare index is not a section or
                        # biozone name. Injecting "0" fabricates an identifier
                        # that exports as if it were printed on the plate.
                        root_warnings.append("identifier_not_recovered_from_index")
                elif kind == "species_ranges" and "species" not in inner2:
                    if not str(wrapper_key).strip().isdigit():
                        inner2["species"] = wrapper_key
                    else:
                        root_warnings.append("identifier_not_recovered_from_index")
                # Drop any pre-existing _extras before re-running the
                # normalizer so we don't end up with a nested ``_extras``
                # field. The normalizer will rebuild it cleanly with
                # ``wrapper_key`` preserved via the _carry_extras merge.
                inner2.pop("_extras", None)
                if kind == "sections":
                    _normalize_section_into(inner2, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(inner2, out["species_ranges"], axes=axes)
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
                    # REVIEW-2026-09-10: a bare string entry
                    # (``{"biozones": ["Clarkina postbitteri Zone"]}``) used
                    # to be skipped in silence (both engines). Coerce it to a
                    # one-field row so the record survives, and flag it.
                    if isinstance(item, str):
                        row = _row_from_string(item, kind)
                        if row is not None:
                            if kind == "sections":
                                _normalize_section_into(row, out["sections"])
                            elif kind == "species_ranges":
                                _normalize_species_into(row, out["species_ranges"], axes=axes)
                            elif kind == "biozones":
                                _normalize_biozone_into(row, out["biozones"])
                            if "string_row_coerced" not in root_warnings:
                                root_warnings.append("string_row_coerced")
                    continue
                if kind == "sections":
                    _normalize_section_into(item, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(item, out["species_ranges"], axes=axes)
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
            # FIX-2026-09-22 (audit item 2 family): additive — the row can
            # already carry index_order_swap / response_kind_conflict and a
            # plain assignment wiped both on the server.
            _add_row_flag(sp, "iron_rule_zone_label")
            if "iron_rule_zone_label" not in root_warnings:
                root_warnings.append("iron_rule_zone_label")

    # AUDIT-2026-10-15 (D1): repair the rows a two-line column header was
    # split into, and drop the ones no evidence can place. See
    # _guard_wrapped_header_rows for the measured figure.
    dropped_fragments = _guard_wrapped_header_rows(
        out["species_ranges"], root_warnings)

    # AUDIT-2026-10-15 (D4): one biozone stamped on every row.
    #
    # Measured on Diaz & Velez 2018 Fig. 5 (`_figtest/diaz_fig5_result.json`):
    # all 28 rows carry the identical
    #     biozone = "Trochammina sp. Zone, Marsonella oxycona Subzone"
    # including the radiolarians drawn OUTSIDE the pink foraminiferal block,
    # while the figure's zonation column is a band that covers only part of the
    # section. Per-row biozone is copied verbatim from the model
    # (_normalize_species_into), so nothing here can correct it: there is no
    # positional evidence in the payload to say which zone a given row sits in,
    # and guessing one would stamp a different fabrication. Two root-level
    # warnings are raised instead, and nothing is rewritten.
    #
    # Root-level, not per-row, on purpose: aggregate.py:1168 records an earlier
    # attempt at a per-row flag for a whole-result pattern that turned four
    # contract tests red, and _warnings is the established vocabulary for a
    # result-level shape problem (dict_shaped_array / string_row_coerced /
    # iron_rule_zone_label all live there). aggregate.py:1454 carries it
    # through a multi-run merge, so the operator still sees it.
    _zones = [s(_bz.get("name")).strip() for _bz in out["biozones"]]
    _zones = [z for z in _zones if z]
    _row_zones = [s(sp.get("biozone")).strip() for sp in out["species_ranges"]]
    _used = [z for z in _row_zones if z]
    if len(_used) >= 3 and len(set(_used)) == 1:
        if "biozone_uniform_across_figure" not in root_warnings:
            root_warnings.append("biozone_uniform_across_figure")
        # The sharper form: the constant is not one of the declared zones at
        # all, it is the figure's ENTIRE zone list joined into one cell -- so
        # the stamp asserts every row belongs to every zone on the plate.
        if (len(_zones) >= 2
                and _used[0] == ", ".join(_zones)
                and "biozone_spans_whole_figure" not in root_warnings):
            root_warnings.append("biozone_spans_whole_figure")

    of = parsed.get("other_fossils") or []
    # Fix B-3: handle case where model returns a string instead of a list.
    # REVIEW-2026-09-10: dict-shaped entries used to be dropped here while
    # the browser (js/minimax.js M1 fix) already lifted their label/species/
    # taxon/name - so a labelled fossil record appeared in the browser and
    # vanished from the server, CSV and XLSX. Both sides now accept both
    # shapes (see _other_fossils_from).
    # REVIEW-2026-09-20 #1: merge, do not replace - the ``_array_root``
    # unwrap above may already have salvaged bare-string labels into
    # out["other_fossils"], and a plain assignment dropped them silently.
    out["other_fossils"] = _merge_other_fossils(out["other_fossils"], of)
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)
    # LOW fix: drop _array_root (and its _note companion) from top-level
    # extras - the per-item rows are already distributed, so the raw
    # payload would be a duplicate.
    extras_src = {k: v for k, v in parsed.items()
                  if k not in _KNOWN_RANGE_CHART_KEYS}
    extras_src.pop("_array_root", None)
    extras_src.pop("_note", None)
    # AUDIT-2026-10-15 (D1): a wrapped-header fragment that no evidence could
    # place is removed from species_ranges, so its text is preserved here
    # rather than lost -- the same "warn, and keep the raw text" contract
    # _fold_axis_calibration below follows for an unusable calibration block.
    if dropped_fragments:
        extras_src["dropped_species_fragments"] = dropped_fragments
    # BORROW-2026-09-20 (B): hoist the optional calibration to a root key so
    # the geometry sidecars' numbers are re-derivable downstream. Absent when
    # the model emitted none — legacy results keep their exact old key set.
    # FIX-2026-09-22 (audit item 7): an unusable block now warns instead of
    # disappearing, and its raw text is kept under ``_extras``.
    _fold_axis_calibration(parsed, axes, out, extras_src, root_warnings)
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="range_chart", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


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
    elif status == 200:
        # REVIEW-2026-09-10: a 200 that produced no usable text is an EMPTY
        # response, not an HTTP error - reporting "HTTP error (HTTP 200)"
        # told the user nothing. The JS engine already uses err.empty here,
        # and the key is translated in all three languages; the reason rides
        # in error_body (see _summarise_empty_payload).
        key = "err.empty"
    return ExtractResult(
        ok=False, error_key=key, status=status, error_body=err_body,
        latency_ms=latency_ms, image_sha256=image_sha256,
    )


# Backward-compat alias used in the success path. Today's code always calls
# the _with_body variant; keeping this name avoids renaming in every caller.
_error_from_status_with_body = _error_from_status


# ---------------------------------------------------------------------------
# Transport retry (REVIEW-2026-09-20 #26)
# ---------------------------------------------------------------------------
# The eight ``extract_*`` entry points used to issue EXACTLY ONE HTTP request,
# so a single 429 / 502 / timeout - by far the most common failure in a batch
# run - was surfaced to the user as a hard error even though the identical
# request made through ``llm.call_llm_api_with_retry`` recovers. Behaviour
# therefore depended on which code path the user happened to trigger.
#
# A local wrapper is used instead of calling ``call_llm_api_with_retry``
# because (a) the modes need ``call_llm_api``'s plain 5-tuple without the
# retry wrapper's re-wrapping, and (b) the test-suite patches
# ``rca_core.extractor.call_llm_api`` - both keep working because the name is
# resolved through this module's globals at CALL time.
#
# Exceptions are deliberately NOT caught here: the modes already guard the
# call and report ``call_llm_api failed: <Type>`` in ``warning`` (see
# tests/test_extraction.py::TestExtractRangeChartNeverRaises).
TRANSPORT_ATTEMPTS = 2                  # 1 normal call + 1 retry
TRANSPORT_BASE_BACKOFF_SEC = 0.8
# Same transient set as llm._RETRYABLE_STATUS: 400/401/403 are client/auth
# mistakes that a retry can only make worse (and that burn quota).
#
# REVIEW-2026-10-19: 529 was missing here too, and this is the wrapper the
# extractor actually uses -- the one the occurrence-matrix path calls. 529 is
# the "overloaded" answer (MiniMax returns `overloaded_error` with it), so a
# congested tier became a failed extraction. On the two-stage path's several
# sequential calls the per-call failure rate compounds into the run's rate:
# MiniMax-M3.1-Flash answered 7 of 8 single requests and then failed every run
# of a multi-call measurement, purely on 529s.
_RETRYABLE_TRANSPORT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504,
                                         529})


def _call_llm(**kwargs):
    """``call_llm_api`` with one bounded retry on transient transport errors.

    Returns the same 5-tuple. The LAST attempt's status is what is returned,
    so ``_error_from_status`` keeps mapping 429 -> ``err.429`` and
    ``None`` -> ``err.network``: the error CONTRACT is unchanged, only
    ``error_body`` gains the ``[transport retry ...]`` annotation that
    ``call_llm_api_with_retry`` already uses. Non-transient answers (2xx, 4xx
    other than the above) never cost an extra request.
    """
    attempts = TRANSPORT_ATTEMPTS
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 2
    notes: list[str] = []
    result = call_llm_api(**kwargs)
    for attempt in range(attempts - 1):
        text, _truncated, status, _err_body, _usage = result
        if text is not None:
            break
        if status is not None and status not in _RETRYABLE_TRANSPORT_STATUS:
            break
        try:
            from . import error_utils
            delay = error_utils.get_retry_delay(
                status=status, headers=None, attempt=attempt,
                initial_delay=TRANSPORT_BASE_BACKOFF_SEC,
            )
        except Exception:
            delay = TRANSPORT_BASE_BACKOFF_SEC
        notes.append(f"[transport retry {attempt + 1}/{attempts - 1}"
                     f" after {delay:.1f}s: HTTP {status if status is not None else 'network'}]")
        time.sleep(delay)
        result = call_llm_api(**kwargs)
    if notes:
        text, truncated, status, err_body, usage = result
        annotated = err_body
        for note in notes:
            annotated = (annotated + "\n" + note) if annotated else note
        result = (text, truncated, status, annotated, usage)
    return result


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
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

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
        for b in _dict_rows(items):
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
        for u in _dict_rows(items):
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
        for s_item in _dict_rows(items):
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
        # REVIEW-2026-09-20 #3: a dict-shaped legend - which is what the model
        # emits when it keys the entries by marker ("legend": {"ammonite":
        # {"meaning": ...}}) - used to fail ``isinstance(x, dict)`` for every
        # value and vanish without a trace. Same tolerant path as norm_cross.
        for x in _dict_rows(items):
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
        for x in _dict_rows(items):
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
    # AUDIT-2026-09-27 P1: a bare-string section row used to be skipped with
    # no warning at all. `sections` is a name-bearing table, so the string is
    # the identifier - coerce it and say so, exactly as range_chart does.
    _columnar_warnings: list[str] = []
    for sec in _dict_rows(parsed.get("sections"), "sections", _columnar_warnings):
        # FIX-2026-10-01: an ABSENT confidence_by_section used to be written as
        # 0.0, byte-identical to a model that explicitly reported 0.0 -- so
        # "the model never said" and "the model said zero" were
        # indistinguishable in every export, and the operator would judge the
        # whole batch as a failed recognition. This module is careful about
        # that distinction elsewhere (the root confidence has a None-aware
        # fallback; row-level _normalize_confidence returns None), so this one
        # coercion was the odd one out. Absent / null / unparsable now stays
        # None ("not stated"); a real number is still clamped to 0..1.
        _raw_cbs = sec.get("confidence_by_section")
        conf_v: float | None
        if _raw_cbs is None or _raw_cbs == "":
            conf_v = None
        else:
            try:
                conf_v = float(_raw_cbs)
            except (TypeError, ValueError):
                conf_v = None
        row = {
            "id": s(sec.get("id")),
            "group": s(sec.get("group")),
            "lithology_blocks": norm_blocks(sec.get("lithology_blocks")),
            "age_units": norm_units(sec.get("age_units")),
            "samples": norm_samples(sec.get("samples")),
            "coordinates_text": s(sec.get("coordinates_text")),
            "thickness_m": s(sec.get("thickness_m")),
            "confidence_by_section": (_confidence_clamped(conf_v)
                                      if conf_v is not None else None),
        }
        _carry_extras(sec, _KNOWN_COLUMNAR_SECTION_KEYS, row)
        sections.append(row)

    try:
        # Models sometimes emit `confidence` at the root instead of the
        # documented `overall_confidence`; fall back so the value isn't
        # silently zeroed (which would also distort aggregate's mean).
        #
        # REVIEW-2026-09-10: `dict.get(key, default)` only falls back when the
        # key is ABSENT, so an explicit ``overall_confidence: null`` (a common
        # model emission) still produced 0.0 and discarded a perfectly good
        # sibling ``confidence``. The browser's mirror uses `!= null`
        # (js/minimax.js) and returned 0.8 for the same payload - the server
        # and the browser disagreed on the same extraction.
        _overall_raw = parsed.get("overall_confidence")
        if _overall_raw is None:
            _overall_raw = parsed.get("confidence", 0.0)
        overall = float(_overall_raw)
    except (TypeError, ValueError):
        overall = 0.0
    overall = _confidence_clamped(overall)

    # Issue-1 fix: unpack tuple return from norm_legend (returns (list, warning))
    fossil_legend, fossil_legend_warn = norm_legend(parsed.get("fossil_legend"))
    lithology_legend, lithology_legend_warn = norm_legend(parsed.get("lithology_legend"))
    # Collect warnings to surface at root level
    legend_warnings = [w for w in (fossil_legend_warn, lithology_legend_warn) if w]
    for w in _columnar_warnings:            # AUDIT-2026-09-27 P1
        if w not in legend_warnings:
            legend_warnings.append(w)

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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="columnar_section", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


# Dispatch table — single entry point for both modes.
_MODE_DISPATCH = {
    "range_chart": extract_range_chart,
    "columnar_section": extract_columnar_section,
    "abundance_diagram": None,  # bound below after the function is defined
    "phylogenetic_tree": None,  # bound below after the function is defined
}


_KNOWN_ABUNDANCE_ROOT_KEYS = ("sites", "abundances", "zones", "confidence",
                              # BORROW-2026-09-20 (B)
                              "axis_calibration")
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
        # REVIEW-2026-09-10: _stringify_scalar, not str(), so a container in a
        # scalar field degrades to "" instead of a Python repr.
        return _stringify_scalar(v)

    warnings: list[str] = []
    out: dict[str, Any] = {
        "sites": [],
        "abundances": [],
        "zones": [],
        "confidence": 0.0,
    }
    # BORROW-2026-09-20 (B): optional figure-level calibration for the
    # continuous axis (depth / abundance value) of the diagram.
    axes = axis_domains_from(parsed)
    # REVIEW-2026-09-10: _iter_rows tolerates a dict-shaped field
    # ({"sites": {"Suigetsu": {...}}}), which previously produced [] and
    # silently discarded every record for this mode.
    for site in _iter_rows(parsed.get("sites"), "sites", warnings):
        row = {
            "name": s(site.get("name")),
            "location": s(site.get("location")),
            "age_range": s(site.get("age_range")),
            "depth_unit": s(site.get("depth_unit")),
        }
        _carry_extras(site, _KNOWN_SITE_KEYS, row)
        out["sites"].append(row)
    for ab in _iter_rows(parsed.get("abundances"), "abundances", warnings):
        row = {
            "taxon": s(ab.get("taxon")),
            "site": s(ab.get("site")),
            "level": s(ab.get("level")),
            "depth": s(ab.get("depth")),
            "abundance": s(ab.get("abundance")),
            "abundance_unit": s(ab.get("abundance_unit")),
        }
        # BORROW-2026-09-20 (A+B): coverage contract + geometry sidecar.
        consumed = attach_row_contract(ab, row, axes=axes)
        _carry_extras(ab, _KNOWN_ABUNDANCE_KEYS + _CONTRACT_SOURCE_KEYS + consumed, row)
        out["abundances"].append(row)
    for z in _iter_rows(parsed.get("zones"), "zones", warnings):
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
    out["confidence"] = _confidence_clamped(conf)
    top_extras = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_ABUNDANCE_ROOT_KEYS})
    # BORROW-2026-09-20 (B): hoist the calibration; absent -> no new key.
    # FIX-2026-09-22 (audit item 7): unusable -> warn + keep the raw block.
    _fold_axis_calibration(parsed, axes, out, top_extras, warnings)
    if top_extras:
        out["_extras"] = top_extras
    if warnings:
        out["_warnings"] = warnings
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="abundance_diagram", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


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
    #
    # REVIEW-2026-09-10: the old form REPLACED `raw` with the inner item, so
    # every outer field the model emitted alongside the array was discarded -
    # e.g. {"_array_root":[{nodes...}], "confidence": 0.9} normalized to
    # confidence 0.0. The wrapper's own keys now fill only the gaps the inner
    # object does not define (setdefault), matching the in-place style the
    # abundance/columnar unwraps already use.
    if "_array_root" in raw and isinstance(raw["_array_root"], list):
        for item in raw["_array_root"]:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("nodes"), list):
                merged = dict(item)
                for key, value in raw.items():
                    if key in ("_array_root", "_note"):
                        continue
                    merged.setdefault(key, value)
                raw = merged
                break

    # REVIEW-2026-09-10: collected repairs to surface on the result.
    phylo_warnings: list[str] = []

    def s(v):
        # Container-safe: a dict/list in a scalar field collapses to "" rather
        # than leaking a Python repr into a scientific field.
        return _stringify_scalar(v)

    def fv(v):
        """Coerce to float or None."""
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # REVIEW-2026-09-10: `_dict_rows` accepts a list OR a dict-shaped
    # ``{"nodes": {"n0": {...}}}`` emission, which previously produced [].
    nodes_in = list(_dict_rows(raw.get("nodes")))

    # REVIEW-2026-09-10: a node with a missing/empty id used to be skipped
    # silently ("continue"), which collapsed the topology - a 2-node tree
    # became a single-node tree and to_newick returned "(Root);". Assign a
    # stable synthetic id instead and flag the repair, so the clade is kept
    # and the operator can see that the id was not in the figure.
    _existing_ids = {str(n.get("id") or "").strip()
                     for n in nodes_in if isinstance(n, dict)}
    _anon = 0
    for n in nodes_in:
        if str(n.get("id") or "").strip():
            continue
        while True:
            _anon += 1
            candidate = f"_anon{_anon}"
            if candidate not in _existing_ids:
                break
        _existing_ids.add(candidate)
        n["id"] = candidate
        phylo_warnings.append("node_missing_id_synthesised")

    # FIX-2026-10-01: a DUPLICATE id. The model reuses one id for two
    # different leaves quite often (it invents "n1"/"1" when the figure prints
    # no node numbers), and every id-keyed structure in this module keeps ONE
    # entry per id. to_newick therefore emitted the second node's name for both
    # and dropped the first entirely: three nodes in, "((Beta,Beta));" out --
    # one taxon silently gone, one duplicated. The table still held all three
    # rows, so nothing looked wrong upstream, and to_newick feeds BOTH the
    # Newick export (rca_core/exporter.py to_newick_file) and the RF-distance
    # metric (rca_core/eval_metrics.py), so the published tree and the score
    # were both wrong. Same treatment as the missing-id case above: keep every
    # taxon, give the later one a unique id, and flag the repair.
    _seen_ids: set[str] = set()
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        if not nid:
            continue
        if nid not in _seen_ids:
            _seen_ids.add(nid)
            continue
        while True:
            _anon += 1
            candidate = f"_dup{_anon}"
            if candidate not in _seen_ids:
                break
        _seen_ids.add(candidate)
        n["id"] = candidate
        phylo_warnings.append("node_duplicate_id_disambiguated")

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
    #
    # AUDIT-2026-09-29: the comprehension was the ONE rejection path in this
    # function that could raise TypeError instead of ValueError. A model that
    # writes a scalar there —
    #     {"root_ids": 42, "nodes": [...]}
    # — made `raw.get("root_ids") or []` yield 42, and iterating an int raises
    # TypeError: 'int' object is not iterable. That escaped every
    # `pytest.raises(ValueError)` contract in tests/test_phylo_parent_null.py
    # and surfaced to the user as `normalize failed: 'int' object is not
    # iterable` (extractor.py catches Exception at the call site), which names
    # an implementation detail instead of anything about their figure.
    #
    # The JS mirror already had this right and handled four shapes explicitly
    # (js/minimax.js:2507-2516): array, string, object (its keys), and
    # anything else -> empty. A scalar is in the last group there, so the
    # browser reported "root_ids is empty" while the desktop raised TypeError
    # on the same payload. Mirroring the branch list keeps the two engines
    # refusing the same payloads for the same stated reason, and restores the
    # ValueError contract this function otherwise keeps everywhere.
    _raw_root_ids = raw.get("root_ids")
    if isinstance(_raw_root_ids, (list, tuple, str, dict)):
        root_ids = [str(r) for r in (_raw_root_ids or [])]
    else:
        # None / int / float / bool: the JS mirror's "anything else" branch.
        root_ids = []
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

    # AUDIT-2026-09-30: `raw.get("metadata") or {}` only rejects FALSY
    # non-mappings, so a truthy string / list / number went straight through
    # and `metadata_raw.get("title", "")` raised
    # AttributeError: 'str' object has no attribute 'get'. That escapes the
    # ValueError contract this function keeps everywhere else, and the user
    # saw "normalize failed: 'str' object has no attribute 'get'" -- an
    # implementation detail instead of anything about their figure. Found by
    # difffuzz_normalize.py once its phylogenetic_tree generator stopped
    # raising on every single case (see that file: node ids were random
    # scalars and the parent key was spelled "parent_id", which this function
    # does not read, so 400/400 cases died before reaching one line of
    # normalisation and "both engines refused" was scored as agreement).
    #
    # The browser mirror failed the same payload differently, and no better:
    # `rcaPyOr(raw.metadata, {})` then `Object.keys("str")` yields ["0","1","2"],
    # so it emitted {"0": "s", "1": "t", "2": "r"} as if a string's character
    # positions were metadata keys. A non-mapping is not a mapping, so both
    # sides now take the SAME shape as the `legend` line three lines below,
    # which was already guarded on both engines: a non-dict is not metadata.
    metadata_raw = raw.get("metadata")
    if not isinstance(metadata_raw, dict):
        metadata_raw = {}
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
        # REVIEW-2026-09-10: `bool(...)` inverted the meaning of the string
        # "false" (non-empty string -> True) and turned an explicit null into
        # False, diverging from the browser mirror (js/minimax.js treats
        # anything that is not exactly false as rooted). _coerce_bool_flag
        # parses the textual forms and keeps the mirror's absent/null default.
        "rooted": _coerce_bool_flag(metadata_raw.get("rooted"), True),
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
    conf = _confidence_clamped(conf)

    root_extras = _pop_array_root_extras(
        {k: v for k, v in raw.items()
         if k not in ("metadata", "nodes", "root_ids", "legend", "confidence")})
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
    if phylo_warnings:
        out["_warnings"] = phylo_warnings
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
    # FIX-2026-10-01: defense in depth for a duplicate id reaching here from
    # a path that bypasses the normalizer's disambiguation (a hand-edited
    # history record, a re-imported JSON payload). Silently keeping the LAST
    # row is what produced "((Beta,Beta));" -- one taxon lost, one duplicated,
    # no warning -- so the collision is now reported with the same convention
    # as the unreachable-node warning below. The first row is kept, which is
    # the one the id-keyed lookup already resolved to.
    _dup_ids: list[str] = []
    for n in nodes:
        if isinstance(n, dict):
            nid = str(n.get("id") or "")
            if nid:
                if nid in nodes_dict:
                    _dup_ids.append(nid)
                    continue
                nodes_dict[nid] = n
    if _dup_ids:
        import warnings as _warnings_mod
        _warnings_mod.warn(
            "to_newick: duplicate node id(s) "
            + ", ".join(repr(d) for d in sorted(set(_dup_ids))[:10])
            + f" ({len(_dup_ids)} row(s)); only the first of each is emitted",
            RuntimeWarning,
            stacklevel=2,
        )

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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="phylogenetic_tree", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


# ============================================================================
# NEW CHART TYPES: Chemical Stratigraphy
# ============================================================================

_KNOWN_CHEMICAL_STRAT_ROOT_KEYS = ("metadata", "data_points", "events", "intervals",
                                   "confidence",
                                   # BORROW-2026-09-20 (B)
                                   "axis_calibration")
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
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    # REVIEW-2026-09-10: repairs to surface on the result.
    _chem_warnings: list[str] = []
    out = {
        "metadata": {},
        "data_points": [],
        "events": [],
        "intervals": [],
        "confidence": 0.0,
    }

    # REVIEW-2026-09-20 #6: this was the only normalizer without the
    # ``_array_root`` unwrap every other mode has - json_utils wraps a bare
    # array emission (the model replying ``[{...}, {...}]`` to an object
    # contract) into {"_array_root": [...]}, and the whole payload was thrown
    # away below because parsed.get("data_points") was empty.
    # BORROW-2026-09-20 (B): depth/age are a continuous axis here, so read the
    # optional calibration once for all rows.
    _chem_axes = axis_domains_from(parsed)
    _unwrap_array_root_into(parsed, out, (
        ("data_points", ("sample_id", "depth_m"), out["data_points"]),
        ("events", ("type", "depth_m"), out["events"]),
        ("intervals", ("name", "top_depth_m"), out["intervals"]),
    ), _chem_warnings)

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
    for pt in _dict_rows(parsed.get("data_points"), "data_points", _chem_warnings):
        values_raw = pt.get("values") or {}
        values = {}
        if isinstance(values_raw, dict):
            for k, v in values_raw.items():
                values[str(k)] = s(v)
        elif isinstance(values_raw, list):
            # REVIEW-2026-09-10: a non-dict `values` (e.g. the model emitting
            # [{"name": "d13C", "value": "-1.24"}]) used to leave values == {}
            # with the raw list unrecoverable - `values` is a known key, so
            # _carry_extras would not preserve it either. That produced a
            # sample row that looks valid but carries no geochemistry.
            # Accept the common list-of-{name,value} shape, and keep anything
            # unrecognised under a companion key rather than losing it.
            for entry in values_raw:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("name", entry.get("element", entry.get("isotope")))
                if label is None:
                    continue
                values[str(label)] = s(entry.get("value", entry.get("val", "")))
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
        _carry_extras(pt,
                      _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS
                      + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(pt, row, axes=_chem_axes), row)
        if values_raw and not values:
            # Nothing could be lifted out of a non-dict `values`. Keep the raw
            # payload on the row so the measurements are not silently lost.
            extras = row.get("_extras")
            if not isinstance(extras, dict):
                extras = {}
                row["_extras"] = extras
            extras["values_raw"] = values_raw
            if "values_unparseable" not in _chem_warnings:
                _chem_warnings.append("values_unparseable")
        out["data_points"].append(row)

    # Normalize events
    # REVIEW-2026-09-20 #4: a dict-shaped ``events`` ({"AEZZ": {...}}) failed
    # the isinstance check for every value and the whole emission vanished
    # silently. Same tolerant path as data_points above (and norm_cross in
    # the columnar mode).
    for ev in _dict_rows(parsed.get("events"), "events", _chem_warnings):
        row = {
            "type": s(ev.get("type")),
            "depth_m": s(ev.get("depth_m")),
            "age_ma": s(ev.get("age_ma")),
            "name": s(ev.get("name")),
            "magnitude": s(ev.get("magnitude")),
            "description": s(ev.get("description")),
        }
        _carry_extras(ev,
                      _KNOWN_CHEMICAL_STRAT_EVENT_KEYS
                      + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(ev, row, axes=_chem_axes), row)
        out["events"].append(row)

    # Normalize intervals
    # REVIEW-2026-09-20 #4: same dict-shaped recovery as events.
    for iv in _dict_rows(parsed.get("intervals"), "intervals", _chem_warnings):
        _iv_top_depth = s(iv.get("top_depth_m"))
        _iv_base_depth = s(iv.get("base_depth_m"))
        _iv_top_age = s(iv.get("top_age_ma"))
        _iv_base_age = s(iv.get("base_age_ma"))
        # FIX-2026-10-01: chemical-stratigraphy intervals had NO order check at
        # all, while columnar's lithology_blocks / age_units swap an inverted
        # pair and flag it and range_chart's species indices do the same. Two
        # independent impossibilities were therefore accepted silently and
        # exported:
        #   * depth pair inverted  (top DEEPER than base, "older above younger")
        #   * age pair inverted    (base YOUNGER than top -- a layer whose top
        #     is older than its base, which cannot exist)
        # A duration computed downstream from an inverted age pair comes out
        # negative. Swap and flag, exactly like the columnar path, so the
        # operator sees the repair instead of inheriting a reversed interval.
        _iv_warnings: list[str] = []

        def _num_or_none(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        _td = _num_or_none(_iv_top_depth)
        _bd = _num_or_none(_iv_base_depth)
        if _td is not None and _bd is not None and _td > _bd:
            _iv_top_depth, _iv_base_depth = _iv_base_depth, _iv_top_depth
            _iv_warnings.append("interval_depth_order_swap")
        _ta = _num_or_none(_iv_top_age)
        _ba = _num_or_none(_iv_base_age)
        # Geologic convention: base is the OLDER end, so its Ma is the LARGER
        # one. base < top means the base is younger -> inverted.
        if _ta is not None and _ba is not None and _ba < _ta:
            _iv_top_age, _iv_base_age = _iv_base_age, _iv_top_age
            _iv_warnings.append("interval_age_order_swap")
        row = {
            "name": s(iv.get("name")),
            "top_depth_m": _iv_top_depth,
            "base_depth_m": _iv_base_depth,
            "top_age_ma": _iv_top_age,
            "base_age_ma": _iv_base_age,
            "characteristic_values": s(iv.get("characteristic_values")),
            "lithology": s(iv.get("lithology")),
        }
        if _iv_warnings:
            row["_warning"] = (_iv_warnings[0] if len(_iv_warnings) == 1
                               else _iv_warnings)
            for _w in _iv_warnings:
                if _w not in _chem_warnings:
                    _chem_warnings.append(_w)
        _carry_extras(iv,
                      _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS
                      + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(iv, row, axes=_chem_axes), row)
        out["intervals"].append(row)

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)
    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items()
         if k not in _KNOWN_CHEMICAL_STRAT_ROOT_KEYS})
    # BORROW-2026-09-20 (B): hoist the calibration; absent -> no new key.
    # FIX-2026-09-22 (audit item 7): unusable -> warn + keep the raw block.
    _fold_axis_calibration(parsed, _chem_axes, out, extras_src, _chem_warnings)
    if extras_src:
        out["_extras"] = extras_src
    if _chem_warnings:
        out["_warnings"] = _chem_warnings
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="chemical_stratigraphy", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


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

# AUDIT-2026-09-27 P0-1: the row tables of a paleogeographic map, most
# specific probe first. ``_classify_array_item`` is a RANGE-CHART heuristic
# that maps any dict carrying a ``name`` onto "sections", which is not a
# paleomap table at all - so without these probes every array item fell
# through to ``_unclassified`` and the map came back empty. A bare
# ``{"name": ...}`` on a paleogeographic map is a landmass, hence the
# ``continents`` catch-all last.
_PALEOMAP_ARRAY_SPEC_KEYS = (
    ("paleolatitude_indicators", ("type", "coordinates")),
    ("fossil_sites", ("lat_lon",)),
    ("fossil_sites", ("marker_type",)),
    ("biogeographic_realms", ("characteristic_fauna",)),
    ("tectonic_features", ("direction",)),
    ("continents", ("paleolatitude",)),
    ("oceans_seas", ("name", "type", "coordinates")),
    ("continents", ("name",)),
)


def first_non_empty(candidates: Any) -> Any:
    """The first candidate that carries a value, else ``None``.

    AUDIT-2026-09-27 [item 7.1] (found by a repo-wide unbound-name scan).
    ``_one_point`` below has called this since paleomap was added, but the
    name was never defined in this module — so every paleomap row whose
    ``coordinates`` arrived as a dict raised ``NameError``. ``extract_paleomap``
    wraps the normaliser in ``except Exception`` and downgrades it to
    ``ok=False`` with a warning string, so the failure presented as
    "extraction failed" and the whole result was lost. It only escaped notice
    because the model often emits the other two coordinate shapes (a
    ``[lat, lon]`` list, or a ``"12N 30E"`` string) which take different
    branches — a data-loss bug whose trigger is model-output-dependent, i.e.
    the worst kind to catch by hand.

    "Carries a value" deliberately means *not None and not an empty
    container/string* — NOT truthiness. ``0.0`` is a real latitude (the
    equator) and longitude (the prime meridian), and a plain
    ``next((v for v in xs if v), None)`` would discard exactly those rows.
    The semantics match ``rca_core.exporter._nonempty``.
    """
    if candidates is None:
        return None
    try:
        iterable = list(candidates)
    except TypeError:
        return candidates if candidates is not None else None
    for v in iterable:
        if v is None:
            continue
        if isinstance(v, (list, dict, str, tuple, set)) and len(v) == 0:
            continue
        return v
    return None


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
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    # REVIEW-2026-09-10: repairs to surface on the result.
    _paleomap_warnings: list[str] = []
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

    # AUDIT-2026-09-27 P0-1: this was the only remaining normalizer without
    # the ``_array_root`` unwrap. json_utils wraps a bare-array emission (the
    # model replying ``[{...}, {...}]`` to an object contract) into
    # ``{"_array_root": [...]}``; ``parsed.get("continents")`` was then empty,
    # ``_pop_array_root_extras`` DELETED the key, and the whole map came back
    # as ``ok=True`` with six empty tables and no warning.
    _rehydrate_array_root(parsed, _PALEOMAP_ARRAY_SPEC_KEYS, _paleomap_warnings)

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

    def _one_point(item):
        """A single coordinate -> [lat, lon], or None when it is not one."""
        if isinstance(item, dict):
            lat = first_non_empty(
                (item.get("lat"), item.get("latitude"), item.get("y")))
            lon = first_non_empty(
                (item.get("lon"), item.get("lng"), item.get("longitude"),
                 item.get("x")))
            if lat is None and lon is None:
                return None
            item = [lat if lat is not None else "",
                    lon if lon is not None else ""]
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            if any(isinstance(p, (list, tuple, dict)) for p in item[:2]):
                return None          # a ring / nested collection, not a point
            try:
                return [float(item[0]), float(item[1])]
            except (TypeError, ValueError):
                return [s(item[0]), s(item[1])]
        return None

    def norm_coords(val, label="coordinates"):
        """Normalize coordinates to a list of [lat, lon] pairs.

        REVIEW-2026-09-20 #16: models emit at least four shapes for this one
        field - ``[[lat, lon], ...]``, a bare ``[lat, lon]`` single point,
        ``{"lat": .., "lon": ..}`` / ``{"latitude": .., "longitude": ..}``
        objects, and dicts of named points. Only the first was accepted, so a
        perfectly readable feature came back as "no coordinates at all"
        without a word about it. Unparseable payloads now warn.
        """
        pts: list[Any] = []
        if isinstance(val, dict):
            direct = _one_point(val)
            if direct is not None:
                return [direct]
            pts = [p for p in (_one_point(v) for v in val.values()) if p]
        elif isinstance(val, (list, tuple)):
            direct = _one_point(val)
            if direct is not None and len(val) == 2:
                return [direct]      # a single [lat, lon] point
            for item in val:
                p = _one_point(item)
                if p is not None:
                    pts.append(p)
                elif isinstance(item, (list, tuple, dict)):
                    pts.extend(norm_coords(item, label))
        if pts or val in (None, "", [], {}):
            return pts
        _paleomap_warnings.append(f"coordinates_unparsed:{label}")
        # AUDIT-2026-09-27 [item 7.4]: a coordinate the model wrote as TEXT is
        # not junk — "48.1N 12.0E" is exactly what a geologist wants to see,
        # and every paleomap table routes through this function, so returning
        # [] here discarded the value in all six of them. The warning stays
        # (the string was not machine-parsed), but the value is kept rather
        # than thrown away; the GUI renders this column with `col.coordinates`
        # and displays a string there without complaint.
        #
        # The test is "contains a digit", not merely "is a non-empty string":
        # the models also emit placeholders ("unknown", "not visible in
        # chart", "-") for a coordinate they could not read, and those must
        # NOT be promoted into the coordinates column as if they were a
        # reading. Those keep the pre-existing behaviour: dropped, warned.
        if isinstance(val, str) and any(ch.isdigit() for ch in val):
            return [val.strip()]
        return pts

    # Normalize continents
    for cont in _dict_rows(parsed.get("continents"), "continents", _paleomap_warnings):
        row = {
            "name": s(cont.get("name")),
            "type": s(cont.get("type")),
            "coordinates": norm_coords(cont.get("coordinates"), "continents"),
            "paleolatitude": s(cont.get("paleolatitude")),
            "note": s(cont.get("note")),
        }
        # H8 (Sprint B REVIEW-2026-09-04): preserve unknown row keys.
        _carry_extras(cont, _KNOWN_PALEOMAP_CONTINENT_KEYS, row)
        out["continents"].append(row)

    # Normalize oceans/seas
    for sea in _dict_rows(parsed.get("oceans_seas"), "oceans_seas", _paleomap_warnings):
        row = {
            "name": s(sea.get("name")),
            "type": s(sea.get("type")),
            "coordinates": norm_coords(sea.get("coordinates"), "oceans_seas"),
            "note": s(sea.get("note")),
        }
        _carry_extras(sea, _KNOWN_PALEOMAP_SEA_KEYS, row)
        out["oceans_seas"].append(row)

    # Normalize tectonic features
    for feat in _dict_rows(parsed.get("tectonic_features"), "tectonic_features", _paleomap_warnings):
        row = {
            "name": s(feat.get("name")),
            "type": s(feat.get("type")),
            "coordinates": norm_coords(feat.get("coordinates"), "tectonic_features"),
            "direction": s(feat.get("direction")),
            "description": s(feat.get("description")),
        }
        _carry_extras(feat, _KNOWN_PALEOMAP_TECTONIC_KEYS, row)
        out["tectonic_features"].append(row)

    # Normalize biogeographic realms
    for realm in _dict_rows(parsed.get("biogeographic_realms"), "biogeographic_realms", _paleomap_warnings):
        row = {
            "name": s(realm.get("name")),
            "type": s(realm.get("type")),
            "coordinates": norm_coords(realm.get("coordinates"), "biogeographic_realms"),
            "characteristic_fauna": s(realm.get("characteristic_fauna")),
        }
        _carry_extras(realm, _KNOWN_PALEOMAP_REALM_KEYS, row)
        out["biogeographic_realms"].append(row)

    # Normalize fossil sites
    for site in _dict_rows(parsed.get("fossil_sites"), "fossil_sites", _paleomap_warnings):
        row = {
            "name": s(site.get("name")),
            # AUDIT-2026-09-27 [item 7.3]: this was the ONE paleomap table
            # that did not route its coordinate through norm_coords — every
            # sibling (continents / oceans_seas / tectonic_features /
            # biogeographic_realms / paleolatitude_indicators) does. `s(...)`
            # stringifies, and a dict/list lat_lon stringifies to "", so a
            # model returning structured coordinates for a site lost them
            # outright. And because "lat_lon" is in _KNOWN_PALEOMAP_SITE_KEYS
            # the value was considered "known", so _carry_extras did not
            # rescue it either. Observed in real runs as lat_lon: "" next to
            # a fossil_sites row that plainly had coordinates.
            # The GUI already renders this column with `col.coordinates`, so
            # normalising it is also what the table was displaying all along.
            "lat_lon": norm_coords(site.get("lat_lon"), "fossil_sites"),
            "age": s(site.get("age")),
            "fossils": s(site.get("fossils")),
            "marker_type": s(site.get("marker_type")),
        }
        _carry_extras(site, _KNOWN_PALEOMAP_SITE_KEYS, row)
        out["fossil_sites"].append(row)

    # Normalize paleolatitude indicators
    for ind in _dict_rows(parsed.get("paleolatitude_indicators"), "paleolatitude_indicators", _paleomap_warnings):
        row = {
            "type": s(ind.get("type")),
            "coordinates": norm_coords(ind.get("coordinates"), "paleolatitude_indicators"),
        }
        _carry_extras(ind, _KNOWN_PALEOMAP_INDICATOR_KEYS, row)
        out["paleolatitude_indicators"].append(row)

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)

    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_PALEOMAP_ROOT_KEYS})
    if extras_src:
        out["_extras"] = extras_src
    if _paleomap_warnings:
        out["_warnings"] = _paleomap_warnings
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="paleomap", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


# ============================================================================
# NEW CHART TYPES: Scatter Plot / Biplot
# ============================================================================

_KNOWN_SCATTER_PLOT_ROOT_KEYS = (
    "metadata", "groups", "points", "outliers", "statistics", "confidence",
    # BORROW-2026-09-20 (B)
    "axis_calibration",
)

# Sprint B (REVIEW-2026-09-04): per-row known keys for the H8 ``_carry_extras``
# contract on scatter sub-rows (groups / points / outliers).
_KNOWN_SCATTER_GROUP_KEYS = ("name", "color", "marker", "n_points_visible", "description")
_KNOWN_SCATTER_POINT_KEYS = ("x", "y", "z", "group", "label", "note")
_KNOWN_SCATTER_OUTLIER_KEYS = ("x", "y", "group", "reason")

# AUDIT-2026-09-27 P0-1: scatter row probes, most specific first. A point is
# the only table carrying a measured coordinate PAIR; an outlier is the only
# one carrying ``reason``; a bare ``{"name": ...}`` is a series definition.
_SCATTER_ARRAY_SPEC_KEYS = (
    ("outliers", ("reason",)),
    ("points", ("x", "y")),
    ("groups", ("name",)),
)


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
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    out = {
        "metadata": {},
        "groups": [],
        "points": [],
        "outliers": [],
        "statistics": {},
        "confidence": 0.0,
    }

    # Normalize metadata
    # REVIEW-2026-09-10: repairs to surface on the result.
    scatter_warnings: list[str] = []
    # BORROW-2026-09-20 (B): a scatter plot is two continuous axes (x, y).
    _scatter_axes = axis_domains_from(parsed)

    # AUDIT-2026-09-27 P0-1: same missing ``_array_root`` unwrap the paleomap
    # normalizer had - a bare-array reply emptied groups/points/outliers and
    # was still reported as a successful extraction.
    _rehydrate_array_root(parsed, _SCATTER_ARRAY_SPEC_KEYS, scatter_warnings)

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
    for grp in _dict_rows(parsed.get("groups"), "groups", scatter_warnings):
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
    # REVIEW-2026-09-10: the cap used to be applied in silence, so the
    # result contradicted its own metadata.n_points (which keeps the model's
    # full count) and the JSON/table/CSV silently reported 500 of N
    # observations. The cap now surfaces as a warning.
    # AUDIT-2026-09-27 P1: `points` / `outliers` take no `kind` - a bare string
    # is not a measurement, so it stays dropped (see _row_from_string).
    _points_all = list(_dict_rows(parsed.get("points")))
    if len(_points_all) > 500:
        scatter_warnings.append("points_truncated_to_500")
    for pt in _points_all[:500]:
        row = {
            "x": s(pt.get("x")),
            "y": s(pt.get("y")),
            "z": s(pt.get("z")),
            "group": s(pt.get("group")),
            "label": s(pt.get("label")),
            "note": s(pt.get("note")),
        }
        _carry_extras(pt,
                      _KNOWN_SCATTER_POINT_KEYS + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(pt, row, axes=_scatter_axes), row)
        out["points"].append(row)

    # Normalize outliers
    for ot in _dict_rows(parsed.get("outliers")):
        row = {
            "x": s(ot.get("x")),
            "y": s(ot.get("y")),
            "group": s(ot.get("group")),
            "reason": s(ot.get("reason")),
        }
        _carry_extras(ot,
                      _KNOWN_SCATTER_OUTLIER_KEYS + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(ot, row, axes=_scatter_axes), row)
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
    out["confidence"] = _confidence_clamped(conf)
    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_SCATTER_PLOT_ROOT_KEYS})
    # BORROW-2026-09-20 (B): hoist the calibration; absent -> no new key.
    # FIX-2026-09-22 (audit item 7): unusable -> warn + keep the raw block.
    _fold_axis_calibration(parsed, _scatter_axes, out, extras_src, scatter_warnings)
    if extras_src:
        out["_extras"] = extras_src
    if scatter_warnings:
        out["_warnings"] = scatter_warnings
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="scatter_plot", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


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
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    warnings: list[str] = []
    out: dict[str, Any] = {
        "zonations": [],
        "zones": [],
        "correlations": [],
        "confidence": 0.0,
    }
    if not isinstance(parsed, dict):
        return out
    # REVIEW-2026-09-10: _iter_rows also recovers dict-shaped fields
    # ({"zones": {"Z1": {...}}}) that previously collapsed to [].
    for z in _iter_rows(parsed.get("zonations"), "zonations", warnings):
        row = {
            "name": s(z.get("name")),
            "region": s(z.get("region")),
            "framework": s(z.get("framework")),
            "reference": s(z.get("reference")),
        }
        _carry_extras(z, _KNOWN_ZONATIONS_KEYS, row)
        out["zonations"].append(row)
    for z in _iter_rows(parsed.get("zones"), "zones", warnings):
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
    for c in _iter_rows(parsed.get("correlations"), "correlations", warnings):
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
        out["confidence"] = _confidence_clamped(
            float(parsed.get("confidence", 0.0) or 0.0))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_ZONATION_ROOT_KEYS})
    if extras_src:
        out["_extras"] = extras_src
    if warnings:
        out["_warnings"] = warnings
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    warning = _truncation_warning(usage, max_tokens)
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
    # REVIEW-2026-09-20 #7: route the success return through the shared
    # contract so an unusable / unrelated payload flips to ok=False.
    return _ok_result(
        p=p, mode_key="zonation_chart", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


_MODE_DISPATCH["zonation_chart"] = extract_zonation_chart


# ---------------------------------------------------------------------------
# OCCURRENCE_MATRIX + GENERIC_TABLE (2026-10-15).
#
# Both are thin wrappers over the same call contract as the other seven modes;
# what distinguishes them is the normalizer each one hands the parsed JSON to,
# and the two normalizers deliberately differ in polarity. See
# normalize_occurrence_matrix_result (sparse: a blank cell means absent, so the
# row is dropped) against normalize_generic_table_result (positional: a blank
# cell is only knowable by keeping its slot).
#
# generic_table is registered in _MODE_DISPATCH so a caller can ASK for it
# explicitly. It is deliberately absent from KNOWN_CHART_TYPES, so the vision
# classifier can never name it and it can never quietly become a default.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# occurrence_matrix: how the cell table got its provenance, and how short an
# answer may be before it stops being an answer at all.
#
# 2026-10-16, measured on a real 22-row x 18-column occurrence matrix scored
# against a hand-checked CSV of all 93 filled cells: the model read the LABELS
# well (19/22 taxa, 19/19 authorities, 11/11 conodont zones, legend all correct)
# and the dense CELLS badly (0.43 accuracy at 0.26 coverage), and the failure
# was omission, not misreading (33 cells never emitted, only 4 misread).
#
# That asymmetry is why `cell_status` exists. A single-pass answer cannot tell
# its reader which half of it is trustworthy, because the result object looks
# identical whether the cells were located geometrically or guessed. Carrying
# the provenance as data lets the exporter refuse to present guessed cells as
# measured ones.
# ---------------------------------------------------------------------------
OCCURRENCE_CELL_STATUSES = (
    # Cells were read by the model in one pass, with no geometric check on
    # whether each one landed in the column it names. The historical default,
    # and the only status that can be wrong in an unverifiable way.
    "single_pass_unverified",
    # Cells were read against a locally detected grid, so every row's column
    # identity was checked against a pixel rectangle.
    "grid_cells",
    # The model was GIVEN the measured lattice so it could place the cells
    # better, but nothing checked where it put them: it is one call, like
    # single_pass, with the spans in the prompt instead of a pixel ink test.
    # It gets its own status because the difference from single_pass is real
    # and the difference from grid_cells is also real -- claiming grid_cells
    # here would let the normalizer keep the model's blank-cell padding as
    # occurrences, on a path where no ink test ever ran.
    "grid_guided",
    # The labels-only path was used and no cell reading was attempted at all.
    "not_attempted",
    # A cell reading was attempted and did not produce usable output.
    "failed",
)
DEFAULT_OCCURRENCE_CELL_STATUS = "single_pass_unverified"

# The shortest plausible output for this mode, measured 2026-10-16 across four
# real runs of a 22x18 matrix: 4190 / 4400 / 5587 / 5428 output tokens, plus a
# deliberately compacted row-packed variant at 1755. Two runs came back at 528
# and 40 tokens - near-empty bodies that parsed as valid JSON and would
# otherwise have been handed to the user as "this figure contains no
# occurrences".
#
# The floor is set well under the smallest GOOD run (1755), not just under the
# bad ones, so an unusually terse-but-correct answer still passes. Output token
# count is the earliest available signal that the model stopped working; a
# self-reported `confidence` does not catch this, because a model that emitted
# nothing is perfectly confident there is nothing there.
OCCURRENCE_MIN_OUTPUT_TOKENS = 1600

# The floor is per strategy, and the strategies are not the same size.
#
# 1600 was measured on the FULL answer (columns + legend + a row per filled
# cell), where four real runs came back at 4190 / 4400 / 5587 / 5428 and a
# deliberately compacted one at 1755. A labels-only answer asks for none of
# that - it returns columns, rows and a legend and nothing else - so it is
# legitimately a fraction of the size and reusing 1600 against it would reject
# correct output.
#
# `gated` and `cells` are None until they have been MEASURED on a real figure.
# A threshold guessed from the contract's arithmetic would be a guess, and a
# guess here rejects good answers silently - which is the exact failure shape
# the whole 2026-10-16 change set exists to remove.
OCCURRENCE_MIN_OUTPUT_TOKENS_BY_STRATEGY = {
    "single_pass": OCCURRENCE_MIN_OUTPUT_TOKENS,
    "gated": None,
    "cells": None,
    "grid_read": None,
}

# How `extract_occurrence_matrix` decides where the cells come from.
#
#   single_pass - one call, the model emits labels and cells together and is
#                 asked to locate each cell itself. Historical behaviour.
#   gated       - labels are read in their own call and no cell reading is
#                 attempted. Yields the half that measured well and admits it.
#   cells       - labels plus a cell pass grounded in locally detected grid
#                 geometry, so every row's column identity was checked against
#                 a pixel rectangle.
#                 CORRECTED 2026-10-19: the check was never made. This path
#                 puts the measured spans in the PROMPT and lets the model
#                 place the cells; nothing verifies where it put them. It is
#                 one call, like single_pass, and it gets its own status
#                 ("grid_guided") rather than claiming "grid_cells", because
#                 that claim is what the normalizer's verified-geometry
#                 discriminator keys off -- and on this path no ink test ever
#                 ran, so the model's blank-cell padding would have been kept
#                 as occurrences.
#   grid_read   - the two-stage path in `rca_core.matrix`: geometry locates
#                 every filled cell, then the model reads each one from its own
#                 magnified crop and reads the axis labels from the figure.
#
# grid_read is the DEFAULT, and the reason is measurement rather than taste.
# Scored against the hand-checked 93-cell CSV on the reference figure:
#
#     strategy      cell precision / recall          n
#     single_pass   0.579 / 0.355                    1
#     cells         0.574 / 0.565                    1
#     grid_read     1.000 / 1.000                    5   (minimum 1.000)
#
# n=5 is above the bar set for changing a default, and the minimum over those
# five runs is 1.000, so this is not an average hiding a bad tail. `grid_read`
# falls back to `single_pass` whenever geometry is unavailable, when a reply is
# truncated, or when the model's axis counts disagree with the measured lattice,
# so making it the default cannot turn any input into "no answer".
OCCURRENCE_CELL_STRATEGIES = ("grid_read", "single_pass", "gated", "cells")
OCCURRENCE_DEFAULT_CELL_STRATEGY = "grid_read"

# Maps a strategy onto the cell_status its output may honestly claim.
# `single_pass` never earns `grid_cells` no matter how good the answer looks.
#
# `grid_read` has no entry that a fallback could borrow: when the two-stage
# attempt fails the run degrades to the `single_pass` code path AND rewrites the
# status, so the status on the wire is always the one the path that actually ran
# earned. Mapping `grid_read -> grid_cells` up here would be a lie in exactly
# the case it matters, because the two-stage path returns None precisely when
# it could not verify anything.
OCCURRENCE_STATUS_BY_STRATEGY = {
    "single_pass": "single_pass_unverified",
    "gated": "not_attempted",
    "cells": "grid_guided",
}

# Strategies that DECIDE their own status from which code path actually ran,
# instead of having one stamped from the table above.
#
# `grid_read` is the only member. It runs the two-stage path and, whenever that
# cannot be completed (no lattice, a truncated reply, axis counts that disagree
# with the measured grid, or a reader that answered none of the tiles), it
# degrades to the `single_pass` path. A fixed table entry would freeze
# `grid_cells` onto that degraded run, which is precisely the claim the status
# exists to prevent.
#
# Together, OCCURRENCE_STATUS_BY_STRATEGY and this set partition
# OCCURRENCE_CELL_STRATEGIES; a strategy in neither would have its status
# decided by accident, and a test asserts that.
OCCURRENCE_SELF_STATUS_STRATEGIES = ("grid_read",)


def _grid_geometry_clause(geom: Any) -> str:
    """Describe the locally detected grid, in the image's own pixel units.

    The model's cell-placement failure is an identity failure, not a reading
    failure: measured on a 22 x 18 matrix, it emitted 19 column headers, wrote
    "87 KT-A19" and "87 KT-A19A" twice, and misread "CAA-A27" - and a shift
    scan of -3..+3 scored ZERO at every offset, which rules out misalignment
    and leaves the wrong column IDENTITY. Naming each column and each row with
    the rectangle it occupies turns "which column is this in" from a guess
    into a lookup.

    Deliberately ONE call. Cropping each cell and asking about it separately
    would be up to n_cols * n_rows = 396 requests for this figure, which is a
    different product with a different price, not a tuning knob.
    """
    cols = ", ".join("[%d]=x%d-%d" % (i, a, b)
                     for i, (a, b) in enumerate(geom.col_spans))
    rows = ", ".join("[%d]=y%d-%d" % (i, a, b)
                     for i, (a, b) in enumerate(geom.row_spans))
    label = ""
    if geom.label_span:
        label = ("\nThe row labels are printed in the left band x%d-%d; that "
                 "band is NOT a data column." % geom.label_span)
    return (
        "\n\nGRID GEOMETRY, measured locally from this exact image (pixel "
        "coordinates, origin top-left; confidence %.2f). Use these to place "
        "each cell, and report `col_index` as the bracketed COLUMN number "
        "below rather than counting columns yourself.%s"
        "\nColumns: %s"
        "\nTaxon rows: %s"
        % (geom.confidence, label, cols, rows)
    )


def extract_occurrence_matrix(
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
    cell_strategy: str = OCCURRENCE_DEFAULT_CELL_STRATEGY,
) -> ExtractResult:
    """Taxon x locality occurrence-matrix extraction.

    Same contract as extract_range_chart. Unlike every other mode, the taxa are
    the rows and the localities are the COLUMNS, so there is no continuous axis
    to calibrate and no ``pos_0_999`` to read.

    ``cell_strategy`` selects how much of the figure the model is asked for.
    See ``OCCURRENCE_CELL_STRATEGIES``. Whatever it is, the returned data
    carries a ``cell_status`` saying which parts were verified.
    """
    from .prompt import (OCCURRENCE_LABELS_SYSTEM_PROMPT,
                         OCCURRENCE_MATRIX_SYSTEM_PROMPT)

    if not image_b64:
        return ExtractResult(ok=False, error_key="err.imageRead")
    if cell_strategy not in OCCURRENCE_CELL_STRATEGIES:
        cell_strategy = OCCURRENCE_DEFAULT_CELL_STRATEGY
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

    # ---- grid_read: geometry locates the cells, the model reads them -----
    #
    # Attempted FIRST and, on failure, the run continues as `single_pass` with
    # the status rewritten. The rewrite is the point: the two-stage path returns
    # None precisely when it could not verify anything (no lattice, a truncated
    # reply, or axis counts that disagree with the measured grid), so letting the
    # requested strategy name survive into the result would advertise
    # `grid_cells` for an answer that never had them verified.
    if cell_strategy == "grid_read":
        t0 = time.perf_counter()
        # Every call this path makes is billed, and the server sums `usage`
        # across a run to report cost. The two-stage path makes ONE label call
        # plus one call per contact sheet, so the usage has to be COLLECTED as
        # it happens -- the first version of this block hard-coded `usage={}`
        # and `raw_text=""`, which under-reported the new default by 2-3x and
        # left the audit trail with no model reply at all for the runs that
        # produced two or three.
        _grid_usage: list[dict[str, Any]] = []
        _grid_raws: list[dict[str, Any]] = []
        try:
            from .matrix import (MATRIX_CALL_TIMEOUT_SEC,
                                extract_matrix_two_stage, guess_media_type)

            def _grid_read_call(system_prompt, img_b64, user_text, mt):
                # FIX-2026-10-17 (P2-3): declare the media type the BYTES
                # actually are. This closure serves two kinds of image — the
                # magnified contact sheets (always PNG, re-encoded from a
                # greyscale array) and the label image, which
                # ``matrix.render_label_image`` returns UNMODIFIED on three of
                # its four paths (over LABEL_SCALE_MAX_EDGE, a failed resize,
                # the PIL-missing fallback). With DEFAULT_MAX_EDGE=4000 an
                # ordinary 4000x3000 JPEG scan hits exactly that branch, and
                # the hardcoded "image/png" then described JPEG bytes to the
                # provider: Anthropic answers 400, the two-stage path returns
                # None, and the run degrades to single_pass with one line of
                # log saying "grid_read failed" — indistinguishable from a
                # geometry miss. Magic-number detection costs a few bytes and
                # cannot be wrong about a file the module itself produced.
                #
                # REVIEW-2026-10-19: the per-call timeout is CAPPED. The
                # server's single-run budget is `timeout_sec * 6 + 25` s, and
                # this path spends one call on the labels plus one per contact
                # sheet, so several calls at the full timeout would eat the
                # whole budget and the request would be abandoned with no data
                # at all. Measured, the calls themselves take 5-45 s -- 90 s is
                # a generous ceiling, and a hung call can no longer spend more
                # than its share of the budget.
                raw, trunc, _status, err_body, usage = _call_llm(
                    provider=p,
                    system_prompt=system_prompt,
                    image_b64=img_b64,
                    media_type=guess_media_type(img_b64),
                    user_text=user_text,
                    max_tokens=mt,
                    timeout_sec=min(timeout_sec, MATRIX_CALL_TIMEOUT_SEC),
                    capture_error_body=True,
                    progress_callback=progress_callback,
                )
                if usage:
                    _grid_usage.append(usage)
                if raw:
                    _grid_raws.append({
                        "call": ("labels" if "axis labels" in user_text
                                 else "cells"),
                        "text": raw,
                    })
                return raw, trunc, err_body

            two_stage = extract_matrix_two_stage(
                image_b64, _grid_read_call, max_tokens=max_tokens)
        except Exception as exc:                # never raises out of this path
            log.warning("grid_read failed, falling back: %s: %s",
                        type(exc).__name__, exc)
            two_stage = None
        if two_stage is not None:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            try:
                # `grid_cells`, NOT the strategy table's entry for "cells".
                # This path DID run a pixel ink test over every emitted cell,
                # which is what that status means; the "cells" STRATEGY only
                # puts the measured spans in the prompt and never checks
                # anything, so it carries "grid_guided" instead.
                from .matrix import CELL_STATUS_GRID
                data = normalize_occurrence_matrix_result(
                    two_stage, cell_status=CELL_STATUS_GRID)
            except Exception as exc:
                log.warning("grid_read normalize failed, falling back: %s", exc)
                two_stage = None
            else:
                return _ok_result(
                    p=p, mode_key="occurrence_matrix", data=data,
                    raw_text=_grid_read_raw(_grid_raws),
                    truncated=False,
                    usage=_merge_usage(_grid_usage),
                    latency_ms=latency_ms, image_sha256=image_sha256,
                    max_tokens=max_tokens, parsed=two_stage)
        cell_strategy = "single_pass"

    cell_status = OCCURRENCE_STATUS_BY_STRATEGY[cell_strategy]
    # The gated path asks a DIFFERENT question, not a shorter version of the
    # same one. The 2026-10-15 attempt to fix this by adding rules to the
    # matrix prompt was measured and reverted; asking for less is the opposite
    # direction and is why OCCURRENCE_LABELS_SYSTEM_PROMPT exists.
    # `gated` and `cells` are NOT the same question. `gated` asks for the
    # labels and forbids reading cells; `cells` asks the full matrix question
    # and adds locally measured pixel spans. Grouping them here made `cells`
    # use the labels prompt AND skip the cells, i.e. a silently broken
    # strategy that returned exactly what `gated` returns under a status
    # claiming geometric verification.
    gated = cell_strategy == "gated"
    system_prompt = (OCCURRENCE_LABELS_SYSTEM_PROMPT if gated
                     else OCCURRENCE_MATRIX_SYSTEM_PROMPT)
    geometry_clause = ""
    if cell_strategy == "cells":
        # Detection can legitimately fail on a figure with no ruling lines at
        # all, and a borderless matrix is not exotic. In that case this run
        # degrades to the single-pass question and says so in the status,
        # because "no geometry found" is exactly the condition under which
        # claiming the cells were geometrically verified would be a lie.
        try:
            from .grid import detect_grid
            geom = detect_grid(image_b64)
        except Exception:
            geom = None
        if geom is None:
            system_prompt = OCCURRENCE_MATRIX_SYSTEM_PROMPT
            cell_status = OCCURRENCE_STATUS_BY_STRATEGY["single_pass"]
        else:
            geometry_clause = _grid_geometry_clause(geom)
    # max_tokens / timeout_sec / image_sha256 / provider `p` were already
    # clamped and built above, before the grid_read attempt, so a fallback runs
    # on exactly the values the caller asked for.
    lang_hint = CHART_LANG_HINT.get(chart_lang, "")
    user_prompt = (
        "Caption:\n"
        + (caption.strip() if caption and caption.strip() else "(no caption)")
        + "\n\n"
        + lang_hint
        + ("Extract the column headers, the taxon row labels and the class "
           "legend as the strict JSON contract. Do NOT read the grid cells."
           if gated else
           "Extract the taxon x locality occurrence matrix as the strict "
           "JSON contract: one row per FILLED cell.")
        + geometry_clause
    )
    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = _call_llm(
            provider=p,
            system_prompt=system_prompt,
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
    warning = _truncation_warning(usage, max_tokens)
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms,
                                  image_sha256=image_sha256)
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
        data = normalize_occurrence_matrix_result(parsed,
                                                  cell_status=cell_status)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    # --- EMPTY-RESPONSE GATE (2026-10-16) ---------------------------------
    #
    # A near-empty body is valid JSON and parses cleanly, so it sailed through
    # both the truncation check and `safe_json_loads` and reached the user as
    # "this figure contains no occurrences". Two real runs measured 528 and 40
    # output tokens where the same figure normally costs 4190-5587; both were
    # the model declining to work, not the figure being empty.
    #
    # The pair of conditions is what makes this safe. Token count alone would
    # reject a genuinely sparse figure, and zero occurrences alone is a
    # legitimate answer this mode is explicitly built to allow. It takes a
    # body too short to have read a 22-row table AND no occurrences to show
    # for it. A provider that reports no usage at all skips the gate, because
    # there is nothing to judge on and inventing a threshold for it would only
    # hide the difference between "empty" and "not reported".
    out_tokens = _usage_int(usage, "output_tokens")
    min_tokens = OCCURRENCE_MIN_OUTPUT_TOKENS_BY_STRATEGY.get(cell_strategy)
    if (min_tokens is not None
            and out_tokens is not None
            and out_tokens < min_tokens
            and not data.get("occurrences")):
        return ExtractResult(
            ok=False, error_key="err.emptyResponse",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms,
            warning=(
                "The model returned %d output tokens and no occurrence rows. "
                "That is below the %d this mode's own figures need, so this is "
                "an empty response rather than an empty figure; re-run before "
                "reading anything into the result."
                % (out_tokens, min_tokens)
            ),
            image_sha256=image_sha256,
        )
    return _ok_result(
        p=p, mode_key="occurrence_matrix", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


_MODE_DISPATCH["occurrence_matrix"] = extract_occurrence_matrix


def extract_generic_table(
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
    """Structural extraction for a figure whose genre is NOT known.

    The refusal path, not a best-guess path: the prompt tells the model to
    report the grid it can see and to decline plainly when it cannot, and this
    function passes that answer through instead of re-interpreting it.
    """
    from .prompt import GENERIC_TABLE_SYSTEM_PROMPT

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
        + "Describe the STRUCTURE of this figure. Do not interpret it as any "
        "particular chart genre, and decline plainly if you cannot read it."
    )
    t0 = time.perf_counter()
    try:
        raw_text, truncated, status, err_body, usage = _call_llm(
            provider=p,
            system_prompt=GENERIC_TABLE_SYSTEM_PROMPT,
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
    warning = _truncation_warning(usage, max_tokens)
    if raw_text is None:
        return _error_from_status(status, err_body, latency_ms,
                                  image_sha256=image_sha256)
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
        data = normalize_generic_table_result(parsed)
    except Exception as exc:
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=raw_text, truncated=truncated, usage=usage or {},
            latency_ms=latency_ms, warning=f"normalize failed: {exc}",
            image_sha256=image_sha256,
        )
    return _ok_result(
        p=p, mode_key="generic_table", data=data, raw_text=raw_text,
        truncated=truncated, usage=usage, latency_ms=latency_ms,
        image_sha256=image_sha256, max_tokens=max_tokens, parsed=parsed)


_MODE_DISPATCH["generic_table"] = extract_generic_table



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

# DERIVED from rca_core/modes.py (2026-10-15). `generic_table` is deliberately
# EXCLUDED: it is the fallback for an unrecognised genre, never something the
# vision classifier may name, so admitting it here would let the classifier
# return a mode that has no extraction path.
KNOWN_CHART_TYPES = frozenset(
    name for name in _MODE_SPECS if name != "generic_table")

_CLASSIFY_MIN_CONFIDENCE = 0.5


def normalize_occurrence_matrix_result(
        parsed: dict[str, Any],
        cell_status: str = DEFAULT_OCCURRENCE_CELL_STATUS) -> dict[str, Any]:
    """Coerce the parsed taxon x LOCALITY matrix into the strict result shape.

    Shape: ``columns`` (the localities, left to right), ``legend``, a SPARSE
    ``occurrences`` long table - one row per FILLED cell - and ``cell_status``,
    which records HOW the cells were obtained (see
    ``OCCURRENCE_CELL_STATUSES``).

    ``cell_status`` is an argument, not something read off ``parsed``. The model
    has no way to know whether its own cell coordinates were checked against
    the figure's geometry, so letting it supply the field would mean the field
    says whatever the prompt hoped for. The caller passes the status its own
    code path earned.

    SPARSITY IS THE CONTRACT. The step-0 baseline run (a real occurrence matrix
    through the real model, asked to answer as an abundance_diagram) came back
    with 63 rows where roughly 60 were real: the model emitted a row for every
    blank cell too, with an empty ``abundance``. Those rows are not neutral -
    each one claims "this taxon occurs at this locality", which is exactly the
    assertion a systematic section gets cited for, and it inflates the record
    count until the real occurrences are indistinguishable from padding. A
    blank cell here means ABSENT, and absence is already recoverable: it is
    every cell that was NOT emitted. So blank rows are DROPPED here rather than
    passed downstream.

    The second thing this fixes, from the same baseline run: the rotated
    column headers came back as taxa and the row labels as localities, which
    produces a complete, well-formed, entirely wrong table. The header list is
    therefore authoritative - a row whose ``site`` matches no column label is
    kept but marked ``uncertain`` with ``unknown_column``, instead of being
    silently accepted into a mis-transposed table.

    ``row_kind`` keeps a stratigraphic comparison band ("Conodont level (see
    text)") out of the taxon rows: it is real data about the figure, but it is
    not a genus, and letting it into ``occurrences`` would pollute every
    taxon-level statistic computed from this table.
    """
    if not isinstance(parsed, dict):
        return {"columns": [], "legend": [], "occurrences": [],
                "strat_rows": [], "taxon_rows": [], "confidence": 0.0,
                "cell_status": cell_status}

    # H3-fix parity: a top-level array arrives wrapped as {"_array_root": [...]}.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                continue
            if "label" in item and "index" in item and "cells" not in item:
                parsed.setdefault("columns", []).append(item)
            elif "taxon" in item or "site" in item:
                parsed.setdefault("occurrences", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        return _stringify_scalar(v)

    warnings: list[str] = []
    out: dict[str, Any] = {
        "columns": [],
        "legend": [],
        "occurrences": [],
        "strat_rows": [],
        "taxon_rows": [],
        "confidence": 0.0,
        "cell_status": (cell_status if cell_status in OCCURRENCE_CELL_STATUSES
                        else DEFAULT_OCCURRENCE_CELL_STATUS),
    }

    # --- columns: the authoritative locality list -------------------------
    labels: list[str] = []
    for i, col in enumerate(parsed.get("columns") or []):
        if not isinstance(col, dict):
            continue
        label = s(col.get("label") or col.get("name") or "").strip()
        if not label:
            # A column with no header cannot be cited, and a row pointing at it
            # would have no verifiable identity. Keep the position, drop the
            # name, and say so.
            warnings.append(f"column {i} has no readable header; dropped")
            continue
        raw_index = col.get("index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = i
        labels.append(label)
        out["columns"].append({
            "index": index,
            "label": label,
            "kind": s(col.get("kind") or "locality").strip() or "locality",
        })

    label_set = set(labels)
    # Case/space-insensitive fallback: the same locality can be transcribed with
    # different spacing by different rows of one answer.
    norm_labels = {re.sub(r"\s+", " ", lb).casefold(): lb for lb in labels}

    # --- legend: code -> class, read from the figure, never assumed --------
    legend_by_code: dict[str, str] = {}
    for entry in parsed.get("legend") or []:
        if not isinstance(entry, dict):
            continue
        code = s(entry.get("code") or "").strip()
        klass = s(entry.get("class") or entry.get("meaning") or "").strip()
        if not code:
            continue
        out["legend"].append({"code": code, "class": klass})
        if klass:
            legend_by_code[code.casefold()] = klass

    # --- occurrences: the sparse long table ------------------------------
    def first_text(*vals: Any) -> str:
        """First value that is actually present, as text.

        NOT ``a or b or c``. A bare ``or`` chain treats the integer 0 as
        absent, so a row carrying the PRINTED value 0 — which is a reading,
        not a blank — short-circuits to the next field and then reads as an
        empty cell: the row is dropped and a false "dropped N row(s) for
        blank cells" warning is emitted. Only None and "" count as absent
        here; False and 0 are real readings and must survive.
        """
        for v in vals:
            if v is None:
                continue
            t = _stringify_scalar(v).strip()
            if t:
                return t
        return ""

    dropped_blank = 0
    unread_cells = 0
    unknown_column_rows = 0
    # FIX-2026-10-17 (P1-3): on the GEOMETRIC path an empty code does not
    # mean "blank cell" — it means "the ink test proved a mark is here and
    # the reader did not say what it was". Those are opposite claims and only
    # one of them may become a silently absent cell.
    #
    # rca_core/matrix.py:771 states the contract the two-stage path was built
    # around: "A tile the model did not answer is recorded with an empty code
    # rather than dropped: the CELL EXISTS, and dropping it would turn a read
    # failure into a silently wrong absence." This loop then dropped exactly
    # those rows, so the guarantee never reached the result: 3 inked cells with
    # 1 unanswered produced 2 occurrences, and the only trace was a warning
    # that described them as blank padding — which is the opposite of what they
    # were. `cell_status` is the discriminator, and it is exactly the right
    # one: it is an ARGUMENT this function receives (never read off the model
    # payload), and it is only `grid_cells` when every emitted cell came from a
    # pixel ink test.
    verified_geometry = (
        out["cell_status"] == "grid_cells")
    for raw_row in parsed.get("occurrences") or []:
        if not isinstance(raw_row, dict):
            continue
        code = first_text(raw_row.get("code"), raw_row.get("symbol"),
                          raw_row.get("value"))
        klass = first_text(raw_row.get("class"),
                           raw_row.get("abundance_class"))
        # Narrow recovery of the baseline shape: asked as an abundance diagram
        # the model puts the class letter in `abundance`. Accept it here so a
        # partially-migrated answer is still usable, but ONLY when there is no
        # dedicated `code`.
        if not code:
            code = first_text(raw_row.get("abundance"))

        # THE SPARSITY CONTRACT. No code and no class == a blank cell.
        #
        # ...except on the geometric path, where it means "inked but unread".
        # See `verified_geometry` above: there, dropping is what converts a
        # failed READING into a confident ABSENCE, and the count is reported
        # separately so the two are never conflated in the output either.
        if not code and not klass:
            if verified_geometry:
                unread_cells += 1
            else:
                dropped_blank += 1
                continue

        taxon = first_text(raw_row.get("taxon"))
        if not taxon:
            warnings.append("occurrence row with no taxon label; dropped")
            continue

        row_kind = first_text(raw_row.get("row_kind")) or "taxon"

        site = s(raw_row.get("site") or raw_row.get("locality")
                 or raw_row.get("column") or "").strip()
        if not klass and code:
            klass = legend_by_code.get(code.casefold(), "")

        row: dict[str, Any] = {
            "taxon": taxon,
            "authority": s(raw_row.get("authority") or "").strip(),
            "row_kind": row_kind,
            "site": site,
            "col_index": raw_row.get("col_index"),
            "code": code,
            "class": klass,
        }
        if verified_geometry and not code and not klass:
            # The cell EXISTS (a pixel ink test found a mark in it) and the
            # reader did not name it. That is a reading failure attached to a
            # known occurrence, and it has to be distinguishable downstream
            # from both a blank cell (never emitted) and a real code.
            row["cell_read"] = "unread"

        # --- column identity must be verifiable --------------------------
        col_index = row["col_index"]
        try:
            col_index = int(col_index)
        except (TypeError, ValueError):
            col_index = None

        resolved = None
        if site:
            resolved = site if site in label_set else norm_labels.get(
                re.sub(r"\s+", " ", site).casefold())
            if resolved:
                # Use the column's own spelling, so downstream grouping by
                # `site` cannot split one locality across two spellings.
                site = resolved

        known_index = {c["index"] for c in out["columns"]}
        if site and resolved is None:
            # A site that names no column is the transposition failure mode.
            # Keep the row (the taxon may still be readable) but flag it.
            unknown_column_rows += 1
            row["site"] = site
            row["col_index"] = col_index
            extra = attach_row_contract(raw_row, row)
            row["response_kind"] = "uncertain"
            row["reason_codes"] = sorted(
                set(list(row.get("reason_codes") or [])) | {"unknown_column"})
            _carry_extras(raw_row, _CONTRACT_SOURCE_KEYS + tuple(extra), row)
            warnings.append(
                f"occurrence row for {taxon!r} names site {site!r}, which is "
                f"not one of the {len(labels)} column headers")
        else:
            # `site` is the authoritative identity, so `col_index` is
            # reconciled to it - not merely range-checked. A row carrying
            # site="B" with col_index=0 (which IS a valid index, just the
            # wrong one) used to pass through untouched and into the export,
            # leaving a row whose two identity fields contradict each other
            # with no warning and no reason code. A contradictory pair means
            # one of the two was misread, which is precisely what
            # response_kind="uncertain" is for.
            site_index = next(
                (c["index"] for c in out["columns"] if c["label"] == site),
                None)
            if col_index is not None and site_index is not None \
                    and col_index != site_index:
                row["site"] = site
                row["col_index"] = col_index
                extra = attach_row_contract(raw_row, row)
                row["response_kind"] = "uncertain"
                row["reason_codes"] = sorted(
                    set(list(row.get("reason_codes") or []))
                    | {"column_index_conflict"})
                _carry_extras(raw_row, _CONTRACT_SOURCE_KEYS + tuple(extra),
                              row)
                warnings.append(
                    f"occurrence row for {taxon!r} claims col_index={col_index} "
                    f"but site {site!r} is column {site_index}; kept both and "
                    f"flagged uncertain")
            else:
                if col_index is None or col_index not in known_index:
                    col_index = site_index if site_index is not None \
                        else col_index
                row["site"] = site
                row["col_index"] = col_index
                extra = attach_row_contract(raw_row, row)
                _carry_extras(raw_row, _CONTRACT_SOURCE_KEYS + tuple(extra),
                              row)

        out["occurrences"].append(row)

    # --- taxon_rows: the labels-only path (cell_strategy "gated") ---------
    #
    # The gated strategy asks the model for headers, taxon labels and the
    # legend and explicitly NOT for grid cells, because those are the three
    # things it measurably reads well (19/22 taxa, 19/19 authorities, legend
    # all correct) while the cells are the thing it does not.
    #
    # These rows cannot go into `occurrences`. That list is SPARSE BY CONTRACT
    # - one row per FILLED cell - so a label with no code and no class is
    # indistinguishable from a blank cell and gets dropped by the rule above.
    # Folding them in would both empty the list and destroy exactly the
    # readings that were good.
    #
    # NO row_kind IS INFERRED HERE. The labels prompt transcribes a
    # stratigraphic comparison band ("Conodont level (see text)") into `taxon`
    # verbatim with an empty `authority`, as instructed, and there is no
    # reliable way to tell that row from a genus that simply has no named
    # authority. Guessing here would put a band into every taxon-level
    # statistic computed from this table. The band is carried faithfully and
    # its discriminator is documented rather than invented.
    for raw_row in parsed.get("rows") or []:
        if not isinstance(raw_row, dict):
            continue
        taxon = first_text(raw_row.get("taxon") or raw_row.get("label"))
        if not taxon:
            warnings.append("labels-only row with no taxon label; dropped")
            continue
        try:
            index = int(raw_row.get("index"))
        except (TypeError, ValueError):
            index = len(out["taxon_rows"])
        out["taxon_rows"].append({
            "index": index,
            "taxon": taxon,
            "authority": s(raw_row.get("authority") or "").strip(),
        })

    # --- strat_rows: the non-taxon comparison band -----------------------
    for band in parsed.get("strat_rows") or []:
        if not isinstance(band, dict):
            continue
        values = band.get("values")
        if not isinstance(values, list):
            values = []
        out["strat_rows"].append({
            "label": s(band.get("label") or "").strip(),
            "values": [s(v) for v in values],
        })

    if dropped_blank:
        # Loud, not silent: the count of rows this answer padding produced is
        # the single most useful thing to know when judging its quality.
        warnings.append(
            f"dropped {dropped_blank} row(s) for blank cells; this mode is "
            f"sparse by contract (one row per FILLED cell)")
    if unread_cells:
        # FIX-2026-10-17 (P1-3): a DIFFERENT failure from the one above, and
        # it is reported in different words on purpose. These cells were
        # proven filled by the ink test; the reader simply did not answer
        # them. They are kept, flagged `cell_read="unread"`, because the
        # alternative — omitting them — states "this taxon does not occur
        # here", which is the one claim this mode must never get wrong.
        warnings.append(
            f"{unread_cells} cell(s) carry a mark but no code was read for "
            f"them; kept as cell_read='unread' rather than reported as absent")

    # --- transposition, decided on the DISTRIBUTION not on a single row ----
    #
    # An occasional unknown_column row is one misread header, which the
    # per-row `uncertain` flag already reports honestly. But when MOST rows
    # name a site that is not a column, the whole table is rotated: the
    # baseline run of this mode did exactly that, returning the column headers
    # as taxa. That output is complete, well-formed and entirely wrong, which
    # is the failure shape this module's docstring calls worse than a refusal.
    #
    # The obvious alternative check - fewer taxa than columns - was rejected:
    # a 3-taxon x 18-locality matrix is an ordinary figure, so that ratio
    # says nothing about orientation.
    if (unknown_column_rows
            and len(out["occurrences"]) >= 3
            and len(out["columns"]) >= 3
            and unknown_column_rows * 2 > len(out["occurrences"])):
        warnings.append(
            f"{unknown_column_rows} of {len(out['occurrences'])} occurrence "
            f"rows name a site that is not one of the {len(out['columns'])} "
            f"column headers; the matrix may be transposed (rows and columns "
            f"swapped). Column labels and row labels stay usable; the "
            f"cell table should not be cited until this is resolved.")

    conf = parsed.get("confidence")
    try:
        out["confidence"] = _confidence_clamped(float(conf))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    _carry_extras(parsed, ("columns", "legend", "occurrences", "strat_rows",
                           "taxon_rows", "confidence", "cell_status"), out)
    if warnings:
        # FIX-2026-10-17 (P3-4): this wrote ``warnings``, while the other
        # eight normalizers write ``_warnings`` and every consumer reads that
        # one (``_payload_mismatch`` at :1350, ``_ok_result``, report.py,
        # aggregate.py, the JS mirror). Measured on the real normalizer, the
        # key simply did not exist downstream — so this mode's only two
        # diagnostics ("dropped N rows for blank cells", "matrix may be
        # transposed") reached no UI and were dropped again at merge time,
        # while the tests that assert on them read the local dict and passed.
        # Nothing else in the file writes a bare ``warnings`` now; the
        # compatibility shim below stays so a payload that already carries one
        # from an older caller still reaches the guard.
        out["_warnings"] = warnings
    return out


def normalize_generic_table_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Last-resort structural extraction for an unrecognised chart genre.

    This is the DEFAULT for a figure the heuristic and the vision classifier
    could not name. What it deliberately does NOT do is the thing every prior
    unknown-chart path did: fall through to ``normalize_result`` and hand back a
    range chart with invented ``pos_0_999`` values. A fabricated range chart
    survives a skim-read of the output - it has the right keys, plausible
    numbers, and an ``ok=True`` result - so it can be cited without anyone
    noticing it was invented. Refusing is the correct behaviour, and it is what
    the prompt asks the model to do when it cannot read the structure.

    Note the polarity is the OPPOSITE of occurrence_matrix: there a blank cell
    means "absent" and the row is dropped, because absence is recoverable from
    the rows that were NOT emitted. Here the row set is already unknown, so a
    blank cell is only knowable by keeping it - ``cells`` is positional and
    must carry an empty string in that slot rather than shortening the list.
    """
    if not isinstance(parsed, dict):
        return {"columns": [], "rows": [], "legend": [], "notes": "",
                "confidence": 0.0}

    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if isinstance(item, dict) and "cells" in item:
                parsed.setdefault("rows", []).append(item)
            elif isinstance(item, dict):
                parsed.setdefault("rows", []).append(item)

    def s(v: Any) -> str:
        return _stringify_scalar(v)

    warnings: list[str] = []      # FIX-2026-10-17 (P1-4)
    out: dict[str, Any] = {
        "columns": [],
        "rows": [],
        "legend": [],
        "notes": s(parsed.get("notes") or "").strip(),
        "confidence": 0.0,
    }

    # FIX-2026-10-17 (P1-4): a column whose HEADER could not be read is kept
    # as a position, not dropped. The loop used to `continue` on it, which
    # compacted ``out["columns"]`` — and every row's ``cells`` is positional,
    # so ``width = len(out["columns"])`` then shrank with it. The docstring
    # below this block spells out the rule the code broke:
    # "``cells`` is positional and must carry an empty string in that slot
    # rather than shortening the list". Measured on
    # ``columns=[A, "", C]`` / ``cells=["a1","b1","c1"]``:
    #
    #     before: columns [A, C]           cells ["a1", "b1"]
    #             -> b1 filed under column C, and c1 gone entirely
    #     after:  columns [A, "", C]        cells ["a1", "b1", "c1"]
    #
    # That is not a missing column, it is a MISLABELLED one plus silent data
    # loss, and it is invisible downstream because nothing warns about it.
    # The blank header is announced instead, so the reader knows which column
    # they are looking at.
    raw_columns = [c for c in (parsed.get("columns") or [])
                   if isinstance(c, dict)]
    for i, col in enumerate(raw_columns):
        label = s(col.get("label") or col.get("name") or "").strip()
        try:
            index = int(col.get("index"))
        except (TypeError, ValueError):
            index = i
        if not label:
            warnings.append(
                f"column {index} has no readable header; kept at its position "
                f"so the row cells stay aligned with the figure")
        out["columns"].append({"index": index, "label": label})

    width = len(out["columns"])
    for i, row in enumerate(parsed.get("rows") or []):
        if not isinstance(row, dict):
            continue
        cells_in = row.get("cells")
        if not isinstance(cells_in, list):
            cells_in = []
        # Positional contract: pad to the column count so the row's cells[i]
        # always addresses column i. A short list would silently shift every
        # later cell one place left.
        cells = [s(c) for c in cells_in][:width]
        cells.extend([""] * (width - len(cells)))
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            index = i
        out["rows"].append({
            "index": index,
            "label": s(row.get("label") or "").strip(),
            "cells": cells,
        })

    for entry in parsed.get("legend") or []:
        if not isinstance(entry, dict):
            continue
        code = s(entry.get("code") or "").strip()
        if not code:
            continue
        out["legend"].append({
            "code": code,
            "class": s(entry.get("class") or entry.get("meaning") or "").strip(),
        })

    conf = parsed.get("confidence")
    try:
        out["confidence"] = _confidence_clamped(float(conf))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    _carry_extras(parsed, ("columns", "rows", "legend", "notes",
                           "confidence", "_warnings", "warnings"), out)
    if warnings:
        # FIX-2026-10-17 (P1-4 + P3-4): ``_warnings`` like every other mode.
        out["_warnings"] = warnings
    return out


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
        conf = _confidence_clamped(
            float(parsed.get("confidence", 0.0) or 0.0))
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
        from .cache import get_cache, stable_extra_body, stable_extra_headers
        from .prompt import prompt_version_for_mode as _pvm
        _cache = get_cache()
        ckey = _cache.make_key(
            endpoint=p.endpoint if p else "",
            model=p.model if p else "",
            api_format=p.api_format.value if p else "",
            # AUDIT-2026-10-01 [item 9.9]: the two sibling extraction keys in
            # server.py carry these and this one did not, so two providers that
            # differ ONLY in their Authorization value or their vendor
            # extra_body shared one cached classification. Its own comment
            # already noted "the sibling extraction keys already include both
            # fields" -- without seeing that the omission was the bug rather
            # than a deliberate narrowing.
            #
            # It matters more here than it looks: mode="auto" DERIVES the
            # extraction mode from this verdict (see REVIEW-2026-09-10 in the
            # comment below), so a verdict served from another identity's cache
            # silently selects the wrong prompt / normalizer / merge schema for
            # the same image. _stable_extra_body's own docstring already states
            # the rule this was breaking: "two requests with different
            # extra_body MUST NOT share a cache entry".
            extra_headers=stable_extra_headers(p),
            extra_body=stable_extra_body(p),
            prompt_version=_pvm("chart_classify"),
            mode="chart_classify",
            image_b64=image_b64,
            # REVIEW-2026-09-10: the caption and chart-language hint are
            # interpolated into the classification prompt above, so they MUST
            # be part of the key - otherwise the same image resubmitted with a
            # different caption was served the earlier verdict (three distinct
            # captions produced one LLM call), and because mode="auto" derives
            # the extraction mode from this result, a stale verdict silently
            # selected the wrong prompt/normalizer/merge schema. The sibling
            # extraction keys already include both fields.
            caption=caption,
            chart_lang=chart_lang,
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
        # REVIEW-2026-09-20 #26: _call_llm = call_llm_api + one transient retry.
        raw_text, truncated, status, err_body, usage = _call_llm(
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
    timeout_sec: int | None = None,
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
        # REVIEW-2026-09-10: the caller's timeout setting was never forwarded,
        # so a user who set 10 s to bound latency still waited up to the
        # classifier's own 120 s default before extraction even started.
        # classify_chart_image clamps its own value, so passing None keeps the
        # previous behaviour for callers that do not have a deadline.
        _cls_timeout = clamp_timeout_sec(timeout_sec) if timeout_sec is not None else None
        cls = classify_chart_image(
            api_key=api_key, image_b64=image_b64, media_type=media_type,
            caption=caption, provider=provider,
            **({"timeout_sec": _cls_timeout} if _cls_timeout is not None else {}),
            progress_callback=progress_callback,
        )
    except Exception as exc:
        cls = ExtractResult(
            ok=False, error_key="err.classify",
            data={"chart_type": "unknown", "reason": str(exc), "confidence": 0.0},
        )
    if cls.ok and isinstance(cls.data, dict):
        chart_type = cls.data.get("chart_type") or "unknown"
        # REVIEW-2026-09-20 #15: the classifier JSON is model output - a
        # ``"confidence": "high"`` used to raise ValueError out of a function
        # documented as "never raises". Treat it as zero confidence.
        try:
            conf = float(cls.data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
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


def _merge_llm_cost(target: ExtractResult, usage: dict[str, Any] | None,
                    latency_ms: int | float | None) -> ExtractResult:
    """Fold an earlier LLM call's cost into ``target`` (REVIEW-2026-09-20 #8).

    Every attempt is billed even when its result is thrown away, so a retry
    or a vision-classify call that is not the returned one must still show up
    in ``usage`` / ``latency_ms`` - otherwise the history and the cost ledger
    under-report exactly on the runs that needed a second call."""
    if isinstance(usage, dict):
        # FIX-2026-09-22: the LLM layer produces the *_tokens keys
        # (rca_core/usage.py) - the old "input"/"output" names never matched,
        # silently dropping the billed tokens of retries and vision
        # classification from the cost ledger.
        for field in ("input_tokens", "output_tokens",
                      "cache_read_tokens", "cache_creation_tokens"):
            add = int(usage.get(field) or 0)
            if add:
                target.usage[field] = int(target.usage.get(field) or 0) + add
    try:
        lat = int(latency_ms or 0)
    except (TypeError, ValueError):
        lat = 0
    if lat:
        target.latency_ms = int(target.latency_ms or 0) + lat
    return target


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
    # REVIEW-2026-09-20 #14: ``extract`` only ever sees base64, so the auto
    # resolver used to be called with an empty filename and no timeout.
    # Optional so existing callers keep working.
    filename: str = "",
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
        # REVIEW-2026-09-20 #14: the classifier call was made without the
        # caller's ``timeout_sec`` (so a user-set 10 s deadline still waited
        # for the classifier's own default) and with a hardcoded empty
        # filename (so the keyword resolver never saw the file name).
        mode, classify_result = resolve_auto_mode(
            caption=caption,
            filename=filename,
            image_b64=image_b64,
            media_type=media_type,
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider=provider,
            timeout_sec=timeout_sec,
            progress_callback=progress_callback,
        )
        mode_source = "vision" if classify_result is not None else "text"
    # REVIEW-2026-09-20 #8: the vision classify is a billed call whatever the
    # extraction does afterwards - remember its cost so it can be folded into
    # the returned result instead of vanishing from the usage ledger.
    _classify_usage = dict(getattr(classify_result, "usage", None) or {}) if classify_result is not None else {}
    _classify_latency = int(getattr(classify_result, "latency_ms", 0) or 0) if classify_result is not None else 0

    fn = _MODE_DISPATCH.get(mode)
    if fn is None:
        # REVIEW-2026-09-20 #13: "err.http" meant an HTTP failure; an unknown
        # mode is a caller/config error. "err.extract" is the closest key the
        # i18n catalogue already ships - the detail stays in ``raw``.
        #
        # FIX-2026-10-01: the original reason here ("a dedicated err.mode would
        # render untranslated") is no longer true -- err.mode now exists in all
        # three locales on BOTH engines. It is still the wrong key to answer
        # here, but for a different reason: it means "this chart mode is a
        # backend-only capability", which is what the BROWSER transport uses it
        # for (js/minimax.js refuses scatter_plot / paleomap /
        # chemical_stratigraphy with it). An unknown mode on the server side is
        # a caller bug, not a capability limit, so err.extract stays correct.
        return ExtractResult(
            ok=False, error_key="err.extract",
            raw=f"unknown mode: {mode!r} (expected one of: "
                + ", ".join(sorted(_MODE_DISPATCH)) + ")")
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
    if _classify_usage or _classify_latency:
        # REVIEW-2026-09-20 #8: fold the vision-classify cost into the
        # extraction result so the usage ledger matches the real spend.
        _merge_llm_cost(result, _classify_usage, _classify_latency)
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
            # REVIEW-2026-09-20 #8: the swap used to discard the first
            # attempt's usage/latency/warning - it was billed all the same.
            _merge_llm_cost(retry, result.usage, result.latency_ms)
            _prev_warning = (result.warning or "").strip()
            retry.warning = ((retry.warning + "; ") if retry.warning else "") \
                + "empty first attempt retried once"
            if _prev_warning and _prev_warning not in retry.warning:
                retry.warning = _prev_warning + "; " + retry.warning
            result = retry
    # UI-REVIEW-2026-09-07: surface which chart type the auto resolver
    # picked so UIs / history records can show "detected: zonation_chart".
    if mode_source:
        result.mode_used = mode
        result.mode_source = mode_source
    return result
