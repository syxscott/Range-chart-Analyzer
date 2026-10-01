"""AUDIT-2026-10-01 [item 28]: a proxied plain-HTTP request had its Host header
overwritten with the PROXY's own address.

``_PinnedHTTPSHandler.https_open`` skips the DNS pin when the request is
proxied -- ``req._tunnel_host`` is set, the stock handler runs the CONNECT
tunnel, and the endpoint is resolved by the proxy, so there is nothing to pin
(AUDIT-2026-09-27 item 5.2; before that fix every https call went DIRECT and
came back HTTP 418 from a transparent filter).

``_PinnedHTTPHandler.http_open`` -- added later, REVIEW-2026-09-20 item 17 --
called ``_pin(req, "http")`` unconditionally.  It cannot reuse the https guard,
because ``Request.set_proxy`` only sets ``_tunnel_host`` for https:

    https://target.example/v1/chat
      -> host='127.0.0.1:7890'  _tunnel_host='target.example'  selector='/v1/chat'
    http://target.example:11434/v1/chat
      -> host='127.0.0.1:7890'  _tunnel_host=None   selector='http://...'

So on a proxied http request ``req.host`` was ALREADY the proxy, and ``_pin``
resolved it, pinned it, then ran
``req.add_unredirected_header("Host", <the proxy's name>)``.  Measured end to
end against a real origin and a real forward proxy, the proxy received:

    requestline = http://origin.invalid:PORT/x     <- correct
    Host        = 127.0.0.1:<proxy port>           <- the PROXY, not the target

The request-line still named the target, so the proxy reached the right place,
but HTTP virtual hosting keys off the Host header -- the same failure shape as
the 418, on the cleartext path.

WHAT IS ASSERTED, AND WHY IT IS NOT THE OBVIOUS THING
-----------------------------------------------------
The first version of this file asserted only the Host header, and proved
nothing about whether pinning happened: an earlier attempt also tried to make
"was it pinned?" observable by patching ``socket.getaddrinfo`` so the fake
hostname resolves.  That patch lands on the socket MODULE, so
``socket.create_connection`` resolves through it too, and pinned and unpinned
became indistinguishable -- the test passed even with the pin deleted.

So the observable here is ``socket.create_connection`` itself: a wrapper
records the address each connection actually dials.  Pinned == the resolved
IP literal; unpinned == the hostname.  The Host header is asserted separately,
because that is the user-visible damage.
"""
from __future__ import annotations

import http.client
import http.server
import os
import socket
import threading
import urllib.request

import pytest

from rca_core import ssrf

_PROXY_ENV = ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")
FAKE_HOST = "origin.invalid"

#: (host, port) each socket.connect actually dialled, in order.
CONNECTS: list[tuple] = []
_ORIGIN_ADDR: tuple[str, int] = ("127.0.0.1", 0)


class _Origin(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802 - stdlib naming
        _Origin.seen.append({"path": self.path,
                             "host": self.headers.get("Host")})
        body = b"ORIGIN"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _Proxy(http.server.BaseHTTPRequestHandler):
    """Forward proxy that relays exactly the Host header it was handed.

    The upstream address is FIXED (``_ORIGIN_ADDR``) rather than resolved from
    the request-line, so the only variable between a broken and a fixed run is
    the Host header.  Resolving the fake name here would turn a Host
    corruption into an unrelated 502 and hide the defect.
    """
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802
        _Proxy.seen.append({"requestline": self.path,
                            "host": self.headers.get("Host")})
        rest = (self.path[len("http://"):]
                if self.path.startswith("http://") else self.path)
        host, _, path = rest.partition("/")
        try:
            conn = http.client.HTTPConnection(_ORIGIN_ADDR[0],
                                               _ORIGIN_ADDR[1], timeout=5)
            conn.request("GET", "/" + path,
                         headers={"Host": self.headers.get("Host") or host})
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:  # noqa: BLE001
            body = ("proxy-error:%s" % exc).encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


@pytest.fixture()
def pair(monkeypatch):
    """Origin + proxy servers, a recorded socket layer, and fake-host DNS."""
    global _ORIGIN_ADDR
    _Origin.seen.clear()
    _Proxy.seen.clear()
    CONNECTS.clear()

    origin, o_port = _start(_Origin)
    _ORIGIN_ADDR = ("127.0.0.1", o_port)
    proxy, p_port = _start(_Proxy)

    real_connect = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def recording_connect(address, *a, **kw):
        CONNECTS.append((address[0], address[1]))
        return real_connect(address, *a, **kw)

    def fake_dns(host, *a, **kw):
        return real_getaddrinfo("127.0.0.1" if host == FAKE_HOST else host,
                                *a, **kw)

    monkeypatch.setattr(ssrf.socket, "create_connection", recording_connect)
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(ssrf, "_ALLOW_PRIVATE", True)

    saved = {k: os.environ.get(k) for k in _PROXY_ENV}
    try:
        yield o_port, p_port
    finally:
        origin.shutdown()
        proxy.shutdown()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _set_proxy(p_port: int | None) -> None:
    if p_port is None:
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "*"
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = ""
    else:
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = ""
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = (
            "http://127.0.0.1:%d" % p_port)


def test_proxied_http_keeps_the_real_host_header(pair):
    """THE BUG: _pin rewrote Host to the proxy's own address."""
    o_port, p_port = pair
    _set_proxy(p_port)
    opener = ssrf.make_pinning_opener()
    url = "http://%s:%d/x" % (FAKE_HOST, o_port)

    with opener.open(url, timeout=10) as resp:
        assert resp.status == 200, resp.read()

    assert _Proxy.seen, "the request never reached the proxy"
    assert _Proxy.seen[0]["requestline"] == url
    assert _Proxy.seen[0]["host"] == "%s:%d" % (FAKE_HOST, o_port), (
        "the proxy was told the target is %r" % _Proxy.seen[0]["host"])
    assert _Origin.seen == [{"path": "/x",
                             "host": "%s:%d" % (FAKE_HOST, o_port)}], (
        "the origin saw a different Host: %r" % _Origin.seen)
    # A proxied request dials the PROXY -- there is no DNS to pin.
    assert CONNECTS and CONNECTS[0][1] == p_port, (
        "expected the connection to the proxy port %d, got %r"
        % (p_port, CONNECTS))


def test_direct_http_is_still_dns_pinned(pair):
    """The fix must not disable pinning on the direct path."""
    o_port, _p_port = pair
    _set_proxy(None)
    opener = ssrf.make_pinning_opener()
    url = "http://%s:%d/y" % (FAKE_HOST, o_port)

    with opener.open(url, timeout=10) as resp:
        assert resp.status == 200, resp.read()

    assert _Origin.seen and _Origin.seen[0]["host"] == "%s:%d" % (FAKE_HOST, o_port)
    # THE pin: the socket dialled the resolved IP, never the hostname.
    assert CONNECTS and CONNECTS[0] == ("127.0.0.1", o_port), (
        "the direct path was not pinned; it dialled %r" % (CONNECTS,))
    assert all(host != FAKE_HOST for host, _p in CONNECTS), (
        "a connection was made to the hostname instead of its IP: %r"
        % (CONNECTS,))


def test_a_query_string_cannot_smuggle_the_pin_off(pair):
    """The proxied-request guard must key on the selector STARTING with a scheme.

    A guard written as ``"://" in req.selector`` is switched off by any direct
    request whose query happens to contain a URL, letting an attacker-chosen
    host skip DNS pinning entirely.
    """
    o_port, _p_port = pair
    _set_proxy(None)
    opener = ssrf.make_pinning_opener()
    # A benign parameter that happens to carry a full URL.
    url = "http://%s:%d/z?next=http://evil.example" % (FAKE_HOST, o_port)

    with opener.open(url, timeout=10) as resp:
        assert resp.status == 200, resp.read()

    assert _Origin.seen, "the request never reached the origin"
    assert CONNECTS and CONNECTS[0] == ("127.0.0.1", o_port), (
        "pinning was switched off by a query string -- the exact bypass the "
        "startswith guard exists to prevent (dialled %r)" % (CONNECTS,))
    assert all("evil.example" not in (host or "") for host, _p in CONNECTS)
