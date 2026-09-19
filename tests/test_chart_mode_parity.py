"""Keyword-table parity lock between the three auto-detect implementations.

rca_core/chart_mode.py (both GUIs), js/app.js rcaAutoDetectChartModeDetailed
(web), and the duplicated arrays inside it must agree. The tables are
declared in two languages, so this test reads BOTH sources and asserts the
ASCII/CJK keyword sets match per category — a one-sided keyword addition
(now fails loudly) used to drift silently (REVIEW-2026-11-07 phylo case).

REVIEW-2026-09-20 #105 CLOSED: the web mirror has landed. The ZON narrowing
("correlation of" -> "correlation of zones"), the split-form phrase regex and
the RANGE / CHEM / PALEO / SCAT tables now exist on BOTH sides, so the
one-sided exception lists are empty and every category is a strict equality
lock again. They are kept (as empty sets) rather than deleted so a future
"temporary" divergence has to be written down here, in the open, and makes
``test_pending_js_mirror_entries_are_still_pending`` fail if the entry is a
keyword that exists on neither side.
"""

import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name: str) -> str:
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


def _py_tuple(src: str, name: str) -> set[str]:
    m = re.search(rf"_{name}_ASCII\s*=\s*\((.*?)\)\n", src, re.DOTALL)
    assert m, f"python _{name}_ASCII not found"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _py_cjk(src: str, name: str) -> set[str]:
    m = re.search(rf"_{name}_CJK\s*=\s*\((.*?)\)\n", src, re.DOTALL)
    assert m, f"python _{name}_CJK not found"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _js_array(src: str, var: str) -> set[str]:
    m = re.search(rf"const {var} = \[(.*?)\];", src, re.DOTALL)
    assert m, f"js {var} not found"
    return set(re.findall(r"'([^']+)'", m.group(1)))


# Every category the web frontend mirrors. ``RANGE`` / ``CHEM`` / ``PALEO`` /
# ``SCAT`` used to be Python-only (see the retired
# ``test_python_only_mode_tables_have_no_web_mirror_yet``); the mirror landed,
# so they are compared exactly like the original four.
GROUPS = ("ZON", "AB", "COL", "PHYLO", "RANGE", "CHEM", "PALEO", "SCAT")

PENDING_PY_ONLY: dict[str, set[str]] = {}
PENDING_JS_ONLY: dict[str, set[str]] = {}


def test_mode_keywords_match_between_python_and_web():
    py = _read(os.path.join("rca_core", "chart_mode.py"))
    js = _read(os.path.join("js", "app.js"))
    for group in GROUPS:
        assert (_py_tuple(py, group) - PENDING_PY_ONLY.get(group, set())
                == _js_array(js, f"{group.lower()}KeysAscii")
                - PENDING_JS_ONLY.get(group, set())), group
        assert _py_cjk(py, group) == _js_array(js, f"{group.lower()}KeysCjk"), group


def test_zon_table_carries_the_narrowed_phrase_only():
    """REVIEW-2026-09-20 #105 item 2: the broad ``correlation of`` keyword
    short-circuited the vision classifier on plain lithostratigraphic
    captions. It must be gone from BOTH engines, and the narrowed zone-
    specific spelling present on both."""
    py = _read(os.path.join("rca_core", "chart_mode.py"))
    js = _read(os.path.join("js", "app.js"))
    for group, keys in (("ZON", _py_tuple(py, "ZON")),
                        ("ZON", _js_array(js, "zonKeysAscii"))):
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
    py = _read(os.path.join("rca_core", "chart_mode.py"))
    js = _read(os.path.join("js", "app.js"))
    for group, pending_py in PENDING_PY_ONLY.items():
        assert pending_py <= _py_tuple(py, group), group
        assert not (pending_py & _js_array(js, f"{group.lower()}KeysAscii")), group
    for group, pending_js in PENDING_JS_ONLY.items():
        assert pending_js <= _js_array(js, f"{group.lower()}KeysAscii"), group
        assert not (pending_js & _py_tuple(py, group)), group


# The assistant-mode tables, pinned verbatim so a keyword can only be added
# by editing BOTH this file and js/app.js (the equality tests above cover the
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
    py = _read(os.path.join("rca_core", "chart_mode.py"))
    js = _read(os.path.join("js", "app.js"))
    for group, tables in EXPECTED_TABLES.items():
        assert _py_tuple(py, group) == tables["ascii"], group
        assert _py_cjk(py, group) == tables["cjk"], group
        assert _js_array(js, f"{group.lower()}KeysAscii") == tables["ascii"], group
        assert _js_array(js, f"{group.lower()}KeysCjk") == tables["cjk"], group


def test_stem_list_matches_the_web_mirror():
    """``_STEMS`` decides leading-boundary vs whole-word matching — a stem
    missing on one side makes "Zonations ..." / "range charts" / "isotope"
    classify differently in the browser and on the desktop."""
    py = _read(os.path.join("rca_core", "chart_mode.py"))
    js = _read(os.path.join("js", "app.js"))
    m = re.search(r"_STEMS\s*=\s*\((.*?)\)\n", py, re.DOTALL)
    assert m, "python _STEMS not found"
    py_stems = set(re.findall(r'"([^"]+)"', m.group(1)))
    m = re.search(r"RCA_CHART_MODE_STEMS = \[(.*?)\];", js, re.DOTALL)
    assert m, "js RCA_CHART_MODE_STEMS not found"
    js_stems = set(re.findall(r"'([^']+)'", m.group(1)))
    assert py_stems == js_stems


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
    import inspect

    import rca_core.chart_mode as cm

    body = inspect.getsource(cm.auto_detect_chart_mode_ex)
    zo = body.index("_ZON_ASCII")
    ab = body.index("_AB_ASCII")
    assert zo < ab, "zonation must be evaluated before abundance"
    assert cm.auto_detect_chart_mode_ex("zonation")[0] == "zonation_chart"
    # The STEM half of the same rule: Python must match the plural the web
    # frontend has always matched (\bzonation leading-boundary only).
    assert cm.auto_detect_chart_mode_ex("Zonations of the sections")[:2] == \
        ("zonation_chart", True)
    # ... while the broad phrase that used to be in the table must NOT
    # short-circuit the vision classifier any more.
    assert cm.auto_detect_chart_mode_ex(
        "Correlation of the measured sections")[:2] == ("range_chart", False)


def test_zon_phrase_regex_is_mirrored_in_js():
    """REVIEW-2026-09-20 #105: narrowing ``correlation of`` to a keyword would
    also lose the split form ("Correlation of Triassic radiolarian ZONES"), so
    Python expresses it as one bounded phrase regex — and js/app.js carries the
    identical pattern in BOTH auto-detect variants."""
    import inspect

    import rca_core.chart_mode as cm

    patterns = cm._ZON_PHRASE_RES
    assert len(patterns) == 1
    pattern = patterns[0].pattern
    assert pattern == r"\bcorrelation of\b[^.\n]{0,80}?\bzones?\b"
    assert inspect.getsource(cm.auto_detect_chart_mode_ex).find(
        "_ZON_PHRASE_RES") != -1, "the phrase rule must actually be consulted"
    js = _read(os.path.join("js", "app.js"))
    assert js.count("/" + pattern + "/") == 2, (
        "the phrase regex must appear in the detailed AND the legacy variant")


def test_assistant_branch_order_matches_python():
    """chart_mode.py evaluates the three new tables PALEO -> SCAT -> CHEM,
    only when the caption does not name a range chart, and always BEFORE the
    explicit-range-chart return. The web mirror keeps the same sequence."""
    js = _read(os.path.join("js", "app.js"))
    start = js.index("function rcaAutoDetectChartModeDetailed")
    end = js.index("function rcaAutoDetectChartMode()", start)
    body = js[start:end]
    paleo = body.index("paleoKeysAscii, ")
    scat = body.index("scatKeysAscii, ")
    chem = body.index("chemKeysAscii, ")
    assert paleo < scat < chem, "paleomap -> scatter_plot -> chemical_stratigraphy"
    assert body.index("if (!saysRangeChart)") < paleo
    assert body.index("phyloKeysAscii, ") < paleo, (
        "the assistant tables must stay AFTER the four established ones")
    # The explicit range-chart hit is a POSITIVE match right before the
    # "nothing matched" default (#105 item 1).
    assert body.rindex("if (saysRangeChart)") < body.rindex("matched: false")
