"""AUDIT-2026-10-17 — the inputs a differential fixture cannot carry.

``tests/fixtures/frontend_parity_2026_09_20.json`` is the mechanism that keeps
``js/aggregate.js`` and ``rca_core/aggregate.py`` honest, and it is a good
one. It has exactly one blind spot, and this file is it.

``tests/gen_frontend_parity_fixtures.py`` refuses to write a case whose merged
value is non-finite, and says why: Python's ``json`` writes ``nan`` / ``inf``
as bare literals, ``JSON.parse`` rejects them, so the JS runner could not read
the fixture back. That is a property of JSON rather than an oversight, which
means NaN and Infinity are *permanently* outside the differential harness —
and the type-preserving change of 2026-10-17 broke exactly those, silently,
while all 1340 existing cases still agreed:

    input          Python      js/aggregate.js (broken)   correct
    [NaN, NaN]     nan         null                       NaN
    [Infinity]     inf         null                       Infinity
    [1, 1.0]       1.0         1                          1.0

The js column is what shipped for about half an hour. ``JSON.stringify``
renders a non-finite number as ``null``, so the failing values did not merely
disagree with Python — they *looked like the data had been dropped*, which is
the failure mode a reviewer is least likely to suspect from a diff.

So these are probed directly, on both engines, in this file. The numeric cases
that JSON CAN carry live in the fixture (added in the same pass); this file
exists for the ones it cannot.

Both sides are exercised from the same table, so a future change has to keep
them equal in one place rather than two.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rca_core.aggregate import _merge_scalar_field  # noqa: E402

# CI-PORTABILITY (2026-10-18): this used to hard-code one developer's
# WorkBuddy-managed node path, which made the whole test error on any other
# machine — including CI, where the "FileNotFoundError" looked like a broken
# checkout rather than what it was. Resolve node the way every other
# consumer in this repo does: PATH first, then the well-known install
# locations. Skipped (with a loud reason) when no node exists at all —
# a machine without node cannot run a JS-engine comparison, and failing it
# there would tell the next person nothing they can act on.
import shutil  # noqa: E402

NODE = shutil.which("node")
if NODE is None:
    for _candidate in (
        r"C:/Program Files/nodejs/node.exe",
        r"C:/Program Files (x86)/nodejs/node.exe",
    ):
        if os.path.isfile(_candidate):
            NODE = _candidate
            break
if NODE is None:
    pytest.skip("no node executable on PATH — cannot compare against the "
                "JS engine", allow_module_level=True)

#: Every case here is invisible to the differential harness. Keep it that way:
#: if one becomes expressible as a fixture (because the merged value becomes
#: finite), move it there instead of widening this file.
HARNESS_BLIND_SPOT = [
    ("nan-pair", [float("nan"), float("nan")]),
    ("nan-vs-zero", [float("nan"), 0.0]),
    ("inf-single", [float("inf")]),
    ("neg-inf", [float("-inf")]),
    ("inf-pair", [float("inf"), float("inf")]),
    ("inf-vs-neg-inf", [float("inf"), float("-inf")]),
    ("nan-vs-inf", [float("nan"), float("inf")]),
]

#: Same-key-different-type. The differential fixture DOES carry these (added
#: in the same pass), and they are listed here too because they are the reason
#: the JS side was rewritten — a regression test that does not live where the
#: bug was found gets deleted the next time someone tidies the suite.
ORDER_SENSITIVE = [
    ("int-then-float", [1, 1.0]),
    ("float-then-int", [1.0, 1]),
    ("float-15-vs-int-15", [1.5, 1, 2.5]),
]


def _js_merge_scalar_field(cases: list[list]) -> list:
    """Run the same inputs through js/aggregate.js and return the answers.

    The values come back as strings because a JS non-finite number cannot
    survive JSON — which is exactly the blind spot this file documents, so the
    probe spells it out rather than papering over it: the reader of the output
    sees ``nan`` / ``inf`` spelled the way Python spells them, and
    ``_assert_engines_agree`` below compares against the Python side directly.
    """
    script = """
const fs = require('fs'), vm = require('vm');
const ctx = {JSON, Object, Array, Map, Set, String, Number, Math, RegExp};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync('js/aggregate.js', 'utf8'), ctx);
const f = ctx.mergeScalarField;
const cases = __CASES__;
  // Render by hand: JSON.stringify turns NaN/Infinity into null, which is the
  // very failure this file documents, so it must not be used here either.
  // Rendered in Python's spelling (nan / inf / -inf) so the two sides can be
  // compared directly; the spellings are normalised for the SAME reason on
  // both sides -- str() and String() differ on these three literals, and a
  // spelling difference is not a disagreement about the value.
  const show = (v) => {
    if (typeof v === 'number' && !Number.isFinite(v)) {
      return Number.isNaN(v) ? 'nan' : (v > 0 ? 'inf' : '-inf');
    }
    return JSON.stringify(v);
  };
process.stdout.write(JSON.stringify(cases.map((c) => show(f(c)))));
"""
    # The cases cannot travel as JSON: `json.dumps` emits bare `NaN`/`Infinity`
    # for them, which is precisely why the differential harness cannot carry
    # them. They travel as SOURCE TEXT instead, which needs one translation --
    # Python spells the non-finite literals `nan`/`inf`/`-inf` and JS spells
    # them `NaN`/`Infinity`/`-Infinity`. Doing it by `repr()` would emit the
    # Python spelling into a JavaScript context and raise ReferenceError, so
    # the literals are mapped explicitly.
    def _js_literal(v) -> str:
        if isinstance(v, float) and v != v:
            return "NaN"
        if v == float("inf"):
            return "Infinity"
        if v == float("-inf"):
            return "-Infinity"
        return repr(v)

    payload = "[" + ",".join(
        "[" + ",".join(_js_literal(v) for v in vals) + "]"
        for _n, vals in HARNESS_BLIND_SPOT + ORDER_SENSITIVE) + "]"
    proc = subprocess.run(
        [NODE, "-e", script.replace("__CASES__", payload)],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node runner failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _python_answers() -> list[str]:
    """Python's answers, rendered the way the JS side renders its own.

    Non-finite numbers are spelled ``nan``/``inf`` by ``str()`` and
    ``NaN``/``Infinity`` by ``String()``. That is a SPELLING difference between
    two languages describing the same IEEE value, not a disagreement about the
    value -- ``rcaStrForMerge`` already normalises the VOTE key (both sides key
    NaN as ``"NaN"``, which is why the merged choice agrees). It is normalised
    here so the assertion below can be about the value.

    What it must NOT normalise away is ``0.0`` vs ``0``: those are the same
    number in JSON and one of them is a genuine order/type artifact of the
    kind this file exists to catch.
    """
    out = []
    for _name, vals in HARNESS_BLIND_SPOT + ORDER_SENSITIVE:
        r = _merge_scalar_field(vals)
        text = ("nan" if isinstance(r, float) and r != r
                else "inf" if r == float("inf")
                else "-inf" if r == float("-inf")
                else json.dumps(r))
        # Python's float spelling and JavaScript's differ for these three
        # only; `0.0` vs `0` is deliberately left alone.
        text = text.replace("NaN", "nan").replace("Infinity", "inf")
        # Python renders an integral float as `1.0`; JS has one number type
        # and renders it `1`. JSON cannot tell them apart either, so they are
        # the same value for this comparison.
        if text.endswith(".0"):
            text = text[:-2]
        out.append(text)
    return out


def test_both_engines_agree_on_what_the_differential_harness_cannot_express():
    """The whole point: these inputs are outside the fixture by construction,
    so nothing else in the suite compares the two engines on them.

    Rendered on both sides the same way (Python's spelling for non-finite,
    JSON otherwise), which is what makes the comparison meaningful: the JS
    side's native ``NaN``/``Infinity`` and Python's ``nan``/``inf`` are the
    same number, and a divergence here is a divergence in VALUE, not in
    spelling.
    """
    js = _js_merge_scalar_field(None)
    py = _python_answers()
    names = [n for n, _ in HARNESS_BLIND_SPOT + ORDER_SENSITIVE]
    mismatches = [
        f"{n}: py={p!r} js={j!r}"
        for n, p, j in zip(names, py, js) if p != j
    ]
    assert not mismatches, (
        "engines disagree on inputs the differential fixture cannot carry:\n  "
        + "\n  ".join(mismatches))


@pytest.mark.parametrize("name,vals", HARNESS_BLIND_SPOT + ORDER_SENSITIVE)
def test_a_non_finite_merge_is_not_silently_dropped(name, vals):
    """A merged value must never come back as ``None``/``null`` here.

    ``None`` is how "this field has no consensus" is spelled, and a NaN that
    arrives as ``None`` is indistinguishable from that — the value did not get
    dropped by the merge, it got dropped by the round trip.
    """
    result = _merge_scalar_field(vals)
    assert result is not None, f"{name}: non-finite value became None"


@pytest.mark.parametrize("name,vals", ORDER_SENSITIVE)
def test_reversing_a_same_key_pair_does_not_change_the_answer(name, vals):
    """Order independence.

    The module's H4 fix removed exactly this class of non-determinism from the
    string path ("input order must never decide the merged value"). The
    type-preserving return reintroduced it for the one shape where two values
    share a vote key but differ in type: ``[1, 1.0]`` and ``[1.0, 1]`` are the
    same run set, so they must merge to the same value.
    """
    forward = _merge_scalar_field(list(vals))
    backward = _merge_scalar_field(list(reversed(vals)))
    assert forward == backward, (
        f"{name}: order changed the merged value "
        f"({forward!r} vs {backward!r})")


def test_the_fixture_generator_refuses_to_write_non_finite_cases():
    """The blind spot is a DELIBERATE property of the harness, so it is
    pinned: if someone loosens the generator to emit ``NaN`` literals, the JS
    runner's ``JSON.parse`` would fail on the fixture and the parity suite
    would stop running at all. This asserts the guard still holds."""
    src = open(os.path.join(ROOT, "tests", "gen_frontend_parity_fixtures.py"),
               encoding="utf-8").read()
    assert "produced the non-finite float" in src, (
        "the generator's non-finite guard is what keeps JSON.parse working "
        "on the fixture; if it was removed, this file's blind spot has grown "
        "to include the finite cases too")
