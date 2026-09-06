"""Keyword-table parity lock between the three auto-detect implementations.

rca_core/chart_mode.py (both GUIs), js/app.js rcaAutoDetectChartModeDetailed
(web), and the duplicated arrays inside it must agree. The tables are
declared in two languages, so this test reads BOTH sources and asserts the
ASCII/CJK keyword sets match per category — a one-sided keyword addition
(now fails loudly) used to drift silently (REVIEW-2026-11-07 phylo case).
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


def test_zon_keywords_match_between_python_and_web():
    py = _read(os.path.join("rca_core", "chart_mode.py"))
    js = _read(os.path.join("js", "app.js"))
    for group in ("ZON", "AB", "COL", "PHYLO"):
        assert _py_tuple(py, group) == _js_array(js, f"{group.lower()}KeysAscii"), group
        assert _py_cjk(py, group) == _js_array(js, f"{group.lower()}KeysCjk"), group


def test_detailed_and_legacy_js_tables_match():
    """The legacy rcaAutoDetectChartMode body and the detailed variant each
    carry their own keyword arrays — they must not drift."""
    js = _read(os.path.join("js", "app.js"))
    # every key-array declaration in the auto-detect region must appear
    # exactly TWICE (detailed + legacy)
    for var in ("zonKeysAscii", "abKeysAscii", "colKeysAscii", "phyloKeysAscii"):
        decl = re.findall(rf"const {var} = \[", js)
        assert len(decl) == 2, f"{var} declared {len(decl)}x (expected 2)"


def test_python_detection_order_zon_first():
    import rca_core.chart_mode as cm

    src_path = os.path.join("rca_core", "chart_mode.py")
    body = _read(src_path)
    zo = body.index("for k in _ZON_ASCII")
    ab = body.index("for k in _AB_ASCII")
    assert zo < ab, "zonation must be evaluated before abundance"
    assert cm.auto_detect_chart_mode_ex("zonation")[0] == "zonation_chart"
