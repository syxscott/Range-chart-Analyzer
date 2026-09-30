r"""Two "FAD <= LAD" checks had to agree, and one of them dropped subscripts.

  rca_core.exporter  range_base_le_range_top  -> an issue, and to_xlsx RAISES
  rca_core.quality   _score_consistency (2)    -> a warning + a score penalty

The disagreement was not symmetric, which is what made it worth fixing.
Measured over 28 shapes, the ONLY direction that ever disagreed was:

    base="23b" top="23a"   exporter: inverted (blocks the export)  quality: fine
    base="23a" top="23"    exporter: inverted (blocks the export)  quality: fine
    base="23c" top="23"    exporter: inverted (blocks the export)  quality: fine

So the user got a validation error with no explanation, and a quality grade
that was not penalised for an inversion the same file could prove.

The cause is _parse_bed_n, which takes the first integer in the string:
"23a" and "23b" are both bed 23, so the comparison says they agree. A subscript
letters UPWARD, so base=23b / top=23a IS inverted, and so is 23a / 23.

This is the defect rca_core/bed_parser.py was created to end -- its own
docstring records "a predicted Bed 23c and ground truth Bed 23d would both
score as integer 23 -> false positive accuracy". The M-1 fix unified
eval_metrics and exporter through the shared parser. quality.py kept a third,
weaker one, and nobody noticed because the two checks had no test comparing
them.

The shape table below is the parity itself: if a future change makes the two
disagree on any of these, the export either blocks a valid range or the quality
report accuses a valid one. Ages are included because quality routes them
through _looks_like_age and the exporter does not -- they are in the table to
prove the two AGREE about skipping them, not to assert they are treated
identically.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import quality as Q  # noqa: E402
from rca_core.exporter import validate_export_invariants  # noqa: E402

# (base, top) -- the shapes where BOTH implementations have an opinion.
SHAPES = [
    ("1", "10"), ("10", "1"), ("1", "1"),
    ("Bed 1", "Bed 10"), ("Bed 10", "Bed 1"),
    ("23", "24"), ("24", "23"),
    ("23a", "23b"), ("23b", "23a"),
    ("23a", "24"), ("24", "23a"),
    ("23", "23a"), ("23a", "23"),
    ("23c", "23"), ("23", "23c"),
    ("23z", "24"), ("24", "23z"),
    ("1.0", "2.0"), ("2.0", "1.0"),
    ("1.5", "2.5"), ("2.5", "1.5"),
    ("", "10"), ("1", ""), ("", ""),
    ("300 Ma", "250 Ma"), ("250 Ma", "300 Ma"),
    ("23", "not a bed"), ("not a bed", "23"),
]


def _exporter(base, top):
    _ok, issues, _w = validate_export_invariants({
        "species_ranges": [{
            "species": "S", "section": "X",
            "range_base": base, "range_top": top,
        }],
    })
    return any(i.get("constraint") == "range_base_le_range_top" for i in issues)


def _quality(base, top):
    _score, issues = Q._score_consistency({
        "species_ranges": [{
            "species": "S", "section": "X", "biozone": "Z",
            "range_base": base, "range_top": top,
        }],
    })
    return any(i.get("msg_key") == "quality.fad_lt_lad" for i in issues)


def _accuracy(base, top):
    """The 0.40-weighted dimension. It made the SAME judgement as the exporter
    and, like the consistency check, forgave exactly the sub-bed inversions."""
    _score, issues = Q._score_accuracy({
        "species_ranges": [{
            "species": "S", "section": "X",
            "range_base": base, "range_top": top,
        }],
    })
    return any(i.get("msg_key") == "quality.range_top_lt_base" for i in issues)


@pytest.mark.parametrize("base,top", SHAPES,
                         ids=[f"{b}-to-{t}" for b, t in SHAPES])
def test_the_two_fad_lad_checks_agree(base, top):
    e = _exporter(base, top)
    q = _quality(base, top)
    assert e == q, (
        f"range_base={base!r} range_top={top!r}: exporter says inverted={e}, "
        f"quality says inverted={q}"
        + ("  (the exporter RAISES on this, so the user gets a validation error "
           "with no explanation)" if e else
           "  (the quality grade is inflated)")
    )


# --- the sub-bed cases, named so the reason cannot rot away --------------


@pytest.mark.parametrize("base,top", [
    ("23b", "23a"),      # subscript descending
    ("23a", "23"),       # subscript down to the bare bed
    ("23c", "23"),
    ("23z", "23a"),
    ("23z", "23b"),
])
def test_a_sub_bed_inversion_is_reported(base, top):
    """A subscript letters upward, so these are inverted even though the bed
    NUMBERS are equal -- which is the entire point."""
    assert _quality(base, top) is True, f"{base!r} -> {top!r} went unreported"
    assert _exporter(base, top) is True


@pytest.mark.parametrize("base,top", [
    ("23a", "23b"),      # subscript ascending
    ("23b", "23c"),
    ("23", "23a"),
    ("23a", "24"),
])
def test_a_sub_bed_in_ascending_order_is_not_reported(base, top):
    assert _quality(base, top) is False, f"{base!r} -> {top!r} was false-flagged"
    assert _exporter(base, top) is False


# --- the same judgement in the 0.40-weighted dimension -----------------


@pytest.mark.parametrize("base,top", [
    ("23b", "23a"), ("23a", "23"), ("23c", "23"), ("23z", "23a"),
])
def test_accuracy_dimension_reports_the_sub_bed_inversion(base, top):
    assert _accuracy(base, top) is True, (
        f"{base!r} -> {top!r}: the exporter blocks this and the 0.40-weighted "
        f"accuracy dimension reported nothing"
    )
    assert _exporter(base, top) is True


@pytest.mark.parametrize("base,top", [
    ("23a", "23b"), ("23b", "23c"), ("23", "23a"), ("23a", "24"),
])
def test_accuracy_dimension_does_not_false_flag_sub_beds(base, top):
    assert _accuracy(base, top) is False, f"{base!r} -> {top!r} was false-flagged"


def test_both_quality_dimensions_agree_where_they_overlap():
    """On the cases BOTH dimensions cover, they must not drift apart.

    They reached this by different edits -- consistency first, accuracy a
    commit later -- and only two calls to one shared helper keep a third edit
    from being needed.

    Scoped to BED-shaped endpoints on purpose. A first draft asserted agreement
    over the whole table and failed on base="250 Ma" / top="300 Ma", where
    accuracy flags and consistency does not. That is not a drift: the accuracy
    dimension checks AGES (its convention is base=FAD=OLDER=LARGER Ma, so
    250 -> 300 is a genuine inversion) while the consistency dimension
    deliberately covers only "cross-field / per-row invariants that the other
    three dimensions do NOT cover", as its own docstring says. Overlap is the
    thing being asserted, so the non-overlapping shapes are excluded rather
    than quietly counted as agreement.
    """
    non_bed = ("", "Ma", "myr", "kyr")
    for base, top in SHAPES:
        if any(u in base for u in non_bed) or any(u in top for u in non_bed):
            continue
        assert _accuracy(base, top) == _quality(base, top), (
            f"{base!r} -> {top!r}: accuracy={_accuracy(base, top)} "
            f"consistency={_quality(base, top)}"
        )


def test_the_accuracy_dimension_is_the_only_one_that_checks_ages():
    """Stated so the asymmetry reads as design rather than as a gap nobody
    noticed -- and so that if accuracy ever stops checking ages, someone sees
    the coverage disappear here."""
    assert _accuracy("250 Ma", "300 Ma") is True, (
        "250 Ma -> 300 Ma is inverted (base must be the LARGER Ma) and the "
        "accuracy dimension is the one that knows it"
    )
    assert _quality("250 Ma", "300 Ma") is False
    assert _exporter("250 Ma", "300 Ma") is False, (
        "the exporter cannot read an age as a bed and skips it"
    )


def test_the_comparison_is_not_vacuous():
    """Both verdicts must appear in the table, or "they agree" means nothing."""
    verdicts = {(_exporter(b, t), _quality(b, t)) for b, t in SHAPES}
    assert (True, True) in verdicts, "no shape is flagged by either side"
    assert (False, False) in verdicts, "no shape is clear to either side"
    assert verdicts == {(True, True), (False, False)}, (
        f"a mixed verdict is back: {verdicts}"
    )
