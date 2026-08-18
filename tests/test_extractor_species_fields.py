"""Regression tests for P0-3: _normalize_species_into must explicitly
populate endpoint_kind / occurrence_mode / range_top_bed / range_base_bed on
every species_ranges row.

Without these, the prompt-requested scientific metadata (observed vs
projected FAD/LAD, occurrence mode, exact bed labels) is silently dropped
because `_carry_extras` filters them out via _KNOWN_SPECIES_KEYS.

REVIEW-2026-07-25 P0-3 / P1-5.

Note: P1-5 renamed the `reworked` boolean to `occurrence_mode` string
(in_situ | reworked | transported | ...). The old `reworked` boolean
is accepted as input and normalized to the string form.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.extractor import _normalize_species_into


class TestSpeciesFieldsPreserved:
    """P0-3: explicit scientific metadata fields on every species_ranges row."""

    def test_endpoint_kind_preserved(self):
        target = []
        _normalize_species_into({
            "species": "Genus sp.",
            "section": "Sec A",
            "range_top": "Bed 9",
            "range_base": "Bed 7",
            "biozone": "B Zone",
            "endpoint_kind": "projected",
        }, target)
        row = target[0]
        assert row.get("endpoint_kind") == "projected", (
            f"endpoint_kind lost: {row}"
        )

    def test_occurrence_mode_reworked_normalized(self):
        """P1-5: reworked=True is normalized to occurrence_mode='reworked'."""
        target = []
        _normalize_species_into({
            "species": "Genus sp.", "section": "Sec A",
            "range_top": "Bed 9", "range_base": "Bed 7",
            "biozone": "B Zone",
            "reworked": True,
        }, target)
        row = target[0]
        assert row.get("occurrence_mode") == "reworked", (
            f"occurrence_mode should be 'reworked', got: {row.get('occurrence_mode')}"
        )

    def test_occurrence_mode_in_situ_normalized(self):
        """P1-5: reworked=False is normalized to occurrence_mode='in_situ'."""
        target = []
        _normalize_species_into({
            "species": "Genus sp.", "section": "Sec A",
            "range_top": "Bed 9", "range_base": "Bed 7",
            "biozone": "B Zone",
            "reworked": False,
        }, target)
        row = target[0]
        assert row.get("occurrence_mode") == "in_situ", (
            f"occurrence_mode should be 'in_situ', got: {row.get('occurrence_mode')}"
        )

    def test_range_top_bed_preserved(self):
        target = []
        _normalize_species_into({
            "species": "Genus sp.", "section": "Sec A",
            "range_top": "Bed 9", "range_base": "Bed 7",
            "biozone": "B Zone",
            "range_top_bed": "Bed 9a",  # exact bed label
        }, target)
        assert target[0].get("range_top_bed") == "Bed 9a", (
            f"range_top_bed lost: {target[0]}"
        )

    def test_range_base_bed_preserved(self):
        target = []
        _normalize_species_into({
            "species": "Genus sp.", "section": "Sec A",
            "range_top": "Bed 9", "range_base": "Bed 7",
            "biozone": "B Zone",
            "range_base_bed": "Bed 7b",
        }, target)
        assert target[0].get("range_base_bed") == "Bed 7b", (
            f"range_base_bed lost: {target[0]}"
        )

    def test_all_explicit_fields_present_after_normalize(self):
        """Authoritative row shape — every prompt-required field appears at top level."""
        target = []
        _normalize_species_into({
            "species": "Genus sp.", "section": "Sec A",
            "range_top": "Bed 9", "range_base": "Bed 7",
            "biozone": "B Zone",
            "author": "Smith", "year": "1950",
            "author_year": "Smith, 1950",
            "range_top_bed": "Bed 9a",
            "range_base_bed": "Bed 7b",
            "endpoint_kind": "observed",
            "reworked": False,
        }, target)
        row = target[0]
        for k in ("species", "section", "range_top", "range_base", "biozone",
                  "author", "year", "author_year",
                  "range_top_bed", "range_base_bed",
                  "range_top_idx", "range_base_idx",
                  "endpoint_kind", "occurrence_mode", "confidence", "note"):
            assert k in row, f"field {k!r} missing: {row}"
        # occurrence_mode should be "in_situ" (reworked=False normalized)
        assert row["occurrence_mode"] == "in_situ"
        assert row["endpoint_kind"] == "observed"

    def test_indices_confidence_and_unknowns_are_typed(self):
        target = []
        _normalize_species_into({
            "species": "Genus alpha", "section": "Sec A",
            "range_top_idx": "9", "range_base_idx": 7.0,
            "confidence": "1.4", "unexpected": {"source": "caption"},
        }, target)
        row = target[0]
        assert row["range_top_idx"] == 9 and isinstance(row["range_top_idx"], int)
        assert row["range_base_idx"] == 7 and isinstance(row["range_base_idx"], int)
        assert row["confidence"] == 1.0
        assert row["endpoint_kind"] == "unknown"
        assert row["occurrence_mode"] == "unknown"
        assert row["_extras"] == {"unexpected": {"source": "caption"}}
        for field in ("range_top_idx", "range_base_idx", "confidence"):
            assert field not in row["_extras"]

    def test_non_integral_indices_and_invalid_confidence_remain_missing(self):
        target = []
        _normalize_species_into({
            "species": "Genus alpha", "section": "Sec A",
            "range_top_idx": "9a", "range_base_idx": 7.5,
            "confidence": "unclear", "endpoint_kind": "certain",
            "occurrence_mode": "native",
        }, target)
        row = target[0]
        assert row["range_top_idx"] is None
        assert row["range_base_idx"] is None
        assert row["confidence"] is None
        assert row["endpoint_kind"] == "unknown"
        assert row["occurrence_mode"] == "unknown"
