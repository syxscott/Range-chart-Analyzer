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

        url = f"http://{host}:{port}/"
        # Open the browser shortly after the server starts serving.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

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
    try:
        import gui_fluent
    except Exception as exc:  # PySide6 / qfluentwidgets missing
        print(
            "[fluent-ui] PySide6 + qfluentwidgets not available "
            f"({exc.__class__.__name__}). Falling back to the Tkinter GUI. "
            "To enable the Fluent UI: pip install PySide6 PySide6-Fluent-Widgets",
            file=sys.stderr,
        )
        return launch_gui()
    return gui_fluent.main()


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
    parser.add_argument("--host", default="127.0.0.1", help="server host (server mode)")
    parser.add_argument("--port", type=int, default=8000, help="server port (server mode)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not auto-open the browser (server mode)",
    )
    args = parser.parse_args()

    ui = args.ui or ""
    mode = args.mode

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
        return launch_server(args.host, args.port, open_browser=not args.no_browser)
    # Default (no args): modern Fluent GUI, which itself falls back to
    # Tkinter when PySide6 / qfluentwidgets aren't installed.
    return launch_fluent()


if __name__ == "__main__":
    raise SystemExit(main())
