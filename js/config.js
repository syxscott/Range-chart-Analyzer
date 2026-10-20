// config.js - default API settings and localStorage / sessionStorage helpers
'use strict';

// F-22 FIX: apiKey is stored in sessionStorage only — it is NEVER persisted
// to localStorage.  sessionStorage is cleared when the browser tab closes,
// so the key never survives across sessions on shared computers.
// On shared computers: do NOT check "remember" (it controls other settings
// persistence only); close the tab when done; prefer a private/incognito
// window so other tabs are isolated too.

const RCA_CONFIG = {
  defaultEndpoint: 'https://api.minimaxi.com/anthropic',
  defaultModel: 'MiniMax-M3',
  // REVIEW-2026-10-15 (measured): 4000 silently truncated ordinary figures.
  // A 22-taxon x 18-locality occurrence matrix needs 4400-5400 output tokens
  // as one JSON record per filled cell; at 4000 the last rows were simply cut
  // off. Must stay equal to DEFAULT_MAX_TOKENS in rca_core/extractor.py.
  defaultMaxTokens: 16000,
  // REVIEW-2026-10-19: reasoning effort levels (MiniMax M3.1 family). The
  // wire shape is `output_config: {effort: ...}` on the Anthropic-compatible
  // endpoint -- NOT `thinking: {budget_tokens: N}`, which is not in M3.1's
  // documented vocabulary. Must stay equal to REASONING_EFFORT_LEVELS in
  // rca_core/llm.py.
  reasoningEffortLevels: ['low', 'medium', 'high', 'xhigh', 'max'],
  defaultEffort: '',
  // M5: explicit bounds for max_tokens clamp — defends against typos.
  // REVIEW-2026-10-19: the ceiling is the MODEL's real limit, not a product
  // guess. MiniMax-M3.1-Flash answers a larger value with
  //   "invalid params, model[MiniMax-M3.1-Flash] does not support max tokens
  //    > 524288 (2013)"
  // so 524288 is the largest value that is actually accepted, and 1000000 is
  // a 400. The old 32000 was a product-side cap that had nothing to do with
  // the model -- and it was why a reasoning model could not be given room to
  // think: the thinking tokens are billed against max_tokens, and at 32000
  // the "max" effort spent the whole budget thinking and returned an EMPTY
  // body (HTTP 200, stop_reason=max_tokens, zero characters of answer).
  // Must stay equal to MAX_MAX_TOKENS in rca_core/extractor.py.
  minMaxTokens: 1,
  maxMaxTokens: 524288,
  requestTimeoutMs: 120000,
  // Long edge cap for client-side image downscale (px). base64 grows ~33%,
  // so we downscale very large scans before upload to avoid huge request
  // bodies. Kept high (4000) so small italic species names stay legible for
  // OCR; a lower cap blurs dense chart text and causes misreads.
  maxImageEdge: 4000,
  // Max upload file size (bytes). handleFile() rejects anything larger before
  // the base64 read so a dragged-in huge TIFF can't exhaust browser memory.
  maxFileBytes: 20 * 1024 * 1024,
};

// Clamp a max_tokens value to the allowed range. Falls back to the default
// when the input is non-numeric or out of range.
function rcaClampMaxTokens(v) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return RCA_CONFIG.defaultMaxTokens;
  return Math.max(RCA_CONFIG.minMaxTokens, Math.min(n, RCA_CONFIG.maxMaxTokens));
}

// Namespaced localStorage keys. Phase L fix: persist the four
// chart-context settings that the Python GUI persists to JSON
// (chart_lang, chart_type, enhance, runs) so the JS and Python
// experiences don't silently diverge when the user switches
// between them. Key names mirror the Python GUI fields verbatim
// (NOT prefixed with `rca.`) so a future migration to a single
// shared settings file is straightforward.
const RCA_STORE = {
  apiKey: 'rca.apiKey',
  endpoint: 'rca.endpoint',
  model: 'rca.model',
  // REVIEW-2026-09-20: this key used to be spelled as a bare
  // `'rca.maxTokens'` literal at both call sites in app.js (load + save),
  // which is how a renamed/removed store key slips past a grep for
  // `RCA_STORE.`. Every namespaced key now goes through this table.
  maxTokens: 'rca.maxTokens',
  // REVIEW-2026-10-19: reasoning effort for the MiniMax M3.1 family.
  // M3.1 ALWAYS thinks and its effort level DEFAULTS TO "max"; thinking tokens
  // consume max_tokens, so on a dense figure the thinking alone outgrew the
  // budget and the call never answered (measured: read timeout at 90 s, 2 of
  // 2 runs, while the same call on M3 -- thinking off by default -- answered
  // in 6.5-13.1 s). "low" answered the same call in 28.3 s.
  // Empty string means "send nothing", which is right for every other model.
  effort: 'rca.effort',
  proxy: 'rca.proxy',
  mode: 'rca.mode',
  maxEdge: 'rca.maxEdge',
  runs: 'rca.runs',
  lang: 'rca.lang',
  rememberKey: 'rca.rememberKey',
  // Phase L: parity with Python GUI ~/.range_chart_analyzer.json
  chartLang: 'chart_lang',
  chartType: 'chart_type',
  enhance: 'enhance',
};

// F-22 FIX: apiKey ALWAYS uses sessionStorage — never localStorage.
// All other keys continue to use localStorage.
function _storageFor(key) {
  return key === RCA_STORE.apiKey ? sessionStorage : localStorage;
}

function rcaStoreGet(key, fallback) {
  try {
    const store = _storageFor(key);
    const v = store.getItem(key);
    return v === null ? (fallback ?? '') : v;
  } catch (_e) {
    return fallback ?? '';
  }
}

function rcaStoreSet(key, value) {
  // FR3: return true when the write actually landed, false otherwise
  // (quota exceeded, security-restricted context, etc.). Callers can
  // surface a real error instead of an optimistic toast that lies.
  try {
    const store = _storageFor(key);
    if (value === null || value === undefined || value === '') {
      store.removeItem(key);
    } else {
      store.setItem(key, String(value));
    }
    return true;
  } catch (_e) {
    return false;
  }
}
