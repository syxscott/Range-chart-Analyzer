"""FIX-2026-09-22 regression tests - audit of the BORROW-2026-09-20 deskew +
geometry round (baseline 8f9ed23, re-verified against HEAD 5e724aa).

Every case below reproduces a bug the prior audit measured with synthetic
plates:

* deskew bug 1 - BOX-downsampling aliased dense ruling grids and the
  projection comb locked onto false peaks, SIGN-INVERTED (a -3 degree plate
  was "corrected" by -1.075, worsening it toward -4).  The estimator must
  now either truly correct (residual near zero) or fail safe to 0.0 - never
  make the plate worse.
* deskew bug 2 - ``expand=False`` silently cropped the plate frame a
  rotation pushed outside the old canvas (4000x3000 framed plate turned 5
  degrees -> top border kept 37 of 4000 black pixels); calibration anchors
  live on that frame.  The correction now grows the canvas (content
  preservation, not size invariance).
* deskew bug 3 - faint ink (paper 250 / line 235) and inverted polarity
  (dark paper, light ink) produced zero votes after the downsample averaged
  the contrast below the fixed floor: 0.0 degrees for a truly 2-degree
  skewed plate.
* geometry bugs 4 / 5 - axis-slot mislabelling in ``from_json``, bare
  ZeroDivisionError on a zero slope, direction derived from a stale slope,
  payload tolerances laundering misfits, guilt-by-proximity outlier flagging,
  uncaught OverflowError on ``schema_version``, inconsistent pixel-span
  thresholds and invalid-JSON / bare-TypeError serialisation.

All offline, deterministic, synthetic images; the deskew cases force
``method="projection"`` so OpenCV presence cannot change the outcome.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from rca_core.deskew import (
    deskew_image,
    estimate_skew_angle,
)
from rca_core.geometry import (
    RESIDUAL_FRACTION,
    SCHEMA_VERSION,
    AxisCalibration,
    Calibration,
    CalibrationError,
    fit_linear,
)

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
needs_pillow = pytest.mark.skipif(
    not PIL_AVAILABLE, reason="Pillow is an optional dependency"
)


def _grid(w: int, h: int, step: int, paper: int = 255, line: int = 20):
    from PIL import Image, ImageDraw  # lazy: optional dependency

    img = Image.new("L", (w, h), paper)
    draw = ImageDraw.Draw(img)
    for y in range(0, h, step):
        draw.line((0, y, w - 1, y), fill=line, width=1)
    return img


def _dark_count(img) -> int:
    return sum(1 for v in img.convert("L").tobytes() if v < 200)


# ---------------------------------------------------------------------------
# deskew bug 1: aliased downsamples must never produce a sign-inverted correction
# ---------------------------------------------------------------------------

@needs_pillow
@pytest.mark.parametrize(
    "size, step, theta",
    [
        # the exact probe matrix from the audit: at these realistic scan
        # sizes the default downsample (factor 4..9) puts the working line
        # period at or below the 2-px Nyquist limit and the old estimator
        # returned -1.075 / +2.050 / -0.015 / +1.000 for thetas whose needed
        # corrections were +3.0 / -1.5 / +2.0 / +3.0.
        ((2400, 1800), 8, -3.0),
        ((2400, 1800), 8, 1.5),
        ((2000, 2800), 8, -2.0),
        ((900, 2400), 10, -3.0),
    ],
)
def test_dense_grid_is_corrected_or_fails_safe_never_worse(size, step, theta):
    plate = _grid(*size, step).rotate(theta, expand=False, fillcolor=255)
    out, angle = deskew_image(plate, method="projection")
    if angle == 0.0:
        return  # audited fail-safe contract: an unverifiable angle is a no-op
    assert abs(angle - (-theta)) < 0.3, (angle, theta)
    residual = estimate_skew_angle(out, method="projection")
    # the correction removed the skew instead of deepening it (old behaviour:
    # -3.0 deg in, -1.075 "correction" out, real residual -4.08 deg)
    assert abs(residual) <= abs(theta) - 0.5, (residual, theta)
    assert abs(residual) < 0.5, residual


@needs_pillow
def test_step4_grid_at_realistic_size_is_still_corrected():
    # period 4 px: even the native patch is dense, but > 2 px so it cannot
    # alias - the verification tier resolves what the factor-4 copy cannot.
    plate = _grid(1600, 1200, 4).rotate(2.0, expand=False, fillcolor=255)
    out, angle = deskew_image(plate, method="projection")
    assert abs(angle - (-2.0)) < 0.3, angle
    assert abs(estimate_skew_angle(out, method="projection")) < 0.5


# ---------------------------------------------------------------------------
# deskew bug 2: the rotation must keep the whole frame, not crop it
# ---------------------------------------------------------------------------

@needs_pillow
def test_rotation_preserves_the_outer_frame_instead_of_cropping_it():
    from PIL import Image, ImageDraw

    img = Image.new("L", (800, 600), 255)
    draw = ImageDraw.Draw(img)
    for y in range(0, 600, 40):
        draw.line((0, y, 799, y), fill=0, width=1)
    draw.rectangle((0, 0, 799, 599), outline=0, width=2)  # the calibration frame
    skewed = img.rotate(4.0, expand=False, fillcolor=255)
    before = _dark_count(skewed)

    out, angle = deskew_image(skewed, method="projection")
    assert abs(angle - (-4.0)) < 0.3, angle
    # expand=True: the canvas grows to hold the rotated plate...
    assert out.size[0] > skewed.size[0] and out.size[1] > skewed.size[1], out.size
    # ...and the frame ink survives (expand=False whitened the corners: a
    # 4-5 deg turn clipped a third of a framed plate's dark pixels).
    assert _dark_count(out) >= 0.9 * before, (_dark_count(out), before)
    assert abs(estimate_skew_angle(out, method="projection")) < 0.3


@needs_pillow
def test_zero_angle_still_returns_the_same_object():
    # identity contract the extractor relies on for its `modified` flag:
    # no rotation -> the ORIGINAL image object, untouched size included.
    img = _grid(600, 420, 40)
    out, angle = deskew_image(img, method="projection")
    assert angle == 0.0
    assert out is img
    assert out.size == img.size


# ---------------------------------------------------------------------------
# deskew bug 3: faint and inverted-polarity ink must survive the downsample
# ---------------------------------------------------------------------------

@needs_pillow
def test_faint_low_contrast_plate_is_corrected():
    # paper 250 / line 235 through a factor-4 BOX shrink leaves ~4 grey
    # levels of contrast; the old fixed floor of 6 voted zero pixels -> 0.0
    # degrees for a genuinely 2-degree-skewed plate.
    plate = _grid(1200, 900, 40, paper=250, line=235).rotate(
        2.0, expand=False, fillcolor=250
    )
    out, angle = deskew_image(plate, method="projection")
    assert abs(angle - (-2.0)) < 0.3, angle
    assert abs(estimate_skew_angle(out, method="projection")) < 0.3


@needs_pillow
def test_inverted_polarity_plate_is_corrected():
    # light ink on dark paper: the old "prefer the bright mode" paper rule
    # called the INK the paper and the dark-ground vote came out empty.
    plate = _grid(1200, 900, 40, paper=10, line=230).rotate(
        2.0, expand=False, fillcolor=10
    )
    out, angle = deskew_image(plate, method="projection")
    assert abs(angle - (-2.0)) < 0.3, angle
    assert abs(estimate_skew_angle(out, method="projection")) < 0.5


# ---------------------------------------------------------------------------
# geometry bug 4: the x / y slot labels must be enforced
# ---------------------------------------------------------------------------

def test_from_json_infers_the_axis_label_from_its_slot():
    cal = Calibration.from_json(
        {
            "x": {"slope": 0.5, "intercept": 1.0},
            "y": {"slope": -0.25, "intercept": 120.0},
        }
    )
    assert cal.x.axis == "x" and cal.y.axis == "y"
    # the audited repro: y's complaint used to be filed twice under x, and
    # worst_residual's single "x" key swallowed the y value.
    assert cal.issues == ("x.insufficient_anchors", "y.insufficient_anchors")
    assert set(cal.worst_residual) == {"x", "y"}
    # ...and the mislabel used to round-trip into exports:
    assert cal.to_json()["y"]["axis"] == "y"
    assert json.loads(cal.to_json_string())["y"]["axis"] == "y"


def test_from_json_rejects_an_axis_label_that_contradicts_its_slot():
    with pytest.raises(CalibrationError):
        Calibration.from_json(
            {
                "x": {"slope": 0.5, "intercept": 1.0},
                "y": {"axis": "x", "slope": -0.25, "intercept": 120.0},
            }
        )
    with pytest.raises(CalibrationError):
        Calibration.from_json(
            {
                "x": {"axis": "y", "slope": 0.5, "intercept": 1.0},
                "y": {"slope": -0.25, "intercept": 120.0},
            }
        )


# ---------------------------------------------------------------------------
# geometry bug 5: contract edges
# ---------------------------------------------------------------------------

def test_to_pixel_with_a_zero_slope_raises_calibration_error():
    # 3-point LSQ legitimately returns slope 0 here; the bare
    # ZeroDivisionError the audit measured must now be a CalibrationError.
    axis = AxisCalibration.fit([(0, 0), (100, 100), (200, 0)], axis="x")
    assert axis.slope == pytest.approx(0.0)
    assert axis.to_data(50.0) == pytest.approx(33.33333, rel=1e-4)
    with pytest.raises(CalibrationError):
        axis.to_pixel(50.0)
    with pytest.raises(CalibrationError):
        Calibration(x=axis).data_to_pixel(1.0, 1.0)


def test_direction_follows_the_refit_not_a_stale_payload_slope():
    payload = {
        "axis": "y",
        "slope": 0.3,  # stale hand-edit; the anchors below refit to -1.0
        "intercept": 0.0,
        "anchors": [{"pixel": 0, "data_value": 0}, {"pixel": 100, "data_value": -100}],
    }
    axis = AxisCalibration.from_json(payload, expected_axis="y")
    assert axis.slope == pytest.approx(-1.0)
    assert axis.direction == "reversed"  # used to be "direct" + direction_conflict
    assert axis.issues == ()
    assert axis.is_trusted

    # An EXPLICIT direction is still honoured-as-declared so the conflict
    # stays visible as an issue instead of being silently rewritten.
    axis = AxisCalibration.from_json(dict(payload, direction="direct"), expected_axis="y")
    assert axis.direction == "direct"
    assert axis.issues == ("direction_conflict",)
    assert axis.is_trusted is False


def test_payload_tolerance_cannot_launder_a_real_misfit():
    payload = {
        "axis": "x",
        "slope": 1.0,
        "intercept": 0.0,
        "anchors": [
            {"pixel": 0, "data_value": 0},
            {"pixel": 100, "data_value": 100},
            {"pixel": 200, "data_value": 233},
        ],
        "residual_tolerance": 1e9,  # the audited laundering value
    }
    axis = AxisCalibration.from_json(payload, expected_axis="x")
    assert axis.max_abs_residual == pytest.approx(11.0)
    # 1e9 is absurd against a 233-unit span: it falls back to the auto 2 %.
    assert axis.residual_tolerance == pytest.approx(RESIDUAL_FRACTION * 233)
    assert axis.is_trusted is False
    assert "residual_above_tolerance" in axis.issues
    # a LENIENT but sane tolerance (30 % of span) still survives the check
    sane = AxisCalibration.from_json(
        dict(payload, residual_tolerance=0.3 * 233), expected_axis="x"
    )
    assert sane.residual_tolerance == pytest.approx(0.3 * 233)
    assert sane.is_trusted


def test_outlier_indices_name_the_real_culprit_not_the_neighbours():
    # six collinear ticks with ONE +80 mis-click: the old rule flagged 0, 3,
    # 4 AND 5 - four anchors for one mistake.
    axis = AxisCalibration.fit(
        [(0, 0), (100, 100), (200, 200), (300, 300), (400, 400), (500, 580)],
        axis="x",
    )
    assert axis.outlier_indices == (5,)
    # three bad ticks among six -> exactly those three, not all six
    axis = AxisCalibration.fit(
        [(0, 0), (100, 100), (200, 200), (300, 900), (400, 1000), (500, 2000)],
        axis="x",
    )
    assert axis.outlier_indices == (3, 4, 5)
    # with only three anchors no removal set is informative (any pair fits
    # exactly), so the conservative all-suspect report stays
    axis = AxisCalibration.fit([(0, 0), (100, 100), (200, 101)], axis="x")
    assert axis.outlier_indices == (0, 1, 2)
    # a clean fit flags nothing
    assert (
        AxisCalibration.fit(
            [(0, 0), (100, 100), (200, 200), (300, 300)], axis="x"
        ).outlier_indices
        == ()
    )


def test_schema_version_rejects_infinity_and_fractional_values():
    # json.dumps emits the bare Infinity token by default, so this payload
    # genuinely round-trips through the SQLite history; int(inf) then raised
    # an uncaught OverflowError.
    with pytest.raises(CalibrationError):
        Calibration.from_json(
            '{"schema_version": Infinity, "x": {"slope": 1.0, "intercept": 0.0}}'
        )
    with pytest.raises(CalibrationError):
        Calibration.from_json({"schema_version": float("inf")})
    # 1.9 must be rejected, not silently truncated to a v1 read
    with pytest.raises(CalibrationError):
        Calibration.from_json({"schema_version": 1.9})
    # an exact integral float still parses
    cal = Calibration.from_json(
        {"schema_version": float(SCHEMA_VERSION), "x": {"slope": 1.0, "intercept": 0.0}}
    )
    assert cal.schema_version == SCHEMA_VERSION


def test_pixel_span_degeneracy_threshold_is_consistent():
    # audit bug 5-6: the two-point path accepted a 1e-7-pixel span while the
    # three-point path rejected the same span as "zero pixel variance".
    two = fit_linear([(0, 0), (1e-7, 1.0)])
    three = fit_linear([(0, 0), (1e-7, 1.0), (2e-7, 2.0)])
    assert two[0] == pytest.approx(three[0], rel=1e-6)
    # and both paths agree on the genuinely degenerate side
    with pytest.raises(CalibrationError):
        fit_linear([(0, 0), (0, 1.0)])
    with pytest.raises(CalibrationError):
        fit_linear([(0, 0), (0, 1.0), (0, 2.0)])


def test_to_json_string_emits_valid_json_or_a_clear_error():
    # non-serialisable provenance raised a bare TypeError out of json.dumps
    cal = Calibration.from_anchors([(0, 0), (100, 100)], provenance={"bad": object()})
    with pytest.raises(CalibrationError):
        cal.to_json_string()
    # NaN could never enter through the validated door in the first place...
    with pytest.raises(CalibrationError):
        AxisCalibration(
            axis="x", name="x", unit="", direction="direct",
            slope=float("nan"), intercept=0.0,
        )
    with pytest.raises(CalibrationError):
        AxisCalibration(
            axis="x", name="x", unit="", direction="direct",
            slope=1.0, intercept=0.0, residuals=(float("nan"),),
        )
    # ...and a clean calibration round-trips through strict JSON.
    cal = Calibration.from_anchors([(0, 0), (100, 100)], [(0, 10), (100, 20)])
    parsed = json.loads(cal.to_json_string())  # json.loads is strict on NaN too
    assert parsed["schema_version"] == SCHEMA_VERSION
    back = Calibration.from_json(parsed)
    assert back.pixel_to_data(50, 50) == pytest.approx(cal.pixel_to_data(50, 50))


# ---------------------------------------------------------------------------
# item 8: rca_core re-exports the deskew / geometry public API
# ---------------------------------------------------------------------------

def test_rca_core_exports_deskew_and_geometry_api():
    import rca_core

    for name in (
        "deskew_image",
        "estimate_skew_angle",
        "hough_skew_angle",
        "DeskewError",
        "MIN_USEFUL_ANGLE",
        "Calibration",
        "CalibrationError",
        "AxisCalibration",
        "Anchor",
        "fit_linear",
        "SCHEMA_VERSION",
        "RESIDUAL_FRACTION",
    ):
        assert hasattr(rca_core, name), name
        assert name in rca_core.__all__, name
