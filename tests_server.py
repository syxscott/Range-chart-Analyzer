"""T2: tests for server.py routing + safety guards (in-process thread)."""
from __future__ import annotations

import json
import os
import sys
import socket
import threading
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import server as srv

_pass = 0
_fail = 0


def check(name, cond, msg=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name, msg)


class _Handler(srv.Handler):
    def log_message(self, *args, **kwargs):
        pass


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start():
    from http.server import ThreadingHTTPServer
    port = _free_port()
    # Populate EXPECTED_HOSTS so the CSRF check is anchored to the
    # actual bind address (mirrors server.main()).
    srv.EXPECTED_HOSTS.clear()
    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/index.html", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    return base, httpd, t


def _stop(httpd, t):
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=2)


def _get(base, path):
    return urllib.request.urlopen(base + path, timeout=5)


def _fetch_csrf(base):
    """GET /api/extract to obtain a CSRF + session token pair."""
    with urllib.request.urlopen(base + "/api/extract", timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body["session_token"], body["csrf_token"]


def _post(base, path, data, headers=None):
    """POST with CSRF + session token automatically populated.

    The default ``X-Requested-With`` and ``Origin`` headers let the
    request pass the same-origin guard. Tests that want to assert on
    the cross-origin rejection path can override them.
    """
    session_token, csrf_token = _fetch_csrf(base)
    h = {
        "Content-Type": "application/json",
        "Origin": base,
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrf_token,
        "X-Session-Token": session_token,
        **(headers or {}),
    }
    body = data if isinstance(data, bytes) else data.encode("utf-8")
    req = urllib.request.Request(base + path, data=body, headers=h, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_static_index_served():
    base, httpd, t = _start()
    try:
        r = _get(base, "/index.html")
        body = r.read().decode("utf-8", errors="replace").lower()
        check("static-200", r.status == 200)
        check("static-has-html", "<html" in body or "<!doctype" in body)
        check("static-nosniff", r.headers.get("X-Content-Type-Options") == "nosniff")
    finally:
        _stop(httpd, t)


def test_path_traversal_blocked():
    base, httpd, t = _start()
    try:
        for path in ("/../etc/passwd", "/../../server.py"):
            try:
                _get(base, path)
                check(f"traversal-blocked:{path}", False, "(200)")
            except urllib.error.HTTPError as e:
                check(f"traversal-blocked:{path}", e.code in (403, 404))
    finally:
        _stop(httpd, t)


def test_unknown_extension_blocked():
    base, httpd, t = _start()
    try:
        try:
            _get(base, "/tests_server.py")
            check("py-ext-blocked", False, "(200)")
        except urllib.error.HTTPError as e:
            check("py-ext-blocked", e.code in (403, 404))
    finally:
        _stop(httpd, t)


def test_404_for_missing():
    base, httpd, t = _start()
    try:
        try:
            _get(base, "/js/no-such-file.js")
            check("404-missing", False)
        except urllib.error.HTTPError as e:
            check("404-missing", e.code == 404)
    finally:
        _stop(httpd, t)


def test_get_to_api_extract_returns_csrf():
    """GET /api/extract now issues a CSRF + session token pair.

    The audit finding called out that this endpoint was unauthenticated
    and un-rate-limited; it now both issues tokens and is gated by the
    per-IP sliding window rate limit.
    """
    base, httpd, t = _start()
    try:
        r = _get(base, "/api/extract")
        body = json.loads(r.read().decode("utf-8"))
        check("get-api-200", r.status == 200)
        check("get-api-csrf", bool(body.get("csrf_token")))
        check("get-api-session", bool(body.get("session_token")))
    finally:
        _stop(httpd, t)


def test_post_extract_no_image():
    base, httpd, t = _start()
    try:
        r = _post(base, "/api/extract",
                  '{"image_b64": "", "provider": null}')
        data = r.read().decode("utf-8", errors="replace")
        check("post-no-image-ok-false", '"ok": false' in data)
        check("post-no-image-errkey", 'err.noImage' in data)
    finally:
        _stop(httpd, t)


def test_post_extract_no_key():
    base, httpd, t = _start()
    try:
        r = _post(base, "/api/extract",
                  '{"image_b64": "QUFB", "provider": null}')
        data = r.read().decode("utf-8", errors="replace")
        check("post-no-key-ok-false", '"ok": false' in data)
        check("post-no-key-errkey", 'err.noKey' in data)
    finally:
        _stop(httpd, t)


def test_post_extract_bad_json():
    base, httpd, t = _start()
    try:
        try:
            r = _post(base, "/api/extract", "not json",
                      {"Content-Type": "application/json"})
            check("bad-json-400", False)
        except urllib.error.HTTPError as e:
            check("bad-json-400", e.code == 400)
    finally:
        _stop(httpd, t)


def test_post_extract_wrong_content_type():
    """The CSRF guard rejects non-JSON POSTs with 415 before parsing."""
    base, httpd, t = _start()
    try:
        try:
            _post(base, "/api/extract", "{}", {"Content-Type": "text/plain"})
            check("wrong-ctype-415", False)
        except urllib.error.HTTPError as e:
            check("wrong-ctype-415", e.code == 415)
    finally:
        _stop(httpd, t)


def test_large_file_rejected():
    """Use a real 60 MB PNG file (in an allowed static subdir) so the
    size guard fires. Files outside the allowed-entries whitelist are
    rejected with 403 before the size check; we want to test the size
    guard specifically, so put the huge file in ``assets/``.
    """
    huge_dir = os.path.join(ROOT, "assets")
    huge = os.path.join(huge_dir, "_huge.png")
    try:
        with open(huge, "wb") as f:
            # Header + padding to 60 MB (sparse file is fine — getsize
            # reports real size).
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * (60 * 1024 * 1024 - 8))
        base, httpd, t = _start()
        try:
            try:
                _get(base, "/assets/_huge.png")
                check("large-413", False)
            except urllib.error.HTTPError as e:
                check("large-413", e.code == 413)
        finally:
            _stop(httpd, t)
    finally:
        try:
            os.remove(huge)
        except OSError:
            pass


def run_all():
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as exc:
            fails += 1
            print(f"ERROR {fn.__name__}: {exc}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    rc = run_all()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(rc)
