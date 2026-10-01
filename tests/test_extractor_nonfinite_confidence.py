"""A NaN confidence must never be reported as certainty (AUDIT-2026-09-30).

This is the third and largest site of one defect class. Python's min()/max()
do not propagate NaN -- ``min(1.0, nan)`` is 1.0, because ``nan < 1.0`` is
False -- so ``max(0.0, min(1.0, parsed))`` silently turned an unstateable
confidence into a PERFECT 1.0. An AST enumeration (not a grep) found **13**
such clamps spread across all 10 normalizers in rca_core/extractor.py.

Meanwhile js/minimax.js already routed every root confidence through ONE
shared function, ``rcaConfidenceClamped``, which guards
``Number.isNaN(n) -> return 0.0``; the per-row mirror
``rcaOptionalConfidence`` ends with ``-> return null``. So the same model
output scored 1.0 on the desktop and 0 in the browser, and three parity cases
sat parked in tests_diff_frontend_parity.js as "documented divergences" for
that reason.

The fix is structural, not 13 patches: rca_core.extractor now has
``_confidence_clamped`` as the single gate, and the last test below uses the
same AST technique that found the bug, so a 14th hand-copied clamp fails the
build rather than reintroducing the drift.
"""

from __future__ import annotations

import ast
import math
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import extractor as E  # noqa: E402

NAN = float("nan")
INF = float("inf")

EXTRACTOR = os.path.join(PROJECT_ROOT, "rca_core", "extractor.py")


# --------------------------------------------------------------------------
# the gate itself
# --------------------------------------------------------------------------
def test_a_nan_root_confidence_is_zero_not_one():
    assert E._confidence_clamped(NAN) == 0.0
    assert E._confidence_clamped(-NAN) == 0.0


def test_out_of_range_still_clamps_to_the_nearest_bound():
    """Pinned by tests/test_auto_classify.py and
    tests/test_phylogenetic_tree.py::test_confidence_clamped -- the NaN guard
    must not have disturbed the ordinary clamp."""
    assert E._confidence_clamped(1.5) == 1.0
    assert E._confidence_clamped(-0.5) == 0.0
    assert E._confidence_clamped(0.0) == 0.0
    assert E._confidence_clamped(1.0) == 1.0
    assert E._confidence_clamped(0.37) == pytest.approx(0.37)


def test_infinities_are_NOT_remapped_to_zero():
    """The subtlety that makes this guard `isnan` and not `isfinite`.

    Measured against the shipped js/minimax.js: rcaConfidenceClamped sends NaN
    to 0.0 but lets +Infinity clamp to 1 and -Infinity to 0, and Python's
    min/max already agreed on those two. Writing `if not math.isfinite(v):
    return 0.0` would have fixed NaN and simultaneously created a brand-new
    divergence on +Infinity. This test is what stops that "simplification"
    from being reintroduced.
    """
    assert E._confidence_clamped(INF) == 1.0
    assert E._confidence_clamped(-INF) == 0.0


def test_the_row_variant_drops_the_field_rather_than_zeroing_it():
    """_normalize_confidence is nullable; js/minimax.js rcaOptionalConfidence
    returns null for a NaN, so the browser OMITS the key."""
    assert E._normalize_confidence(NAN) is None
    assert E._normalize_confidence("NaN") is None
    assert E._normalize_confidence(1.5) == 1.0
    assert E._normalize_confidence(0.5) == 0.5
    assert E._normalize_confidence(None) is None
    assert E._normalize_confidence("") is None
    assert E._normalize_confidence(True) is None
    assert E._normalize_confidence("90%") is None


# --------------------------------------------------------------------------
# end to end, through the real normalizers
# --------------------------------------------------------------------------
def test_every_normalizer_reports_a_nan_confidence_as_zero():
    """The AST count said 13 sites in 10 functions. This is the behavioural
    counterpart: if anyone adds a 14th normalizer, it is covered here too."""
    nan = "NaN"
    results = {
        "range_chart": E.normalize_result(
            {"sections": [], "confidence": nan}),
        "columnar": E.normalize_columnar_result(
            {"sections": [], "confidence": nan}),
        "abundance": E.normalize_abundance_result(
            {"abundances": [], "confidence": nan}),
        "phylogenetic_tree": E._normalize_phylogenetic_tree_into({
            "nodes": [{"id": "n1", "parent": None, "name": "root"}],
            "root_ids": ["n1"], "confidence": nan}),
        "chemical_stratigraphy": E.normalize_chemical_stratigraphy_result(
            {"intervals": [], "confidence": nan}),
        "paleomap": E.normalize_paleomap_result(
            {"paleomap_points": [], "confidence": nan}),
        "scatter_plot": E.normalize_scatter_plot_result(
            {"points": [], "confidence": nan}),
        "zonation_chart": E.normalize_zonation_chart_result(
            {"zones": [], "confidence": nan}),
        "chart_classification": E.normalize_chart_classification(
            {"chart_type": "range_chart", "confidence": nan, "reason": "r"}),
    }
    assert set(results) == set(_NORMALIZERS), "a normalizer is missing here"
    for mode, out in results.items():
        conf = out.get("confidence")
        assert conf == 0.0, f"{mode} reported {conf!r} for a NaN confidence"
        assert not (isinstance(conf, float) and math.isnan(conf)), mode


def test_a_nan_row_confidence_is_absent_from_the_species_row():
    out = E.normalize_result({
        "sections": [], "confidence": 1,
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9", "confidence": "NaN"}],
    })
    row = out["species_ranges"][0]
    assert row.get("confidence") is None, row
    # and the ROOT confidence, a real 1, is untouched by the bad row
    assert out["confidence"] == 1.0


def test_infinity_rows_agree_with_the_browser_too():
    """+Infinity clamps to 1 and -Infinity to 0 on both engines, so these must
    NOT have been swept up by the NaN fix."""
    assert E._normalize_confidence(INF) == 1.0
    assert E._normalize_confidence(-INF) == 0.0
    out = E.normalize_result({"sections": [], "confidence": "Infinity"})
    assert out["confidence"] == 1.0


# --------------------------------------------------------------------------
# scope guard: exactly one clamp gate, no 14th hand copy
# --------------------------------------------------------------------------
_NORMALIZERS = {
    "range_chart", "columnar", "abundance", "phylogenetic_tree",
    "chemical_stratigraphy", "paleomap", "scatter_plot", "zonation_chart",
    "chart_classification",
}


def _clamp_sites():
    """Every max(0.0, min(1.0, x)) / min(1.0, max(0.0, x)) in extractor.py."""
    tree = ast.parse(open(EXTRACTOR, encoding="utf-8").read())
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("min", "max"):
            continue
        src = ast.unparse(node)
        if "1.0" in src and "0.0" in src:
            hits.append((node.lineno, src))
    return sorted(hits)


def _confidence_clamped_span():
    """(first_line, last_line) of ``_confidence_clamped``'s own body.

    AUDIT-2026-10-02: this assertion used to be a hard-coded line WINDOW
    (``1140 <= lineno <= 1180``) as a proxy for "inside _confidence_clamped".
    The proxy is what broke -- not the invariant. Adding two explanatory
    comment blocks ABOVE that function (the iron-rule boundary rewrite and the
    zone_type ladder one, both in the same Unicode-vs-ASCII family) pushed the
    clamp from 1171 to 1189 and the window failed with "the single clamp moved
    to line 1189; it belongs inside _confidence_clamped" while it plainly did.

    A line window encodes an accident of the current file layout, and any
    edit above the function invalidates it without changing anything it was
    meant to protect. The function's own AST span says the same thing and
    cannot go stale that way. The decision is unchanged and still asserted:
    the ONE clamp must live inside _confidence_clamped.
    """
    tree = ast.parse(open(EXTRACTOR, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_confidence_clamped":
            return node.lineno, (node.end_lineno or node.lineno)
    raise AssertionError("_confidence_clamped not found in extractor.py")


def test_there_is_exactly_one_confidence_clamp_in_the_module():
    """Anti-recurrence guard, and it deliberately uses the same AST technique
    that found the 13.

    The count is asserted, not just the presence: the bug was 13 separate
    copies, so "some clamp exists" would pass forever while a new unguarded
    one was added. Exactly one site may contain the clamp expression, and it
    must be inside _confidence_clamped -- the single gate the JS mirror has.
    """
    sites = _clamp_sites()
    assert len(sites) == 1, (
        "extractor.py must hold exactly ONE confidence clamp -- "
        f"_confidence_clamped. Found {len(sites)}: "
        + "; ".join(f"line {ln}: {s}" for ln, s in sites)
        + ". A new hand-copied max(0.0, min(1.0, ...)) reintroduces the NaN "
          "drift that shipped 13 of them."
    )
    lineno, _src = sites[0]
    first, last = _confidence_clamped_span()
    assert first <= lineno <= last, (
        f"the single clamp is at line {lineno}, outside "
        f"_confidence_clamped (lines {first}-{last})"
    )


def test_the_single_gate_is_actually_guarded():
    """Counting the clamps is not enough -- the one that remains must be the
    NaN-guarded one, or the count would still be 1 and the bug would be back.

    Checked over the AST, not the raw text: the docstring deliberately spells
    out `math.isfinite` while explaining why the guard must NOT be one, so a
    substring search would trip over its own warning.
    """
    func = _find_func("_confidence_clamped")
    code_calls = {
        f"{n.func.value.id}.{n.func.attr}"
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
    }
    assert "value != value" in ast.unparse(func), (
        "_confidence_clamped lost its NaN guard; it must test `value != value`"
    )
    assert "math.isfinite" not in code_calls, (
        "the guard must stay `isnan`, not `isfinite`: +Infinity clamps to 1.0 "
        "on BOTH engines and turning it into 0.0 would create a new divergence"
    )


def _find_func(name):
    tree = ast.parse(open(EXTRACTOR, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in extractor.py")
