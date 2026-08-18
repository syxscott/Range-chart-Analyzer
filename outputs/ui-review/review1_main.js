// UI review — main flow (desktop 1440x900).
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8765/';
const { OUT, IMG, INJECT, stripCsp } = require('./helpers');

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
  const page = await ctx.newPage();
  await stripCsp(page);

  const consoleErrs = [];
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') consoleErrs.push(`[${m.type()}] ${m.text()}`); });
  page.on('pageerror', (e) => consoleErrs.push(`[pageerror] ${e.message}`));

  const shot = (name) => page.screenshot({ path: path.join(OUT, name) });
  const state = (label, data) => console.log(`\n=== ${label} ===\n` + JSON.stringify(data, null, 1));

  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');
  await page.waitForTimeout(600); // stagger animation settle
  await shot('01_landing.png');

  // --- empty-state validation: extract with no key, no image ---
  await page.click('#extract-btn');
  await page.waitForTimeout(300);
  state('A1 extract-no-key-no-image alert', await page.locator('#alert-slot').innerText());
  const btnDisabledEmpty = await page.locator('#extract-btn').isDisabled();
  state('A1 extract btn disabled?', btnDisabledEmpty);

  // --- no image (with key) ---
  await page.fill('#api-key', 'sk-mock-1234');
  await page.click('#extract-btn');
  await page.waitForTimeout(300);
  state('A2 extract-no-image alert', await page.locator('#alert-slot').innerText());

  // --- upload image ---
  await page.setInputFiles('#file-input', IMG);
  await page.waitForSelector('#preview-wrap:not(.hidden)');
  await page.waitForTimeout(400);
  state('A3 preview meta', await page.locator('#preview-meta').innerText());
  await shot('02_after_upload.png');

  // --- extraction (slow mock to capture loading state) ---
  await page.evaluate(() => window.__setMock('ok', 2600));
  await page.click('#extract-btn');
  await page.waitForTimeout(700);
  const busy = {
    extractDisabled: await page.locator('#extract-btn').isDisabled(),
    cancelVisible: await page.locator('#cancel-btn').isVisible(),
    loadingVisible: await page.locator('#loading-slot').isVisible(),
    loadingText: await page.locator('#loading-slot p').innerText(),
    btnLabel: await page.locator('#extract-btn').innerText()
  };
  state('A4 loading state', busy);
  await shot('02b_loading.png');

  await page.waitForSelector('#results-content:not(.hidden)', { timeout: 15000 });
  await page.waitForTimeout(900); // confidence tween

  // --- results inspection ---
  const results = await page.evaluate(() => {
    const q = (sel) => document.querySelectorAll(sel).length;
    const txt = (sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : null; };
    return {
      tables: q('.data-table'),
      sectionRows: q('tr[data-table]') ? null : q('.data-table') ? null : 0,
      speciesRows: document.querySelectorAll('.data-table')[1] ? document.querySelectorAll('.data-table')[1].querySelectorAll('tbody tr').length : 0,
      pills: q('.pill'),
      pillClasses: [...document.querySelectorAll('.pill')].map((p) => p.className),
      qualityBadge: q('.quality-badge'),
      qualityText: txt('.quality-badge'),
      confRing: txt('.confidence-ring .num'),
      alerts: [...document.querySelectorAll('#alert-slot .alert')].map((a) => a.className + ' :: ' + a.innerText),
      italicSpecies: (() => { const c = document.querySelector('.cell-species'); return c ? getComputedStyle(c).fontStyle : null; })(),
      numCells: q('.cell-num'),
      lowAgreementRows: q('.row-low-agreement'),
      exportAllBtn: q('#btn-export-all'),
      copyBtns: q('button[data-copy]'),
      csvBtns: q('button[data-csv]'),
      firstRow: (() => { const t = document.querySelectorAll('.data-table')[1]; return t ? t.innerText.slice(0, 200) : null; })()
    };
  });
  state('A5 results', results);
  await shot('03_results.png');

  // --- double-click edit attempt ---
  const speciesCell = page.locator('.data-table').nth(1).locator('tbody td.cell-species').first();
  await speciesCell.dblclick();
  await page.waitForTimeout(600);
  const editor = await page.evaluate(() => ({
    anyEditor: !!document.querySelector('[contenteditable], td input, td textarea'),
    activeTag: document.activeElement ? document.activeElement.tagName : null,
    cellText: document.querySelector('.data-table') ? document.querySelectorAll('.data-table')[1].querySelector('tbody td.cell-species').textContent : null
  }));
  state('A6 dblclick creates editor?', editor);
  await shot('04_edit.png');

  // --- copy TSV ---
  await page.locator('button[data-copy="species_ranges"]').first().click();
  await page.waitForTimeout(400);
  const clip = await page.evaluate(() => navigator.clipboard.readText().catch((e) => 'CLIPBOARD_ERR: ' + e));
  state('A7 clipboard TSV (first 300)', String(clip).slice(0, 300));

  // --- CSV download ---
  const csvPromise = page.waitForEvent('download', { timeout: 8000 });
  await page.locator('button[data-csv="species_ranges"]').first().click();
  const csvDl = await csvPromise;
  state('A8 CSV download', { filename: csvDl.suggestedFilename() });

  // --- export all JSON ---
  const jsonPromise = page.waitForEvent('download', { timeout: 8000 });
  await page.click('#btn-export-all');
  const jsonDl = await jsonPromise;
  state('A9 JSON download', { filename: jsonDl.suggestedFilename() });

  // --- i18n: EN / JA / ZH ---
  await page.click('button[data-lang="en"]');
  await page.waitForTimeout(500);
  state('A10 EN leftover keys', await page.evaluate(() => document.body.innerText.match(/\[\?[^\]]+\]/g) || 'none'));
  await shot('06_en.png');

  await page.click('button[data-lang="ja"]');
  await page.waitForTimeout(500);
  state('A11 JA leftover keys', await page.evaluate(() => document.body.innerText.match(/\[\?[^\]]+\]/g) || 'none'));
  await shot('06_ja.png');

  await page.click('button[data-lang="zh"]');
  await page.waitForTimeout(500);
  await shot('06_zh.png');

  // --- theme dark ---
  await page.click('button[data-theme-choice="dark"]');
  await page.waitForTimeout(500);
  state('A12 dark theme attr', await page.evaluate(() => document.documentElement.getAttribute('data-theme')));
  await shot('07_dark.png');

  // --- reset ---
  await page.click('#reset-btn');
  await page.waitForTimeout(400);
  state('A13 after reset', await page.evaluate(() => ({
    previewHidden: document.getElementById('preview-wrap').classList.contains('hidden'),
    resultsHidden: document.getElementById('results-content').classList.contains('hidden'),
    emptyVisible: !document.getElementById('results-empty').classList.contains('hidden'),
    caption: document.getElementById('caption').value
  })));

  state('CONSOLE ERRORS', consoleErrs.length ? consoleErrs : 'none');
  await browser.close();
  console.log('\nDONE main');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
