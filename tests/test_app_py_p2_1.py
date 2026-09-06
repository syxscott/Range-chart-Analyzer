"""Regression / clarification tests for P2-1: app.py is a PyWebView
wrapper that opens a NATIVE window pointing at the local server. It is
not a separate deployment scenario, so the browser-Origin threat model
of a publicly hosted deployment does not apply verbatim — but the CSRF
Origin check in server.py is fail-closed (REVIEW-2026-07-31), so the
EXPECTED_HOSTS allowlist MUST be populated here too.

Updated for Sprint B (REVIEW-2026-09-04): the previous version of this
test asserted that "EXPECTED_HOSTS hardening is intentionally absent"
in app.py. That assertion has been stale since REVIEW-2026-07-31 —
app.py:_start_server calls server.populate_expected_hosts(host, port)
before binding, so the CSRF check is anchored to a real allowlist and
stays fail-closed in the local-only flow as well. This test now asserts
the CURRENT behavior:
  * app.py starts an HTTP server on a localhost-only binding
  * the frontend URL it loads uses 127.0.0.1 (same origin as server.py)
  * app.py populates server.EXPECTED_HOSTS via populate_expected_hosts
"""
from __future__ import annotations

import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestAppPyLocalOnly:
    def test_app_binds_to_loopback_only(self):
        """app.py must call into server.py with host=127.0.0.1 (default)
        so the PyWebView window is same-origin with the API."""
        with open("app.py", "r", encoding="utf-8") as f:
            src = f.read()
        # Look for either an explicit --host 127.0.0.1 or a default.
        # The simplest check: server.py is invoked with the default host
        # (which server.py wires to 127.0.0.1 via its main()).
        assert "127.0.0.1" in src or "loopback" in src.lower() or "default_host" in src, (
            "app.py should bind the API server to a loopback/localhost address"
        )

    def test_app_populates_expected_hosts(self):
        """Since REVIEW-2026-07-31 (and asserted as of Sprint B
        REVIEW-2026-09-04), app.py must populate server.EXPECTED_HOSTS
        before binding so the CSRF Origin check is anchored to a real
        allowlist instead of failing closed (empty set) — the local
        flow works WITH the allowlist, not without it."""
        with open("app.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "populate_expected_hosts" in src, (
            "app.py must call server.populate_expected_hosts(host, port) "
            "in _start_server so the fail-closed CSRF check has a real "
            "allowlist"
        )

    def test_app_uses_bounded_http_server(self):
        """Sprint B (REVIEW-2026-09-04) #8: the PyWebView path must use
        server._BoundedThreadingHTTPServer (not a bare ThreadingHTTPServer)
        so the concurrent-thread cap and bounded submit queue apply here
        too."""
        with open("app.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "_BoundedThreadingHTTPServer" in src, (
            "app.py must build the server via "
            "server._BoundedThreadingHTTPServer"
        )
        # Bare construction (not prefixed with `_Bounded` / `server._Bounded`)
        # would bypass the thread cap. Negative lookbehind excludes the
        # qualified `_BoundedThreadingHTTPServer(` occurrences.
        assert not re.search(r"(?<![\w.])ThreadingHTTPServer\(", src), (
            "bare ThreadingHTTPServer would bypass the thread cap"
        )
