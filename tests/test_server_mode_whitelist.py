"""Regression tests for server.py mode whitelist (REVIEW-2026-08-17).

P0-1: chemical_stratigraphy / paleomap / scatter_plot were silently
rewritten to range_chart by the mode whitelist (server.py:858). The
extractor added these three modes but the whitelist was not synced, so
the server extracted them with the WRONG prompt, normalize, and merge
schema while returning ok=True. Same bug pattern that previously hit
phylogenetic_tree (see the M-1 fix comment in server.py:855-857).

The expected behavior is: when the client requests a mode that is not
fully wired through the server stack, the server returns a clear 400
rather than silently substituting a different mode.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server
from server import EXPECTED_HOSTS, Handler


def _find_free_port():
    for _ in range(10):
        try:
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', 0))
            port = sock.getnameinfo()[1] if hasattr(sock, 'getnameinfo') else sock.getsockname()[1]
            sock.close()
            return port
        except OSError:
            continue
    raise RuntimeError("Could not find a free port")


def _start_server_in_thread():
    port = _find_free_port()
    EXPECTED_HOSTS.clear()
    EXPECTED_HOSTS.add(f'127.0.0.1:{port}')
    EXPECTED_HOSTS.add(f'localhost:{port}')
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _post_extract(port: int, payload: dict) -> tuple[int, dict]:
    base = f"http://127.0.0.1:{port}"
    # 1. Mint a (csrf, session) pair via GET.
    with urlopen(base + "/api/extract", timeout=5) as r:
        csrf_body = json.loads(r.read().decode("utf-8"))
    csrf = csrf_body.get("csrf_token", "")
    session = csrf_body.get("session_token", "")
    # 2. POST with the tokens + Origin matching EXPECTED_HOSTS.
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{base}/api/extract",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "X-Session-Token": session,
            "Origin": base,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _bad_payload(mode: str) -> dict:
    return {
        "image_b64": "aGVsbG8=",
        "media_type": "image/png",
        "mode": mode,
        "provider": {
            "endpoint": "http://127.0.0.1:1",
            "api_key": "x",
            "model": "m",
        },
    }


def test_chemical_stratigraphy_not_silently_rewritten_to_range_chart():
    """chemical_stratigraphy request must NOT be silently rewritten.

    Acceptable: 200 (mode fully wired) or 400 (clear rejection).
    NOT acceptable: silent rewrite that succeeds with range_chart data.
    """
    httpd, port = _start_server_in_thread()
    try:
        status, body = _post_extract(port, _bad_payload("chemical_stratigraphy"))
        assert status in (200, 400), f"unexpected status {status}: {body}"
        if status == 200:
            assert "species_ranges" not in (body.get("data") or {}), (
                "chemical_stratigraphy request produced range_chart-shaped data; "
                "the mode whitelist silently rewrote it"
            )
    finally:
        httpd.shutdown()


def test_paleomap_not_silently_rewritten_to_range_chart():
    httpd, port = _start_server_in_thread()
    try:
        status, body = _post_extract(port, _bad_payload("paleomap"))
        assert status in (200, 400), f"unexpected status {status}: {body}"
        if status == 200:
            assert "species_ranges" not in (body.get("data") or {})
    finally:
        httpd.shutdown()


def test_scatter_plot_not_silently_rewritten_to_range_chart():
    httpd, port = _start_server_in_thread()
    try:
        status, body = _post_extract(port, _bad_payload("scatter_plot"))
        assert status in (200, 400), f"unexpected status {status}: {body}"
        if status == 200:
            assert "species_ranges" not in (body.get("data") or {})
    finally:
        httpd.shutdown()


def test_unknown_mode_returns_400():
    """Genuinely unknown modes must NOT be silently rewritten to range_chart."""
    httpd, port = _start_server_in_thread()
    try:
        status, body = _post_extract(port, _bad_payload("totally_made_up_mode"))
        assert status == 400, f"unknown mode must 400, got {status}: {body}"
    finally:
        httpd.shutdown()
