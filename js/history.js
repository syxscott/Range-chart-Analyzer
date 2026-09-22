// ===========================================================================
// js/history.js — the undo / redo stack of the in-browser table editor
// FE-BORROW-2026-09-20 (域T)
// ===========================================================================
//
// Borrowed shapes, deliberately without the dependencies:
//
//   * WPD-Stratigraphy `undoManager` — two stacks + a `MAX` depth + the rule
//     that a fresh action TRUNCATES the redo branch (you cannot redo across a
//     fork). Implemented verbatim below.
//   * Tabulator's History module — an action is a plain `{type, ...}` record
//     and undo/redo is a DISPATCH TABLE lookup (`undoers[type]`), never a
//     switch buried in the UI code. Adding a 4th editable verb is therefore
//     one entry here + one action from the editor, nothing else.
//   * ui-ux-pro-max `Undo-Redo` pattern: 40 steps, Ctrl+Z / Ctrl+Shift+Z, and
//     DISABLED state must be reflected on the buttons (subscription callback,
//     not polling).
//
// Scope: exactly THREE action types — cellEdit / rowDelete / rowAdd. Anything
// else is rejected by `push()` (returns false) so a stray action can never sit
// on the stack with no undoer and blow up on the first Ctrl+Z.
//
// Actions are produced by `rcaTableEdits` (js/table.js), which applies them to
// the live model BEFORE they are pushed: the stack stores nothing but the
// instruction, and the undoers call back into the same write primitives. That
// is what keeps undo consistent with `rca_core/editable.py` — the `_deleted_keys`
// / `new_<i>` shapes the Python side expects are produced by one code path, not
// twice.
//
// Plain globals, no ES-module syntax, and `document` is only touched inside a
// `typeof` guard, so tests_edit_history.js can `readFileSync` + `vm` this file
// and drive `undo()` / `redo()` / `handleKey()` without a browser.

// ---- i18n: use the editor's table when it is loaded, else bilingual --------
const RCA_HISTORY_STRINGS = {
  'edit.undo': { zh: '撤销', en: 'Undo' },
  'edit.redo': { zh: '重做', en: 'Redo' },
  'edit.undoCell': { zh: '撤销单元格 {row}/{col}', en: 'Undo cell {row}/{col}' },
  'edit.redoCell': { zh: '重做单元格 {row}/{col}', en: 'Redo cell {row}/{col}' },
  'edit.undoRowDelete': { zh: '恢复第 {row} 行', en: 'Restore row {row}' },
  'edit.redoRowDelete': { zh: '删除第 {row} 行', en: 'Delete row {row}' },
  'edit.undoRowAdd': { zh: '移除新增的第 {row} 行', en: 'Remove added row {row}' },
  'edit.redoRowAdd': { zh: '重新加入第 {row} 行', en: 'Re-add row {row}' },
  'edit.historyEmpty': { zh: '没有可撤销的修改', en: 'Nothing to undo' },
  'edit.historyEnd': { zh: '没有可重做的修改', en: 'Nothing to redo' },
  // FE-FIX-2026-09-21: audit item 5 — a deterministic apply failure (the
  // action's table/row no longer exists) must be announced too, distinct
  // from the empty-stack case; the entry is also popped (see undo/redo).
  'edit.historyFailed': { zh: '过期的修改记录已移出历史栈（目标表或行已不存在）',
    en: 'Stale history entry removed (its table or row no longer exists)' },
};

function rcaHistoryT(key, params) {
  // js/table.js owns the shared editor string table; prefer it when loaded so
  // a key added there is picked up without touching this file.
  if (typeof globalThis !== 'undefined' && typeof globalThis.rcaT === 'function') {
    const viaEditor = globalThis.rcaT(key);
    if (viaEditor && viaEditor.indexOf('[?') !== 0 && viaEditor !== key) return rcaHistoryFill(viaEditor, params);
  }
  const entry = RCA_HISTORY_STRINGS[key];
  let s = entry ? (rcaHistoryLang() === 'zh' ? entry.zh : entry.en) : key;
  if (entry && rcaHistoryLang() !== 'zh' && !entry.en) s = entry.zh;
  return rcaHistoryFill(s, params);
}

function rcaHistoryLang() {
  if (typeof globalThis !== 'undefined' && typeof globalThis.rcaEditLang === 'function') {
    try { return globalThis.rcaEditLang(); } catch (_e) { /* fall through */ }
  }
  return 'zh';
}

function rcaHistoryFill(text, params) {
  if (!params || typeof params !== 'object') return text;
  let out = String(text);
  for (const k of Object.keys(params)) out = out.split('{' + k + '}').join(String(params[k]));
  return out;
}

// ---- the action vocabulary -------------------------------------------------
//
// cellEdit  {tableId,row,col,field,model,before,hadKey,after,cleared}
// rowDelete {tableId,row,item}
// rowAdd    {tableId,row,item}
//
// `before` / `after` are MODEL values (already coerced by exporter.py's
// `_coerce_cell` mirror), `hadKey` records whether the field existed before the
// edit — restoring a value that did not exist must REMOVE the key, otherwise
// the diff would emit `field: None` where the Qt side emits `_deleted_keys`
// (rca_core/editable.py:197).
const RCA_HISTORY_TYPES = ['cellEdit', 'rowDelete', 'rowAdd'];

function rcaHistoryValidAction(action) {
  if (!action || typeof action !== 'object') return false;
  if (RCA_HISTORY_TYPES.indexOf(action.type) === -1) return false;
  if (typeof action.tableId !== 'string' || !action.tableId) return false;
  const row = Number(action.row);
  return Number.isFinite(row) && row >= 0;
}

// The engine is resolved at CALL time, not load time: index.html loads
// table.js first, but a test may load history.js alone, and the app may mount
// the editor later. Everything the undoers need comes through this one seam.
function rcaHistoryDefaultEngine() {
  const g = (typeof globalThis !== 'undefined') ? globalThis : {};
  return {
    edits: g.rcaTableEdits || null,
    afterApply: typeof g.rcaTableEditAfterHistory === 'function'
      ? g.rcaTableEditAfterHistory : null,
    announce: typeof g.rcaEditAnnounce === 'function' ? g.rcaEditAnnounce : null,
  };
}

function rcaHistoryEdits(history) {
  const eng = (history && history.engine) || rcaHistoryDefaultEngine();
  return eng && eng.edits ? eng.edits : null;
}

// ---- the dispatch table ----------------------------------------------------
//
// Each entry knows how to REVERSE and how to RE-APPLY one action. `rowDelete`
// and `rowAdd` are literal inverses of each other, so both verbs share two
// primitives (insert-record-at-index / remove-record-at-index) — the same
// observation Tabulator's History module encodes as "action + its mirror".
//
// FE-FIX-2026-09-21 (audit item 1): the primitives were re-derived from
// js/table.js's ACTUAL signatures, not their names:
//   * edits.undoDeleteRow(tableId, row, item)  INSERTS `item` at `row`
//     (js/table.js:2081, splices the clone in, clamped) — so it is the correct
//     verb for BOTH rowDelete.undo AND rowAdd.redo ("re-add the stored item at
//     action.row"). It returns a strict boolean.
//   * edits.undoAddRow(tableId, row)           REMOVES the row at `row` and
//     returns the removed ITEM (js/table.js:2105) — which for a scalar list
//     row (other_fossils) is the EMPTY STRING: `!!removed` was false, so
//     rowAdd.undo on a scalar row had already spliced the row out of the model
//     yet reported failure — the stacks stuck (depth 1, button enabled, every
//     later click a silent no-op). Compare against `false` instead of taking
//     a truthiness test of the payload.
const rcaHistoryUndoers = {
  cellEdit: {
    undo(action, edits) {
      const restore = action.hadKey ? action.before : null;
      return edits.setCellValue(action.tableId, action.row, action.field, restore,
        { model: action.model }) !== false;
    },
    redo(action, edits) {
      return edits.setCellValue(action.tableId, action.row, action.field, action.after,
        { model: action.model }) !== false;
    },
    describe(action, dir) {
      return rcaHistoryT(dir === 'redo' ? 'edit.redoCell' : 'edit.undoCell',
        { row: Number(action.row) + 1, col: Number(action.col) + 1 });
    },
  },
  rowDelete: {
    // undo = re-INSERT the stored item at the deleted index.
    undo(action, edits) { return edits.undoDeleteRow(action.tableId, action.row, action.item) !== false; },
    // redo = REMOVE the row again (deleteRow returns the action object or null).
    redo(action, edits) { return edits.deleteRow(action.tableId, action.row) != null; },
    describe(action, dir) {
      return rcaHistoryT(dir === 'redo' ? 'edit.redoRowDelete' : 'edit.undoRowDelete',
        { row: Number(action.row) + 1 });
    },
  },
  rowAdd: {
    // undo = REMOVE the added row. undoAddRow answers with the removed ITEM,
    // which is '' for a scalar-list row — FE-FIX-2026-09-21: a truthiness
    // test on that payload lost successful undos, compare against false.
    undo(action, edits) { return edits.undoAddRow(action.tableId, action.row) !== false; },
    // redo = RE-ADD the stored item at action.row. This is NOT
    // edits.addRow(): that would insert a FRESH template and ignore the
    // recorded item. edits.undoDeleteRow(tableId, row, item) is table.js's
    // generic "insert item at index" primitive (its name only tells which
    // action it was first written for), so it is the correct re-add verb —
    // it replays the exact item the stack recorded.
    redo(action, edits) { return edits.undoDeleteRow(action.tableId, action.row, action.item) !== false; },
    describe(action, dir) {
      return rcaHistoryT(dir === 'redo' ? 'edit.redoRowAdd' : 'edit.undoRowAdd',
        { row: Number(action.row) + 1 });
    },
  },
};

// FE-FIX-2026-09-21 (audit item 5): classify a failed apply() so the stack can
// tell "the model is not attached YET" (transient — the action stays on top,
// retryable, exactly the hist-no-engine-fails-closed contract) from
// "deterministically poisoned — the action's tableId no longer exists / its
// row is out of range" (retrying can never succeed; leaving it on top makes
// the button permanently enabled and every click silent). Read through the
// same engine seam the undoers use: edits.listKey(tableId) + edits.live() are
// js/table.js's public resolution path (registry :1784), and nested tables
// bound-check against rcaRowsForTable's flattened rows.
function rcaHistoryActionIsStale(action, direction, edits) {
  try {
    const data = (edits && typeof edits.live === 'function') ? edits.live()
      : (edits && edits.data);
    if (!data || typeof data !== 'object') return false;
    const key = (edits && typeof edits.listKey === 'function')
      ? edits.listKey(action.tableId) : action.tableId;
    const list = data[key];
    if (!Array.isArray(list)) return true;            // tableId gone
    const row = Number(action.row);
    // FE-FIX-2026-09-22 (FE-AUDIT item 8): the old comment claimed inserts
    // "clamp" like Python list.insert — true of the PRE-FIX undoDeleteRow,
    // but FE-FIX-2026-09-21 (audit item 1) made it fail CLOSED for
    // rowIdx > list.length (an out-of-range silent append was the bug it
    // killed). So an insert verb (rowDelete-undo / rowAdd-redo, both replay
    // through undoDeleteRow) whose row is beyond the CURRENT tail can never
    // succeed after an external data shrink — exactly the enabled-but-stuck
    // button the 2026-09-21 poison pass was written for. Classify it as
    // stale (undoDeleteRow still accepts row === list.length: Python
    // list.insert appends), and keep the LIFO semantics untouched: only
    // the top entry is ever examined/popped, transient (no-model) failures
    // stay 'retry' above this line.
    const isInsert = (action.type === 'rowDelete' && direction === 'undo')
      || (action.type === 'rowAdd' && direction === 'redo');
    if (isInsert) return !(row >= 0 && row <= list.length);
    // REMOVE / TOUCH-in-place verbs need the row to exist.
    const touchesRow = action.type === 'cellEdit'
      || (action.type === 'rowDelete' && direction === 'redo')
      || (action.type === 'rowAdd' && direction === 'undo');
    if (!touchesRow) return false;
    let count = list.length;
    const cfg = (edits && typeof edits.cfg === 'function') ? edits.cfg(action.tableId) : null;
    if (cfg && cfg.nested && typeof globalThis !== 'undefined'
      && typeof globalThis.rcaRowsForTable === 'function') {
      const flat = globalThis.rcaRowsForTable(data, action.tableId);
      count = Array.isArray(flat) ? flat.length : 0;
    }
    return !(row >= 0 && row < count);
  } catch (_e) { return false; }                       // unknown shape: keep
}

// FE-FIX-2026-09-21 (audit item 5): feedback for clicks/keys that moved
// nothing. rcaEditAnnounce is the editor's live-region seam (js/table.js
// :2455); guarded by typeof so headless callers and tests without table.js
// stay silent instead of throwing.
function rcaHistoryAnnounce(msg) {
  const g = (typeof globalThis !== 'undefined') ? globalThis : {};
  const fn = (typeof g.rcaEditAnnounce === 'function') ? g.rcaEditAnnounce : null;
  if (!fn || !msg) return false;
  try { fn(msg); return true; } catch (_e) { return false; }
}

// ---- the stack -------------------------------------------------------------

function rcaHistoryCreate() {
  const H = {
    MAX: 200,                       // WPD's undoManager uses 40; a proofreading
    // session reverts more than that, and the records are tiny.
    undoStack: [],
    redoStack: [],
    engine: null,
    undoers: rcaHistoryUndoers,
    _subs: [],

    setEngine(engine) {
      H.engine = (engine && typeof engine === 'object') ? engine : null;
      return H;
    },
    edits() { return rcaHistoryEdits(H); },

    // ---- recording ---------------------------------------------------------
    // The editor has ALREADY applied the action; push() only remembers it.
    // A rejected action returns false and changes nothing (no silent no-op
    // later on Ctrl+Z).
    push(action) {
      if (!rcaHistoryValidAction(action)) return false;
      H.undoStack.push(action);
      while (H.undoStack.length > H.MAX) H.undoStack.shift();
      // Fork rule: a new edit kills the redo branch (WPD undoManager).
      H.redoStack.length = 0;
      H.notify('push', action);
      return true;
    },

    undo() {
      if (!H.undoStack.length) {
        H.notify('empty', null);
        // FE-FIX-2026-09-21 (audit item 5): 'edit.historyEmpty' was dead
        // code — say it out loud when the button/keyboard click moved nothing
        // because there is nothing to move.
        rcaHistoryAnnounce(rcaHistoryT('edit.historyEmpty'));
        return null;
      }
      const action = H.undoStack[H.undoStack.length - 1];
      const res = H.applyResult(action, 'undo');
      if (res === 'ok') {
        H.undoStack.pop();
        H.redoStack.push(action);
        H.notify('undo', action);
        return action;
      }
      if (res === 'stale') H.poison('undoStack', action);   // FE-FIX-2026-09-21
      return null;                                          // 'retry': stacks untouched
    },

    redo() {
      if (!H.redoStack.length) {
        H.notify('empty', null);
        rcaHistoryAnnounce(rcaHistoryT('edit.historyEnd'));  // FE-FIX-2026-09-21
        return null;
      }
      const action = H.redoStack[H.redoStack.length - 1];
      const res = H.applyResult(action, 'redo');
      if (res === 'ok') {
        H.redoStack.pop();
        H.undoStack.push(action);
        H.notify('redo', action);
        return action;
      }
      if (res === 'stale') H.poison('redoStack', action);    // FE-FIX-2026-09-21
      return null;
    },

    // FE-FIX-2026-09-21 (audit item 5): a deterministic apply failure means
    // the entry can NEVER succeed again (its table or row is gone from the
    // live model). Leaving it on top kept the button enabled and made every
    // subsequent click a silent no-op — drop it and announce, so the button
    // re-syncs from the subscribers and the stack walks on to the next
    // undoable action. Transient failures (no attached model) do NOT come
    // here: those stay retryable (hist-no-engine-fails-closed contract).
    poison(stackName, action) {
      const at = H[stackName].indexOf(action);
      if (at !== -1) H[stackName].splice(at, 1);
      H.notify('failed', action);
      rcaHistoryAnnounce(rcaHistoryT('edit.historyFailed'));
      return action;
    },

    // Run one action's undoer. Returns false (leaving the stacks as they were)
    // when the model no longer has the row — a stale index after an external
    // refresh must not throw, it must simply fail to move.
    //
    // FE-FIX-2026-09-21 (audit item 2): this is the SINGLE apply funnel —
    // toolbar clicks, the document-delegated click router, rcaHistory.bind
    // AND the keyboard Ctrl+Z (handleKey -> undo()/redo() -> applyResult)
    // all land here, so afterApply below (rcaTableEditAfterHistory ->
    // rcaEditRerender for rowDelete/rowAdd, cell repaint for cellEdit) fires
    // identically no matter which surface moved the stack. The KEYBOARD
    // path was re-checked against that claim: it does go through this
    // funnel, so structural + following cell repaints stay in sync with the
    // shifted [data-row] indices regardless of entry point.
    apply(action, direction) { return H.applyResult(action, direction) === 'ok'; },

    // 'ok'      — applied, afterApply + announce ran.
    // 'retry'   — failed transiently (no engine / model not attached yet);
    //             the action stays on the stack, retryable.
    // 'stale'   — deterministically poisoned (tableId gone / row out of
    //             range, or an action type with no undoer); the caller pops.
    applyResult(action, direction) {
      const undoer = H.undoers[action && action.type];
      if (!undoer || typeof undoer[direction] !== 'function') return 'stale';
      const edits = H.edits();
      if (!edits || !edits.isAttached()) return 'retry';
      let ok = false;
      try { ok = !!undoer[direction](action, edits); } catch (_e) { ok = false; }
      if (!ok) {
        return rcaHistoryActionIsStale(action, direction, edits) ? 'stale' : 'retry';
      }
      const eng = H.engine || rcaHistoryDefaultEngine();
      if (eng && typeof eng.afterApply === 'function') {
        try { eng.afterApply(action, direction); } catch (_e2) { /* repaint is best-effort */ }
      }
      if (eng && typeof eng.announce === 'function') {
        try { eng.announce(undoer.describe(action, direction)); } catch (_e3) { /* ditto */ }
      }
      return 'ok';
    },

    canUndo() { return H.undoStack.length > 0; },
    canRedo() { return H.redoStack.length > 0; },
    depth() { return H.undoStack.length; },
    nextUndoLabel() {
      const a = H.undoStack[H.undoStack.length - 1];
      return a ? H.undoers[a.type].describe(a, 'undo') : '';
    },
    nextRedoLabel() {
      const a = H.redoStack[H.redoStack.length - 1];
      return a ? H.undoers[a.type].describe(a, 'redo') : '';
    },
    clear() {
      H.undoStack.length = 0;
      H.redoStack.length = 0;
      H.notify('clear', null);
      return H;
    },
    state() {
      return {
        canUndo: H.canUndo(),
        canRedo: H.canRedo(),
        undoDepth: H.undoStack.length,
        redoDepth: H.redoStack.length,
        undoLabel: H.nextUndoLabel(),
        redoLabel: H.nextRedoLabel(),
      };
    },

    // ---- disabled-state notification (ui-ux-pro-max: buttons, not polling) --
    subscribe(fn) {
      if (typeof fn !== 'function') return function () { return false; };
      H._subs.push(fn);
      fn(H.state(), 'init');
      return function unsubscribe() {
        const at = H._subs.indexOf(fn);
        if (at !== -1) H._subs.splice(at, 1);
        return at !== -1;
      };
    },
    notify(reason, action) {
      const st = H.state();
      st.reason = reason;
      st.action = action || null;
      for (const fn of H._subs.slice()) {
        try { fn(st, reason); } catch (_e) { /* a listener cannot break undo */ }
      }
      return st;
    },

    // ---- keyboard ----------------------------------------------------------
    // Ctrl+Z / Cmd+Z  -> undo;   Ctrl+Shift+Z / Ctrl+Y / Cmd+Shift+Z -> redo.
    //
    // YIELD RULE (the part that is easy to get wrong): when the caret is inside
    // a contenteditable / input / textarea, this returns false WITHOUT calling
    // preventDefault, so the browser's own text-level undo runs. Our cells are
    // contenteditable, so while a cell is being typed in, Ctrl+Z un-does the
    // keystrokes; it commits to the model undo stack on blur (Tabulator's
    // editNoEditor/no-editor-block behaves the same way). Row-level structure
    // undo therefore happens with the caret outside a cell.
    handleKey(ev, opts) {
      if (!ev) return false;
      const key = String(ev.key || '').toLowerCase();
      if (key !== 'z' && key !== 'y') return false;
      const mod = !!ev.ctrlKey || !!ev.metaKey;
      if (!mod) return false;
      if (!(opts && opts.ignoreEditable) && rcaHistoryInEditable(ev.target)) return false;
      const redo = key === 'y' || !!ev.shiftKey;
      ev.preventDefault = ev.preventDefault || function () { ev.defaultPrevented = true; };
      ev.preventDefault();
      // FE-FIX-2026-09-21 (audit items 2+5): undo()/redo() now notify AND
      // announce the empty / stale outcomes themselves (and the keyboard
      // path shares the exact apply funnel of the buttons, so structural
      // repaints happen identically here) — a second blanket
      // notify('empty') here mislabeled a deterministic failure as "nothing
      // to undo".
      const moved = redo ? H.redo() : H.undo();
      return !!moved;
    },

    // The returned unbind reports the POST-CONDITION ("no keydown listener of
    // ours is attached any more"), not "work was done" — so a headless caller
    // (Qt history dialog, Node, a page without `document`) can hold the handle
    // and release it unconditionally without having to special-case the
    // no-DOM path. `subscribe()`'s unsubscribe keeps the other convention
    // (did THIS listener get found in the list) because there the caller does
    // care whether it double-retired a live subscription.
    bind(target, opts) {
      const el = target === undefined ? rcaHistoryDocument() : target;
      if (!el || typeof el.addEventListener !== 'function') {
        return function unbind() { return true; };
      }
      const handler = (ev) => H.handleKey(ev, opts);
      el.addEventListener('keydown', handler);
      return function unbind() {
        if (typeof el.removeEventListener === 'function') el.removeEventListener('keydown', handler);
        return true;
      };
    },
  };
  return H;
}

function rcaHistoryDocument() {
  if (typeof document !== 'undefined' && document) return document;
  if (typeof window !== 'undefined' && window && window.document) return window.document;
  return null;
}

// Walk out of the event target looking for an editable host. `isContentEditable`
// is what a real browser reports (it covers an inherited contenteditable); the
// attribute + tag checks keep this working on a minimal DOM stub.
function rcaHistoryInEditable(node) {
  let el = node;
  let guard = 0;
  while (el && el.nodeType !== 9 && guard < 64) {
    guard += 1;
    if (el.isContentEditable === true) return true;
    const tag = String(el.tagName || '').toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (tag === 'INPUT') {
      const type = String((typeof el.getAttribute === 'function'
        && el.getAttribute('type')) || el.type || 'text').toLowerCase();
      // These inputs have no native text undo worth protecting.
      if (['checkbox', 'radio', 'range', 'color', 'file', 'button', 'submit', 'reset']
        .indexOf(type) === -1) return true;
    }
    if (typeof el.getAttribute === 'function') {
      const ce = el.getAttribute('contenteditable');
      if (ce === 'true' || ce === '') return true;
    }
    el = el.parentNode || el.parentElement || null;
  }
  return false;
}

// ---- the singleton the app uses -------------------------------------------
// `var`, not `const`, so the vm sandbox of tests_edit_history.js and the
// browser both see one instance (the same choice js/viz.js makes).
var rcaHistory = rcaHistoryCreate();

// Buttons live in the table toolbar rendered by js/table.js
// (`[data-rca-undo]` / `[data-rca-redo]`). FE-BORROW-2026-09-20: rendering the
// buttons and syncing their disabled state is worthless if nothing listens —
// a live-browser undo click found exactly that missing wire. The click
// listener is delegated on `document` ONCE (the toolbar is re-rendered under
// an unchanged #results-content root, so a root-level wire would be lost on
// every re-render; a document-level one survives). Disabled <button>s swallow
// their own clicks in every browser, so no extra canUndo guard is needed —
// but the branch stays as a no-op-safe fallback for stub DOMs.
//
// FE-FIX-2026-09-21 (audit item 6): all three helpers below take the history
// INSTANCE as an optional last argument (`H`). Omitted, they keep driving the
// page singleton `rcaHistory`, so every existing caller (app.js, table.js,
// the tests that call the plain names) behaves exactly as before — but a
// second stack (rcaHistoryCreate(), e.g. a preview panel) can now wire its
// own buttons instead of being hard-wired to the global one.
function rcaHistoryWireClicksOnce(doc, H) {
  const hist = H || rcaHistory;
  if (!doc || typeof doc.addEventListener !== 'function') return false;
  if (doc.__RCA_HISTORY_CLICK_BOUND__) return true;
  doc.addEventListener('click', (ev) => {
    const target = ev && ev.target;
    const btn = (target && typeof target.closest === 'function')
      ? target.closest('[data-rca-undo],[data-rca-redo]') : null;
    if (!btn) return;
    const isRedo = typeof btn.hasAttribute === 'function'
      ? btn.hasAttribute('data-rca-redo')
      : String(btn.getAttribute ? (btn.getAttribute('data-rca-redo') || '') : '') !== '';
    // FE-FIX-2026-09-21 (audit item 5): undo/redo self-notify on the empty
    // and stale outcomes — the old blanket notify('empty') here called a
    // poisoned entry "nothing to undo".
    if (isRedo) hist.redo(); else hist.undo();
  });
  doc.__RCA_HISTORY_CLICK_BOUND__ = true;
  return true;
}

// `subscribe` accumulates a listener per call, and renderCurrentResult runs on
// every result refresh — so keep exactly one live subscription per instance
// and retire the previous one here instead of leaking a closure per render.
var rcaHistoryUiUnsub = null;

function rcaHistoryAttachUi(root, H) {
  const hist = H || rcaHistory;
  rcaHistoryWireClicksOnce(rcaHistoryDocument(), hist);
  if (typeof hist.__uiUnsub === 'function') {
    try { hist.__uiUnsub(); } catch (_e) { /* already gone */ }
  }
  hist.__uiUnsub = hist.subscribe(() => rcaHistorySyncButtons(root, hist));
  rcaHistorySyncButtons(root, hist);
  // Keep the module-level handle pointed at the page singleton's last
  // subscription (pre-FE-FIX callers/tests only ever used that one).
  if (hist === rcaHistory) rcaHistoryUiUnsub = hist.__uiUnsub;
  return true;
}

// Sync the toolbar buttons' disabled state from the stack so they can never
// disagree with it.
function rcaHistorySyncButtons(root, H) {
  const hist = H || rcaHistory;
  const scope = root || (typeof document !== 'undefined' ? document : null);
  if (!scope || typeof scope.querySelectorAll !== 'function') return 0;
  const st = hist.state();
  const pairs = [['[data-rca-undo]', st.canUndo], ['[data-rca-redo]', st.canRedo]];
  let n = 0;
  for (const pair of pairs) {
    const list = scope.querySelectorAll(pair[0]);
    for (let i = 0; i < (list || []).length; i += 1) {
      const btn = list[i];
      if (typeof btn.toggleAttribute === 'function') {
        btn.toggleAttribute('disabled', !pair[1]);
      } else if (typeof btn.setAttribute === 'function') {
        if (pair[1]) btn.removeAttribute('disabled');
        else btn.setAttribute('disabled', 'disabled');
      }
      if (typeof btn.setAttribute === 'function') {
        btn.setAttribute('aria-disabled', pair[1] ? 'false' : 'true');
      }
      n += 1;
    }
  }
  return n;
}

// Ctrl+Z on the page: bound once, lazily, and ONLY in a DOM environment.
function rcaHistoryAutoBind() {
  if (typeof globalThis === 'undefined') return false;
  if (globalThis.__RCA_HISTORY_BOUND__) return true;
  const doc = rcaHistoryDocument();
  if (!doc || typeof doc.addEventListener !== 'function') return false;
  doc.addEventListener('keydown', (ev) => rcaHistory.handleKey(ev));
  globalThis.__RCA_HISTORY_BOUND__ = true;
  return true;
}

if (typeof globalThis !== 'undefined') {
  globalThis.rcaHistory = rcaHistory;
  globalThis.rcaHistoryCreate = rcaHistoryCreate;
  globalThis.rcaHistoryUndoers = rcaHistoryUndoers;
  globalThis.rcaHistoryValidAction = rcaHistoryValidAction;
  globalThis.rcaHistoryInEditable = rcaHistoryInEditable;
  globalThis.rcaHistorySyncButtons = rcaHistorySyncButtons;
  globalThis.rcaHistoryAttachUi = rcaHistoryAttachUi;
  globalThis.rcaHistoryWireClicksOnce = rcaHistoryWireClicksOnce;
  // FE-FIX-2026-09-21: exposed for tests_history_fixes_2026_09_21.js.
  globalThis.rcaHistoryActionIsStale = rcaHistoryActionIsStale;
  globalThis.rcaHistoryAnnounce = rcaHistoryAnnounce;
  globalThis.RCA_HISTORY_TYPES = RCA_HISTORY_TYPES;
  globalThis.RCA_HISTORY_STRINGS = RCA_HISTORY_STRINGS;
}
if (typeof window !== 'undefined') {
  window.rcaHistory = rcaHistory;
  if (typeof window.document !== 'undefined') rcaHistoryAutoBind();
}
