"""AUDIT-2026-10-22: js/bed_parser.js must mirror rca_core/bed_parser.py.

The endpoint-label/index contradiction repair (rca_core/extractor.py:2345-2358)
and the export guard that reads the same labels both need a LABEL ORDINAL. The
JS side had no such reader at all, so the desktop app corrected a species row
whose printed endpoints and model-assigned indices disagreed and the browser
published the INVERTED one -- for the same model response. The browser did not
notice either: js/quality.js's FAD/LAD check needs a number out of the label
("A5 (606 m)" yields null) and so never checked the row at all.

This module is the guard that keeps the two readers in step. It runs the same
label matrix through both implementations and fails if any answer differs --
the same "verify parity by EVALUATING both sides" rule the rest of the repo
follows.

Paths are passed to node as ARGUMENTS: a hardcoded absolute path works on the
machine that wrote the test and is ENOENT on CI.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from rca_core.bed_parser import label_ordinal, ordinal_kind, parse_bed

ROOT = Path(__file__).resolve().parents[1]

#: Every label shape the two readers could disagree on.
LABELS = [
    # the repair's own shape: a sample label with a depth annotation
    "A5 (606 m)", "A1 (607 m)", "A1 (607 m)", "A21 (602 m)",
    # bare and Bed-prefixed
    "Bed 23", "Bed 23c", "23c", "23 C", "9a", "9", "Bed", "",
    # letter-prefixed and word-prefixed samples
    "A1", "A21", "Sample 3", "sample 12", "SPECIMEN 7", "B1 4C",
    # composite identifiers
    "87 B1 4C", "87 JED 8", "CAASP-84-20/15", "87 B1-4C",
    # things that are NOT positions
    "607 m", "23 m", "253 Ma", "1.7 Ma", "zone Z1", "Zone 3", "Fig. 4",
    "S1 (100 ft)", "1.7", "12.5", "0", "0.0",
    # ranges
    "23-25", "23 – 25", "A1-A5", "A1 - A5", "Bed 7-11", "Bed 7-Bed 11",
    # residue
    "Bed 23 (Fig. 4)", "Bed 23 to 25", "Bed 12.5", "bed  9", "bed9",
    # non-ASCII digits (Python's \d is Unicode-aware, JS's is not)
    "９", "٩", "9３", "Bed ９",
    # geological names that must not be read as positions
    "Wuchiapingian", "Late Pleistocene", "Middle Pleistocene",
    "Neoalbaillella optima", "Entactinia cf. P. longispina",
]

_JS_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');
const ctx = vm.createContext({ console, Math, JSON, String, Number });
ctx.window = ctx; ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx,
                { filename: 'js/bed_parser.js' });
if (typeof ctx.rcaLabelOrdinal !== 'function') {
  console.error('ENV BROKEN: rcaLabelOrdinal is not exposed'); process.exit(3);
}
if (typeof ctx.rcaOrdinalKind !== 'function') {
  console.error('ENV BROKEN: rcaOrdinalKind is not exposed'); process.exit(3);
}
if (typeof ctx.rcaParseBedCanonical !== 'function') {
  console.error('ENV BROKEN: rcaParseBedCanonical is not exposed'); process.exit(3);
}
const labels = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = labels.map((l) => {
  const info = ctx.rcaParseBedCanonical(l);
  return [ctx.rcaLabelOrdinal(l), ctx.rcaOrdinalKind(l),
          info ? info.bed_num : null, info ? info.bed_sub : null];
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_rows():
    import json as _json
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "bp.js"
        d.write_text(_JS_DRIVER, encoding="utf-8")
        s = Path(td) / "labels.json"
        s.write_text(_json.dumps(LABELS, ensure_ascii=True), encoding="utf-8")
        r = subprocess.run(
            ["node", str(d), str(ROOT / "js" / "bed_parser.js"), str(s)],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"js/bed_parser.js did not evaluate: {r.stderr[:400]}"
        return _json.loads(r.stdout)


def test_the_js_reader_answers_exactly_like_the_python_one(js_rows):
    assert len(js_rows) == len(LABELS)
    bad = []
    for label, js in zip(LABELS, js_rows):
        info = parse_bed(label)
        py = [label_ordinal(label), ordinal_kind(label),
              info["bed_num"] if info else None,
              (info.get("bed_sub") or "") if info else None]
        if py != js:
            bad.append((label, py, js))
    assert not bad, "the two bed-label readers disagree:\n" + "\n".join(
        "  %-28r python=%r js=%r" % (label, py, js) for label, py, js in bad)


def test_the_repair_shape_is_readable_by_both():
    """The shape the endpoint-label/index repair exists for."""
    assert label_ordinal("A5 (606 m)") == 5
    assert label_ordinal("A1 (607 m)") == 1
    assert ordinal_kind("A5 (606 m)") == ordinal_kind("A1 (607 m)")


def test_a_depth_is_not_an_ordinal():
    """A depth is monotonic but it is not a position: comparing it against a
    sample index would manufacture an inversion out of nothing."""
    assert label_ordinal("607 m") is None
    assert label_ordinal("1.7 Ma") is None
    assert label_ordinal("zone Z1") is None
    assert label_ordinal("23-25") is None
