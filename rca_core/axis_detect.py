"""Deterministic axis-tick / curve detection -- the "geometry engine" PoC.

WHY THIS EXISTS
===============
README's roadmap item: "丰度/地球化学模式的几何引擎 PoC（确定性 CV 测刻度与
曲线像素，LLM 只读语义——FigDataX 路线）". The premise is that a surveyed
digitiser beats a vision model on NUMERIC read-off because it never *reads* a
number: it measures a pixel and converts it through a calibration. RCA already
has the converter (:mod:`rca_core.geometry`: anchors -> least-squares fit ->
``AxisCalibration``), but every anchor had to be clicked by a human.

This module supplies the missing half: it MEASURES the tick pixels
deterministically, so a calibration can be built without a click and without
asking the model to guess a coordinate.

WHAT IT DOES AND DOES NOT DO
============================
Does:
  * find the axis line (the darkest long run in the search band),
  * find tick marks as periodic protrusions NEXT TO that line,
  * trace a plot curve column-by-column (median ink row per column),
  * fit data = slope * pixel + intercept from detected ticks + known values.

Does NOT:
  * read tick LABELS. Mapping tick index -> data value still needs either an
    operator-supplied value list or an OCR pass. That is deliberate: OCR is a
    different failure mode (confusing 3/8, 1/l) and silently mixing it into
    the geometry would make a misread digit look like a calibration error.
  * handle rotated pages (see :mod:`rca_core.deskew`) or perspective.
  * replace :func:`rca_core.grid.detect_grid`. That one models a RULED TABLE
    (rows x columns with header/label splits); this one models ONE AXIS with
    periodic ticks. Sharing the projection trick is why the two live in
    separate modules: their acceptance tests have almost nothing in common.

DEPENDENCIES: numpy + Pillow, both imported lazily, matching
``rca_core/grid.py``. Importing this module never requires either.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = [
    "AxisDetectError",
    "detect_axis_ticks",
    "trace_plot_curve",
    "calibration_from_ticks",
]

#: Ink is any pixel darker than this after normalisation (0..255). A fixed
#: threshold would fail on grey scans, so ``_ink_mask`` derives its own from
#: the histogram; this is only the fallback when the histogram is degenerate.
_FALLBACK_INK_CUTOFF = 160

#: A tick is a protrusion; anything longer than this many pixels next to the
#: axis is a grid line / table rule / bracket, not a tick.
_DEFAULT_MAX_TICK_THICKNESS = 10

#: How far past the axis line we look for protrusions, in pixels.
_DEFAULT_BAND = 24

#: A peak must exceed baseline by this many robust sigmas (MAD-based).
_PEAK_SIGMAS = 4.0


class AxisDetectError(ValueError):
    """Raised when the input is not a decodable image at all.

    An image that simply HAS no ticks returns ``[]`` instead -- "I looked and
    found none" is a legitimate measurement, a corrupt base64 is not.
    """


def _gray_array(image_b64: Any, *, invert: bool = False):
    """base64 image -> 2-D float array (0..255), or raise AxisDetectError."""
    import numpy as np

    from .grid import _decode_gray

    img = _decode_gray(image_b64)
    if img is None:
        raise AxisDetectError(
            "could not decode the image; expected base64 PNG/JPEG "
            "(a data: URL prefix is accepted)")
    arr = np.asarray(img, dtype=np.float32)
    if arr.ndim != 2 or arr.size == 0:
        raise AxisDetectError(f"expected a 2-D greyscale image, got shape {arr.shape}")
    return 255.0 - arr if invert else arr


def _ink_mask(gray) -> Any:
    """Boolean ink mask, thresholded between the two histogram modes.

    Uses Otsu's criterion on the 256-bin histogram -- ~15 lines of numpy and
    no dependency. A fixed cutoff silently produced an all-ink mask on the
    grey-background scans in the fixture set (measured: 62 % of pixels below
    160), which turned every row into a "tick".
    """
    import numpy as np

    flat = gray.reshape(-1)
    hist, _ = np.histogram(flat, bins=256, range=(0.0, 255.0))
    total = float(flat.size)
    if total <= 0:
        return np.zeros(gray.shape, dtype=bool)
    prob = hist.astype(np.float64) / total
    omega = np.cumsum(prob)
    mu = np.cumsum(prob * np.arange(256, dtype=np.float64))
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = np.where(denom > 0, (mu_t * omega - mu) ** 2 / denom, 0.0)
    cutoff = int(np.argmax(sigma_b))
    if not 0 < cutoff < 255:
        cutoff = _FALLBACK_INK_CUTOFF
    mask = gray < float(cutoff)
    # Otsu splits the histogram into "two classes" but does not know which one
    # is ink. On a dark-background figure (white lines on black) the minority
    # class is the LIGHT pixels, so the mask would select the background and
    # every row would look like a protrusion. The majority class is paper.
    if float(mask.mean()) > 0.5:
        mask = ~mask
    return mask


def _runs(flags: Sequence[bool]) -> list[tuple[int, int]]:
    """Collapse a boolean sequence into inclusive (start, end) runs."""
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


def _axis_line(mask, *, axis: str, band: tuple[int, int] | None) -> int:
    """Pixel coordinate of the axis line itself (the longest dark run)."""
    import numpy as np

    if axis == "vertical":
        lo, hi = band or (0, mask.shape[1])
        column_ink = mask[:, lo:hi].sum(axis=0)
        return lo + int(np.argmax(column_ink))
    lo, hi = band or (0, mask.shape[0])
    row_ink = mask[lo:hi, :].sum(axis=1)
    return lo + int(np.argmax(row_ink))


def detect_axis_ticks(
    image_b64: Any,
    *,
    axis: str = "vertical",
    band: tuple[int, int] | None = None,
    max_tick_thickness: int = _DEFAULT_MAX_TICK_THICKNESS,
    min_ticks: int = 2,
) -> list[int]:
    """Pixel positions of the axis tick marks, ascending.

    Args:
        image_b64: base64 PNG/JPEG, optional ``data:`` prefix.
        axis: ``"vertical"`` for a y-axis (ticks protrude horizontally) or
            ``"horizontal"`` for an x-axis (ticks protrude vertically).
        band: search window as ``(lo, hi)`` along the perpendicular axis.
            Defaults to the whole image; pass a narrow band to stop a table
            rule elsewhere in the figure from winning ``_axis_line``.
        max_tick_thickness: protrusions thicker than this are ignored as
            rules / brackets rather than ticks.
        min_ticks: minimum number of marks for the result to be returned;
            fewer returns ``[]`` so a "found 1 thing" cannot masquerade as a
            calibration.

    Returns:
        Ascending pixel coordinates (one per tick, the centre of its run).
        ``[]`` when fewer than ``min_ticks`` marks were found.
    """
    import numpy as np

    if axis not in ("vertical", "horizontal"):
        raise AxisDetectError(f"axis must be 'vertical' or 'horizontal', got {axis!r}")
    if axis == "vertical" and band is None:
        band = (0, _DEFAULT_BAND)
    if axis == "horizontal" and band is None:
        band = (0, _DEFAULT_BAND)

    gray = _gray_array(image_b64)
    mask = _ink_mask(gray)
    if axis == "vertical":
        line = _axis_line(mask, axis=axis, band=(band[0], mask.shape[1]))
        lo = min(max(line + 1, band[0]), mask.shape[1] - 1)
        hi = min(line + 1 + _DEFAULT_BAND, mask.shape[1])
        if hi <= lo:
            return []
        # Per-row count of ink strictly to one side of the axis line.
        signal = mask[:, lo:hi].sum(axis=1).astype(np.float64)
    else:
        line = _axis_line(mask, axis=axis, band=(band[0], mask.shape[0]))
        lo = min(max(line + 1, band[0]), mask.shape[0] - 1)
        hi = min(line + 1 + _DEFAULT_BAND, mask.shape[0])
        if hi <= lo:
            return []
        signal = mask[lo:hi, :].sum(axis=0).astype(np.float64)

    baseline = float(np.median(signal))
    mad = float(np.median(np.abs(signal - baseline)))
    sigma = 1.4826 * mad
    if sigma <= 0.0:
        # A perfectly flat profile means the "band" contains only the axis
        # itself (or nothing): there is no protrusion to threshold against.
        threshold = baseline + 1.0
    else:
        threshold = baseline + _PEAK_SIGMAS * sigma
    peaks = _runs(signal > threshold)
    centres: list[int] = []
    for start, end in peaks:
        thickness = end - start + 1
        if thickness > max_tick_thickness:
            continue
        centres.append((start + end) // 2)
    centres.sort()
    if len(centres) < min_ticks:
        return []
    return centres


def trace_plot_curve(
    image_b64: Any,
    *,
    samples: int = 64,
    axis: str = "vertical",
    band: tuple[int, int] | None = None,
    ignore_border: int = 2,
) -> list[dict[str, float]]:
    """Trace the plotted curve: one median-ink point per sampled column.

    ``axis="vertical"`` means a y-vs-x plot: for each sampled x column the
    median ink row is returned as ``{"x": <column>, "y": <row>}``. Axis lines
    and the border are excluded (``band`` plus ``ignore_border``), otherwise
    the axis itself would win every column.

    Columns with no ink are skipped, so the caller can see coverage instead of
    receiving interpolated points that look measured. ``samples`` is a target:
    the actual count can be lower.
    """
    import numpy as np

    if axis not in ("vertical", "horizontal"):
        raise AxisDetectError(f"axis must be 'vertical' or 'horizontal', got {axis!r}")
    if samples < 2:
        raise AxisDetectError("samples must be >= 2")
    gray = _gray_array(image_b64)
    mask = _ink_mask(gray)
    if axis == "vertical":
        line = _axis_line(mask, axis="vertical", band=band)
        left = max(line + ignore_border + 1, 0)
        if left >= mask.shape[1] - 1:
            return []
        columns = np.linspace(left, mask.shape[1] - 1, num=min(samples, mask.shape[1] - left))
        out: list[dict[str, float]] = []
        for col in columns:
            c = int(round(float(col)))
            rows = np.where(mask[:, c])[0]
            if rows.size == 0:
                continue
            out.append({"x": float(c), "y": float(np.median(rows))})
        return out
    line = _axis_line(mask, axis="horizontal", band=band)
    top = max(line + ignore_border + 1, 0)
    if top >= mask.shape[0] - 1:
        return []
    rows = np.linspace(top, mask.shape[0] - 1, num=min(samples, mask.shape[0] - top))
    out = []
    for row in rows:
        r = int(round(float(row)))
        cols = np.where(mask[r, :])[0]
        if cols.size == 0:
            continue
        out.append({"x": float(np.median(cols)), "y": float(r)})
    return out


def calibration_from_ticks(
    pixels: Sequence[int],
    values: Sequence[float],
) -> dict[str, Any]:
    """Fit ``data = slope * pixel + intercept`` from detected ticks.

    ``pixels`` must be ascending and ``values`` must correspond to them. The
    fit itself is :func:`rca_core.geometry.fit_linear` -- the same code path
    used when a human clicks anchors, so a PoC calibration and a hand
    calibration are directly comparable.

    Returns ``{"slope", "intercept", "residuals", "max_abs_residual",
    "n"}``. Raises :class:`rca_core.geometry.CalibrationError` for fewer than
    two ticks or a degenerate set -- deliberately the same exception the
    manual path raises, so callers do not need two error branches.
    """
    from .geometry import Anchor, fit_linear

    px = [int(p) for p in pixels]
    vals = [float(v) for v in values]
    if len(px) != len(vals):
        raise ValueError(
            f"pixels and values must be the same length, got {len(px)} and {len(vals)}")
    anchors = [Anchor(pixel=p, data_value=v) for p, v in zip(px, vals)]
    slope, intercept, residuals = fit_linear(anchors)
    return {
        "slope": slope,
        "intercept": intercept,
        "residuals": list(residuals),
        "max_abs_residual": max((abs(r) for r in residuals), default=0.0),
        "n": len(anchors),
    }
