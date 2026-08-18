"""Regression test for top-level confidence merge parity (REVIEW-2026-08-17, P0-3).

Python ``rca_core.aggregate.merge_results`` weights each run's
confidence by its own ``runs`` field (so a run that itself aggregated
3 sub-attempts counts 3x in the average). JS ``js/aggregate.js`` used
plain ``sum/cnt`` and ignored the per-run ``runs`` field, breaking
parity whenever the runs had different ``runs`` values.

This test pins both sides to the SAME weighted-mean formula so the
parity is restored.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core.aggregate import merge_results, RANGE_CHART_SCHEMA  # noqa: E402


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_AGGREGATE = os.path.join(PROJECT_ROOT, 'js', 'aggregate.js')


def _js_merge(runs: list, total_runs, schema_key: str = 'range_chart') -> dict:
    """Run JS rcaMergeResults via node and return its parsed JSON output."""
    runs_json = json.dumps(runs)
    schema_key_json = json.dumps(schema_key)
    total_json = 'null' if total_runs is None else str(total_runs)
    agg_path = repr(JS_AGGREGATE)

    postamble = (
        "\nglobalThis.RCA_DEFAULT_KEYMAP = RCA_DEFAULT_KEYMAP;\n"
        "globalThis.RCA_COLUMNAR_KEYMAP = RCA_COLUMNAR_KEYMAP;\n"
        "globalThis.RCA_ABUNDANCE_KEYMAP = RCA_ABUNDANCE_KEYMAP;\n"
        "globalThis.rcaMergeResults = rcaMergeResults;\n"
    )
    script = (
        "const vm=require('vm');"
        "const fs=require('fs');"
        "const code=fs.readFileSync(" + agg_path + ",'utf8') + " + repr(postamble) + ";"
        "const ctx={console:console};"
        "vm.createContext(ctx);"
        "new vm.Script(code).runInContext(ctx);"
        "const KEYMAP_BY_MODE = {"
        + "  range_chart: ctx.RCA_DEFAULT_KEYMAP,"
        + "  columnar_section: ctx.RCA_COLUMNAR_KEYMAP,"
        + "  abundance_diagram: ctx.RCA_ABUNDANCE_KEYMAP,"
        + "};"
        "const km = KEYMAP_BY_MODE[" + schema_key_json + "] || ctx.RCA_DEFAULT_KEYMAP;"
        "const r = ctx.rcaMergeResults(" + runs_json + ", " + total_json + ", km);"
        "console.log(JSON.stringify(r));"
    )
    result = subprocess.run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=30,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f'node error: {result.stderr}')
    return json.loads(result.stdout)


def test_confidence_weighted_by_per_run_runs_field_parity():
    """Two runs with DIFFERENT per-run ``runs`` weights must agree.

    Run A: confidence=0.9, self-reported runs=3 → counts 3x
    Run B: confidence=0.5, self-reported runs=1 → counts 1x
    Weighted mean = (0.9*3 + 0.5*1) / 4 = 3.2/4 = 0.8
    Unweighted mean = (0.9 + 0.5) / 2 = 0.7

    Python implements weighted, JS used to implement unweighted. Both
    must agree on the weighted value after the fix.
    """
    r1 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.9, 'runs': 3,
    }
    r2 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.5, 'runs': 1,
    }
    py = merge_results([r1, r2], total_runs=2, schema=RANGE_CHART_SCHEMA)
    js = _js_merge([r1, r2], 2)

    assert abs(py['confidence'] - 0.8) < 1e-6, (
        f'python weighted mean should be 0.8, got {py["confidence"]}'
    )
    assert abs(js['confidence'] - 0.8) < 1e-6, (
        f'js confidence should be 0.8 (weighted, parity with python), got {js["confidence"]}'
    )
    assert py['confidence'] == js['confidence'], (
        f'parity broken: py={py["confidence"]} js={js["confidence"]}'
    )


def test_confidence_skips_runs_with_missing_runs_field():
    """When a run has no ``runs`` field, the weight defaults to 1 (Python's
    fallback). JS must mirror this."""
    r1 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.9, 'runs': 2,
    }
    r2 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.5,  # no runs field → weight=1
    }
    py = merge_results([r1, r2], total_runs=2, schema=RANGE_CHART_SCHEMA)
    js = _js_merge([r1, r2], 2)

    # weighted = (0.9*2 + 0.5*1) / 3 = 2.3/3 ≈ 0.7667
    expected = (0.9 * 2 + 0.5 * 1) / 3
    # Python rounds to 4 decimal places (see rca_core/aggregate.py), so
    # compare at the same precision rather than strict 1e-6 equality.
    assert abs(py['confidence'] - round(expected, 4)) < 1e-4
    assert abs(js['confidence'] - round(expected, 4)) < 1e-4
    assert py['confidence'] == js['confidence']
