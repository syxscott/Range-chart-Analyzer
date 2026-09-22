// tests_names_i18n_fixes_2026_09_22.js — FIX-2026-09-22 fix-agent B
// regression tests for the names / i18n frontend domain. Offline and
// deterministic: no network, no timers beyond immediate promises.
//
// Covers:
//  * item 4: js/app.js rcaNameIssuesFromGbif must answer the GBIF verdict
//    "Multiple equal matches" (and the genus-rank equal-alternatives rule)
//    with a names.ambiguous issue — it used to return [] (silence),
//    forking behaviour from rca_core/names.py:name_issues.
//  * item 5: the coverage-ledger / reason_code.* i18n catalog must exist in
//    ALL THREE languages on BOTH transports (js + rca_core/i18n.py), the
//    three JS locales must have identical key sets, and the reason_code
//    catalog must gain a real UI consumer (rcaCoverageReasonIssues).
//  * item 2: js/app.js rcaCleanNameForLookup parity with the Python
//    clean_name_for_lookup for non-parenthesised author + year tails.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let passed = 0;
let failed = 0;
function check(name, ok, detail) {
  if (ok) { passed += 1; console.log('PASS', name); }
  else { failed += 1; console.log('FAIL', name + (detail ? ' — ' + detail : '')); }
}

// ---------------------------------------------------------------------------
// harness: real js/i18n.js in a vm + the four app.js helpers extracted by
// brace-matching (app.js is one big IIFE; same technique tests_frontend.js
// uses, scoped to just the functions under test).
// ---------------------------------------------------------------------------

function extractFn(src, name) {
  const sig = 'function ' + name + '(';
  const start = src.indexOf(sig);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 0;
  for (; i < src.length; i++) {
    const c = src[i];
    if (c === "'" || c === '"' || c === '`') { // skip string literals
      const q = c; i++;
      while (i < src.length && src[i] !== q) { if (src[i] === '\\') i++; i++; }
      continue;
    }
    if (c === '/' && src[i + 1] === '/') { while (i < src.length && src[i] !== '\n') i++; continue; }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 1; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  throw new Error('unbalanced braces for ' + name);
}

const memStore = () => {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
};
const ctx = {
  console,
  // rcaSetLang() persists via config.js's store - load the real config.js
  // under in-memory storage stubs, like tests_frontend.js does.
  localStorage: memStore(), sessionStorage: memStore(),
  document: {
    documentElement: { setAttribute() {}, removeAttribute() {}, getAttribute: () => null },
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, createElement: () => ({ setAttribute() {}, style: {} }),
  },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, 'js', 'config.js'), 'utf8'),
  ctx, { filename: 'js/config.js' });
vm.runInContext(
  'globalThis.RCA_STORE = RCA_STORE; globalThis.rcaStoreSet = rcaStoreSet; globalThis.rcaStoreGet = rcaStoreGet;',
  ctx, { filename: 'js/config.js#exports' });
vm.runInContext(fs.readFileSync(path.join(__dirname, 'js', 'i18n.js'), 'utf8'),
  ctx, { filename: 'js/i18n.js' });
// const/let top-level bindings are lexical in a vm context (not context
// properties) - export them explicitly, like tests_frontend.js does.
vm.runInContext(
  'globalThis.RCA_I18N = RCA_I18N; globalThis.rcaSetLang = rcaSetLang; globalThis.t = t;',
  ctx, { filename: 'js/i18n.js#exports' });
const appSrc = fs.readFileSync(path.join(__dirname, 'js', 'app.js'), 'utf8');
vm.runInContext(
  extractFn(appSrc, 'rcaCleanNameForLookup') + '\n' +
  extractFn(appSrc, 'rcaGbifCandidate') + '\n' +
  extractFn(appSrc, 'rcaNameIssuesFromGbif') + '\n' +
  extractFn(appSrc, 'rcaCoverageReasonIssues') + '\n' +
  'globalThis.rcaCleanNameForLookup = rcaCleanNameForLookup;\n' +
  'globalThis.rcaNameIssuesFromGbif = rcaNameIssuesFromGbif;\n' +
  'globalThis.rcaCoverageReasonIssues = rcaCoverageReasonIssues;\n',
  ctx, { filename: 'js/app.js#names-helpers' });

const I18N = ctx.RCA_I18N;
const t = ctx.t;
const rcaCleanNameForLookup = ctx.rcaCleanNameForLookup;
const rcaNameIssuesFromGbif = ctx.rcaNameIssuesFromGbif;
const rcaCoverageReasonIssues = ctx.rcaCoverageReasonIssues;

// ---------------------------------------------------------------------------
// item 4: the Multiple-equal-matches branch of rcaNameIssuesFromGbif
// ---------------------------------------------------------------------------

const ALTS = [
  { usageKey: 111, canonicalName: 'Ptereoconus', scientificName: 'Ptereoconus Hoenes, 1891',
    matchType: 'EXACT', confidence: 100, rank: 'GENUS', kingdom: 'Chromista',
    phylum: 'Radioolaria', status: 'ACCEPTED' },
  { usageKey: 222, canonicalName: 'Ptereoconus', scientificName: 'Ptereoconus Cook',
    matchType: 'EXACT', confidence: 100, rank: 'GENUS', kingdom: 'Animalia',
    phylum: 'Arthropoda', status: 'ACCEPTED' },
];

{
  const issues = rcaNameIssuesFromGbif({
    matchType: 'Multiple equal matches', confidence: 100,
    canonicalName: 'Ptereoconus', usageKey: null, rank: 'GENUS',
    alternatives: ALTS, query: 'Ptereoconus',
  });
  check('amb-branch-not-silent', issues.length === 1 &&
    issues[0].msg_key === 'names.ambiguous', JSON.stringify(issues));
  check('amb-candidates-mapped', Array.isArray(issues[0].candidates) &&
    issues[0].candidates.length === 2 &&
    issues[0].candidates[0].usage_key === 111 &&
    issues[0].candidates[1].phylum === 'Arthropoda');
  check('amb-name-kept', issues[0].name === 'Ptereoconus');
}
{
  // genus-rank EXACT with an equally-strong alternative: py flags it, js
  // must too (mirror of verify_name_gbif's ambiguity rule).
  const issues = rcaNameIssuesFromGbif({
    matchType: 'EXACT', confidence: 100, canonicalName: 'Ptereoconus',
    usageKey: 111, rank: 'GENUS', alternatives: [ALTS[1]], query: 'Ptereoconus',
  });
  check('amb-genus-equal-alt', issues.length === 1 &&
    issues[0].msg_key === 'names.ambiguous', JSON.stringify(issues));
}
{
  // FUZZY keeps priority; a species-rank EXACT stays quiet; NONE unchanged.
  const fuzzy = rcaNameIssuesFromGbif({ matchType: 'FUZZY', confidence: 90,
    canonicalName: 'B b', alternatives: [] });
  check('amb-fuzzy-still-first', fuzzy.length === 1 && fuzzy[0].msg_key === 'names.fuzzy');
  const exact = rcaNameIssuesFromGbif({ matchType: 'EXACT', confidence: 100,
    canonicalName: 'A a', rank: 'SPECIES', alternatives: [] });
  check('amb-exact-quiet', exact.length === 0);
  const none = rcaNameIssuesFromGbif({ matchType: 'NONE', confidence: 0,
    canonicalName: '', alternatives: [] });
  check('amb-none-unmatched', none.length === 1 && none[0].msg_key === 'names.unmatched');
}

// names.ambiguous resolves (no "[?...]" placeholder) in all three languages.
for (const lang of ['zh', 'en', 'ja']) {
  ctx.rcaSetLang(lang);
  const s = t('names.ambiguous', { name: 'Ptereoconus' });
  check('names.ambiguous-resolves:' + lang,
    s.indexOf('[?') < 0 && s.indexOf('Ptereoconus') >= 0, s);
}
ctx.rcaSetLang('zh');

// ---------------------------------------------------------------------------
// item 5: catalog completeness (js three-way + py<->js) and the consumer
// ---------------------------------------------------------------------------

const NAMED = ['quality.coverage_ledger', 'quality.coverage_reasons', 'names.ambiguous']
  .concat(['not_drawn', 'uncertain', 'obscured', 'inferred', 'legend_only',
            'crosses_top', 'crosses_base', 'truncated', 'no_label',
            'abbreviated', 'low_confidence', 'out_of_scope']
    .map((s) => 'reason_code.' + s));

for (const key of NAMED) {
  for (const lang of ['zh', 'en', 'ja']) {
    check('key-' + key + ':' + lang, typeof I18N[lang][key] === 'string' &&
      I18N[lang][key].length > 0 && I18N[lang][key] !== key);
  }
}

{
  const norm = (o) => Object.keys(o).filter((k) => k !== '_label').sort().join(',');
  check('js-zh-en-identical-keys', norm(I18N.zh) === norm(I18N.en));
  check('js-zh-ja-identical-keys', norm(I18N.zh) === norm(I18N.ja));
}

{
  // py <-> js key-set identity for the five shared namespaces, per locale.
  const pySrc = fs.readFileSync(path.join(__dirname, 'rca_core', 'i18n.py'), 'utf8');
  const rx = /^    "((?:col|sec|quality|names|reason_code)\.[A-Za-z0-9_]+)":/gm;
  for (const lang of ['zh', 'en', 'ja']) {
    const start = pySrc.indexOf('TRANSLATIONS["' + lang + '"] = {');
    const body = pySrc.slice(start, pySrc.indexOf('\n}', start));
    const py = new Set();
    let m; rx.lastIndex = 0;
    while ((m = rx.exec(body)) !== null) py.add(m[1]);
    const js = new Set(Object.keys(I18N[lang])
      .filter((k) => /^(col|sec|quality|names|reason_code)\./.test(k)));
    const pyOnly = [...py].filter((k) => !js.has(k)).sort();
    const jsOnly = [...js].filter((k) => !py.has(k)).sort();
    check('py-js-keysets-identical:' + lang,
      pyOnly.length === 0 && jsOnly.length === 0,
      'pyOnly=' + pyOnly + ' jsOnly=' + jsOnly);
  }
}

{
  // The reason-code consumer: translated labels + counts, missing-key
  // slugs degrade to the raw slug, empty ledgers stay silent.
  const issues = rcaCoverageReasonIssues({
    reason_code_counts: { not_drawn: 3, uncertain: 1 },
  });
  ctx.rcaSetLang('zh');
  const zhIssue = rcaCoverageReasonIssues({ reason_code_counts: { not_drawn: 2 } });
  ctx.rcaSetLang('en');
  const enIssue = rcaCoverageReasonIssues({ reason_code_counts: { not_drawn: 2 } });
  check('reason-consumer-shape', issues.length === 1 &&
    issues[0].msg_key === 'quality.coverage_reasons' &&
    issues[0].params.reasons.indexOf('×3') >= 0 &&
    issues[0].params.reasons.indexOf('×1') >= 0, JSON.stringify(issues));
  check('reason-consumer-translated-per-language',
    zhIssue[0].params.reasons.indexOf('未画') >= 0 &&
    enIssue[0].params.reasons.indexOf('not drawn') >= 0 &&
    zhIssue[0].params.reasons !== enIssue[0].params.reasons,
    zhIssue[0].params.reasons + ' // ' + enIssue[0].params.reasons);
  ctx.rcaSetLang('zh');
  check('reason-consumer-empty', rcaCoverageReasonIssues({}).length === 0 &&
    rcaCoverageReasonIssues(null).length === 0);
  const unknown = rcaCoverageReasonIssues({ reason_code_counts: { bogus_slug: 1 } });
  check('reason-consumer-unknown-slug-degrades',
    unknown.length === 1 && unknown[0].params.reasons.indexOf('bogus_slug') >= 0 &&
    unknown[0].params.reasons.indexOf('[?') < 0, unknown[0].params.reasons);
}

// ---------------------------------------------------------------------------
// item 2: cleaning parity with rca_core/names.py (fixed cases from Python)
// ---------------------------------------------------------------------------

const CLEAN_CASES = [
  ['Ptereoconus hoenesi Hoenes, 1891', 'Ptereoconus hoenesi'],
  ['Clarkina yini Jiang and Wang, 2001', 'Clarkina yini'],
  ['Clarkina yini Jiang et al., 2001', 'Clarkina yini'],
  ['Clarkina yini Jiang et al. 2001', 'Clarkina yini'],
  ['Clarkina yini (Mead and Krotov, 1934)', 'Clarkina yini'],
  ['Clarkina yini', 'Clarkina yini'],
  ['Hindeodus 1979', 'Hindeodus 1979'], // bare year is NOT an author tail
  ['Pseudoalgovella sp.', 'Pseudoalgovella'],
];
for (const [raw, want] of CLEAN_CASES) {
  check('clean-parity:' + raw, rcaCleanNameForLookup(raw) === want,
    'got ' + JSON.stringify(rcaCleanNameForLookup(raw)) + ' want ' + JSON.stringify(want));
}

console.log('\n--- ' + passed + ' passed, ' + failed + ' failed ---');
process.exitCode = failed ? 1 : 0;
