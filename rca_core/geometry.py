"""Pixel <-> data-value axis calibration for digitised charts.

BORROW-2026-09-20 (graph2table): the surveyed digitiser beats a vision
model on numeric read-off precisely because it never *reads* a number -
it measures a pixel position and converts it through a calibration the
operator anchored on the axis ticks.  This module is that converter for
RCA: the operator clicks two (or more) tick marks on the image, we fit a
straight line per axis, and every later coordinate question - "where does
this range line start?", "what bed is this marker in?" - becomes arithmetic
instead of another LLM guess.

Design notes
------------
* One independent 1-D fit per axis; no rotation / perspective term.  Page
  skew is handled upstream by :mod:`rca_core.deskew` so the two error
  sources stay separable (and each testable on its own).
* Exactly two anchors -> the exact line through them.  More than two ->
  least squares with the per-anchor residual reported back, because three
  collinear-looking ticks are the usual symptom of one mis-clicked anchor.
* Residuals live in DATA units (``observed - fitted``); pixels are treated
  as the trustworthy side of the pair, which matches how the anchors are
  picked (a click is precise, a tick label can be misread).
* Stratigraphic axes run backwards: the top of the image is the *youngest*
  sample, so pixel ``y`` grows while ``age``/``level`` shrinks.  That is
  just a negative slope, but the sign is also recorded as ``direction``
  (``"direct"`` / ``"reversed"``) so UIs and exporters never have to infer
  it, and a declared direction that contradicts the fitted sign is an
  :attr:`AxisCalibration.issue` rather than a silent surprise.
* Pure stdlib, and deliberately side-effect free: a :class:`Calibration`
  round-trips through :meth:`Calibration.to_json` into the result dict /
  SQLite history, provenance included, so a later re-derivation of values
  does not need the operator to re-pick anchors.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "SCHEMA_VERSION",
    "RESIDUAL_FRACTION",
    "CalibrationError",
    "Anchor",
    "AxisCalibration",
    "Calibration",
    "fit_linear",
]

# Bumped only on breaking schema changes; :func:`Calibration.from_json`
# accepts anything ``<= SCHEMA_VERSION``.
SCHEMA_VERSION = 1

# Auto tolerance: a fit is trusted while the worst anchor residual stays
# under 2 % of the axis' data span (the tick-spacing scale an operator
# mis-click would show up on).
RESIDUAL_FRACTION = 0.02

# FIX-2026-09-22 (audit bug 5-3): a stored ``residual_tolerance`` wider than
# half the axis' data span is not a judgement, it launders any misfit into
# "trusted" (measured: tolerance 1e9 -> is_trusted True with residual 33).
# Such payloads fall back to the auto fraction instead of being taken verbatim.
_TOLERANCE_SPAN_FRACTION = 0.5

# FIX-2026-09-22 (audit bug 5-4): leave-one-out consensus for
# :attr:`AxisCalibration.outlier_indices` is exhaustive over the small
# deletion sets, which is cheap up to roughly a dozen anchors; beyond that
# the (cheap but guilt-by-proximity) plain-residual rule takes over.
_MAX_CONSENSUS_ANCHORS = 12

_DIRECTIONS = ("direct", "reversed")
_EPS = 1e-12


class CalibrationError(ValueError):
    """Raised for impossible / malformed calibration input."""


def _as_float(value: Any, what: str) -> float:
    """``float(value)`` with a message that names the offending field."""
    if isinstance(value, bool) or value is None:
        raise CalibrationError(f"{what} must be a number, got {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise CalibrationError(f"{what} must be a number, got {value!r}") from None
    if not math.isfinite(out):
        raise CalibrationError(f"{what} must be finite, got {value!r}")
    return out


@dataclass(frozen=True)
class Anchor:
    """One operator-picked tick: image position and the value it stands for."""

    pixel: float
    data_value: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "pixel", _as_float(self.pixel, "anchor.pixel"))
        object.__setattr__(
            self, "data_value", _as_float(self.data_value, "anchor.data_value")
        )

    def as_tuple(self) -> Tuple[float, float]:
        return (self.pixel, self.data_value)

    def to_json(self) -> Dict[str, float]:
        return {"pixel": self.pixel, "data_value": self.data_value}

    @classmethod
    def from_json(cls, obj: Any) -> "Anchor":
        """Accept ``{"pixel": .., "data_value": ..}`` or a ``[pixel, value]`` pair.

        The pair form exists for hand-written / legacy payloads; ``value``
        is accepted as an alias of ``data_value`` for the same reason.
        """
        if isinstance(obj, Anchor):
            return obj
        if isinstance(obj, dict):
            pixel = obj.get("pixel", obj.get("px", obj.get("x")))
            value = obj.get("data_value", obj.get("value", obj.get("data")))
            if pixel is None or value is None:
                raise CalibrationError(
                    "anchor needs 'pixel' and 'data_value' keys, got "
                    f"{sorted(obj)!r}"
                )
            return cls(pixel, value)
        if isinstance(obj, (list, tuple)) and len(obj) == 2:
            return cls(obj[0], obj[1])
        raise CalibrationError(f"cannot read an anchor from {obj!r}")


def _coerce_anchors(anchors: Optional[Iterable[Any]]) -> Tuple[Anchor, ...]:
    if anchors is None:
        return ()
    if isinstance(anchors, (str, bytes, dict)):
        raise CalibrationError("anchors must be a sequence, not a bare string/dict")
    return tuple(Anchor.from_json(a) for a in anchors)


def fit_linear(
    pairs: Sequence[Any],
) -> Tuple[float, float, Tuple[float, ...]]:
    """Fit ``data = slope * pixel + intercept``; return (slope, intercept, residuals).

    Two points give the exact solution; three or more are least-squares
    fitted in data space and every residual (``observed - fitted``, in DATA
    units) is returned so the caller can decide what to do with a bad anchor.

    Raises:
        CalibrationError: fewer than two anchors, or a degenerate set (all
            anchors on one pixel / all anchors naming one value) - neither
            pins down a scale.
    """
    anchors = _coerce_anchors(pairs)
    if len(anchors) < 2:
        raise CalibrationError(
            f"a linear axis needs >= 2 anchors, got {len(anchors)}"
        )
    pixels = [a.pixel for a in anchors]
    values = [a.data_value for a in anchors]
    pixel_span = max(pixels) - min(pixels)
    data_span = max(values) - min(values)
    if pixel_span <= _EPS:
        raise CalibrationError(
            "all anchors sit on the same pixel, so the scale is undefined"
        )
    if data_span <= _EPS:
        raise CalibrationError(
            "all anchors carry the same data value, so the scale is undefined"
        )

    n = len(anchors)
    if n == 2:
        (p1, d1), (p2, d2) = anchors[0].as_tuple(), anchors[1].as_tuple()
        slope = (d2 - d1) / (p2 - p1)
        intercept = d1 - slope * p1
    else:
        mean_p = sum(pixels) / n
        mean_d = sum(values) / n
        sxx = sum((p - mean_p) ** 2 for p in pixels)
        sxy = sum((a.pixel - mean_p) * (a.data_value - mean_d) for a in anchors)
        # FIX-2026-09-22 (audit bug 5-6): the two- and three-point paths now
        # share ONE degeneracy test, the pixel-span check above.  This guard
        # used to be ``sxx <= _EPS`` - a span-squared quantity compared to a
        # span threshold - so a (tiny but legal) 1e-7-pixel triple that the
        # two-point path accepted was rejected here as "zero pixel variance".
        if sxx <= _EPS * _EPS:  # pragma: no cover - guarded by the span check
            raise CalibrationError("degenerate anchor set (zero pixel variance)")
        slope = sxy / sxx
        intercept = mean_d - slope * mean_p

    residuals = tuple(a.data_value - (slope * a.pixel + intercept) for a in anchors)
    return slope, intercept, residuals


def _direction_for(slope: float) -> str:
    """``"reversed"`` when data shrinks as the pixel coordinate grows."""
    return "reversed" if slope < 0 else "direct"


@dataclass(frozen=True)
class AxisCalibration:
    """A fitted single axis (name / unit / direction + the line itself)."""

    axis: str
    name: str
    unit: str
    direction: str
    slope: float
    intercept: float
    anchors: Tuple[Anchor, ...] = ()
    residuals: Tuple[float, ...] = ()
    residual_tolerance: float = 0.0

    # -- construction ----------------------------------------------------
    def __post_init__(self) -> None:
        # FIX-2026-09-22 (audit bug 5-7): one invariant gate for every route
        # into the dataclass (``fit``, ``from_json``, direct construction).
        # Non-finite coefficients / residuals raise HERE instead of emitting
        # invalid JSON (bare ``NaN``) later, and the field coercion is
        # idempotent for the validated paths above.
        object.__setattr__(self, "axis", _check_axis(self.axis))
        object.__setattr__(self, "direction", _check_direction(self.direction))
        object.__setattr__(self, "slope", _as_float(self.slope, "slope"))
        object.__setattr__(self, "intercept", _as_float(self.intercept, "intercept"))
        object.__setattr__(
            self,
            "residual_tolerance",
            abs(_as_float(self.residual_tolerance, "residual_tolerance")),
        )
        object.__setattr__(
            self,
            "residuals",
            tuple(_as_float(r, "residuals[i]") for r in self.residuals),
        )

    @classmethod
    def fit(
        cls,
        anchors: Iterable[Any],
        *,
        axis: str = "x",
        name: str = "",
        unit: str = "",
        direction: Optional[str] = None,
        residual_tolerance: Optional[float] = None,
    ) -> "AxisCalibration":
        """Least-squares (or exact, for two anchors) fit of one axis."""
        axis = _check_axis(axis)
        picked = _coerce_anchors(anchors)
        slope, intercept, residuals = fit_linear(picked)
        if direction is None:
            direction = _direction_for(slope)
        else:
            direction = _check_direction(direction)
        if residual_tolerance is None:
            span = max(a.data_value for a in picked) - min(
                a.data_value for a in picked
            )
            residual_tolerance = max(1e-9, RESIDUAL_FRACTION * abs(span))
        else:
            residual_tolerance = abs(
                _as_float(residual_tolerance, "residual_tolerance")
            )
        return cls(
            axis=axis,
            name=name or axis,
            unit=unit or "",
            direction=direction,
            slope=slope,
            intercept=intercept,
            anchors=picked,
            residuals=residuals,
            residual_tolerance=residual_tolerance,
        )

    # -- conversion ------------------------------------------------------
    def to_data(self, pixel: Any) -> float:
        """Pixel position -> data value."""
        return self.slope * _as_float(pixel, "pixel") + self.intercept

    def to_pixel(self, data_value: Any) -> float:
        """Data value -> pixel position (inverse of :meth:`to_data`).

        Raises:
            CalibrationError: a zero slope maps every pixel to ONE data
                value, so the inverse is undefined.  A least-squares fit can
                legitimately return slope 0 (e.g. anchors (0,0)/(100,100)/
                (200,0)), and this used to surface as a bare ZeroDivisionError
                (FIX-2026-09-22, audit bug 5-1) - mirror of the
                :attr:`pixels_per_unit` guard below.
        """
        if not self.slope:
            raise CalibrationError(
                f"{self.axis}-axis slope is zero: data -> pixel conversion is "
                "undefined (every pixel maps to one data value)"
            )
        return (_as_float(data_value, "data_value") - self.intercept) / self.slope

    # callable aliases - the extractor reads a chart in one direction only,
    # so ``axis(px)`` is a handy shorthand in loops.
    __call__ = to_data

    # -- diagnostics -----------------------------------------------------
    @property
    def units_per_pixel(self) -> float:
        """Data units covered by one pixel (signed; ``abs()`` for resolution)."""
        return self.slope

    @property
    def pixels_per_unit(self) -> float:
        return 1.0 / self.slope if self.slope else float("inf")

    @property
    def data_span(self) -> float:
        if not self.anchors:
            return 0.0
        values = [a.data_value for a in self.anchors]
        return max(values) - min(values)

    @property
    def max_abs_residual(self) -> float:
        return max((abs(r) for r in self.residuals), default=0.0)

    @property
    def rms_residual(self) -> float:
        if not self.residuals:
            return 0.0
        return math.sqrt(sum(r * r for r in self.residuals) / len(self.residuals))

    @property
    def outlier_indices(self) -> Tuple[int, ...]:
        """Anchor positions the fit actually blames for a misfit.

        The plain "residual > tolerance" rule is guilt by smear: a least-
        squares fit drags its neighbours along, so one +80 mis-click among
        six collinear ticks used to flag FOUR innocent anchors (and "3 bad
        -> all 6 flagged"; FIX-2026-09-22, audit bug 5-4).

        Leave-one-out consensus replaces it wherever the anchor set has the
        redundancy to identify culprits: an anchor set is consistent when
        the anchors LEFT after removing a candidate set fit within the
        tolerance, and the smallest such removals are the answer.  Anchor
        counts are tiny (ticks on one axis), so trying every removal set of
        size 1..3 over at most :data:`_MAX_CONSENSUS_ANCHORS` anchors is
        cheap and exact; below 4 anchors no removal explains anything
        uniquely, and an ambiguous / oversized set keeps the conservative
        plain-residual report.
        """
        basic = tuple(
            i
            for i, r in enumerate(self.residuals)
            if abs(r) > self.residual_tolerance
        )
        n = len(self.anchors)
        if not basic or n < 4 or n > _MAX_CONSENSUS_ANCHORS:
            return basic
        for k in (1, 2, 3):
            if n - k < 3:
                continue
            explanations: List[Tuple[int, ...]] = []
            for combo in combinations(range(n), k):
                dropped = set(combo)
                rest = [a for i, a in enumerate(self.anchors) if i not in dropped]
                try:
                    _s, _b, residuals = fit_linear(rest)
                except CalibrationError:
                    continue
                if all(
                    abs(r) <= self.residual_tolerance for r in residuals
                ):
                    explanations.append(combo)
            if explanations:
                if len(explanations) == 1:
                    return explanations[0]
                common = set(explanations[0])
                for combo in explanations[1:]:
                    common &= set(combo)
                return tuple(sorted(common)) if common else basic
        return basic

    @property
    def issues(self) -> Tuple[str, ...]:
        """Machine-readable trust complaints (empty == trustworthy)."""
        found: List[str] = []
        if len(self.anchors) < 2:
            found.append("insufficient_anchors")
        if self.max_abs_residual > self.residual_tolerance:
            found.append("residual_above_tolerance")
        if self.direction != _direction_for(self.slope):
            found.append("direction_conflict")
        return tuple(found)

    @property
    def is_trusted(self) -> bool:
        """False when residuals exceed the tolerance (or metadata disagrees)."""
        return not self.issues

    # -- serialisation ---------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        return {
            "axis": self.axis,
            "name": self.name,
            "unit": self.unit,
            "direction": self.direction,
            "slope": self.slope,
            "intercept": self.intercept,
            "anchors": [a.to_json() for a in self.anchors],
            "residuals": list(self.residuals),
            "residual_tolerance": self.residual_tolerance,
            "max_abs_residual": self.max_abs_residual,
            "rms_residual": self.rms_residual,
            "is_trusted": self.is_trusted,
            "issues": list(self.issues),
        }

    @classmethod
    def from_json(
        cls, obj: Any, *, expected_axis: Optional[str] = None
    ) -> "AxisCalibration":
        """Rebuild an axis from :meth:`to_json` output.

        The fit is *re-derived* from the anchors when they are present (so a
        hand-edited ``slope`` cannot smuggle itself into a result), and the
        stored coefficients are used only for an anchor-less payload.

        ``expected_axis`` is the slot this payload is being loaded into (set
        by :meth:`Calibration.from_json`).  FIX-2026-09-22 (audit bug 4): a
        missing ``axis`` key now takes the slot's letter instead of silently
        defaulting to ``"x"`` - a y payload filed under x duplicated its
        issue codes, collided in ``worst_residual`` and round-tripped the
        wrong label into exports - and an ``axis`` that contradicts the slot
        is rejected outright.
        """
        if isinstance(obj, AxisCalibration):
            return obj
        if not isinstance(obj, dict):
            raise CalibrationError(f"axis calibration must be an object, got {obj!r}")
        for key in ("slope", "intercept"):
            if key not in obj:
                raise CalibrationError(f"axis calibration is missing {key!r}")
        declared = obj.get("axis")
        if expected_axis is not None:
            if declared is not None and _check_axis(declared) != expected_axis:
                raise CalibrationError(
                    f"the {expected_axis!r} calibration slot holds a payload "
                    f"declared as axis {declared!r}; refusing to guess which "
                    "one is right"
                )
            declared = expected_axis
        slope = _as_float(obj["slope"], "slope")
        intercept = _as_float(obj["intercept"], "intercept")
        anchors = _coerce_anchors(obj.get("anchors") or ())
        raw_residuals = obj.get("residuals")
        raw_direction = obj.get("direction")  # checked AFTER the refit below
        tolerance = obj.get("residual_tolerance")

        if len(anchors) >= 2:
            fit_slope, fit_intercept, residuals = fit_linear(anchors)
            span = max(a.data_value for a in anchors) - min(
                a.data_value for a in anchors
            )
            slope, intercept = fit_slope, fit_intercept
            auto = max(1e-9, RESIDUAL_FRACTION * abs(span))
            if tolerance is None:
                tolerance = auto
            else:
                tolerance = abs(_as_float(tolerance, "residual_tolerance"))
                # FIX-2026-09-22 (audit bug 5-3): never trust a payload
                # tolerance so wide that any misfit sails through - a
                # tolerance above half the data span is laundered, so it
                # falls back to the auto fraction recomputed from anchors.
                if tolerance > _TOLERANCE_SPAN_FRACTION * abs(span):
                    tolerance = auto
        else:
            # Anchor-less payload (a fit handed over as bare coefficients):
            # trust the stored residuals, and if there is no tolerance to
            # judge them by, a nonzero residual stays untrusted (conservative).
            residuals = tuple(
                _as_float(r, "residuals[i]") for r in (raw_residuals or ())
            )
            if tolerance is None:
                tolerance = 0.0

        # FIX-2026-09-22 (audit bug 5-2): the direction must follow the
        # REFITTED slope when the payload declares none - deriving it from
        # the (untrusted) stored slope let an anchor fit of -0.3 report
        # "direct" while the anchors are authoritative.  An EXPLICITly
        # declared direction is still kept as declared so the disagreement
        # surfaces as the "direction_conflict" issue, not a silent rewrite.
        direction = (
            _check_direction(raw_direction)
            if raw_direction
            else _direction_for(slope)
        )

        return cls(
            axis=_check_axis(declared if declared is not None else "x"),
            name=str(obj.get("name") or declared or "x"),
            unit=str(obj.get("unit") or ""),
            direction=direction,
            slope=slope,
            intercept=intercept,
            anchors=anchors,
            residuals=residuals,
            residual_tolerance=abs(_as_float(tolerance, "residual_tolerance")),
        )


def _check_axis(axis: Any) -> str:
    out = str(axis or "x").strip().lower()
    if out not in ("x", "y"):
        raise CalibrationError(f"axis must be 'x' or 'y', got {axis!r}")
    return out


def _check_direction(direction: Any) -> str:
    out = str(direction or "direct").strip().lower()
    if out in ("reverse", "reversed", "invert", "inverted", "descending"):
        return "reversed"
    if out in ("direct", "forward", "normal", "ascending"):
        return "direct"
    raise CalibrationError(
        f"direction must be one of {_DIRECTIONS}, got {direction!r}"
    )


@dataclass(frozen=True)
class Calibration:
    """A chart's x and y axis calibrations plus where they came from.

    Either axis may be ``None`` (a single-scale axis chart only needs one);
    the two conversion helpers then refuse to answer rather than inventing a
    value for the unmapped direction.
    """

    x: Optional[AxisCalibration] = None
    y: Optional[AxisCalibration] = None
    schema_version: int = SCHEMA_VERSION
    provenance: Dict[str, Any] = field(default_factory=dict)

    # -- construction ----------------------------------------------------
    @classmethod
    def from_anchors(
        cls,
        x_anchors: Optional[Iterable[Any]] = None,
        y_anchors: Optional[Iterable[Any]] = None,
        *,
        x_name: str = "x",
        y_name: str = "y",
        x_unit: str = "",
        y_unit: str = "",
        x_direction: Optional[str] = None,
        y_direction: Optional[str] = None,
        residual_tolerance: Optional[float] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> "Calibration":
        """Fit both axes from operator-picked anchors.

        ``*_anchors`` take :class:`Anchor` objects, ``{"pixel": ..,
        "data_value": ..}`` dicts or ``[pixel, value]`` pairs; ``None`` / empty
        leaves that axis unmapped.
        """
        x_picked = _coerce_anchors(x_anchors)
        y_picked = _coerce_anchors(y_anchors)
        if not x_picked and not y_picked:
            raise CalibrationError("need anchors for at least one axis")
        x = (
            AxisCalibration.fit(
                x_picked,
                axis="x",
                name=x_name,
                unit=x_unit,
                direction=x_direction,
                residual_tolerance=residual_tolerance,
            )
            if x_picked
            else None
        )
        y = (
            AxisCalibration.fit(
                y_picked,
                axis="y",
                name=y_name,
                unit=y_unit,
                direction=y_direction,
                residual_tolerance=residual_tolerance,
            )
            if y_picked
            else None
        )
        return cls(
            x=x, y=y, provenance=dict(provenance or {"method": "manual_anchors"})
        )

    # -- conversion ------------------------------------------------------
    @property
    def has_x(self) -> bool:
        return self.x is not None

    @property
    def has_y(self) -> bool:
        return self.y is not None

    def pixel_to_data(self, px: Any, py: Any) -> Tuple[float, float]:
        """``(pixel_x, pixel_y) -> (data_x, data_y)``.

        Pixel ``y`` grows downward, which is why a stratigraphic ``y`` axis
        typically comes back with a negative slope (top of the image =
        youngest) - see :attr:`AxisCalibration.direction`.
        """
        return (
            self._axis("x").to_data(px),
            self._axis("y").to_data(py),
        )

    def data_to_pixel(self, x_value: Any, y_value: Any) -> Tuple[float, float]:
        """Inverse of :meth:`pixel_to_data` (draw an overlay, re-place a marker)."""
        return (
            self._axis("x").to_pixel(x_value),
            self._axis("y").to_pixel(y_value),
        )

    def _axis(self, which: str) -> AxisCalibration:
        part = self.x if which == "x" else self.y
        if part is None:
            raise CalibrationError(
                f"no {which}-axis calibration: that axis was never anchored"
            )
        return part

    # -- diagnostics -----------------------------------------------------
    @property
    def issues(self) -> Tuple[str, ...]:
        # FIX-2026-09-22 (audit bug 4): prefixed by the SLOT the axis lives
        # in, not by part.axis - a mislabelled payload used to file y's
        # complaints twice under "x".  Calibration.from_json now also rejects
        # slot/axis mismatches, so the two agree by construction.
        found: List[str] = []
        for slot, part in (("x", self.x), ("y", self.y)):
            if part is None:
                continue
            found.extend(f"{slot}.{code}" for code in part.issues)
        return tuple(found)

    @property
    def is_trusted(self) -> bool:
        """True only when every mapped axis fits its anchors within tolerance."""
        parts = [p for p in (self.x, self.y) if p is not None]
        return bool(parts) and all(p.is_trusted for p in parts)

    @property
    def worst_residual(self) -> Dict[str, float]:
        """``{"x": .., "y": ..}`` worst absolute residuals, for trust display."""
        out: Dict[str, float] = {}
        for slot, part in (("x", self.x), ("y", self.y)):
            if part is not None:
                out[slot] = part.max_abs_residual
        return out

    # -- serialisation ---------------------------------------------------
    def to_json(self) -> Dict[str, Any]:
        """Stable, JSON-ready dict (``to_json`` == "produce JSON data")."""
        return {
            "schema_version": self.schema_version,
            "x": self.x.to_json() if self.x else None,
            "y": self.y.to_json() if self.y else None,
            "is_trusted": self.is_trusted,
            "issues": list(self.issues),
            "provenance": dict(self.provenance),
        }

    def to_json_string(self, *, indent: Optional[int] = None) -> str:
        # FIX-2026-09-22 (audit bug 5-7): emit VALID JSON only.  allow_nan=False
        # refuses the NaN/Infinity tokens json.loads would reject downstream,
        # and a non-serialisable provenance value becomes a CalibrationError
        # naming the problem instead of a bare TypeError from deep inside dumps.
        try:
            return json.dumps(
                self.to_json(),
                ensure_ascii=False,
                indent=indent,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                f"calibration cannot be serialised to JSON: {exc}"
            ) from exc

    @classmethod
    def from_json(cls, payload: Any) -> "Calibration":
        """Parse :meth:`to_json` output (dict or JSON string).

        Round-trips exactly for the fields that matter (coefficients,
        residuals, provenance); a ``schema_version`` newer than this build
        knows about is rejected instead of being half-read.
        """
        if isinstance(payload, Calibration):
            return payload
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError) as exc:
                raise CalibrationError(f"calibration JSON is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise CalibrationError(
                f"calibration must be an object, got {type(payload).__name__}"
            )
        version = payload.get("schema_version", SCHEMA_VERSION)
        # FIX-2026-09-22 (audit bug 5-5): ``int(inf)`` raises OverflowError,
        # which used to escape uncaught (json.dumps happily emits the bare
        # ``Infinity`` token this round-trips through), and int(1.9) silently
        # truncated - reject both instead of guessing.
        if isinstance(version, bool) or version is None:
            raise CalibrationError(
                f"schema_version must be an integer, got {version!r}"
            )
        if isinstance(version, float) and (
            not math.isfinite(version) or not version.is_integer()
        ):
            raise CalibrationError(
                f"schema_version must be an integer, got {version!r}"
            )
        try:
            version = int(version)
        except (TypeError, ValueError, OverflowError):
            raise CalibrationError(
                f"schema_version must be an integer, got {version!r}"
            ) from None
        if version > SCHEMA_VERSION:
            raise CalibrationError(
                f"calibration schema v{version} is newer than this build's "
                f"v{SCHEMA_VERSION}; upgrade Range Chart Analyzer or re-export"
            )
        prov = payload.get("provenance") or {}
        if not isinstance(prov, dict):
            raise CalibrationError("provenance must be an object")
        return cls(
            x=AxisCalibration.from_json(
                payload["x"], expected_axis="x"
            ) if payload.get("x") else None,
            y=AxisCalibration.from_json(
                payload["y"], expected_axis="y"
            ) if payload.get("y") else None,
            schema_version=version,
            provenance=dict(prov),
        )
