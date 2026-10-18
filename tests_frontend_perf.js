// tests_frontend_perf.js — WEB-04 (UX-2026-10-18) browser-side performance
// and containment regression suite.
//
// WHY THIS FILE EXISTS
// --------------------
// Every other Node suite in this repo (tests_frontend 846, tests_edit_history
// 335, tests_contrast 329, tests_viz 161, tests_diff_frontend_parity 1360, ...)
// runs against a HAND-WRITTEN DOM stub — tests_frontend.js:66 buildContext(),
// tests_edit_history.js:1042 fillDom(). A stub has no CSS engine, no layout
// engine and no box model. That is the right tool for pinning behaviour, and it
// is exactly why 3654 assertions could pass while the 5000-row result panel
// froze the main thread for 1.07 s (measured: a single 1071 ms longtask) and
// while a containment change could clip every wrapped cell in the panel without
// a single test turning red.
//
// The assertions below therefore all need a REAL browser. They are the ones
// that cannot be written against a stub.
//
//   node tests_frontend_perf.js            full run
//   node tests_frontend_perf.js --quick    skip the heavy 5000-row cases,
//                                          keep every structural assertion
//
// Exit code is 0 only when every executed assertion passed.
//
// `--quick` NEVER skips silently: the skip banner is printed, and a skipped
// assertion is reported as SKIPPED in the summary. A test that quietly stops
// testing is worse than no test, because it still shows up green in CI.

// ---- locate playwright + a browser ----------------------------------------
const path = require('path');
const fs = require('fs');

function resolvePlaywright() {
  // NODE_PATH is honoured first (that is how the repo's own node_modules is
  // reached on a machine where the cwd is not the repo root), then the two
  // paths a checkout can plausibly live at.
  const candidates = [];
  if (process.env.NODE_PATH) {
    for (const p of process.env.NODE_PATH.split(path.delimiter)) {
      if (p) candidates.push(p);
    }
  }
  candidates.push(path.join(__dirname, 'node_modules'));
  candidates.push(path.join(process.cwd(), 'node_modules'));
  for (const dir of candidates) {
    try {
      return require(path.join(dir, 'playwright'));
    } catch (_e) { /* try the next one */ }
  }
  try { return require('playwright'); } catch (_e) { /* fall through */ }
  return null;
}

// A browser that is actually installed. The bundled chromium is preferred
// (it is what CI installs via `npx playwright install --with-deps chromium`);
// the well-known system Chrome paths are the fallback for a dev machine where
// `node_modules` exists but the download was never run.
function launchCandidates() {
  const list = [undefined];   // let playwright pick its own build
  if (process.env.CHROME_PATH) list.push(process.env.CHROME_PATH);
  if (process.platform === 'win32') {
    list.push('C:/Program Files/Google/Chrome/Application/chrome.exe');
    list.push('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe');
    list.push('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe');
  } else if (process.platform === 'darwin') {
    list.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
  } else {
    list.push('/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser');
  }
  return list;
}

async function launchBrowser(chromium) {
  const tried = [];
  for (const executablePath of launchCandidates()) {
    try {
      return { browser: await chromium.launch(executablePath ? { executablePath } : {}),
               used: executablePath || '(playwright bundled chromium)' };
    } catch (err) {
      tried.push((executablePath || '(bundled)') + ': ' + String(err.message).split('\n')[0]);
    }
  }
  const e = new Error('no usable browser found.\n  tried:\n    ' + tried.join('\n    ')
    + '\n  fix: npx playwright install chromium   (or set CHROME_PATH)');
  e.noBrowser = true;
  throw e;
}

// ---- tiny assertion harness ----------------------------------------------
const QUICK = process.argv.includes('--quick');

let passed = 0;
const failures = [];
const skipped = [];

function check(name, cond, detail) {
  if (cond) {
    passed += 1;
    console.log('PASS ' + name + (detail ? '  — ' + detail : ''));
  } else {
    failures.push(name + (detail ? '  — ' + detail : ''));
    console.log('FAIL ' + name + (detail ? '  — ' + detail : ''));
  }
}

function skip(name, why) {
  skipped.push(name + '  — ' + why);
  console.log('SKIP ' + name + '  — ' + why);
}

function eq(name, actual, expected, detail) {
  check(name, actual === expected,
    (detail ? detail + '; ' : '') + 'expected ' + JSON.stringify(expected)
    + ', got ' + JSON.stringify(actual));
}

// ---- fixtures -------------------------------------------------------------
//
// The row fixtures are DELIBERATELY high-variance. A table of same-length
// strings has a stable max-content width no matter what the CSS does, so it
// would report 0 px of column drift for the WRONG reason and the central
// assertion below would be decorative. `other_fossils` carries long free text
// because that is the one table whose rows legitimately wrap (js/table.js:1506
// adds `cell-wrapping`), which is what a fixed row height would destroy.
function mkResult(nRows, opts) {
  opts = opts || {};
  const species_ranges = [];
  for (let i = 0; i < nRows; i++) {
    species_ranges.push({
      species: opts.longSpecies && i % 3 === 1
        ? 'Radiolarius (' + i + ') longissimus species name variant'
        : 'R. sp. ' + i,
      section: opts.longSection && i % 7 === 0
        ? 'S' + (i % 5) : 'Section ' + i,
      range_base: 1000 + i * 7,
      range_top: 2000 + i * 11,
      zone: i % 11 === 0 ? 'Z' + i : 'Zone-' + i,
      author_year: i % 13 === 0 ? 'A' : 'Jones 19' + (50 + i % 40),
      agreement: (i % 3) + '/3',
      agreement_count: i % 3,
      note: opts.longNoteEvery && i % opts.longNoteEvery === 0 ? 'n'.repeat(120) : '',
    });
  }
  const sections = [];
  for (let s = 0; s < 8; s++) {
    sections.push({ name: 'S' + s, age: 200 + s, stage: 'St', lithology: 'sh', notes: '' });
  }
  const other_fossils = [];
  for (let f = 0; f < (opts.otherRows || 0); f++) {
    other_fossils.push('Fossilium longum taxon ' + f + ' '
      + 'with a great deal of descriptive text '.repeat(12));
  }
  return {
    runs: 3,
    confidence: 0.8,
    overall_confidence: 0.8,
    quality: { score: 0.7, grade: 'B', issues: [] },
    species_ranges: species_ranges,
    sections: sections,
    other_fossils: other_fossils,
  };
}

const INDEX_URL = 'file://' + path.join(__dirname, 'index.html').replace(/\\/g, '/');

// ---- the suite ------------------------------------------------------------
(async function main() {
  const started = Date.now();
  console.log('tests_frontend_perf — WEB-04 containment / large-table regressions'
    + (QUICK ? '  [QUICK MODE]' : ''));
  console.log('');

  const pw = resolvePlaywright();
  if (!pw) {
    console.error('FATAL: playwright not resolvable. Tried NODE_PATH, ./node_modules, cwd/node_modules.');
    console.error('       Run with:  NODE_PATH="' + __dirname + '/node_modules" node tests_frontend_perf.js');
    process.exit(1);
  }

  let browser, used;
  try {
    const l = await launchBrowser(pw.chromium);
    browser = l.browser; used = l.used;
  } catch (err) {
    if (err.noBrowser) {
      // A missing browser is an environment problem, not a green run. Exiting 0
      // here is precisely the "silently skipped == test does not exist" trap.
      console.error('FATAL: ' + err.message);
      process.exit(1);
    }
    throw err;
  }

  console.log('browser: ' + used);
  console.log('');

  const page = await browser.newPage();
  const pageErrors = [];
  page.on('pageerror', (e) => pageErrors.push(String(e && e.message || e)));

  // A CDP session, for the accessibility tree. See section 7: the AX tree is
  // the only signal that can tell "this subtree was skipped by containment"
  // from "this subtree is merely off-screen".
  let cdp = null;
  try {
    cdp = await page.context().newCDPSession(page);
    await cdp.send('Accessibility.enable');
  } catch (_e) {
    cdp = null;   // section 7 will report itself as unavailable, not skip silently
  }

  async function mount(data, editable) {
    await page.evaluate(([d, ed]) => {
      let host = document.getElementById('__perf_host');
      if (!host) {
        host = document.createElement('div');
        host.id = '__perf_host';
        document.body.appendChild(host);
      }
      host.innerHTML = window.rcaRenderResults(d, '', { editable: ed !== false });
    }, [data, editable !== false]);
  }

  async function speciesSection() {
    return page.evaluate(() => {
      const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
      if (!sec) return null;
      const wrap = sec.querySelector('.table-wrap');
      wrap.style.maxHeight = '420px';
      wrap.style.overflowY = 'auto';
      return { sec: sec, wrap: wrap };
    });
  }

  try {
    await page.goto(INDEX_URL);
    await page.waitForFunction(() => typeof window.rcaRenderResults === 'function', { timeout: 30000 });

    // =====================================================================
    // 1. THRESHOLD BOUNDARIES
    //    RCA_VIRTUAL_ROW_THRESHOLD = 600 (js/table.js:1029) gates the
    //    `data-rca-big` attribute; RCA_LONG_TABLE_HINT_ROWS = 1000
    //    (js/table.js:1039) gates the A4 notice. Two different constants on
    //    purpose, so both edges of both thresholds are pinned.
    // =====================================================================
    console.log('--- 1. threshold boundaries (RCA_VIRTUAL_ROW_THRESHOLD=600, RCA_LONG_TABLE_HINT_ROWS=1000) ---');
    for (const n of [1, 599, 600, 601, 999, 1000, 1001]) {
      await mount(mkResult(n));
      const r = await page.evaluate(() => {
        const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
        const tbl = sec && sec.querySelector('table.data-table');
        return {
          big: tbl ? tbl.getAttribute('data-rca-big') : null,
          hint: !!document.querySelector('#__perf_host .table-length-hint'),
        };
      });
      const expectBig = n >= 600;
      const expectHint = n >= 1000;
      eq('rows=' + n + ' data-rca-big', r.big, expectBig ? '1' : null,
        'threshold 600');
      eq('rows=' + n + ' long-table hint', r.hint, expectHint, 'threshold 1000');
    }

    // The containment must not be applied unconditionally: a small table has
    // to keep byte-identical markup, which is what keeps the other suites
    // untouched.
    await mount(mkResult(50));
    const smallCss = await page.evaluate(() => {
      const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
      const tr = sec.querySelector('tbody tr');
      return {
        big: sec.querySelector('table.data-table').getAttribute('data-rca-big'),
        contentVisibility: tr ? getComputedStyle(tr).contentVisibility : null,
      };
    });
    eq('small table carries no data-rca-big', smallCss.big, null);
    check('small table rows have NO containment',
      smallCss.contentVisibility !== 'auto' && smallCss.contentVisibility !== 'hidden',
      'computed content-visibility=' + smallCss.contentVisibility);

    // =====================================================================
    // 2. WRAPPED LONG TEXT IS NOT CLIPPED
    //    This is the regression guard for a real bug: the fixed row height
    //    (`--rca-row-h` + `height`) was first written against a bare
    //    `tbody tr` selector, which also matched `other_fossils` — whose cells
    //    deliberately carry `cell-wrapping` (js/table.js:1506) and are SUPPOSED
    //    to wrap. A fixed height there collapsed every long taxon name to one
    //    clipped line. It is now scoped to `[data-rca-big="1"]`, and both
    //    halves of that scoping are asserted.
    // =====================================================================
    console.log('');
    console.log('--- 2. wrapped long text is never clipped ---');
    for (const [label, n] of [['below threshold', 300], ['ABOVE threshold', 700]]) {
      await mount(mkResult(10, { otherRows: n }));
      const r = await page.evaluate(() => {
        const secs = Array.prototype.slice.call(
          document.querySelectorAll('#__perf_host .result-section'));
        const of = secs.filter((s) => s.getAttribute('data-table') === 'other_fossils')[0];
        if (!of) return null;
        const tbl = of.querySelector('table.data-table');
        const rows = Array.prototype.slice.call(of.querySelectorAll('tbody tr'));
        const cell = of.querySelector('tbody td.cell-wrapping');
        const rect = cell.getBoundingClientRect();
        return {
          big: tbl.getAttribute('data-rca-big'),
          rows: rows.length,
          cellH: Math.round(rect.height),
          cellScrollH: cell.scrollHeight,
          truncated: cell.scrollHeight > Math.ceil(rect.height) + 1,
        };
      });
      check('other_fossils ' + label + ': long text keeps its full box',
        r && !r.truncated,
        r ? ('cell height ' + r.cellH + 'px vs content ' + r.cellScrollH + 'px')
          : 'section not rendered');
      check('other_fossils ' + label + ': wraps to multiple lines',
        r && r.cellH >= 40,
        r ? ('wrapped row height ' + r.cellH + 'px (a clipped single line would be ~23px)') : 'n/a');
    }
    // And the mirror image: with single-line values every row of a big table
    // must come out IDENTICAL, which is the only reason a declared row height
    // is safe there.
    //
    // NOTE on the fixture: this uses SHORT values on purpose. An EDITABLE cell
    // is `white-space: pre-wrap` (css/table-edit.css), so a long value in an
    // editable column legitimately wraps and the row grows past the declared
    // height — measured 123px for a 120-char note. `height` on a table row is
    // a MINIMUM, not a clip, so that is correct behaviour and is asserted
    // separately below; asserting uniformity with long values would be
    // asserting something the design explicitly does not promise.
    await mount(mkResult(700));
    const uniform = await page.evaluate(() => {
      const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
      const rows = Array.prototype.slice.call(sec.querySelectorAll('tbody tr')).slice(0, 150);
      const raw = rows.map((t) => t.getBoundingClientRect().height);
      return {
        min: Math.min.apply(null, raw),
        max: Math.max.apply(null, raw),
        first: raw[0],
        rest: raw.slice(1),
      };
    });
    // Tolerance, not equality: with `border-collapse: collapse` the first
    // body row shares the header's border with the thead, so it measures
    // ~0.5px taller than the rest (measured 40.89 vs 40.39). That is border
    // arithmetic, not a layout bug — hence a 1px tolerance, with the spread
    // reported so a real regression is still visible.
    const spread = uniform.max - uniform.min;
    check('big single-line table: every row is the same height (1px tolerance)',
      spread <= 1,
      'spread ' + spread.toFixed(2) + 'px over ' + 150 + ' rows ('
      + uniform.min.toFixed(2) + '-' + uniform.max.toFixed(2) + 'px); '
      + 'the first row is ' + (uniform.first - Math.min.apply(null, uniform.rest)).toFixed(2)
      + 'px taller (collapsed border shared with thead)');

    // A tall EDITABLE row must still be taller than the declared minimum --
    // i.e. the fixed height is a floor and does not silently clip. This is the
    // species_ranges counterpart of the cell-wrapping check above: same rule,
    // different reason (pre-wrap rather than the .cell-wrapping class).
    await mount(mkResult(1200, { longNoteEvery: 6 }));
    const tall = await page.evaluate(() => {
      const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
      const rows = Array.prototype.slice.call(sec.querySelectorAll('tbody tr'));
      const hs = rows.map((t) => t.getBoundingClientRect().height);
      const tallRow = rows.filter((t) => t.getBoundingClientRect().height > 45)[0];
      if (!tallRow) return null;
      const cell = tallRow.querySelector('.rca-edit-cell');
      const rect = cell.getBoundingClientRect();
      return {
        min: Math.min.apply(null, hs), max: Math.max.apply(null, hs),
        declared: 36,
        clipped: cell.scrollHeight > Math.ceil(rect.height) + 1,
      };
    });
    check('a wrapped editable row grows past the declared minimum (no clipping)',
      tall && !tall.clipped && tall.max > tall.declared,
      tall ? ('rows ' + Math.round(tall.min) + '-' + Math.round(tall.max)
        + 'px vs declared 36px; tallest cell content fits its box: '
        + (tall.clipped ? 'NO' : 'yes')) : 'no tall row produced');

    // =====================================================================
    // 3. COLUMN WIDTH NEVER DRIFTS  (the assertion WEB-04 was parked on)
    // =====================================================================
    console.log('');
    if (QUICK) {
      skip('column width is stable across scroll (5000 rows)',
        '--quick: the 5000-row fixture is the slow part; run without --quick');
    } else {
      console.log('--- 3. column width does not drift while scrolling (5000 rows) ---');
      // Long, high-variance values in the editable columns: this is what makes
      // the assertion able to fail at all (see the negative control below).
      await mount(mkResult(5000, { longSpecies: true, longSection: true }));
      const drift = await page.evaluate(() => {
        const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
        const wrap = sec.querySelector('.table-wrap');
        wrap.style.maxHeight = '420px';
        wrap.style.overflowY = 'auto';
        const ths = Array.prototype.slice.call(sec.querySelectorAll('thead th'));
        const snap = () => ths.map((t) => +t.getBoundingClientRect().width.toFixed(2));
        const cellWidth = (rowIdx) => {
          const td = sec.querySelector('tr[data-row="' + rowIdx + '"] td:nth-child(3)');
          return td ? +td.getBoundingClientRect().width.toFixed(2) : null;
        };
        wrap.scrollTop = 0;
        void wrap.offsetHeight;
        const base = snap();
        const bodyTop = cellWidth(0);
        let max = 0;
        const perPos = [];
        for (const frac of [0.25, 0.5, 0.75, 1]) {
          wrap.scrollTop = Math.floor(wrap.scrollHeight * frac);
          void wrap.offsetHeight;
          const now = snap();
          const d = now.map((v, i) => +(v - base[i]).toFixed(2));
          perPos.push(d);
          for (const x of d) max = Math.max(max, Math.abs(x));
        }
        wrap.scrollTop = wrap.scrollHeight;
        void wrap.offsetHeight;
        const bodyBottom = cellWidth(4999);
        wrap.scrollTop = 0;
        void wrap.offsetHeight;
        return { base: base, perPos: perPos, max: max, bodyTop: bodyTop, bodyBottom: bodyBottom };
      });
      eq('header column drift across 4 scroll positions', drift.max, 0,
        'px; widths ' + drift.base.join(', '));
      check('body cell width is scroll-invariant too',
        drift.bodyTop !== null && drift.bodyBottom !== null
        && Math.abs(drift.bodyBottom - drift.bodyTop) < 0.5,
        'top row ' + drift.bodyTop + 'px vs bottom row ' + drift.bodyBottom + 'px');

      // ------------------------------------------------------------------
      // The containment must not CHANGE the layout either. This is the
      // assertion that actually pins the "on the <tr>, never on the <td>"
      // decision (js/table.js:1015-1017): a contained CELL stops
      // contributing to its column's preferred width, which redistributes the
      // whole table. Comparing against the SAME fixture with containment
      // switched off catches that.
      //
      // (An earlier form of this check took its baseline BEFORE injecting the
      // broken CSS, so it measured "the layout changed when a new rule
      // appeared" instead of "the layout changed". The base has to be taken
      // with the style already in effect.)
      // ------------------------------------------------------------------
      const vsBaseline = await page.evaluate(() => {
        const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
        const wrap = sec.querySelector('.table-wrap');
        const ths = Array.prototype.slice.call(sec.querySelectorAll('thead th'));
        const snap = () => ths.map((t) => +t.getBoundingClientRect().width.toFixed(2));
        const measure = () => {
          wrap.scrollTop = 0;
          void wrap.offsetHeight;
          return snap();
        };
        const shipped = measure();
        const st = document.createElement('style');
        st.textContent = 'table.data-table[data-rca-big="1"] tbody tr'
          + ' { content-visibility: visible !important; contain-intrinsic-size: none !important; }';
        document.head.appendChild(st);
        const without = measure();
        st.remove();
        const after = measure();
        let max = 0;
        shipped.forEach((v, i) => { max = Math.max(max, Math.abs(without[i] - v)); });
        let restored = 0;
        shipped.forEach((v, i) => { restored = Math.max(restored, Math.abs(after[i] - v)); });
        return {
          shipped: shipped, without: without,
          max: +max.toFixed(2), restored: +restored.toFixed(2),
        };
      });
      check('containment does not change the column layout at all',
        vsBaseline.max <= 0.5,
        'max difference vs the same table with containment off: ' + vsBaseline.max + 'px'
        + ' (shipped ' + vsBaseline.shipped.join(', ') + ')');
      eq('removing the containment restores the identical layout', vsBaseline.restored, 0,
        'px');

      // NEGATIVE CONTROL. Without this, a 0 px result is indistinguishable
      // from a fixture too uniform to ever react. Applying containment to the
      // <td>s (the variant js/table.js:1015-1017 warns against) must make
      // this same fixture change its column layout. If this control ever
      // reports ~0 px, the fixture has gone weak and the assertions above
      // are decorative.
      const neg = await page.evaluate(() => {
        const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
        const wrap = sec.querySelector('.table-wrap');
        const ths = Array.prototype.slice.call(sec.querySelectorAll('thead th'));
        const snap = () => ths.map((t) => +t.getBoundingClientRect().width.toFixed(2));
        wrap.scrollTop = 0;
        void wrap.offsetHeight;
        const base = snap();
        const st = document.createElement('style');
        // deliberately WRONG: containment on the cells
        st.textContent = 'table.data-table[data-rca-big="1"] tbody td'
          + ' { content-visibility: auto; contain-intrinsic-size: auto 36px; }';
        document.head.appendChild(st);
        void wrap.offsetHeight;
        const now = snap();
        st.remove();
        void wrap.offsetHeight;
        let max = 0;
        base.forEach((v, i) => { max = Math.max(max, Math.abs(now[i] - v)); });
        return max;
      });
      check('NEGATIVE CONTROL: containment on <td> DOES change the layout (fixture is strong enough)',
        neg > 20,
        'measured ' + neg.toFixed(1) + 'px of difference; if this is ~0 the fixture'
        + ' cannot detect a regression');
    }

    // =====================================================================
    // 4. PRINT MEDIA STILL GETS LAYOUT BOXES
    //    `content-visibility: auto` skips layout for off-screen content. In
    //    print there is no viewport, so a contained row could end up with no
    //    box and silently vanish from a printed/PDF result — the one output
    //    path where a missing row is data loss rather than a cosmetic bug.
    // =====================================================================
    console.log('');
    console.log('--- 4. print media still lays every row out ---');
    if (QUICK) {
      skip('print media keeps every row laid out (5000 rows)',
        '--quick: uses the 5000-row fixture');
    } else {
      await mount(mkResult(5000));
      await page.emulateMedia({ media: 'print' });
      const pr = await page.evaluate(() => {
        const rows = Array.prototype.slice.call(document.querySelectorAll(
          '#__perf_host .result-section[data-table="species_ranges"] tbody tr'));
        let boxed = 0;
        for (const r of rows) {
          if (r.getBoundingClientRect().height > 0) boxed++;
        }
        return { total: rows.length, boxed: boxed };
      });
      await page.emulateMedia({ media: 'screen' });
      eq('every row has a layout box under print media', pr.boxed, pr.total,
        pr.boxed + '/' + pr.total + ' rows');
      check('print kept all rows', pr.total > 0 && pr.boxed === pr.total,
        'total=' + pr.total);
    }

    // =====================================================================
    // 5. EXPORT READS THE MODEL, NOT THE DOM
    //    The A4 notice tells the operator the Copy-TSV / Download-CSV buttons
    //    "cover every row". That claim is only true because the export path
    //    goes rcaRowsForTable(data, tableId) (js/table.js:1524) and never
    //    touches the rendered tree — which is exactly what makes it safe under
    //    containment. Assert the last row of a 5000-row table survives.
    // =====================================================================
    console.log('');
    console.log('--- 5. export covers every row, containment or not ---');
    {
      const data = mkResult(1200);
      await mount(data);
      const exp = await page.evaluate((d) => {
        const all = window.rcaBuildTableExport(d, 'species_ranges');
        return {
          headers: all.headers.length,
          rows: all.rows.length,
          lastIndex: all.rows.length ? all.rows[all.rows.length - 1][0] : null,
          firstIndex: all.rows.length ? all.rows[0][0] : null,
        };
      }, data);
      eq('export row count', exp.rows, 1200);
      eq('export first row index', exp.firstIndex, '1');
      eq('export last row index', exp.lastIndex, '1200',
        'the final row must survive even though it was never scrolled into view');
      check('export produced headers', exp.headers > 1, exp.headers + ' columns');
    }

    // =====================================================================
    // 6. THE RENDER PATH ITSELF DID NOT REGRESS
    // =====================================================================
    console.log('');
    console.log('--- 6. render is still linear-ish and containment is actually active ---');
    {
      await mount(mkResult(700));
      const active = await page.evaluate(() => {
        const sec = document.querySelector('#__perf_host .result-section[data-table="species_ranges"]');
        const tr = sec.querySelector('tbody tr');
        const cs = getComputedStyle(tr);
        return {
          cv: cs.contentVisibility,
          height: cs.height,
          rowVar: cs.getPropertyValue('--rca-row-h').trim(),
          // what the CONTAINMENT is budgeted, which is the value that must be
          // a length (an `auto <length>` pair is a containment conflict and
          // silently disables the whole optimization -- js/table.js's comment
          // calls this out, so it is asserted rather than assumed)
          intrinsic: cs.containIntrinsicSize,
        };
      });
      eq('big table rows DO carry content-visibility', active.cv, 'auto');
      // A custom property returns its SPECIFIED token, not a resolved length:
      // getPropertyValue('--rca-row-h') is "2.25rem" even though the used
      // value is 36px. Assert the token, and the resolved height separately.
      eq('--rca-row-h is the declared token', active.rowVar, '2.25rem',
        'a custom property is not resolved by getPropertyValue');
      check('the declared row height resolves to 36px',
        Math.abs(parseFloat(active.height) - 36) < 0.6 || parseFloat(active.height) > 36,
        'computed height ' + active.height + 'px');
      // `contain-intrinsic-size: var(--rca-row-h)` serializes as "auto 36px"
      // in Chrome regardless of how it is authored (verified: authoring
      // `36px` also serializes to "auto 36px"), so the computed value cannot
      // distinguish the two forms. What CAN be checked is that the value is
      // NOT "none" — that would mean the row has no intrinsic size at all and
      // the scrollbar extent would collapse.
      check('contain-intrinsic-size carries a length',
        active.intrinsic !== 'none' && /\d/.test(active.intrinsic),
        'computed contain-intrinsic-size="' + active.intrinsic + '"');
      check('a contained row is never shorter than the declared height',
        parseFloat(active.height) >= 35.5,
        'computed height ' + active.height + 'px (height on a table row is a MINIMUM:'
        + ' a pre-wrap editable cell with a long value grows past it, which is'
        + ' asserted in section 2)');

      // A guard against the whole feature being silently disabled: rendering
      // 5000 rows must stay in the same order of magnitude as the measured
      // 1926 ms -> 583 ms improvement. Deliberately loose — this is a smoke
      // bound, not a benchmark, and CI machines are not this machine.
      if (QUICK) {
        skip('5000-row render time bound', '--quick: the slow case');
      } else {
        const perf = await page.evaluate(() => {
          const host = document.getElementById('__perf_host');
          const mk = (n) => {
            const rows = [];
            for (let i = 0; i < n; i++) {
              rows.push({ species: 'Radiolarius sp. ' + i, section: 'S' + (i % 9),
                range_base: 1000 + i, range_top: 2000 + i, zone: 'Z' + (i % 5),
                author_year: 'Jones 199' + (i % 10), agreement: (i % 3) + '/3',
                agreement_count: i % 3, note: '' });
            }
            return { runs: 3, confidence: 0.8, overall_confidence: 0.8,
              quality: { score: 0.7, grade: 'B', issues: [] },
              species_ranges: rows,
              sections: [{ name: 'S', age: 1, stage: 'St', lithology: 'sh', notes: '' }] };
          };
          const data = mk(5000);
          const t0 = performance.now();
          host.innerHTML = window.rcaRenderResults(data, '', { editable: true });
          void host.offsetHeight;
          return performance.now() - t0;
        });
        check('5000-row render+layout stays under 4s',
          perf < 4000,
          perf.toFixed(0) + ' ms (measured 1926 ms before containment on this machine;'
          + ' loose bound so CI hardware does not flake)');
      }
    }

    // =====================================================================
    // 7. IS THE CONTAINMENT ACTUALLY DOING ANYTHING?
    //
    // Everything above is a CONTRACT assertion: it holds whether or not the
    // containment engages. That is deliberate for the contracts, but it means
    // those assertions alone cannot tell "correctly engaged" from "entirely
    // inert" -- an inert containment also never changes a column width.
    //
    // This section closes that gap. It asks the question directly: IS THE
    // OFF-SCREEN SUBTREE ACTUALLY SKIPPED?
    //
    // The signal is the ACCESSIBILITY TREE, and it has to be: every geometry
    // probe is invalid here. Calling getClientRects() or
    // getBoundingClientRect() FORCES layout, which realizes the very content
    // whose skipping is being tested, so a rect-based check can never
    // distinguish the two states. Hit testing does not work either --
    // off-screen content is unreachable whether or not it was skipped.
    //
    // The accessibility tree has no such problem: a skipped subtree is not
    // rendered, therefore it is not exposed, therefore it is ABSENT from the
    // tree. Measured on this machine, with a <div> control where the
    // containment provably works:
    //     <div>, containment on  -> 0 of 300 items in the AX tree
    //     <div>, containment off -> 600 of 300 items in the AX tree
    // so the signal both fires and discriminates.
    // =====================================================================
    console.log('');
    console.log('--- 7. is the off-screen subtree actually skipped? (accessibility tree) ---');
    if (!cdp) {
      skip('off-screen rows are genuinely skipped (accessibility tree)',
        'no CDP Accessibility session available in this browser');
    } else {
      // A marker string that appears in exactly one cell of one row, so the
      // AX count is unambiguous.
      const marker = 'AXMARK';
      const buildMarked = () => page.evaluate((marker) => {
        const old = document.getElementById('__perf_host');
        if (old) old.remove();
        const host = document.createElement('div');
        host.id = '__perf_host';
        document.body.appendChild(host);
        const rows = [];
        for (let i = 0; i < 3000; i++) {
          rows.push({
            species: i === 0 ? marker + '-first' : 'Radiolarius sp. ' + i,
            section: 'S' + (i % 9),
            range_base: 1000 + i * 7, range_top: 2000 + i * 11,
            zone: 'Z' + (i % 5),
            author_year: 'Jones 19' + (50 + i % 40),
            agreement: (i % 3) + '/3', agreement_count: i % 3, note: '',
          });
        }
        const data = { runs: 3, confidence: 0.8, overall_confidence: 0.8,
          quality: { score: 0.7, grade: 'B', issues: [] },
          species_ranges: rows,
          sections: [{ name: 'S', age: 1, stage: 'St', lithology: 'sh', notes: '' }] };
        host.innerHTML = window.rcaRenderResults(data, '', { editable: true });
      }, marker);

      const axCount = async (re) => {
        await page.waitForTimeout(400);   // let the AX tree settle
        const ax = await cdp.send('Accessibility.getFullAXTree');
        return ax.nodes
          .map((n) => (n.name && n.name.value) || '')
          .filter((n) => re.test(n)).length;
      };

      // Control first: a <div> list where the containment provably works.
      // If the control does not discriminate, the measurement is broken and
      // the table result below would be meaningless.
      await page.evaluate(() => {
        const old = document.getElementById('__axctl');
        if (old) old.remove();
        const host = document.createElement('div');
        host.id = '__axctl';
        host.style.cssText = 'height:200px;overflow-y:auto;';
        document.body.appendChild(host);
        let h = '';
        for (let i = 0; i < 300; i++) {
          h += '<div style="height:40px;content-visibility:auto;contain-intrinsic-size:40px;">'
            + 'CTLMARK' + i + '</div>';
        }
        host.innerHTML = h;
      });
      const ctlOn = await axCount(/^CTLMARK\d+$/);
      await page.evaluate(() => {
        const host = document.getElementById('__axctl');
        let h = '';
        for (let i = 0; i < 300; i++) h += '<div style="height:40px;">CTLMARK' + i + '</div>';
        host.innerHTML = h;
      });
      const ctlOff = await axCount(/^CTLMARK\d+$/);
      check('CONTROL: the AX-tree probe can detect a skipped subtree',
        ctlOn < ctlOff / 4,
        'containment on -> ' + ctlOn + ' of 300 exposed, off -> ' + ctlOff
        + ' of 300 exposed');
      await page.evaluate(() => {
        const old = document.getElementById('__axctl');
        if (old) old.remove();
      });

      // The table.
      await buildMarked();
      const tableOn = await axCount(new RegExp('^' + marker + '-first$'));
      const tableSpecies = await axCount(/^Radiolarius sp\. \d+$/);
      const allRowsExposed = tableSpecies;

      // The FIRST row is on screen and must be present. The question is the
      // 3000-row table: is the off-screen bulk skipped or not?
      check('the on-screen first row IS exposed to assistive tech',
        tableOn > 0, tableOn + ' AX node(s) named ' + marker + '-first');

      // This is the finding: at the time of writing, the containment on <tr>
      // does NOT skip, so every one of the 3000 rows stays in the AX tree.
      // If a future change makes it skip, this number drops and the
      // measurement below starts reporting a real gain.
      // Each cell contributes more than one AX node (the cell and its text),
      // so the raw count is not a row count. Compare against the count with
      // the containment disabled, which is the meaningful ratio.
      const offRows = await page.evaluate(() => {
        const st = document.createElement('style');
        st.textContent = 'table.data-table[data-rca-big="1"] tbody tr'
          + ' { content-visibility: visible !important; contain-intrinsic-size: none !important; }';
        document.head.appendChild(st);
        return true;
      });
      void offRows;
      const tableOff = await axCount(/^Radiolarius sp\. \d+$/);
      console.log('    [info] AX nodes for the species column: containment ON -> '
        + allRowsExposed + ', containment OFF -> ' + tableOff
        + '  (equal => the off-screen rows were NOT skipped;'
        + ' a skipped subtree would expose only the visible rows)');
      console.log('    [info] containment IS active as a STYLE (content-visibility: auto),'
        + ' but the browser does not skip an internal table box, so the'
        + ' off-screen rows are still laid out and still exposed.');

      // The assertion that encodes the CURRENT, VERIFIED behaviour. It is
      // deliberately written as a measurement, not as an aspiration: it
      // documents what the browser actually does today, and it will FAIL LOUDLY
      // if a future browser starts skipping (which is good -- that is a
      // behaviour change somebody should look at, because it would change
      // what a screen reader can reach in a 5000-row table).
      check('off-screen rows are currently NOT skipped (browser does not skip internal table boxes)',
        allRowsExposed > 2000 && allRowsExposed === tableOff,
        'AX nodes ON=' + allRowsExposed + ' OFF=' + tableOff
        + ' (equal => nothing was skipped); if this diverges, the browser now'
        + ' skips <tr> and the containment finally pays off -- re-measure the win');
    }

    check('no uncaught page errors during the suite', pageErrors.length === 0,
      pageErrors.length ? pageErrors.join(' | ') : 'none');

  } finally {
    await browser.close().catch(() => {});
  }

  // ---- summary -----------------------------------------------------------
  const secs = ((Date.now() - started) / 1000).toFixed(1);
  console.log('');
  console.log('--- summary ---');
  console.log('passed  ' + passed);
  console.log('failed  ' + failures.length);
  console.log('skipped ' + skipped.length);
  console.log('elapsed ' + secs + 's' + (QUICK ? '  (QUICK MODE)' : ''));
  if (skipped.length) {
    console.log('');
    console.log('SKIPPED ASSERTIONS (these are NOT passing — they did not run):');
    for (const s of skipped) console.log('  - ' + s);
    console.log('');
    console.log('Re-run without --quick to execute them.');
  }
  if (failures.length) {
    console.log('');
    console.log('FAILURES:');
    for (const f of failures) console.log('  - ' + f);
    console.log('');
    console.log('tests_frontend_perf: ' + failures.length + ' failed, ' + passed
      + ' passed, ' + skipped.length + ' skipped');
    process.exit(1);
  }
  console.log('');
  console.log('tests_frontend_perf: ' + passed + ' passed, 0 failed'
    + (skipped.length ? ', ' + skipped.length + ' skipped (--quick)' : ''));
  process.exit(0);
})().catch(function (err) {
  console.error('tests_frontend_perf: FATAL');
  console.error(err && err.stack || err);
  process.exit(1);
});
