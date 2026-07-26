"""Test P1-8: abundance sum-to-100 quality scoring.

The ABUNDANCE prompt now instructs the model that percentages should sum to
100±5% per level. The quality scorer detects violations and deducts 0.05
per violating level (capped at 0.3 total).
"""
import pytest


class TestAbundanceSumQuality:
    """P1-8: abundance sum-to-100 constraint in quality scoring."""

    def test_80pct_sum_detected(self):
        """A level summing to 80% should produce a violation and score deduction."""
        from rca_core.quality import _score_abundance_sum
        data = {
            "samples": [
                {"taxon": "Pinus", "level": "L1", "abundance": "40", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L1", "abundance": "40", "abundance_unit": "%"},
                # Sum = 80%, which is < 95% → violation
            ],
        }
        violations = _score_abundance_sum(data)
        assert len(violations) == 1
        assert violations[0]["sample"] == "L1"
        assert abs(violations[0]["sum"] - 80.0) < 0.1

    def test_120pct_sum_detected(self):
        """A level summing to 120% should produce a violation."""
        from rca_core.quality import _score_abundance_sum
        data = {
            "samples": [
                {"taxon": "Pinus", "level": "L1", "abundance": "70", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L1", "abundance": "50", "abundance_unit": "%"},
                # Sum = 120%, which is > 105% → violation
            ],
        }
        violations = _score_abundance_sum(data)
        assert len(violations) == 1
        assert abs(violations[0]["sum"] - 120.0) < 0.1

    def test_100pct_no_violation(self):
        """A level summing to 100% should NOT produce a violation."""
        from rca_core.quality import _score_abundance_sum
        data = {
            "samples": [
                {"taxon": "Pinus", "level": "L1", "abundance": "60", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L1", "abundance": "40", "abundance_unit": "%"},
                # Sum = 100%, which is within 95-105 range → no violation
            ],
        }
        violations = _score_abundance_sum(data)
        assert len(violations) == 0

    def test_97pct_no_violation(self):
        """A level summing to 97% should NOT produce a violation (within ±5%)."""
        from rca_core.quality import _score_abundance_sum
        data = {
            "samples": [
                {"taxon": "Pinus", "level": "L1", "abundance": "50", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L1", "abundance": "47", "abundance_unit": "%"},
            ],
        }
        violations = _score_abundance_sum(data)
        assert len(violations) == 0

    def test_multiple_levels_with_one_violation(self):
        """Multiple levels, one violating, should only flag the bad level."""
        from rca_core.quality import _score_abundance_sum
        data = {
            "samples": [
                # Level 1: 100% total → OK
                {"taxon": "Pinus", "level": "L1", "abundance": "60", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L1", "abundance": "40", "abundance_unit": "%"},
                # Level 2: 75% total → violation
                {"taxon": "Pinus", "level": "L2", "abundance": "40", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L2", "abundance": "35", "abundance_unit": "%"},
            ],
        }
        violations = _score_abundance_sum(data)
        assert len(violations) == 1
        assert violations[0]["sample"] == "L2"

    def test_non_percent_unit_ignored(self):
        """Entries with abundance_unit != '%' are ignored (not summed)."""
        from rca_core.quality import _score_abundance_sum
        data = {
            "samples": [
                {"taxon": "Pinus", "level": "L1", "abundance": "common", "abundance_unit": "relative"},
                {"taxon": "Quercus", "level": "L1", "abundance": "40", "abundance_unit": "%"},
                # Only the '%' entry is summed; 'relative' is ignored
                # Total = 40%, violation
            ],
        }
        violations = _score_abundance_sum(data)
        assert len(violations) == 1

    def test_deduction_capped_at_0_3(self):
        """With many violations the deduction should be capped at 0.3."""
        from rca_core.quality import _score_abundance_sum, _score_accuracy
        # Create 10 levels each summing to 80% (10 violations)
        samples = []
        for i in range(10):
            samples.append({"taxon": "T", "level": f"L{i}", "abundance": "40", "abundance_unit": "%"})
            samples.append({"taxon": "Q", "level": f"L{i}", "abundance": "40", "abundance_unit": "%"})
        data = {"samples": samples}
        violations = _score_abundance_sum(data)
        assert len(violations) == 10
        # Deduction should be min(0.3, 0.05 * 10) = min(0.3, 0.5) = 0.3
        from rca_core.quality import _score_accuracy
        score, issues = _score_accuracy(data)
        # With no other checks (checks=0), score starts at 1.0
        # After 0.3 deduction: 1.0 - 0.3 = 0.7
        assert score <= 0.7 + 1e-9, f"Expected score 0.7, got {score}"

    def test_score_accuracy_with_120pct_violation(self):
        """A 120% sum violation should reduce the accuracy score."""
        from rca_core.quality import _score_accuracy
        data = {
            "samples": [
                {"taxon": "Pinus", "level": "L1", "abundance": "80", "abundance_unit": "%"},
                {"taxon": "Quercus", "level": "L1", "abundance": "40", "abundance_unit": "%"},
            ],
            # No species_ranges/sections → checks=0, score starts at 1.0
        }
        score, issues = _score_accuracy(data)
        # 1 violation → 0.05 deduction → score = 0.95
        assert abs(score - 0.95) < 1e-9, f"Expected score 0.95, got {score}"
        # Should have at least one violation issue
        violation_keys = [i.get("msg_key") for i in issues]
        assert "quality.abundance_sum_violation" in violation_keys or "quality.abundance_sum_violation_count" in violation_keys
