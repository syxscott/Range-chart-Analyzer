const { chromium } = require('playwright');
const path = require('path');
const BASE = 'http://127.0.0.1:8765/';
const { OUT, INJECT, stripCsp } = require('./helpers');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await stripCsp(page);
  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');

  // focus api-key via keyboard (Tab from lang switch)
  await page.locator('button[data-lang="ja"]').focus();
  await page.keyboard.press('Tab'); // api-key
  await page.waitForTimeout(200);
  const inputFocus = await page.evaluate(() => {
    const el = document.getElementById('api-key');
    const cs = getComputedStyle(el);
    return {
      borderColor: cs.borderColor,
      boxShadow: cs.boxShadow,
      outline: cs.outlineStyle + ' ' + cs.outlineWidth,
      active: document.activeElement === el,
      theme: document.documentElement.getAttribute('data-theme')
    };
  });
  console.log('FOCUS-INPUT', JSON.stringify(inputFocus, null, 1));
  await page.screenshot({ path: path.join(OUT, '16_focus_input.png') });

  // slider focus
  await page.keyboard.press('Tab'); // api-key-toggle
  await page.keyboard.press('Tab'); // endpoint
  await page.keyboard.press('Tab'); // model
  await page.keyboard.press('Tab'); // max-tokens slider
  await page.waitForTimeout(200);
  const sliderFocus = await page.evaluate(() => {
    const el = document.getElementById('max-tokens');
    const cs = getComputedStyle(el);
    return { active: document.activeElement === el, boxShadow: cs.boxShadow, borderColor: cs.borderColor };
  });
  console.log('FOCUS-SLIDER', JSON.stringify(sliderFocus, null, 1));

  // arrow keys move slider?
  const before = await page.locator('#max-tokens').inputValue();
  await page.keyboard.press('ArrowRight');
  const after = await page.locator('#max-tokens').inputValue();
  console.log('SLIDER-KEYBOARD', JSON.stringify({ before, after }));

  // check remember-key accessible name
  const rkName = await page.evaluate(() => {
    const b = document.getElementById('remember-key');
    return { text: b.textContent.trim(), ariaLabel: b.getAttribute('aria-label'), title: b.getAttribute('title'), accName: b.innerText.trim() || null };
  });
  console.log('REMEMBER-KEY', JSON.stringify(rkName));

  await browser.close();
  console.log('DONE focus');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
