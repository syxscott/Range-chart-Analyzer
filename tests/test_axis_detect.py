"""The geometry-engine PoC must MEASURE, not guess -- so it is tested on
synthetic figures where the true tick pixels are known exactly.

Every assertion here has a closed-form expected value. That matters: the whole
claim of the FigDataX route is "a deterministic measurer beats a vision model
on numeric read-off", and that claim is only worth anything if the measurement
is verified against ground truth rather than against a second model.
"""
from __future__ import annotations

import base64
import io
import math
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core.axis_detect import (  # noqa: E402
    AxisDetectError,
    calibration_from_ticks,
    detect_axis_ticks,
    trace_plot_curve,
)
from rca_core.geometry import CalibrationError  # noqa: E402

Image = pytest.importorskip("PIL.Image", reason="Pillow is an optional dependency")
ImageDraw = pytest.importorskip("PIL.ImageDraw")


W, H = 420, 400
AXIS_X = 50
AXIS_W = 3
AXIS_TOP, AXIS_BOTTOM = 20, 380
TICK_TOP, TICK_STEP, TICK_COUNT = 30, 50, 8
TICK_LEN = 14
CURVE_X0, CURVE_X1 = 80, 350


def _truth_ticks() -> list[int]:
    return [TICK_TOP + i * TICK_STEP for i in range(TICK_COUNT)]


def _truth_curve_y(x: int) -> float:
    return 200.0 + 60.0 * math.sin((x - CURVE_X0) / 40.0)


def _draw(*, inverted: bool = False, with_curve: bool = True) -> str:
    # "L" mode takes a single 0..255 value, not an RGB tuple.
    bg, ink = (0, 255) if inverted else (255, 0)
    img = Image.new("L", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([AXIS_X, AXIS_TOP, AXIS_X + AXIS_W - 1, AXIS_BOTTOM], fill=ink)
    for y in _truth_ticks():
        d.rectangle([AXIS_X, y, AXIS_X + TICK_LEN - 1, y + 2], fill=ink)
    if with_curve:
        pts = [(x, _truth_curve_y(x)) for x in range(CURVE_X0, CURVE_X1 + 1)]
        d.line(pts, fill=ink, width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _blank() -> str:
    img = Image.new("L", (120, 120), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_detects_every_tick_at_the_right_pixel():
    got = detect_axis_ticks(_draw(), axis="vertical")
    assert len(got) == TICK_COUNT, f"expected {TICK_COUNT} ticks, got {got}"
    truth = _truth_ticks()
    for expected, actual in zip(truth, got):
        # A tick is 3 px tall and the centre of its run is the measurement;
        # 2 px is the tightest bound that is still independent of rounding.
        assert abs(actual - expected) <= 2, (
            f"tick at y={expected} measured as y={actual} (off by "
            f"{abs(actual - expected)} px); all={got}")


def test_ticks_are_ascending_and_unique():
    got = detect_axis_ticks(_draw(), axis="vertical")
    assert got == sorted(got)
    assert len(got) == len(set(got))


def test_dark_background_figure_is_detected_too():
    """Ink-vs-paper is decided by the histogram, not assumed to be dark-on-light."""
    got = detect_axis_ticks(_draw(inverted=True), axis="vertical")
    assert len(got) == TICK_COUNT, got


def test_blank_figure_reports_no_ticks_rather_than_inventing_them():
    assert detect_axis_ticks(_blank(), axis="vertical") == []


def test_undecodable_payload_is_an_error_not_an_empty_result():
    """``[]`` means "I looked and found none"; a corrupt payload is different."""
    with pytest.raises(AxisDetectError):
        detect_axis_ticks("this is not an image", axis="vertical")
    with pytest.raises(AxisDetectError):
        detect_axis_ticks("", axis="vertical")


def test_horizontal_axis_is_measured_in_the_transposed_frame():
    ticks = detect_axis_ticks(_draw(with_curve=False), axis="vertical")
    assert len(ticks) == TICK_COUNT
    # Sanity: asking for a horizontal axis on the same figure must not
    # return the same numbers by accident -- it measures a different thing.
    horizontal = detect_axis_ticks(_draw(with_curve=False), axis="horizontal")
    assert horizontal != ticks


def test_curve_trace_matches_the_synthetic_function():
    points = trace_plot_curve(_draw(), axis="vertical", samples=48)
    assert len(points) >= 20, f"trace produced only {len(points)} points"
    inside = [p for p in points if CURVE_X0 + 4 <= p["x"] <= CURVE_X1 - 4]
    assert len(inside) >= 15
    worst = max(abs(p["y"] - _truth_curve_y(int(p["x"]))) for p in inside)
    # 2 px line width + median vs. centre: 4 px is the honest bound.
    assert worst <= 4.0, f"worst curve error {worst:.1f} px over {len(inside)} samples"


def test_curve_trace_reports_coverage_instead_of_interpolating_gaps():
    """Left of the curve there is ink only on the axis; those columns are
    skipped, so the trace must not start at x=0."""
    points = trace_plot_curve(_draw(), axis="vertical", samples=48)
    assert points, "trace is empty"
    assert min(p["x"] for p in points) > AXIS_X + 1


def test_fit_from_detected_ticks_recovers_the_true_scale():
    ticks = detect_axis_ticks(_draw(), axis="vertical")
    # Ground truth: the top tick (y=30) is 100 Ma, the bottom (y=380) is 0 Ma.
    values = [100.0 - (y - TICK_TOP) / (TICK_STEP * (TICK_COUNT - 1)) * 100.0
              for y in _truth_ticks()]
    fit = calibration_from_ticks(ticks, values)
    assert fit["n"] == TICK_COUNT
    # slope in data units per pixel; 100 Ma over 350 px.
    assert abs(fit["slope"] - (-100.0 / (TICK_STEP * (TICK_COUNT - 1)))) < 1e-3
    assert fit["max_abs_residual"] < 1.5, fit


def test_fit_rejects_a_mismatched_value_list():
    with pytest.raises(ValueError):
        calibration_from_ticks([10, 20, 30], [1.0, 2.0])


def test_fit_rejects_a_degenerate_set_with_the_same_error_as_the_manual_path():
    with pytest.raises(CalibrationError):
        calibration_from_ticks([10], [1.0])
    with pytest.raises(CalibrationError):
        calibration_from_ticks([10, 10], [1.0, 2.0])


def test_no_dependency_on_the_model_layer():
    """This is the whole point of the PoC: the measurer must not need an LLM."""
    src = (ROOT / "rca_core" / "axis_detect.py").read_text(encoding="utf-8")
    for forbidden in ("call_llm_api", "LlmProvider", "extract(", "prompt"):
        assert forbidden not in src, (
            f"axis_detect.py references {forbidden!r}; the deterministic "
            "measurer must not depend on the model path")
