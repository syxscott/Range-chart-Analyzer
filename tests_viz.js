// tests_viz.js — FE-BORROW-2026-09-20 (域V): node regression for the PURE
// layout layer of js/viz.js.
//
// Same loading technique as tests_frontend.js (readFileSync + vm.runInContext),
// with two deliberate differences:
//   * the sandbox ships NO `document` / `window` at all — that is the point: the
//     layout layer (and the file's load itself) must be DOM-free, and the render
//     entry points must degrade to `false` instead of throwing.
//   * only js/ics_table.js (the bundled ICS stage table, the semantic source
//     for the stage-name axis) and js/viz.js are loaded.
//
// Covered here: stage-name axis mapping, geometry-first placement, the
// unplaceable list, the focus state machine (data-side alpha maths) and hover
// hit testing. Run with:  node tests_viz.js
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// ---- sandbox ---------------------------------------------------------------

function buildContext() {
  const ctx = {
    console,
    setTimeout, clearTimeout, Date, Math, JSON,
  };
  ctx.globalThis = ctx;
  return ctx;
}

function loadAll(ctx) {
  vm.createContext(ctx);
  for (const f of ['js/ics_table.js', 'js/viz.js']) {
    const src = fs.readFileSync(path.join(__dirname, f), 'utf8');
    vm.runInContext(src, ctx, { filename: f });
  }
  return ctx;
}

// ---- assertions ------------------------------------------------------------

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log('PASS', name); } else {
    fail++;
    console.log('FAIL', name + (detail === undefined ? '' : ' — ' + detail));
  }
}
function approx(a, b, eps) {
  return typeof a === 'number' && typeof b === 'number'
    && Math.abs(a - b) <= (eps === undefined ? 1e-6 : eps);
}

// ---- fixtures --------------------------------------------------------------

// Bed-labelled rows: the shape the range-chart prompt actually emits.
const BED_RESULT = {
  sections: [{ name: 'S1', age_range: 'Late Permian–Early Triassic' }],
  species_ranges: [
    { species: 'A one', section: 'S1', range_top: 'Bed 9', range_base: 'Bed 7' },
    { species: 'B two', section: 'S1', range_top: 'Bed 5', range_base: 'Bed 1' },
    { species: 'C point', section: 'S1', range_top: 'Bed 9', range_base: 'Bed 9' },
    { species: 'D words', section: 'S1', range_top: 'top of chart', range_base: '' },
    { species: 'E not drawn', section: 'S1', range_top: '', range_base: '',
      response_kind: 'not_drawn' },
    'a malformed (non-dict) row',
  ],
};

// New-contract rows: an explicit geometry sidecar, calibrated.
function geomRow(species, topPos, basePos, extra) {
  const points = {};
  if (topPos !== null) {
    points.range_top_pos_0_999 = { pos: topPos, axis: 'vertical',
      value: extra && extra.topValue, unit: 'bed' };
  }
  if (basePos !== null) {
    points.range_base_pos_0_999 = { pos: basePos, axis: 'vertical',
      value: extra && extra.baseValue, unit: 'bed' };
  }
  const row = {
    species, section: 'S1', range_top: 'Bed 26', range_base: 'Bed 19',
    confidence: extra ? extra.confidence : undefined,
    geometry: {
      version: 1, scale: 'pos_0_999',
      calibrated: !(extra && extra.uncalibrated),
      points,
    },
  };
  if (extra && !extra.uncalibrated) {
    row.geometry.axes = { vertical: { at_0: 1, at_999: 24, unit: 'bed' } };
  }
  if (extra && extra.reasonCodes) row.reason_codes = extra.reasonCodes;
  return row;
}

const GEOM_RESULT = {
  species_ranges: [
    geomRow('G one', 347, 261, { topValue: 9, baseValue: 7, confidence: 0.9 }),
    geomRow('G two', 800, 100, { topValue: 20, baseValue: 3 }),
  ],
  axis_calibration: { vertical: { at_0: 1, at_999: 24, unit: 'bed' } },
};

const GEOM_UNCAL_RESULT = {
  species_ranges: [geomRow('U one', 500, 250, { uncalibrated: true })],
};

const STAGE_RESULT = {
  species_ranges: [
    { species: 'S chang', range_top: 'Changhsingian', range_base: 'Wuchiapingian' },
    { species: 'S induan', range_top: 'Induan', range_base: 'Wuchiapingian' },
    { species: 'S cn', range_top: '长兴阶', range_base: '吴家坪阶' },
  ],
};

// One row per placement source + one row with nothing at all.
const MIXED_RESULT = {
  species_ranges: [
    geomRow('M geom', 400, 200, {}),
    { species: 'M bed', range_top: 'Bed 4', range_base: 'Bed 2' },
    { species: 'M stage', range_top: 'Rhaetian', range_base: 'Norian' },
    { species: 'M none', range_top: '?', range_base: '' },
  ],
};

const MULTI_SECTION = {
  species_ranges: [
    { species: 'X a', section: 'S1', range_top: 'Bed 4', range_base: 'Bed 1' },
    { species: 'X b', section: 'S2', range_top: 'Bed 9', range_base: 'Bed 5' },
  ],
};

const barOf = (layout, i) => layout.bars[i];
const tOf = (u, layout) => (u - layout.axis.band.u0)
  / (layout.axis.band.u1 - layout.axis.band.u0);

// ============================================================================
// 1. load + DOM guard (the file must be require-able without a DOM)
// ============================================================================
function test_dom_free(ctx) {
  check('dom-free-load', typeof ctx.rcaVizLayout === 'function'
    && typeof ctx.rcaViz === 'object' && ctx.rcaViz !== null);
  check('dom-free-no-document', typeof ctx.document === 'undefined');
  check('render-guards-missing-dom', ctx.rcaViz.render(null, BED_RESULT) === false);
  check('render-guards-fake-host', ctx.rcaViz.render({}, BED_RESULT) === false);
  check('focusRow-without-layout', ctx.rcaViz.focusRow(0) === false);
  check('clearFocus-without-layout', ctx.rcaViz.clearFocus() === false);
  check('locateTo-without-layout', ctx.rcaViz.locateTo(0) === false);
  const cb = () => {};
  const un = ctx.rcaViz.onRowHover(cb);
  check('onRowHover-registers', typeof un === 'function'
    && ctx.rcaViz.getState().listeners === 1);
  check('onRowHover-unsubscribes', un() === true && un() === false
    && ctx.rcaViz.getState().listeners === 0);
  check('state-read-only', typeof ctx.rcaViz.getState() === 'object'
    && ctx.rcaViz.getState().mounted === false);
}

// ============================================================================
// 2. parsers (the geometry contract mirror)
// ============================================================================
function test_parsers(ctx) {
  const np = ctx.rcaVizNormalizePos;
  check('pos-int', np(261) === 261);
  check('pos-string', np('347') === 347);
  check('pos-fraction-rejected', np(347.5) === null);
  check('pos-out-of-range-rejected', np(1000) === null && np(-1) === null);
  check('pos-bool-rejected', np(true) === null && np(false) === null);
  check('pos-edge-values-kept', np(0) === 0 && np(999) === 999);
  check('bed-from-text', ctx.rcaVizBedValue('Bed 7 (top Talung Fm)') === 7);
  check('bed-from-chinese', ctx.rcaVizBedValue('第9层') === 9);
  check('bed-from-hash', ctx.rcaVizBedValue('#12') === 12);
  check('bed-from-bare-number', ctx.rcaVizBedValue('7') === 7);
  check('bed-rejects-prose', ctx.rcaVizBedValue('top of chart') === null);
  check('stage-en', ctx.rcaVizStageOf('latest Changhsingian)') === 'Changhsingian');
  check('stage-cn-alias', ctx.rcaVizStageOf('长兴阶') === 'Changhsingian');
  check('stage-period-is-not-a-stage', ctx.rcaVizStageOf('Late Permian') === null);
}

// ============================================================================
// 3. bed / semantic axis mapping + invariants
// ============================================================================
function test_bed_axis(ctx) {
  const before = JSON.stringify(BED_RESULT);
  const L = ctx.rcaVizLayout(BED_RESULT);
  check('bed-axis-kind', L.axis.kind === 'bed', L.axis.kind);
  check('bed-row-index-alignment', L.bars.length === 6
    && L.bars.every((b, i) => b.row_index === i));
  check('bed-band-defaults', approx(L.axis.band.u0, 0.22) && approx(L.axis.band.u1, 0.985));
  // domain = 1..9 seen across the visible rows
  check('bed-domain', L.axis.domain.lo === 1 && L.axis.domain.hi === 9,
    JSON.stringify(L.axis.domain));
  const b0 = barOf(L, 0), b1 = barOf(L, 1);
  check('bed-bar-A-extent', approx(tOf(b0.x, L), 6 / 8) && approx(tOf(b0.x + b0.w, L), 1),
    tOf(b0.x, L) + '/' + tOf(b0.x + b0.w, L));
  check('bed-bar-B-extent', approx(tOf(b1.x, L), 0) && approx(tOf(b1.x + b1.w, L), 0.5),
    tOf(b1.x, L) + '/' + tOf(b1.x + b1.w, L));
  check('bed-bar-left-is-oldest', b0.u0 < b0.u1 && b0.placed_by === 'bed');
  check('bed-bars-in-band', L.bars.filter((b) => b.placeable)
    .every((b) => b.x >= L.axis.band.u0 - 1e-9
      && b.x + b.w <= L.axis.band.u1 + 1e-9));
  check('bed-point-range-widened', b0.placeable && barOf(L, 2).degenerate === true
    && barOf(L, 2).w > 0);
  check('bed-tick-labels', L.axis.ticks.length > 0
    && L.axis.ticks[0].label.indexOf('Bed') === 0
    && L.axis.ticks.every((tk, i, a) => i === 0 || tk.u >= a[i - 1].u));
  check('bed-rows-and-placed', L.axis.rows === 6 && L.axis.placed === 3,
    'placed=' + L.axis.placed);
  check('bed-layout-does-not-mutate-result', JSON.stringify(BED_RESULT) === before);
}

// ============================================================================
// 4. unplaceable list
// ============================================================================
function test_unplaceable(ctx) {
  const L = ctx.rcaVizLayout(BED_RESULT);
  const list = L.axis.unplaceable;
  check('unplaceable-count', list.length === 3, JSON.stringify(list));
  check('unplaceable-no-position', list[0].reason === 'no_position'
    && list[0].row_index === 3 && list[0].species === 'D words');
  check('unplaceable-not-drawn', list[1].reason === 'not_drawn'
    && list[1].row_index === 4);
  check('unplaceable-malformed-row', list[2].reason === 'malformed_row'
    && list[2].row_index === 5);
  check('unplaceable-greyed-flags', L.bars[3].placeable === false
    && L.bars[3].faded_ok === false && L.bars[3].placed_by === null);
  check('unplaceable-keeps-row-band', approx(L.bars[3].x, L.axis.band.u0)
    && approx(L.bars[3].w, L.axis.band.u1 - L.axis.band.u0));
  check('unplaceable-row-order', L.bars[4].y > L.bars[3].y && L.bars[5].y > L.bars[4].y);
  const empty = ctx.rcaVizLayout({});
  check('empty-result-layout', empty.bars.length === 0 && empty.axis.kind === 'none'
    && empty.axis.unplaceable.length === 0);
  check('null-result-layout', ctx.rcaVizLayout(null).bars.length === 0);
  check('non-array-species-ignored',
    ctx.rcaVizLayout({ species_ranges: 'nope' }).bars.length === 0);
  const summary = ctx.rcaVizSummary(L);
  check('summary-states-numbers', summary.indexOf('3') !== -1 && /of/.test(summary),
    summary);
  check('summary-lists-unplaceable', summary.indexOf('D words') !== -1, summary);
  ctx.RCA_LANG = 'zh';
  check('summary-follows-language',
    ctx.rcaVizSummary(L).indexOf('无法定位') !== -1, ctx.rcaVizSummary(L));
  delete ctx.RCA_LANG;
  check('summary-empty-result', ctx.rcaVizSummary(empty).length > 0);
}

// ============================================================================
// 5. geometry precedence + calibrated ticks
// ============================================================================
function test_geometry(ctx) {
  const L = ctx.rcaVizLayout(GEOM_RESULT);
  const b0 = barOf(L, 0);
  check('geometry-axis-kind', L.axis.kind === 'geometry' && L.axis.mixed === false,
    L.axis.kind + '/' + L.axis.mixed);
  check('geometry-preferred-over-bed', b0.placed_by === 'geometry');
  check('geometry-position-from-pos-int',
    approx(b0.u0, L.axis.band.u0 + (261 / 999) * (L.axis.band.u1 - L.axis.band.u0))
    && approx(b0.u1, L.axis.band.u0 + (347 / 999) * (L.axis.band.u1 - L.axis.band.u0)),
    b0.u0 + '..' + b0.u1);
  check('geometry-values-carried', b0.value_old === 7 && b0.value_young === 9);
  check('geometry-calibrated-flag', b0.calibrated === true && L.axis.calibrated === true);
  check('geometry-calibrated-tick-labels',
    L.axis.ticks[0].label === '1 bed' && L.axis.ticks[4].label === '24 bed'
    && L.axis.ticks.length === 5, JSON.stringify(L.axis.ticks.map((t) => t.label)));
  check('geometry-tick-mid-value', approx(L.axis.ticks[2].value, 12.5),
    String(L.axis.ticks[2].value));
  const u = ctx.rcaVizLayout(GEOM_UNCAL_RESULT);
  check('geometry-uncalibrated-unit', u.axis.unit === 'pos_0_999'
    && u.axis.calibrated === false, u.axis.unit);
  check('geometry-uncalibrated-ticks', u.axis.ticks[0].label === '0'
    && u.axis.ticks[4].label === '999', JSON.stringify(u.axis.ticks.map((t) => t.label)));
  const oneSided = ctx.rcaVizLayout({
    species_ranges: [geomRow('H half', 300, null, {})],
  });
  check('geometry-single-boundary-partial',
    oneSided.bars[0].partial === true && oneSided.bars[0].degenerate === true
    && oneSided.bars[0].placeable === true);
  const badPos = ctx.rcaVizLayout({
    species_ranges: [{
      species: 'J bad', range_top: 'Bed 6', range_base: 'Bed 2',
      geometry: { version: 1, calibrated: true, points: {
        range_top_pos_0_999: { pos: 347.5 }, range_base_pos_0_999: { pos: 900 } } },
    }],
  });
  // Python's rule, mirrored: a bad point is dropped, the good one in the same
  // row survives — so the row stays on the geometry axis, flagged partial.
  check('geometry-bad-point-dropped-not-invented',
    badPos.bars[0].placed_by === 'geometry' && badPos.bars[0].partial === true
    && approx(badPos.bars[0].t0, 900 / 999), badPos.bars[0].placed_by);
  const allBad = ctx.rcaVizLayout({
    species_ranges: [{
      species: 'J2 all bad', range_top: 'Bed 6', range_base: 'Bed 2',
      geometry: { version: 1, calibrated: true, points: {
        range_top_pos_0_999: { pos: 347.5 }, range_base_pos_0_999: { pos: 1000 } } },
    }],
  });
  check('geometry-all-points-bad-degrades-to-bed',
    allBad.bars[0].placed_by === 'bed', allBad.bars[0].placed_by);
  const rawFields = ctx.rcaVizLayout({
    species_ranges: [{ species: 'K raw', range_top_pos_0_999: 700,
      range_base_pos_0_999: 300 }],
  });
  check('geometry-raw-pos-keys-accepted',
    rawFields.bars[0].placed_by === 'geometry' && approx(rawFields.bars[0].t0, 300 / 999),
    rawFields.bars[0].placed_by);
  const forcedBed = ctx.rcaVizLayout(GEOM_RESULT, { axis_source: 'bed' });
  check('axis_source-override-drops-foreign-rows',
    forcedBed.axis.kind === 'bed' && forcedBed.bars[0].reason === 'axis_kind_mismatch'
    && forcedBed.axis.unplaceable.length === 2, JSON.stringify(forcedBed.bars[0].reason));
  const strict = ctx.rcaVizLayout(MIXED_RESULT, { strict_axis: true });
  check('strict-axis-keeps-only-dominant', strict.axis.placed === 1
    && strict.bars[1].reason === 'axis_kind_mismatch', String(strict.axis.placed));
  const typo = ctx.rcaVizLayout(MIXED_RESULT, { axis_source: 'bogus' });
  check('unknown-axis_source-falls-back-to-auto', typo.axis.kind === 'geometry'
    && typo.axis.placed === 3, typo.axis.kind);
}

// ============================================================================
// 6. stage-name / age axis
// ============================================================================
function test_stage_axis(ctx) {
  const ICS = ctx.RCA_ICS_TABLE;
  const L = ctx.rcaVizLayout(STAGE_RESULT);
  check('stage-axis-kind', L.axis.kind === 'age' && L.axis.unit === 'Ma', L.axis.kind);
  check('stage-resolves-to-ma', L.bars[0].placed_by === 'age'
    && L.bars[0].value_young === ICS.Changhsingian.top_ma
    && L.bars[0].value_old === ICS.Wuchiapingian.base_ma);
  check('stage-domain', L.axis.domain.lo === ICS.Induan.top_ma
    && L.axis.domain.hi === ICS.Wuchiapingian.base_ma, JSON.stringify(L.axis.domain));
  // oldest (largest Ma) must sit at t = 0 (left edge)
  check('stage-oldest-at-left', approx(L.bars[0].u0, L.axis.band.u0, 1e-9)
    && L.bars[0].u1 > L.bars[0].u0, String(L.bars[0].u0));
  check('stage-cn-alias-places-like-english',
    approx(L.bars[2].u0, L.bars[0].u0) && approx(L.bars[2].u1, L.bars[0].u1),
    L.bars[2].u0 + '/' + L.bars[0].u0);
  check('stage-ticks-are-names', L.axis.ticks.length >= 2
    && L.axis.ticks.some((tk) => tk.label === 'Changhsingian')
    && L.axis.ticks.some((tk) => tk.label === 'Wuchiapingian'),
    JSON.stringify(L.axis.ticks.map((t) => t.label)));
  check('stage-bands-emitted', L.axis.stages.length === 3
    && L.axis.stages[0].u0 < L.axis.stages[L.axis.stages.length - 1].u1,
    JSON.stringify(L.axis.stages.map((s) => s.name)));
  check('stage-bands-ordered-oldest-first',
    L.axis.stages[0].name === 'Wuchiapingian' && L.axis.stages[2].name === 'Induan',
    JSON.stringify(L.axis.stages.map((s) => s.name)));
  const ma = ctx.rcaVizLayout({
    species_ranges: [
      { species: 'P ma', range_top: '251.902 Ma', range_base: '259.51 Ma' },
    ],
  });
  check('explicit-ma-reading-places', ma.bars[0].placed_by === 'age'
    && approx(ma.bars[0].value_young, 251.902), ma.bars[0].placed_by);
  const biozone = ctx.rcaVizLayout({
    species_ranges: [{ species: 'Q zone', range_top: '', range_base: '',
      biozone: 'N. optima Zone (latest Changhsingian)' }],
  });
  check('biozone-fallback-only-when-both-ends-silent',
    biozone.bars[0].placed_by === 'age'
    && biozone.bars[0].value_young === ICS.Changhsingian.top_ma
    && biozone.bars[0].value_old === ICS.Changhsingian.base_ma
    && biozone.bars[0].partial === false, biozone.bars[0].placed_by);
  const noEcho = ctx.rcaVizLayout({
    species_ranges: [
      { species: 'R mixed', range_top: 'Bed 4', range_base: 'Changhsingian' },
      { species: 'R bed too', range_top: 'Bed 9', range_base: 'Bed 1' },
    ],
  });
  check('half-bed-half-stage-row-uses-bed-not-biozone',
    noEcho.bars[0].placed_by === 'bed' && noEcho.bars[0].partial === true,
    noEcho.bars[0].placed_by + '/' + noEcho.bars[0].partial);
}

// ============================================================================
// 7. focus state machine (uPlot semantics, data side)
// ============================================================================
function test_focus(ctx) {
  const L = ctx.rcaVizLayout(BED_RESULT);
  const none = ctx.rcaVizFocusPass(L.bars, null);
  check('focus-none-full-alpha', none.filter((r) => r.placeable)
    .every((r) => r.alpha === 1 && !r.outline && !r.faded));
  check('focus-none-placeholders-muted', none.filter((r) => !r.placeable)
    .every((r) => approx(r.alpha, 0.34)));
  const f = ctx.rcaVizFocusPass(L.bars, 1);
  check('focus-target-solid', f[1].alpha === 1 && f[1].outline === true
    && f[1].focused === true && f[1].faded === false);
  check('focus-others-dimmed', approx(f[0].alpha, 0.3) && f[0].faded === true
    && approx(f[2].alpha, 0.3), String(f[0].alpha));
  check('focus-registry-covers-every-bar', f.length === L.bars.length
    && f.every((r, i) => r.row_index === i));
  check('focus-placeholder-not-double-dimmed', approx(f[3].alpha, 0.34)
    && f[3].faded === false && f[3].outline === false);
  check('focus-unknown-index-dims-all', ctx.rcaVizFocusPass(L.bars, 99)
    .filter((r) => r.placeable).every((r) => approx(r.alpha, 0.3) && !r.outline));
  check('focus-dim-configurable', approx(
    ctx.rcaVizFocusPass(L.bars, 1, { dim: 0.5 })[0].alpha, 0.5));
  const conf = [{ row_index: 0, placeable: true, faded_ok: true, confidence: 0.5 },
    { row_index: 1, placeable: true, faded_ok: true }];
  check('confidence-tints-base-alpha', approx(
    ctx.rcaVizFocusPass(conf, null)[0].alpha, 0.55 + 0.45 * 0.5));
  check('confidence-multiplies-through-focus', approx(
    ctx.rcaVizFocusPass(conf, 1)[0].alpha, 0.775 * 0.3, 1e-3));
  check('confidence_fade-off-restores-1', ctx.rcaVizFocusPass(conf, null,
    { confidence_fade: false })[0].alpha === 1);
  check('confidence-clamped', approx(
    ctx.rcaVizFocusPass([{ row_index: 0, placeable: true, confidence: 5 }], null)[0].alpha, 1));
  check('focus-pass-tolerates-junk', ctx.rcaVizFocusPass([null], 0).length === 1
    && ctx.rcaVizFocusPass(null, 0).length === 0);
  check('focus-dim-default-is-uplot-value', ctx.rcaViz.defaults.dim === 0.3
    && ctx.RCA_VIZ_DEFAULTS.placeholder === undefined
    && ctx.rcaViz.defaults.row_inset > 0);
  const mixedL = ctx.rcaVizLayout(MIXED_RESULT);
  const mf = ctx.rcaVizFocusPass(mixedL.bars, 2);
  check('focus-works-on-mixed-sources', mf[2].focused === true
    && approx(mf[0].alpha, 0.3) && mf[3].faded === false);
}

// ============================================================================
// 8. hover hit testing
// ============================================================================
function test_hit_test(ctx) {
  const L = ctx.rcaVizLayout(BED_RESULT);
  const b1 = barOf(L, 1);
  const centre = { u: b1.x + b1.w / 2, v: b1.y + b1.h / 2 };
  check('hit-inside-bar', ctx.rcaVizHitTest(L, centre.u, centre.v) === 1,
    JSON.stringify(centre));
  check('hit-edge-padding', ctx.rcaVizHitTest(L, b1.x - 0.003, centre.v) === 1);
  check('hit-outside-x', ctx.rcaVizHitTest(L, L.axis.band.u0 - 0.05, centre.v) === null);
  const b0 = barOf(L, 0);
  check('hit-upper-bar', ctx.rcaVizHitTest(L, b0.x + b0.w / 2, b0.y + b0.h / 2) === 0);
  check('hit-placeholder-strip', ctx.rcaVizHitTest(L,
    L.axis.band.u0 + 0.01, barOf(L, 3).y + barOf(L, 3).h / 2) === 3);
  check('hit-above-axis-band', ctx.rcaVizHitTest(L, b0.x + b0.w / 2, 0.005) === null);
  check('hit-below-foot', ctx.rcaVizHitTest(L, b0.x + b0.w / 2, 0.999) === null);
  check('hit-non-finite', ctx.rcaVizHitTest(L, NaN, 0.5) === null
    && ctx.rcaVizHitTest(L, 0.5, undefined) === null);
  check('hit-broken-layout', ctx.rcaVizHitTest(null, 0.5, 0.5) === null
    && ctx.rcaVizHitTest({ bars: [] }, 0.5, 0.5) === null);
  check('hit-overlap-resolves-top-down', (() => {
    // the padded boxes of adjacent rows overlap slightly; first match wins
    const v = b1.y - b1.h * 0.1;
    const hit = ctx.rcaVizHitTest(L, b1.x + b1.w / 2, v);
    return hit === 0 || hit === 1;
  })());
  check('hit-configurable-padding', ctx.rcaVizHitTest(L, b1.x - 0.05, centre.v,
    { hit_pad_u: 0.06 }) === 1
    && ctx.rcaVizHitTest(L, b1.x - 0.05, centre.v) === null);
  const geom = ctx.rcaVizLayout(GEOM_RESULT);
  const g0 = barOf(geom, 0);
  check('hit-geometry-bar-roundtrip', (() => {
    const u = g0.x + g0.w / 2, v = g0.y + g0.h / 2;
    const idx = ctx.rcaVizHitTest(geom, u, v);
    if (idx !== 0) return false;
    const back = geom.bars[idx].t0 + (geom.bars[idx].t1 - geom.bars[idx].t0) / 2;
    return approx(back, ((g0.u0 + g0.u1) / 2 - geom.axis.band.u0)
      / (geom.axis.band.u1 - geom.axis.band.u0), 1e-9);
  })());
}

// ============================================================================
// 9. layout options: frame / direction / section filter / degenerate domain
// ============================================================================
function test_options(ctx) {
  const frame = { x0: 0.1, y0: 0.2, x1: 0.9, y1: 0.95 };
  const L = ctx.rcaVizLayout(BED_RESULT, { frame });
  const band = L.axis.band;
  check('frame-band-mapped', approx(band.u0, 0.1 + 0.22 * 0.8, 1e-9)
    && approx(band.u1, 0.9 - 0.015 * 0.8, 1e-9), band.u0 + '/' + band.u1);
  check('frame-keeps-bars-inside-image-frame', L.bars.every((b) =>
    b.x >= frame.x0 - 1e-9 && b.x + b.w <= frame.x1 + 1e-9));
  check('frame-keeps-rows-inside-frame', L.bars.every((b) =>
    b.y >= frame.y0 - 1e-9 && b.y + b.h <= frame.y1 + 1e-9));
  const R = ctx.rcaVizLayout(BED_RESULT, { direction: 'younger-right' });
  const LL = ctx.rcaVizLayout(BED_RESULT, { direction: 'younger-left' });
  check('direction-mirrors-bars', approx(LL.bars[1].x,
    R.axis.band.u1 - (R.bars[1].x + R.bars[1].w - R.axis.band.u0))
    && approx(LL.bars[1].w, R.bars[1].w), LL.bars[1].x + '');
  check('direction-reported', LL.axis.direction === 'younger-left'
    && R.axis.direction === 'younger-right');
  check('direction-ticks-mirror', approx(
    LL.axis.ticks[0].u, R.axis.band.u1 - (R.axis.ticks[0].u - R.axis.band.u0)));
  const sect = ctx.rcaVizLayout(MULTI_SECTION, { section: 'S1' });
  check('section-filter-keeps-row-index', sect.bars.length === 2
    && sect.bars[1].placeable === false && sect.bars[1].w === 0
    && sect.bars[1].reason === 'other_section');
  check('section-filter-not-reported-unplaceable',
    sect.axis.unplaceable.length === 0 && sect.axis.filtered === 1,
    JSON.stringify(sect.axis.unplaceable));
  const flat = ctx.rcaVizLayout({
    species_ranges: [{ species: 'F one', range_top: 'Bed 3', range_base: 'Bed 3' }],
  });
  check('single-value-domain-degenerate', flat.axis.degenerate_domain === true
    && approx(flat.bars[0].t0, 0.5) && flat.bars[0].placeable === true);
  const custom = ctx.rcaVizLayout(BED_RESULT, { label_width: 0.4, min_bar_w: 0.05 });
  check('label_width-option', approx(custom.axis.band.u0, 0.4)
    && approx(custom.bars[2].w, 0.05, 1e-9), String(custom.bars[2].w));
  check('row-height-per-row', approx(flat.axis.row_h, (1 - 0.055 - 0.03), 1e-9),
    String(flat.axis.row_h));
  check('meta-describes-space', flat.meta.space === 'normalised-image-px'
    && flat.meta.pos_scale === 999);
  check('ticks-bounded-by-max', ctx.rcaVizLayout(BED_RESULT, { max_ticks: 3 })
    .axis.ticks.length <= 5);
}

// ============================================================================
// 10. FE-FIX-2026-09-21 (pure layer): sign-aware Ma parse + section gating
// ============================================================================
function test_fix20260921_pure(ctx) {
  // item 3 — rcaVizMaValue must keep the sign (mirrors rcaVizNum).
  const mv = ctx.rcaVizMaValue;
  check('fix3-ma-negative', mv('-5 Ma') === -5, String(mv('-5 Ma')));
  check('fix3-ma-plus-sign', mv('+5.5 Ma') === 5.5);
  check('fix3-ma-bare-still-positive', mv('5 Ma') === 5);
  check('fix3-ma-cn-negative', mv('-2 百万年') === -2);
  check('fix3-ma-junk-null', mv('Changhsingian') === null && mv('') === null);
  const neg = ctx.rcaVizLayout({
    species_ranges: [
      { species: 'N neg', range_top: '-5 Ma', range_base: '10 Ma' },
      { species: 'N pos', range_top: '2 Ma', range_base: '8 Ma' },
    ],
  });
  check('fix3-negative-ma-keeps-domain',
    neg.axis.domain.lo === -5 && neg.axis.domain.hi === 10,
    JSON.stringify(neg.axis.domain));
  check('fix3-negative-ma-keeps-placement',
    approx(neg.bars[0].t0, 0) && approx(neg.bars[0].t1, 1)
    && neg.bars[0].value_young === -5,
    neg.bars[0].t0 + '..' + neg.bars[0].t1);
  // item 8 — section-filtered rows are inert for draw + hit testing.
  const sect = ctx.rcaVizLayout(MULTI_SECTION, { section: 'S1' });
  const b0 = sect.bars[0], b1 = sect.bars[1];
  check('fix8-barInView-predicate', ctx.rcaVizBarInView(b0) === true
    && ctx.rcaVizBarInView(b1) === false);
  check('fix8-filtered-row-not-hittable',
    ctx.rcaVizHitTest(sect, b1.x, b1.y + b1.h / 2) === null);
  check('fix8-visible-row-still-hittable',
    ctx.rcaVizHitTest(sect, b0.x + b0.w / 2, b0.y + b0.h / 2) === 0);
  check('fix8-junk-bar-in-view-tolerated',
    ctx.rcaVizBarInView(null) === true && ctx.rcaVizBarInView({}) === true);
}

// ============================================================================
// 11. FE-FIX-2026-09-21 (render layer): minimal DOM stub so the browser half
//     of js/viz.js — pin/hover/leave, destroy hygiene, flash tokens, stage
//     width, locateTo offsets, clear() unmount — is actually exercised.
// ============================================================================
function makeStub2d(counters) {
  const c = {};
  const track = function (name) {
    c[name] = function () { counters[name] = (counters[name] || 0) + 1; };
  };
  ['clearRect', 'fillRect', 'beginPath', 'moveTo', 'lineTo', 'stroke',
    'fill', 'closePath', 'save', 'restore', 'translate', 'rotate',
    'setLineDash'].forEach(track);
  // strokeRect marks the hatched placeholder strip — exactly what a filtered
  // row must NOT produce (item 8, draw side).
  c.strokeRect = function () {
    counters.strokeRect = (counters.strokeRect || 0) + 1;
  };
  c.fillText = function (text) {
    counters.fillText = (counters.fillText || 0) + 1;
    (counters.fillTextArgs = counters.fillTextArgs || []).push(String(text));
  };
  c.measureText = function (t) { return { width: 6 * String(t).length }; };
  return c;
}

function buildDomContext() {
  const base = buildContext();
  const counters = {};
  const stub2d = makeStub2d(counters);
  function queryDescendant(root, cls) {
    for (const c of root.children || []) {
      if (!c || typeof c.className !== 'string') continue;
      if (c.className.split(/\s+/).indexOf(cls) !== -1) return c;
      const hit = queryDescendant(c, cls);
      if (hit) return hit;
    }
    return null;
  }
  function makeEl(tag) {
    const el = {
      tagName: String(tag).toUpperCase(), className: '', children: [],
      attrs: {}, style: {}, listeners: {},
      clientWidth: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0,
      offsetTop: 0, offsetParent: null, scrollTop: 0,
      naturalWidth: 0, naturalHeight: 0, textContent: '',
      appendChild(child) { this.children.push(child); return child; },
      setAttribute(k, v) { this.attrs[k] = String(v); },
      getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; },
      removeAttribute(k) { delete this.attrs[k]; },
      addEventListener(t, fn) {
        (this.listeners[t] = this.listeners[t] || []).push(fn);
      },
      removeEventListener(t, fn) {
        const a = this.listeners[t] || [];
        const i = a.indexOf(fn);
        if (i !== -1) a.splice(i, 1);
      },
      dispatch(t, ev) {
        (this.listeners[t] || []).slice().forEach((fn) => fn(ev));
      },
      count(t) { return (this.listeners[t] || []).length; },
      querySelector(sel) { return queryDescendant(this, sel.replace(/^\./, '')); },
      getBoundingClientRect() {
        return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight };
      },
    };
    if (tag === 'canvas') {
      el.width = 0; el.height = 0;
      el.getContext = (kind) => (kind === '2d' ? stub2d : null);
    }
    return el;
  }
  const documentStub = {
    createElement: makeEl,
    documentElement: makeEl('html'),
    listeners: {},
  };
  documentStub.addEventListener = function (t, fn) {
    (this.listeners[t] = this.listeners[t] || []).push(fn);
  };
  documentStub.removeEventListener = function (t, fn) {
    const a = this.listeners[t] || [];
    const i = a.indexOf(fn);
    if (i !== -1) a.splice(i, 1);
  };
  documentStub.dispatch = function (t, ev) {
    (this.listeners[t] || []).slice().forEach((fn) => fn(ev));
  };
  documentStub.count = function (t) { return (this.listeners[t] || []).length; };
  const windowStub = {
    innerHeight: 1000, devicePixelRatio: 1, listeners: {},
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
  };
  windowStub.addEventListener = function (t, fn) {
    (this.listeners[t] = this.listeners[t] || []).push(fn);
  };
  windowStub.removeEventListener = function (t, fn) {
    const a = this.listeners[t] || [];
    const i = a.indexOf(fn);
    if (i !== -1) a.splice(i, 1);
  };
  windowStub.count = function (t) { return (this.listeners[t] || []).length; };
  const observers = [];
  const rafQ = [];
  const ctx = Object.assign(base, {
    document: documentStub,
    window: windowStub,
    MutationObserver: function (cb) {
      const o = { cb: cb, disconnected: false, observed: null,
        observe(el, opts) { this.observed = { el: el, opts: opts }; },
        disconnect() { this.disconnected = true; } };
      observers.push(o);
      return o;
    },
    requestAnimationFrame: (fn) => { rafQ.push(fn); return rafQ.length; },
  });
  ctx.globalThis = ctx;
  return { ctx: ctx, makeEl: makeEl, counters: counters, observers: observers,
    rafQ: rafQ, documentStub: documentStub, windowStub: windowStub };
}

function test_fix20260921_render() {
  const dom = buildDomContext();
  const ctx = dom.ctx;
  loadAll(ctx);
  const S = ctx.RCA_VIZ_STATE;
  const VIZ = ctx.rcaViz;
  const L = ctx.rcaVizLayout(BED_RESULT);
  const host = dom.makeEl('div');
  host.className = 'viz-host';
  host.clientWidth = 600;   // the padded #viz-host box
  host.clientHeight = 300;
  // A consumer that mimics js/table.js EXACTLY: hover -> focusRow(idx);
  // null -> class removal only (no clearFocus). The pinned model must work
  // without relying on the consumer doing anything extra.
  const seen = [];
  VIZ.onRowHover((idx, layout) => {
    seen.push(idx);
    if (idx !== null && idx !== undefined) {
      if (!layout) seen.push('no-layout'); // contract: 2nd arg is the layout
      VIZ.focusRow(idx);
    }
  });
  check('fix-render-initial', VIZ.render(host, BED_RESULT, null, {}) === true
    && VIZ.getState().mounted === true && VIZ.getState().pinned === null);
  // item 6: with the stage measured, canvases size to the STAGE (576), not
  // to the padded host (600).
  const stage = S.stage, hoverCv = S.hoverCv, barsCv = S.barsCv;
  stage.clientWidth = 576; stage.offsetTop = 40; stage.offsetParent = host;
  hoverCv.clientWidth = 576; hoverCv.clientHeight = 220;
  check('fix6-stage-box', ctx.rcaViz.resize() === true
    && barsCv.width === 576 && hoverCv.width === 576, String(barsCv.width));
  const cssH = parseInt(stage.style.height, 10) || 220;
  const ptOf = (bar) => ({
    clientX: (bar.x + bar.w / 2) * 576,
    clientY: (bar.y + bar.h / 2) * cssH,
  });
  // items 1+2: hover focuses transiently; a click PINS; the hover-then-click
  // round-trip through the consumer no longer unpins.
  hoverCv.dispatch('mousemove', ptOf(L.bars[0]));
  check('fix1-hover-focuses-transiently', S.hover === 0 && S.focus === 0
    && S.pinned === null);
  hoverCv.dispatch('click', ptOf(L.bars[0]));
  check('fix1-click-pins', S.pinned === 0 && S.focus === 0);
  hoverCv.dispatch('mousemove', ptOf(L.bars[1]));
  check('fix1-hover-cannot-steal-pin', S.hover === 1 && S.focus === 0
    && S.pinned === 0);
  hoverCv.dispatch('mouseleave');
  check('fix2-leave-restores-pin', S.hover === null && S.focus === 0);
  hoverCv.dispatch('click', ptOf(L.bars[1]));
  check('fix1-click-other-row-repins', S.pinned === 1 && S.focus === 1);
  S.hover = 1; // as if the pointer moved onto the row before the click
  hoverCv.dispatch('click', ptOf(L.bars[1]));
  check('fix1-reclick-unpins-stays-on-hover', S.pinned === null
    && S.focus === 1, 'focus=' + S.focus);
  S.hover = null;
  hoverCv.dispatch('click', { clientX: 300, clientY: cssH - 2 });
  check('fix1-empty-click-clears', S.pinned === null && S.focus === null);
  // item 2: un-pinned leave clears the transient focus (no stale x0.3).
  hoverCv.dispatch('mousemove', ptOf(L.bars[0]));
  check('fix2-transient-focus-set', S.focus === 0);
  hoverCv.dispatch('mouseleave');
  check('fix2-leave-clears-focus', S.hover === null && S.focus === null);
  // clearFocus: works unpinned, refuses to break a pin; Esc releases it.
  VIZ.focusRow(2);
  check('fix1-clearFocus-unpinned', VIZ.clearFocus() === true && S.focus === null);
  hoverCv.dispatch('click', ptOf(L.bars[0]));
  check('fix1-clearFocus-keeps-pin', VIZ.clearFocus() === false && S.focus === 0);
  hoverCv.dispatch('mouseleave');
  dom.documentStub.dispatch('keydown', { key: 'Escape' });
  check('fix1-esc-unpins', S.pinned === null && S.focus === null);
  // the hover callback contract: only numbers/null ever emitted.
  check('fix1-cb-contract-intact', seen.length > 0
    && seen.indexOf('no-layout') === -1
    && seen.every((v) => v === null || typeof v === 'number'), JSON.stringify(seen));
  check('fix1-cb-null-on-leave', seen.indexOf(null) !== -1);
  // items 5+6: two consecutive locateTo calls — the older rAF loop must not
  // kill the newer flash, and the scroll counts the ~40px .viz-head offset.
  dom.rafQ.length = 0;
  const okA = VIZ.locateTo(0);
  const tokenA = S.flash.token;
  host.scrollTop = 0;
  const okB = VIZ.locateTo(3);
  const b3 = L.bars[3];
  const expectedTop = Math.max(0, 40 + b3.y * cssH - host.clientHeight / 2);
  check('fix5-locate-returns-true', okA === true && okB === true);
  check('fix5-flash-tokenised', S.flash.row_index === 3
    && S.flash.token !== tokenA, JSON.stringify(S.flash));
  check('fix6-scroll-uses-stage-offset', host.scrollTop === expectedTop,
    host.scrollTop + ' vs ' + expectedTop);
  const stale = dom.rafQ.shift(); // first queued tick belongs to locateTo(0)
  stale();
  check('fix5-stale-loop-harms-nothing', !!S.flash && S.flash.row_index === 3);
  let guard = 0;
  while (dom.rafQ.length && guard++ < 60) dom.rafQ.shift()();
  check('fix5-current-loop-still-ends', S.flash === null && guard < 60);
  // item 8 (draw side): a section-filtered row paints no strip and no label.
  dom.counters.strokeRect = 0;
  dom.counters.fillTextArgs = [];
  VIZ.render(host, MULTI_SECTION, null, { section: 'S1' });
  ctx.rcaViz.resize();
  check('fix8-draw-skips-filtered-label',
    dom.counters.fillTextArgs.indexOf('X b') === -1
    && dom.counters.fillTextArgs.indexOf('X a') !== -1,
    JSON.stringify(dom.counters.fillTextArgs));
  check('fix8-draw-skips-filtered-strip', !dom.counters.strokeRect,
    'strokeRect=' + dom.counters.strokeRect);
  // item 7: clear() unmounts (host dropped), render() re-mounts.
  VIZ.render(host, BED_RESULT, null, {});
  check('fix7-clear-unmounts', VIZ.clear() === true
    && ctx.rcaViz.getState().mounted === false && S.host === null);
  check('fix7-render-reinit', VIZ.render(host, BED_RESULT, null, {}) === true
    && ctx.rcaViz.getState().mounted === true);
  // item 4: destroy removes EVERY handler + disconnects the observer, and a
  // later render re-registers exactly one copy of each (no duplicates).
  check('fix4-wired-before-destroy', hoverCv.count('mousemove') === 1
    && hoverCv.count('touchstart') === 1
    && dom.documentStub.count('keydown') === 1
    && dom.windowStub.count('resize') === 1
    && dom.observers.length >= 1, JSON.stringify({
      mm: hoverCv.count('mousemove'), ts: hoverCv.count('touchstart'),
      kd: dom.documentStub.count('keydown'), rz: dom.windowStub.count('resize'),
      mo: dom.observers.length }));
  const observerBefore = dom.observers.length;
  check('fix4-destroy-ok', VIZ.destroy() === true);
  check('fix4-destroy-removes-every-handler',
    hoverCv.count('mousemove') === 0 && hoverCv.count('mouseleave') === 0
    && hoverCv.count('click') === 0 && hoverCv.count('touchstart') === 0
    && dom.documentStub.count('keydown') === 0
    && dom.windowStub.count('resize') === 0
    && S.themeObserver === null
    && dom.observers[observerBefore - 1].disconnected === true
    && VIZ.getState().listeners === 0, JSON.stringify({
      mm: hoverCv.count('mousemove'), kd: dom.documentStub.count('keydown'),
      rz: dom.windowStub.count('resize') }));
  VIZ.render(host, BED_RESULT, null, {});
  check('fix4-rerender-registers-exactly-once',
    hoverCv.count('mousemove') === 1 && hoverCv.count('mouseleave') === 1
    && hoverCv.count('click') === 1 && hoverCv.count('touchstart') === 1
    && dom.documentStub.count('keydown') === 1
    && dom.windowStub.count('resize') === 1
    && dom.observers.length === observerBefore + 1
    && dom.observers[observerBefore].observed !== null);
  VIZ.destroy();
}

// ---- run -------------------------------------------------------------------

const ctx = buildContext();
loadAll(ctx);
test_dom_free(ctx);
test_parsers(ctx);
test_bed_axis(ctx);
test_unplaceable(ctx);
test_geometry(ctx);
test_stage_axis(ctx);
test_focus(ctx);
test_hit_test(ctx);
test_options(ctx);
test_fix20260921_pure(ctx);
test_fix20260921_render();

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
