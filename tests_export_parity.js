/**
 * tests_export_parity.js — minimal Node smoke for the js/export.js WPD mirrors.
 *
 * BORROW-2026-09-20 (export domain, JS side). Scope, honestly stated:
 *   * The differential parity fixture (tests/gen_frontend_parity_fixtures.py
 *     -> tests/fixtures/frontend_parity_2026_09_20.json ->
 *     tests_diff_frontend_parity.js) replays the EXTRACTION pipeline only
 *     (normalize_result family, to_newick, safe_json_loads, age_bound). It
 *     never loads
 *     js/export.js, so the new rca_core/exporter.py::to_wpd and the PBDB
 *     upload columns are OUTSIDE that mechanism.
 *   * index.html / js/app.js have no WPD or PBDB export entry point either —
 *     the frontend export buttons only use rcaToCsv/rcaToTsv/rcaDownload.
 *   * Therefore ONLY the pure scalar helpers of the exchange
 *     (_wpd_num / _wpd_num_text / _wpd_slug -> rcaWpdNum / rcaWpdNumText /
 *     rcaWpdSlug) are mirrored so far. The full rcaToWpd bundle builder is
 *     wired to no browser call site yet; when one lands, it joins the
 *     differential fixture as a new group, not as an ad-hoc assertion.
 *
 * The case table below is the contract: this file replays it through the JS
 * mirrors, and tests/test_export_js_mirror_smoke_2026_09_20.py recomputes the
 * SAME table through rca_core.exporter, so the two engines cannot drift
 * apart silently. Run:  node tests_export_parity.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;

//
// FIX-2026-09-22 (items 8 + 11): the "num" block grew the cases the two
// engines used to disagree on — misplaced PEP 515 underscores (Python's
// float() rejects them, the old JS class accepted them), non-ASCII digits and
// NBSP padding (Python's float() accepted them, the JS mirror rejected them —
// BOTH engines now refuse, see _wpd_num), negative zero, and the >= 1e21
// integer expansion. The "slug" block grew the non-Latin plate names the
// README promises stay readable in a file listing, plus the path-traversal
// spellings that must NOT survive.
//
// Every row is replayed by BOTH engines: this file runs the JS mirrors, and
// tests/test_export_js_mirror_smoke_2026_09_20.py recomputes the same table
// against rca_core.exporter. Add to one, add to both engines' expectations.
// Write non-ASCII characters LITERAL, never as a \\uXXXX escape: a JS template
// literal unescapes before JSON.parse sees it, while the Python reader
// unescapes only the JSON escape — the two engines would test different strings.
//
// __RCA_WPD_CASES_BEGIN__
const CASES_SOURCE = `
{
  "num": [
    {"in": 2, "num": 2, "text": "2"},
    {"in": "2.0", "num": 2, "text": "2"},
    {"in": "9", "num": 9, "text": "9"},
    {"in": 23.03846153846154, "num": 23.038462, "text": "23.038462"},
    {"in": 2.5, "num": 2.5, "text": "2.5"},
    {"in": -1.25, "num": -1.25, "text": "-1.25"},
    {"in": 259.51, "num": 259.51, "text": "259.51"},
    {"in": 0, "num": 0, "text": "0"},
    {"in": 1e-7, "num": 0, "text": "0"},
    {"in": -1e-7, "num": 0, "text": "0"},
    {"in": -0, "num": 0, "text": "0"},
    {"in": 1e21, "num": 1e+21, "text": "1000000000000000000000"},
    {"in": 1e3, "num": 1000, "text": "1000"},
    {"in": "1e3", "num": 1000, "text": "1000"},
    {"in": "1_000", "num": 1000, "text": "1000"},
    {"in": "1_0", "num": 10, "text": "10"},
    {"in": "1.5_0", "num": 1.5, "text": "1.5"},
    {"in": "1e1_0", "num": 10000000000, "text": "10000000000"},
    {"in": "1_", "num": null, "text": ""},
    {"in": "1__0", "num": null, "text": ""},
    {"in": "1_.5", "num": null, "text": ""},
    {"in": "1._5", "num": null, "text": ""},
    {"in": "_1", "num": null, "text": ""},
    {"in": "1e_10", "num": null, "text": ""},
    {"in": "２３", "num": null, "text": ""},
    {"in": "١٢٣", "num": null, "text": ""},
    {"in": " 1", "num": null, "text": ""},
    {"in": " 2.5 ", "num": 2.5, "text": "2.5"},
    {"in": null, "num": null, "text": ""},
    {"in": true, "num": null, "text": ""},
    {"in": false, "num": null, "text": ""},
    {"in": "", "num": null, "text": ""},
    {"in": "common", "num": null, "text": ""},
    {"in": "251.902 Ma", "num": null, "text": ""},
    {"in": "NaN", "num": null, "text": ""},
    {"in": "Infinity", "num": null, "text": ""},
    {"in": "-inf", "num": null, "text": ""},
    {"in": "0x10", "num": null, "text": ""}
  ],
  "slug": [
    {"in": "Fig. 3b (a)", "slug": "Fig._3b_a"},
    {"in": "", "slug": "dataset"},
    {"in": "  ", "fallback": "plate", "slug": "plate"},
    {"in": "---", "slug": "dataset"},
    {"in": "P. asiaticus (Zheng, 1979)", "slug": "P._asiaticus_Zheng_1979"},
    {"in": "Fig3_plate", "slug": "Fig3_plate"},
    {"in": "Bed 23a", "fallback": "nopanel", "slug": "Bed_23a"},
    {"in": "图版3", "slug": "图版3"},
    {"in": "剖面 A-1", "slug": "剖面_A-1"},
    {"in": "Разрез 1", "slug": "Разрез_1"},
    {"in": "../etc/passwd", "slug": "etc_passwd"},
    {"in": "a<b/c:d*e", "slug": "a_b_c_d_e"},
    {"in": "Fig3_plate", "slug": "Fig3_plate"},
    {"in": "_Weird...name_", "slug": "Weird...name"},
    {"in": 23.038462, "fallback": "taxon", "slug": "23.038462"}
  ]
}
`;
// __RCA_WPD_CASES_END__

// __RCA_WPD_CASES_BEGIN__ (the pytest drift guard extracts the table the
// same way — keep both markers around the literal above.)
const CASES = JSON.parse(CASES_SOURCE);

function loadExport() {
  const src = fs.readFileSync(path.join(ROOT, 'js', 'export.js'), 'utf8');
  const ctx = {};
  vm.createContext(ctx);
  vm.runInContext(src, ctx, { filename: 'js/export.js' });
  // Classic-script function declarations land on the context's global object.
  return ctx;
}

let pass = 0;
const failures = [];

function check(label, got, want) {
  const ok = got === want;
  if (ok) pass += 1;
  else failures.push(label + ': got ' + JSON.stringify(got) + ', want ' + JSON.stringify(want));
}

function main() {
  const ctx = loadExport();
  for (const name of ['rcaWpdNum', 'rcaWpdNumText', 'rcaWpdSlug']) {
    if (typeof ctx[name] !== 'function') {
      console.log('FAIL js/export.js does not expose ' + name);
      process.exit(1);
    }
  }

  CASES.num.forEach((c, i) => {
    check('num[' + i + '] in=' + JSON.stringify(c.in), ctx.rcaWpdNum(c.in), c.num);
    check('numText[' + i + '] in=' + JSON.stringify(c.in), ctx.rcaWpdNumText(c.in), c.text);
  });

  CASES.slug.forEach((c, i) => {
    const got = ('fallback' in c && c.fallback !== null && c.fallback !== undefined)
      ? ctx.rcaWpdSlug(c.in, c.fallback)
      : ctx.rcaWpdSlug(c.in);
    check('slug[' + i + '] in=' + JSON.stringify(c.in), got, c.slug);
  });

  const total = CASES.num.length * 2 + CASES.slug.length;
  if (failures.length) {
    for (const f of failures) console.log('FAIL', f);
    console.log('tests_export_parity: ' + pass + '/' + total + ' passed, '
      + failures.length + ' FAILED');
    process.exit(1);
  }
  console.log('tests_export_parity: ' + pass + '/' + total + ' checks passed '
    + '(WPD scalar mirrors vs the Python case table)');
}

main();
