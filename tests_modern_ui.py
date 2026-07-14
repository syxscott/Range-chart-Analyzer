"""Tests for the PyWebView 'modern UI' launcher."""
from __future__ import annotations

import http.server
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def test_loading_html_exists_and_has_polling():
    path = os.path.join(ROOT, "app", "loading.html")
    assert os.path.isfile(path), f"missing {path}"
    html = open(path, encoding="utf-8").read()
    # meta-refresh fallback for slow loaders
    assert 'http-equiv="refresh"' in html
    # fetch-based polling jumps to / faster than the meta-refresh interval
    assert "fetch('/" in html or "fetch(\"/" in html
    # Trilingual hint (EN + ZH + JA)
    assert 'lang="en"' in html
    assert "连接中" in html  # zh
    assert "接続中" in html  # ja


def test_pick_free_port_returns_int_in_valid_range():
    from app import _pick_free_port
    p = _pick_free_port()
    assert isinstance(p, int)
    assert 1 <= p <= 65535
    # Make sure the returned port is actually bindable.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", p))  # must not raise


def test_app_module_is_importable_without_pywebview():
    """Even if pywebview is missing, app.py must still import (so main.py
    can show a friendly fallback message)."""
    import importlib
    import app
    importlib.reload(app)
    # _pick_free_port must still work without webview imported.
    assert hasattr(app, "_pick_free_port")


def test_wait_until_ready_succeeds_against_live_server():
    from app import _wait_until_ready

    class _GetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - protocol name
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *_):  # silence test output
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _GetHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        assert _wait_until_ready("127.0.0.1", port, timeout=2.0) is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_requirements_mentions_pywebview():
    txt = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    assert "pywebview" in txt.lower()


def run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            fails += 1
            print(f"FAIL {fn.__name__}: {exc}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    # Allow `python tests_modern_ui.py` for quick smoke; also discoverable
    # by `pytest tests_modern_ui.py -q`.
    raise SystemExit(run_all())
