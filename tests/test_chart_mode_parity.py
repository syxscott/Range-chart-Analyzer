"""Keyword-table parity lock between the three auto-detect implementations.

rca_core/chart_mode.py (both GUIs), js/app.js rcaAutoDetectChartModeDetailed
(web), and the duplicated arrays inside it must agree. The tables are declared
in two languages, so this test reads BOTH sides and asserts the ASCII/CJK
keyword sets match per category -- a one-sided keyword addition fails loudly
where it used to drift silently (REVIEW-2026-11-07 phylo case).

WHY THE PYTHON SIDE IS EVALUATED, NOT SCRAPED (2026-10-15)
===========================================================
This file used to read ``_ZON_ASCII`` / ``_STEMS`` out of
``rca_core/chart_mode.py``'s SOURCE TEXT with a regex, because the tables were
hand-written tuples in that file. They no longer are: chart_mode.py derives
every one of them from ``rca_core/modes.py``, so the literals are gone and the
scraper asserted nothing at all -- it failed with "python _ZON_ASCII not
found" on all four keyword tests.

Scraping source text was the weaker contract anyway: it pins SPELLING, not
BEHAVIOUR, and it went silent (rather than red) the moment the source stopped
containing the literals. So the Python side is now read from the imported
module -- ``rca_core.chart_mode._ZON_ASCII``, ``_STEMS``, and the
``rca_core.modes`` ModeSpec tables -- and compared against the js/app.js
literals. The JS side is still read from source, because those arrays are
function-local consts inside two closures and nothing exports them; they are
plain literal arrays with no computed element, so a literal read is exact.

Two extra locks fall out of the registry now owning the tables:

* ``test_the_cascade_reads_the_registry_tables`` -- chart_mode.py's cascade
  must consume the SAME tuples the registry declares. Without it the registry
  could be edited and the detector would keep matching the old words.
* ``TestOccurrenceMatrix`` -- the 2026-10-15 mode, whose "localit" keyword is
  a STEM and therefore the one keyword in the table that can be silently dead.

REVIEW-2026-09-20 #105 CLOSED: the web mirror has landed. The ZON narrowing
("correlation of" -> "correlation of zones"), the split-form phrase regex and
the RANGE / CHEM / PALEO / SCAT tables now exist on BOTH sides, so the
one-sided exception lists are empty and every category is a strict equality
lock again. They are kept (as empty sets) rather than deleted so a future
"temporary" divergence has to be written down here, in the open, and makes
``test_pending_js_mirror_entries_are_still_pending`` fail if the entry is a
keyword that exists on neither side.

WHAT THIS FILE DOES NOT COVER, and why not -- recorded so nobody rebuilds it
(AUDIT-2026-10-02). This locks the TABLES, the stem list, the branch order and
the phrase regex. It does NOT compare the matching FUNCTION's behaviour
side by side: ``chart_mode.py:_match_kw`` against
``app.js:rcaChartModeMatchKw``, both two-branch (stem -> leading boundary,
otherwise -> whole word, CJK -> plain substring).

Closing that would mean adding the caption classifier to the differential
harness (tests_diff_frontend_parity.js), and the harness cannot load
js/app.js. Measured, not assumed: with the harness's own script list plus
js/app.js, the load fails twice in a row, each failure exposing a new
assumption about the environment rather than a new bug --

    1. with the harness's document stub:  "rcaSetLang is not defined"
       (js/app.js needs js/i18n.js, which the harness does not load)
    2. with js/i18n.js added and getElementById returning a permissive
       Proxy:  "Cannot set properties of null (setting 'value')", then
       after widening that too, "Cannot read properties of undefined
       (reading 'trim')" -- i.e. app.js reaches for real config/DOM state at
       module scope, and a fake DOM invented to load it could make the
       classifier behave differently from the product.

Both functions are pure string functions, so a fake DOM would be built purely
to host them -- the wrong trade, and exactly the "hand-rolled harness inherits
the shape but not the discipline" trap. The behaviour that the cross-engine
agreement actually rests on is pinned one-sidedly instead: the ``re.ASCII``
boundary semantics that make Python's ``\\b`` mean what JavaScript's means are
tested in tests/test_audit_2026_09_27.py against the exact captions from
_match_kw's own docstring ("pollen丰度图", "biplot图", "scatter plot图",
"pollenГрафик"), together with the reverse control that ``re.ASCII`` must not
weaken the longer-word guard. That fix was driven by a measurement over 22
real captions, 3 of which diverged.
"""

import inspect
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import rca_core.chart_mode as CM  # noqa: E402
import rca_core.modes as MODES  # noqa: E402


def _read(name: str) -> str:
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


def _js_string_array(body: str) -> set:
    """Single-quoted JS string literals, escapes intact."""
    return set(re.findall(r"'((?:[^'\\]|\\.)*)'", body))


def _js_array(src: str, var: str) -> set:
    m = re.search(rf"const {var} = \[(.*?)\];", src, re.DOTALL)
    assert m, f"js {var} not found"
    return _js_string_array(m.group(1))


# Each group: the js/app.js array prefix, and the registry mode whose
# ModeSpec holds the keywords chart_mode.py derives from.
GROUPS = ("ZON", "AB", "COL", "PHYLO", "RANGE", "CHEM", "PALEO", "SCAT", "OCC")

MODE_OF = {
    "ZON": "zonation_chart",
    "AB": "abundance_diagram",
    "COL": "columnar_section",
    "PHYLO": "phylogenetic_tree",
    "RANGE": "range_chart",
    "CHEM": "chemical_stratigraphy",
    "PALEO": "paleomap",
    "SCAT": "scatter_plot",
    "OCC": "occurrence_matrix",
}

# The groups chart_mode.py still names as module constants. occurrence_matrix
# is absent on purpose: that branch reads the tables inline through _kw(...),
# so there is no _OCC_* constant to compare -- the registry is the only copy.
CASCADE_ATTRS = ("ZON", "AB", "COL", "PHYLO", "RANGE", "CHEM", "PALEO", "SCAT")


def _registry_table(group: str) -> tuple:
    spec = MODES.MODES[MODE_OF[group]]
    return set(spec.keywords_ascii), set(spec.keywords_cjk)


def _py_table(group: str) -> tuple:
    """The table the DETECTOR uses, read from the imported module."""
    if group in CASCADE_ATTRS:
        return (set(getattr(CM, f"_{group}_ASCII")),
                set(getattr(CM, f"_{group}_CJK")))
    return _registry_table(group)


PENDING_PY_ONLY: dict = {}
PENDING_JS_ONLY: dict = {}


def test_the_cascade_reads_the_registry_tables():
    """chart_mode.py's named constants must BE the registry's tuples.

    The tables moved to rca_core/modes.py in 2026-10-15 and chart_mode.py now
    reads them. If someone edits the registry, the detector follows -- unless a
    future edit re-introduces a local literal, which this catches the first time
    the two sides disagree.
    """
    for group in CASCADE_ATTRS:
        assert (set(getattr(CM, f"_{group}_ASCII"))
                == _registry_table(group)[0]), group
        assert (set(getattr(CM, f"_{group}_CJK"))
                == _registry_table(group)[1]), group


def test_mode_keywords_match_between_python_and_web():
    js = _read(os.path.join("js", "app.js"))
    for group in GROUPS:
        ascii_keys, cjk_keys = _py_table(group)
        assert (ascii_keys - PENDING_PY_ONLY.get(group, set())
                == _js_array(js, f"{group.lower()}KeysAscii")
                - PENDING_JS_ONLY.get(group, set())), group
        assert cjk_keys == _js_array(js, f"{group.lower()}KeysCjk"), group


def test_zon_table_carries_the_narrowed_phrase_only():
    """REVIEW-2026-09-20 #105 item 2: the broad ``correlation of`` keyword
    short-circuited the vision classifier on plain lithostratigraphic
    captions. It must be gone from BOTH engines, and the narrowed zone-
    specific spelling present on both."""
    js = _read(os.path.join("js", "app.js"))
    py_keys = _py_table("ZON")[0]
    for group, keys in (("ZON", py_keys), ("ZON", _js_array(js, "zonKeysAscii"))):
        assert "correlation of zones" in keys, group
        assert "correlation of" not in keys, (
            "the bare 'correlation of' keyword must not come back")
    # The phrase regex (asserted by test_zon_phrase_regex_is_mirrored_in_js),
    # not a keyword, is what keeps "Correlation of Triassic radiolarian ZONES"
    # on the keyword-free path.


def test_pending_js_mirror_entries_are_still_pending():
    """Guard the exception lists above: a pending entry must describe a real
    one-sided difference, never a keyword that exists on NEITHER side (that
    would silently whitelist a key lost from both engines)."""
    js = _read(os.path.join("js", "app.js"))
    for group, pending_py in PENDING_PY_ONLY.items():
        assert pending_py <= _py_table(group)[0], group
        assert not (pending_py & _js_array(js, f"{group.lower()}KeysAscii")), group
    for group, pending_js in PENDING_JS_ONLY.items():
        assert pending_js <= _js_array(js, f"{group.lower()}KeysAscii"), group
        assert not (pending_js & _py_table(group)[0]), group


# The assistant-mode tables, pinned verbatim so a keyword can only be added
# by editing rca_core/modes.py AND js/app.js (the equality tests above cover the
# sets; the literals below cover the case where a whole table disappears from
# one side).
EXPECTED_TABLES = {
    "RANGE": {
        "ascii": {"range chart", "range-chart"},
        "cjk": {"延限", "карта совмещения", "график совмещения"},
    },
    "CHEM": {
        "ascii": {"isotop", "chemostrat", "chemical stratigraphy",
                  "chemical stratigraphic"},
        "cjk": {"同位素", "化学地层", "化学地層", "地球化学",
                "изотоп", "геохим"},
    },
    "PALEO": {
        "ascii": {"paleomap", "palaeomap", "paleogeograph",
                  "palaeogeograph", "paleocontinent", "palaeocontinent"},
        "cjk": {"古地理", "古海洋", "古大陆", "板块重建",
                "палеогеограф", "палеокарт", "палеоконтинент"},
    },
    "SCAT": {
        "ascii": {"scatter plot", "scatterplot", "scatter diagram",
                  "biplot", "crossplot", "cross plot"},
        "cjk": {"散点", "散布図", "точечная диаграмма", "рассеяни",
                "рассеиван"},
    },
}


def test_assistant_mode_tables_exist_on_both_sides():
    """REVIEW-2026-09-20 #105 item 3 (mirrored): chemical_stratigraphy /
    paleomap / scatter_plot used to have no web detection at all, so every
    such caption cost a vision call on the browser transport. Both engines
    now carry the same conservative tables."""
    js = _read(os.path.join("js", "app.js"))
    for group, tables in EXPECTED_TABLES.items():
        assert _py_table(group)[0] == tables["ascii"], group
        assert _py_table(group)[1] == tables["cjk"], group
        assert _js_array(js, f"{group.lower()}KeysAscii") == tables["ascii"], group
        assert _js_array(js, f"{group.lower()}KeysCjk") == tables["cjk"], group


def test_stem_list_matches_the_web_mirror():
    """``_STEMS`` decides leading-boundary vs whole-word matching — a stem
    missing on one side makes "Zonations ..." / "range charts" / "isotope"
    / "localities" classify differently in the browser and on the desktop."""
    js = _read(os.path.join("js", "app.js"))
    m = re.search(r"RCA_CHART_MODE_STEMS = \[(.*?)\];", js, re.DOTALL)
    assert m, "js RCA_CHART_MODE_STEMS not found"
    js_stems = _js_string_array(m.group(1))
    assert set(CM._STEMS) == js_stems


def test_detailed_and_legacy_js_tables_match():
    """The legacy rcaAutoDetectChartMode body and the detailed variant each
    carry their own keyword arrays — they must not drift."""
    js = _read(os.path.join("js", "app.js"))
    # every key-array declaration in the auto-detect region must appear
    # exactly TWICE (detailed + legacy)
    for group in GROUPS:
        for suffix in ("KeysAscii", "KeysCjk"):
            var = f"{group.lower()}{suffix}"
            decl = re.findall(rf"const {var} = \[", js)
            assert len(decl) == 2, f"{var} declared {len(decl)}x (expected 2)"


def test_python_detection_order_zon_first():
    """The zonation branch still runs before the abundance branch (the
    mixed-genre caption rule depends on it). REVIEW-2026-09-20 #105: the two
    branches now call the shared ``_hit`` helper instead of spelling out
    ``for k in _ZON_ASCII``, so the order is located by the table names inside
    the detector's own source rather than by a loop literal."""
    body = inspect.getsource(CM.auto_detect_chart_mode_ex)
    zo = body.index("_ZON_ASCII")
    ab = body.index("_AB_ASCII")
    assert zo < ab, "zonation must be evaluated before abundance"
    assert CM.auto_detect_chart_mode_ex("zonation")[0] == "zonation_chart"
    # The STEM half of the same rule: Python must match the plural the web
    # frontend has always matched (\bzonation leading-boundary only).
    assert CM.auto_detect_chart_mode_ex("Zonations of the sections")[:2] == \
        ("zonation_chart", True)
    # ... while the broad phrase that used to be in the table must NOT
    # short-circuit the vision classifier any more.
    assert CM.auto_detect_chart_mode_ex(
        "Correlation of the measured sections")[:2] == ("range_chart", False)


def test_zon_phrase_regex_is_mirrored_in_js():
    """REVIEW-2026-09-20 #105: narrowing ``correlation of`` to a keyword would
    also lose the split form ("Correlation of Triassic radiolarian ZONES"), so
    Python expresses it as one bounded phrase regex — and js/app.js carries the
    identical pattern in BOTH auto-detect variants."""
    patterns = CM._ZON_PHRASE_RES
    assert len(patterns) == 1
    pattern = patterns[0].pattern
    assert pattern == r"\bcorrelation of\b[^.\n]{0,80}?\bzones?\b"
    assert inspect.getsource(CM.auto_detect_chart_mode_ex).find(
        "_ZON_PHRASE_RES") != -1, "the phrase rule must actually be consulted"
    js = _read(os.path.join("js", "app.js"))
    assert js.count("/" + pattern + "/") == 2, (
        "the phrase regex must appear in the detailed AND the legacy variant")


def test_assistant_branch_order_matches_python():
    """chart_mode.py evaluates the three new tables PALEO -> SCAT -> CHEM, then
    the occurrence_matrix table, only when the caption does not name a range
    chart, and always BEFORE the explicit-range-chart return. The web mirror
    keeps the same sequence."""
    js = _read(os.path.join("js", "app.js"))
    start = js.index("function rcaAutoDetectChartModeDetailed")
    end = js.index("function rcaAutoDetectChartMode()", start)
    body = js[start:end]
    paleo = body.index("paleoKeysAscii, ")
    scat = body.index("scatKeysAscii, ")
    chem = body.index("chemKeysAscii, ")
    occ = body.index("occKeysAscii, ")
    assert paleo < scat < chem < occ, (
        "paleomap -> scatter_plot -> chemical_stratigraphy -> occurrence_matrix")
    assert body.index("if (!saysRangeChart)") < paleo
    assert body.index("phyloKeysAscii, ") < paleo, (
        "the assistant tables must stay AFTER the four established ones")
    # The explicit range-chart hit is a POSITIVE match right before the
    # "nothing matched" default (#105 item 1).
    assert body.rindex("if (saysRangeChart)") < body.rindex("matched: false")


class TestOccurrenceMatrix:
    """The 2026-10-15 mode: taxon x LOCALITY, no continuous axis."""

    def test_the_bare_stem_does_not_dead_end(self):
        """"localit" without a leading-boundary stem entry matches the word
        "localit" and never "localities" — the word the caption actually
        prints. Both engines carry the stem, so both match."""
        assert "localit" in CM._STEMS
        assert CM._match_kw("miscellaneous localities", "localit")
        assert CM._match_kw("a single locality", "localit")
        # ... and the whole-word rule still holds for the non-stem keywords.
        assert not CM._match_kw("boreholeless", "borehole")

    def test_captions_reach_the_branch(self):
        for caption in ("Miscellaneous localities", "Range of occurrences",
                        "Boreholes and wells", "Occurrence matrix",
                        "产地分布矩阵"):
            assert CM.auto_detect_chart_mode_ex(caption) == \
                ("occurrence_matrix", True), caption

    def test_the_branch_is_evaluated_last_among_the_keyword_branches(self):
        """Its keywords are the least specific in the table, so a caption that
        names a genre with its own prompt must still reach that genre."""
        assert CM.auto_detect_chart_mode_ex(
            "Paleogeographic map of fossil localities") == ("paleomap", True)
        assert CM.auto_detect_chart_mode_ex(
            "Locality data with isotope curve") == \
            ("chemical_stratigraphy", True)
        assert CM.auto_detect_chart_mode_ex(
            "Zonation of localities") == ("zonation_chart", True)
        assert CM.auto_detect_chart_mode_ex(
            "Columnar section, localities") == ("columnar_section", True)

    def test_an_explicit_range_chart_still_wins(self):
        """The branch sits inside the `not says_range_chart` block, so a
        caption that names a range chart is a range chart."""
        assert CM.auto_detect_chart_mode_ex(
            "Localities along the δ13C curve above the conodont range chart"
        ) == ("range_chart", True)
        assert CM.auto_detect_chart_mode_ex(
            "range chart of localities") == ("range_chart", True)

    def test_generic_table_is_never_auto_detected(self):
        """The fallback carries no keywords at all -- by design, a keyword
        would let it cannibalise genres that deserve a real mode."""
        assert MODES.MODES["generic_table"].keywords_ascii == ()
        assert MODES.MODES["generic_table"].keywords_cjk == ()
        assert MODES.MODES["generic_table"].keywords_stems == ()
