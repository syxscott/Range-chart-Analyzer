// UI review — error paths + cancel + empty data.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = 'http://127.0.0.1:8765/';
const { OUT, IMG, INJECT, stripCsp } = require('./helpers');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await stripCsp(page);
  const consoleErrs = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrs.push(m.text()); });
  page.on('pageerror', (e) => consoleErrs.push('PAGEERROR: ' + e.message));
  const shot = (name) => page.screenshot({ path: path.join(OUT, name) });
  const state = (label, data) => console.log(`\n=== ${label} ===\n` + JSON.stringify(data, null, 1));

  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');
  await page.fill('#api-key', 'sk-mock-1234');
  await page.setInputFiles('#file-input', IMG);
  await page.waitForSelector('#preview-wrap:not(.hidden)');

  // --- scenario 1: HTTP 500 ---
  await page.evaluate(() => window.__setMock('http500', 900));
  await page.click('#extract-btn');
  await page.waitForSelector('#alert-slot .alert-danger', { timeout: 8000 });
  await page.waitForTimeout(300);
  state('B1 http500', await page.evaluate(() => ({
    alert: document.getElementById('alert-slot').innerText.slice(0, 400),
    extractDisabled: document.getElementById('extract-btn').disabled,
    cancelHidden: document.getElementById('cancel-btn').classList.contains('hidden'),
    loadingHidden: document.getElementById('loading-slot').classList.contains('hidden'),
    emptyVisible: !document.getElementById('results-empty').classList.contains('hidden')
  })));
  await shot('05_error.png');

  // --- scenario 2: ok:false with 502-style body (HTTP 200) ---
  await page.evaluate(() => window.__setMock('fail200', 900));
  await page.click('#extract-btn');
  await page.waitForSelector('#alert-slot .alert-danger', { timeout: 8000 });
  await page.waitForTimeout(300);
  state('B2 fail200', await page.locator('#alert-slot').innerText().then((t) => t.slice(0, 400)));

  // --- scenario 3: empty data ---
  await page.evaluate(() => window.__setMock('empty', 900));
  await page.click('#extract-btn');
  await page.waitForSelector('#results-content:not(.hidden)', { timeout: 8000 });
  await page.waitForTimeout(700);
  state('B3 empty data', await page.evaluate(() => ({
    tables: document.querySelectorAll('.data-table').length,
    noRows: [...document.querySelectorAll('.cell-empty')].map((e) => e.textContent.trim()).slice(0, 6),
    alerts: [...document.querySelectorAll('#alert-slot .alert')].map((a) => a.className + '::' + a.innerText.slice(0, 120)),
    copyBtns: document.querySelectorAll('button[data-copy]').length
  })));
  await shot('05b_empty.png');

  // --- scenario 4: hang + cancel ---
  await page.evaluate(() => window.__setMock('hang', 0));
  await page.click('#extract-btn');
  await page.waitForTimeout(800);
  const busyBefore = { disabled: await page.locator('#extract-btn').isDisabled(), cancelVisible: await page.locator('#cancel-btn').isVisible() };
  await page.click('#cancel-btn');
  await page.waitForTimeout(600);
  state('B4 cancel', await page.evaluate(() => ({
    extractDisabled: document.getElementById('extract-btn').disabled,
    loadingHidden: document.getElementById('loading-slot').classList.contains('hidden'),
    cancelHidden: document.getElementById('cancel-btn').classList.contains('hidden'),
    toast: document.getElementById('toast').textContent,
    toastShown: document.getElementById('toast').classList.contains('show'),
    dangerAlert: !!document.querySelector('#alert-slot .alert-danger')
  })));
  await shot('05c_cancel.png');

  // --- scenario 5: busy guard (double-click extract while busy) ---
  await page.evaluate(() => window.__setMock('ok', 1500));
  await page.click('#extract-btn');
  await page.waitForTimeout(300);
  await page.click('#extract-btn').catch(() => 'click-blocked');
  const runsWhileBusy = await page.evaluate(() => window.__mockPostCount || 0);
  state('B5 busy guard (mock counts POSTs)', { mockPostCount: runsWhileBusy });
  await page.waitForSelector('#results-content:not(.hidden)', { timeout: 10000 });

  state('CONSOLE ERRORS', consoleErrs.length ? consoleErrs : 'none');
  await browser.close();
  console.log('\nDONE errors');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
