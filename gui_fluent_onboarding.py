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


# ---------------------------------------------------------------------------
# What's new — shown on the first launch after an upgrade
# ---------------------------------------------------------------------------

def last_seen_version_path() -> str:
    return os.path.join(
        os.path.expanduser("~"), ".range_chart_analyzer", "last_seen_version"
    )


def read_last_seen_version() -> str:
    """The version the user last saw, or "" when we do not know."""
    try:
        with open(last_seen_version_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_last_seen_version(version: str) -> None:
    try:
        os.makedirs(os.path.dirname(last_seen_version_path()), exist_ok=True)
        with open(last_seen_version_path(), "w", encoding="utf-8") as f:
            f.write(version or "")
    except OSError:
        pass


def changelog_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "CHANGELOG.md")


def _version_tuple(text: str):
    """'0.1.2' -> (0, 1, 2). Unparseable -> None."""
    parts = []
    for chunk in (text or "").strip().lstrip("v").split("."):
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return tuple(parts) or None


def _bullet_title(text: str) -> str:
    """The one-line title of a changelog bullet.

    The changelog's bullets are written as ``**Title.** body``, with the
    title often wrapped over two lines. A dialog that shows the body
    paragraph is unreadable; the title is the thing the user scans.
    """
    flat = " ".join(part.strip() for part in text.split("\n")).strip()
    if flat.startswith("**"):
        end = flat.find("**", 2)
        if end > 0:
            return flat[2:end].strip().rstrip(".")
    # No bold lead: take the first sentence, capped so a run-on line
    # cannot become a paragraph in a dialog.
    stop = flat.find(". ")
    if 0 < stop <= 120:
        return flat[:stop].strip()
    return flat[:120].strip()


def changelog_entries_since(version: str, limit: int = 12) -> list[str]:
    """The titles of the changelog entries added after ``version``.

    Returns an empty list when the changelog is missing (a packaged build
    may not ship it), when the file cannot be parsed, or when nothing new
    was added. The comparison is by version number, and the ``[Unreleased]``
    section counts as newer than every released version.
    """
    target = _version_tuple(version)
    try:
        with open(changelog_path(), "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return []

    out: list[str] = []
    current_new = False
    bullet: list[str] = []
    in_bullet = False

    def _flush():
        nonlocal bullet, in_bullet
        if in_bullet:
            out.append(_bullet_title("\n".join(bullet)))
        bullet = []
        in_bullet = False

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("## ["):
            _flush()
            head = line[3:].lstrip("[").split("]", 1)[0].strip()
            if head.lower() == "unreleased":
                current_new = True
                continue
            found = _version_tuple(head)
            if found is None:
                current_new = False
                continue
            # "Newer than what the user last saw" -- and the section must
            # not be one they have already read.
            current_new = (target is None) or (found > target)
            continue
        if line.startswith("### "):
            _flush()
            continue
        if line.startswith("- "):
            _flush()
            if current_new:
                bullet = [line[2:].strip()]
                in_bullet = True
            continue
        if in_bullet:
            if not line.strip():
                _flush()
                continue
            if line.startswith("  ") or line.startswith("\t"):
                bullet.append(line.strip())
                continue
            _flush()
    _flush()
    return out[:limit]


def maybe_show_whats_new(win, tr: Translator) -> None:
    """Tell the user what changed since the last time they ran the app.

    The rule is the cc-switch one: show it when the VERSION moved, not
    every launch, and not on the very first launch (the welcome dialog
    already covers that).
    """
    if not _HAVE_QT:
        return
    last = read_last_seen_version()
    if not last or last == __version__:
        if not last:
            write_last_seen_version(__version__)
        return
    try:
        entries = changelog_entries_since(last)
    except Exception:
        entries = []
    write_last_seen_version(__version__)
    if not entries:
        return
    try:
        dlg = WhatsNewDialog(win, tr, last, entries)
        dlg.exec()
    except Exception:
        pass


if _HAVE_QT:
    class WhatsNewDialog(MessageBox):
        """The "what changed" dialog shown after an upgrade."""

        def __init__(self, parent, tr: Translator, last_version: str,
                     entries: list[str]) -> None:
            super().__init__(
                tr.t("app.whatsNewTitle"),
                f"{tr.t('about.version')} {last_version} → {__version__}",
                parent,
            )
            body = QWidget()
            v = QVBoxLayout(body)
            v.setContentsMargins(8, 4, 8, 4)
            v.setSpacing(8)
            for entry in entries:
                row = QWidget()
                rl = QHBoxLayout(row)
                rl.setContentsMargins(0, 0, 0, 0)
                rl.setSpacing(8)
                dot = QLabel("•")
                dot.setFixedWidth(14)
                rl.addWidget(dot)
                lbl = BodyLabel(entry)
                lbl.setWordWrap(True)
                rl.addWidget(lbl, 1)
                v.addWidget(row)
            try:
                self.yesButton.setText(tr.t("onboarding.gotIt"))
                self.cancelButton.hide()
            except Exception:
                pass
            self.textLayout.addWidget(body)


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
