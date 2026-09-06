"""Range Chart Analyzer - Modern UI (PyWebView wrapper).

Starts the existing server.py on a background thread, then opens a native
window via pywebview pointing at the local server. The web frontend
(index.html + css/ + js/) is reused unchanged.

Opt-in only:
    python main.py --ui modern        # via main.py
    python main.py modern            # shortcut
    python app.py                    # direct (dev convenience)

If pywebview is not installed OR no native engine is available, falls
back to opening the local URL in the default browser and exits 0.
"""
from __future__ import annotations

import atexit
import http.client
import os
import socket
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# P1-1 (REVIEW-2026-07-25): import LOCK_PATH from rca_core.history so it is
# the single source of truth. Previously this file defined LOCK_FILE separately.
from rca_core.history import LOCK_PATH
# Alias to the old name for backwards-compatible internal references.
LOCK_FILE = LOCK_PATH

# P2-1 (REVIEW-2026-07-25) note: this app.py is a PyWebView wrapper
# that opens a NATIVE window pointing at http://127.0.0.1:<port>/.
# It is not a "modern-UI deployment" with separate Origin — server.py's
# /api/extract endpoint is same-origin by construction (browser makes
# the request to the very host:port it's loaded from). Therefore the
# Origin/EXPECTED_HOSTS hardening that protects server.py when hosted on
# a public host does NOT apply here: the loading screen injected via
# PyWebView always arrives from the same localhost origin.
# We document this so future contributors don't add a redundant
# EXPECTED_HOSTS check that would break the local-only flow.





def _log(msg: str) -> None:
    print(f"[modern-ui] {msg}", flush=True)


def _read_lock():
    """Return (host, port, pid) from an existing lock file, or None."""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        parts = content.split()
        addr = parts[0]
        pid = int(parts[1]) if len(parts) > 1 else None
        host, port = addr.rsplit(":", 1)
        return host, int(port), pid
    except (OSError, ValueError, IndexError):
        return None


def _pid_alive(pid):
    """Best-effort check whether a process is still running.

    POSIX: ``os.kill(pid, 0)`` is the standard "is this pid alive" probe.
    Windows: ``os.kill`` is implemented via ``TerminateProcess`` and signal 0
    is not a reliable liveness probe (and historically raised
    ``PermissionError``/``OSError`` inconsistently depending on ownership,
    which the previous code treated as "dead" but on some Windows builds
    could also terminate the previous instance under the wrong path). Use
    ``OpenProcess`` + ``GetExitCodeProcess`` and check ``STILL_ACTIVE (259)``
    instead.
    """
    if pid is None:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
            )
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except AttributeError:
        return False


def _pick_free_port(preferred=(8000, 8765)) -> int:
    """Try preferred ports first, then ask the kernel for a free one.

    Bug-10 fix: hold a cross-process file lock while probing + binding,
    so two concurrent ``app.py`` launches don't both pick the same
    "free" port (TOCTOU between ``bind`` and the actual ``listen``).
    The lock is released automatically when the process exits or the
    helper returns. On Windows we use ``msvcrt.locking``; on POSIX we
    use ``fcntl.flock``; both work through a single sentinel file in
    the user's home directory.

    Sprint B (REVIEW-2026-09-04) #4: ``main()`` no longer goes through
    this helper — it keeps the lock held across the REAL server bind as
    well (probe + bind atomically under one lock). This probe-only
    helper is retained for tests and external callers.
    """
    return _with_port_lock(lambda: _probe_and_bind(preferred))


def _probe_and_bind(preferred):
    """Probe ports with a REAL bind (no SO_REUSEADDR lies).

    Sprint B (REVIEW-2026-09-04) #4: the probe socket previously set
    SO_REUSEADDR. On Windows that option lets a bind SUCCEED even when
    another socket is already listening on the port (double-bind), so a
    busy port was reported as free and the real server bind later
    double-bound or failed confusingly. The probe now uses a plain bind;
    on Windows we additionally set SO_EXCLUSIVEADDRUSE (guarded with
    hasattr for portability) so a bind against an in-use listening port
    fails outright.
    """
    def _harden(s):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

    for p in preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            _harden(s)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        _harden(s)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _with_port_lock(fn):
    """Run *fn* under a cross-process advisory lock so two app launches
    can't simultaneously probe + bind the same port.

    Uses a sentinel file in the user's home directory. The lock is
    released as soon as ``fn`` returns, so subsequent operations (the
    actual ``serve_forever``) run without it. This is advisory only —
    it does not stop a process that ignores the file, but combined with
    the existing PID-based lock file it covers the realistic two-launch
    race that the bind-only check misses.
    """
    sentinel = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer.portlock")
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # If we can't open the sentinel, fall through without locking —
        # better than refusing to start.
        return fn()
    try:
        if sys.platform == "win32":
            try:
                import msvcrt  # type: ignore
                # Lock 1 byte at offset 0. Blocks if another process holds it.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                try:
                    return fn()
                finally:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            except (ImportError, OSError):
                return fn()
        else:
            try:
                import fcntl  # type: ignore
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    return fn()
                finally:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
            except (ImportError, OSError):
                return fn()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _wait_until_ready(host: str, port: int, timeout: float = 5.0) -> bool:
    """Poll GET / until 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        conn = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=0.5)
            conn.request("GET", "/")
            r = conn.getresponse()
            r.read(64)
            if r.status == 200:
                return True
        except (OSError, http.client.HTTPException):
            pass
        finally:
            # Always close the connection — `with` would do it, but using
            # try/finally + close() also covers the case where the
            # constructor itself raises (e.g. socket.error on some
            # platforms can be raised before `with` enters).
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        time.sleep(0.05)
    return False


def _start_server(host: str, port: int):
    """Launch server.py's bounded HTTP server on a daemon thread.

    Sprint B (REVIEW-2026-09-04) #8: previously this path built a bare
    ``ThreadingHTTPServer``, bypassing server._BoundedThreadingHTTPServer's
    concurrent-thread cap and bounded submit queue. It now uses the same
    bounded server class (with its default max_workers=32 /
    request_queue_size=64, mirroring server.main()).
    """
    import server  # the existing stdlib backend

    # P2-1 / REVIEW-2026-07-31: this app is local-only and same-origin, but
    # the CSRF Origin check must still be anchored to a real allowlist.
    # Previously EXPECTED_HOSTS stayed empty here, so _validate_csrf_and_origin
    # fell back to comparing the Origin against the client-controlled Host
    # header (DNS-rebinding). populate_expected_hosts adds the loopback
    # aliases + local IPs, so the local flow keeps working with the allowlist
    # populated and the server-side check stays fail-closed.
    server.populate_expected_hosts(host, port)

    # Raises OSError when the port is busy — main() turns that into a
    # friendly message + non-zero exit code.
    httpd = server._BoundedThreadingHTTPServer((host, port), server.Handler)
    # daemon_threads is a class attribute on _BoundedThreadingHTTPServer.
    t = threading.Thread(target=httpd.serve_forever, name="rca-http", daemon=True)
    t.start()
    return t, httpd


def _write_lock(host: str, port: int) -> None:
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write("%s:%s %d" % (host, port, os.getpid()))
            f.write(chr(10))
    except OSError:
        pass


def _clear_lock() -> None:
    """Remove the lock file if (and only if) it belongs to us.

    Three safety properties:
      - never delete a lock file owned by a different live PID;
      - if the recorded PID is dead, the lock is stale and is fair game
        (so a crashed previous instance doesn't block new launches);
      - any unexpected I/O error is swallowed: lock cleanup is best-effort
        and must not mask a real exception on shutdown.
    """
    try:
        lock = _read_lock()
        if lock:
            _, _, pid = lock
            # If another live process holds the lock, leave it alone.
            if pid is not None and pid != os.getpid() and _pid_alive(pid):
                return
            # Otherwise (our own pid, or a dead pid) we are entitled to remove it.
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass

def main():
    host = "127.0.0.1"

    # T11: refuse to start if another live instance holds the lock —
    # checked BEFORE we probe or bind anything.
    existing = _read_lock()
    if existing:
        host_e, port_e, pid_e = existing
        if _pid_alive(pid_e):
            _log(f"another GUI instance is running on http://{host_e}:{port_e}/ (pid {pid_e}). Aborting.")
            return 0

    # Sprint B (REVIEW-2026-09-04) #4: probe the port AND bind the real
    # server socket inside the same cross-process lock. Previously the
    # lock was released as soon as the probe returned, leaving a TOCTOU
    # window until _start_server's actual bind. Residual risk (accepted):
    # processes that ignore the advisory lock file can still race us —
    # the lock is cooperative only.
    def _claim_and_start():
        port = _probe_and_bind((8000, 8765))
        _t, _httpd = _start_server(host, port)
        return port, _t, _httpd

    try:
        port, t, httpd = _with_port_lock(_claim_and_start)
    except OSError as exc:
        # Sprint B (REVIEW-2026-09-04) #9: surface the bind failure as a
        # friendly message + non-zero exit code instead of an unhandled
        # traceback (the old comment promised this but main() had no try).
        _log(f"failed to bind the local backend on {host}: {exc}")
        _log("close the program using that port (or reboot) and try again.")
        return 1

    _log(f"starting local backend on http://{host}:{port}/")
    _write_lock(host, port)
    # HIGH-5: register atexit cleanup immediately after starting the server
    # (BEFORE the readiness probe) so a probe-failure path also releases
    # the lock and shuts down the daemon thread.
    atexit.register(_clear_lock)

    if not _wait_until_ready(host, port, timeout=5.0):
        _log("backend failed to become ready in 5s; exiting")
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        # atexit._run will fire _clear_lock on process exit.
        return 1

    loading_url = f"http://{host}:{port}/app/loading.html?next=/"
    final_url = f"http://{host}:{port}/"
    _log(f"backend ready; opening window -> {final_url}")

    try:
        import webview  # type: ignore
    except ImportError:
        _log(
            "pywebview is not installed. Run:\n"
            "    pip install pywebview\n"
            f"Falling back to opening {final_url} in your default browser."
        )
        webbrowser.open(final_url)
        _log("press Ctrl+C here to stop the backend")
        try:
            t.join()
        except KeyboardInterrupt:
            pass
        return 0

    try:
        window = webview.create_window(
            "Range Chart Analyzer",
            url=loading_url,
            width=1280,
            height=820,
            min_size=(960, 640),
        )
        try:
            webview.start()
        except KeyboardInterrupt:
            pass
    except Exception as exc:  # noqa: BLE001 - engine availability varies
        _log(f"native window engine unavailable ({exc.__class__.__name__}).")
        _log(f"opening {final_url} in your default browser instead.")
        webbrowser.open(final_url)
        _log("press Ctrl+C here to stop the backend")
        try:
            t.join()
        except KeyboardInterrupt:
            pass
        return 0

    _log("window closed; stopping backend")
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
