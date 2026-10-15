"""Cross-engine parity for the TABLE CONFIGS themselves — ids and columns.

tests/test_table_config_detectors_parity.py proves the two engines pick the
same BRANCH. This proves they then build the same TABLES, which is the half
that was uncovered: the parity harness has no table-config group, and nothing
replayed `rcaTableConfigs` against `get_configs_for_result`.

AUDIT-2026-10-15 — what that let through. occurrence_matrix shipped with a
`_looks_occurrence_matrix` on the Python side and nothing at all on the JS side,
so `rcaTableConfigs` fell through to the range-chart branch, which returns its
four configs UNCONDITIONALLY. Measured on a real 73-record result:

    python : columns(18)   occurrences(73) legend(3)                      = 3
    js     : sections(0) species_ranges(0) biozones(0) other_fossils(0)
             columns(18) legend(3) occurrences(73) strat_rows(1)          = 8

Four PHANTOM EMPTY tables, and the three real ones arrived through
`rcaGenericConfigs`, whose headers are humanised literals rather than `col.*`
keys — so they could not be translated at all.

This was LATENT, not user-visible: js/minimax.js `RCA_BROWSER_UNSUPPORTED_MODES`
refuses occurrence_matrix inside `extractRangeChart` before the backend
dispatch (executed, not assumed — it returns errorKey 'err.mode' and never
reaches the network), the desktop renders through exporter.py rather than this
file, and the browser has no result-import path. It was one commit from live,
since flipping `browser_supported` to True is the documented way to enable the
mode. The payload set below keeps it that way.

Compares ids and cols only. Row extraction is covered by the export parity and
the per-mode suites; the failure this file exists to catch is "the browser
shows 8 tables and the desktop writes 3", which ids alone already prove.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import exporter  # noqa: E402

# Loaded in the same order as index.html declares them (all `defer`, so that
# order is guaranteed): i18n.js must precede table.js, because rcaColLabel
# resolves labels against RCA_I18N / t().
_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const store = new Map();
const ctx = {
  console, TextEncoder, TextDecoder, URL, setTimeout, clearTimeout,
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  document: {
    addEventListener() {}, removeEventListener() {},
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; },
    documentElement: { setAttribute() {}, getAttribute() { return null; },
                       style: {}, classList: { add() {}, remove() {} } },
    body: { classList: { add() {}, remove() {} } },
  },
};
ctx.globalThis = ctx; ctx.window = ctx; ctx.self = ctx;
vm.createContext(ctx);
for (const f of ['js/config.js', 'js/i18n.js', 'js/json-utils.js',
                 'js/reason-codes.js', 'js/aggregate.js', 'js/ics_table.js',
                 'js/quality.js', 'js/table.js']) {
  new vm.Script(fs.readFileSync(path.join(ROOT, f), 'utf8'),
                { filename: f }).runInContext(ctx);
}
if (typeof ctx.rcaTableConfigs !== 'function') {
  process.stderr.write('rcaTableConfigs missing'); process.exit(3);
}
const payloads = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify(payloads.map((d) => ctx.rcaTableConfigs(d)
  .map((c) => ({ id: c.id, cols: c.cols })))));
"""


def _species():
    return {"species": "Ozarkodina", "section": "S1",
            "range_base": "300 Ma", "range_top": "295 Ma"}


# label -> payload. One representative per mode plus the shapes that decide
# BETWEEN modes, because the divergence only shows at a branch boundary.
#
# STRICT labels route to one of the SPECIFIC branches, where the two engines
# are required to agree exactly. The others fall through to the range-chart /
# generic fallback, where js/table.js deliberately returns four configs
# UNCONDITIONALLY — documented at length in table.js (AUDIT-2026-09-27 item
# 10.1): the browser is the EDITING surface, so the empty shells ARE the
# "add a first row" affordance, while the desktop shows its own empty-state
# page. exporter.py's `_nonempty_or_none` drops them. That asymmetry is a
# recorded product call, not a bug, so asserting equality there would be
# asserting something the project has explicitly declined.
PAYLOADS = [
    ("empty", {}),
    ("range: species", {"sections": [{"name": "S1"}],
                        "species_ranges": [_species()]}),
    ("columnar: sections with id",
     {"sections": [{"id": "S1", "name": "S1"}],
      "species_ranges": [_species()]}),
    ("abundance: rows",
     {"abundances": [{"taxon": "P", "site": "S1", "level": "3",
                      "abundance": "12", "abundance_unit": "%"}]}),
    ("zonation: zones", {"zones": [{"name": "Z1", "age": "290-280 Ma"}]}),
    ("phylo: nodes",
     {"nodes": [{"id": "r", "parent": None}], "root_ids": ["r"]}),
    ("paleomap: continents",
     {"continents": [{"name": "Gondwana"}], "fossil_sites": [{"name": "S1"}]}),
    ("occurrence: full",
     {"columns": [{"index": 0, "label": "87 B1 4C", "kind": "locality"},
                  {"index": 1, "label": "87 B1 5C", "kind": "borehole"}],
      "occurrences": [{"taxon": "Capnodoce", "authority": "De Wever",
                       "site": "87 B1 4C", "code": "A", "class": "abundant"},
                      {"taxon": "Ozarkodina", "authority": "Müller",
                       "site": "87 B1 5C", "code": "R", "class": "rare"}],
      "legend": [{"code": "A", "class": "abundant"},
                 {"code": "R", "class": "rare"}],
      "strat_rows": [{"label": "Conodont level", "values": ["1", "2"]}],
      "confidence": 0.8}),
    ("occurrence: multi-run",
     {"occurrences": [{"taxon": "Capnodoce", "site": "87 B1 4C",
                       "code": "A", "agreement": "2/2"}],
      "runs": 3}),
    ("occurrence: empty cells",
     {"columns": [{"index": 0, "label": "L1", "kind": "locality"}],
      "occurrences": [], "legend": [{"code": "A", "class": "abundant"}]}),
    ("occurrence: rows not dicts", {"occurrences": ["Capnodoce"]}),
    ("generic: unrecognised key",
     {"sections": [{"name": "S1"}], "species_ranges": [_species()],
      "my_notes": [{"topic": "x", "body": "y"}]}),
    ("generic: only an unknown key", {"my_notes": [{"topic": "x"}]}),
]

# Routes to a specific branch on both sides -> the engines must agree exactly.
#
# `zonation: zones` is deliberately NOT here: `{"zones": [{"name", "age"}]}`
# carries no `rank` / `zonation` marker, so _looks_zonation_chart says False on
# BOTH engines (verified against the pre-refactor worktree too) and the table
# arrives through the generic fallback. Labelling it "zonation" and then
# asserting exact equality failed for that reason, not because of a divergence.
STRICT = {
    "columnar: sections with id",
    "abundance: rows",
    "phylo: nodes",
    "paleomap: continents",
    "occurrence: full",
    "occurrence: multi-run",
}

# The four shells rcaRangeChartConfigs returns unconditionally.
SHELLS = {"sections", "species_ranges", "biozones", "other_fossils"}


def _js_tables():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        dp = os.path.join(td, "p.json")
        drv = os.path.join(td, "v.js")
        with open(dp, "w", encoding="utf-8") as fh:
            json.dump([d for _, d in PAYLOADS], fh)
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(_DRIVER)
        proc = subprocess.run(["node", drv, dp], capture_output=True, text=True,
                              encoding="utf-8", timeout=90,
                              env=dict(os.environ, RCA_REPO=PROJECT_ROOT))
        assert proc.returncode == 0, (
            f"node driver failed: {proc.stderr[:600]}")
        return json.loads(proc.stdout)


_JS = _js_tables()


def _py_tables(data):
    return [{"id": c["id"], "cols": list(c["cols"])}
            for c in exporter.get_configs_for_result(data)]


@pytest.mark.parametrize("idx,label", list(enumerate(l for l, _ in PAYLOADS)),
                         ids=[l for l, _ in PAYLOADS])
def test_both_engines_build_the_same_tables(idx, label):
    """On a specific branch the two engines must agree on which tables exist.

    Compared as SETS of ids, not as ordered lists. The columnar branch is the
    case that forced this: it writes seven sheets, the browser renders the same
    seven with the same columns, but in a different sequence
    (exporter.py: sections / fossil_legend / lithology_legend / cross_beds /
    lithology_blocks / age_units / samples, vs js/table.js which groups the
    block-ish tables first). That ordering difference is pre-existing — the
    Python side is byte-identical before and after this round's work — and it
    costs the user nothing but a different tab/sheet sequence. Column parity
    per table is asserted separately and unconditionally.
    """
    data = PAYLOADS[idx][1]
    if label not in STRICT:
        pytest.skip(f"{label} falls through to the range-chart/generic "
                    f"branch, where the browser keeps four empty editing "
                    f"shells on purpose (see STRICT)")
    py_ids = [t["id"] for t in _py_tables(data)]
    js_ids = [t["id"] for t in _JS[idx]]
    assert set(py_ids) == set(js_ids), (
        f"{label}: the browser and the desktop disagree about which sheets "
        f"this result has.\n"
        f"  only python: {sorted(set(py_ids) - set(js_ids))}\n"
        f"  only js    : {sorted(set(js_ids) - set(py_ids))}\n"
        f"get_configs_for_result and rcaTableConfigs are documented mirrors; "
        f"a difference here means the browser shows tables the export never "
        f"writes, or writes columns the browser does not show."
    )


@pytest.mark.parametrize("idx,label", list(enumerate(l for l, _ in PAYLOADS)),
                         ids=[l for l, _ in PAYLOADS])
def test_the_browser_never_drops_or_reshapes_an_exported_table(idx, label):
    """The property that holds on EVERY payload, strict or not.

    js/table.js may add empty editing shells the export does not write — that
    is deliberate. What it must never do is omit a table Python writes, or
    give it different columns: either would mean data that is on screen and
    absent from the file the user cites (the failure exporter.py's
    `_looks_occurrence_matrix` docstring records happening the other way).
    """
    data = PAYLOADS[idx][1]
    js_by_id = {t["id"]: t["cols"] for t in _JS[idx]}
    for table in _py_tables(data):
        assert table["id"] in js_by_id, (
            f"{label}: the desktop writes a `{table['id']}` sheet that the "
            f"browser never renders — js tables: "
            f"{sorted(js_by_id)}"
        )
        assert js_by_id[table["id"]] == table["cols"], (
            f"{label}: `{table['id']}` has different columns per engine.\n"
            f"  python: {table['cols']}\n"
            f"  js    : {js_by_id[table['id']]}"
        )


@pytest.mark.parametrize("idx,label", list(enumerate(l for l, _ in PAYLOADS)),
                         ids=[l for l, _ in PAYLOADS])
def test_the_only_extra_tables_are_the_four_documented_editing_shells(idx, label):
    """Pins WHY the browser may carry more tables, so a real fifth phantom
    table cannot hide inside an accepted difference."""
    js_ids = {t["id"] for t in _JS[idx]}
    extra = js_ids - {t["id"] for t in _py_tables(PAYLOADS[idx][1])}
    assert extra <= SHELLS, (
        f"{label}: the browser renders {sorted(extra)}, which are neither "
        f"exported tables nor the four documented range-chart editing shells"
    )


def test_occurrence_matrix_makes_three_tables_and_not_eight():
    """The measured shape from the audit, pinned by value rather than by
    comparing two engines that could drift together."""
    idx = next(i for i, (l, _) in enumerate(PAYLOADS) if l == "occurrence: full")
    ids = [t["id"] for t in _py_tables(PAYLOADS[idx][1])]
    assert ids == ["columns", "occurrences", "legend"]
    js_ids = [t["id"] for t in _JS[idx]]
    assert js_ids == ids, (
        f"the browser produced {js_ids} — the unconditional range-chart "
        f"fallback is leaking phantom empty tables into an occurrence matrix"
    )
    # And no phantom empties at all: the old fallback added four.
    assert not (set(js_ids) & SHELLS)


def test_every_declared_i18n_key_resolves_in_every_language():
    """A config whose `cols` key is missing from the tables renders as the raw
    dotted key in the GUI (`Translator.t` returns the key verbatim) or as
    `[?col.x]` in the browser (js/i18n.js t()). Both are user-visible, and
    both engines have to be fixed for either to go away.

    Only KEYS are checked. A generic table legitimately carries humanised
    literals for columns no dictionary can anticipate — `_col_label` /
    `rcaColLabel` both produce `Topic` for an invented `topic` column, and
    `Translator.t('Topic')` returning `'Topic'` is correct there, not a leak.
    """
    from rca_core.i18n import TRANSLATIONS, Translator

    keys = set()
    for _, data in PAYLOADS:
        for c in exporter.get_configs_for_result(data):
            keys.add(c["title_key"])
            keys.update(c["cols"])
    keys = {k for k in keys if k.startswith(("col.", "sec."))}
    assert keys, "no i18n keys collected — the collection above is broken"

    for lang in ("zh", "en", "ja"):
        tr = Translator(lang)
        for key in sorted(keys):
            assert tr.t(key) != key, (
                f"{key!r} is referenced by a table config but absent from "
                f"the {lang} table; Translator.t returns the key itself, so "
                f"the user sees the raw dotted key"
            )
    en = TRANSLATIONS["en"]
    for key in sorted(keys):
        assert key in en, f"{key!r} has no English fallback either"
