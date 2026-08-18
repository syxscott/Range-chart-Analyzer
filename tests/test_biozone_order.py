"""Test Steno's Law biozone order check.

Cross-species biozone order check: for each section, if species A is in
biozone X and species B is in biozone Y where X is younger than Y but
A's range is below B's, this is a Steno's Law violation.

M-1 fix (REVIEW-2026-07-25): the original implementation compared
biozone labels with lexicographic string ordering, which has no
relationship to time. The check now uses the ICS 2024 timescale
lookup (ics_age_compare on base_ma). Tests use ICS stage names
directly so the comparator finds them.
"""
import pytest


class TestBiozoneOrder:
    """Steno's Law biozone order check in quality scoring."""

    def test_biozone_order_function_exists(self):
        """_score_biozone_order function must exist."""
        from rca_core.quality import _score_biozone_order
        assert callable(_score_biozone_order)

    def test_no_violation_younger_above_older(self):
        """Younger biozone ABOVE older biozone is correct stratigraphic order."""
        from rca_core.quality import _score_biozone_order
        # Section with two species using real ICS stages:
        # - Sp1: range_top=Bed 5 (higher in section = younger), biozone=Changhsingian
        # - Sp2: range_top=Bed 3 (lower in section = older), biozone=Wuchiapingian
        # Changhsingian (younger) is correctly ABOVE Wuchiapingian (older).
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5",
             "range_base": "3", "biozone": "Changhsingian Zone"},
            {"species": "Sp2", "section": "Sec1", "range_top": "3",
             "range_base": "1", "biozone": "Wuchiapingian Zone"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        assert violations == 0, "Younger biozone above older is correct"

    def test_stenos_law_violation_detected(self):
        """When younger biozone appears BELOW older biozone, it's a violation."""
        from rca_core.quality import _score_biozone_order
        # Sp1 in younger biozone but LOWER in section (older position):
        #   Changhsingian (younger) but at Bed 3
        # Sp2 in older biozone but HIGHER in section (younger position):
        #   Wuchiapingian (older) but at Bed 5
        # Younger biozone is BELOW older biozone -> violation.
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "3",
             "range_base": "1", "biozone": "Changhsingian Zone"},
            {"species": "Sp2", "section": "Sec1", "range_top": "5",
             "range_base": "3", "biozone": "Wuchiapingian Zone"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        assert violations > 0, "Should detect Steno's Law inversion"

    def test_no_violation_same_biozone(self):
        """Species in the same biozone don't trigger a Steno's Law check."""
        from rca_core.quality import _score_biozone_order
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5",
             "range_base": "3", "biozone": "Changhsingian Zone"},
            {"species": "Sp2", "section": "Sec1", "range_top": "3",
             "range_base": "1", "biozone": "Changhsingian Zone"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        assert violations == 0, "Same biozone should not trigger"

    def test_no_violation_empty_biozone(self):
        """Species missing biozone are skipped (not penalised)."""
        from rca_core.quality import _score_biozone_order
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5",
             "range_base": "3", "biozone": ""},
            {"species": "Sp2", "section": "Sec1", "range_top": "3",
             "range_base": "1", "biozone": "Changhsingian Zone"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        assert violations == 0, "Empty biozone should be skipped"

    def test_unparseable_range_top_is_skipped_without_crashing(self):
        """Mixed positioned/unpositioned rows must not crash quality scoring."""
        from rca_core.quality import _score_biozone_order, score_range_chart
        sections = [{"name": "Sec1", "age_range": "Permian"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "Bed 2",
             "range_base": "Bed 1", "biozone": "Changhsingian Zone"},
            {"species": "Sp2", "section": "Sec1", "range_top": "unclear",
             "range_base": None, "biozone": "Wuchiapingian Zone"},
        ]
        assert _score_biozone_order(species, sections) == (0, [])
        result = score_range_chart({
            "sections": sections,
            "species_ranges": species,
            "confidence": 0.5,
        })
        assert 0.0 <= result["score"] <= 1.0

    def test_no_violation_unknown_biozone_names(self):
        """M-1 fix: biozone names that don't match ICS stages are skipped,
        not falsely penalised via lexicographic string ordering."""
        from rca_core.quality import _score_biozone_order
        sections = [{"name": "Sec1", "age_range": "Cretaceous"}]
        species = [
            {"species": "Sp1", "section": "Sec1", "range_top": "5",
             "range_base": "3", "biozone": "Local Biozone A"},
            {"species": "Sp2", "section": "Sec1", "range_top": "3",
             "range_base": "1", "biozone": "Local Biozone B"},
        ]
        violations, issues = _score_biozone_order(species, sections)
        # Neither label matches an ICS stage -> both pairs skipped,
        # no false positive (the lexicographic implementation would
        # have wrongly called this a violation).
        assert violations == 0
