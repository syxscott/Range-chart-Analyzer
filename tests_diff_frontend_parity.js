/**
 * tests_diff_frontend_parity.js — browser mirrors vs the recorded Python oracle.
 *
 * REVIEW-2026-09-20 (frontend parity task, item 14's "generate a differential
 * fixture" suggestion, generalized over the whole extraction domain).
 *
 * The expected answers in tests/fixtures/frontend_parity_2026_09_20.json come
 * from rca_core itself — regenerate them with
 *   python tests/gen_frontend_parity_fixtures.py
 * whenever the Python side intentionally changes, and keep
 *   python -m pytest tests/test_frontend_parity_fixtures_2026_09_20.py
 * honest (it fails when the committed fixture no longer matches Python).
 *
 * Run:  node tests_diff_frontend_parity.js            (summary)
 *       node tests_diff_frontend_parity.js --verbose  (full diffs)
 *       node tests_diff_frontend_parity.js rc_dict_shaped_sections   (one case)
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'frontend_parity_2026_09_20.json');
const VERBOSE = process.argv.includes('--verbose');
const ONLY = process.argv.slice(2).filter((a) => !a.startsWith('--'));

const SCRIPTS = [
  'js/config.js',
  'js/json-utils.js',
  'js/ics_table.js',
  'js/quality.js',
  'js/aggregate.js',
  'js/minimax.js',
];

function buildContext() {
  const store = new Map();
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    TextEncoder, TextDecoder, URL,
    fetch: async () => { throw new Error('network disabled in differential test'); },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    document: {
      documentElement: {
        style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
      },
      addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
      querySelector: () => null, querySelectorAll: () => [],
      getElementById: () => null, createElement: () => ({ style: {}, appendChild() {} }),
      body: { appendChild() {}, removeChild() {}, classList: { add() {}, remove() {} } },
    },
    alert() {}, prompt: () => '', confirm: () => false,
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    navigator: { language: 'en' },
  };
  ctx.window = ctx;
  ctx.self = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const rel of SCRIPTS) {
    const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    vm.runInContext(src, ctx, { filename: rel });
  }
  // `function` declarations land on the context automatically; export helpers
  // declared with const/let that the differential cases need.
  vm.runInContext(`
    globalThis.__exp = {
      safeJsonLoads: typeof safeJsonLoads !== 'undefined' ? safeJsonLoads : null,
      normalizeResult: typeof rcaNormalizeResult !== 'undefined' ? rcaNormalizeResult : null,
      normalizeColumnar: typeof rcaNormalizeColumnarResult !== 'undefined' ? rcaNormalizeColumnarResult : null,
      normalizeAbundance: typeof rcaNormalizeAbundanceResult !== 'undefined' ? rcaNormalizeAbundanceResult : null,
      normalizeZonation: typeof rcaNormalizeZonationChartResult !== 'undefined' ? rcaNormalizeZonationChartResult : null,
      normalizePhylo: typeof rcaNormalizePhylogeneticTreeResult !== 'undefined' ? rcaNormalizePhylogeneticTreeResult : null,
      normalizeClassification: typeof rcaNormalizeChartClassification !== 'undefined' ? rcaNormalizeChartClassification : null,
      toNewick: typeof rcaToNewick !== 'undefined' ? rcaToNewick : null,
      resolveAgeBound: typeof _resolveAgeBound !== 'undefined' ? _resolveAgeBound : null,
    };
  `, ctx);
  return ctx.__exp;
}

const RUNNERS = {
  range_chart: (f, c) => f.normalizeResult(c.payload),
  columnar_section: (f, c) => f.normalizeColumnar(c.payload),
  abundance_diagram: (f, c) => f.normalizeAbundance(c.payload),
  zonation_chart: (f, c) => f.normalizeZonation(c.payload),
  phylogenetic_tree: (f, c) => f.normalizePhylo(c.payload),
  chart_classification: (f, c) => f.normalizeClassification(c.payload),
  to_newick: (f, c) => f.toNewick(f.normalizePhylo(c.payload)),
  safe_json_loads: (f, c) => f.safeJsonLoads(c.payload),
  age_bound: (f, c) => {
    const r = f.resolveAgeBound(c.payload, c.extra);
    return r === null || r === undefined ? null : { name: r.name === undefined ? null : r.name, ma: r.ma === undefined ? null : r.ma };
  },
};

// Numbers: Python 1.0 vs JS 1 (and float noise) are the same value.
function canon(value) {
  if (Array.isArray(value)) return value.map(canon);
  if (value && typeof value === 'object') {
    const out = {};
    for (const k of Object.keys(value)) out[k] = canon(value[k]);
    return out;
  }
  if (typeof value === 'number') return Number.isInteger(value) ? value : Number(value.toFixed(9));
  return value;
}

function diff(a, b, trail, out) {
  if (out.length > 12) return;
  if (a === b) return;
  if (typeof a === 'number' && typeof b === 'number'
      && Math.abs(a - b) < 1e-9) return;
  const ta = a === null ? 'null' : Array.isArray(a) ? 'array' : typeof a;
  const tb = b === null ? 'null' : Array.isArray(b) ? 'array' : typeof b;
  if (ta !== tb) { out.push(`${trail}: py=${ta}(${JSON.stringify(a)}) js=${tb}(${JSON.stringify(b)})`); return; }
  if (ta === 'array') {
    if (a.length !== b.length) {
      out.push(`${trail}: length py=${a.length} js=${b.length}`);
    }
    for (let i = 0; i < Math.min(a.length, b.length); i++) diff(a[i], b[i], `${trail}[${i}]`, out);
    return;
  }
  if (ta === 'object') {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of [...keys].sort()) {
      if (!(k in a)) { out.push(`${trail}.${k}: MISSING in python (js=${JSON.stringify(b[k])})`); continue; }
      if (!(k in b)) { out.push(`${trail}.${k}: MISSING in js (py=${JSON.stringify(a[k])})`); continue; }
      diff(a[k], b[k], `${trail}.${k}`, out);
    }
    return;
  }
  out.push(`${trail}: py=${JSON.stringify(a)} js=${JSON.stringify(b)}`);
}

// Two cases stay diverged ON PURPOSE. Both are recorded here instead of being
// papered over, so the suite still exits 0 and a future drift shows up as a NEW
// failure rather than as noise that everyone learned to skim.
//
//  * rc_dict_shaped_sections — ROW ORDER only. The payload wraps its sections
//    in a dict whose keys are "Pingdingshan", "unclear", "0". ECMAScript
//    enumerates an ordinary object's ARRAY-INDEX-like keys ("0") BEFORE the
//    string keys and in ascending numeric order, whatever the document order
//    was — and JSON.parse already lost the order, so no normalizer code can
//    recover it (only a custom order-preserving parser could). Python dicts
//    keep insertion order, so the two engines emit the same three rows in a
//    different sequence. Values, `wrapper_key` stamps and warnings all match;
//    only the row sequence differs, and only for a numeric-looking wrapper key.
//  * rc_section_formations_string — `coordinates: {"lat": 1}`.
//    rca_core/extractor.py still calls a BARE `str(v)` in six local `s()`
//    closures (normalize_result's section/species/biozone builders and friends,
//    extractor.py:902/986/1052/1108/1572/3143) while the other normalizers use
//    `_stringify_scalar`. Server-side the dict therefore leaks the Python repr
//    "{'lat': 1}" into a scientific field; js/minimax.js mirrors
//    `_stringify_scalar` there ("" for containers). Fixing this means changing
//    rca_core (out of this round's scope: the Python side is the frozen oracle)
//    or writing a full Python-repr serializer in the browser for a value no
//    renderer can use. Tracked as a contract point for the app/table owner.
const EXPECTED_DIVERGENCES = {
  rc_dict_shaped_sections: 'JS enumerates integer-like object keys first (ECMAScript ordinary-object order); row ORDER only',
  rc_section_formations_string: 'Python still calls bare str() in normalize_result; containers leak a repr where _stringify_scalar yields ""',
};

function main() {
  const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  const fns = buildContext();
  let passed = 0;
  let documented = 0;
  const failures = [];
  for (const c of fixture.cases) {
    if (ONLY.length && !ONLY.includes(c.id) && !ONLY.includes(c.group)) continue;
    const runner = RUNNERS[c.group];
    if (!runner) { failures.push(`${c.id}: no JS runner for group ${c.group}`); continue; }
    const notes = [];
    let got; let gotError = null;
    const input = c.payload === null || c.payload === undefined
      ? c.payload : JSON.parse(JSON.stringify(c.payload));
    try {
      got = runner(fns, { payload: input, extra: c.extra });
    } catch (err) {
      gotError = err && err.message ? String(err.message) : String(err);
    }
    if (c.python_error) {
      if (gotError === null) {
        failures.push(`${c.id} [${c.group}]: python raised ${c.python_error}(${c.python_message}), js returned ${JSON.stringify(got)}`);
        continue;
      }
      notes.push(`both raise (py:${c.python_error})`);
      passed += 1;
      if (VERBOSE) console.log(`PASS ${c.id} ${notes.join(' ')}`);
      continue;
    }
    if (gotError !== null) {
      failures.push(`${c.id} [${c.group}]: js threw ${gotError}`);
      continue;
    }
    const a = canon(c.python);
    const b = canon(got);
    const d = [];
    diff(a, b, '', d);
    if (d.length) {
      if (EXPECTED_DIVERGENCES[c.id]) {
        documented += 1;
        console.log(`SKIP ${c.id} [${c.group}] — ${EXPECTED_DIVERGENCES[c.id]}`);
        for (const line of d) console.log('     ' + line);
      } else {
        failures.push(`${c.id} [${c.group}]\n    ${d.join('\n    ')}`);
      }
    } else {
      passed += 1;
      if (VERBOSE) console.log(`PASS ${c.id}`);
    }
  }
  console.log(`\n--- differential parity: ${passed} matched, ${documented} documented, `
              + `${failures.length} diverged ---`);
  for (const f of failures) console.log('FAIL ' + f);
  process.exit(failures.length ? 1 : 0);
}

main();
