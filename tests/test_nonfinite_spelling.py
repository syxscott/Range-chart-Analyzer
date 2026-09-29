"""A non-finite number is spelled the same way in every engine (AUDIT-2026-09-30).

The same day, three separate places let NaN / +-Infinity through with three
different answers:

  * rca_core/extractor.py:_to_float_opt returned them to the axis maths, where
    a NaN endpoint drops the whole _axis_domain calibration (fixed in the
    companion commit, with js/viz.js#rcaVizNum).
  * js/minimax.js#rcaStringifyScalar used native String(), so a non-finite
    scalar rendered as "NaN" / "Infinity" / "-Infinity" in the browser while
    rca_core/extractor.py:_stringify_scalar -- the function it mirrors --
    produced "nan" / "inf" / "-inf". Same number, two spellings, in two
    engines.
  * js/table.js#rcaPyFloatStr already got this RIGHT, and its comment says it
    reproduces ``repr(float('nan'))``. So the product contained two functions
    disagreeing about the same value, and the export path was the correct one.

The rule the codebase had already settled on, in its own words, is the one
applied here: **Python's repr is the reference spelling for a number that ends
up as text.** The mirrored function is brought in line with the mirror and
with the precedent.

This is deliberately a TEXT contract, not a numeric one. The numeric side --
"is this value usable at all" -- is _to_float_opt's job, pinned in
tests/test_viznum_parity.py. A non-finite number reaching a text field is not
a crash, it is a spelling, and it has to match.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.extractor import _stringify_scalar  # noqa: E402

MINIMAX_JS = os.path.join(PROJECT_ROOT, "js", "minimax.js")
TABLE_JS = os.path.join(PROJECT_ROOT, "js", "table.js")

_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const files = process.argv.slice(3, 5);
let src = ['js/config.js', 'js/json-utils.js', 'js/ics_table.js',
           'js/reason-codes.js', 'js/quality.js', 'js/aggregate.js',
           'js/table.js', 'js/minimax.js']
  .map((f) => fs.readFileSync(path.join(ROOT, f), 'utf8')).join('\n');
const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval,
              TextEncoder, TextDecoder, URL,
              fetch: async () => { throw new Error('offline'); },
              document: { documentElement: { style: {}, dataset: {},
                         classList: { add(){}, remove(){}, toggle(){}, contains: () => false } } } };
ctx.globalThis = ctx;
vm.createContext(ctx);
new vm.Script(src).runInContext(ctx);
const SPECIAL = { __NAN__: NaN, __INF__: Infinity, __NINF__: -Infinity };
const out = process.argv[2].split(',').map((k) => {
  const v = (k in SPECIAL) ? SPECIAL[k] : (isNaN(Number(k)) ? k : Number(k));
  return {
    stringify: ctx.rcaStringifyScalar(v),
    pyFloatStr: ctx.rcaPyFloatStr(v),
    which: k,
  };
});
console.log(JSON.stringify(out));
"""


def _js(*values):
    import tempfile
    with pytest.MonkeyPatch.context():
        d = tempfile.mkdtemp(prefix="rca_strscalar_")
        dp = os.path.join(d, "drv.js")
        with open(dp, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER)
        keys = ",".join(values)
        proc = subprocess.run(["node", dp, keys, MINIMAX_JS, TABLE_JS],
                              capture_output=True, text=True,
                              encoding="utf-8", timeout=60,
                              env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
        assert proc.returncode == 0, f"node failed: {proc.stderr[:400]}"
        return json.loads(proc.stdout)


NONFINITE = ["__NAN__", "__INF__", "__NINF__"]


@pytest.mark.parametrize("key,expected", [
    ("__NAN__", "nan"), ("__INF__", "inf"), ("__NINF__", "-inf"),
])
def test_both_engines_spell_a_non_finite_scalar_identically(key, expected):
    assert _stringify_scalar(float(key.strip("_").replace("N", "N")
                                    .replace("AN", "an").replace("IN", "inf"))
                             if False else {
                                 "__NAN__": float("nan"),
                                 "__INF__": float("inf"),
                                 "__NINF__": float("-inf")}[key]) == expected
    got = _js(key)[0]["stringify"]
    assert got == expected, (
        f"js/minimax.js#rcaStringifyScalar spelled it {got!r}; "
        f"rca_core/extractor.py#_stringify_scalar spells it {expected!r}"
    )


def test_the_two_js_sites_agree_with_each_other():
    """js/table.js#rcaPyFloatStr is the reference. minimax.js used to carry a
    3-line stub of the same NAME that spelled NaN "NaN" -- and which one ran
    depended only on <script> order, so index.html was right by accident while
    the differential harnesses (minimax.js loaded last) measured the wrong one.
    With the stub gone there is one definition and every load order agrees."""
    for row in _js(*NONFINITE):
        assert row["stringify"] == row["pyFloatStr"], row
        assert row["pyFloatStr"] in ("nan", "inf", "-inf"), row


def test_no_js_function_is_defined_twice():
    """The whole family, not just this name.

    Two global declarations of one name in plain <script> files are not a
    merge conflict -- the later file silently wins, and which file is later
    differs between index.html and every test harness. That is invisible in
    review and flips behaviour the moment a script tag is added or a bundler
    reorders. This was the only duplicate among 438 global functions when it was
    found, and the count is asserted so a new one cannot slip in quietly.
    """
    import glob
    import re
    from collections import defaultdict

    owners = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(PROJECT_ROOT, "js", "*.js"))):
        text = open(path, encoding="utf-8").read()
        for m in re.finditer(r"^function (\w+)\(", text, re.M):
            owners[m.group(1)].append(os.path.basename(path))
        for m in re.finditer(
                r"^(?:const|let|var) (\w+) = (?:\([^)]*\)|async \([^)]*\)) =>",
                text, re.M):
            owners[m.group(1)].append(os.path.basename(path))

    assert len(owners) > 400, f"scan found only {len(owners)} functions"
    dupes = {n: fs for n, fs in owners.items() if len(fs) > 1}
    assert not dupes, (
        f"these global functions are defined more than once, so <script> order "
        f"silently decides which body runs: {dupes}"
    )


def test_ordinary_scalars_are_untouched():
    for v in (1.0, 0.5, 260.0, -7.25, 1e21, "x", "", None, True, False,
              [], {}, [1], {"a": 1}):
        assert _stringify_scalar(v) in (str(v), "", "true", "false", repr(v)), v
    rows = _js("1", "0.5", "260", "x", "true")
    assert [r["stringify"] for r in rows] == [
        "1", "0.5", "260", "x", "true"], rows
