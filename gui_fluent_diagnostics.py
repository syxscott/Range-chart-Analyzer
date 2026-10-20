"""gui_fluent_diagnostics.py — the app-level safety nets.

Three things cc-switch has and this app did not:

* an **error boundary** — an unhandled exception is shown to the user (and
  copied to the clipboard) instead of disappearing into a console nobody
  has open;
* an **environment banner** — a persistent strip that says what is wrong
  with the environment BEFORE the user spends a minute on an extraction
  that cannot possibly work;
* a **what's new** note on the first launch after an upgrade.

The first two live here; the third lives in ``gui_fluent_onboarding.py``
next to the first-run dialog it belongs with.

Everything in this module is importable WITHOUT Qt: the warning LOGIC is
plain functions over plain data, so it can be unit-tested headlessly, and
the widgets are only built when Qt is present. That split is the same one
the rest of the repo uses, and it is what lets the CI matrix run these
suites on every Python version without installing ~200 MB of Qt.
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

log = logging.getLogger("rca.gui_fluent_diagnostics")

try:
    from PySide6.QtWidgets import (
        QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget,
    )
    from qfluentwidgets import BodyLabel, PushButton
    _HAVE_QT = True
except ImportError:      # pragma: no cover - the CI matrix without Qt
    _HAVE_QT = False


#: The window the error dialog is shown over. It is a module-level holder
#: and not an argument because the boundary has to be installed BEFORE the
#: window exists -- an exception raised while the window is being built is
#: exactly the kind that needs a boundary -- and the window is only known
#: afterwards.
_ERROR_PARENT = None


def set_error_parent(parent) -> None:
    """Point the error dialog at the window, once it exists."""
    global _ERROR_PARENT
    _ERROR_PARENT = parent


# ---------------------------------------------------------------------------
# Environment checks (pure logic -- no Qt, unit-testable)
# ---------------------------------------------------------------------------

#: The environment variables that make a local endpoint unreachable.
#: httpx/urllib honour these, so an endpoint on 127.0.0.1 goes through the
#: proxy and comes back as a connection error that looks like a dead
#: server.
PROXY_ENV_VARS = (
    "HTTP_PROXY", "http_proxy",
    "HTTPS_PROXY", "https_proxy",
    "ALL_PROXY", "all_proxy",
)


def environment_warnings(*, provider_count: int = 0,
                         current_count: int = 0,
                         env: dict | None = None) -> list[str]:
    """What is wrong with the environment right now, as i18n KEYS.

    Returns a list of translation keys rather than sentences, so the
    wording lives in one place (the i18n table) and the CHECKS can be
    tested without a translator.

    The order is the order of severity: an app that cannot extract at all
    says so first.
    """
    warnings: list[str] = []
    if provider_count <= 0:
        # Nothing to call: every extraction will fail at the first request.
        warnings.append("app.envNoProvider")
    elif current_count <= 0:
        # Providers exist but none is active, so the same is true.
        warnings.append("app.envNoProvider")
    environ = os.environ if env is None else env
    if any((environ.get(name) or "").strip() for name in PROXY_ENV_VARS):
        warnings.append("app.envProxy")
    return warnings


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------

def format_exception(exc_type, exc, tb) -> str:
    """The full text of an exception, for the clipboard."""
    try:
        return "".join(traceback.format_exception(exc_type, exc, tb))
    except Exception:      # pragma: no cover - the formatter itself failed
        return "%s: %r" % (getattr(exc_type, "__name__", exc), exc)


def install_error_boundary(tr, parent=None) -> None:
    """Route unhandled exceptions to a dialog instead of the console.

    Three hooks, because there are three ways an exception escapes:

    * ``sys.excepthook`` — the main thread. PySide6 >= 6.5 calls it for
      an exception raised in a SLOT too (verified on 6.11.1: a
      ``QPushButton`` slot that raises ZeroDivisionError reaches the
      hook), so this covers the GUI.
    * ``threading.excepthook`` — the worker threads. The extraction
      worker and the provider's quota/test workers all run in a QThread,
      and an exception in one of them would otherwise print to a console
      that does not exist in a packaged app.
    * ``sys.unraisablehook`` — an exception raised while another one is
      being handled, or in a ``__del__``.

    The handler never re-raises and never lets its own failure escape: a
    boundary that crashes while reporting a crash is worse than the crash.
    """
    if parent is not None:
        set_error_parent(parent)

    def _report(exc_type, exc, tb, context=None):
        text = format_exception(exc_type, exc, tb)
        log.error("unhandled exception", exc_info=(exc_type, exc, tb))
        if context:
            log.error("unraisable context: %r", context)
        try:
            app = QApplication.instance() if _HAVE_QT else None
            if app is not None:
                clipboard = app.clipboard()
                if clipboard is not None:
                    clipboard.setText(text)
        except Exception:
            log.exception("could not copy the traceback to the clipboard")
        try:
            _show_error_dialog(tr, _ERROR_PARENT, text)
        except Exception:
            log.exception("could not show the error dialog")

    def _excepthook(exc_type, exc, tb):
        _report(exc_type, exc, tb)

    def _unraisablehook(unraisable):
        _report(type(unraisable.exc_value), unraisable.exc_value,
                unraisable.exc_traceback, unraisable.err_msg)

    sys.excepthook = _excepthook
    sys.unraisablehook = _unraisablehook
    try:
        import threading
        threading.excepthook = _excepthook
    except Exception:      # pragma: no cover - very old Python
        pass


def _show_error_dialog(tr, parent, text: str) -> None:
    """Show the error, with the traceboard one click away."""
    if not _HAVE_QT:
        return
    from qfluentwidgets import MessageBox
    box = MessageBox(
        tr.t("app.errorBoundaryTitle"),
        tr.t("app.errorBoundaryHint"),
        parent,
    )
    try:
        box.yesButton.setText(tr.t("wizard.ok"))
        box.cancelButton.hide()
    except Exception:
        pass
    box.exec()


# ---------------------------------------------------------------------------
# Environment banner
# ---------------------------------------------------------------------------

if _HAVE_QT:
    class EnvBanner(QFrame):
        """A persistent strip above the extract panel.

        cc-switch runs an environment check on start-up and shows what it
        found. The same information, in the same place, but as a strip
        that STAYS: an InfoBar that disappears after five seconds is a
        message the user has to be looking at exactly when it appears,
        and the failure it warns about ("no provider is configured") is
        one the user is about to hit.
        """

        def __init__(self, parent, translate, on_fix=None):
            super().__init__(parent)
            self._t = translate
            self._on_fix = on_fix
            self.setObjectName("envBanner")
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            lay = QHBoxLayout(self)
            lay.setContentsMargins(14, 8, 12, 8)
            lay.setSpacing(10)
            self._lbl = BodyLabel("")
            self._lbl.setWordWrap(True)
            lay.addWidget(self._lbl, 1)
            self._btn_fix = PushButton(self._t("app.envFix"))
            self._btn_fix.clicked.connect(self._fix_clicked)
            lay.addWidget(self._btn_fix)
            self.setVisible(False)

        def _fix_clicked(self):
            if self._on_fix is not None:
                self._on_fix()

        def set_warnings(self, keys: list[str]) -> None:
            """Show the given i18n keys, or hide the strip entirely."""
            self._keys = [k for k in keys if k]
            self._render()

        def retranslate(self) -> None:
            """Re-render in the current language.

            The CHECKS are language-independent; the WORDS are not. A
            banner left in the previous language is a banner the user
            cannot read, and the language switch is a live switch.
            """
            self._render()

        def _render(self) -> None:
            keys = getattr(self, "_keys", []) or []
            self._btn_fix.setText(self._t("app.envFix"))
            if not keys:
                self.setVisible(False)
                self._lbl.setText("")
                return
            self._lbl.setText("  ".join(self._t(k) for k in keys))
            self.setVisible(True)
            self.setStyleSheet(
                "#envBanner{background:rgba(245,158,11,0.14);"
                "border:1px solid rgba(245,158,11,0.45);border-radius:8px;}"
            )
