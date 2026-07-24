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
      resp = await globalThis.__denoHandler(req);
    } else {
      resp = await globalThis.__cfWorker.fetch(req);
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