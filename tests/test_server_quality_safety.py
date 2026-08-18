"""Regression tests for non-blocking server quality metadata."""

from unittest.mock import patch

from server import _safe_score_range_chart


def test_safe_score_returns_normal_quality():
    quality = _safe_score_range_chart({
        "species_ranges": [{"species": "A"}],
        "confidence": 0.5,
    })
    assert 0.0 <= quality["score"] <= 1.0
    assert quality["grade"] in {"A", "B", "C", "D", "F"}


def test_safe_score_isolates_unexpected_failure():
    with patch("rca_core.quality.score_range_chart", side_effect=RuntimeError("boom")):
        quality = _safe_score_range_chart({"species_ranges": [{"species": "A"}]})
    assert quality["score"] == 0.0
    assert quality["grade"] == "F"
    issue = quality["issues"][0]
    assert issue["msg_key"] == "quality.scoring_failed"
    assert issue["params"]["dimension"] == "server"
    assert issue["params"]["error_type"] == "RuntimeError"
