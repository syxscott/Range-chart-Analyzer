"""Tests for PBDB mapping."""

import pytest
from rca_core.standards.pbdb import (
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
