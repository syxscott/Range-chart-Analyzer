r"""_bed_num put every sub-bed of a bed on one point.

    return float(bed["bed_num"]) + (0.001 if _bed_sub(bed) else 0.0)

0.001 for EVERY letter, so 23a, 23c and 23z were all 23.001. That is the exact
failure rca_core/bed_parser.py exists to prevent -- its docstring records that
"a predicted Bed 23c and ground truth Bed 23d would both score as integer 23 ->
false positive accuracy" -- reappearing in the reasoning track, while
_bin_distance in the same eval_metrics file handled subscripts correctly and
said so in its own docstring. The REVIEW-2026-09-20 note fixed the sibling
_def_ that went through parse_bed_int; nothing recorded this one.

Measured before the fix, through the real entry point:

    _span(23a -> 23z)                        = 0.0
    _span(23 -> 23a)                         = 0.0010000000000012221
    _overlap_extent([23a..23z], [23a..23c])  = 0.0     # they share all of 23a..23c
    base_order, A@23c predicted / A@23a true = 1.0     # a wrong order, scored
                                                       # as full agreement

The last one is the severe form and the reason this file is about a scorer:
the reasoning track returned 1.0 for an order that is the reverse of the truth.

One thing to keep straight when reading the tests below: base_order judges
ORDER, not bed equality. A prediction of A@24 against a truth of A@23c agrees
on which one is higher, so it scores 1.0 -- and it scored 0.0 before the fix,
because the truth's own 23c/23a pair tied and the comparison went False. That
was a false NEGATIVE, corrected by the same change. The tests state both
directions separately so neither can be mistaken for a regression.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import rca_core.eval_metrics as EM  # noqa: E402


def _pos(label, field="range_base"):
    return EM._bed_num({"species": "S", "range_base": label}, field)


# "m" is NOT a bed subscript and never reaches _bed_num: bed_parser rejects it
# as the metre unit ("23 m" is a thickness). The remaining 25 letters are the
# real subscript alphabet, and every one of them must get its own position.
SUBS = tuple(c for c in "abcdefghijklmnopqrstuvwxyz" if c != "m")


def test_m_is_a_unit_not_a_sub_bed():
    """Stated so the two tests below cannot be read as "all 26 letters"."""
    assert _pos("23m") is None, '"23m" must be a thickness, not bed 23 with a sub'
    assert _pos("23 m") is None


# --- the encoding itself ----------------------------------------------


def test_every_subscript_gets_its_own_position():
    seen = {}
    for letter in SUBS:
        v = _pos(f"23{letter}")
        assert v is not None, f"23{letter} did not parse as a bed"
        assert v not in seen, f"23{letter} and {seen.get(v)} both map to {v}"
        seen[v] = letter
    assert len(seen) == 25


def test_positions_are_monotonic_in_the_letter():
    values = [_pos(f"23{letter}") for letter in SUBS]
    assert values == sorted(values), "a sub-bed must sort below the next one"
    assert len(set(values)) == 25


@pytest.mark.parametrize("letter", ["a", "y", "z"])
def test_no_subscript_ever_crosses_into_the_next_bed(letter):
    v = _pos(f"23{letter}")
    assert 23.0 < v < 24.0, f"23{letter} -> {v} escaped bed 23"


def test_a_bare_bed_is_strictly_below_its_first_subscript():
    assert _pos("23") < _pos("23a"), "23 and 23a collapsed"
    assert _pos("23z") < _pos("24"), "23z and 24 collapsed"


def test_beds_are_ordered():
    assert _pos("22z") < _pos("23") < _pos("23a") < _pos("23z") < _pos("24")


# --- what the three consumers report -----------------------------------


def test_a_sub_bed_range_has_a_nonzero_span():
    span = EM._span({"species": "S", "range_base": "23a", "range_top": "23z"})
    assert span > 0.5, f"a real sub-bed range measured {span}"


def test_a_one_sub_bed_step_is_about_one_letter_not_float_noise():
    span = EM._span({"species": "S", "range_base": "23", "range_top": "23a"})
    assert 0.01 < span < 0.03, f"23 -> 23a span = {span!r}"
    # The old value was 0.0010000000000012221: the placeholder itself was the
    # measurement, and its binary representation error dominated it.


def test_overlapping_sub_bed_ranges_overlap():
    a = {"species": "A", "range_base": "23a", "range_top": "23z"}
    b = {"species": "B", "range_base": "23a", "range_top": "23c"}
    extent = EM._overlap_extent(a, b)
    assert extent > 0.0, "23a..23z and 23a..23c share all of 23a..23c"
    assert extent < 0.5, "the shared part is a fraction of the longer range"


def test_disjoint_ranges_still_do_not_overlap():
    a = {"species": "A", "range_base": "10", "range_top": "12"}
    b = {"species": "B", "range_base": "13", "range_top": "15"}
    assert EM._overlap_extent(a, b) == 0.0


def test_plain_bed_arithmetic_is_unchanged():
    for base, top, want in (("23", "25", 2.0), ("1", "2", 1.0), ("10", "20", 10.0)):
        span = EM._span({"species": "S", "range_base": base, "range_top": top})
        assert span == pytest.approx(want), f"{base}->{top} = {span}"


# --- the scorer, through its real entry point --------------------------


def _order_scores(pred_a, truth_a):
    pred = [
        {"species": "Genus A", "range_base": pred_a, "range_top": "24"},
        {"species": "Genus B", "range_base": "23a", "range_top": "24"},
    ]
    gt = [
        {"species": "Genus A", "range_base": truth_a, "range_top": "24"},
        {"species": "Genus B", "range_base": "23a", "range_top": "24"},
    ]
    reasoning = EM.track_scores(pred, gt)["reasoning"]
    comps = reasoning["components"]
    return comps["base_order"], reasoning["score"]


@pytest.mark.parametrize("pred,truth", [
    ("23c", "23a"),   # A sits above B in the prediction, below in the truth
    ("23z", "23a"),
    ("23b", "23a"),
])
def test_a_wrong_same_bed_order_is_not_scored_as_agreement(pred, truth):
    order, _score = _order_scores(pred, truth)
    assert order["score"] == 0.0, (
        f"A@{pred} against truth A@{truth} is the reverse order but scored "
        f"{order['score']}"
    )


def test_a_correct_same_bed_order_still_scores_as_agreement():
    order, score = _order_scores("23a", "23a")
    assert order["score"] == 1.0, order
    assert score == 1.0, score


def test_a_correct_order_across_beds_scores_as_agreement():
    """The false NEGATIVE the old encoding also produced.

    A@24 and A@23c both put A above B@23a, so the ORDER agrees even though the
    bed number does not -- base_order judges order only, and scored 0.0 before
    the fix because the truth's own 23c/23a pair tied.
    """
    order, _score = _order_scores("24", "23c")
    assert order["score"] == 1.0, order


def test_a_reversed_order_across_beds_is_still_detected():
    order, _score = _order_scores("23a", "24")
    assert order["score"] == 0.0, order


# --- consistency with the other notion of position ---------------------


def test_bed_num_agrees_with_bin_distance_on_ordering():
    """Two functions in one file measure bed position; they must not disagree
    about which bed is higher."""
    for a, b in (("23a", "23c"), ("23c", "23z"), ("23z", "24"), ("1", "2"),
                 ("23", "23a"), ("22z", "23")):
        pa, pb = _pos(a), _pos(b)
        ia, ib = EM._parse_bed(a), EM._parse_bed(b)
        assert (pa < pb) == (EM._bin_distance(ia, ib) > 0), (
            f"_bed_num says {a} < {b} is {pa < pb}; "
            f"_bin_distance says the gap is {EM._bin_distance(ia, ib)}"
        )
