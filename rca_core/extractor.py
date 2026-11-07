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
# ARCH-REVIEW-2026-11-02 P3-11: matrix.py needs the budget function and this
# module needs extract_matrix_two_stage from matrix.py. Importing the budget
# back from here made a cycle, so it lives in rca_core/budget.py. Re-exported
# so existing callers (and any module-level monkeypatch of the constants)
# keep working.
# ARCH-REVIEW A9: the row-flag convention now lives in its own
# module so grounding.py does not depend on the extractor.
from .row_flags import add_row_flag as _add_row_flag
from .budget import (
    SINGLE_RUN_MAX_ATTEMPTS,
    SINGLE_RUN_DEADLINE_SLACK_SEC,
    MULTI_RUN_TIMEOUT_SLACK_SEC,
    extraction_wall_clock_budget_sec,
)

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
from .llm import ApiFormat, LlmProvider, call_llm_api, _last_response_headers
from .provider_failure import RETRYABLE_STATUS, classify_provider_failure
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
# The ceiling, raised on 2026-10-19 from 32000 to the MODEL's real limit.
#
# MiniMax-M3.1-Flash answers with an explicit bound:
#     "invalid params, model[MiniMax-M3.1-Flash] does not support max tokens
#      > 524288 (2013)"
# -- so 524288 is the largest value that is actually accepted, and 1000000
# is a 400. The old 32000 was a product-side cap that had nothing to do with
# the model, and it was the reason a reasoning model could not be given room
# to think: the thinking tokens are billed against max_tokens, and at 32000
# the "max" effort spent the whole budget thinking and returned an EMPTY
# body (HTTP 200, stop_reason=max_tokens, zero characters of answer).
#
# max_tokens is a CEILING, not a reservation, so raising it costs nothing
# unless the answer is actually that long.
#
# The web UI's slider must match: index.html's max-tokens input and
# js/config.js's maxMaxTokens both carry this number.
MAX_MAX_TOKENS = 524288
DEFAULT_TIMEOUT_SEC = 120
DEFAULT_MAX_EDGE = 4000

#: The server's single-run budget, derived from the number of outbound
#: attempts the stack can spend on one extraction.
#:
#: REVIEW-2026-10-19: this was 6, derived for a SINGLE call with a retry
#: (call_llm_api_with_retry's 3) plus the empty-reply re-ask, the "auto"
#: classification round-trip and a spare. The two-stage occurrence-matrix
#: path makes FOUR calls of its own -- one for the labels, one per contact
#: sheet -- and each of them may retry, so the stack can spend EIGHT
#: outbound attempts. A budget derived from six was already too small for
#: the two-stage path, and the reasoning levels made the calls slower still.
#:
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
# P1-4: VALID_OCCURRENCE_MODES moved verbatim to rca_core/modes_impl/; re-exported at end of file.

# P1-4: VALID_ENDPOINT_KINDS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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


# P1-4: _KNOWN_RANGE_CHART_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_SECTION_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_SPECIES_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_BIOZONE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

# MEDIUM fix: iron-rule markers for zone labels misclassified as species.
# Per prompt.py:80, names ending in Zone / Zonule / assemblage go into
# ``biozones``, NEVER into ``species_ranges``. We post-normalize to flag any
# slip-through so the operator can see it instead of silently exporting a
# fabricated FAD/LAD for a non-taxon.
# P1-4: _IRON_RULE_ZONE_RE moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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
# P1-4: _NAME_OPEN_NOMINATURE moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _NAME_QUALIFIERS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _name_label_parts moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _is_ascii_lowercase_start moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _is_wrapped_header_fragment moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _is_bare_epithet moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _is_wrappable_header_base moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _is_bare_authority moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _row_is_iron_rule_rejected moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _guard_wrapped_header_rows moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _classify_array_item moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _carry_extras moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _coerce_bool_flag moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _row_from_string moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _single_record_from_dict moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _iter_rows moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _dict_rows moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _PRIMARY_ID_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _merge_other_fossils moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _other_fossils_from moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _stringify_scalar moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _pop_array_root_extras moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _extracted_any moved verbatim to rca_core/modes_impl/; re-exported at end of file.


#: Keys whose presence says nothing about whether any DATA came out.
#: Read by ``_extracted_any`` only; the normalizers themselves are unchanged.
#: ``_extras`` is intentionally ABSENT — see ``_extracted_any``'s docstring.
# P1-4: _NON_CONTENT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _unwrap_array_root_into moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _append_warning moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _rehydrate_array_root moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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


# P1-4: _normalize_section_into moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _normalize_occurrence_mode moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _normalize_endpoint_kind moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _normalize_optional_int moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _confidence_clamped moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _normalize_confidence moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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
# P1-4: POS_SCALE_KEY moved verbatim to rca_core/modes_impl/; re-exported at end of file.
#: Suffix for a per-boundary position (``range_top_pos_0_999``, ``x_pos_0_999``…).
# P1-4: POS_SUFFIX moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: POS_MIN moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: POS_MAX moved verbatim to rca_core/modes_impl/; re-exported at end of file.
#: Version + scale tag written into every ``geometry`` block so a future
#: consumer (rca_core/geometry.py) can tell what the numbers mean.
# P1-4: GEOMETRY_VERSION moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: GEOMETRY_SCALE moved verbatim to rca_core/modes_impl/; re-exported at end of file.
#: How far outside the calibrated axis domain a converted position may sit
#: (fraction of the axis span) before the whole geometry block is discarded.
# P1-4: GEOMETRY_TOLERANCE moved verbatim to rca_core/modes_impl/; re-exported at end of file.
#: Axis names the calibration block may carry.
GEOMETRY_AXIS_NAMES = ("vertical", "x", "y")

#: Row-level source keys the coverage contract consumes. They are added to
#: every ``_carry_extras`` known-key tuple so a model-emitted ``response_kind``
#: lands on the row as ``response_kind`` and never as a duplicate under
#: ``_extras`` (BORROW-2026-09-20 A).
# P1-4: _CONTRACT_SOURCE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

#: geometry field -> axis name. Deliberately NOT a catch-all: an unknown
#: ``<prefix>_pos_0_999`` field maps to the axis named ``<prefix>`` only when
#: the result actually calibrates that axis, so a bar-length read can never
#: silently borrow the vertical (stratigraphic) axis and produce a plausible
#: but wrong engineering value.
# P1-4: _GEOMETRY_AXIS_BY_FIELD moved verbatim to rca_core/modes_impl/; re-exported at end of file.
#: Fields that ARE the vertical axis by contract (per the prompt wording).
# P1-4: _GEOMETRY_VERTICAL_FIELDS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _geometry_axis_for moved verbatim to rca_core/modes_impl/; re-exported at end of file.


#: geometry field -> row fields holding the same boundary as a readable value.
#: Used ONLY as a contradiction test, never as a source for the geometry.
# P1-4: _GEOMETRY_SEMANTIC_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.




# P1-4: _to_float_opt moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_pos_0_999 moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _axis_domain moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _axis_calibration_blocks moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: axis_domains_from moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: axis_calibration_unusable moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _fold_axis_calibration moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: pos_to_axis_value moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _geometry_point moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: geometry_from_row moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: apply_coverage_contract moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: attach_row_contract moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _normalize_species_into moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: _normalize_biozone_into moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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
TRANSPORT_ATTEMPTS = 4                  # 1 normal call + 3 retries
#: How many calls a NETWORK-level failure (status None -- a read timeout, a
#: reset connection) is allowed. Deliberately smaller than
#: ``TRANSPORT_ATTEMPTS``: see the note above the two constants.
TRANSPORT_TIMEOUT_ATTEMPTS = 2         # 1 normal call + 1 retry
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
# REVIEW-2026-10-20: the set now lives in provider_failure, next to the
# classifier that consumes it. The classifier's status fallback IS this
# set, so the two can no longer drift -- and the transport layer below
# asks the CLASSIFIER, which also knows the cases the status alone
# lies about (a 429 that is really an exhausted plan, a 200 whose
# stream ended early).
_RETRYABLE_TRANSPORT_STATUS = RETRYABLE_STATUS

# REVIEW-2026-10-19: the two failure kinds cost wildly different amounts of
# the server's single-run budget, and one retry count for both was wrong for
# each of them.
#
#   * A 429/529/503 is FAST: the upstream said "busy, come back" in about a
#     second. Measured on MiniMax-M3.1-Flash at a congested tier, the
#     reference figure's three-run regression lost 31 of 93 cells to bare
#     529s (precision/recall 0.333 and 0.667) and a third run failed
#     outright -- one retry is simply not enough for a busy tier, and
#     retrying a 1-second answer is nearly free.
#   * A read TIMEOUT costs the whole per-call timeout, and the two-stage path
#     already budgets four calls against the server's 745 s.
#
# So: a server that ANSWERED gets more tries, a network-level failure keeps
# the old count. The worst case is unchanged -- at most two timeouts per
# call, four calls, 2 x 90 s each = 720 s < 745 s -- while the cheap
# failures now get three extra shots.
#
# The attempt counter is therefore split in two: ``calls`` counts every
# request, and a network failure is judged against the SMALLER budget.


def _call_llm(*, time_budget_sec: float | None = None,
              stream: bool = False, thinking_callback=None, **kwargs):
    """``call_llm_api`` with one bounded retry on transient transport errors.

    Returns the same 5-tuple. The LAST attempt's status is what is returned,
    so ``_error_from_status`` keeps mapping 429 -> ``err.429`` and
    ``None`` -> ``err.network``: the error CONTRACT is unchanged, only
    ``error_body`` gains the ``[transport retry ...]`` annotation that
    ``call_llm_api_with_retry`` already uses. Non-transient answers (2xx, 4xx
    other than the above) never cost an extra request.

    ``time_budget_sec`` caps the wall clock THIS call may spend, retries
    included. It exists because an attempt count cannot express the two
    failure kinds: a 429/529 costs about a second and may be retried as often
    as the budget allows, while a read timeout costs the whole per-call
    ceiling. The check is made BEFORE each retry, against the call's own
    timeout, so the chain stops exactly when one more attempt would not fit.
    """
    attempts = TRANSPORT_ATTEMPTS
    try:
        attempts = max(1, int(attempts))
    except (TypeError, ValueError):
        attempts = 4
    timeout_attempts = TRANSPORT_TIMEOUT_ATTEMPTS
    try:
        timeout_attempts = max(1, int(timeout_attempts))
    except (TypeError, ValueError):
        timeout_attempts = 2
    # The per-call ceiling, used to decide whether one more attempt fits in
    # the caller's budget.
    try:
        per_call_timeout = float(kwargs.get("timeout_sec") or 0)
    except (TypeError, ValueError):
        per_call_timeout = 0.0
    t_start = time.monotonic()
    notes: list[str] = []
    if stream and thinking_callback is not None:
        kwargs["stream"] = True
        kwargs["thinking_callback"] = thinking_callback
    result = call_llm_api(**kwargs)
    calls = 1
    attempt = 0
    while True:
        text, _truncated, status, _err_body, _usage = result
        if text is not None:
            break
        # REVIEW-2026-10-20: the caller cancelled. That is not a failure
        # and not a reason to try again.
        try:
            from .llm import is_cancelled
            if is_cancelled(_err_body):
                break
        except Exception:
            pass
        # REVIEW-2026-10-20 (borrowed from opencode's provider-error.ts):
        # the CLASSIFIER decides, and it reproduces the old status policy
        # for every status it knows. Two cases the status alone got wrong:
        #
        # * a 429 whose body says the plan is exhausted. MiniMax's Token
        #   Plan answers 429 both for "you are going too fast" (retry after
        #   a wait) and for "the weekly bucket is empty" (retrying cannot
        #   help, and it spends the 5-hour bucket). Retrying the second
        #   kind four times is how a busy tier gets made worse;
        # * a 200 whose STREAM ended early -- the model never said it was
        #   done, so what arrived is a prefix of the answer, and a prefix
        #   of a table is a wrong table.
        try:
            failure = classify_provider_failure(
                status, _err_body, _last_response_headers())
        except Exception:
            failure = None
        if failure is not None:
            if not failure.retryable:
                notes.append("[%s - not attempting it again: %s]"
                             % (failure.category, failure.reason))
                break
        elif status is not None and status not in _RETRYABLE_TRANSPORT_STATUS:
            break
        # REVIEW-2026-10-19: a server that ANSWERED (429/529/503...) gets the
        # full attempt budget, because that answer came back in about a
        # second; a network-level failure gets the smaller one, because it
        # already cost the whole per-call timeout.
        if status is None:
            if calls >= timeout_attempts:
                break
            max_retries = timeout_attempts - 1
        else:
            if calls >= attempts:
                break
            max_retries = attempts - 1
        # The budget is the outer bound: even a cheap failure is not retried
        # when the caller's remaining time cannot cover one more attempt.
        if time_budget_sec is not None:
            spent = time.monotonic() - t_start
            if spent + per_call_timeout > time_budget_sec:
                break
        try:
            from . import error_utils
            delay = error_utils.get_retry_delay(
                status=status,
                # REVIEW-2026-10-20: the headers are available now, so a
                # provider that answers "429 Retry-After: 120" is waited
                # for the 120 seconds it asked for.
                headers=_last_response_headers(), attempt=attempt,
                initial_delay=TRANSPORT_BASE_BACKOFF_SEC,
            )
        except Exception:
            delay = TRANSPORT_BASE_BACKOFF_SEC
        notes.append(f"[transport retry {attempt + 1}/{max_retries}"
                     f" after {delay:.1f}s: HTTP {status if status is not None else 'network'}]")
        try:
            from .llm import interruptible_sleep
            if interruptible_sleep(delay):
                break
        except Exception:
            time.sleep(delay)
        result = call_llm_api(**kwargs)
        calls += 1
        attempt += 1
    if notes:
        text, truncated, status, err_body, usage = result
        annotated = err_body
        for note in notes:
            annotated = (annotated + "\n" + note) if annotated else note
        result = (text, truncated, status, annotated, usage)
    return result


# P1-4: _KNOWN_COLUMNAR_SECTION_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_BLOCK_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_UNIT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_SAMPLE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_LEGEND_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_CROSS_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_COLUMNAR_ROOT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_columnar_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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


# P1-4: _KNOWN_ABUNDANCE_ROOT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_SITE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_ABUNDANCE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_ZONE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_abundance_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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


# P1-4: _normalize_phylogenetic_tree_into moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_phylogenetic_tree_result (alias) moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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

# P1-4: _KNOWN_CHEMICAL_STRAT_ROOT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_CHEMICAL_STRAT_EVENT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_chemical_stratigraphy_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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

# P1-4: _KNOWN_PALEOMAP_ROOT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

# Sprint B (REVIEW-2026-09-04): per-row known keys so the H8 ``_carry_extras``
# contract holds for sub-rows too — previously any extra key the model emitted
# inside a continents/oceans_seas/... row was silently discarded.
# P1-4: _KNOWN_PALEOMAP_CONTINENT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_PALEOMAP_SEA_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_PALEOMAP_TECTONIC_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_PALEOMAP_REALM_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_PALEOMAP_SITE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_PALEOMAP_INDICATOR_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

# AUDIT-2026-09-27 P0-1: the row tables of a paleogeographic map, most
# specific probe first. ``_classify_array_item`` is a RANGE-CHART heuristic
# that maps any dict carrying a ``name`` onto "sections", which is not a
# paleomap table at all - so without these probes every array item fell
# through to ``_unclassified`` and the map came back empty. A bare
# ``{"name": ...}`` on a paleogeographic map is a landmass, hence the
# ``continents`` catch-all last.
# P1-4: _PALEOMAP_ARRAY_SPEC_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: first_non_empty moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_paleomap_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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

# P1-4: _KNOWN_SCATTER_PLOT_ROOT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

# Sprint B (REVIEW-2026-09-04): per-row known keys for the H8 ``_carry_extras``
# contract on scatter sub-rows (groups / points / outliers).
# P1-4: _KNOWN_SCATTER_GROUP_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_SCATTER_POINT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_SCATTER_OUTLIER_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

# AUDIT-2026-09-27 P0-1: scatter row probes, most specific first. A point is
# the only table carrying a measured coordinate PAIR; an outlier is the only
# one carrying ``reason``; a bare ``{"name": ...}`` is a series definition.
# P1-4: _SCATTER_ARRAY_SPEC_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_scatter_plot_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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
# P1-4: _KNOWN_ZONATION_ROOT_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_ZONATIONS_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_ZONATION_ZONE_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: _KNOWN_CORRELATION_KEYS moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_zonation_chart_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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
# P1-4: OCCURRENCE_CELL_STATUSES moved verbatim to rca_core/modes_impl/; re-exported at end of file.
# P1-4: DEFAULT_OCCURRENCE_CELL_STATUS moved verbatim to rca_core/modes_impl/; re-exported at end of file.

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
    thinking_callback=None,
    cell_strategy: str = OCCURRENCE_DEFAULT_CELL_STRATEGY,
) -> ExtractResult:
    """Taxon x locality occurrence-matrix extraction.

    Same contract as extract_range_chart. Unlike every other mode, the taxa are
    the rows and the localities are the COLUMNS, so there is no continuous axis
    to calibrate and no ``pos_0_999`` to read.

    ``cell_strategy`` selects how much of the figure the model is asked for.
    See ``OCCURRENCE_CELL_STRATEGIES``. Whatever it is, the returned data
    carries a ``cell_status`` saying which parts were verified.

    ``thinking_callback``, if given, receives the model's THINKING as it is
    produced (the two-stage path streams its calls). It is the difference
    between a progress indicator that says "working" and one that shows what
    the model is doing -- which matters at the higher reasoning levels, where
    a single call can spend a minute and a half thinking.
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
            from .matrix import (extract_matrix_two_stage, guess_media_type,
                                matrix_budgets_for)

            # REVIEW-2026-10-19: this path gets its OWN wall-clock budget,
            # and the budget DEPENDS on the reasoning level.
            #
            # The server's single-run budget is `timeout_sec * 8 + 25` s and its
            # meaning is "at most eight outbound attempts' worth of wall
            # clock". This path spends FOUR calls (one for the labels, one per
            # contact sheet), and each of them may retry -- so counting calls
            # is no longer enough and the arithmetic has to be done in
            # seconds.
            #
            # A budget does that job better than an attempt count, because the
            # two failure kinds cost wildly different amounts: a 429/529 comes
            # back in about a second and can be retried as often as the
            # budget allows, while a read timeout costs the whole per-call
            # ceiling. A deadline therefore lets the cheap failures be
            # retried freely and stops the expensive ones exactly when they
            # would start to threaten the run.
            #
            # And the budget scales with the level, because the level decides
            # how long the model thinks: measured, the label call takes
            # 28.3 s on "low", 48.3 s on "high" and 69-102 s on "max". A single
            # 90 s ceiling was right for "low" and too small for the others --
            # at "max" the path gave up before it could answer, and the
            # end-to-end regression failed 3 of 3.
            _per_call_ceiling, _path_budget = matrix_budgets_for(
                p.model, p.reasoning_effort, timeout_sec)
            _grid_deadline = time.monotonic() + _path_budget

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
                # REVIEW-2026-10-19: the per-call timeout is CAPPED, and so is
                # the call's share of the path budget, so a hung call can
                # never spend more than its share.
                _remaining = _grid_deadline - time.monotonic()
                if _remaining <= 1:
                    return None, False, (
                        "[matrix] the path budget was exhausted before this "
                        "call; the single-pass fallback will answer instead")
                _call_timeout = int(min(_per_call_ceiling, _remaining))
                raw, trunc, _status, err_body, usage = _call_llm(
                    provider=p,
                    system_prompt=system_prompt,
                    image_b64=img_b64,
                    media_type=guess_media_type(img_b64),
                    user_text=user_text,
                    max_tokens=mt,
                    timeout_sec=_call_timeout,
                    time_budget_sec=_remaining,
                    capture_error_body=True,
                    progress_callback=progress_callback,
                    # REVIEW-2026-10-19: streamed, so the model's THINKING
                    # reaches the UI while it is still thinking. Measured
                    # against M3.1-Flash: the first thinking chunk arrives
                    # about a second in, and the assembled answer is the same
                    # one the single-body call returns.
                    stream=thinking_callback is not None,
                    thinking_callback=thinking_callback,
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
                image_b64, _grid_read_call, max_tokens=max_tokens,
                progress=progress_callback)
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

    # REVIEW-2026-10-19: tell the UI that the two-stage path is over and
    # the single-pass fallback is answering now, so the progress line does
    # not claim a sheet is being read when it is not.
    if progress_callback:
        try:
            progress_callback("call:single")
        except Exception:
            pass

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


# P1-4: normalize_occurrence_matrix_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


# P1-4: normalize_generic_table_result moved verbatim to rca_core/modes_impl/; re-exported at end of file.


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
        from .cache import (get_cache, stable_extra_body, stable_extra_headers,
                            stable_reasoning_effort)
        from .cache import build_extraction_key
        from .prompt import prompt_version_for_mode as _pvm
        _cache = get_cache()
        ckey = build_extraction_key(
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
            # REVIEW-2026-10-19: the effort changes the request shape the
            # same way extra_body does -- it picks the model's reasoning
            # depth, and on MiniMax's M3.1 it decides whether the call
            # answers at all -- so it must not share a cache entry with a
            # different one. The same rule the comment above states.
            reasoning_effort=stable_reasoning_effort(p),
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


def _thinking_kwargs(fn, thinking_callback) -> dict[str, Any]:
    """The ``thinking_callback`` kwarg, but only for a mode that accepts one.

    The modes share one dispatch table and one call site, and they do NOT
    share a signature: only the occurrence matrix streams its calls today.
    Passing the kwarg to a mode that does not accept it is a TypeError, and
    adding it to every mode's signature would advertise a capability the
    other seven do not have. So the call site's kwargs are built from what
    the target actually takes.
    """
    if thinking_callback is None:
        return {}
    try:
        import inspect
        if "thinking_callback" in inspect.signature(fn).parameters:
            return {"thinking_callback": thinking_callback}
    except (TypeError, ValueError):
        pass
    return {}


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
    # REVIEW-2026-10-19: the model's THINKING, as it is produced. It reaches
    # the modes that stream their calls (today: the occurrence matrix, whose
    # two-stage path makes four calls that can each take a minute).
    thinking_callback=None,
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
        **_thinking_kwargs(fn, thinking_callback),
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
                **_thinking_kwargs(fn, thinking_callback),
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


# ===========================================================================
# Phase 3 / P1-4: mode normalizers + their shared helpers/constants now
# live in rca_core/modes_impl/. The 101 shared names below were MOVED
# VERBATIM into modes_impl/_shared.py (a LEAF: stdlib + two rca_core leaf
# modules, NO import of extractor). The 17 normalizer bodies were moved
# VERBATIM into modes_impl/<name>.py. This block re-exports them so every
# existing import path, call site and monkeypatch target resolves to the
# SAME object. Direction is ONE-WAY: extractor -> modes_impl. Do not add
# any import of extractor inside modes_impl/ -- see
# tests/test_modes_impl_acyclic.py.
# ===========================================================================
from .modes_impl._shared import (
    _add_row_flag as _add_row_flag,
    normalize_response_kind as normalize_response_kind,
    has_value as has_value,
    RESPONSE_EXTRACTED as RESPONSE_EXTRACTED,
    LOW_CONFIDENCE_CODE as LOW_CONFIDENCE_CODE,
    RESPONSE_NOT_DRAWN as RESPONSE_NOT_DRAWN,
    normalize_codes as normalize_codes,
    VALID_OCCURRENCE_MODES as VALID_OCCURRENCE_MODES,
    VALID_ENDPOINT_KINDS as VALID_ENDPOINT_KINDS,
    _KNOWN_RANGE_CHART_KEYS as _KNOWN_RANGE_CHART_KEYS,
    _KNOWN_SECTION_KEYS as _KNOWN_SECTION_KEYS,
    _KNOWN_SPECIES_KEYS as _KNOWN_SPECIES_KEYS,
    _KNOWN_BIOZONE_KEYS as _KNOWN_BIOZONE_KEYS,
    _IRON_RULE_ZONE_RE as _IRON_RULE_ZONE_RE,
    _NAME_OPEN_NOMINATURE as _NAME_OPEN_NOMINATURE,
    _NAME_QUALIFIERS as _NAME_QUALIFIERS,
    _name_label_parts as _name_label_parts,
    _is_ascii_lowercase_start as _is_ascii_lowercase_start,
    _is_wrapped_header_fragment as _is_wrapped_header_fragment,
    _is_bare_epithet as _is_bare_epithet,
    _is_wrappable_header_base as _is_wrappable_header_base,
    _is_bare_authority as _is_bare_authority,
    _row_is_iron_rule_rejected as _row_is_iron_rule_rejected,
    _guard_wrapped_header_rows as _guard_wrapped_header_rows,
    _classify_array_item as _classify_array_item,
    _carry_extras as _carry_extras,
    _coerce_bool_flag as _coerce_bool_flag,
    _row_from_string as _row_from_string,
    _single_record_from_dict as _single_record_from_dict,
    _iter_rows as _iter_rows,
    _dict_rows as _dict_rows,
    _PRIMARY_ID_KEYS as _PRIMARY_ID_KEYS,
    _merge_other_fossils as _merge_other_fossils,
    _other_fossils_from as _other_fossils_from,
    _stringify_scalar as _stringify_scalar,
    _pop_array_root_extras as _pop_array_root_extras,
    _extracted_any as _extracted_any,
    _NON_CONTENT_KEYS as _NON_CONTENT_KEYS,
    _unwrap_array_root_into as _unwrap_array_root_into,
    _append_warning as _append_warning,
    _rehydrate_array_root as _rehydrate_array_root,
    _confidence_clamped as _confidence_clamped,
    POS_SCALE_KEY as POS_SCALE_KEY,
    POS_SUFFIX as POS_SUFFIX,
    POS_MIN as POS_MIN,
    POS_MAX as POS_MAX,
    GEOMETRY_VERSION as GEOMETRY_VERSION,
    GEOMETRY_SCALE as GEOMETRY_SCALE,
    GEOMETRY_TOLERANCE as GEOMETRY_TOLERANCE,
    _CONTRACT_SOURCE_KEYS as _CONTRACT_SOURCE_KEYS,
    _GEOMETRY_AXIS_BY_FIELD as _GEOMETRY_AXIS_BY_FIELD,
    _GEOMETRY_VERTICAL_FIELDS as _GEOMETRY_VERTICAL_FIELDS,
    _geometry_axis_for as _geometry_axis_for,
    _GEOMETRY_SEMANTIC_KEYS as _GEOMETRY_SEMANTIC_KEYS,
    _to_float_opt as _to_float_opt,
    normalize_pos_0_999 as normalize_pos_0_999,
    _axis_domain as _axis_domain,
    _axis_calibration_blocks as _axis_calibration_blocks,
    axis_domains_from as axis_domains_from,
    axis_calibration_unusable as axis_calibration_unusable,
    _fold_axis_calibration as _fold_axis_calibration,
    pos_to_axis_value as pos_to_axis_value,
    _geometry_point as _geometry_point,
    geometry_from_row as geometry_from_row,
    apply_coverage_contract as apply_coverage_contract,
    attach_row_contract as attach_row_contract,
    _KNOWN_COLUMNAR_SECTION_KEYS as _KNOWN_COLUMNAR_SECTION_KEYS,
    _KNOWN_BLOCK_KEYS as _KNOWN_BLOCK_KEYS,
    _KNOWN_UNIT_KEYS as _KNOWN_UNIT_KEYS,
    _KNOWN_SAMPLE_KEYS as _KNOWN_SAMPLE_KEYS,
    _KNOWN_LEGEND_KEYS as _KNOWN_LEGEND_KEYS,
    _KNOWN_CROSS_KEYS as _KNOWN_CROSS_KEYS,
    _KNOWN_COLUMNAR_ROOT_KEYS as _KNOWN_COLUMNAR_ROOT_KEYS,
    _KNOWN_ABUNDANCE_ROOT_KEYS as _KNOWN_ABUNDANCE_ROOT_KEYS,
    _KNOWN_SITE_KEYS as _KNOWN_SITE_KEYS,
    _KNOWN_ABUNDANCE_KEYS as _KNOWN_ABUNDANCE_KEYS,
    _KNOWN_ZONE_KEYS as _KNOWN_ZONE_KEYS,
    _KNOWN_CHEMICAL_STRAT_ROOT_KEYS as _KNOWN_CHEMICAL_STRAT_ROOT_KEYS,
    _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS as _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS,
    _KNOWN_CHEMICAL_STRAT_EVENT_KEYS as _KNOWN_CHEMICAL_STRAT_EVENT_KEYS,
    _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS as _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS,
    _KNOWN_PALEOMAP_ROOT_KEYS as _KNOWN_PALEOMAP_ROOT_KEYS,
    _KNOWN_PALEOMAP_CONTINENT_KEYS as _KNOWN_PALEOMAP_CONTINENT_KEYS,
    _KNOWN_PALEOMAP_SEA_KEYS as _KNOWN_PALEOMAP_SEA_KEYS,
    _KNOWN_PALEOMAP_TECTONIC_KEYS as _KNOWN_PALEOMAP_TECTONIC_KEYS,
    _KNOWN_PALEOMAP_REALM_KEYS as _KNOWN_PALEOMAP_REALM_KEYS,
    _KNOWN_PALEOMAP_SITE_KEYS as _KNOWN_PALEOMAP_SITE_KEYS,
    _KNOWN_PALEOMAP_INDICATOR_KEYS as _KNOWN_PALEOMAP_INDICATOR_KEYS,
    _PALEOMAP_ARRAY_SPEC_KEYS as _PALEOMAP_ARRAY_SPEC_KEYS,
    first_non_empty as first_non_empty,
    _KNOWN_SCATTER_PLOT_ROOT_KEYS as _KNOWN_SCATTER_PLOT_ROOT_KEYS,
    _KNOWN_SCATTER_GROUP_KEYS as _KNOWN_SCATTER_GROUP_KEYS,
    _KNOWN_SCATTER_POINT_KEYS as _KNOWN_SCATTER_POINT_KEYS,
    _KNOWN_SCATTER_OUTLIER_KEYS as _KNOWN_SCATTER_OUTLIER_KEYS,
    _SCATTER_ARRAY_SPEC_KEYS as _SCATTER_ARRAY_SPEC_KEYS,
    _KNOWN_ZONATION_ROOT_KEYS as _KNOWN_ZONATION_ROOT_KEYS,
    _KNOWN_ZONATIONS_KEYS as _KNOWN_ZONATIONS_KEYS,
    _KNOWN_ZONATION_ZONE_KEYS as _KNOWN_ZONATION_ZONE_KEYS,
    _KNOWN_CORRELATION_KEYS as _KNOWN_CORRELATION_KEYS,
    OCCURRENCE_CELL_STATUSES as OCCURRENCE_CELL_STATUSES,
    DEFAULT_OCCURRENCE_CELL_STATUS as DEFAULT_OCCURRENCE_CELL_STATUS,
)
from .modes_impl.range_chart import (
    _normalize_section_into as _normalize_section_into,
    _normalize_occurrence_mode as _normalize_occurrence_mode,
    _normalize_endpoint_kind as _normalize_endpoint_kind,
    _normalize_optional_int as _normalize_optional_int,
    _normalize_confidence as _normalize_confidence,
    _normalize_species_into as _normalize_species_into,
    _normalize_biozone_into as _normalize_biozone_into,
    normalize_result as normalize_result,
)
from .modes_impl.columnar_section import (
    normalize_columnar_result as normalize_columnar_result,
)
from .modes_impl.abundance_diagram import (
    normalize_abundance_result as normalize_abundance_result,
)
from .modes_impl.phylogenetic_tree import (
    _normalize_phylogenetic_tree_into as _normalize_phylogenetic_tree_into,
)
from .modes_impl.zonation_chart import (
    normalize_zonation_chart_result as normalize_zonation_chart_result,
)
from .modes_impl.chemical_stratigraphy import (
    normalize_chemical_stratigraphy_result as normalize_chemical_stratigraphy_result,
)
from .modes_impl.paleomap import (
    normalize_paleomap_result as normalize_paleomap_result,
)
from .modes_impl.scatter_plot import (
    normalize_scatter_plot_result as normalize_scatter_plot_result,
)
from .modes_impl.occurrence_matrix import (
    normalize_occurrence_matrix_result as normalize_occurrence_matrix_result,
)
from .modes_impl.generic_table import (
    normalize_generic_table_result as normalize_generic_table_result,
)
from .modes_impl.phylogenetic_tree import (
    normalize_phylogenetic_tree_result as normalize_phylogenetic_tree_result,
)
