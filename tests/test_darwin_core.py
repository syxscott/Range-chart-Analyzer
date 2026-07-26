"""Tests for Darwin Core mapping."""

import pytest
from rca_core.standards.darwin_core import (
    _parse_coordinates,
    to_darwin_core_occurrences,
    to_darwin_core_archive,
)


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
        assert occs[0]["basisOfRecord"] == "MachineExtractedFromImage"

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

