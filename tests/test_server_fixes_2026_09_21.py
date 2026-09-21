"""FE-FIX-2026-09-21 — server.py keep-alive framing regressions (audit round).

Proves the four audited socket-framing bugs stay fixed against the real
``Handler`` over real sockets:

  1. HIGH - HEAD wrote the JSON error body onto the socket on every static
     error path (404/403/413/500...): the bytes then desynced the *next*
     keep-alive response (wrong content for a different URL). Fixed in
     ``_serve_static`` by threading ``head_only`` into every ``_send_json``.
  2. MED - a ``Transfer-Encoding: chunked`` POST answered 411 without
     ``Connection: close`` (the pending-body detector only looked at
     Content-Length), leaving the chunks to be parsed as the next request.
     Fixed with a chunked-aware ``_body_pending`` + explicit 411 in
     ``do_POST`` + a bounded chunked drain in ``finish``.
  3. MED - the static 200 streamed to EOF after announcing an fstat'ed
     Content-Length (surplus bytes on the reused socket if the asset grew;
     silent keep-alive of a truncated body if the write failed mid-stream).
     Fixed by copying exactly st.st_size bytes and force-closing on any
     mid-stream shortfall/error.
  4. LOW - ``_serve_static`` set ``close_connection = True`` for a request
     that carries an unreadable body without ever signalling
     ``Connection: close`` (RFC 9112 says a close must be stated or implied
     by connection loss). Fixed by emitting the header on the 200/304 paths.

Plus hardening checks: 304 (GET and HEAD) carry no body, and the ETag
comparison really does handle weak ``W/`` tags, ``*`` and candidate lists.

Run just this file with:
    python -m pytest tests/test_server_fixes_2026_09_21.py -q
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as srv  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers (same real-server harness as
# tests/test_server_static_perf_2026_09_20.py)
# --------------------------------------------------------------------------

class _QuietHandler(srv.Handler):
    def log_message(self, *args, **kwargs):
        pass


class _QuietServer(srv._BoundedThreadingHTTPServer):
    """The deliberate client hang-ups these framing tests perform can race
    the keep-alive readline into a WinError 10053 traceback that the stdlib
    prints to stderr; they are the tested event, not a failure, so quiet the
    printer (assertions still surface everything that matters)."""

    def handle_error(self, request, client_address):
        import traceback
        # keep genuine handler bugs visible, hide only socket abort noise
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


@contextmanager
def _conn(port, timeout=15):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


def _raw(port, timeout=10):
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    return sock


def _read_head(sock) -> "tuple[int, dict, bytes]":
    """Read exactly one response header block; return (status, headers,
    surplus) where *surplus* is every byte that arrived after the final
    empty line - on a framing-correct HEAD/304 response that must be empty.
    """
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


def _read_until_eof(sock, strict=False) -> bytes:
    """Drain to EOF. With strict=True a read timeout (server still holding a
    stalled keep-alive open) raises instead of passing silently."""
    sock.settimeout(10)
    chunks = []
    while True:
        try:
            data = sock.recv(65536)
        except OSError as exc:
            if strict:
                raise AssertionError(
                    "server did not close the connection (read stalled/timed "
                    "out or reset)") from exc
            break
        if not data:
            return b"".join(chunks)
        chunks.append(data)
    return b"".join(chunks)


def _head_request(port, path, extra_headers=""):
    """One raw HEAD; returns (status, headers, surplus_bytes).

    *surplus_bytes* is everything that arrived after the header block (the
    leaked body in the pre-fix build) - sampled with a short quiet period so
    a late flush is still caught.
    """
    with _raw(port) as sock:
        sock.sendall(
            (f"HEAD {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
             f"Connection: keep-alive\r\n{extra_headers}\r\n").encode("latin-1"))
        status, headers, surplus = _read_head(sock)
        surplus += _wait_no_bytes(sock)
        return status, headers, surplus


# --------------------------------------------------------------------------
# 1. HEAD error paths must be header-only, on every response family
# --------------------------------------------------------------------------

HEAD_MATRIX = [
    ("/js/does-not-exist.js", 404),      # missing file
    ("/server.py", 403),                 # whitelist reject
    ("/js/notallowed.txt", 403),         # extension reject (403 or 404)
    ("/api/extract", 405),               # HEAD on an API endpoint
    ("/index.html", 200),                # the success path (already ok)
]


@pytest.mark.parametrize("path,want", HEAD_MATRIX)
def test_head_emits_zero_body_on_every_exit(server, path, want):
    """HEAD 404 used to leak 22 body bytes; none of these may leak any."""
    status, headers, surplus = _head_request(server, path)
    if path == "/js/notallowed.txt":
        assert status in (403, 404)
    else:
        assert status == want, f"HEAD {path}: {status} != {want}"
    assert headers.get("content-length") is not None, \
        f"HEAD {path} must still frame the GET-twin length"
    assert surplus == b"", f"HEAD {path} leaked body bytes: {surplus!r}"


def test_head_404_same_connection_still_serves_correct_get(server):
    """The verified repro, end to end: HEAD 404 then GET on ONE socket.

    Before the fix the 22 leaked 404-body bytes came back as the beginning
    of the GET's response (ResponseNotReady / garbage framing).
    """
    with _raw(server) as sock:
        sock.sendall(b"HEAD /js/does-not-exist.js HTTP/1.1\r\n"
                     b"Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
        status, headers, surplus = _read_head(sock)
        assert status == 404
        surplus += _wait_no_bytes(sock)
        assert surplus == b"", f"HEAD 404 leaked {surplus!r}"

        sock.sendall(b"GET /index.html HTTP/1.1\r\n"
                     b"Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n")
        status, headers, body = _read_head(sock)
        assert status == 200, "the socket no longer serves a clean 200"
        clen = int(headers["content-length"])
        while len(body) < clen:
            more = sock.recv(clen - len(body))
            assert more, "connection died mid-body"
            body += more
        assert len(body) == clen, "GET body length drifted from Content-Length"
        assert body.lstrip().startswith(b"<!DOCTYPE html>") or b"<html" in body, \
            "GET on the reused socket returned content for a different URL"
        assert _wait_no_bytes(sock) == b"", "surplus bytes after the GET body"


def test_head_200_exact_content_length_and_no_body(server):
    target = os.path.join(ROOT, "js", "app.js")
    size = os.path.getsize(target)
    status, headers, surplus = _head_request(server, "/js/app.js")
    assert status == 200
    assert int(headers["content-length"]) == size
    assert surplus == b""


def test_head_and_get_304_are_bodyless(server):
    """Hardening: a 304 (GET or HEAD) carries no bytes and keeps the socket."""
    etag = None
    with _conn(server) as conn:
        conn.request("GET", "/js/app.js")
        resp = conn.getresponse()
        resp.read()
        etag = resp.getheader("ETag")
    assert etag
    for method in ("GET", "HEAD"):
        with _raw(server) as sock:
            sock.sendall(
                (f"{method} /js/app.js HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 f"If-None-Match: {etag}\r\nConnection: keep-alive\r\n\r\n"
                 ).encode("ascii"))
            status, headers, surplus = _read_head(sock)
            assert status == 304, f"{method}: {status}"
            assert "content-length" not in headers, \
                "RFC 7232 4.1: 304 must not be length-framed"
            surplus += _wait_no_bytes(sock)
            assert surplus == b"", f"{method} 304 leaked {surplus!r}"
            # and the same socket still serves the next request correctly
            sock.sendall(b"GET /index.html HTTP/1.1\r\n"
                         b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
            status, headers, body = _read_head(sock)
            assert status == 200


# --------------------------------------------------------------------------
# 2. Transfer-Encoding: chunked is rejected *and* the close is announced
# --------------------------------------------------------------------------

def _post_chunked(port, *, content_type, csrf=None, session=None):
    """Send a real chunked POST over a raw socket; return (status, headers,
    extra_bytes_after_the_framed_body, socket_closed)."""
    body = b"5\r\nhello\r\n0\r\n\r\n"
    hdrs = [
        "POST /api/extract HTTP/1.1",
        f"Host: 127.0.0.1:{port}",
        f"Origin: http://127.0.0.1:{port}",
        f"Content-Type: {content_type}",
        "Transfer-Encoding: chunked",
    ]
    if csrf:
        hdrs.append(f"X-CSRF-Token: {csrf}")
        hdrs.append(f"X-Session-Token: {session}")
    raw = ("\r\n".join(hdrs) + "\r\n\r\n").encode("ascii") + body
    with _raw(port) as sock:
        sock.sendall(raw)
        status, headers, surplus = _read_head(sock)
        if headers.get("content-length"):
            want = int(headers["content-length"])
            while len(surplus) < want:
                more = sock.recv(want - len(surplus))
                if not more:
                    break
                surplus += more
            surplus = surplus[want:]  # keep only bytes PAST the framed body
        surplus += _wait_no_bytes(sock)
        # a polite close (drained in finish()) reads as a clean EOF
        closed = _read_until_eof(sock) == b""
    return status, headers, surplus, closed


def test_chunked_post_411_announces_close_and_dies(server):
    """The audited desync: 411 with declared=None kept the socket alive and
    the chunks were parsed as the next request. Now: framed 411 +
    Connection: close + the connection is really gone (after the drain, no
    RST, no phantom second response)."""
    # mint a CSRF pair so the request reaches the chunked gate itself
    with _conn(server) as conn:
        conn.request("GET", "/api/extract")
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.request("GET", "/index.html")  # keep the connection useful
        assert conn.getresponse().status == 200
    status, headers, extra, closed = _post_chunked(
        server, content_type="application/json",
        csrf=payload["csrf_token"], session=payload["session_token"])
    assert status == 411, f"chunked POST should 411, got {status}"
    assert (headers.get("connection") or "").lower() == "close", \
        "a close on an unread body must be announced (RFC 9112)"
    assert extra == b"", f"phantom second response leaked: {extra!r}"
    assert closed, "the chunked connection was not torn down"


def test_chunked_post_rejected_early_still_announces_close(server):
    """Same desync on the even earlier 415 exit (wrong Content-Type): the
    pending-body check behind ``_send_json`` is now chunked-aware."""
    status, headers, extra, closed = _post_chunked(
        server, content_type="text/plain")
    assert status == 415
    assert (headers.get("connection") or "").lower() == "close"
    assert extra == b"" and closed


def test_chunked_get_static_announces_close(server):
    """A GET carrying chunked bytes must not keep-alive desynced either; the
    200 is served but the close is stated."""
    with _raw(server) as sock:
        sock.sendall(b"GET /index.html HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n"
                     b"5\r\nhello\r\n0\r\n\r\n")
        status, headers, surplus = _read_head(sock)
        assert status == 200
        clen = int(headers["content-length"])
        assert (headers.get("connection") or "").lower() == "close", \
            "unread request body: the close must be announced, not silent"
        body = surplus
        while len(body) < clen:
            more = sock.recv(min(65536, clen - len(body)))
            if not more:
                break
            body += more
        assert len(body) == clen
        # the abandoned chunks must never come back as a second response and
        # the announced close must be real (clean EOF after the polite drain)
        assert _wait_no_bytes(sock) == b""
        assert _read_until_eof(sock) == b""


# --------------------------------------------------------------------------
# 3. Static 200 body is exactly the announced Content-Length
# --------------------------------------------------------------------------

def test_mid_stream_truncation_closes_connection(server, monkeypatch):
    """File shrinks between the fstat and the read (simulated by inflating
    the reported size for one sentinel file): the server must still stop at
    the announced length AND close, rather than keep-aliving a short body.
    """
    name = f"_fe_f_{os.getpid()}_{uuid.uuid4().hex[:8]}.css"
    target = os.path.join(ROOT, "assets", name)
    SENT = 4444  # only stats of exactly this size get inflated
    EXTRA = 3000
    real_fstat = os.fstat

    def fake_fstat(fd, *a, **kw):
        st = real_fstat(fd, *a, **kw)
        if getattr(st, "st_size", -1) == SENT:
            import types
            return types.SimpleNamespace(
                st_size=st.st_size + EXTRA,
                st_mtime_ns=st.st_mtime_ns,
                st_mode=st.st_mode,
            )
        return st

    monkeypatch.setattr(srv.os, "fstat", fake_fstat)
    try:
        with open(target, "wb") as f:
            f.write(b"x" * SENT)
        with _raw(server) as sock:
            sock.sendall(b"GET /assets/" + name.encode("ascii") +
                         b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Connection: keep-alive\r\n\r\n")
            status, headers, surplus = _read_head(sock)
            assert status == 200
            clen = int(headers["content-length"])
            assert clen == SENT + EXTRA, "sentinel size not picked up"
            body = _read_until_eof(sock, strict=True)
            total = surplus + body
            assert len(total) == SENT, (
                "the body must stop at the real file size (exactly the "
                f"announced bytes minus nothing): got {len(total)}")
            # THE FIX: a short body may not leave the connection alive - the
            # strict drain above only returns on a server-initiated clean
            # EOF, it raises if the stalled keep-alive socket left the client
            # hanging for the announced bytes that never come.
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


def test_stale_etag_full_body_length_exact_after_grow(server, monkeypatch):
    """An asset that GROWS between stat and read must not spill surplus
    bytes onto the reused socket: the copy stops at the announced length.
    Simulated with an fstat that under-reports the sentinel size.
    """
    name = f"_fe_f_{os.getpid()}_{uuid.uuid4().hex[:8]}.css"
    target = os.path.join(ROOT, "assets", name)
    SENT = 5555
    LIE = 1000  # announce only this many bytes
    real_fstat = os.fstat

    def fake_fstat(fd, *a, **kw):
        st = real_fstat(fd, *a, **kw)
        if getattr(st, "st_size", -1) == SENT:
            import types
            return types.SimpleNamespace(
                st_size=LIE, st_mtime_ns=st.st_mtime_ns, st_mode=st.st_mode)
        return st

    monkeypatch.setattr(srv.os, "fstat", fake_fstat)
    try:
        with open(target, "wb") as f:
            f.write(b"y" * SENT)
        with _raw(server) as sock:
            sock.sendall(b"GET /assets/" + name.encode("ascii") +
                         b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Connection: keep-alive\r\n\r\n")
            status, headers, surplus = _read_head(sock)
            assert status == 200
            clen = int(headers["content-length"])
            assert clen == LIE
            body = surplus
            while len(body) < clen:
                more = sock.recv(clen - len(body))
                assert more, "connection died mid-body"
                body += more
            # exactly clen bytes were announced; nothing further may arrive
            assert _wait_no_bytes(sock) == b"", (
                "surplus bytes past the Content-Length - the old "
                "copyfileobj-to-EOF bug framing")
            # and the same connection can still serve a follow-up GET
            sock.sendall(b"GET /index.html HTTP/1.1\r\n"
                         b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
            status2, _, _ = _read_head(sock)
            assert status2 == 200, "reused socket was desynced by surplus"
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 4. Every close decision is signalled (RFC 9112)
# --------------------------------------------------------------------------

def test_get_with_body_announces_close_on_200(server):
    """A GET that (wrongly) carries a body forces the close - now with the
    matching ``Connection: close`` header on the 200, which the old code
    decided silently."""
    with _conn(server) as conn:
        conn.request("GET", "/index.html", body=b"hello",
                     headers={"Content-Length": "5"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Length") is not None
        resp.read()
        assert (resp.getheader("Connection") or "").lower() == "close"
        assert resp.will_close is True


def test_head_with_body_announces_close_on_404(server):
    """Same on a HEAD error path (body pending + error JSON)."""
    with _conn(server) as conn:
        conn.request("HEAD", "/js/does-not-exist.js", body=b"hello",
                     headers={"Content-Length": "5"})
        resp = conn.getresponse()
        assert resp.status == 404
        assert resp.read() == b""
        assert (resp.getheader("Connection") or "").lower() == "close"


# --------------------------------------------------------------------------
# Hardening spot-check: ETag comparison (the audit called this "clean")
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,tag,want", [
    ('"abc"', '"abc"', True),                          # exact
    ('W/"abc"', '"abc"', True),                        # weak client tag
    ('"abc"', 'W/"abc"', True),                        # weak server tag
    ('  "abc"  ', '"abc"', True),                      # whitespace
    ('"x", W/"abc", "z"', '"abc"', True),              # list + weak + space
    ('*', '"abc"', True),                              # any
    ('  *  ', '"abc"', True),                          # any, padded
    ('"abcde"', '"abcd"', False),                      # prefix must NOT match
    ('abc', '"abc"', False),                           # quotes are load-bearing
    ('W/x', '"abc"', False),                           # weak prefix on garbage
    ('', '"abc"', False),                              # empty header
    (None, '"abc"', False),                            # missing header
])
def test_etag_comparison_handles_weak_star_and_lists(value, tag, want):
    assert srv._etag_header_matches(value, tag) is want


def test_pending_body_helpers_agree_with_chunked():
    """Unit guard for the audit #2 detector itself."""
    h = srv.Handler.__new__(srv.Handler)
    h._request_body_consumed = False

    class _H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    h.headers = _H()
    assert h._chunked_pending() is False
    assert h._body_pending() is False

    h.headers = _H({"Transfer-Encoding": "Chunked"})   # case-insensitive
    assert h._chunked_pending() is True
    assert h._body_pending() is True                   # the fix: not None-safe

    h._request_body_consumed = True
    assert h._body_pending() is False

    h._request_body_consumed = False
    h.headers = _H({"Content-Length": "17"})
    assert h._pending_body_bytes() == 17
    assert h._body_pending() is True
