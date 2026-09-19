"""Regression tests for ics_resolve_age_bound stage-range handling.

REVIEW-2026-08-17 (P0-2): the bare-stage branch (step 4 in
ics_resolve_age_bound) ignored the ``prefer`` parameter and returned the
midpoint Ma of whichever stage appeared FIRST in the text. For inputs
like ``"Wuchiapingian - Changhsingian"`` this meant:
  - prefer="older" returned the Wuchiapingian midpoint (correct by
    accident — first stage IS the older one in this order)
  - prefer="younger" returned the Wuchiapingian midpoint (WRONG — should
    be Changhsingian)
  - reversed text ``"Changhsingian - Wuchiapingian"`` returned
    Changhsingian midpoint regardless of prefer (WRONG — should be
    Wuchiapingian under prefer="older").

Stage-range inputs (no explicit Ma, multiple stage names) are common in
the wild (every chart that annotates an FAD/LAD with a stage range
without numeric ages), and the function is called per-species via
darwin_core._resolve_age_bounds and pbdb._resolve_pbdb_bounds, so the
bug silently contaminated FAD/LAD Ma columns in DwC/PBDB exports.

REVIEW-2026-09-20 extended the same rule to a SINGLE named stage (the
midpoint made FAD == LAD) and made ``prefer`` spelling-tolerant with an
explicit rejection of unknown values.
"""

from __future__ import annotations

import pytest

from rca_core.standards.ics import ics_age_range_bounds, ics_resolve_age_bound


# Known Wuchiapingian and Changhsingian numeric bounds from the bundled
# ics_2024.json. Verified in code:
#   Wuchiapingian: base_ma=259.51 (older), top_ma=254.14 (younger)
#   Changhsingian: base_ma=254.14 (older), top_ma=251.902 (younger)
# (Geological convention: larger Ma = older.)
WUCHIAPINGIAN_BASE = 259.51
WUCHIAPINGIAN_TOP = 254.14
CHANGSHINGIAN_BASE = 254.14
CHANGSHINGIAN_TOP = 251.902


class TestResolveAgeBoundSingleStage:
    """Single-stage inputs return the requested END of that stage.

    REVIEW-2026-09-20: these two cases used to pin the stage MIDPOINT for
    both ``prefer`` values, which is the bug this class now documents: the
    midpoint made ``prefer="older"`` and ``prefer="younger"`` return the same
    number, so a species whose FAD and LAD both read "Wuchiapingian" exported
    ``start_age == end_age`` (zero duration) to Darwin Core / PBDB. A single
    named stage is handled exactly like a one-element range now: older ->
    base_ma, younger -> top_ma.
    """

    def test_single_stage_prefer_older_returns_stage_base(self):
        stage, ma = ics_resolve_age_bound("Wuchiapingian", prefer="older")
        assert stage == "Wuchiapingian"
        assert ma == pytest.approx(WUCHIAPINGIAN_BASE)

    def test_single_stage_prefer_younger_returns_stage_top(self):
        stage, ma = ics_resolve_age_bound("Changhsingian", prefer="younger")
        assert stage == "Changhsingian"
        assert ma == pytest.approx(CHANGSHINGIAN_TOP)

    def test_single_stage_bounds_match_the_range_function(self):
        """The two age paths must agree on one stage's interval."""
        older, younger = ics_age_range_bounds("Wuchiapingian")
        assert ics_resolve_age_bound("Wuchiapingian", prefer="older")[1] == older
        assert ics_resolve_age_bound("Wuchiapingian", prefer="younger")[1] == younger

    def test_prefer_spellings_normalised_and_unknown_rejected(self):
        """REVIEW-2026-09-20: anything but the exact string "older" silently
        took the *younger* branch."""
        assert ics_resolve_age_bound(
            "Wuchiapingian", prefer="Older")[1] == WUCHIAPINGIAN_BASE
        assert ics_resolve_age_bound(
            "Wuchiapingian", prefer=" younger ")[1] == WUCHIAPINGIAN_TOP
        assert ics_resolve_age_bound(
            "Wuchiapingian", prefer="base")[1] == WUCHIAPINGIAN_BASE
        assert ics_resolve_age_bound("Wuchiapingian")[1] == WUCHIAPINGIAN_BASE
        with pytest.raises(ValueError):
            ics_resolve_age_bound("Wuchiapingian", prefer="olader")


class TestResolveAgeBoundStageRange:
    """Stage-range inputs (>= 2 stage names) must honor ``prefer``.

    These are the cases the old code got wrong.
    """

    def test_wu_chang_prefer_older_returns_wu_older_bound(self):
        """``"Wuchiapingian - Changhsingian"``, prefer older → Wuchiapingian's
        base_ma (the older of the two stages' older bounds)."""
        stage, ma = ics_resolve_age_bound(
            "Wuchiapingian - Changhsingian", prefer="older",
        )
        assert stage == "Wuchiapingian"
        assert ma == pytest.approx(WUCHIAPINGIAN_BASE), (
            f"older of Wuchiapingian–Changhsingian should be "
            f"Wuchiapingian's base_ma=259.51, got {ma}"
        )

    def test_wu_chang_prefer_younger_returns_chang_younger_bound(self):
        """``"Wuchiapingian - Changhsingian"``, prefer younger → Changhsingian's
        top_ma (the younger of the two stages' younger bounds)."""
        stage, ma = ics_resolve_age_bound(
            "Wuchiapingian - Changhsingian", prefer="younger",
        )
        assert stage == "Changhsingian"
        assert ma == pytest.approx(CHANGSHINGIAN_TOP), (
            f"younger of Wuchiapingian–Changhsingian should be "
            f"Changhsingian's top_ma=251.9, got {ma}"
        )

    def test_chang_wu_prefer_older_returns_wu_older_bound(self):
        """Reversed text order: the function must NOT depend on which
        stage appears first in the input string."""
        stage, ma = ics_resolve_age_bound(
            "Changhsingian - Wuchiapingian", prefer="older",
        )
        assert stage == "Wuchiapingian"
        assert ma == pytest.approx(WUCHIAPINGIAN_BASE)

    def test_chang_wu_prefer_younger_returns_chang_younger_bound(self):
        stage, ma = ics_resolve_age_bound(
            "Changhsingian - Wuchiapingian", prefer="younger",
        )
        assert stage == "Changhsingian"
        assert ma == pytest.approx(CHANGSHINGIAN_TOP)
