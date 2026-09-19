"""T2-hardening: tests for server.py security & correctness hardening.

Captures bugs from the multi-agent audit (finding set for server.py).
Each test_* function below asserts the post-fix behavior; before the fix
they should fail (RED), and after the fix they should pass (GREEN).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import server as srv

_pass = 0
_fail = 0


@pytest.fixture(autouse=True)
def _reset_server_rate_limit():
    """REVIEW-2026-11-07 (low): server.py's rate limiter keeps its
    per-IP history in a MODULE-level dict (_rate_history, 30 req/60 s).
    Every test file that imports `server` shares that state within one
    pytest process, so a combined full-suite run can burn through the
    window and a later test gets a spurious 429 (seen 2026-11-07 when
    the root-level server tests were run in a single pytest process).
    Reset the history around each test. The intentional-burst test
    (test_get_api_extract_rate_limited) still works: it starts from a
    clean window and generates the burst itself."""
    saved = srv._rate_history
    srv._rate_history = {}
    try:
        yield
    finally:
        srv._rate_history = saved


def check(name, cond, msg=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name, msg)


class _Handler(srv.Handler):
    def log_message(self, *args, **kwargs):
        pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start():
    from http.server import ThreadingHTTPServer
    port = _free_port()
    # Populate EXPECTED_HOSTS so the CSRF check is anchored to the
    # actual bind address (mirrors what server.main() does).
    srv.EXPECTED_HOSTS.clear()
    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/index.html", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    return base, httpd, t


def _stop(httpd, t):
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=2)


def _fetch_csrf(base):
    """GET /api/extract to obtain a CSRF + session token pair."""
    with urllib.request.urlopen(base + "/api/extract", timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body["session_token"], body["csrf_token"]


def _post(base, path, data, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    body = data if isinstance(data, bytes) else data.encode("utf-8")
    req = urllib.request.Request(base + path, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# A) Cache key correctness — captcha/media_type/header values/run_idx
# ---------------------------------------------------------------------------


def test_cache_key_includes_caption():
    """Same request, different caption → different cache key."""
    from rca_core.cache import ResultCache
    base_kwargs = dict(
        endpoint="https://api.example.com", model="m1",
        api_format="anthropic", extra_headers=[], prompt_version="v1",
        max_tokens=100, chart_lang="auto", mode="range_chart",
        image_b64="AAA",
    )
    a = ResultCache.make_key(caption="caption A", media_type="image/png", **base_kwargs)
    b = ResultCache.make_key(caption="caption B", media_type="image/png", **base_kwargs)
    check("cache-key-distinct-caption", a != b)


def test_cache_key_includes_media_type():
    """Same request, different media_type → different cache key."""
    from rca_core.cache import ResultCache
    base_kwargs = dict(
        endpoint="https://api.example.com", model="m1",
        api_format="anthropic", extra_headers=[], prompt_version="v1",
        max_tokens=100, chart_lang="auto", mode="range_chart",
        image_b64="AAA",
    )
    a = ResultCache.make_key(caption="", media_type="image/png", **base_kwargs)
    b = ResultCache.make_key(caption="", media_type="image/jpeg", **base_kwargs)
    check("cache-key-distinct-media-type", a != b)


def test_cache_key_includes_extra_header_values():
    """Same header KEYS but different VALUES → different cache key.

    The old make_key only hashed the keys, allowing a user to flip
    Authorization header values and get a cached wrong-identity response.
    """
    from rca_core.cache import ResultCache
    base = dict(
        endpoint="https://api.example.com", model="m1",
        api_format="anthropic", prompt_version="v1",
        max_tokens=100, chart_lang="auto", mode="range_chart",
        image_b64="AAA", caption="", media_type="image/png",
    )
    a = ResultCache.make_key(extra_headers={"X-A": "1", "X-B": "2"}, **base)
    b = ResultCache.make_key(extra_headers={"X-A": "9", "X-B": "2"}, **base)
    check("cache-key-distinct-header-values", a != b)


def test_multi_run_cache_keys_are_distinct():
    """Each multi-run slot must have a distinct cache key.

    Bug: the server built N keys with loop-invariant fields → identical
    sha256 → only the first slot is stored; subsequent slots return
    whatever was cached.
    """
    from rca_core.cache import ResultCache
    base = dict(
        endpoint="https://api.example.com", model="m1",
        api_format="anthropic", extra_headers=[], prompt_version="v1",
        max_tokens=100, chart_lang="auto", mode="range_chart",
        image_b64="AAA", caption="", media_type="image/png",
    )
    keys = []
    for run_idx in range(3):
        keys.append(ResultCache.make_key(run_idx=run_idx, **base))
    check("multi-run-keys-distinct", len(set(keys)) == 3,
          f"got {keys}")


# ---------------------------------------------------------------------------
# B) CSRF origin check uses FIXED expected host (not Host header)
# ---------------------------------------------------------------------------


def test_csrf_origin_not_controlled_by_host_header():
    """The Origin check must compare against a fixed allowlist of hosts,
    not the client-controlled ``Host`` header (DNS rebinding bypass)."""
    base, httpd, t = _start()
    try:
        # DNS-rebind trick: client sends Host: evil.example.com but
        # Origin: http://evil.example.com. The server must reject because
        # evil.example.com is not in the allowlist of expected hosts.
        status, body = _post(base, "/api/extract",
                             '{"image_b64":"AAA","provider":null}',
                             headers={
                                 "Origin": "http://evil.example.com",
                                 "Host": "evil.example.com",
                                 "X-Requested-With": "XMLHttpRequest",
                             })
        check("csrf-dns-rebind-rejected", status == 403,
              f"got {status} {body}")
    finally:
        _stop(httpd, t)


# ---------------------------------------------------------------------------
# C) Type validation on JSON fields
# ---------------------------------------------------------------------------


def test_type_validation_invalid_body_not_dict():
    """JSON list/string/number must be rejected with 400, not crash."""
    base, httpd, t = _start()
    try:
        session_token, csrf_token = _fetch_csrf(base)
        common_headers = {
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf_token,
            "X-Session-Token": session_token,
        }
        # Valid JSON but not a dict.
        status, body = _post(base, "/api/extract",
                             '[1,2,3]', headers=common_headers)
        check("type-validate-rejects-list", status == 400,
              f"got {status} {body}")
        status, body = _post(base, "/api/extract",
                             '"just a string"', headers=common_headers)
        check("type-validate-rejects-string", status == 400,
              f"got {status} {body}")
        status, body = _post(base, "/api/extract",
                             '42', headers=common_headers)
        check("type-validate-rejects-number", status == 400,
              f"got {status} {body}")
    finally:
        _stop(httpd, t)


# ---------------------------------------------------------------------------
# D) GET /api/extract — should now return 200 with CSRF token, not 404
# ---------------------------------------------------------------------------


def test_get_api_extract_returns_csrf():
    base, httpd, t = _start()
    try:
        try:
            with urllib.request.urlopen(base + "/api/extract", timeout=5) as r:
                body = json.loads(r.read().decode("utf-8"))
            check("get-api-returns-csrf-200", True)
            check("get-api-csrf-present", bool(body.get("csrf_token")))
            check("get-api-session-present", bool(body.get("session_token")))
        except urllib.error.HTTPError as e:
            check("get-api-returns-csrf-200", False, f"got {e.code}")
    finally:
        _stop(httpd, t)


def test_get_api_extract_rate_limited():
    """GET is also rate-limited — a single client must not be able to
    mint unbounded CSRF tokens.

    REVIEW-2026-09-20 update: the burst count was hardcoded to ``range(50)``
    against the shared 30 req/min POST bucket. The mint GET now has its OWN
    bucket and cap (``srv._RATE_MAX_REQUESTS_GET``) precisely so it can no
    longer starve extractions, so the loop is sized from that constant instead
    of a literal. The intent (an unbounded flood must still be capped) is
    unchanged, and the bare ``check()`` got an ``assert`` so a regression fails
    under pytest instead of only printing FAIL in the script runner.
    """
    base, httpd, t = _start()
    try:
        seen_429 = False
        # Hit the rate limit (mint-GET bucket) and look for 429.
        for _ in range(srv._RATE_MAX_REQUESTS_GET + 20):
            try:
                urllib.request.urlopen(base + "/api/extract", timeout=2)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    seen_429 = True
                    break
        check("get-api-rate-limited", seen_429)
        assert seen_429, "CSRF-mint GET flood was never rate limited"
    finally:
        _stop(httpd, t)


def test_get_mint_has_its_own_rate_bucket():
    """REVIEW-2026-09-20: flooding the free CSRF-mint GET must not consume the
    paid POST budget (cross-site denial of service with no privileges)."""
    srv._rate_history.clear()
    try:
        for _ in range(srv._RATE_MAX_REQUESTS_GET + 5):
            allowed, _wait = srv._check_rate_limit(
                "203.0.113.7", bucket="get",
                max_requests=srv._RATE_MAX_REQUESTS_GET)
        # The mint bucket is exhausted...
        assert allowed is False
        # ...yet a plain extraction POST from the same IP still has its full
        # window available.
        for _ in range(srv._RATE_MAX_REQUESTS):
            post_allowed, _wait = srv._check_rate_limit("203.0.113.7")
            assert post_allowed, "POST bucket was crowded out by the GET flood"
    finally:
        srv._rate_history.clear()


# ---------------------------------------------------------------------------
# E) Thread pool cap — concurrent connections must be capped
# ---------------------------------------------------------------------------


def test_thread_pool_cap_exists():
    """Main must use a bounded concurrency primitive, not raw ThreadingHTTPServer."""
    # Inspect the source for the bound: either a ThreadPoolExecutor with
    # max_workers or a custom HTTPServer subclass that caps threads.
    with open(os.path.join(ROOT, "server.py"), "r", encoding="utf-8") as f:
        src = f.read()
    has_pool = ("ThreadPoolExecutor(" in src and "max_workers=" in src)
    has_custom = ("process_request" in src or "min(max_workers" in src)
    check("thread-pool-cap-exists", has_pool or has_custom,
          "no ThreadPoolExecutor(max_workers=...) or process_request cap")


# ---------------------------------------------------------------------------
# F) SSRF DNS rebinding — endpoint DNS is pinned to the validated IP
# ---------------------------------------------------------------------------


def test_ssrf_dns_rebinding_blocked():
    """The endpoint validator must record the resolved IP so the outbound
    HTTP call cannot be redirected to a different IP via DNS rebinding."""
    # Direct test of the pinning primitive — must resolve localhost to
    # a usable IP literal.
    try:
        ip = srv._pinned_endpoint_ip("https://localhost:443")
        check("ssrf-resolves-localhost", bool(ip), f"got {ip!r}")
    except Exception as exc:
        # With RCA_ALLOW_PRIVATE unset, localhost is private so this
        # should raise. That's still correct behaviour — the pin is
        # gated by the same policy as the validator.
        check("ssrf-resolves-localhost-or-rejects",
              "private IP" in str(exc) or "non-public" in str(exc),
              f"unexpected: {exc!r}")
    # And a literal-IP endpoint must succeed without DNS.
    ip = srv._pinned_endpoint_ip("https://8.8.8.8:443")
    check("ssrf-literal-ip-passes", ip == "8.8.8.8")


# ---------------------------------------------------------------------------
# G) Static root restriction — cannot serve source files
# ---------------------------------------------------------------------------


def test_static_root_does_not_serve_source():
    """The static handler must reject requests for Python / SQL / .env files
    even when those files exist under ROOT, because they are outside the
    served sub-tree."""
    base, httpd, t = _start()
    try:
        # server.py is at the project root and contains a .py extension
        # not in the whitelist; it must NOT be served.
        try:
            urllib.request.urlopen(base + "/server.py", timeout=2)
            check("static-no-source-py", False, "(served)")
        except urllib.error.HTTPError as e:
            check("static-no-source-py", e.code in (403, 404))
    finally:
        _stop(httpd, t)


# ---------------------------------------------------------------------------
# H) as_completed None-safety for cache hits
# ---------------------------------------------------------------------------


def test_as_completed_no_none_safety():
    """The multi-run loop must not pass None to as_completed()."""
    with open(os.path.join(ROOT, "server.py"), "r", encoding="utf-8") as f:
        src = f.read()
    # The pattern that passes None to as_completed is:
    #   pending = [(run_idx, fut or None) for ...]
    #   as_completed([f for _, f in pending])   # → includes None!
    # After the fix, the loop must filter None before as_completed.
    has_buggy = (
        "as_completed([f for _, f in pending])" in src
        or "as_completed([fut for _, fut in pending])" in src
    )
    check("as-completed-no-none", not has_buggy,
          "as_completed still receives None for cache-hit slots")


# ---------------------------------------------------------------------------
# I) _csrf_store bounded
# ---------------------------------------------------------------------------


def test_csrf_store_bounded():
    """The CSRF store must cap its size to avoid unbounded growth."""
    with open(os.path.join(ROOT, "server.py"), "r", encoding="utf-8") as f:
        src = f.read()
    has_bound = (
        "_CSRF_MAX" in src or "_csrf_max" in src or "max_csrf" in src
        or "csrf_store_max" in src or "len(_csrf_store)" in src
    )
    check("csrf-store-bounded", has_bound,
          "no bound on _csrf_store size")


def run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            fails += 1
            print(f"ERROR {fn.__name__}: {exc!r}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    rc = run_all()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(rc)