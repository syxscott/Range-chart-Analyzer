"""/health is what makes the single-instance probe meaningful, and it was untested.

Enumerating server.py's routes and grepping the whole test corpus for each
found one with no coverage: GET /health. It is the only other public endpoint
besides /api/extract, and it exists for a specific, documented reason.
server.py:1759:

    FIX-2026-09-22: /health gives instance-probes (app.py _instance_answers)
    a documented, side-effect-free endpoint - the probe used to count ANY
    complete HTTP response (even a 404) as "alive" because there was nothing
    real to hit.

app.py:222 then does the other half of that contract, and the half that matters:

    for path in ("/health", "/"):
        ...
        if r.status is not None:
            return True

Any status counts, including 404. So the fix is entirely dependent on /health
existing and answering -- the fallback `GET /` will happily return 404 from any
unrelated service on a recycled port and be reported as "the instance is still
serving". app.py's docstring is explicit that this is intentional ("we are only
corroborating 'the recorded instance is still serving', not authenticating it"),
so the response is not to check the status; it is to pin that /health is there.

Nothing did. `_instance_answers` is monkeypatched in every test that touches it
(tests/test_app_main_hardening_2026_09_20.py:139, :148, :161), so the real
two-stage probe never ran, and no test ever requested /health. If the route were
renamed, or its `rstrip("/")` match broke, the single-instance guard -- the thing
that stops two app instances fighting over the lock file and the SQLite DB --
would degrade silently to "any HTTP response counts".
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import app as app_mod  # noqa: E402
import server as srv  # noqa: E402


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start():
    from http.server import ThreadingHTTPServer
    port = _free_port()
    saved_hosts = set(srv.EXPECTED_HOSTS)
    srv.EXPECTED_HOSTS.clear()
    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")

    class _Quiet(srv.Handler):
        def log_message(self, *a, **k):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Quiet)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/index.html", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    return base, port, httpd, t, saved_hosts


def _stop(httpd, t, saved_hosts):
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=2)
    srv.EXPECTED_HOSTS.clear()
    srv.EXPECTED_HOSTS.update(saved_hosts)


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def test_health_answers_200_with_ok_true():
    """The endpoint the whole probe contract rests on."""
    base, port, httpd, t, hosts = _start()
    try:
        status, body = _get(base, "/health")
        assert status == 200, f"/health returned {status}: {body[:200]}"
        payload = json.loads(body)
        assert payload.get("ok") is True, payload
    finally:
        _stop(httpd, t, hosts)


@pytest.mark.parametrize("path", ["/health", "/health/"])
def test_health_tolerates_a_trailing_slash(path):
    """The route matches on `urlparse(self.path).path.rstrip("/")`, so both
    spellings must work -- and nothing tested either, so the rstrip could have
    been dropped and every probe would have fallen through to `GET /`."""
    base, port, httpd, t, hosts = _start()
    try:
        status, body = _get(base, path)
        assert status == 200, f"{path} returned {status}: {body[:200]}"
        assert json.loads(body).get("ok") is True
    finally:
        _stop(httpd, t, hosts)


def test_health_needs_no_token_and_no_origin():
    """It is hit by a bare instance probe, before any CSRF token exists."""
    base, port, httpd, t, hosts = _start()
    try:
        status, body = _get(base, "/health")
        assert status == 200, f"a bare GET must work; got {status}"
    finally:
        _stop(httpd, t, hosts)


def test_the_real_instance_probe_agrees_with_a_live_server():
    """`_instance_answers` is monkeypatched in every existing test, so the real
    two-stage probe has never run. Run it for real, against a real server."""
    base, port, httpd, t, hosts = _start()
    try:
        assert app_mod._instance_answers("127.0.0.1", port, timeout=5.0) is True, (
            "the probe must report a live server as alive -- this is the "
            "guard that stops a second instance from taking the lock"
        )
    finally:
        _stop(httpd, t, hosts)


def test_the_real_instance_probe_reports_a_closed_port_as_dead():
    base, port, httpd, t, hosts = _start()
    _stop(httpd, t, hosts)          # server is gone; the port is now free
    assert app_mod._instance_answers("127.0.0.1", port, timeout=1.0) is False, (
        "a port with nothing behind it must not be reported as a live instance"
    )


def test_health_is_the_only_documented_endpoint_the_probe_needs():
    """Pins the assumption in app.py's docstring: the FIRST probe path has to be
    a real endpoint, because the fallback accepts any status at all. If /health
    ever 404s the probe silently degrades, and that is invisible to every
    other test in the repo."""
    src = open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8").read()
    i = src.index("def _instance_answers(")
    body = src[i:src.index("\ndef ", i + 1)]
    paths = [p for p in ('"/health"', '"/"') if p in body]
    assert paths[0] == '"/health"', (
        f"_instance_answers probes {paths} in that order; the first one must be "
        "the real endpoint, because the loop treats ANY status as alive"
    )
    assert "r.status is not None" in body, (
        "the probe accepts any status; that is intentional per its docstring, "
        "and it is exactly why /health has to exist"
    )
