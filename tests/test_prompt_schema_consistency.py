"""AUDIT-2026-10-01 [item 31]: the tree prompt told the model to write two
fields its own schema does not have.

``_degradation_clause()`` is ONE helper appended to all eight mode prompts
(range chart, columnar, abundance, phylogenetic tree, chemical strat, paleomap,
scatter, zonation).  Its text tells the model to "lower the per-row
`confidence` field ... AND set the per-row `note` field".

Seven of those eight modes have per-row ``confidence``/``note`` fields.  A
phylogenetic tree node has NEITHER: the node schema in
``PHYLOGENETIC_TREE_SYSTEM_PROMPT`` defines exactly

    id, name, support, support_confidence, branch_length,
    depth_range_m, depth_confidence, sequence_count, is_leaf, parent

and nothing else for uncertainty.  The clause was copied into the tree prompt
without adaptation, so the model is told to write two fields the schema does not
define.  Either it injects out-of-schema keys into node objects -- which the
normaliser sweeps into ``node.metadata`` and the exporter never reads -- or it
ignores the clause, and either way the uncertainty has nowhere to go.  That
undercuts the very guarantee the clause exists to make.

This is the second CONFIRMED finding in docs/FRONTEND-REVIEW-2026-08-19.json
(verdict CONFIRMED, MEDIUM), filed 2026-08-19 and never fixed.  Like item 30 it
is a prompt/pipeline mismatch, but the fix here is prompt TEXT only -- no
schema, no export column, no data-file change -- so it is not a product
decision.

The tests derive the node schema from the prompt itself rather than restating
it, and they guard the other seven prompts so this fix cannot quietly remove
the generic clause where it IS correct.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from rca_core import prompt as P

REPO = Path(__file__).resolve().parent.parent
JS_PROMPT = REPO / "js" / "prompt.js"

#: The seven modes where a per-row confidence/note field really does exist.
ROW_TABLE_PROMPTS = (
    "RANGE_CHART_SYSTEM_PROMPT",
    "COLUMNAR_SECTION_SYSTEM_PROMPT",
    "ABUNDANCE_DIAGRAM_SYSTEM_PROMPT",
    "CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT",
    "PALEOMAP_SYSTEM_PROMPT",
    "SCATTER_PLOT_SYSTEM_PROMPT",
    "ZONATION_CHART_SYSTEM_PROMPT",
)


def _node_schema_fields(tree_prompt: str) -> set[str]:
    """The node object's own field names, read out of the prompt's schema."""
    block = tree_prompt.split('"nodes": [', 1)
    assert len(block) == 2, "the tree prompt no longer has a nodes block"
    nodes = block[1].split("],", 1)[0]
    return set(re.findall(r'"([a-z_]+)":', nodes))


def test_the_tree_node_schema_really_has_no_confidence_or_note():
    fields = _node_schema_fields(P.PHYLOGENETIC_TREE_SYSTEM_PROMPT)
    # The premise of the whole finding, asserted from the artifact itself.
    assert "confidence" not in fields, (
        "the tree node schema grew a `confidence` field; the generic clause "
        "may then be correct again -- re-read this file.  fields=%r"
        % sorted(fields))
    assert "note" not in fields, (
        "the tree node schema grew a `note` field; the generic clause may "
        "then be correct again -- re-read this file.  fields=%r"
        % sorted(fields))
    # ...and it does define the two fields the tree clause must point at.
    assert {"support_confidence", "depth_confidence"} <= fields


def test_the_tree_prompt_no_longer_asks_for_those_fields():
    text = P.PHYLOGENETIC_TREE_SYSTEM_PROMPT
    assert "per-row `confidence`" not in text, (
        "the tree prompt still instructs the model to lower a per-row "
        "`confidence` field its node schema does not define")
    assert "per-row `note`" not in text, (
        "the tree prompt still instructs the model to set a per-row `note` "
        "field its node schema does not define")
    assert P._degradation_clause() not in text, (
        "the generic clause is back in the tree prompt")


def test_the_tree_clause_points_at_fields_the_schema_defines():
    clause = P._phylogenetic_degradation_clause()
    assert clause in P.PHYLOGENETIC_TREE_SYSTEM_PROMPT
    fields = _node_schema_fields(P.PHYLOGENETIC_TREE_SYSTEM_PROMPT)
    named = set(re.findall(r"`([a-z_]+)`", clause))
    invented = {n for n in named
                if n not in fields and n not in ("is_leaf", "parent")}
    assert not invented, (
        "the tree clause names fields the node schema does not define: %r"
        % sorted(invented))
    assert {"support_confidence", "depth_confidence"} <= named, (
        "the tree clause must degrade through the confidence fields the "
        "schema defines; named=%r" % sorted(named))


@pytest.mark.parametrize("attr", ROW_TABLE_PROMPTS)
def test_the_other_seven_prompts_keep_the_generic_clause(attr):
    """Regression guard: the fix must not leak into the row-table modes."""
    text = getattr(P, attr)
    assert P._degradation_clause() in text, (
        "%s lost the generic degradation clause -- it IS correct there "
        "(those rows have confidence/note fields)" % attr)


def test_the_js_mirror_agrees():
    """The browser copy must not reintroduce the generic clause for the tree."""
    src = JS_PROMPT.read_text(encoding="utf-8")
    start = src.index("const PHYLOGENETIC_TREE_SYSTEM_PROMPT = [")
    end = src.index("].join('\\n');", start)
    phylo = src[start:end]
    assert "_degradationClause()" not in phylo, (
        "js/prompt.js still appends the generic clause to the tree prompt")
    assert "_phylogeneticDegradationClause()" in phylo, (
        "js/prompt.js does not use the tree-specific clause")
    # The other seven must still call the shared helper.
    assert src.count("_degradationClause()") >= 7, (
        "js/prompt.js lost generic-clause call sites: %d left"
        % src.count("_degradationClause()"))
    # Both sides must ship the SAME clause text, or the two transports differ.
    py_clause = P._phylogenetic_degradation_clause()
    assert py_clause in src, (
        "the JS tree clause text differs from rca_core/prompt.py's -- the two "
        "transports must instruct the model identically")


def test_the_prompt_version_bump_reached_both_sides():
    """A changed prompt must invalidate its own cache on BOTH transports.

    tests/test_prompt_fixes.py::test_prompt_version_both_sides checks this
    with ``check()``, which PRINTS and does not fail under pytest (its own
    docstring records that gap), so for the tree mode a one-sided bump would
    not fail the suite.  The cache key carries the version, so a stale v1
    answer -- one that can contain the out-of-schema node keys the old clause
    invited -- would keep being served under the new prompt's key.

    The expected table is read from Python, and the JS literal is parsed out of
    the source; the assertion is that they agree, not that a particular string
    is spelled a certain way.
    """
    src = JS_PROMPT.read_text(encoding="utf-8")
    literal = re.search(r"const\s+PROMPT_VERSION\s*=\s*\{([^}]+)\}", src, re.S)
    assert literal, "js/prompt.js no longer has a PROMPT_VERSION object literal"
    js_versions = dict(re.findall(r"(\w+)\s*:\s*'([^']*)'", literal.group(1)))
    assert js_versions, "the JS PROMPT_VERSION literal parsed to nothing"
    assert js_versions == P.PROMPT_VERSION, (
        "PROMPT_VERSION drift between the transports:\n  py=%r\n  js=%r"
        % (P.PROMPT_VERSION, js_versions))
    # The tree prompt's text changed in this commit, so its version must have
    # moved off v1 -- pinned literally because that is the specific regression
    # this guards (an unbumped tree version keeps serving pre-fix answers).
    assert P.PROMPT_VERSION["phylogenetic_tree"] == "v2", (
        "the tree clause changed but phylogenetic_tree is still %r; a cached "
        "v1 answer would be served under the new prompt"
        % P.PROMPT_VERSION["phylogenetic_tree"])
