#!/usr/bin/env node
/**
 * FIX-2026-09-22 (agent A2) — extraction-contract MIRROR test.
 *
 * The bug this suite pins: js/prompt.js marks response_kind / reason_codes /
 * *pos_0_999 / axis_calibration REQUIRED, but js/minimax.js knew none of
 * these keys, so rcaNormalizeResult swept every contract field into
 * `_extras`. Consequences that were invisible end-to-end:
 *   * js/aggregate.js rcaMergeContractField (priority vote / union /
 *     divergent) NEVER fired — the merge saw no response_kind to vote on;
 *   * js/quality.js rcaCoverageFor saw row.response_kind === undefined —
 *     the frontend coverage ledger never existed;
 *   * the geometry sidecar never existed browser-side, so the zero-span /
 *     contradiction guards (backend audit items 4/5/7/8) had no JS mirror.
 *
 * Everything here therefore runs contract-bearing payloads THROUGH
 * rcaNormalizeResult / rcaNormalizeAbundanceResult (not just unit-fns),
 * then through the real rcaMergeResults and rcaCoverageFor, asserting the
 * contract survives as FIRST-CLASS fields at every hop.
 *
 * Offline, no secrets. Same vm-sandbox pattern as
 * tests_diff_frontend_parity.js (which compares against precomputed Python;
 * the Python-side mirror of this file is
 * tests/test_contract_fixes_2026_09_22.py).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const SCRIPTS = [
  'js/config.js',
  'js/json-utils.js',
  'js/ics_table.js',
  'js/reason-codes.js',
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
    fetch: async () => { throw new Error('network disabled in mirror test'); },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    document: {
      documentElement: {
        style: {}, dataset: {},
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
      },
      addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
      querySelector: () => null, querySelectorAll: () => [],
      getElementById: () => null,
      createElement: () => ({ style: {}, appendChild() {} }),
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
    vm.runInContext(fs.readFileSync(path.join(ROOT, rel), 'utf8'), ctx,
      { filename: rel });
  }
  vm.runInContext(`
    globalThis.__exp = {
      normalizeResult: rcaNormalizeResult,
      normalizeAbundance: rcaNormalizeAbundanceResult,
      mergeResults: rcaMergeResults,
      coverageFor: rcaCoverageFor,
      scoreRangeChart: scoreRangeChart,
      RC: RCAReasonCodes,
    };
  `, ctx);
  return ctx.__exp;
}

const F = buildContext();

// A second, tiny sandbox with a functional-enough DOM to render the
// name-issues block (only i18n.js + table.js are needed).
function buildNameIssuesContext() {
  const el = (tag) => ({
    tag,
    children: [],
    get firstChild() { return this.children.length ? this.children[0] : null; },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i >= 0) this.children.splice(i, 1);
      return c;
    },
    style: {}, dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
    textContent: '', className: '',
  });
  const host = el('div');
  const store = new Map();
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    TextEncoder, TextDecoder, URL,
    fetch: async () => { throw new Error('network disabled'); },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    document: {
      documentElement: el('html'),
      addEventListener() {}, removeEventListener() {},
      querySelector: () => null, querySelectorAll: () => [],
      getElementById: (id) => (id === 'names-verify-slot' ? host : null),
      createElement: (tag) => el(tag),
      body: el('body'),
    },
    alert() {}, prompt: () => '', confirm: () => false,
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    navigator: { language: 'en' },
  };
  ctx.window = ctx;
  ctx.self = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const rel of ['js/i18n.js', 'js/table.js']) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, rel), 'utf8'), ctx,
      { filename: rel });
  }
  return {
    host,
    render: (issues) => vm.runInContext('rcaRenderNameIssues', ctx)(issues),
    template: (key) => vm.runInContext('t', ctx)(key),
  };
}

let passed = 0; let failed = 0;
function ok(cond, name, detail) {
  if (cond) { passed += 1; console.log('PASS ' + name); }
  else { failed += 1; console.log('FAIL ' + name + (detail ? ' :: ' + detail : '')); }
}
function eq(a, b, name) {
  ok(JSON.stringify(a) === JSON.stringify(b), name,
     'got ' + JSON.stringify(a) + ' want ' + JSON.stringify(b));
}

// ---------------------------------------------------------------------------
// 1. rcaNormalizeResult: contract fields land FIRST-CLASS, not in _extras
// ---------------------------------------------------------------------------

function rangeChartPayload(axisCal) {
  const p = {
    sections: [{ name: 'S1' }],
    species_ranges: [
      { species: 'A', section: 'S1', range_top: '9', range_base: '7',
        range_top_pos_0_999: 347, range_base_pos_0_999: 261,
        response_kind: 'extracted', reason_codes: ['crosses_top'] },
      { species: 'B', section: 'S1',
        response_kind: 'not_drawn', reason_codes: ['not_drawn'] },
    ],
    biozones: [], other_fossils: [], confidence: 0.8,
  };
  if (axisCal) p.axis_calibration = axisCal;
  return p;
}

{
  const out = F.normalizeResult(rangeChartPayload(
    { vertical: { at_0: 0, at_999: 25, unit: 'm' } }));
  const a = out.species_ranges[0];
  const b = out.species_ranges[1];
  eq(a.response_kind, 'extracted', 'species A keeps first-class response_kind');
  eq(a.reason_codes, ['crosses_top'], 'species A keeps first-class reason_codes');
  eq(b.response_kind, 'not_drawn', 'species B keeps an honest not_drawn');
  ok(!a._extras || !('response_kind' in a._extras),
     'response_kind is NOT dumped into _extras (A)');
  ok(!a._extras || !('reason_codes' in a._extras),
     'reason_codes is NOT dumped into _extras (A)');
  ok(!a._extras || !('range_top_pos_0_999' in a._extras),
     '*pos_0_999 is NOT dumped into _extras (A)');
  ok(a.geometry && a.geometry.points && a.geometry.points.range_top_pos_0_999,
     'geometry sidecar built browser-side');
  eq(a.geometry.calibrated, true, 'usable calibration -> calibrated geometry');
  ok(Math.abs(a.geometry.points.range_top_pos_0_999.value - 347 * 25 / 999) < 1e-6,
     'converted value mirrors pos_to_axis_value (347/999 of [0,25])');
  ok(out.axis_calibration && out.axis_calibration.vertical.at_999 === 25,
     'root axis_calibration hoisted as a first-class root key, not _extras');
  ok(!out._extras || !('axis_calibration' in out._extras),
     'hoisted axis_calibration does not also sit under _extras');
}

// not_drawn + a readable value -> extracted + response_kind_conflict
{
  const out = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1', range_top: '9',
                       response_kind: 'not_drawn',
                       reason_codes: ['not_drawn'] }],
    biozones: [], other_fossils: [], confidence: 0.8,
  });
  const row = out.species_ranges[0];
  eq(row.response_kind, 'extracted', 'not_drawn + value is resolved to extracted');
  eq(row._warning, 'response_kind_conflict', 'the lie is flagged (single -> string)');
}

// index_order_swap + response_kind_conflict COEXIST (additive, audit item 2)
{
  const out = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1', range_top: '9', range_base: '7',
                       range_top_idx: 3, range_base_idx: 8,
                       response_kind: 'not_drawn',
                       reason_codes: ['not_drawn'] }],
    biozones: [], other_fossils: [], confidence: 0.8,
  });
  const w = out.species_ranges[0]._warning;
  ok(Array.isArray(w) && w.indexOf('response_kind_conflict') !== -1
     && w.indexOf('index_order_swap') !== -1,
     'conflict + order-swap warnings coexist as a list', JSON.stringify(w));
}

// bool rejection (audit item 8) and zero-span unusable (audit items 4 + 7)
{
  const out = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1', range_top: '9',
                       range_top_pos_0_999: true }],
    biozones: [], other_fossils: [], confidence: 0.8,
  });
  const row = out.species_ranges[0];
  ok(!row.geometry || !row.geometry.points || !row.geometry.points.range_top_pos_0_999,
     'a bool position is rejected, never mapped as position 1');
  ok(row.reason_codes && row.reason_codes.indexOf('low_confidence') !== -1,
     'rejected position adds the low_confidence code');
}
{
  const out = F.normalizeResult(rangeChartPayload(
    { vertical: { at_0: 0, at_999: 0 } }));
  ok(!out.axis_calibration, 'zero-span calibration is NEVER hoisted');
  ok(out._warnings.indexOf('axis_calibration_unusable') !== -1,
     'unusable root calibration warns instead of vanishing (audit item 7)');
  ok(out._extras && out._extras.axis_calibration
     && out._extras.axis_calibration.vertical.at_999 === 0,
     'the raw unusable claim survives under _extras for the operator');
  const a = out.species_ranges[0];
  eq(a.geometry.calibrated, false, 'zero span carries no information -> not calibrated');
  ok(a.geometry.points.range_top_pos_0_999.pos === 347
     && a.geometry.points.range_top_pos_0_999.value === undefined,
     'raw 0-999 integer kept as evidence, engineered value withheld');
  ok(a.reason_codes.indexOf('low_confidence') !== -1,
     'zero-span rejection -> low_confidence on the row');
}
{
  const out = F.normalizeResult(rangeChartPayload(
    { vertical: { at_0: 1 } }));
  ok(!out.axis_calibration
     && out._warnings.indexOf('axis_calibration_unusable') !== -1,
     'half-broken axis (one numeric end) is unusable too');
}

// 15% contradiction check on the semantic pair (range_top vs the pos read)
{
  const out = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1', range_top: '10',
                       range_top_pos_0_999: 300 }],
    biozones: [], other_fossils: [], confidence: 0.8,
    axis_calibration: { vertical: { at_0: 0, at_999: 10 } },
  });
  const row = out.species_ranges[0];
  // 300/999*10 = 3.003 vs the transcribed 10: |7| > 15% of 10 -> rejected.
  ok(!row.geometry || !row.geometry.points.range_top_pos_0_999
     || row.geometry.points.range_top_pos_0_999.value === undefined,
     'geometry contradicting the transcribed boundary is dropped',
     JSON.stringify(row.geometry));
  ok(row.reason_codes.indexOf('low_confidence') !== -1,
     'the contradiction surfaces as low_confidence, not silence');
}

// ---------------------------------------------------------------------------
// 2. abundance mode: same contract wiring
// ---------------------------------------------------------------------------
{
  const out = F.normalizeAbundance({
    sites: [{ name: 'S', depth_unit: 'm' }],
    abundances: [
      { taxon: 'T1', site: 'S', level: 'L1', depth: '5.0', abundance: '10',
        pos_0_999: 500, response_kind: 'extracted' },
      { taxon: 'T2', site: 'S', level: 'L1',
        response_kind: 'not_drawn', reason_codes: { obscured: false, not_drawn: true } },
    ],
    zones: [], confidence: 0.5,
    axis_calibration: { vertical: { at_0: 0, at_999: 10, unit: 'm' } },
  });
  const r1 = out.abundances[0];
  const r2 = out.abundances[1];
  eq(r1.response_kind, 'extracted', 'abundance row keeps first-class response_kind');
  ok(r1.geometry && r1.geometry.points.pos_0_999.pos === 500
     && Math.abs(r1.geometry.points.pos_0_999.value - 500 * 10 / 999) < 1e-6,
     'abundance geometry converts against the calibration');
  ok(!r1._extras || !('pos_0_999' in r1._extras),
     'consumed pos field does not duplicate under _extras');
  eq(r2.reason_codes, ['not_drawn'],
     'dict codes: an explicitly-false value DENIES the code (audit item 8)');
  ok(out.axis_calibration && out.axis_calibration.vertical.at_999 === 10,
     'abundance root axis_calibration hoisted first-class');
}

// ---------------------------------------------------------------------------
// 3. aggregate: the priority vote / union / divergent NOW fires
// ---------------------------------------------------------------------------
{
  const runA = { sections: [{ name: 'S1' }], confidence: 0.9,
                 species_ranges: [], biozones: [], other_fossils: [] };
  const runB = { sections: [{ name: 'S1' }], confidence: 0.9,
                 species_ranges: [], biozones: [], other_fossils: [] };
  runA.species_ranges = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1', range_top: '9',
                       response_kind: 'extracted',
                       reason_codes: ['crosses_top'] }],
    biozones: [], other_fossils: [], confidence: 0.9,
  }).species_ranges;
  runB.species_ranges = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1',
                       response_kind: 'not_drawn',
                       reason_codes: ['not_drawn'] }],
    biozones: [], other_fossils: [], confidence: 0.9,
  }).species_ranges;
  const merged = F.mergeResults([runA, runB], 2);
  const row = merged.species_ranges[0];
  eq(row.response_kind, 'extracted',
     'merge priority-votes extracted > not_drawn (never fired pre-fix)');
  ok(Array.isArray(row.response_kind_votes)
     && row.response_kind_votes.indexOf('extracted') !== -1
     && row.response_kind_votes.indexOf('not_drawn') !== -1,
     'response_kind_votes records both runs');
  ok(row._warning && (Array.isArray(row._warning) ? row._warning : [row._warning])
     .indexOf('response_kind_divergent') !== -1,
     'response_kind_divergent flagged on the merged row');
  eq([...row.reason_codes].sort(), ['crosses_top', 'not_drawn'],
     'reason_codes merged by union');
}

// pre-existing row warning survives the divergence flag (py==js shape)
{
  const base = { sections: [{ name: 'S1' }], biozones: [], other_fossils: [],
                 confidence: 0.9 };
  const runA = { ...base, species_ranges: [{
    species: 'A', section: 'S1', range_top: '9', response_kind: 'extracted',
    _warning: 'iron_rule_zone_label' }] };
  const runB = { ...base, species_ranges: [{
    species: 'A', section: 'S1', response_kind: 'not_drawn' }] };
  const row = F.mergeResults([runA, runB], 2).species_ranges[0];
  const w = row._warning;
  ok(Array.isArray(w) && w.indexOf('iron_rule_zone_label') !== -1
     && w.indexOf('response_kind_divergent') !== -1,
     'zone warning + divergent warning coexist (same shape as aggregate.py)',
     JSON.stringify(w));
}

// ---------------------------------------------------------------------------
// 4. quality: the frontend coverage ledger finally EXISTS
// ---------------------------------------------------------------------------
{
  const data = F.normalizeResult(rangeChartPayload(
    { vertical: { at_0: 0, at_999: 25, unit: 'm' } }));
  const ledger = F.coverageFor(data);
  ok(ledger !== null && ledger !== undefined,
     'rcaCoverageFor returns a ledger for contract-bearing normalized rows');
  eq(ledger.totals.extracted, 1, 'ledger counts the extracted cell');
  eq(ledger.totals.not_drawn, 1, 'ledger counts the honest not_drawn cell');
  eq(ledger.totals.cells, 2, 'two species columns attributed (species ladder)');
  const score = F.scoreRangeChart(data);
  ok(score.coverage && score.coverage.totals.cells === 2,
     'scoreRangeChart carries the coverage block');
  ok(score.issues.some((i) => i.msg_key === 'quality.coverage_ledger'),
     'quality.coverage_ledger info issue raised');
  // A pre-contract result stays exactly as quiet as before.
  const legacy = F.normalizeResult({
    sections: [{ name: 'S1' }],
    species_ranges: [{ species: 'A', section: 'S1', range_top: '9' }],
    biozones: [], other_fossils: [], confidence: 0.8,
  });
  ok(F.coverageFor(legacy) === null, 'legacy rows keep coverage null');
  ok(!F.scoreRangeChart(legacy).coverage, 'legacy score output unchanged');
}

// ---------------------------------------------------------------------------
// 5. rollup shape shared with report.py decisions (first-class visibility)
// ---------------------------------------------------------------------------
{
  const data = F.normalizeResult(rangeChartPayload(null));
  const roll = F.RC.reason_code_rollup(data.species_ranges,
    { columnKeys: ['species', 'taxon', 'name'] });
  eq(roll.contracted_rows, 2, 'rollup sees both contracted rows');
  ok(roll.entries.length === 2 && roll.entries[0].code_summaries,
     'rollup entries carry the machine slugs + English glosses');
}

// ---------------------------------------------------------------------------
// 6. js/table.js rcaRenderNameIssues: names.ambiguous gets ITS OWN wording
//    (carry-over from agent B — the renderer used to print the "not matched"
//    claim for ambiguous names, the exact opposite of the truth).
// ---------------------------------------------------------------------------
{
  const ctx = buildNameIssuesContext();
  const host = ctx.host;
  const issues = [
    { msg_key: 'names.ambiguous', name: 'Ptero' },
    { msg_key: 'names.unmatched', name: 'Gyro' },
    { msg_key: 'names.fuzzy', name: 'Onto', suggestion: 'Onto cf.' },
  ];
  ctx.render(issues);
  const rows = host.children[0] ? host.children[0].children : [];
  eq(rows.length, 3, 'one row per issue');
  const ambiguousText = rows[0] ? rows[0].children[1].textContent : '';
  const unmatchedText = rows[1] ? rows[1].children[1].textContent : '';
  const ambTemplate = ctx.template('names.ambiguous');
  const unTemplate = ctx.template('names.unmatched');
  ok(ambiguousText.indexOf('{name}') === -1 && ambiguousText.indexOf('Ptero') !== -1,
     'ambiguous row interpolates the name', ambiguousText);
  ok(ambTemplate !== unTemplate
     && ambiguousText === ambTemplate.split('{name}').join('Ptero'),
     'ambiguous row uses the names.ambiguous wording, NOT the unmatched one',
     ambiguousText + ' vs ' + unmatchedText);
  ok(unmatchedText === unTemplate.split('{name}').join('Gyro'),
     'unmatched (non-fuzzy) rows keep the names.unmatched wording');
}

console.log('\n--- ' + passed + ' passed, ' + failed + ' failed ---');
process.exit(failed ? 1 : 0);
