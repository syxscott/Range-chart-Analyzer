"""Whole-page skew ("deskew") for scanned / photographed chart plates.

BORROW-2026-09-20 (figure-extractor): that pipeline Hough-estimates the
plate's dominant line direction and rotates the picture before anything
else touches it.  Skew matters enormously here: a range line that drifts
2 degrees across the page is read as a run of wrong bed boundaries, and a
calibration picked off a tilted axis (see :mod:`rca_core.geometry`) is
wrong for every point except the anchors themselves.

Two estimators, one contract:

* :func:`_projection_angle` - pure Pillow / stdlib projection profile.
  The grayscale plate is downsampled, binarised against its own paper level,
  and the ink is voted into row / column histograms for a grid of candidate
  angles; the angle whose profile is peakiest is the one where the ruling
  lines sit on whole pixel rows.  Works everywhere Pillow works, no numpy.
  FIX-2026-09-22: the downsampled estimate is cross-checked against one or
  two NATIVE-resolution patches before it is trusted - a heavy downsample
  aliases dense ruling grids (line period below ~2 working pixels) and the
  comb could otherwise lock onto an aliased peak whose SIGN was inverted,
  making the "correction" double the skew.  Ink detection is likewise
  FIX-2026-09-22: polarity-robust (light ink on dark paper votes too) and
  scale-adaptive (a 1-pixel line thinned by the BOX downsample is no longer
  priced out by a fixed contrast floor).
* :func:`hough_skew_angle` - optional OpenCV fast path (same guarded-import
  contract as ``extractor._enhance_image_cv2``): Canny + HoughLinesP, keep
  the near-horizontal / near-vertical segments, length-weighted estimate.
  Used when cv2 happens to be installed; any hiccup falls back to the
  projection path.

Angle convention (shared by both): ``angle_deg`` is the angle handed to
Pillow's ``Image.rotate()`` - i.e. positive rotates the picture
counter-clockwise *as displayed* - and it is the rotation that REMOVES the
skew, so the returned image is ``original.rotate(angle_deg)``.  In pixel
coordinates (y grows downward) ruling lines that fall to the right have
``dy/dx = tan(angle) > 0`` and need ``angle > 0`` to be lifted level again.

FIX-2026-09-22: the correction rotates with ``expand=True``.  With
``expand=False`` a 5-degree turn whitened ~30k corner pixels of a
4000x3000 plate and left 37 of 4000 border pixels on the top row - and the
calibration anchors live exactly on that outer frame.  The canvas therefore
grows to hold the whole plate; callers re-read ``image.size`` after deskew
(``extractor.load_image_b64`` does), and the no-op path (angle ``0.0``)
still returns the ORIGINAL object, so "nothing was changed" stays cheap.

Pillow stays a lazy dependency: importing this module without it works
(everything is stdlib at import time), and only a call raises
:class:`DeskewError`.
"""

from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

__all__ = [
    "MIN_USEFUL_ANGLE",
    "DeskewError",
    "deskew_image",
    "estimate_skew_angle",
    "hough_skew_angle",
]

# Below this the rotation costs more resampling blur than the skew costs in
# reading error, so the plate is handed back untouched.
MIN_USEFUL_ANGLE = 0.15

# Fewer ink pixels than this and a projection profile is meaningless.
_MIN_INK_POINTS = 32
# A pixel must be at least this much away from the paper level (either side,
# FIX-2026-09-22) to vote at all.  Small on purpose: with contrast-weighted
# votes a faint vote is harmless, while dropping a faint line is what makes
# a small skew go undetected.  This is the NATIVE-resolution floor; the
# projection path scales it down by the effective downsample factor, because
# a BOX shrink averages a 1-pixel line away (see :func:`_min_contrast_for`).
_MIN_CONTRAST = 6
# FIX-2026-09-22: anti-aliasing verification of the downsampled estimate.
# The estimate is re-derived on native-resolution patch(es) of at most this
# many pixels per side; an estimate that only exists on the downsampled copy
# is treated as aliasing and rejected (return 0.0, a safe no-op).
_VERIFY_PATCH_SIDE = 512
_VERIFY_PATCH_TOL = 0.4  # degrees; agreement window between the two tiers
_VERIFY_PATCH_MIN_SIDE = 64  # below this a patch is too small to judge
# Vote-bin width of the projection profile, in working-image pixels.  Hard
# 1-pixel bins make the peakiness peak lock onto the bin grid instead of onto
# the ruling lines, which biases the angle by a few tenths of a degree;
# quarter-pixel bins with linear interpolation remove that pull.
_PROFILE_BIN_PX = 0.25
# Gaussian smoothing of the finished profile, in bins (0.5 working pixel).
# A shallowly tilted line is stored as a staircase over whole pixel rows, so
# the untouched profile at angle 0 is a handful of very sharp spikes and wins
# the raw sum-of-squares contest against the truly aligned angle - which is
# how a 1-degree plate gets reported as straight.  Smoothing scores the
# physical WIDTH of each peak instead of how many quantised rows it landed on.
_SMOOTH_SIGMA_BINS = 2.0


def _gauss_kernel(sigma: float = _SMOOTH_SIGMA_BINS) -> Tuple[float, ...]:
    half = max(1, int(math.ceil(3.0 * sigma)))
    vals = [math.exp(-(i * i) / (2.0 * sigma * sigma)) for i in range(-half, half + 1)]
    total = sum(vals)
    return tuple(v / total for v in vals)


_SMOOTH_KERNEL = _gauss_kernel()


class DeskewError(RuntimeError):
    """Raised when deskewing is impossible (Pillow missing, bad arguments)."""


def _require_pillow() -> Any:
    """Pillow's ``Image`` module, or a clear error naming the optional dep."""
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on the environment
        raise DeskewError(
            "deskew needs Pillow, which is an optional dependency of Range "
            "Chart Analyzer (pip install Pillow); the rest of rca_core works "
            "without it"
        ) from exc
    return Image


def _check_image(pil_img: Any) -> None:
    if not hasattr(pil_img, "size") or not hasattr(pil_img, "convert"):
        raise DeskewError(
            "expected a PIL.Image.Image, got "
            f"{type(pil_img).__name__} (Pillow is optional - install it to "
            "use the deskew helpers)"
        )


def _check_params(
    max_angle: Any, downsample: Any, min_angle: Any, axis: Any, method: Any
) -> Tuple[float, int, float, str, str]:
    try:
        max_angle = float(max_angle)
        downsample = int(downsample)
        min_angle = float(min_angle)
    except (TypeError, ValueError) as exc:
        raise DeskewError(f"bad deskew parameters: {exc}") from None
    if not math.isfinite(max_angle) or not 0.0 < max_angle <= 45.0:
        raise DeskewError(f"max_angle must be in (0, 45], got {max_angle!r}")
    if downsample < 1:
        raise DeskewError(f"downsample must be >= 1, got {downsample!r}")
    if not 0.0 <= min_angle < max_angle:
        raise DeskewError(
            f"min_angle must satisfy 0 <= min_angle < max_angle ({max_angle})"
        )
    axis = str(axis or "auto").strip().lower()
    if axis not in ("auto", "rows", "columns"):
        raise DeskewError(f"axis must be 'auto', 'rows' or 'columns', got {axis!r}")
    method = str(method or "auto").strip().lower()
    if method not in ("auto", "projection", "hough"):
        raise DeskewError(
            f"method must be 'auto', 'projection' or 'hough', got {method!r}"
        )
    return max_angle, downsample, min_angle, axis, method


# ---------------------------------------------------------------------------
# Pillow / stdlib projection path
# ---------------------------------------------------------------------------

def _working_gray(
    pil_img: Any, downsample: int, max_side: int, Image: Any
) -> Tuple[Any, int]:
    """Grayscale, downsampled (BOX filter) and capped at ``max_side`` pixels.

    Returns ``(gray, factor)``; FIX-2026-09-22 the effective factor travels
    with the image so the ink detector can undo the contrast the BOX average
    takes away from hairline rulings.
    """
    gray = pil_img.convert("L")
    w, h = gray.size
    if w < 8 or h < 8:
        raise DeskewError(f"image too small to deskew: {w}x{h}")
    factor = max(1, int(downsample))
    if max_side and max_side > 8:
        # Keep the working plate bounded: the projection cost is linear in
        # ink points * candidate angles, so the plate size is the knob that
        # decides wall-clock time.
        while factor > 1 and max(w, h) // factor > max_side:
            factor += 1
        if max(w, h) // factor < 8:  # cannot downsample that far
            factor = max(1, min(downsample, max(1, max(w, h) // 8)))
    nw, nh = max(8, int(round(w / factor))), max(8, int(round(h / factor)))
    if (nw, nh) != (w, h):
        resample = getattr(Image, "BOX", None) or getattr(Image, "BILINEAR", 2)
        gray = gray.resize((nw, nh), resample)  # type: ignore[arg-type]
    return gray, factor


def _paper_level(gray: Any) -> int:
    """The plate's background (paper) grey level - the reference for "ink".

    Deliberately not a global Otsu split: a BOX-downsampled scan of a line
    drawing has paper at 255 and a ruling line at ~201 with antialiasing in
    between, and a two-class cut then lands *inside* the line, dropping the
    faint half of it - which is exactly the small-skew case that still needs
    correcting.  Everything that differs from the paper by enough votes
    instead, weighted by how far (see :func:`_ink_votes`), so a borderline
    pixel contributes a small vote rather than silently vanishing.

    FIX-2026-09-22: the paper is simply the global mode, for dark AND light
    plates.  The old "prefer the bright mode" rule called a dark-ground /
    light-ink plate's INK its paper, which inverted the cut and left such
    plates with zero votes.
    """
    hist = list(gray.histogram())[:256]
    if sum(hist) <= 0:
        return 255
    return max(range(256), key=lambda v: hist[v])


def _min_contrast_for(factor: int) -> int:
    """Contrast floor at a given effective downsample factor.

    A BOX shrink of ``factor`` averages a 1-pixel line's contrast down to
    roughly ``delta / factor``, so a fixed floor of 6 silently deletes every
    hairline from a factor-4+ working plate (a paper-250 / line-235 plate
    voted 0 pixels).  Scaling the floor with the factor keeps faint ink in
    play at the price of a few noise votes - which is what the contrast
    *weights* are for: they keep noise contributions small.
    """
    return max(1, int(math.ceil(_MIN_CONTRAST / max(1, int(factor)))))


def _ink_votes(
    gray: Any,
    threshold: Optional[int] = None,
    *,
    factor: int = 1,
) -> Tuple[List[Tuple[int, int, int]], int]:
    """``((x, y, weight), ...)`` of ink pixels plus the cut that was used.

    ``weight`` is the pixel's contrast against the paper, so faint
    antialiasing still votes.  FIX-2026-09-22: pixels vote on BOTH sides of
    the paper level (light ink on a dark plate is ink), and the default
    floor adapts to ``factor`` (see :func:`_min_contrast_for`).  An explicit
    ``threshold`` keeps the historic meaning - "grey values below this are
    ink" - for callers that want a hard dark cut.  Plain byte scanning with
    no numpy: the working plate is bounded by ``max_side``, so this one pass
    stays cheap.
    """
    w, h = gray.size
    paper = _paper_level(gray)
    if threshold is not None:
        cut = max(0, min(256, int(threshold)))
        one_sided = True
        floor = 1
    else:
        floor = _min_contrast_for(factor)
        cut = paper - floor
        one_sided = False
    data = gray.tobytes()
    votes: List[Tuple[int, int, int]] = []
    append = votes.append
    row_start = 0
    for y in range(h):
        row = data[row_start:row_start + w]
        row_start += w
        if one_sided:
            for x, v in enumerate(row):
                if v < cut:
                    append((x, y, max(1, paper - v)))
        else:
            for x, v in enumerate(row):
                diff = v - paper
                if diff >= floor or diff <= -floor:
                    append((x, y, diff if diff > 0 else -diff))
    return votes, cut


def _stride_sample(points: Sequence[Any], cap: int) -> Sequence[Any]:
    """Deterministic subsample so the estimator's cost has a hard ceiling."""
    n = len(points)
    if cap <= 0 or n <= cap:
        return points
    step = n / float(cap)
    return [points[int(i * step)] for i in range(cap)]


def _make_plan(
    w: int, h: int, max_angle: float, mode: str
) -> Tuple[str, float, int, float, float]:
    """Fixed bin layout for one projection direction.

    The layout is derived from the plate corners at +/- ``max_angle``, so it
    holds for EVERY angle the search tries.  Keeping the bin count constant is
    what makes the score comparable across angles: a per-angle bound would let
    the ``- 1 / bins`` baseline term drift with |angle| and pull the optimum
    toward larger skews.

    Returns ``(mode, shift, bins, scale, baseline)`` where ``baseline`` is the
    flat-profile term subtracted from the raw peakiness.
    """
    rad = math.radians(max_angle)
    s, c = math.sin(rad), math.cos(rad)
    vals: List[float] = []
    for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        for sign in (-1.0, 1.0):
            vals.append(y * c - x * s * sign if mode == "rows" else x * c + y * s * sign)
    lo, hi = min(vals), max(vals)
    scale = 1.0 / _PROFILE_BIN_PX
    bins = int((hi - lo) * scale) + 4
    shift = -lo + 2.0 / scale
    return mode, shift, bins, scale, 1.0 / bins


def _profile_score(
    points: Sequence[Tuple[int, int, int]], angle: float, plan: Tuple[Any, ...]
) -> float:
    """Peakiness of the ink profile projected at ``angle`` (higher == straighter).

    Contrast-weighted votes land in ``_PROFILE_BIN_PX``-wide bins, split
    linearly between the two nearest ones, so the profile is a smooth function
    of the angle and its maximum sits on the ruling lines rather than on the
    bin grid.  The value is a dimensionless chi-square-style excess over the
    flat-profile baseline, which lets a row profile and a (differently long)
    column profile compete for ``axis="auto"``.
    """
    mode, shift, bins, scale, baseline = plan
    rad = math.radians(angle)
    sin_a, cos_a = math.sin(rad), math.cos(rad)
    hist = [0.0] * bins
    total_w = 0.0
    if mode == "rows":
        for x, y, wgt in points:
            total_w += wgt
            pos = (y * cos_a - x * sin_a + shift) * scale
            k = int(pos)
            if 0 <= k + 1 < bins:
                frac = pos - k
                hist[k] += wgt * (1.0 - frac)
                hist[k + 1] += wgt * frac
    else:  # columns
        for x, y, wgt in points:
            total_w += wgt
            pos = (x * cos_a + y * sin_a + shift) * scale
            k = int(pos)
            if 0 <= k + 1 < bins:
                frac = pos - k
                hist[k] += wgt * (1.0 - frac)
                hist[k + 1] += wgt * frac
    if total_w <= 0.0:
        return 0.0
    kernel = _SMOOTH_KERNEL
    half = len(kernel) // 2
    padded = [0.0] * half + hist + [0.0] * half
    total = 0.0
    for i in range(len(hist)):
        smoothed = 0.0
        window = padded[i:i + len(kernel)]
        for j, k in enumerate(kernel):
            smoothed += window[j] * k
        total += smoothed * smoothed
    return total / (total_w * total_w) - baseline


def _search_angle(
    fine_points: Sequence[Any],
    coarse_points: Sequence[Any],
    w: int,
    h: int,
    max_angle: float,
    axis: str,
) -> float:
    """Coarse-to-fine grid search for the straightening angle (degrees).

    One coarse sweep decides the winner between the row and the column profile
    (only when ``axis="auto"``); two refinements then walk the peak down to
    ~0.005 degrees on the full ink set.
    """
    modes: Tuple[str, ...] = ("rows", "columns") if axis == "auto" else (axis,)
    plans = {mode: _make_plan(w, h, max_angle, mode) for mode in modes}
    step = max(0.25, min(1.0, max_angle / 10.0))
    count = int(2.0 * max_angle / step)
    grid = [-max_angle + i * step for i in range(count + 1)]

    best_score = -float("inf")
    best_angle = 0.0
    best_mode = modes[0]
    for mode in modes:
        plan = plans[mode]
        for a in grid:
            score = _profile_score(coarse_points, a, plan)
            if score > best_score:
                best_score, best_angle, best_mode = score, a, mode

    plan = plans[best_mode]
    window = step
    for _ in range(2):
        sub = window / 10.0
        local_angle = best_angle
        for i in range(-5, 6):
            a = best_angle + i * sub
            if a < -max_angle or a > max_angle:
                continue
            score = _profile_score(fine_points, a, plan)
            if score > best_score:
                best_score = score
                local_angle = a
        best_angle = local_angle
        window = sub
    return best_angle


def _angle_from_gray(
    gray: Any,
    max_angle: float,
    axis: str,
    threshold: Optional[int],
    max_ink_points: int,
    factor: int = 1,
) -> Optional[float]:
    """Projection-profile angle of one prepared L image; ``None`` = no signal."""
    w, h = gray.size
    votes, _cut = _ink_votes(gray, threshold, factor=factor)
    if len(votes) < _MIN_INK_POINTS:
        return None  # blank / unreadably faint plate: do not invent a rotation
    cap = max(_MIN_INK_POINTS, int(max_ink_points or 0))
    fine = _stride_sample(votes, cap)
    coarse = _stride_sample(votes, max(2000, cap // 5))
    return _search_angle(fine, coarse, w, h, max_angle, axis)


def _alias_checked_angle(
    pil_img: Any,
    primary: float,
    *,
    max_angle: float,
    axis: str,
    threshold: Optional[int],
    max_ink_points: int,
) -> float:
    """Re-estimate the downsampled ``primary`` angle on native-resolution patches.

    FIX-2026-09-22 (audit bug 1): a factor-4..9 BOX downsample of a dense
    ruling grid (line period below ~2 working pixels) aliases the row
    profile, and the comb can lock onto a false peak whose sign is
    INVERTED - measured: a 2400x1800 step-8 plate skewed -3.0 degrees was
    "corrected" by -1.075, worsening it to 4.08.  The same grid at native
    resolution cannot alias (no rendered plate has a line period below two
    full pixels), so the native patches act as the referee:

    * primary agrees with the centre patch  -> trust primary (global view);
    * they disagree but two native patches agree -> trust the patches;
    * a single patch that sees >= ~70 % of the plate is a full native rerun
      and simply wins over its aliased downsampled twin;
    * anything else -> 0.0, a safe no-op.  A wrong-signed angle is far worse
      than no correction at all.
    """
    limg = pil_img.convert("L")
    w, h = limg.size
    side = min(_VERIFY_PATCH_SIDE, w, h)
    if side < _VERIFY_PATCH_MIN_SIDE:
        return primary
    covers = (side * side) / float(w * h)
    boxes: List[Tuple[int, int]] = [((w - side) // 2, (h - side) // 2), (0, 0)]
    patch_angles: List[float] = []
    seen = set()
    for box in boxes:
        if box in seen:
            continue
        seen.add(box)
        patch = limg.crop((box[0], box[1], box[0] + side, box[1] + side))
        found = _angle_from_gray(patch, max_angle, axis, threshold, max_ink_points, 1)
        if found is not None and math.isfinite(found):
            patch_angles.append(found)
    if not patch_angles:
        return primary  # patches are blank margins: nothing to re-derive from
    first = patch_angles[0]
    if abs(first - primary) <= _VERIFY_PATCH_TOL:
        return primary
    if len(patch_angles) >= 2:
        if abs(patch_angles[0] - patch_angles[1]) <= _VERIFY_PATCH_TOL:
            return first
    elif covers >= 0.7:
        return first  # the patch IS (nearly) the whole plate at native size
    return 0.0


def _projection_angle(
    pil_img: Any,
    *,
    max_angle: float,
    downsample: int,
    axis: str,
    threshold: Optional[int],
    max_side: int,
    max_ink_points: int,
) -> float:
    """Skew estimate from the pure-Pillow projection profile."""
    Image = _require_pillow()
    gray, factor = _working_gray(pil_img, downsample, max_side, Image)
    primary = _angle_from_gray(
        gray, max_angle, axis, threshold, max_ink_points, factor
    )
    if primary is None:
        return 0.0
    if factor <= 1:
        return primary  # already native resolution: nothing can have aliased
    return _alias_checked_angle(
        pil_img,
        primary,
        max_angle=max_angle,
        axis=axis,
        threshold=threshold,
        max_ink_points=max_ink_points,
    )


# ---------------------------------------------------------------------------
# Optional OpenCV / Hough fast path
# ---------------------------------------------------------------------------

def hough_skew_angle(
    pil_img: Any,
    max_angle: float = 5.0,
    *,
    min_line_fraction: float = 0.25,
    max_side: int = 1024,
) -> Optional[float]:
    """Length-weighted skew from Hough line segments.

    Returns ``None`` (never raises) when cv2 / numpy are missing or the plate
    has no usable long ruling lines, so callers can fall back to the
    projection path.  Segments within ``max_angle`` of horizontal *or*
    vertical contribute, which is what makes single-column and grid plates
    both work.
    """
    try:  # guarded exactly like extractor._enhance_image_cv2
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return None
    try:
        _check_image(pil_img)
        max_angle = float(max_angle)
        gray = np.asarray(pil_img.convert("L"), dtype=np.uint8)
        h, w = gray.shape[:2]
        if max_side and max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            gray = cv2.resize(
                gray,
                (max(8, int(w * scale)), max(8, int(h * scale))),
                interpolation=cv2.INTER_AREA,
            )
            h, w = gray.shape[:2]
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(blur, 60, 160)
        min_len = max(16, int(min_line_fraction * max(h, w)))
        segments = cv2.HoughLinesP(
            edges, 1, math.pi / 360.0, 60, minLineLength=min_len, maxLineGap=6
        )
        if segments is None:
            return None
        samples: List[Tuple[float, float]] = []
        for seg in np.asarray(segments).reshape(-1, 4):
            x1, y1, x2, y2 = (float(seg[0]), float(seg[1]), float(seg[2]), float(seg[3]))
            dx, dy = x2 - x1, y2 - y1
            length = math.hypot(dx, dy)
            if length < 1.0:
                continue
            # dev: segment direction folded to (-45, 135]; horizontal lines
            # carry the skew directly, vertical ones as (dev - 90).
            dev = math.degrees(math.atan2(dy, dx)) % 180.0
            if dev >= 135.0:
                dev -= 180.0
            if dev < -45.0:
                dev += 180.0
            angle = dev if dev < 45.0 else dev - 90.0
            if abs(angle) <= max_angle:
                samples.append((angle, length))
        if len(samples) < 3:
            return None
        samples.sort(key=lambda s: s[0])
        half = sum(s[1] for s in samples) / 2.0
        running = 0.0
        median = samples[-1][0]
        for angle, weight in samples:
            running += weight
            if running >= half:
                median = angle
                break
        near = [(a, wgt) for a, wgt in samples if abs(a - median) <= 0.75]
        if not near:
            return None
        total_w = sum(wgt for _, wgt in near)
        if total_w <= 0:
            return None
        return sum(a * wgt for a, wgt in near) / total_w
    except Exception:
        # A fast path must never be a failure path.
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def estimate_skew_angle(
    pil_img: Any,
    max_angle: float = 5.0,
    downsample: int = 4,
    *,
    axis: str = "auto",
    method: str = "auto",
    threshold: Optional[int] = None,
    max_side: int = 480,
    max_ink_points: int = 40000,
) -> float:
    """Rotation angle (``Image.rotate`` convention) that straightens ``pil_img``.

    ``method="auto"`` prefers the optional cv2/Hough estimate and silently
    degrades to the Pillow projection profile; ``"projection"`` / ``"hough"``
    force one estimator (``"hough"`` yields ``0.0`` when cv2 is absent).

    The raw estimate is returned as-is - the ``min_angle`` "not worth
    rotating" filter lives in :func:`deskew_image`, so a caller that wants to
    report a sub-threshold skew to the operator still can.
    """
    max_angle, downsample, _min_angle, axis, method = _check_params(
        max_angle, downsample, MIN_USEFUL_ANGLE, axis, method
    )
    _check_image(pil_img)
    if method in ("auto", "hough"):
        found = hough_skew_angle(pil_img, max_angle)
        if found is not None and math.isfinite(found):
            return max(-max_angle, min(max_angle, float(found)))
        if method == "hough":
            return 0.0
    return _projection_angle(
        pil_img,
        max_angle=max_angle,
        downsample=downsample,
        axis=axis,
        threshold=threshold,
        max_side=max_side,
        max_ink_points=max_ink_points,
    )


def _fill_for(mode: str) -> Any:
    """Background colour for the wedges a rotation exposes."""
    return {
        "1": 255,
        "L": 255,
        "I": 255,
        "F": 255.0,
        "RGB": (255, 255, 255),
        "RGBA": (255, 255, 255, 255),
        "LA": (255, 255),
    }.get(mode, 255)


def _rotate_copy(pil_img: Any, angle: float, fill: Any) -> Any:
    Image = _require_pillow()
    src = pil_img
    if src.mode == "P":  # palettes cannot be resampled
        src = src.convert("RGBA" if "transparency" in src.info else "RGB")
    elif src.mode == "1":
        src = src.convert("L")
    resample = getattr(Image, "BILINEAR", None) or 2
    # FIX-2026-09-22 (audit bug 2): expand=True.  With expand=False the
    # corners were silently whitened away - a 5-degree turn of a 4000x3000
    # framed plate kept 37 of its 4000 top-border black pixels and erased
    # ~30k corner pixels - and the calibration anchors / axis ticks live on
    # exactly that outer frame, so the loss was undetectable downstream.
    # The canvas now grows to hold the whole plate; callers re-read
    # ``image.size`` after deskewing (extractor does), and the no-op path
    # still returns the original object untouched, so the identity contract
    # ("angle 0.0 => same object, nothing resampled") is kept.
    return src.rotate(
        angle, resample=resample, expand=True, fillcolor=fill  # type: ignore[arg-type]
    )


def deskew_image(
    pil_img: Any,
    max_angle: float = 5.0,
    downsample: int = 4,
    *,
    axis: str = "auto",
    method: str = "auto",
    threshold: Optional[int] = None,
    min_angle: float = MIN_USEFUL_ANGLE,
    max_side: int = 480,
    max_ink_points: int = 40000,
    fill: Any = None,
) -> Tuple[Any, float]:
    """Straighten a scanned / photographed plate.

    Args:
        pil_img: a ``PIL.Image.Image`` (any mode; ``P`` / ``1`` are converted).
        max_angle: search window, ``+/-`` degrees (default 5, enough for a
            photographed page; raise it for a badly shot plate at a cost).
        downsample: working-image shrink factor; the estimator runs on the
            small copy and the rotation is applied to the full-resolution one.
        axis: which ruling lines vote - ``"rows"``, ``"columns"`` or
            ``"auto"`` (default; the stronger profile wins).
        method: ``"auto"`` (Hough when cv2 exists, else projection),
            ``"projection"`` (pure Pillow) or ``"hough"``.
        threshold: lowest grey value that still votes in the projection path
            (default: the plate's own paper level minus ``_MIN_CONTRAST``;
            votes are weighted by contrast, see :func:`_ink_votes`).
        min_angle: below this the plate is returned untouched.
        max_side: cap on the working image's long edge (runtime ceiling).
        max_ink_points: cap on ink votes per angle (runtime ceiling).
        fill: colour of the wedge a rotation exposes (default: white).

    Returns:
        ``(image, angle_deg)``.  ``angle_deg`` is the angle already applied,
        i.e. the returned image equals ``pil_img.rotate(angle_deg,
        expand=True)`` (FIX-2026-09-22: the canvas grows so no border /
        calibration-anchor pixels are cropped away); it is ``0.0`` - and the
        ORIGINAL object (not a copy) is returned - when the detected skew is
        smaller than ``min_angle``.  Callers must re-read ``image.size``
        after deskewing.
    """
    max_angle, downsample, min_angle, axis, method = _check_params(
        max_angle, downsample, min_angle, axis, method
    )
    _check_image(pil_img)
    angle = estimate_skew_angle(
        pil_img,
        max_angle=max_angle,
        downsample=downsample,
        axis=axis,
        method=method,
        threshold=threshold,
        max_side=max_side,
        max_ink_points=max_ink_points,
    )
    if abs(angle) < min_angle:
        return pil_img, 0.0
    return _rotate_copy(pil_img, angle, _fill_for(pil_img.mode) if fill is None else fill), angle
