"""Gold-standard metric tests (mock-based, no real LLM).

These tests verify the metric calculation logic using synthetic ground
truth. They should all pass without any API calls.
"""
from __future__ import annotations

import pytest
from rca_core.eval_metrics import (
    species_precision_recall,
    range_top_accuracy,
    biozone_accuracy,
    abundance_sum_error,
    phylogenetic_topology_distance,
    _normalize_taxon,
)


# ---------------------------------------------------------------------------
# _normalize_taxon
# ---------------------------------------------------------------------------


class TestNormalizeTaxon:
    def test_basic(self):
        assert _normalize_taxon("Ammonites koslovensis") == "ammonites koslovensis"

    def test_iczn_cf(self):
        assert _normalize_taxon("Ammonites cf. koslovensis") == "ammonites koslovensis"

    def test_iczn_aff(self):
        assert _normalize_taxon("Genus aff. species") == "genus species"

    def test_iczn_question(self):
        assert _normalize_taxon("Genus? species") == "genus species"

    def test_iczn_exgr(self):
        assert _normalize_taxon("Genus ex gr. species") == "genus species"

    def test_whitespace(self):
        assert _normalize_taxon("  Genus   species  ") == "genus species"


# ---------------------------------------------------------------------------
# species_precision_recall
# ---------------------------------------------------------------------------


class TestSpeciesPrecisionRecall:
    def test_perfect_match(self):
        pred = [{"species": "Genus a"}, {"species": "Genus b"}]
        true = [{"species": "Genus a"}, {"species": "Genus b"}]
        m = species_precision_recall(pred, true)
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
        assert m["f1"] == 1.0
        assert m["true_positives"] == 2

    def test_partial_match(self):
        pred = [{"species": "Genus a"}, {"species": "Genus c"}]
        true = [{"species": "Genus a"}, {"species": "Genus b"}]
        m = species_precision_recall(pred, true)
        assert 0.4 < m["precision"] < 0.6
        assert m["recall"] == 0.5
        assert m["f1"] == pytest.approx(0.5 * 0.5 * 2 / (0.5 + 0.5), rel=0.01)

    def test_complete_miss(self):
        pred = [{"species": "Genus c"}, {"species": "Genus d"}]
        true = [{"species": "Genus a"}, {"species": "Genus b"}]
        m = species_precision_recall(pred, true)
        assert m["precision"] == 0.0
        assert m["recall"] == 0.0
        assert m["f1"] == 0.0

    def test_empty_ground_truth(self):
        pred = [{"species": "Genus a"}]
        true: list = []
        m = species_precision_recall(pred, true)
        assert m["precision"] == 0.0
        assert m["recall"] == 0.0

    def test_iczn_fuzzy_match(self):
        pred = [{"species": "Ammonites cf. koslovensis"}]
        true = [{"species": "Ammonites koslovensis"}]
        m = species_precision_recall(pred, true)
        assert m["recall"] == 1.0  # GT species found
        assert m["precision"] == 1.0  # predicted is valid match


# ---------------------------------------------------------------------------
# range_top_accuracy
# ---------------------------------------------------------------------------


class TestRangeTopAccuracy:
    def test_exact_match(self):
        pred = [{"species": "A", "range_top": "8"}]
        true = [{"species": "A", "range_top": "8"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["acc_exact"] == 1.0
        assert m["exact"] == 1

    def test_within_tolerance(self):
        pred = [{"species": "A", "range_top": "9"}]
        true = [{"species": "A", "range_top": "8"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["acc_tolerance"] == 1.0
        assert m["acc_exact"] == 0.0

    def test_wrong(self):
        pred = [{"species": "A", "range_top": "15"}]
        true = [{"species": "A", "range_top": "8"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["acc_tolerance"] == 0.0
        assert m["wrong"] == 1

    def test_unmatched_species_skipped(self):
        pred = [{"species": "A", "range_top": "8"}, {"species": "B", "range_top": "10"}]
        true = [{"species": "A", "range_top": "8"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["acc_exact"] == 1.0

    def test_bed_label_formats(self):
        pred = [{"species": "A", "range_top": "Bed 9"}]
        true = [{"species": "A", "range_top": "9"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["acc_exact"] == 1.0


# ---------------------------------------------------------------------------
# biozone_accuracy
# ---------------------------------------------------------------------------


class TestBiozoneAccuracy:
    def test_exact_match(self):
        pred = [{"name": "Aspidoceras Zone"}]
        true = [{"name": "Aspidoceras Zone"}]
        m = biozone_accuracy(pred, true)
        assert m["exact"] == 1
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0

    def test_fuzzy_zone_suffix(self):
        pred = [{"name": "Aspidoceras"}]
        true = [{"name": "Aspidoceras Zone"}]
        m = biozone_accuracy(pred, true)
        assert m["fuzzy"] == 1
        assert m["precision"] == 1.0

    def test_miss(self):
        pred = [{"name": "Other Zone"}]
        true = [{"name": "Aspidoceras Zone"}]
        m = biozone_accuracy(pred, true)
        assert m["missed"] == 1
        assert m["recall"] == 0.0


# ---------------------------------------------------------------------------
# abundance_sum_error
# ---------------------------------------------------------------------------


class TestAbundanceSumError:
    def test_perfect_sum(self):
        pred = [
            {"level": "1", "taxon": "A", "abundance": "60", "abundance_unit": "%"},
            {"level": "1", "taxon": "B", "abundance": "40", "abundance_unit": "%"},
        ]
        true = [
            {"level": "1", "taxon": "A", "abundance": "60", "abundance_unit": "%"},
            {"level": "1", "taxon": "B", "abundance": "40", "abundance_unit": "%"},
        ]
        m = abundance_sum_error(pred, true)
        assert m["max_error"] < 0.1

    def test_sum_off_by_10(self):
        pred = [
            {"level": "1", "taxon": "A", "abundance": "55", "abundance_unit": "%"},
            {"level": "1", "taxon": "B", "abundance": "35", "abundance_unit": "%"},
        ]
        true = [
            {"level": "1", "taxon": "A", "abundance": "60", "abundance_unit": "%"},
            {"level": "1", "taxon": "B", "abundance": "40", "abundance_unit": "%"},
        ]
        m = abundance_sum_error(pred, true)
        assert m["max_error"] == 10.0

    def test_ignores_non_percent(self):
        # When BOTH predicted and ground-truth use non-% units at the same
        # level, the function skips them (treats as non-comparable).
        # Result: max_error = 0.0 because nothing was compared.
        pred = [
            {"level": "1", "taxon": "A", "abundance": "60", "abundance_unit": "count"},
            {"level": "1", "taxon": "B", "abundance": "40", "abundance_unit": "count"},
        ]
        true = [
            {"level": "1", "taxon": "A", "abundance": "60", "abundance_unit": "count"},
            {"level": "1", "taxon": "B", "abundance": "40", "abundance_unit": "count"},
        ]
        m = abundance_sum_error(pred, true)
        # Both sides use count (not %), so neither is summed — 0 error.
        assert m["max_error"] == 0.0


# ---------------------------------------------------------------------------
# phylogenetic_topology_distance
# ---------------------------------------------------------------------------


class TestPhylogeneticTopologyDistance:
    def test_identical_trees(self):
        tree = {
            "metadata": {"tree_type": "cladogram", "rooted": True},
            "root_ids": ["root"],
            "nodes": [
                {"id": "root", "parent": None, "name": "", "is_leaf": False, "branch_length": 0.3, "support": None},
                {"id": "n1", "parent": "root", "name": "", "is_leaf": False, "branch_length": 0.2, "support": 95},
                {"id": "sp1", "parent": "n1", "name": "Alpha", "is_leaf": True, "branch_length": 0.1, "support": None},
                {"id": "sp2", "parent": "n1", "name": "Beta", "is_leaf": True, "branch_length": 0.1, "support": None},
            ],
            "confidence": 0.9,
        }
        d = phylogenetic_topology_distance(tree, tree)
        assert d == 0

    def test_different_trees(self):
        # With 3+ leaves, trees with different groupings have non-zero RF distance.
        # Tree1: (sp1, sp2) are sisters; tree2: (sp1, sp3) are sisters.
        tree1 = {
            "metadata": {"tree_type": "cladogram", "rooted": True},
            "root_ids": ["root"],
            "nodes": [
                {"id": "root", "parent": None, "name": "", "is_leaf": False, "branch_length": None, "support": None},
                {"id": "n1", "parent": "root", "name": "", "is_leaf": False, "branch_length": 0.2, "support": 95},
                {"id": "sp1", "parent": "n1", "name": "Alpha", "is_leaf": True, "branch_length": 0.1, "support": None},
                {"id": "sp2", "parent": "n1", "name": "Beta", "is_leaf": True, "branch_length": 0.1, "support": None},
                {"id": "sp3", "parent": "root", "name": "Gamma", "is_leaf": True, "branch_length": 0.1, "support": None},
            ],
            "confidence": 0.9,
        }
        # (sp1, sp3) are sisters instead
        tree2 = {
            "metadata": {"tree_type": "cladogram", "rooted": True},
            "root_ids": ["root"],
            "nodes": [
                {"id": "root", "parent": None, "name": "", "is_leaf": False, "branch_length": None, "support": None},
                {"id": "n1", "parent": "root", "name": "", "is_leaf": False, "branch_length": 0.2, "support": 80},
                {"id": "sp1", "parent": "n1", "name": "Alpha", "is_leaf": True, "branch_length": 0.1, "support": None},
                {"id": "sp3", "parent": "n1", "name": "Gamma", "is_leaf": True, "branch_length": 0.1, "support": None},
                {"id": "sp2", "parent": "root", "name": "Beta", "is_leaf": True, "branch_length": 0.1, "support": None},
            ],
            "confidence": 0.9,
        }
        d = phylogenetic_topology_distance(tree1, tree2)
        assert d > 0
