"""CSRF protection tests for server.py.

Covers the server's Origin/Referer/X-Requested-With CSRF defence AND
the new CSRF token flow (the audit finding replaced the previous
single-step "XRW + Origin" check with a 2-step flow: clients must
first GET /api/extract to mint a (csrf_token, session_token) pair,
then POST with both in custom headers).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from server import (
    Handler, EXPECTED_HOSTS,
    _set_csrf_for_session, _get_csrf_for_session,
    _generate_csrf_token,
)


def _find_free_port():
    for _ in range(10):
        try:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
            sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError("Could not find a free port")


class _TestHandler(Handler):
    test_client_ip = None
    def do_POST(self) -> None:
        self.server.client_ip = self.client_address[0]
        super().do_POST()


def _start_server():
    port = _find_free_port()
    # Populate EXPECTED_HOSTS so the CSRF check is anchored to the
    # actual bind address (mirrors server.main()).
    EXPECTED_HOSTS.clear()
    EXPECTED_HOSTS.add(f'127.0.0.1:{port}')
    EXPECTED_HOSTS.add(f'localhost:{port}')
    httpd = ThreadingHTTPServer(('127.0.0.1', port), _TestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f'http://127.0.0.1:{port}'
    return base, httpd.shutdown


def _fetch_csrf(base):
    """GET /api/extract to mint a (csrf, session) token pair."""
    with urlopen(base + '/api/extract', timeout=5) as r:
        body = json.loads(r.read().decode('utf-8'))
    return body['csrf_token'], body['session_token']


def _post(url, headers=None, body='{"image_b64":"QUFB"}'):
    """POST with auto-fetched CSRF tokens.

    The CSRF layer requires X-CSRF-Token + X-Session-Token (in addition
    to Origin/XRW), so we always mint a fresh pair unless the caller
    explicitly overrides those headers (for negative-path tests).
    """
    csrf, session = _fetch_csrf(_base_of(url))
    base_headers = {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
        'X-Session-Token': session,
    }
    base_headers.update(headers or {})
    req = Request(url, data=body.encode('utf-8'),
                  headers=base_headers, method='POST')
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def _base_of(url: str) -> str:
    """Return ``http://host:port`` from any URL on this server."""
    # urls look like http://127.0.0.1:12345/api/extract
    parts = url.split('/', 3)
    return '/'.join(parts[:3])


class TestCSRF:

    def setup_method(self):
        self.base, self._stop = _start_server()
        self.host = f'127.0.0.1:{self.base.split(":")[2]}'

    def teardown_method(self):
        self._stop()

    # 1. GET /api/extract now issues a CSRF + session token pair.
    def test_get_api_extract_returns_csrf(self):
        with urlopen(self.base + '/api/extract', timeout=5) as r:
            body = json.loads(r.read().decode('utf-8'))
        assert r.status == 200
        assert body.get('csrf_token'), 'no csrf_token in body'
        assert body.get('session_token'), 'no session_token in body'

    # 2. Origin matching an EXPECTED_HOSTS entry → not 403.
    def test_same_origin_origin_accepted(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Origin': self.base, 'Host': self.host},
        )
        assert status != 403, f'same-origin rejected: {body}'

    # 3. Origin mismatched → 403 (HIGH: Origin compares against fixed
    #    EXPECTED_HOSTS allowlist, not the client-controlled Host header).
    def test_cross_origin_origin_rejected(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Origin': 'https://evil.example.com', 'Host': self.host},
        )
        assert status == 403, f'cross-origin Origin not 403: {body}'

    # 4. Referer matching an EXPECTED_HOSTS entry → not 403.
    def test_same_origin_referer_accepted(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Referer': f'{self.base}/', 'Host': self.host},
        )
        assert status != 403, f'same-origin Referer rejected: {body}'

    # 5. Referer mismatched → 403.
    def test_cross_origin_referer_rejected(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Referer': 'https://evil.example.com/', 'Host': self.host},
        )
        assert status == 403, f'cross-origin Referer not 403: {body}'

    # 6. No Origin/Referer, no XRW → 403.
    def test_bare_request_rejected(self):
        # Need to also explicitly drop CSRF so the test exercises the
        # cross-origin path, not the CSRF-missing path.
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'X-CSRF-Token': '', 'X-Session-Token': ''},
        )
        # Either 403 (cross-origin) or 403 (no CSRF) is acceptable —
        # both are correct rejections.
        assert status == 403, f'bare POST not 403: {body}'

    # 7. XRW without Origin/Referer — connection IS from localhost so
    #    accepted (client_address IS 127.0.0.1).
    def test_xrw_from_localhost_accepted(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        assert status != 403, f'localhost XRW rejected: {body}'

    # 8. Invalid CSRF token → 403.
    def test_invalid_csrf_token_rejected(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={
                'Origin': self.base,
                'X-CSRF-Token': 'wrong-token-value',
                'X-Session-Token': 'wrong-session-value',
            },
        )
        assert status == 403, f'invalid CSRF not 403: {body}'

    # 9. OPTIONS → 501 (no do_OPTIONS defined on server.py Handler).
    def test_options_not_found(self):
        req = Request(f'{self.base}/api/extract', method='OPTIONS',
                      headers={'Origin': self.base, 'Host': self.host})
        try:
            with urlopen(req, timeout=5) as r:
                status = r.status
        except HTTPError as e:
            status = e.code
        assert status == 501

    # 10. Wrong Content-Type → 415.
    def test_wrong_content_type(self):
        req = Request(f'{self.base}/api/extract', data=b'not-json',
                     headers={'Content-Type': 'text/plain',
                              'Origin': self.base, 'Host': self.host},
                     method='POST')
        try:
            with urlopen(req, timeout=5) as r:
                status = r.status
        except HTTPError as e:
            status = e.code
        assert status == 415


def test_csrf_token_expires_after_ttl():
    """CSRF token should expire after TTL."""
    # Temporarily reduce TTL for testing
    original_ttl = server._CSRF_TOKEN_TTL_SEC
    server._CSRF_TOKEN_TTL_SEC = 1  # 1 second

    try:
        session = "test-session-expire"
        csrf = _generate_csrf_token()
        _set_csrf_for_session(session, csrf)

        # Should work immediately
        retrieved = _get_csrf_for_session(session)
        assert retrieved == csrf, f"Expected {csrf}, got {retrieved}"

        # Wait for expiration
        time.sleep(1.5)

        # Should be expired now
        retrieved_after = _get_csrf_for_session(session)
        assert retrieved_after is None, f"Expected None after expiry, got {retrieved_after}"
    finally:
        server._CSRF_TOKEN_TTL_SEC = original_ttl
