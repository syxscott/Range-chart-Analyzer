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

SECOND PASS (2026-10-15, audit items 1/2/19): every name-bearing prompt now
embeds ONE shared block, ``rca_core/prompt.py::_literal_reading_rules()`` --
four rules (transcribe the characters, never assemble a label that is not
printed as one string, a name you cannot point at is not there, copy
abbreviations as printed) that used to be ZONATION_CHART's private text while
all the measured transcription drift came out of RANGE_CHART, which carried
none of them.  That touched all eleven constants on the Python side, so all
eleven js/prompt.js blocks were regenerated from the Python constants rather
than hand-edited.  None of them diverged: the generator inserts the block's
element lines and leaves every pre-existing element alone (a whole-block
rewrite would have flattened js/prompt.js's runtime _degradationClause() /
_contractClauses() / _axisPositionClause() calls into typed literals), and the
node+vm comparison below is what proves the two transports still send the same
text.  Which rules each constant carries is pinned by
tests/test_prompt_literal_rules_2026_10_15.py; that file also records why
CHART_CLASSIFY is deliberately NOT a caller of the block.

THIRD PASS (2026-10-16): OCCURRENCE_LABELS_SYSTEM_PROMPT joins the compared
list.  It is the labels-only half of the occurrence_matrix genre, added
because the two halves of that figure have opposite measured error profiles --
big labels 19/22 taxa, 19/19 authorities, 11/11 zones; small grid cells
accuracy 0.43, coverage 0.26.  An earlier attempt to fix the cells by ADDING
a 2966-character rule block to OCCURRENCE_MATRIX_SYSTEM_PROMPT made taxon
recall fall 0.9231 -> 0.6538 and was reverted, so this one REDUCES instead: it
is the same genre asked for less, and the tests below pin the two properties
that make it that way -- it carries the matrix prompt's load-bearing rules
over verbatim, and its schema asks for no field the matrix schema only exists
to carry cell data in.
"""
import importlib.util
import re
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
    # AUDIT-2026-10-16: the labels-only sibling of the prompt above. The two
    # must agree byte for byte exactly like every other pair here, because both
    # engines will eventually be asked the same question of the same figure.
    "OCCURRENCE_LABELS_SYSTEM_PROMPT",
    "GENERIC_TABLE_SYSTEM_PROMPT",
    "PHYLOGENETIC_TREE_SYSTEM_PROMPT",
    "RANGE_CHART_SYSTEM_PROMPT",
    "SCATTER_PLOT_SYSTEM_PROMPT",
    # AUDIT-2026-10-15: ZONATION_CHART moved here from DIVERGENT. It used to
    # differ only in capitalisation ("ZONATION / CORRELATION CHART" vs
    # "zonation / correlation chart"), which is harmless on its own — but this
    # prompt is where the real extraction failure lives, so the JS copy was
    # regenerated wholesale from the Python constant rather than hand-edited
    # again, and the whole text is now byte-identical. Both engines send this
    # prompt; see the literal-reading rules it shares with the other nine
    # name-bearing prompts (see the module docstring).
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


# ---------------------------------------------------------------------------
# OCCURRENCE_LABELS (2026-10-16) — the labels-only sibling prompt.
#
# The point of this constant is that it is SHORTER. An earlier attempt to make
# the matrix prompt read grid cells better went the other way (a +2966
# character rule block) and cost 27 points of taxon recall, so the tests below
# pin the two halves of "ask for less" that are checkable from the text:
# every rule that fixes a MEASURED failure in the matrix prompt survives here
# verbatim, and the schema asks for nothing the matrix schema needs in order
# to carry cell data. Both are derived from the two constants at runtime --
# the field names are read out of each prompt's own JSON skeleton -- so the
# assertions follow the text instead of a transcription of it.
# ---------------------------------------------------------------------------

LABELS = "OCCURRENCE_LABELS_SYSTEM_PROMPT"
MATRIX = "OCCURRENCE_MATRIX_SYSTEM_PROMPT"

#: Fields the labels prompt legitimately defines. Anything else the matrix
#: prompt defines exists to describe a cell, and must not reappear here.
LABEL_FIELDS = frozenset({
    "columns", "rows", "legend",
    "index", "label", "kind", "taxon", "authority", "code", "class",
})

#: One phrase per measured failure that the labels prompt must still carry.
#: Asserted against BOTH constants, which is what makes "carried over" a claim
#: about the matrix prompt instead of a claim about new text: a phrase that is
#: not already in OCCURRENCE_MATRIX_SYSTEM_PROMPT cannot pass.
CARRIED_OVER = (
    # multi-language figures (the verification paper is not an English one)
    "The chart text may be in Chinese, English, Japanese, or Russian.",
    # the taxon-name 'correction' failure: 16 wrong spellings out of 17
    "TRANSCRIBE THE TAXON NAME EXACTLY AS PRINTED, LETTER BY LETTER.",
    "Do NOT 'correct' it, do NOT re-spell it, and do NOT substitute a name "
    "you believe is the standard",
    "These row labels are small italic serif type in a scanned table and "
    "individual letters are genuinely ambiguous",
    # rotated headers get sorted/deduped/reordered by aggregators
    "matrix column order is data \u2014 never sort, dedupe or reorder them",
    # the A/R/C alphabet trap: alphabetical order == abundance order
    "the alphabetical order of the letters coincides with their abundance "
    "order, so any code that sorts codes will be silently right on that "
    "figure and silently wrong on the next one",
    # sparsity polarity: an invented cell asserts a presence that is absent
    "An empty cell means the taxon is ABSENT at that locality.",
    # the conodont-zone strip is read correctly only while it is not a genus
    "A STRATIGRAPHIC COMPARISON BAND IS NOT A TAXON.",
    "A strip such as \"Conodont level (see text)\", a zone strip, or a "
    "numerical bed-scale row is not a genus.",
    # and the two that keep a rotated header from being tidied up
    "THE ROTATED HEADERS ARE LOCALITIES, NOT TAXA.",
    "TRANSCRIBE COLUMN LABELS CHARACTER BY CHARACTER.",
)


def _schema_block(text: str) -> str:
    """The prompt's JSON skeleton: the lone '{' line through the lone '}'.

    Parsed by shape rather than by a marker phrase, so re-wording the sentence
    above the schema cannot silently turn these assertions into no-ops.
    """
    lines = text.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln == "{")
    end = next(i for i, ln in enumerate(lines[start:], start) if ln == "}")
    return "\n".join(lines[start:end + 1])


def _array_body(block: str, container: str) -> str:
    head = '  "%s": [' % container
    start = block.index(head) + len(head)
    return block[start:block.index("\n  ]", start)]


def _array_containers(block: str) -> list:
    """Top-level keys whose value really is a multi-line array.

    `confidence` is a scalar and `strat_rows` is written inline as `[]`, so
    neither is walked into: both would raise inside _array_body rather than
    contributing fields.
    """
    return re.findall(r'^  "(\w+)": \[(?!\])\s*$', block, re.M)


def _top_level_keys(block: str) -> set:
    return set(re.findall(r'^  "(\w+)":', block, re.M))


def _object_keys(arr: str) -> set:
    return set(re.findall(r'"(\w+)":', arr))


def _rule_bullets(text: str) -> list:
    rules = text.split("\nRules:\n", 1)[1]
    return [ln[2:] for ln in rules.split("\n") if ln.startswith("- ")]


def _matrix_schema_fields() -> set:
    """Every field name the matrix prompt's own schema defines."""
    block = _schema_block(getattr(P, MATRIX))
    fields = set(_top_level_keys(block))
    for container in _array_containers(block):
        fields |= _object_keys(_array_body(block, container))
    return fields


class TestLabelsPromptIsLessThanTheMatrixPrompt:
    def test_the_carry_over_list_is_load_bearing(self):
        """A truncated phrase list would make the assertions below vacuous."""
        assert len(CARRIED_OVER) >= 6
        assert all(len(p) >= 40 for p in CARRIED_OVER), (
            "a 'carried over' phrase got too short to be evidence of carry-over")

    def test_every_measured_rule_survives_in_both_prompts(self):
        labels, matrix = getattr(P, LABELS), getattr(P, MATRIX)
        missing = [p for p in CARRIED_OVER if p not in labels or p not in matrix]
        assert not missing, (
            "the labels prompt must carry these over from the matrix prompt; "
            "not present in both: %r" % (missing,))

    def test_it_is_shorter(self):
        labels, matrix = getattr(P, LABELS), getattr(P, MATRIX)
        assert len(labels) < len(matrix), (
            "the labels prompt is %d chars against the matrix prompt's %d; "
            "asking for less has to SHOW UP as fewer tokens, or the split "
            "only relabels the problem" % (len(labels), len(matrix)))


class TestLabelsPromptAsksForNoCells:
    """The whole reason this constant exists: the cells are someone else's job."""

    def test_the_schema_declares_only_the_three_label_arrays(self):
        keys = _top_level_keys(_schema_block(getattr(P, LABELS)))
        assert keys == {"columns", "rows", "legend"}, (
            "expected exactly the label arrays; got %r" % sorted(keys))

    def test_each_object_carries_the_fields_the_contract_promises(self):
        block = _schema_block(getattr(P, LABELS))
        assert _object_keys(_array_body(block, "columns")) == {
            "index", "label", "kind"}
        assert _object_keys(_array_body(block, "rows")) == {
            "index", "taxon", "authority"}
        assert _object_keys(_array_body(block, "legend")) == {
            "code", "class"}

    def test_no_field_the_matrix_prompt_added_for_cells_leaks_in(self):
        """Derived from the matrix prompt's own schema, not from a hand list."""
        matrix_fields = _matrix_schema_fields()
        # Premise of the test: the matrix prompt really does carry cell data.
        assert {"occurrences", "site", "code"} <= matrix_fields, sorted(
            matrix_fields)

        labels = getattr(P, LABELS)
        cell_only = matrix_fields - set(LABEL_FIELDS)
        leaked = sorted(f for f in cell_only if f in labels)
        assert not leaked, (
            "the labels prompt names %r, which exists in the matrix schema "
            "only to describe a grid cell" % (leaked,))

    def test_code_and_class_stay_inside_the_legend(self):
        """`code` / `class` are legal in `legend` and nowhere else.

        The name-only check above cannot see this: `code` IS one of the fields
        the labels prompt legitimately defines, so a `code` smuggled onto a row
        record slips through it. This is the positional form of the same rule.
        """
        block = _schema_block(getattr(P, LABELS))
        without_legend = block.replace(_array_body(block, "legend"), "")
        assert "code" not in without_legend, (
            "a columns[]/rows[] record carries `code`, which only the legend "
            "may: it is a cell value")
        assert "class" not in without_legend, (
            "a columns[]/rows[] record carries `class`, which only the legend "
            "may: it is a cell value")

    def test_one_rule_disclaims_the_grid_cells_and_the_rest_is_silent(self):
        labels = getattr(P, LABELS)
        cell_rules = [b for b in _rule_bullets(labels) if "cell" in b.lower()]
        assert cell_rules, (
            "no rule mentions the cells, so the model is left to volunteer "
            "them -- the one instruction this prompt has to add")
        assert any(("NOT" in b or "NEVER" in b) for b in cell_rules), (
            "the cell rule must forbid reading them, not merely mention them")
