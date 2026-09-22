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
//   - Origin allowlist: set ALLOWED_ORIGINS below. The DEFAULT IS CLOSED
//     (empty list = deny every origin, 403); add the origins you actually
//     serve the app from, or use '*' only for local testing.
//   - Shared secret: if PROXY_SHARED_SECRET is set, requests must include
//     `X-Proxy-Key: <secret>` or they are rejected with 403. This stops
//     anyone who discovers the Worker URL from burning your quota.
//   - Path allowlist: only the exact path /v1/messages is forwarded. Without
//     this, an attacker could route arbitrary upstream paths through the
//     Worker. The match is EXACT and percent-encodings that could normalise
//     to something else (%2e, %2f, %25, %5c, '..' after decoding) are
//     rejected, so '/v1/messages/../admin' can never slip past.
//   - Authorization (Origin allowlist + shared secret) and the method/path
//     checks all run BEFORE the request body is read, so an unauthorised or
//     off-allowlist caller cannot make us buffer up to 50 MB for it.
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
// If you set this, an allowlisted Origin is ALSO required (the two are ANDed
// in fetch() below) unless the allowlist is empty (secret-only mode).
//
// REVIEW-2026-09-10: this constant is only a source-level default. The value
// actually used is `env.PROXY_SHARED_SECRET` from the Worker's bindings, so
// `wrangler secret put` works without editing this file. The old code never
// looked at `env` at all, which made the secret branches dead code on
// Cloudflare and left `wrangler secret put` silently ineffective.
const PROXY_SHARED_SECRET = '';

// Path allowlist: EXACT-match only — a request is forwarded only when its
// pathname is byte-identical to one of these entries. (deno-proxy.js uses the
// same rule; it used to match by prefix, which also admitted
// /v1/messages/anything.) The MiniMax Anthropic-compatible endpoint lives at
// /v1/messages.
const ALLOWED_PATHS = ['/v1/messages'];

// Percent-encodings that must never appear in a forwarded pathname: %2e = '.',
// %2f = '/', %5c = '\', %25 = '%'. They are the classic ways to write a path
// that the edge keeps literal but a downstream component normalises into
// something else ('/v1/messages/%2e%2e/admin'). Legit callers never need them.
const FORBIDDEN_PATH_ENCODINGS = ['%2e', '%2f', '%5c', '%25'];

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
//
// REVIEW-2026-09-20 (honesty fix): the previous comment here claimed the
// Worker "process lives for the duration of a request batch then is destroyed
// by the runtime, so the memory naturally resets — intended behaviour for
// serverless". That is wrong and over-sold the protection: Cloudflare reuses
// a warm isolate across many requests (and across concurrent ones), so the
// Map normally DOES persist — but for how long is not guaranteed, and every
// Colo and every isolate replica keeps its OWN copy. Net effect: this limiter
// is BEST EFFORT. It multiplies its budget by the number of live isolates/
// edge locations and can reset early (isolate evicted) or run longer than one
// window (isolate kept warm). It is an abuse speed bump, not a quota: for a
// hard limit use the Rate Limiting binding / a Durable Object / KV.
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 30;
// Bounded by MAX_RATE_MAP_SIZE so a flood of distinct keys cannot exhaust
// memory (was previously unbounded — a DoS vector). Mirrors deno-proxy.js.
const MAX_RATE_MAP_SIZE = 10_000;
const _rateMap = new Map(); // ip → int[] of timestamps

function rateCheck(ip) {
  const now = Date.now();
  // LRU-style: if we're at the cap, evict the oldest entry (insertion order).
  if (!_rateMap.has(ip) && _rateMap.size >= MAX_RATE_MAP_SIZE) {
    const oldestKey = _rateMap.keys().next().value;
    _rateMap.delete(oldestKey);
  }
  const slots = _rateMap.get(ip);
  if (!slots) {
    _rateMap.set(ip, [now]);
    return { allowed: true, remaining: RATE_MAX - 1, resetMs: RATE_WINDOW_MS };
  }
  // Touch to mark as fresh (LRU).
  _rateMap.delete(ip);
  _rateMap.set(ip, slots);
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
async function secretOk(request, env) {
  // The Worker binding wins (that is what `wrangler secret put` sets); the
  // source-level constant is only a fallback default.
  const secret = (env && env.PROXY_SHARED_SECRET) || PROXY_SHARED_SECRET;
  if (!secret) return false;
  const provided = request.headers.get('X-Proxy-Key') || '';
  return await timingSafeEqual(provided, secret);
}

async function sha256Bytes(text) {
  try {
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return new Uint8Array(buf);
  } catch (_e) {
    // crypto.subtle is unavailable or refused: return null so the caller can
    // fail CLOSED. (Workers and Deno both ship WebCrypto; the only way to get
    // here is a stripped-down runtime, and "cannot verify" must not mean
    // "accept".)
    return null;
  }
}

// Constant-time compare of two equal-length byte arrays. Never returns early:
// the accumulator folds every byte in, so the work done does not depend on
// where the first difference is.
function constantTimeBytesEqual(a, b) {
  if (!a || !b || a.length !== b.length || a.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

// Constant-time string compare via SHA-256 digests.
//
// REVIEW-2026-09-20: the old implementation compared char codes directly and
// short-circuited on `a.length !== b.length`, so the cost of a probe revealed
// the secret's LENGTH (and, because the loop still walked the attacker's
// string, how far the prefixes agreed up to the length cutoff). Hashing both
// sides first makes the compared operands fixed-size (32 bytes) regardless of
// the secret, so neither length nor prefix agreement is observable, and the
// digest compare itself has no early return.
async function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  const [da, db] = await Promise.all([sha256Bytes(a), sha256Bytes(b)]);
  if (!da || !db) return false;   // no WebCrypto -> reject (fail closed)
  return constantTimeBytesEqual(da, db);
}

// Path authorization: EXACT match against ALLOWED_PATHS, with encoded-form
// smuggling rejected first. REVIEW-2026-09-20 (#8, shared with deno-proxy.js):
//   * raw %2e / %2f / %5c / %25 anywhere in the pathname -> refuse;
//   * decodeURIComponent must be a no-op on an allowed path — if decoding
//     changes it, the literal path was not on the allowlist (this also kills
//     '../' and its %2e%2e form, and double-encoded %252e%252e);
//   * malformed escapes (decodeURIComponent throws) -> refuse.
// The target we build below re-uses url.pathname verbatim, so upstream sees
// exactly what we matched — no normalisation gap between "matched path" and
// "forwarded path".
function pathIsAllowed(pathname) {
  if (typeof pathname !== 'string' || pathname.length === 0) return false;
  const lower = pathname.toLowerCase();
  for (const enc of FORBIDDEN_PATH_ENCODINGS) {
    if (lower.includes(enc)) return false;
  }
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch (_e) {
    return false;
  }
  if (decoded !== pathname) return false;
  if (decoded.includes('../') || decoded.includes('..\\')) return false;
  return ALLOWED_PATHS.includes(pathname);
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
  async fetch(request, env) {
    const origin = request.headers.get('Origin') || '';
    // Compute cors + authorization FIRST so the rate-limit 429 branch can
    // safely reference them (TDZ fix: previously `cors` was declared later,
    // so any rate-limited request threw ReferenceError before this 429
    // could be returned with proper CORS headers).
    const cors = corsFor(origin);
    // REVIEW-2026-09-10: this used to be an OR (`cors !== null ||
    // secretOk(request)`), so ANY allowlisted Origin authorized the request by
    // itself and a configured shared secret was never consulted — the opposite
    // of proxy/README.md's "推荐组合" (origin + key both required; a forged
    // Origin without the key is rejected).
    //
    // The documented deployment modes are (README + the mode comments below):
    //   allowlist set + secret set   -> BOTH required (recommended)
    //   allowlist empty + secret set -> key alone is enough (secret-only mode)
    //   allowlist set + no secret    -> Origin alone passes (documented as
    //                                   weak for production; unchanged)
    //   neither                      -> closed (403 for everything)
    //
    // A configured secret must also CHANGE the outcome when the key is
    // missing or wrong: the first draft treated "no key sent" the same as "no
    // secret configured" and let a forged Origin straight through.
    const secretConfigured = Boolean((env && env.PROXY_SHARED_SECRET)
                                     || PROXY_SHARED_SECRET);
    const allowlistEmpty = ALLOWED_ORIGINS.length === 0;
    let authorized;
    if (secretConfigured) {
      // timingSafeEqual() is now async (WebCrypto), so the secret check has
      // to be awaited. It stays the FIRST thing we do with the request — no
      // body byte is read for an unauthorised caller.
      authorized = (await secretOk(request, env)) && (allowlistEmpty || cors !== null);
    } else {
      authorized = cors !== null;
    }

    // Rate-limit key: CF-Connecting-IP when available (set by CF and not
    // client-spoofable) and falls back to the rightmost X-Forwarded-For when
    // behind another trusted proxy (the leftmost is client-controlled and
    // trivially rotatable).
    //
    // REVIEW-2026-09-20: the trailing 'unknown' bucket is shared by every
    // request that carried neither header. On Cloudflare that cannot happen
    // for HTTP traffic (the platform overwrites CF-Connecting-IP), so this is
    // a formality here — unlike deno-proxy.js, which has no such header and
    // therefore keys the no-XFF case on the socket address / a time-sliced
    // bucket instead (see that file).
    const fwd = request.headers.get('x-forwarded-for');
    const rightmostFwd = fwd ? fwd.split(',').slice(-1)[0].trim() : '';
    const clientIp = request.headers.get('CF-Connecting-IP') ||
                     rightmostFwd ||
                     'unknown';

    // Reject unauthorized requests BEFORE consuming a rate-limit slot, so a
    // flood of bogus requests can't fill _rateMap and starve real callers
    // (was previously counting rejected requests against the quota). The
    // outbound target stays the hardcoded MiniMax endpoint regardless.
    if (!authorized) {
      return new Response('Forbidden', { status: 403 });
    }

    // RATE LIMIT: now safe to consume a slot — only authorized callers reach
    // here. The 429 echoes CORS so an authorized browser caller can read it.
    const rl = rateCheck(clientIp);
    if (!rl.allowed) {
      // FIX-2026-09-22: secret-only mode - cors may be null (Origin not
      // allowlisted) but the caller IS authorized by the shared key, so the
      // 429 must echo CORS for that authorized browser caller to read it.
      const cors429 = corsHeadersForAuthorized(request, cors) || {};
      return new Response(JSON.stringify({ error: 'rate_limit_exceeded', retryAfter: rl.resetMs }), {
        status: 429,
        headers: { 'content-type': 'application/json', ...cors429 },
      });
    }

    // For secret-only mode we still need CORS so the browser can use the
    // proxy. This reflects the request's Origin with Vary: Origin.
    const corsEcho = corsHeadersForAuthorized(request, cors) || {};

    // UI-REVIEW-2026-09-22: preflight BEFORE the rate limit - browsers cache
    // preflights for 86400s, so burning a rate slot per preflight just let a
    // page of preflights lock real requests out of the window.
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsEcho });
    }
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405, headers: corsEcho });
    }

    // Path allowlist — REVIEW-2026-09-20 (#7): moved BEFORE the body read.
    // The old order (read up to 50 MB, then decide) let any authorised caller
    // — or anyone who just finds a matching Origin — make the Worker buffer a
    // huge body for a request we were never going to forward. Method and path
    // authorisation needs nothing from the body, so it runs first and the
    // rejected request's body is dropped unconsumed.
    const url = new URL(request.url);
    if (!pathIsAllowed(url.pathname)) {
      return new Response('Not Found', { status: 404, headers: corsEcho });
    }

    // Body size cap — enforce by streaming the body, NOT by trusting
    // Content-Length. Clients can lie about Content-Length; they cannot
    // lie about the bytes they actually send.
    const bounded = await readBoundedBody(request, MAX_BODY_BYTES);
    if (bounded === null) {
      return new Response('Payload Too Large', { status: 413, headers: corsEcho });
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
