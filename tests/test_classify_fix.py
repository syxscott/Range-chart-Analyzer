"""Regression tests for P1-8: rca_core.extractor._classify_array_item
must NOT misclassify a section row whose model emitted ``age`` (no
_range suffix) as a biozone. The pre-fix code unconditionally returned
``biozones`` for any {name, age} item — silently moving sections into
the biozones table.

REVIEW-2026-07-25 P1-8.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.extractor import _classify_array_item


class TestClassifyAgeWithoutZone:
    def test_section_with_age_keyword_not_misclassified(self):
        """{name: 'Sec A', age: 'Albian'} is a section, NOT a biozone."""
        assert _classify_array_item({"name": "Sec A", "age": "Albian"}) == "sections"

    def test_section_with_age_range_classified_correctly(self):
        assert _classify_array_item({"name": "Sec A", "age_range": "Albian-Cenomanian"}) == "sections"

    def test_biozone_with_zone_keyword_classified_as_biozone(self):
        item = {"name": "B Zone (Albian)", "age": "Albian"}
        assert _classify_array_item(item) == "biozones"

    def test_biozone_with_zone_type_field_classified_as_biozone(self):
        # P0-4: zone_type must use spec-compliant full form (e.g. interval_zone,
        # not bare "interval"). Updated from legacy test that used "interval".
        item = {"name": "MyInterval", "age": "Albian", "zone_type": "interval_zone"}
        assert _classify_array_item(item) == "biozones"

    def test_species_row_still_works(self):
        assert _classify_array_item({
            "species": "Genus sp.", "range_top": "9", "range_base": "7",
        }) == "species_ranges"

    def test_section_with_formations_classified_correctly(self):
        assert _classify_array_item({
            "name": "Sec A", "formations": ["Fm X"],
        }) == "sections"

    def test_empty_dict_returns_none(self):
        assert _classify_array_item({}) is None

    def test_non_zone_label_with_age_is_section(self):
        """Plain names without zone/assemblage keyword → section."""
        items = [
            {"name": "Hiraiso Formation", "age": "Cretaceous"},
            {"name": "Nankai Group", "age": "Neogene"},
            {"name": "Outcrop 42", "age": "Quaternary"},
        ]
        for it in items:
            assert _classify_array_item(it) == "sections", (
                f"{it['name']!r} should be a section, not a biozone"
            )