// UI review — a11y: tab order, focus visibility, ARIA states, labels.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8765/';
const { OUT, IMG, INJECT, stripCsp } = require('./helpers');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await stripCsp(page);
  const state = (label, data) => console.log(`\n=== ${label} ===\n` + JSON.stringify(data, null, 1));
  const shot = (name) => page.screenshot({ path: path.join(OUT, name) });

  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');

  // --- tab order (first 18 tabs) ---
  const tabSeq = [];
  for (let i = 0; i < 18; i++) {
    await page.keyboard.press('Tab');
    await page.waitForTimeout(60);
    tabSeq.push(await page.evaluate(() => {
      const el = document.activeElement;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        tag: el.tagName,
        id: el.id,
        cls: String(el.className).slice(0, 30),
        focusVisible: cs.outlineStyle !== 'none' && cs.outlineWidth !== '0px' ? (cs.outlineWidth + ' ' + cs.outlineStyle + ' ' + cs.outlineColor) : 'NONE',
        tabindex: el.tabIndex,
        rect: `${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}x${Math.round(r.height)}`
      };
    }));
  }
  state('D1 tab order', tabSeq);

  // --- dropzone keyboard: focus + Enter opens file dialog ---
  await page.locator('#dropzone').focus();
  state('D2 dropzone focused', await page.evaluate(() => ({
    tag: document.activeElement.tagName,
    outline: getComputedStyle(document.activeElement).outlineStyle
  })));
  await shot('14_dz_focus.png');
  // Enter on dropzone should open the file chooser — we can't see the OS dialog,
  // but confirm focus stays and no crash; use page.on('filechooser') to catch it.
  const fcPromise = page.waitForEvent('filechooser', { timeout: 3000 }).catch(() => null);
  await page.keyboard.press('Enter');
  const fc = await fcPromise;
  state('D3 dropzone Enter opens chooser', { opened: !!fc });
  if (fc) await fc.setFiles(IMG).catch(() => {});
  await page.waitForTimeout(400);

  // --- ARIA checks ---
  state('D4 aria states', await page.evaluate(() => ({
    langBtns: [...document.querySelectorAll('#lang-switch button')].map((b) => [b.getAttribute('data-lang'), b.getAttribute('aria-selected')]),
    themeBtns: [...document.querySelectorAll('.theme-toggle button')].map((b) => [b.getAttribute('data-theme-choice'), b.getAttribute('aria-pressed')]),
    segRadios: [...document.querySelectorAll('#conn-mode-seg button')].map((b) => [b.getAttribute('data-value'), b.getAttribute('aria-checked')]),
    rememberKey: document.getElementById('remember-key').getAttribute('aria-checked'),
    fileInput: { label: document.querySelector('label[for="file-input"]') ? 'yes' : 'no', id: document.getElementById('file-input').id },
    dropzoneRole: document.getElementById('dropzone').getAttribute('role'),
    dzAria: document.getElementById('dz-aria').textContent.slice(0, 60),
    unnamedButtons: [...document.querySelectorAll('button')].filter((b) => !(b.textContent.trim() || b.getAttribute('aria-label') || b.getAttribute('title'))).map((b) => b.id || b.className).slice(0, 10)
  })));

  // --- labels & form association ---
  state('D5 form labels', await page.evaluate(() => {
    const out = {};
    ['api-key', 'endpoint', 'model', 'max-tokens', 'max-edge', 'enhance', 'runs', 'proxy', 'caption', 'chart-mode', 'chart-lang'].forEach((id) => {
      const el = document.getElementById(id);
      out[id] = { labelFor: !!document.querySelector(`label[for="${id}"]`), ariaLabelled: !!(el && (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby'))) };
    });
    return out;
  }));

  // --- run extraction, then check result-table a11y ---
  await page.fill('#api-key', 'sk-mock-1234');
  await page.evaluate(() => window.__setMock('ok', 500));
  await page.click('#extract-btn');
  await page.waitForSelector('#results-content:not(.hidden)', { timeout: 10000 });
  await page.waitForTimeout(700);
  state('D6 result a11y', await page.evaluate(() => {
    const tables = [...document.querySelectorAll('.data-table')];
    return {
      tables: tables.length,
      tableLabels: tables.map((t) => t.getAttribute('aria-label')).slice(0, 4),
      scopeTh: tables[1] ? tables[1].querySelectorAll('thead th[scope="col"]').length : 0,
      scopeRowTh: tables[1] ? tables[1].querySelectorAll('tbody th[scope="row"]').length : 0,
      actionBtnNames: [...document.querySelectorAll('#results-content button')].map((b) => (b.textContent || '').trim().slice(0, 20)),
      alertRole: document.querySelector('#alert-slot .alert') ? document.querySelector('#alert-slot .alert').getAttribute('role') : null
    };
  }));

  // --- focus into results table: tab from toolbar into table cells ---
  await page.locator('#btn-export-all').focus();
  await page.keyboard.press('Tab');
  await page.waitForTimeout(150);
  state('D7 focus after toolbar', await page.evaluate(() => {
    const el = document.activeElement;
    return { tag: el.tagName, cls: String(el.className).slice(0, 30), outline: getComputedStyle(el).outlineStyle };
  }));

  await shot('15_focus_results.png');
  await browser.close();
  console.log('\nDONE a11y');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
