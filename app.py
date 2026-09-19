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


# --- Cross-process locking tuning (REVIEW-2026-09-20) -----------------------
# How long we are willing to block for the port sentinel before giving up on
# the lock. The Windows path used to rely on ``msvcrt.LK_LOCK`` alone, which
# retries once per second for ~10 s and then raises OSError — and the OSError
# handler ran ``fn()`` with NO lock and printed nothing, which is exactly the
# probe+bind TOCTOU window the lock exists to close (and it closed silently,
# most often while the first instance was still cold-starting).
PORT_LOCK_WAIT_S = 60.0
PORT_LOCK_RETRY_S = 0.25
# A lock file older than this whose recorded instance does not answer an HTTP
# probe is treated as stale and taken over (see _existing_instance_running).
STALE_LOCK_MAX_AGE_S = 120.0
# Clock/rounding slack between the boot timestamp stored in the lock and the
# OS process creation time before we conclude the PID was reused.
PID_REUSE_TOLERANCE_S = 10.0
# How long we wait for the recorded host:port to answer during staleness
# checks. Deliberately short: this runs on the startup path.
INSTANCE_PROBE_TIMEOUT_S = 1.0


def _log(msg: str) -> None:
    print(f"[modern-ui] {msg}", flush=True)


def _read_lock():
    """Return (host, port, pid, boot_ts) from an existing lock file, or None.

    REVIEW-2026-09-20: the file gained a 4th field — the epoch seconds at
    which the owning instance wrote it — so a PID that the OS handed out
    again after the owner died can be recognised as reused (compare the
    timestamp against the process' own creation time, see
    _process_creation_time). Legacy 1/2/3-field files still parse; they just
    carry ``boot_ts = None`` and fall back to the age + HTTP-probe rule.
    """
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        parts = content.split()
        addr = parts[0]
        pid = int(parts[1]) if len(parts) > 1 else None
        boot_ts = float(parts[2]) if len(parts) > 2 else None
        host, port = addr.rsplit(":", 1)
        return host, int(port), pid, boot_ts
    except (OSError, ValueError, IndexError):
        return None


def _lock_age() -> float | None:
    """Seconds since the lock file was last written, or None if unreadable."""
    try:
        return max(0.0, time.time() - os.path.getmtime(LOCK_FILE))
    except OSError:
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

    REVIEW-2026-09-20: note what this can NOT answer — it proves the PID is
    *taken*, not that it still belongs to the process that wrote our lock
    file (PIDs get recycled, and a wedged owner stays "alive" forever). Never
    gate a startup refusal on this alone; use _existing_instance_running(),
    which corroborates with the lock's boot timestamp and an HTTP probe.
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


def _process_creation_time(pid) -> float | None:
    """Best-effort OS start time (epoch seconds) of *pid*, or None.

    REVIEW-2026-09-20: this is the PID-reuse discriminator that does NOT need
    a ``wmic`` / ``tasklist`` subprocess. ``_pid_alive`` can only ever answer
    "is *some* process using this PID right now?" — it can never answer "is
    it the process that wrote our lock file?". On Windows we read the
    kernel's own creation time through the same
    ``PROCESS_QUERY_LIMITED_INFORMATION`` handle used by _pid_alive; on Linux
    ``/proc/<pid>``'s mtime tracks process start closely enough for a
    staleness heuristic. Returns None whenever the platform or the
    permission path cannot tell us, and callers then fall back to the
    lock-age + HTTP-probe rule.
    """
    if pid is None:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
            )
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_t = wintypes.FILETIME()
                kernel_t = wintypes.FILETIME()
                user_t = wintypes.FILETIME()
                ok = kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation), ctypes.byref(exit_t),
                    ctypes.byref(kernel_t), ctypes.byref(user_t),
                )
                if not ok:
                    return None
                stamps = ((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
                if not stamps:
                    return None
                # FILETIME counts 100 ns intervals since 1601-01-01; the Unix
                # epoch starts 11644473600 s later.
                return stamps / 1e7 - 11644473600.0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    # Linux / other procfs platforms. Guarded by the directory check so this
    # stays a no-op on macOS and anywhere /proc is absent.
    try:
        if os.path.isdir("/proc"):
            return os.stat(f"/proc/{pid}").st_mtime
    except OSError:
        return None
    return None


def _instance_answers(host: str, port: int, timeout: float = INSTANCE_PROBE_TIMEOUT_S) -> bool:
    """True when *host*:*port* completes an HTTP exchange with us.

    GET /health first (cheap, and the endpoint server.py exposes for exactly
    this), falling back to GET /. Any completed HTTP response counts — we are
    only corroborating "the recorded instance is still serving", not
    authenticating it; a TCP connect alone would also be satisfied by an
    unrelated service that inherited the recycled PID.
    """
    probe_host = host.strip()
    if probe_host in ("", "0.0.0.0", "::"):
        probe_host = "127.0.0.1"
    for path in ("/health", "/"):
        conn = None
        try:
            conn = http.client.HTTPConnection(probe_host, port, timeout=timeout)
            conn.request("GET", path)
            r = conn.getresponse()
            r.read(64)
            if r.status is not None:
                return True
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return False


def _existing_instance_running(existing) -> tuple[bool, str]:
    """Is the pre-existing lock owned by a live, reachable instance?

    Returns ``(running, reason)`` — *reason* is shown to the user either way,
    so that "another instance is running" and "this one is wedged" are no
    longer indistinguishable.

    REVIEW-2026-09-20: the old gate was ``_pid_alive(pid)`` alone, which
    permanently blocked startup in two cases:
      * after the owner dies, the OS hands that PID to an unrelated process,
        so GetExitCodeProcess == STILL_ACTIVE keeps answering "alive"
        forever (and the refusal used to exit 0 with no diagnostics);
      * a crashed / wedged owner leaves the file behind.
    We now corroborate with data the OS actually guarantees:
      (a) if the lock carries a boot timestamp AND we can read the process'
          creation time, a process that started BEFORE the lock was written
          cannot be its author -> the PID was reused, the lock is stale;
      (b) otherwise, once the lock is older than STALE_LOCK_MAX_AGE_S the
          recorded host:port has to answer an HTTP probe, or we take over.
    A lock younger than that threshold is still trusted, so the normal
    "second launch while the first is still booting" case keeps refusing
    (no takeover race).
    """
    host_e, port_e, pid_e, boot_e = existing
    if pid_e is None:
        return False, f"lock file {LOCK_FILE!r} records no PID"
    if not _pid_alive(pid_e):
        return False, f"pid {pid_e} is not running any more"
    creation = _process_creation_time(pid_e)
    if creation is not None and boot_e is not None:
        if creation < (boot_e - PID_REUSE_TOLERANCE_S):
            return False, (
                f"pid {pid_e} was started {boot_e - creation:.0f}s BEFORE the lock was "
                f"written (lock boot stamp {boot_e:.0f}, process start {creation:.0f}) "
                f"— the PID has been recycled by an unrelated process"
            )
        return True, (
            f"pid {pid_e} is alive and started after the lock was written "
            f"(http://{host_e}:{port_e}/)"
        )
    age = _lock_age()
    if age is not None and age > STALE_LOCK_MAX_AGE_S:
        if _instance_answers(host_e, port_e):
            return True, (
                f"pid {pid_e} is alive and http://{host_e}:{port_e}/ still answers "
                f"after {age:.0f}s"
            )
        return False, (
            f"lock is {age:.0f}s old (> {STALE_LOCK_MAX_AGE_S:.0f}s), pid {pid_e} is "
            f"alive but http://{host_e}:{port_e}/ answers nothing — stale lock, "
            f"taking over (process-creation check unavailable: "
            f"{'no boot timestamp in lock' if boot_e is None else 'cannot read process start time'})"
        )
    return True, (
        f"pid {pid_e} is alive and the lock is only "
        f"{('%.0fs' % age) if age is not None else 'of unknown age'} old"
    )


def _pick_free_port(preferred=(8000, 8765), host: str = "127.0.0.1") -> int:
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

    REVIEW-2026-09-20: *host* is now passed through to the probe (it used to
    be ignored — the probe always bound 127.0.0.1 even when the caller would
    later bind something else, so a "free" verdict said nothing about the
    real bind address).
    """
    return _with_port_lock(
        lambda: _probe_and_bind(preferred, host), probe_ports=preferred, host=host
    )


def _socket_family_for(host: str) -> int:
    """AF_INET for v4/empty hosts, AF_INET6 for v6 literals (best effort)."""
    h = (host or "").strip()
    if ":" in h or h.startswith("["):
        return socket.AF_INET6
    return socket.AF_INET


def _probe_and_bind(preferred, host: str = "127.0.0.1"):
    """Probe ports with a REAL bind on *host* (no SO_REUSEADDR lies).

    Sprint B (REVIEW-2026-09-04) #4: the probe socket previously set
    SO_REUSEADDR. On Windows that option lets a bind SUCCEED even when
    another socket is already listening on the port (double-bind), so a
    busy port was reported as free and the real server bind later
    double-bound or failed confusingly. The probe now uses a plain bind;
    on Windows we additionally set SO_EXCLUSIVEADDRUSE (guarded with
    hasattr for portability) so a bind against an in-use listening port
    fails outright.

    REVIEW-2026-09-20 #4: the bind address used to be the hardcoded
    ``"127.0.0.1"`` regardless of the host ``main()`` later serves on, which
    made a "free" result meaningless for any other bind host. The probe now
    uses the caller's *host*. Residual (documented) race: this probe socket
    is closed when the helper returns and the real server binds it again a
    moment later, so the release-and-rebind window still exists — the
    sentinel lock in _with_port_lock only serialises cooperating launches
    (it is a cooperative/advisory lock: it cannot stop a process that never
    opens the file, and it does not survive an unlocked continuation).
    """
    fam = _socket_family_for(host)
    bind_host = "" if not (host or "").strip() else host.strip()
    if fam == socket.AF_INET6 and bind_host.startswith("[") and bind_host.endswith("]"):
        bind_host = bind_host[1:-1]

    def _harden(s):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

    for p in preferred:
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            _harden(s)
            try:
                s.bind((bind_host, p))
                return p
            except OSError:
                continue
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        _harden(s)
        s.bind((bind_host, 0))
        return s.getsockname()[1]


def _busy_ports(ports, host: str = "127.0.0.1") -> list:
    """Ports on *host* that already accept a TCP connection (i.e. look busy)."""
    busy = []
    for p in ports:
        try:
            with socket.socket(_socket_family_for(host), socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex(((host or "").strip() or "127.0.0.1", int(p))) == 0:
                    busy.append(p)
        except (OSError, TypeError, ValueError):
            continue
    return busy


def _try_lock_port_sentinel(fd) -> bool:
    """Take the 1-byte sentinel lock, blocking up to PORT_LOCK_WAIT_S.

    Returns True when the lock is ours, False when we gave up (caller then
    decides how loudly to continue). REVIEW-2026-09-20 #1: the Windows branch
    used to call ``msvcrt.locking(LK_LOCK)`` once — that already retries ~10
    times, once per second, and then raises OSError, which the caller swallowed
    into a *silent* unlocked continuation. Both platforms now retry until the
    budget is spent, and neither blocks forever.
    """
    deadline = time.monotonic() + PORT_LOCK_WAIT_S
    if sys.platform == "win32":
        try:
            import msvcrt  # type: ignore
        except ImportError:
            _log("msvcrt is not importable — the port lock is unavailable on this build")
            return False
        while True:
            try:
                # Lock 1 byte at offset 0 for the whole process lifetime.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                return True
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _log(
                        f"port sentinel lock failed after {PORT_LOCK_WAIT_S:.0f}s "
                        f"({exc.__class__.__name__}: {exc})"
                    )
                    return False
                # LK_LOCK already occupies ~10 s per call before raising, so no
                # long sleep here; just yield before the next attempt.
                time.sleep(min(PORT_LOCK_RETRY_S, remaining))
    try:
        import fcntl  # type: ignore
    except ImportError:
        _log("fcntl is not importable — the port lock is unavailable on this build")
        return False
    while True:
        try:
            # LOCK_NB + our own retry loop: a plain blocking LOCK_EX would
            # park here forever if another process never releases the flock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log(
                    f"port sentinel lock failed after {PORT_LOCK_WAIT_S:.0f}s "
                    f"({exc.__class__.__name__}: {exc})"
                )
                return False
            time.sleep(min(PORT_LOCK_RETRY_S, remaining))


def _unlock_port_sentinel(fd) -> None:
    if sys.platform == "win32":
        try:
            import msvcrt  # type: ignore
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        return
    try:
        import fcntl  # type: ignore
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass


def _with_port_lock(fn, probe_ports=(), host: str = "127.0.0.1"):
    """Run *fn* under a cross-process advisory lock so two app launches
    can't simultaneously probe + bind the same port.

    Uses a sentinel file in the user's home directory. The lock is
    released as soon as ``fn`` returns, so subsequent operations (the
    actual ``serve_forever``) run without it. This is advisory only —
    it does not stop a process that ignores the file, but combined with
    the existing PID-based lock file it covers the realistic two-launch
    race that the bind-only check misses.

    REVIEW-2026-09-20 #1: when the lock cannot be taken (sentinel not
    openable, or the wait budget in PORT_LOCK_WAIT_S is exhausted) we still
    continue — a local GUI must not be held hostage by a foreign process —
    but never silently again: we say so, and before continuing we TCP-probe
    *probe_ports* on *host* so the user learns immediately whether we are
    about to race for a port that is already occupied. ``fn``'s own bind is
    still the authority (a probe can miss a listener that appears in between),
    and bind failures propagate to main()'s OSError handler.
    """
    sentinel = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer.portlock")
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        # If we can't open the sentinel, continue without locking — better
        # than refusing to start — but LOUD, and after checking the ports.
        _log(
            f"WARNING: cannot open the port sentinel {sentinel!r} "
            f"({exc.__class__.__name__}: {exc}); probing ports WITHOUT the lock"
        )
        _warn_unlocked_continuation(probe_ports, host)
        return fn()
    try:
        locked = _try_lock_port_sentinel(fd)
        try:
            if not locked:
                _warn_unlocked_continuation(probe_ports, host, sentinel)
            return fn()
        finally:
            if locked:
                _unlock_port_sentinel(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _warn_unlocked_continuation(probe_ports, host: str, sentinel: str = "") -> None:
    """Print (never raise) what we know about continuing without the lock."""
    ports = [p for p in (probe_ports or ()) if p is not None]
    _log(
        "WARNING: continuing WITHOUT the cross-process port lock — two launches "
        "started at the same instant could still pick the same port. The lock is "
        "cooperative/advisory, so this is a degraded mode, not a refusal."
        + (f" (sentinel {sentinel!r})" if sentinel else "")
    )
    if not ports:
        _log("WARNING: no candidate ports were supplied, so no port pre-flight check ran.")
        return
    busy = _busy_ports(ports, host)
    if busy:
        _log(
            f"WARNING: pre-flight check — {host}:{busy} already accept TCP connections; "
            "expect the probe to report those busy (a foreign instance or service)."
        )
    else:
        _log(
            f"port pre-flight: {host}:{list(ports)} all look free — "
            "continuing unlocked is low-risk right now."
        )


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
    """Record ``host:port pid boot_ts`` — boot_ts is OUR start timestamp.

    REVIEW-2026-09-20 #2: the timestamp is what lets a later launch tell
    "the PID in this file was recycled by an unrelated process" apart from
    "the owner is still alive" (compare against the OS process creation time
    in _process_creation_time) instead of trusting GetExitCodeProcess forever.
    Field order is append-only: gui_fluent_history_detail._read_lock_port()
    reads field[0] only, so the extra field is backwards compatible.
    """
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write("%s:%s %d %d" % (host, port, os.getpid(), int(time.time())))
            f.write(chr(10))
    except OSError:
        _log(f"WARNING: could not write the instance lock file {LOCK_FILE!r}")


def _clear_lock() -> None:
    """Remove the lock file if (and only if) it belongs to us.

    Three safety properties:
      - never delete a lock file owned by a different live PID;
      - if the recorded PID is dead, or the PID was recycled / the instance
        stopped answering (see _existing_instance_running), the lock is stale
        and is fair game, so a crashed previous instance doesn't block new
        launches;
      - any unexpected I/O error is swallowed: lock cleanup is best-effort
        and must not mask a real exception on shutdown.
    """
    try:
        lock = _read_lock()
        if lock:
            pid = lock[2]
            # If another live process holds the lock, leave it alone.
            if pid is not None and pid != os.getpid():
                running, _reason = _existing_instance_running(lock)
                if running:
                    return
            # Otherwise (our own pid, or a dead/reused pid) we are entitled
            # to remove it.
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass


def _stop_server(httpd) -> None:
    """Gracefully stop the background HTTP server. Best-effort, idempotent.

    REVIEW-2026-09-20 #3: every exit path from main() now goes through this.
    shutdown() stops serve_forever() (and therefore the daemon thread) and
    server_close() releases the listening socket, so Ctrl+C in the browser
    fallback no longer leaves the port bound and the lock file pointing at a
    half-dead instance.
    """
    if httpd is None:
        return
    try:
        httpd.shutdown()
    except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
        _log(f"backend shutdown() raised {exc.__class__.__name__} (ignored)")
    try:
        httpd.server_close()
    except Exception as exc:  # noqa: BLE001
        _log(f"backend server_close() raised {exc.__class__.__name__} (ignored)")


def _wait_for_backend_browser_mode(t, httpd) -> int:
    """Browser-fallback park: wait for the backend thread, then ALWAYS close
    the server — including on KeyboardInterrupt (previously Ctrl+C returned
    straight from main() and skipped the shutdown)."""
    _log("press Ctrl+C here to stop the backend")
    try:
        # Poll-join instead of a bare blocking join(): KeyboardInterrupt from
        # the signal handler is only delivered reliably between the short
        # acquire timeouts, and the daemon thread never exits on its own.
        while t is not None and t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        _log("interrupted; stopping the backend")
    finally:
        _stop_server(httpd)
    return 0


def _describe_existing_instance(existing) -> str:
    host_e, port_e, pid_e, boot_e = existing
    age = _lock_age()
    bits = [
        f"pid {pid_e}",
        f"url http://{host_e}:{port_e}/",
        f"lock {LOCK_FILE!r}",
        f"lock age {'%.0fs' % age if age is not None else 'unknown'}",
        f"boot stamp {'%d' % boot_e if boot_e is not None else 'absent (pre-2026-09-20 format)'}",
        f"our pid {os.getpid()}",
    ]
    return ", ".join(bits)


def main():
    host = "127.0.0.1"

    # T11: refuse to start if another live instance holds the lock —
    # checked BEFORE we probe or bind anything.
    # REVIEW-2026-09-20 #2: liveness is no longer "GetExitCodeProcess says the
    # PID is taken"; see _existing_instance_running for the PID-reuse /
    # stale-lock rules. A stale lock is now overwritten instead of blocking
    # every future launch, and a real refusal prints the evidence.
    existing = _read_lock()
    if existing:
        host_e, port_e, pid_e, _boot_e = existing
        running, why = _existing_instance_running(existing)
        if running:
            _log(
                f"another GUI instance is already running on "
                f"http://{host_e}:{port_e}/ — not starting a second one."
            )
            _log(f"  evidence: {why}")
            _log(f"  details : {_describe_existing_instance(existing)}")
            _log("  exit code 0 is intentional (nothing failed: an instance is "
                 "already serving); if it is unreachable, delete the lock file "
                 "above or close that process and retry.")
            return 0
        _log(f"found a STALE lock from a previous run — taking over ({why})")
        _log(f"  details : {_describe_existing_instance(existing)}")
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

    # Sprint B (REVIEW-2026-09-04) #4: probe the port AND bind the real
    # server socket inside the same cross-process lock. Previously the
    # lock was released as soon as the probe returned, leaving a TOCTOU
    # window until _start_server's actual bind. Residual risk (accepted):
    # processes that ignore the advisory lock file can still race us —
    # the lock is cooperative only.
    def _claim_and_start():
        port = _probe_and_bind((8000, 8765), host)
        _t, _httpd = _start_server(host, port)
        return port, _t, _httpd

    try:
        port, t, httpd = _with_port_lock(
            _claim_and_start, probe_ports=(8000, 8765), host=host
        )
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
        _stop_server(httpd)
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
        # REVIEW-2026-09-20 #3: this branch used to `return 0` right after a
        # KeyboardInterrupt with the server still listening.
        return _wait_for_backend_browser_mode(t, httpd)

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
        # REVIEW-2026-09-20 #3: same as above — graceful shutdown here too.
        return _wait_for_backend_browser_mode(t, httpd)

    _log("window closed; stopping backend")
    _stop_server(httpd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
