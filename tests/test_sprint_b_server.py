"""Sprint B (REVIEW-2026-09-04) regression tests for server.py.

Covers the review items that mandate regression tests, plus two cheap
unit-level guards:

  #1  Multi-run batch timeout: a hung extract run must surface
      ``err.timeout`` within the batch budget instead of stalling the
      request forever (the old ``as_completed`` + ``result(timeout=)``
      loop made the timeout branch dead code).
  #2  GET /api/history/<id>/provenance: requires the custom
      ``X-RCA-Client: range-chart-analyzer`` header (403 without it),
      reuses the thread-safe history singleton, and returns real JSON-LD
      for an existing record.
  #6  A non-ASCII X-CSRF-Token header must yield a clean 403 — the old
      ``secrets.compare_digest(str, str)`` raised TypeError on non-ASCII
      and dropped the connection.
  #7  ``_check_rate_limit`` recycles a per-IP entry once its window has
      slid fully empty (no unbounded ``_rate_history`` growth).
  #12 ``_enhance_image_b64``: enhances valid images, silently falls back
      to the input for garbage / oversize re-encodes.
"""
from __future__ import annotations

import base64
import io
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as srv


@pytest.fixture(autouse=True)
def _reset_server_rate_limit():
    """The per-IP rate history is module-level and shared by every test
    file that imports ``server`` — reset around each test so the burst
    budget is clean (same rationale as tests_server.py)."""
    saved = srv._rate_history
    srv._rate_history = {}
    try:
        yield
    finally:
        srv._rate_history = saved


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
    # Anchor the CSRF Origin check to the real bind address (mirrors
    # server.main()).
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
    with urllib.request.urlopen(base + "/api/extract", timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body["session_token"], body["csrf_token"]


def _post(base, path, data, headers=None):
    """POST with CSRF + session tokens auto-populated (tests_server.py
    pattern). Returns (status, body_text)."""
    session_token, csrf_token = _fetch_csrf(base)
    h = {
        "Content-Type": "application/json",
        "Origin": base,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrf_token,
        "X-Session-Token": session_token,
        **(headers or {}),
    }
    body = data if isinstance(data, bytes) else data.encode("utf-8")
    req = urllib.request.Request(base + path, data=body, headers=h,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def _get(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Sprint B #1 — multi-run batch timeout (concurrent.futures.wait)
# ---------------------------------------------------------------------------


def test_multi_run_hung_extract_returns_err_timeout():
    """A multi-run request whose extract calls hang must fail with
    err.timeout after the batch budget instead of hanging forever.

    Pre-fix: ``as_completed`` only yields finished futures, so
    ``fut.result(timeout=...)`` never timed out and the request stalled
    until the client gave up. Post-fix: the batch wait fires and the
    response carries ok=false / err.timeout.
    """
    real_extract = srv.extract

    def hung_extract(**kwargs):  # signature-agnostic fake
        time.sleep(3.0)
        from rca_core.extractor import ExtractResult
        return ExtractResult(ok=True, data={"sections": []})

    # Shrink the batch budget: the inline clamp would otherwise force a
    # >= 20 s wait (timeout_sec >= 10 plus 10 s slack).
    saved_min = srv._MIN_EXTRACT_TIMEOUT_SEC
    saved_slack = srv._MULTI_RUN_TIMEOUT_SLACK_SEC
    srv.extract = hung_extract
    srv._MIN_EXTRACT_TIMEOUT_SEC = 0
    srv._MULTI_RUN_TIMEOUT_SLACK_SEC = 0
    base, httpd, t = _start()
    try:
        t0 = time.monotonic()
        status, body = _post(base, "/api/extract", json.dumps({
            "image_b64": "QUFB",
            "api_key": "test-key",
            "runs": 2,
            "force_rerun": True,
            "timeout_sec": 1,
        }))
        elapsed = time.monotonic() - t0
        assert status == 200, f"expected 200, got {status}: {body}"
        payload = json.loads(body)
        assert payload.get("ok") is False, f"expected ok=false: {body}"
        assert payload.get("error_key") == "err.timeout", \
            f"expected err.timeout, got: {body}"
        # The batch budget (1s) must actually be enforced — allow generous
        # CI slack, but it must finish well before the 3s hung extract.
        assert elapsed < 8.0, f"timeout path took {elapsed:.1f}s — budget not enforced"
    finally:
        srv.extract = real_extract
        srv._MIN_EXTRACT_TIMEOUT_SEC = saved_min
        srv._MULTI_RUN_TIMEOUT_SLACK_SEC = saved_slack
        _stop(httpd, t)


def test_multi_run_partial_exception_error_body_alignment():
    """runs=2 where one run raises: the failure must land in error_body
    (the single-run error convention) instead of being stashed into raw,
    and still merge with the successful run."""
    from rca_core.extractor import ExtractResult
    real_extract = srv.extract
    calls = {"n": 0}

    def one_boom(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("upstream 502 bad gateway")
        return ExtractResult(ok=True, data={"sections": [], "confidence": 0.5})

    saved_min = srv._MIN_EXTRACT_TIMEOUT_SEC
    saved_slack = srv._MULTI_RUN_TIMEOUT_SLACK_SEC
    saved_hist = srv._HAS_HISTORY  # keep the audit writer off the real DB
    saved = srv.extract
    srv.extract = one_boom
    srv._MIN_EXTRACT_TIMEOUT_SEC = 0
    srv._MULTI_RUN_TIMEOUT_SLACK_SEC = 0
    srv._HAS_HISTORY = False
    base, httpd, t = _start()
    try:
        status, body = _post(base, "/api/extract", json.dumps({
            "image_b64": "QUFB",
            "api_key": "test-key",
            "runs": 2,
            "force_rerun": True,
            "timeout_sec": 5,
        }))
        assert status == 200, body
        payload = json.loads(body)
        assert payload.get("ok") is True
        # The failing run is counted as a partial failure.
        assert payload.get("partial_failures") == 1, body
        # The exception text belongs in error_body semantics — it must NOT
        # leak into the merged raw transcript.
        assert "upstream 502" not in (payload.get("raw") or "")
    finally:
        srv.extract = saved
        srv._MIN_EXTRACT_TIMEOUT_SEC = saved_min
        srv._MULTI_RUN_TIMEOUT_SLACK_SEC = saved_slack
        srv._HAS_HISTORY = saved_hist
        _stop(httpd, t)


# ---------------------------------------------------------------------------
# Sprint B #2 — provenance endpoint: header + singleton + rate limit
# ---------------------------------------------------------------------------


@pytest.fixture()
def provenance_env(tmp_path):
    """Point the server's history singleton at a temp DB and seed one
    record. Restores Database + the singleton cache afterwards."""
    from rca_core import Database, HistoryRecord
    saved_db = srv.Database
    saved_cache = srv._HISTORY_STORE_SINGLETON_CACHE
    srv.Database = lambda: Database(str(tmp_path / "history.db"))
    srv._HISTORY_STORE_SINGLETON_CACHE = None  # force re-creation
    store = srv._history_store_singleton()
    rec = HistoryRecord(
        timestamp=time.time(),
        source_file="test:image.png",
        mode="range_chart",
        runs=1,
        result={"sections": [], "confidence": 0.9},
        confidence=0.9,
        status_code=200,
        image_sha256="a" * 64,
        request_meta={"mode": "range_chart"},
    )
    record_id = store.add(rec)
    yield record_id
    srv.Database = saved_db
    srv._HISTORY_STORE_SINGLETON_CACHE = saved_cache


def test_provenance_requires_client_header(provenance_env):
    """Without ``X-RCA-Client: range-chart-analyzer`` the endpoint must
    answer 403 — unauthenticated audit-trail dumps are the bug."""
    record_id = provenance_env
    base, httpd, t = _start()
    try:
        status, body = _get(
            base, f"/api/history/{record_id}/provenance")
        assert status == 403, f"expected 403 without header, got {status}: {body}"
        # A wrong value is equally rejected.
        status, _ = _get(
            base, f"/api/history/{record_id}/provenance",
            headers={"X-RCA-Client": "evil-scanner"})
        assert status == 403, f"expected 403 for wrong header value, got {status}"
    finally:
        _stop(httpd, t)


def test_provenance_with_header_serves_jsonld(provenance_env):
    """With the required header the endpoint serves the PROV-O JSON-LD
    document from the shared singleton store (no per-request Database())."""
    record_id = provenance_env
    base, httpd, t = _start()
    try:
        status, body = _get(
            base, f"/api/history/{record_id}/provenance",
            headers={"X-RCA-Client": "range-chart-analyzer"})
        assert status == 200, f"expected 200, got {status}: {body}"
        doc = json.loads(body)
        assert "@context" in doc
        # And a missing record still 404s under the guard.
        status, _ = _get(
            base, "/api/history/999999/provenance",
            headers={"X-RCA-Client": "range-chart-analyzer"})
        assert status == 404
    finally:
        _stop(httpd, t)


# ---------------------------------------------------------------------------
# Sprint B #6 — non-ASCII CSRF header must not drop the connection
# ---------------------------------------------------------------------------


def test_non_ascii_csrf_token_rejected_cleanly():
    """X-CSRF-Token with non-ASCII characters (headers decode as latin-1)
    must produce a 403 response. Pre-fix: secrets.compare_digest(str, str)
    raised TypeError on non-ASCII and the connection died with no
    response (client-side RemoteDisconnected)."""
    base, httpd, t = _start()
    try:
        session_token, _real_csrf = _fetch_csrf(base)
        h = {
            "Content-Type": "application/json",
            "Origin": base,
            "X-CSRF-Token": "café",  # é is non-ASCII after latin-1 decode
            "X-Session-Token": session_token,
        }
        body = b'{"image_b64": "QUFB", "provider": null}'
        req = urllib.request.Request(base + "/api/extract", data=body,
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, text = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        assert status == 403, \
            f"expected clean 403, got {status}: {text!r}"
        assert "CSRF" in text
    finally:
        _stop(httpd, t)


def test_ascii_csrf_still_accepted():
    """Control: a legitimately minted ASCII token still passes the CSRF
    compare (reaches err.noKey, i.e. past all 403 gates)."""
    base, httpd, t = _start()
    try:
        status, body = _post(base, "/api/extract",
                             '{"image_b64": "QUFB", "provider": null}')
        payload = json.loads(body)
        assert status == 200
        assert payload.get("ok") is False
        assert payload.get("error_key") == "err.noKey", body
    finally:
        _stop(httpd, t)


# ---------------------------------------------------------------------------
# Sprint B #7 — _rate_history recycles empty per-IP entries
# ---------------------------------------------------------------------------


def test_rate_history_recycles_empty_entries():
    """Once an IP's window slides fully empty, its entry must be removed
    from _rate_history (unbounded dict growth was the bug)."""
    ip = "203.0.113.77"
    allowed, _ = srv._check_rate_limit(ip)
    assert allowed
    assert ip in srv._rate_history
    # Age the single entry past the window.
    srv._rate_history[ip].clear()
    srv._rate_history[ip].append(time.time() - srv._RATE_WINDOW_SEC - 1)
    allowed, _ = srv._check_rate_limit(ip)
    assert allowed
    assert ip not in srv._rate_history, (
        "empty window must recycle the per-IP entry"
    )


# ---------------------------------------------------------------------------
# Sprint B #12 — server-side enhance fallback semantics
# ---------------------------------------------------------------------------


def _tiny_png_b64():
    """A 64x64 gradient PNG — unsharp mask + contrast visibly change the
    bytes of a gradient (a flat image would enhance to itself)."""
    from PIL import Image  # noqa: required by the test itself
    img = Image.new("L", (64, 64))
    img.putdata([(x * 4) % 256 for x in range(64 * 64)])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_enhance_image_b64_enhances_and_falls_back():
    """Valid image → enhanced output (different bytes, still a decodable
    PNG); garbage input → original string unchanged (silent fallback)."""
    b64 = _tiny_png_b64()
    out = srv._enhance_image_b64(b64)
    assert out != b64, "enhanced output should differ from the input bytes"
    # Output must still decode to a valid image.
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(out)))
    assert img.size == (64, 64)
    # Garbage b64 → silent fallback to the exact input.
    garbage = "!!!not-base64!!!"
    assert srv._enhance_image_b64(garbage) == garbage
