"""T4: ExtractWorker-equivalent logic tests (no QThread/QApplication).

The real ExtractWorker just wraps rca_core.extract() + ThreadPoolExecutor
+ emits via Qt signals which need a display. Test the underlying logic
(extract, normalize, merge) the same way the web frontend does — fully
headless and strongly validates the worker's contract.
"""
from __future__ import annotations

import os, sys

root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, root)

from rca_core.extractor import ExtractResult, normalize_result
from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results

_pass = 0
_fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)


# --- single-run contract ---
def _fake_extract(params, mode="range_chart", ok=True, error_key=None):
    """Stands in for the real network call inside the worker."""
    if ok:
        return ExtractResult(ok=True, data=_make_result(params), raw="{'a':1}",
                             truncated=False)
    return ExtractResult(ok=False, error_key=error_key or "err.http",
                         raw="", status=None)


def _make_result(params):
    return normalize_result({
        "sections": [{"name": "A", "age_range": "P", "formations": ["F1"],
                      "formation_thickness_m": "", "coordinates": ""}],
        "species_ranges": [
            {"species": "Sp x", "section": "A", "range_base": "1",
             "range_top": "2", "biozone": "Z"},
        ],
        "biozones": [{"name": "Z1", "age": "L", "thickness_m": "3"}],
        "other_fossils": ["Amm: X"],
        "confidence": 0.5,
    })


def test_single_run_success():
    r = _fake_extract({}, ok=True)
    check("single-success-ok", r.ok)
    check("single-success-data", r.data is not None and r.data["confidence"] == 0.5)


def test_single_run_failure():
    r = _fake_extract({}, ok=False, error_key="err.429")
    check("single-failure-ok", not r.ok)
    check("single-failure-errkey", r.error_key == "err.429")


def test_multi_run_merges():
    """How the worker merges ok_datas from N runs (what server.py + gui.py do)."""
    run_1 = _make_result({})
    run_2 = _make_result({})
    run_2["species_ranges"].append({"species": "Sp y", "section": "A",
                                     "range_base": "3", "range_top": "4",
                                     "biozone": ""})
    merged = merge_results([run_1, run_2], total_runs=2,
                           schema=RANGE_CHART_SCHEMA)
    check("multi-runs-2", merged["runs"] == 2)
    check("multi-species-2", len(merged["species_ranges"]) == 2)
    check("multi-top-agreement",
          merged["species_ranges"][0]["agreement"] == "2/2")


if __name__ == "__main__":
    test_single_run_success()
    test_single_run_failure()
    test_multi_run_merges()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(0 if _fail == 0 else 1)
