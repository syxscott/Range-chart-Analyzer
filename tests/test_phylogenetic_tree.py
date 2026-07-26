"""Integration tests for phylogenetic tree extraction feature."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch, call

from rca_core import extractor as E
from rca_core.aggregate import (
    PHYLOGENETIC_TREE_SCHEMA,
    SCHEMA_BY_MODE,
    merge_results,
)


MOCK_TREE_RESPONSE = {
    "version": "1",
    "metadata": {
        "taxon_group": "Radiolaria",
        "extraction_timestamp": "2026-07-26T10:00:00",
        "image_source": "fig2_phylogeny.png",
        "total_nodes": 4,
        "root_name": "Spasmaria",
    },
    "nodes": [
        {
            "id": "n0",
            "name": "Spasmaria",
            "support": None,
            "support_confidence": None,
            "branch_length": None,
            "depth_range_m": "",
            "depth_confidence": None,
            "sequence_count": 15,
            "is_leaf": False,
            "parent": None,
        },
        {
            "id": "n1",
            "name": "Clade A",
            "support": 89.0,
            "support_confidence": 0.95,
            "branch_length": 0.12,
            "depth_range_m": "",
            "depth_confidence": None,
            "sequence_count": None,
            "is_leaf": False,
            "parent": "n0",
        },
        {
            "id": "n2",
            "name": "Spherical Radiolaria",
            "support": 76.0,
            "support_confidence": 0.88,
            "branch_length": None,
            "depth_range_m": "0-200",
            "depth_confidence": 0.85,
            "sequence_count": 8,
            "is_leaf": True,
            "parent": "n1",
        },
        {
            "id": "n3",
            "name": "Spasmaria testus",
            "support": None,
            "support_confidence": None,
            "branch_length": None,
            "depth_range_m": "200-1000",
            "depth_confidence": 0.9,
            "sequence_count": 7,
            "is_leaf": True,
            "parent": "n1",
        },
    ],
    "root_ids": ["n0"],
    "legend": {
        "depth_colors": {
            "epipelagic": {"hex": "#003366", "depth_m": "0-200"},
            "mesopelagic": {"hex": "#00BFFF", "depth_m": "200-1000"},
        },
        "sequence_count_min": 7,
        "sequence_count_max": 15,
    },
    "confidence": 0.85,
}


class TestExtractPhylogeneticTreeSchema(unittest.TestCase):
    """Test that extract_phylogenetic_tree() returns valid schema."""

    def test_extract_returns_valid_schema(self):
        """Mock call_llm_api to return a sample tree JSON, call extract_phylogenetic_tree(),
        verify result.ok, result.data has nodes/metadata/root_ids/legend."""

        def fake_call(*args, **kwargs):
            return (json.dumps(MOCK_TREE_RESPONSE), False, 200, "", {})

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            r = E.extract_phylogenetic_tree(
                api_key="k", image_b64="QQ==", media_type="image/png",
            )

        self.assertTrue(r.ok, msg=f"Expected ok=True, got ok=False. warning={r.warning}")
        self.assertIsInstance(r.data, dict)

        # Verify required top-level fields
        self.assertIn("nodes", r.data)
        self.assertIn("metadata", r.data)
        self.assertIn("root_ids", r.data)
        self.assertIn("legend", r.data)

        # Verify nodes structure
        nodes = r.data["nodes"]
        self.assertIsInstance(nodes, list)
        self.assertEqual(len(nodes), 4)

        # Verify first node (root) structure
        root = nodes[0]
        self.assertEqual(root["id"], "n0")
        self.assertEqual(root["parent"], None)
        self.assertFalse(root["is_leaf"])

        # Verify leaf node
        leaf = nodes[2]
        self.assertTrue(leaf["is_leaf"])
        self.assertIsNotNone(leaf["parent"])

        # Verify metadata
        metadata = r.data["metadata"]
        self.assertEqual(metadata["taxon_group"], "Radiolaria")
        self.assertEqual(metadata["root_name"], "Spasmaria")

        # Verify root_ids
        self.assertEqual(r.data["root_ids"], ["n0"])

        # Verify legend
        legend = r.data["legend"]
        self.assertIn("depth_colors", legend)
        self.assertIn("epipelagic", legend["depth_colors"])

    def test_extract_with_empty_image_returns_error(self):
        """Empty image_b64 should return err.imageRead."""
        r = E.extract_phylogenetic_tree(
            api_key="k", image_b64="", media_type="image/png",
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.error_key, "err.imageRead")


class TestProgressCallback(unittest.TestCase):
    """Test that progress_callback is invoked with stage strings."""

    def test_progress_callback_invoked(self):
        """Mock call_llm_api, track calls to progress_callback, verify it's called
        with submitting/uploading/thinking."""

        progress_calls = []

        def progress_callback(stage):
            progress_calls.append(stage)

        def fake_call(*args, **kwargs):
            # Invoke progress_callback if provided in kwargs
            cb = kwargs.get("progress_callback")
            if cb:
                cb("submitting")
                cb("uploading")
                cb("thinking")
            return (json.dumps(MOCK_TREE_RESPONSE), False, 200, "", {})

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            r = E.extract_phylogenetic_tree(
                api_key="k",
                image_b64="QQ==",
                media_type="image/png",
                progress_callback=progress_callback,
            )

        self.assertTrue(r.ok)
        self.assertIn("submitting", progress_calls)
        self.assertIn("uploading", progress_calls)
        self.assertIn("thinking", progress_calls)

    def test_progress_callback_receives_stage_strings(self):
        """Verify progress_callback receives string stages, not other types."""
        received = []

        def track(stage):
            received.append(stage)

        def fake_call(*args, **kwargs):
            cb = kwargs.get("progress_callback")
            if cb:
                cb("submitting")
                cb("uploading")
                cb("thinking")
            return (json.dumps(MOCK_TREE_RESPONSE), False, 200, "", {})

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            E.extract_phylogenetic_tree(
                api_key="k",
                image_b64="QQ==",
                media_type="image/png",
                progress_callback=track,
            )

        for stage in received:
            self.assertIsInstance(stage, str, f"Expected str, got {type(stage)}")


class TestMergeSchemaRegistered(unittest.TestCase):
    """Test that merge_results() with PHYLOGENETIC_TREE_SCHEMA works."""

    def test_merge_schema_registered(self):
        """Verify PHYLOGENETIC_TREE_SCHEMA is in SCHEMA_BY_MODE."""
        self.assertIn("phylogenetic_tree", SCHEMA_BY_MODE)
        self.assertIs(SCHEMA_BY_MODE["phylogenetic_tree"], PHYLOGENETIC_TREE_SCHEMA)

    def test_merge_results_with_phylo_schema(self):
        """Test merge_results() works with phylogenetic tree data from multiple runs."""
        run1 = MOCK_TREE_RESPONSE

        run2 = {
            "version": "1",
            "metadata": {
                "taxon_group": "Radiolaria",
                "extraction_timestamp": "2026-07-26T11:00:00",
                "image_source": "fig2_phylogeny.png",
                "total_nodes": 4,
                "root_name": "Spasmaria",
            },
            "nodes": [
                {
                    "id": "n0",
                    "name": "Spasmaria",
                    "support": None,
                    "support_confidence": None,
                    "branch_length": None,
                    "depth_range_m": "",
                    "depth_confidence": None,
                    "sequence_count": 14,
                    "is_leaf": False,
                    "parent": None,
                },
                {
                    "id": "n1",
                    "name": "Clade A",
                    "support": 90.0,
                    "support_confidence": 0.93,
                    "branch_length": 0.11,
                    "depth_range_m": "",
                    "depth_confidence": None,
                    "sequence_count": None,
                    "is_leaf": False,
                    "parent": "n0",
                },
                {
                    "id": "n2",
                    "name": "Spherical Radiolaria",
                    "support": 75.0,
                    "support_confidence": 0.87,
                    "branch_length": None,
                    "depth_range_m": "0-200",
                    "depth_confidence": 0.84,
                    "sequence_count": 9,
                    "is_leaf": True,
                    "parent": "n1",
                },
                {
                    "id": "n3",
                    "name": "Spasmaria testus",
                    "support": None,
                    "support_confidence": None,
                    "branch_length": None,
                    "depth_range_m": "200-1000",
                    "depth_confidence": 0.88,
                    "sequence_count": 6,
                    "is_leaf": True,
                    "parent": "n1",
                },
            ],
            "root_ids": ["n0"],
            "legend": {
                "depth_colors": {
                    "epipelagic": {"hex": "#003366", "depth_m": "0-200"},
                    "mesopelagic": {"hex": "#00BFFF", "depth_m": "200-1000"},
                },
                "sequence_count_min": 6,
                "sequence_count_max": 14,
            },
            "confidence": 0.82,
        }

        merged = merge_results(
            [run1, run2],
            total_runs=2,
            schema=PHYLOGENETIC_TREE_SCHEMA,
        )

        # Verify merged structure retains expected fields
        self.assertIn("nodes", merged)
        self.assertIn("metadata", merged)
        self.assertIn("root_ids", merged)
        self.assertIn("legend", merged)

        # Nodes should be deduped by id (primary_id_keys=["id"])
        node_ids = [n["id"] for n in merged["nodes"]]
        self.assertEqual(sorted(node_ids), ["n0", "n1", "n2", "n3"])

        # Legend list_keys should be merged
        self.assertIn("legend", merged)


# ------------------------------------------------------------------
# Raw trees for P0-3 normalizer / Newick tests
# ------------------------------------------------------------------

RAW_TREE_BL_ONLY = {
    "metadata": {"taxon_group": "Foraminifera", "extraction_timestamp": "2026-07-26"},
    "nodes": [
        {"id": "n0", "name": "Root", "support": None, "branch_length": 0.5,
         "is_leaf": False, "parent": None},
        {"id": "n1", "name": "A", "support": None, "branch_length": 0.3,
         "is_leaf": True, "parent": "n0"},
        {"id": "n2", "name": "B", "support": None, "branch_length": 0.4,
         "is_leaf": True, "parent": "n0"},
    ],
    "root_ids": ["n0"],
    "confidence": 0.9,
}

RAW_TREE_FOREST = {
    "metadata": {"taxon_group": "Radiolaria", "extraction_timestamp": "2026-07-26"},
    "nodes": [
        {"id": "r1", "name": "Tree1Root", "support": None,
         "branch_length": None, "is_leaf": False, "parent": None},
        {"id": "r2", "name": "Tree2Root", "support": None,
         "branch_length": None, "is_leaf": False, "parent": None},
        {"id": "l1", "name": "LeafA", "support": None,
         "branch_length": 0.1, "is_leaf": True, "parent": "r1"},
        {"id": "l2", "name": "LeafB", "support": None,
         "branch_length": 0.2, "is_leaf": True, "parent": "r2"},
    ],
    "root_ids": ["r1", "r2"],
    "confidence": 0.8,
}

RAW_TREE_ESCAPE = {
    "metadata": {"taxon_group": "Radiolaria", "extraction_timestamp": "2026-07-26"},
    "nodes": [
        {"id": "n0", "name": "Root (sp. nov.)", "support": None,
         "branch_length": 0.1, "is_leaf": False, "parent": None},
        {"id": "n1", "name": "Clade:Test", "support": 95.0,
         "branch_length": 0.2, "is_leaf": True, "parent": "n0"},
    ],
    "root_ids": ["n0"],
    "confidence": 0.9,
}


# ------------------------------------------------------------------
# P0-3: normalizer invariants
# ------------------------------------------------------------------

class TestNormalizerInvariants(unittest.TestCase):

    def test_root_ids_empty_raises(self):
        raw = dict(MOCK_TREE_RESPONSE, root_ids=[])
        with self.assertRaises(ValueError) as ctx:
            E._normalize_phylogenetic_tree_into(raw)
        self.assertIn("root_ids is empty", str(ctx.exception))

    def test_root_ids_unknown_node_raises(self):
        raw = dict(MOCK_TREE_RESPONSE, root_ids=["n99"])
        with self.assertRaises(ValueError) as ctx:
            E._normalize_phylogenetic_tree_into(raw)
        self.assertIn("root_ids contains unknown node id", str(ctx.exception))

    def test_non_root_node_missing_parent_raises(self):
        raw = copy.deepcopy(MOCK_TREE_RESPONSE)
        for n in raw["nodes"]:
            if n["id"] == "n1":
                n["parent"] = None
        with self.assertRaises(ValueError) as ctx:
            E._normalize_phylogenetic_tree_into(raw)
        self.assertIn("Non-root node n1 must have a parent", str(ctx.exception))

    def test_non_root_node_parent_not_in_ids_raises(self):
        raw = copy.deepcopy(MOCK_TREE_RESPONSE)
        for n in raw["nodes"]:
            if n["id"] == "n2":
                n["parent"] = "n999"
        with self.assertRaises(ValueError) as ctx:
            E._normalize_phylogenetic_tree_into(raw)
        self.assertIn("references parent n999 not in node ids", str(ctx.exception))

    def test_root_node_with_non_null_parent_raises(self):
        raw = copy.deepcopy(MOCK_TREE_RESPONSE)
        for n in raw["nodes"]:
            if n["id"] == "n0":
                n["parent"] = "n1"
        with self.assertRaises(ValueError) as ctx:
            E._normalize_phylogenetic_tree_into(raw)
        self.assertIn("Root node n0 must have parent == None", str(ctx.exception))

    def test_support_out_of_range_raises(self):
        raw = copy.deepcopy(MOCK_TREE_RESPONSE)
        for n in raw["nodes"]:
            if n["id"] == "n1":
                n["support"] = 150.0
        with self.assertRaises(ValueError) as ctx:
            E._normalize_phylogenetic_tree_into(raw)
        self.assertIn("support must be in [0, 100]", str(ctx.exception))

    def test_is_leaf_reverse_check_corrects_mismatch(self):
        raw = copy.deepcopy(MOCK_TREE_RESPONSE)
        for n in raw["nodes"]:
            if n["id"] == "n2":
                n["is_leaf"] = False  # wrong: n2 has no children
        result = E._normalize_phylogenetic_tree_into(raw)
        n2 = next(nn for nn in result["nodes"] if nn["id"] == "n2")
        self.assertTrue(n2["is_leaf"])

    def test_confidence_clamped(self):
        raw = dict(MOCK_TREE_RESPONSE, confidence=1.5)
        result = E._normalize_phylogenetic_tree_into(raw)
        self.assertEqual(result["confidence"], 1.0)
        raw = dict(MOCK_TREE_RESPONSE, confidence=-0.5)
        result = E._normalize_phylogenetic_tree_into(raw)
        self.assertEqual(result["confidence"], 0.0)

    def test_metadata_preserves_raw_fields(self):
        result = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        self.assertEqual(result["metadata"].get("taxon_group"), "Radiolaria")
        self.assertEqual(result["metadata"].get("root_name"), "Spasmaria")

    def test_node_extras_preserved(self):
        result = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        n2 = next(nn for nn in result["nodes"] if nn["id"] == "n2")
        self.assertIn("metadata", n2)
        self.assertEqual(n2["metadata"].get("depth_range_m"), "0-200")

    def test_array_root_unwrap(self):
        raw = {"_array_root": [copy.deepcopy(MOCK_TREE_RESPONSE)]}
        result = E._normalize_phylogenetic_tree_into(raw)
        self.assertEqual(result["metadata"].get("taxon_group"), "Radiolaria")
        self.assertEqual(len(result["nodes"]), 4)


# ------------------------------------------------------------------
# P0-3: Newick export
# ------------------------------------------------------------------

class TestNewickExport(unittest.TestCase):

    def test_basic_tree(self):
        result = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        newick = E.to_newick(result)
        self.assertTrue(newick.endswith(";"))
        self.assertIn("Spherical Radiolaria", newick)

    def test_newick_no_branch_length(self):
        raw = {
            "metadata": {},
            "nodes": [
                {"id": "n0", "name": "Root", "support": None,
                 "branch_length": None, "is_leaf": False, "parent": None},
                {"id": "n1", "name": "Leaf", "support": None,
                 "branch_length": None, "is_leaf": True, "parent": "n0"},
            ],
            "root_ids": ["n0"],
            "confidence": 0.9,
        }
        result = E._normalize_phylogenetic_tree_into(raw)
        newick = E.to_newick(result)
        self.assertNotIn(":", newick)

    def test_newick_branch_length_included(self):
        result = E._normalize_phylogenetic_tree_into(RAW_TREE_BL_ONLY)
        newick = E.to_newick(result)
        self.assertIn(":0.3", newick)
        self.assertIn(":0.4", newick)

    def test_newick_support_included(self):
        result = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        newick = E.to_newick(result)
        self.assertIn("89", newick)

    def test_newick_support_missing_ok(self):
        raw = {
            "metadata": {},
            "nodes": [
                {"id": "n0", "name": "Root", "support": None,
                 "branch_length": 0.1, "is_leaf": False, "parent": None},
                {"id": "n1", "name": "Leaf", "support": None,
                 "branch_length": 0.2, "is_leaf": True, "parent": "n0"},
            ],
            "root_ids": ["n0"],
            "confidence": 0.9,
        }
        result = E._normalize_phylogenetic_tree_into(raw)
        newick = E.to_newick(result)
        self.assertNotIn("None", newick)

    def test_newick_forest(self):
        result = E._normalize_phylogenetic_tree_into(RAW_TREE_FOREST)
        newick = E.to_newick(result)
        self.assertTrue(newick.endswith(";"))
        self.assertIn("LeafA", newick)
        self.assertIn("LeafB", newick)

    def test_newick_name_escaping(self):
        result = E._normalize_phylogenetic_tree_into(RAW_TREE_ESCAPE)
        newick = E.to_newick(result)
        self.assertNotIn("(sp.", newick)
        self.assertNotIn("Clade:Test", newick)

    def test_to_newick_file(self):
        import os, tempfile
        result = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".nwk", delete=False, encoding="utf-8"
        ) as fh:
            path = fh.name
        try:
            from rca_core.exporter import to_newick_file
            to_newick_file(result, path)
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            self.assertTrue(content.endswith(";\n") or content.endswith(";"))
            self.assertIn("Spasmaria", content)
        finally:
            os.unlink(path)


class TestNewickParity(unittest.TestCase):

    def test_deterministic(self):
        result = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        n1 = E.to_newick(result)
        n2 = E.to_newick(result)
        self.assertEqual(n1, n2)

    def test_same_tree_same_output(self):
        r1 = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        r2 = E._normalize_phylogenetic_tree_into(MOCK_TREE_RESPONSE)
        self.assertEqual(E.to_newick(r1), E.to_newick(r2))


if __name__ == "__main__":
    unittest.main()
