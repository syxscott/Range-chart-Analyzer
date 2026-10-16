"""What the strategy comparison actually measured, recorded as a test.

This is not a regression test against the MODEL's accuracy -- a model's score
moves with sampling and asserting on it would make the suite flaky for the
wrong reason. What it pins are the things that are DECISIONS rather than
measurements:

  1. `grid_read` is the default, and the reason is a measured basis rather than
     a preference. The old default was `single_pass`, chosen for exactly this
     reason at n=1; it was replaced when n=5 cleared the bar.
  2. `cell_status` still defaults to "not verified". A higher cell score must
     never be allowed to flip that on its own, because the flag's job is to
     describe PROVENANCE, not confidence.

Both the original n=1 table and the later measurement are kept, because the
comparison is the point: a reader who wants to know why the default moved needs
to see what it moved from.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.extractor import (  # noqa: E402
    OCCURRENCE_CELL_STATUSES,
    OCCURRENCE_CELL_STRATEGIES,
    OCCURRENCE_DEFAULT_CELL_STRATEGY,
    OCCURRENCE_SELF_STATUS_STRATEGIES,
    OCCURRENCE_STATUS_BY_STRATEGY,
)

# 2026-10-16, ONE run of each of the three single-call strategies on the real
# 22 x 18 Ozsvart figure against the 93-row authoritative CSV. Distinct-label
# scoring; cells counted only inside columns whose header matched verbatim.
#
#              cols    taxa   auths  zones legend  cell-acc  cell-cov  tokens
SINGLE_CALL = {
    "single_pass": ("9/15", "20/22", "7/8", "11/11", 3, 0.579, 0.355, 5151),
    "gated":       ("12/15", "20/22", "7/8", "0/11", 0, None, 0.0, 1103),
    "cells":       ("11/15", "20/22", "6/8", "10/11", 3, 0.574, 0.565, 6241),
}

# 2026-10-17, the two-stage path (rca_core.matrix) on the same figure and the
# same CSV. Cell precision / recall are measured per FILLED CELL, which is the
# quantity that decides whether the table is usable -- the older table's
# accuracy/coverage pair cannot be compared to it directly, and pretending
# otherwise is how 0.574 accuracy got mistaken for a good result.
#
#   cell reading, prompt and sheet configuration swept:
#     6x tiles, 93 per sheet, blank allowed   0.290 .. 0.989  (blank collapse)
#     6x tiles, 31 per sheet, no blank        0.968  0.989  0.989
#     8x tiles, 31 per sheet, no blank        1.000  1.000  1.000  1.000  1.000
#
# The 8x/31 configuration is the default; 31-tile sheets and 8x magnification
# were separated so magnification, not sheet size, is credited for the win.
GRID_READ_RUNS = (
    (1.000, 1.000), (1.000, 1.000), (1.000, 1.000), (1.000, 1.000), (1.000, 1.000),
)
GRID_READ_MIN_RUNS = 3          # n required before a default may change
GRID_READ_TARGET = 0.90


def _frac(text):
    """"12/15" -> 0.8.

    Comparing the strings directly puts "9/15" above "12/15", which is a test
    that asserts the opposite of what it means.
    """
    num, den = text.split("/")
    return float(num) / float(den)


def test_the_single_call_table_is_still_recorded():
    """The comparison is the evidence; deleting the loser hides the decision."""
    assert SINGLE_CALL["single_pass"][0] == "9/15"
    assert SINGLE_CALL["gated"][3] == "0/11", (
        "gated reads NO conodont zones: its schema has no strat_rows, and the "
        "legend came back empty too. That is why it is not the default, "
        "despite reading the most column headers correctly (12/15).")


def test_grid_read_is_the_default_because_it_was_measured():
    prec = [p for p, _r in GRID_READ_RUNS]
    rec = [r for _p, r in GRID_READ_RUNS]
    assert OCCURRENCE_DEFAULT_CELL_STRATEGY == "grid_read"
    assert len(GRID_READ_RUNS) >= GRID_READ_MIN_RUNS, (
        "flipping a default needs n>=%d; %d runs is not a basis"
        % (GRID_READ_MIN_RUNS, len(GRID_READ_RUNS)))
    assert min(prec) >= GRID_READ_TARGET and min(rec) >= GRID_READ_TARGET, (
        "the minimum over the runs must clear the target, not just the mean: "
        "P=%.3f R=%.3f" % (min(prec), min(rec)))


def test_grid_read_is_not_simply_the_best_of_the_other_strategies():
    """The default has to win on the thing that matters AND keep the rest.

    `gated` read the most column headers (12/15 vs 9/15) and was still the wrong
    default, because it read no conodont zones and no legend: the band and the
    class legend are what make the sheet interpretable at all.
    """
    assert _frac(SINGLE_CALL["gated"][0]) > _frac(SINGLE_CALL["single_pass"][0])
    assert SINGLE_CALL["gated"][3] == "0/11"


def test_geometry_alone_doubles_correct_cell_coverage():
    """The result that motivated the two-stage split.

    Handing the model locally measured pixel spans moved correct cells from
    0.355 to 0.565 of those in scope while accuracy stayed flat (0.579 ->
    0.574). The failure had been column IDENTITY, and naming each column by
    its rectangle attacks exactly that. That was n=1 and was offered as an
    opt-in; the two-stage path is what that lead eventually paid off.

    NOTE: these two functions used to take a `self` parameter, which makes
    pytest treat them as fixtures. They ERRORed at setup and therefore never
    ran -- the assertion below had been unenforced since the file was written,
    hidden in the middle of a long suite log.
    """
    assert SINGLE_CALL["cells"][6] > 1.5 * SINGLE_CALL["single_pass"][6]
    assert abs(SINGLE_CALL["cells"][5] - SINGLE_CALL["single_pass"][5]) < 0.05


def test_a_better_score_does_not_earn_a_verification_claim():
    """0.57 cell accuracy is not verification. The status describes where the
    reading came from and no score may promote it."""
    assert OCCURRENCE_STATUS_BY_STRATEGY["single_pass"] == \
        "single_pass_unverified"
    assert SINGLE_CALL["cells"][5] < 0.75
    assert "single_pass_unverified" in OCCURRENCE_CELL_STATUSES


def test_every_strategy_accounts_for_its_own_status():
    assert set(OCCURRENCE_STATUS_BY_STRATEGY) | set(
        OCCURRENCE_SELF_STATUS_STRATEGIES) == set(OCCURRENCE_CELL_STRATEGIES)
    assert "grid_read" in OCCURRENCE_SELF_STATUS_STRATEGIES, (
        "grid_read degrades to single_pass, so a fixed status entry would "
        "advertise verification the fallback never performed")
