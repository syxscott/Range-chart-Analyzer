/**
 * tests_pyfloat_parity.js — Python's float() grammar must be ONE decision.
 *
 * P0-1 (ARCH-REVIEW-2026-11-02): js/aggregate.js::rcaPyFloat and
 * js/minimax.js::rcaPyFloatOrNull both claim to mirror Python float(), but
 * only minimax.js folded Unicode Nd digits. The lead reproduced the
 * divergence on the real files:
 *
 *     '１２７'  -> aggregate rcаPyFloat : null   (WRONG)
 *              -> minimax  rcaPyFloatOrNull: 127
 *              -> Python float()          : 127.0
 *
 * Consequences: rcaSortTuple puts a null in the STRING bucket, so a browser
 * runs>1 merge orders the same batch differently from the Python merge and a
 * numeric sort silently becomes a lexicographic one. Fullwidth digits are not
 * exotic in CJK chart labels / OCR output.
 *
 * The expected values below were measured with CPython float() on 2026-11-05
 * (not guessed). Each case is checked against BOTH JS implementations, and
 * the two implementations are asserted identical on every input -- that
 * identity assertion is what stops the next "fix landed on only one copy".
 *
 * Run: node tests/js/tests_pyfloat_parity.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');

function load(rel) {
  const ctx = { console };
  ctx.globalThis = ctx;
  ctx.window = ctx;
  ctx.self = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(REPO, rel), 'utf8'), ctx,
                  { filename: rel });
  return ctx;
}

// Separate contexts: each file declares top-level helpers, and loading them
// into one context would silently overwrite the first. aggregate.js names its
// transliterator rcaAggPyFloatTransliterate precisely to avoid a global clash.
const agg = load('js/aggregate.js');
const mm = load('js/minimax.js');

const pyFloat = agg.rcaPyFloat;
const pyFloatOrNull = mm.rcaPyFloatOrNull;

if (typeof pyFloat !== 'function' || typeof pyFloatOrNull !== 'function') {
  console.error('FAIL: could not resolve the two float helpers');
  process.exit(1);
}

// [input, Python float() answer]; null = Python raises ValueError.
const CASES = [
  ['１２７', 127],
  ['٥٠٠', 500],
  ['１_２', 12],
  ['１２_', null],
  ['１２７.٥', 127.5],
  ['１２３', 123],
  ['1_2e3_4', 1.2e+35],
  ['inf', Infinity],
  ['nan', NaN],
  ['1e3', 1000],
  ['not a number', null],
  ['１ｅ５', null],       // fullwidth e is Ll, not Nd; Python rejects it
  ['-１２', -12],
  ['+１２', 12],
  ['.٥', 0.5],
  ['1_', null],
  ['_１２', null],
  ['1__2', null],
  ['１２abc', null],
];

let passed = 0;
let failed = 0;

function same(a, b) {
  if (a === null || b === null) return a === b;
  if (Number.isNaN(a) && Number.isNaN(b)) return true;
  return Object.is(a, b);
}

function check(name, ok, detail) {
  if (ok) { passed += 1; }
  else { failed += 1; console.log('FAIL ' + name + (detail ? '  ' + detail : '')); }
}

for (const [input, expected] of CASES) {
  const a = pyFloat(input);
  const m = pyFloatOrNull(input);
  check('aggregate mirrors Python: ' + JSON.stringify(input), same(a, expected),
        `got ${String(a)}, expected ${String(expected)}`);
  check('minimax mirrors Python: ' + JSON.stringify(input), same(m, expected),
        `got ${String(m)}, expected ${String(expected)}`);
  // The identity assertion: this is the guard that was missing.
  check('the two JS copies agree: ' + JSON.stringify(input), same(a, m),
        `aggregate=${String(a)} minimax=${String(m)}`);
}

// Non-string inputs (Python float() on bool / number).
for (const [input, expected] of [[true, 1], [false, 0], [3.5, 3.5]]) {
  check('non-string ' + String(input), same(pyFloat(input), expected)
        && same(pyFloatOrNull(input), expected));
}

console.log(`\n--- pyfloat parity: ${passed} passed, ${failed} failed ---`);
process.exit(failed ? 1 : 0);
