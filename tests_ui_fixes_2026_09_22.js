// ===========================================================================
// tests_ui_fixes_2026_09_22.js — FE-FIX-2026-09-22 round (annotations FE-AUDIT
// items 1-11 + the aud4 rcaExportCellText parity leftover)
// ===========================================================================
//
// Run with:  node tests_ui_fixes_2026_09_22.js
//
// Sandbox / bootstrap convention copied from tests_edit_history.js: plain
// readFileSync + vm (no ES modules, no jsdom). js/table.js and js/history.js
// stay top-level globals, so every function and const can be pulled by bare
// name; js/viz.js's typography entry point (item 10) is loaded in a second,
// DOM-free context exactly like tests_viz.js does.
//
// One section per FE-AUDIT item, plus a table-driven parity block for
// rcaExportCellText (dict/list/nested/quote-edge/bool/float/non-finite) whose
// cases are mirrored against the real Python _export_cell_text in
// tests/test_table_export_cell_text_parity_2026_09_22.py.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

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
function deep(v) { return JSON.parse(JSON.stringify(v === undefined ? null : v)); }
function isDict(v) { return !!v && typeof v === 'object' && !Array.isArray(v); }

// js/i18n.js warns "[i18n] missing translation" for every edit.* key it does
// not carry — that is exactly RCA_EDIT_STRINGS' job, so the noise is filtered.
function quietConsole() {
  const drop = (m) => typeof m === 'string' && m.indexOf('[i18n]') === 0;
  return {
    log: (...args) => { if (!drop(args[0])) console.log(...args); },
    warn: (...args) => { if (!drop(args[0])) console.warn(...args); },
    error: (...args) => console.error(...args),
  };
}

// ---------------------------------------------------------------------------
// DOM stub (copied verbatim from tests_edit_history.js so the render paths
// that item 2/5/6 drive behave identically here).
// ---------------------------------------------------------------------------
let ACTIVE = null;

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

// ---------------------------------------------------------------------------
// table.js / history.js sandbox
// ---------------------------------------------------------------------------
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
      createElement: (tag) => makeEl(tag),
      createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
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
  for (const f of ['js/config.js', 'js/i18n.js', 'js/ics_table.js', 'js/reason-codes.js',
    'js/table.js', 'js/history.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), ctx, { filename: f });
  }
  const run = (code) => vm.runInContext(code, ctx);
  const names = [
    'rcaTableEdits', 'rcaTableConfigs', 'rcaRowsForTable', 'rcaTableLowConfidenceRows',
    'rcaTableEditAttach', 'rcaEditRefFromCell', 'rcaEditCommitCell', 'rcaEditOnPaste',
    'rcaEditLocateRow', 'rcaEditOnClick', 'rcaEditOnMouseOver', 'rcaEditAnnounce',
    'rcaEditVizFocus', 'rcaEditVizClearFocus', 'rcaEditVizLocateTo', 'rcaEditVizTracksTable',
    'rcaEditScalarListCfg', 'rcaEditAddRowButton', 'rcaEditRefreshConfigs', 'rcaEditRerender',
    'rcaColEditSpec', 'rcaEditFieldOf', 'rcaEditT', 'rcaSetLang',
    'RCA_EDIT_STRINGS', 'RCA_EDIT_DOM',
    'rcaPyFloatStr', 'rcaExportCellText', 'rcaPyRepr',
    'rcaHistory', 'rcaHistoryActionIsStale',
  ];
  const out = { __ctx: ctx, __run: run };
  for (const n of names) {
    try { out[n] = run(n); } catch (e) { out[n] = undefined; }
  }
  for (const n of names) {
    if (out[n] === undefined) {
      throw new Error('sandbox is missing ' + n + ' — js/table.js or js/history.js changed shape');
    }
  }
  return out;
}

const S = makeContext();

// js/viz.js loads DOM-free (see tests_viz.js). Second context for item 10.
function makeVizContext() {
  const ctx = { console, setTimeout, clearTimeout, Date, Math, JSON };
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const f of ['js/ics_table.js', 'js/viz.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, f), 'utf8'), ctx, { filename: f });
  }
  const run = (code) => vm.runInContext(code, ctx);
  return { rcaVizMaValue: run('rcaVizMaValue'), __run: run };
}
const V = makeVizContext();

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------
const FIX = {
  runs: 2,
  sections: [{ name: 'S1', age_range: 'Cambrian' }],
  species_ranges: [
    { species: 'A', section: 'S1', range_base: '1', range_top: '3', biozone: 'Z1', agreement: '2/2' },
    { species: 'B', section: 'S1', range_base: '2', range_top: '4', biozone: 'Z2', agreement: '1/2', confidence: 0.4 },
    { species: 'C', section: 'S1', range_base: '5', range_top: '6', biozone: 'Z3', agreement: '2/2' },
  ],
  biozones: [{ name: 'Z1', section: 'S1', age: 'Cambrian', thickness_m: 10 }],
  other_fossils: ['Ammonite: X'],
};

function cfgOf(data, id) {
  for (const cfg of S.rcaTableConfigs(data) || []) if (cfg.id === id) return cfg;
  return null;
}
function fieldCol(cfg, field) {
  return (cfg.edit || []).findIndex((s) => s && s.field === field);
}

function fillDom(root, data, onlyIds) {
  const cfgs = (S.rcaTableConfigs(data) || []).filter((c) => !onlyIds || onlyIds.indexOf(c.id) !== -1);
  for (const cfg of cfgs) {
    const lowRows = S.rcaTableLowConfidenceRows(data, cfg.id);
    const section = makeEl('DIV', { 'data-table': cfg.id, 'data-editable': '1', class: 'result-section' });
    const wrap = makeEl('DIV', { 'data-table-nav': cfg.id, class: 'table-wrap', tabindex: '0' });
    const table = makeEl('TABLE', { class: 'data-table' });
    const thead = makeEl('THEAD');
    const htr = makeEl('TR');
    htr.appendChild(makeEl('TH'));
    thead.appendChild(htr);
    table.appendChild(thead);
    const tbody = makeEl('TBODY');
    S.rcaRowsForTable(data, cfg.id).forEach((item, ri) => {
      const tr = makeEl('TR', { 'data-row': String(ri) });
      tr.className = lowRows.indexOf(ri) !== -1 ? 'rca-row-lowconf' : '';
      tr.appendChild(makeEl('TD'));
      (cfg.edit || []).forEach((spec, ci) => {
        if (!spec || !spec.editable) { tr.appendChild(makeEl('TD')); return; }
        const cell = makeEl('TD', {
          'data-rca-edit': '1', 'data-table': cfg.id, 'data-row': String(ri), 'data-col': String(ci),
          contenteditable: 'true', class: 'rca-edit-cell', spellcheck: 'false',
        });
        if (spec.scalar) cell.setAttribute('data-scalar', '1');
        else cell.setAttribute('data-field', spec.model || spec.field);
        cell.setAttribute('data-type', spec.type || 'str');
        tr.appendChild(cell);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    section.appendChild(wrap);
    const foot = makeEl('DIV', { class: 'rca-table-foot' });
    foot.appendChild(makeEl('BUTTON', { 'data-rca-addrow': cfg.id, class: 'rca-addrow-btn' }));
    section.appendChild(foot);
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

// ===========================================================================
// ITEM 1 — re-attaching the SAME data object preserves edits / selection
// ===========================================================================
(function () {
  const data = deep(FIX);
  S.rcaTableEdits.attach(data);
  // A real edit: mutates the live row + sets the dirty mark + pushes nothing to
  // the undo stack, but re-baselines is NOT the point here.
  const res = S.rcaTableEdits.editCell('species_ranges', 0, fieldCol(cfgOf(data, 'species_ranges'), 'species'), 'A*');
  const editedKeys = Object.keys(S.rcaTableEdits._edited['species_ranges'] || {});
  S.rcaTableEdits.toggleRow('species_ranges', 2, true);
  const selectionBefore = S.rcaTableEdits.selectedRows('species_ranges');
  const snapshotSpeciesBefore = S.rcaTableEdits.snapshot.species_ranges[0].species;

  // Re-attach the SAME object (app.js renderCurrentResult path).
  S.rcaTableEdits.attach(data);
  const editedAfter = Object.keys(S.rcaTableEdits._edited['species_ranges'] || {});
  const selectionAfter = S.rcaTableEdits.selectedRows('species_ranges');
  const snapshotSpeciesAfter = S.rcaTableEdits.snapshot.species_ranges[0].species;

  check('item1-same-object-keeps-dirty', res.ok && editedKeys.length > 0 && editedAfter.length === editedKeys.length,
    canonical([editedKeys, editedAfter]));
  check('item1-same-object-keeps-selection', selectionBefore.indexOf(2) !== -1 && selectionAfter.indexOf(2) !== -1,
    canonical([selectionBefore, selectionAfter]));
  check('item1-same-object-no-rebaseline', snapshotSpeciesBefore === 'A' && snapshotSpeciesAfter === 'A'
    && data.species_ranges[0].species === 'A*',
    snapshotSpeciesAfter + '/' + data.species_ranges[0].species);
  check('item1-live-identity-preserved', S.rcaTableEdits.live() === data);

  // A DIFFERENT object must fully reset (the documented attach contract).
  S.rcaTableEdits.attach(deep(FIX));
  check('item1-different-object-resets', Object.keys(S.rcaTableEdits._edited).length === 0
    && S.rcaTableEdits.selectedRows('species_ranges').length === 0);
}());

// ===========================================================================
// ITEM 2 — scalar-list tables: no addRow button, refusal at every layer
// ===========================================================================
(function () {
  const data = deep(FIX);
  const cfg = cfgOf(data, 'other_fossils');
  check('item2-scalar-cfg-detected', S.rcaEditScalarListCfg(cfg) === true);
  check('item2-no-addrow-button', S.rcaEditAddRowButton(cfg) === '', JSON.stringify(S.rcaEditAddRowButton(cfg)));
  S.rcaTableEdits.attach(data);
  check('item2-addRow-refused', S.rcaTableEdits.addRow('other_fossils', data.other_fossils.length) === null);
  check('item2-refusal-string-localized', !!(S.RCA_EDIT_STRINGS['edit.addRowScalar']
    && S.RCA_EDIT_STRINGS['edit.addRowScalar'].zh && S.RCA_EDIT_STRINGS['edit.addRowScalar'].en));

  // The click handler must refuse LOUDLY even for stale markup that still has
  // a button, and must not mutate the list.
  const dom = buildDom(deep(FIX), ['species_ranges', 'other_fossils']);
  const live = deep(FIX);
  S.rcaTableEditAttach(dom, live, { editable: true });
  const btn = dom.querySelector('[data-rca-addrow="other_fossils"]');
  const lenBefore = live.other_fossils.length;
  dom.dispatchEvent(mkEvent('click', btn));
  const liveRegion = dom.querySelector('[data-rca-edit-status]');
  const announced = liveRegion ? String(liveRegion.textContent) : '';
  check('item2-click-refuses-loudly', announced && announced === S.RCA_EDIT_STRINGS['edit.addRowScalar'].zh
    && live.other_fossils.length === lenBefore, JSON.stringify([announced, live.other_fossils.length]));
}());

// ===========================================================================
// ITEM 3 — resolve the write spec via the cell's own data-field, not the
//          (possibly drifted) attach-time column index
// ===========================================================================
(function () {
  const data = deep(FIX);
  const dom = buildDom(data, ['species_ranges']);
  S.rcaTableEditAttach(dom, data, { editable: true });
  const cfg = cfgOf(data, 'species_ranges');
  const bioCol = fieldCol(cfg, 'biozone');
  const spCol = fieldCol(cfg, 'species');

  // A cell whose data-col index (spCol) disagrees with its data-field (biozone):
  // the data-field must win (this is the exact column-drift scenario).
  const drifted = makeEl('TD', {
    'data-rca-edit': '1', 'data-table': 'species_ranges', 'data-row': '0',
    'data-col': String(spCol), 'data-field': 'biozone', contenteditable: 'true',
  });
  const ref = S.rcaEditRefFromCell(drifted);
  check('item3-ref-resolves-by-data-field', !!ref && ref.spec && ref.spec.field === 'biozone',
    ref && ref.spec && ref.spec.field);
  // Without a data-field the stale index is still honoured (legacy fallback).
  const legacy = makeEl('TD', {
    'data-rca-edit': '1', 'data-table': 'species_ranges', 'data-row': '0',
    'data-col': String(spCol), contenteditable: 'true',
  });
  const refL = S.rcaEditRefFromCell(legacy);
  check('item3-index-fallback', !!refL && refL.spec.field === 'species', refL && refL.spec.field);

  // opts.spec drives the WRITE to the right field even if colIdx names another.
  const bioSpec = S.rcaTableEdits.specByField('species_ranges', 'biozone');
  const w = S.rcaTableEdits.editCell('species_ranges', 0, spCol, 'Z9', { spec: bioSpec });
  check('item3-opts-spec-writes-correct-field', w.ok && w.changed
    && data.species_ranges[0].biozone === 'Z9' && data.species_ranges[0].species === 'A',
    JSON.stringify([data.species_ranges[0].biozone, data.species_ranges[0].species]));
  void bioCol;
}());

// ===========================================================================
// ITEM 3 / 7 — rcaEditRerender re-registers FRESH configs (no stale closure)
// ===========================================================================
(function () {
  const data = deep(FIX);
  S.rcaTableEdits.attach(data);
  // Poison the registry with a bogus cfg, then prove the refresh re-reads the
  // live configs (rcaTableEdits.setConfig is the write the old closure lacked).
  S.rcaTableEdits.setConfig({ id: 'species_ranges', edit: [], columns: [] });
  check('item7-bogus-cfg-poisoned', S.rcaTableEdits.cfg('species_ranges').edit.length === 0);
  const n = S.rcaEditRefreshConfigs();
  const fresh = S.rcaTableEdits.cfg('species_ranges');
  check('item7-rerender-refreshes-fresh-cfgs', n > 0 && fieldCol(fresh, 'species') >= 0,
    canonical([n, fresh.edit.length]));
}());

// ===========================================================================
// ITEM 4 — rcaPyFloatStr fixed branch strips leading zeros (matches str(float))
// ===========================================================================
(function () {
  const cases = [
    [1e-5, '1e-05'], [1.5e-5, '1.5e-05'], [-2e-5, '-2e-05'], [9.99e-5, '9.99e-05'],
    [0.0001, '0.0001'], [1.5, '1.5'], [0.1, '0.1'], [100.0, '100.0'],
  ];
  for (const [num, want] of cases) {
    check('item4-str-float-' + num, S.rcaPyFloatStr(num) === want, S.rcaPyFloatStr(num) + ' vs ' + want);
  }
}());

// ===========================================================================
// ITEM 5 — clipboard paste collapses [\r\n\t]+ to a single space
// ===========================================================================
(function () {
  const data = deep(FIX);
  const dom = buildDom(data, ['species_ranges']);
  S.rcaTableEditAttach(dom, data, { editable: true });
  const cell = cellOf(dom, 'species_ranges', 0, fieldCol(cfgOf(data, 'species_ranges'), 'species'));
  cell.textContent = '';
  const ev = mkEvent('paste', cell, { clipboardData: { getData: () => 'q\r\nw\t\te\n\nf' } });
  S.rcaEditOnPaste(ev);
  check('item5-paste-collapses-control-chars', ev.defaultPrevented
    && cell.textContent === 'q w e f' && !/[\r\n\t]/.test(cell.textContent),
    JSON.stringify(cell.textContent));
  // Words preserved (not truncated to the first cell).
  const cell2 = cellOf(dom, 'species_ranges', 1, fieldCol(cfgOf(data, 'species_ranges'), 'species'));
  cell2.textContent = 'X';
  S.rcaEditOnPaste(mkEvent('paste', cell2, { clipboardData: { getData: () => 'A\tB' } }));
  check('item5-paste-keeps-both-cells', cell2.textContent === 'XA B', JSON.stringify(cell2.textContent));
}());

// ===========================================================================
// ITEM 6 — canvas / locate / hover wiring is species_ranges ONLY
// ===========================================================================
(function () {
  check('item6-tracks-table', S.rcaEditVizTracksTable('species_ranges') === true
    && S.rcaEditVizTracksTable('sections') === false
    && S.rcaEditVizTracksTable('biozones') === false);

  // Install a fake viz namespace so a call is observable.
  S.__run("globalThis.__viz = []; globalThis.rcaViz = {"
    + "focusRow: function (i) { __viz.push(['focusRow', i]); return true; },"
    + "clearFocus: function () { __viz.push(['clearFocus']); return true; },"
    + "locateTo: function (i) { __viz.push(['locateTo', i]); return true; }};");
  const Viz = () => JSON.parse(S.__run('JSON.stringify(globalThis.__viz)'));
  const reset = () => S.__run('globalThis.__viz.length = 0;');

  reset();
  check('item6-focus-non-species-blocked', S.rcaEditVizFocus(0, 'sections') === false && Viz().length === 0);
  check('item6-clear-non-species-blocked', S.rcaEditVizClearFocus('biozones') === false && Viz().length === 0);
  reset();
  check('item6-focus-species-allowed', S.rcaEditVizFocus(1, 'species_ranges') === true
    && Viz().length === 1 && Viz()[0][0] === 'focusRow' && Viz()[0][1] === 1);

  // 定位 on a non-species table returns false and never touches the canvas.
  const data = deep(FIX);
  const dom = buildDom(data, ['species_ranges', 'sections']);
  S.rcaTableEditAttach(dom, data, { editable: true });
  reset();
  const ok = S.rcaEditLocateRow('sections', 0);
  const region = dom.querySelector('[data-rca-edit-status]');
  check('item6-locate-non-species-refused', ok === false && Viz().length === 0
    && region && String(region.textContent) === S.RCA_EDIT_STRINGS['edit.noViz'].zh,
    canonical([ok, Viz(), region && region.textContent]));
}());

// ===========================================================================
// ITEM 8 — history insert/undo comment-vs-behavior (row > tail is stale,
//          row === tail still legal because Python list.insert appends)
// ===========================================================================
(function () {
  const data = deep(FIX);
  S.rcaTableEdits.attach(data); // species_ranges has 3 rows
  const edits = S.rcaTableEdits;
  const stale = S.rcaHistoryActionIsStale({ type: 'rowDelete', tableId: 'species_ranges', row: 5 }, 'undo', edits);
  const tailOk = S.rcaHistoryActionIsStale({ type: 'rowDelete', tableId: 'species_ranges', row: 3 }, 'undo', edits);
  const withinOk = S.rcaHistoryActionIsStale({ type: 'rowDelete', tableId: 'species_ranges', row: 1 }, 'undo', edits);
  const removeStale = S.rcaHistoryActionIsStale({ type: 'rowAdd', tableId: 'species_ranges', row: 9 }, 'undo', edits);
  check('item8-insert-beyond-tail-stale', stale === true && removeStale === true);
  check('item8-insert-at-tail-legal', tailOk === false);
  check('item8-insert-within-legal', withinOk === false);
}());

// ===========================================================================
// ITEM 9 — deleteRow shifts the _edited index space (shiftEdited)
// ===========================================================================
(function () {
  const data = deep(FIX);
  S.rcaTableEdits.attach(data);
  const E = S.rcaTableEdits;
  E.setCellEdited('species_ranges', 2, 'species');   // row 2 dirty
  E.deleteRow('species_ranges', 0);                  // delete row 0 -> rows shift up
  check('item9-delete-shifts-edited', E.isCellEdited('species_ranges', 1, 'species') === true
    && E.isCellEdited('species_ranges', 2, 'species') === false,
    canonical([E.isCellEdited('species_ranges', 1, 'species'), E.isCellEdited('species_ranges', 2, 'species')]));
}());

// ===========================================================================
// ITEM 10 — js/viz.js typography: normalise the TRUE MINUS SIGN (U+2212)
// ===========================================================================
(function () {
  check('item10-true-minus-normalised', V.rcaVizMaValue('−5 Ma') === -5, String(V.rcaVizMaValue('−5 Ma')));
  check('item10-ascii-minus', V.rcaVizMaValue('-5 Ma') === -5, String(V.rcaVizMaValue('-5 Ma')));
  check('item10-positive-ma', V.rcaVizMaValue('251.902 Ma') === 251.902, String(V.rcaVizMaValue('251.902 Ma')));
  check('item10-bare-number-null', V.rcaVizMaValue('3') === null, String(V.rcaVizMaValue('3')));
}());

// ===========================================================================
// ITEM 11 — editCell guard failures answer with localized edit.notEditable /
//          edit.noRow (zh + en), not raw keys or the numeric message
// ===========================================================================
(function () {
  const data = deep(FIX);
  S.rcaTableEdits.attach(data);
  const E = S.rcaTableEdits;
  const cfg = cfgOf(data, 'species_ranges');
  const readonlyCol = (cfg.edit || []).findIndex((s) => s && !s.editable);

  S.rcaSetLang('zh');
  const neZh = E.editCell('species_ranges', 0, readonlyCol, 'x');
  const nrZh = E.editCell('species_ranges', 999, fieldCol(cfg, 'species'), 'x');
  const zhNotEd = S.rcaEditT('edit.notEditable');
  const zhNoRow = S.rcaEditT('edit.noRow');
  const zhNumber = S.rcaEditT('edit.cellNotNumber', { value: 'x' });

  check('item11-key-present', neZh.ok === false && neZh.key === 'edit.notEditable'
    && nrZh.ok === false && nrZh.key === 'edit.noRow', canonical([neZh.key, nrZh.key]));
  check('item11-zh-text-not-raw-key', neZh.text === zhNotEd && nrZh.text === zhNoRow
    && neZh.text !== 'edit.notEditable' && neZh.text !== zhNumber
    && nrZh.text !== zhNumber, canonical([neZh.text, nrZh.text]));
  check('item11-zh-and-en-differ-both-present',
    !!(S.RCA_EDIT_STRINGS['edit.notEditable'].zh && S.RCA_EDIT_STRINGS['edit.notEditable'].en
      && S.RCA_EDIT_STRINGS['edit.notEditable'].zh !== S.RCA_EDIT_STRINGS['edit.notEditable'].en));

  S.rcaSetLang('en');
  const neEn = E.editCell('species_ranges', 0, readonlyCol, 'x');
  check('item11-en-localized', neEn.ok === false && neEn.text === 'This column is read-only'
    && neEn.text !== zhNotEd, canonical([neEn.text, zhNotEd]));
  S.rcaSetLang('zh'); // restore default for any later check

  // The DOM commit path routes the failure's OWN key (never borrows the numeric
  // string): rcaEditCommitCell on a read-only cell must be skipped, and a valid
  // guard failure must carry its localized text.
  const dom = buildDom(deep(FIX), ['species_ranges']);
  S.rcaTableEditAttach(dom, deep(FIX), { editable: true });
  const roCell = cellOf(dom, 'species_ranges', 0, readonlyCol);
  check('item11-commit-readonly-cell-skips', (S.rcaEditCommitCell(roCell, 'zz') || {}).skipped === true);
}());

// ===========================================================================
// EXPORT PARITY — rcaExportCellText dict/list repr == Python str(value)
// (mirrored in tests/test_table_export_cell_text_parity_2026_09_22.py)
// ===========================================================================
(function () {
  const CASES = [
    ['dict-int', { a: 1 }, "{'a': 1}"],
    ['dict-str', { species: 'A' }, "{'species': 'A'}"],
    ['list-int', [1, 2], '[1, 2]'],
    ['list-mixed', [1, 'a', true], "[1, 'a', True]"],
    ['nested', { a: [1, { b: 2 }] }, "{'a': [1, {'b': 2}]}"],
    ['quote-single', { k: "it's" }, '{\'k\': "it\'s"}'],
    ['quote-double', { k: 'say "hi"' }, '{\'k\': \'say "hi"\'}'],
    ['quote-both', { k: 'both \' and "' }, '{\'k\': \'both \\\' and "\'}'],
    ['bool', { t: true, f: false }, "{'t': True, 'f': False}"],
    ['float', { f: 1.5 }, "{'f': 1.5}"],
    ['float-exp', { f: 1e-05 }, "{'f': 1e-05}"],
    ['none', { n: null }, "{'n': None}"],
    ['dict-order', { name: 'plain', count: 3, ratio: 2.25 },
      "{'name': 'plain', 'count': 3, 'ratio': 2.25}"],
    ['empty-dict', {}, '{}'],
    ['empty-list', [], '[]'],
    ['escapes', { 'back\\slash': 'tab\there\nnewline' }, "{'back\\\\slash': 'tab\\there\\nnewline'}"],
  ];
  for (const [name, input, want] of CASES) {
    let got;
    try { got = S.rcaExportCellText(input); } catch (e) { got = 'THREW ' + e.message; }
    check('export-' + name, got === want, JSON.stringify([got, want]));
  }

  // Non-JSON-representable corners kept in lock-step with Python semantics:
  check('export-toplevel-nonfinite-blanks', S.rcaExportCellText(Number.NaN) === ''
    && S.rcaExportCellText(Infinity) === '' && S.rcaExportCellText(-Infinity) === '');
  check('export-toplevel-null-blanks', S.rcaExportCellText(null) === '' && S.rcaExportCellText(undefined) === '');
  check('export-bare-nonfinite-word', S.rcaExportCellText('nan') === '' && S.rcaExportCellText('  INF ') === '');
  // A non-finite nested inside a container keeps Python's repr spelling (only a
  // TOP-LEVEL non-finite cell blanks) — matches str({'x': float('nan')}).
  check('export-nested-nonfinite-kept', S.rcaExportCellText({ x: Number.NaN }) === '{\'x\': nan}'
    && S.rcaExportCellText({ x: Infinity }) === '{\'x\': inf}',
    JSON.stringify([S.rcaExportCellText({ x: Number.NaN }), S.rcaExportCellText({ x: Infinity })]));
  // Regression: an object no longer leaks '[object Object]'.
  check('export-no-object-object', S.rcaExportCellText({ a: 1 }) !== '[object Object]'
    && S.rcaExportCellText([1, 2]) !== '1,2');
}());

// ---------------------------------------------------------------------------
console.log('\n' + pass + ' passed, ' + fail + ' failed');
if (fail) {
  console.log('FAILURES:\n  ' + failures.join('\n  '));
  process.exit(1);
}
