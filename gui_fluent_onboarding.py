"""Onboarding dialog + About page content.

The Onboarding dialog is shown once on first launch (a marker file is
written so it doesn't show again). It's deliberately small — three
steps and a "Got it" button. The About page lists version, license,
and links; the link rows are clickable QLabel-like buttons.
"""

from __future__ import annotations

import os
from typing import Optional

# PySide6 imports are wrapped so the marker-path helpers can still be
# unit-tested in a headless environment. The Qt widget classes are
# only referenced at module-import time; if PySide6 is missing the
# calling code (gui_fluent.py) catches it and falls back to the legacy
# About body.
try:
    from PySide6.QtCore import Qt, QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    )
    from qfluentwidgets import (
        TitleLabel, SubtitleLabel, BodyLabel, CaptionLabel,
        StrongBodyLabel, PrimaryPushButton, PushButton,
        CardWidget, FluentIcon as FIF, MessageBox, InfoBar, InfoBarPosition,
        ScrollArea,
    )
    _HAVE_QT = True
except ImportError:
    _HAVE_QT = False

from rca_core import __version__, Translator


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

def onboarding_marker_path() -> str:
    return os.path.join(
        os.path.expanduser("~"), ".range_chart_analyzer", "onboarding_seen"
    )


def has_seen_onboarding() -> bool:
    try:
        return os.path.isfile(onboarding_marker_path())
    except Exception:
        return True   # fail open: don't show if we can't tell


def mark_onboarding_seen() -> None:
    try:
        os.makedirs(os.path.dirname(onboarding_marker_path()), exist_ok=True)
        with open(onboarding_marker_path(), "w", encoding="utf-8") as f:
            f.write("seen")
    except Exception:
        pass


def maybe_show_onboarding(win, tr: Translator) -> None:
    """Show the welcome dialog the first time the app launches."""
    if not _HAVE_QT:
        return
    if has_seen_onboarding():
        return
    try:
        dlg = OnboardingDialog(win, tr)
        dlg.exec()
    finally:
        mark_onboarding_seen()


if _HAVE_QT:
    class OnboardingDialog(MessageBox):
        """First-launch welcome dialog with three setup steps."""

        def __init__(self, parent, tr: Translator) -> None:
            super().__init__(tr.t("onboarding.welcome"), tr.t("onboarding.intro"), parent)
            # Replace the simple content with a richer card-style body.
            body = QWidget()
            v = QVBoxLayout(body)
            v.setContentsMargins(8, 4, 8, 4)
            v.setSpacing(10)

            for key in ("step1", "step2", "step3"):
                title = tr.t(f"onboarding.{key}.title")
                desc = tr.t(f"onboarding.{key}.desc")
                row = CardWidget()
                rl = QVBoxLayout(row)
                rl.setContentsMargins(14, 10, 14, 10)
                rl.addWidget(StrongBodyLabel(title))
                rl.addWidget(BodyLabel(desc))
                v.addWidget(row)

            # Hide the default yes/no buttons; we just want an OK button.
            try:
                self.yesButton.setText(tr.t("onboarding.gotIt"))
                self.cancelButton.hide()
            except Exception:
                pass
            self.textLayout.addWidget(body)

        # Always mark as seen on close.
        def closeEvent(self, event):
            mark_onboarding_seen()
            super().closeEvent(event)


# ---------------------------------------------------------------------------
# About page
# ---------------------------------------------------------------------------

ABOUT_LINKS = [
    ("GitHub", "https://github.com/syxscott/Range-chart-Analyzer"),
    ("MiniMax M3 API", "https://platform.minimaxi.com"),
]


def _build_about(win, tr: Translator):
    if not _HAVE_QT:
        return None
    page = QWidget()
    page.setObjectName("aboutPage")
    lay = QVBoxLayout(page)
    lay.setContentsMargins(28, 24, 28, 28)
    lay.setSpacing(10)

    title = TitleLabel(tr.t("about.appName"))
    version = CaptionLabel(f"{tr.t('about.version')}: {__version__}")
    desc = BodyLabel(tr.t("about.description"))
    desc.setWordWrap(True)

    lay.addWidget(title)
    lay.addWidget(version)
    lay.addSpacing(6)
    lay.addWidget(desc)
    lay.addSpacing(16)

    # License / tech card
    tech_card = CardWidget()
    tv = QVBoxLayout(tech_card)
    tv.setContentsMargins(20, 14, 20, 14)
    tv.setSpacing(6)
    tech_lbl = StrongBodyLabel(tr.t("about.license"))
    tech_body = BodyLabel("MIT License · Python 3.9+ · PySide6 + qfluentwidgets")
    tv.addWidget(tech_lbl)
    tv.addWidget(tech_body)
    lay.addWidget(tech_card)

    # Links card
    links_card = CardWidget()
    lv = QVBoxLayout(links_card)
    lv.setContentsMargins(20, 14, 20, 14)
    lv.setSpacing(8)
    lv.addWidget(StrongBodyLabel(tr.t("about.links")))
    for label, url in ABOUT_LINKS:
        btn = PushButton(label)
        btn.setIcon(FIF.LINK)
        btn.clicked.connect(lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))
        lv.addWidget(btn)
    lay.addWidget(links_card)

    lay.addStretch(1)
    return page
