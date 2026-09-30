r"""A fabricated data point was reported as unanimous cross-run consensus.

merge_results mode-merges each field of a row independently, so the merged row
can be a COMBINATION no single extraction ever produced. _is_chimeric_row
exists to catch that, and its own docstring says why: "emitting such a row as if
it were a real consensus is misleading".

The key it compared on was HARDCODED to the range-chart field names:

    keys = ("range_base", "range_top", "biozone", "section")      # Python
    const keys = ['range_base', ...];                            # JS

An abundance row has none of those keys, so the tuple came out all-empty, the
`if not any(...)` guard returned False for EVERY row, and the safeguard was
inert for the abundance, columnar and zonation schemas -- exactly the schemas
whose rows carry the most mode-merged fields.

Measured, on three runs whose per-field 2-of-3 modes each came from a
different row:

    r1  (depth 120cm, abundance 35, unit %)
    r2  (depth 120cm, abundance 40, unit ind)
    r3  (depth 150cm, abundance 35, unit ind)

No run ever saw (120cm, 35, ind), and the merged output carried exactly that as

    "abundance": "35", "abundance_unit": "ind", "agreement": "3/3"

-- a fabricated measurement labelled unanimous, in the column a researcher
would cite. The range-chart equivalent of the same construction was correctly
flagged, so the two schemas disagreed about whether fabrication was consensus.

Both engines were fixed together from the schema's own declaration of which
fields it mode-merges. The frontend parity fixture is what proved they had to
move together: fixing only Python turned the committed fixtures red.

NOTE on the fixtures below: they are written out per schema rather than
generated from the schema's field lists. A first draft tried to build them
programmatically and failed -- for five of the schemas it produced payloads the
merger did not recognise as that mode at all, so the "recombined" tests were
passing against nothing. The property is schema-agnostic in STATEMENT but the
data has to be real-shaped, and saying so is cheaper than a clever builder
that silently tests the wrong thing.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.aggregate import (  # noqa: E402
    ABUNDANCE_DIAGRAM_SCHEMA, COLUMNAR_SECTION_SCHEMA, PHYLOGENETIC_TREE_SCHEMA,
    RANGE_CHART_SCHEMA, ZONATION_CHART_SCHEMA, _norm, merge_results,
)

SCHEMAS = [
    ("range_chart", RANGE_CHART_SCHEMA),
    ("columnar", COLUMNAR_SECTION_SCHEMA),
    ("abundance", ABUNDANCE_DIAGRAM_SCHEMA),
    ("phylo", PHYLOGENETIC_TREE_SCHEMA),
    ("zonation", ZONATION_CHART_SCHEMA),
]


def _run(payload):
    p = dict(payload)
    p.setdefault("confidence", 0.8)
    p.setdefault("runs", 1)
    return p


# --- the two constructions that were MEASURED to fabricate a row ---------
# (base, top, biozone) modes: 10 from r1/r2, 20 from r1/r3, Z1 from r2/r3.
RANGE_CHART_CHIMERA = [
    _run({"species_ranges": [{"species": "Genus A", "section": "Sec",
                             "range_base": "Bed 10", "range_top": "Bed 20",
                             "biozone": "Z2"}]}),
    _run({"species_ranges": [{"species": "Genus A", "section": "Sec",
                             "range_base": "Bed 10", "range_top": "Bed 30",
                             "biozone": "Z1"}]}),
    _run({"species_ranges": [{"species": "Genus A", "section": "Sec",
                             "range_base": "Bed 20", "range_top": "Bed 20",
                             "biozone": "Z1"}]}),
]

# (depth, abundance, unit) modes: 120cm from r1/r2, 35 from r1/r3, ind from r2/r3.
ABUNDANCE_CHIMERA = [
    _run({"abundances": [{"taxon": "Pinus", "site": "Core A", "level": "L1",
                         "depth": "120 cm", "abundance": "35",
                         "abundance_unit": "%"}]}),
    _run({"abundances": [{"taxon": "Pinus", "site": "Core A", "level": "L1",
                         "depth": "120 cm", "abundance": "40",
                         "abundance_unit": "ind"}]}),
    _run({"abundances": [{"taxon": "Pinus", "site": "Core A", "level": "L1",
                         "depth": "150 cm", "abundance": "35",
                         "abundance_unit": "ind"}]}),
]

CASES = [
    ("range_chart", RANGE_CHART_SCHEMA, "species_ranges", RANGE_CHART_CHIMERA),
    ("abundance", ABUNDANCE_DIAGRAM_SCHEMA, "abundances", ABUNDANCE_CHIMERA),
]


def _observed(runs, schema, list_key):
    out = set()
    for r in runs:
        for row in r.get(list_key) or []:
            ident = tuple(_norm(row.get(k, "")) for k in schema.primary_id_keys)
            content = tuple(_norm(row.get(k, ""))
                            for k in schema.primary_str_mode_fields)
            out.add((ident, content))
    return out


def _fabricated(rows, runs, schema, list_key):
    seen = _observed(runs, schema, list_key)
    out = []
    for row in rows:
        ident = tuple(_norm(row.get(k, "")) for k in schema.primary_id_keys)
        content = tuple(_norm(row.get(k, ""))
                        for k in schema.primary_str_mode_fields)
        if (ident, content) not in seen:
            out.append(row)
    return out


def _is_flagged(row):
    return (row.get("_warning") == "recombined_consensus"
            or row.get("_chimera_recombined") is True)


@pytest.mark.parametrize("name,schema,key,runs", CASES, ids=[c[0] for c in CASES])
def test_the_construction_does_fabricate_a_row(name, schema, key, runs):
    """The control. Without it, the test below could pass vacuously -- which is
    exactly what happened when the first draft of this file built its payloads
    from the schema field lists and the merger produced no rows at all."""
    merged = merge_results(runs)
    rows = merged.get(key) or []
    assert rows, f"{name}: the construction produced no rows"
    assert _fabricated(rows, runs, schema, key), (
        f"{name}: the construction no longer fabricates a row, so the flag "
        f"test below is vacuous"
    )


@pytest.mark.parametrize("name,schema,key,runs", CASES, ids=[c[0] for c in CASES])
def test_a_fabricated_row_is_always_flagged(name, schema, key, runs):
    merged = merge_results(runs)
    rows = merged.get(key) or []
    for row in rows:
        ident = tuple(_norm(row.get(k, "")) for k in schema.primary_id_keys)
        content = tuple(_norm(row.get(k, ""))
                        for k in schema.primary_str_mode_fields)
        if (ident, content) in _observed(runs, schema, key):
            continue
        assert _is_flagged(row), (
            f"{name}: row {row} combines values no single run produced and "
            f"carries no recombined_consensus flag -- it would be reported to "
            f"a researcher as agreement={row.get('agreement')!r}"
        )


@pytest.mark.parametrize("name,schema,key,runs", CASES, ids=[c[0] for c in CASES])
def test_a_fabricated_row_is_surfaced_to_the_caller(name, schema, key, runs):
    """Marking the row is not enough on its own -- the operator has to be told,
    which is what chimera_warnings is for."""
    merged = merge_results(runs)
    assert merged.get("chimera_warnings"), (
        f"{name}: a fabricated row was flagged but chimera_warnings is empty, "
        f"so nothing reaches the operator"
    )


@pytest.mark.parametrize("name,schema,key,runs", CASES, ids=[c[0] for c in CASES])
def test_ballots_are_recorded_so_a_researcher_can_adjudicate(name, schema, key,
                                                           runs):
    """The AUDIT-2026-09-27 replacement for deleting the row: show every
    reading and its vote count, so the disagreement can be resolved rather than
    hidden."""
    merged = merge_results(runs)
    for row in merged.get(key) or []:
        if not _is_flagged(row):
            continue
        ballots = row.get("_recombination_ballots")
        assert ballots, f"{name}: flagged row carries no ballots"
        assert sum(b["votes"] for b in ballots) == len(runs), (
            f"{name}: ballots account for {sum(b['votes'] for b in ballots)} "
            f"votes across {len(runs)} runs"
        )
        for b in ballots:
            for field in schema.primary_str_mode_fields:
                assert field in b, f"{name}: ballot omits {field!r}: {b}"


# --- the other direction: agreement must not be flagged ------------------


@pytest.mark.parametrize("name,schema,key,runs", [
    ("range_chart", RANGE_CHART_SCHEMA, "species_ranges", [
        _run({"species_ranges": [{"species": "Genus A", "section": "Sec",
                                 "range_base": "Bed 10", "range_top": "Bed 20",
                                 "biozone": "Z1"}]}),
    ] * 3),
    ("abundance", ABUNDANCE_DIAGRAM_SCHEMA, "abundances", [
        _run({"abundances": [{"taxon": "Pinus", "site": "Core A",
                             "level": "L1", "depth": "120 cm",
                             "abundance": "35", "abundance_unit": "%"}]}),
    ] * 3),
], ids=["range_chart", "abundance"])
def test_unanimous_input_is_a_plain_consensus(name, schema, key, runs):
    """Otherwise the flag becomes noise an operator learns to ignore."""
    merged = merge_results(runs)
    rows = merged.get(key) or []
    assert rows, f"{name}: unanimous input merged to no rows"
    flagged = [r for r in rows if _is_flagged(r)]
    assert flagged == [], f"{name}: unanimous input was flagged: {flagged}"


# --- the key comes from the schema, not a literal -----------------------


def test_the_python_guard_takes_its_key_as_a_parameter():
    import ast
    import pathlib

    src = (pathlib.Path(PROJECT_ROOT) / "rca_core" / "aggregate.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "_is_chimeric_row")
    assert "keys" in [a.arg for a in fn.args.args], (
        "_is_chimeric_row must take the key as a parameter"
    )
    literals = [n for n in ast.walk(fn)
                if isinstance(n, ast.Tuple) and n.elts
                and all(isinstance(e, ast.Constant) for e in n.elts)]
    assert literals == [], (
        f"_is_chimeric_row still hardcodes a key: "
        f"{[ast.unparse(t) for t in literals]}"
    )


def test_the_js_guard_takes_its_key_as_a_parameter():
    """Both engines, because the parity fixture is what proves they agree."""
    import pathlib
    import re

    src = (pathlib.Path(PROJECT_ROOT) / "js" / "aggregate.js").read_text(
        encoding="utf-8")
    body = src[src.index("function rcaIsChimericRow"):]
    body = body[:body.index("\n}\n") + 3]
    assert re.search(r"function rcaIsChimericRow\(\s*group\s*,\s*merged\s*,\s*keys\s*\)",
                     body), (
        "the JS guard must take the keymap's strModeFields as a third parameter"
    )
    # Not "the function must not mention strModeFields" -- a first draft wrote
    # it that way and tripped over its own explanatory comment. The thing that
    # matters is that no FIELD-NAME ARRAY is spelled out in the body.
    assert not re.search(r"\[(['\"])(range_base|species_ranges)\1\s*,", body), (
        "js/aggregate.js still spells out the range-chart key array"
    )
    assert "rcaIsChimericRow(group, aggr, km.strModeFields)" in src, (
        "the JS call site must pass the keymap's strModeFields"
    )


@pytest.mark.parametrize("name,schema", SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_every_schema_declares_the_fields_its_guard_needs(name, schema):
    assert schema.primary_str_mode_fields, (
        f"{name} declares no mode-merged fields, so its chimera guard could "
        f"never fire"
    )
