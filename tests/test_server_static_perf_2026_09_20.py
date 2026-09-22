"""FE-BORROW-2026-09-20 (域P) — static serving + load-perf regressions.

Covers the contract the "static serving & load performance" round introduced in
``server.py`` / ``index.html``:

  1. ``Handler.protocol_version = "HTTP/1.1"`` — connection reuse. Measured, not
     assumed: two GETs (and a GET after a 304, and a GET after a HEAD) run over
     ONE ``http.client`` connection and the socket fileno is proven identical, so
     no reconnect happened behind our back.
  2. Every response is length-framed, because a keep-alive response without
     ``Content-Length`` hangs the client until the inactivity timeout. Each
     response family is asserted explicitly (200 static, 200 JSON, 403/404,
     405-for-HEAD, 501-for-unknown-method, 304-by-definition bodyless).
  3. A POST answered *before* its body was read must close the connection, or
     the unread bytes would be parsed as the next pipelined request.
  4. ETag + ``Cache-Control: no-cache`` + ``If-None-Match`` → 304 (strong tag
     from mtime_ns+size, weak/list matching, and a fresh tag after an edit).
  5. ``do_HEAD`` mirrors the GET headers with no body.
  6. The newly whitelisted frontend asset paths pass the gate (404 when the file
     is still being authored in parallel, never 403) while non-whitelisted paths
     keep answering 403.
  7. The frontend half: every script tag is ``defer``ed in dependency order, the
     split-out css/js of this round are pre-hung, the Worker is not, and every
     ``[data-i18n]`` element carries static default text so the first paint has
     no empty-label flicker.

Run just this file with:
    python -m pytest tests/test_server_static_perf_2026_09_20.py -q
"""
from __future__ import annotations

import http.client
import glob
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server as srv  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------

class _QuietHandler(srv.Handler):
    """Same handler (so protocol_version/_serve_static are the real ones),
    just without one stderr line per request polluting the pytest output."""

    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    """Module-level rate history is shared across test files; start/finish
    clean so a burst of conditional GETs here never starves another file."""
    saved = srv._rate_history
    srv._rate_history = {}
    try:
        yield
    finally:
        srv._rate_history = saved


# FE-FIX-2026-09-22 (server cleanup finding 3): the PID-suffixed scratch
# css this module writes into assets/ (test_editing_a_file_changes_the_etag)
# already has a per-test try/finally, but a finally cannot run when the
# process dies hard mid-run — an orphan assets/_fe_p_perf_<pid>_<hex>.css
# was observed on disk after such a kill. This module-scoped sweep closes
# the gap: it runs at setup (reclaiming leftovers from an earlier crashed
# run) and again at module teardown, so a completed run never leaves the
# artifact behind. Only this module's exact naming pattern is touched.
_PERF_ARTIFACT_GLOB = os.path.join(ROOT, "assets", "_fe_p_perf_*.css")


def _sweep_perf_artifacts():
    for victim in glob.glob(_PERF_ARTIFACT_GLOB):
        try:
            os.remove(victim)
        except OSError:
            pass


@pytest.fixture(autouse=True, scope="module")
def _sweep_test_artifacts():
    _sweep_perf_artifacts()
    try:
        yield
    finally:
        _sweep_perf_artifacts()


@pytest.fixture()
def server():
    """A real bounded server on a free loopback port."""
    saved_hosts = set(srv.EXPECTED_HOSTS)
    srv.EXPECTED_HOSTS.clear()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")

    httpd = srv._BoundedThreadingHTTPServer(("127.0.0.1", port), _QuietHandler,
                                            max_workers=8)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    # Readiness probe (urllib always sends Connection: close, so it cannot
    # wedge on the very thing these tests measure).
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
        httpd.server_close()
        t.join(timeout=3)
        srv.EXPECTED_HOSTS.clear()
        srv.EXPECTED_HOSTS.update(saved_hosts)


@contextmanager
def _conn(port, timeout=15):
    """One HTTP/1.1 connection.

    ``http.client.HTTPConnection`` is not a context manager on 3.13 (the
    protocol was removed), and closing it explicitly matters here: the whole
    point of these tests is that the *client* decides when the socket dies.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


def _get(conn, path, headers=None):
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    return resp, body


def _fileno(conn):
    """The live socket fd, or None when http.client already dropped it.

    A server that closes the connection makes ``HTTPConnection.sock`` go to
    None (and the next request transparently reconnects), so comparing this
    across requests is what actually proves reuse.
    """
    sock = getattr(conn, "sock", None)
    if sock is None:
        return None
    try:
        return sock.fileno()
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# 1 + 2. HTTP/1.1 keep-alive and length framing
# --------------------------------------------------------------------------

def test_handler_announces_http_11():
    assert srv.Handler.protocol_version == "HTTP/1.1"


def test_two_static_gets_reuse_one_connection(server):
    """The headline claim: one socket, two responses, no reconnect."""
    port = server
    with _conn(port) as conn:
        r1, b1 = _get(conn, "/index.html")
        fd1 = _fileno(conn)
        r2, b2 = _get(conn, "/css/style.css")
        fd2 = _fileno(conn)

    assert r1.version == 11, "server answered an older protocol than 1.1"
    assert (r1.status, r2.status) == (200, 200)
    assert b1 and b2
    assert fd1 is not None and fd1 == fd2, (
        "second request did not run on the first request's socket "
        f"({fd1} -> {fd2}): keep-alive is not actually working")
    # http.client only keeps a connection when the response said it may.
    assert r1.will_close is False and r2.will_close is False
    assert (r1.getheader("Connection") or "").lower() != "close"
    assert (r2.getheader("Connection") or "").lower() != "close"


def test_every_response_family_declares_its_length(server):
    """Audit of every framing exit point (the keep-alive hang risk).

    A 1.1 response with neither Content-Length nor chunked framing is
    "terminated by the peer closing", which wedges the client until the
    60 s inactivity timeout — so each family is asserted here rather than
    trusted to the reader.
    """
    port = server
    cases = [
        ("GET", "/index.html", 200),
        ("GET", "/api/extract", 200),          # CSRF mint -> JSON body
        ("GET", "/server.py", 403),            # whitelist reject (JSON)
        ("GET", "/js/definitely-not-here.js", 404),
        ("HEAD", "/css/style.css", 200),
        ("HEAD", "/api/extract", 405),
    ]
    for method, path, want in cases:
        with _conn(port) as conn:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
        assert resp.status == want, f"{method} {path}: {resp.status} != {want}"
        clen = resp.getheader("Content-Length")
        assert clen is not None, f"{method} {path} has no Content-Length"
        if method == "HEAD":
            assert body == b"", f"{method} {path} sent a body"
        else:
            assert len(body) == int(clen), (
                f"{method} {path}: body {len(body)} != Content-Length {clen}")

    # Unknown methods are the stdlib's send_error path: it must still be
    # length-framed and must close rather than sit on a reused socket.
    with _conn(port) as conn:
        conn.request("DELETE", "/index.html")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 501
        assert resp.getheader("Content-Length") is not None
        assert resp.will_close is True, "501 must not leave a keep-alive socket"


def test_http10_client_still_gets_a_framed_response(server):
    """The upgrade must not strand a plain HTTP/1.0 client."""
    with socket.create_connection(("127.0.0.1", server), timeout=10) as sock:
        sock.sendall(b"GET /index.html HTTP/1.0\r\n\r\n")
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks).split(b"\r\n\r\n", 1)
    assert raw and raw[0].startswith(b"HTTP/1.1 200 OK"), raw[0][:64]
    assert b"Content-Length:" in raw[0]
    assert len(raw[1]) > 100


def test_post_rejected_before_body_read_closes_connection(server):
    """Keep-alive desync guard, and the Windows corollary.

    A 403/415/413 answer sent while the request body is still in the socket
    would leave those bytes to be parsed as the *next* request on a persistent
    connection, so the handler must force ``Connection: close``. Hanging up,
    though, used to abort the connection with our own response still queued on
    Windows (WinError 10053 out of ``_read_status``) - ``Handler.finish`` now
    drains the abandoned body first, which is what the repeated small/mid-size
    posts below check: the client must actually receive the status.
    """
    port = server
    for size in (8, 4096, 200_000):
        payload = b"{}" + b"x" * size
        for _attempt in range(3):  # a race is a race; run it a few times
            with _conn(port) as conn:
                conn.request("POST", "/api/extract", body=payload,
                             headers={"Content-Type": "text/plain",
                                      "Origin": f"http://127.0.0.1:{port}"})
                resp = conn.getresponse()
                body = resp.read()
            assert resp.status == 415, f"body {size}B -> {resp.status}"
            assert (resp.getheader("Connection") or "").lower() == "close"
            assert resp.will_close is True
            assert b"err.badContentType" in body

    # The socket really is gone afterwards (no phantom second request).
    with _conn(port) as conn:
        conn.request("POST", "/api/extract", body=b"{}" * 100,
                     headers={"Content-Type": "text/plain"})
        conn.getresponse().read()
        assert _fileno(conn) is None, (
            "the socket survived an unread request body — the leftovers would "
            "be parsed as a phantom second request")


# --------------------------------------------------------------------------
# 4. ETag / Cache-Control / If-None-Match
# --------------------------------------------------------------------------

def test_static_response_has_strong_etag_and_no_cache(server):
    port = server
    size = os.path.getsize(os.path.join(ROOT, "index.html"))
    with _conn(port) as conn:
        r, body = _get(conn, "/index.html")
    etag = r.getheader("ETag")
    assert r.status == 200 and etag, "no ETag on a static 200"
    assert re.fullmatch(r'"[0-9a-f]{16}"', etag), (
        f"ETag is not a strong 16-hex tag: {etag!r}")
    assert r.getheader("Cache-Control") == "no-cache", (
        "a local tool under active development must revalidate, not cache "
        "blind (and must not use max-age, which serves stale JS after an edit)")
    assert int(r.getheader("Content-Length")) == size == len(body)


def test_if_none_match_returns_304_and_keeps_the_socket(server):
    """The 304 itself, and — the part that breaks silently — that a 304 does
    not end the persistent connection."""
    port = server
    with _conn(port) as conn:
        r1, _ = _get(conn, "/index.html")
        etag = r1.getheader("ETag")
        assert etag
        fd1 = _fileno(conn)

        conn.request("GET", "/index.html", headers={"If-None-Match": etag})
        r2 = conn.getresponse()
        body2 = r2.read()
        fd2 = _fileno(conn)

        # ... and a third request still rides the same connection.
        r3, b3 = _get(conn, "/css/style.css")
        fd3 = _fileno(conn)

    assert r2.status == 304
    assert body2 == b"", "a 304 must not carry a body"
    assert r2.getheader("Content-Length") is None, (
        "RFC 7232 4.1: a 304 describes no representation, do not frame it "
        "with the GET's length")
    assert r2.getheader("ETag") == etag
    assert r2.getheader("Cache-Control") == "no-cache"
    assert r2.will_close is False, "304 must stay on the keep-alive connection"
    assert fd1 is not None and fd2 == fd1 and fd3 == fd1, (
        f"socket changed across the 304: {fd1} -> {fd2} -> {fd3}")
    assert r3.status == 200 and b3


@pytest.mark.parametrize("shape", [
    lambda etag: etag,                      # exact
    lambda etag: "W/" + etag,               # weak client tag vs strong server
    lambda etag: '"0000000000000000", ' + etag,   # candidate list
    lambda etag: "  " + etag + "  ",        # sloppy whitespace
    lambda etag: "*",                       # any
])
def test_if_none_match_forms_all_match(server, shape):
    etag = shape(_etag_of(server, "/js/app.js"))
    with _conn(server) as conn:
        conn.request("GET", "/js/app.js", headers={"If-None-Match": etag})
        resp = conn.getresponse()
        body = resp.read()
    assert resp.status == 304, f"{etag!r} should revalidate, got {resp.status}"
    assert body == b""


def _etag_of(port, path):
    with _conn(port) as conn:
        r, _ = _get(conn, path)
    assert r.status == 200
    return r.getheader("ETag")


def test_stale_etag_gets_the_full_body(server):
    with _conn(server) as conn:
        r, body = _get(conn, "/index.html",
                       headers={"If-None-Match": '"deadbeefdeadbeef"'})
    assert r.status == 200
    assert body.startswith(b"<!DOCTYPE html>") or b"<html" in body


def test_editing_a_file_changes_the_etag(server):
    """The other half of ``no-cache``: a touched file must be re-fetched on the
    very next load, i.e. the tag has to track content, not path."""
    name = f"_fe_p_perf_{os.getpid()}_{uuid.uuid4().hex[:8]}.css"
    target = os.path.join(ROOT, "assets", name)
    try:
        with open(target, "wb") as f:
            f.write(b".a{color:red}\n")
        first = _etag_of(server, "/assets/" + name)

        with _conn(server) as conn:
            conn.request("GET", "/assets/" + name,
                         headers={"If-None-Match": first})
            resp = conn.getresponse()
            resp.read()
        assert resp.status == 304

        time.sleep(0.01)
        with open(target, "ab") as f:
            f.write(b".b{color:blue}\n")

        second = _etag_of(server, "/assets/" + name)
        assert second != first, "ETag ignored a size/mtime change"
        with _conn(server) as conn:
            conn.request("GET", "/assets/" + name,
                         headers={"If-None-Match": first})   # stale tag
            resp = conn.getresponse()
            body = resp.read()
        assert resp.status == 200, "an edited file must not 304 to the old tag"
        assert b".b{color:blue}" in body
        assert resp.getheader("ETag") == second
    finally:
        try:
            os.remove(target)
        except OSError:
            pass


def test_etag_helpers_are_pure_and_stable():
    """Unit guard on the tag derivation itself (mtime_ns + size, 16 hex)."""
    st_a = os.stat(os.path.join(ROOT, "index.html"))
    st_b = os.stat(os.path.join(ROOT, "index.html"))
    assert srv._static_etag(st_a) == srv._static_etag(st_b)
    other = os.stat(os.path.join(ROOT, "css", "style.css"))
    assert srv._static_etag(st_a) != srv._static_etag(other)
    assert re.fullmatch(r'"[0-9a-f]{16}"', srv._static_etag(st_a))
    assert srv._etag_header_matches('"0000000000000000"', '"0000000000000000"')
    assert not srv._etag_header_matches('"other"', '"0000000000000000"')
    assert not srv._etag_header_matches(None, '"0000000000000000"')


# --------------------------------------------------------------------------
# 5. do_HEAD
# --------------------------------------------------------------------------

def test_head_matches_get_headers_and_sends_no_body(server):
    port = server
    with _conn(port) as conn:
        conn.request("HEAD", "/js/app.js")
        head = conn.getresponse()
        head_body = head.read()
        head_fd = _fileno(conn)
        # HEAD must not end the connection either — the browser follows it up
        # with the real GET on the same socket.
        r_get, body = _get(conn, "/js/app.js")
        get_fd = _fileno(conn)
    assert head.status == 200
    assert head_body == b"", "HEAD sent a body"
    assert head.will_close is False, "HEAD must stay on the keep-alive socket"
    assert head_fd is not None and get_fd == head_fd, (
        f"socket changed across the HEAD: {head_fd} -> {get_fd}")
    got = {k: head.getheader(k) for k in
           ("Content-Type", "Content-Length", "ETag", "Cache-Control")}
    want = {k: r_get.getheader(k) for k in
            ("Content-Type", "Content-Length", "ETag", "Cache-Control")}
    assert got == want, f"HEAD/GET header sets diverge: {got} != {want}"
    assert int(head.getheader("Content-Length")) == len(body)


def test_head_honours_if_none_match(server):
    etag = _etag_of(server, "/css/style.css")
    with _conn(server) as conn:
        conn.request("HEAD", "/css/style.css", headers={"If-None-Match": etag})
        resp = conn.getresponse()
        assert resp.read() == b""
    assert resp.status == 304


def test_head_on_api_endpoints_is_405_without_body(server):
    with _conn(server) as conn:
        conn.request("HEAD", "/api/extract")
        resp = conn.getresponse()
        body = resp.read()
    assert resp.status == 405
    assert body == b""
    assert resp.getheader("Content-Length") is not None


# --------------------------------------------------------------------------
# 6. whitelist
# --------------------------------------------------------------------------

NEW_ASSET_PATHS = (
    "css/viz.css",
    "css/table-edit.css",
    "css/app-ux.css",
    "js/viz.js",
    "js/history.js",
    "js/sharpen_worker.js",
)


def test_new_asset_paths_are_registered():
    for rel in NEW_ASSET_PATHS:
        assert rel in srv.STATIC_ALLOWED_ENTRIES or rel.split("/", 1)[0] \
            in srv.STATIC_ALLOWED_ENTRIES, f"{rel} is not whitelisted"
        # Explicitly registered (not just covered by the directory entry), so
        # the page/asset contract is stated in one place.
        assert rel in srv.STATIC_ALLOWED_ENTRIES, f"{rel} missing by name"


def test_new_asset_paths_pass_the_gate(server):
    """404 (still being authored in parallel) is fine, 403 is not.

    The files are created by other agents in this round, so the assertion is
    about the *whitelist decision*, not about their existence.
    """
    for rel in NEW_ASSET_PATHS:
        exists = os.path.isfile(os.path.join(ROOT, rel.replace("/", os.sep)))
        with _conn(server) as conn:
            conn.request("GET", "/" + rel)
            resp = conn.getresponse()
            resp.read()
        if exists:
            assert resp.status == 200, f"{rel} exists but got {resp.status}"
            assert resp.getheader("ETag"), f"{rel} served without an ETag"
            assert resp.getheader("Cache-Control") == "no-cache"
        else:
            assert resp.status == 404, (
                f"{rel} should 404 (not yet created) but got {resp.status}; "
                "403 means the whitelist gate rejected a path index.html "
                "references")


def test_whitelist_still_rejects_sources_and_traversal(server):
    for path in ("/server.py", "/rca_core/extractor.py", "/js/../server.py",
                 "/tests/conftest.py", "/requirements.txt"):
        with _conn(server) as conn:
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
        assert resp.status in (403, 404), f"{path} leaked: {resp.status}"
    # ... and a disallowed extension inside an allowed directory stays 403.
    with open(os.path.join(ROOT, "js", f"_fe_p_tmp_{os.getpid()}.txt"),
              "wb") as f:
        f.write(b"nope\n")
    try:
        with _conn(server) as conn:
            conn.request("GET", "/js/" + f"_fe_p_tmp_{os.getpid()}.txt")
            resp = conn.getresponse()
            resp.read()
        assert resp.status == 403
    finally:
        os.remove(os.path.join(ROOT, "js", f"_fe_p_tmp_{os.getpid()}.txt"))


# --------------------------------------------------------------------------
# 7. index.html load-order / first-paint contract
# --------------------------------------------------------------------------

INDEX = os.path.join(ROOT, "index.html")


def _index_html() -> str:
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def _index_code() -> str:
    """index.html with the HTML comments stripped.

    The comments in this file *name* the assets they deliberately exclude (the
    Worker) and quote markup examples, so tag-level assertions must look at the
    markup only.
    """
    return re.sub(r"<!--.*?-->", "", _index_html(), flags=re.S)


def _i18n_block(lang: str) -> str:
    src = _read_i18n_js()
    marker = "zh: {" if lang == "zh" else f"RCA_I18N.{lang} = {{"
    start = src.find(marker)
    if start < 0:
        return ""
    end = src.find("\n};", start)
    if lang == "zh":
        end = src.find("\n  },", start)
    return src[start:end if end > 0 else None]


def _read_i18n_js() -> str:
    with open(os.path.join(ROOT, "js", "i18n.js"), encoding="utf-8") as f:
        return f.read()


def _i18n_value(block: str, key: str):
    m = re.search(
        r"""['"]%s['"]\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""" %
        re.escape(key), block)
    if not m:
        return None
    raw = m.group(1)[1:-1]
    return raw.encode("utf-8").decode("unicode_escape").encode(
        "latin-1", "ignore").decode("utf-8", "ignore") if "\\u" in raw else raw


def test_all_scripts_are_deferred_and_ordered():
    html = _index_code()
    tags = re.findall(r"<script\b[^>]*>", html)
    assert tags, "no script tags found"
    for tag in tags:
        assert "defer" in tag, f"script is not deferred: {tag}"
        assert "async" not in tag, f"async breaks the load-order contract: {tag}"
    srcs = [re.search(r'src="([^"]+)"', t).group(1) for t in tags
            if 'src="' in t]
    # The dependency graph: the data layer must exist before table/export, and
    # app.js (which calls init() as soon as it runs) must be last.
    assert srcs.index("js/reason-codes.js") < srcs.index("js/aggregate.js")
    assert srcs.index("js/aggregate.js") < srcs.index("js/quality.js")
    assert srcs.index("js/theme.js") < srcs.index("js/app.js")
    assert srcs[-1] == "js/app.js", srcs
    for rel in ("js/viz.js", "js/history.js"):
        assert rel in srcs, f"{rel} is not pre-hung on the page"
        assert srcs.index(rel) < srcs.index("js/app.js")


def test_new_stylesheets_are_linked_and_worker_is_not():
    html = _index_code()
    hrefs = re.findall(r'<link\b[^>]*href="([^"]+)"', html)
    for rel in ("css/style.css", "css/viz.css", "css/table-edit.css",
                "css/app-ux.css"):
        assert rel in hrefs, f"{rel} is not linked"
    # The new sheets come after the base one so they win the cascade.
    assert hrefs.index("css/style.css") < hrefs.index("css/viz.css")
    assert "js/sharpen_worker.js" not in html, (
        "the Worker is fetched by new Worker(), never by the page")


def test_every_referenced_asset_is_whitelisted():
    """index.html <-> server.py contract: nothing the page asks for may 403."""
    html = _index_code()
    refs = re.findall(r'<(?:script|link)\b[^>]*?(?:src|href)="([^"]+)"', html)
    assert refs
    for ref in refs:
        if ref.startswith(("http:", "https:", "data:", "#")):
            continue
        rel = ref.lstrip("/")
        first = rel.split("/", 1)[0]
        assert first in srv.STATIC_ALLOWED_ENTRIES or \
            rel in srv.STATIC_ALLOWED_ENTRIES, (
            f"index.html references {ref!r} which the static whitelist blocks")


def test_data_i18n_elements_have_static_default_text():
    """First-paint flicker guard: no [data-i18n] element may be empty, and the
    default text must be a real runtime string (the zh default locale or
    English, per js/i18n.js) so the layout does not jump when i18n.js rewrites
    ``textContent``.

    Matching either locale is deliberate: the markup is only a placeholder to
    hold the layout, ``rcaApplyI18n`` overwrites it either way, and a few
    pre-existing labels were authored in English.
    """
    html = _index_code()
    zh, en = _i18n_block("zh"), _i18n_block("en")
    empty, mismatched = [], []
    for m in re.finditer(
            r'<([a-zA-Z]+)\b[^>]*data-i18n="([^"]+)"[^>]*>([^<]*)<', html):
        tag, key, inner = m.group(1), m.group(2), m.group(3)
        if not inner.strip():
            empty.append(f"<{tag}> {key}")
            continue
        candidates = [c for c in (_i18n_value(zh, key), _i18n_value(en, key))
                      if c]
        if candidates and not any(inner.strip() == c.strip()
                                  for c in candidates):
            mismatched.append(f"{key}: {inner.strip()!r} not in {candidates!r}")
    assert not empty, "empty data-i18n labels flicker on load: " + ", ".join(empty)
    assert not mismatched, (
        "static default text drifted from js/i18n.js: " + "; ".join(mismatched))


def test_app_init_hook_survives_defer():
    """app.js must still self-init when it arrives deferred (readyState is
    'interactive', not 'loading'). Read-only guard on the assumption域P relies
    on: it handles both branches, so defer cannot leave the page uninitialised.
    """
    src = open(os.path.join(ROOT, "js", "app.js"), encoding="utf-8").read()
    assert "readyState" in src and "DOMContentLoaded" in src, (
        "app.js no longer guards document.readyState — deferred loading may "
        "now skip init()")
