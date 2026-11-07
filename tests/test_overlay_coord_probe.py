"""Pin the G2 decision input: pos_0_999 is normalised, not a pixel.

The probe in scripts/audit/overlay_coord_probe.py answers a question that a
10-24 person-day decision (docs/OWNER_DECISIONS.md G2) depends on. Its answer
is a FACT ABOUT THE DATA, so it must not silently go stale: if a future prompt
adds pixel anchors, or if the linear mapping stops holding, this test fails and
forces G2 to be reopened instead of letting the audit document rot.

See docs/OVERLAY-COORD-AUDIT.md for the measured numbers and the reasoning.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit.overlay_coord_probe import probe  # noqa: E402


@pytest.fixture(scope="module")
def report():
    fixture_dir = ROOT / "tests" / "fixtures" / "real_payloads"
    if not fixture_dir.is_dir():
        pytest.skip("real_payloads fixtures are not present in this checkout")
    return probe(fixture_dir)


def test_the_probe_actually_measured_something(report):
    """A vacuous pass would be a conclusion with no data behind it."""
    assert report["files"] >= 5, report
    assert report["files_with_calibration"] >= 2, report
    assert report["files_with_both"] >= 2, report
    assert report["linear_checks_with_declared_value"] >= 20, report


def test_pos_to_data_value_is_exact_linear_arithmetic(report):
    assert report["max_abs_residual"] is not None
    assert report["max_abs_residual"] < 0.01, (
        "pos_0_999 no longer maps linearly onto the calibration end values; "
        f"max |residual| = {report['max_abs_residual']!r}. Re-measure before "
        "trusting anything downstream of pos.")
    assert report["pos_maps_to_data_value_linearly"] is True


def test_axis_calibration_carries_no_pixel_anchors(report):
    """This is the fact G2 is blocked on.

    If it flips, a TRUE overlay became possible and G2 should be revisited --
    so fail loudly rather than let docs/OVERLAY-COORD-AUDIT.md contradict the
    data.
    """
    assert report["pixel_anchor_hits"] == [], (
        "axis_calibration now exposes pixel anchors "
        f"({report['pixel_anchor_hits']}). That changes the G2 trade-off: a "
        "true pos -> pixel overlay is now possible. Update "
        "docs/OVERLAY-COORD-AUDIT.md and reopen docs/OWNER_DECISIONS.md G2.")
    assert report["pos_maps_to_pixel"] is False


def test_conclusion_states_the_half_true_overlay_is_available_now(report):
    conclusion = report["conclusion"] or ""
    assert "NORMALISED" in conclusion or "normalised" in conclusion
    assert "pixel" in conclusion.lower()
