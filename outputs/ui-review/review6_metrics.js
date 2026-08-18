const { chromium } = require('playwright');
const path = require('path');
const BASE = 'http://127.0.0.1:8765/';
const { OUT, IMG, INJECT, stripCsp } = require('./helpers');

const luminance = (hex) => {
  const c = hex.match(/\d+/g).map(Number).slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const contrast = (a, b) => { const l1 = luminance(a), l2 = luminance(b); return ((Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)).toFixed(2); };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await stripCsp(page);
  await page.addInitScript(INJECT);
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#extract-btn');
  const state = (label, data) => console.log(`\n=== ${label} ===\n` + JSON.stringify(data, null, 1));

  // ---- landing geometry ----
  state('M1 landing geometry', await page.evaluate(() => {
    const r = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; };
    return {
      header: r(document.querySelector('.header')),
      subtitle: r(document.querySelector('.subtitle')),
      settingsCard: r(document.getElementById('settings-card')),
      uploadCard: r(document.getElementById('upload-card')),
      dropzone: r(document.getElementById('dropzone')),
      extractBtn: r(document.getElementById('extract-btn')),
      footer: r(document.querySelector('.footer')),
      dzTextAlign: getComputedStyle(document.getElementById('dropzone')).textAlign,
      dropzoneVisible: document.getElementById('dropzone').offsetHeight > 0
    };
  }));

  // ---- contrast checks (light) ----
  state('M2 contrast (light)', await page.evaluate((fn) => {
    const lum = (c) => { const m = c.match(/\d+/g).map(Number).slice(0,3).map(v => { v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); }); return 0.2126*m[0]+0.7152*m[1]+0.0722*m[2]; };
    const ratio = (a,b) => { const l1=lum(a), l2=lum(b); return (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05); };
    const cs = (el, prop) => getComputedStyle(el)[prop];
    const out = {};
    const checks = [
      ['body text', document.body, 'color', 'backgroundColor'],
      ['subtitle', document.querySelector('.subtitle'), 'color', 'backgroundColor'],
      ['primary btn text', document.getElementById('extract-btn'), 'color', 'backgroundColor'],
      ['secondary btn text', document.getElementById('reset-btn'), 'color', 'backgroundColor'],
      ['field hint', document.querySelector('.field-hint'), 'color', 'backgroundColor'],
      ['dz title', document.querySelector('.dz-title'), 'color', 'backgroundColor'],
    ];
    for (const [name, el, fgProp, bgProp] of checks) {
      const fg = cs(el, fgProp), bg = cs(el, bgProp);
      out[name] = { fg, bg, ratio: ratio(fg, bg).toFixed(2) };
    }
    return out;
  }));

  // ---- results page ----
  await page.fill('#api-key', 'sk-mock-1234');
  await page.setInputFiles('#file-input', IMG);
  await page.waitForSelector('#preview-wrap:not(.hidden)');
  await page.evaluate(() => window.__setMock('ok', 600));
  await page.click('#extract-btn');
  await page.waitForSelector('#results-content:not(.hidden)', { timeout: 10000 });
  await page.waitForTimeout(900);

  state('M3 toolbar geometry', await page.evaluate(() => {
    const r = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; };
    return {
      toolbar: r(document.querySelector('.results-toolbar')),
      ring: r(document.querySelector('.confidence-ring')),
      badge: r(document.querySelector('.quality-badge')),
      label: r(document.querySelector('.results-toolbar .label')),
      exportAll: r(document.getElementById('btn-export-all')),
      ringBaseline: (() => { const a = document.querySelector('.confidence-ring').getBoundingClientRect().bottom; const b = document.querySelector('#btn-export-all').getBoundingClientRect().bottom; return a - b; })(),
      toolbarHeight: document.querySelector('.results-toolbar').getBoundingClientRect().height
    };
  }));

  // th/td alignment: compare header cell x positions vs body cell x positions
  state('M4 table column alignment', await page.evaluate(() => {
    const t = document.querySelectorAll('.data-table')[1];
    const ths = [...t.querySelectorAll('thead th')].map((h) => Math.round(h.getBoundingClientRect().x));
    const tds = [...t.querySelectorAll('tbody tr:first-child td')].map((h) => Math.round(h.getBoundingClientRect().x));
    return { thX: ths, tdX: tds, aligned: JSON.stringify(ths) === JSON.stringify(tds) };
  }));

  // overlap detection on results
  state('M5 overlapping elements', await page.evaluate(() => {
    const vis = [...document.querySelectorAll('#results-card *, .toast.show, .header *')].filter((el) => {
      const s = getComputedStyle(el); const r = el.getBoundingClientRect();
      return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
    });
    const bad = [];
    for (let i = 0; i < vis.length; i++) for (let j = i + 1; j < vis.length; j++) {
      const a = vis[i].getBoundingClientRect(), b = vis[j].getBoundingClientRect();
      if (a.left >= b.right || b.left >= a.right || a.top >= b.bottom || b.top >= a.bottom) continue;
      const inter = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const iw = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      const area = inter * iw;
      const minArea = Math.min(a.width * a.height, b.width * b.height);
      if (area > 0 && area / minArea > 0.15) {
        const parentOf = (el) => el.closest('.result-section, .results-toolbar') ? el.closest('.result-section, .results-toolbar').className : '';
        bad.push(`${vis[i].tagName}.${String(vis[i].className).slice(0,24)} vs ${vis[j].tagName}.${String(vis[j].className).slice(0,24)} [${Math.round(area / minArea * 100)}%] (in: ${parentOf(vis[i])})`);
      }
    }
    return bad.slice(0, 15);
  }));

  // --- narrow toolbar check at 375 ---
  await page.setViewportSize({ width: 375, height: 812 });
  await page.waitForTimeout(400);
  state('M6 toolbar at 375', await page.evaluate(() => {
    const r = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.x), w: Math.round(b.width), right: Math.round(b.right) }; };
    const tb = r(document.querySelector('.results-toolbar'));
    const action = r(document.querySelector('.rt-actions'));
    return { toolbar: tb, actions: action, fits: tb.right <= 375, wrap: getComputedStyle(document.querySelector('.results-toolbar')).flexWrap };
  }));
  await page.screenshot({ path: path.join(OUT, '17_toolbar375.png') });

  // --- dark theme color swap check ---
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.click('button[data-theme-choice="dark"]');
  await page.waitForTimeout(400);
  state('M7 dark theme colors', await page.evaluate(() => {
    const g = (el, p) => getComputedStyle(el)[p];
    return {
      bodyBg: g(document.body, 'backgroundColor'),
      cardBg: g(document.querySelector('.card'), 'backgroundColor'),
      text: g(document.body, 'color'),
      tableHeadBg: g(document.querySelector('.data-table thead th'), 'backgroundColor'),
      badgeBorder: g(document.querySelector('.quality-badge'), 'borderColor'),
      themeAttr: document.documentElement.getAttribute('data-theme')
    };
  }));
  await page.screenshot({ path: path.join(OUT, '18_dark_results.png') });

  // --- caption counter ---
  await page.fill('#caption', 'x'.repeat(1200));
  state('M8 caption counter', await page.evaluate(() => ({
    counter: document.getElementById('caption-counter').textContent,
    cls: document.getElementById('caption-counter').className
  })));

  await browser.close();
  console.log('DONE metrics');
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
