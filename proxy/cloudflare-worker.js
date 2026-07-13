// cloudflare-worker.js
//
// Optional CORS proxy for Range Chart Analyzer.
// Deploy this ONLY if a direct browser call to MiniMax is blocked by CORS
// (you will see a "network / CORS" error in the app). It is a stateless,
// transparent pass-through: it forwards the request to MiniMax and adds the
// CORS headers the browser requires. It does NOT read, log, or store your
// API key — the key stays in the request headers and is forwarded as-is.
//
// Deploy (Cloudflare Workers, free tier):
//   1. Create a Worker at https://dash.cloudflare.com  (Workers & Pages)
//   2. Paste this file as the Worker script and Deploy.
//   3. Copy the Worker URL (e.g. https://range-proxy.<you>.workers.dev)
//   4. Paste it into the app's "Proxy URL" field in API Settings.
//
// The app calls  <proxyUrl>/v1/messages , so this Worker forwards
// <UPSTREAM>/v1/messages  preserving the path.

const UPSTREAM = 'https://api.minimaxi.com/anthropic';

// Restrict who may use your proxy. '*' allows any origin (simplest, but
// anyone who learns the URL can route through it). To lock it down, set this
// to your site origin, e.g. 'https://yourname.github.io'.
const ALLOW_ORIGIN = '*';

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': ALLOW_ORIGIN,
  'Access-Control-Allow-Methods': 'POST, OPTIONS',
  'Access-Control-Allow-Headers': 'content-type, x-api-key, anthropic-version',
  'Access-Control-Max-Age': '86400',
};

export default {
  async fetch(request) {
    // Preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405, headers: CORS_HEADERS });
    }

    const url = new URL(request.url);
    const target = UPSTREAM.replace(/\/+$/, '') + url.pathname;

    // Forward the request verbatim (method, headers, body).
    const upstreamResp = await fetch(target, {
      method: 'POST',
      headers: request.headers,
      body: request.body,
    });

    // Copy the upstream response and attach CORS headers.
    const respHeaders = new Headers(upstreamResp.headers);
    for (const [k, v] of Object.entries(CORS_HEADERS)) {
      respHeaders.set(k, v);
    }
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      statusText: upstreamResp.statusText,
      headers: respHeaders,
    });
  },
};
