"""Range Chart Analyzer - one-click launcher.

Usage:
    python main.py                # launch the desktop GUI (default, no CORS)
    python main.py gui            # same as above, explicit
    python main.py server        # launch the web server and open the browser
    python main.py server --port 8080 --no-browser
    python main.py modern        # native window via PyWebView (requires: pip install pywebview)
    python main.py --ui modern   # alias for the above

The GUI is the default because it makes the LLM call server-side (no
browser CORS limits) and needs zero setup. The "modern" mode is opt-in
and wraps the existing web frontend in a native window.
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
    except Exception as exc:  # never traceback
        print(f"[modern-ui] failed to load app.py: {exc}", file=sys.stderr)
        return 0
    return _modern.main()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Range Chart Analyzer launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="gui",
        choices=["gui", "server", "web", "modern"],
        help="gui (default): desktop app; server/web: browser + local backend; modern: native window via PyWebView",
    )
    parser.add_argument(
        "--ui",
        choices=["modern"],
        help="alias: --ui modern",
    )
    parser.add_argument("--host", default="127.0.0.1", help="server host (server mode)")
    parser.add_argument("--port", type=int, default=8000, help="server port (server mode)")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not auto-open the browser (server mode)",
    )
    args = parser.parse_args()

    if args.ui == "modern" or args.mode == "modern":
        return launch_modern()
    if args.mode in ("server", "web"):
        return launch_server(args.host, args.port, open_browser=not args.no_browser)
    return launch_gui()


if __name__ == "__main__":
    raise SystemExit(main())
