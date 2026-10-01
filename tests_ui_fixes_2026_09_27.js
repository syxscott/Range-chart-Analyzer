/**
 * Regression tests for the AUDIT-2026-09-27 frontend UI wave (F-01 .. F-07).
 *
 * Run:  node tests_ui_fixes_2026_09_27.js
 *
 * Hand-rolled suite like every other root Node suite in this repo: no
 * package.json, no framework, no dependency. `check(name, ok, detail)`.
 *
 * The wave's theme: several defects were invisible because a CONTROL existed
 * but rendered the wrong thing. These assertions read the shipped source
 * rather than a DOM, because the defects are in CSS rules, an icon glyph, a
 * string catalogue and a load-order list — all of which are exactly the
 * things a visual smoke test waves through.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = __dirname;
const read = (rel) => fs.readFileSync(path.join(REPO, rel), 'utf8');

let pass = 0;
let fail = 0;
const failed = [];
function check(name, ok, detail) {
  if (ok) {
    pass += 1;
    console.log('PASS', name);
  } else {
    fail += 1;
    failed.push(name);
    console.log('FAIL', name, detail === undefined ? '' : '— ' + detail);
  }
}

// ---------------------------------------------------------------------------
// F-01 — the "-" placeholder must not render while the cell has focus
// ---------------------------------------------------------------------------
// Symptom: focusing an empty editable cell and typing showed "-Brachiopoda",
// because the dash is a `::before` on `.cell-empty` and the class is only
// re-stripped on the next repaint. The placeholder must stay a `::before`
// (never DOM text, so it can never be selected or committed) AND be
// suppressed while focused.
(() => {
  const css = read('css/table-edit.css');
  const rule = css.match(
    /\[data-editable="1"\][^{]*\.cell-empty[^{]*\{[^}]*\}/);
  check('f01-empty-placeholder-rule-exists', !!rule, rule && rule[0].slice(0, 60));
  check('f01-placeholder-guards-focus',
    !!rule && /:not\(:focus\)/.test(rule[0]),
    rule && rule[0].replace(/\s+/g, ' ').slice(0, 90));
  check('f01-placeholder-is-still-a-pseudo-element',
    !!rule && /::before\s*\{/.test(rule[0]));
  check('f01-placeholder-not-selectable',
    !!rule && /pointer-events:\s*none/.test(rule[0]));
  // The invalid-state marker is a different glyph and must keep its own rule.
  // NB the content is "? " WITH a trailing space — that is what ships.
  check('f01-invalid-marker-preserved',
    /rca-cell-invalid::before\s*\{\s*content:\s*"\u26a0\s*"/.test(css));
})();

// ---------------------------------------------------------------------------
// F-02 — the accessible-summary catalogue must cover every locale
// ---------------------------------------------------------------------------
// `rcaVizT` falls back to `en`, so a missing `ja` block means a Japanese user
// hears English inside a role="status" live region.
(() => {
  const viz = read('js/viz.js');
  const seg = viz.slice(viz.indexOf('var RCA_VIZ_STRINGS = {'));
  const body = seg.slice(0, seg.indexOf('\n};'));
  const blocks = {};
  const re = /\n  (\w+): \{(.*?)\n  \},/gs;
  let m;
  while ((m = re.exec(body)) !== null) { blocks[m[1]] = m[2]; }
  const keys = (b) => (b.match(/^\s+(\w+):/gm) || [])
    .map((s) => s.trim().replace(':', ''));
  const en = keys(blocks.en || '');
  check('f02-has-en', en.length > 0, en.length);
  ['zh', 'ja'].forEach((loc) => {
    check('f02-has-' + loc, !!blocks[loc], Object.keys(blocks).join(','));
  });
  ['zh', 'ja'].forEach((loc) => {
    const missing = en.filter((k) => keys(blocks[loc] || '').indexOf(k) === -1);
    check('f02-' + loc + '-key-parity', missing.length === 0, missing.join(','));
  });
  check('f02-no-mojibake',
    !/\ufffd/.test(blocks.ja || ''), 'replacement chars in the ja block');
  check('f02-ja-uses-placeholders',
    /\{placed\}/.test(blocks.ja || '') && /\{rows\}/.test(blocks.ja || ''));
})();

// ---------------------------------------------------------------------------
// F-03 — the settings grid must not reflow by viewport width
// ---------------------------------------------------------------------------
// `auto-fit minmax(220px,1fr)` made the column COUNT an emergent property of
// the viewport: measured live, 3 columns at 928px and 5 at 1280px, so the
// field grouping fragmented as the window widened.
(() => {
  const css = read('css/style.css');
  const grid = css.match(/\.form-grid\s*\{[^}]*\}/);
  check('f03-form-grid-rule-exists', !!grid, grid && grid[0].slice(0, 60));
  check('f03-no-auto-fit',
    !!grid && !/auto-fit/.test(grid[0]),
    grid && grid[0].replace(/\s+/g, ' ').slice(0, 80));
  // A single-column phone fallback must still exist.
  check('f03-keeps-narrow-fallback',
    /@media\s*\(max-width:\s*480px\)[\s\S]{0,200}?\.form-grid\s*\{[^}]*grid-template-columns:\s*1fr/.test(css));
})();

// ---------------------------------------------------------------------------
// F-04 — the slider readout must not read as the track's end
// ---------------------------------------------------------------------------
// `.range-row` was `1fr 64px` with the value in the trailing 64px column, so
// with the thumb near the left the number sat ~1200px away and looked like it
// belonged to the far end of the track.
(() => {
  const css = read('css/style.css');
  const row = css.match(/\.range-row\s*\{[^}]*\}/);
  check('f04-range-row-rule-exists', !!row, row && row[0].slice(0, 60));
  check('f04-no-trailing-64px-value-column',
    !!row && !/1fr\s+64px/.test(row[0]),
    row && row[0].replace(/\s+/g, ' ').slice(0, 80));
  check('f04-value-aligns-to-label-line',
    /range-value|slider-head|range-head/.test(css),
    'a header/label line class for value+label');
})();

// ---------------------------------------------------------------------------
// F-05 — the theme icons must be SVG, not ambiguous glyphs
// ---------------------------------------------------------------------------
// The bare characters U+2600 / U+263E render at ~12px such that the crescent
// is indistinguishable from the letter "C", and the two glyphs come from
// different symbol fonts so they do not match each other.
(() => {
  const html = read('index.html');
  ['light', 'dark'].forEach((choice) => {
    const btn = html.match(
      new RegExp('data-theme-choice="' + choice + '"[\\s\\S]{0,700}?</button>'));
    check('f05-' + choice + '-button-found', !!btn);
    check('f05-' + choice + '-uses-svg', !!btn && /<svg/.test(btn[0]));
    check('f05-' + choice + '-icon-is-hidden-from-at',
      !!btn && /aria-hidden="true"/.test(btn[0]),
      'the button title is the accessible name');
    check('f05-' + choice + '-no-bare-sky-glyph',
      !!btn && !/\u2600/.test(btn[0]));
    check('f05-' + choice + '-no-bare-moon-glyph',
      !!btn && !/\u263e/i.test(btn[0]));
    check('f05-' + choice + '-keeps-accessible-name',
      !!btn && /data-i18n-title=/.test(btn[0]) && /title=/.test(btn[0]));
    check('f05-' + choice + '-keeps-pressed-state',
      !!btn && /aria-pressed=/.test(btn[0]));
  });
  // Scan the BUTTONS only: the explanatory comment beside them quotes the old
  // glyphs on purpose, so a blind slice over the toggle block would flag its
  // own documentation.
  check('f05-no-moon-glyph-anywhere-in-toggle', (() => {
    const iconButtons = [...html.matchAll(
      /<button[^>]*data-theme-choice="(?:light|dark)"[\s\S]{0,700}?<\/button>/g)]
      .map((m) => m[0]);
    return iconButtons.length === 2
      && iconButtons.every((b) => !/[\u2600\u263e]/i.test(b));
  })());
})();

// ---------------------------------------------------------------------------
// F-06 — the results card must not reserve dead space before a result exists
// ---------------------------------------------------------------------------
(() => {
  const html = read('index.html');
  check('f06-empty-state-exists', /id="results-empty"/.test(html));
  check('f06-content-slot-exists', /id="results-content"/.test(html));
  // The old copy pointed the user UPWARD at the upload card while occupying
  // ~230px itself. The new copy must address the action, not the direction.
  const m = html.match(/id="results-empty"[\s\S]{0,700}?<\/div>/);
  const copy = m ? (m[0].match(/data-i18n="([^"]+)"/g) || []).join(' ') : '';
  check('f06-empty-state-is-i18n-keyed', /data-i18n=/.test(copy), copy);
  const app = read('js/app.js');
  check('f06-empty-card-hidden-until-result',
    /results-card/.test(app) && /classList\.(add|remove)\(['"]hidden['"]\)/.test(app),
    'app.js must toggle #results-card');
})();

// ---------------------------------------------------------------------------
// F-07 — the contrast suite must read every stylesheet in the cascade
// ---------------------------------------------------------------------------
// It concatenated only style.css + app-ux.css, so viz.css and table-edit.css
// had never been audited — and table-edit.css carried three hardcoded
// dark-theme tints that nothing checked.
(() => {
  const src = read('tests_contrast.js');
  ['css/style.css', 'css/app-ux.css', 'css/viz.css', 'css/table-edit.css']
    .forEach((f) => {
      check('f07-contrast-reads-' + path.basename(f),
        src.indexOf("readCss('" + f + "')") !== -1
        || src.indexOf('"' + f + '"') !== -1, f);
    });
  // Cascade order must be preserved, not alphabetical. Scan only the RULES
  // build: the filenames also appear in a comment ABOVE it, so a whole-file
  // indexOf would find the comment and report a bogus order.
  const rulesBlock = (() => {
    const i = src.indexOf('const RULES');
    return i === -1 ? '' : src.slice(i, src.indexOf(';', i) + 1);
  })();
  const order = ['css/style.css', 'css/viz.css', 'css/table-edit.css',
                 'css/app-ux.css'].map((f) => rulesBlock.indexOf(f))
    .filter((i) => i !== -1);
  check('f07-all-four-read-in-the-rules-block', order.length === 4, order.length);
  check('f07-cascade-order-still-ascending',
    order.length === 4 && order.every((v, i) => i === 0 || v > order[i - 1]),
    order.join(','));
  // The three hardcoded tints became real tokens: a soft fill per hue plus
  // an overlay set for the dark blocks.
  ['--primary-soft', '--warning-soft', '--info-soft',
   '--overlay-primary', '--overlay-warning', '--overlay-info'].forEach((tok) => {
    check('f07-token-defined-' + tok,
      read('css/style.css').indexOf(tok + ':') !== -1, tok);
  });
})();

// ---------------------------------------------------------------------------
// House rules this wave must not have broken
// ---------------------------------------------------------------------------
(() => {
  const html = read('index.html');
  // C10: every referenced STYLESHEET stays linked, and the cascade order is
  // pinned by tests/test_server_static_perf_2026_09_20.py. The server
  // allowlist check for <script> tags lives in that Python test — the
  // allowlist is a data structure there, not literal paths, so asserting
  // `server.py.indexOf(src)` here would be a false signal.
  const hrefs = [...html.matchAll(/<link[^>]*href="([^"]+)"/g)].map((m) => m[1]);
  ['css/style.css', 'css/viz.css', 'css/table-edit.css', 'css/app-ux.css']
    .forEach((f) => {
      check('f07-asset-linked-' + path.basename(f), hrefs.indexOf(f) !== -1);
    });
  const idx = (f) => hrefs.indexOf(f);
  check('f07-href-cascade-intact',
    idx('css/style.css') < idx('css/viz.css')
    && idx('css/viz.css') < idx('css/table-edit.css')
    && idx('css/table-edit.css') < idx('css/app-ux.css'),
    hrefs.join(' '));
  // No new <script> may appear: this wave is CSS/markup/catalogue only.
  const srcs = [...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map((m) => m[1]);
  // 16 is the shipped count; this wave is CSS / markup / catalogue only.
  check('f07-script-count-unchanged', srcs.length === 16, srcs.length);
  check('f07-no-runtime-worker-script-tag',
    srcs.indexOf('js/sharpen_worker.js') === -1);
})();

// ---------------------------------------------------------------------------
// AUDIT-2026-09-27 [item 10.1] — table-config contract for the GENERIC
// fallback, on the editing surface.
//
// tests_diff_frontend_parity.js compares NORMALISER output, not table-config
// output, so nothing there covers rcaTableConfigs itself. These pin the two
// behaviours the fallback must not break.
//
// It does NOT pin how many range-chart shells come back: js/table.js keeps
// all four unconditionally (pinned twice in tests_frontend.js, and required
// by tests_edit_history.js::empty-table-keeps-addrow, since the shells ARE
// the "Add row" affordance), while exporter.py drops unclaimed ones and shows
// its own empty-state page. That divergence is a product decision, not a bug,
// and is documented at the branch in js/table.js.
// ---------------------------------------------------------------------------
(() => {
  const src = read('js/table.js');
  const sb = { console, Math, JSON, Object, Array, String, Number, Boolean,
    Date, RegExp, Error, isNaN, parseInt, parseFloat, Intl, Map, Set,
    Promise };
  sb.window = sb; sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(src, sb, { filename: 'table.js' });
  const configs = sb.rcaTableConfigs;

  // A key no mode-specific branch owns: the case the fallback exists for.
  const novel = configs({
    confidence: 0.7,
    unnamed_future_key: [
      { label: 'A', value: '1.5', unit: 'm' },
      { label: 'B', value: '2.5', unit: 'm' },
    ],
  });
  const novelCfg = novel.filter((c) => c.id === 'unnamed_future_key');
  check('f08-generic-fallback-renders-unknown-key',
    novelCfg.length === 1 && novelCfg[0].generic === true,
    JSON.stringify(novel.map((c) => c.id)));
  check('f08-generic-row-reads',
    novelCfg.length === 1 && Array.isArray(novelCfg[0].row(
      { label: 'A', value: '1.5', unit: 'm' })),
    novelCfg.length === 1 ? JSON.stringify(novelCfg[0].row(
      { label: 'A', value: '1.5', unit: 'm' })) : 'n/a');
  // The generic table must not be duplicated by the range-chart branch.
  check('f08-no-duplicate-generic-table',
    novel.filter((c) => c.id === 'unnamed_future_key').length === 1,
    String(novel.length));

  // other_fossils is a deliberate STRING list (a caption), and it must keep
  // rendering — see tests/test_exporter_xlsx_fix.py::TestToXlsxStringOtherFossils.
  const stringList = configs({
    other_fossils: ['a caption line'], confidence: 0.5,
  });
  check('f08-string-list-table-still-renders',
    stringList.some((c) => c.id === 'other_fossils'),
    JSON.stringify(stringList.map((c) => c.id)));

  // An empty-but-present range chart keeps its shells so "Add row" works.
  const empty = configs({
    sections: [], species_ranges: [], biozones: [], other_fossils: [],
  });
  check('f08-empty-present-keys-keep-shells',
    ['sections', 'species_ranges', 'biozones', 'other_fossils']
      .every((k) => empty.some((c) => c.id === k)),
    JSON.stringify(empty.map((c) => c.id)));
})();

// ---------------------------------------------------------------------------
// f09 - recombination ballots must be ORDERED the way Python orders them
// ---------------------------------------------------------------------------
// rcaRecombinationBallots tie-broke on JSON.stringify(tup) rather than on the
// tuple. The separator ',' (0x2C) sorts after the space (0x20) inside a
// value, so any value that is a PREFIX of another landed in the opposite
// order from rca_core/aggregate.py's `sorted(..., key=t)`. The Python and
// browser paths then presented the same run disagreement in opposite order.
//
// The suite's earlier ballot checks (Python tests/test_audit_2026_09_27.py) only
// asserted len() and the set of vote counts, so nothing caught it. This
// asserts the ORDER, and the Python twin asserts the same literal.
(() => {
  const src = read('js/aggregate.js');
  const sb = { console, Math, JSON, Object, Array, String, Number, Boolean,
    Date, RegExp, Error, isNaN, parseInt, parseFloat, Intl, Map, Set,
    Promise, Symbol };
  sb.window = sb; sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(src, sb, { filename: 'aggregate.js' });

  const S = 'Test species';
  const mk = (base, zone, top) => ({
    sections: ['s1'],
    species_ranges: [{ species: S, section: 's1', range_base: base,
      range_top: top || 'bed 9', biozone: zone }],
  });
  // 1-1 tie on every field, so the per-field winners come from different
  // runs and the merged tuple is observed by neither.
  const merged = sb.rcaMergeResults([
    mk('bed 1', 'zone c'),
    mk('bed 1 (rp13)', 'zone b'),
  ]);
  const row = merged.species_ranges[0];

  check('f09-prefix-ballots-sort-lexicographically',
    row && row._warning === 'recombined_consensus'
    && (row._recombination_ballots || []).map((b) => b.range_base).join('|')
      === 'bed 1|bed 1 (rp13)',
    JSON.stringify(row && row._recombination_ballots));

  check('f09-warning-ballots-keep-the-same-order',
    (merged.chimera_warnings || []).length === 1
    && (merged.chimera_warnings[0].ballots || [])
      .map((b) => b.range_base).join('|') === 'bed 1|bed 1 (rp13)',
    JSON.stringify(merged.chimera_warnings));

  // A majority reading must outrank a minority one, whatever the strings.
  // base: bed 9 x2, bed 3 x1, bed 4 x1 -> "bed 9" wins outright.
  // top / biozone: 2-2 ties -> alphabetical, both landing on the run that
  // read "bed 8 / zone a", so the merged tuple is observed by NO run.
  const maj = sb.rcaMergeResults([
    mk('bed 9', 'bed 9', 'zone b'),
    mk('bed 9', 'bed 9', 'zone b'),
    mk('bed 3', 'bed 8', 'zone a'),
    mk('bed 4', 'bed 8', 'zone a'),
  ]);
  const mrow = maj.species_ranges.find((r) => r._recombination_ballots);
  check('f09-majority-ballot-comes-first',
    mrow && mrow._recombination_ballots.length === 3
    && mrow._recombination_ballots[0].votes === 2
    && mrow._recombination_ballots[0].range_base === 'bed 9',
    JSON.stringify(mrow && mrow._recombination_ballots));
})();

// ---------------------------------------------------------------------------
// f10 - a bare-string row must survive normalisation, in EVERY engine
// ---------------------------------------------------------------------------
// rcaDictRows in js/minimax.js took only `raw`. Python's _dict_rows also takes
// `kind` and `warnings`, and coerces a bare-string list entry into a row and
// flags `string_row_coerced`. Without those two parameters the browser dropped
// the row outright: {"sections": ["Section 1"]} — a completely natural thing
// for a model to emit — produced a populated table in the desktop app and an
// EMPTY table in the browser, with no warning on either side. Found by
// difffuzz_normalize.py; see the AUDIT-2026-09-27 P1 comment on rcaDictRows.
(() => {
  const sb = { console, Math, JSON, Object, Array, String, Number, Boolean,
    Date, RegExp, Error, isNaN, parseInt, parseFloat, Intl, Map, Set,
    Promise, Symbol };
  sb.window = sb; sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(read('js/minimax.js'), sb, { filename: 'minimax.js' });
  const normCol = sb.rcaNormalizeColumnarResult;

  const one = normCol({ sections: ['a'], confidence: 1 });
  check('f10-string-section-row-is-kept',
    Array.isArray(one.sections) && one.sections.length === 1,
    JSON.stringify(one && one.sections));
  check('f10-string-section-row-keeps-its-text',
    one.sections[0] && one.sections[0]._extras
    && String(one.sections[0]._extras.name) === 'a',
    JSON.stringify(one.sections[0] && one.sections[0]._extras));
  check('f10-string-section-row-is-flagged',
    Array.isArray(one._warnings)
    && one._warnings.indexOf('string_row_coerced') !== -1,
    JSON.stringify(one && one._warnings));

  // Non-string junk in the same position is still dropped, and a blank string
  // is NOT a row -- coercing those would invent records.
  const mixed = normCol({ sections: ['a', null, 'b', '', 7, []], confidence: 1 });
  check('f10-only-blank-safety',
    mixed.sections.length === 2
    && String(mixed.sections[0]._extras.name) === 'a'
    && String(mixed.sections[1]._extras.name) === 'b',
    JSON.stringify(mixed.sections.map((s) => s && s._extras)));

  // A dict-shaped array is a different repair and must stay different: the
  // wrapper key fills the missing id, and no string flag is raised.
  const dictShaped = normCol({ sections: { 'Ki-1': { id: 'Ki-1' } }, confidence: 1 });
  check('f10-dict-shaped-array-not-mistaken-for-string-coercion',
    dictShaped.sections.length === 1
    && !(Array.isArray(dictShaped._warnings)
         && dictShaped._warnings.indexOf('string_row_coerced') !== -1),
    JSON.stringify({ sections: dictShaped.sections, warnings: dictShaped._warnings }));
})();

// ---------------------------------------------------------------------------
// f11 - a scalar row inserted into a string list must survive capture/apply
// ---------------------------------------------------------------------------
// `other_fossils` holds plain strings. The edits payload encodes an inserted
// row as `new_<i>`, and that payload is a DICT -- rcaApplyEdits drops a
// non-dict insertion -- so a scalar insertion set neither `new_<i>` nor the
// list-level `_replaced`. The payload came out empty, isDirty() said "nothing
// changed", and the row was dropped on Save. Identical bug in
// rca_core/editable.py; both sides now route the case to the replacement,
// which is the only representation a scalar row has.
(() => {
  const src = read('js/table.js');
  const sb = { console, Math, JSON, Object, Array, String, Number, Boolean,
    Date, RegExp, Error, isNaN, parseInt, parseFloat, Intl, Map, Set,
    Promise, Symbol };
  sb.window = sb; sb.globalThis = sb; sb.self = sb;
  vm.createContext(sb);
  vm.runInContext(src, sb, { filename: 'table.js' });
  vm.runInContext(';globalThis.__cap = rcaCaptureEdits;'
                + 'globalThis.__app = rcaApplyEdits;'
                + 'globalThis.__dirty = rcaIsDirtyEdits;', sb);
  const cap = sb.__cap, app = sb.__app, dirty = sb.__dirty;
  check('f11-editors-are-present',
    typeof cap === 'function' && typeof app === 'function'
    && typeof dirty === 'function', 'rcaCaptureEdits/rcaApplyEdits missing');

  const roundTrip = (key, b, a) => {
    const before = JSON.parse(JSON.stringify({ [key]: b }));
    const after = JSON.parse(JSON.stringify({ [key]: a }));
    const edits = cap(JSON.parse(JSON.stringify(before)),
                      JSON.parse(JSON.stringify(after)));
    const replayed = app(JSON.parse(JSON.stringify(before)),
                         JSON.parse(JSON.stringify(edits)));
    return { got: replayed[key], edits, isDirty: !!dirty(edits) };
  };

  const mid = roundTrip('other_fossils', ['a', 'b'], ['a', 'NEW', 'b']);
  check('f11-mid-list-scalar-insert-survives',
    JSON.stringify(mid.got) === JSON.stringify(['a', 'NEW', 'b']),
    JSON.stringify(mid.got));
  check('f11-scalar-insert-marks-dirty', mid.isDirty === true,
    JSON.stringify(mid.edits));

  const tail = roundTrip('other_fossils', ['a', 'b'], ['a', 'b', 'c']);
  check('f11-trailing-scalar-append-survives',
    JSON.stringify(tail.got) === JSON.stringify(['a', 'b', 'c']),
    JSON.stringify(tail.got));

  const head = roundTrip('other_fossils', ['a', 'b'], ['NEW', 'a', 'b']);
  check('f11-leading-scalar-insert-survives',
    JSON.stringify(head.got) === JSON.stringify(['NEW', 'a', 'b']),
    JSON.stringify(head.got));

  // A DICT row must keep the precise new_<i> encoding rather than collapsing
  // the whole list to a replacement -- the two repair paths stay distinct.
  const dict = roundTrip('species_ranges',
    [{ species: 'A', section: 'S' }, { species: 'B', section: 'S' }],
    [{ species: 'A', section: 'S' }, { species: 'NEW', section: 'S' },
     { species: 'B', section: 'S' }]);
  check('f11-dict-insert-keeps-new-index-encoding',
    dict.got[1] && dict.got[1].species === 'NEW'
    && Object.keys(dict.edits.species_ranges || {})
      .some((k) => k.indexOf('new_') === 0)
    && !('_replaced' in (dict.edits.species_ranges || {})),
    JSON.stringify(dict.edits));
})();

// ---------------------------------------------------------------------------
console.log('');
if (fail) { console.log('failed:', failed.join(', ')); }
console.log(`--- ${pass} passed, ${fail} failed ---`);
process.exit(fail ? 1 : 0);
