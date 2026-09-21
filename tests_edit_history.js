// ===========================================================================
// tests_edit_history.js — 网页端表格编辑 + 撤销栈 (FE-BORROW-2026-09-20, 域T)
// ===========================================================================
//
// Run with:  node tests_edit_history.js
//
// Three coverage groups:
//
//   1. THE DIRTY MODEL, mirrored from rca_core/editable.py. `CASES` below is a
//      hand-written translation of the Python suite ./tests_editable.py — every
//      case names the Python check it copies (the `py:` field) and the Python
//      file cites nothing back, so this table IS the cross-reference. On top of
//      that the same table is replayed through the real `rca_core.editable`
//      when Python is on PATH (runPythonOracle), so the mirror cannot drift
//      silently even if nobody reads the comments.
//
//   2. UNDO / REDO (js/history.js): the three action types, the dispatch table,
//      redo-branch truncation, the disabled-state subscription and the "yield
//      to the browser inside a contenteditable" rule.
//
//   3. NAVIGATION / VALIDATION STATE MACHINE (js/table.js's DOM layer) through
//      a ~120-line DOM stub: commit on Enter, revert on Esc, arrow navigation
//      that skips read-only columns, the red frame that KEEPS focus, the
//      selection bar, J/K over the low-confidence rows, guarded rcaViz calls.
//
// js/table.js and js/history.js stay plain globals (readFileSync + vm, no ES
// modules, no top-level `document`) — that is what makes this file possible
// without jsdom.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { spawnSync } = require('child_process');

let pass = 0;
let fail = 0;
const failures = [];

function check(name, ok, detail) {
  if (ok) {
    pass += 1;
    console.log('PASS', name);
  } else {
    fail += 1;
    const line = 'FAIL ' + name + (detail === undefined || detail === true ? '' : ' — ' + detail);
    console.log(line);
    failures.push(name);
  }
}

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort()
      .map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  if (value === undefined) return 'null';
  return JSON.stringify(value);
}
function eqJson(a, b) { return canonical(a) === canonical(b); }
function deep(v) { return JSON.parse(JSON.stringify(v === undefined ? null : v)); }
function isDict(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }

// ===========================================================================
// sandbox
// ===========================================================================

// js/i18n.js logs "[i18n] missing translation: edit.*" for every key the
// editor uses before domain U lifts them into i18n.js. That is exactly the
// behaviour RCA_EDIT_STRINGS exists for, so the noise is filtered — the test
// asserts the fallback instead of the console.
function quietConsole() {
  const drop = (m) => typeof m === 'string' && m.indexOf('[i18n]') === 0;
  return {
    log: (...args) => { if (!drop(args[0])) console.log(...args); },
    warn: (...args) => { if (!drop(args[0])) console.warn(...args); },
    error: (...args) => console.error(...args),
  };
}

function makeContext() {
  const docEl = {
    lang: 'zh-CN',
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: {},
  };
  const ctx = {
    console: quietConsole(),
    setTimeout, clearTimeout,
    document: {
      documentElement: docEl,
      addEventListener() {}, removeEventListener() {},
      querySelector: () => null,
      querySelectorAll: () => [],
      getElementById: () => null,
      createElement: (tag) => {
        // FE-BORROW-2026-09-20: table.js appends document.createElement nodes
        // into the same tree as the makeEl fixtures, and the test-side
        // querySelectorAll walker calls .matches on every child - a bare
        // literal here silently became "not a function" mid-tree. Build real
        // makeEl elements (hoisted function declaration, safe at call time).
        const el = makeEl(tag);
        return el;
      },
      body: {},
    },
    window: { addEventListener() {}, removeEventListener() {} },
    navigator: { language: 'zh-CN' },
    location: { protocol: 'http:', host: 'localhost', pathname: '/' },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    URL: require('url').URL,
    Blob: class { constructor() {} },
  };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  // js/config.js is not part of 域T, but js/i18n.js:rcaSetLang persists the
  // locale through its store helpers (i18n.js:943 -> rcaStoreSet(RCA_STORE.lang,
  // lang)), so the language cases in section 7 need the real module rather than
  // a stub that would hide a rename.
  for (const f of ['js/config.js', 'js/i18n.js', 'js/ics_table.js', 'js/reason-codes.js',
    'js/table.js', 'js/history.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), ctx, { filename: f });
  }
  const run = (code) => vm.runInContext(code, ctx);
  const names = [
    'rcaTableEdits', 'rcaRenderResults', 'rcaTableConfigs', 'rcaRowsForTable',
    'rcaCaptureEdits', 'rcaCaptureListEdits', 'rcaApplyEdits', 'rcaIsDirtyEdits',
    'rcaNewRowTemplate', 'rcaValidateCell', 'rcaColEditSpec', 'rcaEditFieldOf',
    'rcaEditScalarField', 'rcaEditT', 'rcaHistoryT', 'RCA_EDIT_I18N_KEYS',
    'RCA_EDIT_STRINGS', 'RCA_EDIT_LIST_KEYS', 'RCA_LOWCONF_THRESHOLD',
    'rcaTableLowConfidenceRows', 'rcaNextLowConfidenceRow', 'rcaRowAgreementBand',
    'rcaTableEditAttach', 'rcaTableEditDetach', 'rcaTableHighlightRow',
    'rcaEditCommitCell', 'rcaEditHandleKey', 'rcaEditNavigate', 'rcaEditRevertCell',
    'rcaEditRefFromCell', 'rcaEditSelectionAction', 'rcaEditStepLowConfidence',
    'rcaEditLocateRow', 'rcaTableEditAfterHistory', 'rcaCoerceCellValue',
    'rcaCoerceVal', 'rcaPyEqual', 'rcaPyNotEqual', 'rcaIsEmptyModelValue',
    'RCA_EDIT_DOM', 'rcaHistory', 'rcaHistoryCreate', 'rcaHistoryUndoers',
    'rcaHistoryValidAction', 'rcaHistoryInEditable', 'rcaHistorySyncButtons',
    'rcaHistoryAttachUi', 'rcaHistoryWireClicksOnce',
    'RCA_HISTORY_TYPES', 'RCA_HISTORY_STRINGS', 'rcaExportCellText',
    'rcaIcsLookupStage', 'rcaSetLang',
    // FE-BORROW-2026-09-21: seams the audit-item regression section drives —
    // the Python str(float)/str(bool) mirrors (items 13/10), the PEP-515
    // numeric grammar (item 12), the paste guard (item 9), the foreign-slot
    // capture (item 2), the plain re-render entry point and the neighbour
    // scan that used to run off the column end (item 16).
    'rcaPyFloatStr', 'rcaEditPyStr', 'rcaPyParseNumber', 'rcaEditOnPaste',
    'rcaEditForeignCapture', 'rcaTableRerender', 'rcaEditNeighbourCell',
  ];
  const out = { __ctx: ctx, __run: run };
  for (const n of names) {
    try { out[n] = run(n); } catch (e) { out[n] = undefined; }
  }
  for (const n of names) {
    if (out[n] === undefined) throw new Error('sandbox is missing ' + n + ' — js/table.js or js/history.js changed shape');
  }
  return out;
}

const S = makeContext();

// ===========================================================================
// fixtures
// ===========================================================================

// A range-chart result with two runs (so `agreement` appears) and one row that
// carries a low confidence (so J/K has somewhere to walk).
const FIX = {
  runs: 2,
  sections: [{ name: 'S1', age_range: 'Cambrian', formations: ['F1'],
    formation_thickness_m: '', coordinates: '' }],
  species_ranges: [
    { species: 'A', section: 'S1', range_base: '1', range_top: '3', biozone: 'Z1', agreement: '2/2' },
    { species: 'B', section: 'S1', range_base: '2', range_top: '4', biozone: 'Z2', agreement: '1/2', confidence: 0.4 },
  ],
  biozones: [{ name: 'Z1', section: 'S1', age: 'Cambrian', thickness_m: 10 }],
  other_fossils: ['Ammonite: X'],
};

const FIX_COLUMNAR = {
  chart_mode: 'columnar_section',
  sections: [{
    id: 'S1', name: 'S1',
    lithology_blocks: [{ pattern: 'mud', range_top_idx: 3, range_base_idx: 1 }],
    age_units: [{ name: 'U1', top_m: 1, base_m: 2 }],
    samples: [{ label: 'A', depth_m: 1.5 }],
  }],
};

const MODE_FIXTURES = {
  range_chart: FIX,
  range_chart_single: { runs: 1, sections: [{ name: 'S1' }], biozones: [],
    species_ranges: [{ species: 'A', section: 'S1', range_base: '1', range_top: '3' }] },
  columnar: FIX_COLUMNAR,
  abundance: { chart_mode: 'abundance_diagram', sites: [{ name: 'S1' }],
    abundances: [{ taxon: 'T1', site: 'S1', level: 1, depth: 2, abundance: 3, abundance_unit: '%' }],
    zones: [{ name: 'Z1' }] },
  zonation: { chart_mode: 'zonation_chart', zonations: [{ name: 'Z1' }],
    zones: [{ name: 'Z1', age: 'Cambrian' }], correlations: [{ id: 'S1', zone: 'Z1' }] },
  tree: { chart_mode: 'phylogenetic_tree', nodes: [{ id: 'N1', taxon: 'A' }] },
};

function cfgOf(data, id) {
  for (const cfg of S.rcaTableConfigs(data) || []) if (cfg.id === id) return cfg;
  return null;
}
function fieldCol(cfg, field) {
  return (cfg.edit || []).findIndex((s) => s && s.field === field);
}

// ===========================================================================
// 1. the editable.py mirror — hand-written case table + live Python oracle
// ===========================================================================
//
// __RCA_EDIT_CASES_BEGIN__
// `py` = the check(s) in ./tests_editable.py this row mirrors. Keep the two in
// step; a pytest drift guard can extract this table the way
// tests_export_parity.js extracts __RCA_WPD_CASES__.
const CASES = [
  { id: 'cap-clean', py: 'cap-clean', op: 'capture',
    before: { species_ranges: [{ species: 'A', section: 'S1' }] },
    after: { species_ranges: [{ species: 'A', section: 'S1' }] },
    expect: {} },

  { id: 'cap-one-cell', py: 'cap-one-row + cap-one-col', op: 'capture',
    before: { species_ranges: [{ species: 'A', section: 'S1' }] },
    after: { species_ranges: [{ species: 'A', section: 'S1', range_base: 'Bed 1' }] },
    expect: { species_ranges: { 0: { range_base: 'Bed 1' } } } },

  { id: 'cap-two-rows', py: 'cap-row-0 + cap-row-1', op: 'capture',
    before: { species_ranges: [{ species: 'A', section: 'S1' }, { species: 'B', section: 'S2' }] },
    after: { species_ranges: [{ species: 'A*', section: 'S1' }, { species: 'B', section: 'S2*' }] },
    expect: { species_ranges: { 0: { species: 'A*' }, 1: { section: 'S2*' } } } },

  { id: 'cap-new-row', py: 'cap-new-row-key + cap-new-row-shape', op: 'capture',
    before: { species_ranges: [{ species: 'A' }] },
    after: { species_ranges: [{ species: 'A' }, { species: 'B' }] },
    expect: { species_ranges: { new_1: { species: 'B' } } } },

  { id: 'cap-string-row', py: 'cap-string-row-not-empty + -edit-list + -edit-value', op: 'capture',
    before: { other_fossils: ['Ammonite: X', 'Conodont: Y', 'Brachiopod: Z'] },
    after: { other_fossils: ['Ammonite: X2', 'Conodont: Y', 'Brachiopod: Z'] },
    expect: { other_fossils: { _replaced: ['Ammonite: X2', 'Conodont: Y', 'Brachiopod: Z'] } } },

  { id: 'cap-string-row-clean', py: 'cap-string-row-no-change', op: 'capture',
    before: { other_fossils: ['Ammonite: X'] },
    after: { other_fossils: ['Ammonite: X'] },
    expect: {} },

  { id: 'cap-string-row-dirty', py: 'cap-string-row-is-dirty', op: 'dirty',
    before: { other_fossils: ['Ammonite: X'] },
    after: { other_fossils: ['Ammonite: Y'] },
    expect: true },

  { id: 'cap-mixed-rows', py: 'cap-mix-dict-row + cap-mix-dict-row-val + cap-mix-scalar-replaced', op: 'capture',
    before: { sections: [{ name: 'A' }, 'Bare string'] },
    after: { sections: [{ name: 'A2' }, 'Bare string edited'] },
    expect: { sections: { 0: { name: 'A2' }, _replaced: [{ name: 'A2' }, 'Bare string edited'] } } },

  { id: 'cap-whitespace', py: 'cap-whitespace-equal', op: 'capture',
    before: { species_ranges: [{ species: 'A', section: 'S1' }] },
    after: { species_ranges: [{ species: '  A  ', section: ' S1' }] },
    expect: {} },

  { id: 'dirty-true', py: 'dirty-true', op: 'dirty',
    before: { species_ranges: [{ species: 'A' }] },
    after: { species_ranges: [{ species: 'A*' }] },
    expect: true },

  { id: 'dirty-false', py: 'dirty-false', op: 'dirty',
    before: { species_ranges: [{ species: 'A' }] },
    after: { species_ranges: [{ species: 'A' }] },
    expect: false },

  // editable.py:192-196 — an added key compares against None, so an added
  // EMPTY string still counts as an edit ("" != None in Python).
  { id: 'cap-added-empty-string', py: 'editable.py:192-196 (no Python case)', op: 'capture',
    before: { species_ranges: [{ species: 'A' }] },
    after: { species_ranges: [{ species: 'A', note: '' }] },
    expect: { species_ranges: { 0: { note: '' } } } },

  // editable.py:197-199 — the deletion branch.
  { id: 'cap-deleted-key', py: 'editable.py:197-199 (no Python case)', op: 'capture',
    before: { species_ranges: [{ species: 'A', biozone: 'Z1' }] },
    after: { species_ranges: [{ species: 'A' }] },
    expect: { species_ranges: { 0: { _deleted_keys: ['biozone'] } } } },

  // editable.py:187 — `_extras` (the normalizer's scratch space) never edits.
  { id: 'cap-extras-skipped', py: 'editable.py:187 (mirrors apply-extras-unchanged)', op: 'capture',
    before: { species_ranges: [{ species: 'A', _extras: { k: 'v' } }] },
    after: { species_ranges: [{ species: 'A', _extras: { k: 'CHANGED' } }] },
    expect: {} },

  // A nested columnar edit must be visible to the `sections` row diff, because
  // the list value compares DEEP (why rcaPyEqual is recursive).
  { id: 'cap-nested-deep', py: 'editable.py:188-191 deep compare', op: 'capture',
    before: { sections: [{ id: 'S1', lithology_blocks: [{ pattern: 'mud', range_top_idx: 3 }] }] },
    after: { sections: [{ id: 'S1', lithology_blocks: [{ pattern: 'wack', range_top_idx: 3 }] }] },
    expect: { sections: { 0: { lithology_blocks: [{ pattern: 'wack', range_top_idx: 3 }] } } } },

  // bool / None corners: Python says True == 1 and None == None.
  { id: 'cap-bool-equal', py: 'editable.py:_coerce + != semantics', op: 'capture',
    before: { species_ranges: [{ species: 'A', confirmed: true }] },
    after: { species_ranges: [{ species: 'A', confirmed: 1 }] },
    expect: {} },

  { id: 'cap-none-vs-empty', py: 'editable.py:192-196', op: 'capture',
    before: { species_ranges: [{ species: 'A', note: null }] },
    after: { species_ranges: [{ species: 'A', note: '' }] },
    expect: { species_ranges: { 0: { note: '' } } } },

  { id: 'apply-cell', py: 'apply-cell', op: 'apply',
    result: { species_ranges: [{ species: 'A', section: 'S1' }] },
    edits: { species_ranges: { 0: { species: 'A*' } } },
    expect: { species_ranges: [{ species: 'A*', section: 'S1' }] } },

  { id: 'apply-new-row', py: 'apply-new-len + apply-new-val', op: 'apply',
    result: { species_ranges: [{ species: 'A' }] },
    edits: { species_ranges: { new_1: { species: 'B' } } },
    expect: { species_ranges: [{ species: 'A' }, { species: 'B' }] } },

  { id: 'apply-out-of-range', py: 'apply-oor-len', op: 'apply',
    result: { species_ranges: [{ species: 'A' }] },
    edits: { species_ranges: { 99: { species: 'B' } } },
    expect: { species_ranges: [{ species: 'A' }] } },

  { id: 'apply-extras', py: 'apply-extras-unchanged', op: 'apply',
    result: { species_ranges: [{ species: 'A', _extras: { k: 'v' } }] },
    edits: { species_ranges: { 0: { _extras: { k: 'X' } } } },
    expect: { species_ranges: [{ species: 'A', _extras: { k: 'v' } }] } },

  { id: 'apply-string-replaced', py: 'apply-string-row-len + -types + -content', op: 'apply',
    result: { other_fossils: ['Ammonite: X', 'Conodont: Y'] },
    edits: { other_fossils: { _replaced: ['Ammonite: Z', 'Conodont: Y', 'Brachiopod: W'] } },
    expect: { other_fossils: ['Ammonite: Z', 'Conodont: Y', 'Brachiopod: W'] } },

  { id: 'apply-deleted-key', py: 'editable.py:285-288', op: 'apply',
    result: { species_ranges: [{ species: 'A', biozone: 'Z1' }] },
    edits: { species_ranges: { 0: { _deleted_keys: ['biozone'] } } },
    expect: { species_ranges: [{ species: 'A' }] } },

  // editable.py:271-275 — a scalar row hit by cell edits is promoted to
  // {'value': item} so nothing is lost.
  { id: 'apply-scalar-promote', py: 'editable.py:271-275', op: 'apply',
    result: { sections: ['Bare string'] },
    edits: { sections: { 0: { name: 'S1' } } },
    expect: { sections: [{ value: 'Bare string', name: 'S1' }] } },

  // editable.py:291-299 — insertions replay ASCENDING and clamped (list.insert).
  { id: 'apply-insert-order', py: 'editable.py:291-299', op: 'apply',
    result: { species_ranges: [{ species: 'A' }] },
    edits: { species_ranges: { new_99: { species: 'C' }, new_0: { species: 'B' } } },
    expect: { species_ranges: [{ species: 'B' }, { species: 'A' }, { species: 'C' }] } },

  { id: 'apply-replaced-wins', py: 'editable.py:255-259', op: 'apply',
    result: { other_fossils: ['x'] },
    edits: { other_fossils: { _replaced: ['a', 'b'], 0: { nope: 1 } } },
    expect: { other_fossils: ['a', 'b'] } },

  { id: 'apply-unknown-key', py: 'editable.py:246-249 (only _LIST_KEYS)', op: 'apply',
    result: { species_ranges: [{ species: 'A' }], quality: { score: 0.5 } },
    edits: { quality: { 0: { score: 0.9 } }, nope_key: { 0: { x: 1 } } },
    expect: { species_ranges: [{ species: 'A' }], quality: { score: 0.5 } } },
];
// __RCA_EDIT_CASES_END__

function jsCaseResult(c) {
  if (c.op === 'capture') return S.rcaCaptureEdits(deep(c.before), deep(c.after));
  if (c.op === 'dirty') {
    return S.rcaIsDirtyEdits(S.rcaCaptureEdits(deep(c.before), deep(c.after)));
  }
  return S.rcaApplyEdits(deep(c.result), deep(c.edits));
}

for (const c of CASES) {
  const got = jsCaseResult(c);
  const ok = c.op === 'dirty' ? (got === c.expect) : eqJson(got, c.expect);
  check('mirror-' + c.id, ok, ok ? undefined
    : 'js=' + canonical(got) + ' expected=' + canonical(c.expect) + ' (py: ' + c.py + ')');
}

// Round-trip property: apply_edits(snapshot, capture_edits(snapshot, edited))
// reproduces the edited model — the contract the Qt "Apply" button relies on,
// and the reason the browser can send the identical payload.
for (const c of CASES.filter((x) => x.op === 'capture' && Object.keys(x.expect).length > 0)) {
  const replayed = S.rcaApplyEdits(deep(c.before), deep(c.expect));
  check('roundtrip-' + c.id, eqJson(replayed, c.after),
    'got=' + canonical(replayed) + ' want=' + canonical(c.after));
}

function runPythonOracle() {
  const script = [
    'import json, sys',
    'sys.path.insert(0, ".")',
    'from rca_core.editable import capture_edits, apply_edits, is_dirty',
    'out = []',
    'for c in json.loads(sys.argv[1]):',
    '    try:',
    '        if c["op"] == "capture":',
    '            out.append(capture_edits(c["before"], c["after"]))',
    '        elif c["op"] == "dirty":',
    '            out.append(is_dirty(c["before"], c["after"]))',
    '        else:',
    '            d = json.loads(json.dumps(c["result"]))',
    '            apply_edits(d, c["edits"])',
    '            out.append(d)',
    '    except Exception as exc:',
    '        out.append({"__error__": type(exc).__name__})',
    'print(json.dumps(out))',
  ].join('\n');
  const payload = CASES.map((c) => ({ id: c.id, op: c.op, before: c.before, after: c.after,
    result: c.result, edits: c.edits }));
  const py = process.platform === 'win32' ? 'python' : 'python3';
  let res = null;
  try {
    res = spawnSync(py, ['-c', script, JSON.stringify(payload)],
      { encoding: 'utf8', cwd: __dirname, timeout: 60000 });
  } catch (e) { return null; }
  if (!res || res.status !== 0 || !res.stdout || !res.stdout.trim()) return null;
  try { return JSON.parse(res.stdout); } catch (e) { return null; }
}

const ORACLE = runPythonOracle();
if (ORACLE && ORACLE.length === CASES.length) {
  check('oracle-python-ran', true);
  const bad = [];
  CASES.forEach((c, i) => {
    const py = ORACLE[i];
    if (py && py.__error__) { bad.push(c.id + ': python raised ' + py.__error__); return; }
    const js = jsCaseResult(c);
    if (!eqJson(js, py)) bad.push(c.id + ' js=' + canonical(js) + ' py=' + canonical(py));
  });
  check('oracle-all-cases-agree', bad.length === 0, bad.join(' | '));
} else {
  console.log('SKIP oracle-python-ran / oracle-all-cases-agree (python or rca_core unavailable)');
}

// ---- Python value semantics the diff depends on --------------------------
check('pyeq-true-vs-one', S.rcaPyEqual(true, 1) === true, 'Python True == 1');
check('pyeq-none-vs-undefined', S.rcaPyEqual(null, undefined) === true);
check('pyeq-empty-string-not-none', S.rcaPyEqual('', null) === false, "Python '' != None");
check('pyeq-deep-list', S.rcaPyEqual([1, [2, 3]], [1, [2, 3]]) === true);
check('pyeq-dict-order-irrelevant', S.rcaPyEqual({ a: 1, b: 2 }, { b: 2, a: 1 }) === true);
check('pyeq-dict-vs-missing-key', S.rcaPyEqual({ a: 1 }, {}) === false);
check('coerce-int-truncates', S.rcaCoerceCellValue('3.9', 'int') === 3, 'int(float("3.9"))');
check('coerce-underscore-int', S.rcaCoerceCellValue('1_000', 'int') === 1000, 'PEP 515 grammar');
check('coerce-rejects-hex', S.rcaCoerceCellValue('0x10', 'int') === '0x10', 'int("0x10") raises');
check('coerce-keeps-unparsable', S.rcaCoerceCellValue('5m', 'number') === '5m',
  'exporter.py:_coerce_cell keeps the typed text');
check('coerce-str-leaves-text', S.rcaCoerceCellValue('  A  ', 'str') === '  A  ',
  'exporter.py:_coerce_cell docstring: "The default (str) leaves the value '
  + 'unchanged". The trim that decides dirty-ness lives one layer up, in '
  + 'editable.py:_coerce (mirrored by rcaCoerceVal) — see rcaCoerceVal-trims.');
check('rcaCoerceVal-trims', S.rcaCoerceVal('  A  ') === 'A'
  && eqJson(S.rcaCoerceVal(['  A  ']), ['  A  ']) && S.rcaCoerceVal(7) === 7,
  'editable.py:_coerce "trim whitespace; pass through everything else" — '
  + 'isinstance(str) only, so lists and numbers ride untouched');
check('coerce-list-splits', eqJson(S.rcaCoerceCellValue('F1; F2;', 'list'), ['F1', 'F2']));
check('coerce-bool-yn', S.rcaCoerceCellValue('Y', 'bool_yn') === true
  && S.rcaCoerceCellValue('n', 'bool_yn') === false);
check('coerce-nullable-empty', S.rcaCoerceCellValue('  ', 'nullable_str') === null);
check('coerce-empty-numeric-null', S.rcaCoerceCellValue('', 'int') === null);
check('empty-value-test', S.rcaIsEmptyModelValue(null) && S.rcaIsEmptyModelValue('')
  && S.rcaIsEmptyModelValue([]) && !S.rcaIsEmptyModelValue(0) && !S.rcaIsEmptyModelValue('x'));

// ---- new_row_template (editable.py:new_row_template/_template_from_cfg) ----
const spCfg = cfgOf(FIX, 'species_ranges');
const secCfg = cfgOf(FIX, 'sections');
const bzCfg = cfgOf(FIX, 'biozones');
const ofCfg = cfgOf(FIX, 'other_fossils');
const tplSpecies = S.rcaNewRowTemplate(spCfg, 'species_ranges');

check('tpl-species-key', 'species' in tplSpecies, 'py: tpl-species-key');
check('tpl-section-key', 'section' in tplSpecies, 'py: tpl-section-key');
check('tpl-biozone-key', 'name' in S.rcaNewRowTemplate(bzCfg, 'biozones'), 'py: tpl-biozone-key');
check('tpl-skips-agreement', !('agreement' in tplSpecies),
  'editable.py:_template_from_cfg skips the computed column');
check('tpl-scalar-row-empty-string', S.rcaNewRowTemplate(ofCfg, 'other_fossils') === '',
  'editable.py:new_row_template -> "" for _SCALAR_LIST_KEYS');
check('tpl-null-for-numeric', S.rcaNewRowTemplate(bzCfg, 'biozones').thickness_m === null,
  'editable.py:_default_for: number/bool/nullable -> None');
check('tpl-list-default', eqJson(S.rcaNewRowTemplate(secCfg, 'sections').formations, []),
  'editable.py:_default_for: list -> []');

// ---- the edit / cols alignment contract -----------------------------------
const misaligned = [];
const badSpec = [];
let tables = 0;
// A list column used to be rejected unless it carried its own `split` key.
// No such key exists — the separator belongs to the COLUMN TYPE, exactly as in
// exporter.py:_coerce_cell ("``list`` splits on ``;`` and trims"), which
// rcaCoerceCellValue mirrors in js/table.js. What the contract really requires
// is that the type-driven splitter answers for every editable list column.
const listSplits = eqJson(S.rcaCoerceCellValue('a; b', 'list'), ['a', 'b']);
for (const mode of Object.keys(MODE_FIXTURES)) {
  for (const cfg of S.rcaTableConfigs(MODE_FIXTURES[mode]) || []) {
    tables += 1;
    if (!Array.isArray(cfg.edit) || cfg.edit.length !== cfg.cols.length) {
      misaligned.push(mode + '/' + cfg.id + ' cols=' + cfg.cols.length + ' edit=' + ((cfg.edit || []).length));
      continue;
    }
    cfg.edit.forEach((spec, i) => {
      if (!spec) { badSpec.push(mode + '/' + cfg.id + '#' + i + ': missing'); return; }
      if (!spec.scalar && !spec.field) badSpec.push(mode + '/' + cfg.id + '#' + i + ': no field');
      if (['str', 'number', 'int', 'float', 'list', 'bool_yn', 'nullable_str'].indexOf(spec.type) === -1) {
        badSpec.push(mode + '/' + cfg.id + '#' + i + ': type=' + spec.type);
      }
      if (spec.editable && spec.type === 'list' && !listSplits) {
        badSpec.push(mode + '/' + cfg.id + '#' + i + ': list column without a splitter');
      }
      if (spec.validate === 'range-pair' && !Array.isArray(spec.peer)) {
        badSpec.push(mode + '/' + cfg.id + '#' + i + ': pair validator without peer');
      }
    });
  }
}
check('edit-cols-aligned-all-modes', misaligned.length === 0, misaligned.join(' | '));
check('edit-cols-aligned-count', tables >= 18, 'only ' + tables + ' tables checked');
check('edit-spec-shape', badSpec.length === 0, badSpec.join(' | '));
check('agreement-column-readonly', (spCfg.edit || [])
  .filter((s) => s.field === 'agreement').every((s) => s.editable === false));
check('nested-subtables-flagged', !!cfgOf(FIX_COLUMNAR, 'lithology_blocks').nested);

// ===========================================================================
// 2. the registry: capture / dirty marks / structural ops / selection
// ===========================================================================

function freshEdits(data) {
  S.rcaTableEdits.detach();
  S.rcaTableEdits.attach(data);
  return S.rcaTableEdits;
}

const d1 = deep(FIX);
freshEdits(d1);
check('registry-attached', S.rcaTableEdits.isAttached() && S.rcaTableEdits.live() === d1);
check('registry-clean', eqJson(S.rcaTableEdits.captureAll(), {}));

const e1 = S.rcaTableEdits.editCell('species_ranges', 0, 0, 'A*');
check('editcell-ok', e1.ok === true && e1.changed === true);
check('editcell-action', isDict(e1.action) && e1.action.type === 'cellEdit'
  && e1.action.tableId === 'species_ranges' && e1.action.row === 0 && e1.action.col === 0
  && e1.action.field === 'species' && e1.action.before === 'A' && e1.action.after === 'A*'
  && e1.action.hadKey === true, canonical(e1.action));
check('editcell-writes-model', d1.species_ranges[0].species === 'A*');
check('editcell-capture-mirror', eqJson(S.rcaTableEdits.capture('species_ranges'),
  { 0: { species: 'A*' } }), 'cap-one-row / cap-one-col shape');
check('editcell-dirty-mark', S.rcaTableEdits.editedCount() === 1
  && S.rcaTableEdits.isCellEdited('species_ranges', 0, 'species'));
check('registry-isdirty', S.rcaTableEdits.isDirty() === true);

const e2 = S.rcaTableEdits.editCell('species_ranges', 0, 0, '  A*  ');
check('editcell-same-value-noop', e2.ok === true && e2.changed === false && e2.action === null,
  'editable.py:_coerce trims before comparing');
check('editcell-whitespace-not-dirty', S.rcaTableEdits.editedCount() === 1);

S.rcaTableEdits.editCell('species_ranges', 0, 0, 'A');
check('editcell-revert-uncleans', eqJson(S.rcaTableEdits.capture('species_ranges'), {})
  && S.rcaTableEdits.editedCount() === 0,
  'back to the snapshot value: no phantom edit, no phantom frame');

S.rcaTableEdits.editCell('species_ranges', 0, 4, '');
check('editcell-clear-removes-key', !('biozone' in d1.species_ranges[0]),
  canonical(d1.species_ranges[0]));
check('editcell-clear-deleted-keys', eqJson(S.rcaTableEdits.capture('species_ranges'),
  { 0: { _deleted_keys: ['biozone'] } }), 'editable.py:197-199');

S.rcaTableEdits.setCellEdited('species_ranges', 1, 'note');
check('registry-setcelledited', S.rcaTableEdits.getEditedCells('species_ranges')
  .some((p) => p[0] === 1 && p[1] === 'note'));
S.rcaTableEdits.clearCellEdited('species_ranges', 1, 'note');
check('registry-cleared-cell', !S.rcaTableEdits.getEditedCells('species_ranges')
  .some((p) => p[0] === 1 && p[1] === 'note'));
S.rcaTableEdits.clearEdited();
check('registry-clearEdited', S.rcaTableEdits.editedCount() === 0
  && Object.keys(S.rcaTableEdits._edited).length === 0);

const d2 = deep(FIX);
freshEdits(d2);
d2.species_ranges[1].species = 'B*';
d2.species_ranges.push({ species: 'C', section: 'S1', range_base: '1', range_top: '2' });
const payload = S.rcaTableEdits.captureAll();
check('captureall-new-row', isDict(payload.species_ranges) && !!payload.species_ranges.new_2,
  canonical(payload));
const applied = S.rcaTableEdits.applyEdits(deep(FIX), deep(payload));
check('applyedits-roundtrip', eqJson(applied.species_ranges, d2.species_ranges),
  'capture -> apply must reproduce the live model');

const d3 = deep(FIX);
freshEdits(d3);
const sc = S.rcaTableEdits.editCell('other_fossils', 0, 0, 'Ammonite: Y');
check('scalar-edit-ok', sc.ok === true && d3.other_fossils[0] === 'Ammonite: Y');
check('scalar-edit-field', sc.action.field === S.rcaEditScalarField());
check('scalar-edit-capture', eqJson(S.rcaTableEdits.capture('other_fossils'),
  { _replaced: ['Ammonite: Y'] }), 'editable.py:171-175 scalar rows ride as _replaced');

const d4 = deep(FIX_COLUMNAR);
freshEdits(d4);
const nested = S.rcaTableEdits.editCell('lithology_blocks', 0, 2, '9');
check('nested-edit-applied', nested.ok === true
  && d4.sections[0].lithology_blocks[0].range_top_idx === 9,
  canonical(d4.sections[0].lithology_blocks[0]));
check('nested-capture-via-parent', eqJson(S.rcaTableEdits.capture('lithology_blocks'),
  { 0: { lithology_blocks: [{ pattern: 'mud', range_top_idx: 9, range_base_idx: 1 }] } }),
  'the diff rides on the sections row (editable.py deep list compare)');
check('nested-structural-disabled', S.rcaTableEdits.canDeleteRows('lithology_blocks') === false
  && S.rcaTableEdits.deleteRow('lithology_blocks', 0) === null
  && S.rcaTableEdits.addRow('lithology_blocks') === null);
check('nested-undo-clears-dirty', (function () {
  S.rcaHistory.clear();
  S.rcaHistory.push(nested.action);
  const ok = S.rcaHistory.undo() !== null && d4.sections[0].lithology_blocks[0].range_top_idx === 3;
  return ok && S.rcaTableEdits.editedCount() === 0
    && eqJson(S.rcaTableEdits.capture('lithology_blocks'), {});
})(), 'undoing a nested edit back to the snapshot must clear its frame');

const d5 = deep(FIX);
freshEdits(d5);
const del = S.rcaTableEdits.deleteRow('species_ranges', 0);
check('deleterow-action', isDict(del) && del.type === 'rowDelete' && del.row === 0
  && del.item.species === 'A');
check('deleterow-model', d5.species_ranges.length === 1 && d5.species_ranges[0].species === 'B');
check('deleterow-capture-replaced', eqJson(S.rcaTableEdits.capture('species_ranges'),
  { _replaced: deep(d5.species_ranges) }), 'editable.py:147-154 shrink -> _replaced');
check('undedeleterow', S.rcaTableEdits.undoDeleteRow('species_ranges', 0, del.item) === true
  && d5.species_ranges.length === 2 && d5.species_ranges[0].species === 'A');
const add = S.rcaTableEdits.addRow('species_ranges');
check('addrow-action', isDict(add) && add.type === 'rowAdd' && add.row === 2);
check('addrow-template', d5.species_ranges[2].species === '' && !('agreement' in d5.species_ranges[2]));
check('addrow-capture-new', eqJson(S.rcaTableEdits.capture('species_ranges'),
  { new_2: d5.species_ranges[2] }), 'editable.py:205-209 appended dict rows -> new_<i>');
check('undoaddrow', S.rcaTableEdits.undoAddRow('species_ranges', 2) !== false
  && d5.species_ranges.length === 2);
check('addrow-index-honoured', (function () {
  const a = S.rcaTableEdits.addRow('sections', 0);
  const ok = a && a.row === 0 && d5.sections[0].name === '';
  S.rcaTableEdits.undoAddRow('sections', 0);
  return !!ok;
})());

const d6 = deep(FIX);
freshEdits(d6);
S.rcaTableEdits.toggleRow('species_ranges', 0);
S.rcaTableEdits.toggleRow('species_ranges', 1);
check('selection-toggle', eqJson(S.rcaTableEdits.selectedRows('species_ranges'), [0, 1]));
S.rcaTableEdits.toggleRow('species_ranges', 0);
check('selection-untoggle', eqJson(S.rcaTableEdits.selectedRows('species_ranges'), [1]));
S.rcaTableEdits.clearSelection('species_ranges');
S.rcaTableEdits.selectAll('species_ranges', d6.species_ranges, true);
check('selection-all', eqJson(S.rcaTableEdits.selectedRows('species_ranges'), [0, 1]));
check('selection-has', S.rcaTableEdits.hasSelection('species_ranges') === true);
const exp = S.rcaTableEdits.exportRows('species_ranges', [1]);
check('selection-export-rows', exp.rows.length === 1 && exp.rows[0][0] === '2'
  && exp.rows[0][1] === 'B', canonical(exp.rows));
check('selection-export-headers', exp.headers.length === d6.species_ranges[0]
  ? exp.headers.length >= 6 : exp.headers.length >= 6, canonical(exp.headers));
S.rcaTableEdits.clearSelection();
check('selection-cleared', Object.keys(S.rcaTableEdits._selection).length === 0);
check('selection-shift-on-delete', (function () {
  S.rcaTableEdits.toggleRow('species_ranges', 1);
  S.rcaTableEdits.deleteRow('species_ranges', 0);
  const ok = eqJson(S.rcaTableEdits.selectedRows('species_ranges'), [0]);
  S.rcaTableEdits.clearSelection();
  return ok;
})(), 'a delete must not leave the selection pointing at the wrong row');

// ===========================================================================
// 3. validators
// ===========================================================================

const confCol = fieldCol(spCfg, 'confidence');
const baseCol = fieldCol(spCfg, 'range_base');
const confSpec = S.rcaColEditSpec(spCfg, confCol);
check('fixture-columns-resolved', confCol === 5 && fieldCol(spCfg, 'agreement') === 6
  && baseCol === 2, 'conf=' + confCol + ' agree=' + fieldCol(spCfg, 'agreement'));
check('valid-range-pair-spec', baseCol.validate === undefined);
check('valid-str-accepts-anything', S.rcaValidateCell(S.rcaColEditSpec(spCfg, 0), 'x', {}).ok === true);
check('valid-float-rejects-text', S.rcaValidateCell(confSpec, 'abc', {}).ok === false);
check('valid-float-rejects-text-key', S.rcaValidateCell(confSpec, 'abc', {}).key === 'edit.cellNotNumber');
check('valid-float-accepts-number', S.rcaValidateCell(confSpec, '0.7', {}).ok === true);
check('valid-float-accepts-empty', S.rcaValidateCell(confSpec, '', {}).ok === true,
  'clearing a field is a legal correction (it travels as _deleted_keys)');
check('valid-range-pair-inverted', S.rcaValidateCell(S.rcaColEditSpec(spCfg, 2), '5',
  { range_top: '2', range_base: '5' }).ok === false, 'top 2 younger-side of base 5');
check('valid-range-pair-ok', S.rcaValidateCell(S.rcaColEditSpec(spCfg, 2), '1',
  { range_top: '3', range_base: '1' }).ok === true);
check('valid-range-age-inverted', S.rcaValidateCell(S.rcaColEditSpec(spCfg, 2), '100 Ma',
  { range_top: '120 Ma', range_base: '100 Ma' }).ok === false,
  'js/table.js:1575 rcaRangePairInverted: baseMa < topMa — a base YOUNGER than '
  + 'its top has the older end on top of the younger one');
check('valid-range-age-ok', S.rcaValidateCell(S.rcaColEditSpec(spCfg, 2), '120 Ma',
  { range_top: '100 Ma', range_base: '120 Ma' }).ok === true,
  'the geologically valid pair (120 Ma below, 100 Ma above) must NOT be refused');
check('valid-range-uncomparable-ok', S.rcaValidateCell(S.rcaColEditSpec(spCfg, 2), 'limestone',
  { range_top: '', range_base: '' }).ok === true);
check('valid-range-error-params', (S.rcaValidateCell(S.rcaColEditSpec(spCfg, 2), '5',
  { range_top: '2', range_base: '5' }).text || '').indexOf('2') !== -1);
const stageSpec = { field: 'name', type: 'str', editable: true, validate: 'stage' };
check('valid-stage-known', S.rcaValidateCell(stageSpec, 'Aalenian', {}).ok === true,
  'RCA_ICS_TABLE lookup: ' + S.rcaIcsLookupStage('Aalenian'));
check('valid-stage-alias', S.rcaValidateCell(stageSpec, 'jurassic', {}).ok === true,
  'period alias via RCA_ICS_PERIOD_NAMES');
check('valid-stage-bogus', S.rcaValidateCell(stageSpec, 'Notastage', {}).key === 'edit.badStage');
// quality.js:_INFORMAL_STAGE_RE (js/quality.js:207) is
// `^(unnumbered|unnamed|stage)\s+[\dxvi]+$` — ONE keyword then a number, so
// "Stage IV" / "Unnumbered 4" are the informal spellings it waves through.
// "Unnumbered Stage III" is two words after the keyword and matches nowhere,
// i.e. it stays a bad stage in quality.js too; the old case asserted the
// opposite of the rule it cited.
check('valid-stage-informal', S.rcaValidateCell(stageSpec, 'Stage IV', {}).ok === true
  && S.rcaValidateCell(stageSpec, 'Unnumbered 4', {}).ok === true
  && S.rcaValidateCell(stageSpec, 'Unnumbered Stage III', {}).ok === false,
  'quality.js allows informal stages, in exactly its own spelling');
check('valid-stage-informal-regex-shared', (function () {
  const src = ['js/quality.js', 'js/table.js'].map((f) => {
    const text = fs.readFileSync(path.join(__dirname, f), 'utf8');
    const m = text.match(/INFORMAL_STAGE_RE\s*=\s*(\/[^\n]*?\/[a-z]*)/);
    return m ? m[1] : null;
  });
  return !!src[0] && src[0] === src[1];
})(), 'the editor must not grow its own idea of "informal"');
const dv = deep(FIX);
freshEdits(dv);
const bad = S.rcaTableEdits.editCell('species_ranges', 1, confCol, 'not-a-number');
check('editcell-rejects-bad-number', bad.ok === false
  && dv.species_ranges[1].confidence === 0.4, 'model untouched on invalid input');
check('editcell-invalid-text-localized', typeof bad.text === 'string' && bad.text.length > 0
  && bad.text.indexOf('[?') !== 0, bad.text);
check('editcell-readonly-column-refused', S.rcaTableEdits
  .editCell('species_ranges', 0, fieldCol(spCfg, 'agreement'), 'x').ok === false);
check('editcell-no-row-refused', S.rcaTableEdits.editCell('species_ranges', 99, 0, 'x').ok === false);
check('lowconf-threshold-used', S.rcaTableLowConfidenceRows(FIX, 'species_ranges').join(',') === '1',
  'row 1 has confidence 0.4 < ' + S.RCA_LOWCONF_THRESHOLD);
check('agreement-band-matches-pill', (function () {
  // L2 (REVIEW-2026-09-20, js/table.js:691) put the row background and the
  // agreement pill on ONE integer rule: good 3k>=2n, mid 3k>n, low 3k<=n.
  // 1/2 is therefore "mid" (3>2), NOT "low" — the old expectation predated L2
  // (it is exactly the "amber pill on a red review band" disagreement L2
  // removed). Assert all three bands agree with the rendered pill and with
  // the row that gets the review background.
  const items = [
    { species: 'A', section: 'S1', agreement: '2/2' },
    { species: 'B', section: 'S1', agreement: '1/2' },
    { species: 'C', section: 'S1', agreement: '0/2' },
  ];
  const bands = items.map((it) => S.rcaRowAgreementBand(it, 2));
  const html = S.rcaRenderResults({ runs: 2, sections: [], species_ranges: items,
    biozones: [], other_fossils: [] }, '');
  const pills = (html.match(/pill-(good|mid|low)/g) || []).map((s) => s.slice(5));
  return eqJson(bands, ['good', 'mid', 'low']) && eqJson(pills, bands)
    && (html.match(/row-low-agreement/g) || []).length === 1
    && /class="row-low-agreement" data-row="2"/.test(html);
})(), 'row band, pill class and review background must agree');
check('next-lowconf-forward', S.rcaNextLowConfidenceRow([1, 3, 7], 2, +1) === 3);
check('next-lowconf-backward', S.rcaNextLowConfidenceRow([1, 3, 7], 2, -1) === 1);
check('next-lowconf-exhausted', S.rcaNextLowConfidenceRow([1, 3], 3, +1) === -1
  && S.rcaNextLowConfidenceRow([], 0, +1) === -1);
check('next-lowconf-from-none', S.rcaNextLowConfidenceRow([2, 5], -1, +1) === 2
  && S.rcaNextLowConfidenceRow([2, 5], 99, -1) === 5);

// ===========================================================================
// 4. undo / redo stack
// ===========================================================================

const H = S.rcaHistory;
H.clear();
check('hist-start-empty', H.canUndo() === false && H.canRedo() === false);
check('hist-rejects-unknown-type', H.push({ type: 'columnResize', tableId: 'species_ranges', row: 0 }) === false
  && H.canUndo() === false);
check('hist-rejects-malformed', H.push({ type: 'cellEdit', tableId: '', row: 0 }) === false
  && H.push({ type: 'cellEdit', tableId: 'species_ranges', row: -1 }) === false
  && H.push(null) === false && S.rcaHistoryValidAction({ type: 'rowAdd', tableId: 't', row: 0 }) === true);
check('hist-types-are-three', eqJson(S.RCA_HISTORY_TYPES.slice().sort(), ['cellEdit', 'rowAdd', 'rowDelete']));
check('hist-dispatch-complete', S.RCA_HISTORY_TYPES.every((k) => H.undoers[k]
  && typeof H.undoers[k].undo === 'function' && typeof H.undoers[k].redo === 'function'
  && typeof H.undoers[k].describe === 'function'));

const seen = [];
const unsub = H.subscribe((st) => seen.push([st.canUndo, st.canRedo]));
check('hist-subscribe-init', seen.length === 1 && seen[0][0] === false && seen[0][1] === false);
H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
  model: 'species', before: 'A', hadKey: true, after: 'A*' });
check('hist-push-notifies', seen.length === 2 && seen[1][0] === true && seen[1][1] === false,
  canonical(seen));

const hd = deep(FIX);
freshEdits(hd);
H.clear();
function editAndPush(tableId, row, col, text) {
  const res = S.rcaTableEdits.editCell(tableId, row, col, text);
  if (res.ok && res.changed) H.push(res.action);
  return res;
}
editAndPush('species_ranges', 0, 0, 'A*');
check('hist-undo-restores', H.undo() !== null && hd.species_ranges[0].species === 'A'
  && S.rcaTableEdits.editedCount() === 0 && H.canUndo() === false);
check('hist-redo-reapplies', H.redo() !== null && hd.species_ranges[0].species === 'A*'
  && H.canRedo() === false && H.canUndo() === true);
H.undo();
editAndPush('species_ranges', 1, 0, 'B*');
check('hist-new-action-truncates-redo', H.canRedo() === false && H.redo() === null,
  'the redo branch dies on a fork (WPD undoManager)');
H.undo(); H.undo();
check('hist-undo-twice', hd.species_ranges[0].species === 'A' && hd.species_ranges[1].species === 'B');
check('hist-empty-undo-null', H.undo() === null && H.undo() === null && H.depth() === 0);
H.redo(); H.redo();
check('hist-redo-to-end', hd.species_ranges[0].species === 'A'
  && hd.species_ranges[1].species === 'B*' && H.depth() === 1 && H.canRedo() === false,
  'the fork at :791 killed the A* branch (hist-new-action-truncates-redo), so '
  + '"redo to the end" can only ever walk the B* branch back on');

// undoing an edit that ADDED a key removes it again (hadKey === false)
const hd2 = deep(FIX);
freshEdits(hd2);
H.clear();
const addKey = editAndPush('species_ranges', 0, confCol, '0.5');
check('hist-addkey-hadkey-false', addKey.action.hadKey === false, canonical(addKey.action));
H.undo();
check('hist-addkey-undo-removes-key', !('confidence' in hd2.species_ranges[0]),
  canonical(hd2.species_ranges[0]));
check('hist-addkey-undo-clean-diff', eqJson(S.rcaTableEdits.capture('species_ranges'), {}),
  'no ghost `_deleted_keys` for a key that never existed');
H.redo();
check('hist-addkey-redo-restores', hd2.species_ranges[0].confidence === 0.5);

// a cleared cell undoes back to the ORIGINAL value, not to ''
const hd3 = deep(FIX);
freshEdits(hd3);
H.clear();
const cleared = editAndPush('species_ranges', 0, 4, '');
check('clear-action-marked', cleared.action.cleared === true);
H.undo();
check('clear-undo-restores-value', hd3.species_ranges[0].biozone === 'Z1',
  canonical(hd3.species_ranges[0]));

// rowDelete / rowAdd round-trips through the stack
const hd4 = deep(FIX);
freshEdits(hd4);
H.clear();
const delAct = S.rcaTableEdits.deleteRow('species_ranges', 1);
H.push(delAct);
check('hist-rowdelete-undo', H.undo() !== null && hd4.species_ranges.length === 2
  && hd4.species_ranges[1].species === 'B');
check('hist-rowdelete-redo', H.redo() !== null && hd4.species_ranges.length === 1);
const addAct = S.rcaTableEdits.addRow('biozones');
H.push(addAct);
check('hist-rowadd-undo', H.undo() !== null && hd4.biozones.length === 1);
check('hist-rowadd-redo', H.redo() !== null && hd4.biozones.length === 2
  && canonical(hd4.biozones[1]) === canonical(addAct.item));

// stale indices: an out-of-range INSERT undo FAILS CLOSED now (FE-FIX-2026-09-21
// audit item 1). The old expectation praised the clamp (append the row at the
// end and report success) — the audit proved that wrong: the row lands at the
// wrong index while the stack believes the exact-position restore happened.
// undoDeleteRow accepts `row === len` (Python list.insert append) and refuses
// `row > len`, which history.js classifies as transient ('retry'): the entry
// stays on the stack, the model is untouched.
H.clear();
H.push({ type: 'rowDelete', tableId: 'species_ranges', row: 99, item: { species: 'ghost' } });
check('hist-stale-insert-clamps', H.undo() === null && hd4.species_ranges.length === 1
  && H.depth() === 1,
  'out-of-range undoDeleteRow returns false (js/table.js FE-FIX item 1); the '
  + 'rowDelete/undo verb does not TOUCH a row, so it is retryable, not stale');
check('hist-stale-insert-undo', H.redo() === null && hd4.species_ranges.length === 1,
  'the failed undo never moved the action to the redo stack, so there is '
  + 'nothing to redo and no phantom ghost row (the old clamp appended one)');
H.clear();
H.push({ type: 'rowAdd', tableId: 'species_ranges', row: 400, item: { species: 'x' } });
// FE-FIX-2026-09-21 (audit, history.js item 5 contract): a rowAdd UNDO removes
// the row AT `row` — row 400 is deterministically out of range, so the entry
// can never succeed. The old expectation demanded it stay on the stack
// (depth unchanged, silent no-op forever: the button remained enabled and
// every click moved nothing). js/history.js now classifies that as 'stale'
// and POISONS the entry — popped, announced, and the stack walks on. The
// model stays untouched either way.
check('hist-stale-remove-fails-closed', H.undo() === null && H.depth() === 0
  && hd4.species_ranges.length === 1,
  'a deterministically impossible action is dropped, not stuck');

H.clear();
const savedMax = H.MAX;
H.MAX = 5;
for (let i = 0; i < 9; i += 1) {
  H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'v' + i });
}
check('hist-max-trims-oldest', H.depth() === 5 && H.undoStack[0].after === 'v4', 'depth=' + H.depth());
H.MAX = savedMax;
H.clear();
check('hist-clear', H.canUndo() === false && H.canRedo() === false);

// labels + subscription teardown
H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 2, col: 1, field: 'section',
  model: 'section', before: 'S1', hadKey: true, after: 'S2' });
check('hist-undo-label', /\S/.test(H.state().undoLabel), JSON.stringify(H.state()));
unsub();
const n = seen.length;
H.push({ type: 'rowAdd', tableId: 'biozones', row: 0, item: {} });
check('hist-unsubscribe', seen.length === n, 'a retired listener must not be called');
H.clear();

// a second stack instance stays independent (a preview panel can undo alone)
const H2 = S.rcaHistoryCreate();
H2.push({ type: 'cellEdit', tableId: 'x', row: 0, col: 0, field: 'f', model: 'f',
  before: 1, hadKey: true, after: 2 });
check('hist-instance-isolation', H2.canUndo() === true && H.canUndo() === false);

// engine seam: without an attached model nothing moves, and nothing throws
H.clear();
H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
  model: 'species', before: 'A', hadKey: true, after: 'zz' });
const detachedOk = (function () {
  S.rcaTableEdits.detach();
  const res = H.undo();
  freshEdits(hd4);
  return res === null;
})();
check('hist-no-engine-fails-closed', detachedOk && H.canUndo() === true,
  'undo without a model must fail closed, keeping the action retryable');
check('hist-describe-three-types', ['cellEdit', 'rowDelete', 'rowAdd'].every((t) => {
  const a = { type: t, tableId: 'species_ranges', row: 1, col: 2, item: {}, field: 'f',
    model: 'f', before: 'x', after: 'y', hadKey: true };
  const s = H.undoers[t].describe(a, 'undo');
  return typeof s === 'string' && s.length > 0 && s.indexOf('[?') !== 0;
}), 'undo labels must be localised too');

// ===========================================================================
// 5. the DOM state machine (minimal stub)
// ===========================================================================

function parseCompound(text) {
  const out = { tag: null, classes: [], attrs: [] };
  const re = /(^([a-zA-Z][\w-]*))|(\.[-\w]+)|(\[[^\]]+\])/g;
  let m;
  while ((m = re.exec(text))) {
    if (m[2]) out.tag = m[2].toUpperCase();
    else if (m[3]) out.classes.push(m[3].slice(1));
    else {
      const body = m[4].slice(1, -1);
      const eq = body.indexOf('=');
      if (eq === -1) out.attrs.push([body, null]);
      else out.attrs.push([body.slice(0, eq), body.slice(eq + 1).replace(/^["']|["']$/g, '')]);
    }
  }
  return out;
}
function matchesCompound(el, comp) {
  if (comp.tag && String(el.tagName).toUpperCase() !== comp.tag) return false;
  const cls = ' ' + String(el.className || '') + ' ';
  for (const c of comp.classes) if (cls.indexOf(' ' + c + ' ') === -1) return false;
  for (const pair of comp.attrs) {
    const v = el.getAttribute ? el.getAttribute(pair[0]) : null;
    if (v === null || v === undefined) return false;
    if (pair[1] !== null && String(v) !== pair[1]) return false;
  }
  return true;
}
function matchesSelector(el, selector) {
  const parts = String(selector).trim().split(/\s+/).map(parseCompound);
  if (!parts.length) return false;
  if (!matchesCompound(el, parts[parts.length - 1])) return false;
  let idx = parts.length - 2;
  let node = el.parentNode;
  while (idx >= 0) {
    if (!node) return false;
    if (matchesCompound(node, parts[idx])) idx -= 1;
    node = node.parentNode;
  }
  return true;
}

let ACTIVE = null;
function makeEl(tagName, attrs) {
  const el = {
    tagName: String(tagName || 'DIV').toUpperCase(),
    attributes: {}, className: '', children: [], parentNode: null,
    textContent: '', checked: false, focused: 0, blurred: 0, scrollCalls: 0,
    nodeType: 1, style: {}, dataset: {}, _listeners: {},
  };
  el.setAttribute = (k, v) => {
    el.attributes[String(k)] = String(v);
    if (String(k) === 'class') el.className = String(v);
  };
  el.getAttribute = (k) => (String(k) in el.attributes ? el.attributes[String(k)] : null);
  el.removeAttribute = (k) => { delete el.attributes[String(k)]; };
  el.classList = {
    add: (c) => { if (!el.classList.contains(c)) el.className = (el.className + ' ' + c).trim(); },
    remove: (c) => { el.className = el.className.split(/\s+/).filter((x) => x && x !== c).join(' '); },
    contains: (c) => el.className.split(/\s+/).indexOf(c) !== -1,
    toggle: (c, on) => {
      const has = el.classList.contains(c);
      const want = on === undefined ? !has : !!on;
      if (want) el.classList.add(c); else el.classList.remove(c);
      return want;
    },
  };
  el.matches = (sel) => matchesSelector(el, sel);
  el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
  el.querySelectorAll = (sel) => {
    const out = [];
    (function walk(node) {
      for (const c of node.children) { if (c.matches(sel)) out.push(c); walk(c); }
    })(el);
    return out;
  };
  el.querySelector = (sel) => el.querySelectorAll(sel)[0] || null;
  el.addEventListener = (type, cb) => { (el._listeners[type] = el._listeners[type] || []).push(cb); };
  el.removeEventListener = (type, cb) => {
    el._listeners[type] = (el._listeners[type] || []).filter((x) => x !== cb);
  };
  el.dispatchEvent = (ev) => {
    ev.target = ev.target || el;
    for (const cb of (el._listeners[ev.type] || []).slice()) cb(ev);
    return true;
  };
  el.focus = () => { el.focused += 1; ACTIVE = el; };
  el.blur = () => { el.blurred += 1; };
  el.scrollIntoView = () => { el.scrollCalls += 1; };
  for (const k of Object.keys(attrs || {})) el.setAttribute(k, attrs[k]);
  return el;
}

// Build the same tree rcaRenderResults emits (section > wrap > table > tr > td).
//
// FE-BORROW-2026-09-20 (域T): `fillDom` is separate from `buildDom` because the
// root's `innerHTML` setter calls it AGAIN. js/table.js repaints a structural
// change (delete / add row / undo of either) with `root.innerHTML =
// rcaRenderResults(...)` — in a browser that throws away EVERY node in the
// table and parses fresh ones, so a querySelector after a re-render finds the
// NEW checkbox. Wiping `children` without refilling made the re-rendered table
// empty, and the delete -> undo scenario then died on `querySelector(...) ===
// null`, i.e. it measured the stub instead of the implementation. Refilling
// from `root.rcaData` (rcaTableEdits mutates that object in place, so it IS the
// live model) is the closest a node-only stub gets to re-parsing, and it keeps
// the `rerenders` counter honest: the count still comes from the innerHTML
// write, while the tree that follows is addressable again.
function fillDom(root, data, onlyIds) {
  const cfgs = (S.rcaTableConfigs(data) || []).filter((c) => !onlyIds || onlyIds.indexOf(c.id) !== -1);
  for (const cfg of cfgs) {
    const lowRows = S.rcaTableLowConfidenceRows(data, cfg.id);
    const section = makeEl('DIV', { 'data-table': cfg.id, 'data-editable': '1', class: 'result-section' });
    const wrap = makeEl('DIV', { 'data-table-nav': cfg.id, class: 'table-wrap', tabindex: '0' });
    const table = makeEl('TABLE', { class: 'data-table' });
    const thead = makeEl('THEAD');
    const htr = makeEl('TR');
    const selTh = makeEl('TH', { class: 'rca-col-select' });
    selTh.appendChild(makeEl('INPUT', { type: 'checkbox', 'data-select-all': cfg.id, class: 'rca-select-all' }));
    htr.appendChild(selTh);
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = makeEl('TBODY');
    S.rcaRowsForTable(data, cfg.id).forEach((item, ri) => {
      const tr = makeEl('TR', { 'data-row': String(ri) });
      tr.className = lowRows.indexOf(ri) !== -1 ? 'rca-row-lowconf' : '';
      const selTd = makeEl('TD', { class: 'rca-cell-select' });
      selTd.appendChild(makeEl('INPUT', { type: 'checkbox', 'data-row-select': cfg.id, 'data-row': String(ri) }));
      tr.appendChild(selTd);
      tr.appendChild(makeEl('TH', { class: 'cell-empty' }));
      (cfg.edit || []).forEach((spec, ci) => {
        if (!spec || !spec.editable) { tr.appendChild(makeEl('TD')); return; }
        const cell = makeEl('TD', {
          'data-rca-edit': '1', 'data-table': cfg.id, 'data-row': String(ri), 'data-col': String(ci),
          contenteditable: 'true', class: 'rca-edit-cell', spellcheck: 'false',
        });
        if (spec.scalar) cell.setAttribute('data-scalar', '1');
        else cell.setAttribute('data-field', spec.model || spec.field);
        cell.setAttribute('data-type', spec.type || 'str');
        if (spec.validate) cell.setAttribute('data-validate', spec.validate);
        if (Array.isArray(spec.peer)) cell.setAttribute('data-peer', spec.peer.join(','));
        cell.textContent = displayOf(cfg, item, ci);
        tr.appendChild(cell);
      });
      const locTd = makeEl('TD', { class: 'rca-cell-locate' });
      locTd.appendChild(makeEl('BUTTON', { 'data-rca-locate': cfg.id + ':' + ri, class: 'rca-locate-btn btn' }));
      tr.appendChild(locTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    section.appendChild(wrap);
    const foot = makeEl('DIV', { class: 'rca-table-foot' });
    foot.appendChild(makeEl('BUTTON', { 'data-rca-addrow': cfg.id, class: 'rca-addrow-btn' }));
    section.appendChild(foot);
    const bar = makeEl('DIV', { class: 'rca-selection-bar', 'data-selection-bar': cfg.id, hidden: '' });
    bar.appendChild(makeEl('SPAN', { 'data-selection-count': cfg.id, class: 'rsb-count' }));
    for (const act of ['export', 'delete', 'clear']) {
      bar.appendChild(makeEl('BUTTON', { 'data-rca-sel-action': act, 'data-rca-sel-table': cfg.id }));
    }
    section.appendChild(bar);
    root.appendChild(section);
  }
}

function buildDom(data, onlyIds) {
  const root = makeEl('DIV');
  root.rerenders = 0;
  root.rcaData = data;
  root.rcaOnlyIds = onlyIds;
  let html = '';
  Object.defineProperty(root, 'innerHTML', {
    get: () => html,
    set: (v) => {
      html = String(v);
      root.rerenders += 1;
      root.children.length = 0;
      fillDom(root, root.rcaData, root.rcaOnlyIds);
    },
    enumerable: true,
  });
  fillDom(root, data, onlyIds);
  return root;
}

function displayOf(cfg, item, ci) {
  const spec = cfg.edit[ci] || {};
  let v;
  if (spec.scalar) v = isDict(item) ? (item.fossil !== undefined ? item.fossil : item.text) : item;
  else v = isDict(item) ? item[spec.field] : '';
  const text = S.rcaExportCellText(v === undefined ? '' : v);
  return text.trim() ? text : '-';
}

function mkEvent(type, target, extra) {
  const ev = { type, target, defaultPrevented: false };
  ev.preventDefault = () => { ev.defaultPrevented = true; };
  for (const k of Object.keys(extra || {})) ev[k] = extra[k];
  return ev;
}
function cellOf(root, tableId, row, col) {
  return root.querySelector('[data-rca-edit="1"][data-table="' + tableId + '"]'
    + '[data-row="' + row + '"][data-col="' + col + '"]');
}
function rowOf(root, tableId, row) {
  return root.querySelector('[data-table="' + tableId + '"] tr[data-row="' + row + '"]');
}
// A row's cell by cfg column index: children[0] is the select <td> and
// children[1] the row header, so cfg.edit column ci sits at children[2 + ci].
// Read-only columns keep their cell (js/table.js renders a bare <td>), which is
// exactly the node rcaEditRefFromCell has to refuse — counting them by hand is
// what made this fixture point at the confidence cell instead.
function rowCellEl(root, tableId, row, ci) {
  return rowOf(root, tableId, row).children[2 + ci];
}

const domData = deep(FIX);
const exportCalls = [];
const dom = buildDom(domData, ['species_ranges', 'biozones', 'other_fossils']);
S.rcaTableEditAttach(dom, domData, { editable: true, onExport: (t, rows, payload) => {
  exportCalls.push([t, rows.slice(), payload.rows.length]);
} });
check('dom-attached', S.RCA_EDIT_DOM.root === dom && S.rcaTableEdits.live() === domData);
check('dom-cells-addressable', dom.querySelectorAll('[data-rca-edit="1"]').length > 0
  && !!cellOf(dom, 'species_ranges', 0, 0));
check('dom-descriptors', cellOf(dom, 'species_ranges', 0, 2).getAttribute('data-validate') === 'range-pair'
  && cellOf(dom, 'species_ranges', 0, 2).getAttribute('data-peer') === 'range_top,range_base');
check('dom-ref-from-cell', (function () {
  const ref = S.rcaEditRefFromCell(cellOf(dom, 'species_ranges', 1, 0));
  return ref && ref.tableId === 'species_ranges' && ref.row === 1 && ref.col === 0
    && ref.spec.field === 'species';
})());
check('dom-ref-rejects-readonly', (function () {
  const agree = fieldCol(spCfg, 'agreement');
  const ro = rowCellEl(dom, 'species_ranges', 0, agree);
  const editableNeighbour = rowCellEl(dom, 'species_ranges', 0, agree - 1);
  return S.rcaEditRefFromCell(ro) === null
    && !!editableNeighbour && editableNeighbour.getAttribute('data-field') === 'confidence'
    && S.rcaEditRefFromCell(editableNeighbour) !== null;
})(), 'column 6 is the computed agreement cell, column 5 is still editable');

// focus == editTriggerEvent:"focus": snapshot + row highlight
const c00 = cellOf(dom, 'species_ranges', 0, 0);
c00.textContent = 'A';
dom.dispatchEvent(mkEvent('focusin', c00));
check('dom-focus-starts-edit', !!S.RCA_EDIT_DOM.editing && S.RCA_EDIT_DOM.editing.cell === c00
  && S.RCA_EDIT_DOM.editing.text0 === 'A', canonical(S.RCA_EDIT_DOM.editing && S.RCA_EDIT_DOM.editing.text0));
check('dom-focus-highlights-row', rowOf(dom, 'species_ranges', 0).className.indexOf('rca-row-active') !== -1,
  rowOf(dom, 'species_ranges', 0).className);

// Enter commits, pushes one action, moves down
S.rcaHistory.clear();
c00.textContent = 'A*';
S.rcaEditHandleKey(mkEvent('keydown', c00, { key: 'Enter' }));
check('dom-enter-commits', domData.species_ranges[0].species === 'A*');
check('dom-enter-pushes-history', S.rcaHistory.depth() === 1);
check('dom-enter-moves-down', cellOf(dom, 'species_ranges', 1, 0).focused > 0);
check('dom-enter-dirty-frame', c00.className.indexOf('rca-cell-dirty') !== -1, c00.className);
check('dom-enter-cell-text-repainted', c00.textContent === 'A*', c00.textContent);

// Shift+Enter moves up
const c10 = cellOf(dom, 'species_ranges', 1, 0);
dom.dispatchEvent(mkEvent('focusin', c10));
S.rcaEditHandleKey(mkEvent('keydown', c10, { key: 'Enter', shiftKey: true }));
check('dom-shift-enter-up', c00.focused > 0, 'focus=' + (ACTIVE === c00 ? 'row0' : 'other'));

// Esc reverts: text back, model untouched, stack untouched
dom.dispatchEvent(mkEvent('focusin', c10));
c10.textContent = 'typo';
dom.dispatchEvent(mkEvent('keydown', c10, { key: 'Escape' }));
check('dom-esc-reverts-text', c10.textContent === 'B', c10.textContent);
check('dom-esc-no-model-write', domData.species_ranges[1].species === 'B');
check('dom-esc-no-history', S.rcaHistory.depth() === 1);

// arrow navigation skips the read-only agreement column
const nb = cellOf(dom, 'species_ranges', 1, 5);
const c14 = cellOf(dom, 'species_ranges', 1, 4);
dom.dispatchEvent(mkEvent('focusin', c14));
S.rcaEditHandleKey(mkEvent('keydown', c14, { key: 'ArrowRight' }));
check('dom-arrow-skips-readonly', !!nb && nb.getAttribute('data-field') === 'confidence'
  && nb.focused > 0, 'agreement is column 6 and must be skipped');
const c13 = cellOf(dom, 'species_ranges', 1, 3);
nb.focused = 0;
dom.dispatchEvent(mkEvent('focusin', nb));
S.rcaEditHandleKey(mkEvent('keydown', nb, { key: 'ArrowLeft' }));
check('dom-arrow-left-skips-readonly', c14.focused > 0 && c13.focused === 0,
  'one editable step left of confidence (col 5) is biozone (col 4), not '
  + 'range_top (col 3) — rcaEditNeighbourCell walks a single step');
check('dom-arrow-right-past-readonly', S.rcaEditHandleKey(mkEvent('keydown', nb,
  { key: 'ArrowRight' })) === false
  && rowCellEl(dom, 'species_ranges', 1, fieldCol(spCfg, 'agreement')).focused === 0,
  'confidence is the last editable column: the read-only agreement cell to its '
  + 'right must never become a navigation target');

// invalid input: red frame, focus kept, model + stack untouched
const confCell = cellOf(dom, 'species_ranges', 1, confCol);
dom.dispatchEvent(mkEvent('focusin', confCell));
confCell.textContent = 'abc';
const stackBefore = S.rcaHistory.depth();
const blocked = S.rcaEditHandleKey(mkEvent('keydown', confCell, { key: 'Enter' }));
check('dom-invalid-blocks-navigation', blocked === false);
check('dom-invalid-red-frame', confCell.className.indexOf('rca-cell-invalid') !== -1, confCell.className);
check('dom-invalid-aria', confCell.getAttribute('aria-invalid') === 'true');
check('dom-invalid-keeps-focus', confCell.focused > 0);
check('dom-invalid-no-write', domData.species_ranges[1].confidence === 0.4);
check('dom-invalid-no-push', S.rcaHistory.depth() === stackBefore);
check('dom-invalid-hint-shown', (confCell.getAttribute('data-rca-invalid') || '').length > 0
  && (confCell.getAttribute('data-rca-invalid') || '').indexOf('[?') !== 0,
  confCell.getAttribute('data-rca-invalid'));
const focusBefore = confCell.focused;
dom.dispatchEvent(mkEvent('focusout', confCell));
check('dom-invalid-locks-focus-on-blur', confCell.focused === focusBefore + 1);
check('dom-invalid-text-survives', confCell.textContent === 'abc', 'Tabulator keeps the typed value');
confCell.textContent = '0.9';
dom.dispatchEvent(mkEvent('keydown', confCell, { key: 'Enter' }));
check('dom-invalid-cleared-after-fix', confCell.className.indexOf('rca-cell-invalid') === -1);
check('dom-fixed-value-committed', domData.species_ranges[1].confidence === 0.9);

// range-pair inversion is refused on commit, Esc clears the frame
const c02 = cellOf(dom, 'species_ranges', 0, 2);
dom.dispatchEvent(mkEvent('focusin', c02));
c02.textContent = '99';
dom.dispatchEvent(mkEvent('keydown', c02, { key: 'Enter' }));
check('dom-range-inversion-refused', domData.species_ranges[0].range_base === '1'
  && c02.className.indexOf('rca-cell-invalid') !== -1, domData.species_ranges[0].range_base);
dom.dispatchEvent(mkEvent('keydown', c02, { key: 'Escape' }));
check('dom-esc-clears-frame', c02.className.indexOf('rca-cell-invalid') === -1
  && c02.textContent === '1', c02.textContent);

// focusout elsewhere commits
const c01 = cellOf(dom, 'species_ranges', 0, 1);
dom.dispatchEvent(mkEvent('focusin', c01));
c01.textContent = 'S9';
dom.dispatchEvent(mkEvent('focusout', c01));
check('dom-focusout-commits', domData.species_ranges[0].section === 'S9');

// commit helper is usable without the DOM at all (app.js / Qt bridge reuse)
check('dom-commit-api-on-plain-cell', (function () {
  const cell = makeEl('TD', { 'data-rca-edit': '1', 'data-table': 'biozones', 'data-row': '0',
    'data-col': '3', 'data-field': 'thickness_m', 'data-type': 'number', contenteditable: 'true' });
  cell.textContent = '12';
  const res = S.rcaEditCommitCell(cell);
  return res.ok === true && res.changed === true && domData.biozones[0].thickness_m === 12;
})());

// selection: checkbox -> bar -> export -> delete -> undo -> clear
const box0 = dom.querySelector('[data-row-select="species_ranges"][data-row="0"]');
box0.checked = true;
dom.dispatchEvent(mkEvent('change', box0));
let bar = dom.querySelector('[data-selection-bar="species_ranges"]');
let count = dom.querySelector('[data-selection-count="species_ranges"]');
check('dom-selection-bar-opens', bar.getAttribute('hidden') === null && count.textContent === '1',
  'count=' + count.textContent);
dom.dispatchEvent(mkEvent('click', dom.querySelector('[data-rca-sel-action="export"][data-rca-sel-table="species_ranges"]')));
check('dom-export-selected-hook', exportCalls.length === 1 && exportCalls[0][0] === 'species_ranges'
  && exportCalls[0][1].join(',') === '0', canonical(exportCalls));
S.rcaHistory.clear();
const rr0 = dom.rerenders;
dom.dispatchEvent(mkEvent('click', dom.querySelector('[data-rca-sel-action="delete"][data-rca-sel-table="species_ranges"]')));
check('dom-delete-selected-rows', domData.species_ranges.length === 1
  && domData.species_ranges[0].species === 'B');
check('dom-delete-pushes-history', S.rcaHistory.depth() === 1
  && S.rcaHistory.undoStack[0].type === 'rowDelete');
check('dom-delete-rerenders', dom.rerenders === rr0 + 1, 'structural ops repaint');
check('dom-delete-clears-selection', S.rcaTableEdits.selectedRows('species_ranges').length === 0);
S.rcaHistory.undo();
// The restored row is the one the scenario EDITED at :1160 (species 'A*' with
// section 'S9'), and rowDelete carries the item verbatim — so undo restores the
// uncommitted edit, not the snapshot's 'A'. Its dirty frame has to come back on
// the FRESH node too: rcaEditRerender repaints it from the registry alone.
check('dom-delete-undo-restores', domData.species_ranges.length === 2
  && domData.species_ranges[0].species === 'A*'
  && domData.species_ranges[0].section === 'S9'
  && domData.species_ranges[1].species === 'B' && dom.rerenders === rr0 + 2,
  'length=' + domData.species_ranges.length + ' species='
  + canonical(domData.species_ranges.map((r) => r.species)) + ' rerenders='
  + (dom.rerenders - rr0));
check('dom-undo-rerender-is-addressable', (function () {
  // A structural re-render replaces every node (a browser re-parses the
  // markup), so the handles taken before it are dead — re-query them.
  bar = dom.querySelector('[data-selection-bar="species_ranges"]');
  count = dom.querySelector('[data-selection-count="species_ranges"]');
  const restored = cellOf(dom, 'species_ranges', 0, 0);
  // The re-render paints the RESTORED VALUE, and the save path still owns the
  // uncommitted edit: undoDeleteRow keeps the diff intact (capture re-derives
  // it from the snapshot) even though `_edited` — the frame cache, which
  // shiftEdited drops with the deleted row — has to be repainted by the next
  // actual write. That is a cosmetic gap, not a lost edit.
  //
  // The diff has TWO rows, not one: this same scenario committed row 1's
  // confidence 0.4 -> 0.9 at :1230 (`dom-fixed-value-committed`), and
  // editable.py:capture_edits diffs the live list against the ATTACH snapshot
  // index by index, so that edit is still unsaved here. Verified against the
  // Python oracle on 2026-09-20:
  //   capture_edits(before, after) ==
  //     {'species_ranges': {'0': {'section': 'S9', 'species': 'A*'},
  //                          '1': {'confidence': 0.9}}}
  // An expectation that omitted row 1 would be asking the save path to DROP a
  // committed edit, i.e. silent data loss on Apply.
  return !!bar && !!restored && restored.textContent === 'A*'
    && restored.getAttribute('data-field') === 'species'
    && eqJson(S.rcaTableEdits.capture('species_ranges'),
      { 0: { species: 'A*', section: 'S9' }, 1: { confidence: 0.9 } });
})(), 'undo -> rerender must leave a live tree AND an undoable diff');
const selBox1 = dom.querySelector('[data-row-select="species_ranges"][data-row="1"]');
selBox1.checked = true;
dom.dispatchEvent(mkEvent('change', selBox1));
dom.dispatchEvent(mkEvent('click', dom.querySelector('[data-rca-sel-action="clear"][data-rca-sel-table="species_ranges"]')));
check('dom-clear-selection', S.rcaTableEdits.selectedRows('species_ranges').length === 0
  && bar.getAttribute('hidden') !== null
  && dom.querySelectorAll('[data-row-select="species_ranges"]').every((b) => b.checked === false),
  '清除所选 must also take the bar and the ticks away, not just the model bucket');
const all = dom.querySelector('[data-select-all="species_ranges"]');
all.checked = true;
dom.dispatchEvent(mkEvent('change', all));
check('dom-select-all', S.rcaTableEdits.selectedRows('species_ranges').length === 2);
all.checked = false;
dom.dispatchEvent(mkEvent('change', all));
check('dom-select-all-off', S.rcaTableEdits.selectedRows('species_ranges').length === 0
  && dom.querySelectorAll('[data-row-select="species_ranges"]').every((b) => b.checked === false)
  && dom.querySelector('[data-selection-bar="species_ranges"]').getAttribute('hidden') !== null,
  'selectAll(false) deletes the bucket, so the sync has to be told which table '
  + 'to repaint (rcaEditSyncSelectionDom(root, extraTables))');

// 定位 / hover <-> guarded rcaViz calls
const vizCalls = [];
S.__ctx.rcaViz = {
  focusRow: (i) => { vizCalls.push(['focusRow', i]); return true; },
  locateTo: (i) => { vizCalls.push(['locateTo', i]); return true; },
  clearFocus: () => { vizCalls.push(['clearFocus']); return true; },
};
dom.dispatchEvent(mkEvent('click', dom.querySelector('[data-rca-locate="species_ranges:1"]')));
check('dom-locate-calls-viz', vizCalls.some((p) => p[0] === 'locateTo' && p[1] === 1), canonical(vizCalls));
check('dom-locate-highlights-row', rowOf(dom, 'species_ranges', 1).className.indexOf('rca-row-active') !== -1);
const hoverCell = cellOf(dom, 'species_ranges', 0, 0);
dom.dispatchEvent(mkEvent('mouseover', hoverCell));
check('dom-hover-focuses-viz', vizCalls.some((p) => p[0] === 'focusRow' && p[1] === 0), canonical(vizCalls));
// The locate click above legitimately pushed its own focusRow(1) into the log,
// so "deduped" has to be measured against the calls so far, not against a
// count of one.
const hoverCount = vizCalls.filter((p) => p[0] === 'focusRow').length;
dom.dispatchEvent(mkEvent('mouseover', hoverCell));
check('dom-hover-deduped', vizCalls.filter((p) => p[0] === 'focusRow').length === hoverCount,
  canonical(vizCalls));
dom.dispatchEvent(mkEvent('mouseout', hoverCell));
check('dom-leave-clears-viz', vizCalls.some((p) => p[0] === 'clearFocus'), canonical(vizCalls));
const row2 = cellOf(dom, 'species_ranges', 0, 0);
dom.dispatchEvent(mkEvent('focusin', row2));
check('dom-focus-focuses-viz', vizCalls.filter((p) => p[0] === 'focusRow').length >= 2);
delete S.__ctx.rcaViz;
check('dom-viz-absent-is-silent', S.rcaEditLocateRow('species_ranges', 0) === false
  && S.rcaTableHighlightRow(1, 'species_ranges') === true);

// add-row button
S.rcaHistory.clear();
const n0 = domData.biozones.length;
dom.dispatchEvent(mkEvent('click', dom.querySelector('[data-rca-addrow="biozones"]')));
check('dom-addrow-appends', domData.biozones.length === n0 + 1 && domData.biozones[n0].name === '');
check('dom-addrow-history', S.rcaHistory.depth() === 1 && S.rcaHistory.undoStack[0].type === 'rowAdd');
S.rcaHistory.undo();
check('dom-addrow-undo', domData.biozones.length === n0);
S.rcaHistory.redo();
check('dom-addrow-redo', domData.biozones.length === n0 + 1);

// scalar row (other_fossils) writes the LIST ITEM
const ofCell = cellOf(dom, 'other_fossils', 0, 0);
dom.dispatchEvent(mkEvent('focusin', ofCell));
ofCell.textContent = 'Ammonite: Z';
dom.dispatchEvent(mkEvent('keydown', ofCell, { key: 'Enter' }));
check('dom-scalar-row-edit', domData.other_fossils[0] === 'Ammonite: Z');
check('dom-scalar-row-capture', eqJson(S.rcaTableEdits.capture('other_fossils'),
  { _replaced: ['Ammonite: Z'] }), canonical(S.rcaTableEdits.capture('other_fossils')));

// J / K over the low-confidence rows (the container is the focus context)
const jd = deep(FIX);
const jRoot = buildDom(jd, ['species_ranges']);
S.rcaTableEditAttach(jRoot, jd, { editable: true });
const nav = jRoot.querySelector('[data-table-nav="species_ranges"]');
const lowRows = S.rcaTableLowConfidenceRows(jd, 'species_ranges');
check('dom-lowconf-class-rendered', rowOf(jRoot, 'species_ranges', lowRows[0])
  .className.indexOf('rca-row-lowconf') !== -1);
check('dom-j-k-walk', (function () {
  if (!S.rcaEditHandleKey(mkEvent('keydown', nav, { key: 'j' }))) return false;
  if (cellOf(jRoot, 'species_ranges', lowRows[0], 0).focused < 1) return false;
  if (S.rcaEditHandleKey(mkEvent('keydown', nav, { key: 'j' }))) return false; // no more
  if (S.rcaEditHandleKey(mkEvent('keydown', nav, { key: 'k' }))) return false; // nothing above
  return true;
})(), 'lowRows=' + canonical(lowRows));
check('dom-j-caps-out-at-ends', S.rcaEditStepLowConfidence('species_ranges', +1) === false
  && S.rcaEditStepLowConfidence('species_ranges', -1) === false);
check('dom-j-ignored-outside-table', S.rcaEditHandleKey(mkEvent('keydown', jRoot, { key: 'j' })) === false);
check('dom-j-not-typed-into-cell', (function () {
  const cell = cellOf(jRoot, 'species_ranges', 0, 0);
  dom.dispatchEvent(mkEvent('focusin', cell));
  return S.rcaEditHandleKey(mkEvent('keydown', cell, { key: 'j' })) === false;
})(), 'typing j inside a cell must insert text, not jump');
S.rcaTableEditDetach();
check('dom-detach-clears-root', S.RCA_EDIT_DOM.root === null);

// attach twice on two roots. The editor is a ONE-document layer:
// rcaTableEditAttach dedups its listener wiring per root (re-attaching the same
// node must not stack a second copy of every handler, or one Enter would commit
// twice) and rewires onto a NEW root, unhooking the previous panel so a stale
// DOM can never write into the model the newest attach owns. js/app.js attaches
// exactly once per render — this is the guard for the re-attach path.
const countListeners = (el) => Object.keys(el._listeners)
  .reduce((n, k) => n + el._listeners[k].length, 0);
const dataA = deep(FIX);
const twoA = buildDom(dataA, ['biozones']);
S.rcaTableEditAttach(twoA, dataA, { editable: true });
const dataB = deep(FIX);
const twoB = buildDom(dataB, ['biozones']);
S.rcaTableEditAttach(twoB, dataB, { editable: true });
const bCell = cellOf(twoB, 'biozones', 0, 0);
twoB.dispatchEvent(mkEvent('focusin', bCell));
bCell.textContent = 'ZZ';
twoB.dispatchEvent(mkEvent('keydown', bCell, { key: 'Enter' }));
check('dom-second-root-wired', dataB.biozones[0].name === 'ZZ'
  && dataA.biozones[0].name === 'Z1', canonical([dataA.biozones[0], dataB.biozones[0]]));
const aCell = cellOf(twoA, 'biozones', 0, 0);
twoA.dispatchEvent(mkEvent('focusin', aCell));
aCell.textContent = 'YY';
twoA.dispatchEvent(mkEvent('keydown', aCell, { key: 'Enter' }));
check('dom-first-root-unwired-on-reattach', dataA.biozones[0].name === 'Z1'
  && dataB.biozones[0].name === 'ZZ' && S.RCA_EDIT_DOM.root === twoB
  && S.RCA_EDIT_DOM.wiredRoot === twoB,
  'the previous root must stop handling events — one editor document at a time');
const wired0 = countListeners(twoB);
S.rcaTableEditAttach(twoB, dataB, { editable: true });
check('dom-same-root-reattach-no-double-wiring', wired0 >= 4 && countListeners(twoB) === wired0,
  'wired=' + wired0 + ' after re-attach=' + countListeners(twoB));
S.rcaTableEditDetach();

// ===========================================================================
// 6. Ctrl+Z / Ctrl+Shift+Z and the native-yield rule
// ===========================================================================

const hk = S.rcaHistory;
hk.clear();
// Section 5 ends by detaching the editor, and every undoer FAILS CLOSED without
// an attached model (hist-no-engine-fails-closed, js/history.js:215) — so the
// Ctrl+Z cases below would all be no-ops. Re-attach a model of their own: the
// routing is what section 6 tests, and routing has to be measured on a stack
// that is actually able to move.
const hkData = deep(FIX);
freshEdits(hkData);
hk.clear();
const ceTarget = { tagName: 'TD', getAttribute: (k) => (k === 'contenteditable' ? 'true' : null), parentNode: null };
const plain = { tagName: 'DIV', getAttribute: () => null, parentNode: null };
check('yield-detects-contenteditable', S.rcaHistoryInEditable(ceTarget) === true);
check('yield-detects-input', S.rcaHistoryInEditable({ tagName: 'INPUT', type: 'text',
  getAttribute: () => null, parentNode: null }) === true);
check('yield-skips-checkbox', S.rcaHistoryInEditable({ tagName: 'INPUT', type: 'checkbox',
  getAttribute: (k) => (k === 'type' ? 'checkbox' : null), parentNode: null }) === false);
check('yield-walks-up-to-host', S.rcaHistoryInEditable({ tagName: 'B', getAttribute: () => null,
  parentNode: ceTarget }) === true);
check('yield-lowercase-tagname', S.rcaHistoryInEditable({ tagName: 'td', getAttribute: () => null,
  parentNode: ceTarget }) === true);
check('yield-clean-target', S.rcaHistoryInEditable(plain) === false);

hk.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
  model: 'species', before: 'A', hadKey: true, after: 'Z*' });
const evInCell = mkEvent('keydown', ceTarget, { key: 'z', ctrlKey: true });
check('key-yields-inside-cell', hk.handleKey(evInCell) === false
  && evInCell.defaultPrevented === false && hk.depth() === 1,
  'native text undo must win while a cell is focused');
const evPlain = mkEvent('keydown', plain, { key: 'z', ctrlKey: true });
check('key-ctrl-z-undoes', hk.handleKey(evPlain) === true && evPlain.defaultPrevented === true
  && hk.depth() === 0);
const evRedo = mkEvent('keydown', plain, { key: 'z', ctrlKey: true, shiftKey: true });
check('key-ctrl-shift-z-redoes', hk.handleKey(evRedo) === true && hk.canRedo() === false);
hk.undo();
const evY = mkEvent('keydown', plain, { key: 'y', ctrlKey: true });
check('key-ctrl-y-redoes', hk.handleKey(evY) === true);
const evCmd = mkEvent('keydown', plain, { key: 'z', metaKey: true });
check('key-cmd-z-undoes', hk.handleKey(evCmd) === true);
check('key-ignores-bare-z', hk.handleKey(mkEvent('keydown', plain, { key: 'z' })) === false);
check('key-ignores-ctrl-x', hk.handleKey(mkEvent('keydown', plain, { key: 'x', ctrlKey: true })) === false);
check('key-ignores-shift-without-mod', hk.handleKey(mkEvent('keydown', plain, { key: 'z', shiftKey: true })) === false);
check('key-force-option-bypasses-yield', (function () {
  hk.clear();
  hk.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'F*' });
  const ev = mkEvent('keydown', ceTarget, { key: 'z', ctrlKey: true });
  const handled = hk.handleKey(ev, { ignoreEditable: true });
  const moved = hk.depth() === 0;
  hk.redo();
  return handled === true && moved === true && ev.defaultPrevented === true;
})(), 'row-level structure undo stays reachable from app.js');

const fakeDoc = { _l: {}, addEventListener(t, cb) { (this._l[t] = this._l[t] || []).push(cb); },
  removeEventListener(t, cb) { this._l[t] = (this._l[t] || []).filter((x) => x !== cb); },
  querySelectorAll: () => [] };
hk.clear();
hk.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
  model: 'species', before: 'A', hadKey: true, after: 'D*' });
const unbind = hk.bind(fakeDoc);
check('key-bind-routes-undo', (function () {
  fakeDoc._l.keydown.forEach((cb) => cb(mkEvent('keydown', plain, { key: 'z', ctrlKey: true })));
  return hk.depth() === 0;
})());
check('key-unbind', unbind() === true && (fakeDoc._l.keydown || []).length === 0);
check('key-bind-without-dom', typeof hk.bind(null) === 'function' && hk.bind(null)() === true);

// disabled state reaches the buttons through the subscription
const uiRoot = makeEl('DIV');
uiRoot.appendChild(makeEl('BUTTON', { 'data-rca-undo': '1' }));
uiRoot.appendChild(makeEl('BUTTON', { 'data-rca-redo': '1' }));
hk.clear();
S.rcaHistoryAttachUi(uiRoot);
// FE-BORROW-2026-09-20: attachUi must be the one that wires the click router
// (the browser bug was a stale duplicate definition shadowing it at load).
check('ui-attach-wires-clicks', S.__ctx.document.__RCA_HISTORY_CLICK_BOUND__ === true);
const undoBtn = uiRoot.querySelector('[data-rca-undo]');
const redoBtn = uiRoot.querySelector('[data-rca-redo]');
check('ui-disabled-when-empty', undoBtn.getAttribute('disabled') !== null
  && undoBtn.getAttribute('aria-disabled') === 'true', canonical(undoBtn.attributes));
hk.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
  model: 'species', before: 'A', hadKey: true, after: 'E*' });
check('ui-enabled-after-push', undoBtn.getAttribute('disabled') === null
  && redoBtn.getAttribute('disabled') !== null, canonical([undoBtn.attributes, redoBtn.attributes]));
hk.undo();
check('ui-redo-enabled-after-undo', redoBtn.getAttribute('disabled') === null
  && undoBtn.getAttribute('disabled') !== null);
check('ui-sync-returns-count', S.rcaHistorySyncButtons(uiRoot) === 2);

// FE-BORROW-2026-09-20 regression: a live-browser probe found the toolbar
// undo button rendering, enabled, and doing NOTHING — the buttons were
// synced but never had a click listener. rcaHistoryAttachUi must wire a
// (one-time, document-delegated) click router.
const clickDoc = { _l: {},
  addEventListener(t, cb) { (this._l[t] = this._l[t] || []).push(cb); },
  removeEventListener() {} };
check('ui-wire-first-binds', S.rcaHistoryWireClicksOnce(clickDoc) === true
  && (clickDoc._l.click || []).length === 1);
check('ui-wire-idempotent', S.rcaHistoryWireClicksOnce(clickDoc) === true
  && (clickDoc._l.click || []).length === 1
  && clickDoc.__RCA_HISTORY_CLICK_BOUND__ === true);
function fakeBtn(attr) {
  const b = { getAttribute(n) { return n === attr ? '1' : null; },
    hasAttribute(n) { return n === attr; } };
  b.closest = (sel) => (sel.indexOf(attr) !== -1 ? b : null);
  return b;
}
const fireClick = (btn) => { (clickDoc._l.click || []).forEach((cb) => cb(mkEvent('click', btn))); };
// State here: undoStack empty, redoStack holds the 'E*' cellEdit (undone above).
fireClick(fakeBtn('data-rca-redo'));
check('ui-click-redo-routes', hk.depth() === 1 && hk.state().canRedo === false,
  'clicking [data-rca-redo] must replay the action');
fireClick(fakeBtn('data-rca-undo'));
check('ui-click-undo-routes', hk.depth() === 0 && hk.state().canRedo === true,
  'clicking [data-rca-undo] must revert the action');
fireClick(fakeBtn('data-rca-undo'));
check('ui-click-empty-no-throw', hk.depth() === 0,
  'undo on an empty stack is a silent no-op, not an exception');
fireClick({ closest: () => null });
check('ui-click-ignores-other-targets', hk.depth() === 0);

// ===========================================================================
// 7. i18n: the real key wins, RCA_EDIT_STRINGS is the safety net
// ===========================================================================

const EDIT_KEYS = S.RCA_EDIT_I18N_KEYS.concat(Object.keys(S.RCA_HISTORY_STRINGS));
check('i18n-every-key-answers', EDIT_KEYS.every((k) => {
  const s = S.rcaEditT(k);
  return typeof s === 'string' && s.length > 0 && s.indexOf('[?') !== 0;
}), 'no editor string may reach the user as [?key]');
check('i18n-keys-bilingual', Object.keys(S.RCA_EDIT_STRINGS).every((k) => S.RCA_EDIT_STRINGS[k].zh
  && S.RCA_EDIT_STRINGS[k].en) && Object.keys(S.RCA_HISTORY_STRINGS).every((k) => S.RCA_HISTORY_STRINGS[k].zh
  && S.RCA_HISTORY_STRINGS[k].en));
check('i18n-key-list-exported', S.RCA_EDIT_I18N_KEYS.length >= 15,
  'domain U lifts these into js/i18n.js');
S.rcaSetLang('en');
check('i18n-english-fallback', S.rcaEditT('edit.addRow') === 'Add row', S.rcaEditT('edit.addRow'));
S.rcaSetLang('ja');
check('i18n-unknown-locale-falls-back-to-en', S.rcaEditT('edit.addRow') === 'Add row',
  'js/i18n.js has a ja locale, the temporary table does not');
S.rcaSetLang('zh');
check('i18n-chinese-fallback', S.rcaEditT('edit.addRow') === '新增行', S.rcaEditT('edit.addRow'));
check('i18n-params-substituted', S.rcaEditT('edit.selectedCount', { n: 3 }).indexOf('3') !== -1
  && S.rcaEditT('edit.selectedCount', { n: 3 }).indexOf('{n}') === -1);
check('i18n-history-t-localized', /\S/.test(S.rcaHistoryT('edit.undoRowDelete', { row: 4 }))
  && S.rcaHistoryT('edit.undoRowDelete', { row: 4 }).indexOf('4') !== -1);
check('i18n-history-t-falls-through-to-editor-t', (function () {
  // `edit.addRow` lives in table.js's table: history.js must reuse it, not
  // invent a second copy of the string.
  const a = S.rcaHistoryT('edit.addRow');
  S.rcaSetLang('en');
  const b = S.rcaHistoryT('edit.addRow');
  S.rcaSetLang('zh');
  return a === '新增行' && b === 'Add row';
})(), S.rcaHistoryT('edit.addRow'));
S.__run("RCA_I18N.zh['edit.addRow'] = '插入行'");
check('i18n-prefers-real-key', S.rcaEditT('edit.addRow') === '插入行', S.rcaEditT('edit.addRow'));
S.__run("delete RCA_I18N.zh['edit.addRow']");
check('i18n-falls-back-when-key-gone', S.rcaEditT('edit.addRow') === '新增行');

// ===========================================================================
// 8. the read-only renderer is untouched (Qt Fluent history dialog guard)
// ===========================================================================

const htmlRO = S.rcaRenderResults(deep(FIX), '{raw}');
const htmlRO2 = S.rcaRenderResults(deep(FIX), '{raw}', { editable: false });
const htmlED = S.rcaRenderResults(deep(FIX), '{raw}', { editable: true });
check('readonly-signature-compatible', htmlRO === htmlRO2,
  'the 2-arg Qt call must render byte-identically');
check('readonly-no-editors', htmlRO.indexOf('contenteditable') === -1
  && htmlRO.indexOf('data-editable') === -1
  && htmlRO.indexOf('rca-selection-bar') === -1
  && htmlRO.indexOf('data-rca-locate') === -1
  && htmlRO.indexOf('data-table-nav') === -1
  && htmlRO.indexOf('rca-row-lowconf') === -1);
check('readonly-still-renders-cells', htmlRO.indexOf('>A</td>') !== -1
  && htmlRO.indexOf('data-copy="species_ranges"') !== -1);
check('editable-markup-present', htmlED.indexOf('contenteditable="true"') !== -1
  && htmlED.indexOf('data-editable="1"') !== -1
  && htmlED.indexOf('data-selection-bar="species_ranges"') !== -1
  && htmlED.indexOf('data-row-select="species_ranges"') !== -1
  && htmlED.indexOf('tabindex="0"') !== -1);
check('editable-keeps-existing-actions', htmlED.indexOf('data-csv="species_ranges"') !== -1
  && htmlED.indexOf('id="btn-export-all"') !== -1);
check('editable-dirty-mark-survives-rerender', (function () {
  const d = deep(FIX);
  freshEdits(d);
  S.rcaTableEdits.editCell('species_ranges', 0, 0, 'QQ');
  const html = S.rcaRenderResults(d, '', { editable: true });
  S.rcaTableEdits.detach();
  return html.indexOf('rca-cell-dirty') !== -1 && html.indexOf('>QQ<') !== -1;
})(), 'a re-render must repaint dirty frames from the registry alone');
check('empty-table-keeps-addrow', S.rcaRenderResults({ sections: [], species_ranges: [],
  biozones: [], other_fossils: [] }, '', { editable: true })
  .indexOf('data-rca-addrow="species_ranges"') !== -1);
check('viz-file-not-required', S.RCA_EDIT_DOM.root === null);

const css = fs.readFileSync(path.join(__dirname, 'css', 'table-edit.css'), 'utf8');
check('css-exists-and-tagged', css.indexOf('FE-BORROW-2026-09-20') !== -1);
check('css-uses-existing-tokens', (css.match(/var\(--[a-z0-9-]+/g) || []).length > 12);
check('css-no-hardcoded-color-declarations',
  !/^\s*(background|color|border[^:]*):\s*#[0-9a-f]{3,8}\b/mi.test(css.replace(/\/\*[\s\S]*?\*\//g, '')));
check('css-covers-all-states', ['rca-edit-cell', 'rca-cell-dirty', 'rca-cell-invalid',
  'rca-selection-bar', 'rca-row-lowconf', 'rca-row-active', 'rca-edit-status',
  'rca-cell-locate', 'prefers-reduced-motion', 'forced-colors']
  .every((k) => css.indexOf(k) !== -1));
const idx = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
check('index-loads-history-and-css', idx.indexOf('js/history.js') !== -1
  && idx.indexOf('css/table-edit.css') !== -1);
check('history-after-table-order', idx.indexOf('js/table.js') < idx.indexOf('js/history.js'),
  'history.js resolves rcaTableEdits lazily anyway, but the order must not fight it');

// ===========================================================================
// 9. FE-FIX-2026-09-21 — the 19 table.js audit fixes, one block per item
// ===========================================================================
//
// The CODE fixes (marked "FE-FIX-2026-09-21 (audit item N)" in js/table.js)
// landed fleet-wide; this section is the regression net. Note the comment
// numbering in table.js is the AUDIT's own (its item 1 covers both
// attach-clears-history AND undoDeleteRow; its item 17 is the apply_edits
// negative index) — the checks below are named by the task list and each
// cites the table.js line it pins.
//
// Already-covered elsewhere and NOT re-asserted here:
//   * undoDeleteRow out-of-range via the STACK (hist-stale-insert-clamps /
//     hist-stale-insert-undo, section 4) — only the direct registry-level
//     verb is added below.
//   * history.js-side items (1/2/5/6) owned by tests_history_fixes_2026_09_21.js.

// ---- item 1: attach(DIFFERENT data) clears the undo stack ------------------
{
  const a1 = deep(FIX);
  freshEdits(a1);
  H.clear();
  H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'Q*' });
  check('audit1-attach-different-data-clears-history', (function () {
    const b1 = deep(FIX);
    S.rcaTableEdits.attach(b1);           // no detach() in between — the re-render path
    return H.depth() === 0;
  })(), 'depth=' + H.depth() + ' (js/table.js rcaTableEdits.attach, FE-FIX item 1)');
  H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'Q*' });
  const same = S.rcaTableEdits.live();
  S.rcaTableEdits.attach(same);           // SAME object identity -> protected
  check('audit1-attach-same-object-keeps-history', H.depth() === 1,
    'depth=' + H.depth() + ' — re-attaching the SAME data must not wipe the stack');
  H.clear();
}

// ---- item 2: undoDeleteRow verb itself refuses row > length ----------------
{
  const a2 = deep(FIX);
  freshEdits(a2);
  const len0 = a2.species_ranges.length;
  check('audit2-undo-deleterow-out-of-range', (function () {
    const tooFar = S.rcaTableEdits.undoDeleteRow('species_ranges', len0 + 5, { species: 'ghost' });
    const negative = S.rcaTableEdits.undoDeleteRow('species_ranges', -1, { species: 'ghost' });
    const refusedUntouched = a2.species_ranges.length === len0;
    const atEnd = S.rcaTableEdits.undoDeleteRow('species_ranges', len0, { species: 'tail' });
    return tooFar === false && negative === false && refusedUntouched
      && atEnd === true && a2.species_ranges.length === len0 + 1
      && a2.species_ranges[len0].species === 'tail';
  })(), 'row>len and row<0 fail closed; row===len stays legal (Python list.insert appends)');
}

// ---- item 3: a structural re-render preserves the foreign slots -------------
{
  const a3 = deep(FIX);
  const dom3 = buildDom(a3, ['species_ranges']);
  S.rcaTableEditAttach(dom3, a3, { editable: true });
  const vizHost = makeEl('DIV', { id: 'viz-host' });
  const canvas = makeEl('CANVAS');
  vizHost.appendChild(canvas);
  dom3.appendChild(vizHost);
  const nameSlot = makeEl('DIV', { id: 'names-verify-slot' });
  const slotted = makeEl('SPAN');
  nameSlot.appendChild(slotted);
  dom3.appendChild(nameSlot);
  const rerendered = S.rcaTableRerender();
  check('audit3-rerender-preserves-viz-host-identity', rerendered === true
    && dom3.children.indexOf(vizHost) !== -1 && vizHost.parentNode === dom3
    && vizHost.children.indexOf(canvas) !== -1,
    'the SAME #viz-host node object (with its mounted canvas) must survive the innerHTML swap');
  check('audit3-rerender-preserves-names-verify-slot',
    dom3.children.indexOf(nameSlot) !== -1 && nameSlot.children.indexOf(slotted) !== -1,
    '#names-verify-slot keeps the content app.js filled asynchronously');
  S.rcaTableEditDetach();
}

// ---- item 4: the '-' placeholder can never be committed ---------------------
{
  // (a) the editable branch renders an EMPTY cell + .cell-empty, never a '-'
  //     inside the contenteditable (js/table.js FE-FIX items 3 + 14).
  check('audit4-editable-markup-has-no-dash-inside-cell', (function () {
    const html = S.rcaRenderResults(deep(FIX), '', { editable: true });
    const dashInCell = /<td class="[^"]*rca-edit-cell[^"]*"[^>]*>-<\/td>/.test(html);
    const emptyCell = /<td class="[^"]*cell-empty[^"]*rca-edit-cell[^"]*"[^>]*><\/td>/.test(html);
    return !dashInCell && emptyCell;
  })(), 'row 0 has no confidence value -> that editable cell must be empty, not "-"');

  // (b) the OLD-style cell (a '-' still sitting in the contenteditable, which
  //     the app's own buildDom fixture renders): a visit + blur must be a pure
  //     no-op — no commit, no model write, no invalid frame, no re-focus loop
  //     on the numeric column (js/table.js rcaEditOnFocusOut, FE-FIX item 3).
  const a4 = deep(FIX);
  const dom4 = buildDom(a4, ['species_ranges']);
  S.rcaTableEditAttach(dom4, a4, { editable: true });
  const confCell4 = cellOf(dom4, 'species_ranges', 0, confCol);   // float column, text '-'
  confCell4.textContent = '-';
  const focusBefore4 = confCell4.focused;
  dom4.dispatchEvent(mkEvent('focusin', confCell4));
  dom4.dispatchEvent(mkEvent('focusout', confCell4));
  check('audit4-placeholder-blur-is-noop', confCell4.focused === focusBefore4
    && confCell4.className.indexOf('rca-cell-invalid') === -1
    && !('confidence' in a4.species_ranges[0])
    && eqJson(S.rcaTableEdits.captureAll(), {}) && S.rcaHistory.depth() === 0,
    'visit-then-blur on a "-" numeric cell must not loop or write (FE-FIX item 3/4)');

  // (c) even a hard Enter-commit of '-' is refused BEFORE the model: the value
  //     never lands, the stack never grows (the red frame + kept focus is the
  //     documented blocked-editor behaviour, covered by dom-invalid-* above).
  dom4.dispatchEvent(mkEvent('focusin', confCell4));
  const blockedEnter = S.rcaEditHandleKey(mkEvent('keydown', confCell4, { key: 'Enter' }));
  check('audit4-dash-enter-never-stores', blockedEnter === false
    && a4.species_ranges[0].confidence === undefined
    && eqJson(S.rcaTableEdits.captureAll(), {}) && S.rcaHistory.depth() === 0,
    'literal "-" must not reach the model on any path');
  S.rcaTableEditDetach();
}

// ---- item 5: a second commit of one cell keeps the key-removal mark ---------
{
  const a5 = deep(FIX);
  const dom5 = buildDom(a5, ['species_ranges']);
  S.rcaTableEditAttach(dom5, a5, { editable: true });
  H.clear();
  const c5 = cellOf(dom5, 'species_ranges', 0, 4);   // biozone 'Z1'
  dom5.dispatchEvent(mkEvent('focusin', c5));
  c5.textContent = '';
  S.rcaEditHandleKey(mkEvent('keydown', c5, { key: 'Enter' }));   // commit 1: key removed
  check('audit5-double-commit-keeps-dirty-mark', (function () {
    const midCount = S.rcaTableEdits.editedCount();
    dom5.dispatchEvent(mkEvent('focusin', c5));                   // re-enter the cell
    c5.textContent = '';
    dom5.dispatchEvent(mkEvent('focusout', c5));                  // blur-commit path #2
    S.rcaTableEdits.editCell('species_ranges', 0, 4, '');         // forced third commit
    return midCount === 1 && S.rcaTableEdits.editedCount() === 1
      && S.rcaTableEdits.isCellEdited('species_ranges', 0, 'biozone')
      && eqJson(S.rcaTableEdits.capture('species_ranges'), { 0: { _deleted_keys: ['biozone'] } });
  })(), 'js/table.js editCell FE-FIX item 4: markCell re-derives, never blind-clears');
  H.clear();
  S.rcaTableEditDetach();
}

// ---- item 6: deleteRow drops the deleted row's marks; undo restores them ----
{
  const a6 = deep(FIX);
  freshEdits(a6);
  S.rcaTableEdits.editCell('species_ranges', 1, 0, 'B*');
  const del6 = S.rcaTableEdits.deleteRow('species_ranges', 1);
  check('audit6-deleterow-drops-own-marks', del6 && S.rcaTableEdits.editedCount() === 0
    && !S.rcaTableEdits.isCellEdited('species_ranges', 0, 'species')
    && !S.rcaTableEdits.isCellEdited('species_ranges', 1, 'species'),
    'the old shiftEdited(rowIdx,-1) parked the deleted row\'s mark on the PREVIOUS row');
  const back6 = S.rcaTableEdits.undoDeleteRow('species_ranges', 1, del6.item);
  check('audit6-undo-deleterow-restores-marks', back6 === true
    && S.rcaTableEdits.isCellEdited('species_ranges', 1, 'species')
    && S.rcaTableEdits.editedCount() === 1,
    'stash/restore by (row, item) — FE-FIX item 5 popEditedRow/takeStashedMarks');
  check('audit6-surviving-marks-still-shift', (function () {
    freshEdits(a6);
    S.rcaTableEdits.editCell('species_ranges', 1, 0, 'B**');
    S.rcaTableEdits.deleteRow('species_ranges', 0);   // row 1 walks up to 0
    return S.rcaTableEdits.isCellEdited('species_ranges', 0, 'species');
  })(), 'a mark on a row that MOVED must move with it — only the deleted row dies clean');
  S.rcaTableEdits.clearEdited();
}

// ---- item 7: undo of a clear on an originally-EMPTY cell keeps the key ------
{
  const a7 = { sections: [{ name: 'S1', age_range: '', formations: [],
    formation_thickness_m: '', coordinates: '' }], species_ranges: [] };
  freshEdits(a7);
  H.clear();
  const formCol7 = fieldCol(cfgOf(a7, 'sections'), 'formations');
  const ed7 = S.rcaTableEdits.editCell('sections', 0, formCol7, 'A;B');
  check('audit7-empty-baseline-edit', ed7.ok === true && ed7.changed === true
    && eqJson(a7.sections[0].formations, ['A', 'B']), canonical(a7.sections[0]));
  H.push(ed7.action);
  H.undo();
  check('audit7-empty-baseline-undo-keeps-key', (function () {
    const row = a7.sections[0];
    return Object.prototype.hasOwnProperty.call(row, 'formations')
      && eqJson(row.formations, [])
      && eqJson(S.rcaTableEdits.capture('sections'), {});
  })(), 'editable.py:290 ASSIGNS the restored [] (no _deleted_keys) — '
    + 'rcaEditKeepsEmptyKey mirrors it; the old JS deleted the key');
  check('audit7-apply-parity-empty-list', (function () {
    const applied = S.rcaApplyEdits({ sections: [{ name: 'S1', formations: [] }] },
      { sections: { 0: { formations: [] } } });
    return Object.prototype.hasOwnProperty.call(applied.sections[0], 'formations')
      && eqJson(applied.sections[0].formations, []);
  })(), 'cross-checked against rca_core/editable.py apply_edits (item[col] = deepcopy(val))');
  H.clear();
}

// ---- item 8: inverted idx pairs on the nested columnar tables are refused --
{
  const a8 = {
    chart_mode: 'columnar_section',
    sections: [{
      id: 'S1', name: 'S1',
      lithology_blocks: [{ pattern: 'mud', range_top_idx: 1, range_base_idx: 9 }],
      age_units: [{ label: 'U1', range_top_idx: 1, range_base_idx: 9 }],
      samples: [],
    }],
  };
  freshEdits(a8);
  const blkCfg8 = cfgOf(a8, 'lithology_blocks');
  const blkBaseCol8 = fieldCol(blkCfg8, 'base_idx');
  const rej8 = S.rcaTableEdits.editCell('lithology_blocks', 0, blkBaseCol8, '2');
  check('audit8-lithology-inverted-rejected', rej8.ok === false
    && rej8.key === 'edit.idxInverted'
    && a8.sections[0].lithology_blocks[0].range_base_idx === 9,
    'top 1 / base 2 is inverted (bed rule: top < base) — the row is keyed by '
    + 'range_top_idx while peer says top_idx; only peerModel closes the gap (FE-FIX item 7)');
  const auCfg8 = cfgOf(a8, 'age_units');
  const rejAu = S.rcaTableEdits.editCell('age_units', 0, fieldCol(auCfg8, 'base_idx'), '2');
  check('audit8-age-units-inverted-rejected', rejAu.ok === false
    && a8.sections[0].age_units[0].range_base_idx === 9, canonical(rejAu));
  const ok8 = S.rcaTableEdits.editCell('lithology_blocks', 0, blkBaseCol8, '1');
  check('audit8-inverted-control-accepts', ok8.ok === true
    && a8.sections[0].lithology_blocks[0].range_base_idx === 1,
    'top 1 / base 1 is legal — the refusal above is the pair rule, not a dead column');
  S.rcaTableEdits.detach();
}

// ---- item 9: after a structural op the toolbar reflects the stack depth -----
{
  // The REAL app re-renders the whole results root, and rcaEditToolbar emits
  // FRESH `disabled` undo/redo buttons on every write (they are part of the
  // innerHTML). buildDom's setter would just drop a hand-appended button, so
  // this fixture replays the app's behaviour: wipe, emit a fresh disabled
  // toolbar, refill the tables. The fix (js/table.js rcaEditRerender, FE-FIX
  // item 8) re-runs rcaHistorySyncButtons AFTER the swap.
  const a9 = deep(FIX);
  const barRoot = makeEl('DIV');
  barRoot.rcaData = a9;
  let barHtml = '';
  Object.defineProperty(barRoot, 'innerHTML', {
    get: () => barHtml,
    set: (v) => {
      barHtml = String(v);
      barRoot.children.length = 0;
      barRoot.appendChild(makeEl('BUTTON', { 'data-rca-undo': '1', disabled: 'disabled' }));
      barRoot.appendChild(makeEl('BUTTON', { 'data-rca-redo': '1', disabled: 'disabled' }));
      fillDom(barRoot, a9, ['species_ranges']);
    },
    enumerable: true,
  });
  barRoot.innerHTML = '';                       // initial render == fresh toolbar
  S.rcaTableEditAttach(barRoot, a9, { editable: true });
  // The app.js wiring: a stack subscription that re-syncs the CURRENT toolbar
  // nodes on every push/undo/redo.
  S.rcaHistoryAttachUi(barRoot);
  H.clear();
  check('audit9-fresh-toolbar-starts-disabled',
    barRoot.querySelector('[data-rca-undo]').getAttribute('disabled') !== null);
  barRoot.dispatchEvent(mkEvent('click', barRoot.querySelector('[data-rca-addrow="species_ranges"]')));
  const afterAdd = barRoot.querySelector('[data-rca-undo]');
  check('audit9-undo-enabled-after-structural-rerender', H.depth() === 1
    && afterAdd.getAttribute('disabled') === null
    && afterAdd.getAttribute('aria-disabled') === 'false',
    'the fresh post-addRow toolbar used to stay disabled while the stack had the rowAdd');
  S.rcaHistory.undo();                           // -> afterHistory -> re-render again
  const afterUndo = barRoot.querySelector('[data-rca-undo]');
  const afterUndoRedo = barRoot.querySelector('[data-rca-redo]');
  check('audit9-toolbar-resynced-after-undo-rerender', H.depth() === 0
    && afterUndo !== afterAdd && afterUndo.getAttribute('disabled') !== null
    && afterUndoRedo.getAttribute('disabled') === null,
    'undo empties the stack -> the NEW toolbar ends up undo-disabled, redo-enabled '
    + '(rerender sync + the stack subscription; the in-apply sync runs pre-pop) — '
    + canonical([H.depth(), afterUndo === afterAdd, afterUndo.getAttribute('disabled'),
      afterUndoRedo.getAttribute('disabled')]));
  H.clear();
  S.rcaTableEditDetach();
}

// ---- item 10: paste inserts PLAIN TEXT only ---------------------------------
{
  const a10 = deep(FIX);
  const dom10 = buildDom(a10, ['species_ranges']);
  S.rcaTableEditAttach(dom10, a10, { editable: true });
  const cell10 = cellOf(dom10, 'species_ranges', 1, 0);   // species 'B'
  cell10.textContent = 'B';
  const seenFormats = [];
  const paste10 = mkEvent('paste', cell10, { clipboardData: {
    getData: (fmt) => {
      seenFormats.push(fmt);
      return fmt === 'text/plain' ? 'p10text' : '<table><tr><td><b>EXCEL</b></td></tr></table>';
    },
  } });
  dom10.dispatchEvent(paste10);
  check('audit10-paste-plain-text-only', paste10.defaultPrevented === true
    && seenFormats.indexOf('text/plain') !== -1
    && cell10.textContent === 'Bp10text'
    && cell10.textContent.indexOf('<b>') === -1,
    'rcaEditOnPaste cancels the native paste and appends text/plain only (FE-FIX item 9)');
  const plainTarget = makeEl('TH');
  const paste10b = mkEvent('paste', plainTarget, { clipboardData: { getData: () => 'x' } });
  dom10.dispatchEvent(paste10b);
  check('audit10-paste-ignores-noneditable', paste10b.defaultPrevented === false,
    'outside an editable cell the native behaviour must stay untouched');
  S.rcaTableEditDetach();
}

// ---- item 11: table.js no longer clobbers viz.js / minimax.js globals -------
{
  // The renamed helpers (js/table.js FE-FIX items 10 + the viz-guard block):
  // the OLD public names must be absent from the sandbox, the new private
  // names present. (history.js/app.js never define them either — table.js
  // alone leaked them before.)
  check('audit11-no-leaked-globals', S.__ctx.rcaVizFocusRow === undefined
    && S.__ctx.rcaVizClearFocus === undefined && S.__ctx.rcaVizLocateTo === undefined
    && S.__ctx.rcaPyStr === undefined,
    'rcaVizFocusRow/rcaVizClearFocus/rcaVizLocateTo/rcaPyStr must not exist on the context');
  check('audit11-renamed-helpers-exist', typeof S.__ctx.rcaEditVizFocus === 'function'
    && typeof S.__ctx.rcaEditVizClearFocus === 'function'
    && typeof S.__ctx.rcaEditVizLocateTo === 'function'
    && typeof S.__ctx.rcaEditPyStr === 'function'
    && S.rcaEditPyStr(true) === 'True' && S.rcaEditPyStr(null) === '',
    'the table.js-private copies carry the same Python str() semantics');
}

// ---- item 12: rcaPyParseNumber runs the PEP-515 grammar on the RAW text ----
{
  check('audit12-underscore-grammar', S.rcaPyParseNumber('_1') === null
    && S.rcaPyParseNumber('1_') === null && S.rcaPyParseNumber('1__0') === null
    && S.rcaPyParseNumber('1_0') === 10 && S.rcaPyParseNumber('1_000') === 1000,
    'int("_1")/int("1_")/int("1__0") raise; int("1_0")==10 — the old test stripped '
    + 'underscores first, which accepted all three rejects (FE-FIX item 12)');
  check('audit12-ascii-digits-only', S.rcaPyParseNumber('\u0663\u0665') === null
    && S.rcaCoerceCellValue('\u0663', 'int') === '\u0663',
    'Arabic-Indic digits: JS \\d admits them, CPython int() rejects them');
}

// ---- item 13: the Python str(float) grammar --------------------------------
{
  check('audit13-pyfloatstr-grammar', S.rcaPyFloatStr(3) === '3.0'
    && S.rcaPyFloatStr(2.5) === '2.5' && S.rcaPyFloatStr(1e16) === '1e+16'
    && S.rcaPyFloatStr(1e-7) === '1e-07' && S.rcaPyFloatStr(0.0001) === '0.0001',
    'integral float -> ".0", decpt<=-4 or >16 -> 2-digit padded exponent (str(3.0)="3.0")');
  check('audit13-exportcelltext-routing', S.rcaExportCellText(2.5) === '2.5'
    && S.rcaExportCellText(3) === '3' && S.rcaExportCellText(true) === 'True'
    && S.rcaExportCellText(Infinity) === '',
    'JSON collapses 3.0 to the int 3 (documented limitation, js/table.js:696) — '
    + 'the float grammar is pinned on rcaPyFloatStr above');
}

// ---- item 14: the editable branch keeps its title tooltip -------------------
{
  check('audit14-editable-cell-title-attr', (function () {
    const html = S.rcaRenderResults(deep(FIX), '', { editable: true });
    return /<td class="[^"]*rca-edit-cell[^"]*"[^>]*title="A"/.test(html);
  })(), 'the read-only path had title=, the editable one used to drop it (FE-FIX item 14)');
  const a14 = deep(FIX);
  const dom14 = buildDom(a14, ['species_ranges']);
  S.rcaTableEditAttach(dom14, a14, { editable: true });
  const c14 = cellOf(dom14, 'species_ranges', 0, 0);
  dom14.dispatchEvent(mkEvent('focusin', c14));
  c14.textContent = 'T14';
  S.rcaEditCommitCell(c14);
  check('audit14-paint-keeps-title', c14.getAttribute('title') === 'T14',
    'rcaEditPaintCell sets title on every repaint');
  S.rcaTableEditDetach();
}

// ---- item 15: detach / re-wire honour the capture flag ----------------------
{
  const a15 = deep(FIX);
  const dom15 = buildDom(a15, ['species_ranges']);
  const countL15 = (el) => Object.keys(el._listeners)
    .reduce((n, k) => n + el._listeners[k].length, 0);
  S.rcaTableEditAttach(dom15, a15, { editable: true, capture: true });
  const wired15 = countL15(dom15);
  S.rcaTableEditAttach(dom15, a15, { editable: true });     // flag flip on the SAME root
  check('audit15-capture-flip-rewires-once', wired15 === countL15(dom15)
    && wired15 > 0 && S.RCA_EDIT_DOM.capture === false,
    'js/table.js FE-FIX item 15: remove+add under the OLD flag, bookkeeping updated');
  S.rcaTableEditDetach();
  check('audit15-detach-unhooks-everything', countL15(dom15) === 0 && (function () {
    const c15 = cellOf(dom15, 'species_ranges', 0, 0);
    dom15.dispatchEvent(mkEvent('focusin', c15));
    return S.RCA_EDIT_DOM.editing === null;
  })(), 'after detach no delegated listener may still fire');
}

// ---- item 16: rcaApplyEdits negative new_-index mirrors CPython insert ------
{
  const neg16 = S.rcaApplyEdits(
    { species_ranges: [{ species: 'A' }, { species: 'B' }] },
    { species_ranges: { 'new_-1': { species: 'X' } } });
  const far16 = S.rcaApplyEdits(
    { species_ranges: [{ species: 'A' }, { species: 'B' }] },
    { species_ranges: { 'new_-5': { species: 'X' } } });
  check('audit16-negative-insert-mirrors-python', (function () {
    const ids = (o) => o.species_ranges.map((r) => r.species).join(',');
    // CPython [A,B].insert(-1, X) -> [A, X, B]; insert(-5, X) floors at 0.
    return ids(neg16) === 'A,X,B' && ids(far16) === 'X,A,B';
  })(), 'the old clamp folded every negative to 0 — the browser and the GUI '
    + 'disagreed on where new_-1 lands (editable.py:299, table.js FE-FIX item 17)');
}

// ---- item 17: the ghost focusout after an innerHTML swap is skipped ---------
{
  const a17 = deep(FIX);
  const dom17 = buildDom(a17, ['species_ranges']);
  S.rcaTableEditAttach(dom17, a17, { editable: true });
  const c17 = cellOf(dom17, 'species_ranges', 0, 0);
  dom17.dispatchEvent(mkEvent('focusin', c17));            // registers the edit record
  c17.textContent = 'GHOST';
  c17.parentNode = null;                                    // innerHTML swap killed the node
  const res17 = S.rcaEditCommitCell(c17);
  check('audit17-detached-cell-commit-skipped', res17.skipped === true
    && res17.changed === false && a17.species_ranges[0].species === 'A'
    && S.RCA_EDIT_DOM.editing === null,
    'rcaEditCellDetached (FE-FIX item 18): stale value must never reach the model');
  const plain17 = makeEl('TD', { 'data-rca-edit': '1', 'data-table': 'species_ranges',
    'data-row': '1', 'data-col': '0', 'data-field': 'species', 'data-type': 'str',
    contenteditable: 'true' });
  plain17.textContent = 'P*';
  const res17b = S.rcaEditCommitCell(plain17);
  check('audit17-untracked-plain-cell-still-commits', res17b.ok === true
    && res17b.changed === true && a17.species_ranges[1].species === 'P*',
    'only the CURRENTLY TRACKED cell can be a ghost — the app.js/Qt bridge API stays legal');
  S.rcaTableEditDetach();
}

// ---- item 18: selected rows carry .rca-row-selected on the <tr> -------------
{
  const a18 = deep(FIX);
  const dom18 = buildDom(a18, ['species_ranges']);
  S.rcaTableEditAttach(dom18, a18, { editable: true });
  const box18 = dom18.querySelector('[data-row-select="species_ranges"][data-row="0"]');
  box18.checked = true;
  dom18.dispatchEvent(mkEvent('change', box18));
  const tr18 = rowOf(dom18, 'species_ranges', 0);
  const onWhileSelected = tr18.classList.contains('rca-row-selected')
    && !rowOf(dom18, 'species_ranges', 1).classList.contains('rca-row-selected');
  dom18.dispatchEvent(mkEvent('click',
    dom18.querySelector('[data-rca-sel-action="clear"][data-rca-sel-table="species_ranges"]')));
  check('audit18-row-selected-class-toggles', onWhileSelected
    && !rowOf(dom18, 'species_ranges', 0).classList.contains('rca-row-selected'),
    'css/table-edit.css paints tr.rca-row-selected — table.js FE-FIX item 19 must emit it');
  S.rcaTableEditDetach();
}

// ---- item 19: the sticky focus columns are pinned in RENDER order -----------
{
  // The emitted tbody row is [td.rca-cell-select | th[scope=row] | data...]
  // (js/table.js:1004-1009), so the checkbox column must pin at the SMALLER
  // left offset; the old CSS had them swapped (row index at 0, checkbox at 34px).
  // NOTE: the CSS uses the bare `left: 0` zero form (no unit), so `px` is
  // optional in the probe regexes.
  const selM = css.match(/td\.rca-cell-select\s*\{[^}]*?left:\s*(\d+)(?:px)?/);
  const idxM = css.match(/th\[scope="row"\]\s*\{[^}]*?left:\s*(\d+)(?:px)?/);
  check('audit19-sticky-column-order', !!selM && !!idxM
    && Number(selM[1]) < Number(idxM[1]),
    'checkbox left:' + (selM && selM[1]) + 'px must precede row-index left:'
    + (idxM && idxM[1]) + 'px (css/table-edit.css FE-FIX item 11)');
  const firstCell = /<tr[^>]*data-row="0"[^>]*>\s*<td class="rca-cell-select"/
    .test(S.rcaRenderResults(deep(FIX), '', { editable: true }));
  check('audit19-emitted-order-matches-css', firstCell,
    'the FIRST cell of an edit-mode tbody row is the checkbox — the CSS pinning follows it');
}

// ===========================================================================

console.log('\n--- ' + pass + ' passed, ' + fail + ' failed ---');
if (fail) console.log('failed: ' + failures.join(', '));
process.exit(fail ? 1 : 0);
