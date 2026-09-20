// tests_contrast.js — WCAG AA contrast audit for the RCA design tokens.
//
// FE-BORROW-2026-09-20 (domain U): rewritten to READ the palette out of
// css/style.css (plus the app-ux.css layer) instead of duplicating it here.
// The previous hand-copied LIGHT/DARK tables had already drifted twice — they
// kept reporting green while the real CSS shipped #1e3a8a-on-#1e3a8a dropzone
// hover text (1.0:1) and #065f46-on-#064e3b dark badges (1.26:1). An audit
// that re-declares its subject can only ever confirm the author's belief, so
// every colour below is now resolved from the stylesheet at run time:
//
//   * tokens are parsed from the `:root` blocks (light) and from the
//     [data-theme="dark"] / @media (prefers-color-scheme: dark) blocks (dark);
//   * element/pill/badge colours come from the matching CSS rules, following
//     cascade order across the linked stylesheets;
//   * `var(--token)` references — including fallbacks and chains — are
//     resolved against the theme map, rgba()/rgb() are alpha-blended over
//     their background, so nothing has to be recomputed by hand either.
//
// Thresholds: WCAG AA 4.5:1 for normal text, 3:1 for non-text (focus rings,
// progress fills). Decorative chrome (input borders, card shadows, the
// header/body background split) is deliberately NOT asserted — those are
// duplicated by text and labels, and 1.4.11 does not apply.
//
// Run:  node tests_contrast.js
'use strict';

const fs = require('fs');
const path = require('path');

// ---------- color math (no deps) ----------

function parseColor(value) {
  if (value == null) return null;
  let v = String(value).trim().replace(/!important$/i, '').trim();
  if (!v) return null;
  if (v[0] === '#') {
    let h = v.slice(1);
    if (/^[0-9a-f]{3}$/.test(h)) h = h.split('').map((c) => c + c).join('');
    else if (/^[0-9a-f]{4}$/.test(h)) h = h.slice(0, 3).split('').map((c) => c + c).join('');
    else if (/^[0-9a-f]{8}$/.test(h)) h = h.slice(0, 6);
    else if (!/^[0-9a-f]{6}$/.test(h)) return null;
    const n = parseInt(h, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
  }
  const m = /^rgba?\(([^)]*)\)$/i.exec(v);
  if (m) {
    const parts = m[1].split(/[,/\s]+/).filter(Boolean);
    if (parts.length < 3) return null;
    const num = (p, scale) => {
      if (/%$/.test(p)) return (parseFloat(p) / 100) * scale;
      return parseFloat(p);
    };
    const r = num(parts[0], 255), g = num(parts[1], 255), b = num(parts[2], 255);
    if ([r, g, b].some((x) => Number.isNaN(x))) return null;
    let a = 1;
    if (parts.length > 3) {
      a = parts[3].endsWith('%') ? parseFloat(parts[3]) / 100 : parseFloat(parts[3]);
      if (Number.isNaN(a)) return null;
    }
    return { r, g, b, a };
  }
  const named = {
    white: { r: 255, g: 255, b: 255, a: 1 },
    black: { r: 0, g: 0, b: 0, a: 1 },
  };
  if (named[v.toLowerCase()]) return Object.assign({}, named[v.toLowerCase()]);
  // `transparent`, `currentColor`, `none`, lengths, gradients → not a solid.
  return null;
}

function relativeLuminance(c) {
  const lin = (v) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
}

function contrastRatio(a, b) {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}

// Flatten a translucent colour onto the opaque background it sits on.
function blendOver(fg, bg) {
  if (fg.a >= 1) return { r: fg.r, g: fg.g, b: fg.b, a: 1 };
  const a = fg.a;
  return {
    r: a * fg.r + (1 - a) * bg.r,
    g: a * fg.g + (1 - a) * bg.g,
    b: a * fg.b + (1 - a) * bg.b,
    a: 1,
  };
}

const asHex = (c) =>
  '#' + [c.r, c.g, c.b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');

// ---------- minimal CSS reader ----------

const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '');

function splitTop(value, sep) {
  const out = [];
  let depth = 0, cur = '';
  for (const ch of value) {
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    if (ch === sep && depth === 0) { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

function parseDecls(body) {
  const decls = {};
  for (const part of body.split(';')) {
    const idx = part.indexOf(':');
    if (idx === -1) continue;
    const k = part.slice(0, idx).trim().toLowerCase();
    const v = part.slice(idx + 1).trim();
    if (k && !k.startsWith('--@')) decls[k] = v;
  }
  return decls;
}

// Recursive block walker: returns a flat list of
// {selector, media:[...], decls:{}, dark:boolean} in document order.
function collectRules(css, media, out) {
  let i = 0;
  while (i < css.length) {
    const open = css.indexOf('{', i);
    if (open === -1) break;
    const prelude = css.slice(i, open).trim();
    let depth = 1, j = open + 1;
    while (j < css.length && depth > 0) {
      const c = css[j];
      if (c === '{') depth++;
      else if (c === '}') depth--;
      j++;
    }
    const body = css.slice(open + 1, j - 1);
    if (/^@media|^@supports/.test(prelude)) {
      collectRules(body, media.concat(prelude.replace(/^@\w+\s*/, '').trim()), out);
    } else if (!/^@/.test(prelude)) {
      const dark = /data-theme=["']?dark/.test(prelude)
        || media.some((m) => /prefers-color-scheme:\s*dark/.test(m));
      for (const selector of splitTop(prelude, ',')) {
        out.push({ selector, media, dark, decls: parseDecls(body) });
      }
    }
    i = j;
  }
  return out;
}

const readCss = (rel) => {
  const file = path.join(__dirname, rel);
  if (!fs.existsSync(file)) return '';
  return stripComments(fs.readFileSync(file, 'utf8'));
};

// Linked stylesheet order (index.html): style.css first, then the layers that
// may override it. Missing files read as '' so the audit degrades with them.
const RULES = []
  .concat(collectRules(readCss('css/style.css'), [], []))
  .concat(collectRules(readCss('css/app-ux.css'), [], []));

const isRootBlock = (r, names) => names.some((n) => r.selector === n);

function tokensFrom(selectorNames, dark) {
  const map = {};
  for (const r of RULES) {
    if (r.dark !== dark) continue;
    if (!isRootBlock(r, selectorNames)) continue;
    for (const k of Object.keys(r.decls)) if (k.startsWith('--')) map[k] = r.decls[k];
  }
  return map;
}

const LIGHT = tokensFrom([':root'], false);
const DARK = Object.assign(
  {},
  LIGHT,
  tokensFrom([':root:not([data-theme])'], true),      // OS-preference path
  tokensFrom([':root[data-theme="dark"]'], true)      // explicit toggle path
);

const themeTokens = (theme) => (theme === 'dark' ? DARK : LIGHT);

// Resolve a declaration value to a solid colour, following var() chains
// (including `var(--x, fallback)`), and alpha-blending rgba() over `over`.
function resolveColor(value, tokens, over) {
  let v = String(value == null ? '' : value).trim();
  const seen = [];
  for (let guard = 0; guard < 12; guard++) {
    const m = /^var\(\s*(--[\w-]+)\s*(?:,([\s\S]*))?\)$/i.exec(v);
    if (!m) break;
    const name = m[1];
    if (seen.includes(name)) return null;         // cyclic reference
    seen.push(name);
    if (Object.prototype.hasOwnProperty.call(tokens, name)) {
      v = String(tokens[name]).trim();
    } else if (m[2] != null) {
      v = m[2].trim();
    } else {
      return null;
    }
  }
  const c = parseColor(v);
  if (!c) return null;
  return over ? blendOver(c, over) : c;
}

// First solid colour found inside a (possibly shorthand) value.
function colorIn(value, tokens, over) {
  if (value == null) return null;
  const direct = resolveColor(value, tokens, over);
  if (direct) return direct;
  for (const tok of splitTop(String(value), ' ')) {
    const c = resolveColor(tok, tokens, over);
    if (c) return c;
  }
  return null;
}

// Cheap (a,b,c) specificity — enough to reproduce why
// `:root[data-theme="dark"] .badge-success` beats the base `.badge-success`
// rule even though the base rule appears later in the sheet.
function specificity(sel) {
  let s = sel;
  let a = 0, b = 0, c = 0;
  const attrs = s.match(/\[[^\]]*\]/g) || [];
  b += attrs.length;
  s = s.replace(/\[[^\]]*\]/g, ' ');
  const pseudoElems = s.match(/::[\w-]+/g) || [];
  c += pseudoElems.length;
  s = s.replace(/::[\w-]+/g, ' ');
  const pseudoClasses = s.match(/:(?!:)[\w-]+(\([^)]*\))?/g) || [];
  // :is()/:not() take the specificity of their argument; count one class each.
  b += pseudoClasses.length;
  s = s.replace(/:(?!:)[\w-]+(\([^)]*\))?/g, ' ');
  a += (s.match(/#[\w-]+/g) || []).length;
  s = s.replace(/#[\w-]+/g, ' ');
  b += (s.match(/\.[\w-]+/g) || []).length;
  s = s.replace(/\.[\w-]+/g, ' ');
  c += (s.match(/(^|[\s>+~(])\*|(^|[\s>+~])\b[a-z][\w-]*\b/gi) || []).length;
  return a * 10000 + b * 100 + c;
}

// Highest-specificity declaration for `prop` that applies to `baseSel` in
// `theme`; equal specificity is broken by document order (later sheet/block
// wins), which is also how the browser resolves style.css vs app-ux.css. The
// dark pass sees EVERY rule: the base declarations still apply, and the
// dark-scoped ones override them — exactly as `[data-theme="dark"]
// .badge-success { color: … }` does on a real page.
function declFor(baseSel, prop, theme) {
  const darkPass = theme === 'dark';
  let value = null;
  let best = -1;
  for (const r of RULES) {
    if (!darkPass && r.dark) continue;
    if (r.decls[prop] === undefined) continue;
    const selectors = splitTop(r.selector, ',');
    let hit = false;
    let spec = 0;
    for (const s of selectors) {
      if (s === baseSel || s.endsWith(' ' + baseSel)) {
        hit = true;
        spec = Math.max(spec, specificity(s));
      }
    }
    if (!hit) continue;
    if (spec >= best) { best = spec; value = r.decls[prop]; }
  }
  return value;
}

function colorFor(src, theme) {
  const tokens = themeTokens(theme);
  if (src.token) {
    const c = resolveColor(tokens[src.token], tokens);
    return { color: c, label: src.token };
  }
  const props = Array.isArray(src.prop) ? src.prop : [src.prop || 'color'];
  let raw = null;
  let used = '';
  for (const p of props) {
    const v = declFor(src.sel, p, theme);
    if (v != null) { raw = v; used = p; break; }
  }
  const label = src.sel + ' {' + used + '}';
  if (raw == null) return { color: null, label };
  const bg = src.over
    ? colorFor(src.over, theme).color
    : (src.overToken ? resolveColor(tokens[src.overToken], tokens) : null);
  return { color: colorIn(raw, tokens, bg && bg.a >= 1 ? bg : null), label };
}

// ---------- audit table ----------

// Text-on-text pairs that are pure token questions (body copy, links).
const TOKEN_PAIRS = [
  ['--text-dark', '--bg-page', 4.5],
  ['--text-dark', '--bg-white', 4.5],
  ['--text-base', '--bg-page', 4.5],
  ['--text-base', '--bg-white', 4.5],
  ['--text-base', '--bg-light', 4.5],
  ['--text-base', '--bg-lighter', 4.5],
  ['--text-muted', '--bg-page', 4.5],
  ['--text-muted', '--bg-white', 4.5],
  ['--text-muted', '--bg-light', 4.5],
  ['--text-muted', '--bg-lighter', 4.5],
  ['--text-light', '--bg-white', 4.5],
  ['--text-light', '--bg-page', 4.5],
  ['--primary-color', '--bg-white', 4.5],
  ['--primary-color', '--bg-page', 4.5],
  ['--primary-dark', '--primary-soft', 4.5],   // dropzone hover, both themes
];

// Component pairs resolved from the rules themselves. `bg:'self'` means "the
// element's own background property"; `over` nests another selector's
// background (for translucent fills); a `bgToken` names the surface the text
// actually sits on when the element itself has no background.
const COMPONENT_PAIRS = [
  { name: 'primary button', sel: '.btn-primary', bgSel: '.btn-primary' },
  { name: 'secondary button', sel: '.btn-secondary', bgSel: '.btn-secondary' },
  { name: 'badge-success', sel: '.badge-success', bgSel: '.badge-success' },
  { name: 'badge-warning', sel: '.badge-warning', bgSel: '.badge-warning' },
  { name: 'badge-danger', sel: '.badge-danger', bgSel: '.badge-danger' },
  { name: 'badge-info', sel: '.badge-info', bgSel: '.badge-info' },
  { name: 'badge-muted', sel: '.badge-muted', bgSel: '.badge-muted' },
  { name: 'pill-good', sel: '.pill-good', bgSel: '.pill-good' },
  { name: 'pill-mid', sel: '.pill-mid', bgSel: '.pill-mid' },
  { name: 'pill-low', sel: '.pill-low', bgSel: '.pill-low' },
  { name: 'alert-success', sel: '.alert-success', bgSel: '.alert-success' },
  { name: 'alert-warning', sel: '.alert-warning', bgSel: '.alert-warning' },
  { name: 'alert-danger', sel: '.alert-danger', bgSel: '.alert-danger' },
  { name: 'alert-info', sel: '.alert-info', bgSel: '.alert-info' },
  { name: 'quality badge high', sel: '.quality-badge.high', bgSel: '.quality-badge.high' },
  { name: 'quality badge mid', sel: '.quality-badge.mid', bgSel: '.quality-badge.mid' },
  { name: 'quality badge low', sel: '.quality-badge.low', bgSel: '.quality-badge.low' },
  { name: 'dropzone idle', sel: '.dropzone', bgSel: '.dropzone' },
  { name: 'dropzone hover', sel: '.dropzone:hover', bgSel: '.dropzone:hover' },
  { name: 'dropzone title', sel: '.dropzone .dz-title', bgSel: '.dropzone' },
  { name: 'preview meta', sel: '.preview-meta', bgSel: '.card' },
  { name: 'segmented hover', sel: '.segmented button:hover', bgSel: '.segmented button:hover' },
  { name: 'lang button idle', sel: '.lang-switch button', bgSel: '.lang-switch' },
  { name: 'lang button hover', sel: '.lang-switch button:hover', bgSel: '.lang-switch button:hover' },
  { name: 'lang button active', sel: '.lang-switch button.active', bgSel: '.lang-switch button.active' },
  { name: 'lang button aria-checked', sel: '.lang-switch button[aria-checked="true"]', bgSel: '.lang-switch button[aria-checked="true"]' },
  { name: 'confirm dialog', sel: '.rca-dialog', bgSel: '.rca-dialog' },
  { name: 'table header (sticky)', sel: 'table.data-table thead th', bgSel: 'table.data-table thead th' },
  { name: 'table cell', sel: 'table.data-table tbody td', bgToken: '--bg-white' },
  // Counter states sit on the card surface, not on their own background.
  { name: 'caption counter', sel: '.caption-counter', bgToken: '--bg-white' },
  { name: 'caption counter warn', sel: '.caption-counter.warn', bgToken: '--bg-white' },
  { name: 'caption counter over', sel: '.caption-counter.over', bgToken: '--bg-white' },
];

// Non-text indicators: focus rings and the determinate progress fill.
const NON_TEXT_PAIRS = [
  { name: 'focus ring on card', sel: ':focus-visible', bgToken: '--bg-white' },
  { name: 'focus ring on page', sel: ':focus-visible', bgToken: '--bg-page' },
  { name: 'input focus ring on card', sel: 'input:focus-visible', bgToken: '--bg-white' },
  { name: 'range focus ring on card', sel: '.range-row input[type=range]:focus-visible', bgToken: '--bg-white' },
  { name: 'focus ring on danger alert', sel: ':focus-visible', bgSel: '.alert-danger' },
  { name: 'focus ring on warning alert', sel: ':focus-visible', bgSel: '.alert-warning' },
  { name: 'progress fill on track', sel: '.run-progress-fill', bgSel: '.run-progress', min: 3 },
];

const TEXT_MIN = 4.5;
const NON_TEXT_MIN = 3.0;

let pass = 0;
let fail = 0;
const failures = [];

function check(label, ok, detail) {
  if (ok) { pass++; return; }
  fail++;
  failures.push(label + (detail ? ' — ' + detail : ''));
  console.log('FAIL ' + label + (detail ? ' — ' + detail : ''));
}

function ratio(label, fgSrc, bgSrc, min, theme) {
  const fg = colorFor(fgSrc, theme);
  const bg = colorFor(bgSrc, theme);
  if (!fg.color || !bg.color) {
    check(`[${theme}] ${label} (${fg.label} on ${bg.label})`, false,
      'colour not resolvable from CSS (drifted/removed declaration)');
    return;
  }
  const r = contrastRatio(fg.color, bg.color);
  check(
    `[${theme}] ${label} ${asHex(fg.color)} on ${asHex(bg.color)}`,
    r >= min,
    `ratio=${r.toFixed(2)} (need >= ${min})`
  );
}

// ---- 0. parser sanity: a silent regex drift must fail, not shrink the audit
const REQUIRED_TOKENS = [
  '--text-dark', '--text-base', '--text-muted', '--text-light',
  '--bg-page', '--bg-white', '--bg-light', '--bg-lighter',
  '--primary-color', '--primary-soft', '--primary-dark', '--primary-active',
  '--success-soft', '--warning-soft', '--danger-soft', '--info-soft',
];
for (const tok of REQUIRED_TOKENS) {
  check(`token ${tok} parsed from :root`, Object.prototype.hasOwnProperty.call(LIGHT, tok),
    'not found in the light :root block — parser or token name drifted');
  check(`token ${tok} present + resolvable in the dark map`,
    !!resolveColor(DARK[tok], DARK), `dark value=${JSON.stringify(DARK[tok])}`);
}
// Every surface/text token must flip in dark mode — one that does not means a
// component keeps its light colours on a dark card (the class of bug that
// made the dark badges 1.26:1).
for (const tok of REQUIRED_TOKENS) {
  if (!/^(--text-|--bg-)/.test(tok)) continue;
  check(`dark theme redefines ${tok}`, DARK[tok] !== LIGHT[tok],
    `both themes resolve to ${JSON.stringify(LIGHT[tok])}`);
}

// ---- 0b. token-name collision guard ------------------------------------
// A custom property is one value per element: defining `--text-base` twice
// (once as a colour, once as `14px`) makes every `color: var(--text-base)`
// invalid at computed-value time, i.e. a *silent* loss of theme colour. The
// audit therefore refuses any token that is consumed in a colour position
// while resolving to a non-colour in either theme.
const COLOR_PROPS = ['color', 'background', 'background-color', 'border-color',
  'outline', 'outline-color', 'fill', 'stroke'];
for (const theme of ['light', 'dark']) {
  const tokens = themeTokens(theme);
  const checked = new Set();
  for (const r of RULES) {
    if (theme === 'light' && r.dark) continue;
    for (const prop of COLOR_PROPS) {
      const value = r.decls[prop];
      if (!value || !/var\(/.test(value)) continue;
      for (const m of value.matchAll(/var\(\s*(--[\w-]+)/gi)) {
        const name = m[1];
        if (checked.has(name + '|' + theme)) continue;
        checked.add(name + '|' + theme);
        if (!/^(--text|--bg|--primary|--secondary|--success|--warning|--danger|--info|--border|--surface)/.test(name)) continue;
        check(`[${theme}] colour-position token ${name} resolves to a colour`,
          !!resolveColor(tokens[name], tokens),
          `value=${JSON.stringify(tokens[name])} (a non-colour here silently voids every "${prop}: var(${name})")`);
      }
    }
  }
}

// ---- 1. token pairs ------------------------------------------------------
for (const theme of ['light', 'dark']) {
  for (const [fgTok, bgTok, min] of TOKEN_PAIRS) {
    ratio(`${fgTok} on ${bgTok}`, { token: fgTok }, { token: bgTok }, min, theme);
  }

  // ---- 2. components ----------------------------------------------------
  for (const c of COMPONENT_PAIRS) {
    const bgSrc = c.bgToken ? { token: c.bgToken } : { sel: c.bgSel, prop: ['background', 'background-color'] };
    ratio(c.name, { sel: c.sel, prop: 'color' }, bgSrc, TEXT_MIN, theme);
  }

  // ---- 3. non-text ------------------------------------------------------
  for (const c of NON_TEXT_PAIRS) {
    const bgSrc = c.bgToken ? { token: c.bgToken } : { sel: c.bgSel, prop: ['background', 'background-color'] };
    ratio(c.name, { sel: c.sel, prop: ['outline-color', 'outline', 'background'] },
      bgSrc, c.min || NON_TEXT_MIN, theme);
  }
}

// ---- 4. structural a11y assertions ---------------------------------------
// These live here because they are "the palette must stay usable" questions,
// and the contrast harness is the file that reads the stylesheet.
const cssOf = (rel) => readCss(rel);
const styleCss = cssOf('css/style.css');

check('focus ring is not the invisible --primary-soft wash',
  !/outline:[^;]*var\(--primary-soft\)/.test(styleCss),
  'a 3px --primary-soft ring measures ~1.05:1 on a white card (WCAG 2.4.7 failure)');
check('inputs keep a :focus-visible indicator after the outline:none reset',
  /input:focus[^{]*\{[^}]*outline:\s*none/.test(styleCss)
    && /input:focus-visible[^{]*\{[^}]*outline:\s*\d+px\s+solid/.test(styleCss));
check('range slider gets a focus ring despite appearance:none',
  /\.range-row input\[type=range\]:focus-visible[^{]*\{[^}]*outline:\s*\d+px\s+solid/.test(styleCss));
check('outline-offset separates the ring from the control',
  /outline-offset:\s*\d+px/.test(styleCss));

if (fs.existsSync(path.join(__dirname, 'css', 'app-ux.css'))) {
  const ux = cssOf('css/app-ux.css');
  check('sticky thead is opaque (no rows ghosting through)',
    /table\.data-table thead th[^{]*\{[^}]*background:\s*var\(--bg-lighter\)/.test(ux));
  check('sticky thead is inside a scrollable, height-capped wrap',
    /\.table-wrap[^{]*\{[^}]*max-height/.test(ux) && /\.table-wrap[^{]*\{[^}]*overflow-y:\s*auto/.test(ux));
  check('sticky thead carries a z-index above the body cells',
    /table\.data-table thead th[^{]*\{[^}]*z-index:\s*[2-9]/.test(ux));
  check('dark sticky thead stays opaque',
    /:root\[data-theme="dark"\][^{]*table\.data-table thead th[^{]*\{[^}]*background:\s*#[0-9a-f]{6}/i.test(ux));
} else {
  check('css/app-ux.css exists (linked from index.html)', false,
    'the a11y layer vanished from disk — sticky header/progress/dialog styles are gone');
}

const indexHtml = fs.existsSync(path.join(__dirname, 'index.html'))
  ? fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8') : '';
if (indexHtml) {
  check('app-ux.css is linked after style.css',
    indexHtml.indexOf('css/app-ux.css') > indexHtml.indexOf('css/style.css'));
  check('language switch is a radiogroup with aria-checked options',
    /id="lang-switch"[^>]*role="radiogroup"/.test(indexHtml)
      && /role="radio"[^>]*aria-checked=/.test(indexHtml)
      && !/id="lang-switch"[^>]*role="tablist"/.test(indexHtml));
  check('progress slot is a determinate progressbar',
    /id="run-progress"[^>]*role="progressbar"[^>]*aria-valuemin/.test(indexHtml)
      && /aria-valuenow/.test(indexHtml));
  check('transient strings come from the i18n catalog',
    /data-i18n-label="a11y\.languageLabel"/.test(indexHtml)
      && /data-i18n-title="theme\.system"/.test(indexHtml));
  // Item 6: the banner slot is the ONE live region (index.html declares it,
  // app.js only ever moves its politeness). A nested role="alert" inside an
  // aria-live parent made screen readers read the same failure twice.
  check('alert slot is a live region in the markup',
    /id="alert-slot"[^>]*role="status"[^>]*aria-live="polite"/.test(indexHtml));
}

// ---- 5. app.js behaviour contracts (domain U) ----------------------------
// Source-level on purpose: the contrast harness has no DOM, and these are
// "the code must keep doing X" guards for changes that have no colour story.
const appJs = fs.existsSync(path.join(__dirname, 'js', 'app.js'))
  ? fs.readFileSync(path.join(__dirname, 'js', 'app.js'), 'utf8') : '';
if (appJs) {
  check('banner element carries no live role of its own',
    /_alertSlotLive\(/.test(appJs) && !/div\.setAttribute\('role'/.test(appJs),
    'showAlert must move role/aria-live onto #alert-slot, not onto the banner');
  check('clearAlert restores the polite announcement mode',
    /function clearAlert[\s\S]{0,200}_alertSlotLive\('status'\)/.test(appJs));
  check('extraction result gets keyboard focus (tabindex -1 + focus)',
    /setAttribute\('tabindex', '-1'\)/.test(appJs)
      && /focus\(\{ preventScroll: true \}\)/.test(appJs));
  check('language switch re-translates instead of rebuilding the result',
    /rcaRetranslateResult\(\)/.test(appJs)
      && /if \(state\.result\) rcaRetranslateResult\(\);/.test(appJs));
  check('re-translation skips the panel while a cell is being edited',
    /isContentEditable/.test(appJs)
      && /rec\.node\.textContent !== rec\.text/.test(appJs));
  check('danger banner offers copy-details + retry (textContent built)',
    /\.textContent = t\('err\.copyDetails'\)/.test(appJs)
      && /\.textContent = t\('err\.retry'\)/.test(appJs)
      && !/innerHTML[^;]*err\.copy/.test(appJs));
  check('force-rerun shows after a FAILED first run too (image is enough)',
    /state\.result \|\| state\.dataUrl/.test(appJs));
  check('run progress bar is driven by the {done}/{total} signals',
    /_rcaSetRunProgress\(prm\.done, prm\.total\)/.test(appJs)
      && /setAttribute\('aria-valuenow'/.test(appJs));
  check('language radiogroup keeps a single tab stop (roving tabindex)',
    /setAttribute\('tabindex', tabbable \? '0' : '-1'\)/.test(appJs));
}

// ---- 6. i18n completeness of this round's copy ---------------------------
// Three-way parity itself is locked by tests_frontend.js; what is checked
// here is (a) that every string the markup/code asks for actually exists in
// all three browser locales AND in the Python (GUI) catalog, and (b) that the
// FE-BORROW additions did not land in js/i18n.js only.
const catalogs = (function () {
  const out = { js: {}, py: {} };
  if (!fs.existsSync(path.join(__dirname, 'js', 'i18n.js'))) return out;
  const src = fs.readFileSync(path.join(__dirname, 'js', 'i18n.js'), 'utf8');
  const cut = (a, b) => {
    const i = src.indexOf(a);
    if (i === -1) return '';
    const j = b ? src.indexOf(b, i) : src.indexOf('\n};', i);
    return src.slice(i, j === -1 ? undefined : j);
  };
  out.js.zh = cut('const RCA_I18N = {', '\nRCA_I18N.en');
  out.js.en = cut('RCA_I18N.en = {', '\nRCA_I18N.ja');
  out.js.ja = cut('RCA_I18N.ja = {', null);
  if (fs.existsSync(path.join(__dirname, 'rca_core', 'i18n.py'))) {
    const py = fs.readFileSync(path.join(__dirname, 'rca_core', 'i18n.py'), 'utf8');
    for (const lang of ['zh', 'en', 'ja']) {
      const a = py.indexOf('TRANSLATIONS["' + lang + '"] = {');
      const b = py.indexOf('\n}', a);
      out.py[lang] = a === -1 ? '' : py.slice(a, b === -1 ? undefined : b);
    }
  }
  return out;
})();

// Every key the markup (data-i18n*) and app.js (t('…')) asks for.
const wanted = new Set();
for (const m of indexHtml.matchAll(/data-i18n(?:-[a-z]+)?="([A-Za-z0-9_.]+)"/g)) {
  wanted.add(m[1]);
}
for (const m of appJs.matchAll(/\bt\(\s*'([A-Za-z0-9_.]+)'/g)) wanted.add(m[1]);
check('markup + app.js ask for at least 40 i18n keys', wanted.size >= 40,
  'key harvest collapsed — the regexes above drifted');
for (const key of [...wanted].sort()) {
  const inJs = ['zh', 'en', 'ja'].map((l) => (catalogs.js[l] || '').includes("'" + key + "':"));
  check('i18n key resolves in the browser catalog: ' + key,
    inJs.every(Boolean), 'missing in ' + ['zh', 'en', 'ja'].filter((_, i) => !inJs[i]).join(','));
}

// The keys this round added (FE-BORROW / domain U) must ALSO exist in the
// Python catalog the Tkinter/Fluent GUIs read, in all three languages.
const ROUND_KEYS = [
  'a11y.skipToContent', 'a11y.themeLabel', 'a11y.languageLabel',
  'theme.system', 'theme.light', 'theme.dark', 'loading.progress',
  'err.copyDetails', 'err.retry', 'err.copyFailed',
  'confirm.forceRerun', 'confirm.reset',
];
for (const key of ROUND_KEYS) {
  const hit = ['zh', 'en', 'ja'].map((l) => (catalogs.py[l] || '').includes('"' + key + '":'));
  check('round-2026-09-20 copy mirrored in rca_core/i18n.py: ' + key,
    hit.every(Boolean), 'missing in ' + ['zh', 'en', 'ja'].filter((_, i) => !hit[i]).join(','));
}

console.log(`\n--- contrast: ${pass} passed, ${fail} failed ---`);
process.exit(fail ? 1 : 0);
