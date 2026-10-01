"""The quality paths no test had ever exercised (AUDIT-2026-09-30).

Enumerating what rca_core/quality.py and js/quality.js can emit, and then
asking which of those any test had ever produced, turned up keys that both
engines could emit and nothing had ever reached:

    quality.missing_confidence        quality.sections_absent
    quality.biozone_section_mismatch  quality.chimera_dropped
    quality.agreement_overflow        quality.all_section_refs_unmatched
    quality.empty_primary_rows        quality.many_extras
    quality.unmatched_section_ref

That is nine reachable paths through the module whose output is the grade the
operator trusts, with no differential coverage. The last two bugs found here
lived in exactly this shape of hole -- the abundance sum-to-100 check had a
whole group with eight cases and not one payload that reached it -- so the
question "which of the things this module can EMIT has ever run" is the one
worth asking, and it had never been.

Measured, all nine AGREE: same score, same grade, same issue set on both
engines. So this is coverage, not a fix. It is a test-only commit, and it is
worth having because the alternative is leaving nine paths unpinned for the
next person to break silently.

One finding from the same enumeration is NOT a divergence and is deliberately
not acted on: `quality.missing_section_ref` appears in rca_core/quality.py's
module docstring as an example issue and carries translations in both i18n
tables in three locales, but no code path on either side emits it. Deleting a
translation key is a product decision, not an audit fix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.quality import score_range_chart as py_score  # noqa: E402
from rca_core import quality as _Q  # noqa: E402

Q_MODULE = _Q.__file__

_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const ctx = { console, TextEncoder, TextDecoder, URL };
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const f of ['js/reason-codes.js', 'js/quality.js', 'js/ics_table.js']) {
  new vm.Script(fs.readFileSync(path.join(ROOT, f), 'utf8')).runInContext(ctx);
}
if (typeof ctx.rcaPyRound !== 'function') {
  process.stderr.write('rcaPyRound did not load\n'); process.exit(3);
}
const out = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).map((p) => {
  try { return ctx.scoreRangeChart(p); }
  catch (e) { return { error: String(e && e.message || e) }; }
});
console.log(JSON.stringify(out));
"""


def _js_scores(payloads):
    import tempfile
    d = tempfile.mkdtemp(prefix="rca_qkeys_")
    dp = os.path.join(d, "d.json")
    drv = os.path.join(d, "v.js")
    with open(dp, "w", encoding="utf-8") as fh:
        json.dump(payloads, fh)
    with open(drv, "w", encoding="utf-8") as fh:
        fh.write(_DRIVER)
    p = subprocess.run(["node", drv, dp], capture_output=True, text=True,
                       encoding="utf-8", timeout=90,
                       env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
    assert p.returncode == 0, f"node failed: {p.stderr[:500]}"
    return json.loads(p.stdout)


def _base(**over):
    d = {"sections": [{"name": "S1", "age_range": "300-290 Ma"}],
         "species_ranges": [{"species": "A", "section": "S1",
                             "range_base": "300 Ma", "range_top": "290 Ma",
                             "biozone": "Z1"}],
         "biozones": [{"name": "Z1", "section": "S1"}],
         "other_fossils": [], "confidence": 0.8}
    d.update(over)
    return d


# name -> (payload, the msg_key this payload is meant to produce)
CASES = {
    "missing_confidence": (_base(confidence=None),
                           "quality.missing_confidence"),
    "sections_absent": (_base(sections=[]), "quality.sections_absent"),
    "all_section_refs_unmatched": (
        _base(sections=[{"name": "S1"}],
              species_ranges=[{"species": "A", "section": "NOPE", "range_top": "9"},
                              {"species": "B", "section": "N2", "range_top": "9"}]),
        "quality.all_section_refs_unmatched"),
    "biozone_section_mismatch": (
        _base(biozones=[{"name": "Z1", "section": "OTHER"}]),
        "quality.biozone_section_mismatch"),
    "chimera_dropped": (
        _base(chimera_warnings=[{"row": {"species": "A"}, "kept": False,
                                 "dropped": 1}]),
        "quality.chimera_dropped"),
    "agreement_overflow": (
        _base(runs=2, species_ranges=[{"species": "A", "section": "S1",
                                       "range_top": "9", "agreement_count": 5,
                                       "runs": 2}]),
        "quality.agreement_overflow"),
    "empty_primary_rows": (_base(species_ranges=[]),
                           "quality.empty_primary_rows"),
    "many_extras": (_base(_extras={f"k{i}": i for i in range(9)}),
                    "quality.many_extras"),
    # No single payload reaches `unmatched_section_ref` -- that branch wants
    # SOME but not ALL refs unmatched, which is `all_section_refs_unmatched`'s
    # sibling. Built explicitly rather than left uncovered.
    "unmatched_section_ref": (
        _base(sections=[{"name": "S1"}],
              species_ranges=[{"species": "A", "section": "S1", "range_top": "9"},
                              {"species": "B", "section": "NOPE", "range_top": "9"}]),
        "quality.unmatched_section_ref"),
}

NAMES = list(CASES)
_PAYLOADS = [CASES[n][0] for n in NAMES]
_PY = [py_score(json.loads(json.dumps(p))) for p in _PAYLOADS]
_JS = _js_scores(_PAYLOADS)


def _keys(out):
    return sorted({i["msg_key"] for i in (out.get("issues") or [])})


@pytest.mark.parametrize("name", NAMES)
def test_the_path_is_reachable_and_both_engines_agree(name):
    idx = NAMES.index(name)
    py, js = _PY[idx], _JS[idx]
    want = CASES[name][1]
    assert want in _keys(py), (
        f"{name}: the payload no longer produces {want} on the Python side "
        f"(got {_keys(py)}), so this test is not exercising what it claims"
    )
    assert want in _keys(js), (
        f"{name}: python emits {want} and the browser does not "
        f"(js emitted {_keys(js)})"
    )
    assert js.get("error") is None, f"{name}: the browser scorer threw {js.get('error')}"
    assert js["score"] == pytest.approx(py["score"], abs=0.0), (
        f"{name}: python {py['score']} vs js {js['score']}"
    )
    assert js["grade"] == py["grade"], f"{name}: grades differ"
    assert _keys(js) == _keys(py), f"{name}: issue sets differ"


def test_no_quality_msg_key_is_left_without_a_case():
    """The scope assertion. Both engines can emit these; if a future change
    adds a key, or drops one, this is where it shows up rather than in a
    production grade nobody can reproduce.

    Python keys are collected with AST, not with a regex over the file: the
    module docstring contains `{"severity": "warning", "msg_key":
    "quality.missing_section_ref"}` as an EXAMPLE, and a text scan counts
    that as an emitted key. It is not emitted by anything -- the key exists in
    both i18n tables in three locales and in no code path -- which is a
    documentation smell, not a divergence, and the assertion below would
    otherwise report a bug that does not exist.
    """
    import ast
    import pathlib
    import re as _re

    tree = ast.parse(pathlib.Path(Q_MODULE).read_text(encoding="utf-8"))
    py_keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "msg_key"
                    and isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                    and v.value.startswith("quality.")):
                py_keys.add(v.value)

    js_src = pathlib.Path(
        os.path.join(PROJECT_ROOT, "js", "quality.js")).read_text(encoding="utf-8")
    # Strip block and line comments so the header's example output shape does
    # not count as an emitted key either.
    js_src = _re.sub(r"/\*[\s\S]*?\*/", "", js_src)
    js_src = _re.sub(r"(?m)^\s*//.*$", "", js_src)
    js_keys = set(_re.findall(r"msg_key:\s*'(quality\.[a-z0-9_]+)'", js_src))

    assert py_keys, "the AST scan found no msg_key -- the walk is wrong"
    assert js_keys, "the JS scan found no msg_key -- the comment strip is wrong"

    # quality.scoring_failed is the fail-closed exception path on the Python
    # side; it has no JS counterpart by design (the JS scorer's own exception
    # handler reports it, and cannot be provoked by a payload).
    python_only = py_keys - js_keys - {"quality.scoring_failed"}
    assert not python_only, (
        f"these keys rca_core/quality.py can emit and js/quality.js cannot: "
        f"{sorted(python_only)}. A payload that earns one of them shows a "
        f"warning on the desktop and nothing in the browser."
    )
    js_only = js_keys - py_keys
    assert not js_only, (
        f"these keys js/quality.js can emit and rca_core/quality.py cannot: "
        f"{sorted(js_only)}"
    )
