"""Headless smoke tests for the Fluent (PySide6) GUI.

Guarded by importorskip so machines WITHOUT PySide6 / qfluentwidgets
skip cleanly (never fail). Runs under QT_QPA_PLATFORM=offscreen so no
display is required.

Run:  QT_QPA_PLATFORM=offscreen python tests_gui_fluent.py
      (or via pytest)
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_pass = 0
_fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)


def _have_deps():
    try:
        import PySide6  # noqa: F401
        import qfluentwidgets  # noqa: F401
        return True
    except Exception:
        return False


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def run_all():
    if not _have_deps():
        print("SKIP tests_gui_fluent: PySide6 / qfluentwidgets not installed")
        return 0

    _app()
    import gui_fluent

    check("module-has-main", hasattr(gui_fluent, "main"))

    win = gui_fluent.RangeChartFluentWindow()
    try:
        names = {win.extract_page.objectName(),
                 win.providers_page.objectName(),
                 win.settings_page.objectName(),
                 win.about_page.objectName()}
        check("has-extract-page", "extractPage" in names)
        check("has-providers-page", "providersPage" in names)
        check("has-settings-page", "settingsPage" in names)
        check("has-about-page", "aboutPage" in names)

        # Settings accessors reflect defaults.
        check("max-tokens-accessor", isinstance(win.max_tokens(), int))
        check("runs-in-range", 1 <= win.runs() <= 5)
        check("chart-type-valid", win.chart_type() in ("auto", "range_chart", "columnar_section"))

        # Language cycle switches + re-translates without error.
        before = win.tr.lang
        win._cycle_lang()
        check("lang-cycles", win.tr.lang != before)

        # Extract worker class exists and is a QThread.
        from PySide6.QtCore import QThread
        check("extract-worker-is-qthread", issubclass(gui_fluent.ExtractWorker, QThread))
        check("conntest-worker-is-qthread", issubclass(gui_fluent.ConnTestWorker, QThread))
    finally:
        win.close()
        win.deleteLater()

    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    import sys
    rc = run_all()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(rc)
