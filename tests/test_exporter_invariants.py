"""Regression tests for P2-5: rca_core.exporter.validate_export_invariants
must catch missing required fields and violated scientific constraints
before CSV / xlsx / JSON export.

REVIEW-2026-07-25 P2-5.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.exporter import validate_export_invariants, to_xlsx


class TestExportInvariants:
    def test_clean_data_passes(self):
        data = {
            "species_ranges": [
                {"species": "G", "section": "X", "range_base": "7",
                 "range_top": "9", "biozone": "B"},
            ],
            "biozones": [{"name": "B"}],
            "sections": [{"name": "X"}],
        }
        ok, issues = validate_export_invariants(data)
        assert ok
        assert issues == []

    def test_missing_species_field_is_an_issue(self):
        data = {
            "species_ranges": [
                {"section": "X", "range_base": "7", "range_top": "9"},  # no species
            ],
            "biozones": [{"name": "B"}],
            "sections": [{"name": "X"}],
        }
        ok, issues = validate_export_invariants(data)
        assert not ok
        assert any(i.get("missing") == "species" for i in issues)

    def test_inverted_range_emits_constraint_issue(self):
        data = {
            "species_ranges": [
                {"species": "G", "section": "X",
                 "range_base": "9", "range_top": "5",  # inverted
                 "biozone": "B"},
            ],
            "biozones": [{"name": "B"}],
            "sections": [{"name": "X"}],
        }
        ok, issues = validate_export_invariants(data)
        assert not ok
        assert any(i.get("constraint") == "range_base_le_range_top"
                   for i in issues)

    def test_multiple_issues_per_row(self):
        """A row with multiple problems should report ALL of them."""
        data = {
            "species_ranges": [
                {"species": "", "section": "", "range_base": "9",
                 "range_top": "5", "biozone": ""},  # many missing
            ],
            "biozones": [{"name": "B"}],
            "sections": [{"name": "X"}],
        }
        ok, issues = validate_export_invariants(data)
        assert not ok
        missing = {i.get("missing") for i in issues}
        assert "species" in missing
        assert "section" in missing

    def test_other_fossils_non_dict_is_allowed(self):
        """other_fossils plain-string rows are valid and skipped by the
        validator (no required fields)."""
        data = {
            "species_ranges": [],
            "biozones": [],
            "sections": [],
            "other_fossils": ["Genus A sp.", "Genus B sp."],
        }
        ok, issues = validate_export_invariants(data)
        assert ok, f"string other_fossils must pass: {issues}"
        assert issues == []

    def test_biozones_require_name(self):
        data = {
            "species_ranges": [],
            "biozones": [{"age": "Albian"}],  # no name
            "sections": [],
        }
        ok, issues = validate_export_invariants(data)
        assert not ok

    def test_to_xlsx_raises_on_invariant_violation(self):
        """F-1 (REVIEW-2026-07-25 P2-5): to_xlsx must propagate
        ValueError when data violates an export invariant."""
        bad_data = {
            "species_ranges": [
                {"section": "X", "range_base": "9",
                 "range_top": "5"},  # missing species + inverted range
            ],
            "biozones": [],
            "sections": [{"name": "X"}],
        }
        try:
            to_xlsx(bad_data)
            assert False, "to_xlsx must raise ValueError on invariant violation"
        except ValueError as exc:
            assert "export invariants violated" in str(exc)
            # issues must be non-empty
            import ast
            issues_literal = str(exc).split("export invariants violated:", 1)[1].strip()
            issues = ast.literal_eval(issues_literal)
            assert len(issues) > 0

    def test_to_xlsx_succeeds_on_clean_data(self):
        """Sanity check: clean data passes through to_xlsx without error."""
        clean = {
            "species_ranges": [
                {"species": "G", "section": "X",
                 "range_base": "5", "range_top": "9",
                 "biozone": "B"},
            ],
            "biozones": [{"name": "B"}],
            "sections": [{"name": "X"}],
        }
        # Should return bytes, not raise
        result = to_xlsx(clean)
        assert isinstance(result, bytes)
        assert len(result) > 0