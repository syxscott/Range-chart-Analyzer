# -*- coding: utf-8 -*-
"""FE-FIX-2026-09-22 (aud4 leftover): rcaExportCellText parity with Python.

The browser's export-cell text (js/table.js:rcaExportCellText) had no dict/list
branch, so a plain object exported as '[object Object]' while Python's
``rca_core.exporter._export_cell_text`` runs ``str(value)`` (the recursive
repr for containers).  This guard re-runs the SAME case table as
``tests_ui_fixes_2026_09_22.js`` against:

  1. the real Python ``_export_cell_text``            (asserts the authority),
  2. the JS ``rcaExportCellText`` via ``node -e``     (asserts the mirror),

and fails if EITHER drifts from the shared expected string -- and additionally
if JS drifts from live Python.  Node is required; when it is absent the JS leg
is skipped (the pure-Python authority leg still runs).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from rca_core.exporter import _export_cell_text

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (name, python value, expected str() output). The Python values are json.dumps'd
# and handed to node, so the exact same value reaches both implementations.
CASES = [
    ("dict-int", {"a": 1}, "{'a': 1}"),
    ("dict-str", {"species": "A"}, "{'species': 'A'}"),
    ("list-int", [1, 2], "[1, 2]"),
    ("list-mixed", [1, "a", True], "[1, 'a', True]"),
    ("nested", {"a": [1, {"b": 2}]}, "{'a': [1, {'b': 2}]}"),
    ("quote-single", {"k": "it's"}, "{'k': \"it's\"}"),
    ("quote-double", {"k": 'say "hi"'}, "{'k': 'say \"hi\"'}"),
    ("quote-both", {"k": "both ' and \""}, "{'k': 'both \\' and \"'}"),
    ("bool", {"t": True, "f": False}, "{'t': True, 'f': False}"),
    ("float", {"f": 1.5}, "{'f': 1.5}"),
    ("float-exp", {"f": 1e-05}, "{'f': 1e-05}"),
    ("none", {"n": None}, "{'n': None}"),
    ("dict-order", {"name": "plain", "count": 3, "ratio": 2.25},
     "{'name': 'plain', 'count': 3, 'ratio': 2.25}"),
    ("empty-dict", {}, "{}"),
    ("empty-list", [], "[]"),
    ("escapes", {"back\\slash": "tab\there\nnewline"},
     "{'back\\\\slash': 'tab\\there\\nnewline'}"),
]

CASE_NAMES = [c[0] for c in CASES]

# A non-JSON-representable corner asserted only on the Python side (a container
# holding a float('nan') keeps the repr spelling; only a TOP-LEVEL non-finite
# cell blanks).  Mirrors the JS 'export-nested-nonfinite-kept' check.
NON_FINITE_NESTED = ({"x": float("nan")}, "{'x': nan}")
NON_FINITE_TOP = (float("nan"), "")

_NODE_DRIVER = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const quiet = () => ({ log() {}, warn() {}, error() {} });
const ctx = {
  console: quiet(), setTimeout, clearTimeout,
  document: {
    documentElement: { lang: 'zh', setAttribute() {}, getAttribute() { return null; },
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {} },
    addEventListener() {}, removeEventListener() {}, querySelector: () => null,
    querySelectorAll: () => [], getElementById: () => null,
    createElement: () => ({ setAttribute() {}, getAttribute() { return null; }, style: {},
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false } }),
    body: {},
  },
  window: { addEventListener() {}, removeEventListener() {} },
  navigator: { language: 'zh' }, location: { protocol: 'http:', host: 'x', pathname: '/' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  URL: require('url').URL, Blob: class { constructor() {} },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const f of ['js/config.js', 'js/i18n.js', 'js/ics_table.js', 'js/reason-codes.js', 'js/table.js']) {
  vm.runInContext(fs.readFileSync(f, 'utf8'), ctx, { filename: f });
}
const ec = vm.runInContext('rcaExportCellText', ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = cases.map((c) => ({ name: c.name, result: ec(c.input) }));
fs.writeFileSync(process.argv[3], JSON.stringify(out));
"""


def _node():
    return shutil.which("node")


@pytest.fixture(scope="module")
def js_results():
    node = _node()
    if not node:
        pytest.skip("node is required for the JS export-cell parity leg")
    payload = [{"name": name, "input": value} for name, value, _exp in CASES]
    with tempfile.TemporaryDirectory() as td:
        driver = os.path.join(td, "driver.cjs")
        cases_path = os.path.join(td, "cases.json")
        out_path = os.path.join(td, "out.json")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write(_NODE_DRIVER)
        with open(cases_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        proc = subprocess.run(
            [node, driver, cases_path, out_path],
            cwd=REPO_ROOT, capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            raise AssertionError(
                "node driver failed (%d): %s" % (
                    proc.returncode, proc.stderr.decode("utf-8", "replace"))
            )
        with open(out_path, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
    return {r["name"]: r["result"] for r in rows}


@pytest.mark.parametrize("name", CASE_NAMES)
def test_python_matches_expected(name):
    """Authority leg: Python str(value) is the contract being mirrored."""
    value, expected = next((v, e) for n, v, e in CASES if n == name)
    assert _export_cell_text(value) == expected


@pytest.mark.parametrize("name", CASE_NAMES)
def test_js_matches_expected(name, js_results):
    """Mirror leg: the JS must equal the shared expected string."""
    _value, expected = next((v, e) for n, v, e in CASES if n == name)
    assert js_results[name] == expected, (
        "JS rcaExportCellText(%r) drifted: got %r want %r" % (name, js_results[name], expected)
    )


@pytest.mark.parametrize("name", CASE_NAMES)
def test_js_matches_python(name, js_results):
    """Drift guard: JS and live Python must agree, not just match a constant."""
    value, _expected = next((v, e) for n, v, e in CASES if n == name)
    assert js_results[name] == _export_cell_text(value)


def test_export_cell_text_is_pure_function_of_python():
    # The three legs above key off the SAME expected; this one proves Python
    # itself never leaks '[object Object]' style text for a container.
    assert _export_cell_text({"a": 1}).startswith("{")
    assert "[object" not in _export_cell_text({"a": 1})


def test_non_finite_top_level_blanks():
    value, expected = NON_FINITE_TOP
    assert _export_cell_text(value) == expected


def test_non_finite_nested_keeps_repr():
    value, expected = NON_FINITE_NESTED
    assert _export_cell_text(value) == expected
