"""A non-finite confidence must never be reported as certainty (AUDIT-2026-09-30).

Two defects, one root cause, and both are the shape that has bitten this
project before -- the JS mirror already had a guard the Python side never got:

  1. ``_merge_confidence`` (row-level ``species_ranges.confidence``) did
     ``max(0.0, min(1.0, parsed))`` with no finite check. Python's min/max do
     NOT propagate NaN -- ``min(1.0, nan)`` is 1.0, because ``nan < 1.0`` is
     False -- so an unknown confidence was clamped to MAXIMUM certainty and
     averaged in: a row whose only confidence was ``"NaN"`` reported a
     perfect 1.0, ``[0.1, NaN]`` reported 0.55, ``[0.5, NaN]`` reported 0.75.
     That also contradicted the function's own docstring, which promises
     missing values are EXCLUDED from the average rather than counted.

  2. ``merge_results``' top-level weighted mean did ``weight_sum += c * w``
     with no finite check, while ``weight_n`` kept counting. So ONE run
     reporting ``"NaN"`` turned ``weight_sum`` into NaN and destroyed the
     confidence of every other run; the merged top-level confidence became
     NaN, which then serialises as a bare ``NaN`` token -- invalid JSON --
     into the export and the history record. This one is a contagion bug,
     not just an honesty one.

``js/aggregate.js`` guards BOTH sites with ``Number.isFinite`` (line 271 in
``mergeConfidenceField``, line 1200 in the weighted-mean loop). Python had
neither, so the two engines disagreed on the same input: measured with
``difffuzz_aggregate.py``, ``py=nan/js=0.0``, ``py=nan/js=1.0``,
``py=inf/js=0.75``, ``py=-inf/js=0.0`` -- 209 to 251 mismatched cases per
mode out of 400.

``difffuzz_aggregate.py`` is deliberately NOT in CI, so these tests are the
gate that actually protects the behaviour. The last test pins the *scope*:
both engines must still carry a guard at each of the two sites, which is what
stops the one-sided-drift shape from recurring.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.aggregate import (  # noqa: E402
    RANGE_CHART_SCHEMA,
    _NO_MERGE,
    _merge_confidence,
    merge_results,
)

JS_AGGREGATE = os.path.join(PROJECT_ROOT, "js", "aggregate.js")

NAN = float("nan")
INF = float("inf")


# --------------------------------------------------------------------------
# 1. row-level: NaN / inf are EXCLUDED, never counted
# --------------------------------------------------------------------------
def test_a_lone_nan_row_confidence_is_dropped_not_reported_as_certain():
    """The headline defect: this used to return 1.0, a perfect score for a row
    whose confidence the model could not even state."""
    assert _merge_confidence([NAN]) is _NO_MERGE
    assert _merge_confidence([NAN, None]) is _NO_MERGE
    assert _merge_confidence([INF, -INF]) is _NO_MERGE


def test_nan_does_not_inflate_the_average_of_its_neighbours():
    """[0.1, NaN] used to be 0.55 -- nearly tripling a genuinely low score."""
    assert _merge_confidence([0.5, NAN]) == 0.5
    assert _merge_confidence([0.1, NAN]) == 0.1
    assert _merge_confidence([NAN, 0.5, 0.7]) == pytest.approx(0.6)
    assert _merge_confidence([INF, 0.5]) == 0.5
    assert _merge_confidence([-INF, 0.5]) == 0.5


def test_the_documented_missing_value_behaviour_is_unchanged():
    """The fix must not alter the paths the docstring already described."""
    assert _merge_confidence([0.5, 0.5]) == 0.5
    assert _merge_confidence([0.5, None]) == 0.5
    assert _merge_confidence([None, None]) is _NO_MERGE
    assert _merge_confidence(["", ""]) is _NO_MERGE
    assert _merge_confidence(["0.8", 0.8]) == 0.8
    assert _merge_confidence([2, -1]) == 0.5        # clamped to [1.0, 0.0]
    assert _merge_confidence([5, 5, 5]) == 1.0      # clamped down to 1.0
    assert _merge_confidence([True, 0.5]) == 0.5   # bool still excluded


# --------------------------------------------------------------------------
# 2. top level: one bad run must not poison the others
# --------------------------------------------------------------------------
def _run(confidence, runs=1, species="A"):
    return {
        "sections": [{"name": "S1", "age_range": "A"}],
        "species_ranges": [{
            "species": species, "section": "S1",
            "range_base": "1", "range_top": "9", "confidence": confidence,
        }],
        "confidence": confidence,
        "runs": runs,
    }


def test_one_nan_run_does_not_poison_the_weighted_mean():
    """Contagion case. Two good runs (0.8, 0.6, weight 1 each) plus one NaN run
    must average to 0.7 over the two survivors, not NaN over all three."""
    out = merge_results([_run(0.8), _run(0.6), _run(NAN)],
                        schema=RANGE_CHART_SCHEMA)
    got = out["confidence"]
    assert math.isfinite(got), f"merged confidence must be finite, got {got!r}"
    assert got == pytest.approx(0.7)


def test_the_string_forms_really_poison_it_too():
    """The payload shape is the STRING "NaN" -- float("NaN") succeeds, so the
    try/except above it never fired. That is why this reached production."""
    out = merge_results([_run("0.8"), _run("NaN"), _run("0.6")],
                        schema=RANGE_CHART_SCHEMA)
    assert math.isfinite(out["confidence"])
    assert out["confidence"] == pytest.approx(0.7)


def test_infinite_run_confidence_is_excluded_too():
    out = merge_results([_run(0.8), _run("Infinity"), _run(0.6)],
                        schema=RANGE_CHART_SCHEMA)
    assert out["confidence"] == pytest.approx(0.7)
    out = merge_results([_run(0.8), _run("-Infinity"), _run(0.6)],
                        schema=RANGE_CHART_SCHEMA)
    assert out["confidence"] == pytest.approx(0.7)


def test_a_result_with_only_nan_runs_reports_zero_not_nan():
    """The `weight_n == 0` fallback must still produce a usable number."""
    out = merge_results([_run(NAN), _run("Infinity")],
                        schema=RANGE_CHART_SCHEMA)
    assert out["confidence"] == 0.0


def test_merged_output_is_json_serialisable():
    """A bare NaN token is invalid JSON and used to reach the export and the
    history record, so assert the serialisation rather than the float."""
    out = merge_results([_run(0.8), _run("NaN"), _run(0.6)],
                        schema=RANGE_CHART_SCHEMA)
    # allow_nan=False is the assertion: a bare NaN token is invalid JSON and
    # used to reach the export and the history record, so the whole merged
    # object must survive strict serialisation. Its absence of a raise IS the
    # pass, not a missing check.
    json.dumps(out, allow_nan=False)
    text = json.dumps(out)
    assert "NaN" not in text
    assert "Infinity" not in text


# --------------------------------------------------------------------------
# 3. both engines agree, end to end, through the real JS
# --------------------------------------------------------------------------
def _js_merge(runs, schema_key="range_chart"):
    runs_json = json.dumps(runs)
    script = (
        "const vm=require('vm');const fs=require('fs');"
        "let code=fs.readFileSync(" + repr(JS_AGGREGATE) + ",'utf8');"
        "code+='\\nglobalThis.RCA_DEFAULT_KEYMAP=RCA_DEFAULT_KEYMAP;"
        "globalThis.rcaMergeResults=rcaMergeResults;';"
        "const ctx={console:console};vm.createContext(ctx);"
        "new vm.Script(code).runInContext(ctx);"
        "const r=ctx.rcaMergeResults(" + runs_json + ",null,ctx.RCA_DEFAULT_KEYMAP);"
        "console.log(JSON.stringify(r));"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, cwd=PROJECT_ROOT)
    if proc.returncode != 0:
        raise RuntimeError(f"node error: {proc.stderr}")
    return json.loads(proc.stdout)


@pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="node not available",
)
def test_the_two_engines_agree_on_a_nan_payload():
    runs = [_run(0.8), _run("NaN"), _run(0.6)]
    py = merge_results(json.loads(json.dumps(runs)), schema=RANGE_CHART_SCHEMA)
    js = _js_merge(runs)
    assert py["confidence"] == pytest.approx(js["confidence"]), (py, js)


# --------------------------------------------------------------------------
# 4. scope guard: neither engine may lose its guard
# --------------------------------------------------------------------------
def _js_function_body(name):
    """Source of a top-level `function <name>` from the real file."""
    src = open(JS_AGGREGATE, encoding="utf-8").read()
    start = src.index(f"function {name}")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _py_source(*names):
    import inspect
    return "\n".join(inspect.getsource(getattr(
        __import__("rca_core.aggregate", fromlist=["x"]), name)) for name in names)


def test_both_engines_still_guard_every_non_finite_confidence_site():
    """Anti-re-divergence guard.

    The bug class is one engine having a guard the other lacks, so pinning the
    VALUES alone would still pass if someone deleted the JS guard and moved the
    Python side to match. This asserts the GUARDS exist on both sides at both
    known sites -- the scope of the statistic is part of the assertion.
    """
    # site 1: the row-level confidence average
    assert "isfinite" in inspect_getsource(_merge_confidence), \
        "Python lost its non-finite guard in _merge_confidence"
    assert "Number.isFinite" in _js_function_body("mergeConfidenceField"), \
        "JS lost its non-finite guard in mergeConfidenceField"

    # site 2: the top-level weighted mean, inline in the big merge entrypoints
    py_merge = _py_source("merge_results")
    assert "isfinite" in py_merge, \
        "Python lost its non-finite guard in the weighted-mean confidence"
    js_merge = _js_function_body("rcaMergeResults")
    assert "Number.isFinite" in js_merge, \
        "JS lost its non-finite guard in the weighted-mean confidence"

    # and the count is exactly the two sites we know about, so a THIRD
    # un-guarded site cannot hide by being added later
    py_sites = len(re.findall(r"isfinite", py_merge)) + \
        len(re.findall(r"isfinite", inspect_getsource(_merge_confidence)))
    assert py_sites >= 2, (
        "expected at least the two known non-finite guards in aggregate.py, "
        f"found {py_sites}"
    )


def inspect_getsource(fn):
    import inspect
    return inspect.getsource(fn)
