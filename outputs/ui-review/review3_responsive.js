// UI review — responsive layouts + overflow detection.
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

  const overflowReport = () => page.evaluate(() => {
    const de = document.documentElement;
    const wide = [...document.querySelectorAll('body *')]
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && (r.right > de.clientWidth + 1 || r.left < -1);
      })
      .slice(0, 12)
      .map((el) => {
        const r = el.getBoundingClientRect();
        return `${el.tagName}#${el.id}.${String(el.className).slice(0, 40)} L=${Math.round(r.left)} R=${Math.round(r.right)}`;
      });
    return {
      scrollW: de.scrollWidth,
      clientW: de.clientWidth,
      overflowX: de.scrollWidth - de.clientWidth,
      wideEls: wide
    };
  });

  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');
  await page.fill('#api-key', 'sk-mock-1234');
  await page.setInputFiles('#file-input', IMG);
  await page.waitForSelector('#preview-wrap:not(.hidden)');
  await page.evaluate(() => window.__setMock('ok', 700));
  await page.click('#extract-btn');
  await page.waitForSelector('#results-content:not(.hidden)', { timeout: 10000 });
  await page.waitForTimeout(800);

  // --- 1280x800 ---
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.waitForTimeout(500);
  state('C1 1280x800 overflow', await overflowReport());
  await shot('08_narrow.png');

  // --- 900x700 ---
  await page.setViewportSize({ width: 900, height: 700 });
  await page.waitForTimeout(500);
  state('C2 900x700 overflow', await overflowReport());
  await shot('09_compact.png');

  // --- 375x812 mobile ---
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForTimeout(500);
  state('C3 375x812 overflow', await overflowReport());
  await shot('10_mobile.png');

  // --- 320x640 small phone ---
  await page.setViewportSize({ width: 320, height: 640 });
  await page.waitForTimeout(500);
  state('C4 320x640 overflow', await overflowReport());
  await shot('11_tiny.png');

  // --- tablet 768 ---
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.waitForTimeout(500);
  state('C5 768x1024 overflow', await overflowReport());
  await shot('12_tablet.png');

  // header layout check at 375
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForTimeout(400);
  state('C6 header at 375', await page.evaluate(() => {
    const h = document.querySelector('.header');
    const hr = h.getBoundingClientRect();
    const ha = document.querySelector('.header-actions');
    const har = ha.getBoundingClientRect();
    return {
      headerW: Math.round(hr.width),
      headerBottom: Math.round(hr.bottom),
      actionsX: Math.round(har.left),
      actionsW: Math.round(har.width),
      h1Fits: h.scrollWidth <= h.clientWidth
    };
  }));
  await shot('13_header375.png');

  await browser.close();
  console.log('\nDONE responsive');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
