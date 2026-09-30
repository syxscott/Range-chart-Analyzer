"""The file a researcher actually downloads: CSV / TSV rendering, both engines.

AUDIT-2026-10-01. tests_export_parity.js is honest about its scope -- it covers
only the WPD scalar helpers (rcaWpdNum / rcaWpdNumText / rcaWpdSlug) through a
shared case table, and its own header says the bundle builder has no browser
call site yet. What nothing covered is the RENDERING of the CSV/TSV the user
downloads or pastes, which is the most consequential surface in the product:
these are the cells that reach a collaborator or a spreadsheet.

Measured over 33 (headers, rows) pairs. What the measurement found:

  * NO safety divergence. The formula-injection guard behaves the same on both
    sides, including the two cases that have separate histories behind them: a
    value whose trigger is HIDDEN by leading whitespace ("   =HYPERLINK(x)"),
    where the TSV whitespace fold reveals it and the guard has to run again
    (Python's to_tsv does; rcaToTsv does), and a value whose trigger is exposed
    the same way on the CSV path. Every one of those lands as inert text on both
    engines.
  * Two byte-level differences that are NOT defects, pinned below rather than
    "fixed":
      - the BOM lives in a different place. Python's to_csv returns it with the
        string; js/export.js adds it in the download wrapper (needsBom, for
        csv/tsv mimes), so a downloaded file carries it either way. The EFFECTIVE
        contract is asserted, not the location.
      - Python's csv.writer ends the last record with CRLF and the JS join does
        not. Both are valid RFC 4180, and changing the browser's bytes is a
        product decision rather than a mirror fix, so it is pinned as measured.
  * The remaining differences are the str()/String() family that
    rca_core/extractor._stringify_scalar and js/minimax.js#rcaStringifyScalar
    already carry (integral float, exponent padding, and a Python bool repr);
    the exporter inherits them because it is handed whatever the result dict
    holds. Recorded here so the export surface is not mistaken for a new home
    of that disease.

The security assertions below are SEMANTIC (is the cell inert?) rather than
byte equality, so the two pinned cosmetic differences do not fail the build --
but a guard that stopped prefixing a trigger WOULD.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from rca_core.exporter import to_csv, to_tsv  # noqa: E402

BOM = "\ufeff"
TRIGGERS = ("=", "+", "-", "@")

# (id, headers, rows) -- the shapes that decide whether a cell is inert.
CASES = [
    ("plain", ["a", "b"], [["1", "2"], ["3", "4"]]),
    ("comma_in_cell", ["a,b", "c"], [["x,y", "z"]]),
    ("quote_in_cell", ['a"b', "c"], [['x"y', "z"]]),
    ("newline_in_cell", ["a", "b"], [["line1\nline2", "z"]]),
    ("formula_eq", ["a"], [["=1+1"]]),
    ("formula_plus", ["a"], [["+1+1"]]),
    ("formula_minus", ["a"], [["-2+3"]]),
    ("formula_at", ["a"], [["@SUM(A1)"]]),
    ("formula_tab_eq", ["a"], [["\t=CMD|1!A1"]]),
    ("formula_leading_space", ["a"], [["   =HYPERLINK(x)"]]),
    ("formula_crlf", ["a"], [["\r-2+3"]]),
    ("formula_apostrophe_first", ["a"], [["'=cmd"]]),
    ("empty_cell", ["a", "b"], [["", "z"]]),
    ("none_cell", ["a", "b"], [[None, "z"]]),
    ("cjk", ["物种", "剖面"], [["中华虫", "剖面1"]]),
]

JS_DRIVER = r"""
const vm = require('vm');
const fs = require('fs');
const ctx = vm.createContext({
  console, Blob: class Blob {},
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
  setTimeout: (fn) => fn(), clearTimeout: () => {},
  document: { createElement: () => ({ click() {} }),
               body: { appendChild() {}, removeChild() {} } },
});
ctx.window = ctx; ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync('D:/GIthub/Range-chart Analyzer/js/export.js', 'utf8'),
                ctx, { filename: 'js/export.js' });
for (const fn of ['rcaToCsv', 'rcaToTsv', 'rcaDownload']) {
  if (typeof ctx[fn] !== 'function') { console.error('ENV BROKEN: ' + fn); process.exit(3); }
}
if (ctx.rcaToCsv(['a'], [['1']]) !== 'a\r\n1') {
  console.error('ENV BROKEN: rcaToCsv canonical case: '
                + JSON.stringify(ctx.rcaToCsv(['a'], [['1']])));
  process.exit(3);
}
// Capture what rcaDownload would put in the Blob, so the BOM can be asserted
// where the browser actually adds it.
let captured = null;
class CaptureBlob { constructor(parts) { captured = parts.join(''); } }
ctx.Blob = CaptureBlob;
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const out = spec.cases.map((c) => ({
  csv: ctx.rcaToCsv(c[1], c[2]),
  tsv: ctx.rcaToTsv(c[1], c[2]),
}));
ctx.rcaDownload('probe.csv', out[0].csv, 'text/csv');
const bomCsv = captured;
captured = null;
ctx.rcaDownload('probe.tsv', out[1].tsv, 'text/tab-separated-values');
const bomTsv = captured;
console.log(JSON.stringify({ out, bomCsv, bomTsv }));
"""

_TMP = Path.home() / "AppData" / "Local" / "Temp" / "rca_export_probe"


def _js():
    # Driver and spec go to TEMP: they are probe scaffolding, not repo files.
    _TMP.mkdir(parents=True, exist_ok=True)
    driver = _TMP / "export_render_driver.js"
    driver.write_text(JS_DRIVER, encoding="utf-8")
    spec = {"cases": [[cid, h, r] for cid, h, r in CASES],
            "headers": ["a", "b"], "rows": [["1", "2"]]}
    sp = _TMP / "export_render_spec.json"
    sp.write_text(json.dumps(spec, ensure_ascii=True), encoding="utf-8")
    r = subprocess.run(["node", str(driver), str(sp)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, f"js/export.js did not run: {r.stderr[:400]}"
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def js():
    return _js()


def _cells(text, sep):
    return [line.split(sep) for line in text.split("\n") if line != ""]


def _is_inert(cell: str) -> bool:
    """True when a spreadsheet would show the cell as text, not evaluate it."""
    s = cell.lstrip()
    return not s[:1] in TRIGGERS or cell[:1] == "'"


class TestCsvRenderingParity:
    def test_byte_identical_apart_from_the_two_pinned_differences(self, js):
        """Everything except the BOM and the trailing terminator must match."""
        mismatched = []
        for (cid, headers, rows), j in zip(CASES, js["out"]):
            py = to_csv(headers, [list(r) for r in rows])
            assert py.startswith(BOM), f"{cid}: python to_csv lost its BOM"
            body = py[len(BOM):]
            js_body = j["csv"] + "\r\n"      # the pinned terminator difference
            if body != js_body:
                mismatched.append((cid, body, js_body))
        assert not mismatched, "\n".join(
            f"{cid}\n  py={p!r}\n  js={j!r}" for cid, p, j in mismatched)

    def test_bare_formula_triggers_are_prefixed_on_both_engines(self, js):
        """The CSV path must not leave a cell STARTING with a trigger.

        A trigger hidden behind leading whitespace is a different question and
        is deliberately NOT guarded on either side: Excel does not evaluate a
        cell whose text begins with a space, and the TSV path is the one that
        has to re-guard after folding that space away (asserted in the TSV
        class). Asserted only for the bare triggers, which is the property
        both engines actually promise.
        """
        bare = {"formula_eq", "formula_plus", "formula_minus", "formula_at"}
        for (cid, headers, rows), j in zip(CASES, js["out"]):
            if cid not in bare:
                continue
            py_cell = to_csv(headers, [list(r) for r in rows]).split("\r\n")[1]
            js_cell = j["csv"].split("\r\n")[1]
            assert py_cell.startswith("'"), f"py {cid} not guarded: {py_cell!r}"
            assert js_cell.startswith("'"), f"js {cid} not guarded: {js_cell!r}"

    def test_the_bom_reaches_the_downloaded_csv_from_both_engines(self, js):
        # Python carries it in the string; the browser adds it in rcaDownload.
        # What matters is that a downloaded csv has one.
        assert to_csv(["a"], [["1"]]).startswith(BOM)
        assert js["bomCsv"].startswith(BOM), "the browser download lost the BOM"


class TestTsvRenderingParity:
    def test_after_the_fold_no_cell_starts_with_a_trigger_on_either_engine(self, js):
        """The TSV path folds whitespace FIRST, so a trigger hidden behind
        leading spaces becomes visible and the guard has to run again. That is
        the hole REVIEW-2026-09-20 reopened, and it is asserted on both sides
        here because the fold is what creates the exposure."""
        for (cid, headers, rows), j in zip(CASES, js["out"]):
            if "formula" not in cid:
                continue
            py_cells = _cells(to_tsv(headers, [list(r) for r in rows]), "\t")[1:]
            js_cells = _cells(j["tsv"], "\t")[1:]
            for i, (p, q) in enumerate(zip(py_cells, js_cells)):
                assert _is_inert(p[0] if p else ""), f"py tsv {cid}[{i}]: {p!r}"
                assert _is_inert(q[0] if q else ""), f"js tsv {cid}[{i}]: {q!r}"

    def test_the_tsv_carries_no_bom_on_either_side(self, js):
        # rcaToTsv is only used for the clipboard (js/app.js's data-copy path);
        # there is no TSV download, so a BOM would be a stray cell. Recorded
        # because js/export.js's needsBom comment also mentions TSV, and it keys
        # on the MIME string containing "csv" or "tsv" -- which the IANA
        # standard "text/tab-separated-values" does not. Harmless today, and it
        # is the trap for whoever adds a TSV download.
        assert not to_tsv(["a"], [["1"]]).startswith(BOM)
        assert not js["out"][0]["tsv"].startswith(BOM)
        assert not js["bomTsv"].startswith(BOM)


class TestPinnedCosmeticDifferences:
    """Recorded so a change to either is visible, not to make it fail."""

    def test_the_csv_trailing_terminator_as_measured(self, js):
        py = to_csv(["a"], [["1"]])
        js_csv = js["out"][0]["csv"]
        assert py.endswith("\r\n"), "python to_csv no longer ends with CRLF"
        assert not js_csv.endswith("\r\n"), (
            "the browser CSV now ends with CRLF too -- the pinned difference is "
            "resolved, so update this test and the module docstring")

    def test_the_pinned_tsv_differences_are_still_exactly_these(self, js):
        # The str()/String() family reaches the exporter because it is handed
        # whatever the result dict holds. Pinned so a NEW spelling difference
        # shows up as a failing test rather than as noise.
        index = {cid: j for (cid, _, _), j in zip(CASES, js["out"])}
        # None and empty render the same on both sides.
        for cid in ("none_cell", "empty_cell"):
            assert to_tsv(["a", "b"], [[None if cid == "none_cell" else "", "z"]]) \
                == index[cid]["tsv"], cid
