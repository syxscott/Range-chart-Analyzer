"""The evidence chain must name checks that did NOT run.

The point of ``rca_core/degradations.py`` is not that a list exists -- it is
that the absence of matplotlib no longer looks identical to a successful
redraw verification. These tests prove:
  * the report always carries the key (even when everything is installed),
  * an entry appears when a dependency is missing, with the right code,
  * the other report content is byte-for-byte unaffected (additive change).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core import degradations, report  # noqa: E402


def _sample_report():
    return report.build_extraction_report(
        data={"species_ranges": [
            {"species": "Alpha", "range_top": 10.0, "range_base": 20.0}
        ]},
        mode="range_chart",
    )


def test_report_always_carries_degradations():
    rep = _sample_report()
    assert "degradations" in rep
    assert isinstance(rep["degradations"], list)
    # Still schema v2: the key is additive, older readers ignore it.
    assert rep["schema_version"] == report.REPORT_SCHEMA_VERSION == 2


def test_collect_degradations_reports_a_missing_dependency_with_impact(monkeypatch):
    monkeypatch.setattr(degradations, "_installed",
                        lambda name: name != "matplotlib")
    rows = degradations.collect_degradations()
    codes = [r["code"] for r in rows]
    assert "redraw.unavailable" in codes
    entry = next(r for r in rows if r["code"] == "redraw.unavailable")
    assert "matplotlib" in entry["reason"]
    assert "verification" in entry["affects"]
    assert entry["impact"], "the user must be told what was NOT verified"


def test_a_fully_provisioned_machine_reports_no_degradations(monkeypatch):
    monkeypatch.setattr(degradations, "_installed", lambda name: True)
    assert degradations.collect_degradations() == []


def test_degradations_is_additive_to_the_report(monkeypatch):
    """Other keys and their values must not change when the list grows."""
    monkeypatch.setattr(degradations, "_installed", lambda name: True)
    clean = _sample_report()
    monkeypatch.setattr(degradations, "_installed",
                        lambda name: name != "numpy")
    degraded = _sample_report()
    assert degraded["degradations"], "missing numpy must be visible"
    for key in clean:
        if key == "degradations":
            continue
        assert clean[key] == degraded[key], f"unexpected change in report.{key}"
