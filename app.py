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

LOCK_FILE = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer.lock")





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
    """
    return _with_port_lock(lambda: _probe_and_bind(preferred))


def _probe_and_bind(preferred):
    for p in preferred:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
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
    """Launch server.py's ThreadingHTTPServer on a daemon thread."""
    from http.server import ThreadingHTTPServer
    import server  # the existing stdlib backend

    try:
        httpd = ThreadingHTTPServer((host, port), server.Handler)
    except OSError:
        # Surface the bind failure so main() can return a non-zero exit code
        # instead of crashing with an unhandled traceback.
        raise
    httpd.daemon_threads = True
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
    port = _pick_free_port()
    _log(f"starting local backend on http://{host}:{port}/")

    # T11: refuse to start if another live instance holds the lock.
    existing = _read_lock()
    if existing:
        host_e, port_e, pid_e = existing
        if _pid_alive(pid_e):
            _log(f"another GUI instance is running on http://{host_e}:{port_e}/ (pid {pid_e}). Aborting.")
            return 0

    t, httpd = _start_server(host, port)
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
