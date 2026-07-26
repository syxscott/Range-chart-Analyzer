"""P0-4 tests: rca_core.extractor._classify_array_item zone_type strict routing.

Verifies:
- Explicit zone_type field on a biozone item routes it correctly even without zone keyword in name.
- Explicit zone_type field on a species_ranges item routes it correctly.
- zone_type field on a section item routes it correctly.
- _normalize_biozone_into infers zone_type from name keywords (acme_zone, etc.).
- _normalize_species_into flags species names that look like zones.

REVIEW-2026-07-25 P0-4.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.extractor import (
    _classify_array_item,
    _normalize_biozone_into,
    _normalize_species_into,
)


class TestClassifyZoneTypeExplicit:
    """P0-4: explicit zone_type field takes priority over name-based heuristics."""

    def test_biozone_with_zone_type_acme_zone(self):
        """zone_type: 'acme_zone' routes to biozones even without zone keyword in name."""
        item = {"name": "Acme Zone of N. optima", "age": "Albian", "zone_type": "acme_zone"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_biozone(self):
        item = {"name": "Some OPU Name", "age": "Albian", "zone_type": "biozone"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_interval_zone(self):
        item = {"name": "MyInterval", "age": "Albian", "zone_type": "interval_zone"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_oppel_zone(self):
        item = {"name": "Standard Ammonite Oppel", "age": "Bajocian", "zone_type": "oppel_zone"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_lineage_zone(self):
        item = {"name": "Gracilis Lineage", "age": "Campanian", "zone_type": "lineage_zone"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_range_zone(self):
        item = {"name": "Taxon Range Zone", "age": "Maastrichtian", "zone_type": "range_zone"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_zonule(self):
        item = {"name": "Upper Zonule", "age": "Cenomanian", "zone_type": "zonule"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_subzone(self):
        item = {"name": "Subzone A", "age": "Turonian", "zone_type": "subzone"}
        assert _classify_array_item(item) == "biozones"

    def test_species_range_with_zone_type_species_range(self):
        """zone_type: 'species_range' routes to species_ranges even with zone keywords in name."""
        item = {
            "species": "Genus Zone-like Name",
            "range_top": "10",
            "range_base": "5",
            "zone_type": "species_range",
        }
        assert _classify_array_item(item) == "species_ranges"

    def test_species_range_with_zone_type_taxon_range(self):
        item = {
            "species": "Some Species",
            "range_top": "9",
            "range_base": "3",
            "zone_type": "taxon_range",
        }
        assert _classify_array_item(item) == "species_ranges"

    def test_species_range_with_zone_type_fad_lad(self):
        item = {"species": "Other Taxon", "zone_type": "fad_lad"}
        assert _classify_array_item(item) == "species_ranges"

    def test_section_with_zone_type_section(self):
        item = {
            "name": "Hiraiso Formation",
            "age_range": "Cretaceous",
            "zone_type": "section",
        }
        assert _classify_array_item(item) == "sections"

    def test_section_with_zone_type_measured_section(self):
        item = {
            "name": "Outcrop 42",
            "formations": ["Fm X"],
            "zone_type": "measured_section",
        }
        assert _classify_array_item(item) == "sections"


class TestNormalizeBiozoneIntoZoneType:
    """P0-4: _normalize_biozone_into enforces zone_type field and infers from name."""

    def test_zone_type_written_to_row(self):
        rows = []
        _normalize_biozone_into(
            {"name": "B Zone", "age": "Albian"},
            rows,
        )
        assert len(rows) == 1
        assert rows[0]["zone_type"] == "biozone"

    def test_zone_type_explicit_preserved(self):
        rows = []
        _normalize_biozone_into(
            {"name": "B Zone", "age": "Albian", "zone_type": "interval_zone"},
            rows,
        )
        assert rows[0]["zone_type"] == "interval_zone"

    def test_infers_acme_zone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Acme Zone of N. optima", "age": "Albian"},
            rows,
        )
        assert rows[0]["zone_type"] == "acme_zone"

    def test_infers_assemblage_zone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Assemblage Zone B", "age": "Cenomanian"},
            rows,
        )
        assert rows[0]["zone_type"] == "assemblage_zone"

    def test_infers_assemblage_zone_ass_abbrev(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Zone Ass. Alpha", "age": "Turonian"},
            rows,
        )
        assert rows[0]["zone_type"] == "assemblage_zone"

    def test_infers_lineage_zone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Lineage Zone of M. flexuosus", "age": "Campanian"},
            rows,
        )
        assert rows[0]["zone_type"] == "lineage_zone"

    def test_infers_interval_zone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Interval Zone 3", "age": "Coniacian"},
            rows,
        )
        assert rows[0]["zone_type"] == "interval_zone"

    def test_infers_zonule(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Upper Zonule", "age": "Santonian"},
            rows,
        )
        assert rows[0]["zone_type"] == "zonule"

    def test_infers_subzone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Lower Subzone", "age": "Maastrichtian"},
            rows,
        )
        assert rows[0]["zone_type"] == "subzone"

    def test_infers_oppel_zone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Standard Ammonite Oppel Zone", "age": "Bajocian"},
            rows,
        )
        assert rows[0]["zone_type"] == "oppel_zone"

    def test_infers_range_zone(self):
        rows = []
        _normalize_biozone_into(
            {"name": "Taxon Range Zone", "age": "Albian"},
            rows,
        )
        assert rows[0]["zone_type"] == "range_zone"


class TestNormalizeSpeciesIntoZoneDefense:
    """P0-4: _normalize_species_into flags species names that look like zones."""

    def test_species_name_with_zone_triggers_warning(self):
        rows = []
        _normalize_species_into(
            {"species": "Genus Zone-like sp.", "range_top": "10", "range_base": "5"},
            rows,
        )
        assert "[zone-mislabel-warning]" in rows[0]["note"]

    def test_species_name_with_assemblage_triggers_warning(self):
        rows = []
        _normalize_species_into(
            {"species": "Assemblage Taxon sp.", "range_top": "8", "range_base": "2"},
            rows,
        )
        assert "[zone-mislabel-warning]" in rows[0]["note"]

    def test_species_name_with_acme_triggers_warning(self):
        rows = []
        _normalize_species_into(
            {"species": "Acme Species sp.", "range_top": "7", "range_base": "1"},
            rows,
        )
        assert "[zone-mislabel-warning]" in rows[0]["note"]

    def test_species_name_clean_no_warning(self):
        rows = []
        _normalize_species_into(
            {"species": "Normal genus sp.", "range_top": "9", "range_base": "3"},
            rows,
        )
        assert "[zone-mislabel-warning]" not in rows[0].get("note", "")

    def test_warning_appended_not_replaced(self):
        rows = []
        _normalize_species_into(
            {
                "species": "Genus Zone sp.",
                "range_top": "10",
                "range_base": "5",
                "note": "pre-existing note",
            },
            rows,
        )
        assert "pre-existing note" in rows[0]["note"]
        assert "[zone-mislabel-warning]" in rows[0]["note"]
