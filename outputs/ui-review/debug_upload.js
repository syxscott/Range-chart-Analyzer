const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const BASE = 'http://127.0.0.1:8765/';
const OUT = 'D:/GIthub/Range-chart Analyzer/outputs/ui-review';
const IMG = 'D:/GIthub/Range-chart Analyzer/tests/fixtures/gold/range_chart/rc_synth_001/image.png';
const { INJECT, stripCsp } = require('./helpers');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await stripCsp(page);
  page.on('console', (m) => console.log('CONSOLE', m.type(), m.text()));
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message));
  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');
  await page.setInputFiles('#file-input', IMG);
  await page.waitForTimeout(2500);
  const info = await page.evaluate(() => ({
    alertSlot: document.getElementById('alert-slot').innerText,
    previewHidden: document.getElementById('preview-wrap').classList.contains('hidden'),
    fileInputFiles: document.getElementById('file-input').files.length,
    caption: document.getElementById('caption').value
  }));
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
