r"""server.py: paths the filesystem cannot represent escaped do_GET with no response.

Discovered by asking "what can this guard be handed that it has never been
handed?", not by reading it. `_safe_local_path` is the single place server.py
decodes a request path (`unquote(urlparse(url_path).path)` -- one call site in
the whole file), so it is the whole attack surface for path handling.

It segment-matches `..` before handing the path to the filesystem, and then
calls os.path.realpath(). Those two steps disagree about what a valid path is,
and realpath is the one call in the function that touches the OS -- so it, not
the string checks, decides which paths are survivable. It answers a path it
cannot represent by RAISING, and nothing on the way out catches that. Two
shapes do this, and both are reachable from a client:

  embedded NUL   -> ValueError: embedded null character in path
                   "GET /js/a%00b.js" -- only paths whose FIRST segment
                   already passed STATIC_ALLOWED_ENTRIES, so "/%00" alone was
                   always safe and only js/css/assets reached the crash.

  path >= 32730  -> ValueError: path too long for Windows (the \\?\ prefix
                   caps a path at 32767 chars and ROOT spends ~40 of them).
                   This one needs no encoding tricks at all, just a long URL,
                   and CPython's request-line cap is 65536 bytes -- ~32791
                   bytes of headroom, so it is comfortably reachable.

Measured over a real socket, before the fix:

    control  GET /js/definitely_missing.js     -> HTTP 404 {"error": ...}
    control  GET /server.py                    -> HTTP 403 {"error": ...}
    bug      GET /js/a%00b.js                  -> RemoteDisconnected, 0 bytes
    bug      GET /js/<32757 a's>.js            -> RemoteDisconnected, 0 bytes
    control  GET /js/<70000 a's>.js            -> HTTP 414 (stdlib's own cap)
    both     ... plus a ValueError traceback in the server log

So the symptom is not "an attacker reads a file outside ROOT" -- realpath never
returns, so nothing escapes. It is that whole ranges of request paths produce a
connection with no HTTP response at all while every neighbouring unusable path
produces a well-formed one. The server itself survives and keeps serving
(asserted below), so this is a malformed-response defect, not a
worker-exhaustion DoS.

The fix is at the realpath call, not one guard per shape -- and not two.
Enumerating shapes individually is exactly how the second one got shipped
after the first was fixed, so the first attempt at this fix used an explicit
`if "\x00" in rel` and that was measured to be dead code the moment the
try/except landed: withdrawing the explicit check left all 53 tests green.
It has been removed rather than kept as redundant defence, because a guard
that provably does nothing tells a future reader "this line handles NUL", and
inviting them to drop the except that actually does the work would bring the
long-path defect straight back.

What this file asserts is therefore split in two, and the split is not
cosmetic -- the first version of this file asserted only the strict half and
the Linux CI leg proved it wrong:

  * UNIVERSAL (asserted everywhere): _safe_local_path answers rather than
    raising, and whatever it returns is inside ROOT. This is the real contract
    and the original defect violated it on every platform.
  * STRICT (asserted only where a runtime measurement says the platform
    refuses the path at all): the answer is specifically None. NUL qualifies
    everywhere. The over-long shape qualifies only on Windows: posixpath's
    realpath is a userspace lstat loop that swallows the ENAMETOOLONG and
    returns a best-effort path, so on Linux there is nothing to refuse and
    demanding None is simply a wrong test.

The length threshold is measured, never hardcoded. 32730 is what Windows
reported on the machine the bug was found on, and it is a function of how long
ROOT is; a repo checked out under /home/runner/work/... has a different one.
A test that encodes one machine's number is a test that will be red on CI for a
reason that has nothing to do with the code.

server.py:1735 (traversal) is unchanged and remains the first gate.
"""

from __future__ import annotations

import http.client
import os
import socket
import sys
import threading
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import server as srv  # noqa: E402

ROOT_REAL = os.path.realpath(srv.ROOT)


# --- platform calibration, measured not assumed ------------------------
#
# Everything below is sized from what THIS interpreter and OS actually do, not
# from a constant copied off a Windows measurement. That is not fastidiousness:
# the first version of this file hard-coded Windows' 32730 and CI went red on
# Linux, because posixpath.realpath is a userspace lstat loop that swallows the
# ENAMETOOLONG it gets back and returns a best-effort path, while Windows'
# realpath goes through _getfinalpathname and raises ValueError. The defect is
# genuinely Windows-shaped; the CONTRACT ("no exception escapes, nothing
# escapes ROOT") is not, and only the contract may be asserted unconditionally.

def _refuses(n: int) -> bool:
    """Does os.path.realpath raise for a rel of length n, on this platform?"""
    try:
        os.path.realpath(os.path.join(ROOT_REAL, "js", "a" * n + ".js"))
    except (ValueError, OSError):
        return True
    return False


def _measure_break_at() -> int | None:
    """Shortest rel length this platform refuses, or None if it never does."""
    if not _refuses(60000):
        return None
    lo, hi = 100, 60000
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _refuses(mid):
            hi = mid
        else:
            lo = mid
    return hi


LONG_BREAK_AT = _measure_break_at()
# Over the limit: on Windows this is the crashing shape. Sized from the
# measurement so it is past the real boundary on any platform that has one.
LONG_JUST_OVER = (LONG_BREAK_AT + 30) if LONG_BREAK_AT else 60000
# Under the limit, with room to spare -- this one is a real, legal path shape
# everywhere, so "still handled normally" is a claim that can hold on both.
LONG_JUST_UNDER = (LONG_BREAK_AT - 200) if LONG_BREAK_AT else 2000

SKIP_NO_LIMIT = pytest.mark.skipif(
    LONG_BREAK_AT is None,
    reason=(
        "this platform's os.path.realpath never raises for an over-long path "
        "(posixpath swallows ENAMETOOLONG and returns best-effort), so the "
        "over-long shape cannot crash here and the strict assertion below is "
        "not measurable on this platform. The contract that IS asserted "
        "unconditionally -- no exception escapes, nothing escapes ROOT -- is "
        "still covered by test_hostile_path_never_escapes_and_never_raises."
    ),
)


# Shapes whose first segment is whitelisted, so they reach realpath() and are
# the ones that crashed on Windows. "/%00" is deliberately NOT here: it is a
# control, already rejected by the whitelist before the crash point.
NUL_SHAPES = [
    "/js/a%00b.js",
    "/js/%00",
    "/css/%00style.css",
    "/assets/img%00.png",
    "/index.html%00",
    "/js/viz.js%00.js",
    "/app%00.py",
    "/js/\x00.js",
]

LONG_SHAPES = [
    "/js/" + "a" * LONG_JUST_OVER + ".js",
    "/css/" + "b" * 40000 + ".css",
    "/assets/" + "c" * 60000 + ".png",
]

# Long but still legal: sized from the same measurement, so it sits under the
# limit by construction. The first version of this file hard-coded 4000 nested
# components, which is under Windows' limit and over Linux' PATH_MAX -- the test
# asserted a platform-specific fact and CI proved it. Legal means "the platform
# resolves it", not "some number of characters".
LONG_BUT_LEGAL_SHAPES = [
    "/js/" + "/".join("d" for _ in range(200)) + "/x.js",
]

# pytest uses the parameter itself as the test id, and a 60k-char path turns
# one failing run into 50 KB of 'a'. Short ids keep a failure readable; the
# shape is still fully determined by the parameter.
LONG_IDS = ["long-just-over", "long-40k", "long-60k"]
LEGAL_IDS = ["long-but-legal-deep-200"]
NUL_IDS = [f"nul-{i}" for i in range(len(NUL_SHAPES))]


def _inside_root(path: str) -> bool:
    real = os.path.realpath(path)
    return real == ROOT_REAL or real.startswith(ROOT_REAL + os.sep)


def _call(url_path):
    """Invoke the unbound method; it reads nothing from ``self``."""
    return srv.Handler._safe_local_path(None, url_path)


def _shown(url_path: str) -> str:
    return url_path if len(url_path) < 80 else url_path[:60] + f"...<{len(url_path)}>"


# --- the contract, asserted on every platform --------------------------


# --- the strict claims, asserted only where they are measurable --------


@pytest.mark.parametrize("url_path", NUL_SHAPES, ids=NUL_IDS)
def test_nul_path_is_refused(url_path):
    """A NUL is not a path on any platform, so the answer is always None.

    Distinct from the test above: that one tolerates a platform that resolves
    the string. This one says the refusal is the contract, and holds on POSIX
    too -- which is why the first CI run only reddened the long-path shapes.
    """
    out = _call(url_path)
    assert out is None, (
        f"{_shown(url_path)!r} was not refused, it resolved to {out!r}"
    )


@SKIP_NO_LIMIT
@pytest.mark.parametrize("url_path", LONG_SHAPES, ids=LONG_IDS)
def test_over_long_path_is_refused_where_the_platform_refuses(url_path):
    """Strict, and only asserted where the measurement says it can hold.

    On Windows realpath raises and the fix must turn that into a refusal. On
    Linux it returns a best-effort path, and the correct behaviour there is
    still "answer inside ROOT" (asserted above) -- not "answer None". Asserting
    None anyway is what turned this file red on the Linux CI leg.
    """
    out = _call(url_path)
    assert out is None, (
        f"{_shown(url_path)!r} was not refused on a platform that refuses "
        f"paths this long; it resolved to {out!r}"
    )


def test_a_path_under_the_length_limit_is_still_handled_cleanly():
    """Pins the boundary from the other side, so the guard cannot widen into
    "long -> 403". Sized from the measured break point, because that point
    depends on how long ROOT is and is not a constant of the code."""
    url_path = "/js/" + "a" * LONG_JUST_UNDER + ".js"
    out = _call(url_path)          # must not raise
    assert out is None or _inside_root(out), (
        "a resolvable path must either be refused or stay inside ROOT, "
        f"never resolve outside it: {out!r}"
    )



def test_bare_nul_path_stays_refused():
    """The control for the reachability claim above: "/%00" was already safe,
    and the NUL check must not have changed how it is answered."""
    assert _call("/%00") is None


# --- the user-visible consequence --------------------------------------


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def live_server():
    port = _free_port()
    saved_hosts = set(srv.EXPECTED_HOSTS)
    srv.EXPECTED_HOSTS.clear()
    srv.EXPECTED_HOSTS.add(f"127.0.0.1:{port}")
    srv.EXPECTED_HOSTS.add(f"localhost:{port}")

    class _Quiet(srv.Handler):
        def log_message(self, *a, **k):
            pass

    class _QuietServer(srv._BoundedThreadingHTTPServer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.errors = []

        def handle_error(self, request, client_address):
            exc = sys.exc_info()[1]
            self.errors.append(exc)
            try:
                request.close()
            except OSError:
                pass

    httpd = _QuietServer(("127.0.0.1", port), _Quiet, max_workers=8)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    # NB: no `with http.client.HTTPConnection(...)` -- the context-manager
    # protocol is not available on this interpreter, and a readiness loop that
    # never sends a request reports "did not come up" for the wrong reason.
    ready = False
    last_exc = None
    for _ in range(100):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            c.request("GET", "/index.html")
            c.getresponse().read()
            ready = True
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.05)
        finally:
            c.close()
    if not ready:
        httpd.shutdown()
        httpd.server_close()
        pytest.fail(f"test server did not come up: {last_exc!r}")
    try:
        yield port, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)
        srv.EXPECTED_HOSTS.clear()
        srv.EXPECTED_HOSTS.update(saved_hosts)


def _raw_get(port, target):
    """Send a request line we control verbatim.

    urllib/http.client normalise the target, and the whole point of the defect
    is what reaches the server untouched, so the line is written by hand.
    """
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        conn.putheader("Connection", "close")
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_nul_path_gets_a_well_formed_403(live_server):
    """What a client actually sees, for the shape that is refused everywhere.

    The function-level test above can pass while the wire behaviour is still
    broken, so the observable contract is pinned directly: a complete HTTP
    response, the same 403 every other unusable path gets, and no traceback
    logged by the server.
    """
    port, httpd = live_server
    status, body = _raw_get(port, "/js/a%00b.js")
    assert status == 403, f"expected a clean 403, got {status} / {body[:200]!r}"
    assert b"forbidden" in body, body[:200]
    assert httpd.errors == [], (
        f"server logged {len(httpd.errors)} unhandled error(s): "
        f"{[type(e).__name__ for e in httpd.errors]}"
    )


@SKIP_NO_LIMIT
def test_over_long_path_gets_a_well_formed_403(live_server):
    """The wire contract for the over-long shape, on the platforms where that
    shape is a defect at all.

    Kept separate from the NUL case because the expected answer is not the same
    everywhere: where the platform resolves a long path there is nothing to
    refuse, and the honest expectation is "some complete response, no
    traceback" -- which is still the property the defect broke.
    """
    port, httpd = live_server
    status, body = _raw_get(port, "/js/" + "a" * LONG_JUST_OVER + ".js")
    assert status in (403, 404), (
        f"expected a complete 403/404, got {status} / {body[:200]!r}"
    )
    assert httpd.errors == [], (
        f"server logged {len(httpd.errors)} unhandled error(s): "
        f"{[type(e).__name__ for e in httpd.errors]}"
    )
    # Never the failure mode: no response at all.
    assert status is not None


def test_a_path_under_the_length_limit_still_gets_a_normal_answer(live_server):
    """The other side of the boundary, over the wire.

    Without this, "reject anything long" would pass every test above while
    quietly breaking real deep-but-legal asset paths with a 403. Just under the
    limit the path is well-formed, so it must get the ordinary not-found
    answer, not a forbidden one.
    """
    port, httpd = live_server
    status, _ = _raw_get(port, "/js/" + "a" * LONG_JUST_UNDER + ".js")
    assert status == 404, f"expected the ordinary 404, got {status}"
    assert httpd.errors == [], [type(e).__name__ for e in httpd.errors]


def test_an_over_long_request_line_is_still_the_stdlibs_414(live_server):
    """Sanity on the outer gate: past CPython's 65536-byte request-line cap,
    parse_request answers before _safe_local_path is ever reached. If this
    ever stops being 414, the reachable window for the defect has moved and
    the numbers quoted in server.py need re-measuring."""
    port, httpd = live_server
    status, _ = _raw_get(port, "/js/" + "a" * 70000 + ".js")
    assert status == 414, f"expected the stdlib's 414, got {status}"
    assert httpd.errors == [], [type(e).__name__ for e in httpd.errors]


def test_neighbouring_paths_are_unaffected(live_server):
    """Controls: the fix must not have changed the answers around it."""
    port, httpd = live_server
    assert _raw_get(port, "/js/definitely_missing.js")[0] == 404
    assert _raw_get(port, "/server.py")[0] == 403
    assert _raw_get(port, "/index.html")[0] == 200
    assert httpd.errors == [], [type(e).__name__ for e in httpd.errors]


def test_server_keeps_serving_after_a_rejected_path(live_server):
    """Pins the severity honestly: the defect is a malformed response, not a
    dead server. If this ever starts failing, the bug got worse and the
    severity note above is wrong."""
    port, httpd = live_server
    _raw_get(port, "/js/a%00b.js")
    status, _ = _raw_get(port, "/index.html")
    assert status == 200, "server stopped serving after a NUL-path request"


# --- the full adversarial table, as a standing net -------------------

# Shapes that must never escape ROOT and must never raise. This is the table
# the bug was found with; keeping it means the next change to
# _safe_local_path is measured against all of it, not just the one shape.
HOSTILE_SHAPES = [
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/%2E%2E/secret.txt",
    "/%252e%252e/secret.txt",
    r"/js/..\..\secret.txt",
    r"/js\..\..\secret.txt",
    r"/js/..\../secret.txt",
    "//attacker/share/secret.txt",
    r"/C:/Windows/win.ini",
    "/C:/Windows/win.ini",
    r"\secret.txt",
    "/js/../../secret.txt",
    "/js%2f..%2f..%2fsecret.txt",
    "/index.html.",
    "/index.html ",
    "/index.html:stream",
    "/./index.html",
    "//js//viz.js",
]


@pytest.mark.parametrize(
    "url_path",
    HOSTILE_SHAPES + NUL_SHAPES + LONG_SHAPES + LONG_BUT_LEGAL_SHAPES,
    ids=[f"hostile-{i}" for i in range(len(HOSTILE_SHAPES))]
         + NUL_IDS + LONG_IDS + LEGAL_IDS)
def test_hostile_path_never_escapes_and_never_raises(url_path):
    """The contract that holds on every platform, over the whole table.

    Whatever the platform does with the path, _safe_local_path must answer
    rather than raise, and anything it hands back must be inside ROOT. This is
    the assertion that catches the original defect anywhere, and unlike "must
    return None" it is not a statement about one OS -- which is exactly the
    mistake that turned this file red on the Linux CI leg.

    It is deliberately ONE test over the whole table rather than a strict test
    per family: the strict variants below are the ones that need a platform
    measurement, and having a single unconditional net means no shape is ever
    left uncovered just because the strict one skipped.
    """
    try:
        out = _call(url_path)
    except Exception as exc:  # noqa: BLE001 - that IS the bug
        pytest.fail(f"{_shown(url_path)!r} raised "
                    f"{type(exc).__name__}: {exc} -- it must answer, not raise")
    if out is not None:
        assert _inside_root(out), (
            f"{_shown(url_path)!r} resolved outside ROOT: {out!r}"
        )


@pytest.mark.parametrize("url_path", LONG_BUT_LEGAL_SHAPES, ids=LEGAL_IDS)
def test_a_long_but_resolvable_path_is_not_refused(url_path):
    """The other direction, so the fix cannot quietly grow into "long == 403".

    Sized from the measured break point, so it is under the limit on whatever
    platform is running. A guard that refused everything long would pass every
    "must be refused" case above and still break this, so it is asserted
    separately -- and it is the test that catches a too-wide guard on the
    platforms where the strict ones skip.
    """
    out = _call(url_path)
    assert out is not None, (
        "a resolvable path was refused; the length guard is too wide"
    )
    assert _inside_root(out)


@pytest.mark.parametrize("url_path", ["/index.html", "/js/viz.js", "/"])
def test_ordinary_paths_still_resolve(url_path):
    """The rejection must stay narrow: real assets still serve."""
    out = _call(url_path)
    assert out is not None, f"{url_path!r} was wrongly refused"
    assert _inside_root(out)
    assert os.path.isfile(out), f"{url_path!r} resolved to a non-file: {out!r}"
