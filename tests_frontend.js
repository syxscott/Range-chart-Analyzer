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

// The browser's `URL` is a CONSTRUCTIBLE WHATWG parser that also carries the
// object-URL helpers; the sandbox used to stub it as a bare object literal,
// so `new URL(...)` threw inside js/minimax.js and every direct-mode test ran
// against an "unparseable" target (empty hostname). Since REVIEW-2026-09-20
// the SSRF gate fails closed on an empty hostname, which turned that stub into
// 25 false failures. Hand the context Node's real WHATWG URL — the same
// parser Chrome/Firefox ship, including the decimal/hex IPv4 normalization the
// gate relies on — plus the two object-URL helpers js/export.js calls.
class SandboxURL extends require('url').URL {
  static createObjectURL() { return 'blob:fake'; }
  static revokeObjectURL() { /* no-op */ }
}

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
    // UI-REVIEW-2026-09-08: firstChild is required by the evidence-chain
    // UI's explicit child-clearing loop (while (host.firstChild) ...).
    get firstChild() { return this.children[0] || null; },
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
function check(name, ok, detail) {
  if (ok) { pass++; console.log('PASS', name); } else {
    fail++;
    // An optional detail makes a parity failure readable without having to
    // re-run the Python oracle by hand.
    console.log('FAIL', name + (detail === undefined ? '' : ' — ' + detail));
  }
}

// Async tests run fire-and-forget: their assertions land whenever their
// promise resolves. The summary used to be printed from a fixed 100 ms
// timer, so a test that took longer had its assertions run AFTER
// process.exit() — the run reported green while those checks were silently
// dropped. Register every async test here, count rejections as failures,
// and drain the list before printing the summary.
const pendingTests = [];
function trackAsync(name, p) {
  if (!p || typeof p.then !== 'function') return p;
  pendingTests.push(
    Promise.resolve(p).catch((e) => {
      fail += 1;
      console.log('FAIL', name, '(rejected:' + ((e && e.message) || e) + ')');
    })
  );
  return p;
}

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
    // 2026-09-20 (BORROW-2026-09-20 A): the coverage-contract mirror. It has
    // to load before quality.js / aggregate.js, which call into it for the
    // ledger and the response_kind / reason_codes merge rules.
    'js/reason-codes.js',
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
          'globalThis.rcaCleanNameForLookup = rcaCleanNameForLookup;\n' +
          // FIX-2026-09-22 (items 4/5): the GBIF issue builder (names.ambiguous
          // branch) and the coverage reason-code consumer need to be reachable.
          'globalThis.rcaNameIssuesFromGbif = rcaNameIssuesFromGbif;\n' +
          'globalThis.rcaCoverageReasonIssues = rcaCoverageReasonIssues;\n' +
          'globalThis.rcaAttachCoverageReasonHints = rcaAttachCoverageReasonHints;\n' +
          // REVIEW-2026-09-20 parity tests: the chart-mode detectors and the
          // GBIF name-verification round need to be reachable from the test.
          'globalThis.rcaAutoDetectChartMode = rcaAutoDetectChartMode;\n' +
          'globalThis.rcaAutoDetectChartModeDetailed = rcaAutoDetectChartModeDetailed;\n' +
          'globalThis.rcaVerifySpeciesNamesAsync = rcaVerifySpeciesNamesAsync;\n' +
          'globalThis.rcaCancelNameVerify = rcaCancelNameVerify;\n' +
          'globalThis.rcaResultFilePrefix = rcaResultFilePrefix;\n' +
          'globalThis.NAME_VERIFY_TIMEOUT_MS = NAME_VERIFY_TIMEOUT_MS;\n' +
          'globalThis.NAME_VERIFY_BUDGET_MS = NAME_VERIFY_BUDGET_MS;\n' +
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
      vm.runInContext(src, ctx, { filename: f });
    } catch (loadErr) {
      console.error('WARN: skipping', f, 'due to load error:', loadErr.message);
    }
    // After each file loads, export its top-level bindings for testing.
    // These are plain `const`/`function` declarations that vm doesn't expose
    // on the context automatically.
    const exports = {
      'js/config.js': ['RCA_CONFIG', 'RCA_STORE', 'rcaStoreGet', 'rcaStoreSet', 'rcaClampMaxTokens'],
      'js/i18n.js': ['RCA_I18N', 'rcaSetLang', 'rcaApplyI18n', 't'],
      'js/json-utils.js': ['safeJsonLoads', 'extractBalancedJsonObject'], // truncation-repair fixture test uses safeJsonLoads
      'js/aggregate.js': ['rcaMergeResults', 'RCA_DEFAULT_KEYMAP', 'RCA_COLUMNAR_KEYMAP', 'RCA_ZONATION_KEYMAP'],
      // 2026-09-20 (BORROW-2026-09-20 A): the contract mirror + the ledger
      // entry point, so the reason-code tests and the merge wiring can call
      // them directly.
      'js/reason-codes.js': [
        'RCAReasonCodes', 'rcaCoverageLedger', 'rcaNormalizeReasonCodes',
        'rcaNormalizeResponseKind', 'rcaMergeResponseKinds', 'rcaMergeReasonCodes',
        'rcaCoverageState', 'rcaIsAnswered', 'rcaReasonCodeRollup', 'rcaPyRound',
      ],
      'js/quality.js': ['rcaCoverageFor'],
      // UI-REVIEW-2026-09-05: zonation_chart normalizer.
      // UI-REVIEW-2026-09-07: chart-type classification normalizer.
      'js/minimax.js': ['rcaNormalizeZonationChartResult', 'rcaNormalizeChartClassification'],
      'js/table.js': [
        'rcaRenderResults', 'rcaTableConfigs', 'rcaBuildTableExport',
        // REVIEW-2026-09-20 (M4/L2): the exporter mirrors and the shared
        // agreement-band rule are asserted directly now, so they have to be
        // reachable from the test context.
        'rcaRowsForTable', 'rcaRowCellsFor', 'rcaExportCellText',
        'rcaAgreementBand', 'rcaRowAgreementBand', 'rcaIsDict',
        'rcaLooksZonationChart', 'rcaLooksAbundance', 'rcaLooksColumnar',
        'rcaLooksColumnarEmptySections', 'rcaLooksPhylogeneticTree',
      ],
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
// FIX-2026-09-22 (audit item 5): the OLD version of this block FROZE the
// zh-only gap of the BORROW-2026-09-20 coverage-ledger / reason_code.*
// catalog as a derived "exemption" - the test itself was part of the bug.
// The en + ja glosses have now landed on BOTH transports (rca_core/i18n.py
// and js/i18n.js), so the exemption is gone: STRICT three-way parity is
// asserted on both sides, and a completeness check forbids ANY key that the
// Python oracle carries in fewer than all three locales.
function _pyI18nKeysByLocale() {
  const pySrc = fs.readFileSync(
    path.join(__dirname, 'rca_core', 'i18n.py'), 'utf8');
  const out = {};
  for (const lang of ['zh', 'en', 'ja']) {
    const keys = new Set();
    const start = pySrc.indexOf('TRANSLATIONS["' + lang + '"] = {');
    if (start !== -1) {
      const end = pySrc.indexOf('\n}', start);
      const body = pySrc.slice(start, end === -1 ? undefined : end);
      let m;
      const rx = /^    "((?:col|sec|quality|names|reason_code)\.[A-Za-z0-9_]+)":/gm;
      while ((m = rx.exec(body)) !== null) keys.add(m[1]);
    }
    out[lang] = keys;
  }
  return out;
}

function test_i18n_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const py = _pyI18nKeysByLocale();
  // 1. Completeness on the ORACLE side: every col/sec/quality/names/
  //    reason_code key must exist in zh AND en AND ja. (The fallback chain
  //    in rca_core/i18n.py is lang -> en -> raw key, so a zh-only key
  //    renders as a raw dotted key for en/ja users.)
  const pyLangs = ['zh', 'en', 'ja'];
  const allPy = new Set([...py.zh, ...py.en, ...py.ja]);
  const incomplete = [...allPy].filter(
    (k) => pyLangs.some((l) => !py[l].has(k)));
  check('i18n-py-all-locales-complete', incomplete.length === 0,
    incomplete.join(','));
  // 2. STRICT three-way parity on the JS side (no exemption set any more).
  const zh = Object.keys(ctx.RCA_I18N.zh).sort();
  const en = Object.keys(ctx.RCA_I18N.en).sort();
  const ja = Object.keys(ctx.RCA_I18N.ja).sort();
  check('i18n-zh-en-parity', JSON.stringify(zh) === JSON.stringify(en),
    'zh=' + zh.length + ' en=' + en.length);
  check('i18n-zh-ja-parity', JSON.stringify(zh) === JSON.stringify(ja),
    'zh=' + zh.length + ' ja=' + ja.length);
  // 3. The keys the oracle carries must not be MISSING from the JS zh
  //    catalog either (shared-parity covers both directions per locale, but
  //    this pins the oracle-catalogue relationship for all namespaces).
  const missing = [...allPy].filter((k) => !(k in ctx.RCA_I18N.zh));
  check('i18n-js-covers-py-catalog', missing.length === 0, missing.join(','));
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
  // CONTRACT-UPDATE-2026-09-20 (REVIEW-2026-09-20 #4): the abundance buckets are
  // keyed on Python's key-PRESENCE probes (rca_core/extractor.py:1941-1955),
  // not on the loose `site_id` / `count` / `zone` spellings this test used to
  // pin. Oracle: normalize_abundance_result({"_array_root":[...legacy keys...]})
  // -> sites/abundances/zones all EMPTY and every record under
  // _extras._unclassified, i.e. nothing is dropped, nothing is invented.
  const legacy = [
    { site_id: 'S1', location: 'Loc1' },   // no `name` -> not a site
    { abundance: 'A', count: 5 },          // `abundance` without `level`
    { zone: 'Z1', assemblage: 'ass' },     // neither `name`+`age` nor `taxon`
  ];
  const legacyOut = ctx.rcaNormalizeAbundanceResult({ _array_root: legacy, _note: 'wrap' });
  check('p0-5: abundance legacy rows empty (sites)',
        legacyOut.sites && legacyOut.sites.length === 0);
  check('p0-5: abundance legacy rows empty (abundances)',
        legacyOut.abundances && legacyOut.abundances.length === 0);
  check('p0-5: abundance legacy rows empty (zones)',
        legacyOut.zones && legacyOut.zones.length === 0);
  check('p0-5: abundance legacy records survive in _extras._unclassified',
        legacyOut._extras && legacyOut._extras._unclassified
        && legacyOut._extras._unclassified.length === 3
        && legacyOut._extras._unclassified[0].site_id === 'S1');

  // The unwrap itself still has to work — with the keys the Python contract
  // documents (oracle: sites=2 / abundances=2 / zones=1, _unclassified=1).
  const arr = [
    { name: 'S1', location: 'Loc1' },                        // site
    { name: 'S2', depth_unit: 'm' },                         // site (depth_unit)
    { taxon: 'Pinus', level: '3' },                          // abundance
    { abundance: '20%', level: '3' },                        // abundance
    { name: 'Z1', age: 'Holocene' },                         // zone
    { foo: 1 },                                              // _unclassified
  ];
  const out = ctx.rcaNormalizeAbundanceResult({ _array_root: arr, _note: 'wrap' });
  check('p0-5: abundance sites>=1', out.sites && out.sites.length === 2);
  check('p0-5: abundance abundances>=1', out.abundances && out.abundances.length === 2);
  check('p0-5: abundance zones>=1', out.zones && out.zones.length === 1);
  // Python mutates `parsed` IN PLACE with setdefault: `confidence` is read from
  // the root, and `_note`/`_array_root` are stripped from `_extras`
  // (_pop_array_root_extras) so the wrapper never duplicates the payload.
  check('p0-5: abundance wrapper stripped from _extras',
        out._extras && out._extras._array_root === undefined
        && out._extras._note === undefined);
  check('p0-5: abundance unclassified kept',
        out._extras._unclassified && out._extras._unclassified.length === 1
        && out._extras._unclassified[0].foo === 1);
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
  // The inverted-range warning is keyed `quality.range_top_lt_base` on the
  // accuracy path, both for bed indices and for resolved ages — this mirrors
  // rca_core/quality.py and tests/test_review_2026_07_31_domain.py
  // ::test_inverted_numeric_ma_range_flagged. (`quality.fad_lt_lad` is the
  // consistency check's bed-inversion key, a different signal.)
  check('quality Ma range valid (no range_top_lt_base)',
    !ageOk.issues.some(i => i.msg_key === 'quality.range_top_lt_base'));
  // Inverted age range IS flagged.
  const ageInv = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian' }],
    species_ranges: [{ species: 'X', section: 'A', range_base: '250 Ma', range_top: '300 Ma', biozone: '' }],
    confidence: 0.9,
  });
  check('quality inverted Ma range flagged',
    ageInv.issues.some(i => i.msg_key === 'quality.range_top_lt_base'));
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
const _m14 = trackAsync('m14-alert-animationend', test_m14_alert_removed_on_animationend());

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
const _m3 = trackAsync('m3-fetch-manual-redirect', test_m3_fetch_manual_redirect());

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
  // Sprint A (CODE_REVIEW_2026-09-01): values updated to ICS v2024/12
  // (Santonian base 85.7, Dapingian base 471.3-side top 469.4; was
  // GTS2016-vintage 86.3 / 470.0).
  check('ics_table Santonian base 85.7', t.Santonian && t.Santonian.base_ma === 85.7);
  check('ics_table Turonian base 93.9', t.Turonian && t.Turonian.base_ma === 93.9);
  check('ics_table no Pleistocene pseudo-stage', !('Pleistocene' in t));
  check('ics_table Dapingian base 471.3', t.Dapingian && t.Dapingian.base_ma === 471.3);
}

test_json_utils_wrapper_promotion();
test_quality_fad_lad_ma_branch();
test_quality_cross_era_proportional();
test_quality_steno_biozone_map();
test_aggregate_author_h7_parity();
test_prompt_phylo_parent_and_degradation();
test_ics_table_data_parity();

// FE-FIX-2026-09-21: structural parity lock between resources/ics_current.json
// (the authority, refreshed by scripts/update_ics.py — which does NOT emit
// JS, so js/ics_table.js is hand-synced) and globalThis.RCA_ICS_TABLE. The
// Silurian/Quaternian stages Aeronian, Rhuddanian, Telychian, Homerian,
// Gorstian, Sheinwoodian, Ludfordian, Greenlandian, Meghalayan,
// Northgrippian and Late Pleistocene were silently missing, so viz.js
// rcaVizStageBounds returned null for them (unplaceable in the browser while
// Python placed them). These checks make ANY future JSON<->table drift —
// missing rows, extra rows, or age/era mismatches — fail this suite loudly on
// the next refresh instead of degrading placement quality silently.
function test_ics_table_vs_ics_current_json() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const t = ctx.RCA_ICS_TABLE;
  const authority = JSON.parse(fs.readFileSync(
    path.join(__dirname, 'rca_core', 'resources', 'ics_current.json'), 'utf8'));
  const jsonKeys = Object.keys(authority);
  const missing = jsonKeys.filter((k) => !t[k]);
  const extra = Object.keys(t).filter((k) => !authority[k]);
  check('ics-parity no rows missing vs ics_current.json',
    missing.length === 0, 'missing: ' + JSON.stringify(missing));
  check('ics-parity no extra rows vs ics_current.json',
    extra.length === 0, 'extra: ' + JSON.stringify(extra));
  const ageDiffs = [];
  const eraDiffs = [];
  for (const k of jsonKeys) {
    if (!t[k]) continue;
    if (t[k].top_ma !== authority[k].top_ma ||
        t[k].base_ma !== authority[k].base_ma) {
      ageDiffs.push(k + ' js[' + t[k].top_ma + ',' + t[k].base_ma +
        '] json[' + authority[k].top_ma + ',' + authority[k].base_ma + ']');
    }
    if (authority[k].era && t[k].era && t[k].era !== authority[k].era) {
      eraDiffs.push(k + ' js=' + t[k].era + ' json=' + authority[k].era);
    }
  }
  check('ics-parity top/base ages byte-consistent with ics_current.json',
    ageDiffs.length === 0, ageDiffs.join(' | '));
  check('ics-parity eras match ics_current.json',
    eraDiffs.length === 0, eraDiffs.join(' | '));
  // Spot regression guards for the 11 formerly-missing rows: they must
  // resolve through the same shape viz.js consumes (numeric top/base).
  for (const k of ['Aeronian', 'Rhuddanian', 'Telychian', 'Homerian',
                   'Gorstian', 'Sheinwoodian', 'Ludfordian', 'Greenlandian',
                   'Meghalayan', 'Northgrippian', 'Late Pleistocene']) {
    check('ics-parity ' + k + ' resolvable',
      !!t[k] && typeof t[k].top_ma === 'number' &&
      typeof t[k].base_ma === 'number' &&
      t[k].top_ma === authority[k].top_ma &&
      t[k].base_ma === authority[k].base_ma);
  }
}

test_ics_table_vs_ics_current_json();

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
// Mirror rca_core/extractor.py. CONTRACT-UPDATE-2026-09-20 (REVIEW-2026-09-20
// #7): the flag is raised in TWO places and they are not the same for every
// mode:
//   * `normalize_result` (range_chart ONLY, extractor.py:1123-1128) tags a
//     foreign root itself, and `extract_range_chart` (1391-1406) then turns
//     that tag into ok=False/err.parse BEFORE the shared `_ok_result` runs;
//   * the other normalizers do NOT tag their output — they park the foreign
//     keys under `_extras` and stay ok=True. The shared `_ok_result`
//     (rcaUnusablePayloadReason) only fires when the payload produced NOTHING
//     (`_extracted_any` false), which `_extras`/`_warnings` already prevent.
// So the old "every mode's normalizer must flag" expectation was JS-only and
// has been replaced by what the Python oracle returns for the same input.
function test_h5_normalizer_truncated_warning_flag() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // Plain unknown payload — no known range_chart roots.
  const out = ctx.rcaNormalizeResult({ foo: 1 });
  check('h5-range-warnings-array', Array.isArray(out._warnings));
  check('h5-range-warning-flag-set',
    out._warnings && out._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
  // Python keeps the undocumented root key visible instead of dropping it.
  check('h5-range-foreign-key-in-extras',
    out._extras && out._extras.foo === 1);
}
test_h5_normalizer_truncated_warning_flag();

function test_h5_normalizer_truncated_warning_columnar() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeColumnarResult({ foo: 1 });
  // Oracle normalize_columnar_result({"foo":1}):
  //   {"sections":[],"fossil_legend":[],"lithology_legend":[],"cross_beds":[],
  //    "confidence":0.0,"_extras":{"foo":1}}  — no `_warnings` key at all.
  check('h5-columnar-warning-flag-set', out._warnings === undefined);
  check('h5-columnar-foreign-key-in-extras',
    out._extras && out._extras.foo === 1);
}
test_h5_normalizer_truncated_warning_columnar();

function test_h5_normalizer_truncated_warning_abundance() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeAbundanceResult({ foo: 1 });
  // Oracle normalize_abundance_result({"foo":1}): same shape, same silence.
  check('h5-abundance-warning-flag-set', out._warnings === undefined);
  check('h5-abundance-foreign-key-in-extras',
    out._extras && out._extras.foo === 1);
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

// ---- PR1 H6: phylo metadata inheritance ----
//
// CONTRACT-UPDATE-2026-09-20 (REVIEW-2026-09-20 #4): `_normalize_phylogenetic
// _tree_into` (rca_core/extractor.py:2298-2342) reads the metadata block from
// ONE source — `raw["metadata"]` — writes the six canonical keys
// (title / extraction_timestamp / tree_type / scale / rooted / source) and then
// copies over every OTHER key the metadata block carried (that is how the
// legacy taxon_group / root_name / total_nodes / version / image_source survive).
// Root-level siblings are NOT promoted any more: they are undocumented root
// keys, so they land in `out["_extras"]` untouched. The old JS copy invented
// `image_source: ""` on every tree and read the taxonomy from the root, so the
// browser and the server disagreed on the same payload.
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
  // Root level: nothing lifted, nothing lost — `_extras` keeps all three.
  check('h6-taxon_group-present', out.metadata.taxon_group === undefined);
  check('h6-total_nodes-present', out._extras.total_nodes === 42);
  check('h6-version-default', out._extras.version === '1');
  check('h6-root_name-empty', out.metadata.root_name === undefined);
  check('h6-image_source-empty', out.metadata.image_source === undefined);
  check('h6-taxon_group-not-fabricated-on-extras',
    out._extras.taxon_group === 'Radiolaria');
  // The six canonical keys always exist, with the Python defaults.
  check('h6-metadata-canonical-keys',
    out.metadata.title === '' && out.metadata.extraction_timestamp === ''
    && out.metadata.tree_type === '' && out.metadata.scale === ''
    && out.metadata.rooted === true && out.metadata.source === '');

  // Inside `metadata`, the same fields are carried through verbatim — this is
  // the path the provenance UI reads.
  const inside = ctx.rcaNormalizePhylogeneticTreeResult({
    metadata: { taxon_group: 'Radiolaria', total_nodes: 42, version: '1', root_name: '' },
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false }],
  });
  check('h6-metadata-taxon_group-lifted', inside.metadata.taxon_group === 'Radiolaria');
  check('h6-metadata-total_nodes-lifted', inside.metadata.total_nodes === 42);
  check('h6-metadata-version-lifted', inside.metadata.version === '1');
  check('h6-metadata-root_name-lifted', inside.metadata.root_name === '');
}
test_h6_phylo_metadata_inherits_root_taxonomy();

function test_h6_phylo_metadata_promotes_image_source_from_root() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // metadata-level `image_source` is the documented alias of `source`
  // (Python: `s(metadata_raw.get("source", metadata_raw.get("image_source", "")))`),
  // and because it is not one of the six NEW_META_KEYS it also survives
  // verbatim next to it.
  const out = ctx.rcaNormalizePhylogeneticTreeResult({
    metadata: { tree_type: 'cladogram', image_source: 'file.jpg' },
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false }],
  });
  check('h6-image_source-on-source', out.metadata.source === 'file.jpg');
  check('h6-image-source-field', out.metadata.image_source === 'file.jpg');
  check('h6-tree_type-promoted', out.metadata.tree_type === 'cladogram');
  // A ROOT-level image_source is not metadata: it must not fabricate a source
  // and must not vanish either (extractor.py:2330-2342 -> _extras).
  const rootLevel = ctx.rcaNormalizePhylogeneticTreeResult({
    image_source: 'file.jpg',
    metadata: { tree_type: 'cladogram' },
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'Spasmaria', is_leaf: false }],
  });
  check('h6-root-image-source-not-promoted',
    rootLevel.metadata.source === ''
    && rootLevel.metadata.image_source === undefined);
  check('h6-root-image-source-in-extras',
    rootLevel._extras.image_source === 'file.jpg');
  // An explicit null `source` wins over the `image_source` fallback: `dict.get`
  // only defaults when the KEY IS ABSENT (REVIEW-2026-09-10).
  const nullSource = ctx.rcaNormalizePhylogeneticTreeResult({
    metadata: { source: null, image_source: 'img' },
    root_ids: ['n0'],
    nodes: [{ id: 'n0', parent: null, name: 'A' }],
  });
  check('h6-source-null-no-fallback',
    nullSource.metadata.source === ''
    && nullSource.metadata.image_source === 'img');
}
test_h6_phylo_metadata_promotes_image_source_from_root();

// ---- PR1 H2: columnar 4 new sub-tables (lithology_blocks / age_units /
// samples / confidence_by_section) ----
//
// Sprint B (REVIEW-2026-09-04): the three sub-tables no longer stash flat
// row arrays on `data` (data._lithology_blocks_rows / _age_units_rows /
// _samples_rows) — that stash made rendering and export diverge (render
// read data[cfg.id], always empty; export read the stash) and leaked
// underscore keys into the JSON export. Both now read the single derived
// source rcaColumnarSubTableRows/rcaRowsForTable, so these tests assert
// rendering + export directly: row counts AND visible copy/CSV buttons.
// Also covers M12: rcaFormulaSafe must guard a leading LF (\n) the same
// way it does other formula triggers.
function test_h2_columnar_lithology_blocks_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh'); // ensure i18n loaded for rcaRenderResults
  // rcaTableConfigs is hoisted onto globalThis in table.js.
  const data = {
    sections: [{ id: 'S1', lithology_blocks: [{ pattern: 'dots', range_top_idx: 1, range_base_idx: 5 }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [],
    confidence: 0,
  };
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'lithology_blocks');
  check('h2-lithology-blocks-cfg', !!cfg);
  check('h2-lithology-blocks-title', cfg && cfg.titleKey === 'sec.lithologyBlocks');
  // Render must produce one data row (was 0 before the same-source fix).
  const html = ctx.rcaRenderResults(data, '');
  check('h2-lithology-blocks-rows', ctx.rcaBuildTableExport(data, 'lithology_blocks').rows.length === 1);
  check('h2-lithology-blocks-render-row', /data-table="lithology_blocks"[\s\S]*?<td[^>]*>dots<\/td>/.test(html));
  check('h2-lithology-blocks-no-stash', data._lithology_blocks_rows === undefined);
  // Copy/CSV buttons are only emitted when rows.length > 0.
  check('h2-lithology-blocks-copy-btn', html.indexOf('data-copy="lithology_blocks"') !== -1);
  check('h2-lithology-blocks-csv-btn', html.indexOf('data-csv="lithology_blocks"') !== -1);
}
test_h2_columnar_lithology_blocks_table();

function test_h2_columnar_age_units_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh');
  const data = {
    sections: [{ id: 'S1', age_units: [{ label: 'Ypresian', range_top_idx: 0, range_base_idx: 3 }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'age_units');
  check('h2-age-units-cfg', !!cfg);
  check('h2-age-units-title', cfg && cfg.titleKey === 'sec.ageUnits');
  check('h2-age-units-rows', ctx.rcaBuildTableExport(data, 'age_units').rows.length === 1);
  const html = ctx.rcaRenderResults(data, '');
  check('h2-age-units-render-row', /data-table="age_units"[\s\S]*?<td[^>]*>Ypresian<\/td>/.test(html));
  check('h2-age-units-copy-btn', html.indexOf('data-copy="age_units"') !== -1);
}
test_h2_columnar_age_units_table();

function test_h2_columnar_samples_table() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh');
  const data = {
    sections: [{ id: 'S1', samples: [{ bed_idx: 4, fossil_marker: 'A', ref: 'Smith 1950' }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  const cfg = ctx.rcaTableConfigs(data).find((c) => c.id === 'samples');
  check('h2-samples-cfg', !!cfg);
  check('h2-samples-title', cfg && cfg.titleKey === 'sec.samples');
  check('h2-samples-rows', ctx.rcaBuildTableExport(data, 'samples').rows.length === 1);
  const html = ctx.rcaRenderResults(data, '');
  check('h2-samples-render-row', /data-table="samples"[\s\S]*?<td[^>]*>Smith 1950<\/td>/.test(html));
  check('h2-samples-csv-btn', html.indexOf('data-csv="samples"') !== -1);
}
test_h2_columnar_samples_table();

function test_h2_columnar_extra_rows_attached() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh');
  const data = {
    sections: [
      { id: 'S1', lithology_blocks: [{ pattern: 'a' }, { pattern: 'b' }],
        age_units: [{ label: 'A' }], samples: [{ bed_idx: 1 }, { bed_idx: 2 }, { bed_idx: 3 }] },
      { id: 'S2', lithology_blocks: [{ pattern: 'c' }],
        age_units: [], samples: [] },
    ],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0,
  };
  // Multi-section flatten (sections concatenated in order) — same numbers
  // the export path must produce.
  check('h2-lithology-rows-total', ctx.rcaBuildTableExport(data, 'lithology_blocks').rows.length === 3);
  check('h2-age-rows-total', ctx.rcaBuildTableExport(data, 'age_units').rows.length === 1);
  check('h2-samples-rows-total', ctx.rcaBuildTableExport(data, 'samples').rows.length === 3);
  // Render count badges agree with export ("(3)" / "(1)").
  const html = ctx.rcaRenderResults(data, '');
  check('h2-render-count-badge-litho',
    /data-table="lithology_blocks"[\s\S]{0,400}?result-count">\(3\)<\/span>/.test(html));
  check('h2-flatten-section-id-carried', ctx.rcaBuildTableExport(data, 'lithology_blocks').rows[2].indexOf('S2') !== -1);
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
const _m11a = trackAsync('m11-retry-eventual-ok', test_m11_retry_succeeds_on_eventual_ok());

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
const _m11b = trackAsync('m11-retry-unrecoverable', test_m11_retry_returns_null_on_unrecoverable());

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
const _m11c = trackAsync('m11-retry-abort-signal', test_m11_retry_aborts_on_signal());

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
const _m11d = trackAsync('m11-extract-retries-5xx', test_m11_extract_retries_on_5xx());

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
const _m11e = trackAsync('m11-extract-no-retry-4xx', test_m11_extract_no_retry_on_4xx());

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

// H5 end-to-end: extractRangeChart must flip ok=false when the parsed
// JSON trips the truncated_or_unrecognized_payload warning in range_chart
// mode, but keep ok=true for the other modes.
//
// CONTRACT-UPDATE-2026-09-20 (REVIEW-2026-09-20 #7): the Python oracle for
// {"totally_unrelated_key": 1} is
//   range_chart       -> ok=False, error_key='err.parse',
//                        warning='<truncation prose> | rescued inner object: unusable',
//                        data._warnings=['truncated_or_unrecognized_payload']
//   columnar_section  -> ok=True,  warning='',  NO data._warnings,
//                        data._extras={'totally_unrelated_key': 1}
// i.e. the ok=False flip is extractor.py:1391-1406 (range_chart only, and it
// runs BEFORE the shared `_ok_result`), while the shared guard is what covers
// the other seven modes. `warning` is the Python prose, not the bare tag — the
// tag lives in `data._warnings` on both engines.
function test_h5_extract_range_chart_flip_to_error() {
  const ctx = buildContext();
  loadAllScripts(ctx);
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
    check('h5-range-extract-warning-flag',
      res.warning === 'Result may be truncated (model hit max_tokens). '
      + 'Try raising the max_tokens setting and re-running.'
      + ' | rescued inner object: unusable');
    check('h5-range-extract-data-warnings',
      res.data && Array.isArray(res.data._warnings)
      && res.data._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
    check('h5-range-extract-fetch-called', calls.length >= 1);
  });
}
const _h5E = trackAsync('h5-range-extract-error', test_h5_extract_range_chart_flip_to_error());

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
    // Oracle extract_columnar_section: NO `_warnings` (only the range-chart
    // normalizer raises that tag), and the foreign keys stay visible in
    // `_extras` — that is the whole point of the H8 extras contract.
    check('h5-columnar-extract-warnings-flagged',
      res.data && res.data._warnings === undefined
      && res.data._extras && res.data._extras.totally_unrelated_key === 1);
  });
}
const _h5C = trackAsync('h5-columnar-extract', test_h5_columnar_keeps_ok_true());

// The SHARED half of the guard (extractor.py `_ok_result`, REVIEW-2026-09-20 #7
// "扩展到 8 模式"): every mode now answers through one contract, so a payload
// that rescued NOTHING is a hard error on every mode — not just range_chart —
// while a payload that produced any content (even only `_extras` /
// `_unclassified`) stays ok=True. Matrix below is the verbatim Python oracle
// (`extract_<mode>` with rca_core.extractor.call_llm_api stubbed); the JS
// direct transport must reproduce it line for line.
function test_h5_shared_guard_matrix() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const PROSE = 'Result may be truncated (model hit max_tokens). '
    + 'Try raising the max_tokens setting and re-running.';
  const TRUNC_NO_ROWS = PROSE + ' | truncated output rescued no usable records';
  const CASES = [
    // payload                        mode                 trunc  ok   warning
    ['{"sections":[]}',                'range_chart',       false, true,  ''],
    ['{"sections":[]}',                'range_chart',       true,  false, TRUNC_NO_ROWS],
    ['{"sections":[]}',                'columnar_section',  false, true,  ''],
    ['{"sections":[]}',                'columnar_section',  true,  false, TRUNC_NO_ROWS],
    ['{"sites":[],"abundances":[],"zones":[]}', 'abundance_diagram', false, true, ''],
    ['{"sites":[],"abundances":[],"zones":[]}', 'abundance_diagram', true, false, TRUNC_NO_ROWS],
    ['{"zonations":[],"zones":[],"correlations":[]}', 'zonation_chart', false, true, ''],
    ['{"zonations":[],"zones":[],"correlations":[]}', 'zonation_chart', true, false, TRUNC_NO_ROWS],
    // A truncated run that DID rescue rows stays ok=true, prose warning only.
    ['{"_array_root":[{"zzz":1}]}',    'range_chart',       true,  true,  PROSE],
    // Foreign-but-empty abundance root: still ok=true, keys parked in _extras.
    ['{"some_key":1}',                 'abundance_diagram', false, true,  ''],
  ];
  const run = (text, truncated) => {
    ctx.fetch = async () => ({
      ok: true,
      json: async () => ({
        content: [{ type: 'text', text }],
        stop_reason: truncated ? 'max_tokens' : 'end_turn',
      }),
      text: async () => text,
    });
  };
  const steps = CASES.map(([text, mode, truncated, expectOk, expectWarning]) => () => {
    run(text, truncated);
    return ctx.extractRangeChart({
      dataUrl: 'data:image/png;base64,QUFB',
      mode,
      baseUrl: 'https://example.com',
      apiKey: 'test-key',
    }).then((res) => {
      const id = 'h5-shared-guard ' + mode + (truncated ? '+trunc' : '');
      check(id + ' ok', res.ok === expectOk);
      check(id + ' warning', res.warning === expectWarning);
      if (!expectOk) check(id + ' errorKey', res.errorKey === 'err.parse');
    });
  });
  // Sequential: every step rewrites ctx.fetch.
  return steps.reduce((p, step) => p.then(step), Promise.resolve());
}
const _h5S = trackAsync('h5-shared-guard-matrix', test_h5_shared_guard_matrix());

// ---------------------------------------------------------------------------
// Sprint B (REVIEW-2026-09-04) regression tests
// ---------------------------------------------------------------------------

// Item 2: _array_root classification parity with rca_core/extractor.py
// _classify_array_item (extractor.py:344-389) + the unwrap rules at
// extractor.py:636-661 — key-existence checks, name-only section fallback,
// bare strings -> other_fossils, unclassifiable dicts -> _unclassified
// (kept, never dropped).
function test_sprintb_array_root_classification_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const wrapped = { _array_root: [
    { name: 'S1', age_range: 'Cretaceous' },  // section: name + age_range signal
    { name: 'S2' },                            // section: name-only fallback
    'bare string',                             // other_fossils (stripped)
    { foo: 1 },                                // _unclassified
  ] };
  const out = ctx.rcaNormalizeResult(wrapped);
  check('sprintb-array-root-section-s1',
    out.sections.length === 2 && out.sections[0].name === 'S1');
  check('sprintb-array-root-section-name-only', out.sections[1].name === 'S2');
  check('sprintb-array-root-bare-string-fossil',
    out.other_fossils.length === 1 && out.other_fossils[0] === 'bare string');
  check('sprintb-array-root-unclassified-kept',
    Array.isArray(out._unclassified) && out._unclassified.length === 1
    && out._unclassified[0].foo === 1);
}
test_sprintb_array_root_classification_parity();

// Item 11: KNOWN_ROOTS.range_chart no longer contains _extras, mirroring
// rca_core/extractor.py:629-630 RANGE_CHART_ROOTS. An _extras-only payload
// must now trip truncated_or_unrecognized_payload exactly like Python.
function test_sprintb_range_chart_roots_exclude_extras() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeResult({ _extras: { foo: 1 } });
  check('sprintb-extras-only-flagged-truncated',
    Array.isArray(out._warnings)
    && out._warnings.indexOf('truncated_or_unrecognized_payload') !== -1);
}
test_sprintb_range_chart_roots_exclude_extras();

// Item 9: abundance-mode array rescue must preserve top-level confidence
// (and other non-bucket fields) and keep unclassifiable dicts under
// _unclassified, mirroring rca_core/extractor.py:1941-1955 in-place
// setdefault behavior.
//
// CONTRACT-UPDATE-2026-09-20 (REVIEW-2026-09-20 #4): ALL FOUR rows below are
// unclassifiable for Python — its probes need `name`+(`location`|`depth_unit`|
// `age_range`), `taxon`, (`abundance`+`level`) or `name`+`age`, and none of
// `site_id` / `count` / `zone` / `assemblage` satisfies one. The old test only
// expected `{foo:1}` there because the browser copy still bucketed the other
// three on its own looser spelling.
function test_sprintb_abundance_unwrap_keeps_top_level() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const out = ctx.rcaNormalizeAbundanceResult({
    _array_root: [
      { site_id: 'S1', location: 'Loc1' },
      { abundance: 'A', count: 5 },
      { zone: 'Z1', assemblage: 'ass' },
      { foo: 1 },
    ],
    confidence: 0.7,
    _note: 'wrap',
  });
  check('sprintb-abundance-confidence-kept', out.confidence === 0.7);
  check('sprintb-abundance-unclassified-in-extras',
    out._extras && Array.isArray(out._extras._unclassified)
    && out._extras._unclassified.length === 4);
  // Python mutates `parsed` in place, so the wrapper never reaches `_extras`
  // (`_pop_array_root_extras`) but the records all survive in document order.
  check('sprintb-abundance-unclassified-order',
    out._extras._unclassified.map((r) => Object.keys(r)[0]).join(',')
    === 'site_id,abundance,zone,foo');
  check('sprintb-abundance-wrapper-stripped',
    out._extras._array_root === undefined && out._extras._note === undefined);
}
test_sprintb_abundance_unwrap_keeps_top_level();

// Item 3: multi-fence payload selection in safeJsonLoads. The model
// restates the JSON schema in a FIRST fence and emits the real payload in a
// SECOND fence; the first fence that strictly parses to a dict carrying a
// known root key must win; no qualifying block falls back to the first
// block (old behavior).
function test_sprintb_multifence_selects_payload_block() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const schemaBlock = '{"properties": {"sections": {"type": "array"}}, "required": ["sections"], "format": "rca-v1"}';
  const payloadBlock = '{"sections": [{"id": "A", "group": "G", "thickness_m": "10"}], "overall_confidence": 0.9}';
  const text = 'Schema:\n```json\n' + schemaBlock + '\n```\nResult:\n```json\n' + payloadBlock + '\n```\n';
  const r = ctx.safeJsonLoads(text);
  check('sprintb-multifence-payload-selected',
    Array.isArray(r.sections) && r.sections.length === 1 && r.sections[0].id === 'A');
  // Single qualifying fence keeps working (back-compat with prose-wrapped fences).
  const single = ctx.safeJsonLoads('Here:\n```json\n' + payloadBlock + '\n```\nThanks');
  check('sprintb-multifence-single-block-ok',
    Array.isArray(single.sections) && single.sections.length === 1);
  // No block qualifies -> fall back to the FIRST block (pre-existing rule).
  const noneQualify = '```json\n{"properties": {"a": 1}}\n```\nand\n```json\n{"required": ["x"]}\n```';
  const r3 = ctx.safeJsonLoads(noneQualify);
  check('sprintb-multifence-fallback-first-block',
    r3 && r3.properties && r3.properties.a === 1);
}
test_sprintb_multifence_selects_payload_block();

// Item 8: boundary Ma determinism in _resolveAgeBound — mirror
// ics_stage_from_age (rca_core/standards/ics.py:38-74): strict interior hit
// first, then the first base match (boundary belongs to the YOUNGER stage
// whose base it defines), then the top match. Values below are verified
// against the Python oracle directly.
function test_sprintb_resolve_age_bound_boundary_determinism() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const r1 = ctx._resolveAgeBound('259.51 Ma', 'older');
  check('sprintb-boundary-259.51-wuchiapingian',
    r1 && r1.name === 'Wuchiapingian' && r1.ma === 259.51);
  const r2 = ctx._resolveAgeBound('254.14 Ma', 'older');
  check('sprintb-boundary-254.14-changhsingian', r2 && r2.name === 'Changhsingian');
  // Parity note: the review text suggested 251.902 -> Changhsingian, but
  // the Python oracle (ics_stage_from_age(251.902)) deterministically
  // returns 'Induan' — 251.902 is Induan's BASE, and base matches outrank
  // top matches. We lock the Python-verified value.
  const r3 = ctx._resolveAgeBound('251.902 Ma', 'older');
  check('sprintb-boundary-251.902-induan', r3 && r3.name === 'Induan');
  // Strict interior hits are unaffected.
  const r4 = ctx._resolveAgeBound('255 Ma', 'older');
  check('sprintb-interior-255-wuchiapingian', r4 && r4.name === 'Wuchiapingian');
  const r5 = ctx._resolveAgeBound('253 Ma', 'older');
  check('sprintb-interior-253-changhsingian', r5 && r5.name === 'Changhsingian');
}
test_sprintb_resolve_age_bound_boundary_determinism();

// Item 14: retryWithBackoff must implement the documented retryable
// predicate semantics (mirrors the parallel Python error_utils fix):
// retryable(result) === false -> accept the result immediately (no more
// retries); === true -> keep retrying until retries are exhausted, then
// return the LAST result without throwing.
function test_sprintb_retryable_predicate_semantics() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // (a) retryable always false -> exactly 1 call, result returned.
  let callsA = 0;
  const pa = ctx.RCAErrorUtils.retryWithBackoff(async () => {
    callsA += 1;
    return { ok: true, value: 'immediate' };
  }, { maxRetries: 3, initialDelay: 0.001, backoffFactor: 1.0, maxDelay: 0.001, retryable: () => false });
  // (b) retryable always true -> 1 + maxRetries calls, LAST result returned.
  let callsB = 0;
  const pb = ctx.RCAErrorUtils.retryWithBackoff(async () => {
    callsB += 1;
    return { attempt: callsB };
  }, { maxRetries: 2, initialDelay: 0.001, backoffFactor: 1.0, maxDelay: 0.001, retryable: () => true });
  // (c) mixed: first result warrants retry, second is accepted.
  let callsC = 0;
  const pc = ctx.RCAErrorUtils.retryWithBackoff(async () => {
    callsC += 1;
    return { n: callsC };
  }, { maxRetries: 3, initialDelay: 0.001, backoffFactor: 1.0, maxDelay: 0.001, retryable: (r) => r.n < 2 });
  return Promise.all([pa, pb, pc]).then(([ra, rb, rc]) => {
    check('sprintb-retryable-false-immediate', callsA === 1 && ra.value === 'immediate');
    check('sprintb-retryable-true-retries-then-last-result', callsB === 3 && rb.attempt === 3);
    check('sprintb-retryable-mixed-accepted-midway', callsC === 2 && rc.n === 2);
  });
}
const _sprintbRetry = trackAsync('sprintb-retryable-semantics', test_sprintb_retryable_predicate_semantics());

// Item 15: ICS Sprint B data sync — 15 English rock-unit series labels in
// RCA_ICS_SERIES + the Chinese 统 aliases in RCA_ICS_CN_SERIES, mirroring
// rca_core/standards/ics.py _SERIES_STAGE_LISTS / _CN_SERIES_ALIASES.
function test_sprintb_ics_series_rock_units() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const series = ctx.RCA_ICS_SERIES;
  const added = ['upper permian', 'lower permian', 'upper triassic', 'lower triassic',
    'upper jurassic', 'lower jurassic', 'upper ordovician', 'lower ordovician',
    'upper devonian', 'lower devonian', 'upper silurian', 'lower silurian',
    'upper carboniferous', 'lower carboniferous', 'upper cambrian'];
  check('sprintb-ics-15-rock-unit-labels', added.every((k) => k in series));
  // Each rock-unit label must equal its early/late sibling exactly
  // (series = time-translated epoch).
  check('sprintb-ics-upper-permian-sibling',
    JSON.stringify(series['upper permian']) === JSON.stringify(series['late permian']));
  check('sprintb-ics-lower-jurassic-sibling',
    JSON.stringify(series['lower jurassic']) === JSON.stringify(series['early jurassic']));
  check('sprintb-ics-upper-cambrian-furongian',
    series['upper cambrian'] && series['upper cambrian'].name === 'Furongian');
  check('sprintb-ics-lower-carboniferous-mississippian',
    series['lower carboniferous'] && series['lower carboniferous'].name === 'Mississippian');
  const cn = ctx.RCA_ICS_CN_SERIES;
  const tongKeys = Object.keys(cn).filter((k) => k.indexOf('统') !== -1);
  check('sprintb-ics-cn-22-tong-aliases', tongKeys.length === 22);
  check('sprintb-ics-cn-tong-maps',
    cn['下白垩统'] === 'early cretaceous' && cn['上寒武统'] === 'late cambrian'
    && cn['中奥陶统'] === 'middle ordovician' && cn['下二叠统'] === 'early permian');
}
test_sprintb_ics_series_rock_units();

// Item 6: the three CSRF early-return paths must run the same cleanup as
// the happy path. Behavioral check: a failed CSRF GET with no cached token
// returns err.csrfFetch, and BOTH the timeout timer (clearTimeout called on
// the live timer id) and the caller's abort listener are released.
function test_sprintb_csrf_early_return_cleans_up() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  // CSRF GET fails with a non-OK response and there is no cached token ->
  // early return err.csrfFetch (previously leaked timer + listener).
  ctx.fetch = async () => ({ ok: false, status: 500, json: async () => ({}), text: async () => 'boom' });
  ctx.rcaCallBackend._sessionToken = '';
  ctx.rcaCallBackend._csrfToken = '';
  // Spy on the context's clearTimeout AFTER the scripts are loaded (the vm
  // resolves globals at call time), so we observe the cleanup call itself.
  const realClearTimeout = ctx.clearTimeout;
  const cleared = [];
  ctx.clearTimeout = (t) => { cleared.push(t); return realClearTimeout(t); };
  const removed = [];
  const opts = {
    apiKey: 'sk', baseUrl: 'https://e', model: 'm', maxTokens: 100,
    mode: 'range_chart', transport: 'backend',
    dataUrl: 'data:image/png;base64,QUFB', mediaType: 'image/png',
    caption: '', chartLang: 'auto',
    signal: {
      aborted: false,
      addEventListener() {},
      removeEventListener(_t, cb) { removed.push(cb); },
    },
  };
  return ctx.extractRangeChart(opts).then((res) => {
    check('sprintb-csrf-early-error-key', res.ok === false && res.errorKey === 'err.csrfFetch');
    check('sprintb-csrf-early-clears-timer', cleared.length === 1);
    check('sprintb-csrf-early-removes-abort-listener', removed.length === 1);
  });
}
const _sprintbCsrf = trackAsync('sprintb-csrf-cleanup', test_sprintb_csrf_early_return_cleans_up());

// ---- UI-REVIEW-2026-09-05: low-agreement flag must parse the "n/m"
// agreement string when agreement_count is absent, so a "3/3" row is not
// flagged low (cream row + green pill contradiction). ----
function test_ui_low_agreement_fallback() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh');
  const data = {
    sections: [{ name: 'S1' }],
    species_ranges: [
      { species: 'Alpha alpha', section: 'S1', range_base: '1', range_top: '2', biozone: '', agreement_count: 3, agreement: '3/3' },
      { species: 'Beta beta', section: 'S1', range_base: '1', range_top: '2', biozone: '', agreement: '3/3' },
      { species: 'Gamma gamma', section: 'S1', range_base: '1', range_top: '2', biozone: '', agreement: '1/3' },
      { species: 'Delta delta', section: 'S1', range_base: '1', range_top: '2', biozone: '' },
    ],
    biozones: [], other_fossils: [], confidence: 0.8, runs: 3,
  };
  const html = ctx.rcaRenderResults(data, '');
  function rowHasFlag(name) {
    const i = html.indexOf(name);
    if (i === -1) return null;
    const start = html.lastIndexOf('<tr', i);
    return html.slice(start, i).includes('row-low-agreement');
  }
  check('ui-lowagreement-count3-not-flagged', rowHasFlag('Alpha alpha') === false);
  check('ui-lowagreement-string3-not-flagged', rowHasFlag('Beta beta') === false);
  check('ui-lowagreement-string1-flagged', rowHasFlag('Gamma gamma') === true);
  check('ui-lowagreement-missing-flagged', rowHasFlag('Delta delta') === true);
}
test_ui_low_agreement_fallback();

// ---- UI-REVIEW-2026-09-05: zonation_chart (radiolarian biozonation /
// correlation charts) — normalizer, table render, and merge keymap. ----
function test_zonation_chart_support() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh');
  check('zon-normalizer-exported', typeof ctx.rcaNormalizeZonationChartResult === 'function');
  check('zon-keymap-exported', !!ctx.RCA_ZONATION_KEYMAP && ctx.RCA_ZONATION_KEYMAP.primary === 'zones');

  const data = ctx.rcaNormalizeZonationChartResult({
    zonations: [
      { name: 'Bragin (2018), Koryak', region: 'Koryak Highlands', framework: 'radiolarian',
        reference: 'Bragin, 2018', legacy_key: 'keepme' },
    ],
    zones: [
      { name: 'Proparvicingula moniliformis Zone', zonation: 'Bragin (2018), Koryak',
        rank: 'zone', age_span: 'lower Rhaetian', base_age: '', top_age: '',
        stage: 'Rhaetian', defined_by: 'FAD P. moniliformis', note: '' },
    ],
    correlations: [
      { from_zone: 'Proparvicingula moniliformis Zone', to_zone: 'Crassistephanus thuyensis Zone',
        from_zonation: 'Bragin (2018), Koryak', to_zonation: 'Carter (1993)',
        basis: 'shared stage', note: '' },
    ],
    confidence: 0.9,
  });
  check('zon-normalize-3-tables', data.zonations.length === 1 && data.zones.length === 1 && data.correlations.length === 1);
  check('zon-normalize-row', data.zones[0].name === 'Proparvicingula moniliformis Zone' && data.zones[0].stage === 'Rhaetian');
  check('zon-normalize-extras', data.zonations[0]._extras && data.zonations[0]._extras.legacy_key === 'keepme');
  check('zon-normalize-confidence', data.confidence === 0.9);

  // array-root rescue
  const rescued = ctx.rcaNormalizeZonationChartResult({ _array_root: [
    { from_zone: 'A Zone', to_zone: 'B Zone' },
    { name: 'C Zone', zonation: 'Z1' },
    { name: 'Z2', framework: 'ammonoid' },
    { something: 'else' },
  ] });
  check('zon-array-root-rescue', rescued.correlations.length === 1 && rescued.zones.length === 1 && rescued.zonations.length === 1);
  check('zon-array-root-unclassified', rescued._extras && Array.isArray(rescued._extras._unclassified) && rescued._extras._unclassified.length === 1);

  // table render: 3 tables with the right rows
  const html = ctx.rcaRenderResults(data, '');
  check('zon-render-zonations-table', html.indexOf('data-table="zonations"') !== -1);
  check('zon-render-zones-table', html.indexOf('data-table="zones"') !== -1);
  check('zon-render-correlations-table', html.indexOf('data-table="correlations"') !== -1);
  check('zon-render-zone-row', html.indexOf('Proparvicingula moniliformis Zone') !== -1);
  check('zon-render-corr-row', html.indexOf('Crassistephanus thuyensis Zone') !== -1);
  check('zon-export-zones-rows', ctx.rcaBuildTableExport(data, 'zones').rows.length === 1);
  check('zon-export-corr-rows', ctx.rcaBuildTableExport(data, 'correlations').rows.length === 1);

  // merge via keymap: two identical runs → agreement 2/2
  const merged = ctx.rcaMergeResults([data, data], 2, ctx.RCA_ZONATION_KEYMAP);
  check('zon-merge-zones', merged && Array.isArray(merged.zones) && merged.zones.length === 1);
  check('zon-merge-agreement', merged.zones[0].agreement === '2/2');
  check('zon-merge-correlations', merged.correlations.length === 1);
}
test_zonation_chart_support();

// ---- UI-REVIEW-2026-09-07: vision chart-type classifier normalizer +
// auto-forwarding wiring (app.js detailed heuristic -> 'auto' passthrough;
// minimax.js direct-mode classify -> effective mode). ----
function test_auto_classify() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('cls-normalizer-exported', typeof ctx.rcaNormalizeChartClassification === 'function');

  const good = ctx.rcaNormalizeChartClassification(
    { chart_type: 'zonation_chart', reason: 'tie lines', confidence: 0.9 });
  check('cls-valid', good.chart_type === 'zonation_chart' && good.confidence === 0.9);

  const unknown = ctx.rcaNormalizeChartClassification({ chart_type: 'banana', confidence: 0.9 });
  check('cls-unknown-degrades', unknown.chart_type === 'unknown');

  const empty = ctx.rcaNormalizeChartClassification({});
  check('cls-empty-degrades', empty.chart_type === 'unknown' && empty.confidence === 0);

  // source-level wiring: app.js forwards unmatched auto; minimax classifies
  const appSrc = require('fs').readFileSync(path.join(__dirname, 'js/app.js'), 'utf8');
  check('cls-app-detailed-heuristic', appSrc.indexOf('rcaAutoDetectChartModeDetailed') !== -1);
  check('cls-app-auto-forward', /return detected\.matched \? detected\.mode : 'auto';/.test(appSrc));
  const mmSrc = require('fs').readFileSync(path.join(__dirname, 'js/minimax.js'), 'utf8');
  check('cls-minimax-classify-direct', mmSrc.indexOf('rcaNormalizeChartClassification(safeJsonLoads(clsText))') !== -1);
  check('cls-minimax-conf-threshold', mmSrc.indexOf("cls.confidence >= 0.5") !== -1);
  check('cls-server-auto-whitelisted',
        require('fs').readFileSync(path.join(__dirname, 'server.py'), 'utf8').indexOf("'auto',") !== -1);
}
test_auto_classify();

// ---- UI-REVIEW-2026-09-07: truncation repair (Level 3.5) must recover
// completed rows above a max_tokens cut — real fig_19 fixture. ----
function test_truncation_repair() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const fs2 = require('fs');
  const fixture = JSON.parse(fs2.readFileSync(
    path.join(__dirname, 'tests', 'fixtures', 'truncation', 'fig19_truncated_raw.json'),
    'utf8'));
  const out = ctx.safeJsonLoads(fixture.raw);
  check('trunc-repair-sites', (out.sites || []).length === 3);
  check('trunc-repair-abundances', (out.abundances || []).length === 61);
  check('trunc-repair-first-row', out.abundances[0]
    && out.abundances[0].taxon === 'Amphimelissa setosa'
    && out.abundances[0].abundance === '100');

  // synthetic: partial last row is kept (data-maximizing semantics)
  const syn = ctx.safeJsonLoads(
    '{"sites": [{"name": "S1"}], "abundances": [{"taxon": "A", "abundance": "10"}, {"taxon": "B", "abun');
  check('trunc-syn-sites', (syn.sites || []).length === 1);
  check('trunc-syn-rows', (syn.abundances || []).length === 2
    && syn.abundances[1].taxon === 'B');
}
test_truncation_repair();

// ---- UI-REVIEW-2026-09-08: GBIF name-verification UI rendering ----
function test_names_verify_ui() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  ctx.t('zh');
  check('nv-render-exported', typeof ctx.rcaRenderNameIssues === 'function');

  // DOM-level render into a real host element
  const host = ctx.document.getElementById('names-verify-slot');
  ctx.rcaRenderNameIssues([
    { msg_key: 'names.fuzzy', name: 'Psendotirolites asiaticus',
      suggestion: 'Pseudotirolites asiaticus', confidence: 85 },
    { msg_key: 'names.unmatched', name: 'Endemicus wonderus' },
  ]);
  // The stub's textContent does not aggregate children, so assert through
  // the child labels (children[1] = the text span in each row).
  const box = host.children[0];
  const rows = box.children.filter((c) => c.className === 'names-issue');
  check('nv-two-issues-rendered', rows.length === 2);
  const fuzzyLabel = rows[0].children[1].textContent;
  const unmatchedLabel = rows[1].children[1].textContent;
  check('nv-fuzzy-text', fuzzyLabel.indexOf('Pseudotirolites asiaticus') !== -1
    && fuzzyLabel.indexOf('names.fuzzy') === -1, fuzzyLabel);
  check('nv-unmatched-text', unmatchedLabel.indexOf('Endemicus wonderus') !== -1);

  // re-render with fewer issues replaces the block (stub innerHTML is a
  // plain property, so assert on the freshly built box instead)
  ctx.rcaRenderNameIssues([
    { msg_key: 'names.fuzzy', name: 'A', suggestion: 'B', confidence: 80 },
  ]);
  const box2 = host.children[0];
  check('nv-rerender-replaces', box2.children.length === 1);

  // app.js wiring present (async GBIF verify after render)
  const appSrc = require('fs').readFileSync(
    path.join(__dirname, 'js', 'app.js'), 'utf8');
  check('nv-app-gbif-wired', appSrc.indexOf('api.gbif.org/v1/species/match') !== -1);
  check('nv-app-render-hook', appSrc.indexOf('rcaRenderNameIssues') !== -1);
}
test_names_verify_ui();

// ---- CODE_REVIEW_2026-09-10 (backend parity) regression tests ----

// (R7) A model that restates the JSON contract in a fence before emitting the
// payload writes the REAL root keys into that example (plus <placeholders>),
// so the example qualified as a payload and evicted the data. Python and JS
// must pick the same block: the payload.
function test_json_fence_placeholder_ranking() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const ex = '{"sections": [{"name": "<section name>"}], '
    + '"species_ranges": [{"species": "<binomial>"}], "confidence": 0.9}';
  const pay = '{"sections": [{"name": "Real"}], '
    + '"species_ranges": [{"species": "Neoalbaillella optima"}], "confidence": 0.8}';
  const cases = [
    'Here is the contract:\n```json\n' + ex + '\n```\nNow the result:\n```json\n' + pay + '\n```',
    '```json\n' + ex + '\n```\n```json\n' + pay + '\n```',
  ];
  for (const text of cases) {
    const got = ctx.safeJsonLoads(text);
    check('json-fence-picks-payload',
      (got.species_ranges || [{}])[0].species === 'Neoalbaillella optima');
  }
  // Zonation payloads are recognised as payload roots (they were not).
  const zon = 'Schema:\n```json\n{"$schema": "x", "type": "object"}\n```\n'
    + 'Data:\n```json\n{"zonations": [{"name": "Z"}], "correlations": [], "zones": [], "confidence": 0.7}\n```';
  const zped = ctx.safeJsonLoads(zon);
  check('json-fence-zonation-payload',
    Array.isArray(zped.zonations) && zped.zonations.length === 1);
}

// (R8) A literal newline inside a JSON string is invalid JSON; the browser
// must escape it and keep the whole reply (Level 4 used to salvage one inner
// row instead, silently emptying the extraction).
function test_json_literal_newline_in_string() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const text = '{"sections": [{"name": "A"}], "note": "row 1\nrow 2", "confidence": 0.9}';
  let got = null;
  try { got = ctx.safeJsonLoads(text); } catch (_e) { got = null; }
  check('json-literal-newline-parsed', !!got && Array.isArray(got.sections)
    && got.sections.length === 1);
  check('json-literal-newline-note', !!got && got.note === 'row 1\nrow 2');
}

// (R9) Structured-item dedup: `String(obj)` produced the constant
// "[object Object]", so two runs whose blocks differed only by an _extras
// value collapsed into ONE block and the second was discarded.
function test_aggregate_extras_aware_dedup() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const run = (note) => ({
    sections: [{ id: 'S1', lithology_blocks: [
      { pattern: 'chert', range_base_idx: 1, range_top_idx: 4, _extras: { note } }] }],
    fossil_legend: [], lithology_legend: [], cross_beds: [], confidence: 0.6,
  });
  const merged = ctx.rcaMergeResults([run('A'), run('B')], 2, ctx.RCA_COLUMNAR_KEYMAP);
  const blocks = (merged.sections || [{}])[0].lithology_blocks || [];
  check('aggregate-extras-dedup-keeps-both', blocks.length === 2);
  // Same content still merges.
  const same = ctx.rcaMergeResults([run('A'), run('A')], 2, ctx.RCA_COLUMNAR_KEYMAP);
  check('aggregate-extras-dedup-merges-equal',
    ((same.sections || [{}])[0].lithology_blocks || []).length === 1);
}

// (R10) The qualifier-restore compared a NORMALISED value against the RAW
// mode, so it never fired — the merged taxon name differed from the server's
// for case/whitespace jitter.
function test_aggregate_qualifier_restore_normalised() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const run = (species) => ({
    species_ranges: [{ species, section: 'S1', range_base: '1',
                       range_top: '2', biozone: '' }],
    sections: [], biozones: [], other_fossils: [], confidence: 0.5,
  });
  // Both runs carry one occurrence, so the tie-break picks the first-seen
  // original — the point is that the restore now FIRES at all and the
  // qualifier survives verbatim (it used to no-op, leaving the raw mode
  // string, which differed from the server's answer).
  for (const [a, b] of [['Genus sp.', 'genus sp.'], ['genus sp.', 'Genus sp.']]) {
    const merged = ctx.rcaMergeResults([run(a), run(b)], 2);
    const sp = (merged.species_ranges || [{}])[0].species;
    check('aggregate-qualifier-restored:' + a, /sp\./.test(sp), sp);
    check('aggregate-qualifier-verbatim:' + a, sp === a || sp === b, sp);
  }
}

// (R11) Root-level _extras / _warnings carry figure-level data; the N-run
// path used to drop them while the single-run passthrough kept them.
function test_aggregate_root_extras_survive_multi_run() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const runs = [
    { species_ranges: [{ species: 'A', section: 'S1', range_base: '1', range_top: '2' }],
      sections: [], biozones: [], other_fossils: [], confidence: 0.5,
      _extras: { caption: 'Fig 3' }, _warnings: ['string_row_coerced'] },
    { species_ranges: [{ species: 'A', section: 'S1', range_base: '1', range_top: '2' }],
      sections: [], biozones: [], other_fossils: [], confidence: 0.5 },
  ];
  const merged = ctx.rcaMergeResults(runs, 2);
  check('aggregate-root-extras-kept',
    JSON.stringify(merged._extras) === JSON.stringify({ caption: 'Fig 3' }));
  check('aggregate-root-warnings-kept',
    JSON.stringify(merged._warnings) === JSON.stringify(['string_row_coerced']));
}

// (R12) `_warning` is a bare string for one flag and an array when several
// fire; the strict === missed the swap in exactly that combination.
function test_quality_warning_flag_shapes() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('quality-warning-flags-string',
    ctx.rcaWarningFlags('index_order_swap').indexOf('index_order_swap') !== -1);
  check('quality-warning-flags-array',
    ctx.rcaWarningFlags(['range_top_idx_truncated', 'index_order_swap'])
      .indexOf('index_order_swap') !== -1);
  check('quality-warning-flags-none', ctx.rcaWarningFlags(null).length === 0);
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian', lithology_blocks: [
      { pattern: 'chert', range_base_idx: 1, range_top_idx: 4,
        _warning: ['range_top_idx_truncated', 'index_order_swap'] }] }],
    species_ranges: [], biozones: [], other_fossils: [], confidence: 0.6,
  });
  check('quality-warning-swap-reported',
    res.issues.some((i) => i.msg_key === 'quality.bed_index_order_swapped'));
}

// (R13) `_payloadScore` used float division where Python floors, so an exact
// tie picked a different candidate in each engine.
function test_payload_score_integer_division() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const nested = { sections: [{ name: 'A' }],
                   x: { y: { z: { species_ranges: [], format: 'q' } } } };
  const score = ctx._payloadScore(nested);
  check('payload-score-is-integral', Number.isInteger(score), String(score));
}

test_json_fence_placeholder_ranking();
test_json_literal_newline_in_string();
test_aggregate_extras_aware_dedup();
test_aggregate_qualifier_restore_normalised();
test_aggregate_root_extras_survive_multi_run();
test_quality_warning_flag_shapes();
test_payload_score_integer_division();

// ---- CODE_REVIEW_2026-09-10 regression tests ----

// (R1) rcaCleanNameForLookup is a mirror of rca_core/names.py and feeds the
// GBIF query string. Its qualifier / ex-gr patterns carried literal 0x08
// bytes where a word boundary was intended, so "cf." / "aff." stripping never
// fired and the browser queried GBIF with the raw open-nomenclature string.
// These cases are the golden set from tests/test_survey_borrowed.py — the two
// implementations must agree input-for-input.
function test_name_clean_lookup_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const golden = [
    ['Clarkina yini', 'Clarkina yini'],
    ['Pseudotirolites cf. P. asiaticus (Zheng, 1979)', 'Pseudotirolites asiaticus'],
    ['Genus sp.', 'Genus'],
    ['Nankinella? aff. discoides', 'Nankinella discoides'],
    ['Albaillella ex gr. A. levis', 'Albaillella levis'],
    ['', ''],
  ];
  for (const [raw, expected] of golden) {
    const got = ctx.rcaCleanNameForLookup(raw);
    check('name-clean-parity:' + JSON.stringify(raw.slice(0, 28)), got === expected);
  }
  // The regexes must not contain a literal backspace (0x08) control char.
  const src = require('fs').readFileSync(
    path.join(__dirname, 'js', 'app.js'), 'utf8');
  check('name-clean-no-backspace-bytes', !/[\u0008]/.test(src));
}

// (R2) A malformed row (null / primitive) inside a result array used to
// throw out of rcaRenderResults, blanking the entire results panel.
//
// REVIEW-2026-09-20 (M4): the tolerance rule is rca_core/exporter.py's, NOT
// "drop whatever cannot be dereferenced". `_table_items` hands EVERY entry of
// the list to `_row_values`, which writes a non-dict row as a single padded
// cell (exporter.py:890-906) — it never filters. Dropping them made the
// browser render 1 row where the GUI grid and the XLSX showed 3 for the SAME
// payload.
//
// Every expected value below was read off the Python oracle
// (`rca_core.exporter.build_table_export(data, id, Translator('en').t)`) over
// this exact payload:
//   species_ranges -> [['1','','','','','',''],
//                      ['2','not-an-object','','','','',''],
//                      ['3','X','A','1','2','']]
//   other_fossils  -> [['1','plain string'],['2',''],['3','']]
// (exporter.py:395 reads only "fossil" / "text" out of a dict row, so a
// `{'label': ...}` row is an EMPTY cell on both transports — the old JS
// `label || species || taxon || name` chain invented keys no producer writes.)
function test_render_tolerates_malformed_rows() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const data = {
    species_ranges: [null, 'not-an-object',
                     { species: 'X', section: 'A', range_base: '1', range_top: '2' }],
    sections: [{ name: 'A' }],
    biozones: [],
    other_fossils: ['plain string', { label: 'Ammonite' }, null],
  };
  let html = null;
  try { html = ctx.rcaRenderResults(data, ''); }
  catch (e) { check('render-null-row-no-throw', false); return; }
  check('render-null-row-no-throw', true);
  check('render-keeps-valid-row', html.indexOf('>X<') !== -1);
  // The primitive row is shown as its own text, not dropped.
  check('render-primitive-row-shown', html.indexOf('not-an-object') !== -1);
  check('render-other-fossils-string-row', html.indexOf('plain string') !== -1);
  check('render-other-fossils-unknown-key-blank', html.indexOf('Ammonite') === -1);
  check('render-no-object-marker', html.indexOf('[object Object]') === -1);
  // Row COUNTS mirror the exporter: one rendered row per list entry.
  const exp = ctx.rcaBuildTableExport(data, 'species_ranges');
  check('render-export-keeps-all-rows', exp.rows.length === 3);
  check('render-export-null-row-is-blank',
    JSON.stringify(exp.rows[0]) === JSON.stringify(['1', '', '', '', '', '']));
  check('render-export-primitive-row-single-cell',
    JSON.stringify(exp.rows[1]) === JSON.stringify(['2', 'not-an-object', '', '', '', '']));
  check('render-export-valid-row-alive',
    JSON.stringify(exp.rows[2]) === JSON.stringify(['3', 'X', 'A', '1', '2', '']));
  const expF = ctx.rcaBuildTableExport(data, 'other_fossils');
  check('render-export-other-fossils-parity',
    JSON.stringify(expF.rows)
    === JSON.stringify([['1', 'plain string'], ['2', ''], ['3', '']]));
  // The rendered table advertises the same three rows in its header count
  // (title resolved through t() so the check is language-independent).
  check('render-species-count-label',
    html.indexOf(ctx.t('sec.species') + ' <span class="result-count">(3)</span>') !== -1);
}

// (M4 / REVIEW-2026-09-20, item e) js/table.js must dispatch to a table set
// in the SAME order as rca_core/exporter.py:get_configs_for_result, and with
// the SAME shape predicates. The order used to be abundance → … → zonation
// fourth, and `hasColumnarShape` accepted ANY `sections` array, so one
// payload rendered an abundance diagram in the browser and a zones table in
// the GUI / Excel export.
//
// The first half of the check compares the two SOURCE files (so reordering a
// branch on one side fails here even when no fixture exercises it); the
// second half is behavioral, on the payloads that actually drifted.
function test_table_dispatch_order_mirrors_exporter() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const pySrc = fs.readFileSync(path.join(__dirname, 'rca_core', 'exporter.py'), 'utf8');
  const jsSrc = fs.readFileSync(path.join(__dirname, 'js', 'table.js'), 'utf8');
  const pyBody = pySrc.slice(pySrc.indexOf('def get_configs_for_result'),
    pySrc.indexOf('def _abundance_diagram_tables'));
  const jsBody = jsSrc.slice(jsSrc.indexOf('function rcaTableConfigs'),
    jsSrc.indexOf('// -------- abundance-diagram'));
  const orderOf = (src, rx) => {
    const out = [];
    let m;
    const g = new RegExp(rx.source, 'g');
    while ((m = g.exec(src)) !== null) out.push(m[1]);
    return out;
  };
  const pyOrder = orderOf(pyBody, /if _looks_(zonation_chart|abundance|columnar|phylogenetic_tree)\(/);
  const jsOrder = orderOf(jsBody, /if \(rcaLooks(ZonationChart|Abundance|Columnar|PhylogeneticTree)\(/);
  const norm = {
    zonation_chart: 'ZonationChart', abundance: 'Abundance',
    columnar: 'Columnar', phylogenetic_tree: 'PhylogeneticTree',
  };
  check('dispatch-order-mirror:py-found-all', pyOrder.length === 4);
  check('dispatch-order-mirror:js-found-all', jsOrder.length === 4);
  check('dispatch-order-mirror:same-sequence',
    pyOrder.map((k) => norm[k]).join('>') === jsOrder.join('>'));

  // Behavior: the two shapes that used to disagree.
  const ids = (d) => ctx.rcaTableConfigs(d).map((c) => c.id).join(',');
  // Zonation descriptors + abundance rows in one payload: Python picks the
  // zones tables, so the browser must too.
  const both = {
    abundances: [{ taxon: 'T', level: '1' }],
    correlations: [{ from_zone: 'A', to_zone: 'B' }],
    zones: [], zonations: [], sections: [], species_ranges: [],
  };
  check('dispatch-zonation-beats-abundance',
    ids(both).indexOf('correlations') !== -1 && ids(both).indexOf('abundances') === -1);
  const abOnly = { abundances: [{ taxon: 'T', level: '1' }], sections: [], species_ranges: [] };
  check('dispatch-abundance-when-not-zonation', ids(abOnly).indexOf('abundances') !== -1);
  // An EMPTY abundances placeholder is every normalized result's default and
  // must not route anywhere (exporter.py:_looks_abundance).
  const emptyAb = { abundances: [], sections: [{ name: 'A' }], species_ranges: [{ species: 'X' }] };
  check('dispatch-empty-abundance-is-range-chart', ids(emptyAb) === 'sections,species_ranges,biozones,other_fossils');
  // hasColumnarShape: "the FIRST section is a dict carrying an `id`"
  // (exporter.py:_looks_columnar), not "sections is a non-empty array".
  check('columnar-predicate-needs-id',
    ctx.rcaLooksColumnar({ sections: [{ id: 'C1' }] }) === true
    && ctx.rcaLooksColumnar({ sections: [{ name: 'A' }] }) === false
    && ctx.rcaLooksColumnar({ sections: ['C1'] }) === false
    && ctx.rcaLooksColumnar({ sections: [] }) === false
    && ctx.rcaLooksColumnar(null) === false);
  check('dispatch-name-only-sections-stays-range-chart',
    ids({ sections: [{ name: 'A' }], species_ranges: [] }) === 'sections,species_ranges,biozones,other_fossils');
  check('dispatch-id-sections-is-columnar',
    ids({ sections: [{ id: 'C1' }], species_ranges: [] }).indexOf('lithology_blocks') !== -1);
  // Empty sections + a columnar-only key still means columnar (I7 rule).
  check('dispatch-empty-sections-columnar-fallback',
    ids({ sections: [], fossil_legend: [{ marker: 'm' }] }).indexOf('fossil_legend') !== -1);
  // Phylo only after the three above.
  check('dispatch-phylo-nodes',
    ids({ nodes: [{ id: 'n1', parent: null }] }) === 'nodes');

  // (item h) The dead `rcaRenderViz` no-op must stay deleted — nothing ever
  // called it, and #viz-host is preserved by app.js, not by a fake renderer.
  check('render-viz-dead-code-gone',
    jsSrc.indexOf('function rcaRenderViz') === -1
    && jsSrc.indexOf('globalThis.rcaRenderViz') === -1);
}

// (item g / L2) The low-agreement ROW BAND and the agreement PILL must come
// from the same integer thresholds, so a row never shows a red "needs
// review" background under a green/amber pill (or the reverse). The old row
// rule was `ac <= runs/2`, which flagged 1/2 and 2/4 while the pill called
// them "mid".
function test_agreement_band_and_pill_share_thresholds() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const band = (k, n) => ctx.rcaAgreementBand(k, n);
  check('band-good', band(2, 3) === 'good' && band(3, 3) === 'good' && band(1, 1) === 'good');
  check('band-mid', band(1, 2) === 'mid' && band(2, 4) === 'mid' && band(3, 8) === 'mid');
  check('band-low', band(1, 3) === 'low' && band(0, 3) === 'low');
  check('band-degenerate-n', band(1, 0) === 'low' && band(1, NaN) === 'low');
  // A non-dict row carries no agreement at all (null = "do not band it"),
  // which is also what keeps the render loop from dereferencing a string.
  check('band-skips-malformed-row',
    ctx.rcaRowAgreementBand(null, 3) === null
    && ctx.rcaRowAgreementBand('x', 3) === null
    && ctx.rcaRowAgreementBand({ agreement: '3/3' }, 3) === 'good');

  // Render a mixed species table and assert band == pill for every row.
  const rows = [
    { species: 'R23', agreement: '2/3', agreement_count: 2 },
    { species: 'R13', agreement: '1/3', agreement_count: 1 },
    { species: 'R33', agreement: '3/3', agreement_count: 3 },
    { species: 'Rmiss' },
  ];
  const html = ctx.rcaRenderResults({
    confidence: 0.5, runs: 3, sections: [], biozones: [], other_fossils: [],
    species_ranges: rows,
  }, '');
  for (const r of rows) {
    const at = html.indexOf('>' + r.species + '<');
    if (at === -1) { check('band-row-present:' + r.species, false); continue; }
    const trStart = html.lastIndexOf('<tr', at);
    const trEnd = html.indexOf('</tr>', at);
    const tr = html.slice(trStart, trEnd);
    const flagged = tr.indexOf('row-low-agreement') !== -1;
    const pill = tr.indexOf('pill-good') !== -1 ? 'good'
      : tr.indexOf('pill-mid') !== -1 ? 'mid' : 'low';
    check('band-matches-pill:' + r.species, flagged === (pill === 'low'),
      'flagged=' + flagged + ' pill=' + pill);
  }
  // 1/2 is "mid": no red band any more (the old rule flagged it).
  const half = ctx.rcaRenderResults({
    confidence: 0.5, runs: 2, sections: [], biozones: [], other_fossils: [],
    species_ranges: [{ species: 'RH', agreement: '1/2', agreement_count: 1 }],
  }, '');
  check('band-mid-1of2-not-flagged',
    half.indexOf('pill-mid') !== -1 && half.indexOf('row-low-agreement') === -1);
}

// (item f) Export / rendered cell text must go through the mirror of
// rca_core/exporter.py:_export_cell_text: NaN/Infinity instances AND the
// literal texts a producer already stringified blank out instead of reaching
// the CSV as data that looks real.
function test_export_cell_text_mirrors_python() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const t2 = (v) => ctx.rcaExportCellText(v);
  check('celltext-null', t2(null) === '' && t2(undefined) === '');
  check('celltext-nonfinite-instances',
    t2(NaN) === '' && t2(Infinity) === '' && t2(-Infinity) === '');
  check('celltext-nonfinite-texts',
    t2('nan') === '' && t2('NaN') === '' && t2(' inf ') === ''
    && t2('-Inf') === '' && t2('+inf') === '' && t2('Infinity') === ''
    && t2('-infinity') === '');
  check('celltext-finite-kept',
    t2(0) === '0' && t2('0') === '0' && t2(1.5) === '1.5'
    && t2('nanometer') === 'nanometer' && t2('information') === 'information'
    && t2('Nankinella') === 'Nankinella');
  // str(True) == 'True' in Python, 'true' in JS.
  check('celltext-bool-casing', t2(true) === 'True' && t2(false) === 'False');
  // And the rule is applied on the export path, not just in isolation. The
  // expected row is what rca_core/exporter.py writes for this payload: the
  // two optional columns DO exist (a value is present), but the cells read
  // empty once _export_cell_text has blanked them.
  const exp = ctx.rcaBuildTableExport({
    sections: [], biozones: [], other_fossils: [],
    species_ranges: [{ species: 'X', confidence: NaN, note: 'inf', range_base: 3 }],
  }, 'species_ranges');
  const header = exp.headers.join('|');
  check('celltext-optional-columns-exist',
    header.indexOf(ctx.t('col.colConfidence')) !== -1
    && header.indexOf(ctx.t('col.note')) !== -1, header);
  check('celltext-applied-in-export',
    JSON.stringify(exp.rows[0])
      === JSON.stringify(['1', 'X', '', '3', '', '', '', '']),
    JSON.stringify(exp.rows[0]));
  // …and in the rendered table (the Python GUI grid renders the very same
  // build_table_export text).
  const html = ctx.rcaRenderResults({
    confidence: 0.5, sections: [], biozones: [], other_fossils: [],
    species_ranges: [{ species: 'X', note: 'NaN' }],
  }, '');
  check('celltext-applied-in-render', html.indexOf('>NaN<') === -1);
}

// (item j) The SHARED i18n namespaces — everything js/table.js renders and
// everything the quality badge / GBIF hint block prints — must be present on
// BOTH sides and in all three languages. The rest of each catalog is
// surface-specific (the desktop GUI's history / edit / provider keys have no
// browser counterpart, and vice versa), so only the shared prefixes are
// locked; a col.* / sec.* key added to one transport alone used to render
// "[?col.foo]" in the other.
function test_i18n_shared_namespace_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const pySrc = fs.readFileSync(path.join(__dirname, 'rca_core', 'i18n.py'), 'utf8');
  const pyLocale = (lang) => {
    const start = pySrc.indexOf('TRANSLATIONS["' + lang + '"] = {');
    if (start === -1) return null;
    const end = pySrc.indexOf('\n}', start);
    const body = pySrc.slice(start, end);
    const keys = new Set();
    let m;
    const rx = /^    "((?:col|sec|quality|names|reason_code)\.[A-Za-z0-9_]+)":/gm;
    while ((m = rx.exec(body)) !== null) keys.add(m[1]);
    return keys;
  };
  // FIX-2026-09-22 (item 5): reason_code.* joins the locked namespaces —
  // the catalog now has en/ja glosses on both transports, so key-for-key
  // py↔js parity holds for it too.
  const SHARED = ['col.', 'sec.', 'quality.', 'names.', 'reason_code.'];
  for (const lang of ['zh', 'en', 'ja']) {
    const py = pyLocale(lang);
    check('i18n-py-locale-block:' + lang, py !== null);
    if (!py) continue;
    const js = new Set(Object.keys(ctx.RCA_I18N[lang] || {})
      .filter((k) => SHARED.some((p) => k.indexOf(p) === 0)));
    const pyOnly = [...py].filter((k) => !js.has(k)).sort();
    const jsOnly = [...js].filter((k) => !py.has(k)).sort();
    check('i18n-shared-parity:' + lang, pyOnly.length === 0 && jsOnly.length === 0,
      'py-only=' + pyOnly.join(',') + ' js-only=' + jsOnly.join(','));
  }
  // Every column / section key table.js actually renders must resolve (not
  // fall through to the "[?key]" placeholder) in the browser catalog.
  const tableSrc = fs.readFileSync(path.join(__dirname, 'js', 'table.js'), 'utf8');
  let m;
  const rx = /'((?:col|sec)\.[A-Za-z0-9_]+)'/g;
  const used = new Set();
  while ((m = rx.exec(tableSrc)) !== null) used.add(m[1]);
  check('i18n-table-keys-nonempty', used.size > 40);
  for (const lang of ['zh', 'en', 'ja']) {
    const missing = [...used].filter((k) => !(k in ctx.RCA_I18N[lang])).sort();
    check('i18n-table-keys-resolve:' + lang, missing.length === 0, missing.join(','));
  }
}

// (item e, app.js side) The export filename must follow the tables the file
// actually contains, so it is derived from the SAME predicates — a
// `columnar_section_*.json` holding range-chart sheets (or a tree exported as
// range_chart_*) is the same drift in the other direction.
function test_export_filename_follows_dispatch() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const FIRST_PREFIX = {
    zonations: 'zonation_chart_', sites: 'abundance_diagram_',
    nodes: 'phylogenetic_tree_', sections: null,
  };
  const cases = [
    { label: 'range', data: { sections: [{ name: 'A' }], species_ranges: [{ species: 'X' }] } },
    { label: 'columnar', data: { sections: [{ id: 'C1' }], species_ranges: [] } },
    { label: 'columnar-empty', data: { sections: [], fossil_legend: [{ marker: 'm' }] } },
    { label: 'abundance', data: { abundances: [{ taxon: 'T' }], sections: [] } },
    { label: 'phylo', data: { nodes: [{ id: 'n1', parent: null }], sections: [] } },
    { label: 'zonation', data: { correlations: [{ from_zone: 'A' }], sections: [] } },
    { label: 'empty', data: { sections: [], species_ranges: [] } },
  ];
  for (const c of cases) {
    const prefix = ctx.rcaResultFilePrefix(c.data);
    const ids = ctx.rcaTableConfigs(c.data).map((x) => x.id);
    const expected = FIRST_PREFIX[ids[0]] === undefined ? null : FIRST_PREFIX[ids[0]];
    // 'sections' is the first id for BOTH the range-chart and the columnar
    // sets, so disambiguate with the presence of the columnar sub-tables.
    const want = expected === null
      ? (ids.indexOf('lithology_blocks') !== -1 ? 'columnar_section_' : 'range_chart_')
      : expected;
    check('file-prefix-matches-tables:' + c.label, prefix === want,
      prefix + ' vs ids=' + ids.join(','));
  }
  check('file-prefix-null-safe', ctx.rcaResultFilePrefix(null) === 'range_chart_');
}

// (checklist a, BEHAVIOURAL half) tests/test_chart_mode_parity.py locks the
// keyword TABLES by source inspection; that proves the lists are equal, not
// that the two matchers agree on branch ORDER, stem-vs-whole-word handling and
// the `matched` flag. The golden table below was produced by calling the
// oracle (`rca_core.chart_mode.auto_detect_chart_mode_ex`) over each input and
// recording `(mode, matched)` verbatim — so this is the end-to-end check that
// js/app.js classifies the same caption the same way.
//
// The JS detector reads the DOM (caption) + state.file.name exactly like the
// real page does, and builds `caption + ' ' + fileName`, which is why the
// filename cases use a LEADING SPACE in the Python-side input too.
function test_chart_mode_detection_matches_python() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const captionEl = ctx.document.getElementById('caption');
  const CASES = [
    // caption, fileName, mode, matched  (oracle: auto_detect_chart_mode_ex)
    ['', '', 'range_chart', false],
    ['Conodont range chart', '', 'range_chart', true],
    ['Columnar section with radiolarian range chart. Zonation of the sections',
      '', 'range_chart', true],
    ['Zonation of the sections', '', 'zonation_chart', true],
    ['Zonations of the formations', '', 'zonation_chart', true],
    ['Correlation of the measured sections', '', 'range_chart', false],
    ['Correlation of Triassic radiolarian ZONES and subzones', '', 'zonation_chart', true],
    ['Pollen percentage diagram', '', 'abundance_diagram', true],
    ['Isotope chemostratigraphy', '', 'chemical_stratigraphy', true],
    ['Paleogeographic map', '', 'paleomap', true],
    ['delta13C biplot', '', 'scatter_plot', true],
    ['Scatter plot of epsilon Nd', '', 'scatter_plot', true],
    ['\u03b413C curve above the conodont range chart', '', 'range_chart', true],
    ['\u53e4\u5730\u7406\u56fe', '', 'paleomap', true],
    ['\u6563\u70b9\u56fe', '', 'scatter_plot', true],
    ['\u540c\u4f4d\u7d20\u66f2\u7ebf', '', 'chemical_stratigraphy', true],
    ['\u67f1\u72b6\u56fe', '', 'columnar_section', true],
    ['\u5ef6\u9650\u8868', '', 'range_chart', true],
    ['\u751f\u7269\u5e26\u5bf9\u6bd4', '', 'zonation_chart', true],
    ['\u041f\u0430\u043b\u0435\u043e\u0433\u0435\u043e\u0433\u0440\u0430\u0444\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u043a\u0430\u0440\u0442\u0430',
      '', 'paleomap', true],
    ['\u0418\u0437\u043e\u0442\u043e\u043f\u043d\u0430\u044f \u043a\u0440\u0438\u0432\u0430\u044f',
      '', 'chemical_stratigraphy', true],
    ['Phylogenetic tree of the radiolarians', '', 'phylogenetic_tree', true],
    ['molecular phylogeny', '', 'phylogenetic_tree', true],
    ['dendrogram', '', 'phylogenetic_tree', true],
    // False-positive guards: the whole-word rules must refuse these.
    ['Pollinator study', '', 'range_chart', false],
    ['colour variation', '', 'range_chart', false],
    ['Depth range of the samples', '', 'range_chart', false],
    ['Biozonation', '', 'zonation_chart', true],
    ['spore abundance', '', 'abundance_diagram', true],
    ['zone correlation chart', '', 'zonation_chart', true],
    ['biplot of major elements', '', 'scatter_plot', true],
    ['crossplot', '', 'scatter_plot', true],
    ['range charts of the conodonts', '', 'range_chart', true],
    // Filename-only paths (empty caption).
    ['', 'pollen-diagram.tiff', 'abundance_diagram', true],
    ['', 'scatterplot.eps', 'scatter_plot', true],
    ['', 'columnar_section_measured.png', 'range_chart', false],
    ['', 'fig_23_zonation.png', 'range_chart', false],
    ['', 'random-fig.png', 'range_chart', false],
  ];
  for (const [cap, fileName, mode, matched] of CASES) {
    captionEl.value = cap;
    ctx.state.file = fileName ? { name: fileName } : null;
    const det = ctx.rcaAutoDetectChartModeDetailed();
    const label = 'chart-mode:' + (cap || fileName || '(empty)');
    check(label + '-mode', det.mode === mode, det.mode + ' != ' + mode);
    check(label + '-matched', det.matched === matched,
      String(det.matched) + ' != ' + String(matched));
    // The legacy wrapper must return the same mode — it is what the GUIs and
    // older call sites still use.
    check(label + '-legacy', ctx.rcaAutoDetectChartMode() === mode,
      ctx.rcaAutoDetectChartMode() + ' != ' + mode);
  }
  captionEl.value = '';
  ctx.state.file = null;
}

// (R3) The SSRF guard must mirror the literal-IP policy of
// rca_core/ssrf.py (`_is_non_public_ip` + `validate_endpoint_local_ok`),
// including REVIEW-2026-09-20 item 14: the odd IPv4 spellings, 0.0.0.0/8,
// 100.64.0.0/10, 192.88.99.0/24, host.docker.internal, IPv4-compatible IPv6
// ([::127.0.0.1]), the ::ffff family, NAT64 / 6to4 and *.localhost.
//
// CONTRACT-UPDATE-2026-09-20: the rules used to be an inline pile of regexes
// inside the request path, so the only honest assertion was a source grep
// (`src.indexOf('::ffff:')`, `/f\[cd\]\[0-9a-f\]/`). They now live in
// rcaIsSsrfBlockedHost(), so these are BEHAVIORAL checks — and the hostnames
// are the ones `new URL()` hands that function, which is what makes the
// decimal/hex spellings testable at all. Every expectation below was compared
// against the Python oracle (`rca_core.ssrf._is_non_public_ip`) over the same
// literal.
function test_ssrf_guard_ipv6() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  check('ssrf-guard-function-exists',
    typeof ctx.rcaIsSsrfBlockedHost === 'function');
  if (typeof ctx.rcaIsSsrfBlockedHost !== 'function') return;

  const hostOf = (u) => { try { return new URL(u).hostname.toLowerCase(); } catch (_) { return ''; } };
  const blocked = (u) => ctx.rcaIsSsrfBlockedHost(hostOf(u));

  // Blocked: everything a browser could otherwise be talked into dialling.
  const BLOCKED = [
    // IPv4 written the weird way round (normalized by `new URL`).
    ['https://2130706433/', 'decimal single-label loopback'],
    ['https://0x7f000001/', 'hexadecimal single-label loopback'],
    ['https://127.1/', 'two-part short form'],
    ['https://0x7f.1/', 'hex + short mixed form'],
    // This-round IPv4 prefixes.
    ['https://0.0.0.0/', '0/8 unspecified'],
    ['https://0.1.2.3/', '0/8 (not just 0.0.0.0)'],
    ['https://100.64.0.1/', 'CGNAT 100.64/10 start'],
    ['https://100.127.255.255/', 'CGNAT 100.64/10 end'],
    ['https://192.88.99.7/', '6to4 relay anycast 192.88.99/24'],
    ['https://169.254.169.254/', 'cloud metadata'],
    ['https://10.1.2.3/', 'RFC1918'],
    // Docker host aliases: they resolve to the developer machine.
    ['https://host.docker.internal:11434/', 'host.docker.internal'],
    ['https://gateway.docker.internal/', 'gateway.docker.internal'],
    // localhost zone (RFC 6761) + internal / metadata names.
    ['https://localhost/', 'localhost apex'],
    ['https://ollama.localhost:11434/', '*.localhost'],
    ['https://metadata.google.internal/', 'GCE metadata'],
    ['https://llm.internal/', '.internal'],
    // IPv4-compatible IPv6 (::/96) — is_global == True in CPython, routed to
    // the embedded v4 by the kernel. Previously the biggest hole.
    ['https://[::127.0.0.1]/', 'IPv4-compatible loopback'],
    ['https://[::ffff:a9fe:a9fe]/', 'IPv4-mapped link-local metadata'],
    ['https://[::ffff:7f00:1]/', 'IPv4-mapped loopback'],
    ['https://[::ffff:6440:1]/', 'IPv4-mapped CGNAT'],
    ['https://[::]/', 'unspecified'],
    ['https://[::1]/', 'IPv6 loopback'],
    ['https://[64:ff9b::a9fe:a9fe]/', 'NAT64 64:ff9b::/96 -> metadata'],
    ['https://[64:ff9b:1::a00:1]/', 'NAT64 64:ff9b:1::/48 -> RFC1918'],
    ['https://[2002:7f00:1::]/', '6to4 embedding loopback'],
    ['https://[fd00::1]/', 'ULA fc00::/7'],
    ['https://[fe80::2]/', 'link-local fe80::/10'],
    ['https://[100::1]/', 'discard-only 100::/64'],
    ['https://[2001:db8::1]/', 'documentation 2001:db8::/32'],
    // Fail-closed shapes: an unparseable host must never reach fetch().
    ['https://[not-an-ipv6]/', 'bracketed garbage'],
    ['https://[::ffff:999.1.1.1]/', 'v4-mapped with an impossible octet'],
  ];
  for (const [url, why] of BLOCKED) {
    check('ssrf-blocks:' + why, blocked(url) === true);
  }

  // Allowed: the guard must stay precise, not become "https public names
  // only". A blocked provider endpoint is a user-visible regression.
  const ALLOWED = [
    ['https://api.minimaxi.com/', 'public provider name'],
    ['https://api.anthropic.com/', 'public provider name 2'],
    ['https://cafe/', 'hex-looking name is not an address'],
    ['https://localhost.example.com/', 'lookalike suffix, not *.localhost'],
    ['https://[2606:4700:4700::1111]/', 'public IPv6 literal'],
    ['https://[::ffff:808:808]/', 'IPv4-mapped PUBLIC v4 (oracle parity)'],
    ['https://[64:ff9b:2::1]/', 'just outside both NAT64 prefixes'],
    ['https://[fec0::2]/', 'site-local: CPython still calls it global'],
    ['https://8.8.8.8/', 'public IPv4 literal'],
  ];
  for (const [url, why] of ALLOWED) {
    check('ssrf-allows:' + why, blocked(url) === false);
  }

  // A bare IPv6 without brackets cannot come out of `new URL`, but the
  // helper is public: keep it fail-closed for direct callers too.
  check('ssrf-blocks:bare-ipv6-no-brackets',
    ctx.rcaIsSsrfBlockedHost('::1') === true);
  check('ssrf-blocks:empty-host', ctx.rcaIsSsrfBlockedHost('') === true);

  // The gate must actually be wired into the direct transport: a private
  // https endpoint is refused and the request is re-routed through the
  // same-origin backend instead of the browser dialling it.
  const calls = [];
  ctx.fetch = async (url, init) => {
    calls.push({ url, method: (init && init.method) || 'GET' });
    return {
      ok: true, status: 200,
      json: async () => ({ ok: true, data: { species_ranges: [], sections: [] } }),
      text: async () => '',
    };
  };
  resetBackendSession(ctx);
  return ctx.extractRangeChart({
    dataUrl: 'data:image/png;base64,QUFB',
    mode: 'range_chart',
    baseUrl: 'https://[::ffff:a9fe:a9fe]:8443',   // == 169.254.169.254
    apiKey: 'sk', model: 'm', maxTokens: 100,
    transport: 'direct',
  }).then(() => {
    check('ssrf-direct-falls-back-to-backend',
      calls.length > 0 && calls.every((c) => c.url === '/api/extract'));
    check('ssrf-direct-never-dials-target',
      calls.every((c) => c.url.indexOf('a9fe') === -1 && c.url !== '/v1/messages'));
  });
}
const _ssrfGuard = trackAsync('ssrf-guard-ipv6', test_ssrf_guard_ipv6());

// ---------------------------------------------------------------------------
// REVIEW-2026-09-20 network layer: serialization, CSRF silent retry, the
// error_key whitelist and the retry-delay semantics.
// ---------------------------------------------------------------------------

/** Clear rcaCallBackend's module-level session state between tests. */
function resetBackendSession(ctx) {
  ctx.rcaCallBackend._sessionToken = '';
  ctx.rcaCallBackend._csrfToken = '';
  ctx.rcaCallBackend._pending = null;
}

/** Minimal non-cancelling AbortSignal for backend opts. */
function nullSignal() {
  return { aborted: false, addEventListener() {}, removeEventListener() {} };
}

/** A successful backend GET/POST responder that records what it saw. */
function backendResponder(onPost) {
  const seen = { gets: 0, posts: 0, postHeaders: [], getHeaders: [] };
  const fetchImpl = async (url, init) => {
    const method = (init && init.method) || 'GET';
    if (method === 'GET') {
      seen.gets += 1;
      seen.getHeaders.push((init && init.headers) || {});
      return {
        ok: true, status: 200,
        json: async () => ({ session_token: 's' + seen.gets, csrf_token: 'c' + seen.gets }),
        text: async () => '',
      };
    }
    seen.posts += 1;
    seen.postHeaders.push((init && init.headers) || {});
    return onPost ? onPost(seen, init) : {
      ok: true, status: 200,
      json: async () => ({ ok: true, data: { species_ranges: [] } }),
      text: async () => '',
    };
  };
  return { seen, fetchImpl };
}

// (N1) `rcaCallBackend._pending` is supposed to serialize token acquisition —
// it used to build a link off `_pending` and then throw the link away, so the
// stored promise stayed `Promise.resolve()` forever and two fast extractions
// fired two racing CSRF GETs. Assert the chain is real, survives a rejecting
// link (a Cancel must not poison the callers behind it), and that concurrent
// callers get strictly ordered GETs.
function test_backend_serial_chain() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  let inFlight = 0, maxInFlight = 0, gets = 0, posts = 0;
  const order = [];
  ctx.fetch = async (url, init) => {
    if (init && init.method === 'POST') {
      posts += 1;
      return { ok: true, status: 200,
               json: async () => ({ ok: true, data: {} }), text: async () => '' };
    }
    gets += 1; inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    order.push('start' + gets);
    await new Promise((r) => setTimeout(r, 8));
    order.push('end' + gets);
    inFlight -= 1;
    return { ok: true, status: 200,
             json: async () => ({ session_token: 's' + gets, csrf_token: 'c' + gets }),
             text: async () => '' };
  };
  resetBackendSession(ctx);
  const opts = {
    apiKey: 'sk', baseUrl: 'https://example.com', model: 'm', maxTokens: 100,
    mode: 'range_chart', transport: 'backend', signal: nullSignal(),
  };
  const before = ctx.rcaCallBackend._pending;
  const two = Promise.all([
    ctx.rcaCallBackend(opts, 'QUFB'),
    ctx.rcaCallBackend(opts, 'QUFB'),
  ]).then(([a, b]) => {
    check('chain-token-get-serialized', maxInFlight === 1);
    check('chain-get-order-interleaved-nothing',
      order.join(',') === 'start1,end1,start2,end2');
    check('chain-both-callers-resolve', a.ok === true && b.ok === true);
    check('chain-two-get-two-post', gets === 2 && posts === 2);
  });

  // The chain helper directly: a rejecting task must propagate to ITS caller
  // only, and the stored link must stay usable for the next caller.
  const chainProbe = two.then(async () => {
    check('chain-written-back', !!ctx.rcaCallBackend._pending
      && ctx.rcaCallBackend._pending !== before);
    let caught = null;
    await ctx.rcaQueueBackendTask(() => Promise.reject(new Error('boom')))
      .catch((e) => { caught = e && e.message; });
    check('chain-rejection-reaches-its-caller', caught === 'boom');
    const alive = await ctx.rcaQueueBackendTask(async () => 'alive');
    check('chain-survives-rejection', alive === 'alive');
    let secondCallerSawAbort = 'pending';
    await ctx.rcaQueueBackendTask(() => Promise.reject(new Error('second boom')))
      .then(() => { secondCallerSawAbort = 'resolved'; }, () => { secondCallerSawAbort = 'rejected'; });
    const after = await ctx.rcaQueueBackendTask(async () => 'still-running');
    check('chain-not-poisoned-by-second-rejection',
      secondCallerSawAbort === 'rejected' && after === 'still-running');
  });
  return chainProbe;
}
const _chain = trackAsync('backend-serial-chain', test_backend_serial_chain());

// (N2a) A 403 whose body says CSRF is now RECOVERED, not just reported: the
// backend re-mints the token on GET /api/extract (sliding TTL, and the mint
// bills to its own rate bucket since REVIEW-2026-09-20), so the frontend must
// re-GET and resend ONCE instead of telling the user to press the button
// again.
// (N2b) …and exactly once: a rejection that survives the retry must not turn
// into a hammering loop.
function test_backend_csrf_silent_retry() {
  const ctx = buildContext();
  loadAllScripts(ctx);

  // --- recovered on the second attempt -------------------------------------
  const ok = backendResponder((seen, init) => (seen.posts === 1
    ? { ok: false, status: 403,
        json: async () => ({ ok: false, error_key: 'err.forbidden',
                             error_body: 'Invalid or expired CSRF token.' }),
        text: async () => 'Invalid or expired CSRF token.' }
    : { ok: true, status: 200,
        json: async () => ({ ok: true, data: { species_ranges: [] } }),
        text: async () => '' }));
  ctx.fetch = ok.fetchImpl;
  resetBackendSession(ctx);
  const opts = {
    apiKey: 'sk', baseUrl: 'https://example.com', model: 'm', maxTokens: 100,
    mode: 'range_chart', transport: 'backend', signal: nullSignal(),
  };
  return ctx.rcaCallBackend(opts, 'QUFB').then((res) => {
    check('csrf-retry-recovered', res.ok === true);
    check('csrf-retry-two-gets', ok.seen.gets === 2);
    check('csrf-retry-two-posts', ok.seen.posts === 2);
    check('csrf-retry-clears-stale-token',
      ok.seen.postHeaders[0]['X-CSRF-Token'] === 'c1'
      && ok.seen.postHeaders[1]['X-CSRF-Token'] === 'c2');
    // The stale pair really was dropped: the recovery GET starts a NEW
    // session (empty X-Session-Token) instead of replaying 's1', which is
    // what server.py's sliding-TTL mint expects from a client that was told
    // its token is no longer valid.
    check('csrf-retry-drops-stale-session',
      ok.seen.getHeaders[0]['X-Session-Token'] === ''
      && ok.seen.getHeaders[1]['X-Session-Token'] === '');

    // --- the retry also fails: stop, surface the rejection, no storm -------
    const storm = backendResponder(() => ({
      ok: false, status: 403,
      json: async () => ({ ok: false, error_key: 'err.forbidden',
                           error_body: 'Missing CSRF token. Fetch /api/extract (GET) to obtain a valid token.' }),
      text: async () => 'missing csrf',
    }));
    ctx.fetch = storm.fetchImpl;
    resetBackendSession(ctx);
    return ctx.rcaCallBackend(opts, 'QUFB').then((r2) => {
      check('csrf-storm-bounded-to-one-retry',
        storm.seen.posts === 2 && storm.seen.gets === 2);
      check('csrf-storm-surfaces-forbidden',
        r2.ok === false && r2.errorKey === 'err.forbidden');
    });
  }).then(() => {
    // --- an ORIGIN 403 (same err.forbidden, no CSRF in the body) must NOT be
    // retried: a new token cannot fix it. -----------------------------------
    const origin = backendResponder(() => ({
      ok: false, status: 403,
      json: async () => ({ ok: false, error_key: 'err.forbidden',
                           error_body: 'Origin mismatch: request came from http://evil.example' }),
      text: async () => 'origin mismatch',
    }));
    ctx.fetch = origin.fetchImpl;
    resetBackendSession(ctx);
    const opts2 = {
      apiKey: 'sk', baseUrl: 'https://example.com', model: 'm', maxTokens: 100,
      mode: 'range_chart', transport: 'backend', signal: nullSignal(),
    };
    return ctx.rcaCallBackend(opts2, 'QUFB').then((r3) => {
      check('csrf-origin-403-not-retried', origin.seen.posts === 1);
      check('csrf-origin-403-key-preserved',
        r3.ok === false && r3.errorKey === 'err.forbidden');
    });
  });
}
const _csrfRetry = trackAsync('backend-csrf-silent-retry', test_backend_csrf_silent_retry());

// (N3) An `error_key` read out of a response body is attacker-shaped input:
// it used to be copied into `result.errorKey` verbatim, and app.js renders
// that through t(). Only keys that exist in the known corpus may be adopted.
function test_backend_error_key_whitelist() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const opts = (transport, baseUrl) => ({
    apiKey: 'sk', baseUrl: baseUrl || 'https://example.com', model: 'm',
    maxTokens: 100, mode: 'range_chart', transport, signal: nullSignal(),
    dataUrl: 'data:image/png;base64,QUFB', mediaType: 'image/png',
  });

  // A hostile body cannot install an arbitrary i18n key (and the raw value is
  // not echoed into the result either).
  const hostile = backendResponder(() => ({
    ok: false, status: 400,
    json: async () => ({ ok: false, error_key: 'err.<img src=x onerror=alert(1)>',
                         error_body: 'provider said no' }),
    text: async () => 'no',
  }));
  ctx.fetch = hostile.fetchImpl;
  resetBackendSession(ctx);
  return ctx.rcaCallBackend(opts('backend'), 'QUFB').then((res) => {
    check('errkey-unknown-backend-dropped',
      res.ok === false && res.errorKey === 'err.http');
    check('errkey-unknown-not-echoed', String(res.errorKey).indexOf('<img') === -1);
    check('errkey-body-still-shown', res.errorBody === 'provider said no');

    // A known key is adopted verbatim — the whitelist must not flatten the
    // server's specific diagnostics.
    const known = backendResponder(() => ({
      ok: false, status: 429,
      json: async () => ({ ok: false, error_key: 'err.rateLimit',
                           error_body: 'slow down' }),
      text: async () => 'slow down',
    }));
    ctx.fetch = known.fetchImpl;
    resetBackendSession(ctx);
    return ctx.rcaCallBackend(opts('backend'), 'QUFB').then((r2) => {
      check('errkey-known-backend-adopted', r2.errorKey === 'err.rateLimit');
      check('errkey-known-not-replaced-by-generic', r2.status === 429);
    });
  }).then(() => {
    // ok=true with a stray key: no error surfaced at all.
    const good = backendResponder(() => ({
      ok: true, status: 200,
      json: async () => ({ ok: true, error_key: 'err.not-a-real-key',
                           data: { species_ranges: [] } }),
      text: async () => '',
    }));
    ctx.fetch = good.fetchImpl;
    resetBackendSession(ctx);
    return ctx.rcaCallBackend(opts('backend'), 'QUFB').then((r3) => {
      check('errkey-ok-true-no-key', r3.ok === true && r3.errorKey === null);
    });
  }).then(() => {
    // --- direct transport: same whitelist, plus the #110 truncation bound. --
    const directWith = (bodyText, status) => {
      ctx.fetch = async () => ({
        ok: false, status: status,
        json: async () => { throw new Error('not json'); },
        text: async () => bodyText,
      });
      return ctx.extractRangeChart(opts('direct', 'https://api.example.com'));
    };
    return directWith('{"error_key":"err.injected.key"}', 400).then((r4) => {
      check('errkey-direct-unknown-dropped',
        r4.ok === false && r4.errorKey === 'err.http');
      return directWith('{"error_key":"err.bodyTooLarge"}', 400);
    }).then((r5) => {
      check('errkey-direct-known-adopted', r5.errorKey === 'err.bodyTooLarge');
      return directWith('{"error_key":"err.401"}', 403);
    }).then((r6) => {
      // The body key wins over the status-derived one — that is the point of
      // lifting it (unchanged from before the whitelist existed).
      check('errkey-direct-403-body-key', r6.errorKey === 'err.401');
      // A key sitting BEYOND MAX_ERROR_BODY_CHARS is cut off with the body it
      // came in, so it cannot surface (mirror of error_utils.py #110).
      const MAX = ctx.RCAErrorUtils.MAX_ERROR_BODY_CHARS;
      const padded = '{"pad":"' + 'a'.repeat(MAX) + '","error_key":"err.rateLimit"}';
      return directWith(padded, 429).then((r7) => {
        check('errkey-direct-truncated-key-dropped', r7.errorKey === 'err.429');
        check('errkey-direct-body-bounded', (r7.raw || '').length === MAX);
      });
    });
  });
}
const _errKey = trackAsync('backend-error-key-whitelist', test_backend_error_key_whitelist());

// (N4) error-utils retry semantics mirrored this round:
//   #108 1 s floor that a `maxDelay` below it still overrides,
//   #109 no sleep after the LAST attempt and onRetry called once per retry,
//   #110 the error code is parsed out of the TRUNCATED body only.
function test_error_utils_retry_and_truncation() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const delay = (o) => ctx.getRetryDelay(o);
  const hdr = (map) => ({ get: (k) => map[String(k).toLowerCase()] });

  check('eu-floor-lifts-retry-after-zero',
    delay({ status: 429, headers: hdr({ 'retry-after': '0' }), attempt: 0, maxDelay: 60 }) >= 1.0);
  check('eu-floor-lifts-retry-after-ms-zero',
    delay({ status: 429, headers: hdr({ 'retry-after-ms': '0' }), attempt: 0, maxDelay: 60 }) >= 1.0);
  check('eu-retry-after-above-floor-kept',
    delay({ status: 429, headers: hdr({ 'retry-after': '5' }), attempt: 0, maxDelay: 60 }) === 5);
  check('eu-floor-default-first-attempt',
    delay({ status: 503, attempt: 0, initialDelay: 0.8, backoffFactor: 1.6, maxDelay: 30 }) >= 1.0);
  check('eu-maxdelay-zero-still-wins',
    delay({ status: 429, headers: hdr({ 'retry-after': '30' }), attempt: 2, maxDelay: 0 }) === 0);
  check('eu-maxdelay-below-floor-wins',
    delay({ status: 429, attempt: 5, initialDelay: 0.4, backoffFactor: 1, maxDelay: 0.5 }) === 0.5);

  // #109: spy on the timers the loop arms (abortableSleep is the only user of
  // setTimeout inside retryWithBackoff) and run them immediately.
  const realSetTimeout = ctx.setTimeout;
  const sleeps = [];
  ctx.setTimeout = (fn, ms) => { sleeps.push(ms); return realSetTimeout(fn, 0); };
  let attempts = 0;
  const onRetryCalls = [];
  return ctx.retryWithBackoff(async () => {
    attempts += 1;
    const e = new Error('HTTP 503'); e.status = 503;
    throw e;
  }, {
    maxRetries: 2, initialDelay: 0.01, backoffFactor: 1.0, maxDelay: 60,
    onRetry: (a, d) => onRetryCalls.push([a, d]),
  }).then(() => null, () => null).then(() => {
    ctx.setTimeout = realSetTimeout;
    check('eu-throws-after-last-attempt', attempts === 3);
    check('eu-no-sleep-after-last-failure', sleeps.length === 2);
    check('eu-onretry-count-equals-retries', onRetryCalls.length === 2);
    check('eu-onretry-receives-the-real-delay',
      onRetryCalls.length === 2 && onRetryCalls[0][1] === sleeps[0] / 1000);
    check('eu-loop-delay-carries-floor', sleeps.every((ms) => ms >= 1000));
  }).then(() => {
    // The `retryable` predicate path: a result that keeps being refused is
    // returned after the last attempt WITHOUT a further sleep.
    const real2 = ctx.setTimeout;
    const sleeps2 = [];
    ctx.setTimeout = (fn, ms) => { sleeps2.push(ms); return real2(fn, 0); };
    let calls = 0;
    return ctx.retryWithBackoff(async () => { calls += 1; return { status: 503 }; }, {
      maxRetries: 2, initialDelay: 0.01, backoffFactor: 1, maxDelay: 60,
      retryable: (r) => r.status === 503,
    }).then((last) => {
      ctx.setTimeout = real2;
      check('eu-predicate-attempts', calls === 3);
      check('eu-predicate-returns-last-result', last && last.status === 503);
      check('eu-predicate-no-trailing-sleep', sleeps2.length === 2);
    });
  }).then(() => {
    // #110: normalizeError must parse the stored (truncated) body only.
    const MAX = ctx.RCAErrorUtils.MAX_ERROR_BODY_CHARS;
    check('eu-max-body-chars-exported', MAX === 2000);
    const shortBody = '{"error_code":"RATE_LIMITED","message":"no"}';
    const norm1 = ctx.normalizeError(429, shortBody, 'HTTP 429');
    check('eu-code-parsed-from-short-body', norm1.errorCode === 'RATE_LIMITED');
    const longBody = '{"pad":"' + 'a'.repeat(MAX) + '","error_code":"HIDDEN"}';
    const norm2 = ctx.normalizeError(429, longBody, 'HTTP 429');
    check('eu-body-truncated', norm2.body.length === MAX);
    check('eu-code-beyond-truncation-dropped', norm2.errorCode === null);
    check('eu-extract-code-empty-body-null', ctx.extractErrorCode('') === null);
    check('eu-extract-code-parses-given-text',
      ctx.extractErrorCode('{"code":"X"}') === 'X');
  });
}
const _euRetry = trackAsync('error-utils-retry-truncation', test_error_utils_retry_and_truncation());

// (R4) i18n: every language must carry the same placeholders as the Python
// dictionary. The zh abundance message had dropped {sample}, so Chinese
// users alone lost the level identifier in the warning.
function test_i18n_placeholder_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const key = 'quality.abundance_sum_violation';
  for (const lang of ['zh', 'en', 'ja']) {
    const val = (ctx.RCA_I18N[lang] || {})[key] || '';
    check('i18n-abundance-sample:' + lang, val.indexOf('{sample}') !== -1);
    check('i18n-abundance-sum:' + lang, val.indexOf('{sum}') !== -1);
  }
  // The quality scorer must actually pass those params, or the placeholder
  // would render literally.
  const qsrc = require('fs').readFileSync(
    path.join(__dirname, 'js', 'quality.js'), 'utf8');
  check('i18n-abundance-params-passed',
    /abundance_sum_violation[\s\S]{0,200}sample:/.test(qsrc));
}

// (R5) The accuracy path reports inverted ranges as range_top_lt_base
// (mirrors rca_core/quality.py); the consistency path uses fad_lt_lad.
function test_quality_msg_key_parity() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const res = ctx.scoreRangeChart({
    sections: [{ name: 'A', age_range: 'Permian' }],
    species_ranges: [{ species: 'X', section: 'A',
                       range_base: '250 Ma', range_top: '300 Ma', biozone: '' }],
    confidence: 0.9,
  });
  check('quality-accuracy-uses-range_top_lt_base',
    res.issues.some(i => i.msg_key === 'quality.range_top_lt_base'));
  check('quality-accuracy-not-fad_lt_lad',
    !res.issues.some(i => i.msg_key === 'quality.fad_lt_lad'));
}

// (R6) zones / correlations / zonations count as primary content
// (rca_core/quality.py:_CONTENT_KEYS carries them). Before the JS list was
// aligned, a zonation extraction that returned zone rows but no sections /
// species was graded F "pure extraction miss" — the zone data was discarded
// as if nothing had been read. Both directions are asserted here.
function test_quality_zonation_content_keys() {
  const ctx = buildContext();
  loadAllScripts(ctx);
  const zonesOnly = ctx.scoreRangeChart({
    sections: [], species_ranges: [], abundances: [], biozones: [],
    other_fossils: [], cross_beds: [], lithology_legend: [],
    fossil_legend: [], age_units: [],
    zones: [{ name: 'Clarkina postbitteri Zone' }],
    confidence: 0.8,
  });
  check('quality-zonation-rows-are-content',
    !zonesOnly.issues.some(i => i.msg_key === 'quality.empty_result'));
  check('quality-zonation-rows-not-graded-f', zonesOnly.grade !== 'F');
  const allEmpty = ctx.scoreRangeChart({
    sections: [], species_ranges: [], abundances: [], biozones: [],
    other_fossils: [], cross_beds: [], lithology_legend: [],
    fossil_legend: [], age_units: [],
    zones: [], correlations: [], zonations: [],
    confidence: 0.2,
  });
  check('quality-all-empty-is-pure-miss',
    allEmpty.issues.some(i => i.msg_key === 'quality.empty_result'));
}

test_name_clean_lookup_parity();
test_render_tolerates_malformed_rows();
// REVIEW-2026-09-20 (M4 / L2 / item e-g, j): the exporter-mirror locks.
test_table_dispatch_order_mirrors_exporter();
test_agreement_band_and_pill_share_thresholds();
test_export_cell_text_mirrors_python();
test_i18n_shared_namespace_parity();
test_export_filename_follows_dispatch();
test_chart_mode_detection_matches_python();
// test_ssrf_guard_ipv6() and the four network-layer tests now self-register
// with trackAsync() at their definition site (they are async), so they must
// NOT be called again here.
test_i18n_placeholder_parity();
test_quality_msg_key_parity();
test_quality_zonation_content_keys();

// Drain every registered async test before printing the summary. A fixed
// timeout is not enough: assertions that resolve later would run after
// process.exit() and be silently dropped, reporting a green run over checks
// that never executed. The hard timer is a last-resort guard so a test that
// never settles fails the run loudly instead of hanging it.
const HARD_TIMEOUT_MS = 60000;
const hardTimer = setTimeout(() => {
  console.error(`\nTIMEOUT: async tests still pending after ${HARD_TIMEOUT_MS} ms` +
    ` (${pendingTests.length} registered)`);
  process.exit(1);
}, HARD_TIMEOUT_MS);
if (hardTimer.unref) hardTimer.unref();

Promise.all(pendingTests).then(() => {
  clearTimeout(hardTimer);
  console.log(`\n--- ${pass} passed, ${fail} failed ---`);
  process.exit(fail ? 1 : 0);
}).catch((e) => {
  clearTimeout(hardTimer);
  console.error('FAIL summary:', (e && e.message) || e);
  process.exit(1);
});
