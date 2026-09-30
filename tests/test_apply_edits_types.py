r"""A zero-edit Apply-Edits pass must not change a value's TYPE, either.

The key-shape half of this is test_apply_edits_noop.py. This file covers the
value half, and it found the one case that survived there: the untouched probe
compared values NUMERICALLY (``_norm`` maps 84 and 84.0 to the same thing), so
an int silently becoming a float looked identical.

The phylo ``nodes`` table declared ``support`` as ``"float"``. The model holds
it as an int -- 84 / 94 / 76 in the gold fixtures, ``rng.randint(75, 98)`` in
tests/fixtures/synthetic_data.py -- the cell renders ``str(84) = "84"``, and
``float("84") = 84.0``. So every Apply-edits pass, including one with zero
edits, rewrote 3 of 8 nodes.

The reason it was invisible until now: numerically nothing changed, and the
untouched probe normalised numbers before comparing. The reason it mattered:
``_build_newick_node`` formats support as ``f"{support}"``, so the exported
Newick went from ``(...)84;`` to ``(...)84.0;`` after a pass in which the
operator changed nothing. A file export that differs because you looked at it
is the same defect as one that differs because you edited it.

``"number"`` reproduces the model's own type distribution exactly (measured:
model {None: 5, int: 3}; ``"float"`` -> {None: 5, float: 3}; ``"number"`` ->
{None: 5, int: 3}) and still returns a float for a genuinely fractional
value, so 95.5 survives as 95.5.

The tests below are written so the general property is asserted, not the one
column: every typed numeric column of every gold table, compared with STRICT
type equality, so the next "float"-typed int column fails on its own.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.exporter import (  # noqa: E402
    COL_TYPES, apply_table_edits, build_table_export, get_configs_for_result,
)

GOLD_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures", "gold")
T = lambda k: k  # noqa: E731

# The one normalisation the "number" tag exists to perform on sections. A
# columnar section's thickness is stored as the string the normalizer produced
# ('11') and the tag turns it into the number downstream checks want; the
# COL_TYPES comment says so. Anything else is a defect.
EXPECTED_STRING_TO_NUMBER = {
    ("cs_synth_001", "sections"),
    ("cs_synth_002", "sections"),
}

NUMERIC_TYPES = {"int", "float", "number"}


def _load_gold():
    out = []
    for dirpath, _d, files in os.walk(GOLD_DIR):
        for fn in files:
            if fn == "ground_truth.json":
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    out.append((os.path.basename(os.path.dirname(p)), json.load(f)))
    return sorted(out)


GOLD = _load_gold()


def _type_shifts(before, after, path=()):
    """Paths whose value changed TYPE (not merely value)."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) & set(after)):
            out += _type_shifts(before[k], after[k], path + (k,))
        return out
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return []
        out = []
        for i, (b, a) in enumerate(zip(before, after)):
            out += _type_shifts(b, a, path + (i,))
        return out
    tb, ta = type(before), type(after)
    if tb is not ta and not (tb is bool or ta is bool):
        # A numeric value that only changed between int and float is a TYPE
        # shift for our purposes: int 84 and float 84.0 serialise differently
        # and format differently through f"{...}".
        numeric = {int, float}
        if {tb, ta} <= numeric:
            return [(".".join(map(str, path)), f"{before!r}({tb.__name__}) "
                                             f"-> {after!r}({ta.__name__})")]
        return []
    return []


def test_gold_fixtures_exist():
    assert len(GOLD) == 8, f"expected 8 gold cases, found {len(GOLD)}"


@pytest.mark.parametrize("case_id,data", GOLD, ids=[c for c, _ in GOLD])
def test_untouched_pass_changes_no_value_type(case_id, data):
    work = copy.deepcopy(data)
    for tid in [c["id"] for c in get_configs_for_result(copy.deepcopy(work))]:
        _h, rows = build_table_export(work, tid, T)
        if rows:
            apply_table_edits(work, tid, [list(r) for r in rows])
    shifts = _type_shifts(data, work)
    if (case_id, "sections") in EXPECTED_STRING_TO_NUMBER:
        shifts = [s for s in shifts if "thickness_m" not in s[0]]
    assert shifts == [], f"{len(shifts)} value type(s) changed: {shifts}"


# --- the column itself, named so the reason is not lost ----------------


def test_support_is_declared_as_number_not_float():
    assert COL_TYPES["nodes"]["support"] == "number", (
        "an int percentage must not be rewritten as a float on every edit"
    )


def test_support_keeps_its_type_through_an_untouched_pass():
    path = os.path.join(GOLD_DIR, "phylogenetic_tree", "pt_synth_001",
                        "ground_truth.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    before = [type(n.get("support")).__name__ for n in data["nodes"]]
    assert "int" in before, f"fixture no longer exercises an int support: {before}"

    work = copy.deepcopy(data)
    _h, rows = build_table_export(work, "nodes", T)
    apply_table_edits(work, "nodes", [list(r) for r in rows])
    after = [type(n.get("support")).__name__ for n in work["nodes"]]
    assert after == before, f"support types {before} -> {after}"


def test_a_fractional_support_is_still_a_float():
    """The other direction: "number" must not flatten 95.5 to 95."""
    path = os.path.join(GOLD_DIR, "phylogenetic_tree", "pt_synth_001",
                        "ground_truth.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["nodes"][0]["support"] = 95.5
    work = copy.deepcopy(data)
    _h, rows = build_table_export(work, "nodes", T)
    apply_table_edits(work, "nodes", [list(r) for r in rows])
    assert work["nodes"][0]["support"] == 95.5, work["nodes"][0]["support"]
    assert isinstance(work["nodes"][0]["support"], float)


def test_the_newick_spelling_is_unchanged_by_an_untouched_pass():
    """The consequence that made this visible, asserted at the output.

    _build_newick_node formats support with f"{support}", so int 84 and float
    84.0 produce different bytes in the exported tree for the same data.
    """
    from rca_core.extractor import to_newick

    path = os.path.join(GOLD_DIR, "phylogenetic_tree", "pt_synth_001",
                        "ground_truth.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    before = to_newick(copy.deepcopy(data))
    work = copy.deepcopy(data)
    _h, rows = build_table_export(work, "nodes", T)
    apply_table_edits(work, "nodes", [list(r) for r in rows])
    after = to_newick(work)
    assert after == before, "the Newick export changed after a zero-edit pass"


def test_typing_a_fractional_support_by_hand_still_works():
    """The operator path, not just the round trip.

    The column index is read off the headers rather than hardcoded: a literal
    6 vs 7 here is the same class of error as the path-length threshold that
    reddened CI, and it fails for a reason that has nothing to do with the
    behaviour under test.
    """
    path = os.path.join(GOLD_DIR, "phylogenetic_tree", "pt_synth_001",
                        "ground_truth.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    work = copy.deepcopy(data)
    headers, rows = build_table_export(work, "nodes", T)
    support_col = headers.index("col.support")
    rows[1][support_col] = "99.5"
    apply_table_edits(work, "nodes", rows)
    assert work["nodes"][1]["support"] == 99.5, work["nodes"][1]["support"]
    assert isinstance(work["nodes"][1]["support"], float)
