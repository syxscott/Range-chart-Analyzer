"""Every prompt constant compared across rca_core/prompt.py and js/prompt.js.

AUDIT-2026-10-01. The existing prompt parity guard
(tests/test_prompt_fixes.py::test_byte_identical_python_vs_js) hard-asserts the
generated contract block in four modes, but:

  * for RANGE_CHART and ABUNDANCE_DIAGRAM the FULL-prompt comparison goes
    through check(), which under pytest only PRINTS -- so a one-character drift
    anywhere else in the two most important prompts leaves CI green. (That same
    function was also comparing against a raw-source scraper that could not see
    runtime-generated clauses at all; fixed separately in the same audit.)
  * its specs list holds FOUR modes, so COLUMNAR_SECTION, ZONATION_CHART,
    PALEOMAP and CHART_CLASSIFY had NO full-text comparison whatsoever.

Measured, with the real evaluator (node + vm, not a source scraper): six of the
nine constants are byte-identical and three are not, and the three that differ
are exactly the ones the old list could not have caught.

  COLUMNAR_SECTION      py 3695 / js 3676 -- the JS copy dropped the quotes:
                        "(e.g. J, T)" vs "(e.g. 'J', 'T')", and
                        "column (section)" vs "column ('section')"
  ZONATION_CHART        py 5325 / js 5335 -- "a BIOSTRATIGRAPHIC ZONATION /
                        CORRELATION CHART" vs the same phrase in lower case
  PALEOMAP              py 5335 / js 5347 -- "the site's note" vs "the note
                        field of that site", which names a concrete field

Reachability, which splits the three:

  * columnar_section and zonation_chart are sent by BOTH engines
    (rca_core/extractor.py:2643 and :4466, js/minimax.js:3572 and :3578), so
    those two differences are live: the desktop and the browser ask the model
    slightly different things for the same figure.
  * paleomap, chemical_stratigraphy and scatter_plot are BACKEND-ONLY. js/
    minimax.js says so in its own comment ("The browser has no normalizer for
    those three"), so the PALEOMAP difference is inert today.

The wording is deliberately NOT "fixed" here. These are prompts: changing one
changes what the model is told and therefore what comes back, so which text is
intended is a content decision, not a mirror bug. What is pinned is the current
state exactly -- each variant asserted verbatim, so any edit on either side is
visible in a review instead of drifting silently. The live two are reported for
that decision.

NEW (2026-10-15): the two modes added alongside rca_core/modes.py are listed as
IDENTICAL because js/prompt.js's copies are GENERATED from these two python
constants (single-quoted, line-per-element arrays, escaped with json.dumps)
rather than hand-transcribed -- which is the defect this file exists to catch.
CHART_CLASSIFY_SYSTEM_PROMPT is in the same boat: the Python side gained an
``occurrence_matrix`` entry and js/prompt.js was regenerated from it, so the
pair is still byte-identical.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import rca_core.prompt as P  # noqa: E402

# The evaluator the existing guard already uses: node + vm, because the contract
# / axis / degradation clauses are FUNCTION CALLS inside the array literal and a
# source scraper cannot see them.
_spec = importlib.util.spec_from_file_location(
    "prompt_fixes_probe", REPO / "tests" / "test_prompt_fixes.py")
_pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pf)

# Must stay byte-identical: measured identical on 2026-10-01.
IDENTICAL = (
    "ABUNDANCE_DIAGRAM_SYSTEM_PROMPT",
    "CHART_CLASSIFY_SYSTEM_PROMPT",
    "CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT",
    "OCCURRENCE_MATRIX_SYSTEM_PROMPT",
    "GENERIC_TABLE_SYSTEM_PROMPT",
    "PHYLOGENETIC_TREE_SYSTEM_PROMPT",
    "RANGE_CHART_SYSTEM_PROMPT",
    "SCATTER_PLOT_SYSTEM_PROMPT",
    # AUDIT-2026-10-15: ZONATION_CHART moved here from DIVERGENT. It used to
    # differ only in capitalisation ("ZONATION / CORRELATION CHART" vs
    # "zonation / correlation chart"), which is harmless on its own — but this
    # prompt is where the real extraction failure lives, so the JS copy was
    # regenerated wholesale from the Python constant rather than hand-edited
    # again, and the whole text (6333 chars) is now byte-identical. Both
    # engines send this prompt; see the rules added about never emitting a zone
    # the figure does not draw.
    "ZONATION_CHART_SYSTEM_PROMPT",
)

# Measured different, pinned verbatim on both sides. (name, live?, python, js)
DIVERGENT = (
    ("COLUMNAR_SECTION_SYSTEM_PROMPT", True,
     "Each vertical column ('section') in the figure",
     "Each vertical column (section) in the figure"),
    ("PALEOMAP_SYSTEM_PROMPT", False,   # backend-only: js/minimax.js has no
                                       # normalizer for paleomap
     "put its value into the site's note",
     "put its value into the note field of that site"),
)

ALL = IDENTICAL + tuple(d[0] for d in DIVERGENT)


@pytest.fixture(scope="module")
def js_texts():
    got = _pf._eval_js_prompt_consts(list(ALL))
    if isinstance(got, list):
        got = dict(zip(ALL, got))
    # Environment self-check: the evaluator must have returned real text, or every
    # comparison below would be vacuously true.
    for name in ALL:
        assert isinstance(got.get(name), str) and len(got[name]) > 500, (
            f"the evaluator returned nothing usable for {name}: {got.get(name)!r}")
    return got


class TestEveryConstantIsCompared:
    def test_no_prompt_constant_is_left_uncompared(self):
        """A new constant must join one of the two lists, or it is unchecked."""
        module_consts = {n for n in dir(P)
                         if n.endswith("_SYSTEM_PROMPT")
                         and isinstance(getattr(P, n), str)}
        listed = set(IDENTICAL) | {d[0] for d in DIVERGENT}
        assert module_consts == listed, (
            f"uncompared prompt constants: {sorted(module_consts - listed)}; "
            f"listed but gone: {sorted(listed - module_consts)}")

    def test_the_js_evaluator_reaches_every_constant(self, js_texts):
        for name in ALL:
            assert name in js_texts and js_texts[name], name


class TestIdenticalConstants:
    @pytest.mark.parametrize("name", IDENTICAL)
    def test_byte_identical(self, name, js_texts):
        py = getattr(P, name)
        js = js_texts[name]
        assert py == js, (
            f"{name} drifted: py len={len(py)} js len={len(js)}\n"
            f"first difference at {_first_diff(py, js)}")


class TestKnownDivergences:
    """Pinned, not fixed: the wording is a content decision."""

    @pytest.mark.parametrize("name,live,py_snippet,js_snippet", DIVERGENT)
    def test_pinned_both_sides(self, name, live, py_snippet, js_snippet, js_texts):
        py, js = getattr(P, name), js_texts[name]
        assert py_snippet in py, f"python side no longer contains {py_snippet!r}"
        assert js_snippet in js, f"js side no longer contains {js_snippet!r}"
        assert py != js, (
            f"{name} now matches on both sides -- the divergence is resolved, so "
            "move it to IDENTICAL and note the resolution here")
        # The other side's wording must NOT also be present, or the pin is
        # describing a pair that both carry.
        assert js_snippet not in py, f"{name}: the js snippet also appears in python"
        assert py_snippet not in js, f"{name}: the python snippet also appears in js"

    def test_live_divergences_are_exactly_the_ones_the_browser_dispatches(self):
        """`live` must mean "textually divergent AND sent by js/minimax.js".

        Derived from js/minimax.js rather than hardcoded, because AUDIT-2026-10-15
        moved ZONATION_CHART_SYSTEM_PROMPT from DIVERGENT to IDENTICAL and a
        literal set broke on the move instead of on a real regression. Deriving it
        also widens the check: this used to assert only the forward direction (a
        live prompt is dispatched), so marking a prompt live that the browser
        never sends -- the stale-pin failure -- passed silently.
        """
        live = {name for name, is_live, _, _ in DIVERGENT if is_live}
        js_src = (REPO / "js" / "minimax.js").read_text(encoding="utf-8")
        sent = {n for n, _, _, _ in DIVERGENT if n in js_src}

        assert live == sent, (
            f"live/sent disagreement. marked live but not dispatched: "
            f"{sorted(live - sent)}; dispatched but not marked live: "
            f"{sorted(sent - live)}. Update the DIVERGENT live flag or the "
            "browser dispatch, whichever actually changed.")

        # Not vacuous: the classification must still mark something live, or the
        # comparison above would hold for the wrong reason (e.g. every DIVERGENT
        # entry was flipped to False and js/minimax.js names none of them).
        assert live, "no divergence is marked live; check js/minimax.js still ships one"

    def test_paleomap_is_still_backend_only(self):
        """The one inert divergence depends on the browser lacking a normalizer."""
        js_src = (REPO / "js" / "minimax.js").read_text(encoding="utf-8")
        assert "no normalizer for those three" in js_src, (
            "js/minimax.js no longer says the browser lacks normalizers for "
            "paleomap / chemical_stratigraphy / scatter_plot; if that changed, "
            "PALEOMAP_SYSTEM_PROMPT's divergence became live and this pin's "
            "reachability note is stale")


def _first_diff(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i
