// tests_contrast.js - WCAG AA contrast checker for the RCA design tokens.
//
// Computes the relative luminance of every (text, background) pair declared
// in the PAIRS array and asserts the contrast ratio meets WCAG AA:
//   - >= 4.5 for normal text (< 18px or < 14px bold)
//   - >= 3.0 for large text (>= 18px or >= 14px bold)  [not enforced here;
//     all our pairs are normal-text so we use 4.5 uniformly]
//
// Run:  node tests_contrast.js
'use strict';

// --- color math (no deps) ---

function hexToRgb(hex) {
  const h = hex.replace('#', '');
  const n = parseInt(h.length === 3
    ? h.split('').map((c) => c + c).join('')
    : h, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

function relativeLuminance(hex) {
  const { r, g, b } = hexToRgb(hex);
  const lin = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrastRatio(hexA, hexB) {
  const la = relativeLuminance(hexA);
  const lb = relativeLuminance(hexB);
  const lighter = Math.max(la, lb);
  const darker = Math.min(la, lb);
  return (lighter + 0.05) / (darker + 0.05);
}

// --- token maps ---

// Light theme tokens (from css/style.css :root).
const LIGHT = {
  '--text-dark':    '#0c0a09',
  '--text-base':    '#1c1917',
  '--text-muted':   '#57534e',
  '--text-light':   '#78716c',
  '--bg-page':      '#fafaf9',
  '--bg-white':     '#ffffff',
  '--bg-light':     '#f7f6f5',
  '--bg-lighter':   '#efedeb',
  '--border-color': '#e7e5e4',
  '--primary-color':'#2563eb',
  '--primary-soft': '#eff6ff',
  '--success-color':'#10b981',
  '--success-soft': '#d1fae5',
  '--warning-color':'#f59e0b',
  '--warning-soft': '#fef3c7',
  '--danger-color': '#ef4444',
  '--danger-soft':  '#fee2e2',
  '--info-color':   '#6366f1',
  '--info-soft':    '#e0e7ff',
};

// Dark theme tokens (from the [data-theme="dark"] + prefers-color-scheme block).
const DARK = {
  '--text-dark':    '#f5f5f4',
  '--text-base':    '#e7e5e4',
  '--text-muted':   '#a8a29e',
  '--text-light':   '#a8a29e',
  '--bg-page':      '#0c0a09',
  '--bg-white':     '#221f1d',
  '--bg-light':     '#161312',
  '--bg-lighter':   '#1f1c1a',
  '--border-color': '#292524',
  '--border-strong':'#3f3a36',
  '--primary-color':'#60a5fa',
  '--primary-soft': '#1e3a8a',
  '--success-color':'#10b981',
  '--success-soft': '#064e3b',
  '--warning-color':'#f59e0b',
  '--warning-soft': '#78350f',
  '--danger-color': '#ef4444',
  '--danger-soft':  '#7f1d1d',
  '--info-color':   '#6366f1',
  '--info-soft':    '#312e81',
};

// (text_token, bg_token) pairs for LIGHT theme.
// Alert/pill text uses the hardcoded light-mode colors from CSS.
const LIGHT_PAIRS = [
  ['--text-dark',  '--bg-page'],
  ['--text-dark',  '--bg-white'],
  ['--text-base',  '--bg-page'],
  ['--text-base',  '--bg-white'],
  ['--text-base',  '--bg-light'],
  ['--text-base',  '--bg-lighter'],
  ['--text-muted', '--bg-page'],
  ['--text-muted', '--bg-white'],
  ['--text-muted', '--bg-light'],
  ['--text-light', '--bg-white'],
  ['#ffffff', '--primary-color'],
  ['--primary-color', '--bg-white'],
  ['--primary-color', '--bg-page'],
  ['#065f46', '--success-soft'],
  ['#92400e', '--warning-soft'],
  ['#991b1b', '--danger-soft'],
  ['#3730a3', '--info-soft'],
];

// (text_token, bg_token) pairs for DARK theme.
// Alert/pill text uses the dark-mode override colors from CSS.
const DARK_PAIRS = [
  ['--text-dark',  '--bg-page'],
  ['--text-dark',  '--bg-white'],
  ['--text-base',  '--bg-page'],
  ['--text-base',  '--bg-white'],
  ['--text-base',  '--bg-lighter'],
  ['--text-muted', '--bg-white'],
  ['--text-muted', '--bg-page'],
  ['--text-light', '--bg-white'],
  /* Dark-mode primary button uses dark text (#0c0a09) on lighter blue. */
  ['#0c0a09', '--primary-color'],
  ['--primary-color', '--bg-white'],
  ['--primary-color', '--bg-page'],
  ['#6ee7b7', '--success-soft'],
  ['#fcd34d', '--warning-soft'],
  ['#fca5a5', '--danger-soft'],
  ['#a5b4fc', '--info-soft'],
];

let pass = 0, fail = 0;

function checkPair(themeName, tokens, textTok, bgTok) {
  const textHex = tokens[textTok] || textTok;  // allow raw hex in PAIRS
  const bgHex = tokens[bgTok] || bgTok;
  const ratio = contrastRatio(textHex, bgHex);
  const ok = ratio >= 4.5;
  if (ok) {
    pass++;
  } else {
    fail++;
    console.log(`FAIL [${themeName}] ${textTok} on ${bgTok}: ratio=${ratio.toFixed(2)} (need >= 4.5)`);
  }
}

function runTheme(themeName, tokens, pairs) {
  for (const [textTok, bgTok] of pairs) {
    // Skip pairs where the token doesn't exist in this theme.
    if (!tokens[textTok] && textTok.startsWith('--')) continue;
    if (!tokens[bgTok] && bgTok.startsWith('--')) continue;
    checkPair(themeName, tokens, textTok, bgTok);
  }
}

runTheme('light', LIGHT, LIGHT_PAIRS);
runTheme('dark', DARK, DARK_PAIRS);

console.log(`\n--- contrast: ${pass} passed, ${fail} failed ---`);
process.exit(fail ? 1 : 0);
