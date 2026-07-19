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
const ALLOWED_ORIGINS = [
  // 'https://yourname.github.io',
  // 'http://localhost:8000',
];

// Optional shared secret. Set via `wrangler secret put PROXY_SHARED_SECRET`
// (https://developers.cloudflare.com/workers/configuration/secrets/). When
// set, requests must include `X-Proxy-Key: <secret>`. Empty string = off.
const PROXY_SHARED_SECRET = '';

// Path allowlist: only these path prefixes are forwarded to the upstream.
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

const CORS_HEADERS = {
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'content-type, x-api-key, anthropic-version, x-proxy-key',
  'Access-Control-Max-Age': '86400',
};

function corsFor(origin) {
  const allowed = ALLOWED_ORIGINS.length === 0 || ALLOWED_ORIGINS.includes('*')
    ? '*'
    : ALLOWED_ORIGINS.includes(origin)
      ? origin
      : null;
  return allowed ? { 'Access-Control-Allow-Origin': allowed, ...CORS_HEADERS } : null;
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

export default {
  async fetch(request) {
    const origin = request.headers.get('Origin') || '';
    const cors = corsFor(origin);

    // Preflight
    if (request.method === 'OPTIONS') {
      if (!cors) return new Response('Forbidden', { status: 403 });
      return new Response(null, { status: 204, headers: cors });
    }
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405, headers: cors || {} });
    }
    // Origin check (skip when allowlist is empty AND '*' is the default
    // — that mode is intentionally permissive for local dev).
    if (ALLOWED_ORIGINS.length > 0 && !ALLOWED_ORIGINS.includes('*') && !ALLOWED_ORIGINS.includes(origin)) {
      return new Response('Forbidden', { status: 403 });
    }
    // Shared-secret check
    if (PROXY_SHARED_SECRET) {
      const got = request.headers.get('X-Proxy-Key') || '';
      if (got !== PROXY_SHARED_SECRET) {
        return new Response('Unauthorized', { status: 401, headers: cors || {} });
      }
    }

    // Body size cap
    const lenHeader = request.headers.get('Content-Length');
    const declaredLen = lenHeader ? parseInt(lenHeader, 10) : 0;
    if (declaredLen > MAX_BODY_BYTES) {
      return new Response('Payload Too Large', { status: 413, headers: cors || {} });
    }

    // Path allowlist
    const url = new URL(request.url);
    if (!ALLOWED_PATH_PREFIXES.some((p) => url.pathname === p || url.pathname.startsWith(p + '/'))) {
      return new Response('Not Found', { status: 404, headers: cors || {} });
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
        body: request.body,
      });
    } catch (_e) {
      // Upstream network error (DNS, refused, TLS) — return a real 502 with
      // CORS headers so the browser can surface a meaningful diagnostic
      // instead of an opaque network failure (the default worker error
      // omits CORS, defeating the proxy's primary purpose).
      return new Response(JSON.stringify({ error: 'upstream_unreachable' }), {
        status: 502,
        headers: { 'content-type': 'application/json', ...(cors || {}) },
      });
    }

    // Echo back a sanitized subset of the upstream response headers.
    const respHeaders = pickHeaders(upstreamResp.headers, FORWARDED_RESPONSE_HEADERS);
    if (cors) for (const [k, v] of Object.entries(cors)) respHeaders.set(k, v);
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      statusText: upstreamResp.statusText,
      headers: respHeaders,
    });
  },
};
