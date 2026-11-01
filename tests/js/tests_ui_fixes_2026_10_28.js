/**
 * Regression tests for the UI fix wave of 2026-10-28 (five confirmed bugs).
 *
 * Run:  node tests/js/tests_ui_fixes_2026_10_28.js
 *
 * Hand-rolled suite like every other root Node suite in this repo: no
 * package.json, no framework, no dependency. `check(name, ok, detail)`.
 * The vm-sandbox loading pattern (plain-globals context + IIFE strip with
 * test-only exports) follows tests_ui_fixes_2026_09_27.js; the browser-ish
 * buildContext/makeEl stubs follow tests_frontend.js, with three deliberate
 * upgrades this wave needs:
 *   1. classList.contains is REAL (the run-progress assertions read the
 *      live class, not a recorded log);
 *   2. document.querySelectorAll scans the cached elements (so
 *      rcaApplyI18n's [data-i18n] walk actually reaches the elements a
 *      test registered — exactly what index.html does in production);
 *   3. matches() understands `tag[attr=value]` (syncSegmentedFromSelect
 *      queries `button[role=radio]`, which the plain tagName stub can
 *      never match).
 */
'use strict';

const fs = require('fs');
const path = require('path');
// tests/js/ -> repository root. These harnesses read js/, css/, index.html and
// rca_core/ by relative path; until the 2026-11-01 layout change they sat at
// the root, so __dirname WAS the root. Stating it once keeps every read below
// spelled as "js/app.js" instead of "../../js/app.js", which is the version a
// reviewer can still check at a glance.
const REPO = path.resolve(__dirname, '..', '..');

const vm = require('vm');

const read = (rel) => fs.readFileSync(path.join(REPO, rel), 'utf8');

let pass = 0;
let fail = 0;
const failed = [];
function check(name, ok, detail) {
  if (ok) {
    pass += 1;
    console.log('PASS', name);
  } else {
    fail += 1;
    failed.push(name);
    console.log('FAIL', name, detail === undefined ? '' : '— ' + detail);
  }
}

// The browser's URL is constructible and carries the object-URL helpers
// (same rationale as tests_frontend.js: js/minimax.js's SSRF gate needs a
// real WHATWG parser).
class SandboxURL extends require('url').URL {
  static createObjectURL() { return 'blob:fake'; }
  static revokeObjectURL() { /* no-op */ }
}

// Selector matcher shared by element.querySelectorAll and
// document.querySelectorAll. Supports the three shapes the loaded scripts
// actually use: '.class', '#id', 'TAG', and 'TAG[attr=value]'.
function matchesSel(node, sel) {
  if (!sel) return false;
  if (sel[0] === '.') {
    return (node.className || '').split(/\s+/).indexOf(sel.slice(1)) !== -1;
  }
  if (sel[0] === '#') return node.id === sel.slice(1);
  const compound = sel.match(/^([A-Za-z][A-Za-z0-9-]*)\[([^=\]]+)=(.*)\]$/);
  if (compound) {
    return node.tagName === compound[1].toUpperCase()
      && String(node.attributes[compound[2]]) === compound[3];
  }
  return node.tagName === sel.toUpperCase();
}

function makeEl(id, tagName) {
  const collectAll = (root, sel, out) => {
    for (const c of (root.children || [])) {
      if (matchesSel(c, sel)) out.push(c);
      collectAll(c, sel, out);
    }
  };
  const listeners = {};
  const el = {
    id,
    className: '',
    classList: {
      _calls: [],
      add(c) { this._calls.push(['add', c]); el.className = (el.className + ' ' + c).trim(); },
      remove(c) { this._calls.push(['remove', c]); el.className = el.className.split(/\s+/).filter((x) => x !== c).join(' '); },
      toggle(c, f) {
        this._calls.push(['toggle', c, f]);
        const on = f === undefined ? el.className.split(/\s+/).indexOf(c) === -1 : !!f;
        if (on) this.add(c); else this.remove(c);
      },
      contains(c) { return el.className.split(/\s+/).indexOf(c) !== -1; },
    },
    style: {},
    dataset: {},
    attributes: {},
    children: [],
    parentNode: null,
    get firstChild() { return this.children[0] || null; },
    value: '',
    textContent: '',
    innerHTML: '',
    type: 'text',
    checked: false,
    selected: false,
    tagName: (tagName || 'DIV').toUpperCase(),
    title: '',
    disabled: false,
    addEventListener(type, cb) {
      if (!listeners[type]) listeners[type] = [];
      listeners[type].push(cb);
    },
    removeEventListener(type, cb) {
      if (!listeners[type]) return;
      listeners[type] = listeners[type].filter((l) => l !== cb);
    },
    setAttribute(k, v) { el.attributes[k] = String(v); if (k === 'id') el.id = v; },
    getAttribute(k) { return el.attributes[k] != null ? el.attributes[k] : null; },
    removeAttribute(k) { delete el.attributes[k]; },
    querySelector(sel) {
      const out = [];
      collectAll(el, sel, out);
      return out[0] || null;
    },
    querySelectorAll(sel) {
      const out = [];
      collectAll(el, sel, out);
      return out;
    },
    appendChild(c) { el.children.push(c); c.parentNode = el; },
    removeChild(c) {
      const idx = el.children.indexOf(c);
      if (idx !== -1) { el.children.splice(idx, 1); c.parentNode = null; }
    },
    focus: () => {},
    click: () => {},
    reset() {},
    dispatchEvent(ev) {
      const type = ev && ev.type;
      const list = listeners[type] || [];
      for (const cb of list) {
        try { cb(ev); } catch (_e) { /* ignore */ }
      }
      return true;
    },
  };
  return el;
}

function buildContext() {
  const stored = new Map();
  // Backing store for documentElement attributes AND a recording style
  // object: applyRowsUnit writes --rca-rows-unit through
  // style.setProperty, and the rows-unit regression must read the value
  // back the way css/table-edit.css's .rsb-count::after would.
  const docElAttrs = {};
  const cssVars = {};
  const docEl = {
    lang: 'zh-CN',
    style: {
      setProperty: (k, v) => { cssVars[String(k)] = String(v); },
      getPropertyValue: (k) => (k in cssVars ? cssVars[k] : ''),
      removeProperty: (k) => { delete cssVars[String(k)]; },
    },
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute: (k, v) => { docElAttrs[String(k)] = String(v); },
    removeAttribute: (k) => { delete docElAttrs[String(k)]; },
    getAttribute: (k) => (k in docElAttrs ? docElAttrs[String(k)] : null),
  };
  const ctx = {
    console,
    setTimeout, clearTimeout,
    document: {
      _listeners: {},
      addEventListener(type, cb) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(cb);
      },
      removeEventListener(type, cb) {
        if (!this._listeners[type]) return;
        this._listeners[type] = this._listeners[type].filter((l) => l !== cb);
      },
      dispatchEvent(ev) {
        const list = this._listeners[ev && ev.type] || [];
        for (const cb of list) {
          try { cb(ev); } catch (_e) { /* ignore */ }
        }
        return true;
      },
      // Elements are cached by id (same as tests_frontend.js) so DOM state
      // survives repeated getElementById calls.
      _els: new Map(),
      getElementById(id) {
        if (!this._els.has(id)) {
          const tagMap = {
            'api-key': 'INPUT',
            'caption': 'TEXTAREA',
            'preview-wrap': 'DIV',
            'preview-img': 'IMG',
            'results-content': 'DIV',
            'alert-slot': 'DIV',
            'results-empty': 'DIV',
          };
          this._els.set(id, makeEl(id, tagMap[id] || 'DIV'));
        }
        return this._els.get(id);
      },
      // REAL querySelectorAll over the cached elements: rcaApplyI18n walks
      // [data-i18n] / [data-i18n-ph] / ... on every language switch, and
      // the api-key-toggle regression needs that walk to reach the toggle
      // the test registered (index.html gives it data-i18n="settings.show").
      querySelectorAll(sel) {
        const out = [];
        this._els.forEach((el) => {
          if (matchesSel(el, sel)) out.push(el);
        });
        return out;
      },
      createElement: (tag) => makeEl('el', tag),
      documentElement: docEl,
      body: makeEl('body'),
      head: makeEl('head'),
      readyState: 'complete',
    },
    window: {
      addEventListener() {}, removeEventListener() {},
      matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
      IntersectionObserver: class {
        constructor(){} observe(){} unobserve(){} disconnect(){}
      },
      performance: { now: () => Date.now() },
    },
    IntersectionObserver: class {
      constructor(){} observe(){} unobserve(){} disconnect(){}
    },
    performance: { now: () => Date.now() },
    navigator: { language: 'zh', clipboard: { writeText: async () => {} }, },
    localStorage: {
      getItem: (k) => (stored.has(k) ? stored.get(k) : null),
      setItem: (k, v) => { stored.set(k, String(v)); },
      removeItem: (k) => { stored.delete(k); },
    },
    URL: SandboxURL,
    Blob: class { constructor(){} },
    FileReader: class {
      readAsDataURL() {
        setImmediate(() => this.onload && this.onload({ target: { result: 'data:image/png;base64,QUFB' } }));
      }
    },
    Image: class {
      set src(_) { setImmediate(() => this.onload && this.onload()); }
    },
    fetch: async () => ({ ok: true, json: async () => ({ content: [{type:'text',text:'{}'}]}), text: async () => '' }),
    AbortController: class {
      constructor() {
        this.signal = {
          aborted: false,
          _listeners: [],
          addEventListener(_e, cb) { this._listeners.push(cb); },
          removeEventListener(_e, cb) {
            this._listeners = this._listeners.filter((l) => l !== cb);
          },
        };
      }
      abort() {
        if (this.signal.aborted) return;
        this.signal.aborted = true;
        for (const cb of this.signal._listeners) {
          try { cb(); } catch (_e) { /* ignore */ }
        }
      }
    },
    DOMException: class DOMException extends Error {
      constructor(message, name) {
        super(message);
        this.name = name || 'Error';
        this.message = message || '';
      }
    },
    Event: class Event {
      constructor(type, init) {
        this.type = type;
        this.bubbles = !!(init && init.bubbles);
        this.cancelable = !!(init && init.cancelable);
      }
      stopPropagation() {}
      preventDefault() {}
    },
    KeyboardEvent: class KeyboardEvent extends Event {
      constructor(type, init) {
        super(type, init);
        this.key = (init && init.key) || '';
        this.ctrlKey = !!(init && init.ctrlKey);
        this.metaKey = !!(init && init.metaKey);
        this.shiftKey = !!(init && init.shiftKey);
        this.altKey = !!(init && init.altKey);
      }
    },
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    location: { protocol: 'http:', host: 'localhost', pathname: '/' },
  };
  ctx.globalThis = ctx;
  // Exposed so the rows-unit assertions can read the CSS custom property
  // applyRowsUnit writes.
  ctx.__cssVars = cssVars;
  return ctx;
}

function loadAllScripts(ctx) {
  vm.createContext(ctx);
  for (const f of [
    'js/config.js',
    'js/i18n.js',
    'js/prompt.js',
    'js/json-utils.js',
    'js/ics_table.js',
    'js/reason-codes.js',
    'js/bed_parser.js', 'js/quality.js',
    'js/aggregate.js',
    'js/table.js',
    'js/export.js',
    'js/error-utils.js',
    'js/minimax.js',
    'js/theme.js',
    'js/app.js',
  ]) {
    let src = fs.readFileSync(path.join(REPO, f), 'utf8');
    if (f === 'js/app.js') {
      // Strip the trailing `})();` so internals become top-level, then
      // re-attach an IIFE wrapper with the test exports inside (the same
      // technique tests_frontend.js uses).
      const tail = '})();';
      if (src.trimEnd().endsWith(tail)) {
        const head = src.slice(0, src.indexOf('(function ()'));
        const inner = src.slice(src.indexOf('(function ()') + '(function () {'.length,
                                  src.lastIndexOf(tail));
        src = head + '(function () {\n  \'use strict\';\n' +
          inner +
          '\n// Test-only exports to globalThis.\n' +
          'globalThis.rcaResultFilePrefix = rcaResultFilePrefix;\n' +
          'globalThis.syncSegmentedFromSelect = syncSegmentedFromSelect;\n' +
          'globalThis.switchLang = switchLang;\n' +
          'globalThis.runExtraction = runExtraction;\n' +
          'globalThis.setBusyLabel = setBusyLabel;\n' +
          'globalThis.state = state;\n' +
          'globalThis.$ = $;\n' +
          '})();';
      }
    }
    try {
      vm.runInContext(src, ctx, { filename: f });
    } catch (loadErr) {
      console.error('WARN: skipping', f, 'due to load error:', loadErr.message);
    }
    // Export top-level bindings of the support files the assertions read.
    const exports = {
      'js/config.js': ['RCA_CONFIG', 'RCA_STORE', 'rcaStoreGet', 'rcaStoreSet',
                       'rcaClampMaxTokens'],
      'js/i18n.js': ['RCA_I18N', 'rcaSetLang', 'rcaApplyI18n', 't'],
      'js/table.js': [
        'rcaTableConfigs', 'rcaIsDict',
        // The two predicates the 2026-10-28 wave added to the prefix mirror.
        'rcaLooksOccurrenceMatrix', 'rcaLooksPaleomap',
        'rcaLooksZonationChart', 'rcaLooksAbundance',
      ],
    };
    const toExport = exports[f] || [];
    if (toExport.length) {
      let exportSrc = '';
      for (const name of toExport) {
        exportSrc += `if (typeof ${name} !== 'undefined') globalThis.${name} = ${name};\n`;
      }
      vm.runInContext(exportSrc, ctx);
    }
  }
}

function fresh() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  return ctx;
}

// ===========================================================================
// FIX 1 — rcaResultFilePrefix must mirror table.js's FULL dispatch
// (zonation → occurrence_matrix → abundance → columnar → phylo → paleomap →
// range_chart), so occurrence-matrix and paleomap results can no longer
// export as range_chart_result.json / abundance_diagram_*.
// Fixtures are built from the predicates at js/table.js:88 (paleomap) and
// js/table.js:192 (occurrence matrix).
// ===========================================================================
(() => {
  const ctx = fresh();
  const P = ctx.rcaResultFilePrefix;

  // Minimal occurrence-matrix shape: rcaLooksOccurrenceMatrix keys on a
  // non-empty `occurrences` list whose first row is a dict.
  const OCC = {
    occurrences: [{ taxon: 'M. motoyamai', authority: 'Chen', site: 'Loc-1',
      code: 'a', class: 'Ammonoidea' }],
  };
  // The other half of the same predicate: `taxon_rows` (the `gated` cell
  // strategy's payload) must map to occurrence too.
  const OCC_TAXA = { taxon_rows: [{ index: 0, taxon: 'T', authority: 'A' }] };
  // Minimal paleomap shape: one paleomap-exclusive key holding dict rows
  // (table.js KEYS list — continents is the first).
  const PALEO = {
    continents: [{ name: 'Laurentia', type: 'craton', paleolatitude: '15N',
      coordinates: '10, -30' }],
  };
  // The mislabel case: an occurrence answer that ALSO carries abundance-like
  // rows. rcaLooksAbundance fires on it — occurrence must still win, because
  // the dispatch (and now the prefix) tests occurrence FIRST.
  const OCC_ABUND = {
    occurrences: OCC.occurrences,
    abundances: [{ taxon: 'x', site: 's', level: '1', depth: '2 m',
      abundance: '12', abundance_unit: '%' }],
  };
  // Plain range chart: hits NO predicate, falls through to the default.
  const RANGE = {
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'Sp', section: 'S1', range_base: 'bed 1',
      range_top: 'bed 9', biozone: 'Z1' }],
  };

  check('fix1-occurrence-matrix-prefix', P(OCC) === 'occurrence_matrix_', P(OCC));
  check('fix1-occurrence-via-taxon-rows', P(OCC_TAXA) === 'occurrence_matrix_', P(OCC_TAXA));
  check('fix1-paleomap-prefix', P(PALEO) === 'paleomap_', P(PALEO));

  // Order, not just membership: the payload IS abundance-shaped too.
  check('fix1-occurrence-payload-is-also-abundance-shaped',
    ctx.rcaLooksAbundance(OCC_ABUND) === true);
  check('fix1-occurrence-tested-before-abundance',
    P(OCC_ABUND) === 'occurrence_matrix_', P(OCC_ABUND));

  // The name can never contradict the contents: whatever tables table.js
  // renders for these payloads, the prefix must name.
  check('fix1-occurrence-prefix-matches-tables',
    ctx.rcaTableConfigs(OCC).some((c) => c.id === 'occurrences'));
  check('fix1-paleomap-prefix-matches-tables',
    ctx.rcaTableConfigs(PALEO).some((c) => c.id === 'continents'));

  // The fallback and the pre-existing mappings stay put.
  check('fix1-range-chart-fallback', P(RANGE) === 'range_chart_', P(RANGE));
  check('fix1-null-safe', P(null) === 'range_chart_', P(null));
  check('fix1-zonation-still-first',
    P({ correlations: [{ from_zone: 'a', to_zone: 'b' }] }) === 'zonation_chart_');
})();

// ===========================================================================
// FIX 2 — syncSegmentedFromSelect's roving tabindex: exactly ONE tab stop,
// on the checked button; the first button only when nothing is checked.
// The old `isChecked || !taken` gave the first button a SECOND tab stop
// whenever a non-first option was selected.
// ===========================================================================
(() => {
  const ctx = fresh();
  const seg = ctx.$('conn-mode-seg');
  const sel = ctx.$('conn-mode');
  const mkBtn = (v) => {
    const b = ctx.document.createElement('button');
    b.setAttribute('data-value', v);
    b.setAttribute('role', 'radio');
    seg.appendChild(b);
    return b;
  };
  const b1 = mkBtn('backend');
  const b2 = mkBtn('direct');
  const b3 = mkBtn('auto');
  const btns = [b1, b2, b3];
  const tabbable = () => btns.filter((b) => b.getAttribute('tabindex') === '0');
  const names = (list) => list.map((b) => b.getAttribute('data-value')).join(',');

  sel.value = 'direct';
  ctx.syncSegmentedFromSelect();
  let t = tabbable();
  check('fix2-selected-non-first-exactly-one', t.length === 1, names(t));
  check('fix2-selected-non-first-is-the-checked-one',
    t.length === 1 && t[0] === b2, names(t));
  check('fix2-aria-checked-travels',
    b1.getAttribute('aria-checked') === 'false'
    && b2.getAttribute('aria-checked') === 'true'
    && b3.getAttribute('aria-checked') === 'false');

  sel.value = 'auto';
  ctx.syncSegmentedFromSelect();
  t = tabbable();
  check('fix2-last-option-no-second-tab-stop',
    t.length === 1 && t[0] === b3, names(t));

  // Unknown stored value: fall back to the FIRST button, still one stop.
  sel.value = 'bogus';
  ctx.syncSegmentedFromSelect();
  t = tabbable();
  check('fix2-unknown-value-falls-back-to-first',
    t.length === 1 && t[0] === b1, names(t));
  check('fix2-unknown-value-nothing-checked',
    btns.every((b) => b.getAttribute('aria-checked') === 'false'));

  sel.value = 'backend';
  ctx.syncSegmentedFromSelect();
  t = tabbable();
  check('fix2-first-option-selected',
    t.length === 1 && t[0] === b1, names(t));

  // Re-sync must fully re-write stale tab indices (no residue from the
  // previous state).
  check('fix2-others-untabbable',
    b2.getAttribute('tabindex') === '-1' && b3.getAttribute('tabindex') === '-1');
})();

// ===========================================================================
// FIX 3 — switchLang must refresh the selection bar's rows unit
// (--rca-rows-unit), which lives in a CSS custom property the static i18n
// walk cannot reach. applyRowsUnit() was only called from init().
// ===========================================================================
(() => {
  const ctx = fresh();
  const vars = ctx.__cssVars;
  // init() already ran applyRowsUnit for the stored/default language (zh).
  check('fix3-initial-unit-zh', vars['--rca-rows-unit'] === ' 行',
    vars['--rca-rows-unit']);

  ctx.switchLang('en');
  check('fix3-rows-unit-en', vars['--rca-rows-unit'] === ' row(s)',
    vars['--rca-rows-unit']);

  ctx.switchLang('ja');
  check('fix3-rows-unit-ja', vars['--rca-rows-unit'] === ' 行',
    vars['--rca-rows-unit']);

  ctx.switchLang('zh');
  check('fix3-rows-unit-back-to-zh', vars['--rca-rows-unit'] === ' 行',
    vars['--rca-rows-unit']);
})();

// ===========================================================================
// FIX 4 — after a language switch, #api-key-toggle must read its label from
// the ACTUAL input state, not from data-i18n="settings.show" (index.html).
// The toggle is registered with the same data-i18n attribute the real page
// ships, so rcaApplyI18n's walk really does clobber the label and the
// post-i18n re-apply has to correct it.
// ===========================================================================
(() => {
  const ctx = fresh();
  const input = ctx.$('api-key');
  const toggle = ctx.$('api-key-toggle');
  // Same hook index.html ships (index.html:104).
  toggle.setAttribute('data-i18n', 'settings.show');

  input.type = 'text';  // key currently VISIBLE
  ctx.switchLang('en');
  check('fix4-visible-key-reads-hide-en',
    toggle.textContent === ctx.t('settings.hide'), toggle.textContent);

  ctx.switchLang('zh');
  check('fix4-visible-key-reads-hide-zh',
    toggle.textContent === ctx.t('settings.hide'), toggle.textContent);

  input.type = 'password';  // hidden again
  ctx.switchLang('en');
  check('fix4-hidden-key-reads-show',
    toggle.textContent === ctx.t('settings.show'), toggle.textContent);

  // The click handler shares the same label helper, so the wording can
  // never drift between the two writers.
  toggle.dispatchEvent(new ctx.Event('click'));
  check('fix4-click-reveals-and-relabels',
    input.type === 'text' && toggle.textContent === ctx.t('settings.hide'),
    input.type + ' / ' + toggle.textContent);
  toggle.dispatchEvent(new ctx.Event('click'));
  check('fix4-click-hides-and-relabels',
    input.type === 'password' && toggle.textContent === ctx.t('settings.show'),
    input.type + ' / ' + toggle.textContent);
})();

// ===========================================================================
// FIX 5 — backend multi-run extraction must keep the plain 'analyzing'
// label (no permanently-stuck "0/3") and must NEVER reveal #run-progress
// (index.html:340-354: the determinate bar is direct-mode only).
// runExtraction is driven end-to-end with the transport mocked at the
// extractRangeChart boundary; the observation happens INSIDE the mock, i.e.
// exactly while the busy label is live.
// ===========================================================================
async function testFix5BackendProgress() {
  const ctx = fresh();
  ctx.state.busy = false;
  ctx.state.loading = false;
  ctx.$('api-key').value = 'test-key';
  ctx.$('endpoint').value = 'http://127.0.0.1:8000';
  ctx.state.dataUrl = 'data:image/png;base64,QUFB';
  ctx.state.mediaType = 'image/png';
  ctx.$('conn-mode').value = 'backend';     // force the backend transport
  ctx.$('chart-mode').value = 'range_chart';
  ctx.$('runs').value = '3';                // multi-run

  // The label paragraph setBusyLabel writes into.
  const slot = ctx.$('loading-slot');
  slot.appendChild(ctx.document.createElement('p'));
  const prog = ctx.$('run-progress');
  const progFill = ctx.$('run-progress-fill');

  let seen = null;
  ctx.extractRangeChart = async (opts) => {
    seen = {
      transport: opts.transport,
      runs: opts.runs,
      // Observed mid-flight, while the busy label is on screen.
      label: slot.querySelector('p').textContent,
      progHidden: prog.classList.contains('hidden'),
    };
    return { ok: false, errorKey: 'err.http', status: 500 };
  };

  await ctx.runExtraction();

  check('fix5-really-backend-multi-run',
    !!seen && seen.transport === 'backend' && seen.runs === 3,
    JSON.stringify(seen));
  check('fix5-label-has-no-counter',
    !!seen && seen.label === ctx.t('loading.analyzing'),
    seen && seen.label);
  check('fix5-progress-hidden-during-backend-run',
    !!seen && seen.progHidden === true,
    seen && String(seen.progHidden));
  // aria-valuetext is written ONLY by _rcaSetRunProgress — its absence
  // proves the bar was never driven. (aria-valuenow is legitimately
  // zeroed by updateActionButtons, so it proves nothing.)
  check('fix5-progress-never-driven',
    prog.getAttribute('aria-valuetext') === null,
    prog.getAttribute('aria-valuetext'));
  check('fix5-progress-never-revealed',
    !prog.classList._calls.some((c) => c[0] === 'remove' && c[1] === 'hidden'),
    JSON.stringify(prog.classList._calls));
  check('fix5-fill-never-advanced',
    !progFill.style.width || progFill.style.width === '0%',
    progFill.style.width);

  // Symmetry: the DIRECT multi-run path still gets the counter label and
  // drives the bar — the fix must not have starved it.
  const ctx2 = fresh();
  ctx2.state.busy = false;
  ctx2.state.loading = false;
  ctx2.$('api-key').value = 'test-key';
  ctx2.$('endpoint').value = 'http://127.0.0.1:8000';
  ctx2.state.dataUrl = 'data:image/png;base64,QUFB';
  ctx2.state.mediaType = 'image/png';
  ctx2.$('conn-mode').value = 'direct';
  ctx2.$('chart-mode').value = 'range_chart';
  ctx2.$('runs').value = '3';
  const slot2 = ctx2.$('loading-slot');
  slot2.appendChild(ctx2.document.createElement('p'));
  const prog2 = ctx2.$('run-progress');

  let seen2 = null;
  ctx2.extractRangeChart = async () => {
    seen2 = {
      label: slot2.querySelector('p').textContent,
      revealed: !prog2.classList.contains('hidden'),
      valuetext: prog2.getAttribute('aria-valuetext'),
    };
    return { ok: false, errorKey: 'err.http', status: 500 };
  };
  await ctx2.runExtraction();
  check('fix6-direct-multi-run-still-counts',
    !!seen2 && seen2.label === ctx2.t('loading.analyzing') + ' (0/3)',
    seen2 && seen2.label);
  check('fix6-direct-multi-run-drives-bar',
    !!seen2 && seen2.revealed === true && seen2.valuetext === '0/3',
    JSON.stringify(seen2));
}

// ---------------------------------------------------------------------------
(async () => {
  await testFix5BackendProgress();

  console.log('');
  if (fail) { console.log('failed:', failed.join(', ')); }
  console.log(`--- ${pass} passed, ${fail} failed ---`);
  process.exit(fail ? 1 : 0);
})().catch((e) => {
  console.error('SUITE ERROR', e);
  process.exit(1);
});
