"""Proxy security & parity tests (cloudflare-worker.js + deno-proxy.js).

These tests use Node.js (via subprocess) to exercise the actual JS files,
because the JS proxy modules are deployed as-is and the security fixes
must work in production, not just in Python re-implementations.

The Python harness:
  - For CF worker: dynamic-imports cloudflare-worker.js (which uses
    `export default { fetch }`), stubs the global `fetch` to a mock
    upstream, and dispatches synthetic Request objects.
  - For Deno worker: stubs the `Deno` global (serve/env.get) and then
    dynamic-imports deno-proxy.js, extracting the registered handler
    by re-registering it on a fake Deno.serve.

Run:  python tests/test_proxy.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = ROOT / "proxy"

NODE = "node"

# --- tiny test harness ------------------------------------------------------

_pass = 0
_fail = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"PASS  {name}")
    else:
        _fail += 1
        msg = f"FAIL  {name}" + (f" -- {detail}" if detail else "")
        print(msg)
        _failures.append(msg)


# --- helpers to drive the JS modules from Node --------------------------------

NODE_DRIVER = r"""
// driver.mjs -- shared harness. Receives JSON payload on stdin, runs the
// named handler, prints JSON-encoded result on stdout.
//
// payload = {
//   module: "cloudflare-worker.js" | "deno-proxy.js",  (file:// URL)
//   moduleOriginal: "cloudflare-worker.js" | "deno-proxy.js",
//   testOverrides: { ALLOWED_ORIGINS?: [...], PROXY_SHARED_SECRET?: "..." },
//   scenario: "...",
//   method, url, headers, body,
//   info: optional Deno.serve handler info ({ remoteAddr: { hostname, port,
//         transport } }) — only meaningful for deno-proxy.js, where the
//         no-X-Forwarded-For rate-limit key comes from the socket peer;
//   mockUpstream: { status, body, headers } | null,
//   env: optional { PROXY_SHARED_SECRET: "..." },
//   batchRequests?: [{ method, url, headers, body, mockUpstream? }, ...],
// }

import { Buffer } from "node:buffer";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

function readStdin() {
  return new Promise((resolve, reject) => {
    let chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

const payloadText = await readStdin();
const payload = JSON.parse(payloadText);

// --- install env overrides ---
if (payload.env) {
  for (const [k, v] of Object.entries(payload.env)) {
    process.env[k] = v;
  }
}

// --- Rewrite the source so we can inject test overrides for the
//     otherwise-const ALLOWED_ORIGINS / PROXY_SHARED_SECRET values. ---
const tmpDir = process.env.__PROXY_TEST_TMP;
const originalPath = payload.module;
let sourceText;
try {
  sourceText = fs.readFileSync(new URL(originalPath), "utf8");
} catch (_e) {
  console.log(JSON.stringify({ error: "cannot read source: " + originalPath }));
  process.exit(3);
}
const overrides = payload.testOverrides || {};
let rewritten = sourceText;
if (Object.prototype.hasOwnProperty.call(overrides, "ALLOWED_ORIGINS")) {
  const v = JSON.stringify(overrides.ALLOWED_ORIGINS);
  rewritten = rewritten.replace(
    /const\s+ALLOWED_ORIGINS\s*=\s*\[[\s\S]*?\];/m,
    `const ALLOWED_ORIGINS = ${v};`
  );
}
if (Object.prototype.hasOwnProperty.call(overrides, "PROXY_SHARED_SECRET")) {
  const v = JSON.stringify(overrides.PROXY_SHARED_SECRET);
  rewritten = rewritten.replace(
    /const\s+PROXY_SHARED_SECRET\s*=\s*(?:"";\s*\/\/[^\n]*\n|'[^']*';\s*\/\/[^\n]*\n|Deno\.env\.get\("PROXY_SHARED_SECRET"\)\s*\|\|\s*"")/,
    `const PROXY_SHARED_SECRET = ${v}`
  );
}
// Write to a unique temp file in tmpDir
const suffix = Math.random().toString(36).slice(2);
const tempFile = path.join(tmpDir, "proxy_" + suffix + ".mjs");
fs.writeFileSync(tempFile, rewritten, "utf8");
const modulePath = new URL("file:///" + tempFile.replace(/\\/g, "/")).href;

// --- mock upstream fetch (single-request mode) ---
let upstreamCallCount = 0;
const upstreamCalls = [];
function buildMockedFetch(mockUpstreamSpec) {
  return async function mockedFetch(url, init) {
    upstreamCallCount++;
    upstreamCalls.push({ url, init });
    const mu = mockUpstreamSpec;
    const headers = new Headers(mu && mu.headers ? mu.headers : {});
    headers.set("anthropic-ratelimit-requests-remaining", "5");
    headers.set("anthropic-ratelimit-tokens-remaining", "999");
    headers.set("x-request-id", "should-be-stripped");
    return new Response((mu && mu.body) || JSON.stringify({ ok: true }), {
      status: (mu && mu.status) || 200,
      headers,
    });
  };
}

// --- mock Deno global if needed ---
if (payload.moduleOriginal === "deno-proxy.js") {
  let registered = null;
  globalThis.Deno = {
    env: { get: (k) => process.env[k] || null },
    serve: (handler) => {
      registered = handler;
    },
  };
  await import(modulePath);
  if (!registered) {
    console.log(JSON.stringify({ error: "deno handler not registered" }));
    process.exit(2);
  }
  globalThis.__denoHandler = registered;
} else {
  const mod = await import(modulePath);
  globalThis.__cfWorker = mod.default || mod;
}

function buildRequest(spec) {
  const headers = new Headers(spec.headers || {});
  let body = null;
  if (spec.body != null) {
    body = new TextEncoder().encode(String(spec.body));
  }
  return new Request(spec.url, {
    method: spec.method || "POST",
    headers,
    body,
  });
}

async function dispatchOne(spec) {
  // Reset mock per-request so each request sees the same default mock
  // unless it specifies its own mockUpstream.
  globalThis.fetch = buildMockedFetch(spec.mockUpstream || payload.mockUpstream);
  const req = buildRequest(spec);
  let resp;
  let threw = null;
  try {
    if (payload.moduleOriginal === "deno-proxy.js") {
      // REVIEW-2026-09-20: forward Deno.serve's second handler argument (the
      // request info, carrying the socket's remoteAddr) when the scenario
      // provides one, so the "no X-Forwarded-For" path can be exercised.
      // Omitting it must keep working — that is what a runtime without
      // socket info (or this harness) looks like.
      resp = await globalThis.__denoHandler(req, spec.info);
    } else {
      // REVIEW-2026-09-10: pass the documented `env` bindings through. The
      // field existed in the spec (see the header comment) but was never
      // forwarded, so the worker's env-based secret lookup — the path
      // `wrangler secret put` configures — had no test coverage at all.
      resp = await globalThis.__cfWorker.fetch(req, payload.env || {});
    }
  } catch (e) {
    threw = { name: e?.name, message: e?.message, stack: e?.stack };
  }
  let respOut = null;
  if (resp) {
    const h = {};
    for (const [k, v] of resp.headers.entries()) h[k.toLowerCase()] = v;
    let text = null;
    try { text = await resp.text(); } catch (_e) { text = null; }
    respOut = { status: resp.status, headers: h, body: text };
  }
  return { threw, response: respOut };
}

let resultOut;
if (payload.batchRequests && payload.batchRequests.length > 0) {
  const results = [];
  for (const spec of payload.batchRequests) {
    results.push(await dispatchOne(spec));
  }
  resultOut = { batch: results, upstreamCalls, upstreamCallCount };
} else {
  const one = await dispatchOne(payload);
  resultOut = { response: one.response, threw: one.threw, upstreamCalls, upstreamCallCount };
}

process.stdout.write(JSON.stringify(resultOut));
"""


def run_js(payload: dict) -> dict:
    """Run the Node driver with the given payload and return parsed JSON."""
    # Keep original module name for branch selection, but also expose a
    # file:// URL so Node's ESM loader accepts it on Windows.
    payload["moduleOriginal"] = payload["module"]
    payload["module"] = Path((PROXY / payload["module"]).resolve()).as_uri()
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "driver.mjs"
        driver.write_text(NODE_DRIVER, encoding="utf-8")
        env = {**os.environ, "__PROXY_TEST_TMP": tmp}
        proc = subprocess.run(
            [NODE, str(driver)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROXY),
            env=env,
        )
        if proc.returncode not in (0, 2, 3):
            raise RuntimeError(
                f"node exited {proc.returncode}\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
            )
        out = proc.stdout.strip()
        if not out:
            raise RuntimeError(f"node produced no stdout. STDERR:{proc.stderr}")
        return json.loads(out)


# Default test origin that is on the (overridden) allowlist.
ALLOWED_ORIGIN = "https://allowed.test"
ALLOWED_ORIGINS_OVERRIDE = [ALLOWED_ORIGIN]


# --- request factories -------------------------------------------------------

def cf_req(method: str = "POST", url: str = "https://worker.example/v1/messages",
           headers: dict | None = None, body: str | None = '{"x":1}',
           origin: str | None = None,
           test_overrides: dict | None = None) -> dict:
    h = headers.copy() if headers else {}
    if origin:
        h.setdefault("Origin", origin)
    return {
        "module": "cloudflare-worker.js",
        "method": method,
        "url": url,
        "headers": h,
        "body": body,
        "testOverrides": test_overrides if test_overrides is not None else {
            "ALLOWED_ORIGINS": ALLOWED_ORIGINS_OVERRIDE,
        },
    }


def deno_req(method: str = "POST", url: str = "https://proxy.example/v1/messages",
             headers: dict | None = None, body: str | None = '{"x":1}',
             origin: str | None = None,
             test_overrides: dict | None = None) -> dict:
    h = headers.copy() if headers else {}
    if origin:
        h.setdefault("Origin", origin)
    return {
        "module": "deno-proxy.js",
        "method": method,
        "url": url,
        "headers": h,
        "body": body,
        "testOverrides": test_overrides if test_overrides is not None else {
            "ALLOWED_ORIGINS": ALLOWED_ORIGINS_OVERRIDE,
        },
    }


# ============================================================================
# F1. CF worker: TDZ in 429 path -- cors referenced before const.
# ============================================================================

def test_cf_tdz_in_rate_limit_path():
    """Hammer the CF worker with > RATE_MAX requests from the same IP.
    The 429 response must NOT throw ReferenceError; must include CORS
    headers when origin is allowlisted, and must have retryAfter in body."""
    # Build a batch of 35 requests sharing one Node process so _rateMap persists
    payload = cf_req(
        headers={"Origin": "https://allowed.test", "X-Forwarded-For": "1.2.3.4"},
        body=None,
    )
    batch = []
    for _ in range(35):
        spec = dict(payload)
        spec["headers"] = dict(payload["headers"])
        batch.append(spec)
    payload["batchRequests"] = batch
    payload["method"] = "GET"
    payload["body"] = None
    payload["headers"] = {"Origin": "https://allowed.test"}
    # Use batch mode; don't dispatch single
    result = run_js(payload)
    saw_429 = False
    any_threw = False
    last_429 = None
    for i, r in enumerate(result.get("batch", [])):
        if r["threw"]:
            any_threw = True
            check(
                "cf-tdz-429-no-throw",
                False,
                detail=f"iteration {i}: threw {r['threw']}",
            )
            return
        if r["response"] and r["response"]["status"] == 429:
            saw_429 = True
            last_429 = r
    check("cf-tdz-429-no-throw-any", not any_threw, detail="some iteration threw")
    check("cf-tdz-saw-429", saw_429, detail="never saw 429 after 35 requests")
    if last_429:
        body = last_429["response"]["body"]
        if body:
            try:
                parsed = json.loads(body)
                check(
                    "cf-tdz-429-retry-after",
                    "retryAfter" in parsed,
                    detail=f"body={body!r}",
                )
            except Exception:
                check("cf-tdz-429-body-json", False, detail=f"body={body!r}")
        else:
            check("cf-tdz-429-body-not-empty", False, detail="empty body")
        cors = last_429["response"]["headers"].get("access-control-allow-origin")
        check("cf-tdz-429-cors-present", cors == "https://allowed.test",
              detail=f"got cors={cors!r}")


# ============================================================================
# F2. Deno worker: same TDZ in 429 path.
# ============================================================================

def test_deno_tdz_in_rate_limit_path():
    payload = deno_req(
        headers={"Origin": "https://allowed.test", "X-Forwarded-For": "1.2.3.4"},
        body=None,
    )
    batch = []
    for _ in range(35):
        spec = dict(payload)
        spec["headers"] = dict(payload["headers"])
        batch.append(spec)
    payload["batchRequests"] = batch
    payload["method"] = "GET"
    payload["body"] = None
    payload["headers"] = {"Origin": "https://allowed.test"}
    result = run_js(payload)
    saw_429 = False
    any_threw = False
    last_429 = None
    for i, r in enumerate(result.get("batch", [])):
        if r["threw"]:
            any_threw = True
            check(
                "deno-tdz-429-no-throw",
                False,
                detail=f"iteration {i}: threw {r['threw']}",
            )
            return
        if r["response"] and r["response"]["status"] == 429:
            saw_429 = True
            last_429 = r
    check("deno-tdz-429-no-throw-any", not any_threw, detail="some iteration threw")
    check("deno-tdz-saw-429", saw_429, detail="never saw 429 after 35 requests")
    if last_429:
        body = last_429["response"]["body"]
        if body:
            try:
                parsed = json.loads(body)
                check(
                    "deno-tdz-429-retry-after",
                    "retryAfter" in parsed,
                    detail=f"body={body!r}",
                )
            except Exception:
                check("deno-tdz-429-body-json", False, detail=f"body={body!r}")
        else:
            check("deno-tdz-429-body-not-empty", False, detail="empty body")
        cors = last_429["response"]["headers"].get("access-control-allow-origin")
        check("deno-tdz-429-cors-present", cors == "https://allowed.test",
              detail=f"got cors={cors!r}")


# ============================================================================
# F3. Vary: Origin on reflected CORS responses.
# ============================================================================

def test_cf_vary_origin_header():
    """Preflight + final responses with a specific origin reflected must
    include Vary: Origin (CDN cache-poisoning mitigation)."""
    payload = cf_req(
        method="OPTIONS",
        headers={
            "Origin": "https://allowed.test",
            "Access-Control-Request-Method": "POST",
        },
        body=None,
    )
    result = run_js(payload)
    check("cf-vary-preflight-no-throw", result["threw"] is None,
          detail=str(result["threw"]))
    if result["response"]:
        vary = result["response"]["headers"].get("vary", "")
        check("cf-vary-preflight-includes-origin",
              "Origin" in vary,
              detail=f"vary={vary!r}")

    payload2 = cf_req(
        headers={"Origin": "https://allowed.test"},
        body='{"model":"x"}',
    )
    payload2["mockUpstream"] = {"status": 200, "body": '{"ok":true}',
                                "headers": {"content-type": "application/json"}}
    result2 = run_js(payload2)
    check("cf-vary-final-no-throw", result2["threw"] is None,
          detail=str(result2["threw"]))
    if result2["response"]:
        vary = result2["response"]["headers"].get("vary", "")
        check("cf-vary-final-includes-origin",
              "Origin" in vary,
              detail=f"vary={vary!r}")


def test_deno_vary_origin_header():
    payload = deno_req(
        method="OPTIONS",
        headers={
            "Origin": "https://allowed.test",
            "Access-Control-Request-Method": "POST",
        },
        body=None,
    )
    result = run_js(payload)
    check("deno-vary-preflight-no-throw", result["threw"] is None,
          detail=str(result["threw"]))
    if result["response"]:
        vary = result["response"]["headers"].get("vary", "")
        check("deno-vary-preflight-includes-origin",
              "Origin" in vary,
              detail=f"vary={vary!r}")

    payload2 = deno_req(
        headers={"Origin": "https://allowed.test"},
        body='{"model":"x"}',
    )
    payload2["mockUpstream"] = {"status": 200, "body": '{"ok":true}',
                                "headers": {"content-type": "application/json"}}
    result2 = run_js(payload2)
    check("deno-vary-final-no-throw", result2["threw"] is None,
          detail=str(result2["threw"]))
    if result2["response"]:
        vary = result2["response"]["headers"].get("vary", "")
        check("deno-vary-final-includes-origin",
              "Origin" in vary,
              detail=f"vary={vary!r}")


# ============================================================================
# F4. Shared-secret mode: valid X-Proxy-Key with NON-allowlisted origin
#     must STILL emit CORS headers (with Vary: Origin) so secret-mode
#     works from the browser.
# ============================================================================

def test_cf_secret_only_mode_emits_cors():
    """When PROXY_SHARED_SECRET is set, a valid X-Proxy-Key should
    authenticate the caller AND emit CORS headers (with Vary: Origin)
    so the browser can use the proxy."""
    payload = cf_req(
        headers={
            "Origin": "https://not-allowlisted.test",
            "X-Proxy-Key": "topsecret",
        },
        body='{"model":"x"}',
        test_overrides={"ALLOWED_ORIGINS": [], "PROXY_SHARED_SECRET": "topsecret"},
    )
    payload["env"] = {"PROXY_SHARED_SECRET": "topsecret"}
    payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}',
                               "headers": {"content-type": "application/json"}}
    result = run_js(payload)
    check("cf-secret-mode-no-throw", result["threw"] is None,
          detail=str(result["threw"]))
    if result["response"]:
        check("cf-secret-mode-status", result["response"]["status"] == 200,
              detail=f"status={result['response']['status']}")
        acao = result["response"]["headers"].get("access-control-allow-origin")
        check("cf-secret-mode-acao", acao == "https://not-allowlisted.test",
              detail=f"acao={acao!r}")
        vary = result["response"]["headers"].get("vary", "")
        check("cf-secret-mode-vary", "Origin" in vary, detail=f"vary={vary!r}")


def test_deno_secret_only_mode_emits_cors():
    payload = deno_req(
        headers={
            "Origin": "https://not-allowlisted.test",
            "X-Proxy-Key": "topsecret",
        },
        body='{"model":"x"}',
        test_overrides={"ALLOWED_ORIGINS": [], "PROXY_SHARED_SECRET": "topsecret"},
    )
    payload["env"] = {"PROXY_SHARED_SECRET": "topsecret"}
    payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}',
                               "headers": {"content-type": "application/json"}}
    result = run_js(payload)
    check("deno-secret-mode-no-throw", result["threw"] is None,
          detail=str(result["threw"]))
    if result["response"]:
        check("deno-secret-mode-status", result["response"]["status"] == 200,
              detail=f"status={result['response']['status']}")
        acao = result["response"]["headers"].get("access-control-allow-origin")
        check("deno-secret-mode-acao", acao == "https://not-allowlisted.test",
              detail=f"acao={acao!r}")
        vary = result["response"]["headers"].get("vary", "")
        check("deno-secret-mode-vary", "Origin" in vary, detail=f"vary={vary!r}")


# ============================================================================
# F5. Wrong X-Proxy-Key must NOT pass even with allowlisted origin IF the
#     secret is configured -- and must NOT leak upstream responses.
# ============================================================================

def test_cf_wrong_secret_rejected():
    payload = cf_req(
        headers={
            "Origin": "https://allowed.test",
            "X-Proxy-Key": "wrong-key",
        },
        body='{"model":"x"}',
        test_overrides={"ALLOWED_ORIGINS": [], "PROXY_SHARED_SECRET": "right-key"},
    )
    payload["env"] = {"PROXY_SHARED_SECRET": "right-key"}
    result = run_js(payload)
    check("cf-wrong-secret-status",
          result["response"] is not None and result["response"]["status"] == 403,
          detail=str(result["response"]))


# ============================================================================
# F6. Body cap: enforce via STREAM accumulation, not Content-Length header
# (clients can lie about Content-Length). We send an actual oversized body
# and verify the proxy rejects with 413 before forwarding.
# ============================================================================

def test_cf_body_cap_stream_enforced():
    payload = cf_req(
        headers={"Origin": "https://allowed.test"},
        body="x" * (60 * 1024 * 1024),  # 60MB > 50MB cap
    )
    result = run_js(payload)
    check("cf-body-cap-stream",
          result["response"] is not None and result["response"]["status"] == 413,
          detail=str(result["response"]))


def test_cf_body_cap_lied_content_length_accepted_if_small():
    """An attacker can lie about Content-Length, but cannot lie about
    the actual bytes they send. A body of 10 bytes with a declared
    Content-Length of 1GB should be ACCEPTED (no stream cap exceeded)."""
    payload = cf_req(
        headers={
            "Origin": "https://allowed.test",
            "Content-Length": str(1024 * 1024 * 1024),  # 1GB lie
        },
        body="hello",  # 5 bytes actual
    )
    result = run_js(payload)
    check("cf-body-cap-lied-cl-accepted",
          result["response"] is not None and result["response"]["status"] == 200,
          detail=str(result["response"]))


def test_deno_body_cap_stream_enforced():
    payload = deno_req(
        headers={"Origin": "https://allowed.test"},
        body="x" * (60 * 1024 * 1024),
    )
    result = run_js(payload)
    check("deno-body-cap-stream",
          result["response"] is not None and result["response"]["status"] == 413,
          detail=str(result["response"]))


def test_deno_body_cap_lied_content_length_accepted_if_small():
    payload = deno_req(
        headers={
            "Origin": "https://allowed.test",
            "Content-Length": str(1024 * 1024 * 1024),
        },
        body="hello",
    )
    result = run_js(payload)
    check("deno-body-cap-lied-cl-accepted",
          result["response"] is not None and result["response"]["status"] == 200,
          detail=str(result["response"]))


# ============================================================================
# F7. pickHeaders wildcard parity (CF & Deno must both accept anthropic-ratelimit-*).
# ============================================================================

def test_cf_pickheaders_anthropic_ratelimit():
    payload = cf_req(
        headers={"Origin": "https://allowed.test"},
        body='{"model":"x"}',
    )
    payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}',
                               "headers": {"content-type": "application/json"}}
    result = run_js(payload)
    check("cf-pickheaders-no-throw", result["threw"] is None,
          detail=str(result["threw"]))
    if result["response"]:
        h = result["response"]["headers"]
        check("cf-pickheaders-anthropic-requests",
              h.get("anthropic-ratelimit-requests-remaining") == "5",
              detail=f"headers={list(h.keys())}")
        check("cf-pickheaders-anthropic-tokens",
              h.get("anthropic-ratelimit-tokens-remaining") == "999",
              detail=f"headers={list(h.keys())}")
        # And internal headers are stripped
        check("cf-pickheaders-strips-x-request-id",
              "x-request-id" not in h,
              detail=f"headers={list(h.keys())}")


def test_deno_pickheaders_anthropic_ratelimit():
    payload = deno_req(
        headers={"Origin": "https://allowed.test"},
        body='{"model":"x"}',
    )
    payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}',
                               "headers": {"content-type": "application/json"}}
    result = run_js(payload)
    check("deno-pickheaders-no-throw", result["threw"] is None,
          detail=str(result["threw"]))
    if result["response"]:
        h = result["response"]["headers"]
        check("deno-pickheaders-anthropic-requests",
              h.get("anthropic-ratelimit-requests-remaining") == "5",
              detail=f"headers={list(h.keys())}")
        check("deno-pickheaders-anthropic-tokens",
              h.get("anthropic-ratelimit-tokens-remaining") == "999",
              detail=f"headers={list(h.keys())}")
        check("deno-pickheaders-strips-x-request-id",
              "x-request-id" not in h,
              detail=f"headers={list(h.keys())}")


# ============================================================================
# F8. Deno rate limiter: leftmost XFF is client-spoofable; key must be on
#     rightmost XFF (closest to proxy).
# ============================================================================

def test_deno_rate_limit_rightmost_xff():
    """Rotate LEFTMOST XFF per request -- if rate limiter keys on leftmost
    (current bug), attacker bypasses limit. Keying on rightmost (after
    trusted proxy) means rotated leftmost cannot multiply the budget."""
    base = deno_req(
        headers={"Origin": "https://allowed.test"},
        body=None,
    )
    batch = []
    for i in range(35):
        spec = dict(base)
        spec["headers"] = {
            "Origin": "https://allowed.test",
            "X-Forwarded-For": f"10.0.0.{i}, 9.9.9.9",  # rotate leftmost, fixed rightmost
        }
        batch.append(spec)
    base["batchRequests"] = batch
    base["method"] = "GET"
    base["body"] = None
    base["headers"] = {"Origin": "https://allowed.test"}
    result = run_js(base)
    saw_429 = False
    for i, r in enumerate(result.get("batch", [])):
        if r["threw"]:
            check("deno-rate-xff-no-throw", False, detail=str(r["threw"]))
            return
        if r["response"] and r["response"]["status"] == 429:
            saw_429 = True
            break
    check("deno-rate-xff-saw-429", saw_429,
          detail="rate limiter keyed on client-spoofable leftmost XFF (bypass)")


# ============================================================================
# F9. Deno _rateMap LRU bound -- after > MAX_ENTRIES distinct keys,
#     the map must NOT grow without bound (we approximate by ensuring
#     memory-cap constant MAX_RATE_MAP_SIZE is honored by reading the
#     source -- this test is integration-level on the request flow).
# ============================================================================

def test_deno_rate_map_lru_bound_exists():
    """Verify the source declares a hard upper bound on _rateMap size."""
    src = (PROXY / "deno-proxy.js").read_text(encoding="utf-8")
    check("deno-rate-map-lru-cap-declared",
          "MAX_RATE_MAP" in src or "MAX_RATEMAP" in src or "lru" in src.lower(),
          detail="no LRU bound constant in source")


# ============================================================================
# F10. README must not advertise a permissive "*" default; must reference
#      the correct env var name (ALLOWED_ORIGINS, not ALLOW_ORIGIN).
# ============================================================================

def test_proxy_readme_safety():
    text = (PROXY / "README.md").read_text(encoding="utf-8")
    check("readme-no-broken-alias",
          "ALLOW_ORIGIN" not in text,
          detail="README still references the non-existent variable ALLOW_ORIGIN")
    check("readme-no-permissive-default",
          "默认 `ALLOW_ORIGIN = '*'`" not in text and
          "`ALLOW_ORIGIN` = `*`" not in text,
          detail="README still recommends permissive '*' default")
    check("readme-documents-secret",
          "PROXY_SHARED_SECRET" in text and ("secret" in text.lower() or "密钥" in text or "シークレット" in text),
          detail="README does not document shared secret")


# ============================================================================
# F11. REVIEW-2026-09-20 #8: path allowlist is EXACT in both variants, and
#      percent-encoded / traversal look-alikes never reach upstream.
#      (deno-proxy.js used to match by prefix: /v1/messages/anything passed.)
# ============================================================================

# (path, must_be_forwarded) — everything except the first is rejected.
PATH_CASES = [
    ("/v1/messages", True),
    ("/v1/messages/", False),          # trailing slash is a DIFFERENT path
    ("/v1/messages/extra", False),     # the old prefix-match hole
    ("/v1/messages/messages", False),
    ("/v1", False),
    ("/", False),
    ("/V1/messages", False),           # case-sensitive on purpose
    ("/v1/messages/..%2fadmin", False),
    ("/v1/messages/%2e%2e/admin", False),
    ("/v1/messages/%2E%2E/admin", False),   # case-insensitive escapes
    ("/%2e%2e/%2e%2e/etc/passwd", False),
    ("/v1/%2e%2e/admin", False),
    ("/v1/messages%2f/admin", False),
    ("/v1/messages/%252e%252e/admin", False),  # double-encoded
    ("/v1/messages/..%5cadmin", False),
    ("/v1/messages/%zz", False),       # malformed escape
    ("/v1/messages/%", False),
    ("/v1/../v1/messages", True),      # the URL parser collapses the dot
                                       # segment BEFORE we look at it, so the
                                       # forwarded pathname is literally the
                                       # allowed one — no bypass, and the
                                       # target is built from that same
                                       # normalised pathname.
]


def _status_for_path(module: str, path: str) -> tuple:
    req = cf_req if module == "cloudflare-worker.js" else deno_req
    payload = req(
        url=f"https://proxy.example{path}",
        headers={"Origin": "https://allowed.test"},
        body='{"model":"x"}',
    )
    payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}'}
    result = run_js(payload)
    assert result["threw"] is None, f"{module} {path} threw {result['threw']}"
    resp = result["response"]
    return (resp["status"] if resp else None), result.get("upstreamCallCount", 0)


def test_cf_path_allowlist_is_exact():
    for path, allowed in PATH_CASES:
        status, upstream_calls = _status_for_path("cloudflare-worker.js", path)
        expect = 200 if allowed else 404
        check(f"cf-path-exact {path}", status == expect,
              detail=f"got {status}, upstream calls {upstream_calls}")
        assert status == expect, f"cloudflare-worker.js {path} -> {status}, want {expect}"
        # A rejected path must never be forwarded, in particular not the
        # prefix-match survivors.
        assert upstream_calls == (1 if allowed else 0), (
            f"cloudflare-worker.js {path}: forwarded {upstream_calls} times"
        )


def test_deno_path_allowlist_is_exact():
    for path, allowed in PATH_CASES:
        status, upstream_calls = _status_for_path("deno-proxy.js", path)
        expect = 200 if allowed else 404
        check(f"deno-path-exact {path}", status == expect,
              detail=f"got {status}, upstream calls {upstream_calls}")
        assert status == expect, f"deno-proxy.js {path} -> {status}, want {expect}"
        assert upstream_calls == (1 if allowed else 0), (
            f"deno-proxy.js {path}: forwarded {upstream_calls} times"
        )


def test_allowed_path_keeps_query_string():
    """The allowlist matches the PATHNAME; the query still reaches upstream."""
    for module, req in (("cloudflare-worker.js", cf_req), ("deno-proxy.js", deno_req)):
        payload = req(
            url="https://proxy.example/v1/messages?model=x&n=1",
            headers={"Origin": "https://allowed.test"},
            body='{"model":"x"}',
        )
        payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}'}
        result = run_js(payload)
        assert result["threw"] is None
        assert result["response"]["status"] == 200, module
        target = result["upstreamCalls"][0]["url"]
        assert target.endswith("/v1/messages?model=x&n=1"), f"{module} -> {target}"


# ============================================================================
# F12. REVIEW-2026-09-20 #7: authorization (origin/secret), method and path are
#      checked BEFORE the body is read, so a rejected request cannot make the
#      proxy buffer 50 MB. We prove the order by pairing a huge body with a
#      rejection reason: the status must be the authorization one, not 413.
# ============================================================================

BIG = "x" * (60 * 1024 * 1024)   # > MAX_BODY_BYTES


def _single(module: str, **kw):
    req = cf_req if module == "cloudflare-worker.js" else deno_req
    payload = req(**kw)
    result = run_js(payload)
    assert result["threw"] is None, f"{module} threw {result['threw']}"
    return result


def test_bad_path_with_huge_body_is_404_not_413():
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        result = _single(module,
                         url="https://proxy.example/v1/messages/../../etc/passwd",
                         headers={"Origin": "https://allowed.test"},
                         body=BIG)
        status = result["response"]["status"]
        check(f"{module}-order-path-before-body", status == 404,
              detail=f"got {status} (413 means the body was read first)")
        assert status == 404, f"{module}: {status} — the body was consumed before the path check"
        assert result["upstreamCallCount"] == 0


def test_encoded_path_with_huge_body_is_404_not_413():
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        result = _single(module,
                         url="https://proxy.example/v1/messages/%2e%2e/admin",
                         headers={"Origin": "https://allowed.test"},
                         body=BIG)
        status = result["response"]["status"]
        check(f"{module}-order-encoded-before-body", status == 404,
              detail=f"got {status}")
        assert status == 404, f"{module}: {status}"


def test_unauthorized_with_huge_body_is_403_not_413():
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        result = _single(module,
                         headers={"Origin": "https://not-allowed.test"},
                         body=BIG)
        status = result["response"]["status"]
        check(f"{module}-order-auth-first", status == 403, detail=f"got {status}")
        assert status == 403, f"{module}: {status}"


def test_put_with_huge_body_is_405_not_413():
    """Method check also precedes the body read. (PUT, not GET: the Request
    constructor forbids a GET body, which would mask the ordering we are
    testing.)"""
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        result = _single(module, method="PUT",
                         headers={"Origin": "https://allowed.test"}, body=BIG)
        status = result["response"]["status"]
        check(f"{module}-order-method-first", status == 405, detail=f"got {status}")
        assert status == 405, f"{module}: {status}"
        assert result["upstreamCallCount"] == 0


def test_body_cap_still_applies_on_the_allowed_path():
    """Regression guard for the reordering: the 413 must still happen for a
    real oversized POST to /v1/messages."""
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        result = _single(module, headers={"Origin": "https://allowed.test"}, body=BIG)
        status = result["response"]["status"]
        check(f"{module}-body-cap-kept", status == 413, detail=f"got {status}")
        assert status == 413, f"{module}: {status}"
        assert result["upstreamCallCount"] == 0


# ============================================================================
# F13. REVIEW-2026-09-20 #9: the shared-secret compare runs on SHA-256 digests
#      (async WebCrypto) and must keep accepting exactly / rejecting anything
#      else, including keys that are shorter, longer or equal-length-wrong.
# ============================================================================

def _secret_case(module: str, key: str, secret: str):
    req = cf_req if module == "cloudflare-worker.js" else deno_req
    payload = req(
        headers={"Origin": "https://not-allowlisted.test", "X-Proxy-Key": key},
        body='{"model":"x"}',
        test_overrides={"ALLOWED_ORIGINS": [], "PROXY_SHARED_SECRET": secret},
    )
    payload["env"] = {"PROXY_SHARED_SECRET": secret}
    payload["mockUpstream"] = {"status": 200, "body": '{"ok":true}'}
    result = run_js(payload)
    assert result["threw"] is None, f"{module} threw {result['threw']}"
    return result["response"]["status"]


SECRET = "correct-horse-battery-staple"


def test_secret_digest_compare_accepts_and_rejects():
    cases = [
        (SECRET, 200),             # exact match, even though the digest path
                                   # no longer looks at lengths at all
        (SECRET[:-1], 403),        # same length minus one
        (SECRET + "x", 403),       # longer
        ("x" * len(SECRET), 403),  # same length, wrong
        ("", 403),                 # no key at all
        (SECRET.upper(), 403),     # case matters
    ]
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        for key, expect in cases:
            got = _secret_case(module, key, SECRET)
            check(f"{module}-secret-digest {key[:12]!r}", got == expect,
                  detail=f"got {got}, want {expect}")
            assert got == expect, f"{module}: key {key!r} -> {got}, want {expect}"


def test_no_secret_configured_rejects_any_key():
    for module in ("cloudflare-worker.js", "deno-proxy.js"):
        got = _secret_case(module, "anything", "")
        check(f"{module}-no-secret-configured", got == 403, detail=f"got {got}")
        assert got == 403


# ============================================================================
# F14. REVIEW-2026-09-20 #9 (Deno): with no X-Forwarded-For the limiter keys on
#      the socket's remote address instead of one shared "unknown" bucket.
# ============================================================================

def _deno_batch(specs):
    base = deno_req(headers={"Origin": "https://allowed.test"}, body=None)
    base["batchRequests"] = specs
    base["method"] = "GET"
    base["body"] = None
    base["headers"] = {"Origin": "https://allowed.test"}
    return run_js(base)


def test_deno_no_xff_uses_socket_addr():
    """35 requests from 35 DIFFERENT socket peers, no XFF at all: with the old
    shared 'unknown' key the first 30 would exhaust everyone's budget."""
    specs = []
    for i in range(35):
        specs.append({
            "method": "GET",
            "url": "https://proxy.example/v1/messages",
            "headers": {"Origin": "https://allowed.test"},
            "body": None,
            "info": {"remoteAddr": {"hostname": f"203.0.113.{i}", "port": 1000 + i,
                                    "transport": "tcp"}},
        })
    result = _deno_batch(specs)
    statuses = [r["response"]["status"] if r["response"] else None
                for r in result["batch"]]
    saw_429 = 429 in statuses
    check("deno-no-xff-per-socket-bucket", not saw_429,
          detail=f"statuses={statuses[:5]}... — looks like one shared bucket")
    assert not saw_429, "distinct socket peers must not share one rate bucket"


def test_deno_same_socket_addr_is_limited():
    """The same peer, no XFF: still limited — the bucket is per address, not
    switched off."""
    specs = [{
        "method": "GET",
        "url": "https://proxy.example/v1/messages",
        "headers": {"Origin": "https://allowed.test"},
        "body": None,
        "info": {"remoteAddr": {"hostname": "203.0.113.7", "port": 9,
                                "transport": "tcp"}},
    } for _ in range(35)]
    result = _deno_batch(specs)
    statuses = [r["response"]["status"] if r["response"] else None
                for r in result["batch"]]
    check("deno-socket-bucket-limited", 429 in statuses,
          detail=f"statuses={statuses[-3:]}")
    assert 429 in statuses, "one peer must still hit RATE_MAX"


def test_deno_xff_wins_over_socket_addr():
    """Behind a trusted proxy the RIGHTMOST XFF keys the bucket. Here every
    request arrives from the same socket peer (the proxy itself) but with a
    different rightmost XFF: if the code preferred the socket address, all 35
    would share one bucket and 429 — the rotating XFF must win instead."""
    specs = []
    for i in range(35):
        specs.append({
            "method": "GET",
            "url": "https://proxy.example/v1/messages",
            "headers": {"Origin": "https://allowed.test",
                        "X-Forwarded-For": f"10.10.10.10, 198.51.100.{i}"},
            "body": None,
            # same socket peer for all of them (the proxy itself)
            "info": {"remoteAddr": {"hostname": "10.0.0.1", "port": 1,
                                    "transport": "tcp"}},
        })
    result = _deno_batch(specs)
    statuses = [r["response"]["status"] if r["response"] else None
                for r in result["batch"]]
    check("deno-xff-preferred-over-socket", 429 not in statuses,
          detail=f"statuses={statuses[:5]}...")
    assert 429 not in statuses, "rightmost XFF must key the bucket, not the proxy IP"


def test_deno_anon_bucket_is_documented_shared_window():
    """No XFF AND no socket info (harness / hidden peer address): the key is a
    window-sliced anonymous bucket. It is shared by every unidentified caller
    — deliberately, and commented as such in the file — but it must at least
    expire: a permanently shared 'unknown' key lets one caller poison the
    proxy for everyone, forever."""
    specs = [{
        "method": "GET",
        "url": "https://proxy.example/v1/messages",
        "headers": {"Origin": "https://allowed.test"},
        "body": None,
    } for _ in range(35)]
    result = _deno_batch(specs)
    statuses = [r["response"]["status"] if r["response"] else None
                for r in result["batch"]]
    check("deno-anon-bucket-limited", 429 in statuses, detail=f"statuses={statuses[-3:]}")
    assert 429 in statuses, "the anonymous fallback bucket must still be capped"
    # And the key is time-sliced, not the literal 'unknown'.
    deno = (PROXY / "deno-proxy.js").read_text(encoding="utf-8")
    assert "anon-window-" in deno and "RATE_WINDOW_MS" in deno


# ============================================================================
# F15. Source-level parity guards for this round (cheap, catches drift between
#      the two variants when a deploy script only ships one of them).
# ============================================================================

def test_review_2026_09_20_source_parity():
    cf = (PROXY / "cloudflare-worker.js").read_text(encoding="utf-8")
    deno = (PROXY / "deno-proxy.js").read_text(encoding="utf-8")
    for name, src in (("cloudflare-worker.js", cf), ("deno-proxy.js", deno)):
        assert "ALLOWED_PATH_PREFIXES" not in src, f"{name}: prefix allowlist is back"
        assert "ALLOWED_PATHS" in src and "function pathIsAllowed" in src, name
        assert "FORBIDDEN_PATH_ENCODINGS" in src, name
        assert "crypto.subtle.digest" in src, f"{name}: no WebCrypto digest"
        assert "async function timingSafeEqual" in src, f"{name}: sync compare is back"
        assert "charCodeAt" not in src, (
            f"{name}: the length-leaking char-by-char compare is back")
        assert "diff |= a[i] ^ b[i]" in src, f"{name}: no constant-time byte loop"
        # ordering: path check before the body read (rindex = the CALL site;
        # the function definition sits earlier in the file)
        assert src.index("pathIsAllowed(url.pathname)") < src.rindex("readBoundedBody(request,"), \
            f"{name}: the body is still read before the path check"
        # isolate-lifetime claim must be stated honestly ("not a quota" is the
        # wording both files now carry; the claim used to be that the runtime
        # destroys the process and the memory therefore resets itself)
        _low = src.lower()
        assert "best-effort" in _low or "best effort" in _low, \
            f"{name}: rate limiter not labelled best effort"
        assert "not a quota" in src, f"{name}: no honest note on what the limiter is"
        assert "solate" in src, f"{name}: no mention of isolate reuse / lifetime"
    assert '|| "unknown"' not in deno, \
        "deno-proxy.js must not fall back to one shared 'unknown' bucket"
    assert "rightmostFwd || socketAddr" in deno, \
        "deno-proxy.js should key the no-XFF case on the socket address"


# ============================================================================
# Runner
# ============================================================================

if __name__ == "__main__":
    tests = [
        test_cf_tdz_in_rate_limit_path,
        test_deno_tdz_in_rate_limit_path,
        test_cf_vary_origin_header,
        test_deno_vary_origin_header,
        test_cf_secret_only_mode_emits_cors,
        test_deno_secret_only_mode_emits_cors,
        test_cf_wrong_secret_rejected,
        test_cf_body_cap_stream_enforced,
        test_cf_body_cap_lied_content_length_accepted_if_small,
        test_deno_body_cap_stream_enforced,
        test_deno_body_cap_lied_content_length_accepted_if_small,
        test_cf_pickheaders_anthropic_ratelimit,
        test_deno_pickheaders_anthropic_ratelimit,
        test_deno_rate_limit_rightmost_xff,
        test_deno_rate_map_lru_bound_exists,
        test_proxy_readme_safety,
        test_cf_path_allowlist_is_exact,
        test_deno_path_allowlist_is_exact,
        test_allowed_path_keeps_query_string,
        test_bad_path_with_huge_body_is_404_not_413,
        test_encoded_path_with_huge_body_is_404_not_413,
        test_unauthorized_with_huge_body_is_403_not_413,
        test_put_with_huge_body_is_405_not_413,
        test_body_cap_still_applies_on_the_allowed_path,
        test_secret_digest_compare_accepts_and_rejects,
        test_no_secret_configured_rejects_any_key,
        test_deno_no_xff_uses_socket_addr,
        test_deno_same_socket_addr_is_limited,
        test_deno_xff_wins_over_socket_addr,
        test_deno_anon_bucket_is_documented_shared_window,
        test_review_2026_09_20_source_parity,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            check(f"{t.__name__}-exception", False, detail=f"{type(e).__name__}: {e}")
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    for f in _failures:
        print(f)
    sys.exit(0 if _fail == 0 else 1)