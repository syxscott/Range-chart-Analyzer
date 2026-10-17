"""FIX-2026-10-17 — the two server-side findings from the 10-17 code review.

  P1-1  A malformed endpoint hostname (an IDNA label > 63 bytes, or an empty
        label) made ``ssrf.is_private_host`` raise ``UnicodeEncodeError``,
        which walked out of ``do_POST``: socketserver logged a traceback and
        closed the socket, so the client received ZERO bytes while every
        neighbouring unusable endpoint answers ``err.badEndpoint``.

  P1-6  ``_safe_local_path`` claimed ``realpath`` would raise on an embedded
        NUL and therefore declined to check for one. On Python 3.13 / Windows
        it does not (``os.path.realpath('...\\\\js\\\\a\\x00b.js')`` returns the
        string unchanged), so a NUL path was answered 404 instead of the 403
        every unusable path gets — seven failing tests in
        tests/test_server_path_resolution.py.

  P2-5  ``is_loopback_bind("")`` returned True, but
        ``ThreadingHTTPServer(("", port))`` binds 0.0.0.0. The one startup
        warning that says "this exposes the server to EVERY peer" was
        therefore skipped for the spelling that exposes it most, and
        ``--host "$RCA_HOST"`` with the variable unset passes exactly that.

Each test below drives the REAL code, not a mock. The P1-1 and P1-6 cases
are asserted on the wire (a complete HTTP response, the expected status, no
unhandled exception recorded by the server), because both defects lived in
the difference between what the function returned and what the client saw.

Run just this file with:
    python -m pytest tests/test_server_exposure_2026_10_17.py -q
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import traceback
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server as srv


#: A label longer than the 63-byte DNS limit, and an empty label. Both are
#: rejected by the IDNA ENCODER, which raises UnicodeEncodeError rather than
#: the socket.gaierror the resolver would have produced.
MALFORMED_HOSTS = [
    "https://" + "a" * 70 + ".com/v1",
    "https://a..b/v1",
    # A single legal-but-unresolvable host is the control: it must be
    # REFUSED (ok=False), never raise. If this one raises, the guard below
    # is not catching what it claims to catch.
    "https://this-host-does-not-exist.invalid/v1",
]


# ---------------------------------------------------------------------------
# P2-5 — the exposure warning must not be skipped by a spelled-empty --host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host,expect_loopback", [
    ("", False),            # binds 0.0.0.0
    ("   ", False),         # ditto, after strip
    ("localhost", True),
    ("127.0.0.1", True),
    ("::1", True),
    ("[::1]", True),
    ("0.0.0.0", False),
    ("::", False),
    ("192.168.31.225", False),
])
def test_is_loopback_bind_matches_what_the_socket_actually_binds(
        host, expect_loopback):
    """The predicate must agree with ``bind()``, not with intent.

    FIX-2026-10-17 (P2-5): the empty string used to answer True here while
    ``ThreadingHTTPServer(("", 0))`` measured ``('0.0.0.0', <port>)``. The
    disagreement is invisible in the predicate and only shows up as a
    missing startup warning.
    """
    assert srv.is_loopback_bind(host) is expect_loopback


def test_an_empty_host_really_does_bind_every_interface():
    """The other half of the same finding, asserted so the comment above
    cannot drift away from the platform again."""
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    httpd = ThreadingHTTPServer(("", 0), BaseHTTPRequestHandler)
    try:
        assert httpd.socket.getsockname()[0] == "0.0.0.0"
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# P1-1 — a malformed endpoint is refused with a structured error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", MALFORMED_HOSTS)
def test_malformed_endpoint_is_refused_not_raised(endpoint):
    """``validate_endpoint`` answers ``(False, reason)`` for these.

    Before the fix the first two raised UnicodeEncodeError out of the
    validator. The third is the control that proves the ``except`` clause
    catches the real thing rather than the test's imagination.
    """
    ok, why = srv._validate_endpoint_safe(endpoint)
    assert ok is False, f"{endpoint!r} was accepted"
    assert why, "a refusal must carry a reason the UI can show"


@pytest.mark.parametrize("endpoint", MALFORMED_HOSTS)
def test_validate_endpoint_itself_does_not_raise(endpoint):
    """The layer below the server wrapper, asserted on its own: the caller
    that reaches ``ssrf`` directly gets the same answer."""
    from rca_core import ssrf
    ok, _why = ssrf.validate_endpoint(endpoint)
    assert ok is False


def test_is_private_host_refuses_rather_than_raising():
    """``is_private_host`` documents "a name we cannot resolve is private".
    That has to include a name the IDNA encoder refuses, which is the only
    way this defect reached the client as a dropped connection."""
    from rca_core import ssrf
    assert ssrf.is_private_host("a" * 70 + ".com") is True
    assert ssrf.is_private_host("a..b") is True


# ---------------------------------------------------------------------------
# P1-6 — the NUL refusal, at both the function and the wire level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url_path", [
    "/js/a%00b.js",
    "/css/%00style.css",
    "/assets/img%00.png",
])
def test_nul_path_is_refused_by_the_resolver(url_path):
    """``_safe_local_path`` returns None, not a path containing a NUL.

    ``os.path.realpath`` no longer raises on this platform, so the
    ``except (ValueError, OSError)`` around it does not fire and the only
    thing standing between a NUL and a served path is the explicit check.
    """
    out = srv.Handler._safe_local_path(None, url_path)
    assert out is None, f"{url_path!r} resolved to {out!r}"
    assert out is None or "\x00" not in out


# ---------------------------------------------------------------------------
# wire-level harness — both defects lived between return value and response
# ---------------------------------------------------------------------------


class _QuietHandler(srv.Handler):
    def log_message(self, *a, **k):
        pass

    def log_error(self, *a, **k):
        pass


class _RecordingServer(__import__("http.server",
                                 fromlist=["ThreadingHTTPServer"]
                                 ).ThreadingHTTPServer):
    """Captures what socketserver would otherwise print to stderr."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.unhandled: list[str] = []

    def handle_error(self, request, client_address):
        self.unhandled.append(traceback.format_exc())


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_server():
    port = _free_port()
    saved = set(srv.EXPECTED_HOSTS)
    srv.EXPECTED_HOSTS.clear()
    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")
    httpd = _RecordingServer(("127.0.0.1", port), _QuietHandler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)
        srv.EXPECTED_HOSTS.clear()
        srv.EXPECTED_HOSTS.update(saved)


#: The sandbox's HTTP proxy answers 127.0.0.1 too and rejects non-Latin-1
#: header bytes, which is an environment artefact, not the server's
#: behaviour. Bypassing it keeps the assertion about OUR code.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def test_a_nul_path_gets_a_clean_403_on_the_wire(live_server):
    base, httpd = live_server
    try:
        with _OPENER.open(base + "/js/a%00b.js", timeout=5) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    assert status == 403, f"expected 403, got {status}: {body[:200]!r}"
    assert b"forbidden" in body
    assert httpd.unhandled == [], (
        f"server logged {len(httpd.unhandled)} unhandled error(s): "
        f"{[t.strip().splitlines()[-1] for t in httpd.unhandled]}")


@pytest.mark.parametrize("endpoint", MALFORMED_HOSTS)
def test_a_malformed_endpoint_gets_a_structured_answer_on_the_wire(
        live_server, endpoint):
    """The observable contract: a complete response carrying
    ``err.badEndpoint``, and nothing raised on the server."""
    base, httpd = live_server
    with _OPENER.open(base + "/api/extract", timeout=5) as r:
        tokens = json.loads(r.read().decode("utf-8"))

    body = json.dumps({
        "image_b64": "QUFB",
        "api_key": "sk-test-not-a-real-key",
        "endpoint": endpoint,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/extract", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": base,
            "X-CSRF-Token": tokens["csrf_token"],
            "X-Session-Token": tokens["session_token"],
        })
    try:
        with _OPENER.open(req, timeout=10) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()

    assert status == 200, f"expected a structured 200, got {status}"
    payload = json.loads(raw.decode("utf-8"))
    assert payload.get("ok") is False
    assert payload.get("error_key") == "err.badEndpoint", payload
    assert httpd.unhandled == [], (
        f"endpoint validation raised out of do_POST: "
        f"{[t.strip().splitlines()[-1] for t in httpd.unhandled]}")
