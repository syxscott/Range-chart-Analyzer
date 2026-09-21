// ===========================================================================
// tests_history_fixes_2026_09_21.js — FE-FIX-2026-09-21 regression checks
// ===========================================================================
//
// Run with:  node tests_history_fixes_2026_09_21.js
//
// Covers the audit items fixed in js/history.js (+ js/app.js / index.html):
//
//   1. rowAdd/rowDelete dispatch re-derived from js/table.js's ACTUAL
//      primitive semantics — undoAddRow returns the removed ITEM (an empty
//      string for scalar rows: `!!item` lost successful undos), and
//      undoDeleteRow(tableId,row,item) is the insert-at-index verb that
//      rowAdd.redo must use to re-ADD the stored item at action.row.
//   2. The keyboard Ctrl+Z path shares the apply/afterApply funnel, so a
//      structural undo re-renders (opts.rerender spy proves the call).
//   5. Empty-stack vs poisoned-entry feedback: rcaEditAnnounce hears a
//      distinct message for each, and a deterministic apply failure pops
//      the poisoned entry so the button re-syncs and the stack unlocks.
//   6. rcaHistoryWireClicksOnce / AttachUi / SyncButtons take an optional
//      history instance — the singleton stays the default.
//
// Same vm-sandbox pattern as tests_edit_history.js (readFileSync + vm, plain
// globals, DOM stub). tests_edit_history.js itself is NOT modified (owned by
// the table.js agent).
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0;
let fail = 0;
const failures = [];
function check(name, ok, detail) {
  if (ok) { pass += 1; console.log('PASS', name); }
  else {
    fail += 1;
    console.log('FAIL ' + name + (detail === undefined || detail === true ? '' : ' — ' + detail));
    failures.push(name);
  }
}
function deep(v) { return JSON.parse(JSON.stringify(v === undefined ? null : v)); }
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort()
      .map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  if (value === undefined) return 'null';
  return JSON.stringify(value);
}

// ---- DOM stub (selector engine copied from tests_edit_history.js) ----------
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
    textContent: '', checked: false, nodeType: 1, style: {}, dataset: {}, _listeners: {},
  };
  el.setAttribute = (k, v) => {
    el.attributes[String(k)] = String(v);
    if (String(k) === 'class') el.className = String(v);
  };
  el.getAttribute = (k) => (String(k) in el.attributes ? el.attributes[String(k)] : null);
  el.removeAttribute = (k) => { delete el.attributes[String(k)]; };
  el.hasAttribute = (k) => String(k) in el.attributes;
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
  el.toggleAttribute = (k, on) => { if (on) el.setAttribute(k, '1'); else el.removeAttribute(k); };
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
  el.closest = (sel) => (el.matches(sel) ? el : (el.parentNode ? el.parentNode.closest(sel) : null));
  for (const k of Object.keys(attrs || {})) el.setAttribute(k, attrs[k]);
  return el;
}
function mkEvent(type, target, extra) {
  const ev = { type, target, defaultPrevented: false };
  ev.preventDefault = () => { ev.defaultPrevented = true; };
  for (const k of Object.keys(extra || {})) ev[k] = extra[k];
  return ev;
}

// ---- sandbox ---------------------------------------------------------------
function makeContext() {
  const docEl = {
    lang: 'zh-CN',
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, style: {},
  };
  const quiet = {
    log: () => {}, warn: () => {}, error: (...a) => console.error(...a),
  };
  const ctx = {
    console: quiet, setTimeout, clearTimeout,
    document: {
      documentElement: docEl,
      addEventListener() {}, removeEventListener() {},
      querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
      createElement: (tag) => makeEl(tag), body: {},
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
  return ctx;
}
const ctx = makeContext();
const run = (code) => vm.runInContext(code, ctx);
const edits = run('rcaTableEdits');
const H = run('rcaHistory');
const rcaHistoryCreate = run('rcaHistoryCreate');
const undoers = run('rcaHistoryUndoers');
const wireClicksOnce = run('rcaHistoryWireClicksOnce');
const attachUi = run('rcaHistoryAttachUi');
const syncButtons = run('rcaHistorySyncButtons');

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
function fresh(model) { edits.detach(); edits.attach(deep(model)); }

// ===========================================================================
// A. audit item 1 — rowAdd / rowDelete dispatch re-derived from table.js
// ===========================================================================

fresh(FIX);
H.clear();

// scalar rowAdd: undoAddRow answers with the removed ITEM ('' here) — the
// pre-fix `!!removed` test reported a SUCCESSFUL removal as a failure and
// left the stack stuck on top.
const scalarAdd = edits.addRow('other_fossils');
check('item1-scalar-add-action', scalarAdd && scalarAdd.type === 'rowAdd'
  && scalarAdd.item === '' && scalarAdd.row === 1, canonical(scalarAdd));
H.push(scalarAdd);
const u1 = H.undo();
check('item1-scalar-rowadd-undo-moves', u1 !== null && H.depth() === 0 && H.canRedo() === true
  && ctx.rcaTableEdits.live().other_fossils.length === 1,
  'undo=' + (u1 === null ? 'null' : 'ok') + ' depth=' + H.depth());
const r1 = H.redo();
check('item1-scalar-rowadd-redo-readds', r1 !== null && H.depth() === 1
  && edits.live().other_fossils.length === 2 && edits.live().other_fossils[1] === '',
  'redo=' + (r1 === null ? 'null' : 'ok'));
H.undo();

// dict rowAdd at an explicit index: redo must RE-ADD the STORED item AT
// action.row (not a fresh template elsewhere) — verified by mutating the
// recorded item between undo and redo.
const addAct = edits.addRow('biozones', 0);
H.push(addAct);
check('item1-rowadd-undo-removes-row-at-index',
  H.undo() !== null && edits.live().biozones.length === 1
  && edits.live().biozones[0].name === 'Z1', canonical(edits.live().biozones));
addAct.item.name = 'MUT';
check('item1-rowadd-redo-readds-stored-item-at-row',
  H.redo() !== null && edits.live().biozones.length === 2
  && edits.live().biozones[0].name === 'MUT', canonical(edits.live().biozones));
H.undo();
addAct.item.name = 'Z1';
H.redo();
H.clear();

// rowDelete round-trip through the (renamed) shared insert/remove verbs.
const delAct = edits.deleteRow('species_ranges', 1);
H.push(delAct);
check('item1-rowdelete-undo-reinserts-item',
  H.undo() !== null && edits.live().species_ranges.length === 2
  && edits.live().species_ranges[1].species === 'B');
check('item1-rowdelete-redo-removes-again',
  H.redo() !== null && edits.live().species_ranges.length === 1);
H.clear();

// The undoers called directly: each verb answers with a real boolean, not a
// payload truthiness test.
const scratchAdd = edits.addRow('other_fossils');   // row 1 now exists, item ''
check('item1-undoer-returns-booleans',
  undoers.rowAdd.undo({ type: 'rowAdd', tableId: 'other_fossils', row: 1, item: '' }, edits) === true
  && undoers.rowAdd.redo({ type: 'rowAdd', tableId: 'other_fossils', row: 1, item: '' }, edits) === true
  && undoers.rowAdd.undo({ type: 'rowAdd', tableId: 'other_fossils', row: 99, item: '' }, edits) === false,
  'undo/redo must be strict booleans; out-of-range removal stays false');
void scratchAdd;
H.clear();

// ===========================================================================
// B. audit item 2 — keyboard Ctrl+Z shares the apply/afterApply funnel so
//    a structural action re-renders the DOM (shifted [data-row] indices)
// ===========================================================================

fresh(FIX);
H.clear();
const root2 = makeEl('DIV');
let rerenders = 0;
const afterHistoryCalls = [];
ctx.rcaTableEditAfterHistory = (function (orig) {
  return function (action, direction) {
    afterHistoryCalls.push([action && action.type, direction]);
    return orig(action, direction);
  };
})(run('rcaTableEditAfterHistory'));
run('rcaTableEditAttach')(root2, edits.live(), {
  editable: true,
  rerender: () => { rerenders += 1; return true; },
});
const del2 = edits.deleteRow('species_ranges', 0);
if (del2) H.push(del2);
check('item2-keyboard-structural-undo-rerenders', (function () {
  const before = rerenders;
  const ev = mkEvent('keydown', { tagName: 'DIV', getAttribute: () => null, parentNode: null },
    { key: 'z', ctrlKey: true });
  const moved = H.handleKey(ev);
  return moved === true && H.depth() === 0 && rerenders === before + 1
    && afterHistoryCalls.some((p) => p[0] === 'rowDelete' && p[1] === 'undo');
})(), 'rerenders=' + rerenders + ' calls=' + canonical(afterHistoryCalls));
check('item2-keyboard-structural-redo-rerenders', (function () {
  const before = rerenders;
  const ev = mkEvent('keydown', { tagName: 'DIV', getAttribute: () => null, parentNode: null },
    { key: 'z', ctrlKey: true, shiftKey: true });
  return H.handleKey(ev) === true && rerenders === before + 1
    && afterHistoryCalls.some((p) => p[0] === 'rowDelete' && p[1] === 'redo');
})(), 'rerenders=' + rerenders);
check('item2-undoer-cell-then-structural-funnel', (function () {
  const ce = edits.editCell('species_ranges', 0, 0, 'K*');
  if (!ce.ok || !ce.changed) return false;
  H.push(ce.action);
  return H.undo() !== null
    && afterHistoryCalls.some((p) => p[0] === 'cellEdit' && p[1] === 'undo');
})(), 'cell edits flow through the same afterApply (per-cell repaint)');
run('rcaTableEditDetach')();
H.clear();

// ===========================================================================
// C. audit item 5 — undo/redo button + live-region feedback, poisoned pop
// ===========================================================================

fresh(FIX);
H.clear();
const announced = [];
ctx.rcaEditAnnounce = (msg) => { announced.push(String(msg)); };
const reasons = [];
const unsub = H.subscribe((st, reason) => { reasons.push(reason); });

check('item5-empty-undo-announces-historyEmpty', (function () {
  const before = announced.length;
  const got = H.undo();
  return got === null
    && announced.slice(before).some((m) => m.indexOf('没有可撤销的修改') !== -1 || /nothing to undo/i.test(m))
    && reasons[reasons.length - 1] === 'empty';
})(), 'announced=' + canonical(announced) + ' reasons=' + canonical(reasons));
check('item5-empty-redo-announces-historyEnd', (function () {
  const before = announced.length;
  const got = H.redo();
  return got === null
    && announced.slice(before).some((m) => m.indexOf('没有可重做的修改') !== -1 || /nothing to redo/i.test(m));
})(), 'announced=' + canonical(announced));

// deterministic poison #1: tableId no longer exists.
H.push({ type: 'rowDelete', tableId: 'gone_table', row: 0, item: { x: 1 } });
check('item5-stale-table-pops-and-announces-failed', (function () {
  const beforeA = announced.length;
  const got = H.undo();
  return got === null && H.depth() === 0 && H.canUndo() === false
    && reasons[reasons.length - 1] === 'failed'
    && announced.slice(beforeA).some((m) => /过期|stale/i.test(m));
})(), 'depth=' + H.depth() + ' reasons=' + canonical(reasons));

// deterministic poison #2: row out of range (undoAddRow removes at index).
const depth0 = edits.live().species_ranges.length;
H.push({ type: 'rowAdd', tableId: 'species_ranges', row: 400, item: { species: 'ghost' } });
check('item5-stale-row-pops-without-touching-model',
  H.undo() === null && H.depth() === 0
  && edits.live().species_ranges.length === depth0, 'model=' + depth0);

// poisoned entry between a good one and the caller: after the pop the NEXT
// undo reaches the healthy action (pre-fix the button was stuck on poison).
const ce2 = edits.editCell('species_ranges', 0, 0, 'P*');
H.push(ce2.action);
H.push({ type: 'cellEdit', tableId: 'gone_table', row: 5, col: 0, field: 'f', model: 'f',
  before: 1, hadKey: true, after: 2 });
const dBefore = H.depth();
check('item5-poison-unlocks-the-stack', (function () {
  const first = H.undo();   // top = poisoned cellEdit -> popped, returns null
  const depthAfterPoison = H.depth();
  const second = H.undo();  // now the real action moves
  return dBefore === 2 && first === null && depthAfterPoison === 1 && second !== null
    && edits.live().species_ranges[0].species === 'A';
})(), 'depthBefore=' + dBefore + ' depth=' + H.depth());

// transient failure (no attached model) must KEEP the action retryable.
const data3 = deep(FIX);
edits.detach();
H.clear();
H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
  model: 'species', before: 'A', hadKey: true, after: 'T*' });
check('item5-transient-failure-keeps-entry',
  H.undo() === null && H.depth() === 1 && H.canUndo() === true);
edits.attach(data3);
check('item5-retry-after-transient-succeeds',
  H.undo() !== null && data3.species_ranges[0].species === 'A' && H.depth() === 0);

// rcaHistoryActionIsStale unit corners.
fresh(FIX);
check('item5-stale-classifier-inserts-never-stale',
  run('rcaHistoryActionIsStale')({ type: 'rowDelete', tableId: 'species_ranges', row: 99, item: {} },
    'undo', edits) === false
  && run('rcaHistoryActionIsStale')({ type: 'rowAdd', tableId: 'species_ranges', row: 400, item: {} },
    'redo', edits) === false
  && run('rcaHistoryActionIsStale')({ type: 'rowAdd', tableId: 'species_ranges', row: 400, item: {} },
    'undo', edits) === true
  && run('rcaHistoryActionIsStale')({ type: 'cellEdit', tableId: 'nope', row: 0 }, 'undo', edits) === true,
  'inserts clamp (survivable), removes/cellEdits out of range are deterministic');
unsub();
H.clear();

// ===========================================================================
// D. audit item 6 — UI helpers parameterized by instance, singleton default
// ===========================================================================

fresh(FIX);
H.clear();
const H2 = rcaHistoryCreate();
H2.clear();

// click router bound to a private document, driving ONLY H2.
const fakeDoc = { _l: {},
  addEventListener(t, cb) { (this._l[t] = this._l[t] || []).push(cb); },
  removeEventListener() {} };
const ce3 = edits.editCell('species_ranges', 0, 0, 'I*');
H2.push(ce3.action);
H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 1, col: 0, field: 'species',
  model: 'species', before: 'B', hadKey: true, after: 'SINGLETON' });
check('item6-wire-clicks-uses-instance', (function () {
  if (wireClicksOnce(fakeDoc, H2) !== true) return false;
  const btn = {
    getAttribute(n) { return n === 'data-rca-undo' ? '1' : null; },
    hasAttribute(n) { return n === 'data-rca-undo'; },
    closest: (sel) => (sel.indexOf('data-rca-undo') !== -1 ? btn : null),
  };
  (fakeDoc._l.click || []).forEach((cb) => cb(mkEvent('click', btn)));
  return H2.depth() === 0 && H.depth() === 1
    && edits.live().species_ranges[0].species === 'A';
})(), 'H2=' + H2.depth() + ' singleton=' + H.depth());

// button sync follows the PASSED instance, not the singleton.
const uiRoot = makeEl('DIV');
uiRoot.appendChild(makeEl('BUTTON', { 'data-rca-undo': '1' }));
uiRoot.appendChild(makeEl('BUTTON', { 'data-rca-redo': '1' }));
const undoBtn = uiRoot.querySelector('[data-rca-undo]');
check('item6-sync-buttons-uses-instance', (function () {
  // H2 empty, singleton has 1 action -> H2's buttons must be DISABLED even
  // though the global stack is undoable.
  syncButtons(uiRoot, H2);
  const disabledForH2 = undoBtn.getAttribute('disabled') !== null;
  H.undo();  // empty the singleton too
  syncButtons(uiRoot, H2);
  H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'X*' });
  syncButtons(uiRoot, H2);
  const stillH2State = undoBtn.getAttribute('disabled') !== null;
  H.clear();
  return disabledForH2 && stillH2State;
})(), 'syncButtons(root) must reflect H2, not the singleton');

// attachUi(root, H2): subscription + clicks bound to H2; default call (no
// instance) still targets the page singleton.
check('item6-attach-ui-with-instance', (function () {
  if (attachUi(uiRoot, H2) !== true) return false;
  H2.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'Y*' });
  return undoBtn.getAttribute('disabled') === null;
})());
check('item6-default-still-singleton', (function () {
  attachUi(uiRoot);  // no instance -> singleton
  H2.clear();
  H.push({ type: 'cellEdit', tableId: 'species_ranges', row: 0, col: 0, field: 'species',
    model: 'species', before: 'A', hadKey: true, after: 'Z*' });
  const enabled = undoBtn.getAttribute('disabled') === null;
  H.clear(); H2.clear();
  return enabled === true;
})(), 'plain attachUi(root) keeps driving the singleton (existing callers)');

// ===========================================================================
console.log('\n--- ' + pass + ' passed, ' + fail + ' failed ---');
if (fail) console.log('failed: ' + failures.join(', '));
process.exit(fail ? 1 : 0);
