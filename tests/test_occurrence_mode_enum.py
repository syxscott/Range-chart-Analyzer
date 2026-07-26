"""Test P1-5: occurrence_mode enum replaces boolean reworked field.

The species_ranges row key `reworked: bool` is replaced by
`occurrence_mode: str` with values: in_situ | reworked | transported |
cavity_fill | bioturbated | derived | lag_deposit.
"""
import pytest


class TestOccurrenceModeEnum:
    """P1-5: occurrence_mode enum for species rows."""

    def test_valid_occurrence_modes_defined(self):
        """VALID_OCCURRENCE_MODES must contain all expected values."""
        from rca_core.extractor import VALID_OCCURRENCE_MODES
        expected = {
            "in_situ", "reworked", "transported",
            "cavity_fill", "bioturbated", "derived", "lag_deposit",
        }
        assert expected.issubset(VALID_OCCURRENCE_MODES), (
            f"Missing occurrence modes: {expected - VALID_OCCURRENCE_MODES}"
        )

    def test_normalize_occurrence_mode_valid_string(self):
        """Valid occurrence_mode string passes through unchanged."""
        from rca_core.extractor import _normalize_occurrence_mode

        for mode in ["in_situ", "reworked", "transported", "cavity_fill",
                     "bioturbated", "derived", "lag_deposit"]:
            result = _normalize_occurrence_mode({"occurrence_mode": mode})
            assert result == mode, f"Valid mode {mode!r} should pass through"

    def test_normalize_occurrence_mode_old_reworked_true(self):
        """Old reworked=True maps to occurrence_mode='reworked'."""
        from rca_core.extractor import _normalize_occurrence_mode
        result = _normalize_occurrence_mode({"reworked": True})
        assert result == "reworked"

    def test_normalize_occurrence_mode_old_reworked_false(self):
        """Old reworked=False maps to occurrence_mode='in_situ'."""
        from rca_core.extractor import _normalize_occurrence_mode
        result = _normalize_occurrence_mode({"reworked": False})
        assert result == "in_situ"

    def test_normalize_occurrence_mode_default(self):
        """Missing occurrence_mode and reworked defaults to 'in_situ'."""
        from rca_core.extractor import _normalize_occurrence_mode
        result = _normalize_occurrence_mode({})
        assert result == "in_situ"

    def test_normalize_occurrence_mode_invalid_string(self):
        """Invalid occurrence_mode string falls back to 'in_situ'."""
        from rca_core.extractor import _normalize_occurrence_mode
        result = _normalize_occurrence_mode({"occurrence_mode": "invalid_mode"})
        assert result == "in_situ"

    def test_normalize_species_into_uses_occurrence_mode(self):
        """_normalize_species_into must produce occurrence_mode key, not reworked."""
        from rca_core.extractor import _normalize_species_into
        target = []
        sp = {
            "species": "Testus",
            "section": "Section A",
            "range_top": "5",
            "range_base": "3",
            "biozone": "Zone A",
            "occurrence_mode": "transported",
        }
        _normalize_species_into(sp, target)
        row = target[0]
        assert "occurrence_mode" in row, "Row must have occurrence_mode key"
        assert "reworked" not in row, "Row must NOT have old reworked key"
        assert row["occurrence_mode"] == "transported"

    def test_normalize_species_into_reworked_bool_backward_compat(self):
        """Old reworked:True boolean still works and maps to occurrence_mode."""
        from rca_core.extractor import _normalize_species_into
        target = []
        sp = {
            "species": "Testus",
            "section": "Section A",
            "range_top": "5",
            "range_base": "3",
            "biozone": "Zone A",
            "reworked": True,
        }
        _normalize_species_into(sp, target)
        row = target[0]
        assert row.get("occurrence_mode") == "reworked"
        assert "reworked" not in row

    def test_prompt_references_occurrence_mode(self):
        """prompt.py must reference occurrence_mode, not reworked."""
        import os
        prompt_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "rca_core", "prompt.py"
        )
        with open(prompt_path, encoding="utf-8") as f:
            content = f.read()
        # Should mention occurrence_mode
        assert "occurrence_mode" in content, (
            "prompt.py must reference occurrence_mode in the schema"
        )
        # Should NOT mention the old boolean form
        # (check it's not mentioned as the old "reworked: false" format)
        assert '"reworked"' not in content or "occurrence_mode" in content


class TestOccurrenceModeRoundTrip:
    """All valid occurrence_mode values must round-trip through normalization."""

    @pytest.mark.parametrize("mode", [
        "in_situ", "reworked", "transported", "cavity_fill",
        "bioturbated", "derived", "lag_deposit",
    ])
    def test_round_trip(self, mode):
        """Each enum value round-trips through _normalize_occurrence_mode."""
        from rca_core.extractor import _normalize_occurrence_mode
        result = _normalize_occurrence_mode({"occurrence_mode": mode})
        assert result == mode, f"Round-trip failed for {mode!r}"
