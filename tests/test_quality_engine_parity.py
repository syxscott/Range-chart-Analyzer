"""The two quality scorers must agree on REAL model responses (AUDIT-2026-09-30).

js/quality.js opens with "Byte-for-byte parity with rca_core/quality.py" and
both produce the weighted A-F grade the operator reads. But quality was not one
of the twelve groups in tests_diff_frontend_parity.js, so that claim had never
been tested against each other -- and it was false.

Measured on the eight committed fixtures in tests/fixtures/real_payloads/
(genuine responses recorded by tests_real_corpus.py), one diverged by a full
letter grade:

    Hollis_et_al_2020_Austrral_radiolarian_biozone_p003_ra2.json
      rca_core   0.94  A
      js         0.89  B

The cause is a specific half-finished fix, not a vague disagreement.
js/quality.js's _detectMode grew a 'zonation' branch in UI-REVIEW-2026-09-05,
and its own comment says it used to stop at 'range_chart' so zonation results
were "scored with range-chart expectations". That was true of the DETECTOR.
_detectMode has two consumers in that file -- scoreCompleteness and
scoreStructure -- and only the first was given a 'zonation' branch. So
scoreStructure fell into the range_chart `else` and null-checked a zonation
payload against sections / biozones / other_fossils, none of which it emits:
three nulls, check failed, structure dimension 1.0 -> 0.5, composite 0.94 ->
0.89, grade A -> B. Same extraction, different letter depending on whether the
operator used the desktop app or the browser.

The second test below is the one that would have caught it: it asserts that
EVERY consumer of _detectMode handles 'zonation', not merely that the
detector can return it.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.quality import score_range_chart  # noqa: E402

QUALITY_JS = os.path.join(PROJECT_ROOT, "js", "quality.js")
RAW = os.path.join(PROJECT_ROOT, "tests", "fixtures", "real_payloads")

# reason-codes.js must be loaded before quality.js: the scorer consults it and
# degrades to pre-contract behaviour when absent, which would compare a guarded
# engine against an unguarded one.
JS_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const src = fs.readFileSync(path.join(ROOT, 'js', 'reason-codes.js'), 'utf8')
  + '\n' + fs.readFileSync(path.join(ROOT, 'js', 'quality.js'), 'utf8')
  + '\nglobalThis.__q = { scoreRangeChart };';
const ctx = { console: console, window: {} };
vm.createContext(ctx);
new vm.Script(src).runInContext(ctx);
console.log(JSON.stringify(ctx.__q.scoreRangeChart(JSON.parse(
  fs.readFileSync(process.argv[2], 'utf8')))));
"""


def _real_payloads():
    files = sorted(glob.glob(os.path.join(RAW, "*.json")))
    assert files, f"no real payload fixtures in {RAW} -- the corpus is the point"
    out = []
    for p in files:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        out.append((os.path.basename(p), data))
    return out


def _js_score(tmp_path, data):
    import json as _json
    dp = os.path.join(str(tmp_path), "d.json")
    drv = os.path.join(str(tmp_path), "drv.js")
    with open(dp, "w", encoding="utf-8") as fh:
        _json.dump(data, fh, ensure_ascii=False)
    with open(drv, "w", encoding="utf-8") as fh:
        fh.write(JS_DRIVER)
    proc = subprocess.run(["node", drv, dp], capture_output=True, text=True,
                          encoding="utf-8",
                          env=dict(os.environ, RCA_REPO=PROJECT_ROOT),
                          timeout=120)
    assert proc.returncode == 0, f"node driver failed: {proc.stderr[:2000]}"
    return _json.loads(proc.stdout)


def _canon(out):
    return (
        out.get("score"),
        out.get("grade"),
        tuple(sorted(f"{i.get('severity')}:{i.get('msg_key')}"
                     for i in (out.get("issues") or []) if isinstance(i, dict))),
    )


@pytest.mark.skipif(
    subprocess.run(["node", "--version"], capture_output=True).returncode != 0,
    reason="node not available",
)
@pytest.mark.parametrize("name,data", _real_payloads(),
                         ids=[n for n, _ in _real_payloads()])
def test_real_responses_score_identically_on_both_engines(name, data, tmp_path):
    py = _canon(score_range_chart(data))
    js = _canon(_js_score(tmp_path, data))
    assert py == js, (
        f"{name}: rca_core graded it {py[0]}/{py[1]} and js graded it "
        f"{js[0]}/{js[1]}. A letter grade that depends on which engine ran is "
        "not a parity question, it is a wrong answer on one side."
    )


def test_the_zonation_payload_specifically_is_not_penalised():
    """The exact regression, named, so a future change that reintroduces it
    fails with a message that says what broke rather than a bare number."""
    payload = {
        "zones": [{"name": "Z1", "age": "290-280 Ma", "level_range": "1-2"}],
        "zonations": [{"name": "bed 7", "note": "n1"}],
        "correlations": [],
        "confidence": 0.8,
    }
    py = score_range_chart(payload)
    assert py["grade"] == "A", py
    # A zonation payload has no sections/biozones/other_fossils. If any scorer
    # null-checks it against those range-chart keys it drops the structure
    # dimension, and 0.5 there is enough to move the letter on its own.
    from rca_core.quality import _score_structure
    structure, _issues = _score_structure(payload)
    assert structure == 1.0, (
        "the structure dimension must not null-check a zonation payload "
        f"against range-chart keys; got {structure}"
    )


def test_every_detectmode_consumer_handles_zonation():
    """The anti-recurrence guard, and the test that should have existed.

    _detectMode was taught about 'zonation' in 2026-09-05. That is not the same
    as every code path that READS the mode acting on it -- scoreStructure was
    left behind and silently penalised every zonation chart by half a
    dimension. So this asserts the consumer set, not the detector: any function
    in js/quality.js that compares _detectMode's result against 'zonation' must
    handle the columnar and abundance cases too, and must not fall through to a
    range_chart default that a zonation payload cannot satisfy.
    """
    src = open(QUALITY_JS, encoding="utf-8").read()

    def body_of(name):
        start = src.index(f"function {name}(")
        i = src.index("{", start)
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[start:j + 1]
        raise AssertionError(f"unbalanced braces in {name}")

    # Brace matching, not a regex: a regex cannot span the nested `if (...) {`
    # blocks that appear before the _detectMode call in most of these functions,
    # and a guard that silently finds only some of the consumers is worse than
    # none -- it would have missed this very bug.
    names = re.findall(r"function (\w+)\(", src)
    consumers = {n for n in names
                 if n != "_detectMode" and "_detectMode(" in body_of(n)}
    assert consumers, "no _detectMode consumers found -- the parse is wrong"
    assert "scoreStructure" in consumers, (
        f"expected scoreStructure among the consumers, found {sorted(consumers)}"
    )

    offenders = [fn for fn in sorted(consumers)
                 if "mode === 'zonation'" not in body_of(fn)]

    assert not offenders, (
        f"these _detectMode consumers never handle 'zonation': {offenders}. "
        "A detector that returns a mode is not enough; every consumer must "
        "act on it, or that mode falls through to range-chart defaults."
    )


_MODE_RE = re.compile(r"mode === '(\w+)'")


def _js_detector_modes():
    src = open(QUALITY_JS, encoding="utf-8").read()
    start = src.index("function _detectMode(")
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return set(re.findall(r"return '(\w+)'", src[start:j + 1]))
    raise AssertionError("unbalanced braces in _detectMode")


def test_both_detectors_return_the_same_mode_vocabulary():
    """A zonation fix that taught only ONE engine about zonation is the bug
    this file is about, one level down. Pin the vocabulary itself.

    The docstring of rca_core/quality.py::_detect_mode listed only columnar /
    abundance / range_chart and omitted zonation, which it has returned since
    UI-REVIEW-2026-09-05 -- the contract above the branch, not the branch
    itself, was stale.
    """
    from rca_core.quality import _detect_mode as py_detect

    py_modes = set()
    for probe in (
        {}, {"abundances": []}, {"sections": []},
        {"cross_beds": ["a"]}, {"correlations": []},
        {"zones": [], "zonations": []}, {"species_ranges": []},
        {"correlations": [], "species_ranges": []},
    ):
        py_modes.add(py_detect(probe))
    js_modes = _js_detector_modes()

    assert "zonation" in py_modes, (
        "the Python detector no longer classifies a zonation payload as "
        "'zonation' -- the whole scorer dispatch depends on it"
    )
    assert py_modes == js_modes, (
        f"the two detectors disagree on the mode vocabulary: "
        f"python={sorted(py_modes)} js={sorted(js_modes)}"
    )


def test_every_detectmode_consumer_handles_every_mode_it_can_receive():
    """Generalisation of the zonation guard: the invariant is not "handles
    zonation", it is "handles every mode the detector can return". Naming one
    mode in the test means the next mode someone adds ships unhandled."""
    src = open(QUALITY_JS, encoding="utf-8").read()

    def body_of(name):
        start = src.index(f"function {name}(")
        i = src.index("{", start)
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[start:j + 1]
        raise AssertionError(f"unbalanced braces in {name}")

    consumers = {n for n in re.findall(r"function (\w+)\(", src)
                 if n != "_detectMode" and "_detectMode(" in body_of(n)}
    assert consumers, "no _detectMode consumers found -- the parse is wrong"

    all_modes = _js_detector_modes()
    # range_chart is the `else` of every chain, so it is handled by
    # construction; anything else must appear as an explicit comparison.
    explicit_required = all_modes - {"range_chart"}
    assert explicit_required, "the detector returns nothing but range_chart?"

    incomplete = {}
    for fn in sorted(consumers):
        handled = set(_MODE_RE.findall(body_of(fn)))
        missing = explicit_required - handled
        if missing:
            incomplete[fn] = sorted(missing)
    assert not incomplete, (
        f"these _detectMode consumers do not handle every mode: {incomplete}. "
        f"Required explicitly: {sorted(explicit_required)}."
    )
