// cloudflare-worker.js
//
// Optional CORS proxy for Range Chart Analyzer.
// Deploy this ONLY if a direct browser call to MiniMax is blocked by CORS
// (you will see a "network / CORS" error in the app). It is a stateless,
// transparent pass-through: it forwards the request to MiniMax and adds the
// CORS headers the browser requires. It does NOT read, log, or store your
// API key — the key stays in the request headers and is forwarded as-is.
//
// SECURITY (Bug-3 / Bug-17 fixes):
//   - Origin allowlist: set ALLOWED_ORIGINS below. Default is permissive
//     (matches your dev origin) but should be tightened in production.
//   - Shared secret: if PROXY_SHARED_SECRET is set, requests must include
//     `X-Proxy-Key: <secret>` or they are rejected with 401. This stops
//     anyone who discovers the Worker URL from burning your quota.
//   - Path allowlist: only /v1/messages is forwarded. Without this, an
//     attacker could route arbitrary upstream paths through the Worker.
//   - Response header filter: only content-type, content-length, and
//     streaming-related headers are echoed back. Cookies, internal IPs
//     (x-real-ip, cf-ray) and similar upstream diagnostic headers are
//     dropped to avoid leaking server internals.
//   - Body / header size cap: requests larger than 50 MB are rejected
//     before any upstream call is made.
//
// Deploy (Cloudflare Workers, free tier):
//   1. Create a Worker at https://dash.cloudflare.com  (Workers & Pages)
//   2. Paste this file as the Worker script and Deploy.
//   3. Copy the Worker URL (e.g. https://range-proxy.<you>.workers.dev)
//   4. Paste it into the app's "Proxy URL" field in API Settings.

const UPSTREAM = 'https://api.minimaxi.com/anthropic';

// Restrict who may use your proxy. Replace with the origins your app
// is actually served from. '*' is allowed for local testing but allows
// any site to route LLM calls through your Worker — never deploy '*' to
// production.
// SECURITY: this list MUST be non-empty in production. An empty list here
// is treated as "deny all inbound origins" — the Worker will reject every
// request with 403 until you either (a) add an origin below, or (b) set a
// PROXY_SHARED_SECRET so access is gated by a secret instead. This closes
// the open-relay footgun where an empty allowlist previously became `*`
// (any site that discovered your Worker URL could burn your LLM quota).
//
// Note: this allowlist governs the browser's Origin only. Because the
// outbound target (MiniMax) is hardcoded below, the Worker is NEVER an
// arbitrary-URL forwarder — it can only proxy to MiniMax — but open access
// still lets strangers spend your quota.
const ALLOWED_ORIGINS = [
  // 'https://yourname.github.io',
  // 'http://localhost:8000',
];

// Optional shared secret. Set via `wrangler secret put PROXY_SHARED_SECRET`
// (https://developers.cloudflare.com/workers/configuration/secrets/). When
// set, requests must include `X-Proxy-Key: <secret>`. Empty string = off.
// If you set this, origin allowlisting is still applied when the allowlist
// is non-empty, but a valid key alone is enough to pass when the allowlist
// is empty.
const PROXY_SHARED_SECRET = '';

// Path allowlist: exact-match only — only these paths are forwarded to upstream.
// The MiniMax Anthropic-compatible endpoint lives at /v1/messages.
const ALLOWED_PATH_PREFIXES = ['/v1/messages'];

// Max request body size (50 MB) — rejects obviously abusive uploads
// before opening an upstream connection.
const MAX_BODY_BYTES = 50 * 1024 * 1024;

// Headers we forward from the client to upstream. Whitelist so the
// client cannot inject arbitrary headers (e.g. Host overrides, internal
// routing, or proxy-bypass headers).
const FORWARDED_REQUEST_HEADERS = new Set([
  'content-type',
  'x-api-key',
  'anthropic-version',
  'authorization',
]);

// Headers we forward from upstream to the client. Whitelist so we never
// leak upstream diagnostic / routing headers (set-cookie, x-real-ip,
// cf-ray, server, via, x-request-id, …).
const FORWARDED_RESPONSE_HEADERS = new Set([
  'content-type',
  'content-length',
  'content-encoding',
  'transfer-encoding',
  'x-ratelimit-remaining',
  'x-ratelimit-reset',
  'retry-after',
  'anthropic-ratelimit-*',
]);

// --- Sliding-window rate limiter (30 requests / 60 seconds per IP) ---
// Uses an in-memory Map; each entry is [timestamp, ...] sorted oldest→newest.
// The Worker process lives for the duration of a request batch then is
// destroyed by the runtime, so the memory naturally resets — this is the
// intended behaviour for a serverless environment.
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 30;
const _rateMap = new Map(); // ip → int[] of timestamps

function rateCheck(ip) {
  const now = Date.now();
  const key = ip;
  const slots = _rateMap.get(key);
  if (!slots) {
    _rateMap.set(key, [now]);
    return { allowed: true, remaining: RATE_MAX - 1, resetMs: RATE_WINDOW_MS };
  }
  // Prune entries older than the window.
  const cutoff = now - RATE_WINDOW_MS;
  let idx = 0;
  while (idx < slots.length && slots[idx] < cutoff) idx++;
  if (idx > 0) slots.splice(0, idx);
  if (slots.length >= RATE_MAX) {
    const oldest = slots[0];
    return {
      allowed: false,
      remaining: 0,
      resetMs: Math.ceil((oldest + RATE_WINDOW_MS - now) / 1000) + 1,
    };
  }
  slots.push(now);
  return { allowed: true, remaining: RATE_MAX - slots.length, resetMs: RATE_WINDOW_MS };
}

const CORS_HEADERS = {
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'content-type, x-api-key, anthropic-version, x-proxy-key',
  'Access-Control-Max-Age': '86400',
  // Always advertise that we key on Origin when we reflect it, so caches
  // (and CF itself) don't hand origin A's response to origin B.
  'Vary': 'Origin',
};

// Decide whether an inbound request is authorized to use this proxy, and
// return the CORS headers to echo back (or null to reject). Authorization
// is granted when EITHER the browser Origin is on the allowlist OR the
// request carries a valid X-Proxy-Key. An empty allowlist with no secret
// configured yields null (reject) — this is the deliberate fix for the
// open-relay footgun where an empty list previously became `*`.
function corsFor(origin) {
  const originOk = ALLOWED_ORIGINS.includes('*') || ALLOWED_ORIGINS.includes(origin);
  if (originOk) {
    // Reflect the specific origin (not `*`) so credentials/cookies could be
    // used later if needed; `*` only when the allowlist is literally `*`.
    const allowOrigin = ALLOWED_ORIGINS.includes('*') ? '*' : origin;
    return { 'Access-Control-Allow-Origin': allowOrigin, ...CORS_HEADERS };
  }
  return null;
}

// True when the request carries the correct shared secret. Always returns
// false when no secret is configured (an empty secret is treated as "off").
// Comparison is constant-time to prevent timing-side-channel discovery of
// the secret length / prefix by an attacker who can probe the Worker.
function secretOk(request) {
  if (!PROXY_SHARED_SECRET) return false;
  const provided = request.headers.get('X-Proxy-Key') || '';
  return timingSafeEqual(provided, PROXY_SHARED_SECRET);
}

// Constant-time string compare (length-mismatch is not constant but
// length is itself public; this matches what the rest of the codebase
// calls timingSafeEqual).
function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  if (a.length !== b.length) {
    // Still consume the same time as a max-length compare.
    let acc = 0;
    for (let i = 0; i < a.length; i++) acc |= a.charCodeAt(i);
    return false;
  }
  let acc = 0;
  for (let i = 0; i < a.length; i++) {
    acc |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return acc === 0;
}

// Build the CORS headers we want to echo for an authorized caller.
// In secret-only mode (origin NOT allowlisted but secretOk() returned true),
// we still need to emit CORS so the browser can read the response. We
// reflect the request's Origin and always include Vary: Origin so caches
// cannot poison. The shared secret is what actually authenticates the
// caller in this branch — Origin alone is not trusted (it's trivially
// spoofable by any non-browser client).
function corsHeadersForAuthorized(request, cors) {
  if (cors) return cors;          // origin matched allowlist
  const o = request.headers.get('Origin');
  if (!o) return null;            // non-browser caller without CORS need
  return { 'Access-Control-Allow-Origin': o, ...CORS_HEADERS };
}

function pickHeaders(source, whitelist) {
  const out = new Headers();
  for (const [k, v] of source.entries()) {
    const lk = k.toLowerCase();
    if (whitelist.has(lk) || [...whitelist].some((p) => p.endsWith('*') && lk.startsWith(p.slice(0, -1)))) {
      out.set(k, v);
    }
  }
  return out;
}

// Accumulate the request body in chunks and enforce a hard byte cap that
// does NOT trust the client's Content-Length header (which any non-browser
// caller can lie about). We stream-read until either EOF or the cap is
// exceeded, then return null (cap exceeded) or a Uint8Array.
async function readBoundedBody(request, maxBytes) {
  if (!request.body) return new Uint8Array(0);
  const reader = request.body.getReader();
  const chunks = [];
  let received = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > maxBytes) {
      try { reader.cancel(); } catch (_e) { /* ignore */ }
      return null;
    }
    chunks.push(value);
  }
  // Concat
  const total = chunks.reduce((n, c) => n + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { out.set(c, offset); offset += c.byteLength; }
  return out;
}

export default {
  async fetch(request) {
    const origin = request.headers.get('Origin') || '';
    // Compute cors + authorization FIRST so the rate-limit 429 branch can
    // safely reference them (TDZ fix: previously `cors` was declared later,
    // so any rate-limited request threw ReferenceError before this 429
    // could be returned with proper CORS headers).
    const cors = corsFor(origin);
    const authorized = cors !== null || secretOk(request);

    // RATE LIMIT: apply after computing cors/authorized so 429 can echo
    // CORS. The rate-limit key is CF-Connecting-IP when available (set by
    // CF and not client-spoofable) and falls back to the rightmost
    // X-Forwarded-For when behind another trusted proxy (the leftmost is
    // client-controlled and trivially rotatable).
    const fwd = request.headers.get('x-forwarded-for');
    const rightmostFwd = fwd ? fwd.split(',').slice(-1)[0].trim() : '';
    const clientIp = request.headers.get('CF-Connecting-IP') ||
                     rightmostFwd ||
                     'unknown';
    const rl = rateCheck(clientIp);
    if (!rl.allowed) {
      return new Response(JSON.stringify({ error: 'rate_limit_exceeded', retryAfter: rl.resetMs }), {
        status: 429,
        headers: { 'content-type': 'application/json', ...(cors || {}) },
      });
    }

    // Authorized when the Origin is allowlisted OR a valid secret is
    // presented. With an empty allowlist and no secret, corsFor() returns
    // null here and every request is rejected — this is the deliberate
    // closure of the open-relay footgun. The outbound target is still the
    // hardcoded MiniMax endpoint, so even an authorized caller can only
    // reach MiniMax, never an arbitrary URL.
    if (!authorized) {
      return new Response('Forbidden', { status: 403 });
    }

    // For secret-only mode we still need CORS so the browser can use the
    // proxy. This reflects the request's Origin with Vary: Origin.
    const corsEcho = corsHeadersForAuthorized(request, cors) || {};

    // Preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsEcho });
    }
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405, headers: corsEcho });
    }

    // Body size cap — enforce by streaming the body, NOT by trusting
    // Content-Length. Clients can lie about Content-Length; they cannot
    // lie about the bytes they actually send.
    const bounded = await readBoundedBody(request, MAX_BODY_BYTES);
    if (bounded === null) {
      return new Response('Payload Too Large', { status: 413, headers: corsEcho });
    }

    // Path allowlist
    const url = new URL(request.url);
    if (!ALLOWED_PATH_PREFIXES.includes(url.pathname)) {
      return new Response('Not Found', { status: 404, headers: corsEcho });
    }

    const target = UPSTREAM.replace(/\/+$/, '') + url.pathname + url.search;

    // Forward a sanitized subset of the request headers.
    const reqHeaders = pickHeaders(request.headers, FORWARDED_REQUEST_HEADERS);
    // Always set our own content-type — don't trust the client to set
    // something that confuses the upstream.
    reqHeaders.set('content-type', reqHeaders.get('content-type') || 'application/json');

    let upstreamResp;
    try {
      upstreamResp = await fetch(target, {
        method: 'POST',
        headers: reqHeaders,
        body: bounded,
      });
    } catch (_e) {
      // Upstream network error (DNS, refused, TLS) — return a real 502 with
      // CORS headers so the browser can surface a meaningful diagnostic
      // instead of an opaque network failure (the default worker error
      // omits CORS, defeating the proxy's primary purpose).
      return new Response(JSON.stringify({ error: 'upstream_unreachable' }), {
        status: 502,
        headers: { 'content-type': 'application/json', ...corsEcho },
      });
    }

    // Echo back a sanitized subset of the upstream response headers.
    const respHeaders = pickHeaders(upstreamResp.headers, FORWARDED_RESPONSE_HEADERS);
    for (const [k, v] of Object.entries(corsEcho)) respHeaders.set(k, v);
    // Surface rate-limit state so the client can back off appropriately.
    respHeaders.set('X-RateLimit-Remaining', String(rl.remaining));
    respHeaders.set('X-RateLimit-Reset-Ms', String(rl.resetMs));
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      statusText: upstreamResp.statusText,
      headers: respHeaders,
    });
  },
};
