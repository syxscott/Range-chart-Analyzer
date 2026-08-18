"""Regression test for phylogenetic-tree auto-detection in js/aggregate.js.

REVIEW-2026-08-17 (P2): the JS ``rcaAutoDetectKeymap`` function only
checks for range_chart / columnar_section / abundance_diagram shapes.
Phylogenetic-tree results carry a ``nodes`` array (and ``metadata`` /
``legend`` dicts, ``root_ids`` list) — none of those detectors fire, so
the JS path silently falls back to ``RCA_DEFAULT_KEYMAP`` and merges
phylo data as if it were a range chart, destroying the primary row
key (``nodes[].id``), the list keys (``root_ids``) and the confidence
field, and emitting range-chart-shaped output that has nothing to do
with the input.

The Python side ``_auto_detect_schema`` has the same gap when called
without an explicit ``schema=`` argument — but server.py always passes
``schema=SCHEMA_BY_MODE[mode]`` for known modes, so the Python gap is
not exercised in production. The JS path uses auto-detect and is hit
by every pure-frontend / proxy request that doesn't supply a keymap.

The fix: mirror the phylo detector from rca_core.aggregate into JS
(check for ``nodes`` list + ``metadata`` dict) and return the phylo
keymap. We pin the keymap's contract via this test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core.aggregate import (  # noqa: E402
    merge_results,
    PHYLOGENETIC_TREE_SCHEMA,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_AGGREGATE = os.path.join(PROJECT_ROOT, 'js', 'aggregate.js')


def _js_auto_detect_and_merge(runs: list) -> dict:
    """Run ``rcaAutoDetectKeymap`` over ``runs`` in JS, then merge with
    the detected keymap, and return the merged result. This is exactly
    what ``js/app.js`` does in production for the pure-frontend path."""
    runs_json = json.dumps(runs)
    agg_path = repr(JS_AGGREGATE)
    postamble = (
        "\nglobalThis.__KM_DEFAULT = RCA_DEFAULT_KEYMAP;"
        "\nglobalThis.__KM_COL = RCA_COLUMNAR_KEYMAP;"
        "\nglobalThis.__KM_AB = RCA_ABUNDANCE_KEYMAP;"
        "\nglobalThis.__MERGE = rcaMergeResults;"
        "\nglobalThis.__AUTODETECT = rcaAutoDetectKeymap;"
    )
    script = (
        "const vm=require('vm');const fs=require('fs');"
        "let code=fs.readFileSync(" + agg_path + ",'utf8')+"
        + repr(postamble) + ";"
        "const ctx={console:console};vm.createContext(ctx);"
        "new vm.Script(code).runInContext(ctx);"
        "const runs=" + runs_json + ";"
        "const km=ctx.__AUTODETECT(runs);"
        "const merged=ctx.__MERGE(runs, runs.length, km);"
        "console.log(JSON.stringify({keymap:km.primary, merged:merged}));"
    )
    out = subprocess.run(
        ['node', '-e', script],
        capture_output=True, text=True, timeout=30, cwd=PROJECT_ROOT,
    )
    if out.returncode != 0:
        raise RuntimeError(f'node error: {out.stderr}')
    return json.loads(out.stdout)


def test_js_autodetect_recognizes_phylogenetic_tree_primary_key():
    """Given two phylo-shaped runs, ``rcaAutoDetectKeymap`` MUST return
    a keymap whose primary is ``nodes`` (not ``species_ranges``).

    Before the fix, the JS path returned RCA_DEFAULT_KEYMAP (primary
    ``species_ranges``), and the merger then wrote phylo nodes into
    ``merged.species_ranges`` while leaving ``merged.nodes`` empty.
    """
    runs = [
        {
            'nodes': [
                {'id': 'n1', 'name': 'root', 'parent': None,
                 'branch_length': 0.0, 'rank': 'domain',
                 'clade_label': '', 'confidence': 0.9},
                {'id': 'n2', 'name': 'Bacteria', 'parent': 'n1',
                 'branch_length': 1.0, 'rank': 'domain',
                 'clade_label': '', 'confidence': 0.85},
            ],
            'metadata': {'age_range': 'Phanerozoic', 'taxon': 'mixed'},
            'legend': {'n1': 'root'},
            'root_ids': ['n1'],
            'confidence': 0.88,
        },
        {
            'nodes': [
                {'id': 'n1', 'name': 'root', 'parent': None,
                 'branch_length': 0.0, 'rank': 'domain',
                 'clade_label': '', 'confidence': 0.9},
            ],
            'metadata': {'age_range': 'Phanerozoic'},
            'legend': {},
            'root_ids': ['n1'],
            'confidence': 0.92,
        },
    ]
    result = _js_auto_detect_and_merge(runs)

    # The auto-detected keymap must recognize phylo shape → primary=nodes.
    assert result['keymap'] == 'nodes', (
        f"JS auto-detect missed phylo shape; got primary={result['keymap']!r}, "
        f"expected 'nodes'. This means phylo data was merged as range_chart."
    )
    # And the merged result must preserve the nodes under 'nodes'.
    assert 'nodes' in result['merged'], (
        f"Merged result missing 'nodes' key: keys={sorted(result['merged'].keys())}"
    )
    assert len(result['merged']['nodes']) >= 1, (
        "Merged nodes list is empty — auto-detect chose wrong keymap"
    )


def test_python_autodetect_recognizes_phylogenetic_tree_primary_key():
    """Mirror test on the Python side — also missing phylo detection."""
    runs = [
        {
            'nodes': [
                {'id': 'n1', 'name': 'root', 'parent': None, 'confidence': 0.9},
            ],
            'metadata': {'age_range': 'Phanerozoic'},
            'root_ids': ['n1'],
            'confidence': 0.88,
        },
        {
            'nodes': [
                {'id': 'n1', 'name': 'root', 'parent': None, 'confidence': 0.9},
                {'id': 'n2', 'name': 'Bacteria', 'parent': 'n1', 'confidence': 0.85},
            ],
            'metadata': {'age_range': 'Phanerozoic'},
            'root_ids': ['n1'],
            'confidence': 0.92,
        },
    ]
    merged = merge_results(runs, total_runs=len(runs))  # no schema= → auto-detect
    assert 'nodes' in merged, (
        f"Python auto-detect missed phylo shape; merged keys={sorted(merged.keys())}"
    )
    assert len(merged['nodes']) >= 1, (
        "Python-merged nodes list is empty — auto-detect chose wrong schema"
    )


def test_python_autodetect_with_explicit_schema_still_works():
    """Passing an explicit phylo schema must always work — this is the
    path server.py takes, and the assertion guards against the fix
    accidentally breaking it."""
    runs = [
        {
            'nodes': [{'id': 'n1', 'name': 'root', 'parent': None}],
            'metadata': {},
            'root_ids': ['n1'],
            'confidence': 0.9,
        },
    ]
    merged = merge_results(runs, total_runs=1, schema=PHYLOGENETIC_TREE_SCHEMA)
    assert 'nodes' in merged
    assert len(merged['nodes']) == 1
    assert merged['nodes'][0]['name'] == 'root'


def test_python_multi_run_phylo_preserves_metadata_and_legend():
    """Pin the parity target: Python's multi-run phylo merge MUST keep
    ``metadata`` and ``legend`` (they are single dicts, not in
    list_keys — see rca_core/aggregate.py:790-796)."""
    runs = [
        {
            'nodes': [{'id': 'n1', 'name': 'root', 'parent': None,
                       'branch_length': 0.0, 'confidence': 0.9}],
            'metadata': {'age_range': 'Phanerozoic', 'taxon': 'Bacteria'},
            'legend': {'n1': 'root clade'},
            'root_ids': ['n1'],
            'confidence': 0.88,
        },
        {
            'nodes': [{'id': 'n1', 'name': 'root', 'parent': None,
                       'branch_length': 0.0, 'confidence': 0.92}],
            'metadata': {'age_range': 'Phanerozoic', 'taxon': 'Archaea'},
            'legend': {'n1': 'root clade'},
            'root_ids': ['n1'],
            'confidence': 0.94,
        },
    ]
    merged = merge_results(runs, total_runs=2, schema=PHYLOGENETIC_TREE_SCHEMA)
    assert 'metadata' in merged, (
        f"Python lost 'metadata' in multi-run phylo merge: keys={sorted(merged.keys())}"
    )
    assert merged['metadata'].get('age_range') == 'Phanerozoic'
    assert 'legend' in merged, (
        f"Python lost 'legend' in multi-run phylo merge: keys={sorted(merged.keys())}"
    )
    assert merged['legend'].get('n1') == 'root clade'


def test_js_multi_run_phylo_preserves_metadata_and_legend():
    """Same as the Python test but on the JS side — this is the bug the
    review surfaced: JS's multi-run merge path did not mirror Python's
    dict-preservation branch at lines 790-796, so phylo's ``metadata``
    and ``legend`` were silently dropped in the pure-frontend path."""
    runs = [
        {
            'nodes': [{'id': 'n1', 'name': 'root', 'parent': None,
                       'branch_length': 0.0, 'confidence': 0.9}],
            'metadata': {'age_range': 'Phanerozoic', 'taxon': 'Bacteria'},
            'legend': {'n1': 'root clade'},
            'root_ids': ['n1'],
            'confidence': 0.88,
        },
        {
            'nodes': [{'id': 'n1', 'name': 'root', 'parent': None,
                       'branch_length': 0.0, 'confidence': 0.92}],
            'metadata': {'age_range': 'Phanerozoic', 'taxon': 'Archaea'},
            'legend': {'n1': 'root clade'},
            'root_ids': ['n1'],
            'confidence': 0.94,
        },
    ]
    result = _js_auto_detect_and_merge(runs)

    assert 'metadata' in result['merged'], (
        f"JS lost 'metadata' in multi-run phylo merge: "
        f"keys={sorted(result['merged'].keys())}"
    )
    assert result['merged']['metadata'].get('age_range') == 'Phanerozoic', (
        f"JS metadata content lost: {result['merged'].get('metadata')}"
    )
    assert 'legend' in result['merged'], (
        f"JS lost 'legend' in multi-run phylo merge: "
        f"keys={sorted(result['merged'].keys())}"
    )
    assert result['merged']['legend'].get('n1') == 'root clade'