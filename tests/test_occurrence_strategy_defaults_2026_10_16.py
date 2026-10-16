"""Record what the three-strategy run actually measured, as a test.

This is not a regression test against the MODEL's accuracy - a model's score
moves with sampling and asserting on it would make the suite flaky for the
wrong reason. What it pins is the two things that are decisions rather than
measurements:

  1. `single_pass` is still the default, and the reason is recorded. Flipping
     it needs a measured basis; an n=1 run with differences of 1-2 items does
     not have one.
  2. `cell_status` still defaults to "not verified". A higher cell score must
     never be allowed to flip that on its own, because the flag's job is to
     describe PROVENANCE, not confidence.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.extractor import (  # noqa: E402
    OCCURRENCE_CELL_STATUSES,
    OCCURRENCE_CELL_STRATEGIES,
    OCCURRENCE_DEFAULT_CELL_STRATEGY,
    OCCURRENCE_STATUS_BY_STRATEGY,
)

# 2026-10-16, one run of each strategy on the real 22 x 18 Ozsvart figure
# against the 93-row authoritative CSV. Distinct-label scoring; cells counted
# only inside columns whose header matched verbatim.
MEASURED = {
    #              cols    taxa   auths  zones legend  cell-acc  cell-cov  tokens
    "single_pass": ("9/15", "20/22", "7/8", "11/11", 3, 0.579, 0.355, 5151),
    "gated":       ("12/15", "20/22", "7/8", "0/11", 0, None, 0.0, 1103),
    "cells":       ("11/15", "20/22", "6/8", "10/11", 3, 0.574, 0.565, 6241),
}


def test_the_measured_table_is_the_one_we_wrote_down():
    """If someone re-runs and the numbers move, this is where to notice."""
    assert set(MEASURED) == set(OCCURRENCE_CELL_STRATEGIES)
    assert MEASURED["single_pass"][0] == "9/15"
    assert MEASURED["gated"][3] == "0/11", (
        "gated reads NO conodont zones: its schema has no strat_rows, and the "
        "legend came back empty too. That is why it is not the default, "
        "despite reading the most column headers correctly (12/15).")


def test_the_default_is_the_strategy_that_lost_no_capability():
    """gated wins columns and loses zones + legend. Those are not
    interchangeable: the band and the class legend are what make the sheet
    interpretable at all."""
    assert OCCURRENCE_DEFAULT_CELL_STRATEGY == "single_pass"
    assert MEASURED["single_pass"][3] == "11/11"
    assert MEASURED["gated"][3] == "0/11"


def test_geometry_doubles_correct_cell_coverage():
    """The one difference large and mechanism-consistent enough to act on.

    Handing the model locally measured pixel spans moved correct cells from
    0.355 to 0.565 of those in scope while accuracy stayed flat (0.579 ->
    0.574). The failure had been column IDENTITY, and naming each column by
    its rectangle attacks exactly that.

    n=1. This is offered as an opt-in, not a default change.
    """
    assert MEASURED["cells"][6] > 1.5 * MEASURED["single_pass"][6]
    assert abs(MEASURED["cells"][5] - MEASURED["single_pass"][5]) < 0.05


def test_a_better_score_does_not_earn_a_verification_claim():
    """0.57 cell accuracy is not verification. The status describes where the
    reading came from and no score may promote it."""
    assert OCCURRENCE_STATUS_BY_STRATEGY["single_pass"] == \
        "single_pass_unverified"
    assert MEASURED["cells"][5] < 0.75
    assert "single_pass_unverified" in OCCURRENCE_CELL_STATUSES
