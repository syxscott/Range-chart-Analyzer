"""The `_looks_*` detectors decide which sheets an export contains.

js/table.js's rcaLooksColumnar / rcaLooksAbundance / rcaLooksZonationChart /
rcaLooksPhylogeneticTree / rcaLooksPaleomap / rcaLooksOccurrenceMatrix are
documented mirrors of rca_core/exporter.py's _looks_columnar / _looks_abundance
/ _looks_zonation_chart / _looks_phylogenetic_tree / _looks_paleomap /
_looks_occurrence_matrix, and get_configs_for_result dispatches the whole export
on them. If one side disagrees, the browser and the desktop write DIFFERENT SETS
OF SHEETS for the same result -- the same class of divergence as the quality
zonation branch fixed in b7f592b, one layer down.

Nothing covered this. The parity harness has no table-config group, and
app.js's call to each of these is named by no test (measured: 0 mentions
each). Paleomap was added to both sides on 2026-09-27 item 4.2, which is
exactly the kind of edit that can leave one side behind.

AUDIT-2026-10-15: occurrence_matrix repeated that mistake. `_looks_occurrence_matrix`
went into exporter.py with no PAIRS entry and no shape below, and the suite
stayed green -- because the scope assertion was `len(PAIRS) == 5`, which pinned
the hand-written list to its own length instead of to the detectors the code
actually has. The count is gone; the set is now enumerated from the exporter
module and from what js/table.js really defines.

Measured over 34 shapes: zero disagreements. So this is coverage, not a fix --
and it is the coverage that would have caught a one-sided change to any of the
six.

The paleomap cases are the delicate ones and are here on purpose. The trigger
is an EXCLUSIVE key that actually holds rows: `metadata` and `confidence`
overlap every mode, and being strict on the key NAME without checking the
CONTENT is what made the old behaviour wrong in both directions -- seven real
responses with 24 populated tables classified as "no tables at all", while
seven genuinely empty range charts were let through as tabular.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.exporter import (  # noqa: E402
    _looks_abundance,
    _looks_columnar,
    _looks_occurrence_matrix,
    _looks_paleomap,
    _looks_phylogenetic_tree,
    _looks_zonation_chart,
)

PAIRS = [
    ("rcaLooksColumnar", _looks_columnar),
    ("rcaLooksAbundance", _looks_abundance),
    ("rcaLooksZonationChart", _looks_zonation_chart),
    ("rcaLooksPhylogeneticTree", _looks_phylogenetic_tree),
    ("rcaLooksPaleomap", _looks_paleomap),
    # AUDIT-2026-10-15: occurrence_matrix was added to exporter.py on
    # 2026-10-15 with NO entry here and NO shape below, so the parity suite
    # stayed green through a one-sided change — the exact failure this file
    # exists to prevent. `len(PAIRS) == 5` was the reason: the assertion pinned
    # the hand-written list to its own length instead of to the set of
    # detectors the code actually has, so the sixth pair could not be added
    # without also editing the assertion. That count is gone (see
    # test_every_python_detector_has_a_js_mirror).
    ("rcaLooksOccurrenceMatrix", _looks_occurrence_matrix),
]

_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const ctx = { console, TextEncoder, TextDecoder, URL };
ctx.globalThis = ctx; ctx.window = ctx;
vm.createContext(ctx);
for (const f of ['js/config.js', 'js/json-utils.js', 'js/ics_table.js',
                 'js/reason-codes.js', 'js/quality.js', 'js/aggregate.js',
                 'js/table.js']) {
  new vm.Script(fs.readFileSync(path.join(ROOT, f), 'utf8'),
                { filename: f }).runInContext(ctx);
}
const NAMES = %NAMES%;
// Report facts; do NOT exit(3) on a missing mirror. A driver that dies makes
// every test in this file fail as a collection error, which reads like a
// harness problem instead of the one-sided-mirror problem it actually is.
// A missing name maps to `null` in the matrix and the tests below say so.
const missing = NAMES.filter((n) => typeof ctx[n] !== 'function');
const defined = Object.keys(ctx).filter((k) => /^rcaLooks[A-Z]/.test(k)
  && typeof ctx[k] === 'function');
const out = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).map((d) =>
  NAMES.map((n) => (typeof ctx[n] === 'function' ? !!ctx[n](d) : null)));
console.log(JSON.stringify({ matrix: out, defined: defined, missing: missing }));
"""


def _species(extra=None):
    base = {"species": "A", "section": "S1",
            "range_base": "300 Ma", "range_top": "290 Ma"}
    if extra:
        base.update(extra)
    return base


# label -> payload. 28 shapes, chosen to straddle each detector's decision.
SHAPES = [
    ("empty", {}),
    ("range: one species", {"species_ranges": [_species()]}),
    ("range: two species",
     {"species_ranges": [_species(), _species({"species": "B"})]}),
    ("range: no species_ranges key", {"sections": [{"name": "S1"}]}),
    ("columnar: cross_beds",
     {"sections": [{"name": "S1"}], "cross_beds": [{"from": "S1", "to": "S2"}]}),
    ("columnar: empty cross_beds",
     {"sections": [{"name": "S1"}], "cross_beds": []}),
    ("columnar: only empty sections",
     {"sections": [], "cross_beds": [{"from": "a", "to": "b"}]}),
    ("abundance: rows",
     {"abundances": [{"taxon": "P", "site": "S1", "level": "3",
                      "abundance": "12"}]}),
    ("abundance: empty list", {"abundances": []}),
    ("abundance + species",
     {"abundances": [{"taxon": "P", "site": "S1", "level": "3"}],
      "species_ranges": [_species()]}),
    ("zonation: zones", {"zones": [{"name": "Z1", "age": "290-280 Ma"}]}),
    ("zonation: zones + correlations",
     {"zones": [{"name": "Z1"}],
      "correlations": [{"from_zone": "Z1", "to_zone": "Z2"}]}),
    ("zonation: only zonations", {"zonations": [{"name": "Z1"}]}),
    ("phylo: nodes + root_ids",
     {"nodes": [{"id": "r", "parent": None}], "root_ids": ["r"]}),
    ("phylo: nodes no root_ids", {"nodes": [{"id": "r", "parent": None}]}),
    ("phylo: empty nodes", {"nodes": [], "root_ids": []}),
    ("phylo: nodes + species_ranges",
     {"nodes": [{"id": "r", "parent": None}], "root_ids": ["r"],
      "species_ranges": [_species()]}),
    ("sections only", {"sections": [{"name": "S1"}]}),
    ("zones + nodes",
     {"zones": [{"name": "Z1"}], "nodes": [{"id": "r", "parent": None}],
      "root_ids": ["r"]}),
    ("data_points (chemical/scatter)", {"data_points": [{"sample_id": "D1"}]}),
    ("scatter points", {"points": [{"label": "P1", "x": 1, "y": 2}]}),
    # --- the paleomap exclusivity cases ---
    ("paleomap: continents", {"continents": [{"name": "Gondwana"}]}),
    ("paleomap: sites", {"fossil_sites": [{"name": "S1"}]}),
    ("paleomap: key present but EMPTY", {"continents": []}),
    ("paleomap: rows not dicts", {"continents": ["Gondwana"]}),
    ("paleomap: only metadata+confidence",
     {"metadata": {"title": "x"}, "confidence": 0.9}),
    ("paleomap + species_ranges",
     {"continents": [{"name": "G"}], "species_ranges": [_species()]}),
    ("paleomap: dict rows", {"continents": [{"name": "G"}]}),
    # --- occurrence-matrix cases (added 2026-10-15) ---
    ("occurrence: minimal",
     {"occurrences": [{"taxon": "A", "site": "S1", "code": "A",
                       "class": "abundant"}]}),
    ("occurrence: empty occurrences", {"occurrences": []}),
    ("occurrence: rows not dicts", {"occurrences": ["A"]}),
    # The generic_table shape. `_looks_occurrence_matrix` keyed on `columns`
    # once and claimed this, which deleted the generic `rows` from the export.
    ("generic_table: columns+rows+legend",
     {"columns": [{"index": 0, "label": "L1"}],
      "rows": [{"index": 0, "label": "r1"}],
      "legend": [{"code": "1", "class": "one"}]}),
    # A partially-migrated matrix answer carries abundance-shaped rows AND
    # columns; the branch ORDER (occurrence above abundance) is what resolves
    # it, and get_configs_for_result must not route it to the abundance tables.
    ("occurrence + abundances",
     {"occurrences": [{"taxon": "A", "site": "S1", "code": "A"}],
      "abundances": [{"taxon": "P", "site": "S1", "abundance": "12"}]}),
    ("occurrence + species_ranges",
     {"occurrences": [{"taxon": "A", "site": "S1", "code": "A"}],
      "species_ranges": [_species()]}),
]

_NAMES = [j for j, _ in PAIRS]


def _js_matrix():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dp = os.path.join(td, "d.json")
        drv = os.path.join(td, "v.js")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump([d for _, d in SHAPES], fh)
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER.replace("%NAMES%", json.dumps(_NAMES)))
        proc = subprocess.run(["node", drv, dp], capture_output=True, text=True,
                              encoding="utf-8", timeout=90,
                              env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
        assert proc.returncode == 0, (
            f"node driver failed: {proc.stderr[:400]}")
        payload = json.loads(proc.stdout)
        return payload["matrix"], set(payload["defined"]), list(payload["missing"])


_JS, _JS_MIRROR_NAMES, _JS_MISSING = _js_matrix()


@pytest.mark.parametrize("idx,label", list(enumerate(l for l, _ in SHAPES)),
                         ids=[l for l, _ in SHAPES])
def test_both_engines_classify_every_shape_the_same_way(idx, label):
    data = SHAPES[idx][1]
    for k, (js_name, py_fn) in enumerate(PAIRS):
        js_val = _JS[idx][k]
        py_val = bool(py_fn(data))
        assert js_val is not None, (
            f"{label}: {js_name} does not exist in js/table.js. The two "
            f"engines would classify this shape differently because only one "
            f"of them has the detector."
        )
        assert py_val == js_val, (
            f"{label}: {py_fn.__name__} says {py_val} and "
            f"{js_name} says {js_val}. get_configs_for_result dispatches the "
            f"whole export on these, so the two engines would write different "
            f"sheets for the same result."
        )


def test_every_detector_in_pairs_is_a_callable_on_both_sides():
    """The scope assertion: a detector added to one side only would otherwise
    route exports differently with nothing failing.

    AUDIT-2026-10-15: this used to assert ``len(PAIRS) == 5``, which pinned the
    hand-written list to its own length rather than to the set of detectors the
    code actually has — so adding `_looks_occurrence_matrix` to exporter.py
    alone left the suite green. It now enumerates the real `_looks_*` functions
    out of the exporter module and demands a JS mirror for each, which is the
    question the assertion was supposed to ask.
    """
    import inspect

    from rca_core import exporter as _exporter

    py_detectors = {
        name for name, obj in vars(_exporter).items()
        if name.startswith("_looks_") and inspect.isfunction(obj)
    }
    covered = {fn.__name__ for _, fn in PAIRS}
    assert py_detectors == covered, (
        "every _looks_* detector in rca_core/exporter.py needs an entry in "
        f"PAIRS (with its js/table.js mirror). "
        f"only in exporter: {sorted(py_detectors - covered)}; "
        f"only in PAIRS: {sorted(covered - py_detectors)}"
    )
    for js_name, py_fn in PAIRS:
        assert callable(py_fn), f"{py_fn} is not callable on the Python side"


def test_every_python_detector_has_a_js_mirror():
    """Executed, not just listed: proves js/table.js really defines each name.

    PAIRS naming a function that js/table.js does not define would make every
    shape fail on a `None` verdict, which reads like a disagreement about the
    payload rather than a missing function. This names the gap directly.
    """
    js_names = [n for n, _ in PAIRS]
    assert len(js_names) == len(set(js_names)), \
        "PAIRS lists the same JS mirror twice"
    assert not _JS_MISSING, (
        f"PAIRS pairs these with js/table.js functions that do not exist: "
        f"{_JS_MISSING}. Defined instead: {sorted(_JS_MIRROR_NAMES)}"
    )
    for js_name in js_names:
        assert js_name in _JS_MIRROR_NAMES, (
            f"{js_name} is paired in PAIRS but was not picked up from "
            f"js/table.js — the two engines would dispatch differently. "
            f"js/table.js defines: {sorted(_JS_MIRROR_NAMES)}"
        )


def test_paleomap_needs_an_exclusive_key_that_holds_rows():
    """Pinned separately because it is the rule that was wrong in BOTH
    directions before item 4.2: strict on the key name, wrong on the content."""
    assert _looks_paleomap({"continents": [{"name": "G"}]}) is True
    assert _looks_paleomap({"continents": []}) is False, \
        "a paleomap key with no rows is not a paleomap"
    assert _looks_paleomap({"continents": ["G"]}) is False, \
        "rows that are not objects are not tables"
    assert _looks_paleomap({"metadata": {"title": "x"},
                            "confidence": 0.9}) is False, \
        "metadata and confidence overlap every mode and must not trigger it"
    for js_name, py_fn in PAIRS:
        if py_fn is _looks_paleomap:
            continue
        assert py_fn({"continents": [{"name": "G"}]}) is False, \
            f"{py_fn.__name__} must not claim a paleomap payload"
