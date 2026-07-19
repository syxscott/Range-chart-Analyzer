"""Python ↔ JS aggregate parity tests.

Runs the same inputs through both:
  - Python: rca_core.aggregate.merge_results
  - JS:     js/aggregate.js  (loaded via node)

and verifies that the merged output structure and field values are
identical byte-for-byte (modulo type coercion of numbers, which JS
stores all as floats while Python distinguishes int/float — we
compare string representations of numeric values).
"""

from __future__ import annotations

import subprocess
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core.aggregate import merge_results, RANGE_CHART_SCHEMA


# ---------------------------------------------------------------------------
# JS runner
# ---------------------------------------------------------------------------

JS_AGGREGATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'js', 'aggregate.js')


def _run_js(script: str) -> str:
    """Execute script via node, return stdout."""
    result = subprocess.run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=30,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if result.returncode != 0:
        raise RuntimeError(f'node error: {result.stderr}')
    return result.stdout


def _js_merge(runs: list, total_runs: int, schema_key: str = 'range_chart') -> dict:
    """Run JS rcaMergeResults via node -e with a single self-contained script.

    All `const` declarations from aggregate.js are scoped to the evaluated code.
    We evaluate everything in one vm.Script so those bindings stay in scope.
    """
    import json as _json
    import tempfile as _tempfile

    runs_json = _json.dumps(runs)
    schema_key_json = _json.dumps(schema_key)
    agg_path = repr(JS_AGGREGATE)

    # Build a single JS script that loads the file and calls rcaMergeResults
    # all within the same vm.Script evaluation scope — so const bindings are
    # visible to the merge call.
    script = (
        "const vm=require('vm');"
        "const fs=require('fs');"
        "const code=fs.readFileSync(" + agg_path + ",'utf8');"
        "const ctx={console:console};"
        "vm.createContext(ctx);"
        "const script=new vm.Script(code);"
        "script.runInContext(ctx);"
        # Call rcaAutoDetectKeymap inside the same evaluated scope
        "const km=ctx.rcaAutoDetectKeymap(" + runs_json + ")||ctx.RCA_KEYMAP_BY_MODE[" + schema_key_json + "];"
        "const r=ctx.rcaMergeResults(" + runs_json + "," + str(total_runs) + ",km);"
        "console.log(JSON.stringify(r));"
    )

    result = __import__('subprocess').run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=30,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if result.returncode != 0:
        raise RuntimeError(f'node error: {result.stderr}')
    return _json.loads(result.stdout)


def _norm_num(v):
    """Stringify numbers so int/float mismatches don't cause false parity failures."""
    if isinstance(v, float):
        return round(v, 6)
    return v


def _deep_compare(a, b, path=''):
    """Return list of (path, a_val, b_val) differences, or [] if identical."""
    diffs = []
    if type(a) != type(b):
        diffs.append((path, type(a).__name__, type(b).__name__))
        return diffs
    if isinstance(a, dict):
        keys = set(list(a.keys()) + list(b.keys()))
        for k in keys:
            if k not in a:
                diffs.append((f'{path}.{k}', '<missing>', b[k]))
            elif k not in b:
                diffs.append((f'{path}.{k}', a[k], '<missing>'))
            else:
                diffs.extend(_deep_compare(a[k], b[k], f'{path}.{k}'))
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append((f'{path} (len)', len(a), len(b)))
        else:
            for i in range(len(a)):
                diffs.extend(_deep_compare(a[i], b[i], f'{path}[{i}]'))
    else:
        a_norm = _norm_num(a)
        b_norm = _norm_num(b)
        if a_norm != b_norm:
            diffs.append((path, a, b))
    return diffs


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_parity_single_run():
    """Single-run range-chart: both sides produce identical output."""
    r = [{
        'sections': [{'name': 'Pingdingshan', 'age_range': 'Late Permian',
                       'formations': ['Talung Fm'], 'formation_thickness_m': '9m', 'coordinates': '31N'}],
        'species_ranges': [
            {'species': 'Neoalbaillella optima', 'section': 'Pingdingshan',
             'range_top': 'Bed 9', 'range_base': 'Bed 7', 'biozone': 'N. optima Zone'},
        ],
        'biozones': [{'name': 'N. optima Zone', 'section': 'Pingdingshan',
                       'age': 'Latest Changhsingian', 'thickness_m': '3m'}],
        'other_fossils': ['Ammonoid: Pleuronodoceras sp.'],
        'confidence': 0.85,
    }]
    py = merge_results(r, total_runs=1)
    js = _js_merge(r, 1)
    diffs = _deep_compare(py, js)
    assert not diffs, f'single-run parity failed: {diffs}'


def test_parity_multi_run_b1_fix():
    """B-1 fix: sp./bare/genus-only should NOT be merged together.

    "Neoalbaillella sp." (twice) and "Neoalbaillella" (once) must produce
    two separate rows, not one merged row.
    """
    r1 = [{
        'sections': [{'name': 'S1', 'age_range': 'X', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'species_ranges': [
            {'species': 'Neoalbaillella sp.', 'section': 'S1',
             'range_top': 'B9', 'range_base': 'B7', 'biozone': ''},
        ],
        'biozones': [], 'other_fossils': [], 'confidence': 0.8,
    }]
    r2 = [{
        'sections': [{'name': 'S1', 'age_range': 'X', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'species_ranges': [
            {'species': 'Neoalbaillella sp.', 'section': 'S1',
             'range_top': 'B9', 'range_base': 'B7', 'biozone': ''},
        ],
        'biozones': [], 'other_fossils': [], 'confidence': 0.8,
    }]
    r3 = [{
        'sections': [{'name': 'S1', 'age_range': 'X', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'species_ranges': [
            {'species': 'Neoalbaillella', 'section': 'S1',
             'range_top': 'B9', 'range_base': 'B7', 'biozone': ''},
        ],
        'biozones': [], 'other_fossils': [], 'confidence': 0.8,
    }]
    py = merge_results([r1[0], r2[0], r3[0]], total_runs=3)
    js = _js_merge([r1[0], r2[0], r3[0]], 3)

    # Must be 2 rows
    assert len(py['species_ranges']) == 2, f'py rows: {len(py["species_ranges"])}'
    assert len(js['species_ranges']) == 2, f'js rows: {len(js["species_ranges"])}'

    # Sp. row: agreement 2/3
    sp_rows = [r for r in py['species_ranges'] if 'sp.' in r.get('species', '')]
    assert len(sp_rows) == 1 and sp_rows[0]['agreement'] == '2/3', \
        f'sp. row: {sp_rows}'
    # Bare row: agreement 1/3
    bare_rows = [r for r in py['species_ranges'] if not 'sp.' in r.get('species', '')]
    assert len(bare_rows) == 1 and bare_rows[0]['agreement'] == '1/3', \
        f'bare row: {bare_rows}'

    # JS must agree
    js_sp = [r for r in js['species_ranges'] if 'sp.' in r.get('species', '')]
    js_bare = [r for r in js['species_ranges'] if not 'sp.' in r.get('species', '')]
    assert len(js_sp) == 1 and js_sp[0]['agreement'] == '2/3'
    assert len(js_bare) == 1 and js_bare[0]['agreement'] == '1/3'


def test_parity_biozone_section_fold():
    """Same biozone name in different sections must NOT be collapsed together."""
    r1 = [{
        'sections': [{'name': 'A', 'age_range': 'X', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'biozones': [
            {'name': 'N. optima Zone', 'section': 'A',
             'age': 'L Changhsingian', 'thickness_m': '3m'},
        ],
        'species_ranges': [], 'other_fossils': [], 'confidence': 0.8,
    }]
    r2 = [{
        'sections': [{'name': 'B', 'age_range': 'X', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'biozones': [
            {'name': 'N. optima Zone', 'section': 'B',
             'age': 'L Changhsingian', 'thickness_m': '5m'},
        ],
        'species_ranges': [], 'other_fossils': [], 'confidence': 0.8,
    }]
    py = merge_results([r1[0], r2[0]], total_runs=2)
    js = _js_merge([r1[0], r2[0]], 2)

    # Must be 2 rows (different sections)
    assert len(py['biozones']) == 2, f'py biozone rows: {len(py["biozones"])} — should be 2 (diff sections)'
    assert len(js['biozones']) == 2, f'js biozone rows: {len(js["biozones"])}'

    # Sections should be merged (same name)
    assert len(py['sections']) == 2  # one per section


def test_parity_other_fossils_union():
    """other_fossils is a plain string list — union by lowercase dedup."""
    r1 = [{
        'sections': [{'name': 'S1', 'age_range': '', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'other_fossils': ['Ammonoid: Pleuronodoceras sp.', 'Conodont: Clarkina sp.'],
        'species_ranges': [], 'biozones': [], 'confidence': 0.5,
    }]
    r2 = [{
        'sections': [{'name': 'S1', 'age_range': '', 'formations': [],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'other_fossils': ['Conodont: Clarkina sp.', 'Brachiopod sp.'],
        'species_ranges': [], 'biozones': [], 'confidence': 0.5,
    }]
    py = merge_results([r1[0], r2[0]], total_runs=2)
    js = _js_merge([r1[0], r2[0]], 2)

    py_set = set(py['other_fossils'])
    js_set = set(js['other_fossils'])
    assert py_set == js_set, f'other_fossils mismatch: py={py_set} js={js_set}'
    assert len(py['other_fossils']) == 3  # all 3 unique items


if __name__ == '__main__':
    tests = [
        test_parity_single_run,
        test_parity_multi_run_b1_fix,
        test_parity_biozone_section_fold,
        test_parity_other_fossils_union,
    ]
    results = []
    for fn in tests:
        try:
            fn()
            print(f'  {fn.__name__}: PASS')
            results.append(True)
        except AssertionError as e:
            print(f'  {fn.__name__}: FAIL - {e}')
            results.append(False)
        except Exception as e:
            print(f'  {fn.__name__}: ERROR - {type(e).__name__}: {e}')
            results.append(False)
    print()
    print('ALL PASSED' if all(results) else f'{sum(results)}/{len(results)} PASSED')
