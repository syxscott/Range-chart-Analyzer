"""A confidence mean rounds the same way in both engines (AUDIT-2026-09-30).

js/aggregate.js had two sites still doing `Math.round(x * 10000) / 10000` --
the row-level mean in mergeConfidenceField and the top-level weighted mean in
merge_results. That idiom rounds halves AWAY FROM ZERO.

js/reason-codes.js#rcaPyRound exists precisely to avoid it, and its own
comment names these very inputs:

    ``Math.round(x * 10000) / 10000`` is NOT that: it rounds halves away from
    zero, so 1/32 = 0.03125 yields 0.0313 here but 0.0312 in Python (a
    32-cell grid is a normal chart, so this is reachable).

rca_core/aggregate.py rounds with `round(..., 4)`, which is ties-to-EVEN. So a
BigInt-exact reproduction of CPython rounding was written for one file and the
two call sites that could have used it were left behind. Measured over
tie-shaped inputs, 4 of 19 diverged: [0.01005], [0.01015], [0.03125] and
[1/32]. A mean reaches a tie whenever the confidences average to a half at the
fifth decimal, and these two functions are exactly the ones that compute a
confidence mean -- row level and aggregate level.

Both sites now call rcaPyRound when it is loaded, keeping the old idiom as the
documented fallback for a context that does not load reason-codes.js.

The JS runs the SHIPPED files, in index.html's order, rather than an extracted
function: extracting one function into a fresh context hides rcaPyRound and
silently takes the fallback, which reproduces the pre-fix numbers and makes a
correct fix look broken. The probe asserts rcaPyRound actually loaded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.aggregate import _NO_MERGE, _merge_confidence  # noqa: E402

_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const ctx = { console, TextEncoder, TextDecoder, URL };
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const f of ['js/reason-codes.js', 'js/aggregate.js']) {
  new vm.Script(fs.readFileSync(path.join(ROOT, f), 'utf8'), { filename: f })
    .runInContext(ctx);
}
if (typeof ctx.rcaPyRound !== 'function') {
  process.stderr.write('rcaPyRound did not load\n');
  process.exit(3);
}
const sets = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = {
  row: sets.map((v) => {
    const r = ctx.mergeConfidenceField(v);
    return (typeof r === 'number' && !isFinite(r)) ? String(r)
         : (r === ctx.NO_MERGE || r === undefined ? null : r);
  }),
};
// The aggregate-level weighted mean: a 32-run merge whose confidences all
// equal 1/32 is the reachable shape rcaPyRound's comment describes.
out.aggregate = (() => {
  const runs = sets[0].map((c) => ({
    sections: [], species_ranges: [], biozones: [], other_fossils: [],
    confidence: c,
  }));
  return ctx.rcaMergeResults(runs, null, ctx.RCA_DEFAULT_KEYMAP);
})();
console.log(JSON.stringify(out));
"""


def _js(sets):
    import tempfile
    with pytest.MonkeyPatch.context():
        d = tempfile.mkdtemp(prefix="rca_mergeround_")
        dp = os.path.join(d, "data.json")
        drv = os.path.join(d, "drv.js")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(sets, fh)
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER)
        proc = subprocess.run(["node", drv, dp], capture_output=True, text=True,
                              encoding="utf-8", timeout=60,
                              env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
        assert proc.returncode == 0, f"node driver failed: {proc.stderr[:400]}"
        return json.loads(proc.stdout)


# Inputs whose mean lands exactly on a half at the fifth decimal, plus the
# well-known binary-representation traps around them.
TIE_SETS = [
    [0.12345], [0.1234501], [0.1234499],
    [0.01005], [0.01015],                       # diverged before the fix
    [0.03125], [1 / 32], [0.09375],             # exact binary fractions
    [0.00005], [0.00015], [1.00005],
    [0.0625, 0.1875], [0.1, 0.2], [0.123455, 0.123445],
    [1 / 3, 1 / 3, 1 / 3], [-0.12345],
    [0.000015, 0.000015, 0.000015, 0.000015, 0.000015],
]


@pytest.mark.parametrize("values", TIE_SETS, ids=[str(v) for v in TIE_SETS])
def test_the_row_mean_rounds_identically(values):
    got = _js([values])["row"][0]
    want = _merge_confidence(values)
    if want is _NO_MERGE:
        assert got is None
    else:
        assert got == pytest.approx(want, abs=0.0), (
            f"mean of {values}: python {want!r} vs js {got!r} -- the ids agree "
            f"only if the rounding does"
        )


def test_the_aggregate_weighted_mean_rounds_identically():
    """The aggregate-level site, the second of the two.

    TWO runs, not one: with a single result merge_results takes the
    single-run passthrough and returns the confidence untouched, so a one-run
    fixture never reaches the weighted mean at all -- the same
    the-test-never-executed shape as the fuzzer that used to raise on 400/400
    cases. Two runs of 1/32 average to exactly 1/32, which is the case the
    rcaPyRound comment calls out by name.
    """
    from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results

    values = [1 / 32, 1 / 32]
    runs = [{"sections": [], "species_ranges": [], "biozones": [],
             "other_fossils": [], "confidence": c, "runs": 1} for c in values]
    py = merge_results(runs, schema=RANGE_CHART_SCHEMA)["confidence"]
    js = _js([values])["aggregate"]["confidence"]
    assert js == pytest.approx(py, abs=0.0), f"python {py!r} vs js {js!r}"
    assert js == 0.0312, (
        f"1/32 must round to 0.0312 on both sides (ties to even); got {js!r}. "
        "0.0313 is the old Math.round behaviour and is the bug this pins"
    )


def test_ordinary_confidences_are_unaffected():
    for v in (0.0, 0.25, 0.5, 0.8, 0.9, 1.0):
        got = _js([[v]])["row"][0]
        assert got == pytest.approx(_merge_confidence([v]), abs=0.0)
