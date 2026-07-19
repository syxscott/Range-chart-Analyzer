"""T2: tests for server.py routing + safety guards (in-process thread)."""
from __future__ import annotations

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


def _post(base, path, data, headers=None):
    h = headers or {"Content-Type": "application/json"}
    # Default X-Requested-With so requests pass the server's CSRF/origin
    # check (the real frontend always sends it). Tests that want to assert
    # on the cross-origin rejection path can override it.
    h.setdefault("X-Requested-With", "XMLHttpRequest")
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
            _get(base, "/no-such-file.html")
            check("404-missing", False)
        except urllib.error.HTTPError as e:
            check("404-missing", e.code == 404)
    finally:
        _stop(httpd, t)


def test_get_to_api_extract_404():
    base, httpd, t = _start()
    try:
        try:
            _get(base, "/api/extract")
            check("get-api-404", False)
        except urllib.error.HTTPError as e:
            check("get-api-404", e.code == 404)
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
    """Use a real 60 MB PNG file (in whitelist) so the size guard fires."""
    huge = os.path.join(ROOT, "_huge.png")
    try:
        with open(huge, "wb") as f:
            # Header + padding to 60 MB (sparse file is fine — getsize
            # reports real size).
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * (60 * 1024 * 1024 - 8))
        base, httpd, t = _start()
        try:
            try:
                _get(base, "/_huge.png")
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
