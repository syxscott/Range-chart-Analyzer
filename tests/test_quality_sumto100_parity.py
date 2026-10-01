"""The sum-to-100 abundance check must score the same on both engines.

Found by adding the FIRST fixture case that exercises it. `quality_coverage`
had eight cases and a live runner, and every structural consistency check on
the harness passed -- but no payload in the group carried `abundance_unit: "%"`
at all, so P1-8 (a named scientific rule with its own weight, emitting a
warning the operator reads) had ZERO differential coverage. Same shape as the
merge group driving one mode of five, and the phylo fuzzer raising on 400/400
cases while reporting agreement.

With the case added, two real defects appeared at once:

1. The `sum` message param. js/quality.js formatted it with
   `String(Math.round(v.sum * 10) / 10)` -- halves away from zero -- while
   rca_core/quality.py uses `str(round(total, 1))`, halves to even. This is
   the idiom js/reason-codes.js#rcaPyRound exists to replace, and at ONE
   decimal the ties are common rather than exotic: 8 of 16 tie-shaped sums
   disagreed (2.25 -> 2.2 vs 2.3, 1.25 -> 1.2 vs 1.3, 100.25 -> 100.2 vs
   100.3, 0.15 -> 0.1 vs 0.2).

2. The deduction itself. js/quality.js did `passed = max(0, passed -
   deduction)`, so the penalty became (passed - deduction) / checks -- scaled
   by 1/checks, meaning the rule's weight depended on how many unrelated
   accuracy checks happened to run. And `if (checks === 0) return [1.0]`
   skipped the deduction altogether, where rca_core assigns `score = 1.0` and
   only THEN subtracts. Measured on the fixture: Python 0.98, browser 1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.quality import _abundance_sum_violations  # noqa: E402

_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const ctx = { console, TextEncoder, TextDecoder, URL };
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const f of ['js/reason-codes.js', 'js/quality.js']) {
  new vm.Script(fs.readFileSync(path.join(ROOT, f), 'utf8')).runInContext(ctx);
}
if (typeof ctx.rcaPyRound !== 'function') {
  process.stderr.write('rcaPyRound did not load\n'); process.exit(3);
}
console.log(JSON.stringify(ctx.scoreRangeChart(
  JSON.parse(fs.readFileSync(process.argv[2], 'utf8')))));
"""


def _js_score(payload):
    import tempfile
    with pytest.MonkeyPatch.context():
        d = tempfile.mkdtemp(prefix="rca_sumq_")
        dp = os.path.join(d, "d.json")
        drv = os.path.join(d, "drv.js")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER)
        proc = subprocess.run(["node", drv, dp], capture_output=True, text=True,
                              encoding="utf-8", timeout=60,
                              env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
        assert proc.returncode == 0, f"node failed: {proc.stderr[:400]}"
        return json.loads(proc.stdout)


def _py_score(payload):
    from rca_core.quality import score_range_chart
    return score_range_chart(json.loads(json.dumps(payload)))


def _rows(pairs):
    return {"abundances": [
        {"taxon": f"T{i}", "site": "S1", "level": lvl, "abundance": str(v),
         "abundance_unit": "%", "response_kind": "extracted"}
        for i, (lvl, v) in enumerate(pairs)],
        "sections": [{"name": "S1", "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}


# tie-shaped sums at the FIRST decimal -- the ones that used to disagree
TIE_CASES = [
    [("L1", 1.25), ("L1", 1.0)],
    [("L1", 0.05), ("L1", 0.0)],
    [("L1", 0.15)],
    [("L2", 100.25)],
    [("L1", 96.35)],
    [("L1", 2.25)],
    [("L1", 40), ("L1", 60)],          # a valid sum, no violation
    [("L1", 40), ("L1", 30)],          # short by 30
]


@pytest.mark.parametrize("pairs", TIE_CASES, ids=[str(p) for p in TIE_CASES])
def test_the_sum_to_100_check_scores_and_reports_identically(pairs):
    payload = _rows(pairs)
    py, js = _py_score(payload), _js_score(payload)
    assert js["score"] == pytest.approx(py["score"], abs=0.0), (
        f"python {py['score']} vs js {js['score']} for {pairs}"
    )
    assert js["grade"] == py["grade"], f"grades differ for {pairs}"
    py_keys = sorted(i["msg_key"] for i in py["issues"])
    js_keys = sorted(i["msg_key"] for i in js["issues"])
    assert js_keys == py_keys, f"issues differ for {pairs}: {py_keys} vs {js_keys}"


def test_the_reported_sum_is_rounded_the_python_way():
    """The number the operator reads. 2.25 must read 2.2, not 2.3."""
    payload = _rows([("L1", 1.25), ("L1", 1.0)])
    py = _py_score(payload)
    js = _js_score(payload)
    py_sum = [i["params"]["sum"] for i in py["issues"]
              if i["msg_key"] == "quality.abundance_sum_violation"][0]
    js_sum = [i["params"]["sum"] for i in js["issues"]
              if i["msg_key"] == "quality.abundance_sum_violation"][0]
    assert py_sum == "2.2", f"python side changed: {py_sum}"
    assert js_sum == py_sum, f"browser said {js_sum!r}, python said {py_sum!r}"


def test_the_deduction_applies_even_when_there_are_no_accuracy_checks():
    """`if (checks === 0) return [1.0]` used to skip the deduction entirely.
    rca_core assigns score = 1.0 and only then subtracts, so a result with no
    applicable accuracy checks but failing percentages is still penalised."""
    payload = _rows([("L1", 1.25), ("L1", 1.0)])
    py = _py_score(payload)
    assert any(i["msg_key"] == "quality.abundance_sum_violation"
               for i in py["issues"]), "the python side stopped warning"
    assert _abundance_sum_violations({"L1": 2.25}), (
        "2.25 is outside 95..105, so the violation is real"
    )
    js = _js_score(payload)
    assert js["score"] < 1.0, (
        "the browser must not report a perfect score for percentages that do "
        "not sum to 100"
    )
