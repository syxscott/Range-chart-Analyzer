"""A non-mapping `metadata` is not metadata (AUDIT-2026-09-30).

`metadata` and `legend` are the two OPTIONAL mappings on a phylogenetic tree,
and a model can put a string in either. Both sides used `x or {}` /
`rcaPyOr(x, {})`, which rejects only FALSY non-mappings, so a truthy string
slipped through and each engine failed differently:

  * rca_core/extractor.py called `metadata_raw.get("title", "")` and raised
    ``AttributeError: 'str' object has no attribute 'get'``. That escapes the
    ValueError contract this function keeps for every other rejection -- the
    user saw "normalize failed: 'str' object has no attribute 'get'", an
    implementation detail rather than anything about their figure. It is the
    same shape as the root_ids TypeError fixed on 2026-09-29, one field over.
  * js/minimax.js reached ``Object.keys("str")``, which yields ["0","1","2"],
    and emitted ``{"0": "s", "1": "t", "2": "r"}`` -- a string's character
    positions presented as metadata keys.

Both sides now treat a non-mapping as absent, matching the `legend` line that
was already guarded on both engines.

WHY THIS IS A PYTEST AND NOT A PARITY CASE
------------------------------------------
The three new fixture cases (ph_non_dict_metadata, ph_non_dict_legend,
ph_metadata_list) pin the JS side, and the parity harness does catch a JS
regression -- counter-verified: removing the guard fails
ph_non_dict_metadata and ph_metadata_list.

It cannot catch the PYTHON side, and that is structural rather than a gap:
tests_diff_frontend_parity.js never runs Python. It compares the `python`
value RECORDED in tests/fixtures/frontend_parity_2026_09_20.json against the
LIVE JS result, so a Python edit cannot move the number it compares against.
difffuzz_normalize.py does run both and does catch it (133 mismatched cases
over 400, all "python raised / js succeeded"), but it is deliberately not in
CI. So the Python half of this contract needs a test that actually executes
Python in the build, which is what this file is.
"""

from __future__ import annotations

import pytest

from rca_core.extractor import _normalize_phylogenetic_tree_into

BASE_NODES = [{"id": "r", "parent": None, "name": "A"}]
BASE = {"root_ids": ["r"], "nodes": BASE_NODES, "confidence": 0.9}


def _norm(**over):
    data = dict(BASE)
    data.update(over)
    return _normalize_phylogenetic_tree_into(data)


# --------------------------------------------------------------------------
# the six canonical keys, defaults
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["str", ["a"], [1], 42, 4.2, True])
def test_a_non_mapping_metadata_is_treated_as_absent(bad):
    """Before the fix the string case raised AttributeError; the list/number
    cases raised the same way, because only FALSY values were filtered."""
    out = _norm(metadata=bad)
    assert out["metadata"] == {
        "title": "", "extraction_timestamp": "", "tree_type": "",
        "scale": "", "rooted": True, "source": "",
    }, out["metadata"]


def test_no_character_indices_appear_as_metadata_keys():
    """The JS failure mode specifically: Object.keys("str") is ["0","1","2"],
    so the string's characters came back as metadata keys. Pin the shape, not
    just the absence of an exception."""
    out = _norm(metadata="str")
    assert set(out["metadata"]) == {
        "title", "extraction_timestamp", "tree_type", "scale",
        "rooted", "source",
    }, out["metadata"]
    assert "0" not in out["metadata"]


# --------------------------------------------------------------------------
# a real mapping is untouched
# --------------------------------------------------------------------------
def test_a_real_mapping_still_merges_and_preserves_unknown_keys():
    out = _norm(metadata={"title": "T", "rooted": False,
                          "taxon_group": "Radiolaria", "total_nodes": 2})
    meta = out["metadata"]
    assert meta["title"] == "T"
    assert meta["rooted"] is False
    assert meta["taxon_group"] == "Radiolaria"
    assert meta["total_nodes"] == 2


def test_an_empty_mapping_is_unchanged():
    assert _norm(metadata={})["metadata"]["title"] == ""


# --------------------------------------------------------------------------
# the sibling field, so the two cannot drift apart again
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["str", ["a"], 42])
def test_legend_behaves_the_same_way(bad):
    """`legend` was already guarded on both engines. It is pinned here so the
    pair stays symmetric rather than one being fixed and forgotten."""
    assert _norm(legend=bad)["legend"] == {}


def test_a_real_legend_still_survives():
    assert _norm(legend={"a": 1})["legend"] == {"a": 1}


# --------------------------------------------------------------------------
# the contract this function keeps everywhere else
# --------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [
    {"root_ids": [], "nodes": [{"id": "r"}]},
    {"root_ids": ["missing"], "nodes": [{"id": "r", "parent": None}]},
    {"root_ids": 42, "nodes": [{"id": "r", "parent": None}]},
    {"root_ids": ["r"], "nodes": [{"id": "r", "parent": None,
                                   "support": 400}]},
])
def test_every_rejection_still_raises_value_error_not_attribute_error(payload):
    """The bug was not that Python raised -- it is that it raised the WRONG
    error, escaping the ValueError contract the rest of the function keeps.
    So the guard against its return is that nothing here raises anything else."""
    with pytest.raises(ValueError):
        _normalize_phylogenetic_tree_into(dict(payload))


def test_metadata_never_raises_at_all():
    """metadata is optional and cosmetic; a bad one must not fail the tree."""
    for bad in ("str", ["a"], 42, 4.2, True, {"x": 1}, None, ""):
        out = _norm(metadata=bad)
        assert isinstance(out["metadata"], dict)
