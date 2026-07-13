// config.js - default API settings and localStorage helpers
'use strict';

const RCA_CONFIG = {
  defaultEndpoint: 'https://api.minimaxi.com/anthropic',
  defaultModel: 'MiniMax-M3',
  defaultMaxTokens: 4000,
  // M5: explicit bounds for max_tokens clamp — defends against typos.
  minMaxTokens: 1,
  maxMaxTokens: 32000,
  requestTimeoutMs: 120000,
  // Long edge cap for client-side image downscale (px). base64 grows ~33%,
  // so we downscale very large scans before upload to avoid huge request
  // bodies. Kept high (4000) so small italic species names stay legible for
  // OCR; a lower cap blurs dense chart text and causes misreads.
  maxImageEdge: 4000,
};

// Clamp a max_tokens value to the allowed range. Falls back to the default
// when the input is non-numeric or out of range.
function rcaClampMaxTokens(v) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return RCA_CONFIG.defaultMaxTokens;
  return Math.max(RCA_CONFIG.minMaxTokens, Math.min(n, RCA_CONFIG.maxMaxTokens));
}

// Namespaced localStorage keys.
const RCA_STORE = {
  apiKey: 'rca.apiKey',
  endpoint: 'rca.endpoint',
  model: 'rca.model',
  proxy: 'rca.proxy',
  mode: 'rca.mode',
  maxEdge: 'rca.maxEdge',
  runs: 'rca.runs',
  lang: 'rca.lang',
  rememberKey: 'rca.rememberKey',
};

function rcaStoreGet(key, fallback) {
  try {
    const v = localStorage.getItem(key);
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
    if (value === null || value === undefined || value === '') {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, String(value));
    }
    return true;
  } catch (_e) {
    return false;
  }
}
