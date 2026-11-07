"""Deterministic plate segmentation -- the verifiable half of "auto-identification".

WHY THIS EXISTS
===============
README roadmap: "图版自动鉴定接入（scampi 开放权重 / RadiolarianClassifier
路线）". The classifier itself cannot be wired here: this repository ships no
model weights, and without a labelled radiolarian plate set there is no way to
measure whether a prediction is right. Shipping a `predict()` that returns a
label anyway would be the exact failure mode this project keeps auditing --
a plausible-looking number with nothing behind it.

What CAN be verified is the step every classifier needs first: cutting a plate
into one crop per specimen, deterministically, with pixel-exact bounds. That is
what this module does, and it is tested against synthetic plates whose true
crops are known by construction.

WHAT IT DOES AND DOES NOT DO
============================
Does:
  * threshold ink against paper using the same histogram criterion as
    :mod:`rca_core.axis_detect`,
  * split on fully-empty gutters (rows/columns with no ink), which is how
    essentially every published plate is laid out,
  * tighten each cell to its ink bounding box and drop specks,
  * return the crop as base64 PNG plus its box, ready for any classifier.

Does NOT:
  * classify anything. No label, no confidence, no species name is produced
    here -- deliberately, see above.
  * handle overlapping or diagonally-packed specimens. Those need connected
    components with region growing (union-find / scipy.ndimage.label), which is
    a different algorithm with a different failure mode. ``detect_layout``
    reports ``"irregular"`` when the gutter model does not fit, so a caller can
    refuse rather than silently return merged crops.

DEPENDENCIES: numpy + Pillow, imported lazily, same contract as
``rca_core/grid.py`` and ``rca_core/axis_detect.py``.
"""
from __future__ import annotations

import base64
import io
from typing import Any

__all__ = ["PlateSegmentError", "detect_layout", "segment_plate"]

#: A gutter must be at least this many consecutive ink-free lines to count.
#: One or two blank lines also occur INSIDE a specimen (between spines), so a
#: lower threshold split single fossils in half (measured on a real plate).
_MIN_GUTTER = 3

#: Cells whose ink covers less than this fraction of the plate are specks /
#: dust / scale-bar ticks, not specimens.
_MIN_CELL_INK_FRAC = 0.002


class PlateSegmentError(ValueError):
    """Raised when the payload is not a decodable image."""


def _gray(image_b64: Any):
    from .grid import _decode_gray

    img = _decode_gray(image_b64)
    if img is None:
        raise PlateSegmentError(
            "could not decode the image; expected base64 PNG/JPEG "
            "(a data: URL prefix is accepted)")
    return img


def _ink_mask(gray):
    import numpy as np

    from .axis_detect import _ink_mask as mask_for

    arr = np.asarray(gray, dtype=np.float32)
    if arr.ndim != 2 or arr.size == 0:
        raise PlateSegmentError(f"expected a 2-D greyscale image, got {arr.shape}")
    return mask_for(arr)


def _runs(flags) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def _bands(mask, axis: int) -> list[tuple[int, int]]:
    """Ink-free gutters as inclusive bands along ``axis``."""
    empty = ~mask.any(axis=axis)
    return [(s, e) for s, e in _runs(empty.tolist()) if e - s + 1 >= _MIN_GUTTER]


def _cells(bands: list[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """Complement of the gutters: the candidate cell spans."""
    out: list[tuple[int, int]] = []
    cursor = 0
    for start, end in bands:
        if start > cursor:
            out.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor < total:
        out.append((cursor, total - 1))
    return out


def detect_layout(image_b64: Any) -> dict[str, Any]:
    """Report whether the gutter model fits this plate.

    Returns ``{"layout": "grid"|"irregular"|"empty", "rows": n, "cols": n,
    "width": w, "height": h, "ink_fraction": f}``.

    ``"irregular"`` means one cell is more than 3x the size of the smallest,
    i.e. the plate is not laid out on gutters and :func:`segment_plate` would
    merge specimens. Max/min rather than max/median on purpose: a plate with
    two ordinary specimens and one very flat one has a median that hides the
    flat one, and merging two fossils into one "specimen" is the error that
    would poison a training set. The 3x bound is deliberately conservative --
    refusing a usable plate costs a manual pass, accepting a merged one costs
    a wrong label.
    """
    import numpy as np

    gray = _gray(image_b64)
    mask = _ink_mask(gray)
    h, w = mask.shape
    ink_fraction = float(mask.mean())
    if not mask.any():
        return {"layout": "empty", "rows": 0, "cols": 0,
                "width": int(w), "height": int(h), "ink_fraction": 0.0}
    row_cells = _cells(_bands(mask, axis=1), h)
    col_cells = _cells(_bands(mask, axis=0), w)
    heights = [b - a + 1 for a, b in row_cells]
    widths = [b - a + 1 for a, b in col_cells]
    regular = True
    for spans in (heights, widths):
        if len(spans) > 1:
            lo, hi = min(spans), max(spans)
            if lo <= 0 or hi / lo > 3.0:
                regular = False
    layout = "grid" if regular else "irregular"
    return {"layout": layout, "rows": len(row_cells), "cols": len(col_cells),
            "width": int(w), "height": int(h), "ink_fraction": ink_fraction}


def segment_plate(
    image_b64: Any,
    *,
    min_ink_fraction: float = _MIN_CELL_INK_FRAC,
    pad: int = 2,
    drop_irregular: bool = False,
) -> list[dict[str, Any]]:
    """Cut a plate into one crop per specimen.

    Args:
        image_b64: base64 PNG/JPEG, optional ``data:`` prefix.
        min_ink_fraction: cells below this ink fraction of the whole plate are
            dropped as dust / scale bars.
        pad: pixels of white margin kept around each tightened box.
        drop_irregular: when ``True``, a plate that :func:`detect_layout` calls
            ``"irregular"`` returns ``[]`` instead of possibly-merged crops.
            Default ``False`` because a merged pair is still useful evidence;
            set it when the crops feed a classifier.

    Returns:
        List of ``{"index", "box": {"x0","y0","x1","y1"}, "width", "height",
        "ink_fraction", "png_b64"}`` sorted by reading order (top-to-bottom,
        then left-to-right). ``[]`` for a blank plate.

    The crops are re-encoded as PNG so a caller never has to know the input
    format, and so a future classifier receives one uniform media type.
    """
    import numpy as np
    from PIL import Image

    info = detect_layout(image_b64)
    if info["layout"] == "empty":
        return []
    if drop_irregular and info["layout"] == "irregular":
        return []
    gray = _gray(image_b64)
    mask = _ink_mask(gray)
    h, w = mask.shape
    row_cells = _cells(_bands(mask, axis=1), h)
    col_cells = _cells(_bands(mask, axis=0), w)
    total_ink = max(int(mask.sum()), 1)
    out: list[dict[str, Any]] = []
    for r0, r1 in row_cells:
        for c0, c1 in col_cells:
            sub = mask[r0:r1 + 1, c0:c1 + 1]
            if not sub.any():
                continue
            rows = np.where(sub.any(axis=1))[0]
            cols = np.where(sub.any(axis=0))[0]
            y0 = r0 + int(rows[0])
            y1 = r0 + int(rows[-1])
            x0 = c0 + int(cols[0])
            x1 = c0 + int(cols[-1])
            ink = int(sub.sum())
            frac = ink / total_ink
            if frac < min_ink_fraction:
                continue
            cx0 = max(0, x0 - pad)
            cy0 = max(0, y0 - pad)
            cx1 = min(w - 1, x1 + pad)
            cy1 = min(h - 1, y1 + pad)
            crop = gray.crop((cx0, cy0, cx1 + 1, cy1 + 1))
            buf = io.BytesIO()
            crop.save(buf, format="PNG", optimize=True)
            out.append({
                "index": len(out),
                "box": {"x0": cx0, "y0": cy0, "x1": cx1, "y1": cy1},
                "width": cx1 - cx0 + 1,
                "height": cy1 - cy0 + 1,
                "ink_fraction": round(frac, 6),
                "png_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            })
    return out
