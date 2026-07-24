"""Python ↔ JS aggregate parity tests.

Runs identical inputs through both:
  - Python: rca_core.aggregate.merge_results
  - JS:     js/aggregate.js  (loaded via node vm)

and verifies that the merged output structure is identical. When the
two sides disagree on a known divergence, the test pins which side
produces the correct value (matches the JS path, which is the
canonical browser-direct/proxy behaviour).

Run:  python -m pytest tests/test_aggregate_parity.py -v
       python tests/test_aggregate_parity.py
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core.aggregate import (  # noqa: E402
    merge_results,
    ABUNDANCE_DIAGRAM_SCHEMA,
    COLUMNAR_SECTION_SCHEMA,
)


# ---------------------------------------------------------------------------
# JS runner
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_AGGREGATE = os.path.join(PROJECT_ROOT, 'js', 'aggregate.js')


def _js_merge(runs: list, total_runs, schema_key: str = 'range_chart') -> dict:
    """Run JS rcaMergeResults via node and return its parsed JSON output.

    Auto-detection in JS picks the keymap from shape. We pass a fixed
    ``schema_key`` to keep both sides on the same schema. Range-chart is
    the default; columnar/abundance need explicit keymap selection.
    """
    runs_json = _json.dumps(runs)
    schema_key_json = _json.dumps(schema_key)
    total_json = 'null' if total_runs is None else str(total_runs)
    agg_path = repr(JS_AGGREGATE)

    # Build a single JS script that loads aggregate.js (then a postamble
    # that hoists the `const` bindings onto globalThis so the merge
    # call can reach them via ctx.*), then calls rcaMergeResults.
    # Mirrors the technique in tests_aggregate.js (project root).
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
    return _json.loads(result.stdout)


def _py_merge(runs: list, total_runs, schema=None) -> dict:
    return merge_results(runs, total_runs=total_runs, schema=schema)


def _diff(a, b, path=''):
    """Return list of (path, a_val, b_val) tuples for any differences."""
    diffs = []
    if type(a) != type(b):
        # int/float coercion tolerated
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if a == b:
                return diffs
        diffs.append((f'{path} (type)', type(a).__name__, type(b).__name__))
        return diffs
    if isinstance(a, dict):
        keys = set(list(a.keys()) + list(b.keys()))
        for k in keys:
            if k not in a:
                diffs.append((f'{path}.{k}', '<missing>', b[k]))
            elif k not in b:
                diffs.append((f'{path}.{k}', a[k], '<missing>'))
            else:
                diffs.extend(_diff(a[k], b[k], f'{path}.{k}'))
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append((f'{path} (len)', len(a), len(b)))
        else:
            for i in range(len(a)):
                diffs.extend(_diff(a[i], b[i], f'{path}[{i}]'))
    else:
        if a != b:
            diffs.append((path, a, b))
    return diffs


# ---------------------------------------------------------------------------
# Test cases — each one drives identical inputs through both engines
# ---------------------------------------------------------------------------

def test_parity_single_run_range_chart():
    """Single-run range-chart passthrough: both sides identical."""
    r = [{
        'sections': [{'name': 'Pingdingshan', 'age_range': 'Late Permian',
                      'formations': ['Talung Fm'], 'formation_thickness_m': '9m',
                      'coordinates': '31N'}],
        'species_ranges': [
            {'species': 'Neoalbaillella optima', 'section': 'Pingdingshan',
             'range_top': 'Bed 9', 'range_base': 'Bed 7', 'biozone': 'N. optima Zone'},
        ],
        'biozones': [{'name': 'N. optima Zone', 'section': 'Pingdingshan',
                      'age': 'Latest Changhsingian', 'thickness_m': '3m'}],
        'other_fossils': ['Ammonoid: Pleuronodoceras sp.'],
        'confidence': 0.85,
    }]
    py = _py_merge(r, total_runs=1)
    js = _js_merge(r, 1)
    diffs = _diff(py, js)
    assert not diffs, f'single-run parity failed: {diffs}'


def test_parity_empty_result_includes_sections():
    """Empty range-chart result: both sides must include 'sections': [].

    js/aggregate.js always emits a 'sections' key on the empty shape
    (range-chart backward-compat). Python's _empty_for originally did
    not, which broke parity for callers that look up sections.
    """
    py = _py_merge([], total_runs=0)
    js = _js_merge([], 0)
    assert 'sections' in py, 'python missing sections in empty result'
    assert 'sections' in js, 'js missing sections in empty result'
    assert py['sections'] == js['sections'] == [], 'sections not empty list'


def test_parity_multi_run_range_chart():
    """Multi-run range-chart with section, species, biozone merge."""
    r1 = {
        'sections': [{'name': 'S1', 'age_range': 'Permian', 'formations': ['F1'],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'species_ranges': [
            {'species': 'Neoalbaillella optima', 'section': 'S1',
             'range_top': 'B9', 'range_base': 'B7', 'biozone': 'Z'},
        ],
        'biozones': [{'name': 'Z', 'age': 'Late', 'thickness_m': '3m'}],
        'other_fossils': ['Ammonoid: X'],
        'confidence': 0.8,
    }
    r2 = {
        'sections': [{'name': 'S1', 'age_range': 'Permian', 'formations': ['F1', 'F2'],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'species_ranges': [
            {'species': 'Neoalbaillella optima', 'section': 'S1',
             'range_top': 'B9', 'range_base': 'B7', 'biozone': 'Z'},
            {'species': 'Paracopicyntra longispina', 'section': 'S1',
             'range_top': 'B26', 'range_base': 'B20', 'biozone': ''},
        ],
        'biozones': [{'name': 'Z', 'age': 'Late', 'thickness_m': '3m'}],
        'other_fossils': ['Ammonoid: X', 'Ammonoid: Y'],
        'confidence': 0.9,
    }
    py = _py_merge([r1, r2], total_runs=2)
    js = _js_merge([r1, r2], 2)
    diffs = _diff(py, js)
    assert not diffs, f'multi-run range-chart parity failed: {diffs}'


def test_parity_abundance_single_site_empty_site_field():
    """Single-site abundance-diagram with empty site field.

    Both sides must produce one merged row keyed on (site='', taxon, level)
    — not drop the row. This regression-guards the CRITICAL dedup-key bug
    where Python's `key0 = tuple(k[0] ...)` only sampled the first id field's
    first character and silently dropped rows when the first id was empty.
    """
    r1 = {
        'sites': [{'name': '', 'location': '35N', 'age_range': 'Holocene', 'depth_unit': 'cm'}],
        'abundances': [{'taxon': 'Pinus', 'site': '', 'level': '120 cm',
                        'depth': '120', 'abundance': '35', 'abundance_unit': '%'}],
        'zones': [],
        'confidence': 0.8,
    }
    r2 = {
        'sites': [{'name': '', 'location': '35N', 'age_range': 'Holocene', 'depth_unit': 'cm'}],
        'abundances': [{'taxon': 'Pinus', 'site': '', 'level': '120 cm',
                        'depth': '120', 'abundance': '35', 'abundance_unit': '%'}],
        'zones': [],
        'confidence': 0.8,
    }
    py = _py_merge([r1, r2], total_runs=2, schema=ABUNDANCE_DIAGRAM_SCHEMA)
    js = _js_merge([r1, r2], 2, 'abundance_diagram')

    # Both sides must agree on row count
    assert len(py['abundances']) == len(js['abundances']), \
        f'abundance row count: py={len(py["abundances"])} js={len(js["abundances"])}'
    # And both must NOT have dropped the row
    assert len(py['abundances']) >= 1, 'python dropped abundance row — dedup bug'


def test_parity_columnar_sections_single_pass():
    """Columnar sections: Python must NOT re-merge out['sections'] with a
    second algorithm. JS only runs `mergePrimaryList` once on the columnar
    primary key. Python used to do a second pass with a different (less
    complete) merge, silently diverging.
    """
    r1 = {
        'sections': [{'id': 'Ki-1', 'group': 'Lower', 'lithology_blocks': [],
                      'age_units': [], 'samples': [], 'coordinates_text': '',
                      'thickness_m': '500m', 'confidence_by_section': 0.7}],
        'fossil_legend': [], 'lithology_legend': [], 'cross_beds': [],
        'confidence': 0.7,
    }
    r2 = {
        'sections': [{'id': 'Ki-1', 'group': 'Lower', 'lithology_blocks': [],
                      'age_units': [], 'samples': [], 'coordinates_text': 'NW wing',
                      'thickness_m': '500m', 'confidence_by_section': 0.8}],
        'fossil_legend': [], 'lithology_legend': [], 'cross_beds': [],
        'confidence': 0.8,
    }
    py = _py_merge([r1, r2], total_runs=2, schema=COLUMNAR_SECTION_SCHEMA)
    js = _js_merge([r1, r2], 2, 'columnar_section')

    # Both sides should have one merged section with agreement 2/2
    assert len(py['sections']) == len(js['sections']) == 1
    assert py['sections'][0].get('agreement') == '2/2'
    assert js['sections'][0].get('agreement') == '2/2'
    # coordinates_text mode-merged
    assert py['sections'][0].get('coordinates_text') == 'NW wing'
    assert js['sections'][0].get('coordinates_text') == 'NW wing'


def test_parity_confidence_skips_missing_runs():
    """When one run is missing the confidence field, both sides must SKIP
    that run (not treat it as 0.0) so the average reflects only runs that
    actually reported a confidence.
    """
    r1 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.8,
    }
    r2 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        # no confidence
    }
    py = _py_merge([r1, r2], total_runs=2)
    js = _js_merge([r1, r2], 2)

    # Both should be 0.8 (only r1 counted), not 0.4 (0.8 / 2)
    assert abs(py['confidence'] - 0.8) < 1e-6, \
        f'python confidence {py["confidence"]} != 0.8'
    assert abs(js['confidence'] - 0.8) < 1e-6, \
        f'js confidence {js["confidence"]} != 0.8'
    assert py['confidence'] == js['confidence']


def test_parity_extras_dict_stringified():
    """A field whose value is a dict (e.g. _extras) must be handled
    consistently across both sides.

    Both sides should drop non-primitive values from the mixed-branch
    fallback rather than letting Python's repr or JS's "[object Object]"
    leak into the merged row.
    """
    r1 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.5,
        # simulate an _extras field where one run has a dict and one has a string
        'extra_field': {'note': 'a'},
    }
    r2 = {
        'sections': [], 'species_ranges': [], 'biozones': [], 'other_fossils': [],
        'confidence': 0.5,
        'extra_field': 'plain_value',
    }
    py = _py_merge([r1, r2], total_runs=2)
    js = _js_merge([r1, r2], 2)

    # Whichever side keeps extra_field, both sides must agree
    if 'extra_field' in py:
        assert 'extra_field' in js, 'parity: python kept extra_field but js dropped it'
        assert py['extra_field'] == js['extra_field'], \
            f'extras diverge: py={py["extra_field"]!r} js={js["extra_field"]!r}'
    else:
        assert 'extra_field' not in js, 'parity: js kept extra_field but python dropped it'


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    tests = [
        test_parity_single_run_range_chart,
        test_parity_empty_result_includes_sections,
        test_parity_multi_run_range_chart,
        test_parity_abundance_single_site_empty_site_field,
        test_parity_columnar_sections_single_pass,
        test_parity_confidence_skips_missing_runs,
        test_parity_extras_dict_stringified,
    ]
    results = []
    for fn in tests:
        try:
            fn()
            print(f'  PASS  {fn.__name__}')
            results.append(True)
        except AssertionError as e:
            print(f'  FAIL  {fn.__name__}: {e}')
            results.append(False)
        except Exception as e:
            print(f'  ERROR {fn.__name__}: {type(e).__name__}: {e}')
            results.append(False)
    print()
    passed = sum(results)
    print(f'{passed}/{len(results)} passed')
    sys.exit(0 if passed == len(results) else 1)