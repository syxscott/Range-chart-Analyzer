"""Tests for the ICS 2024 lookup table module."""

import pytest
from rca_core.standards.ics import (
    ICS_2024,
    ics_stage_from_age,
    ics_age_compare,
    ics_parse_age_range,
    ics_era,
    ics_period,
)


class TestICSJSON:
    """Test ICS JSON loading and field completeness."""

    def test_ics_json_loads(self):
        """ICS JSON loads successfully."""
        assert isinstance(ICS_2024, dict)
        assert len(ICS_2024) > 80

    def test_all_stages_have_required_fields(self):
        """All stages have required fields."""
        required = {"rank", "abbrev", "top_ma", "base_ma", "period", "era"}
        for name, info in ICS_2024.items():
            assert required.issubset(info.keys()), f"{name} missing fields"

    def test_wuchiapingian_fields(self):
        """Wuchiapingian has correct data."""
        wu = ICS_2024["Wuchiapingian"]
        assert wu["abbrev"] == "Wu"
        assert 254 < wu["top_ma"] < 255
        assert 259 < wu["base_ma"] < 260
        assert wu["period"] == "Permian"
        assert wu["era"] == "Paleozoic"

    def test_ma_boundaries(self):
        """Stage ages are in correct order (base > top geologically)."""
        for name, info in ICS_2024.items():
            assert info["base_ma"] > info["top_ma"], f"{name} has invalid age range"


class TestIcsStageFromAge:
    """Test ics_stage_from_age function."""

    def test_wuchiapingian_age(self):
        result = ics_stage_from_age(255)
        assert result == "Wuchiapingian"

    def test_ma_at_top_boundary(self):
        result = ics_stage_from_age(255.0)
        assert result == "Wuchiapingian"

    def test_ma_at_base_boundary(self):
        result = ics_stage_from_age(259.51)
        assert result == "Wuchiapingian"

    def test_unknown_age(self):
        result = ics_stage_from_age(9999)
        assert result is None


class TestIcsAgeCompare:
    """Test ics_age_compare function."""

    def test_normal_order(self):
        result = ics_age_compare("Wuchiapingian", "Changhsingian")
        assert result == -1

    def test_reversed_order(self):
        result = ics_age_compare("Changhsingian", "Wuchiapingian")
        assert result == 1

    def test_same_stage(self):
        result = ics_age_compare("Wuchiapingian", "Wuchiapingian")
        assert result == 0

    def test_unknown_stage(self):
        result = ics_age_compare("UnknownStage", "Wuchiapingian")
        assert result is None


class TestIcsParseAgeRange:
    """Test ics_parse_age_range function."""

    def test_single_stage(self):
        result = ics_parse_age_range("Wuchiapingian")
        assert result == ["Wuchiapingian"]

    def test_stage_range(self):
        result = ics_parse_age_range("Late Permian (Wuchiapingian - Changhsingian)")
        assert "Wuchiapingian" in result
        assert "Changhsingian" in result
        assert result.index("Wuchiapingian") < result.index("Changhsingian")

    def test_multiple_stages(self):
        result = ics_parse_age_range("Carnian to Norian")
        assert len(result) >= 2

    def test_empty_text(self):
        result = ics_parse_age_range("")
        assert result == []
        result = ics_parse_age_range("Early Triassic")
        assert result == []


class TestIcsEra:
    """Test ics_era function."""

    def test_paleozoic(self):
        assert ics_era("Wuchiapingian") == "Paleozoic"
        assert ics_era("Tournaisian") == "Paleozoic"

    def test_mesozoic(self):
        assert ics_era("Tithonian") == "Mesozoic"
        assert ics_era("Carnian") == "Mesozoic"

    def test_cenozoic(self):
        assert ics_era("Gelasian") == "Cenozoic"
        assert ics_era("Piacenzian") == "Cenozoic"

    def test_unknown(self):
        assert ics_era("UnknownStage") is None


class TestIcsPeriod:
    """Test ics_period function."""

    def test_permian(self):
        assert ics_period("Wuchiapingian") == "Permian"
        assert ics_period("Changhsingian") == "Permian"

    def test_jurassic(self):
        assert ics_period("Tithonian") == "Jurassic"

    def test_all_eras_represented(self):
        eras = set(info["era"] for info in ICS_2024.values())
        assert "Paleozoic" in eras
        assert "Mesozoic" in eras
        assert "Cenozoic" in eras

    def test_unknown(self):
        assert ics_period("UnknownStage") is None
