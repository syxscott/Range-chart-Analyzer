"""Tests for the geometry domain added BORROW-2026-09-20.

* :mod:`rca_core.geometry` - two-point / least-squares pixel <-> data
  calibration (pure stdlib, always runs).
* :mod:`rca_core.deskew` - plate skew estimation and correction.  Pillow is
  an OPTIONAL dependency, so the deskew cases skip instead of failing when it
  is absent (the project's standard optional-dependency contract), while the
  module itself must still import.
"""

from __future__ import annotations

import importlib.util
import json
import math

import pytest

from rca_core import deskew
from rca_core.deskew import (
    DeskewError,
    MIN_USEFUL_ANGLE,
    deskew_image,
    estimate_skew_angle,
    hough_skew_angle,
)
from rca_core.geometry import (
    RESIDUAL_FRACTION,
    SCHEMA_VERSION,
    Anchor,
    Calibration,
    CalibrationError,
    AxisCalibration,
    fit_linear,
)

PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None
needs_pillow = pytest.mark.skipif(
    not PIL_AVAILABLE, reason="Pillow is an optional dependency"
)


# ---------------------------------------------------------------------------
# geometry: the fit itself
# ---------------------------------------------------------------------------

def test_two_anchor_fit_is_the_exact_line():
    # BORROW-2026-09-20 (graph2table): two ticks must give the exact scale.
    axis = AxisCalibration.fit([(10.0, 0.0), (210.0, 20.0)], axis="x", unit="Ma")
    assert axis.slope == pytest.approx(0.1)
    assert axis.intercept == pytest.approx(-1.0)
    assert axis.max_abs_residual == pytest.approx(0.0, abs=1e-9)
    assert axis.rms_residual == pytest.approx(0.0, abs=1e-9)
    assert axis.is_trusted
    assert axis.direction == "direct"
    assert axis.units_per_pixel == pytest.approx(0.1)
    assert axis.pixels_per_unit == pytest.approx(10.0)
    assert axis(50.0) == pytest.approx(4.0)  # __call__ == to_data shorthand


def test_fit_linear_returns_residuals_in_data_units():
    # The middle tick was misread by 1 data unit; the fit must not hide it.
    slope, intercept, residuals = fit_linear(
        [(0.0, 0.0), (100.0, 99.0), (200.0, 200.0)]
    )
    assert slope == pytest.approx(1.0)
    assert intercept == pytest.approx(-1 / 3.0)
    assert len(residuals) == 3
    assert residuals == pytest.approx([1 / 3.0, -2 / 3.0, 1 / 3.0])
    assert max(abs(r) for r in residuals) > 0.4


def test_least_squares_matches_the_manual_solution():
    # pixels 0/50/100/150, values 0/50/101/151 -> by hand: mean_p 75,
    # mean_d 75.5, Sxy 12600, Sxx 12500 -> slope 1.008, intercept -0.1.
    axis = AxisCalibration.fit(
        [(0, 0), (50, 50), (100, 101), (150, 151)], axis="y", name="level"
    )
    assert axis.slope == pytest.approx(1.008)
    assert axis.intercept == pytest.approx(-0.1)
    assert axis.residuals == pytest.approx([0.1, -0.3, 0.3, -0.1])
    assert sum(axis.residuals) == pytest.approx(0.0, abs=1e-9)  # centred fit
    # span = 151 -> tolerance = 2 % of it; the worst residual is well inside.
    assert axis.residual_tolerance == pytest.approx(RESIDUAL_FRACTION * 151)
    assert axis.max_abs_residual < axis.residual_tolerance
    assert axis.is_trusted and axis.issues == ()
    assert axis.outlier_indices == ()
    assert axis.rms_residual == pytest.approx(
        math.sqrt((0.01 + 0.09 + 0.09 + 0.01) / 4)
    )


def test_mis_clicked_anchor_is_flagged_as_outlier_and_untrusted():
    # "Bed 101" clicked where Bed 100 belongs, on an axis that should read 200.
    # Least squares smears one bad tick over the whole fit - which is exactly
    # why the residuals are reported instead of a bare slope.
    axis = AxisCalibration.fit(
        [(0, 0), (100, 100), (200, 101)], axis="x", name="bed"
    )
    assert axis.slope == pytest.approx(0.505)
    assert axis.intercept == pytest.approx(16.5)
    assert axis.residuals == pytest.approx([-16.5, 33.0, -16.5])
    assert axis.is_trusted is False
    assert "residual_above_tolerance" in axis.issues
    assert axis.max_abs_residual == pytest.approx(33.0)
    assert axis.outlier_indices == (0, 1, 2)  # span 101 -> tolerance 2.02
    assert axis.data_span == pytest.approx(101.0)

    # With more anchors the report gets sharper: the tick that stayed on the
    # fitted line is not flagged, every other one is.
    wider = AxisCalibration.fit(
        [(0, 0), (100, 100), (200, 200), (300, 300), (400, 200)], axis="x"
    )
    assert wider.slope == pytest.approx(0.6)
    assert wider.intercept == pytest.approx(40.0)
    assert wider.residuals == pytest.approx([-40.0, 0.0, 40.0, 80.0, -80.0])
    # FIX-2026-09-22 (audit bug 5-4): the old plain-residual rule flagged
    # (0, 2, 3, 4) - four anchors for one mis-click, guilt by smear.  The
    # leave-one-out consensus names the single culprit instead.
    assert wider.outlier_indices == (4,)
    assert wider.max_abs_residual == pytest.approx(80.0)


def test_explicit_tolerance_overrides_the_auto_fraction():
    anchors = [(0, 0), (100, 100), (200, 200.5)]
    lenient = AxisCalibration.fit(anchors, axis="x")
    strict = AxisCalibration.fit(anchors, axis="x", residual_tolerance=0.05)
    assert lenient.is_trusted and lenient.max_abs_residual < 0.5
    assert strict.is_trusted is False
    assert "residual_above_tolerance" in strict.issues


def test_reversed_stratigraphic_axis_top_is_younger():
    # Level decreases downward: pixel 0 = 120 m, pixel 400 = 20 m.
    axis = AxisCalibration.fit(
        [(0.0, 120.0), (400.0, 20.0)], axis="y", name="level", unit="m"
    )
    assert axis.direction == "reversed"
    assert axis.slope == pytest.approx(-0.25)
    assert axis.to_data(0) == pytest.approx(120.0)
    assert axis.to_data(200) == pytest.approx(70.0)
    assert axis.to_pixel(70.0) == pytest.approx(200.0)
    # A declared direction that contradicts the fitted sign is an issue.
    wrong = AxisCalibration.fit(
        [(0.0, 120.0), (400.0, 20.0)], axis="y", direction="direct"
    )
    assert wrong.direction == "direct"
    assert wrong.issues == ("direction_conflict",)
    assert wrong.is_trusted is False


def test_degenerate_anchor_sets_are_rejected():
    with pytest.raises(CalibrationError):
        fit_linear([(10, 5)])
    with pytest.raises(CalibrationError):
        fit_linear([])
    with pytest.raises(CalibrationError):  # no scale on a single pixel
        fit_linear([(10, 5), (10, 9)])
    with pytest.raises(CalibrationError):  # no scale on a single value
        fit_linear([(10, 5), (20, 5)])
    with pytest.raises(CalibrationError):
        AxisCalibration.fit([(0, 0), (1, 1)], axis="z")
    with pytest.raises(CalibrationError):
        AxisCalibration.fit([(0, 0), (1, 1)], direction="sideways")
    with pytest.raises(CalibrationError):
        fit_linear([(0, 0), (1, float("nan"))])


def test_anchor_input_forms_are_all_accepted():
    forms = [
        [Anchor(0, 0), Anchor(100, 10)],
        [{"pixel": 0, "data_value": 0}, {"pixel": 100, "data_value": 10}],
        [{"px": 0, "value": 0}, (100, 10)],
        [[0, 0], [100, 10]],
    ]
    for form in forms:
        axis = AxisCalibration.fit(form, axis="x")
        assert axis.to_data(50) == pytest.approx(5.0)
    with pytest.raises(CalibrationError):
        Anchor.from_json({"pixel": 0})
    with pytest.raises(CalibrationError):
        Anchor.from_json(42)


# ---------------------------------------------------------------------------
# geometry: the two-axis Calibration
# ---------------------------------------------------------------------------

def _sample_calibration(**kw: object) -> Calibration:
    """Age increases downward on x, level decreases downward on y."""
    return Calibration.from_anchors(
        [(120.0, 0.0), (620.0, 50.0)],          # x: age in Ma
        [(0.0, 120.0), (400.0, 20.0)],          # y: level in m (reversed)
        x_name="age",
        y_name="level",
        x_unit="Ma",
        y_unit="m",
        provenance={"method": "manual_anchors", "picked_by": "operator", "image": "plate_04.tif"},
        **kw,
    )


def test_calibration_maps_both_directions():
    cal = _sample_calibration()
    assert cal.x is not None and cal.y is not None
    assert cal.x.direction == "direct" and cal.y.direction == "reversed"
    # x: 120 px -> 0 Ma, 620 px -> 50 Ma -> 0.1 Ma/px
    assert cal.pixel_to_data(370.0, 200.0) == pytest.approx((25.0, 70.0))
    assert cal.data_to_pixel(25.0, 70.0) == pytest.approx((370.0, 200.0))
    # round trip
    assert cal.data_to_pixel(*cal.pixel_to_data(111.0, 222.0)) == pytest.approx(
        (111.0, 222.0)
    )
    assert cal.pixel_to_data(*cal.data_to_pixel(3.7, 91.2)) == pytest.approx(
        (3.7, 91.2)
    )
    assert cal.is_trusted and cal.issues == ()
    assert cal.worst_residual == {"x": cal.x.max_abs_residual, "y": cal.y.max_abs_residual}


def test_calibration_needs_at_least_one_axis():
    with pytest.raises(CalibrationError):
        Calibration.from_anchors([], [])
    only_y = Calibration.from_anchors(None, [(0, 10), (100, 20)], y_name="level")
    assert only_y.has_x is False and only_y.has_y is True
    assert only_y.y.to_data(50) == pytest.approx(15.0)
    with pytest.raises(CalibrationError):
        only_y.pixel_to_data(10, 10)
    with pytest.raises(CalibrationError):
        only_y.data_to_pixel(1, 1)


def test_untrusted_axis_makes_the_whole_calibration_untrusted():
    cal = Calibration.from_anchors(
        [(0, 0), (100, 100), (200, 101)], [(0, 0), (10, 10)]
    )
    assert cal.is_trusted is False
    assert "x.residual_above_tolerance" in cal.issues
    assert "y.residual_above_tolerance" not in cal.issues


def test_to_json_schema_is_stable_and_round_trips():
    cal = _sample_calibration()
    payload = cal.to_json()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert set(payload) == {
        "schema_version", "x", "y", "is_trusted", "issues", "provenance",
    }
    assert payload["provenance"]["image"] == "plate_04.tif"
    assert payload["x"]["anchors"] == [
        {"pixel": 120.0, "data_value": 0.0},
        {"pixel": 620.0, "data_value": 50.0},
    ]
    for key in ("axis", "name", "unit", "direction", "slope", "intercept",
                "residuals", "residual_tolerance", "max_abs_residual",
                "rms_residual", "is_trusted", "issues"):
        assert key in payload["y"], key
    assert json.dumps(payload)  # JSON-ready, no dataclass leaking through

    text = cal.to_json_string()
    back = Calibration.from_json(text)          # parses a JSON string too
    assert back.to_json() == payload
    assert back.provenance["image"] == "plate_04.tif"
    assert back.provenance["picked_by"] == "operator"
    assert back.is_trusted == cal.is_trusted
    for px, py in ((0.0, 0.0), (370.0, 200.0), (620.0, 400.0)):
        assert back.pixel_to_data(px, py) == pytest.approx(cal.pixel_to_data(px, py))


def test_from_json_re_refits_instead_of_trusting_hand_edited_coefficients():
    payload = _sample_calibration().to_json()
    payload["x"]["slope"] = 999.0
    payload["x"]["intercept"] = -999.0
    back = Calibration.from_json(payload)
    assert back.pixel_to_data(370.0, 0.0) == pytest.approx((25.0, 120.0))


def test_from_json_rejects_malformed_payloads():
    with pytest.raises(CalibrationError):
        Calibration.from_json("{not json")
    with pytest.raises(CalibrationError):
        Calibration.from_json([1, 2, 3])
    with pytest.raises(CalibrationError):
        Calibration.from_json({"schema_version": SCHEMA_VERSION + 1})
    with pytest.raises(CalibrationError):
        Calibration.from_json({"x": {"intercept": 1.0}})  # missing slope
    with pytest.raises(CalibrationError):
        Calibration.from_json({"schema_version": "v1"})
    with pytest.raises(CalibrationError):
        Calibration.from_json({"provenance": [1]})


def test_coefficient_only_calibration_still_round_trips():
    cal = _sample_calibration()
    stripped = {
        "schema_version": SCHEMA_VERSION,
        "x": {"axis": "x", "slope": cal.x.slope, "intercept": cal.x.intercept,
              "residuals": [0.0, 0.0], "residual_tolerance": 1e-6},
        "y": {"axis": "y", "slope": cal.y.slope, "intercept": cal.y.intercept},
        "provenance": {"method": "hand_entered"},
    }
    back = Calibration.from_json(stripped)
    assert back.pixel_to_data(370.0, 200.0) == pytest.approx((25.0, 70.0))
    assert back.x.anchors == ()
    assert back.x.direction == "direct" and back.y.direction == "reversed"
    # No anchors means nothing to check the coefficients against, so the
    # trust flag stays conservative while the mapping itself still works.
    assert back.is_trusted is False
    assert back.issues == ("x.insufficient_anchors", "y.insufficient_anchors")
    assert back.provenance == {"method": "hand_entered"}


# ---------------------------------------------------------------------------
# deskew: helpers
# ---------------------------------------------------------------------------

def _grid(w: int = 600, h: int = 420, step: int = 40, columns: int = 0):
    """White plate with horizontal ruling lines (and optional vertical ones)."""
    from PIL import Image, ImageDraw  # imported lazily: optional dependency

    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    for y in range(0, h, step):
        draw.line((0, y, w - 1, y), fill=20, width=1)
    if columns:
        for x in range(0, w, max(1, w // columns)):
            draw.line((x, 0, x, h - 1), fill=20, width=1)
    return img


def _longest_flat_run(img) -> int:
    """Longest unbroken ink run inside a single row - an independent measure.

    A truly horizontal line gives one run across the whole plate; a line
    tilted by theta is cut into runs of about ``1 / tan(theta)`` pixels, so
    this separates "level" from "skewed" without using our own estimator.
    """
    w, h = img.size
    data = img.convert("L").tobytes()
    best = 0
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        run = 0
        for v in row:
            if v < 200:  # any darkening counts; the run LENGTH is the signal
                run += 1
                if run > best:
                    best = run
            else:
                run = 0
    return best


def _dark_count(img) -> int:
    """Number of visibly-inked pixels (L < 200) - a rotation must not lose them."""
    data = img.convert("L").tobytes()
    return sum(1 for v in data if v < 200)


def _assert_frame_preserved(out, src) -> None:
    """FIX-2026-09-22 (audit bug 2): deskew rotates with expand=True.

    The canvas grows so the WHOLE plate survives - size invariance is gone
    on purpose (border rows / calibration anchors used to be cropped into
    white wedges) - so the stable contract is content preservation:
    a bigger canvas plus nearly all of the source ink still present.
    """
    assert out.size[0] >= src.size[0] and out.size[1] >= src.size[1], (
        out.size, src.size
    )
    assert _dark_count(out) >= 0.75 * _dark_count(src), (
        _dark_count(out), _dark_count(src)
    )


@needs_pillow
def test_straight_plate_is_returned_untouched():
    img = _grid()
    out, angle = deskew_image(img)
    assert angle == 0.0
    assert out is img  # no copy, no resampling blur for a 0-degree "fix"


@needs_pillow
@pytest.mark.parametrize("true_rotation", [-3.5, -1.0, 1.0, 2.0, 3.0])
def test_projection_finds_the_known_skew(true_rotation: float):
    skewed = _grid().rotate(true_rotation, expand=False, fillcolor=255)
    out, angle = deskew_image(skewed, method="projection")
    # Pillow rotates counter-clockwise, so a +2 deg input plate shows up as
    # a -2 deg correction; the magnitude is what the operator cares about.
    assert abs(abs(angle) - abs(true_rotation)) < 0.2, (angle, true_rotation)
    assert abs(angle - (-true_rotation)) < 0.2
    _assert_frame_preserved(out, skewed)
    # correcting with the returned angle must level the ruling lines
    from PIL import Image

    assert isinstance(out, Image.Image)
    residual = estimate_skew_angle(out, method="projection")
    assert abs(residual) < MIN_USEFUL_ANGLE


@needs_pillow
def test_corrected_lines_really_are_horizontal():
    """Verify with an independent measure, not with our own estimator."""
    skewed = _grid().rotate(2.0, expand=False, fillcolor=255)
    before = _longest_flat_run(skewed)
    assert before < 60, before  # 2 deg cuts each ruling line into ~29 px runs

    out, angle = deskew_image(skewed, method="projection")
    after = _longest_flat_run(out)
    assert after > 400, (angle, after)  # lines lie on whole pixel rows again
    _assert_frame_preserved(out, skewed)


@needs_pillow
def test_vertical_ruling_lines_also_drive_the_estimate():
    # A plate with only verticals: axis="auto" must use the column profile.
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (420, 600), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for x in range(0, 420, 20):
        draw.line((x, 0, x, 599), fill=(30, 30, 30), width=1)
    skewed = img.rotate(2.5, expand=False, fillcolor=(255, 255, 255))
    out, angle = deskew_image(skewed, method="projection")
    assert abs(angle - (-2.5)) < 0.25, angle
    assert out.mode == "RGB"
    _assert_frame_preserved(out, skewed)


@needs_pillow
def test_small_skew_is_left_alone_but_min_angle_is_configurable():
    slightly_off = _grid().rotate(0.4, expand=False, fillcolor=255)
    untouched, angle = deskew_image(
        slightly_off, min_angle=0.75, method="projection"
    )
    assert angle == 0.0 and untouched is slightly_off
    fixed, angle = deskew_image(slightly_off, min_angle=0.05, method="projection")
    assert 0.2 <= abs(angle) <= 0.7, angle  # ~0.4 deg, within the absolute spec
    assert fixed is not slightly_off


@needs_pillow
def test_search_is_bounded_by_max_angle():
    skewed = _grid().rotate(4.0, expand=False, fillcolor=255)
    _out, angle = deskew_image(skewed, max_angle=1.0, method="projection")
    assert abs(angle) <= 1.0 + 1e-9


@needs_pillow
def test_blank_plate_is_not_a_signal():
    from PIL import Image

    blank = Image.new("L", (300, 300), 255)
    out, angle = deskew_image(blank, method="projection")
    assert angle == 0.0 and out is blank


@needs_pillow
def test_palette_and_small_images_are_handled():
    # A 300x200 plate leaves only a 75x50 working image, so this checks that
    # palette input survives the pipeline and corrects in the right direction;
    # sub-0.2-degree accuracy is asserted on the full-size plate above.
    img = _grid(w=300, h=200).convert("P")
    skewed = img.rotate(1.5, expand=False)
    out, angle = deskew_image(skewed, method="projection")
    _assert_frame_preserved(out, skewed)
    assert out.mode in ("RGB", "RGBA", "P")
    assert angle < 0  # the correction goes the way that levels the lines
    assert abs(angle - (-1.5)) < 0.75, angle
    assert _longest_flat_run(out) > _longest_flat_run(skewed)


@needs_pillow
def test_auto_method_produces_a_straight_plate():
    skewed = _grid(columns=10).rotate(2.0, expand=False, fillcolor=255)
    assert _longest_flat_run(skewed) < 60
    out, angle = deskew_image(skewed, method="auto")
    assert abs(angle) > 0.1  # the fast path found the skew
    assert _longest_flat_run(out) > 400


@needs_pillow
def test_noisy_yellowed_scan_is_still_corrected():
    """A photorealistic plate: yellowed paper, speckle noise, faint lines."""
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(20260920)
    img = Image.new("L", (700, 480))
    px = img.load()
    for y in range(480):  # uneven lighting, ~215-240 paper
        base = 240 - y // 16
        for x in range(700):
            px[x, y] = max(0, min(255, base + rng.randint(-6, 6)))
    draw = ImageDraw.Draw(img)
    for y in range(30, 470, 45):  # pencil-thin ruling lines
        draw.line((20, y, 680, y), fill=150, width=1)
    for x in range(20, 690, 80):  # the chart's vertical grid
        draw.line((x, 30, x, 465), fill=150, width=1)
    skewed = img.rotate(-1.5, expand=False, fillcolor=240)

    out, angle = deskew_image(skewed, method="projection")
    assert abs(angle - 1.5) < 0.3, angle
    assert _longest_flat_run(out) > _longest_flat_run(skewed)
    _assert_frame_preserved(out, skewed)


def test_hough_path_reports_none_without_opencv():
    # Never raises: cv2 is fully optional, and the projection path covers us.
    try:
        import cv2  # noqa: F401
        have_cv2 = True
    except Exception:
        have_cv2 = False
    if not PIL_AVAILABLE:
        pytest.skip("Pillow is an optional dependency")
    result = hough_skew_angle(_grid().rotate(2.0, expand=False, fillcolor=255))
    if have_cv2:
        assert result == pytest.approx(-2.0, abs=0.5)
    else:
        assert result is None


# ---------------------------------------------------------------------------
# deskew: import / error contract (must work WITHOUT Pillow installed)
# ---------------------------------------------------------------------------

def test_deskew_module_is_importable_without_pillow():
    # The lazy-import contract: rca_core must stay stdlib-only on machines
    # without Pillow, so nothing at module scope may touch it.
    import rca_core.deskew as mod

    assert callable(mod.deskew_image)
    assert mod.MIN_USEFUL_ANGLE > 0


class _NotAnImage:
    size = (10, 10)


def test_bad_arguments_raise_deskew_error():
    with pytest.raises(DeskewError):
        deskew_image(_NotAnImage())  # no .convert -> not a PIL image
    with pytest.raises(DeskewError):
        estimate_skew_angle(object())
    with pytest.raises(DeskewError):
        deskew_image(12345, max_angle=0.0)
    with pytest.raises(DeskewError):
        deskew_image(12345, downsample=0)
    with pytest.raises(DeskewError):
        deskew_image(12345, max_angle=5.0, min_angle=5.0)
    with pytest.raises(DeskewError):
        deskew_image(12345, axis="diagonal")
    with pytest.raises(DeskewError):
        deskew_image(12345, method="magic")
    with pytest.raises(DeskewError):
        deskew_image(12345, max_angle=90.0)


@needs_pillow
def test_parameter_errors_precede_the_pillow_call():
    with pytest.raises(DeskewError):
        deskew_image(_grid(), max_angle=-1.0)


@needs_pillow
def test_downsample_cap_bounds_the_work():
    big = _grid(w=2400, h=1600, step=25)
    skewed = big.rotate(1.0, expand=False, fillcolor=255)
    out, angle = deskew_image(skewed, downsample=8, max_side=320, method="projection")
    _assert_frame_preserved(out, skewed)  # full resolution in, whole frame out
    assert abs(angle - (-1.0)) < 0.25, angle
