"""Regression tests for P0-2: columnar-section sections must not be
double-merged (rca_core/aggregate.py:574 already produces a correct
result via _merge_primary_list; the elif block at 632-674 was an
unconditional overwrite that lost seen_in_run dedup and sort_keys and
could produce agreement_count > n).

REVIEW-2026-07-25 P0-2.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.aggregate import merge_columnar_results


def _sec(sid, group="A", name="Sec A", thickness_m=10.0):
    return {
        "id": sid, "group": group, "name": name,
        "thickness_m": thickness_m,
        "lithology_blocks": [], "age_units": [], "samples": [],
    }


class TestColumnarSingleMerge:
    """columnar merge must NOT double-merge sections."""

    def test_no_double_merge_with_duplicate_in_run(self):
        """Run 1 emits the same (id, group) twice (model hiccup);
        Run 2 emits it once. agreement must be '2/2', not '3/2'."""
        results = [
            {
                "sections": [_sec("Ki-1"), _sec("Ki-1")],
                "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
                "confidence": 0.9,
            },
            {
                "sections": [_sec("Ki-1")],
                "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
                "confidence": 0.9,
            },
        ]
        merged = merge_columnar_results(results, total_runs=2)
        secs = merged["sections"]
        ki1 = [s for s in secs if s.get("id") == "Ki-1"]
        assert len(ki1) == 1, f"expected 1 Ki-1, got {len(ki1)}: {ki1}"
        assert ki1[0]["agreement_count"] == 2, (
            f"agreement_count must be 2 (one per run), got {ki1[0]['agreement_count']}"
        )
        assert ki1[0]["agreement"] == "2/2"

    def test_sort_keys_applied(self):
        """sort_keys must put Ki-1 before Ki-2 (id ascending)."""
        results = [
            {
                "sections": [_sec("Ki-2"), _sec("Ki-1")],
                "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
                "confidence": 0.9,
            },
            {
                "sections": [_sec("Ki-1"), _sec("Ki-2")],
                "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
                "confidence": 0.9,
            },
        ]
        merged = merge_columnar_results(results, total_runs=2)
        ids = [s.get("id") for s in merged["sections"]]
        assert ids == ["Ki-1", "Ki-2"], (
            f"sort_keys not applied — sections out of order: {ids}"
        )

    def test_agreement_count_never_exceeds_n(self):
        """agreement_count must be <= total_runs for every section."""
        results = [
            {
                "sections": [_sec("Ki-1"), _sec("Ki-1"), _sec("Ki-1")],
                "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
                "confidence": 0.9,
            },
            {
                "sections": [_sec("Ki-1")],
                "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
                "confidence": 0.9,
            },
        ]
        merged = merge_columnar_results(results, total_runs=2)
        for s in merged["sections"]:
            assert s["agreement_count"] <= 2, (
                f"agreement_count {s['agreement_count']} exceeds total_runs=2 "
                f"for section {s.get('id')}"
            )