"""Range Chart Analyzer - one-click launcher.

Usage:
    python main.py                # modern Fluent GUI (falls back to Tkinter)
    python main.py fluent         # same as above, explicit
    python main.py gui            # force the classic Tkinter GUI
    python main.py tk             # alias for gui
    python main.py server        # launch the web server and open the browser
    python main.py server --port 8080 --no-browser
    python main.py modern        # native window via PyWebView (pip install pywebview)

Default (no args) now launches the modern PySide6 + qfluentwidgets GUI.
If those packages aren't installed, it automatically falls back to the
classic Tkinter GUI (`gui.py`) — no traceback, zero setup. All modes
share the same rca_core backend and config.

`--host`, `--port` and `--no-browser` configure the HTTP backend, so they
only apply to `server`/`web`; in every other mode they are reported as
ignored on stderr instead of being dropped silently.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def launch_gui() -> int:
    try:
        import tkinter  # noqa: F401
    except Exception:
        print(
            "ERROR: tkinter is not available in this Python.\n"
            "Use the web server instead:  python main.py server",
            file=sys.stderr,
        )
        return 1
    import gui

    gui.main()
    return 0


def launch_server(host: str, port: int, open_browser: bool) -> int:
    import server

    if open_browser:
        import threading
        import webbrowser

        # Sprint B (REVIEW-2026-09-04) #10: 0.0.0.0 / :: / empty are bind
        # wildcards, not addresses a browser can open — normalize them to
        # loopback so we don't launch an unreachable http://0.0.0.0:port/.
        browser_host = host.strip() if host else ""
        if browser_host in ("", "0.0.0.0", "::"):
            browser_host = "127.0.0.1"
        if ":" in browser_host and not browser_host.startswith("["):
            # Bare IPv6 literal needs brackets in a URL.
            url = f"http://[{browser_host}]:{port}/"
        else:
            url = f"http://{browser_host}:{port}/"
        # daemon=True so a pending timer can't keep the process alive
        # (Timer.__init__ takes no daemon kwarg — set the attribute).
        timer = threading.Timer(1.0, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()

    sys.argv = ["server.py", "--host", host, "--port", str(port)]
    server.main()
    return 0


def launch_modern() -> int:
    try:
        import app as _modern
    except Exception as exc:
        # Previously this silently returned 0, swallowing ImportError on
        # Windows from pywebview/WebView2 missing. Fail loud with a clear
        # hint and a non-zero exit so CI/scripts detect it.
        print(
            f"[modern-ui] failed to load app.py ({exc.__class__.__name__}): {exc}\n"
            "Common cause: pywebview is installed but the platform WebView "
            "engine is unavailable (Windows: install Edge WebView2 Runtime; "
            "Linux: install webkit2gtk-4.0).\n"
            "Fallback: run  python main.py gui  for the Tkinter GUI.",
            file=sys.stderr,
        )
        return 2
    return _modern.main()


def launch_fluent() -> int:
    # REVIEW-2026-09-20: this used to be one `except Exception`, which turned
    # EVERY import-time failure into the message "PySide6 + qfluentwidgets not
    # available" — a missing matplotlib / pillow / etc. inside gui_fluent's
    # dependency chain got misdiagnosed as a Qt problem, and real bugs in the
    # import body were hidden. ImportError (which covers the ModuleNotFoundError
    # subclass raised for PySide6 / qfluentwidgets themselves) keeps the
    # documented fallback and now names the original exception; anything else
    # prints its real traceback before falling back, because staying usable
    # still beats crashing on a user's machine.
    try:
        import gui_fluent
    except ImportError as exc:
        print(
            "[fluent-ui] the modern GUI could not be imported "
            f"({exc.__class__.__name__}: {exc}).\n"
            "Most likely PySide6 and/or qfluentwidgets are missing. Falling "
            "back to the Tkinter GUI. To enable the Fluent UI: "
            "pip install PySide6 PySide6-Fluent-Widgets",
            file=sys.stderr,
        )
        return launch_gui()
    except Exception as exc:  # noqa: BLE001 - availability first, then truth
        import traceback
        print(
            f"[fluent-ui] importing gui_fluent raised an unexpected "
            f"{exc.__class__.__name__} (NOT a missing-PySide6 problem). "
            "Original traceback follows, then we fall back to the Tkinter GUI.",
            file=sys.stderr,
        )
        traceback.print_exc()
        return launch_gui()
    return gui_fluent.main()


# Server-only flags and their effective defaults. ``None`` for an argparse
# default means "the user did not pass it", which is what makes the
# ignored-flag warning below accurate — with real defaults in place we could
# not tell an explicit ``--port 8000`` from silence. The effective values are
# substituted where the server mode is actually entered (see main()).
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def _ignored_server_flags(args, mode: str, ui: str) -> list:
    """Which server-only flags were passed but have no effect in this mode?

    REVIEW-2026-09-20: ``python main.py gui --port 8080`` used to accept the
    flag and silently drop it, so users believed the Tk / Fluent GUI was
    listening on their port. Only ``server``/``web`` start the HTTP backend,
    so only there do these three flags mean anything. ``--ui`` is an alias of
    the GUI family, so ``--ui modern`` with a positional ``server`` routes to
    PyWebView and the flags are dropped there too (mirroring main()'s order).
    """
    routed_to_server = (
        mode in ("server", "web")
        and ui not in ("modern", "fluent", "tk", "gui")
    )
    if routed_to_server:
        return []
    ignored = []
    if args.host is not None:
        ignored.append(f"--host={args.host}")
    if args.port is not None:
        ignored.append(f"--port={args.port}")
    if args.no_browser:
        ignored.append("--no-browser")
    return ignored


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Range Chart Analyzer launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="default",
        choices=["default", "gui", "tk", "server", "web", "modern", "fluent"],
        help="(default): modern Fluent GUI, falls back to Tkinter; "
             "gui/tk: force classic Tkinter GUI; server/web: browser + local "
             "backend; modern: native window via PyWebView; fluent: modern GUI",
    )
    parser.add_argument(
        "--ui",
        choices=["modern", "fluent", "tk", "gui"],
        help="alias: --ui modern | --ui fluent | --ui tk",
    )
    parser.add_argument(
        "--host", default=None, metavar="HOST",
        help="bind host for the server (only meaningful in server/web mode; "
             f"default {_DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port", type=int, default=None, metavar="PORT",
        help="port for the server (only meaningful in server/web mode; "
             f"default {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not auto-open the browser (only meaningful in server/web mode)",
    )
    args = parser.parse_args()

    ui = args.ui or ""
    mode = args.mode

    # Warn about server flags that this mode will never look at, then keep
    # going — refusing to start a GUI over a stray flag would be worse.
    ignored = _ignored_server_flags(args, mode, ui)
    if ignored:
        effective_mode = ui or ("server" if mode in ("server", "web") else mode)
        print(
            f"[main] warning: {' '.join(ignored)} {'are' if len(ignored) > 1 else 'is'} "
            f"only honoured by the 'server'/'web' modes — ignored here "
            f"(mode={effective_mode}).",
            file=sys.stderr,
        )

    # Explicit classic-Tkinter request.
    if ui in ("tk", "gui") or mode in ("gui", "tk"):
        return launch_gui()
    # Explicit PyWebView request.
    if ui == "modern" or mode == "modern":
        return launch_modern()
    # Explicit Fluent request.
    if ui == "fluent" or mode == "fluent":
        return launch_fluent()
    # Server / web.
    if mode in ("server", "web"):
        host = args.host if args.host is not None else _DEFAULT_HOST
        port = args.port if args.port is not None else _DEFAULT_PORT
        return launch_server(host, port, open_browser=not args.no_browser)
    # Default (no args): modern Fluent GUI, which itself falls back to
    # Tkinter when PySide6 / qfluentwidgets aren't installed.
    return launch_fluent()


if __name__ == "__main__":
    raise SystemExit(main())
