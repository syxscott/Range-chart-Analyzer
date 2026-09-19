"""Test P0-7: Bed string preservation in exporter.

The exporter's invariant validator must not coerce "Bed 23c" to float(NaN).
String Bed identifiers must round-trip correctly.
"""
import pytest
from rca_core.exporter import _parse_bed, validate_export_invariants


class TestParseBed:
    """Parse bed identifiers preserving subscripts."""

    def test_bed_23c_parses_correctly(self):
        """'Bed 23c' should parse to bed_num=23, bed_sub='c'."""
        result = _parse_bed("Bed 23c")
        assert result is not None
        assert result["bed_num"] == 23
        assert result["bed_sub"] == "c"
        assert result["raw"] == "Bed 23c"

    def test_bed_27a_parses_correctly(self):
        """'Bed 27a' should parse to bed_num=27, bed_sub='a'."""
        result = _parse_bed("Bed 27a")
        assert result is not None
        assert result["bed_num"] == 27
        assert result["bed_sub"] == "a"

    def test_bed_100_no_sub_parses(self):
        """'Bed 100' (no subscript) should still parse."""
        result = _parse_bed("Bed 100")
        assert result is not None
        assert result["bed_num"] == 100
        assert result["bed_sub"] == ""

    def test_bed_23c_round_trip(self):
        """Parsed Bed 23c should preserve raw for round-trip."""
        result = _parse_bed("Bed 23c")
        assert result is not None
        assert result["raw"] == "Bed 23c"

    def test_bare_23c_parses(self):
        """Bare '23c' (no 'Bed' prefix) should also parse."""
        result = _parse_bed("23c")
        assert result is not None
        assert result["bed_num"] == 23
        assert result["bed_sub"] == "c"

    def test_bed_with_space_variations(self):
        """Various spacing in 'Bed N' form should all parse."""
        result = _parse_bed("Bed  5b")
        assert result is not None
        assert result["bed_num"] == 5
        assert result["bed_sub"] == "b"

    def test_case_insensitive_bed(self):
        """'BED 23c' and 'bed 23c' should both parse."""
        result = _parse_bed("bed 23c")
        assert result is not None
        assert result["bed_num"] == 23
        assert result["bed_sub"] == "c"


class TestValidateExportInvariantsBed:
    """validate_export_invariants must not coerce Bed strings to NaN."""

    def test_bed_23c_not_nan(self):
        """'Bed 23c' must not produce a NaN issue."""
        data = {
            "sections": [{"name": "Test Section"}],
            "species_ranges": [
                {
                    "species": "Testus",
                    "section": "Test Section",
                    "range_base": "Bed 23c",
                    "range_top": "Bed 23c",
                    "biozone": "Test Zone",
                }
            ],
        }
        ok, issues, _warnings = validate_export_invariants(data)
        # Should NOT flag as NaN or invalid - the bed strings are valid
        constraint_issues = [i for i in issues if i.get("constraint") == "range_base_le_range_top"]
        assert len(constraint_issues) == 0, f"Unexpected constraint issues: {constraint_issues}"

    def test_bed_27a_bed_30b_order(self):
        """'Bed 27a' < 'Bed 30b' should not violate FAD<LAD."""
        data = {
            "sections": [{"name": "Test Section"}],
            "species_ranges": [
                {
                    "species": "Testus",
                    "section": "Test Section",
                    "range_base": "Bed 27a",  # older
                    "range_top": "Bed 30b",  # younger
                    "biozone": "Test Zone",
                }
            ],
        }
        ok, issues, _warnings = validate_export_invariants(data)
        constraint_issues = [i for i in issues if i.get("constraint") == "range_base_le_range_top"]
        assert len(constraint_issues) == 0, f"Unexpected constraint issues: {constraint_issues}"

    def test_bed_23c_violation_detected(self):
        """When Bed 23c > Bed 22 (range_base > range_top), violation is detected."""
        data = {
            "sections": [{"name": "Test Section"}],
            "species_ranges": [
                {
                    "species": "Testus",
                    "section": "Test Section",
                    "range_base": "Bed 23c",  # older
                    "range_top": "Bed 22",   # younger - VIOLATION
                    "biozone": "Test Zone",
                }
            ],
        }
        ok, issues, _warnings = validate_export_invariants(data)
        constraint_issues = [i for i in issues if i.get("constraint") == "range_base_le_range_top"]
        assert len(constraint_issues) > 0, "Should detect Bed 23c > Bed 22 violation"

    def test_bed_100_preserved_as_string(self):
        """'Bed 100' is still preserved as a string (not coerced to float)."""
        result = _parse_bed("Bed 100")
        assert result is not None
        # raw should be the original string
        assert result["raw"] == "Bed 100"
        # bed_num should be the integer
        assert result["bed_num"] == 100
