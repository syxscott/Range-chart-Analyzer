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
]);

// --- Sliding-window rate limiter (30 requests / 60 seconds per IP) ---
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 30;
const _rateMap = new Map(); // ip → int[] of timestamps

function rateCheck(ip) {
  const now = Date.now();
  const slots = _rateMap.get(ip);
  if (!slots) {
    _rateMap.set(ip, [now]);
    return { allowed: true, remaining: RATE_MAX - 1, resetMs: RATE_WINDOW_MS };
  }
  const cutoff = now - RATE_WINDOW_MS;
  let idx = 0;
  while (idx < slots.length && slots[idx] < cutoff) idx++;
  if (idx > 0) slots.splice(0, idx);
  if (slots.length >= RATE_MAX) {
    const oldest = slots[0];
    const resetMs = Math.ceil((oldest + RATE_WINDOW_MS - now) / 1000) + 1;
    return { allowed: false, remaining: 0, resetMs };
  }
  slots.push(now);
  return { allowed: true, remaining: RATE_MAX - slots.length, resetMs };
}

const CORS_BASE = {
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers":
    "content-type, x-api-key, anthropic-version, x-proxy-key",
  "Access-Control-Max-Age": "86400",
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
// false when no secret is configured.
function secretOk(request) {
  if (!PROXY_SHARED_SECRET) return false;
  return (request.headers.get("X-Proxy-Key") || "") === PROXY_SHARED_SECRET;
}

function pickHeaders(source, whitelist) {
  const out = new Headers();
  for (const [k, v] of source.entries()) {
    if (whitelist.has(k.toLowerCase())) out.set(k, v);
  }
  return out;
}

Deno.serve(async (request) => {
  const origin = request.headers.get("Origin") || "";
  // RATE LIMIT: apply before any other processing.
  const clientIp = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
                   request.headers.get("cf-connecting-ip") || "unknown";
  const rl = rateCheck(clientIp);
  if (!rl.allowed) {
    return new Response(JSON.stringify({ error: "rate_limit_exceeded", retryAfter: rl.resetMs }), {
      status: 429,
      headers: { "content-type": "application/json", ...(cors || {}) },
    });
  }

  const cors = corsFor(origin);
  const authorized = cors !== null || secretOk(request);

  if (request.method === "OPTIONS") {
    if (!authorized) return new Response("Forbidden", { status: 403 });
    return new Response(null, { status: 204, headers: cors || {} });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405, headers: (cors || {}) });
  }
  if (!authorized) {
    return new Response("Forbidden", { status: 403 });
  }

  const lenHeader = request.headers.get("Content-Length");
  const declaredLen = lenHeader ? parseInt(lenHeader, 10) : 0;
  if (declaredLen > MAX_BODY_BYTES) {
    return new Response("Payload Too Large", { status: 413, headers: cors || {} });
  }

  const url = new URL(request.url);
  if (!ALLOWED_PATH_PREFIXES.some((p) => url.pathname === p || url.pathname.startsWith(p + "/"))) {
    return new Response("Not Found", { status: 404, headers: cors || {} });
  }

  const target = UPSTREAM.replace(/\/+$/, "") + url.pathname + url.search;

  const reqHeaders = pickHeaders(request.headers, FORWARDED_REQUEST_HEADERS);
  reqHeaders.set("content-type", reqHeaders.get("content-type") || "application/json");

  let upstream;
  try {
    upstream = await fetch(target, {
      method: "POST",
      headers: reqHeaders,
      body: request.body,
    });
  } catch (_e) {
    return new Response(JSON.stringify({ error: "upstream_unreachable" }), {
      status: 502,
      headers: { "content-type": "application/json", ...(cors || {}) },
    });
  }

  const respHeaders = pickHeaders(upstream.headers, FORWARDED_RESPONSE_HEADERS);
  if (cors) for (const [k, v] of Object.entries(cors)) respHeaders.set(k, v);
  respHeaders.set("x-ratelimit-remaining", String(rl.remaining));
  respHeaders.set("x-ratelimit-reset-ms", String(rl.resetMs));
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: respHeaders,
  });
});
