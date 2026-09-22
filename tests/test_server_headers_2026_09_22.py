"""FE-FIX-2026-09-22 — cache-hardening headers on the dynamic per-session GETs.

Server audit round (server findings, FE-AUDIT-2026-09-22):

  1. The two dynamic GET responses used to carry NO cache policy at all:
       * the CSRF token mint success JSON (``GET /api/extract`` ->
         ``_send_json(200, ...)``) — per-session secret material;
       * the provenance raw 200 (``GET /api/history/<id>/provenance``,
         written straight to ``wfile``, bypassing ``_send_json``) — live
         per-record audit data, personalised by ``X-RCA-Client`` and the
         loopback/token gate.
     Both now answer ``Cache-Control: no-store`` (the mint gained a
     ``cache_control`` pass-through on ``_send_json``; the provenance exit
     states it inline) and provenance additionally declares
     ``Vary: X-RCA-Client`` so no intermediary may share it across
     variants.
  2. HEAD on both API routes keeps its pre-existing contract — 405, ZERO
     body bytes, and the keep-alive connection stays reusable; the new
     headers may not break that framing.

All assertions run against the real ``Handler`` over raw sockets (same
harness as tests/test_server_fixes_2026_09_21.py), because the framing
half of the contract is invisible to a friendly client library.

Run just this file with:
    python -m pytest tests/test_server_headers_2026_09_22.py -q
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as srv  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers (same real-server harness as
# tests/test_server_fixes_2026_09_21.py)
# --------------------------------------------------------------------------

class _QuietHandler(srv.Handler):
    def log_message(self, *args, **kwargs):
        pass


class _QuietServer(srv._BoundedThreadingHTTPServer):
    """Quiet the stdlib traceback printer for the deliberate socket aborts
    these framing probes can race (same rationale as the 2026-09-21 file)."""

    def handle_error(self, request, client_address):
        import traceback
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, socket.timeout, TimeoutError)):
            try:
                request.close()
            except OSError:
                pass
            return
        traceback.print_exception(*sys.exc_info())


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    saved = srv._rate_history
    srv._rate_history = {}
    try:
        yield
    finally:
        srv._rate_history = saved


@pytest.fixture()
def server():
    """A real bounded server on a free loopback port."""
    saved_hosts = set(srv.EXPECTED_HOSTS)
    srv.EXPECTED_HOSTS.clear()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")

    httpd = _QuietServer(("127.0.0.1", port), _QuietHandler, max_workers=8)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    for _ in range(100):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/index.html", timeout=1):
                break
        except Exception:
            time.sleep(0.05)
    else:
        httpd.shutdown()
        pytest.fail("test server did not come up")
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=3)
        srv.EXPECTED_HOSTS.clear()
        srv.EXPECTED_HOSTS.update(saved_hosts)


@pytest.fixture()
def provenance_record(tmp_path, monkeypatch):
    """Seed one history record in a throwaway DB and point the server's
    store singleton at it (tests/test_sprint_b_server.py pattern). The
    token env is cleared so the loopback gate (not a secret) decides."""
    from rca_core import Database, HistoryRecord
    monkeypatch.delenv(srv._PROVENANCE_TOKEN_ENV, raising=False)
    saved_db = srv.Database
    saved_cache = srv._HISTORY_STORE_SINGLETON_CACHE
    srv.Database = lambda: Database(str(tmp_path / "history.db"))
    srv._HISTORY_STORE_SINGLETON_CACHE = None
    try:
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
        yield store.add(rec)
    finally:
        srv.Database = saved_db
        srv._HISTORY_STORE_SINGLETON_CACHE = saved_cache


def _raw(port, timeout=10):
    return socket.create_connection(("127.0.0.1", port), timeout=timeout)


def _read_head(sock) -> "tuple[int, dict, bytes]":
    """Read exactly one response header block; return (status, headers,
    surplus) where *surplus* is every byte that arrived after the final
    empty line."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(
                f"connection closed before headers were complete: {buf!r}")
        buf += chunk
    head, surplus = buf.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, surplus


def _wait_no_bytes(sock, secs=0.35) -> bytes:
    """Return whatever is sitting on the socket within *secs* (must be b'')."""
    sock.settimeout(secs)
    try:
        return sock.recv(4096)
    except socket.timeout:
        return b""
    finally:
        sock.settimeout(10)


def _read_body(sock, headers, surplus) -> bytes:
    """Drain exactly the announced Content-Length from a framed response."""
    clen = int(headers["content-length"])
    body = surplus
    while len(body) < clen:
        more = sock.recv(clen - len(body))
        assert more, "connection died mid-body"
        body += more
    assert len(body) == clen
    return body


def _get_raw(port, path, extra_headers=()):
    """One raw keep-alive GET; returns (sock, status, headers, body).

    The caller owns the socket — half of these tests continue on it to
    prove the connection stayed reusable.
    """
    sock = _raw(port)
    try:
        lines = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1",
                 "Connection: keep-alive", *extra_headers]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        status, headers, surplus = _read_head(sock)
        body = b""
        if headers.get("content-length"):
            body = _read_body(sock, headers, surplus)
        else:
            body = surplus
        return sock, status, headers, body
    except BaseException:
        sock.close()
        raise


def _follow_up_get_ok(sock, port):
    """Prove the connection is reusable: one more framed GET on the SAME
    socket must arrive clean (status + exact length, no surplus)."""
    sock.sendall(b"GET /index.html HTTP/1.1\r\n"
                 b"Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
    status, headers, surplus = _read_head(sock)
    assert status == 200, "the socket no longer serves a clean 200"
    _read_body(sock, headers, surplus)
    assert _wait_no_bytes(sock) == b"", "surplus bytes after the follow-up GET"


# --------------------------------------------------------------------------
# 1. Token mint GET: 200 JSON now says no-store, framing untouched
# --------------------------------------------------------------------------

def test_mint_get_success_declares_no_store(server):
    sock, status, headers, body = _get_raw(server, "/api/extract")
    try:
        assert status == 200
        payload = json.loads(body)
        assert payload.get("csrf_token") and payload.get("session_token")
        assert headers.get("cache-control") == "no-store", (
            "the minted CSRF pair is per-session secret material on a "
            f"cacheable GET; got Cache-Control: {headers.get('cache-control')!r}")
        assert _wait_no_bytes(sock) == b"", "surplus bytes past Content-Length"
        # keep-alive framing survived the new header: the socket must still
        # serve the next request cleanly.
        _follow_up_get_ok(sock, server)
    finally:
        sock.close()


def test_mint_head_is_bodyless_and_reusable(server):
    """HEAD /api/extract answers 405 (no representation for the API GETs);
    the hardening must not leak a body or end the connection."""
    sock = _raw(server)
    try:
        sock.sendall(b"HEAD /api/extract HTTP/1.1\r\n"
                     b"Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
        status, headers, surplus = _read_head(sock)
        assert status == 405
        assert headers.get("content-length") is not None, \
            "HEAD must still frame the GET-twin length"
        surplus += _wait_no_bytes(sock)
        assert surplus == b"", f"HEAD leaked body bytes: {surplus!r}"
        _follow_up_get_ok(sock, server)
    finally:
        sock.close()


# --------------------------------------------------------------------------
# 2. Provenance raw 200: no-store + Vary, body exact, socket reusable
# --------------------------------------------------------------------------

def test_provenance_get_declares_no_store_and_vary(server, provenance_record):
    path = f"/api/history/{provenance_record}/provenance"
    sock, status, headers, body = _get_raw(
        server, path, extra_headers=["X-RCA-Client: range-chart-analyzer"])
    try:
        assert status == 200, f"expected 200, got {status}: {body!r}"
        doc = json.loads(body)
        assert "@context" in doc
        assert headers.get("cache-control") == "no-store", (
            "the raw-200 provenance exit bypasses _send_json and used to "
            f"ship no cache policy at all: {headers.get('cache-control')!r}")
        assert headers.get("vary") == srv._PROVENANCE_CLIENT_HEADER, (
            "the response is personalised by X-RCA-Client (and the "
            "loopback/token gate) — intermediaries must not share variants")
        assert _wait_no_bytes(sock) == b"", "surplus bytes past Content-Length"
        _follow_up_get_ok(sock, server)
    finally:
        sock.close()


def test_provenance_variant_changes_with_client_header(server,
                                                       provenance_record):
    """Two GETs differing only in X-RCA-Client must not be interchangeable
    (403 vs 200), and the 200 variant carries no-store + Vary — the exact
    property ``Vary: X-RCA-Client`` advertises."""
    path = f"/api/history/{provenance_record}/provenance"
    with _raw(server) as s1:
        status1, headers1, _ = _get_raw_into(
            s1, path, "X-RCA-Client: range-chart-analyzer")
        assert status1 == 200
        assert headers1.get("cache-control") == "no-store"
        assert headers1.get("vary") == "X-RCA-Client"
    with _raw(server) as s2:
        status2, headers2, _ = _read_head_after(
            s2, path, "X-RCA-Client: some-other-client")
        assert status2 == 403, (
            "a different X-RCA-Client value selects a different (403) "
            f"variant, got {status2}")
        # _read_head_after already drained the framed 403 body.


def test_provenance_head_is_bodyless_and_reusable(server, provenance_record):
    sock = _raw(server)
    path = f"/api/history/{provenance_record}/provenance"
    try:
        sock.sendall((f"HEAD {path} HTTP/1.1\r\n"
                      "Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n"
                      ).encode("latin-1"))
        status, headers, surplus = _read_head(sock)
        assert status == 405
        assert headers.get("content-length") is not None
        surplus += _wait_no_bytes(sock)
        assert surplus == b"", f"HEAD leaked body bytes: {surplus!r}"
        _follow_up_get_ok(sock, server)
    finally:
        sock.close()


# --- small helpers built on the raw send/read pair for the two-variant test
def _get_raw_into(sock, path, client_header_line):
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"{client_header_line}\r\nConnection: keep-alive\r\n\r\n"
                  ).encode("latin-1"))
    status, headers, surplus = _read_head(sock)
    body = b""
    if headers.get("content-length"):
        body = _read_body(sock, headers, surplus)
    return status, headers, body


def _read_head_after(sock, path, client_header_line):
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  f"{client_header_line}\r\nConnection: keep-alive\r\n\r\n"
                  ).encode("latin-1"))
    status, headers, surplus = _read_head(sock)
    if headers.get("content-length"):
        _read_body(sock, headers, surplus)
    else:
        surplus += _wait_no_bytes(sock)
        assert surplus == b"", "unframed body on a 403"
    return status, headers, surplus
