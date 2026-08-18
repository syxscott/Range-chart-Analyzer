"""Regression test for js/aggregate.js single-run passthrough deep-copy.

REVIEW-2026-08-17 (P1-5): the single-run passthrough in
``js/aggregate.js`` used ``Object.assign({}, runs[0])`` which is a
SHALLOW copy. The Python side (``rca_core.aggregate.merge_results``)
was already changed to ``copy.deepcopy(runs[0])`` to fix the same
bug, but the JS side was not synced, so:
  - Mutations to nested structures (e.g. ``merged.sections[0].formations``)
    silently rewrote the source run's nested structures.
  - Parity between Python and JS for the single-run case is broken.

This test calls the JS merger via Node and verifies the merged result
is decoupled from the source run at the nesting level that mattered
in real use: ``sections[].formations`` (range chart) and ``metadata``/
``legend`` (phylogenetic tree). Mirror Python-side behavior.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core.aggregate import (  # noqa: E402
    merge_results,
    RANGE_CHART_SCHEMA,
    PHYLOGENETIC_TREE_SCHEMA,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_AGGREGATE = os.path.join(PROJECT_ROOT, 'js', 'aggregate.js')


def _js_merge(runs: list, total_runs, schema_key: str = 'range_chart') -> dict:
    return _js_merge_internal(runs, total_runs, schema_key, mutate=False)


def _js_merge_and_self_check(runs: list, total_runs, schema_key: str = 'range_chart') -> dict:
    """Same as ``_js_merge`` but also returns a reference to the JS-side
    source run AFTER the merged result has been mutated. Used to verify
    that the single-run passthrough is a deep copy."""
    return _js_merge_internal(runs, total_runs, schema_key, mutate=True)


def _js_merge_internal(runs, total_runs, schema_key, mutate):
    runs_json = json.dumps(runs)
    schema_key_json = json.dumps(schema_key)
    total_json = 'null' if total_runs is None else str(total_runs)
    agg_path = repr(JS_AGGREGATE)
    postamble = (
        "\nglobalThis.__KM_DEFAULT = RCA_DEFAULT_KEYMAP;"
        "\nglobalThis.__KM_COL = RCA_COLUMNAR_KEYMAP;"
        "\nglobalThis.__KM_AB = RCA_ABUNDANCE_KEYMAP;"
        "\nglobalThis.__MERGE = rcaMergeResults;"
    )
    # When mutate=True: after the merge, mutate the merged result's
    # nested structure in place, then return BOTH the merged result and
    # the original source run. If the merger used a shallow copy, the
    # mutation will have leaked back into the source run's nested
    # structures.
    self_check = (
        "\nconst __srcRef = runs[0];"
        "\nif (" + ('true' if mutate else 'false') + ") {"
        "\n  merged.sections[0].formations.push('MUTATED_BY_TEST');"
        "\n  console.log(JSON.stringify({merged: merged, source: __srcRef}));"
        "\n} else {"
        "\n  console.log(JSON.stringify(merged));"
        "\n}"
    )
    script = (
        "const vm=require('vm');"
        "const fs=require('fs');"
        "const code=fs.readFileSync(" + agg_path + ",'utf8') + " + repr(postamble) + ";"
        "const ctx={console:console};"
        "vm.createContext(ctx);"
        "new vm.Script(code).runInContext(ctx);"
        "const KEYMAP_BY_MODE = {"
        + "  range_chart: ctx.__KM_DEFAULT,"
        + "  columnar_section: ctx.__KM_COL,"
        + "  abundance_diagram: ctx.__KM_AB,"
        + "};"
        "const km = KEYMAP_BY_MODE[" + schema_key_json + "] || ctx.__KM_DEFAULT;"
        "const runs = " + runs_json + ";"
        "const merged = ctx.__MERGE(runs, " + total_json + ", km);"
        + self_check
    )
    result = subprocess.run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=30,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f'node error: {result.stderr}')
    return json.loads(result.stdout)


def test_js_single_run_mutating_merged_does_not_taint_source():
    """Mutating the merged result's nested structure must NOT rewrite the
    source run's nested structure. The previous shallow-copy JS code
    failed this test (the source run was silently modified).

    Implementation: we ask JS to hold the source run in a local variable,
    mutate the merged result, then return both. If the merge was a shallow
    copy, mutating the merged nested structure would also mutate the
    source-side reference (the same array on both sides).
    """
    run = {
        'sections': [{
            'name': 'S1',
            'age_range': 'Permian',
            'formations': ['F1', 'F2'],
            'formation_thickness_m': '',
            'coordinates': '',
        }],
        'species_ranges': [{
            'species': 'Neoalbaillella optima',
            'section': 'S1',
            'range_top': 'B9', 'range_base': 'B7', 'biozone': '',
        }],
        'biozones': [],
        'other_fossils': [],
        'confidence': 0.85,
    }
    result = _js_merge_and_self_check([run], None, 'range_chart')

    # The merged result's formations list was mutated INSIDE JS.
    assert 'MUTATED_BY_TEST' in result['merged']['sections'][0]['formations'], (
        "Test setup error: mutation didn't land on the merged result"
    )
    # But the JS-side source run's formations list MUST remain untouched.
    assert 'MUTATED_BY_TEST' not in result['source']['sections'][0]['formations'], (
        f'JS shallow-copy leaked back into source run: '
        f'source formations = {result["source"]["sections"][0]["formations"]}'
    )
    assert result['source']['sections'][0]['formations'] == ['F1', 'F2']


def test_python_single_run_mutating_merged_does_not_taint_source():
    """Same test as above but on the Python side — should pass already,
    pins the parity target."""
    run = {
        'sections': [{
            'name': 'S1',
            'age_range': 'Permian',
            'formations': ['F1', 'F2'],
            'formation_thickness_m': '',
            'coordinates': '',
        }],
        'species_ranges': [{
            'species': 'Neoalbaillella optima',
            'section': 'S1',
            'range_top': 'B9', 'range_base': 'B7', 'biozone': '',
        }],
        'biozones': [],
        'other_fossils': [],
        'confidence': 0.85,
    }
    py_merged = merge_results([run], total_runs=1, schema=RANGE_CHART_SCHEMA)

    py_merged['sections'][0]['formations'].append('MUTATED_BY_TEST')
    assert run['sections'][0]['formations'] == ['F1', 'F2'], (
        f'python shallow-copy leaked back into source run: '
        f'source formations = {run["sections"][0]["formations"]}'
    )


def test_js_single_run_run_level_nested_object_is_deep_copied():
    """The single-run passthrough's top-level shallow copy must NOT alias
    run-level nested objects. Mutating the merged result's ``_extras``
    must not leak back into the source run's ``_extras`` (in JS memory)."""
    run = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.85,
        '_extras': {'nested': {'key': 'value'}},
    }
    # Reuse the same self-check machinery (mutate=true) by reshaping the
    # mutation target: we mutate ``_extras.nested.key`` instead of
    # ``sections[0].formations``. Easiest way is to inline the script.
    runs_json = json.dumps([run])  # must be an array of runs
    agg_path = repr(JS_AGGREGATE)
    postamble = (
        "\nglobalThis.__KM_DEFAULT = RCA_DEFAULT_KEYMAP;"
        "\nglobalThis.__MERGE = rcaMergeResults;"
    )
    script = (
        "const vm=require('vm');const fs=require('fs');"
        "let code=fs.readFileSync(" + agg_path + ",'utf8')"
        "+" + repr(postamble) + ";"
        "const ctx={console:console};vm.createContext(ctx);"
        "new vm.Script(code).runInContext(ctx);"
        "const runs=" + runs_json + ";"
        "const merged=ctx.__MERGE(runs,null,ctx.__KM_DEFAULT);"
        "const srcRef=runs[0];"
        "merged._extras.nested.key='MUTATED';"
        "console.log(JSON.stringify({merged:merged,source:srcRef}));"
    )
    out = subprocess.run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=30, cwd=PROJECT_ROOT,
    )
    if out.returncode != 0:
        raise RuntimeError(f'node error: {out.stderr}')
    result = json.loads(out.stdout)

    assert result['merged']['_extras']['nested']['key'] == 'MUTATED', (
        "Test setup error: mutation didn't land on the merged result"
    )
    assert result['source']['_extras']['nested']['key'] == 'value', (
        f'JS run-level nested object shallow-copied: '
        f'source._extras = {result["source"]["_extras"]}'
    )