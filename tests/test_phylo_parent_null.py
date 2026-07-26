"""Test P1-10: phylogenetic tree parent:null semantics.

The PHYLOGENETIC_TREE_SYSTEM_PROMPT and _normalize_phylogenetic_tree_into
require: every root's parent == None, every non-root's parent in node ids.
"""
import pytest


class TestPhyloParentNull:
    """P1-10: root parent must be null; non-root parent must be valid node id."""

    def test_normalize_phylogenetic_tree_root_parent_null(self):
        """Root node with parent=None is accepted."""
        from rca_core.extractor import _normalize_phylogenetic_tree_into
        data = {
            "metadata": {"taxon_group": "Radiolaria"},
            "nodes": [
                {"id": "n0", "parent": None, "name": "Root", "is_leaf": False},
                {"id": "n1", "parent": "n0", "name": "Leaf1", "is_leaf": True},
            ],
            "root_ids": ["n0"],
        }
        result = _normalize_phylogenetic_tree_into(data)
        assert "nodes" in result
        assert result["nodes"][0]["parent"] is None

    def test_root_with_non_null_parent_raises(self):
        """Root node with parent != None raises ValueError."""
        from rca_core.extractor import _normalize_phylogenetic_tree_into
        data = {
            "metadata": {"taxon_group": "Radiolaria"},
            "nodes": [
                {"id": "n0", "parent": "n1", "name": "Root", "is_leaf": False},
                {"id": "n1", "parent": None, "name": "OtherRoot", "is_leaf": False},
            ],
            "root_ids": ["n0", "n1"],
        }
        with pytest.raises(ValueError, match="parent.*None"):
            _normalize_phylogenetic_tree_into(data)

    def test_non_root_with_null_parent_raises(self):
        """Non-root node with parent=None raises ValueError."""
        from rca_core.extractor import _normalize_phylogenetic_tree_into
        data = {
            "metadata": {"taxon_group": "Radiolaria"},
            "nodes": [
                {"id": "n0", "parent": None, "name": "Root", "is_leaf": False},
                {"id": "n1", "parent": None, "name": "Leaf1", "is_leaf": True},
            ],
            "root_ids": ["n0"],
        }
        with pytest.raises(ValueError, match="Non-root node"):
            _normalize_phylogenetic_tree_into(data)

    def test_non_root_with_nonexistent_parent_raises(self):
        """Non-root node referencing non-existent parent raises ValueError."""
        from rca_core.extractor import _normalize_phylogenetic_tree_into
        data = {
            "metadata": {"taxon_group": "Radiolaria"},
            "nodes": [
                {"id": "n0", "parent": None, "name": "Root", "is_leaf": False},
                {"id": "n1", "parent": "n99", "name": "Leaf1", "is_leaf": True},
            ],
            "root_ids": ["n0"],
        }
        with pytest.raises(ValueError, match="parent.*not in node ids"):
            _normalize_phylogenetic_tree_into(data)

    def test_multi_root_forest_all_roots_have_null_parent(self):
        """Forest with two roots (both parent==null) is accepted."""
        from rca_core.extractor import _normalize_phylogenetic_tree_into
        data = {
            "metadata": {"taxon_group": "Radiolaria"},
            "nodes": [
                {"id": "n0", "parent": None, "name": "Root1", "is_leaf": False},
                {"id": "n1", "parent": "n0", "name": "Child1", "is_leaf": True},
                {"id": "n2", "parent": None, "name": "Root2", "is_leaf": False},
                {"id": "n3", "parent": "n2", "name": "Child2", "is_leaf": True},
            ],
            "root_ids": ["n0", "n2"],
        }
        result = _normalize_phylogenetic_tree_into(data)
        # Both roots should have parent None
        nodes_by_id = {n["id"]: n for n in result["nodes"]}
        assert nodes_by_id["n0"]["parent"] is None
        assert nodes_by_id["n2"]["parent"] is None

    def test_prompt_mentions_parent_null_invariant(self):
        """prompt.py PHYLOGENETIC_TREE_SYSTEM_PROMPT must mention the parent invariant."""
        from rca_core.prompt import PHYLOGENETIC_TREE_SYSTEM_PROMPT
        assert "PARENT INVARIANT" in PHYLOGENETIC_TREE_SYSTEM_PROMPT or "parent" in PHYLOGENETIC_TREE_SYSTEM_PROMPT.lower()
        # Must explicitly state root's parent is null
        assert "parent" in PHYLOGENETIC_TREE_SYSTEM_PROMPT
        # Must state non-root parent must be in node ids
        text = PHYLOGENETIC_TREE_SYSTEM_PROMPT.lower()
        assert "null" in text or "None" in text
