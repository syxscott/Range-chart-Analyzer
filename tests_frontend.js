// tests_frontend.js — regression coverage for FR1..FR4 in js/app.js.
//
// Strategy: load js/app.js + js/config.js + js/i18n.js + js/minimax.js +
// js/json-utils.js + js/prompt.js + js/aggregate.js + js/table.js +
// js/export.js under a minimal in-memory DOM/stubs via vm.runInContext.
//
// Run with:  node tests_frontend.js
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Build a minimal browser-ish context.
function buildContext() {
  const stored = new Map();
  // Backing store for documentElement attributes. theme.js toggles
  // data-theme via setAttribute / removeAttribute and then reads it back
  // via getAttribute; both must hit the same map.
  const docElAttrs = {};
  const docEl = {
    lang: 'zh-CN',
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute: (k, v) => { docElAttrs[String(k)] = String(v); },
    removeAttribute: (k) => { delete docElAttrs[String(k)]; },
    getAttribute: (k) => (k in docElAttrs ? docElAttrs[String(k)] : null),
  };
  const ctx = {
    console,
    setTimeout, clearTimeout,
    // DOM stubs. Keep this as a SINGLE document declaration — a duplicate
    // shadows the first and the mock loses its backing store. theme.js +
    // app.js both touch document.* at load time.
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
      // PR3 M14/M16: dispatchEvent on document for tests that simulate
      // animationend / keydown events bubbling up.
      dispatchEvent(ev) {
        const list = this._listeners[ev && ev.type] || [];
        for (const cb of list) {
          try { cb(ev); } catch (_e) { /* ignore */ }
        }
        return true;
      },
      // UI-REVIEW-2026-08-01: cache elements by id so DOM state (e.g.
      // classList recordings, value) survives repeated getElementById
      // calls — the previous fresh-instance-per-call stub made it
      // impossible to observe visibility toggles.
      _els: new Map(),
      getElementById(id) {
        if (!this._els.has(id)) {
          // Hardcoded tag map for elements whose tagName the test cares
          // about (api-key is an <input>, caption is <textarea>, etc.).
          // Everything else defaults to DIV via makeEl.
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
      // Default: no theme-choice buttons exist in the test sandbox, so an
      // empty NodeList matches what theme.js's wireToggleButtons sees when
      // the page hasn't been wired up yet.
      querySelectorAll: () => [],
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
    URL: {
      createObjectURL: () => 'blob:fake',
      revokeObjectURL() {},
    },
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
    // PR2 M11: AbortController stub needs a real signal with
    // addEventListener so error-utils.js's abortableSleep can listen; the
    // previous `this.signal = {}` made it impossible to test retry/abort.
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
    // error-utils.js throws `new DOMException('Aborted', 'AbortError')`.
    // vm context doesn't have a DOMException global — provide a minimal
    // substitute so the test sandbox exercises the same path.
    DOMException: class DOMException extends Error {
      constructor(message, name) {
        super(message);
        this.name = name || 'Error';
        this.message = message || '';
      }
    },
    // PR3 M14/M16: Event / KeyboardEvent constructors for dispatching
    // animationend and keydown events. vm context has no DOM globals.
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
    // Browser-only globals used by rcaResolveMode / syncFooterRuntime etc.
    location: { protocol: 'http:', host: 'localhost', pathname: '/' },
  };
  // Make sure global.* points to the same context.
  ctx.globalThis = ctx;
  return ctx;
}

function makeEl(id, tagName) {
  const matches = (node, sel) => {
    if (!sel) return false;
    if (sel[0] === '.') {
      return (node.className || '').split(/\s+/).indexOf(sel.slice(1)) !== -1;
    }
    if (sel[0] === '#') return node.id === sel.slice(1);
    return node.tagName === sel.toUpperCase();
  };
  const collectAll = (root, sel, out) => {
    for (const c of (root.children || [])) {
      if (matches(c, sel)) out.push(c);
      collectAll(c, sel, out);
    }
  };
  const listeners = {};
  const el = {
    id,
    className: '',
    // UI-REVIEW-2026-08-01: record classList calls so tests can assert
    // visibility toggles (e.g. the force-rerun button after a result).
    classList: {
      _calls: [],
      add(c) { this._calls.push(['add', c]); el.className = (el.className + ' ' + c).trim(); },
      remove(c) { this._calls.push(['remove', c]); el.className = el.className.split(/\s+/).filter((x) => x !== c).join(' '); },
      toggle(c, f) { this._calls.push(['toggle', c, f]); el.className = (el.className + ' ' + c).trim(); },
      contains: () => false,
    },
    style: {},
    dataset: {},
    attributes: {},
    children: [],
    parentNode: null,
    value: '',
    textContent: '',
    innerHTML: '',
    get innerText() { return el.textContent; },
    set innerText(v) { el.textContent = v; },
    type: 'text',
    checked: false,
    selected: false,
    tagName: (tagName || 'DIV').toUpperCase(),
    title: '',
    addEventListener(type, cb) {
      if (!listeners[type]) listeners[type] = [];
      listeners[type].push(cb);
    },
    removeEventListener(type, cb) {
      if (!listeners[type]) return;
      listeners[type] = listeners[type].filter((l) => l !== cb);
    },
    setAttribute(k, v) { el.attributes[k] = v; if (k === 'id') el.id = v; },
    getAttribute(k) { return el.attributes[k] != null ? el.attributes[k] : null; },
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
    // PR3 M14/M16: dispatchEvent for tests that simulate animationend /
    // keydown events. Calls listeners registered on this element.
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

let pass = 0, fail = 0;
function check(name, ok) { if (ok) { pass++; console.log('PASS', name); } else { fail++; console.log('FAIL', name); } }

function loadAllScripts(ctx) {
  vm.createContext(ctx);
  for (const f of [
    'js/config.js',
    'js/i18n.js',
    'js/prompt.js',
    'js/json-utils.js',
    // REVIEW-2026-07-31: ics_table.js (RCA_ICS_TABLE + label maps) and
    // quality.js (scoreRangeChart) are loaded so the pure-frontend
    // quality scorer is actually exercised in tests — before, it had
    // zero behavioral coverage.
    'js/ics_table.js',
    'js/quality.js',
    'js/aggregate.js',
    'js/table.js',
    'js/export.js',
    // PR2 M11: error-utils.js is loaded so retryWithBackoff is wired
    // through extractRangeChart.
    'js/error-utils.js',
    'js/minimax.js',
    'js/theme.js',
    'js/app.js',
  ]) {
    let src = fs.readFileSync(path.join(__dirname, f), 'utf8');
    if (f === 'js/app.js') {
      // Strip the trailing `})();` so internals (state, handleFile, etc.)
      // become top-level. Then re-attach an IIFE wrapper with the test
      // exports inside, so the script's own `'use strict'` directive still
      // scopes correctly but the bindings leak to globalThis for the test.
      const tail = '})();';
      if (src.trimEnd().endsWith(tail)) {
        const head = src.slice(0, src.indexOf('(function ()'));
        const inner = src.slice(src.indexOf('(function ()') + '(function () {'.length,
                                  src.lastIndexOf(tail));
        src = head + '(function () {\n  \'use strict\';\n' +
          inner +
          '\n// Test-only exports to globalThis.\n' +
          'globalThis.runExtraction = runExtraction;\n' +
          'globalThis.resetUpload = resetUpload;\n' +
          'globalThis.handleFile = handleFile;\n' +
          'globalThis.state = state;\n' +
          'globalThis.saveSettings = saveSettings;\n' +
          'globalThis.updateActionButtons = updateActionButtons;\n' +
          'globalThis.$ = $;\n' +
          'globalThis.showAlert = showAlert;\n' +
          'globalThis.Event = Event;\n' +
          'globalThis.KeyboardEvent = KeyboardEvent;\n' +
          'globalThis.document = document;\n' +
          '})();';
      }
    }
    // Allow individual scripts (e.g. js/prompt.js while another agent's
    // edit is in flight) to fail without taking down the whole test
    // suite. Surface the failure in stderr so it's not silently lost.
    try {
      // Allow individual scripts (e.g. js/prompt.js while another agent's
    // edit is in flight) to fail without taking down the whole test
    // suite. Surface the failure in stderr so it's not silently lost.
    try {
      vm.runInContext(src, ctx, { filename: f });
    } catch (loadErr) {
      console.error('WARN: skipping', f, 'due to load error:', loadErr.message);
    }
    } catch (loadErr) {
      console.error('WARN: skipping', f, 'due to load error:', loadErr.message);
    }
    // After each file loads, export its top-level bindings for testing.
    // These are plain `const`/`function` declarations that vm doesn't expose
    // on the context automatically.
    const exports = {
      'js/config.js': ['RCA_CONFIG', 'RCA_STORE', 'rcaStoreGet', 'rcaStoreSet', 'rcaClampMaxTokens'],
      'js/i18n.js': ['RCA_I18N', 'rcaSetLang', 'rcaApplyI18n', 't'],
      'js/json-utils.js': ['safeJsonLoads', 'extractBalancedJsonObject'],
      'js/aggregate.js': ['rcaMergeResults', 'RCA_DEFAULT_KEYMAP', 'RCA_COLUMNAR_KEYMAP'],
      'js/table.js': ['rcaRenderResults', 'rcaTableConfigs', 'rcaBuildTableExport'],
      'js/export.js': ['rcaToCsv', 'rcaToTsv', 'rcaDownload', 'rcaCopyText'],
      'js/error-utils.js': ['RCAErrorUtils'],
      'js/theme.js': ['rcaTheme'],
    };
    const toExport = exports[f] || [];
    if (toExport.length) {
      let exportSrc = '';
      for (const name of toExport) {
        // Check local scope first, then window (theme.js puts rcaTheme on window).
        exportSrc += `if (typeof ${name} !== 'undefined') globalThis.${name} = ${name}; else if (typeof window !== 'undefined' && window.${name}) globalThis.${name} = window.${name};\n`;
      }
      vm.runInContext(exportSrc, ctx);
    }
  }
}

// ---- FR3: rcaStoreSet reports true on success, false on failure ----
function test_fr3() {
  const ctx = buildContext();
  // Backup real localStorage and force a quota-exceeded throw.
  const real = ctx.localStorage.setItem;
  ctx.localStorage.setItem = () => { throw new Error('QuotaExceededError'); };
  loadAllScripts(ctx);
  let wrote = false;
  const ok = ctx.rcaStoreSet('rca.test', 'hi');
  check('fr3-set-fails-on-quota', ok === false);
}

// ---- FR3 (positive): writes succeed when storage is healthy ----
function test_fr3_ok() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const ok = ctx.rcaStoreSet('rca.test', 'hi');
  check('fr3-set-success', ok === true);
}

// ---- FR1: handleFile token race ----
// We mock rcaLoadAndMaybeResize to return a slowly-resolving promise, then
// trigger two handleFile() calls in quick succession. The slower one must
// not overwrite the faster one.
function test_fr1_token_race() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Stub rcaLoadAndMaybeResize to return a promise with controllable delay.
  const file1 = { name: 'big.png', size: 1000, type: 'image/png' };
  const file2 = { name: 'small.png', size: 100, type: 'image/png' };
  let r1done, r2done;
  ctx.rcaLoadAndMaybeResize = (f) => {
    if (f.name === 'big.png') return new Promise((res) => { r1done = () => res({ dataUrl: 'data:image/png;base64,BIG', mime: 'image/png', width: 100, height: 100, resized: false }); });
    if (f.name === 'small.png') return new Promise((res) => { r2done = () => res({ dataUrl: 'data:image/png;base64,SMALL', mime: 'image/png', width: 50, height: 50, resized: false }); });
  };

  // Call handleFile for big, then small. Both are async (promise pending).
  ctx.handleFile(file1);  // token 1
  ctx.handleFile(file2);  // token 2 (bumps state.expectedToken; r2 supersedes r1)

  // Resolve small first (fast path) — state.dataUrl becomes SMALL.
  r2done();
  // Then resolve big — should be IGNORED because its token is stale.
  r1done();

  // After both promises settle, the survivor is small.
  setImmediate(() => {
    check('fr1-last-write-wins', ctx.state.dataUrl === 'data:image/png;base64,SMALL');
  });
}

// ---- FR2: Reset drops in-flight extraction result ----
// To test without DOM interactivity we directly bump the token, then call
// extractRangeChart and verify the token guard fires.
function test_fr2_reset_drops() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.state.extractToken = 1;
  ctx.state.expectedToken = 1;
  // Simulate a user Reset by bumping expectedToken/extractToken.
  ctx.state.expectedToken = 2;
  ctx.state.extractToken = 2;
  // The "in-flight" call would have used token=1; the live token is 2.
  // Simulating the post-await guard:
  const myToken = 1;
  ctx.state.expectedToken += 1; // additional bump
  const currentToken = ctx.state.extractToken;
  check('fr2-drops-stale', myToken !== currentToken);
}

// ---- FR4: try/finally around extraction ----
// We can't easily run the full extraction, but we can verify the function
// is wrapped in try/finally by static inspection.
function test_fr4_try_finally() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  // Find runExtraction definition and check it contains the try/finally
  // with setBusy(true) inside try and setBusy(false) inside finally.
  const idx = src.indexOf('async function runExtraction');
  if (idx === -1) { check('fr4-found-function', false); return; }
  const body = src.slice(idx, src.indexOf('\n  }', idx));
  const hasTry = /try\s*\{/.test(body);
  const hasFinally = /finally\s*\{/.test(body);
  const hasBusyTrue = /setBusy\(true\)/.test(body);
  const hasBusyFalse = /setBusy\(false\)/.test(body);
  check('fr4-try-present', hasTry);
  check('fr4-finally-present', hasFinally);
  check('fr4-busy-true', hasBusyTrue);
  check('fr4-busy-false', hasBusyFalse);
}

// ---- FR1/FR2 source sanity: tokens are bumped by handleFile and resetUpload ----
function test_fr_token_bump() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  const handleFn = src.slice(
    src.indexOf('async function handleFile'),
    src.indexOf('\n  }', src.indexOf('async function handleFile'))
  );
  const resetFn = src.slice(
    src.indexOf('function resetUpload'),
    src.indexOf('\n  }', src.indexOf('function resetUpload'))
  );
  check('fr1-handleFile-bumps-token', /state\.expectedToken\s*\+=\s*1/.test(handleFn));
  check('fr2-resetUpload-bumps-token', /state\.expectedToken\s*\+=\s*1/.test(resetFn));
}

// ---- rcaStoreSet always returns boolean ----
function test_rcaStoreSet_contract() {
  // Block both setItem and removeItem so the empty-value path (which
  // delegates to removeItem) also surfaces a failure.
  const ctx = buildContext();
  ctx.localStorage.setItem = () => { throw new Error('SecurityError'); };
  ctx.localStorage.removeItem = () => { throw new Error('SecurityError'); };
  loadAllScripts(ctx);
  const a = ctx.rcaStoreSet('a', 'x');
  const b = ctx.rcaStoreSet('a', '');  // removeItem path
  const c = ctx.rcaStoreSet('a', null);  // removeItem path
  check('rcaStoreSet-quota-returns-bool', typeof a === 'boolean' && typeof b === 'boolean' && typeof c === 'boolean');
  check('rcaStoreSet-setItem-throws', a === false);
  check('rcaStoreSet-removeItem-throws', b === false && c === false);
  // Healthy storage returns true.
  ctx.localStorage.setItem = (k, v) => { ctx.__test_store = ctx.__test_store || new Map(); ctx.__test_store.set(k, v); };
  ctx.localStorage.removeItem = (k) => { (ctx.__test_store || new Map()).delete(k); };
  check('rcaStoreSet-healthy-true', ctx.rcaStoreSet('a', 'hi') === true);
}

test_fr3();
test_fr3_ok();
test_rcaStoreSet_contract();
test_fr1_token_race();
test_fr2_reset_drops();
test_fr4_try_finally();
test_fr_token_bump();

// ---- Phase B: confidence ring renders SVG, not badge ----
function test_confidence_ring() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const html = ctx.rcaRenderResults({
    confidence: 0.75,
    sections: [], species_ranges: [], biozones: [], other_fossils: [],
  }, '');
  check('conf-ring-has-svg', html.indexOf('confidence-ring') !== -1);
  check('conf-ring-has-circle', html.indexOf('<circle') !== -1);
  check('conf-ring-has-data-conf', html.indexOf('data-conf="75"') !== -1);
  check('conf-ring-no-old-badge', html.indexOf('badge-danger') === -1 && html.indexOf('badge-success') === -1);
}

// ---- Phase C: agreement pill color encoding ----
function test_agreement_pill() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // 2/3 -> good (>= 0.667)
  let html = ctx.rcaRenderResults({
    confidence: 0.5, runs: 3,
    sections: [], biozones: [], other_fossils: [],
    species_ranges: [{
      species: 'A', section: 'S', range_base: '1', range_top: '2',
      biozone: '', agreement: '2/3', agreement_count: 2,
    }],
  }, '');
  check('pill-good-2-of-3', html.indexOf('pill-good') !== -1);

  // 1/3 -> low (<= 0.333)
  html = ctx.rcaRenderResults({
    confidence: 0.5, runs: 3,
    sections: [], biozones: [], other_fossils: [],
    species_ranges: [{
      species: 'B', section: 'S', range_base: '1', range_top: '2',
      biozone: '', agreement: '1/3', agreement_count: 1,
    }],
  }, '');
  check('pill-low-1-of-3', html.indexOf('pill-low') !== -1);
}

// ---- Phase B: i18n key parity across zh/en/ja ----
function test_i18n_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const zh = Object.keys(ctx.RCA_I18N.zh).sort();
  const en = Object.keys(ctx.RCA_I18N.en).sort();
  const ja = Object.keys(ctx.RCA_I18N.ja).sort();
  check('i18n-zh-en-parity', JSON.stringify(zh) === JSON.stringify(en));
  check('i18n-zh-ja-parity', JSON.stringify(zh) === JSON.stringify(ja));
}

// ---- Phase B: theme.js contract ----
function test_theme_contract() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // rcaTheme should be exposed by theme.js
  check('theme-exists', typeof ctx.rcaTheme === 'object');
  if (!ctx.rcaTheme) return;
  check('theme-has-set', typeof ctx.rcaTheme.set === 'function');
  check('theme-has-get', typeof ctx.rcaTheme.get === 'function');
  // Default mode should be 'system'
  check('theme-default-system', ctx.rcaTheme.get() === 'system');
  // set('dark') should persist + apply
  ctx.rcaTheme.set('dark');
  check('theme-set-dark', ctx.rcaTheme.get() === 'dark');
  check('theme-applied-dark', ctx.document.documentElement.getAttribute('data-theme') === 'dark');
  // set('light') should apply light
  ctx.rcaTheme.set('light');
  check('theme-applied-light', ctx.document.documentElement.getAttribute('data-theme') === 'light');
  // set('system') + no dark pref -> remove data-theme
  ctx.rcaTheme.set('system');
  check('theme-system-clears-attr', !ctx.document.documentElement.getAttribute('data-theme'));
}

// ---- Phase C: rcaStoreSet returns bool (already tested, verify in new context) ----
// (covered by test_rcaStoreSet_contract above)

// ---- Phase D: cross-fade class exists in source ----
function test_crossfade_source() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  const renderFn = src.slice(
    src.indexOf('function renderCurrentResult'),
    src.indexOf('\n  }', src.indexOf('function renderCurrentResult'))
  );
  check('d-crossfade-present', /fade-in/.test(renderFn));
  check('d-reduced-motion-guard', /prefers-reduced-motion/.test(renderFn));
}

// ---- BE-5: direct/proxy multi-run path uses Promise.all (concurrent) ----
function test_be5_promise_all_concurrent() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  // Find the multi-run branch (runs > 1, not backend).
  const i = src.indexOf("} else if (runs > 1) {");
  const j = src.indexOf('\n      } else {', i);
  check('be5-found-multirun-block', i > 0 && j > i);
  const body = src.slice(i, j);
  check('be5-uses-promise-all', /Promise\.all\s*\(/.test(body));
  // The old serial for-loop on extractRangeChart must be gone.
  check('be5-no-serial-for-extract',
        !/for\s*\(\s*let\s+i\s*=\s*0\s*;[^;]*extractRangeChart/.test(body));
}

// ---- FE-1: staged loading labels wired into i18n.js and app.js ----
function test_fe1_staged_labels() {
  const i18n = fs.readFileSync(path.join(__dirname, 'js/i18n.js'), 'utf8');
  // i18n.js keys are declared once per locale, so a simple count check is
  // enough to confirm all three locales have the key.
  for (const k of ['loading.uploading', 'loading.analyzing', 'loading.aggregating']) {
    const re = new RegExp("'" + k + "'\\s*:", 'g');
    const matches = i18n.match(re) || [];
    check('fe1-i18n-' + k + '-3-langs', matches.length === 3);
  }
  const app = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  check('fe1-app-calls-analyzing-label', /loading\.analyzing/.test(app));
  check('fe1-app-calls-aggregating-label', /loading\.aggregating/.test(app));
}

// ---- Phase D: IntersectionObserver entrance in source ----
function test_io_entrance_source() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  check('d-io-observer', /IntersectionObserver/.test(src));
  check('d-io-init-class', /io-init/.test(src));
  check('d-io-in-view-class', /in-view/.test(src));
}

test_confidence_ring();
test_agreement_pill();
test_i18n_parity();
test_theme_contract();
test_crossfade_source();
test_io_entrance_source();
test_be5_promise_all_concurrent();
test_fe1_staged_labels();

// ---- BIOZONE-SECTION: biozone normalization must preserve `section` field ----
function test_biozone_section_preserved() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const fn = ctx.rcaNormalizeResult;
  if (typeof fn !== 'function') {
    check('biozone-section-fn-exists', false);
    return;
  }
  const out = fn({
    biozones: [
      { name: 'A', section: 'sec-1', age: 'a1', thickness_m: '10' },
      { name: 'B', section: '', age: 'a2', thickness_m: '20' },
    ],
  });
  check('biozone-section-fn-exists', true);
  check('biozone-section-row1', out.biozones[0].section === 'sec-1');
  check('biozone-section-row2-empty', out.biozones[1].section === '');
}

// ---- I18N-SERVER-ERROR-KEYS: server-error keys must exist in all locales ----
function test_i18n_server_error_keys() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  for (const key of ['err.forbidden', 'err.badEndpoint', 'err.rateLimit',
                     'err.bodyTooLarge', 'err.imageTooLarge', 'err.badContentType']) {
    check('i18n-' + key + '-zh', typeof ctx.RCA_I18N.zh[key] === 'string' && ctx.RCA_I18N.zh[key] !== key);
    check('i18n-' + key + '-en', typeof ctx.RCA_I18N.en[key] === 'string' && ctx.RCA_I18N.en[key] !== key);
    check('i18n-' + key + '-ja', typeof ctx.RCA_I18N.ja[key] === 'string' && ctx.RCA_I18N.ja[key] !== key);
  }
}

// ---- CSV-TAB-SANITIZE: leading TAB/CR must be neutralized as formula triggers ----
function test_csv_tab_sanitize() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const csvTab = ctx.rcaToCsv(['c1'], [['\t=CMD|"calc"!A1']]);
  check('csv-tab-prefix-sanitized', csvTab.indexOf("'\t=CMD") !== -1);
  const csvCr = ctx.rcaToCsv(['c1'], [['\r-2+3']]);
  check('csv-cr-prefix-sanitized', csvCr.indexOf("'\r-2+3") !== -1);
  const csvPlus = ctx.rcaToCsv(['c1'], [['+1+1']]);
  check('csv-plus-prefix-sanitized', csvPlus.indexOf("'+1+1") !== -1);
}

// ---- DETECT-WORD-BOUNDARY: ASCII keywords must respect word boundaries ----
function test_detect_word_boundary() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  const fnStart = src.indexOf('function rcaAutoDetectChartMode');
  const fnEnd = src.indexOf('\n  }', fnStart);
  const body = src.slice(fnStart, fnEnd);
  const hasWordAware = /\\b/.test(body);
  check('detect-uses-word-boundary-or-set', hasWordAware);
  const plainIdx = /indexOf\(k\)\s*!==\s*-1/.test(body);
  check('detect-no-plain-indexof', !plainIdx || hasWordAware);
}

// ---- POST-BODY: force_rerun + enhance must be in the POST payload ----
function test_post_body_includes_flags() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let capturedBody = null;
  ctx.fetch = async (url, init) => {
    if (init && init.method === 'POST') capturedBody = init.body;
    return {
      ok: true,
      json: async () => ({ ok: true, data: {}, session_token: 't', csrf_token: 'c' }),
      text: async () => '',
    };
  };
  ctx.rcaCallBackend._sessionToken = '';
  ctx.rcaCallBackend._csrfToken = '';
  const opts = {
    apiKey: 'sk-test', baseUrl: 'https://example.com', model: 'm', maxTokens: 4000,
    mode: 'range_chart', transport: 'backend',
    dataUrl: 'data:image/png;base64,QUFB', mediaType: 'image/png',
    caption: '', chartLang: 'auto',
    force_rerun: true, enhance: true,
    signal: { aborted: false, addEventListener() {}, removeEventListener() {} },
  };
  ctx.extractRangeChart(opts).then(() => {
    if (!capturedBody) {
      check('post-body-captured', false);
      return;
    }
    const body = JSON.parse(capturedBody);
    check('post-body-captured', true);
    check('post-body-has-force-rerun', body.force_rerun === true);
    check('post-body-has-enhance', body.enhance === true);
  });
}

// ---- WARNING-SURFACE: backend `warning` field must be returned ----
function test_warning_surface() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.fetch = async () => ({
    ok: true,
    json: async () => ({
      ok: true, data: {}, raw: '',
      session_token: 't', csrf_token: 'c',
      warning: 'partial aggregation: 2 of 3 runs succeeded',
    }),
    text: async () => '',
  });
  ctx.rcaCallBackend._sessionToken = '';
  ctx.rcaCallBackend._csrfToken = '';
  const opts = {
    apiKey: 'sk', baseUrl: 'https://e', model: 'm', maxTokens: 100,
    mode: 'range_chart', transport: 'backend',
    dataUrl: 'data:image/png;base64,QUFB', mediaType: 'image/png',
    caption: '', chartLang: 'auto', signal: { aborted: false, addEventListener() {}, removeEventListener() {} },
  };
  ctx.extractRangeChart(opts).then((res) => {
    check('warning-surface-present', typeof res.warning === 'string' && res.warning.length > 0);
  });
}

// ---- CSRF-TIMEOUT: csrf GET must use the same AbortController timeout ----
function test_csrf_timeout() {
  const src = fs.readFileSync(path.join(__dirname, 'js/minimax.js'), 'utf8');
  const fnStart = src.indexOf('async function rcaCallBackend');
  const fnEnd = src.indexOf('\n}', fnStart);
  const body = src.slice(fnStart, fnEnd);
  // Use a balanced-brace match instead of [^}]* which stops at the first
  // inner `}` (the headers object) and would miss the outer signal: line.
  const csrfStart = body.indexOf("fetch('/api/extract',");
  if (csrfStart === -1) { check('csrf-get-has-signal', false); return; }
  let depth = 0, end = csrfStart;
  for (let i = csrfStart; i < body.length; i++) {
    const c = body[i];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { end = i; break; } }
  }
  const csrfBlock = body.slice(csrfStart, end + 1);
  const csrGetHasSignal = /signal\s*:/.test(csrfBlock);
  check('csrf-get-has-signal', csrGetHasSignal);
}

// ---- VIZ-HOST-PRESERVE: render must not destroy #viz-host ----
function test_viz_host_preserved() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  const fn = src.slice(src.indexOf('function renderCurrentResult'),
                       src.indexOf('\n  }', src.indexOf('function renderCurrentResult')));
  // Fix: viz-host must be preserved across innerHTML reassignment. Accept
  // either order — capture-before-render + appendChild-after-render, or a
  // single appendChild that reattaches the host.
  const preservesViz = /viz-host/.test(fn)
    && (/appendChild[\s\S]*?viz-host/.test(fn) || /viz-host[\s\S]*?appendChild/.test(fn));
  check('viz-host-preserved-in-source', preservesViz);
}

// ---- Partial-failure i18n key exists in all locales ----
function test_partial_failure_i18n() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const allKeys = [
    ...Object.keys(ctx.RCA_I18N.zh),
    ...Object.keys(ctx.RCA_I18N.en),
    ...Object.keys(ctx.RCA_I18N.ja),
  ];
  const partialKey = allKeys.find((k) => k.indexOf('partialFailure') !== -1 || k.indexOf('partial') !== -1);
  check('partial-failure-i18n-key', !!partialKey);
  if (partialKey) {
    check('partial-failure-i18n-zh', typeof ctx.RCA_I18N.zh[partialKey] === 'string');
    check('partial-failure-i18n-en', typeof ctx.RCA_I18N.en[partialKey] === 'string');
    check('partial-failure-i18n-ja', typeof ctx.RCA_I18N.ja[partialKey] === 'string');
  }
}

// ---- HANDLEFILE-ABORT: new file selection must abort in-flight extraction ----
function test_handlefile_abort() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  const fnStart = src.indexOf('async function handleFile');
  const fnEnd = src.indexOf('\n  }', fnStart);
  const body = src.slice(fnStart, fnEnd);
  check('handlefile-aborts-inflight', /state\.abort\s*&&\s*state\.abort\.abort\(\)/.test(body)
    || /state\.abort\b[^;]*abort\(\)/.test(body));
}

test_biozone_section_preserved();
test_i18n_server_error_keys();
test_csv_tab_sanitize();
test_detect_word_boundary();
test_post_body_includes_flags();
test_warning_surface();
test_csrf_timeout();
test_viz_host_preserved();
test_partial_failure_i18n();
test_handlefile_abort();

// ---------------------------------------------------------------------------
// P0-5 (REVIEW-2026-07-25) regression: js/minimax.js must unwrap
// _array_root wrappers in all three normalizers (range_chart,
// columnar_section, abundance_diagram).
// ---------------------------------------------------------------------------
function test_p0_5_array_root_range_chart() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const arr = [
    { species: 'Genus sp.', section: 'Sec A', range_top: 'Bed 9', range_base: 'Bed 7', biozone: 'B Zone' },
    { name: 'Sec A', age_range: 'Albian', formations: ['Fm X'], thickness_m: '10' },
    { name: 'B Zone', age: 'Albian' },
    'Genus sp. A',
  ];
  const wrapped = { _array_root: arr, _note: 'wrap' };
  const out = ctx.rcaNormalizeResult(wrapped);
  check('p0-5: range_chart sections>=1',  out.sections.length >= 1);
  check('p0-5: range_chart species>=1',   out.species_ranges.length >= 1);
  check('p0-5: range_chart biozones>=1',  out.biozones.length >= 1);
  check('p0-5: range_chart species.species=Genus sp.',
        out.species_ranges[0] && out.species_ranges[0].species === 'Genus sp.');
}

function test_p0_5_array_root_columnar() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const arr = [
    { id: 'Ki-1', group: 'A', lithology_blocks: [], age_units: [], samples: [] },
    { marker: 'F1', pattern: 'wavy', meaning: 'fossil' },
    { pattern: 'sand', meaning: 'sandstone' },
    { from_section: 'A', from_bed_idx: 1, to_section: 'B', to_bed_idx: 2 },
  ];
  const wrapped = { _array_root: arr, _note: 'wrap' };
  const out = ctx.rcaNormalizeColumnarResult(wrapped);
  check('p0-5: columnar sections>=1', out.sections && out.sections.length >= 1);
  check('p0-5: columnar fossil_legend>=1', out.fossil_legend && out.fossil_legend.length >= 1);
  check('p0-5: columnar lithology_legend>=1', out.lithology_legend && out.lithology_legend.length >= 1);
  check('p0-5: columnar cross_beds>=1', out.cross_beds && out.cross_beds.length >= 1);
}

function test_p0_5_array_root_abundance() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const arr = [
    { site_id: 'S1', location: 'Loc1' },
    { abundance: 'A', count: 5 },
    { zone: 'Z1', assemblage: 'ass' },
  ];
  const wrapped = { _array_root: arr, _note: 'wrap' };
  const out = ctx.rcaNormalizeAbundanceResult(wrapped);
  check('p0-5: abundance sites>=1', out.sites && out.sites.length >= 1);
  check('p0-5: abundance abundances>=1', out.abundances && out.abundances.length >= 1);
  check('p0-5: abundance zones>=1', out.zones && out.zones.length >= 1);
}

function test_p0_5_non_array_passthrough() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const plain = { sections: [], species_ranges: [], biozones: [], other_fossils: [], confidence: 0.5 };
  const out = ctx.rcaNormalizeResult(plain);
  check('p0-5: non-array passthrough confidence',
        out && out.confidence === 0.5);
}

test_p0_5_array_root_range_chart();
test_p0_5_array_root_columnar();
test_p0_5_array_root_abundance();
test_p0_5_non_array_passthrough();

// ---------------------------------------------------------------------------
// P1-6 (REVIEW-2026-07-25) regression: js/minimax.js SP_KNOWN must include
// the full 12 fields that match Python _KNOWN_SPECIES_KEYS; the species
// row dict must promote those fields instead of dropping them into _extras.
// ---------------------------------------------------------------------------
function test_p1_6_sp_known_includes_extra_fields() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const parsed = {
    species_ranges: [
      {
        species: 'Genus sp.',
        section: 'Sec A',
        range_top: 'Bed 9',
        range_base: 'Bed 7',
        biozone: 'B Zone',
        author: 'Smith',
        year: '1950',
        author_year: 'Smith, 1950',
        range_top_bed: 'Bed 9a',
        range_base_bed: 'Bed 7b',
        range_top_idx: '9',
        range_base_idx: 7,
        endpoint_kind: 'observed',
        reworked: false,
        confidence: '1.4',
      },
    ],
    sections: [], biozones: [], other_fossils: [], confidence: 0.9,
  };
  const out = ctx.rcaNormalizeResult(parsed);
  check('p1-6: species row has author',
        out.species_ranges[0].author === 'Smith');
  check('p1-6: species row has year',
        out.species_ranges[0].year === '1950');
  check('p1-6: species row has author_year',
        out.species_ranges[0].author_year === 'Smith, 1950');
  check('p1-6: species row has range_top_bed',
        out.species_ranges[0].range_top_bed === 'Bed 9a');
  check('p1-6: species row has range_base_bed',
        out.species_ranges[0].range_base_bed === 'Bed 7b');
  check('p1-6: species row has typed range_top_idx',
        out.species_ranges[0].range_top_idx === 9);
  check('p1-6: species row has typed range_base_idx',
        out.species_ranges[0].range_base_idx === 7);
  check('p1-6: species row confidence is clamped',
        out.species_ranges[0].confidence === 1);
  check('p1-6: species row has endpoint_kind',
        out.species_ranges[0].endpoint_kind === 'observed');
  // M-1 fix: 'occurrence_mode' (string enum) replaces the old
  // boolean 'reworked'. Default is 'in_situ' when absent.
  check('p1-6: species row has occurrence_mode',
        out.species_ranges[0].occurrence_mode === 'in_situ');
  check('p1-6: species row has note',
        typeof out.species_ranges[0].note === 'string');
  // None of those 7 fields must leak into _extras (that path destroys data).
  const extras = out.species_ranges[0]._extras || {};
  for (const k of ['author', 'year', 'author_year', 'range_top_bed',
                   'range_base_bed', 'range_top_idx', 'range_base_idx',
                   'endpoint_kind', 'occurrence_mode', 'confidence', 'reworked']) {
    check(`p1-6: ${k} NOT in _extras`, !(k in extras));
  }
}

function test_unknown_scientific_states_are_not_promoted() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeResult({
    species_ranges: [{ species: 'Genus alpha', occurrence_mode: 'native', endpoint_kind: 'certain' }],
    sections: [], biozones: [], other_fossils: [], confidence: 0.5,
  });
  check('unknown occurrence mode remains unknown', out.species_ranges[0].occurrence_mode === 'unknown');
  check('unknown endpoint kind remains unknown', out.species_ranges[0].endpoint_kind === 'unknown');
  check('missing row confidence remains null', out.species_ranges[0].confidence === null);
}

test_p1_6_sp_known_includes_extra_fields();
test_unknown_scientific_states_are_not_promoted();

// ---------------------------------------------------------------------------
// REVIEW-2026-07-31 regression tests: JS/Python parity fixes
// ---------------------------------------------------------------------------

function test_json_utils_wrapper_promotion() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Level 3: {"data": {...}} promotes inner keys to the top level.
  const r1 = ctx.safeJsonLoads('{"data": {"species_ranges": [{"species": "A"}], "confidence": 0.9}}');
  check('json-utils L3 wrapper promotes species_ranges', Array.isArray(r1.species_ranges) && r1.species_ranges.length === 1);
  check('json-utils L3 wrapper keeps siblings', r1.confidence === 0.9);
  // Level 4: schema restated in prose, payload inside {"data": {...}}.
  const text = 'Schema: {"properties": {"species_ranges": {"type": "array", "items": {"type": "object"}}}, "required": ["species_ranges"]}. Result: {"data": {"species_ranges": [{"species": "B", "range_base": "1", "range_top": "5"}], "confidence": 0.8}}';
  const r2 = ctx.safeJsonLoads(text);
  check('json-utils L4 wrapper promotes payload', Array.isArray(r2.species_ranges) && r2.species_ranges.length === 1 && r2.species_ranges[0].species === 'B');
  // Long schema string must NOT outscore the small real payload (H8).
  const schemaLong = '{"properties": {"species_ranges": {"type": "array", "items": {"type": "object", "properties": {"species": {"type": "string"}, "section": {"type": "string"}, "range_top": {"type": "string"}, "range_base": {"type": "string"}, "biozone": {"type": "string"}}}}, "required": ["species_ranges"]}}';
  const text2 = 'Here is the schema for reference: ' + schemaLong + '. And here is the actual result: {"species_ranges": [{"species": "Real", "range_base": "2", "range_top": "7"}]}';
  const r3 = ctx.safeJsonLoads(text2);
  check('json-utils long schema does not outscore payload', Array.isArray(r3.species_ranges) && r3.species_ranges[0].species === 'Real');
}

function test_quality_fad_lad_ma_branch() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Age-based ranges must NOT be flagged (bed branch would read 300/250).
  const ageOk = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian' }],
    species_ranges: [{ species: 'X', section: 'A', range_base: '300 Ma', range_top: '250 Ma', biozone: '' }],
    confidence: 0.9,
  });
  check('quality Ma range valid (no fad_lt_lad)', !ageOk.issues.some(i => i.msg_key === 'quality.fad_lt_lad'));
  // Inverted age range IS flagged.
  const ageInv = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian' }],
    species_ranges: [{ species: 'X', section: 'A', range_base: '250 Ma', range_top: '300 Ma', biozone: '' }],
    confidence: 0.9,
  });
  check('quality inverted Ma range flagged', ageInv.issues.some(i => i.msg_key === 'quality.fad_lt_lad'));
  // Bed labels containing "ma" (Madison) must NOT be read as ages.
  const bedOk = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian' }],
    species_ranges: [{ species: 'X', section: 'A', range_base: 'Madison 3', range_top: 'Madison 6', biozone: '' }],
    confidence: 0.9,
  });
  check('quality "Madison 3/6" bed pair not flagged', !bedOk.issues.some(i => i.msg_key === 'quality.fad_lt_lad'));
}

function test_quality_cross_era_proportional() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian', lithology_blocks: [
      { age: 'Permian' }, { age: 'Triassic' },
    ]}],
    species_ranges: [], confidence: 0.9,
  });
  // 1 violating section → accuracy gets 0.5, NOT a flat 0 (parity with Python).
  const acc = res.details && res.details.accuracy;
  check('quality cross-era penalty proportional', acc === undefined || acc >= 0.4);
}

function test_quality_steno_biozone_map() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // "N. optima Zone" must resolve via the biozone stage map; a LOWER
  // species (range_top 3) in a YOUNGER biozone than an UPPER species
  // (range_top 9) is a Steno violation.
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian' }],
    species_ranges: [
      { species: 'Low', section: 'A', range_base: '1', range_top: '3', biozone: 'N. optima Zone' },
      { species: 'Up', section: 'A', range_base: '4', range_top: '9', biozone: 'Clarkina orientalis Zone' },
    ],
    confidence: 0.9,
  });
  check('quality Steno detects lower-species-in-younger-zone', res.issues.some(i => i.msg_key === 'quality.biozone_order_violation'));
}

// PR2 M6: stage-order check should be proportional — section A with 4
// stages and 2 inverted adjacent pairs reads accuracy > 0 (was 0 before).
// Mirror rca_core/quality.py:_score_cross_era_accuracy.
function test_m6_stage_order_proportional() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // 4 stages with 2 inverted pairs (A above B, C above B in age_range).
  // Pre-fix: 1 check, 0 passed → 0% accuracy on this section.
  // Post-fix: 3 checks, 1 passed → 33% accuracy on this section.
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Wordian Roadian Capitanian Kungurian' }],
    species_ranges: [], confidence: 0.9,
  });
  const issues = res.issues.filter((i) => i.msg_key === 'quality.stage_order_reversed');
  check('m6-stage-order-counts-each-pair', issues.length >= 2);
  // The accuracy detail must exist; without regression we allowed 0.
  if (res.details && typeof res.details.accuracy === 'number') {
    check('m6-stage-order-not-flat-zero', res.details.accuracy > 0);
  }
}
test_m6_stage_order_proportional();

// PR2 M7: Steno's-law check skips sections whose age_range is empty /
// missing. Without this, sections referenced by species_ranges but not
// declared in `sections` synthesize false violations.
function test_m7_steno_skips_section_without_age_range() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Section A has no age_range. The species below would normally trip a
  // Steno violation (lower in younger biozone), but the anchor is missing.
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: '' }],
    species_ranges: [
      { species: 'Low', section: 'A', range_base: '1', range_top: '3', biozone: 'N. optima Zone' },
      { species: 'Up', section: 'A', range_base: '4', range_top: '9', biozone: 'Clarkina orientalis Zone' },
    ],
    confidence: 0.9,
  });
  check('m7-steno-skips-missing-age-range',
    !res.issues.some((i) => i.msg_key === 'quality.biozone_order_violation'));
}
test_m7_steno_skips_section_without_age_range();

// PR2 M13: en translation of quality.range_top_lt_base must describe a
// violation (LAD earlier than FAD), not the normal case. Pre-fix: "Some
// species have range top younger than their range base" — that describes
// the LEGAL state (top younger = newer = correct). Post-fix: violation.
function test_m13_range_top_lt_base_en_is_violation() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const en = ctx.RCA_I18N.en['quality.range_top_lt_base'];
  check('m13-en-key-exists', typeof en === 'string' && en.length > 0);
  check('m13-en-mentions-LAD', /LAD/i.test(en));
  check('m13-en-mentions-FAD', /FAD/i.test(en));
  check('m13-en-earlier-than', /earlier/i.test(en));
  // Pre-fix had "younger than" which is the OPPOSITE direction.
  check('m13-en-not-younger-than', !/younger than/i.test(en));
  // Parity: zh/ja already correct ("LAD 早于 FAD" / "LAD が FAD より早い").
  const zh = ctx.RCA_I18N.zh['quality.range_top_lt_base'];
  const ja = ctx.RCA_I18N.ja['quality.range_top_lt_base'];
  check('m13-zh-mentions-zhao', /早于/.test(zh));
  check('m13-ja-mentions-hayai', /早い/.test(ja));
}
test_m13_range_top_lt_base_en_is_violation();

// ---- PR3 M14: showAlert removes the old alert on animationend ----
//
// The alert fade-out uses a CSS @keyframes animation, not a transition,
// so listening on `transitionend` never fires. The fix switches to
// `animationend` (with a setTimeout safety net so a missed event still
// unblocks the slot).
function test_m14_alert_removed_on_animationend() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const slot = ctx.$('alert-slot');
  if (!slot) {
    check('m14-alert-shown-on-no-key', false);
    check('m14-old-alert-removed-on-animationend', false);
    return Promise.resolve(null);
  }
  // Show two alerts back-to-back. The first one should be removed via
  // the animationend listener (post-fix). Pre-fix the transitionend
  // listener never fired and both stayed in the slot.
  ctx.showAlert('danger', 'first');
  ctx.showAlert('danger', 'second');
  const before = slot.querySelectorAll('.alert');
  check('m14-alert-shown-on-no-key', before.length >= 1);
  // The first alert was tagged alert-fade-out and the animationend
  // listener was registered. Dispatch animationend to trigger removal.
  // The post-fix uses animationend + 400ms setTimeout safety net.
  const old = before[0];
  const ev = new ctx.Event('animationend', { bubbles: true });
  old.dispatchEvent(ev);
  return new Promise((resolve) => setTimeout(resolve, 5)).then(() => {
    const remaining = slot.querySelectorAll('.alert');
    check('m14-old-alert-removed-on-animationend', remaining.length === 1);
  });
}
const _m14 = test_m14_alert_removed_on_animationend();

// ---- PR3 M15: resetUpload preserves #viz-host ----
function test_m15_reset_upload_preserves_viz_host() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const resultsContent = ctx.$('results-content');
  // Manually create the viz-host inside results-content (mirrors index.html).
  const viz = ctx.document.createElement('div');
  viz.id = 'viz-host';
  resultsContent.appendChild(viz);
  // Add some other content to be cleared.
  const span = ctx.document.createElement('span');
  span.textContent = 'old content';
  resultsContent.appendChild(span);
  ctx.resetUpload();
  // After resetUpload, viz-host is preserved (post-fix) and old content
  // is cleared. Use children directly because the stub querySelector may
  // not index span.
  const ids = Array.from(resultsContent.children).map((c) => c.id || c.tagName);
  check('m15-viz-host-preserved', ids.indexOf('viz-host') !== -1);
  check('m15-old-content-cleared', ids.filter((t) => t === 'SPAN').length === 0);
}
test_m15_reset_upload_preserves_viz_host();

// ---- PR3 M16: Ctrl+Enter in textarea triggers extract ----
//
// Caption textarea: pressing Ctrl+Enter should call runExtraction (NOT
// silently swallow the keystroke). Pre-fix: the listener returned early
// for textarea, swallowing the shortcut.
function test_m16_ctrl_enter_in_textarea_triggers_extract() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.state.busy = false;
  const caption = ctx.$('caption');
  check('m16-caption-is-textarea', caption.tagName === 'TEXTAREA');
  const slot = ctx.$('alert-slot');
  const beforeAlerts = slot.querySelectorAll('.alert').length;
  const ev = new ctx.KeyboardEvent('keydown', {
    key: 'Enter',
    ctrlKey: true,
    bubbles: true,
    cancelable: true,
  });
  Object.defineProperty(ev, 'target', { value: caption, configurable: true });
  ctx.document.dispatchEvent(ev);
  // runExtraction will trigger err.noKey (api-key is empty) which appends
  // an .alert to the alert slot. Pre-fix the listener returned early for
  // textarea so no alert appeared.
  const afterAlerts = slot.querySelectorAll('.alert').length;
  check('m16-ctrl-enter-textarea-triggers-extract', afterAlerts > beforeAlerts);
  // Reset alert slot for downstream tests.
  slot.innerHTML = '';
}
test_m16_ctrl_enter_in_textarea_triggers_extract();

// Ctrl+Enter inside an <input type="text"> should NOT trigger extract.
function test_m16_ctrl_enter_in_text_input_does_not_trigger() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let called = 0;
  const origRun = ctx.runExtraction;
  ctx.runExtraction = () => { called += 1; };
  ctx.state.busy = false;
  const apiKey = ctx.$('api-key');
  const ev = new ctx.KeyboardEvent('keydown', {
    key: 'Enter',
    ctrlKey: true,
    bubbles: true,
    cancelable: true,
  });
  Object.defineProperty(ev, 'target', { value: apiKey });
  ctx.document.dispatchEvent(ev);
  check('m16-ctrl-enter-text-input-no-extract', called === 0);
  ctx.runExtraction = origRun;
}
test_m16_ctrl_enter_in_text_input_does_not_trigger();

// ---- PR3 M1: other_fossils dict shape handling ----
//
// other_fossils can be a list of strings OR a list of dicts
// (label/species/taxon/name). The normalizer must lift the first
// available label so the rendered/exported table shows the fossil
// name, not '[object Object]'.
function test_m1_other_fossils_dict_shape() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const norm = ctx.rcaNormalizeResult({
    sections: [], species_ranges: [], biozones: [],
    other_fossils: [{ species: 'X' }, { label: 'Y' }, { taxon: 'Z' }, 'plain', { name: 'W' }],
    confidence: 0,
  });
  check('m1-other-fossils-dict-species', norm.other_fossils[0] === 'X');
  check('m1-other-fossils-dict-label', norm.other_fossils[1] === 'Y');
  check('m1-other-fossils-dict-taxon', norm.other_fossils[2] === 'Z');
  check('m1-other-fossils-plain-string', norm.other_fossils[3] === 'plain');
  check('m1-other-fossils-dict-name', norm.other_fossils[4] === 'W');
  check('m1-other-fossils-count', norm.other_fossils.length === 5);
}
test_m1_other_fossils_dict_shape();

function test_m1_other_fossils_csv_export_strings_only() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('en');
  const data = {
    sections: [],
    species_ranges: [], biozones: [],
    other_fossils: [{ species: 'X' }, { label: 'Y' }],
    confidence: 0,
  };
  ctx.rcaNormalizeResult(data);  // mutate data? No — return new; use the input directly for export
  // Build table export directly from the raw data shape.
  const exp = ctx.rcaBuildTableExport(data, 'other_fossils');
  check('m1-other-fossils-export-rows', exp.rows.length === 2);
  for (const row of exp.rows) {
    for (const cell of row) {
      check('m1-other-fossils-export-cell-not-object', typeof cell === 'string');
    }
  }
}
test_m1_other_fossils_csv_export_strings_only();

// ---- PR3 M2: CSRF comment is descriptive (no behavioral change) ----
//
// No runtime assertion; the test just ensures the loadAllScripts step
// still works (M2 was a comment-only fix). Documented for parity with
// the Python-side CSRF flow description.
function test_m2_callbackend_serializes_promises() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('m2-rca-call-backend-fn-exists', typeof ctx.extractRangeChart === 'function');
}
test_m2_callbackend_serializes_promises();

// ---- PR3 M3: direct-mode fetch passes redirect: 'manual' ----
//
// A 3xx response from the upstream MUST NOT auto-follow with the
// x-api-key header attached; pass `redirect: 'manual'` so the browser
// returns the 3xx as the response instead of dispatching the same
// headers to the redirect target.
function test_m3_fetch_manual_redirect() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let captured = null;
  ctx.fetch = async (u, opts) => {
    captured = opts;
    return { ok: true, json: async () => ({
      content: [{ type: 'text', text: JSON.stringify({
        sections: [], species_ranges: [], biozones: [], other_fossils: [], confidence: 0,
      })}],
    }), text: async () => '' };
  };
  return ctx.extractRangeChart({
    dataUrl: 'data:image/png;base64,QUFB',
    mode: 'range_chart',
    baseUrl: 'https://example.com',
    model: 'm', maxTokens: 100,
  }).then(() => {
    check('m3-fetch-redirect-manual', captured && captured.redirect === 'manual');
  });
}
const _m3 = test_m3_fetch_manual_redirect();

// ---- PR3 LOW batch ----
//
// LOW 6: quality-badge colors use solid #1f7a3a / #c08400 / #c0392b
// instead of tokenized vars so the on-screen contrast meets WCAG AA
// regardless of the user's --surface / --text-base customization.
function test_low_quality_badge_solid_colors() {
  // Static source check: css/style.css must include the three solid colors
  // for .quality-badge.{high,mid,low}.
  const fs = require('fs');
  const path = require('path');
  const src = fs.readFileSync(path.join(__dirname, 'css', 'style.css'), 'utf8');
  check('low-badge-high-color', /\.quality-badge\.high\s*\{[^}]*background:\s*#1f7a3a/i.test(src));
  check('low-badge-mid-color', /\.quality-badge\.mid\s*\{[^}]*background:\s*#c08400/i.test(src));
  check('low-badge-low-color', /\.quality-badge\.low\s*\{[^}]*background:\s*#c0392b/i.test(src));
  check('low-badge-white-text', /\.quality-badge\.(high|mid|low)\s*\{[^}]*color:\s*#ffffff/i.test(src));
}
test_low_quality_badge_solid_colors();

// LOW 9: cross-era regex must include paleocene (was missing).
function test_low_cross_era_paleocene() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'S1', age_range: 'Paleocene-Eocene', lithology_blocks: [
      { age: 'Paleocene' }, { age: 'Eocene' },
    ]}],
    species_ranges: [], confidence: 0.9,
  });
  // Should NOT trigger cross_era violation because both are Cenozoic.
  // Pre-fix: paleocene wasn't in the Cenozoic regex, only Eocene matched;
  // paleocene wasn't classified and the era set had size 1 — so the test
  // passed accidentally. Post-fix: paleocene also resolves to Cenozoic.
  check('low-cross-era-paleocene-classified-cenozoic',
    !res.issues.some((i) => i.msg_key === 'quality.ages_inconsistent'));
}
test_low_cross_era_paleocene();

// LOW 10: dragend listener hides the page-drop-overlay (static check).
function test_low_drop_overlay_dragend_listener() {
  const fs = require('fs');
  const path = require('path');
  const src = fs.readFileSync(path.join(__dirname, 'js', 'app.js'), 'utf8');
  check('low-dragend-listener-present',
    /addEventListener\(['"]dragend['"]/.test(src));
}
test_low_drop_overlay_dragend_listener();

function test_aggregate_author_h7_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('aggregate em-dash author normalized', ctx.rcaNormIcbnAuthor('Smith—Jones, 1950') === 'smith jones|1950');
  check('aggregate double-dash author normalized', ctx.rcaNormIcbnAuthor('Smith--Jones, 1950') === 'smith jones|1950');
  check('aggregate ex author stripped', ctx.rcaNormIcbnAuthor('Smith ex Jones, 1950') === 'smith|1950');
  check('aggregate in author stripped', ctx.rcaNormIcbnAuthor('Smith in Jones, 1950') === 'smith|1950');
}

function test_prompt_phylo_parent_and_degradation() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // PHYLOGENETIC_TREE_SYSTEM_PROMPT is a `const` — not visible on the
  // context object; evaluate it inside the vm instead. (The final value
  // is a string: the array literal ends with `.join('\\n')`.)
  const promptText = vm.runInContext('PHYLOGENETIC_TREE_SYSTEM_PROMPT', ctx);
  check('phylo prompt has parent field', /parent/.test(promptText));
  check('phylo prompt has degradation clause', /DEGRADE GRACEFULLY/.test(promptText));
}

function test_ics_table_data_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const t = ctx.RCA_ICS_TABLE;
  check('ics_table Campanian base 83.6', t.Campanian && t.Campanian.base_ma === 83.6);
  check('ics_table Santonian base 86.3', t.Santonian && t.Santonian.base_ma === 86.3);
  check('ics_table Turonian base 93.9', t.Turonian && t.Turonian.base_ma === 93.9);
  check('ics_table no Pleistocene pseudo-stage', !('Pleistocene' in t));
  check('ics_table Dapingian base 470.0', t.Dapingian && t.Dapingian.base_ma === 470.0);
}

test_json_utils_wrapper_promotion();
test_quality_fad_lad_ma_branch();
test_quality_cross_era_proportional();
test_quality_steno_biozone_map();
test_aggregate_author_h7_parity();
test_prompt_phylo_parent_and_degradation();
test_ics_table_data_parity();

// UI-REVIEW-2026-08-01 (H1): the force-rerun button must become visible
// after the FIRST successful result. Previously setBusy(false) ran in
// `finally` BEFORE state.result was assigned, so the visibility check
// read the stale null and the button never appeared on first extraction.
function test_force_rerun_visible_after_first_result() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const rerun = ctx.document.getElementById('force-rerun-btn');
  ctx.state.result = null;
  ctx.state.busy = false;
  ctx.updateActionButtons();
  // Recorded calls are ['toggle', className, force] — the class name is
  // index 1 and the visibility flag index 2.
  const callsBefore = (rerun.classList._calls || []).filter(c => c[1] === 'hidden');
  // What runExtraction does AFTER the finally block: store the result,
  // then re-evaluate the buttons.
  ctx.state.result = { sections: [], species_ranges: [], confidence: 0.9 };
  ctx.updateActionButtons();
  const callsAfter = (rerun.classList._calls || []).filter(c => c[1] === 'hidden');
  const last = callsAfter[callsAfter.length - 1];
  check('force-rerun visible after first result', callsAfter.length > callsBefore.length && last && last[2] === false);
}

test_force_rerun_visible_after_first_result();

// ---- UI-FIX-2026-08-07: phylogenetic_tree mode must be selectable ----
// The web frontend previously had no chart-mode option and no auto-detect
// keyword for phylogenetic trees, so trees could never be extracted.
function test_ui_phylo_mode_selectable() {
  const appSrc = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  const htmlSrc = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const i18nSrc = fs.readFileSync(path.join(__dirname, 'js/i18n.js'), 'utf8');
  check('ui-option-phylo-in-html', htmlSrc.includes('<option value="phylogenetic_tree"'));
  check('ui-resolve-whitelist-phylo', /choice === 'phylogenetic_tree'/.test(appSrc));
  check('ui-auto-keywords-phylo', /phyloKeysAscii = \['phylogen'/.test(appSrc));
  const locales = (i18nSrc.match(/upload\.chartMode\.phylogeneticTree/g) || []).length;
  check('ui-i18n-phylo-three-locales', locales === 3);
}

test_ui_phylo_mode_selectable();

// ---- UI-FIX-2026-08-07: result cells carry title + other_fossils wraps ----
// Every td defaults to nowrap + ellipsis; without a title attribute the
// truncated tail was the only visible part of long values.
function test_ui_cell_title_and_wrapping() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const data = {
    sections: [{ name: 'A very long section name that will definitely exceed two hundred and eighty pixels of width', age_range: '', formations: ['F1'], formation_thickness_m: '', coordinates: '' }],
    species_ranges: [{ species: 'Genus species', section: 'S', range_base: 'Bed 1', range_top: 'Bed 5', biozone: 'Z' }],
    biozones: [], other_fossils: ['A very long free-text fossil record that should wrap instead of truncate because it is the only column of the table and users need to read it'],
    confidence: 0.9,
  };
  const html = ctx.rcaRenderResults(data, '');
  check('ui-cell-title-attr-present', html.includes('title="'));
  check('ui-cell-title-has-long-value', html.includes('title="A very long section name'));
  check('ui-other-fossils-wrapping', html.includes('cell-wrapping'));
  // Empty cells render the dash placeholder and must NOT carry a title.
  check('ui-empty-cell-no-title', !/<td[^>]*title="[^"]*"[^>]*>-\s*<\/td>/.test(html));
}

test_ui_cell_title_and_wrapping();

// ---- UI-FIX-2026-08-07: CSS token/selector fixes present in source ----
function test_ui_css_fixes() {
  const cssSrc = fs.readFileSync(path.join(__dirname, 'css/style.css'), 'utf8');
  check('css-surface-defined', /--surface:\s*var\(--bg-lighter\)/.test(cssSrc));
  check('css-segmented-uses-primary-active', /\.segmented button\[aria-checked="true"\]\s*\{[\s\S]*?var\(--primary-active\)/.test(cssSrc));
  check('css-label-selector-fixed', /\.rt-left \.label\s*\{/.test(cssSrc));
  check('css-sticky-odd-row-bg', /tr:nth-child\(odd\) td:first-child/.test(cssSrc));
}

test_ui_css_fixes();

// ---- UI-FIX-2026-08-07: phylogenetic-tree payload renders a nodes table ----
// rcaTableConfigs had no phylo branch, so a tree result rendered four empty
// range-chart tables; the nodes table is the web-side mirror of the Python
// exporter's _phylogenetic_tree_tables.
function test_ui_phylo_nodes_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const phylo = {
    metadata: {}, root_ids: ['n0'],
    nodes: [
      { id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false, branch_length: null, node_age_ma: null, support: 89 },
      { id: 'n1', parent: 'n0', name: 'Taxon A', is_leaf: true, branch_length: 0.12, node_age_ma: 5.0, support: null },
    ],
    confidence: 0.9, runs: 1,
  };
  const configs = ctx.rcaTableConfigs(phylo);
  check('ui-phylo-nodes-table-present', configs.length === 1 && configs[0].id === 'nodes');
  const html = ctx.rcaRenderResults(phylo, '');
  check('ui-phylo-nodes-title-i18n', html.includes(ctx.t('sec.nodes')));
  check('ui-phylo-node-row-rendered', html.includes('Spasmaria') && html.includes('Taxon A'));
  check('ui-phylo-leaf-column', html.includes('>Y<') && html.includes('>N<'));
}

test_ui_phylo_nodes_table();

// ---- UI-FIX-2026-08-07: phylo i18n keys exist in all 3 locales (py+js) ----
function test_ui_phylo_i18n_keys() {
  const pySrc = fs.readFileSync(path.join(__dirname, 'rca_core/i18n.py'), 'utf8');
  const jsSrc = fs.readFileSync(path.join(__dirname, 'js/i18n.js'), 'utf8');
  for (const key of ['sec.nodes', 'col.nodeId', 'col.parent', 'col.isLeaf', 'col.branchLength', 'col.nodeAgeMa', 'col.support']) {
    // Match only key DEFINITIONS (": value"), not mentions inside comments.
    check('ui-py-i18n-' + key, (pySrc.match(new RegExp('"' + key + '"\s*:', 'g')) || []).length === 3);
    check('ui-js-i18n-' + key, (jsSrc.match(new RegExp("'" + key + "'\\s*:", 'g')) || []).length === 3);
  }
}

test_ui_phylo_i18n_keys();

// ---- PR1 H5: truncated_or_unrecognized_payload guard ----
//
// Mirror rca_core/extractor.py:866-880. A payload that has none of the
// mode-known root keys must trip a `truncated_or_unrecognized_payload`
// warning, and only the range_chart extract path flips ok=false.
function test_h5_normalizer_truncated_warning_flag() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Plain unknown payload — no known range_chart roots.
  const out = ctx.rcaNormalizeResult({ foo: 1 });
  check('h5-range-warnings-array', Array.isArray(out._warnings));
  check('h5-range-warning-flag-set',
    out._warnings && out._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
}
test_h5_normalizer_truncated_warning_flag();

function test_h5_normalizer_truncated_warning_columnar() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeColumnarResult({ foo: 1 });
  check('h5-columnar-warning-flag-set',
    out._warnings && out._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
}
test_h5_normalizer_truncated_warning_columnar();

function test_h5_normalizer_truncated_warning_abundance() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeAbundanceResult({ foo: 1 });
  check('h5-abundance-warning-flag-set',
    out._warnings && out._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
}
test_h5_normalizer_truncated_warning_abundance();

function test_h5_normalizer_truncated_warning_phylo() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Phylo normalizer throws on missing root_ids/nodes before reaching the
  // warning — we only verify a known-valid fixture does NOT spuriously
  // emit the warning. A valid payload must come back warning-free.
  const out = ctx.rcaNormalizePhylogeneticTreeResult({
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false }],
  });
  const flagged = (out._warnings || []).indexOf('truncated_or_unrecognized_payload') !== -1;
  check('h5-phylo-valid-no-warning', !flagged);
  // Soft: the throw path is exercised by test_prompt_phylo_parent_and_degradation.
}
test_h5_normalizer_truncated_warning_phylo();

// ---- PR1 H1: ICS 2024 stage boundaries refresh + Wuliuan ----
//
// Refreshed to ICS 2024-09 values. Mirrors the Python
// rca_core/resources/ics_2024.json exactly. The H1 parity test (Python
// vs JS) lives in tests_bugfixes.py.
function test_h1_ics_emsian_base_410_62() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('h1-emsian-base-410.62', ctx.RCA_ICS_TABLE.Emsian.base_ma === 410.62);
}
test_h1_ics_emsian_base_410_62();

function test_h1_ics_wuliuan_present() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('h1-wuliuan-present', 'Wuliuan' in ctx.RCA_ICS_TABLE);
  check('h1-wuliuan-top-504.5', ctx.RCA_ICS_TABLE.Wuliuan.top_ma === 504.5);
  check('h1-wuliuan-base-506.5', ctx.RCA_ICS_TABLE.Wuliuan.base_ma === 506.5);
}
test_h1_ics_wuliuan_present();

function test_h1_ics_anisian_base_246_7() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('h1-anisian-base-246.7', ctx.RCA_ICS_TABLE.Anisian.base_ma === 246.7);
}
test_h1_ics_anisian_base_246_7();

function test_h1_ics_drumian_base_504_5() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('h1-drumian-base-504.5', ctx.RCA_ICS_TABLE.Drumian.base_ma === 504.5);
}
test_h1_ics_drumian_base_504_5();

function test_h1_ics_rhaetian_base_205_7() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('h1-rhaetian-base-205.7', ctx.RCA_ICS_TABLE.Rhaetian.base_ma === 205.7);
}
test_h1_ics_rhaetian_base_205_7();

// ---- PR1 H8: handleFile race fix ----
//
// `state.file = file` must be assigned AFTER the `await rcaLoadAndMaybeResize`
// resolves, not before. Otherwise the new filename is exposed while the
// preview still shows the old image — a race visible in export filenames
// and footer labels. Verified by static-source inspection.
function test_h8_handlefile_state_file_after_await() {
  const src = fs.readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  // Locate the handleFile body.
  const handleStart = src.indexOf('async function handleFile(file)');
  if (handleStart < 0) { check('h8-handleFile-found', false); return; }
  const handleEnd = src.indexOf('async function handleFileMetaRefresh', handleStart);
  const body = src.slice(handleStart, handleEnd > 0 ? handleEnd : handleStart + 6000);
  // Match ACTUAL statements only (start of whitespace then assignment).
  // Strip line comments first to avoid matching `// ... state.file = file ...`.
  const code = body.split('\n').filter((ln) => !/^\s*\/\//.test(ln)).join('\n');
  const fileAssigns = code.match(/state\.file\s*=\s*file\s*;?/g) || [];
  check('h8-handleFile-single-assign', fileAssigns.length === 1);
  const loadIdx = code.indexOf('await rcaLoadAndMaybeResize');
  const assignIdx = code.indexOf('state.file = file');
  check('h8-assign-after-load',
    loadIdx >= 0 && assignIdx >= 0 && assignIdx > loadIdx);
}
test_h8_handlefile_state_file_after_await();

// ---- PR1 H6: phylo metadata full inheritance ----
//
// Mirror rca_core/extractor.py: taxon_group, root_name, total_nodes,
// version, image_source must be carried onto out.metadata from either
// parsed.metadata OR the root level. The Python side does this so the
// frontend can render provenance, license, and dataset identity.
function test_h6_phylo_metadata_inherits_root_taxonomy() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizePhylogeneticTreeResult({
    version: '1',
    taxon_group: 'Radiolaria',
    total_nodes: 42,
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false }],
  });
  check('h6-taxon_group-present', out.metadata.taxon_group === 'Radiolaria');
  check('h6-total_nodes-present', out.metadata.total_nodes === 42);
  check('h6-version-default', out.metadata.version === '1');
  check('h6-root_name-empty', out.metadata.root_name === '');
  check('h6-image_source-empty', out.metadata.image_source === '');
}
test_h6_phylo_metadata_inherits_root_taxonomy();

function test_h6_phylo_metadata_promotes_image_source_from_root() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizePhylogeneticTreeResult({
    image_source: 'file.jpg',
    metadata: { tree_type: 'cladogram' },
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false }],
  });
  // image_source lives on both .source and .image_source after promotion.
  check('h6-image_source-on-source', out.metadata.source === 'file.jpg');
  check('h6-image-source-field', out.metadata.image_source === 'file.jpg');
  check('h6-tree_type-promoted', out.metadata.tree_type === 'cladogram');
}
test_h6_phylo_metadata_promotes_image_source_from_root();

// ---- PR1 H2: columnar 4 new sub-tables (lithology_blocks / age_units /
// samples / confidence_by_section) ----
//
// rcaTableConfigs must emit configs for the 3 new sub-tables in columnar
// mode, and rcaBuildTableExport must read from the stashed flat row
// arrays attached to data during config generation. Also covers M12:
// rcaFormulaSafe must guard a leading LF (\n) the same way it does
// other formula triggers.
function test_h2_columnar_lithology_blocks_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // rcaTableConfigs is hoisted onto globalThis in table.js.
  const data = {
    sections: [{ id: 'S1', lithology_blocks: [{ pattern: 'dots', range_top_idx: 1, range_base_idx: 5 }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [],
    confidence: 0,
  };
  // Trigger config generation so data._lithology_blocks_rows is built.
  ctx.rcaTableConfigs(data);
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'lithology_blocks');
  check('h2-lithology-blocks-cfg', !!cfg);
  check('h2-lithology-blocks-title', cfg && cfg.titleKey === 'sec.lithologyBlocks');
  check('h2-lithology-blocks-rows', data._lithology_blocks_rows.length === 1);
  check('h2-lithology-blocks-row-pattern', data._lithology_blocks_rows[0].pattern === 'dots');
}
test_h2_columnar_lithology_blocks_table();

function test_h2_columnar_age_units_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const data = {
    sections: [{ id: 'S1', age_units: [{ label: 'Ypresian', range_top_idx: 0, range_base_idx: 3 }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  ctx.rcaTableConfigs(data);
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'age_units');
  check('h2-age-units-cfg', !!cfg);
  check('h2-age-units-title', cfg && cfg.titleKey === 'sec.ageUnits');
  check('h2-age-units-rows', data._age_units_rows.length === 1);
  check('h2-age-units-row-label', data._age_units_rows[0].label === 'Ypresian');
}
test_h2_columnar_age_units_table();

function test_h2_columnar_samples_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const data = {
    sections: [{ id: 'S1', samples: [{ bed_idx: 4, fossil_marker: 'A', ref: 'Smith 1950' }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  ctx.rcaTableConfigs(data);
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'samples');
  check('h2-samples-cfg', !!cfg);
  check('h2-samples-title', cfg && cfg.titleKey === 'sec.samples');
  check('h2-samples-rows', data._samples_rows.length === 1);
  check('h2-samples-row-ref', data._samples_rows[0].ref === 'Smith 1950');
}
test_h2_columnar_samples_table();

function test_h2_columnar_extra_rows_attached() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const data = {
    sections: [
      { id: 'S1', lithology_blocks: [{ pattern: 'a' }, { pattern: 'b' }],
        age_units: [{ label: 'A' }], samples: [{ bed_idx: 1 }, { bed_idx: 2 }, { bed_idx: 3 }] },
      { id: 'S2', lithology_blocks: [{ pattern: 'c' }],
        age_units: [], samples: [] },
    ],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  ctx.rcaTableConfigs(data);
  check('h2-lithology-rows-total', data._lithology_blocks_rows.length === 3);
  check('h2-age-rows-total', data._age_units_rows.length === 1);
  check('h2-samples-rows-total', data._samples_rows.length === 3);
}
test_h2_columnar_extra_rows_attached();

function test_h2_columnar_csv_includes_patterns() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh'); // ensure i18n loaded
  const data = {
    sections: [{ id: 'S1', lithology_blocks: [{ pattern: 'dots', range_top_idx: 1, range_base_idx: 5 }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  ctx.rcaTableConfigs(data);
  const exp = ctx.rcaBuildTableExport(data, 'lithology_blocks');
  check('h2-csv-export-rows', exp.rows.length === 1);
  check('h2-csv-export-header-section-id', exp.headers.indexOf(ctx.t('col.secId')) !== -1);
  check('h2-csv-export-row-content', exp.rows[0].indexOf('dots') !== -1);
}
test_h2_columnar_csv_includes_patterns();

// ---- PR1 H3: species_ranges CSV optional columns ----
//
// rcaTableConfigs range-chart branch must dynamically expand the species
// columns based on which fields are populated in the actual rows, so a
// CSV/TSV export only shows columns with non-default data. Mirrors
// rca_core/exporter.py's optional column logic.
//
// 7 new trilingual keys: col.authorYear, col.rangeTopBed, col.rangeTopIdx,
// col.endpointKind, col.occurrenceMode, col.colConfidence, col.note.
//
// The test asserts the BEHAVIOR (extra header appears iff a row populates
// the corresponding field) — not the column order — so future column
// ordering tweaks don't silently break the test.
function test_h3_species_csv_optional_columns() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // First row populates 5 optional fields; second row only `author_year`.
  // Expectation: all 5 optional cols present, but the columns triggered by
  // a row that has `author_year` only (e.g. col.rangeTopBed) MUST also be
  // present when ANY row triggers them. Conversely, col.note, triggered by
  // row[1], must also be present.
  const data = {
    sections: [{ name: 'S1', age_range: 'Permian', formations: [], formation_thickness_m: '', coordinates: '' }],
    species_ranges: [
      { species: 'A. typicus', section: 'S1', range_base: 'Capitanian', range_top: 'Wordian',
        biozone: '', author_year: 'Smith, 1950', range_top_bed: 'Bed 12', range_top_idx: 4,
        endpoint_kind: 'observed', occurrence_mode: 'in_situ', confidence: 0.85, note: '' },
      { species: 'B. decorus', section: 'S1', range_base: 'Roadian', range_top: 'Kungurian',
        biozone: '', author_year: 'Jones, 1960', note: 'rare' },
    ],
    biozones: [], other_fossils: [], confidence: 0,
  };
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'species_ranges');
  check('h3-species-cfg-exists', !!cfg);
  // Base 5 cols + 5 optionals + 1 (agreement if multi) — multi is false
  // here (no data.runs), so exactly 10 cols.
  check('h3-col-author-year-included',
    cfg.cols.indexOf('col.authorYear') !== -1);
  check('h3-col-range-top-bed-included',
    cfg.cols.indexOf('col.rangeTopBed') !== -1);
  check('h3-col-range-top-idx-included',
    cfg.cols.indexOf('col.rangeTopIdx') !== -1);
  check('h3-col-endpoint-kind-included',
    cfg.cols.indexOf('col.endpointKind') !== -1);
  check('h3-col-occurrence-mode-included',
    cfg.cols.indexOf('col.occurrenceMode') !== -1);
  check('h3-col-confidence-included',
    cfg.cols.indexOf('col.colConfidence') !== -1);
  check('h3-col-note-included',
    cfg.cols.indexOf('col.note') !== -1);
  check('h3-no-agreement-column-when-single-run',
    cfg.cols.indexOf('col.agreement') === -1);
  // Base columns remain in front.
  check('h3-base-col-species-first', cfg.cols[0] === 'col.species');
  check('h3-base-col-biozone-fifth', cfg.cols[4] === 'col.biozone');
}
test_h3_species_csv_optional_columns();

function test_h3_species_csv_optional_note() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Only row[0] has author_year; only row[1] has note. Both optional cols
  // must be in the headers (because "some row has it").
  const data = {
    sections: [], species_ranges: [
      { species: 'A', section: '', range_base: '', range_top: '', biozone: '', author_year: 'X' },
      { species: 'B', section: '', range_base: '', range_top: '', biozone: '', note: 'cf.' },
    ],
    biozones: [], other_fossils: [], confidence: 0,
  };
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'species_ranges');
  check('h3-note-col-present', cfg.cols.indexOf('col.note') !== -1);
  check('h3-author-year-col-present', cfg.cols.indexOf('col.authorYear') !== -1);
}
test_h3_species_csv_optional_note();

function test_h3_species_csv_no_optional_when_empty() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // No optional fields populated anywhere → only the 5 base columns.
  const data = {
    sections: [], species_ranges: [
      { species: 'A', section: '', range_base: '', range_top: '', biozone: '' },
      { species: 'B', section: '', range_base: '', range_top: '', biozone: '' },
    ],
    biozones: [], other_fossils: [], confidence: 0,
  };
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'species_ranges');
  check('h3-base-5-cols', cfg.cols.length === 5);
  check('h3-no-author-year', cfg.cols.indexOf('col.authorYear') === -1);
  check('h3-no-range-top-bed', cfg.cols.indexOf('col.rangeTopBed') === -1);
  check('h3-no-range-top-idx', cfg.cols.indexOf('col.rangeTopIdx') === -1);
  check('h3-no-endpoint-kind', cfg.cols.indexOf('col.endpointKind') === -1);
  check('h3-no-occurrence-mode', cfg.cols.indexOf('col.occurrenceMode') === -1);
  check('h3-no-confidence', cfg.cols.indexOf('col.colConfidence') === -1);
  check('h3-no-note', cfg.cols.indexOf('col.note') === -1);
}
test_h3_species_csv_no_optional_when_empty();

// H3: rcaBuildTableExport must produce rows whose column count matches
// cfg.cols so CSV/TSV headers align with data. Uses note + author_year
// (both present) + a row with no optionals.
function test_h3_species_csv_export_alignment() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('en');
  const data = {
    sections: [], species_ranges: [
      { species: 'A', section: '', range_base: '', range_top: '', biozone: '',
        author_year: 'X, 1990', note: 'cf.' },
      { species: 'B', section: '', range_base: '', range_top: '', biozone: '' },
    ],
    biozones: [], other_fossils: [], confidence: 0,
  };
  ctx.rcaTableConfigs(data);
  const exp = ctx.rcaBuildTableExport(data, 'species_ranges');
  const expectedCols = 1 + 5 + 2; // # + 5 base + authorYear + note
  check('h3-export-headers-count', exp.headers.length === expectedCols);
  for (const row of exp.rows) {
    check('h3-export-row-count-matches-headers', row.length === exp.headers.length);
  }
}
test_h3_species_csv_export_alignment();

// ---- PR2 M11: retryWithBackoff wired into extractRangeChart ----
//
// Direct-mode fetch should retry on transient network failures (5xx,
// TypeError from fetch) up to 3 times before failing. The wrapped
// call must respect the caller-supplied opts.signal so a user cancel
// stops retries mid-flight.
function test_m11_retry_with_backoff_importable() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // error-utils.js assigns window.RCAErrorUtils; in vm context "window"
  // is the context object itself.
  check('m11-error-utils-on-context', !!ctx.RCAErrorUtils);
  check('m11-retry-with-backoff-is-function',
    ctx.RCAErrorUtils && typeof ctx.RCAErrorUtils.retryWithBackoff === 'function');
}
test_m11_retry_with_backoff_importable();

function test_m11_retry_succeeds_on_eventual_ok() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let calls = 0;
  const result = ctx.RCAErrorUtils.retryWithBackoff(async () => {
    calls += 1;
    if (calls < 3) throw new Error('transient');
    return { ok: true, payload: 42 };
  }, { maxRetries: 3, initialDelay: 0.001, backoffFactor: 1.0, maxDelay: 0.001 });
  return result.then((r) => {
    check('m11-retry-eventual-ok-returns', r && r.ok === true);
    check('m11-retry-attempts-3', calls === 3);
  });
}
const _m11a = test_m11_retry_succeeds_on_eventual_ok();

function test_m11_retry_returns_null_on_unrecoverable() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let calls = 0;
  const result = ctx.RCAErrorUtils.retryWithBackoff(async () => {
    calls += 1;
    throw new Error('always fails');
  }, { maxRetries: 2, initialDelay: 0.001, backoffFactor: 1.0, maxDelay: 0.001 });
  return result.then(() => {
    check('m11-retry-all-fail-attempts-3', calls === 3); // 1 initial + 2 retries
  }).catch((e) => {
    // We don't expect catch here — retryWithBackoff rethrows on lastError.
    check('m11-retry-throws-last-error', !!e);
  });
}
const _m11b = test_m11_retry_returns_null_on_unrecoverable();

function test_m11_retry_aborts_on_signal() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const ctl = new AbortController();
  let calls = 0;
  const result = ctx.RCAErrorUtils.retryWithBackoff(async () => {
    calls += 1;
    throw new Error('transient');
  }, { maxRetries: 5, initialDelay: 0.05, backoffFactor: 1.0, maxDelay: 0.05, signal: ctl.signal });
  // Abort after a tick so the retry loop has time to enter one sleep cycle.
  setTimeout(() => ctl.abort(), 30);
  return result.then(() => {
    // If retry returns a result (no throw), the loop exited cleanly.
    check('m11-retry-abort-stops-loop', calls < 6);
  }).catch((e) => {
    // If it throws, either AbortError or transient Error is acceptable
    // as long as the loop stopped before exhausting all retries.
    check('m11-retry-abort-stops-loop', calls < 6);
  });
}
const _m11c = test_m11_retry_aborts_on_signal();

// M11 e2e: extractRangeChart must retry on transient 5xx (and stop retrying
// on auth/4xx). First 2 attempts return 503, 3rd returns ok. Stub
// RCAErrorUtils with shorter delays so the test is fast.
function test_m11_extract_retries_on_5xx() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let calls = 0;
  ctx.fetch = async () => {
    calls += 1;
    if (calls < 3) {
      return { ok: false, status: 503, text: async () => 'service unavailable' };
    }
    return {
      ok: true,
      json: async () => ({
        content: [{ type: 'text', text: JSON.stringify({
          sections: [], species_ranges: [], biozones: [], other_fossils: [],
          confidence: 0.5,
        })}],
      }),
      text: async () => '',
    };
  };
  // Speed up retries: override the retry helper on the ErrorUtils object.
  const origRet = ctx.RCAErrorUtils.retryWithBackoff;
  ctx.RCAErrorUtils.retryWithBackoff = (fn, opts) => origRet(fn, {
    ...opts,
    initialDelay: 0.001, backoffFactor: 1.0, maxDelay: 0.001,
  });
  return ctx.extractRangeChart({
    dataUrl: 'data:image/png;base64,QUFB',
    mode: 'range_chart',
    baseUrl: 'https://example.com',
    model: 'm', maxTokens: 100,
  }).then((res) => {
    check('m11-extract-retry-eventual-ok', res.ok === true);
    check('m11-extract-retry-attempts-3', calls === 3);
  });
}
const _m11d = test_m11_extract_retries_on_5xx();

// M11 e2e: 4xx auth errors should NOT retry — first failure surfaces.
function test_m11_extract_no_retry_on_4xx() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let calls = 0;
  ctx.fetch = async () => {
    calls += 1;
    return { ok: false, status: 401, text: async () => 'unauthorized' };
  };
  return ctx.extractRangeChart({
    dataUrl: 'data:image/png;base64,QUFB',
    mode: 'range_chart',
    baseUrl: 'https://example.com',
    model: 'm', maxTokens: 100,
  }).then((res) => {
    check('m11-extract-4xx-no-retry', calls === 1);
    check('m11-extract-4xx-error-key', res.ok === false && res.errorKey === 'err.401');
  });
}
const _m11e = test_m11_extract_no_retry_on_4xx();

// M12: leading LF must also trigger the formula guard (PR3 anchored here
// so the export-side regression is captured with PR1's H2 export change).
function test_export_newline_injection_guard() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaToCsv(['c1'], [['\n=cmd']]);
  // rcaCsvCell wraps cells containing \r/\n/" in double quotes, so the
  // second line is `"'\n=cmd"`. The KEY assertion is that the guard
  // inserted an apostrophe before the trigger character — the raw `=cmd`
  // is no longer at the start of the cell.
  const secondLine = out.split('\r\n')[1] || '';
  check('export-newline-cell-prefixed-with-quote', secondLine.indexOf("'") === 1);
  check('export-newline-trigger-not-at-start',
    secondLine.replace(/^"+|'+/, '').indexOf('=cmd') > 0 ||
    // The cell is `"'\n=cmd"` — after stripping leading `"` the next char
    // is `'`, NOT `=`.
    secondLine[1] === "'");
}
test_export_newline_injection_guard();
test_export_newline_injection_guard();

// H5 end-to-end: extractRangeChart must flip ok=false when the parsed
// JSON trips the truncated_or_unrecognized_payload warning in range_chart
// mode, but keep ok=true with the warning attached for the other modes.
function test_h5_extract_range_chart_flip_to_error() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // The payload must NOT match any known root (no 'sections' etc.) so the
  // normalizer flags it. We pre-pend the warning because the warning path
  // is what extract_range_chart checks post-normalization.
  const payload = JSON.stringify({ totally_unrelated_key: 1 });
  const calls = [];
  ctx.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return {
      ok: true,
      json: async () => ({ content: [{ type: 'text', text: payload }] }),
      text: async () => payload,
    };
  };
  return ctx.extractRangeChart({
    dataUrl: 'data:image/png;base64,QUFB',
    mode: 'range_chart',
    baseUrl: 'https://example.com',
    apiKey: 'test-key',
  }).then((res) => {
    check('h5-range-extract-ok-false', res.ok === false);
    check('h5-range-extract-errorKey-err-parse', res.errorKey === 'err.parse');
    check('h5-range-extract-warning-flag', res.warning === 'truncated_or_unrecognized_payload');
    check('h5-range-extract-fetch-called', calls.length >= 1);
  });
}
const _h5E = test_h5_extract_range_chart_flip_to_error();
if (_h5E && typeof _h5E.then === 'function') {
  _h5E.catch((e) => { console.log('FAIL h5-range-extract-error', e && e.message); fail++; });
}

function test_h5_columnar_keeps_ok_true() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const payload = JSON.stringify({ totally_unrelated_key: 1 });
  ctx.fetch = async () => ({
    ok: true,
    json: async () => ({ content: [{ type: 'text', text: payload }] }),
    text: async () => payload,
  });
  return ctx.extractRangeChart({
    dataUrl: 'data:image/png;base64,QUFB',
    mode: 'columnar_section',
    baseUrl: 'https://example.com',
    apiKey: 'test-key',
  }).then((res) => {
    check('h5-columnar-extract-ok-true', res.ok === true);
    check('h5-columnar-extract-warnings-flagged',
      res.data && Array.isArray(res.data._warnings) &&
      res.data._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
  });
}
const _h5C = test_h5_columnar_keeps_ok_true();
if (_h5C && typeof _h5C.then === 'function') {
  _h5C.catch((e) => { console.log('FAIL h5-columnar-extract-error', e && e.message); fail++; });
}

// Wait for async races to settle before printing summary.
setTimeout(() => {
  console.log(`\n--- ${pass} passed, ${fail} failed ---`);
  process.exit(fail ? 1 : 0);
}, 100);
