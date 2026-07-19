"""CSRF protection tests for server.py.

Covers the server's Origin/Referer/X-Requested-With CSRF defence.
Tests run against a live ThreadingHTTPServer on a random free port.
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server import Handler


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
    httpd = ThreadingHTTPServer(('127.0.0.1', port), _TestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f'http://127.0.0.1:{port}'
    return base, httpd.shutdown


def _post(url, headers=None, body='{"image_b64":"QUFB"}'):
    req = Request(url, data=body.encode('utf-8'),
                  headers={'Content-Type': 'application/json', **(headers or {})},
                  method='POST')
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


class TestCSRF:

    def setup_method(self):
        self.base, self._stop = _start_server()
        self.host = f'127.0.0.1:{self.base.split(":")[2]}'

    def teardown_method(self):
        self._stop()

    # 1. Origin matching Host → 200 (not 403)
    def test_same_origin_origin_accepted(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Origin': self.base, 'Host': self.host},
        )
        assert status != 403, f'same-origin rejected: {body}'

    # 2. Origin mismatched → 403
    def test_cross_origin_origin_rejected(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Origin': 'https://evil.example.com', 'Host': self.host},
        )
        assert status == 403, f'cross-origin Origin not 403: {body}'

    # 3. Referer matching Host → 200
    def test_same_origin_referer_accepted(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Referer': f'{self.base}/', 'Host': self.host},
        )
        assert status != 403, f'same-origin Referer rejected: {body}'

    # 4. Referer mismatched → 403
    def test_cross_origin_referer_rejected(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'Referer': 'https://evil.example.com/', 'Host': self.host},
        )
        assert status == 403, f'cross-origin Referer not 403: {body}'

    # 5. No Origin/Referer, no XRW → 403
    def test_bare_request_rejected(self):
        status, body = _post(f'{self.base}/api/extract', headers={})
        assert status == 403, f'bare POST not 403: {body}'

    # 6. XRW without Origin/Referer — connection IS from localhost so accepted
    #    (client_address IS 127.0.0.1 even though Host header is set differently)
    def test_xrw_from_localhost_accepted(self):
        status, body = _post(
            f'{self.base}/api/extract',
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        # localhost connection is trusted even without Origin/Referer
        assert status != 403, f'localhost XRW rejected: {body}'

    # 7. OPTIONS → 501 (no do_OPTIONS defined on server.py Handler)
    def test_options_not_found(self):
        req = Request(f'{self.base}/api/extract', method='OPTIONS',
                      headers={'Origin': self.base, 'Host': self.host})
        try:
            with urlopen(req, timeout=5) as r:
                status = r.status
        except HTTPError as e:
            status = e.code
        assert status == 501

    # 8. Wrong Content-Type → 415
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
