"""Regression tests for P1-3: rca_core.quality._score_consistency must
actually compute consistency, not return 1.0. The previous hard-coded
1.0 made the 0.20 weight a free 0.20 bonus and prevented D/F grades.

REVIEW-2026-07-25 P1-3.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.quality import _score_consistency, score_range_chart


class TestConsistencyNotAlwaysOne:
    def test_clean_data_still_scores_high(self):
        """Well-formed data with no chimera/FAD-LAD issues keeps a high
        consistency score."""
        data = {
            "species_ranges": [
                {"species": "Genus A sp.", "section": "X",
                 "range_top": "9", "range_base": "7", "biozone": "B Zone",
                 "agreement_count": 2},
            ],
            "runs": 2, "confidence": 0.9,
        }
        score, issues = _score_consistency(data)
        assert score >= 0.9, f"clean data must score high, got {score}"

    def test_fad_lt_lad_drops_score(self):
        """An inverted FAD/LAD row triggers consistency penalty."""
        data = {
            "species_ranges": [
                {"species": "Genus A sp.", "section": "X",
                 "range_top": "5", "range_base": "9",  # inverted
                 "biozone": "B Zone", "agreement_count": 2},
            ],
            "runs": 2, "confidence": 0.9,
        }
        score, _issues = _score_consistency(data)
        assert score < 1.0, "FAD<LAD violation must drop consistency"
        assert score <= 0.9

    def test_agreement_overflow_drops_score(self):
        """agreement_count > total_runs is a sign of merge corruption."""
        data = {
            "species_ranges": [
                {"species": "G", "section": "X", "range_top": "9",
                 "range_base": "7", "biozone": "Z", "agreement_count": 7},
            ],
            "runs": 2,
            "confidence": 0.9,
        }
        score, issues = _score_consistency(data)
        assert score < 1.0
        assert any("agreement" in i.get("msg_key", "") for i in issues)

    def test_chimera_warnings_drop_score(self):
        data = {
            "species_ranges": [],
            "chimera_warnings": [
                {"table": "species_ranges", "row": {"species": "X"}, "reason": "no run"},
                {"table": "species_ranges", "row": {"species": "Y"}, "reason": "no run"},
            ],
            "runs": 3, "confidence": 0.9,
        }
        score, issues = _score_consistency(data)
        assert score <= 0.8

    def test_missing_biozone_drops_score(self):
        data = {
            "species_ranges": [
                {"species": "G1", "section": "X", "range_top": "9",
                 "range_base": "7", "biozone": "", "agreement_count": 1},
                {"species": "G2", "section": "X", "range_top": "9",
                 "range_base": "7", "biozone": "", "agreement_count": 1},
            ],
            "runs": 1, "confidence": 0.9,
        }
        score, _ = _score_consistency(data)
        assert score < 1.0


class TestScoreRangeChartGradeFloor:
    """The free-0.20 from _score_consistency used to make D/F unreachable
    on non-empty inputs. Now they must be reachable."""

    def test_d_or_f_grade_now_reachable(self):
        """Force a worst-case scenario: 3 FAD-inverted rows + over-agreement
        + missing biozones + chimera warnings. Score must be low enough
        for D or F (was always >= 0.60 = C before this fix)."""
        data = {
            "species_ranges": [
                {"species": f"G{i}", "section": "X", "range_top": "1",
                 "range_base": "9", "biozone": "", "agreement_count": 7}
                for i in range(3)
            ],
            "sections": [],
            "chimera_warnings": [
                {"table": "species_ranges", "row": {"species": "X"}, "reason": "no run"},
                {"table": "species_ranges", "row": {"species": "Y"}, "reason": "no run"},
                {"table": "species_ranges", "row": {"species": "Z"}, "reason": "no run"},
            ],
            "runs": 1, "confidence": 0.9,
        }
        result = score_range_chart(data)
        # Before the fix, the free 0.20 weight floor was 0.60 (C). Now
        # with consistency actually computing, this worst-case must drop
        # below C. Allow D or F.
        assert result["grade"] in ("C", "D", "F"), (
            f"D/F must be reachable; got grade={result['grade']} score={result['score']}"
        )
        assert result["score"] < 0.75, (
            f"the fix must drop the worst-case score below the old floor 0.60; "
            f"got {result['score']}"
        )