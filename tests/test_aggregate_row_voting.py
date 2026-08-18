"""Regression tests for P1-1: full-row voting with bio-geological
consistency gate. The previous algorithm emitted chimeric rows where
no single source run observed the merged (range_base, range_top,
biozone) tuple. The merge must now DROP such rows and surface them
via ``chimera_warnings``.

REVIEW-2026-07-25 P1-1.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.aggregate import merge_results


def _run(ranges):
    return {
        "sections": [], "species_ranges": list(ranges), "biozones": [],
        "other_fossils": [], "confidence": 0.9,
    }


class TestChimeraDetection:
    def test_pure_disagreement_drops_chimeric_row(self):
        """Three runs, same (S, X) key, but every field disagreeing —
        per-field mode votes combine into a tuple no run ever observed.
        Merged output must drop the row and surface a chimera warning."""
        results = [
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 10", "range_base": "Bed 8",
                   "biozone": "C Zone"}]),
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 11", "range_base": "Bed 6",
                   "biozone": "A Zone"}]),
        ]
        merged = merge_results(results, total_runs=3)
        # The merged row must NOT appear in species_ranges — it's chimeric.
        sp_rows = merged.get("species_ranges", [])
        assert len(sp_rows) == 0, (
            f"chimeric row should be dropped, got: {sp_rows}"
        )
        # But a warning should be surfaced.
        warnings = merged.get("chimera_warnings", [])
        assert len(warnings) == 1, f"expected 1 chimera warning, got {warnings}"
        assert warnings[0]["table"] == "species_ranges"
        assert warnings[0]["row"]["species"] == "Genus sp."

    def test_consistent_rows_not_flagged_as_chimeric(self):
        """Two runs that agree on the (range_base, range_top, biozone)
        tuple — the merged row IS observed, so no chimera warning."""
        results = [
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
        ]
        merged = merge_results(results, total_runs=2)
        sp_rows = merged.get("species_ranges", [])
        assert len(sp_rows) == 1
        assert "chimera_warnings" not in merged or not merged["chimera_warnings"]

    def test_majority_agreement_with_one_outlier_kept(self):
        """Two runs agree on (Bed 7, Bed 9, B Zone); one run disagrees.
        The merged tuple IS observed by the majority → not chimeric."""
        results = [
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "X",
                   "range_top": "Bed 10", "range_base": "Bed 8",
                   "biozone": "C Zone"}]),
        ]
        merged = merge_results(results, total_runs=3)
        sp_rows = merged.get("species_ranges", [])
        assert len(sp_rows) == 1, f"majority-agreed row should be kept, got: {sp_rows}"
        assert merged.get("chimera_warnings", []) == []

    def test_typed_scientific_fields_and_extras_merge_without_stringification(self):
        results = [
            _run([{"species": "Genus alpha", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7", "biozone": "",
                   "range_top_idx": 9, "range_base_idx": 7,
                   "endpoint_kind": "unknown", "occurrence_mode": "unknown",
                   "confidence": 0.4, "_extras": {"source": "caption", "page": 2}}]),
            _run([{"species": "Genus alpha", "section": "X",
                   "range_top": "Bed 9", "range_base": "Bed 7", "biozone": "",
                   "range_top_idx": 9, "range_base_idx": 8,
                   "endpoint_kind": "observed", "occurrence_mode": "in_situ",
                   "confidence": 0.8, "_extras": {"source": "caption", "review": True}}]),
        ]
        row = merge_results(results, total_runs=2)["species_ranges"][0]
        assert row["range_top_idx"] == 9 and isinstance(row["range_top_idx"], int)
        assert row["range_base_idx"] == 7 and isinstance(row["range_base_idx"], int)
        assert row["confidence"] == 0.6
        assert row["endpoint_kind"] == "observed"
        assert row["occurrence_mode"] == "in_situ"
        assert isinstance(row["_extras"], dict)
        assert row["_extras"]["source"] == "caption"
        assert row["_extras"]["page"] == "2"
        assert row["_extras"]["review"] is True

    def test_single_run_passthrough_no_chimera_check(self):
        """Single run: no chimera check needed (nothing to disagree with)."""
        results = [_run([{"species": "Genus sp.", "section": "X",
                          "range_top": "Bed 9", "range_base": "Bed 7",
                          "biozone": "B Zone"}])]
        merged = merge_results(results, total_runs=1)
        assert len(merged["species_ranges"]) == 1
        assert merged.get("chimera_warnings", []) == []