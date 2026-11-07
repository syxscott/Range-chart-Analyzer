/**
 * tests_shared_js_parity.js — the JS twins that CANNOT be physically merged.
 *
 * WHY A LOCK INSTEAD OF A SHARED FILE (ARCH-REVIEW A1 / A5)
 * ========================================================
 * The review proposed `js/pyfloat.js` and `js/warnings.js` as shared files.
 * That advice assumed "add one line to the test script list". In reality this
 * repo has NO module system and NO build step: index.html loads 14 scripts
 * by <script defer>, the Fluent desktop inlines three of them into a
 * QWebEngineView page, and -- critically -- tests_aggregate.js deliberately
 * loads js/aggregate.js ALONE (its own header says so). Extracting a shared
 * file would require changing ~10 load sites across index.html, six Node
 * harnesses, the differential parity SCRIPTS array and the desktop inline
 * builder, and would break the "each harness can load one file standalone"
 * property the whole test architecture rests on.
 *
 * So the DUPLICATION REMAINS, but the DRIFT RISK IS REMOVED: every twin is
 * pinned to its sibling here by source-level identity plus behavioural
 * equality, exactly like tests_pyfloat_parity.js pins the Python-float twins
 * against a CPython oracle. A fix that lands on only one copy fails this
 * suite immediately.
 *
 * Measured facts (2026-11-07): the "105-line duplicate" in the review is a
 * SIX-line function starting at js/quality.js:105; it is byte-identical to
 * js/table.js's rcaEditWarningFlags after renaming.
 *
 * Run: node tests/js/tests_shared_js_parity.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const read = (rel) => fs.readFileSync(path.join(REPO, rel), 'utf8');

let passed = 0;
let failed = 0;
const check = (ok, msg) => {
  if (ok) passed += 1;
  else { failed += 1; console.log('FAIL ' + msg); }
};

/** Extract a top-level function's full source by brace matching. */
function functionSource(src, name) {
  const start = src.indexOf('function ' + name + '(');
  if (start === -1) return null;
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth += 1;
    else if (src[i] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  return null;
}

// --- A5: warning-flag twins ------------------------------------------------
const qualitySrc = read('js/quality.js');
const tableSrc = read('js/table.js');
const qBody = functionSource(qualitySrc, 'rcaWarningFlags');
const tBody = functionSource(tableSrc, 'rcaEditWarningFlags');
check(typeof qBody === 'string' && typeof tBody === 'string',
      'both warning-flag functions must exist');
if (qBody && tBody) {
  check(qBody === tBody.replace(/rcaEditWarningFlags/g, 'rcaWarningFlags'),
        'js/quality.js rcaWarningFlags and js/table.js rcaEditWarningFlags '
        + 'have drifted -- fix BOTH copies');
}

function loadWith(rel, needs) {
  const ctx = { console, TextEncoder, URL };
  ctx.globalThis = ctx;
  ctx.window = ctx;
  ctx.self = ctx;
  vm.createContext(ctx);
  for (const n of needs) {
    vm.runInContext(read(n), ctx, { filename: n });
  }
  vm.runInContext(read(rel), ctx, { filename: rel });
  return ctx;
}

const INPUTS = [
  null, undefined, '', 'single_flag', ['a', 'b'], ['a', 'a'],
  { _warning: 'x' }, 0, false, ['nested', ['x']], '  spaced  ',
];
try {
  const q = loadWith('js/quality.js', ['js/json-utils.js']);
  const t = loadWith('js/table.js', ['js/json-utils.js', 'js/quality.js']);
  if (typeof q.rcaWarningFlags !== 'function'
      || typeof t.rcaEditWarningFlags !== 'function') {
    check(false, 'could not resolve both warning-flag functions in vm');
  } else {
    for (const input of INPUTS) {
      let a, b, err = null;
      try {
        a = JSON.stringify(q.rcaWarningFlags(input));
        b = JSON.stringify(t.rcaEditWarningFlags(input));
      } catch (e) { err = e; }
      check(err === null && a === b,
            `warning-flag twins disagree on ${JSON.stringify(input)}: `
            + `${a} vs ${b}${err ? ' (' + err.message + ')' : ''}`);
    }
  }
} catch (e) {
  check(false, 'warning-flag vm load failed: ' + e.message);
}

// --- A1: Python-float twins ------------------------------------------------
const aggSrc = read('js/aggregate.js');
const mmSrc = read('js/minimax.js');
const aggT = functionSource(aggSrc, 'rcaAggPyFloatTransliterate');
const mmT = functionSource(mmSrc, 'rcaPyFloatTransliterate');
check(typeof aggT === 'string' && typeof mmT === 'string',
      'both Unicode-Nd transliterators must exist');
if (aggT && mmT) {
  check(aggT === mmT.replace(/rcaPyFloatTransliterate/g,
                             'rcaAggPyFloatTransliterate'),
        'the two transliterators have drifted -- run '
        + 'tests/js/tests_pyfloat_parity.js and fix BOTH');
}
// The behavioural oracle lives in its own suite; require it to be present.
check(fs.existsSync(path.join(REPO, 'tests/js/tests_pyfloat_parity.js')),
      'the pyfloat CPython-oracle suite must exist');

console.log(`\n--- shared JS parity: ${passed} passed, ${failed} failed ---`);
process.exit(failed ? 1 : 0);
