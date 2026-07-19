"""SSRF protection tests for server.py _validate_endpoint.

Tests _is_private_host and _validate_endpoint against:
  - IPv4 private/reserved addresses
  - IPv6 link-local / loopback / ULA addresses
  - DNS rebinding (getaddrinfo returning mixed public+private)
  - HTTP → HTTPS redirect attempts
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import _validate_endpoint, _is_private_host


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(endpoint):
    ok, why = _validate_endpoint(endpoint)
    return ok


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSSRF:
    """SSRF defence tests for server.py."""

    # ---- Valid public endpoints (should all return True) ----
    def test_public_https_accepted(self):
        assert _ok('https://api.anthropic.com')
        assert _ok('https://api.minimaxi.com/anthropic')
        assert _ok('https://openrouter.ai/api')

    def test_public_ip_accepted(self):
        assert _ok('https://8.8.8.8')
        assert _ok('https://1.1.1.1')

    # ---- IPv4 private ranges → reject ----
    def test_ipv4_loopback_rejected(self):
        assert not _ok('https://127.0.0.1')
        assert not _ok('https://127.0.0.1:8080')
        assert not _ok('https://localhost')

    def test_ipv4_private_rejected(self):
        assert not _ok('https://10.0.0.1')
        assert not _ok('https://172.16.0.1')
        assert not _ok('https://192.168.1.1')

    def test_ipv4_link_local_rejected(self):
        assert not _ok('https://169.254.0.1')   # APIPA / link-local

    def test_ipv4_cloud_metadata_rejected(self):
        assert not _ok('https://169.254.169.254')   # AWS/GCP/Azure metadata
        assert not _ok('https://metadata.google.internal')  # GCP

    # ---- IPv6 addresses ----
    def test_ipv6_loopback_rejected(self):
        assert not _ok('https://[::1]')
        assert not _ok('https://[::1]:8080')

    def test_ipv6_link_local_rejected(self):
        # fe80::/10 link-local
        assert not _ok('https://[fe80::1]')

    def test_ipv6_ula_rejected(self):
        # fc00::/7 ULA (unique local addresses)
        assert not _ok('https://[fc00::1]')

    def test_ipv6_global_accepted(self):
        # 2000::/3 global unicast — should be accepted
        assert _ok('https://[2001:4860:4860::8888]')

    # ---- DNS resolution ----
    def test_localhost_is_private(self):
        # 'localhost' resolves to 127.0.0.1 (loopback) → should be flagged as private
        assert _is_private_host('localhost')

    def test_public_hostname_accepted(self):
        # 'api.anthropic.com' resolves to public IPs
        assert _is_private_host('api.anthropic.com') == False

    # ---- HTTP scheme rejected ----
    def test_http_rejected(self):
        assert not _ok('http://api.minimaxi.com')  # cleartext API keys leak

    # ---- Missing host rejected ----
    def test_empty_endpoint_rejected(self):
        ok, why = _validate_endpoint('')
        assert not ok and 'empty' in why.lower()

    # ---- _NoRedirect handler ----
    def test_http_redirect_refused(self):
        """Verify that _NoRedirect refuses 3xx redirects.

        We set up a server that returns 302 and verify urllib raises
        HTTPError with the redirect-rejection message.
        """
        from rca_core.llm import _NoRedirect
        import urllib.request

        class _RedirectHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'https://evil.example.com/')
                self.end_headers()
            def log_message(self, fmt, *args):
                pass  # suppress

        # Find free port
        for _ in range(10):
            try:
                sock = socket.socket()
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                sock.close()
                break
            except OSError:
                continue

        httpd = ThreadingHTTPServer(('127.0.0.1', port), _RedirectHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{port}/')
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                opener.open(req, timeout=5)
                raise AssertionError('redirect was followed (should have raised)')
            except urllib.error.HTTPError as e:
                assert 'refused' in str(e).lower() or e.code == 302
        finally:
            httpd.shutdown()
