r"""The Steno check's pair ORDER was alphabetical when two species shared a bed.

_score_biozone_order positions each species, sorts, then reads
sorted_sp[i] as the "younger" slot and sorted_sp[i+1] as the "older" one,
flagging a violation when the younger-slot species really is younger in time.

Position came from _parse_bed_n, so "23a" and "23c" are BOTH 23.0 and the sort
fell through to its tie-break:

    positioned.sort(key=lambda item: (item[0], str(item[1].get("species") or "")))

which is the SPECIES LABEL. So which of two same-bed species was treated as
the younger one was decided alphabetically.

Measured, that produced BOTH error directions:

    A@23c (younger biozone) + B@23a   not flagged, though A@24/B@23 is
    A@23a (younger biozone) + B@23c   not flagged, though A@23/B@24 would be
    A@23a (older biozone)   + B@23c   FLAGGED, though A@23/B@24 is not

A different failure shape from the FAD/LAD gaps fixed in the two previous
commits: there the COMPARISON dropped the subscript, here the ORDER did.

The property tested is the one that makes this legible: a sub-bed pair must be
judged exactly as the whole-bed pair with the same relative geometry. 23c is
above 23a exactly as 24 is above 23, so the verdicts must match -- and they now
do, on all four label assignments.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import quality as Q  # noqa: E402
from rca_core.bed_parser import bed_position, parse_bed  # noqa: E402

SECTIONS = [{"name": "Sec A", "age_range": "150-160 Ma"}]

# Real ICS stage names, so the resolver and ics_age_compare both accept them
# and the check is LIVE rather than skipped as an unknown biozone -- an earlier
# version of the audit probe guessed a name that did not resolve and measured
# nothing.
OLDER_BZ = "Sinemurian"
YOUNGER_BZ = "Olenekian"

pytestmark = pytest.mark.skipif(
    not getattr(Q, "_HAS_ICS", False),
    reason="the ICS table is unavailable, so every pair is skipped",
)


def _violations(a_top, b_top, a_bz, b_bz):
    species = [
        {"species": "Genus A", "section": "Sec A", "biozone": a_bz,
         "range_top": a_top, "range_base": str(a_top)},
        {"species": "Genus B", "section": "Sec A", "biozone": b_bz,
         "range_top": b_top, "range_base": str(b_top)},
    ]
    n, _issues = Q._score_biozone_order(species, SECTIONS)
    return n


# (a_top, b_top, a_bz, b_bz) for the WHOLE-BED reference, then the sub-bed
# pair with the same geometry. 24 is above 23; 23c is above 23a.
GEOMETRY = [
    # A higher, carrying the younger label -> violation
    ("24", "23", YOUNGER_BZ, OLDER_BZ, "23c", "23a", 1),
    # A lower, carrying the younger label -> fine
    ("23", "24", YOUNGER_BZ, OLDER_BZ, "23a", "23c", 0),
    # A higher, carrying the OLDER label -> fine
    ("24", "23", OLDER_BZ, YOUNGER_BZ, "23c", "23a", 0),
    # A lower, carrying the older label -> violation
    ("23", "24", OLDER_BZ, YOUNGER_BZ, "23a", "23c", 1),
]


@pytest.mark.parametrize(
    "a_top,b_top,a_bz,b_bz,sub_a,sub_b,expected", GEOMETRY,
    ids=[f"{a}/{b}-{a_bz[:4]}" for a, b, a_bz, b_bz, _c, _d, _e in GEOMETRY])
def test_whole_bed_reference_behaves(a_top, b_top, a_bz, b_bz,
                                     sub_a, sub_b, expected):
    """The control: the case the code already handled. If this changes, the
    test below is comparing against a broken reference."""
    assert _violations(a_top, b_top, a_bz, b_bz) == expected


@pytest.mark.parametrize(
    "a_top,b_top,a_bz,b_bz,sub_a,sub_b,expected", GEOMETRY,
    ids=[f"{a}/{b}-{a_bz[:4]}" for a, b, a_bz, b_bz, _c, _d, _e in GEOMETRY])
def test_sub_bed_pair_is_judged_like_the_whole_bed_pair(
        a_top, b_top, a_bz, b_bz, sub_a, sub_b, expected):
    assert _violations(sub_a, sub_b, a_bz, b_bz) == expected, (
        f"Genus A@{sub_a} ({a_bz}) / Genus B@{sub_b} ({b_bz}): a sub-bed pair "
        f"must be judged exactly as A@{a_top}/B@{b_top}, which gives {expected}"
    )


def test_the_geometry_pairs_actually_differ_by_a_subscript():
    """Otherwise "same geometry" is a claim nothing checks: 23c and 23a must
    sit on opposite sides of each other, and neither may equal its whole-bed
    counterpart."""
    assert bed_position("23a") < bed_position("23c")
    assert bed_position("23c") < bed_position("24")
    assert bed_position("23a") > bed_position("23")
    assert bed_position("23") < bed_position("24")
    assert bed_position("23a") != bed_position("23b")


@pytest.mark.parametrize("label", [
    "23", "23a", "23b", "23m", "23z", "24", "Bed 12", "Bed 12c", "300 Ma",
    "", None, "not a bed",
])
def test_position_is_never_unordered_relative_to_its_bed(label):
    """A position must stay inside its own bed's interval, or sorting on it
    would move a row into a different bed."""
    pos = bed_position(label)
    info = parse_bed(label)
    if pos is None or info is None:
        assert pos is None, f"{label!r} parsed but produced no position"
        return
    base = float(info["bed_num"])
    assert base <= pos < base + 1, (
        f"{label!r} -> {pos} escaped bed {base}"
    )


def test_the_bed_base_comes_from_the_shared_parser():
    """A first draft recovered the bed number with lstrip("Bb"), which strips a
    CHARACTER SET rather than a prefix, so "Bed 12" became "ed 12" and five
    fixtures raised ValueError. The parser is right there; a clever shortcut to
    avoid calling it is a bug waiting to happen."""
    assert parse_bed("Bed 12")["bed_num"] == 12
    assert parse_bed("23a")["bed_num"] == 23
    assert parse_bed("not a bed") is None


def test_the_vacuity_guard_still_holds():
    """Both verdicts must appear across the geometry table, or "the pair matches
    its reference" could be satisfied by a check that never fires."""
    verdicts = {
        _violations(a, b, ab, bb) for a, b, ab, bb, _c, _d, _e in GEOMETRY
    }
    assert verdicts == {0, 1}, f"geometry table only produces {verdicts}"
