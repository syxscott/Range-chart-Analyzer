"""Tests for PBDB mapping."""

import pytest
from rca_core.standards.pbdb import (
    _parse_age_range_ma,
    to_pbdb_occurrences,
    to_pbdb_collections,
)


class TestToPbdbOccurrences:
    """Test PBDB occurrence conversion."""

    def test_basic_occurrence(self):
        result = {
            "sections": [{"name": "X", "coordinates": "31N, 117E"}],
            "species_ranges": [
                {"species": "A", "section": "X", "biozone": "Wu", "author_year": "Smith 1900"}
            ]
        }
        occs = to_pbdb_occurrences(result)
        assert len(occs) == 1
        assert occs[0]["taxon_name"] == "A"
        assert occs[0]["identified_by"] == ""
        assert "author_year: Smith 1900" in occs[0]["notes"]

    def test_multiple_species(self):
        result = {
            "sections": [{"name": "X", "coordinates": "31N, 117E"}],
            "species_ranges": [
                {"species": "A", "section": "X", "biozone": "Wu"},
                {"species": "B", "section": "X", "biozone": "Wu"},
            ]
        }
        occs = to_pbdb_occurrences(result)
        assert len(occs) == 2

    def test_bed_identifiers_do_not_create_pbdb_ages(self):
        result = {
            "sections": [{"name": "X", "age_range": "Bed 1 to Bed 12"}],
            "species_ranges": [
                {"species": "A", "section": "X", "range_base": "Bed 7", "range_top": "Bed 9"}
            ],
        }
        occurrence = to_pbdb_occurrences(result)[0]
        assert occurrence["early_interval"] == ""
        assert occurrence["late_interval"] == ""
        assert occurrence["max_ma"] == ""
        assert occurrence["min_ma"] == ""

    def test_explicit_ma_bounds_preserve_pbdb_age_direction(self):
        result = {
            "sections": [{"name": "X"}],
            "species_ranges": [
                {"species": "A", "section": "X", "range_base": "260 Ma", "range_top": "252 Ma"}
            ],
        }
        occurrence = to_pbdb_occurrences(result)[0]
        assert occurrence["max_ma"] == "260.0"
        assert occurrence["min_ma"] == "252.0"
        assert float(occurrence["max_ma"]) >= float(occurrence["min_ma"])

    def test_reversed_explicit_bounds_are_left_blank(self):
        result = {
            "sections": [{"name": "X"}],
            "species_ranges": [
                {"species": "A", "section": "X", "range_base": "252 Ma", "range_top": "260 Ma"}
            ],
        }
        occurrence = to_pbdb_occurrences(result)[0]
        assert occurrence["max_ma"] == ""
        assert occurrence["min_ma"] == ""


class TestAgeRangeParsing:
    def test_requires_explicit_absolute_age_unit(self):
        assert _parse_age_range_ma("Bed 1 to Bed 12") == (None, None)
        assert _parse_age_range_ma("260-252 Ma") == (252.0, 260.0)
        assert _parse_age_range_ma("260 Ma to 252 Ma") == (252.0, 260.0)


class TestToPbdbCollections:
    """Test PBDB collection conversion."""

    def test_basic_collection(self):
        result = {
            "sections": [{"name": "X", "coordinates": "31N, 117E", "formations": ["Formation A"]}]
        }
        cols = to_pbdb_collections(result)
        assert len(cols) == 1
        assert cols[0]["collection_name"] == "X"
        assert cols[0]["latitude"] == "31.0"
        assert cols[0]["formation"] == "Formation A"
