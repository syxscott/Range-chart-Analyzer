#!/usr/bin/env node
/**
 * FIX-2026-10-28 — js/quality.js mirror-drift regression suite.
 *
 * Five confirmed divergences from the Python twin rca_core/quality.py (and
 * rca_core/standards/ics.py), each pinned here with a minimal payload:
 *
 *   1. _score_steno_biozone (js ~1450) sorted EVERY range_top through
 *      _parseBedN. Python quality.py:1108-1151 routes age-looking tops
 *      through -Ma, keeps beds on the subscript-aware bed_position, and
 *      skips a section mixing the two notations. The old mirror flagged
 *      correctly ordered "254.14 Ma"/"259.5 Ma" sections and missed
 *      same-bed-index reversals ("23a" vs "23c").
 *   2. _resolveAgeBound step 2 returned on the FIRST Chinese alias hit;
 *      Python ics.py:695-704 claims a text only when EXACTLY ONE alias
 *      matches and otherwise falls through to the max-bases/min-tops stage
 *      range rule. Python also registers the aliases into _STAGE_PATTERNS
 *      (ics.py:393-397) so ics_parse_age_range sees a Chinese age_range —
 *      the JS mirror fed only English stage names in, so a reversed
 *      "长兴阶-吴家坪阶" section was invisible to the stage-order check.
 *   3. The ICS stage-order check awarded ONE accuracy check per section
 *      with a proportional pass fraction; Python quality.py:731-734 counts
 *      each VIOLATION as one failed check and a clean section adds nothing.
 *   4. A non-object entry in chimera_warnings deducted 0.1 but emitted no
 *      message; Python quality.py:841-846 renders it verbatim (str[:200]).
 *   5. No per-dimension exception guard: one scorer throwing took the whole
 *      badge down. Python quality.py:1599-1611 scores the failed dimension
 *      0.0 and emits quality.scoring_failed.
 *
 * Offline, no secrets. Same vm-sandbox loading pattern as
 * tests_contract_mirror_2026_09_22.js (ics_table -> reason-codes ->
 * bed_parser -> quality, as in index.html).
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

const ROOT = REPO;
const SCRIPTS = [
  'js/ics_table.js',
  'js/reason-codes.js',
  'js/bed_parser.js',
  'js/quality.js',
];

function buildContext() {
  const ctx = { console };
  ctx.globalThis = ctx;
  ctx.window = ctx;
  ctx.self = ctx;
  vm.createContext(ctx);
  for (const rel of SCRIPTS) {
    vm.runInContext(fs.readFileSync(path.join(ROOT, rel), 'utf8'), ctx,
      { filename: rel });
  }
  return ctx;
}

const ctx = buildContext();
const run = (code) => vm.runInContext(code, ctx, { filename: 'test-body' });

let passed = 0;
let failed = 0;
function check(name, cond, detail) {
  if (cond) {
    passed += 1;
    console.log('PASS ' + name);
  } else {
    failed += 1;
    console.log('FAIL ' + name + (detail !== undefined ? ' — ' + detail : ''));
  }
}

function issuesOf(result, key) {
  // scoreRangeChart returns {issues}; the four dimension scorers return
  // [score, issues]. Accept both shapes.
  const list = (result && !Array.isArray(result) && result.issues)
    ? result.issues
    : (Array.isArray(result) ? result[1] : []);
  return list.filter((i) => i.msg_key === key);
}

// ---------------------------------------------------------------------------
// Fix 1 — Steno biozone order: age sections sort by -Ma, beds stay
// index-aware, mixed notations are skipped (quality.py:1108-1151).
// ---------------------------------------------------------------------------

(function test_steno_ma_ordering() {
  // (a) REGRESSION (false positive): a correctly ordered age-notation
  // section — older Wuchiapingian zone LOWER at 259.5 Ma, younger
  // Changhsingian zone UPPER at 254.14 Ma. The old mirror read both labels
  // as beds 259 / 254, put the younger zone at the LOWER slot and flagged.
  const ok = run(`
    scoreConsistency({
      species_ranges: [
        { species: 'A', section: 'S1', range_top: '259.5 Ma',
          biozone: 'Clarkina orientalis' },
        { species: 'B', section: 'S1', range_top: '254.14 Ma',
          biozone: 'Clarkina changxingensis' },
      ],
      sections: [{ name: 'S1', age_range: '259.51-251.902 Ma' }],
    })
  `);
  check('fix1a: correct Ma-ordered section is NOT a steno violation',
    issuesOf(ok, 'quality.biozone_order_violation').length === 0
      && ok[0] === 1, JSON.stringify(ok));

  // (b) TRUE POSITIVE, age notation: the younger Changhsingian zone sits at
  // the OLDER (deeper, 259.5 Ma) position while the older Wuchiapingian
  // zone sits above it at 254.14 Ma. The old mirror sorted by the leading
  // integer (254 < 259) and put B in the lower slot — the violation was
  // invisible for every age-notation section. -Ma ordering catches it.
  const revAge = run(`
    scoreConsistency({
      species_ranges: [
        { species: 'A', section: 'S1', range_top: '259.5 Ma',
          biozone: 'Clarkina changxingensis' },
        { species: 'B', section: 'S1', range_top: '254.14 Ma',
          biozone: 'Clarkina orientalis' },
      ],
      sections: [{ name: 'S1', age_range: '259.51-251.902 Ma' }],
    })
  `);
  const revAgeIssues = issuesOf(revAge, 'quality.biozone_order_violation');
  check('fix1b: reversed Ma-ordered section IS flagged',
    revAgeIssues.length === 1
      && revAgeIssues[0].params.species === 'A'
      && revAgeIssues[0].params.younger_biozone === 'Clarkina changxingensis'
      && revAgeIssues[0].params.older_biozone === 'Clarkina orientalis',
    JSON.stringify(revAge));
  check('fix1b: violation deducts 0.1 from consistency',
    Math.abs(revAge[0] - 0.9) < 1e-9, String(revAge[0]));

  // (c) REGRESSION (missed violation): both rows on the SAME bed number,
  // subscripts deciding. B@23c carries the OLDER Wuchiapingian zone and sits
  // ABOVE A@23a with the younger Changhsingian zone. _parseBedN collapsed
  // both to 23 and the stable tie kept input order [B, A] — no violation.
  // bed_position separates them: 23a = 23.019..., 23c = 23.096....
  const subRev = run(`
    scoreConsistency({
      species_ranges: [
        { species: 'B', section: 'S1', range_top: '23c',
          biozone: 'Clarkina orientalis' },
        { species: 'A', section: 'S1', range_top: '23a',
          biozone: 'Clarkina changxingensis' },
      ],
      sections: [{ name: 'S1', age_range: '253-251 Ma' }],
    })
  `);
  const subRevIssues = issuesOf(subRev, 'quality.biozone_order_violation');
  check('fix1c: same-bed subscript reversal IS flagged (index-aware)',
    subRevIssues.length === 1 && subRevIssues[0].params.species === 'A',
    JSON.stringify(subRev));

  // (d) The correctly nested same-bed pair stays clean: older zone at 23a
  // BELOW, younger zone at 23c ABOVE.
  const subOk = run(`
    scoreConsistency({
      species_ranges: [
        { species: 'A', section: 'S1', range_top: '23a',
          biozone: 'Clarkina orientalis' },
        { species: 'B', section: 'S1', range_top: '23c',
          biozone: 'Clarkina changxingensis' },
      ],
      sections: [{ name: 'S1', age_range: '253-251 Ma' }],
    })
  `);
  check('fix1d: same-bed correctly nested pair is clean',
    issuesOf(subOk, 'quality.biozone_order_violation').length === 0
      && subOk[0] === 1, JSON.stringify(subOk));

  // (e) A section mixing bed and age notations has no single ordering
  // scale — Python skips it entirely (kinds > 1). The old mirror compared
  // bed 23 against "bed 254" read out of "254 Ma".
  const mixed = run(`
    scoreConsistency({
      species_ranges: [
        { species: 'A', section: 'S1', range_top: '23',
          biozone: 'Clarkina changxingensis' },
        { species: 'B', section: 'S1', range_top: '259.5 Ma',
          biozone: 'Clarkina orientalis' },
      ],
      sections: [{ name: 'S1', age_range: '259.51-251.902 Ma' }],
    })
  `);
  check('fix1e: bed+age mixed section is skipped, not compared',
    issuesOf(mixed, 'quality.biozone_order_violation').length === 0
      && mixed[0] === 1, JSON.stringify(mixed));

  // (f) bed_position unit values (mirror of bed_parser.bed_position:
  // "23a" -> 23 + 0.5/26, "23c" -> 23 + 2.5/26, non-bed -> null).
  const pos = run(`
    [_bedPosition('23'), _bedPosition('23a'), _bedPosition('23c'),
     _bedPosition('254 Ma')]
  `);
  check('fix1f: bed_position mirrors bed_parser.bed_position',
    pos[0] === 23
      && Math.abs(pos[1] - (23 + 0.5 / 26)) < 1e-12
      && Math.abs(pos[2] - (23 + 2.5 / 26)) < 1e-12
      && pos[3] === null,
    JSON.stringify(pos));
})();

// ---------------------------------------------------------------------------
// Fix 2 — Chinese stage resolution mirrors ics.py:695-704 (exactly-one
// alias) and ics.py:393-397 (aliases registered as age_range patterns).
// ---------------------------------------------------------------------------

(function test_chinese_alias_resolution() {
  // (a) Exactly one alias: claimed, with the stage's own prefer bound.
  const single = run(`
    [_resolveAgeBound('吴家坪阶', 'older'),
     _resolveAgeBound('吴家坪阶', 'younger')]
  `);
  check('fix2a: single Chinese alias resolves per prefer',
    single[0] && single[0].name === 'Wuchiapingian' && single[0].ma === 259.51
      && single[1] && single[1].name === 'Wuchiapingian' && single[1].ma === 254.14,
    JSON.stringify(single));

  // (b) REGRESSION: a Chinese RANGE ("吴家坪阶-长兴阶") has TWO alias hits.
  // It must fall through to step 3 and resolve with max-of-bases /
  // min-of-tops — the old mirror returned the FIRST alias (吴家坪阶) for
  // both ends, a zero-length interval.
  const range = run(`
    [_resolveAgeBound('吴家坪阶-长兴阶', 'older'),
     _resolveAgeBound('吴家坪阶-长兴阶', 'younger')]
  `);
  check('fix2b: two-alias range resolves max-base/min-top',
    range[0] && range[0].name === 'Wuchiapingian' && range[0].ma === 259.51
      && range[1] && range[1].name === 'Changhsingian' && range[1].ma === 251.902,
    JSON.stringify(range));

  // (c) Reversed Chinese range: prefer="younger" is still the younger end,
  // independent of the order the aliases appear in the text.
  const reversed = run(`
    _resolveAgeBound('长兴阶-吴家坪阶', 'younger')
  `);
  check('fix2c: reversed Chinese range honors prefer=younger',
    reversed && reversed.name === 'Changhsingian' && reversed.ma === 251.902,
    JSON.stringify(reversed));

  // (d) The aliases are age_range patterns now (ics.py:393-397): a Chinese
  // age_range parses to its canonical stages in text order.
  const parsed = run(`
    [_icsParseAgeRange(globalThis.RCA_ICS_TABLE, '吴家坪阶-长兴阶'),
     _icsParseAgeRange(globalThis.RCA_ICS_TABLE, '长兴阶-吴家坪阶')]
  `);
  check('fix2d: Chinese age_range parses to canonical stages',
    JSON.stringify(parsed[0]) === JSON.stringify(['Wuchiapingian', 'Changhsingian'])
      && JSON.stringify(parsed[1]) === JSON.stringify(['Changhsingian', 'Wuchiapingian']),
    JSON.stringify(parsed));

  // (e) REGRESSION (false range_top_lt_base): a compound Chinese range_top
  // resolved to Wuchiapingian for BOTH ends before, so base "长兴阶"
  // (254.14) < top (259.51) looked inverted. Now the compound resolves to
  // Changhsingian 251.902 on the younger end and the row is well-formed.
  const fadlad = run(`
    scoreAccuracy({
      species_ranges: [
        { species: 'A', section: 'S1',
          range_base: '长兴阶', range_top: '吴家坪阶-长兴阶' },
      ],
      sections: [{ name: 'S1', age_range: '吴家坪阶-长兴阶' }],
      confidence: 0.9,
    })
  `);
  check('fix2e: compound Chinese range_top is not a false FAD<LAD violation',
    issuesOf(fadlad, 'quality.range_top_lt_base').length === 0,
    JSON.stringify(fadlad));

  // (f) REGRESSION (invisible reversal): a reversed Chinese section
  // age_range parses to [Changhsingian, Wuchiapingian] and the stage-order
  // check flags it — before the alias feed it parsed to [] and the section
  // was never checked.
  const revSection = run(`
    scoreAccuracy({
      sections: [{ name: 'S1', age_range: '长兴阶-吴家坪阶' }],
    })
  `);
  const soIssues = issuesOf(revSection, 'quality.stage_order_reversed');
  check('fix2f: reversed Chinese section IS flagged by the stage-order check',
    soIssues.length === 1 && soIssues[0].params.section === 'S1'
      && soIssues[0].params.detail === 'Changhsingian above Wuchiapingian',
    JSON.stringify(revSection));
})();

// ---------------------------------------------------------------------------
// Fix 3 — stage-order scoring counts each VIOLATION as one failed check;
// clean sections add nothing (quality.py:731-734).
// ---------------------------------------------------------------------------

(function test_stage_order_scoring() {
  // (a) Hand-computed: one species row with an inverted bed pair (accuracy
  // checks: section-ref 1/1, FAD<LAD 0/1) PLUS a clean two-stage section.
  // Python: the clean section adds NOTHING -> 1/2 = 0.5. The old mirror
  // awarded the clean section a free pass -> (1+0+1)/3 = 0.666...
  const clean = run(`
    scoreAccuracy({
      species_ranges: [
        { species: 'A', section: 'S1', range_top: '1', range_base: '2' },
      ],
      sections: [{ name: 'S1', age_range: 'Wuchiapingian - Changhsingian' }],
    })
  `);
  check('fix3a: clean multi-stage section adds no accuracy check (0.5)',
    Math.abs(clean[0] - 0.5) < 1e-9
      && issuesOf(clean, 'quality.range_top_lt_base').length === 1
      && issuesOf(clean, 'quality.stage_order_reversed').length === 0,
    JSON.stringify(clean));

  // (b) Hand-computed: three-stage section, exactly ONE of the two adjacent
  // pairs reversed (Changhsingian < Induan is fine; Induan above
  // Wuchiapingian is reversed). Python: two failed-pair checks, 0 passed
  // -> 0.0. No other accuracy check runs (no species rows), so the old
  // mirror read (1 - 1/2) = 0.5 for the same input.
  const oneOfTwo = run(`
    scoreAccuracy({
      sections: [{ name: 'S1',
        age_range: 'Changhsingian - Induan - Wuchiapingian' }],
    })
  `);
  check('fix3b: one reversed pair of two -> accuracy 0.0, one issue',
    Math.abs(oneOfTwo[0] - 0.0) < 1e-9
      && issuesOf(oneOfTwo, 'quality.stage_order_reversed').length === 1
      && issuesOf(oneOfTwo, 'quality.stage_order_reversed')[0].params.detail
        === 'Induan above Wuchiapingian',
    JSON.stringify(oneOfTwo));

  // (c) End-to-end math through the badge, hand-computed:
  //   completeness 5/5 = 1.0; accuracy 1/2 = 0.5 (species-ref passes, the
  //   FAD<LAD violation fails, and the CLEAN stage section adds nothing);
  //   consistency 0.85 (0.1 FAD<LAD + 0.05 missing biozone); structure 0.5
  //   (extras passes; sections/biozones/other_fossils null-check has 2
  //   nulls -> nothing passes).
  //   composite = .3 + .4*.5 + .2*.85 + .1*.5 = 0.72 -> C.
  const e2e = run(`
    scoreRangeChart({
      species_ranges: [
        { species: 'A', section: 'S1', range_top: '1', range_base: '2' },
      ],
      sections: [{ name: 'S1', age_range: 'Wuchiapingian - Changhsingian' }],
      confidence: 0.9,
    })
  `);
  check('fix3c: composite mirrors per-violation math (0.72 / C)',
    e2e.score === 0.72 && e2e.grade === 'C',
    JSON.stringify({ score: e2e.score, grade: e2e.grade }));
})();

// ---------------------------------------------------------------------------
// Fix 4 — non-object chimera_warnings entries emit quality.chimera_dropped
// with the entry rendered verbatim (quality.py:841-846).
// ---------------------------------------------------------------------------

(function test_chimera_non_object() {
  const r = run(`
    scoreConsistency({ chimera_warnings: [
      { row: { species: 'A' } },
      'free-form warning',
      42,
    ] })
  `);
  const chim = issuesOf(r, 'quality.chimera_dropped');
  check('fix4: every chimera entry emits a message (dict + string + number)',
    chim.length === 3
      && chim[0].severity === 'warning'
      && chim[0].params.row === '{"species":"A"}'
      && chim[1].params.row === 'free-form warning'
      && chim[2].params.row === '42',
    JSON.stringify(chim));
  check('fix4: deduction unchanged (1 - min(0.5, 0.3))',
    Math.abs(r[0] - 0.7) < 1e-9, String(r[0]));

  // Over 200 chars: verbatim render is truncated to 200, like str()[:200].
  const long = run(`
    scoreConsistency({ chimera_warnings: ['x'.repeat(500)] })
  `);
  check('fix4: non-object render truncates at 200 chars',
    issuesOf(long, 'quality.chimera_dropped').length === 1
      && issuesOf(long, 'quality.chimera_dropped')[0].params.row.length === 200,
    String(issuesOf(long, 'quality.chimera_dropped')[0]
      && issuesOf(long, 'quality.chimera_dropped')[0].params.row.length));
})();

// ---------------------------------------------------------------------------
// Fix 5 — per-dimension exception guard (quality.py:1599-1611): a throwing
// scorer scores 0.0 for its dimension, emits quality.scoring_failed, and
// the other three dimensions still grade.
// ---------------------------------------------------------------------------

(function test_per_dimension_guard() {
  // Patch scoreConsistency to throw, exactly the failure the guard exists
  // for. Before the fix the exception propagated and scoreRangeChart threw
  // — no badge at all.
  run(`
    globalThis.__realScoreConsistency = scoreConsistency;
    scoreConsistency = function () { throw new TypeError('boom'); };
  `);
  let r = null;
  let threw = false;
  try {
    r = run(`
      scoreRangeChart({
        species_ranges: [
          { species: 'A', section: 'S1', range_top: '1', range_base: '2',
            biozone: 'Zone X' },
        ],
        sections: [{ name: 'S1' }],
        confidence: 0.9,
      })
    `);
  } catch (e) {
    threw = true;
  }
  run('scoreConsistency = globalThis.__realScoreConsistency;');

  check('fix5: scoreRangeChart does not throw when one dimension throws',
    !threw && r !== null, threw ? 'scoreRangeChart threw' : '');
  if (r) {
    const sf = issuesOf(r, 'quality.scoring_failed');
    // Hand-computed with consistency forced to 0.0:
    //   completeness 1.0, accuracy 0.5 (1/2), consistency 0.0, structure
    //   0.5 (extras passes; biozones + other_fossils absent -> 2 nulls ->
    //   nothing passes) -> .3 + .2 + 0 + .05 = 0.55 -> D.
    check('fix5: failed dimension scores 0 and emits quality.scoring_failed',
      sf.length === 1 && sf[0].severity === 'warning'
        && sf[0].params.dimension === 'consistency'
        && sf[0].params.error_type === 'TypeError',
      JSON.stringify(sf));
    check('fix5: remaining dimensions still grade (0.55 / D)',
      r.score === 0.55 && r.grade === 'D',
      JSON.stringify({ score: r.score, grade: r.grade }));
    check('fix5: other dimensions\' issues are still reported',
      issuesOf(r, 'quality.range_top_lt_base').length === 1,
      JSON.stringify(r.issues));
  }

  // A second dimension can fail independently; each emits its own message.
  run(`
    globalThis.__realScoreConsistency2 = scoreConsistency;
    globalThis.__realScoreStructure2 = scoreStructure;
    scoreConsistency = function () { throw new RangeError('k'); };
    scoreStructure = function () { throw new EvalError('s'); };
  `);
  let r2 = null;
  try {
    r2 = run('scoreRangeChart({ sections: [{ name: "S1" }], confidence: 0.9 })');
  } catch (e) { /* must not throw */ }
  run(`
    scoreConsistency = globalThis.__realScoreConsistency2;
    scoreStructure = globalThis.__realScoreStructure2;
  `);
  const sf2 = r2 ? issuesOf(r2, 'quality.scoring_failed') : [];
  check('fix5: two failing dimensions each emit their own scoring_failed',
    r2 !== null && sf2.length === 2
      && sf2.some((i) => i.params.dimension === 'consistency'
        && i.params.error_type === 'RangeError')
      && sf2.some((i) => i.params.dimension === 'structure'
        && i.params.error_type === 'EvalError'),
    JSON.stringify(sf2));
})();

// ---------------------------------------------------------------------------
// fix6 — _bedPosition pins the NUMBERS, not just the ordering
// ---------------------------------------------------------------------------
//
// The biozone-order cases above only prove 23a sorts below 23c. That leaves
// the formula itself unasserted: a mirror that returned bed_num + 0.001 for
// every letter, or that used 27 instead of 26, still orders correctly for
// small gaps and would pass. rca_core.bed_parser.bed_position is the
// authority (its docstring spells out 23a -> 23.019..., 23z -> 23.981...),
// and these are exactly the values it produces.
(() => {
  const pos = (label) => run('_bedPosition(' + JSON.stringify(label) + ')');
  const close = (a, b) => typeof a === 'number' && Math.abs(a - b) < 1e-12;

  check('fix6: bare bed is the integer itself', close(pos('23'), 23),
    String(pos('23')));
  check('fix6: 23a is 23 + 0.5/26', close(pos('23a'), 23 + 0.5 / 26),
    String(pos('23a')));
  check('fix6: 23c is 23 + 2.5/26', close(pos('23c'), 23 + 2.5 / 26),
    String(pos('23c')));
  check('fix6: 23z is 23 + 25.5/26', close(pos('23z'), 23 + 25.5 / 26),
    String(pos('23z')));
  // Strictly inside (n, n+1) for every letter the parser reads as a SUB-bed.
  // NOT all 26: the shared parser treats 'm' as the metre unit ("23 m"), so
  // 23m must come back null. Measured against rca_core.bed_parser, which
  // answers None for exactly the same inputs.
  check('fix6: every accepted sub-bed sits strictly between its bed and the next',
    (() => {
      let seen = 0;
      for (const bed of ['1', '9', '23', '99']) {
        const base = Number(bed);
        for (let i = 0; i < 26; i += 1) {
          const p = pos(bed + String.fromCharCode(97 + i));
          if (p === null) continue;
          if (!(p > base && p < base + 1)) return false;
          seen += 1;
        }
      }
      return seen === 100;   // 4 beds x 25 letters (a-z minus 'm')
    })());
  check("fix6: 'm' is the metre unit, not a sub-bed", pos('23m') === null,
    String(pos('23m')));
  // Prefix and case are the parser's business, not this function's; it must
  // still land on the same numbers when the shared parser accepts the label.
  check('fix6: prefixed and upper-case labels agree',
    close(pos('Bed 23c'), pos('23c')) && close(pos('23C'), pos('23c')),
    String(pos('Bed 23c')) + ' / ' + String(pos('23C')));
  check('fix6: unparseable label yields null', pos('not a bed') === null,
    String(pos('not a bed')));
})();

// ---------------------------------------------------------------------------

console.log('\n--- quality frontend mirror: ' + passed + ' passed, '
            + failed + ' failed ---');
process.exit(failed ? 1 : 0);
