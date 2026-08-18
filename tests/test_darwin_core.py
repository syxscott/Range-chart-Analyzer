"""Tests for Darwin Core mapping."""

import pytest
from rca_core.standards.darwin_core import (
    _parse_coordinates,
    _resolve_age_bounds,
    to_darwin_core_occurrences,
    to_darwin_core_archive,
)
from rca_core.standards.ics import ics_resolve_age_bound


class TestParseCoordinates:
    """Test coordinate parsing."""

    def test_31n_117e(self):
        lat, lon = _parse_coordinates("31N, 117E")
        assert lat == 31.0
        assert lon == 117.0

    def test_315s_1175w(self):
        lat, lon = _parse_coordinates("31.5 S 117.5 W")
        assert lat == -31.5
        assert lon == -117.5

    def test_315s_1175e(self):
        lat, lon = _parse_coordinates("31.5 S, 117.5 E")
        assert lat == -31.5
        assert lon == 117.5

    def test_not_visible(self):
        lat, lon = _parse_coordinates("Not visible in chart")
        assert lat is None
        assert lon is None


class TestToDarwinCoreOccurrences:
    """Test DwC occurrence conversion."""

    def test_basic_occurrence(self):
        result = {
            "sections": [{"name": "X", "coordinates": "31N, 117E"}],
            "species_ranges": [
                {"species": "A", "section": "X", "biozone": "Wu", "author_year": "Smith 1900"}
            ]
        }
        occs = to_darwin_core_occurrences(result)
        assert len(occs) == 1
        assert occs[0]["scientificName"] == "A"
        assert occs[0]["decimalLatitude"] == "31.0"
        # H1 fix (REVIEW-2026-07-25): basisOfRecord must be a valid DwC
        # vocabulary term. "MachineGenerated" is NOT in the TDWG
        # basisOfRecord vocabulary; "MachineObservation" is the correct
        # term for records produced automatically by a machine process.
        assert occs[0]["basisOfRecord"] == "MachineObservation"

    def test_multiple_species(self):
        result = {
            "sections": [{"name": "X", "coordinates": "31N, 117E"}],
            "species_ranges": [
                {"species": "A", "section": "X", "biozone": "Wu"},
                {"species": "B", "section": "X", "biozone": "Wu"},
            ]
        }
        occs = to_darwin_core_occurrences(result)
        assert len(occs) == 2

    def test_bed_identifiers_are_not_absolute_ages(self):
        assert ics_resolve_age_bound("Bed 9 (Yinkeng Fm base)") == (None, None)
        assert ics_resolve_age_bound("Sample 7") == (None, None)
        assert _resolve_age_bounds({"range_base": "Bed 7", "range_top": "Bed 9"}) == (
            "",
            "",
            None,
            None,
        )

    def test_explicit_ma_bounds_are_resolved(self):
        earliest, latest, earliest_ma, latest_ma = _resolve_age_bounds(
            {"range_base": "260 Ma", "range_top": "252 Ma"}
        )
        assert earliest
        assert latest
        assert earliest_ma == 260.0
        assert latest_ma == 252.0

    def test_reversed_explicit_ma_pair_is_not_exported_as_numeric_range(self):
        _, _, earliest_ma, latest_ma = _resolve_age_bounds(
            {"range_base": "252 Ma", "range_top": "260 Ma"}
        )
        assert earliest_ma is None
        assert latest_ma is None

