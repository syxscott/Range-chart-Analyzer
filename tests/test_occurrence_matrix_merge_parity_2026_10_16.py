"""occurrence_matrix must MERGE the same way in the browser and on the desktop.

AUDIT-2026-10-16 (HIGH): rca_core/aggregate.py has carried OCCURRENCE_MATRIX_SCHEMA
since 2026-10-15, wired into both SCHEMA_BY_MODE and _auto_detect_schema.
js/aggregate.js mirrored neither. It shipped a five-entry RCA_KEYMAP_BY_MODE and
an rcaAutoDetectKeymap that counted phylo, columnar and abundance only.

The consequence is named in the Python docstring for _looks_occurrence_matrix
and it is not a degradation: merge_results with the range-chart schema's
`species_ranges` primary produced NOTHING for the real rows, so a locality only
the second run reported vanished and two localities of one genus collapsed into
a single record. In the browser the same thing happened for a different
reason -- js/app.js:1318 feeds rcaAutoDetectKeymap's answer straight into
rcaMergeResults, so a missing branch is a missing branch there too.

Two things make this file's assertions what they are:

  1. It passes the keymap to the JS engine, the way js/app.js:1318 does.
     `rcaMergeResults(results, totalRuns, keymap)` does NOT auto-detect -- its
     keymap argument is optional and defaults to RCA_DEFAULT_KEYMAP -- while
     Python's `merge_results` detects when `schema is None`. A first draft of
     this test called it with no keymap and so compared a range-chart merge
     against an occurrence merge and reported the suite's own agreement.

  2. It compares the merged RESULT, not just the chosen primary. Two engines
     agreeing on the name of the primary list while writing different key sets
     is the divergence that costs the user data.

Mutations that must turn this red:
  * deleting `occurrence_matrix: RCA_OCCURRENCE_KEYMAP` from RCA_KEYMAP_BY_MODE
  * deleting the occCount branch from rcaAutoDetectKeymap
  * reverting either engine's `_looks_*_occurrence_matrix` to `occurrences` only
    (which breaks the gated cases, because that strategy empties it)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.aggregate import (  # noqa: E402
    OCCURRENCE_MATRIX_SCHEMA,
    _auto_detect_schema,
    merge_results,
)


def _occ(site, code="A"):
    return {"taxon": "Capnodoce", "authority": "Moser", "site": site,
            "code": code, "class": "abundant"}


# `occurrences` populated: the ordinary single_pass / grid_cells result.
POPULATED = [
    {"occurrences": [_occ("S1")], "columns": [{"index": 0, "label": "S1"}],
     "cell_status": "single_pass_unverified", "confidence": 0.7},
    {"occurrences": [_occ("S2", "C")],
     "columns": [{"index": 0, "label": "S1"}, {"index": 1, "label": "S2"}],
     "cell_status": "single_pass_unverified", "confidence": 0.8},
]

# `occurrences` empty BY DESIGN (the `gated` cell strategy), names in
# `taxon_rows`. This is the case the second detector signal exists for.
GATED = [
    {"columns": [{"index": 0, "label": "S1"}, {"index": 1, "label": "S2"}],
     "legend": [{"code": "A", "class": "abundant"}], "occurrences": [],
     "taxon_rows": [{"index": 0, "taxon": "Capnodoce", "authority": "Moser"}],
     "cell_status": "not_attempted", "confidence": 0.7},
    {"columns": [{"index": 0, "label": "S1"}, {"index": 1, "label": "S2"},
                 {"index": 2, "label": "S3"}],
     "legend": [{"code": "A", "class": "abundant"}], "occurrences": [],
     "taxon_rows": [{"index": 0, "taxon": "Capnodoce", "authority": "Moser"},
                    {"index": 1, "taxon": "Capmuchosphaera", "authority": "M"}],
     "cell_status": "not_attempted", "confidence": 0.8},
]

SINGLE_GATED = [
    {"columns": [{"index": 0, "label": "S1"}], "occurrences": [],
     "taxon_rows": [{"index": 0, "taxon": "Capnodoce", "authority": "M"}],
     "cell_status": "not_attempted", "confidence": 0.7},
]

CASES = {
    "populated": POPULATED,
    "gated": GATED,
    "single-gated": SINGLE_GATED,
}

_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const ctx = { console, TextEncoder, TextDecoder, URL, structuredClone };
ctx.globalThis = ctx; ctx.window = ctx;
vm.createContext(ctx);
for (const f of ['js/config.js', 'js/json-utils.js', 'js/ics_table.js',
                 'js/reason-codes.js', 'js/quality.js', 'js/aggregate.js']) {
  new vm.Script(fs.readFileSync(path.join(ROOT, f), 'utf8'),
                { filename: f }).runInContext(ctx);
}
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {};
for (const [name, runs] of Object.entries(cases)) {
  const km = ctx.rcaAutoDetectKeymap(runs);            // what app.js:1318 does
  out[name] = { primary: km.primary,
                keys: Object.keys(ctx.rcaMergeResults(runs, undefined, km)).sort() };
}
console.log(JSON.stringify(out));
"""


def _js_cases():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dp = os.path.join(td, "c.json")
        dv = os.path.join(td, "d.js")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(CASES, fh)
        with open(dv, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER)
        proc = subprocess.run(["node", dv, dp], capture_output=True, text=True,
                              encoding="utf-8", timeout=120,
                              env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
    assert proc.returncode == 0, f"node driver failed: {proc.stderr[:800]}"
    return json.loads(proc.stdout)


_JS = _js_cases()


@pytest.mark.parametrize("name", sorted(CASES))
def test_both_engines_pick_the_same_primary_list(name):
    runs = CASES[name]
    py_primary = _auto_detect_schema(runs).primary_list_key
    js_primary = _JS[name]["primary"]
    assert py_primary == js_primary == "occurrences", (
        f"{name}: python chose {py_primary!r} and the browser chose "
        f"{js_primary!r}. js/app.js:1318 feeds this choice straight into the "
        f"merge, so the browser and the desktop would keep different records "
        f"for the same runs."
    )


@pytest.mark.parametrize("name", sorted(CASES))
def test_both_engines_write_the_same_key_set(name):
    py_keys = sorted(merge_results(CASES[name]))
    js_keys = list(_JS[name]["keys"])
    assert py_keys == js_keys, (
        f"{name}: python writes {py_keys} and the browser writes {js_keys}. "
        f"only-python={sorted(set(py_keys) - set(js_keys))} "
        f"only-js={sorted(set(js_keys) - set(py_keys))}"
    )


def test_a_populated_matrix_does_not_collapse_to_the_range_chart_primary():
    """The original data loss, asserted directly rather than by parity.

    Parity alone would be satisfied by two engines that were both wrong. This
    pins the outcome: two localities of one genus stay two records.
    """
    merged = merge_results(POPULATED)
    sites = sorted(r["site"] for r in merged["occurrences"])
    assert sites == ["S1", "S2"], (
        f"expected one record per locality, got {sites} -- the range-chart "
        f"primary collapsed them"
    )


def test_disagreeing_cell_codes_stay_one_record_with_the_disagreement_visible():
    """`code` is not in strModeFields, on purpose, on both engines.

    Two runs that read the same cell as A and as C must produce ONE row that
    says so, not two rows that each look certain.
    """
    merged = merge_results([
        {"occurrences": [_occ("S1", "A")], "confidence": 0.7},
        {"occurrences": [_occ("S1", "C")], "confidence": 0.7},
    ])
    rows = merged["occurrences"]
    assert len(rows) == 1, f"expected 1 merged row, got {len(rows)}"
    assert rows[0]["agreement_count"] == 2


def test_the_schema_is_reachable_by_mode_name():
    """RCA_KEYMAP_BY_MODE is a second, independent door to the same bug.

    Deleting the entry compiles fine and every other test in this file still
    passes, because app.js auto-detects. It is asserted here because the
    desktop reaches the schema by mode name and the browser can be asked to.
    """
    with open(os.path.join(PROJECT_ROOT, "js", "aggregate.js"), "r",
              encoding="utf-8") as fh:
        src = fh.read()
    assert "occurrence_matrix: RCA_OCCURRENCE_KEYMAP" in src, (
        "RCA_KEYMAP_BY_MODE has no occurrence_matrix entry; an explicit "
        "per-mode merge silently falls back to the range-chart keymap"
    )
    assert OCCURRENCE_MATRIX_SCHEMA.primary_list_key == "occurrences"
