r"""The export invariant that can kill a whole export, tested and found correct.

validate_export_invariants returns (ok, issues, warnings), and to_xlsx RAISES
when ok is False. So a false positive here is not a cosmetic warning -- it
destroys the entire workbook for that result. The comment on the "required"
rule records that this already happened: "treating an empty string as a
missing REQUIRED field made validate_export_invariants fail on perfectly valid
results and to_xlsx raised ValueError -- i.e. whole-workbook export died on
valid data".

The range_base_le_range_top constraint does real arithmetic on mixed Bed /
numeric endpoints, which is where a wrong verdict is plausible: it either
accuses the user of an inverted range or clears an inverted one. Nothing tested
it, so this file pins it.

MEASURED RESULT: the constraint is CORRECT on all 28 pairs below, and no gold
result is flagged. That is a negative result and it is still worth committing
-- the alternative is leaving the only export-blocking constraint untested
against the next change to bed_parser.

One note on the fixtures: a first draft of this file asserted that
base="Bed 23c", top="23.5" should be flagged as inverted. It should not -- 23.5
lies between bed 23 and bed 24, so 23c -> 23.5 is ascending, and the code
already says so. The fixture was wrong, not the validator, and it is written
here with the reasoning attached so nobody "fixes" it the other way.
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
    EXPORT_INVARIANTS, validate_export_invariants,
)

GOLD_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures", "gold")


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

CONSTRAINT = "range_base_le_range_top"


def _flagged(base, top):
    """Does the constraint fire? Required/warn issues are a separate concern."""
    data = {"species_ranges": [{"species": "S", "section": "X",
                                "range_base": base, "range_top": top}]}
    _ok, issues, _warns = validate_export_invariants(data)
    return any(i.get("constraint") == CONSTRAINT for i in issues)


# (base, top, should_be_flagged, why)
PAIRS = [
    ("1", "10", False, "ascending"),
    ("10", "1", True, "descending"),
    ("Bed 23", "Bed 25", False, "ascending beds"),
    ("Bed 25", "Bed 23", True, "descending beds"),
    ("Bed 23a", "Bed 23b", False, "same bed, a before b"),
    ("Bed 23b", "Bed 23a", True, "same bed, b after a"),
    ("Bed 23a", "Bed 24", False, "last subscript -> next bed"),
    ("Bed 24", "Bed 23a", True, "next bed -> last subscript"),
    ("23a", "23b", False, "bare subscript, ascending"),
    ("23b", "23a", True, "bare subscript, descending"),
    ("Bed 23a", "23", True, "a subscript sits ABOVE the bare integer, so "
                            "23a -> 23 is inverted"),
    ("23", "Bed 23a", False, "bare integer -> subscript, ascending"),
    ("Bed 23a", "23.5", False, "23.5 lies between bed 23 and 24, so it is "
                               "ABOVE 23c -- ascending, not inverted"),
    ("Bed 24", "23.5", True, "24 -> 23.5 is descending"),
    ("23.5", "Bed 24", False, "23.5 -> 24 is ascending"),
    ("", "10", False, "blank base is legal extractor output (warning, not issue)"),
    ("1", "", False, "blank top is legal (open-ended FAD/LAD)"),
    ("Bed 1", "", False, "blank top is legal"),
    ("", "Bed 1", False, "blank base is legal"),
    ("1", "not a bed", False, "unparseable top is ignored, per the rule"),
    ("not a bed", "1", False, "unparseable base is ignored"),
    ("not a bed", "other label", False, "both unparseable: ignored"),
    ("Bed 23a", "not a bed", False, "unparseable top is ignored"),
    ("not a bed", "Bed 23a", False, "unparseable base is ignored"),
    ("1e2", "2e2", False, "scientific notation ascending"),
    ("2e2", "1e2", True, "scientific notation descending"),
    ("-5", "5", False, "negative base ascending"),
    ("5", "-5", True, "descending into negatives"),
    ("1.0", "2.0", False, "float spellings ascending"),
    ("2.0", "1.0", True, "float spellings descending"),
]


@pytest.mark.parametrize("base,top,want,why", PAIRS,
                         ids=[f"{b}-to-{t}" for b, t, _w, _r in PAIRS])
def test_range_constraint_verdict(base, top, want, why):
    assert _flagged(base, top) is want, (
        f"range_base={base!r} range_top={top!r}: expected flagged={want} ({why})"
    )


def test_the_constraint_is_actually_wired_up():
    """A negative result is only meaningful if the check can go off."""
    fired = sum(1 for b, t, w, _r in PAIRS if _flagged(b, t) is True)
    assert fired >= 8, f"only {fired} of the fixtures flag -- the test is vacuous"
    assert CONSTRAINT in EXPORT_INVARIANTS["species_ranges"]["constraints"]


def test_pair_table_is_symmetric():
    """Every ascending pair has a descending twin in the table, so deleting a
    row cannot quietly unbalance it.

    Compared on (base, top, want) only -- the `why` prose is deliberately
    different for the two directions, so including it in the comparison makes
    every row look asymmetric.
    """
    triples = {(b, t, w) for b, t, w, _why in PAIRS}
    for b, t, want, _why in PAIRS:
        if want is True:
            assert (t, b, False) in triples, f"({b!r},{t!r}) has no reverse entry"


# --- the property that matters most: no valid result is accused ---------


def test_gold_fixtures_exist():
    assert len(GOLD) == 8, f"expected 8 gold cases, found {len(GOLD)}"


@pytest.mark.parametrize("case_id,data", GOLD, ids=[c for c, _ in GOLD])
def test_no_gold_result_is_flagged(case_id, data):
    """The blunt form of the property, and the one that has bitten before.

    A false positive here raises ValueError in to_xlsx and takes the whole
    workbook with it, so this is asserted directly rather than inferred from
    the pair table.
    """
    ok, issues, _warnings = validate_export_invariants(copy.deepcopy(data))
    assert ok is True, f"{case_id} was blocked from export: {json.dumps(issues)}"
    assert issues == [], f"{case_id}: {json.dumps(issues, ensure_ascii=False)}"


def test_xlsx_really_does_raise_on_a_false_positive():
    """Proves the stakes rather than asserting them in a comment."""
    import io

    from rca_core.exporter import to_xlsx

    broken = {"species_ranges": [
        {"species": "S", "section": "X", "range_base": "10", "range_top": "1"},
    ]}
    with pytest.raises(ValueError):
        to_xlsx(broken, io.BytesIO())

    # ...and the same shape, ascending, exports. Note to_xlsx SAVES INTO a
    # stream passed as file_or_path and returns None; only the no-argument form
    # returns bytes, so the result is checked on the buffer.
    buf = io.BytesIO()
    assert to_xlsx(
        {"species_ranges": [
            {"species": "S", "section": "X",
             "range_base": "1", "range_top": "10"},
        ]},
        buf,
    ) is None
    assert buf.getvalue()[:2] == b"PK", "an .xlsx is a zip; nothing was written"
