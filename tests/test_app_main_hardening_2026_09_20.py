"""REVIEW-2026-09-20收尾: app.py / main.py hardening regressions.

Covers the six items fixed in this round:
  app.py #1  _with_port_lock waits ~60 s and NEVER continues unlocked in
             silence (it warns and pre-flights the candidate ports).
  app.py #2  the instance lock stores a boot timestamp and a stale lock /
             recycled PID is taken over instead of blocking every launch;
             a genuine refusal prints diagnostics.
  app.py #3  the browser-fallback branches shut the backend down (including
             on KeyboardInterrupt).
  app.py #4  _probe_and_bind probes the host it is given, not 127.0.0.1.
  main.py #5 server-only flags are reported as ignored outside server/web.
  main.py #6 launch_fluent() only blames PySide6 for ImportError; anything
             else prints its real traceback before falling back to Tk.

All hermetic: the real ~/.range_chart_analyzer/lock file and the real GUI /
backend are never touched.
"""
from __future__ import annotations

import builtins
import os
import socket
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import main as launcher  # noqa: E402


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class FakeServer:
    """Stands in for server._BoundedThreadingHTTPServer."""

    def __init__(self):
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.close_calls += 1


class FakeThread:
    """Stands in for the httpd daemon thread."""

    def __init__(self, alive=False, raise_on_join=None):
        self._alive = alive
        self.raise_on_join = raise_on_join
        self.join_timeouts = []

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)
        if self.raise_on_join is not None:
            raise self.raise_on_join
        self._alive = False
        return None


@pytest.fixture()
def lock_file(tmp_path, monkeypatch):
    path = tmp_path / "lock"
    monkeypatch.setattr(app, "LOCK_FILE", str(path))
    return path


# --------------------------------------------------------------------------
# app.py #2 — lock file carries a boot timestamp, and it parses both ways
# --------------------------------------------------------------------------

class TestLockFileFormat:
    def test_write_then_read_roundtrip(self, lock_file, monkeypatch):
        monkeypatch.setattr(app.os, "getpid", lambda: 4242)
        app._write_lock("127.0.0.1", 8765)
        raw = lock_file.read_text(encoding="utf-8").strip()
        parts = raw.split()
        assert parts[0] == "127.0.0.1:8765"
        assert parts[1] == "4242"
        # 4th field = the boot timestamp, appended without breaking the
        # legacy readers (gui_fluent_history_detail._read_lock_port reads
        # field[0] only).
        assert len(parts) == 3 and int(parts[2]) > 1_700_000_000
        assert app._read_lock() == ("127.0.0.1", 8765, 4242, float(parts[2]))

    def test_legacy_three_field_lock_still_parses(self, lock_file):
        lock_file.write_text("127.0.0.1:8000 999\n", encoding="utf-8")
        host, port, pid, boot = app._read_lock()
        assert (host, port, pid) == ("127.0.0.1", 8000, 999)
        assert boot is None, "legacy locks simply carry no boot timestamp"

    def test_missing_or_garbage_lock_returns_none(self, lock_file):
        assert app._read_lock() is None
        lock_file.write_text("not-a-lock\n", encoding="utf-8")
        assert app._read_lock() is None


class TestStaleLockTakeover:
    def test_dead_pid_is_stale(self, lock_file, monkeypatch):
        monkeypatch.setattr(app, "_pid_alive", lambda pid: False)
        running, why = app._existing_instance_running(("127.0.0.1", 8000, 999, None))
        assert running is False
        assert "not running" in why

    def test_recycled_pid_detected_from_creation_time(self, lock_file, monkeypatch):
        """A process that started BEFORE the lock was written cannot be its
        author — the old code called this 'alive' forever."""
        boot = 1_700_000_000.0
        monkeypatch.setattr(app, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(app, "_process_creation_time", lambda pid: boot - 86_400)
        running, why = app._existing_instance_running(("127.0.0.1", 8000, 999, boot))
        assert running is False
        assert "recycled" in why

    def test_matching_creation_time_is_a_real_instance(self, monkeypatch):
        boot = 1_700_000_000.0
        monkeypatch.setattr(app, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(app, "_process_creation_time", lambda pid: boot + 1)
        running, _why = app._existing_instance_running(("127.0.0.1", 8000, 999, boot))
        assert running is True

    def test_old_lock_with_no_answer_is_taken_over(self, monkeypatch):
        """The simple rule: age > 120 s + nothing answering host:port == stale
        (used when the OS cannot tell us the process creation time)."""
        monkeypatch.setattr(app, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(app, "_process_creation_time", lambda pid: None)
        monkeypatch.setattr(app, "_lock_age", lambda: 3600.0)
        monkeypatch.setattr(app, "_instance_answers", lambda *a, **k: False)
        running, why = app._existing_instance_running(("127.0.0.1", 8000, 999, None))
        assert running is False
        assert "stale lock" in why

    def test_old_lock_that_still_answers_is_respected(self, monkeypatch):
        monkeypatch.setattr(app, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(app, "_process_creation_time", lambda pid: None)
        monkeypatch.setattr(app, "_lock_age", lambda: 3600.0)
        monkeypatch.setattr(app, "_instance_answers", lambda *a, **k: True)
        running, _why = app._existing_instance_running(("127.0.0.1", 8000, 999, None))
        assert running is True

    def test_fresh_lock_is_trusted_without_probing(self, monkeypatch):
        """Cold start of the FIRST instance must not be raced by a second
        launch: under 120 s we trust the lock and never take over."""
        calls = []
        monkeypatch.setattr(app, "_pid_alive", lambda pid: True)
        monkeypatch.setattr(app, "_process_creation_time", lambda pid: None)
        monkeypatch.setattr(app, "_lock_age", lambda: 5.0)
        monkeypatch.setattr(
            app, "_instance_answers",
            lambda *a, **k: calls.append(a) or True,
        )
        running, _why = app._existing_instance_running(("127.0.0.1", 8000, 999, None))
        assert running is True
        assert calls == [], "no probe should run while the lock is young"

    def test_main_refuses_with_diagnostics(self, lock_file, monkeypatch, capsys):
        monkeypatch.setattr(app, "_read_lock",
                            lambda: ("127.0.0.1", 8000, 999, 1_700_000_000.0))
        monkeypatch.setattr(
            app, "_existing_instance_running",
            lambda _lock: (True, "pid 999 is alive and started after the lock"),
        )
        monkeypatch.setattr(app, "_start_server",
                            lambda *a, **k: pytest.fail("must not bind when refusing"))
        assert app.main() == 0
        out = capsys.readouterr().out
        assert "already running" in out
        # REVIEW-2026-09-20 #2: diagnosability, not a bare "Aborting."
        assert "evidence:" in out and "details :" in out
        assert "exit code 0 is intentional" in out

    def test_main_takes_over_a_stale_lock(self, lock_file, monkeypatch, capsys):
        lock_file.write_text("127.0.0.1:8000 999 1700000000\n", encoding="utf-8")
        fake_httpd = FakeServer()
        # atexit must stay out of it: the real _clear_lock would otherwise run
        # at interpreter exit against the (unpatched) home-directory lock.
        monkeypatch.setattr(app.atexit, "register", lambda *a, **k: None)
        monkeypatch.setattr(app, "_existing_instance_running",
                            lambda _lock: (False, "pid 999 was recycled"))
        monkeypatch.setattr(app, "_with_port_lock", lambda fn, **kw: fn())
        monkeypatch.setattr(app, "_probe_and_bind",
                            lambda preferred, host="127.0.0.1": 8123)
        monkeypatch.setattr(app, "_start_server",
                            lambda host, port: (FakeThread(), fake_httpd))
        monkeypatch.setattr(app, "_wait_until_ready", lambda *a, **k: True)
        # Force the "no pywebview" branch without importing the real thing.
        monkeypatch.setitem(sys.modules, "webview", None)
        monkeypatch.setattr(app.webbrowser, "open", lambda *a, **k: True)
        assert app.main() == 0
        out = capsys.readouterr().out
        assert "STALE lock" in out and "recycled" in out
        assert fake_httpd.shutdown_calls == 1 and fake_httpd.close_calls == 1, \
            "the browser-fallback branch has to close the backend (#3)"
        # The takeover rewrote the lock for OUR pid, on the probed port.
        host, port, pid, boot = app._read_lock()
        assert (host, port, pid) == ("127.0.0.1", 8123, os.getpid())
        assert boot is not None

    def test_main_reports_bind_failure(self, lock_file, monkeypatch, capsys):
        monkeypatch.setattr(app.atexit, "register", lambda *a, **k: None)
        monkeypatch.setattr(app, "_read_lock", lambda: None)

        def boom(fn, **kw):
            raise OSError(98, "address already in use")

        monkeypatch.setattr(app, "_with_port_lock", boom)
        assert app.main() == 1
        out = capsys.readouterr().out
        assert "failed to bind the local backend" in out


class TestClearLock:
    def test_keeps_a_live_foreign_lock(self, lock_file, monkeypatch):
        lock_file.write_text("127.0.0.1:8000 999 1700000000\n", encoding="utf-8")
        monkeypatch.setattr(app, "_existing_instance_running", lambda _l: (True, "alive"))
        app._clear_lock()
        assert lock_file.exists()

    def test_removes_its_own_lock(self, lock_file, monkeypatch):
        app._write_lock("127.0.0.1", 8000)
        monkeypatch.setattr(app, "_existing_instance_running",
                            lambda _l: pytest.fail("our own lock needs no judgement"))
        app._clear_lock()
        assert not lock_file.exists()


# --------------------------------------------------------------------------
# app.py #1 — the port sentinel lock never fails silently
# --------------------------------------------------------------------------

class TestPortSentinelLock:
    @pytest.fixture(autouse=True)
    def _temp_sentinel(self, tmp_path, monkeypatch):
        """Keep the tests off the real ~/.range_chart_analyzer.portlock."""
        monkeypatch.setattr(app.os.path, "expanduser",
                            lambda _p: str(tmp_path / "sentinel_home"))
        (tmp_path / "sentinel_home").mkdir()
        self._sentinel = str(tmp_path / "sentinel_home" / ".range_chart_analyzer.portlock")

    def test_timeout_still_runs_fn_but_warns_and_pre_flights(self, monkeypatch, capsys):
        seen = {}

        def fake_busy(ports, host="127.0.0.1"):
            seen["ports"], seen["host"] = list(ports), host
            return [8000]

        released = []
        monkeypatch.setattr(app, "_try_lock_port_sentinel", lambda fd: False)
        monkeypatch.setattr(app, "_unlock_port_sentinel", lambda fd: released.append(fd))
        monkeypatch.setattr(app, "_busy_ports", fake_busy)
        called = []
        assert app._with_port_lock(lambda: called.append(1) or "result",
                                   probe_ports=(8000, 8765), host="127.0.0.1") == "result"
        assert called == [1]
        assert released == [], "nothing to release when the lock was never taken"
        out = capsys.readouterr().out
        assert "WARNING" in out and "WITHOUT the cross-process port lock" in out
        assert "8000" in out, "the busy port has to be named"
        assert seen["ports"] == [8000, 8765] and seen["host"] == "127.0.0.1"

    def test_unopenable_sentinel_is_loud_too(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(app.os.path, "expanduser",
                            lambda _p: str(tmp_path / "does" / "not" / "exist"))
        monkeypatch.setattr(app, "_busy_ports", lambda ports, host="127.0.0.1": [])
        assert app._with_port_lock(lambda: 5, probe_ports=(8000,)) == 5
        out = capsys.readouterr().out
        assert "cannot open the port sentinel" in out
        assert "all look free" in out

    def test_windows_wait_budget_is_60s(self):
        assert app.PORT_LOCK_WAIT_S >= 60.0
        assert app.STALE_LOCK_MAX_AGE_S == 120.0

    def test_sentinel_lock_is_taken_and_released(self, monkeypatch):
        """Happy path: fn runs while the lock is held, and we release after."""
        events = []
        monkeypatch.setattr(app, "_try_lock_port_sentinel",
                            lambda fd: events.append("acquire") or True)
        monkeypatch.setattr(app, "_unlock_port_sentinel",
                            lambda fd: events.append("release"))
        assert app._with_port_lock(lambda: "ok") == "ok"
        assert events == ["acquire", "release"]
        assert os.path.exists(self._sentinel)

    def test_lock_released_even_when_fn_raises(self, monkeypatch):
        events = []
        monkeypatch.setattr(app, "_try_lock_port_sentinel",
                            lambda fd: events.append("acquire") or True)
        monkeypatch.setattr(app, "_unlock_port_sentinel",
                            lambda fd: events.append("release"))

        def boom():
            raise OSError("bind failed")

        with pytest.raises(OSError):
            app._with_port_lock(boom)
        assert events == ["acquire", "release"]


class TestBusyPorts:
    def test_detects_a_real_listener(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            port = srv.getsockname()[1]
            assert app._busy_ports([port]) == [port]
            # A port nobody listens on (the ephemeral range we just released).
            free = srv.getsockname()[1]
        finally:
            srv.close()
        assert app._busy_ports([free]) == []


# --------------------------------------------------------------------------
# app.py #4 — the probe binds the host it was told to serve on
# --------------------------------------------------------------------------

class TestProbeUsesRealHost:
    def _stub_socket(self, recorded, bind_error_on=()):
        class _Sock:
            def __init__(self, fam, typ):
                self.fam, self.typ = fam, typ
                self._bound = None

            def setsockopt(self, *a):
                pass

            def bind(self, addr):
                host, port = addr
                if (host, port) in bind_error_on:
                    raise OSError(98, "in use")
                self._bound = addr
                recorded.append(addr)

            def getsockname(self):
                return (self._bound[0], 12345)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return types.SimpleNamespace(
            socket=lambda fam, typ: _Sock(fam, typ),
            AF_INET=2, AF_INET6=23, SOCK_STREAM=1, SOL_SOCKET=0xFFF,
            SO_EXCLUSIVEADDRUSE=getattr(socket, "SO_EXCLUSIVEADDRUSE", None),
        )

    def test_probe_binds_the_given_host(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(app, "socket", self._stub_socket(recorded))
        assert app._probe_and_bind((8000,), "0.0.0.0") == 8000
        assert recorded == [("0.0.0.0", 8000)]

    def test_probe_skips_a_busy_port_on_that_host(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            app, "socket",
            self._stub_socket(recorded, bind_error_on=(("127.0.0.1", 8000),)),
        )
        assert app._probe_and_bind((8000, 8765), "127.0.0.1") == 8765
        # Successful binds are recorded in order: 8000 was refused on this host.
        assert recorded == [("127.0.0.1", 8765)]

    def test_probe_falls_back_to_an_ephemeral_port(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            app, "socket",
            self._stub_socket(recorded,
                              bind_error_on=(("127.0.0.1", 8000), ("127.0.0.1", 8765))),
        )
        assert app._probe_and_bind((8000, 8765), "127.0.0.1") == 12345
        assert recorded == [("127.0.0.1", 0)]

    def test_ipv6_host_uses_an_inet6_socket(self, monkeypatch):
        recorded = []
        monkeypatch.setattr(app, "socket", self._stub_socket(recorded))
        app._probe_and_bind((8000,), "::1")
        assert recorded == [("::1", 8000)]
        assert app._socket_family_for("::1") == socket.AF_INET6
        assert app._socket_family_for("127.0.0.1") == socket.AF_INET

    def test_no_hardcoded_loopback_left_in_the_probe(self):
        src = open("app.py", encoding="utf-8").read()
        assert 's.bind(("127.0.0.1"' not in src, (
            "the probe must bind the caller's host, not a hardcoded loopback"
        )

    def test_pick_free_port_forwards_host(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(app, "_with_port_lock",
                            lambda fn, **kw: (seen.update(kw), fn())[1])
        monkeypatch.setattr(app, "_probe_and_bind",
                            lambda preferred, host="127.0.0.1": (seen.__setitem__("bind", (preferred, host)) or 8000))
        assert app._pick_free_port((8000,), host="10.0.0.1") == 8000
        assert seen["bind"] == ((8000,), "10.0.0.1")
        assert seen["host"] == "10.0.0.1" and list(seen["probe_ports"]) == [8000]


# --------------------------------------------------------------------------
# app.py #3 — the browser fallback always closes the backend
# --------------------------------------------------------------------------

class TestBrowserFallbackShutdown:
    def test_keyboard_interrupt_still_shuts_down(self, monkeypatch, capsys):
        t = FakeThread(alive=True, raise_on_join=KeyboardInterrupt())
        httpd = FakeServer()
        assert app._wait_for_backend_browser_mode(t, httpd) == 0
        assert httpd.shutdown_calls == 1 and httpd.close_calls == 1
        assert "interrupted; stopping the backend" in capsys.readouterr().out

    def test_thread_exit_shuts_down(self):
        t = FakeThread(alive=True)
        httpd = FakeServer()
        assert app._wait_for_backend_browser_mode(t, httpd) == 0
        assert httpd.shutdown_calls == 1
        assert t.join_timeouts and all(x is not None for x in t.join_timeouts), \
            "poll-join so Ctrl+C is delivered"

    def test_stop_server_swallows_errors(self):
        class Boom:
            def shutdown(self):
                raise OSError("boom")

            def server_close(self):
                raise OSError("boom")

        app._stop_server(Boom())   # must not raise
        app._stop_server(None)     # must not raise

    def test_main_has_no_bare_return_after_keyboardinterrupt(self):
        """Source-level guard: no browser-fallback branch may `return` right
        after a KeyboardInterrupt without going through the shutdown helper."""
        src = open("app.py", encoding="utf-8").read()
        assert "httpd.shutdown()" in src and "httpd.server_close()" in src
        assert src.count("_wait_for_backend_browser_mode(t, httpd)") >= 2, (
            "both pywebview-missing and engine-unavailable branches must park "
            "through the helper that shuts the backend down"
        )
        assert "except KeyboardInterrupt:\n            pass\n        return 0" not in src


# --------------------------------------------------------------------------
# main.py #5 — server-only flags outside server/web are reported
# --------------------------------------------------------------------------

def _ns(host=None, port=None, no_browser=False):
    return types.SimpleNamespace(host=host, port=port, no_browser=no_browser)


class TestIgnoredServerFlags:
    def test_server_mode_honours_them(self):
        assert launcher._ignored_server_flags(_ns("0.0.0.0", 9000, True), "server", "") == []
        assert launcher._ignored_server_flags(_ns(port=9000), "web", "") == []

    @pytest.mark.parametrize("mode,ui", [
        ("gui", ""), ("tk", ""), ("modern", ""), ("fluent", ""), ("default", ""),
        ("default", "modern"), ("default", "fluent"), ("default", "tk"),
        ("server", "modern"),
    ])
    def test_other_modes_report_them(self, mode, ui):
        got = launcher._ignored_server_flags(_ns("0.0.0.0", 9000, True), mode, ui)
        assert got == ["--host=0.0.0.0", "--port=9000", "--no-browser"]

    def test_silent_when_nothing_passed(self):
        assert launcher._ignored_server_flags(_ns(), "gui", "") == []

    def test_main_prints_warning_and_still_launches(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["main.py", "gui", "--port", "8080"])
        called = []
        monkeypatch.setattr(launcher, "launch_gui", lambda: called.append("tk") or 0)
        assert launcher.main() == 0
        err = capsys.readouterr().err
        assert "--port=8080" in err and "ignored" in err and "server" in err
        assert called == ["tk"], "a stray flag must not stop the GUI"

    def test_main_server_mode_still_gets_defaults(self, monkeypatch, capsys):
        captured = {}
        monkeypatch.setattr(sys, "argv", ["main.py", "server"])
        monkeypatch.setattr(
            launcher, "launch_server",
            lambda host, port, open_browser: captured.update(
                host=host, port=port, open_browser=open_browser) or 0,
        )
        assert launcher.main() == 0
        assert captured == {"host": "127.0.0.1", "port": 8000, "open_browser": True}
        assert "warning" not in capsys.readouterr().err

    def test_main_server_mode_passes_explicit_values(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(sys, "argv",
                            ["main.py", "server", "--host", "0.0.0.0",
                             "--port", "9001", "--no-browser"])
        monkeypatch.setattr(
            launcher, "launch_server",
            lambda host, port, open_browser: captured.update(
                host=host, port=port, open_browser=open_browser) or 0,
        )
        launcher.main()
        assert captured == {"host": "0.0.0.0", "port": 9001, "open_browser": False}


# --------------------------------------------------------------------------
# main.py #6 — launch_fluent no longer blames PySide6 for everything
# --------------------------------------------------------------------------

class TestLaunchFluentDiagnostics:
    def _patch_import(self, monkeypatch, exc):
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "gui_fluent":
                raise exc
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)

    def test_import_error_names_the_original_exception(self, monkeypatch, capsys):
        self._patch_import(monkeypatch, ModuleNotFoundError("No module named 'PySide6'"))
        calls = []
        monkeypatch.setattr(launcher, "launch_gui", lambda: calls.append(1) or 0)
        assert launcher.launch_fluent() == 0
        err = capsys.readouterr().err
        assert "ModuleNotFoundError" in err and "No module named 'PySide6'" in err
        assert "pip install PySide6" in err
        assert "Traceback" not in err, "the expected case stays a one-line hint"
        assert calls == [1]

    def test_missing_optional_dep_message_is_not_a_silent_lie(self, monkeypatch, capsys):
        """A missing matplotlib surfaces as ImportError too — the message now
        quotes it instead of claiming Qt is absent."""
        self._patch_import(monkeypatch, ImportError("No module named 'matplotlib'"))
        monkeypatch.setattr(launcher, "launch_gui", lambda: 0)
        launcher.launch_fluent()
        assert "matplotlib" in capsys.readouterr().err

    def test_unexpected_error_prints_traceback_then_falls_back(self, monkeypatch, capsys):
        self._patch_import(monkeypatch, ValueError("undefined name 'foo' in gui_fluent"))
        calls = []
        monkeypatch.setattr(launcher, "launch_gui", lambda: calls.append(1) or 0)
        assert launcher.launch_fluent() == 0
        err = capsys.readouterr().err
        assert "Traceback (most recent call last)" in err
        assert "NOT a missing-PySide6 problem" in err
        assert calls == [1], "availability first: we still fall back to Tk"

    def test_success_path_calls_the_gui(self, monkeypatch):
        stub = types.ModuleType("gui_fluent")
        stub.main = lambda: 7
        monkeypatch.setitem(sys.modules, "gui_fluent", stub)
        assert launcher.launch_fluent() == 7


# --------------------------------------------------------------------------
# cross-checks that the module still exposes what callers expect
# --------------------------------------------------------------------------

def test_app_module_surface_unchanged():
    for name in ("_pid_alive", "_read_lock", "_write_lock", "_clear_lock",
                 "_probe_and_bind", "_pick_free_port", "_with_port_lock",
                 "_wait_until_ready", "_start_server", "main"):
        assert hasattr(app, name), f"app.{name} disappeared — external callers rely on it"


def test_pid_alive_still_probes_itself():
    assert app._pid_alive(os.getpid()) is True
    assert app._pid_alive(None) is False


def test_process_creation_time_is_sane_on_this_platform():
    ct = app._process_creation_time(os.getpid())
    if sys.platform == "win32":
        assert ct is not None, "Windows must answer via GetProcessTimes"
        import time as _t
        assert abs(_t.time() - ct) < 600, "creation time is this process' start"
    else:
        assert ct is None or ct > 0
