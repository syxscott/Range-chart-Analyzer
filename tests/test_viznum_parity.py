"""js/viz.js#rcaVizNum and rca_core/extractor.py#_to_float_opt must agree.

rcaVizNum is documented as the mirror of _to_float_opt, and it feeds the plot
frame maths -- the numbers that decide where a fossil is DRAWN. Measured on 2026
-09-30 they disagreed on 11 of 42 probed inputs, in both directions:

    "260 Ma"    python=None   js=260     <- the worst
    "260ma"     python=None   js=260
    "1.2e2"     python=120    js=null
    "1e5"       python=100000 js=null
    ".5"        python=0.5    js=null
    "5."        python=5      js=null
    "1_000"     python=1000   js=null
    NaN / Infinity / -Infinity   python=nan/inf   js=null

"260 Ma" is the most ordinary age notation there is, and it is not a rounding
difference: _axis_domain() returns None when EITHER end fails to parse, so a
model that wrote {"at_0": "0 Ma", "at_999": "120 Ma"} produced a CALIBRATED
chart in the browser and an UNCALIBRATED one on the desktop, with the row's
point geometry dropped for want of a domain.

The non-finite case is the same class as the confidence-clamp and phylo-metadata
defects fixed the same day: Python's min/max do not propagate NaN, and a
non-finite number is not a number. js/viz.js already said so (isFinite);
_to_float_opt returned float(value) and let it through into the frame maths.

Each side now accepts what the other already accepted and nothing more: the
number grammar is Python's, the unit suffix is the mirror's explicit
``ma | m.a. | megaa`` list.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.extractor import _to_float_opt  # noqa: E402

VIZ_JS = os.path.join(PROJECT_ROOT, "js", "viz.js")

# Pull the shipped function out of the real file rather than restating it, so
# this test cannot pass against a stale copy of the code.
_JS_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const i = src.indexOf('function rcaVizNum(');
if (i < 0) { process.stderr.write('rcaVizNum not found\n'); process.exit(3); }
const j = src.indexOf('{', i);
let depth = 0, end = j;
for (let k = j; k < src.length; k++) {
  if (src[k] === '{') depth++;
  else if (src[k] === '}') { depth--; if (depth === 0) { end = k + 1; break; } }
}
const ctx = { console };
vm.createContext(ctx);
new vm.Script(src.slice(i, end) + '\nglobalThis.__n = rcaVizNum;').runInContext(ctx);
// "NaN"/"Infinity" spell the non-finite numbers a JSON payload cannot carry.
const SPECIAL = { __NAN__: NaN, __INF__: Infinity, __NINF__: -Infinity };
const out = process.argv.slice(3).map((a) => {
  const v = Object.prototype.hasOwnProperty.call(SPECIAL, a) ? SPECIAL[a] : a;
  const r = ctx.__n(v);
  return (typeof r === 'number' && !isFinite(r)) ? String(r) : r;
});
console.log(JSON.stringify(out));
"""


def _js_viz_num(*values):
    with pytest.MonkeyPatch.context() as mp:
        import tempfile
        d = tempfile.mkdtemp(prefix="rca_viznum_")
        dp = os.path.join(d, "driver.js")
        with open(dp, "w", encoding="utf-8") as fh:
            fh.write(_JS_DRIVER)
        proc = subprocess.run(["node", dp, VIZ_JS, *values],
                              capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
        assert proc.returncode == 0, f"node driver failed: {proc.stderr[:500]}"
        return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# the unit suffix, which is the case that cost the desktop its calibration
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("260 Ma", 260.0), ("260ma", 260.0), ("260 m.a.", 260.0),
    ("260 megaa", 260.0), ("-260 Ma", -260.0), ("1.2e2 Ma", 120.0),
    ("  260 MA  ", 260.0),
])
def test_both_engines_accept_a_unit_suffixed_age(text, expected):
    assert _to_float_opt(text) == expected
    assert _js_viz_num(text)[0] == expected


# --------------------------------------------------------------------------
# the numeric forms the hand-rolled JS regex used to reject
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("1.2e2", 120.0), ("1e5", 100000.0), ("1E5", 100000.0),
    (".5", 0.5), ("5.", 5.0), ("1_000", 1000.0), ("1_000 Ma", 1000.0),
    ("260", 260.0), ("260,5", 260.5), ("  260  ", 260.0),
    ("-260", -260.0), ("+260", 260.0),
])
def test_both_engines_agree_on_numeric_forms(text, expected):
    assert _to_float_opt(text) == expected
    assert _js_viz_num(text)[0] == expected


# --------------------------------------------------------------------------
# things that are not numbers, in either engine
# --------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "", "  ", "Bed 9", "120 cm", "4.2-6.8", "abc", "Ma", "x260",
])
def test_both_engines_refuse_non_numbers(text):
    assert _to_float_opt(text) is None
    assert _js_viz_num(text)[0] is None


# --------------------------------------------------------------------------
# the non-finite numbers a JSON payload cannot carry
# --------------------------------------------------------------------------
def test_a_real_nan_is_not_a_number_on_either_side():
    """A NaN endpoint would poison _axis_domain and drop the whole
    calibration; the same NaN that poisons canvas arithmetic in the browser."""
    assert _to_float_opt(float("nan")) is None
    assert _to_float_opt(float("inf")) is None
    assert _to_float_opt(float("-inf")) is None
    assert _js_viz_num("__NAN__", "__INF__", "__NINF__") == [None, None, None]


def test_ordinary_floats_still_pass_through():
    for v in (0.0, -0.0, 0.5, 1e21, 260.0, -260.0):
        assert _to_float_opt(v) == v
    assert _js_viz_num("0.5", "1e21", "260") == [0.5, 1e21, 260]


def test_bools_and_containers_are_still_refused():
    for v in (True, False, None, [], {}, [1], {"a": 1}):
        assert _to_float_opt(v) is None
    assert _js_viz_num("true", "[]") == [None, None]


# --------------------------------------------------------------------------
# the consequence, at the level the user sees
# --------------------------------------------------------------------------
def test_an_axis_calibration_written_in_years_survives_on_both_engines():
    """This is the regression in one line: a model writing the plot frame as
    {"at_0": "0 Ma", "at_999": "120 Ma"} used to lose the ENTIRE calibration on
    the desktop side, because _axis_domain returns None if either end is
    unparsable, and every point on that axis then had no domain to map into."""
    from rca_core.extractor import _axis_domain

    # _axis_domain takes the value UNDER the axis name; {"vertical": ...} is
    # what the caller walks, and its per-axis value is this dict.
    domain = _axis_domain({"at_0": "0 Ma", "at_999": "120 Ma"})
    assert domain is not None, "the axis domain was dropped"
    assert domain["at_0"] == 0.0
    assert domain["at_999"] == 120.0

    # and the mirror reads the same two endpoints
    assert _js_viz_num("0 Ma", "120 Ma") == [0.0, 120.0]

    # the older bed-index form keeps working, so the unit suffix was ADDED to
    # what parses rather than traded against it
    assert _axis_domain({"bottom": 1, "top": 24}) == {
        "at_0": 1.0, "at_999": 24.0, "unit": ""}
