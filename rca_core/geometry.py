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
        if sxx <= _EPS:  # pragma: no cover - guarded by the span checks above
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
        """Data value -> pixel position (inverse of :meth:`to_data`)."""
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
        """Anchor positions whose residual breaks the tolerance.

        With 3+ anchors the worst offender is almost always a single
        mis-clicked tick; surfacing its index lets the UI highlight exactly
        that anchor instead of rejecting the whole axis.
        """
        return tuple(
            i
            for i, r in enumerate(self.residuals)
            if abs(r) > self.residual_tolerance
        )

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
    def from_json(cls, obj: Any) -> "AxisCalibration":
        """Rebuild an axis from :meth:`to_json` output.

        The fit is *re-derived* from the anchors when they are present (so a
        hand-edited ``slope`` cannot smuggle itself into a result), and the
        stored coefficients are used only for an anchor-less payload.
        """
        if isinstance(obj, AxisCalibration):
            return obj
        if not isinstance(obj, dict):
            raise CalibrationError(f"axis calibration must be an object, got {obj!r}")
        for key in ("slope", "intercept"):
            if key not in obj:
                raise CalibrationError(f"axis calibration is missing {key!r}")
        slope = _as_float(obj["slope"], "slope")
        intercept = _as_float(obj["intercept"], "intercept")
        anchors = _coerce_anchors(obj.get("anchors") or ())
        raw_residuals = obj.get("residuals")
        direction = obj.get("direction") or _direction_for(slope)
        tolerance = obj.get("residual_tolerance")

        if len(anchors) >= 2:
            fit_slope, fit_intercept, residuals = fit_linear(anchors)
            span = max(a.data_value for a in anchors) - min(
                a.data_value for a in anchors
            )
            slope, intercept = fit_slope, fit_intercept
            if tolerance is None:
                tolerance = max(1e-9, RESIDUAL_FRACTION * abs(span))
        else:
            # Anchor-less payload (a fit handed over as bare coefficients):
            # trust the stored residuals, and if there is no tolerance to
            # judge them by, a nonzero residual stays untrusted (conservative).
            residuals = tuple(
                _as_float(r, "residuals[i]") for r in (raw_residuals or ())
            )
            if tolerance is None:
                tolerance = 0.0

        return cls(
            axis=_check_axis(obj.get("axis", "x")),
            name=str(obj.get("name") or obj.get("axis") or "x"),
            unit=str(obj.get("unit") or ""),
            direction=_check_direction(direction),
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
        found: List[str] = []
        for part in (self.x, self.y):
            if part is None:
                continue
            found.extend(f"{part.axis}.{code}" for code in part.issues)
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
        for part in (self.x, self.y):
            if part is not None:
                out[part.axis] = part.max_abs_residual
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
        return json.dumps(self.to_json(), ensure_ascii=False, indent=indent,
                          sort_keys=True)

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
        try:
            version = int(version)
        except (TypeError, ValueError):
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
            x=AxisCalibration.from_json(payload["x"]) if payload.get("x") else None,
            y=AxisCalibration.from_json(payload["y"]) if payload.get("y") else None,
            schema_version=version,
            provenance=dict(prov),
        )
