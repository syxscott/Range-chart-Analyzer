"""Integration tests for phylogenetic tree extraction feature."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
