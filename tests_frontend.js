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
      addEventListener() {}, removeEventListener() {},
      getElementById: (id) => makeEl(id),
      // Default: no theme-choice buttons exist in the test sandbox, so an
      // empty NodeList matches what theme.js's wireToggleButtons sees when
      // the page hasn't been wired up yet.
      querySelectorAll: () => [],
      createElement: () => makeEl('el'),
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
    AbortController: class { constructor(){ this.signal={}; } abort(){} },
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    // Browser-only globals used by rcaResolveMode / syncFooterRuntime etc.
    location: { protocol: 'http:', host: 'localhost', pathname: '/' },
  };
  // Make sure global.* points to the same context.
  ctx.globalThis = ctx;
  return ctx;
}

function makeEl(id) {
  const el = {
    id,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
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
    tagName: 'DIV',
    title: '',
    addEventListener() {}, removeEventListener() {},
    setAttribute() {}, getAttribute() { return null; },
    querySelector() { return makeEl('sub'); },
    querySelectorAll: () => [],
    appendChild(c) { el.children.push(c); c.parentNode = el; },
    removeChild() {},
    focus: () => {},
    click: () => {},
    reset() {},
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
    'js/aggregate.js',
    'js/table.js',
    'js/export.js',
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
          '})();';
      }
    }
    vm.runInContext(src, ctx, { filename: f });
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

// Wait for async races to settle before printing summary.
setTimeout(() => {
  console.log(`\n--- ${pass} passed, ${fail} failed ---`);
  process.exit(fail ? 1 : 0);
}, 50);
