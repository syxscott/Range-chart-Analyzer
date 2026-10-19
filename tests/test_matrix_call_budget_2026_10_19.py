"""The two-stage path must fit inside the caller's wall-clock budget.

The server's single-run budget is `timeout_sec * 6 + 25` s, derived from SIX
attempts, and the two-stage path spends one call on the labels plus one per
contact sheet. Before the bound, the call count grew with the matrix: 93 cells
cost 4 calls, 186 cost 7, 400 cost 14 -- and 7 sequential calls at the default
120 s each overrun the 745 s budget, so the request was abandoned with
`err.timeout` and NO data, where the single-pass path (one call) would have
answered.

This pins the bound, and the sheet-size choice that keeps the count low.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core import matrix as M                              # noqa: E402

#: The server's own budget, mirrored here so a change to either side is caught.
SERVER_MAX_ATTEMPTS = 6


def _chunk_for(n_cells):
    """The sheet size the module picks for a matrix of this many cells."""
    return min(M.SHEET_CHUNK_MAX,
               max(M.SHEET_CHUNK, -(-n_cells // M.MAX_SHEET_CALLS)))


def _calls_for(n_cells):
    chunk = _chunk_for(n_cells)
    return 1 + -(-n_cells // chunk)          # label call + one per sheet


class TestCallBudget:
    def test_the_reference_figure_fits_the_server_budget(self):
        n = 93
        calls = _calls_for(n)
        assert calls <= SERVER_MAX_ATTEMPTS, (
            "the two-stage path may not spend more calls than the server's "
            "budget has attempts")
        assert calls + 1 <= SERVER_MAX_ATTEMPTS, (
            "and it must still fit a fallback to the single-pass path")

    @pytest.mark.parametrize("n_cells", [93, 186, 372])
    def test_a_matrix_within_range_fits_with_a_fallback_too(self, n_cells):
        assert _calls_for(n_cells) + 1 <= SERVER_MAX_ATTEMPTS

    def test_the_worst_case_wall_clock_fits_the_server_budget(self):
        """The path's own budget + the fallback's worst case must fit.

        REVIEW-2026-10-19: the path is bounded in SECONDS, not in calls. It
        makes four calls and each of them may retry, and the two failure
        kinds cost different amounts -- a 429/529 comes back in about a
        second and may be ridden out, while a read timeout costs the whole
        per-call ceiling. A deadline expresses that; an attempt count cannot.

        The fallback to the single-pass path still has to fit afterwards, so
        the path's share has to scale with the caller's timeout: a fixed
        number would either starve a short-timeout run or overrun a long one.
        """
        from rca_core.extractor import TRANSPORT_TIMEOUT_ATTEMPTS
        for timeout_sec in (30, 60, 120, 300):
            server_budget = timeout_sec * SERVER_MAX_ATTEMPTS + 25
            fallback = TRANSPORT_TIMEOUT_ATTEMPTS * timeout_sec
            path = M.matrix_path_budget_sec(timeout_sec)
            assert path + fallback <= server_budget, (
                "the two-stage path plus its fallback must fit inside the "
                "server's wall-clock budget at timeout_sec=%s" % timeout_sec)

    def test_a_single_call_cannot_outlive_the_path_budget(self):
        assert M.MATRIX_CALL_TIMEOUT_SEC <= M.matrix_path_budget_sec(30), \
            "a per-call ceiling above the whole-path budget would make the " \
            "budget meaningless"

    def test_the_path_budget_scales_with_the_timeout(self):
        assert (M.matrix_path_budget_sec(120)
                < M.matrix_path_budget_sec(300)), \
            "a longer per-request timeout may buy a longer path budget"

    def test_the_sheet_grows_so_the_call_count_stays_bounded(self):
        """A bigger matrix must not mean more calls."""
        assert _chunk_for(372) > _chunk_for(93)
        assert _calls_for(372) <= _calls_for(186) + 1

    def test_a_matrix_too_big_for_the_budget_is_refused_not_attempted(self):
        """Beyond the budget the honest answer is to fall back, not to start a
        sequence that will be cut off mid-way with nothing to show."""
        huge = 5000
        chunk = _chunk_for(huge)
        sheets = -(-huge // chunk)
        assert sheets > M.MAX_SHEET_CALLS, (
            "this test is about the over-budget case; if the arithmetic changed, "
            "the expectation must change with it")

    def test_the_sheet_size_is_bounded(self):
        """Past 124 tiles the API's own image downscale starts eating the
        magnification, which is the thing that actually decides accuracy."""
        assert M.SHEET_CHUNK_MAX == 124
        assert _chunk_for(10 ** 6) <= M.SHEET_CHUNK_MAX

    def test_the_magnification_is_the_accuracy_driver_not_the_sheet_size(self):
        """Measured through the real path on the reference figure, MiniMax-M3:

            31 tiles/sheet -> 3 sheets -> 4 calls -> precision 1.000
            62 tiles/sheet -> 2 sheets -> 3 calls -> precision 1.000
           124 tiles/sheet -> 1 sheet  -> 2 calls -> precision 1.000

        All three are perfect, so the sheet is sized to the budget rather than
        fixed at the smallest size that once worked. The MAGNIFICATION is what
        decides accuracy (6x measured 0.968-0.989, 8x measured 1.000 five
        times out of five), and that is not a knob.
        """
        assert M.SHEET_SCALE == 8
        assert M.SHEET_CHUNK <= M.SHEET_CHUNK_MAX
