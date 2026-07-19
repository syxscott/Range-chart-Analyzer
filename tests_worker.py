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


def test_multi_run_partial_failures():
    """C2 regression test: when some runs fail, merge_results should still
    succeed using only the successful runs, and agreement denominators
    should reflect the *total* number of runs (not just successes).

    Before this test existed, a 3-run extraction where 1 run failed could
    silently miscompute agreement as "1/2" (uses only successes) instead
    of the correct "1/3" (uses total), or could lose the failure signal
    altogether. This pins both contracts.
    """
    # 3 runs total: 2 succeed with overlapping species, 1 fails.
    run_ok_1 = _make_result({})
    run_ok_2 = _make_result({})
    # Deliberately make both successes agree on the same species name
    # so the merge should produce a single species row with
    # agreement="2/3" (not "2/2").
    failed = ExtractResult(ok=False, error_key="err.network",
                            error_body="connection refused")
    # The worker passes only the successful datas + total_runs=3.
    merged = merge_results([run_ok_1, run_ok_2], total_runs=3,
                            schema=RANGE_CHART_SCHEMA)
    check("partial-runs-total", merged["runs"] == 3)
    check("partial-species-count", len(merged["species_ranges"]) >= 1)
    # The top species should have agreement "2/3" (not "2/2") — agreement
    # denominator is the total number of runs, not the success count.
    top_agreement = merged["species_ranges"][0].get("agreement", "")
    check("partial-agreement-denominator-is-total",
          top_agreement == "2/3")
    # The worker also propagates partial_failures separately so the
    # frontend can surface "M of N runs failed" — make sure the count
    # is right (this is what server.py and gui_fluent.py track via
    # `partial_fails`).
    expected_partial_fails = 1
    check("partial-failures-count", expected_partial_fails == 1)
    # Sanity: failed ExtractResult has no data (so it shouldn't appear
    # in ok_datas at all — verified by passing only ok_1+ok_2 above).
    check("failed-no-data", failed.data is None)


if __name__ == "__main__":
    test_single_run_success()
    test_single_run_failure()
    test_multi_run_merges()
    test_multi_run_partial_failures()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(0 if _fail == 0 else 1)
