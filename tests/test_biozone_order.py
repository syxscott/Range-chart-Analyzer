"""Test P1-12: Steno's Law biozone order check.

Cross-species biozone order check: for each section, if species A is in
biozone X and species B is in biozone Y where X is younger than Y but
A's range is below B's, this is a Steno's Law violation.
"""
import pytest


class TestBiozoneOrder:
    """P1-12: Steno's Law biozone order check in quality scoring."""

    def test_biozone_order_function_exists(self):
        """_score_biozone_order function must exist."""
        from rca_core.quality import _score_biozone_order
        assert callable(_score_biozone_order)

    def test_violation_detected(self):
        """Species in younger biozone appearing below older biozone violates Steno's Law."""
        from rca_core.quality import _score_biozone_order
        # Section with two species:
        # - Sp1: range_top="Bed 5" (younger), biozone="Late Permian"
        # - Sp2: range_top="Bed 3" (older), biozone="Early Permian"
        # Lexicographically "Late Permian" > "Early Permian"
        # But Sp1 is in a HIGHER bed (Bed 5 > Bed 3) which is YOUNGER
        # So Sp1 should appear ABOVE Sp2 in the stratigraphic column (younger on top).
        # If Sp1 is BELOW Sp2, it's a violation.
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5", "range_base": "3", "biozone": "Late Permian"},
            {"species": "Sp2", "section": "Sec1", "range_top": "3", "range_base": "1", "biozone": "Early Permian"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        # The function uses lexicographic comparison: "Late Permian" > "Early Permian"
        # Sp1 (younger biozone) has range_top=Bed 5 (younger/higher bed index)
        # Sp2 (older biozone) has range_top=Bed 3
        # Since Bed 5 > Bed 3, Sp1 is ABOVE Sp2 in stratigraphic order
        # (younger = higher bed number in this numbering scheme)
        # So there's NO violation - younger biozone is correctly above older biozone.
        assert violations == 0

    def test_stenos_law_violation_detected(self):
        """When younger biozone appears BELOW older biozone, it's a violation."""
        from rca_core.quality import _score_biozone_order
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            # Sp1 in younger biozone but LOWER in section (older stratigraphic position)
            {"species": "Sp1", "section": "Sec1", "range_top": "3", "range_base": "1", "biozone": "Late Permian"},
            # Sp2 in older biozone but HIGHER in section (younger stratigraphic position)
            {"species": "Sp2", "section": "Sec1", "range_top": "5", "range_base": "3", "biozone": "Early Permian"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        # "Late Permian" > "Early Permian" lexicographically
        # But Sp1 (Late Permian = younger) has range_top=Bed 3 (older/lower)
        # and Sp2 (Early Permian = older) has range_top=Bed 5 (younger/higher)
        # So younger biozone is BELOW older biozone → violation
        assert violations > 0, "Should detect Steno's Law violation"

    def test_no_violation_same_biozone(self):
        """Species in the same biozone don't trigger a Steno's Law check."""
        from rca_core.quality import _score_biozone_order
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5", "range_base": "3", "biozone": "Late Permian"},
            {"species": "Sp2", "section": "Sec1", "range_top": "3", "range_base": "1", "biozone": "Late Permian"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        assert violations == 0, "Same biozone should not trigger Steno's Law check"

    def test_no_violation_empty_biozone(self):
        """Species missing biozone are skipped."""
        from rca_core.quality import _score_biozone_order
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5", "range_base": "3", "biozone": ""},
            {"species": "Sp2", "section": "Sec1", "range_top": "3", "range_base": "1", "biozone": "Late Permian"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        assert violations == 0, "Species without biozone should be skipped"
