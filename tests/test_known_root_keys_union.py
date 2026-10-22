"""AUDIT-2026-10-22: the "known root keys" set must BE the union it claims.

There were THREE hand-maintained lists of the same thing:

  * ``rca_core/json_utils.py::_KNOWN_ROOT_KEYS``  (the union, used by the
    JSON recovery chain to decide whether a repaired fragment is a payload);
  * ``js/json-utils.js::RCA_KNOWN_ROOT_KEYS``      (its mirror);
  * ``js/minimax.js::RCA_MODE_ROOT_KEYS``           (per-mode, for the
    normalizers).

The union was missing the occurrence_matrix and generic_table roots, and the
consequence was not cosmetic: a TRUNCATED occurrence-matrix reply whose only
roots are ``columns`` and ``occurrences`` did not qualify as a payload, so
the recovery chain returned ONE INNER OBJECT instead of the array and the
whole matrix was lost. The same payload with a ``legend`` key alongside
repaired correctly -- which is why no test caught it.

This module pins the union to the per-mode lists, on BOTH sides, so the
hand-maintained copy cannot drift again without a red build.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _python_union() -> set[str]:
    from rca_core.json_utils import _KNOWN_ROOT_KEYS
    return set(_KNOWN_ROOT_KEYS)


def _python_per_mode() -> set[str]:
    from rca_core.extractor import _mode_root_keys
    from rca_core.modes import MODES
    out: set[str] = set()
    for name in MODES:
        out.update(_mode_root_keys(name))
    return out


def _js_sets() -> tuple[set[str], dict[str, list[str]]]:
    """(the union set, the per-mode map) as the browser sees them."""
    import re
    union_text = (ROOT / "js" / "json-utils.js").read_text(encoding="utf-8")
    m = re.search(r"RCA_KNOWN_ROOT_KEYS\s*=\s*new Set\(\[(.*?)\]\);", union_text, re.S)
    assert m, "js/json-utils.js no longer declares RCA_KNOWN_ROOT_KEYS"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    body = re.sub(r"(?m)^\s*//.*$", "", body)
    union = set(re.findall(r"'([^']+)'", body))

    mode_text = (ROOT / "js" / "minimax.js").read_text(encoding="utf-8")
    mm = re.search(r"RCA_MODE_ROOT_KEYS\s*=\s*\{(.*?)\n\};", mode_text, re.S)
    assert mm, "js/minimax.js no longer declares RCA_MODE_ROOT_KEYS"
    raw = re.sub(r"/\*.*?\*/", "", mm.group(1), flags=re.S)
    raw = re.sub(r"(?m)^\s*//.*$", "", raw)
    per_mode: dict[str, list[str]] = {}
    for name, chunk in re.findall(r"(\w+)\s*:\s*\[(.*?)\]", raw, re.S):
        per_mode[name] = re.findall(r"'([^']+)'", chunk)
    return union, per_mode


def test_the_python_union_is_every_per_mode_root_key():
    union = _python_union()
    per_mode = _python_per_mode()
    missing = sorted(per_mode - union)
    assert not missing, (
        "rca_core/json_utils.py::_KNOWN_ROOT_KEYS is missing %r -- it claims to "
        "be the union of the per-mode ROOTS constants, and a payload whose only "
        "root key is one of those does not qualify as a payload, so the JSON "
        "recovery chain returns a fragment instead of the array" % (missing,))
    extra = sorted(union - per_mode)
    assert not extra, (
        "_KNOWN_ROOT_KEYS carries %r, which no mode declares -- the set has "
        "drifted from the modes it is supposed to mirror" % (extra,))


def test_the_js_union_is_every_per_mode_root_key():
    union, per_mode = _js_sets()
    declared = {k for keys in per_mode.values() for k in keys}
    missing = sorted(declared - union)
    assert not missing, (
        "js/json-utils.js::RCA_KNOWN_ROOT_KEYS is missing %r -- the browser "
        "would reject a payload the desktop accepts" % (missing,))
    extra = sorted(union - declared)
    assert not extra, (
        "js/json-utils.js::RCA_KNOWN_ROOT_KEYS carries %r, which no mode "
        "declares" % (extra,))


def test_the_two_unions_agree():
    py = _python_union()
    js, _ = _js_sets()
    assert py == js, (
        "the two unions differ: python-only=%r js-only=%r -- the desktop and "
        "the browser would qualify different payloads"
        % (sorted(py - js), sorted(js - py)))


def test_a_truncated_occurrence_matrix_reply_recovers_as_the_array():
    """The failure this whole module exists for."""
    from rca_core.json_utils import safe_json_loads
    truncated = ('{"columns": [{"key": "a", "cells": ["x"]}], '
                 '"occurrences": [["x"')
    out = safe_json_loads(truncated)
    assert isinstance(out, dict) and "columns" in out, (
        "the truncated occurrence-matrix reply recovered as %r -- one inner "
        "object instead of the array" % (out,))
    assert "occurrences" in out, out


def test_the_browser_recovers_the_same_way():
    import os
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "probe.js"
        d.write_text(
            "const fs=require('fs'),vm=require('vm');"
            "const ctx=vm.createContext({console,JSON,String,Number,Object});"
            "ctx.window=ctx;ctx.globalThis=ctx;"
            "vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),ctx);"
            "const out=ctx.safeJsonLoads(process.argv[3]);"
            "process.stdout.write(JSON.stringify(out));",
            encoding="utf-8")
        r = subprocess.run(
            ["node", str(d), str(ROOT / "js" / "json-utils.js"),
             '{"columns": [{"key": "a", "cells": ["x"]}], "occurrences": [["x"'],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, r.stderr[:400]
        out = json.loads(r.stdout)
        assert isinstance(out, dict) and "columns" in out, (
            "the browser recovered %r -- one inner object instead of the array"
            % (out,))
