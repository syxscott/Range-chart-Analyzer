"""The quota endpoint must answer, and must not leak.

REVIEW-2026-10-20 (cc-switch's usage query): the browser front-end needs
the plan's remaining quota, so the local server grew a ``GET /api/quota``.

Two properties matter and they pull in opposite directions:

* it has to WORK -- the number it returns is the one the user acts on;
* it must not LEAK -- the answer describes a PAID account, and the server
  can be started with ``--host 0.0.0.0``.

So it carries the same guards the provenance export has (a client header,
loopback-only unless a token is configured, and its own rate bucket), and
every failure mode is an ANSWER rather than an exception: a provider that
publishes no quota, a store that cannot be read, a vendor that is down.

The tests run against the real Handler over a real socket, not against a
mock, because the guards live in the routing code and a mock would test
the mock.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv                                          # noqa: E402


class _QuietHandler(srv.Handler):
    def log_message(self, *args, **kwargs):
        pass


class _QuietServer(srv._BoundedThreadingHTTPServer):
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
        srv.EXPECTED_HOSTS.clear()
        srv.EXPECTED_HOSTS.update(saved_hosts)


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_the_quota_endpoint_needs_the_client_header(server):
    """Without the header it is 403 -- the endpoint describes a paid
    account, and the server can be exposed to the network."""
    status, _ = _get(server, "/api/quota")
    assert status == 403, "the quota endpoint answered without the client header"


def test_the_quota_endpoint_answers_with_the_header(server, monkeypatch):
    import rca_core.llm as llm

    class _Provider:
        name = "MiniMax"
        model = "MiniMax-M3.1-Flash"
        api_key = "sk-test"

    class _Store:
        def load(self):
            return self

        def get_current(self):
            return _Provider()

    class _Tier:
        def __init__(self, name, used):
            self.name = name
            self.used_percent = used
            self.resets_at = None
            self.remaining = None
            self.total = None
            self.unit = "%"

    class _Result:
        ok = True
        tiers = [_Tier("five_hour", 1.0), _Tier("weekly_limit", 18.0)]
        queried_at = 1700000000000
        error = ""
        detail = ""

    monkeypatch.setattr(llm, "ProviderStore", _Store)
    monkeypatch.setattr(llm, "query_coding_plan_quota",
                        lambda provider, timeout_sec=15: _Result())
    status, body = _get(server, "/api/quota",
                        headers={"X-RCA-Client": "range-chart-analyzer"})
    assert status == 200
    data = json.loads(body.decode("utf-8"))
    assert data["ok"] is True
    assert data["provider"] == "MiniMax"
    tiers = {t["name"]: t for t in data["tiers"]}
    # The API reports the USED share; the client shows what is LEFT.
    assert tiers["five_hour"]["used_percent"] == 1.0
    assert tiers["five_hour"]["left_percent"] == 99
    assert tiers["weekly_limit"]["left_percent"] == 82


def test_a_provider_with_no_quota_is_an_answer_not_a_crash(server, monkeypatch):
    import rca_core.llm as llm

    class _Provider:
        name = "Relay"
        model = "m"
        api_key = "sk-test"

    class _Store:
        def load(self):
            return self

        def get_current(self):
            return _Provider()

    class _Result:
        ok = False
        tiers = []
        queried_at = 0
        error = "this provider publishes no quota"
        detail = ""

    monkeypatch.setattr(llm, "ProviderStore", _Store)
    monkeypatch.setattr(llm, "query_coding_plan_quota",
                        lambda provider, timeout_sec=15: _Result())
    status, body = _get(server, "/api/quota",
                        headers={"X-RCA-Client": "range-chart-analyzer"})
    assert status == 200, "a provider with no quota must still answer"
    data = json.loads(body.decode("utf-8"))
    assert data["ok"] is False
    assert data["error"]


def test_no_provider_at_all_is_an_answer_not_a_crash(server, monkeypatch):
    import rca_core.llm as llm

    class _Store:
        def load(self):
            return self

        def get_current(self):
            return None

    monkeypatch.setattr(llm, "ProviderStore", _Store)
    status, body = _get(server, "/api/quota",
                        headers={"X-RCA-Client": "range-chart-analyzer"})
    assert status == 200
    data = json.loads(body.decode("utf-8"))
    assert data["ok"] is False
    assert "provider" in data["error"]


def test_head_is_not_supported(server):
    """HEAD has no representation to describe on this endpoint."""
    req = urllib.request.Request(f"http://127.0.0.1:{server}/api/quota",
                                 method="HEAD",
                                 headers={"X-RCA-Client": "range-chart-analyzer"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 405


def test_the_quota_endpoint_has_its_own_rate_bucket():
    """Every call leaves the machine and spends the account's own limit,
    so it cannot share a bucket with the cheap static assets."""
    assert srv._RATE_MAX_REQUESTS_QUOTA < srv._RATE_MAX_REQUESTS_GET
    assert srv._RATE_MAX_REQUESTS_QUOTA < srv._RATE_MAX_REQUESTS_PROV
