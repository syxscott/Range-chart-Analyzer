"""Deterministic grid geometry for a ruled taxon x locality table.

WHY THIS MODULE EXISTS
======================
Measured on the Ozsvart et al. range chart
(``_figtest/r11_ozsvart_table.png``, 777x543): a vision model reads the big
rotated sample names and the italic taxon names essentially without error,
and gets the *abundance codes* wrong - they are single letters typed at ~8 px
inside a 24x17 px cell, and the model returns them shifted by a row or a
column often enough to corrupt the matrix while looking perfectly plausible.
The letters cannot be recovered by better prompting; the only reliable fix
is to stop asking the model for geometry at all.

So the geometry is computed here, on the CPU, from the pixels alone. The
model is left the one thing it is good at (reading names) and this module
supplies the other thing (the lattice the names sit in).

THE FEATURE: CONTINUITY, NOT DARKNESS
=====================================
The first attempt thresholded the *mean* darkness of each pixel row and it
failed completely: the dense italic text in the top-left corner is the
darkest thing on the page and it scored above every real rule, while the
rules themselves - thin, but perfectly uniform - did not separate. Mean
intensity is the wrong feature because a rule and a paragraph have the same
ink budget at different densities.

The feature that works is the FRACTION of a row that is ink. A ruling line
is not "dark", it is *unbroken*: every pixel across the table's width is
ink. Measured on the figure above, the 23 horizontal rules that bound the
taxon block score frac = 0.991, while the densest line of italic species
text scores ~0.30. There is no overlap and no tuning knob in between.

Same in the other axis: a vertical rule is dark down a large fraction of the
table's height (rotated header text is not), so column rules are found by
the column-mean of the ink mask.

WHY IT RETURNS ``None`` INSTEAD OF GUESSING
===========================================
The caller falls back to its single-pass path on ``None``. A fabricated
geometry would be far worse than no geometry: the fallback still produces a
usable extraction, whereas a wrong lattice silently shifts every cell by one
row and turns a good result into a confidently wrong one. Every rejection
below is therefore tuned to refuse, not to cope. In particular a figure with
NO ruling (a hand-drawn chart, a bar chart, a photo of a book page) must come
back ``None``, and random noise - which is the adversarial case, because its
per-row ink fraction sits near 0.5 and a stray run can clear a fixed
threshold - is rejected by the spacing-uniformity tests rather than by any
ink-brightness test.

DEPENDENCIES: numpy + Pillow only, and Pillow is not imported at module
import time (it was just dropped from the PyInstaller bundle's hard set along
with torch/scipy/pandas/spacy), so importing this module in a headless test
never needs it.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "GridGeometry",
    "detect_grid",
    "INK_LEVEL",
    "ROW_RULE_FRAC",
    "COL_RULE_FRAC",
    "RULE_MERGE_GAP",
    "MIN_ROW_RULES",
    "MIN_COL_RULES",
    "MAX_SPACING_CV",
    "HEADER_PITCH_TOL",
    "LABEL_COL_RATIO",
]

# --------------------------------------------------------------------------
# Tunables. Every one of these is a measured quantity on a real ruled table,
# not a round number picked for looks. The two *_FRAC values are the ones the
# second prototype settled on after the threshold-based first attempt failed.
# --------------------------------------------------------------------------

#: Gray level below which a pixel counts as ink. 200 sits well clear of the
#: paper (>=240 in the source scan) and well below the antialiased edge of a
#: stroke, so a rule's core pixels are solidly inside and its halo is not.
INK_LEVEL = 200.0

#: A horizontal rule is ink across at least this fraction of the table's
#: width. Measured: real rules 0.991, densest italic text row ~0.30.
ROW_RULE_FRAC = 0.80

#: A vertical rule is ink across at least this fraction of the table's
#: height. Lower than the row figure because the header band's rotated
#: sample names are dark in most columns and eat into the denominator.
COL_RULE_FRAC = 0.55

#: Pixel gap under which two dark runs are one 2-3 px thick rule.
RULE_MERGE_GAP = 2

#: A real occurrence table has far more rows than this; below it, whatever we
#: found is text, not a lattice.
MIN_ROW_RULES = 6

#: Frame + label rule + at least two sample columns.
MIN_COL_RULES = 4

#: Rejected above this coefficient of variation in spacing. A ruled table's
#: rows are a printing constraint, not an aesthetic one, so they come out
#: within a pixel or two of each other; prose and noise do not.
MAX_SPACING_CV = 0.25

#: A header band's rule spacing deviates from the modal row pitch by more
#: than this fraction of it. Measured: taxon rows sit within 6% of the pitch,
#: the header bands at 35% and 594%, so the threshold lives between them.
HEADER_PITCH_TOL = 0.15

#: The label column is this many times the median data-column width.
LABEL_COL_RATIO = 2.0


@dataclass(frozen=True)
class GridGeometry:
    """The measured lattice of one ruled table.

    Spans are ``(x0, x1)`` / ``(y0, y1)`` in ORIGINAL image pixel
    coordinates and are half-open on the right: ``x1`` / ``y1`` is the start
    of the next rule line, not its last inked column. That is what makes the
    spans tile the table exactly and makes ``col_spans[i][1] == col_spans[i+1][0]``
    true by construction.
    """

    n_cols: int
    n_rows: int
    col_spans: Tuple[Tuple[int, int], ...]
    row_spans: Tuple[Tuple[int, int], ...]
    header_span: Optional[Tuple[int, int]]
    label_span: Optional[Tuple[int, int]]
    confidence: float


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def _decode_gray(image_b64: str):
    """base64 (with or without a data-URL prefix) -> PIL "L" image, else None."""
    try:
        from PIL import Image  # noqa: WPS433 - deliberately lazy, see module doc
    except Exception:
        return None
    if not isinstance(image_b64, str):
        return None
    payload = image_b64.strip()
    if payload.lower().startswith("data:"):
        # data:image/png;base64,<payload> - the caller may hand us either.
        if "," not in payload:
            return None
        payload = payload.split(",", 1)[1]
    payload = "".join(payload.split())
    if not payload:
        return None
    raw = None
    for decoder in (
        lambda s: base64.b64decode(s, validate=False),
        # Some clients emit the URL-safe alphabet (-_ instead of +/).
        lambda s: base64.b64decode(s.replace("-", "+").replace("_", "/"), validate=False),
    ):
        try:
            raw = decoder(payload)
            break
        except (binascii.Error, ValueError):
            continue
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img.convert("L")  # PNG and JPEG alike; convert() normalises both
    except Exception:
        return None


# --------------------------------------------------------------------------
# rule extraction
# --------------------------------------------------------------------------


def _groups(idx: Sequence[int], gap: int = RULE_MERGE_GAP) -> List[Tuple[int, int]]:
    """Collapse sorted indexes into runs separated by more than ``gap``."""
    out: List[Tuple[int, int]] = []
    cur: List[int] = []
    for v in idx:
        if cur and v - cur[-1] > gap:
            out.append((cur[0], cur[-1]))
            cur = []
        cur.append(v)
    if cur:
        out.append((cur[0], cur[-1]))
    return out


def _rule_positions(mask: Any, axis: int, frac: float) -> List[int]:
    """Rule start coordinates along ``axis`` of a boolean ink ``mask``.

    A candidate row (axis=0) is one whose ink fraction across the mask's width
    is at least ``frac``; a candidate column (axis=1) is the same transposed.
    Returns the START of each surviving run, which is the coordinate the
    spans are built from.
    """
    import numpy as np

    if axis == 0:
        profile = mask.mean(axis=1)
    else:
        profile = mask.mean(axis=0)
    hits = np.where(profile > frac)[0]
    return [s for s, _ in _groups(hits.tolist())] if hits.size else []


def _cv(values: Sequence[float]) -> float:
    """Coefficient of variation; ``inf`` when there is nothing to measure.

    Plain stdlib: this runs over at most a few dozen spans, so pulling numpy
    into it would cost more than it saves.
    """
    if not values:
        return float("inf")
    mean = sum(values) / len(values)
    if mean <= 0:
        return float("inf")
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) / mean


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def _spans(positions: Sequence[int]) -> List[Tuple[int, int]]:
    """Rule starts -> half-open cell spans between consecutive rules."""
    return [(int(positions[i]), int(positions[i + 1])) for i in range(len(positions) - 1)]


def _mode(values: Sequence[int]) -> float:
    """Most frequent value (smallest wins a tie) - the table's printed pitch.

    The mode, not the mean or the median: the taxon rules vastly outnumber the
    header rules, so the mode lands on the row pitch even when the header band
    is enormous. On the source figure the spacings are 118, 23, 17, 18, 17 ...
    and 17 wins 15 times out of 24.
    """
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    best = min(counts, key=lambda v: (-counts[v], v))
    return float(best)


def _split_header(row_rules: Sequence[int], pitch: float) -> Tuple[Optional[Tuple[int, int]], List[int]]:
    """Peel the tall header band off the top of the ruled row list.

    The header is recognised by PITCH, not by content: the band that holds the
    rotated sample names is several times taller than a taxon row, so its rule
    spacing stands out against the modal row pitch. Measured on the source
    figure the spacings open 118, 23, 17, 18, 17 ... against a modal pitch of
    17, so two steps are consumed (118 px, then the 23 px sub-band carrying the
    two-line "Miscellaneous localities" / "Conodont level (see text)" caption)
    and the header ends at y=160 - the rule directly above the first taxon,
    "Archaeoacanthocircus rectangularis". That yields 22 taxon rows from the 23
    rules between y=160 and y=540.

    The test is TWO-SIDED: a leading gap is header while it deviates from the
    pitch by more than HEADER_PITCH_TOL. One-sided "taller than" cannot work
    here, because the caption sub-band is only 1.35x the pitch while a real
    taxon row is already 1.06x it - there is no one-sided threshold that
    separates them, and a symmetric window of +/-15% does (the two families
    sit at 6% and 35% deviation).
    """
    if len(row_rules) < 2:
        return None, list(row_rules)
    k = 0
    while k < len(row_rules) - 1:
        gap = row_rules[k + 1] - row_rules[k]
        if abs(gap - pitch) > HEADER_PITCH_TOL * pitch:
            k += 1
            continue
        break
    if k == 0:
        return None, list(row_rules)
    return (int(row_rules[0]), int(row_rules[k])), [int(v) for v in row_rules[k:]]


def _split_label(col_rules: Sequence[int]) -> Tuple[Optional[Tuple[int, int]], List[int]]:
    """Peel the label column off the left of the ruled column list.

    The label column is the only interval that is not a data cell, so it is
    found as the one interval several times wider than the median interval.
    Everything to its RIGHT is a sample column; anything to the left is
    dropped rather than guessed at, because on this figure class there is
    nothing else there.
    """
    spans = _spans(col_rules)
    if len(spans) < 2:
        return None, [int(v) for v in col_rules]
    widths = [b - a for a, b in spans]
    ordered = sorted(widths)
    median = ordered[len(ordered) // 2]
    widest = max(range(len(widths)), key=lambda i: widths[i])
    if median <= 0 or widths[widest] < LABEL_COL_RATIO * median:
        return None, [int(v) for v in col_rules]
    return spans[widest], [int(v) for v in col_rules[widest + 1:]]


def _geometry_from_mask(mask: Any) -> Optional[GridGeometry]:
    """Run the full rule analysis over an ink mask. None when implausible."""
    import numpy as np

    ys, xs = np.where(mask)
    if ys.size == 0:
        return None
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    if (y1 - y0) < 8 or (x1 - x0) < 8:
        return None

    # Pass 1 - rules over the whole ink bounding box.
    box = mask[y0:y1 + 1, x0:x1 + 1]
    row_rules = [y0 + v for v in _rule_positions(box, axis=0, frac=ROW_RULE_FRAC)]
    col_rules = [x0 + v for v in _rule_positions(box, axis=1, frac=COL_RULE_FRAC)]
    if len(row_rules) < MIN_ROW_RULES or len(col_rules) < MIN_COL_RULES:
        return None

    # Pass 2 - re-measure inside the table's own extent, so that a caption or
    # a page number sharing the bounding box cannot dilute a real rule below
    # threshold. Measured: this leaves both rule sets unchanged on the source
    # figure; it only makes the test sharper.
    tbl = mask[row_rules[0]:row_rules[-1] + 1, col_rules[0]:col_rules[-1] + 1]
    row_rules = [row_rules[0] + v for v in _rule_positions(tbl, axis=0, frac=ROW_RULE_FRAC)]
    col_rules = [col_rules[0] + v for v in _rule_positions(tbl, axis=1, frac=COL_RULE_FRAC)]
    if len(row_rules) < MIN_ROW_RULES or len(col_rules) < MIN_COL_RULES:
        return None

    # Row pitch: the MODE of the rule spacings. Taken over every spacing so
    # that a single huge header band cannot drag it, which is exactly what the
    # mode is for.
    all_gaps = [row_rules[i + 1] - row_rules[i] for i in range(len(row_rules) - 1)]
    pitch = _mode(all_gaps)

    header_span, taxon_rules = _split_header(row_rules, float(pitch))
    if len(taxon_rules) < 3:
        return None

    label_span, sample_rules = _split_label(col_rules)
    if len(sample_rules) < 2:
        return None

    row_spans = _spans(taxon_rules)
    col_spans = _spans(sample_rules)
    if not row_spans or not col_spans:
        return None

    row_gaps = [b - a for a, b in row_spans]
    col_gaps = [b - a for a, b in col_spans]
    cv_rows = _cv(row_gaps)
    cv_cols = _cv(col_gaps)
    # Uniformity is the honest confidence signal: a ruled table's pitch is a
    # printing constraint, so an irregular lattice means we are looking at
    # something that is not one.
    if not math.isfinite(cv_rows) or not math.isfinite(cv_cols):
        return None
    if cv_rows > MAX_SPACING_CV or cv_cols > MAX_SPACING_CV:
        return None
    confidence = max(0.0, min(1.0, 1.0 - 0.5 * (cv_rows + cv_cols)))

    return GridGeometry(
        n_cols=len(col_spans),
        n_rows=len(row_spans),
        col_spans=tuple(col_spans),
        row_spans=tuple(row_spans),
        header_span=header_span,
        label_span=label_span,
        confidence=confidence,
    )


def detect_grid(image_b64: str) -> Optional[GridGeometry]:
    """Measure the table lattice in ``image_b64``, or return ``None``.

    ``image_b64`` is base64 image bytes, either bare or wrapped in a
    ``data:image/...;base64,`` prefix; PNG and JPEG are both accepted.

    Returns ``None`` - and never raises - whenever the figure does not carry
    a plausible ruled lattice, or the image cannot be decoded at all. The
    caller is expected to fall back to its single-pass path in that case, so
    "not sure" has to mean ``None`` and not a low-confidence guess.
    """
    try:
        import numpy as np

        img = _decode_gray(image_b64)
        if img is None:
            return None
        arr = np.asarray(img, dtype=np.float32)
        if arr.ndim != 2 or arr.size == 0:
            return None
        mask = arr < INK_LEVEL
        # NOTE: "no ink" is refused once, in _geometry_from_mask, by its empty
        # bbox check. It used to be guarded here as well; the duplicate was
        # harmless but it made the refusal untestable (either copy alone
        # satisfies the contract, so neither can be shown to matter).
        return _geometry_from_mask(mask)
    except Exception:
        # detect_grid is a best-effort enhancement of an existing pipeline.
        # It must never be the thing that breaks an extraction.
        return None
