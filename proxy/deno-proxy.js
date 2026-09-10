// deno-proxy.js
//
// Optional CORS proxy for Range Chart Analyzer (Deno Deploy variant).
// Same purpose as cloudflare-worker.js: a stateless pass-through that adds
// the CORS headers a browser needs. It does not read or store your API key.
//
// SECURITY (Bug-3 / Bug-17 fixes):
//   See cloudflare-worker.js for the full rationale — same hardening
//   applies here. In short:
//     - Origin allowlist via ALLOWED_ORIGINS
//     - Optional shared secret via PROXY_SHARED_SECRET (Deno env var)
//     - Path allowlist
//     - Response header filter
//     - Body size cap
//
// Deploy (Deno Deploy, free tier):
//   1. Create a project at https://dash.deno.com
//   2. Paste this file, deploy.
//   3. Set env var PROXY_SHARED_SECRET if you want auth.
//   4. Copy the project URL and paste it into the app's "Proxy URL" field.

const UPSTREAM = "https://api.minimaxi.com/anthropic";
const ALLOWED_ORIGINS = [
  // 'https://yourname.github.io',
  // 'http://localhost:8000',
];
const PROXY_SHARED_SECRET = Deno.env.get("PROXY_SHARED_SECRET") || "";
const ALLOWED_PATH_PREFIXES = ["/v1/messages"];
const MAX_BODY_BYTES = 50 * 1024 * 1024;

const FORWARDED_REQUEST_HEADERS = new Set([
  "content-type",
  "x-api-key",
  "anthropic-version",
  "authorization",
]);

const FORWARDED_RESPONSE_HEADERS = new Set([
  "content-type",
  "content-length",
  "content-encoding",
  "transfer-encoding",
  "x-ratelimit-remaining",
  "x-ratelimit-reset",
  "retry-after",
  "anthropic-ratelimit-*",
]);

// --- Sliding-window rate limiter (30 requests / 60 seconds per IP) ---
// Bounded by MAX_RATE_MAP_SIZE so a flood of distinct keys cannot exhaust
// memory (was previously unbounded — a DoS vector).
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 30;
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

const CORS_BASE = {
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers":
    "content-type, x-api-key, anthropic-version, x-proxy-key",
  "Access-Control-Max-Age": "86400",
  // Always advertise that we key on Origin when we reflect it.
  "Vary": "Origin",
};

// Authorized when the browser Origin is on the allowlist. An empty allowlist
// yields null (reject) unless a valid X-Proxy-Key is presented — see the
// secretOk() check below. This closes the open-relay footgun where an empty
// list previously became `*`.
function corsFor(origin) {
  if (ALLOWED_ORIGINS.includes("*")) {
    return { "Access-Control-Allow-Origin": "*", ...CORS_BASE };
  }
  if (ALLOWED_ORIGINS.includes(origin)) {
    return { "Access-Control-Allow-Origin": origin, ...CORS_BASE };
  }
  return null;
}

// True when the request carries the correct shared secret. Always returns
// false when no secret is configured. Constant-time compare so an attacker
// cannot probe the secret length / prefix via timing.
function secretOk(request) {
  if (!PROXY_SHARED_SECRET) return false;
  const provided = request.headers.get("X-Proxy-Key") || "";
  return timingSafeEqual(provided, PROXY_SHARED_SECRET);
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) {
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

// In secret-only mode (origin not allowlisted but secretOk() returned true)
// we still need to emit CORS so the browser can read the response. We
// reflect the request's Origin and always include Vary: Origin so caches
// cannot poison. The shared secret is what actually authenticates the
// caller — Origin alone is not trusted (it's trivially spoofable).
function corsHeadersForAuthorized(request, cors) {
  if (cors) return cors;
  const o = request.headers.get("Origin");
  if (!o) return null;
  return { "Access-Control-Allow-Origin": o, ...CORS_BASE };
}

function pickHeaders(source, whitelist) {
  const out = new Headers();
  for (const [k, v] of source.entries()) {
    const lk = k.toLowerCase();
    if (whitelist.has(lk) || [...whitelist].some((p) => p.endsWith("*") && lk.startsWith(p.slice(0, -1)))) {
      out.set(k, v);
    }
  }
  return out;
}

// Stream-read the request body with a hard cap that does NOT trust the
// client's Content-Length (clients can lie about that header).
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
  const total = chunks.reduce((n, c) => n + c.byteLength, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const c of chunks) { out.set(c, offset); offset += c.byteLength; }
  return out;
}

Deno.serve(async (request) => {
  const origin = request.headers.get("Origin") || "";

  // Compute cors + authorization FIRST so the rate-limit 429 branch can
  // safely reference them (TDZ fix: previously `cors` was declared later,
  // so any rate-limited request threw ReferenceError before this 429
  // could be returned with proper CORS headers).
  const cors = corsFor(origin);
  // REVIEW-2026-09-10: mirror of cloudflare-worker.js — an allowlisted Origin
  // alone no longer authorizes a request when a secret is configured. The
  // documented modes: both configured -> both required; allowlist empty +
  // secret -> key alone; no secret -> Origin alone (documented as weak);
  // neither -> closed. A configured secret must also CHANGE the outcome when
  // the key is missing or wrong (the first draft treated "no key sent" the
  // same as "no secret configured" and let a forged Origin through).
  const secretConfigured = Boolean(PROXY_SHARED_SECRET);
  const allowlistEmpty = ALLOWED_ORIGINS.length === 0;
  let authorized;
  if (secretConfigured) {
    authorized = secretOk(request) && (allowlistEmpty || cors !== null);
  } else {
    authorized = cors !== null;
  }

  // RATE LIMIT: key on the RIGHTMOST X-Forwarded-For (edge-appended when
  // behind a trusted proxy). The leftmost is client-controlled and trivially
  // rotatable — never trust it.
  //
  // REVIEW-2026-09-10: this used to prefer `cf-connecting-ip`. Only Cloudflare
  // sets that header (and overwrites a client-supplied one); Deno Deploy does
  // not, so a client could send a fresh cf-connecting-ip per request, reset
  // its rate-limit bucket every time, and also churn the LRU map to evict
  // other clients' entries. This deployment is not Cloudflare, so that header
  // carries no authority here (cloudflare-worker.js keeps using it).
  const fwd = request.headers.get("x-forwarded-for");
  const rightmostFwd = fwd ? fwd.split(",").slice(-1)[0].trim() : "";
  const clientIp = rightmostFwd || "unknown";
  // Sprint B (REVIEW-2026-09-04) #11: reject unauthorized requests BEFORE
  // consuming a rate-limit slot, matching cloudflare-worker.js. Previously
  // the deno variant checked the rate limit first, so a flood of bogus
  // (unauthorized) requests filled _rateMap and starved real callers of
  // their quota. The outbound target stays the hardcoded MiniMax endpoint
  // regardless.
  if (!authorized) {
    return new Response("Forbidden", { status: 403 });
  }

  // RATE LIMIT: now safe to consume a slot — only authorized callers reach
  // here. The 429 echoes CORS so an authorized browser caller can read it.
  const rl = rateCheck(clientIp);
  if (!rl.allowed) {
    return new Response(JSON.stringify({ error: "rate_limit_exceeded", retryAfter: rl.resetMs }), {
      status: 429,
      headers: { "content-type": "application/json", ...(cors || {}) },
    });
  }

  // Secret-only mode still emits CORS so the browser can use the proxy.
  const corsEcho = corsHeadersForAuthorized(request, cors) || {};

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsEcho });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405, headers: corsEcho });
  }

  // Enforce body cap via streaming, not Content-Length.
  const bounded = await readBoundedBody(request, MAX_BODY_BYTES);
  if (bounded === null) {
    return new Response("Payload Too Large", { status: 413, headers: corsEcho });
  }

  const url = new URL(request.url);
  if (!ALLOWED_PATH_PREFIXES.some((p) => url.pathname === p || url.pathname.startsWith(p + "/"))) {
    return new Response("Not Found", { status: 404, headers: corsEcho });
  }

  const target = UPSTREAM.replace(/\/+$/, "") + url.pathname + url.search;

  const reqHeaders = pickHeaders(request.headers, FORWARDED_REQUEST_HEADERS);
  reqHeaders.set("content-type", reqHeaders.get("content-type") || "application/json");

  let upstream;
  try {
    upstream = await fetch(target, {
      method: "POST",
      headers: reqHeaders,
      body: bounded,
    });
  } catch (_e) {
    return new Response(JSON.stringify({ error: "upstream_unreachable" }), {
      status: 502,
      headers: { "content-type": "application/json", ...corsEcho },
    });
  }

  const respHeaders = pickHeaders(upstream.headers, FORWARDED_RESPONSE_HEADERS);
  for (const [k, v] of Object.entries(corsEcho)) respHeaders.set(k, v);
  respHeaders.set("x-ratelimit-remaining", String(rl.remaining));
  respHeaders.set("x-ratelimit-reset-ms", String(rl.resetMs));
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
});
