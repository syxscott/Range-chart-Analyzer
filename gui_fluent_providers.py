"""gui_fluent_providers.py — modern provider management UI (cc-switch alignment).

ProviderCard: a single provider card with cc-switch design (icon tile,
name + status dot + endpoint row, hover-highlighted action buttons,
active/current blue gradient, drag handle).

ProviderDragList: vertical list of ProviderCards with press-and-drag
reordering. Emits `orderChanged(providerIds)` on a successful re-order.

ProviderWizard: modal QDialog with a searchable preset grid on step 1
and a form-filled detail view on step 2. Returns `.created_provider`.

ProvidersPage: scrollable QScrollArea page that hosts the Providers list,
add button, threaded connection-test, i18n live switch, config persist.
"""

from __future__ import annotations

import logging

log = logging.getLogger("rca.gui_fluent_providers")

from PySide6.QtCore import (
    Qt, QMimeData, QPoint, QSize, Signal, QTimer,
)
from PySide6.QtGui import QDrag, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QLineEdit, QMenu,
)

# AUDIT-2026-09-27 [item B-13] (B-13): PillPushButton / TransparentToolButton /
# qconfig are new here. The first two replace the four hand-styled action
# buttons (hardcoded light greys, invisible on an OS dark theme) with
# components that paint themselves from the active theme; `qconfig` is where
# qfluentwidgets publishes themeChanged, which NOTHING in this repo was
# subscribed to, so a live OS theme switch never re-applied any of the
# hand-written setStyleSheet rules below. `isDarkTheme` (imported but unused
# until now) is the branch the card's own border uses.
from qfluentwidgets import (
    CardWidget, ComboBox, FluentIcon as FIF, InfoBar, InfoBarPosition,
    LineEdit, MessageBox, PasswordLineEdit, PillPushButton, PrimaryPushButton,
    PushButton, ScrollArea, SearchLineEdit, StrongBodyLabel, BodyLabel,
    CaptionLabel, TitleLabel, ToolButton, TransparentToolButton, isDarkTheme,
    qconfig,
)

from rca_core import (
    PROVIDER_PRESETS, ApiFormat, LlmProvider, ProviderStore, Translator,
)
from rca_core.extractor import DEFAULT_ENDPOINT, DEFAULT_MODEL
from rca_core.llm import (REASONING_DEFAULT, REASONING_LEVELS,
                          test_llm_connection)
from rca_core.ssrf import validate_endpoint as _validate_endpoint


class _NoPreset:
    """Sentinel for "no preset applies" (editing an existing provider).

    API-REVIEW-2026-10-18: ProviderPreset carries doc_url / api_key_url /
    endpoint_candidates, and _apply_preset_meta needs one argument rather
    than a branch at every call site. A duck-typed stub keeps the module
    importable without PySide6 (the tests exercise the helper directly).
    """

    name = ""
    endpoint = ""
    extra_headers: dict = {}
    doc_url = ""
    api_key_url = ""
    api_key_hint = "API Key"
    endpoint_candidates: list = []


_NO_PRESET = _NoPreset()


def _icon_char(name: str) -> str:
    """Derive a 1–2 char glyph from the provider name for the icon tile."""
    if not name:
        return "?"
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


def _store_signature() -> str:
    """A digest of the providers file, or "" when it does not exist.

    REVIEW-2026-10-20 (cc-switch's external-change guard): the providers
    file is a plain JSON file in the user's profile, so a text editor, a
    sync client or a second copy of the app can change it while this one
    has a modal open. Whichever version is written last wins, and the
    loser is silent. A digest taken when the dialog opened, compared with
    the one on disk when it closes, is what turns that silent loss into a
    question the user can answer.
    """
    import hashlib
    try:
        with open(ProviderStore().path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Per-provider icon tile (colored rounded rectangle + glyph)
# ---------------------------------------------------------------------------
class ProviderIconTile(QLabel):
    def __init__(self, name, parent=None, icon=""):
        super().__init__(icon.strip() if icon and icon.strip()
                         else _icon_char(name), parent)
        self.setFixedSize(40, 40)
        self.setAlignment(Qt.AlignCenter)
        self.setObjectName("provIconTile")
        h = abs(hash(name or "")) % 360
        # REVIEW-2026-10-20 (cc-switch's custom provider logo): a custom
        # glyph gets a NEUTRAL tile, because the colour is derived from the
        # name -- and the whole point of a custom glyph is to tell two
        # same-named relays apart, which a name-keyed colour would undo.
        bg = "hsl(220,14%,46%)" if (icon and icon.strip()) \
            else f"hsl({h},55%,47%)"
        self.setStyleSheet(
            f"#provIconTile{{background-color:{bg};"
            f"color:#fff;border-radius:9px;"
            f"font:650 13px 'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}}"
        )


# ---------------------------------------------------------------------------
# Drag handle (⠿) — left of each card; press-and-drag starts a reorder
# ---------------------------------------------------------------------------
class DragHandle(QLabel):
    """Initiates a QDrag against the parent card on press-and-drag."""

    def __init__(self, card: "ProviderCard", parent=None):
        super().__init__("⠿", parent)   # ⠿ braille drag glyph
        self._card = card
        self.setCursor(Qt.OpenHandCursor)
        self.setStyleSheet("color:rgba(120,120,120,0.55);padding:0 6px;")
        self._start: QPoint | None = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = event.pos()

    def mouseMoveEvent(self, event):
        if self._start is None:
            return
        if (event.pos() - self._start).manhattanLength() < QApplication.startDragDistance():
            return
        card = self._card
        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(card.provider.id if card.provider else "")
        drag.setMimeData(mime)
        pix = QPixmap(card.size())
        card.render(pix)
        drag.setPixmap(pix)
        drag.setHotSpot(event.pos())
        drag.exec(Qt.MoveAction)
        self._start = None


# ---------------------------------------------------------------------------
# HealthBadge — shows upstream health/consecutive-failures status
# ---------------------------------------------------------------------------
class HealthBadge(QLabel):
    """Colored badge showing consecutive failure count, matching cc-switch style."""

    def __init__(self, consecutive_failures: int = 0, parent=None):
        super().__init__(parent)
        self._failures = consecutive_failures
        self._update_style()

    def set_failures(self, failures: int):
        self._failures = failures
        self._update_style()

    def _update_style(self):
        if self._failures <= 0:
            self.setText("✓")
            self.setStyleSheet("color:#22c55e;font-size:12px;font-weight:bold;")
        elif self._failures == 1:
            self.setText("⚠")
            self.setStyleSheet("color:#f59e0b;font-size:12px;font-weight:bold;")
        elif self._failures == 2:
            self.setText("✗")
            self.setStyleSheet("color:#ef4444;font-size:12px;font-weight:bold;")
        else:
            self.setText(f"✗{self._failures}")
            self.setStyleSheet("color:#dc2626;font-size:11px;font-weight:bold;")


# ---------------------------------------------------------------------------
# ProviderCard — cc-switch styled, hover-reveal action buttons
# ---------------------------------------------------------------------------
class ProviderCard(CardWidget):
    def __init__(self, provider: LlmProvider, is_active: bool,
                 translate, parent=None):
        super().__init__(parent)
        self.provider = provider
        self._t = translate
        self._active = is_active
        self._testing = False
        # Hydrate the in-memory badge state from the persisted
        # consecutive_failures count so the badge survives app restart.
        # Clamp to the same range the GUI displays (≤3) — values above
        # that would only show as "✗3+" so loading a stale 9 from disk
        # would visually look identical to a fresh 3 anyway.
        self._health_failures = min(int(getattr(provider, "consecutive_failures", 0) or 0), 3)
        self.setObjectName("providerCard")
        # Use min+max instead of fixed height so the card can grow if the provider
        # name is long (font scaling, translations) without clipping content.
        self.setMinimumHeight(72)
        # REVIEW-2026-10-20: the card grew two rows (the provider's note and
        # the plan's quota chip), so the old 120 px ceiling clipped them.
        self.setMaximumHeight(160)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setAttribute(Qt.WA_Hover, True)
        # AUDIT-2026-09-27 [item B-13] (B-13): the action row was revealed by
        # enterEvent ONLY, and Qt skips setVisible(False) subtrees when it
        # builds the focus chain — so a keyboard user could Tab onto a card
        # and reach none of Test / Set active / Edit / Delete. The card is a
        # tab stop now, and focusInEvent reveals the same row for the
        # keyboard path (enterEvent is untouched for the mouse path).
        self.setFocusPolicy(Qt.StrongFocus)

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 8, 4, 10)
        row.setSpacing(10)

        self.drag = DragHandle(self, self)
        row.addWidget(self.drag)

        self.icon_tile = ProviderIconTile(
            provider.name, icon=getattr(provider, "icon", "") or "")
        row.addWidget(self.icon_tile)

        mid = QVBoxLayout()
        mid.setSpacing(1)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.lbl_name = StrongBodyLabel(
            provider.name or translate("settings.llmProvider"))
        name_row.addWidget(self.lbl_name)
        # AUDIT-2026-09-27 [item B-13] (B-13): this was a bare "●"/"○"
        # CaptionLabel in a hardcoded #2563eb / rgba(120,120,120,0.7),
        # carrying the single most important state in the app (which provider
        # Extract will use) with no text and no accessible name — invisible
        # to a screen reader and unreadable in a dark theme. It is now a
        # PillPushButton, which paints itself from the active theme, reading
        # settings.currentProvider ("当前" / "Active") and shown only while
        # THIS card is the active provider. The attribute keeps its old name
        # so nothing outside this file has to learn a new one.
        self.lbl_dot = PillPushButton(translate("settings.currentProvider"))
        self.lbl_dot.setFocusPolicy(Qt.NoFocus)   # a state pill, not an action
        self.lbl_dot.setAccessibleName(translate("settings.currentProvider"))
        self.lbl_dot.setToolTip(translate("settings.activeConfigHint"))
        self.lbl_dot.setVisible(is_active)
        name_row.addWidget(self.lbl_dot)
        self._health_badge = HealthBadge(self._health_failures)
        name_row.addWidget(self._health_badge)
        name_row.addStretch(1)
        mid.addLayout(name_row)
        sub = provider.endpoint or "(" + translate("provider.noEndpoint") + ")"
        self.lbl_sub = CaptionLabel(sub)
        mid.addWidget(self.lbl_sub)

        # REVIEW-2026-10-20 (cc-switch): the provider's own note, and the
        # plan's remaining quota, on the card itself.
        #
        # The note is what turns five relay accounts from five opaque names
        # into five identifiable ones -- "monthly, expires in March" -- and
        # cc-switch shows it in the same place.
        #
        # The quota chip is the same idea as cc-switch's UsageFooter: the
        # number that decides whether the next extraction will even start.
        # It is filled in ASYNCHRONOUSLY (the page's quota worker), because
        # the query is a network round trip and the card is built on the UI
        # thread.
        self.lbl_note = CaptionLabel("")
        self.lbl_note.setVisible(False)
        mid.addWidget(self.lbl_note)
        # The note belongs to the CARD, so the card reads it: a caller that
        # forgets to set it would show a card with a saved note and no
        # trace of it, and the user would retype what was already there.
        self.set_note(getattr(provider, "notes", "") or "")
        self.lbl_quota = CaptionLabel("")
        self.lbl_quota.setVisible(False)
        mid.addWidget(self.lbl_quota)
        row.addLayout(mid, 1)

        # Action buttons — hidden until the card is hovered OR focused
        # (cc-switch style; see the setFocusPolicy note above).
        self._actions_widget = QWidget()
        self._actions_layout = QHBoxLayout(self._actions_widget)
        self._actions_layout.setContentsMargins(0, 0, 0, 0)
        self._actions_layout.setSpacing(4)
        self._actions_widget.setVisible(False)

        # AUDIT-2026-09-27 [item B-13] (B-13): btn_test / btn_active were
        # PushButtons whose setStyleSheet hardcoded LIGHT greys (#475569 text
        # on #f1f5f9 / #e2e8f0), while btn_edit / btn_delete used a lighter
        # #94a3b8: under setTheme(Theme.AUTO) with an OS dark theme the first
        # pair fell to ~1.6:1 contrast while the second stayed readable — the
        # same control in two visual states. All four are now qfluentwidgets
        # components that paint themselves from the active theme, so there is
        # no colour left here to go stale. Each also gets a focus policy (they
        # are the tab targets inside the revealed row), an accessible name
        # and a tooltip, because an icon-only button is otherwise announced
        # as "button" with no label. The trade-off: the delete button's custom
        # red hover is gone — re-adding it would mean hardcoding a hex again
        # or overriding the component's own paintEvent.
        self.btn_test = PillPushButton(translate("settings.testConnection"))
        self.btn_test.setFixedHeight(28)
        self.btn_active = PillPushButton(translate("wizard.setActive"))
        self.btn_active.setFixedHeight(28)
        self.btn_edit = TransparentToolButton(FIF.EDIT)
        self.btn_edit.setFixedSize(28, 28)
        self.btn_delete = TransparentToolButton(FIF.DELETE)
        self.btn_delete.setFixedSize(28, 28)
        for _btn, _name in (
            (self.btn_test, translate("settings.testConnection")),
            (self.btn_active, translate("wizard.setActive")),
            (self.btn_edit, translate("wizard.configure")),
            (self.btn_delete, translate("wizard.delete")),
        ):
            _btn.setFocusPolicy(Qt.StrongFocus)
            _btn.setAccessibleName(_name)
            _btn.setToolTip(_name)
        self._actions_layout.addWidget(self.btn_test)
        self._actions_layout.addWidget(self.btn_active)
        self._actions_layout.addWidget(self.btn_edit)
        self._actions_layout.addWidget(self.btn_delete)
        row.addWidget(self._actions_widget)

        # AUDIT-2026-09-27 [item B-13] (B-13): a BOUND METHOD, not a lambda —
        # PySide6 gives the connection this card's QObject as its context, so
        # Qt drops it when the card is destroyed. A lambda here would keep
        # every card ever built alive for the life of the process, which is
        # the leak B-11 spends its time removing.
        qconfig.themeChanged.connect(self._apply_theme)

        self.set_active(is_active)

    def _apply_theme(self, *_args) -> None:
        """Re-apply the card's OWN border/background for the current theme.

        AUDIT-2026-09-27 [item B-13] (B-13): the inactive card used
        ``border:1px solid rgba(0,0,0,0.08)`` — a black hairline at 8% alpha,
        i.e. no border at all on a dark background, and nothing in the repo
        listened for a theme change, so it could not recover. The idle /
        hover border colours now branch on isDarkTheme(), and this method is
        connected to qconfig.themeChanged (see __init__), so a live OS theme
        switch re-applies them. The four action buttons need no equivalent:
        they are themed components now.
        """
        dark = isDarkTheme()
        idle = "rgba(255,255,255,0.14)" if dark else "rgba(0,0,0,0.08)"
        hover = "rgba(96,165,250,0.45)" if dark else "rgba(37,99,235,0.3)"
        if self._active:
            self.setStyleSheet(
                "#providerCard{border:1.5px solid rgba(37,99,235,0.55);"
                "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 rgba(37,99,235,0.08),stop:1 transparent)}"
            )
        else:
            self.setStyleSheet(
                f"#providerCard{{border:1px solid {idle}}}"
                f"#providerCard:hover{{border-color:{hover}}}"
            )

    def set_active(self, active: bool):
        self._active = active
        # B-13: the state pill carries the meaning the ●/○ glyph used to.
        self.lbl_dot.setVisible(active)
        self._apply_theme()
        # Unchanged behaviour: "set as active" is meaningless on the card
        # that already is. (setVisible(True) on a child of the still-hidden
        # action row just clears the explicit-hide flag; the row reveals it.)
        self.btn_active.setVisible(not active)

    def set_testing(self, testing: bool):
        self._testing = testing
        self.btn_test.setEnabled(not testing)
        self.btn_test.setText("⏳" if testing else self._t("settings.testConnection"))
        # B-13: the ⏳ glyph is decorative — a screen reader needs the word.
        self.btn_test.setAccessibleName(
            self._t("settings.testing") if testing
            else self._t("settings.testConnection"))
        self.btn_test.setToolTip(self.btn_test.accessibleName())

    def set_health(self, consecutive_failures: int):
        """Update the health badge from connection test result."""
        self._health_failures = consecutive_failures
        self._health_badge.set_failures(consecutive_failures)

    def set_note(self, note: str) -> None:
        """Show the provider's own note (cc-switch shows it on the card)."""
        note = (note or "").strip()
        self.lbl_note.setText(note)
        self.lbl_note.setVisible(bool(note))

    def set_quota(self, result) -> None:
        """Show the plan's remaining quota, or say that it is unavailable.

        The two windows are shown separately because they are separately
        binding: MiniMax's Token Plan has a 5-hour bucket that refills
        quickly and a weekly bucket that does not, and "1% left" against
        the wrong one is either panic or false comfort.
        """
        from rca_core.llm import QuotaResult

        if not isinstance(result, QuotaResult) or not result.ok:
            # A provider that publishes no quota is NOT an error — there is
            # nothing to say, so nothing is said. Only the tiers we can
            # actually read are rendered.
            self.lbl_quota.setVisible(False)
            return
        bits = []
        for tier in result.tiers:
            if tier.name == "five_hour":
                bits.append(self._t("provider.quota5h").format(
                    pct=self._pct_left(tier)))
            elif tier.name == "weekly_limit":
                bits.append(self._t("provider.quotaWeekly").format(
                    pct=self._pct_left(tier)))
        if not bits:
            self.lbl_quota.setVisible(False)
            return
        self.lbl_quota.setText(" · ".join(bits))
        self.lbl_quota.setVisible(True)

    @staticmethod
    def _pct_left(tier) -> int:
        """How much of the window is LEFT, as a whole percent.

        The API reports the USED share, and a chip that says "1% used" when
        the user means "1% left" is a sign error with real consequences.
        """
        used = float(getattr(tier, "used_percent", 0.0) or 0.0)
        return int(round(max(0.0, 100.0 - used)))

    def _actions_should_show(self) -> bool:
        """Visible while the mouse is over the card OR the keyboard focus is
        anywhere inside it (the card itself or one of its action buttons)."""
        fw = QApplication.focusWidget()
        return self.underMouse() or (
            fw is not None and (fw is self or self.isAncestorOf(fw)))

    def _sync_actions_visibility(self) -> None:
        self._actions_widget.setVisible(self._actions_should_show())

    def enterEvent(self, event):
        """Show action buttons when mouse hovers over card (mouse path)."""
        self._actions_widget.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Hide action buttons when the mouse leaves — unless the keyboard
        focus is still inside the card, which is how the row stays reachable
        after a Tab (B-13)."""
        super().leaveEvent(event)
        QTimer.singleShot(0, self._sync_actions_visibility)

    def focusInEvent(self, event):
        """B-13: the keyboard path into the action row. Without this a Tab
        that lands on a card revealed nothing at all."""
        self._actions_widget.setVisible(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        """B-13: hand the row back when focus leaves the card. Deferred by one
        event-loop turn because Qt delivers focusOut BEFORE the next widget's
        focusIn, so QApplication.focusWidget() is still this card here."""
        super().focusOutEvent(event)
        QTimer.singleShot(0, self._sync_actions_visibility)


# AUDIT-2026-09-27 [item B-12] (B-12): the floor for a card's width. It used
# to be the seed of `setFixedWidth(max(400, self.width()))` in set_cards(),
# where the FIRST call happens inside ProvidersPage.__init__ — before the page
# has been laid out — so self.width() was still Qt's default and the result
# was pinned at 400px for the whole session in a 1240px window. The width is
# now owned by resizeEvent()/_fit_cards() alone; this is only its minimum.
CARD_MIN_WIDTH = 400


# ---------------------------------------------------------------------------
# ProviderDragList — vertical list with drag-to-reorder
# ---------------------------------------------------------------------------
class ProviderDragList(QWidget):
    orderChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._cards: list[ProviderCard] = []
        # AUDIT-2026-09-27 [item B-11] (B-11): cards that set_cards() took out
        # of the layout but could NOT delete yet, because a connection test
        # is still running against them. They wait here and are released by
        # flush_parked() when that test finishes — the same park-until-finished
        # shape the other GUI suites use for orphaned workers
        # (gui_fluent._park_orphaned_worker), applied to a widget.
        self._parked: list[ProviderCard] = []
        # Predicate installed by the page: True => this card is still driving
        # an in-flight test, so it must outlive the refresh that replaced it.
        self._retain = None
        self.setAcceptDrops(True)

    def set_retain_hook(self, fn) -> None:
        """Install the "still in use" predicate (see ``_parked``)."""
        self._retain = fn

    def _release_card(self, w) -> None:
        """Detach a card from the list and let it go — or park it."""
        w.setParent(None)
        if self._retain is not None and self._retain(w):
            self._parked.append(w)     # a test is still using it
        else:
            w.deleteLater()

    def flush_parked(self) -> None:
        """deleteLater() every parked card no test is using any more.

        Driven by ProvidersPage._release_parked_cards, i.e. exactly when the
        last reason to keep a card alive has gone away.
        """
        if not self._parked:
            return
        keep = []
        for c in self._parked:
            if self._retain is not None and self._retain(c):
                keep.append(c)
            else:
                c.deleteLater()
        self._parked = keep

    # AUDIT-2026-09-27 [item B-12] (B-12): the ONE owner of the card width.
    # Nothing else in this file may set it.
    def _fit_cards(self) -> None:
        w = max(CARD_MIN_WIDTH, self.width())
        for c in self._cards:
            if c.width() != w:
                c.setFixedWidth(w)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_cards()

    def set_cards(self, cards: list[ProviderCard]) -> None:
        """Replace the list's cards, releasing the ones being dropped.

        AUDIT-2026-09-27 [item B-11] (B-11): the old loop only did
        ``setParent(None)``, so every refresh (language switch, add, delete,
        reorder) orphaned the previous cards: the C++ widget survived for as
        long as anything still referenced it — including a connection test
        in flight, which kept an invisible card alive. Cards that no test is
        using are now deleteLater()'d; the rest are parked (see
        ``_release_card``). A widget that is in ``cards`` already (a caller
        re-showing the same card objects) is only re-added, never released.
        """
        incoming = {id(c) for c in cards}
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is None or id(w) in incoming:
                continue
            self._release_card(w)
        self._cards = list(cards)
        for c in cards:
            c.setParent(self)
            self._layout.addWidget(c)

    def _id_order(self) -> list[str]:
        return [c.provider.id for c in self._cards if c.provider]

    def _emit_order(self):
        self.orderChanged.emit(self._id_order())

    def dragEnterEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        src_id = event.mimeData().text().strip()
        if not src_id:
            event.ignore()
            return
        src_idx = next(
            (i for i, c in enumerate(self._cards)
             if c.provider and c.provider.id == src_id),
            None,
        )
        if src_idx is None:
            event.ignore()
            return
        card = self._cards.pop(src_idx)
        drop_y = event.pos().y()
        insert_idx = next(
            (i for i, c in enumerate(self._cards) if c.geometry().center().y() > drop_y),
            len(self._cards),
        )
        self._cards.insert(insert_idx, card)
        # Re-layout the SAME cards: unlike set_cards() this is a reorder, not
        # a teardown, so nothing here may be deleteLater()'d (B-11). Width is
        # owned by resizeEvent() only (B-12).
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        for c in self._cards:
            c.setParent(self)
            self._layout.addWidget(c)
        self._emit_order()
        event.acceptProposedAction()


# ---------------------------------------------------------------------------
# External-change conflict
# ---------------------------------------------------------------------------


class _ConflictDialog(QDialog):
    """Ask what to do when the providers file changed underneath us.

    Three answers, because there are three real outcomes and a two-button
    dialog cannot express them:

    * keep the file on disk and drop the edit in front of the user;
    * overwrite the file with the edit;
    * do nothing at all and leave the user to decide later.

    The middle one is the dangerous one, which is why it is not the
    default: the file on disk may hold a key the user pasted while this
    dialog was open, and overwriting it silently would destroy a paid
    credential.
    """

    KEEP_DISK = 1
    OVERWRITE = 2
    CANCEL = 0

    def __init__(self, parent, translate):
        super().__init__(parent)
        self._t = translate
        self.setWindowTitle(self._t("provider.conflictTitle"))
        self.resize(460, 200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        msg = BodyLabel(self._t("provider.conflictHint"))
        msg.setWordWrap(True)
        lay.addWidget(msg)
        row = QHBoxLayout()
        btn_cancel = PushButton(self._t("wizard.cancel"))
        btn_cancel.clicked.connect(self.reject)
        btn_keep = PushButton(self._t("provider.conflictReload"))
        btn_keep.clicked.connect(self._accept_keep)
        btn_over = PrimaryPushButton(self._t("provider.conflictOverwrite"))
        btn_over.clicked.connect(self._accept_overwrite)
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_keep)
        row.addWidget(btn_over)
        lay.addLayout(row)
        self._choice = self.CANCEL

    def _accept_keep(self):
        self._choice = self.KEEP_DISK
        self.accept()

    def _accept_overwrite(self):
        self._choice = self.OVERWRITE
        self.accept()

    def choice(self) -> int:
        return self._choice


# ---------------------------------------------------------------------------
# Endpoint speed test
# ---------------------------------------------------------------------------


class _EndpointSpeedDialog(QDialog):
    """Measure the candidate endpoints and keep the alternates.

    cc-switch's EndpointSpeedTest measures every candidate, colours the
    latency (<300 ms green, 300-500 yellow, 500-800 orange, >=800 red),
    can auto-select the fastest, and stores the custom endpoints on the
    provider so the next run can use them.

    The value here is lower than in cc-switch, and it is worth saying why:
    what decides how long an extraction takes is the MODEL'S THINKING, not
    the endpoint's round trip. What the round trip does tell you is whether
    the endpoint is alive and how far away it is -- and with several relay
    accounts, "which of them answers" is a real question.
    """

    def __init__(self, parent, endpoint="", custom=None, api_key="",
                 api_format="anthropic", translate=None):
        super().__init__(parent)
        # The translator is taken from the parent when it has one and is
        # passed in otherwise. Reaching into ``parent._t`` unconditionally
        # made the dialog unusable with a None parent, which is exactly
        # what a test wants to do.
        self._t = translate or getattr(parent, "_t", None) or (lambda k: k)
        self.setWindowTitle(self._t("wizard.speedTest"))
        self.resize(560, 420)
        self._rows: list[dict] = []
        self._custom: list[str] = [u for u in (custom or []) if u]
        self._selected: str = endpoint
        self._api_key = api_key
        self._api_format = api_format
        # The in-flight measurement, if any. Kept so a second click on
        # "run" does not start a second round of probes.
        self._worker = None
        seen = set()
        for url in [endpoint] + self._custom:
            if url and url not in seen:
                seen.add(url)
                self._rows.append({"url": url, "latency": None,
                                   "status": None, "error": ""})

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)

        self._auto = QCheckBox(self._t("wizard.speedAutoSelect"))
        lay.addWidget(self._auto)
        self._btn_run = PushButton(self._t("wizard.speedRun"))
        self._btn_run.clicked.connect(self._run)
        lay.addWidget(self._btn_run)

        self._list = QWidget()
        self._list_lay = QVBoxLayout(self._list)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(4)
        lay.addWidget(self._list)

        add_row = QHBoxLayout()
        self._ipt_new = LineEdit()
        self._ipt_new.setPlaceholderText("https://")
        btn_add = ToolButton(FIF.ADD)
        btn_add.setFixedSize(28, 28)
        btn_add.clicked.connect(self._add_endpoint)
        add_row.addWidget(self._ipt_new)
        add_row.addWidget(btn_add)
        add_row.addStretch(1)
        lay.addLayout(add_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = PushButton(self._t("wizard.cancel"))
        btn_cancel.clicked.connect(self.reject)
        btn_ok = PrimaryPushButton(self._t("wizard.save"))
        btn_ok.clicked.connect(self.accept)
        buttons.addWidget(btn_cancel)
        buttons.addWidget(btn_ok)
        lay.addLayout(buttons)
        self._rebuild()

    # -- the list ----------------------------------------------------------
    def _rebuild(self):
        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for row in self._rows:
            text = row["url"]
            colour = ""
            if row["latency"] is not None:
                text += "  ·  %d ms" % row["latency"]
                # cc-switch's thresholds: <300 ms green, 300-500 yellow,
                # 500-800 orange, >=800 red. The number is the point; the
                # colour is what makes a list of them scannable.
                lat = row["latency"]
                if lat < 300:
                    colour = "#16a34a"
                elif lat < 500:
                    colour = "#ca8a04"
                elif lat < 800:
                    colour = "#ea580c"
                else:
                    colour = "#dc2626"
            elif row["error"]:
                text += "  ·  " + row["error"]
                colour = "#dc2626"
            btn = PushButton(text)
            style = "text-align:left;padding-left:10px;"
            if colour:
                style += "color:%s;font-weight:600;" % colour
            if row["url"] == self._selected:
                style += "background:#eff6ff;border:1px solid #2563eb;"
            btn.setStyleSheet(style)
            btn.clicked.connect(lambda _=False, u=row["url"]: self._select(u))
            self._list_lay.addWidget(btn)

    def _select(self, url):
        self._selected = url
        self._rebuild()

    def _add_endpoint(self):
        url = self._ipt_new.text().strip().rstrip("/")
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            InfoBar.warning("", self._t("wizard.speedOnlyHttp"),
                            parent=self, position=InfoBarPosition.TOP)
            return
        if any(r["url"] == url for r in self._rows):
            InfoBar.warning("", self._t("wizard.speedDuplicate"),
                            parent=self, position=InfoBarPosition.TOP)
            return
        self._rows.append({"url": url, "latency": None, "status": None,
                            "error": ""})
        if url not in self._custom:
            self._custom.append(url)
        self._ipt_new.clear()
        self._rebuild()

    # -- the measurement ----------------------------------------------------
    def _run(self):
        """Measure every candidate endpoint, in the background.

        The measurement is a NETWORK round trip with a 12 s timeout per
        endpoint, so it cannot run on the GUI thread: with three
        candidates the dialog would be frozen for half a minute, and a
        frozen dialog is a dialog the user closes and never uses.
        """
        if self._worker is not None and self._worker.isRunning():
            return
        from PySide6.QtCore import QThread
        from rca_core.llm import ApiFormat, LlmProvider

        fmt = ApiFormat.ANTHROPIC
        try:
            fmt = ApiFormat(self._api_format)
        except ValueError:
            pass
        urls = [r["url"] for r in self._rows]

        class _SpeedWorker(QThread):
            measured = Signal(object, object, object, object)  # url, lat, status, err

            def __init__(self, urls, api_key, api_format, parent=None):
                super().__init__(parent)
                self._urls = urls
                self._key = api_key
                self._fmt = api_format

            def run(self):
                from rca_core.llm import measure_endpoint_latency
                for url in self._urls:
                    prov = LlmProvider(name="probe", api_format=self._fmt,
                                       endpoint=url, api_key=self._key,
                                       model="probe")
                    lat, status, err = measure_endpoint_latency(
                        prov, timeout_sec=12)
                    self.measured.emit(url, lat, status, err)

        # The worker is parented to the APPLICATION, not to the dialog:
        # closing the dialog must not destroy a running QThread (that is
        # a crash), and a worker that outlives its dialog is harmless --
        # its result simply goes nowhere.
        app = QApplication.instance()
        self._worker = _SpeedWorker(urls, self._api_key, fmt, parent=app)
        self._worker.measured.connect(self._on_measured)
        self._worker.finished.connect(self._on_measurement_done)
        self._btn_run.setEnabled(False)
        self._worker.start()

    def _on_measured(self, url, latency, status, err) -> None:
        """One endpoint's result -- GUI thread."""
        for row in self._rows:
            if row["url"] == url:
                row["latency"] = latency
                row["status"] = status
                row["error"] = err or ""
                break
        self._rebuild()

    def _on_measurement_done(self, *args) -> None:
        """All the endpoints are done -- pick the fastest if asked to."""
        self._btn_run.setEnabled(True)
        if self._auto.isChecked():
            good = [r for r in self._rows if r["latency"] is not None]
            if good:
                best = min(good, key=lambda r: r["latency"])
                self._selected = best["url"]
                self._rebuild()

    # -- the result --------------------------------------------------------
    def selected_url(self) -> str:
        return self._selected

    def custom_urls(self) -> list[str]:
        return list(self._custom)


# ---------------------------------------------------------------------------
# ProviderWizard — modal to add / edit a provider (preset grid -> form)
# ---------------------------------------------------------------------------
class ProviderWizard(QDialog):
    def __init__(self, parent, translate, existing: LlmProvider | None = None):
        super().__init__(parent)
        self._t = translate
        self.created_provider: LlmProvider | None = None
        self._existing = existing
        # Bug fix: preserve the original id + created_at across an edit so
        # LlmProvider.__post_init__ doesn't auto-generate a fresh UUID on
        # the new instance (which would make store.update() fail its
        # id-based lookup and silently drop the rename).
        self._edit_id: str = existing.id if existing else ""
        self._edit_created_at: float = existing.created_at if existing else 0
        if existing is None:
            self.setWindowTitle(self._t("wizard.choosePreset"))
        else:
            self.setWindowTitle(self._t("wizard.configureName").format(name=existing.name or ""))
        self.resize(720, 580)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(20, 16, 20, 16)
        self._root.setSpacing(12)

        # ---- step 1: search + preset grid ----
        self._search = SearchLineEdit(self)
        self._search.setPlaceholderText(self._t("settings.searchHint"))
        self._search.textChanged.connect(self._render_presets)
        self._root.addWidget(self._search)

        self._cat_row = QHBoxLayout()
        self._cat_buttons: list[tuple[PushButton, str]] = []
        self._root.addLayout(self._cat_row)

        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(360)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setSpacing(8)
        self._scroll.setWidget(self._grid_host)
        self._root.addWidget(self._scroll)

        # ---- step 2: details form (hidden at first) ----
        self._form = QWidget()
        fl = QGridLayout(self._form)
        fl.setContentsMargins(0, 12, 0, 0)
        fl.setSpacing(10)

        r = 0
        lbl = BodyLabel(self._t("wizard.fieldName"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_name = LineEdit()
        fl.addWidget(self._ipt_name, r, 0, 1, 2); r += 1

        lbl = BodyLabel(self._t("wizard.fieldFormat"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._cmb_fmt = ComboBox()
        self._cmb_fmt.addItems([f.value for f in ApiFormat])
        fl.addWidget(self._cmb_fmt, r, 0, 1, 2); r += 1

        lbl = BodyLabel(self._t("wizard.fieldEndpoint"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        endpoint_row = QHBoxLayout()
        self._ipt_endpoint = LineEdit()
        self._ipt_endpoint.setPlaceholderText("https://")
        # REVIEW-2026-10-20 (cc-switch's FullUrlToggle): a gateway whose path
        # is not the canonical one cannot be reached by appending /v1/... to a
        # prefix, so the endpoint may be declared to be the FULL request URL
        # and used verbatim. cc-switch gates the same switch behind the same
        # warning, for the same reason.
        self._chk_full_url = QCheckBox(self._t("wizard.fullUrl"))
        self._chk_full_url.setToolTip(self._t("wizard.fullUrlHelp"))
        # REVIEW-2026-10-20 (cc-switch's EndpointSpeedTest): measure which of
        # the candidate endpoints is fastest, and keep the alternates.
        self._btn_speed = ToolButton(FIF.SPEED_HIGH)
        self._btn_speed.setToolTip(self._t("wizard.speedTest"))
        self._btn_speed.setFixedSize(28, 28)
        self._btn_speed.clicked.connect(self._open_speed_test)
        endpoint_row.addWidget(self._ipt_endpoint)
        endpoint_row.addWidget(self._chk_full_url)
        endpoint_row.addWidget(self._btn_speed)
        endpoint_row.addStretch(1)
        fl.addLayout(endpoint_row, r, 0, 1, 2); r += 1

        lbl = BodyLabel(self._t("settings.apiKey"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_key = PasswordLineEdit()
        self._ipt_key.setPlaceholderText("sk-…")
        fl.addWidget(self._ipt_key, r, 0, 1, 2); r += 1

        # F-05 (REVIEW-2026-10-17): the "API Key field" combo
        # (ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY) was REMOVED, along
        # with its label. It was pure decoration: nothing in `_on_ok` ever
        # read `_cmb_key_field`, and `LlmProvider` (rca_core/llm.py) has no
        # field for it — so a user who switched it to ANTHROPIC_API_KEY got a
        # silently unchanged request header and no error. Adding the field
        # to the core dataclass instead would have been a schema change that
        # all three front-ends would have to understand, which costs far more
        # than the one dropdown was worth.

        lbl = BodyLabel(self._t("settings.model"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        model_row = QHBoxLayout()
        self._ipt_model = LineEdit()
        # REVIEW-2026-10-20 (cc-switch): "自动获取模型" -- a button that asks
        # the endpoint which models it serves and offers them in a menu.
        #
        # A wrong model name is the commonest configuration mistake, and it
        # fails LATE: the settings look fine, and the first extraction comes
        # back 404. cc-switch says it outright -- "模型名稱要對得上供應商 ...
        # 填錯會回 404 或回到非預期的模型" -- and its answer is to make the
        # endpoint list its own models instead of making the user copy a slug
        # off a web page. Measured against MiniMax's own endpoint: the
        # listing answers on both the Anthropic-compatible and the
        # OpenAI-compatible route and carries the real ids.
        self._btn_models = ToolButton(FIF.SYNC)
        self._btn_models.setToolTip(self._t("wizard.fetchModels"))
        self._btn_models.setFixedSize(28, 28)
        self._btn_models.clicked.connect(self._fetch_models)
        # cc-switch's ModelInputWithFetch puts the INPUT and the fetched-model
        # picker SIDE BY SIDE: the name can be typed OR picked, and the picker
        # filters as you type. A button that opens a menu and disappears
        # afterwards (the first version here) makes the second use a
        # round-trip. qfluentwidgets' ComboBox has no search of its own, so
        # the filtering is done here: the typed text narrows the list.
        self._cmb_models = ComboBox()
        self._cmb_models.setPlaceholderText(self._t("wizard.fetchModels"))
        self._cmb_models.setFixedWidth(200)
        self._cmb_models.setVisible(False)
        self._model_options: list[str] = []
        self._model_filtering = False
        model_row.addWidget(self._ipt_model)
        model_row.addWidget(self._cmb_models)
        model_row.addWidget(self._btn_models)
        model_row.addStretch(1)
        fl.addLayout(model_row, r, 0, 1, 2); r += 1
        # The worker that fetches the model list. Held so it is not garbage
        # collected while it is running -- a QThread destroyed mid-flight is
        # a crash, not a failed fetch.
        self._models_worker = None
        # REVIEW-2026-10-20 (cc-switch's GptContextWindowHint + the model
        # metadata fill): say what the model can do instead of letting the
        # user find out. The max-output number is MEASURED -- M3.1-Flash
        # answers a larger value with "does not support max tokens > 524288"
        # -- so typing 1000000 is a 400, and the limit belongs on screen.
        self._lbl_model_meta = CaptionLabel("")
        fl.addWidget(self._lbl_model_meta, r, 0, 1, 2); r += 1
        self._ipt_model.textChanged.connect(self._refresh_effort_options)
        self._ipt_model.textChanged.connect(self._refresh_model_meta)
        self._ipt_model.textChanged.connect(self._filter_model_options)
        # Picking a fetched model fills the field. The guard flag keeps the
        # two directions from chasing each other.
        self._cmb_models.currentTextChanged.connect(self._on_model_picked)

        # REVIEW-2026-10-19: reasoning effort.
        #
        # MiniMax's M3.1 family always thinks and its effort level DEFAULTS TO
        # ``max``; thinking tokens consume max_tokens, so on a dense figure
        # the thinking alone outgrew the budget and the call never answered
        # (measured: read timeout at 90 s, 2 of 2 runs, while the same call on
        # M3 -- whose thinking is off by default -- answered in 6.5-13.1 s).
        # ``low`` answered the same call in 28.3 s with a complete reading.
        #
        # This is a FIELD and not a hand-written extra_body blob on purpose:
        # the wizard rebuilds the provider from a fixed field list, so
        # anything smuggled into extra_body would be silently dropped the
        # moment the user edited this provider, and the product would revert
        # to the timing-out configuration with nothing on screen to say why.
        #
        # The LEVELS are the MODEL's, not the program's: minimax-code declares
        # them per model in its catalog and asks the runtime for the list
        # rather than hard-coding one, and M3 does not take an effort at all
        # (its thinking is an on/off switch). So the combo is repopulated
        # from the model name as it is typed.
        lbl = BodyLabel(self._t("wizard.fieldEffort"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._cmb_effort = ComboBox()
        # The combo shows a localized "default" row, so the VALUES are kept in
        # a parallel list -- an index into a translated label list would break
        # the moment the wording changed.
        #
        # The initial population is the UNIVERSAL level set; the model's own
        # map narrows it as soon as a model is known (see
        # _refresh_effort_options), because the levels a provider accepts are
        # the provider's, not the program's.
        self._effort_values = list(REASONING_LEVELS)
        self._cmb_effort.addItems([self._t("wizard.effortDefault")]
                                  + [lv for lv in REASONING_LEVELS
                                     if lv != REASONING_DEFAULT])
        fl.addWidget(self._cmb_effort, r, 0, 1, 2); r += 1
        self._ipt_model.textChanged.connect(self._refresh_effort_options)

        # ---- advanced section (collapsed by default) ---------------------
        #
        # REVIEW-2026-10-20 (cc-switch's collapsible fields): the extra
        # headers, the note, the icon and the profile are filled in ONCE per
        # account, while the name / endpoint / key / model are filled in
        # every time. Laid out flat, the four rarely-used rows pushed the
        # save button off the bottom of the 580 px dialog, and a field the
        # user cannot see is a field the user cannot fill in.
        self._btn_adv = PushButton("▸ " + self._t("wizard.advanced"))
        self._btn_adv.setCheckable(True)
        self._btn_adv.setFixedHeight(28)
        self._btn_adv.clicked.connect(self._toggle_advanced)
        fl.addWidget(self._btn_adv, r, 0, 1, 2); r += 1

        self._adv_panel = QWidget()
        al = QGridLayout(self._adv_panel)
        al.setContentsMargins(14, 4, 0, 0)
        al.setSpacing(10)
        ar = 0

        # Extra headers field
        lbl = BodyLabel(self._t("wizard.extraHeaders"))
        al.addWidget(lbl, ar, 0, 1, 2); ar += 1
        headers_row = QHBoxLayout()
        self._ipt_extra_headers = LineEdit()
        self._ipt_extra_headers.setPlaceholderText('{"X-Custom-Header": "value"}')
        # REVIEW-2026-10-20 (cc-switch's JsonEditor): a "format" button that
        # re-indents the JSON and reports WHERE it is wrong. The field has
        # always been raw JSON, and a typo in it fails at the request with
        # nothing pointing at the field.
        self._btn_format_headers = ToolButton(FIF.EDIT)
        self._btn_format_headers.setToolTip(self._t("wizard.formatJson"))
        self._btn_format_headers.setFixedSize(28, 28)
        self._btn_format_headers.clicked.connect(self._format_extra_headers)
        headers_row.addWidget(self._ipt_extra_headers)
        headers_row.addWidget(self._btn_format_headers)
        headers_row.addStretch(1)
        al.addLayout(headers_row, ar, 0, 1, 2); ar += 1

        # REVIEW-2026-10-20 (cc-switch): a free-text note on the provider --
        # what the account is for, which plan it is on, when it expires. The
        # card shows it and the list search finds it, which is what makes a
        # list of five relay accounts usable instead of five opaque names.
        lbl = BodyLabel(self._t("wizard.fieldNotes"))
        al.addWidget(lbl, ar, 0, 1, 2); ar += 1
        self._ipt_notes = LineEdit()
        self._ipt_notes.setPlaceholderText(self._t("wizard.notesHint"))
        al.addWidget(self._ipt_notes, ar, 0, 1, 2); ar += 1

        # REVIEW-2026-10-20 (cc-switch's custom provider logo): the card's
        # icon tile is derived from the provider's name, which gives five
        # relay accounts five identical two-letter tiles. One glyph fixes
        # that without an image pipeline, a file path to keep portable, or
        # a decoder to depend on.
        lbl = BodyLabel(self._t("wizard.fieldIcon"))
        al.addWidget(lbl, ar, 0, 1, 2); ar += 1
        self._ipt_icon = LineEdit()
        self._ipt_icon.setPlaceholderText(self._t("wizard.iconHint"))
        self._ipt_icon.setFixedWidth(90)
        icon_row = QHBoxLayout()
        icon_row.addWidget(self._ipt_icon)
        icon_row.addStretch(1)
        al.addLayout(icon_row, ar, 0, 1, 2); ar += 1

        # REVIEW-2026-10-20 (cc-switch's profiles): a provider belongs to a
        # named group, and the providers page can show one group at a time.
        lbl = BodyLabel(self._t("wizard.fieldProfile"))
        al.addWidget(lbl, ar, 0, 1, 2); ar += 1
        self._ipt_profile = LineEdit()
        self._ipt_profile.setPlaceholderText(self._t("wizard.profileHint"))
        al.addWidget(self._ipt_profile, ar, 0, 1, 2); ar += 1

        al.setColumnStretch(1, 1)
        self._adv_panel.setVisible(False)
        fl.addWidget(self._adv_panel, r, 0, 1, 2); r += 1

        # API-REVIEW-2026-10-18 (cc-switch comparison): ProviderPreset
        # carries doc_url / api_key_url / endpoint_candidates, and until
        # now NOTHING read them — a preset that names its own documentation
        # and key page was indistinguishable from one that doesn't, and the
        # backup endpoints (Qiniu publishes two, SiliconFlow two) were
        # invisible, so an operator whose relay moved had to go find the new
        # host by hand. One hint row, hidden until a preset is picked.
        self.lbl_preset_hint = CaptionLabel("")
        self.lbl_preset_hint.setWordWrap(True)
        self.lbl_preset_hint.setVisible(False)
        fl.addWidget(self.lbl_preset_hint, r, 0, 1, 2); r += 1
        self.lnk_preset = PushButton(FIF.LINK, "")
        self.lnk_preset.setVisible(False)
        fl.addWidget(self.lnk_preset, r, 0, 1, 2); r += 1
        self.lnk_preset_key = PushButton(FIF.LINK, "")
        self.lnk_preset_key.setVisible(False)
        fl.addWidget(self.lnk_preset_key, r, 0, 1, 2); r += 1

        fl.setColumnStretch(1, 1)
        self._form.setVisible(False)
        self._root.addWidget(self._form)

        # ---- footer ----
        footer = QHBoxLayout()
        # REVIEW-2026-10-20 (cc-switch's full-screen provider detail): the
        # detail form is taller than the 580 px dialog it lives in, so the
        # fields at the bottom (the save button, the preset links) can end
        # up below the fold. The button toggles the dialog between its
        # natural size and the screen's, and remembers which it is.
        self._btn_fullscreen = ToolButton(FIF.FULL_SCREEN)
        self._btn_fullscreen.setToolTip(self._t("wizard.fullscreen"))
        self._btn_fullscreen.setFixedSize(28, 28)
        self._btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        footer.addWidget(self._btn_fullscreen)
        footer.addStretch(1)
        self._btn_cancel = PushButton(self._t("wizard.cancel"))
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_ok = PrimaryPushButton(
            self._t("wizard.save") if existing else self._t("wizard.add"))
        self._btn_ok.clicked.connect(self._on_ok)
        footer.addWidget(self._btn_cancel)
        footer.addWidget(self._btn_ok)
        self._root.addLayout(footer)

        self._preset: object | None = None
        # REVIEW-2026-10-20: the provider's own alternates (the speed test
        # writes them) and the wizard's full-screen state. Both are
        # initialised here rather than lazily in their getters, so a
        # half-built wizard cannot answer "does this provider have
        # alternates?" with an AttributeError.
        self._custom_endpoints: list[str] = []
        self._fullscreen = False
        # Whether the advanced section is open. A flag and not
        # ``isVisible()`` -- see the property for why.
        self._advanced_open = False
        # REVIEW-2026-10-20: the digest of the providers file as it was
        # when this dialog opened. The page compares it with the file on
        # disk before writing, so a change made elsewhere while the modal
        # was open is a question rather than a silent overwrite.
        self.store_signature = _store_signature()
        self._build_category_chips()
        self._render_presets()

        if existing:
            self._prefill(existing)

    def _build_category_chips(self):
        for btn, _ in self._cat_buttons:
            btn.setParent(None)
        self._cat_buttons.clear()
        cats: list[str] = []
        for p in PROVIDER_PRESETS:
            if p.category not in cats:
                cats.append(p.category)
        for cat in cats:
            display = cat.replace("_", " ").capitalize()
            btn = PushButton(display)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                "PushButton{background:transparent;color:#64748b;border:1px solid #e2eef0;padding:0 12px;border-radius:14px}"
                "PushButton:hover{color:#0f172a}"
                "PushButton:checked{background:#2563eb;color:#fff;border-color:#2563eb}"
            )
            btn.clicked.connect(lambda _=False, c=cat: self._filter_cat(c))
            self._cat_row.addWidget(btn)
            self._cat_buttons.append((btn, cat))

    def _filter_cat(self, cat):
        for btn, c in self._cat_buttons:
            btn.setChecked(c == cat)
        self._render_presets()

    def _render_presets(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        q = self._search.text().strip().lower()
        active_cat = next((c for _, c in self._cat_buttons if _.isChecked()), "")
        rows: dict[str, list] = {}
        for preset in PROVIDER_PRESETS:
            if q and q not in preset.name.lower() and q not in preset.category.lower():
                continue
            if active_cat and preset.category != active_cat:
                continue
            rows.setdefault(preset.category, []).append(preset)
        r = c = 0
        cols = 3
        for cat, presets in rows.items():
            for preset in presets:
                btn = PushButton(preset.name)
                btn.setFixedHeight(34)
                btn.setStyleSheet(
                    "PushButton{background:#f8fafc;color:#334155;border:1px solid #e2e8f0;text-align:left;padding-left:12px}"
                    "PushButton:hover{background:#eff6ff;border-color:#2563eb;color:#2563eb}"
                )
                btn.clicked.connect(lambda _=False, p=preset: self._pick_preset(p))
                self._grid.addWidget(btn, r, c)
                c += 1
                if c >= cols:
                    c = 0
                    r += 1
        custom = PushButton(self._t("wizard.customProvider"))
        custom.setFixedHeight(34)
        custom.setStyleSheet(
            "PushButton{background:#f1f5f9;border:1px dashed #94a3b8;color:#64748b;text-align:left;padding-left:12px}"
            "PushButton:hover{color:#0f172a;border-color:#2563eb}"
        )
        # API-REVIEW-2026-10-18: picking "Custom" after a preset must drop
        # that preset's doc/key/backup row — otherwise the custom form still
        # advertises the previously-picked provider's endpoints.
        custom.clicked.connect(lambda _=False: self._show_custom_form())
        self._grid.addWidget(custom, r, c)

    def _show_custom_form(self) -> None:
        if self._form_has_content() and not self._confirm_preset_switch():
            return
        self._preset = None
        self._apply_preset_meta(_NO_PRESET)
        self._show_form()

    # REVIEW-2026-10-20: "does the form already hold the user's work?"
    #
    # Only the fields a preset OVERWRITES count. The note, the icon and the
    # profile survive a preset switch (nothing writes them), and the API key
    # does not: a preset never carries a key, so a key the user typed is
    # still there afterwards -- and warning about it would train the user to
    # dismiss the warning.
    def _form_has_content(self) -> bool:
        return bool(
            self._ipt_endpoint.text().strip()
            or self._ipt_model.text().strip()
            or self._ipt_key.text().strip()
        )

    def _confirm_preset_switch(self) -> bool:
        """Ask before a preset replaces what the user typed.

        Returns True when the switch may go ahead. The question is a
        MessageBox rather than an InfoBar because it BLOCKS the action it
        describes: an InfoBar that merely announces what already happened
        is not a warning, it is a post-mortem.
        """
        box = MessageBox(
            self._t("wizard.presetSwitchWarning"),
            self._t("wizard.presetSwitchWarning"),
            self,
        )
        return bool(box.exec())

    def _toggle_advanced(self, *_args) -> None:
        """Show or hide the fields that most accounts never touch."""
        self._advanced_open = not self._advanced_open
        self._adv_panel.setVisible(self._advanced_open)
        self._btn_adv.setText(("▾ " if self._advanced_open else "▸ ")
                              + self._t("wizard.advanced"))
        self._btn_adv.setChecked(self._advanced_open)

    @property
    def advanced_open(self) -> bool:
        """Whether the advanced section is open.

        A flag and not ``isVisible()``: a widget that is not on screen
        reports ``isVisible() == False`` whatever its own state is, so a
        test (and any logic) that asks "is the advanced section open?"
        would get the wrong answer for a dialog that has not been shown.
        """
        return self._advanced_open

    def _toggle_fullscreen(self, *_args) -> None:
        """Grow the dialog to the screen, or put it back.

        The detail form is taller than the dialog, so the bottom of it --
        where the save button lives -- can be below the fold on a short
        screen. This is the same escape hatch cc-switch gives its provider
        detail view.
        """
        if self._fullscreen:
            self.showNormal()
            self._fullscreen = False
            self._btn_fullscreen.setToolTip(self._t("wizard.fullscreen"))
        else:
            self.showFullScreen()
            self._fullscreen = True
            self._btn_fullscreen.setToolTip(self._t("wizard.restore"))

    def _open_advanced_if_needed(self) -> None:
        """Open the advanced section when it already holds something.

        A provider that HAS a note must show the note, or the field reads
        as empty and the user retypes what was already saved.
        """
        if (self._ipt_notes.text().strip()
                or self._ipt_icon.text().strip()
                or self._ipt_profile.text().strip()
                or self._ipt_extra_headers.text().strip()):
            self._advanced_open = True
            self._adv_panel.setVisible(True)
            self._btn_adv.setChecked(True)
            self._btn_adv.setText("▾ " + self._t("wizard.advanced"))

    def _pick_preset(self, preset):
        # REVIEW-2026-10-20 (cc-switch warns before it overwrites a filled
        # form): a preset is a TEMPLATE, and picking one replaces whatever
        # the user had typed -- endpoint, model and API key. cc-switch puts
        # the same warning on the same gesture, because the two directions
        # look identical in the UI and only one of them destroys work.
        if self._form_has_content() and not self._confirm_preset_switch():
            return
        self._ipt_name.setText(preset.name)
        self._cmb_fmt.setCurrentText(preset.api_format.value)
        self._ipt_endpoint.setText(preset.endpoint)
        self._ipt_model.setText(preset.model)
        # API-REVIEW-2026-10-18 (cc-switch comparison): a preset is a
        # COMPLETE provider template — its extra_headers are part of the
        # wire contract (e.g. a gateway demanding X-Use-Url-Key). Dropping
        # them here meant the saved provider silently diverged from the
        # preset the user clicked. Current presets ship none, so this is
        # forward-proofing; the field stays user-editable afterwards.
        import json
        self._ipt_extra_headers.setText(
            json.dumps(preset.extra_headers) if preset.extra_headers else "")
        self._preset = preset
        self._apply_preset_meta(preset)
        self._show_form()

    def _show_form(self):
        self._search.setVisible(False)
        for btn, _ in self._cat_buttons:
            btn.setVisible(False)
        self._scroll.setVisible(False)
        self._form.setVisible(True)
        # Only clobber the title when creating a new provider (not editing an existing one,
        # where __init__ already set the provider-specific "Configure [Name]" title).
        if not self._existing:
            self.setWindowTitle(self._t("wizard.configure"))

    # API-REVIEW-2026-10-18: publish the preset's doc / key / backup-endpoint
    # metadata. Split out of _pick_preset so an EDIT of an existing provider
    # (which has no preset) hides the row rather than showing a stale one.
    def _apply_preset_meta(self, preset) -> None:
        hint = getattr(self, "lbl_preset_hint", None)
        lnk = getattr(self, "lnk_preset", None)
        lnk_key = getattr(self, "lnk_preset_key", None)
        if hint is None:
            return
        candidates = list(getattr(preset, "endpoint_candidates", None) or [])
        # The preset's own endpoint is candidates[0]'s sibling — list the
        # ALTERNATES only, so the hint reads "also try …" and not the value
        # already sitting in the field.
        alternates = [c for c in candidates if c != preset.endpoint]
        parts = []
        if alternates:
            parts.append(self._t("wizard.presetAltEndpoints").format(
                urls=", ".join(alternates)))
        if getattr(preset, "api_key_hint", "") and \
                preset.api_key_hint != "API Key":
            parts.append(self._t("wizard.presetKeyHint").format(
                hint=preset.api_key_hint))
        hint.setText(" · ".join(parts))
        hint.setVisible(bool(parts))

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        def _wire(button, url):
            if not url:
                button.setVisible(False)
                return
            button.setVisible(True)
            button.clicked.connect(
                lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u)))

        _wire(lnk, getattr(preset, "doc_url", ""))
        _wire(lnk_key, getattr(preset, "api_key_url", ""))

    def _prefill(self, existing: LlmProvider):
        self._ipt_name.setText(existing.name)
        self._cmb_fmt.setCurrentText(
            existing.api_format.value if hasattr(existing.api_format, 'value')
            else str(existing.api_format)
        )
        self._ipt_endpoint.setText(existing.endpoint)
        self._ipt_model.setText(existing.model)
        self._ipt_key.setText(existing.api_key)
        # REVIEW-2026-10-20: the cc-switch fields. Each one is a FIELD of
        # the record, so the wizard's rebuild has to carry it over — the
        # same argument the reasoning effort makes: a field the rebuild
        # forgets is a field the user loses the moment they edit the
        # provider, with nothing on screen to explain why.
        self._ipt_notes.setText(getattr(existing, "notes", "") or "")
        self._ipt_icon.setText(getattr(existing, "icon", "") or "")
        self._ipt_profile.setText(getattr(existing, "profile", "") or "")
        self._chk_full_url.setChecked(bool(getattr(existing, "full_url", False)))
        self._custom_endpoints = list(getattr(existing, "custom_endpoints", None) or [])
        # Restore extra_headers as JSON string
        if existing.extra_headers:
            import json
            try:
                self._ipt_extra_headers.setText(json.dumps(existing.extra_headers))
            except Exception:
                pass
        # REVIEW-2026-10-19: carry the stored reasoning effort into the combo.
        # Without this, editing a provider would silently reset the effort to
        # "default" -- i.e. back to effort=max for M3.1, i.e. back to the
        # configuration that times out on a dense figure.
        if getattr(existing, "reasoning_effort", ""):
            try:
                self._cmb_effort.setCurrentIndex(
                    self._effort_values.index(existing.reasoning_effort))
            except ValueError:
                pass
        # API-REVIEW-2026-10-18: editing an existing provider has no preset,
        # so hide the doc/key/backup row — otherwise the wizard that opened
        # on preset A and then picked "Edit" on provider B would still
        # advertise A's docs and backup endpoints.
        self._preset = None
        self._apply_preset_meta(_NO_PRESET)
        self._show_form()
        # A provider that already has a note / icon / profile must SHOW
        # them, or the advanced section reads as empty and the user
        # retypes what was already saved.
        self._open_advanced_if_needed()

    def _parse_extra_headers(self) -> tuple[dict, str | None]:
        """Parse extra_headers from the text field.

        Returns ``(headers, error_message)``. ``error_message`` is None
        when the field is empty or parsed successfully. When the field
        is non-empty but malformed, returns ``({}, error_message)`` so
        the caller can surface a warning (the previous version silently
        dropped the user's input).
        """
        text = self._ipt_extra_headers.text().strip()
        if not text:
            return {}, None
        import json
        try:
            parsed = json.loads(text)
        except Exception as exc:
            return {}, f"{type(exc).__name__}: {exc}"
        if isinstance(parsed, dict):
            return parsed, None
        return {}, f"extra_headers must be a JSON object, got {type(parsed).__name__}"

    def _refresh_effort_options(self, *_args):
        """Repopulate the effort combo from the MODEL's own list of levels.

        The levels belong to the model: MiniMax's M3.1 takes five named
        levels, M3 takes an on/off switch instead, and any other model takes
        none. Offering a level the model does not understand would be a
        control that does nothing, and offering none where there are some
        would hide the one knob that decides whether the model answers at
        all.

        The SELECTION is kept if the new list still contains it, and the
        model's own default is selected otherwise -- which is minimax-code's
        resolution rule, transcribed: a valid stored choice wins, the
        catalogue's default wins over the midpoint, and without a default an
        even-length list uses the higher midpoint.
        """
        from rca_core.llm import (REASONING_DEFAULT, reasoning_options_for,
                                  resolve_reasoning_choice)
        model = self._ipt_model.text().strip()
        options = reasoning_options_for(model)
        if not options:
            # A model with no reasoning control: keep the row but say so, and
            # do not let a stale selection linger.
            self._effort_values = [""]
            self._cmb_effort.clear()
            self._cmb_effort.addItems([self._t("wizard.effortNotSupported")])
            self._cmb_effort.setEnabled(False)
            return
        self._cmb_effort.setEnabled(True)
        self._effort_values = list(options)
        labels = []
        for opt in options:
            if opt == REASONING_DEFAULT:
                labels.append(self._t("wizard.effortDefault"))
            else:
                labels.append(opt)
        current = self._effort_values[self._cmb_effort.currentIndex()] \
            if self._cmb_effort.currentIndex() >= 0 else ""
        self._cmb_effort.clear()
        self._cmb_effort.addItems(labels)
        wanted = resolve_reasoning_choice(model, current)
        try:
            self._cmb_effort.setCurrentIndex(self._effort_values.index(wanted))
        except ValueError:
            self._cmb_effort.setCurrentIndex(0)

    def _on_model_picked(self, name):
        """A model was picked from the fetched list -- put it in the field.

        The guard flag matters here: setting the field fires textChanged,
        which fires the FILTER, which would narrow the list down to the
        one entry just picked. The user would then have to clear the field
        to pick a second model -- a one-shot picker where the user
        expects to compare.
        """
        if self._model_filtering or not name:
            return
        self._model_filtering = True
        try:
            self._ipt_model.setText(name)
        finally:
            self._model_filtering = False

    def _refresh_model_meta(self, *_args):
        """Say what the model can do, instead of letting the user find out.

        The max-output number is measured, not copied: MiniMax-M3.1-Flash
        answers a larger ``max_tokens`` with "does not support max tokens >
        524288 (2013)", so typing 1000000 is a 400 and the limit belongs on
        screen.
        """
        try:
            from rca_core.llm import model_metadata_for
            meta = model_metadata_for(self._ipt_model.text().strip())
        except Exception:
            meta = None
        if meta is None:
            self._lbl_model_meta.setText("")
            self._lbl_model_meta.setVisible(False)
            return
        bits = []
        if meta.max_output:
            bits.append(self._t("wizard.metaMaxOutput").format(
                n=format(meta.max_output, ",")))
        if meta.context_window:
            bits.append(self._t("wizard.metaContext").format(
                n=format(meta.context_window, ",")))
        if meta.note:
            # The catalogue carries an i18n KEY, not a sentence -- a
            # Chinese note in an English interface is a defect. The
            # fallback keeps a catalogue entry visible even when its key
            # is missing from the table.
            note = self._t(meta.note)
            if note == meta.note:
                note = meta.note
            bits.append(note)
        self._lbl_model_meta.setText(" · ".join(bits))
        self._lbl_model_meta.setVisible(True)

    def _filter_model_options(self, *_args):
        """Narrow the fetched-model picker to what the typed text matches.

        qfluentwidgets' ComboBox has no search of its own, so the filtering
        is done here: typing in the model field narrows the list, which is
        the interaction cc-switch's ModelInputWithFetch gives.
        """
        if self._model_filtering or not self._model_options:
            return
        needle = self._ipt_model.text().strip().lower()
        matches = [m for m in self._model_options
                   if not needle or needle in m.lower()]
        current = self._ipt_model.text().strip()
        self._model_filtering = True
        try:
            self._cmb_models.blockSignals(True)
            self._cmb_models.clear()
            self._cmb_models.addItems(matches)
            # Keep the current model highlighted when it survived the
            # filter, so the combo does not jump to the first match while
            # the user is typing.
            self._cmb_models.setCurrentIndex(
                matches.index(current) if current in matches else -1)
            self._cmb_models.setVisible(bool(matches))
        finally:
            self._cmb_models.blockSignals(False)
            self._model_filtering = False

    def _format_extra_headers(self, *_args):
        """Re-indent the extra-headers JSON and say WHERE it is wrong.

        The field has always been raw JSON, and a typo in it fails at the
        request with nothing pointing at the field.
        """
        text = self._ipt_extra_headers.text().strip()
        if not text:
            return
        import json
        try:
            pretty = json.dumps(json.loads(text), indent=2,
                                ensure_ascii=False, sort_keys=True)
        except ValueError as exc:
            InfoBar.warning("", self._t("wizard.jsonInvalid").format(
                reason=str(exc)), parent=self, position=InfoBarPosition.TOP)
            return
        self._ipt_extra_headers.setText(pretty)

    def _open_speed_test(self, *_args):
        """Open the endpoint speed test.

        cc-switch's EndpointSpeedTest measures every candidate endpoint,
        colours the latency (<300 ms green, 300-500 yellow, 500-800 orange,
        >=800 red), can auto-select the fastest, and keeps the alternates on
        the provider. Ours measures the same candidates -- the endpoint plus
        the ones the user has kept -- and writes the result back the same
        way.
        """
        dlg = _EndpointSpeedDialog(
            self,
            endpoint=self._ipt_endpoint.text().strip(),
            custom=list(self._speed_custom_endpoints()),
            api_key=self._ipt_key.text().strip(),
            api_format=self._cmb_fmt.currentText(),
        )
        dlg.exec()
        if dlg.result() == QDialog.Accepted:
            self._ipt_endpoint.setText(dlg.selected_url() or
                                      self._ipt_endpoint.text())
            self._speed_custom_endpoints(dlg.custom_urls())

    def _speed_custom_endpoints(self, value=None):
        """Read or write the provider's custom endpoint list."""
        if value is not None:
            self._custom_endpoints = [u for u in value if u]
        return getattr(self, "_custom_endpoints", [])

    def _fetch_models(self, *_args):
        """Ask the endpoint which models it serves, and offer them in a menu.

        The network call runs on a worker thread -- a GET against a distant
        relay can take seconds, and the wizard is a modal dialog, so blocking
        the GUI thread here would freeze the app mid-edit.
        """
        endpoint = self._ipt_endpoint.text().strip()
        api_key = self._ipt_key.text().strip()
        if not endpoint or not api_key:
            InfoBar.warning("", self._t("wizard.fetchModelsNeedKey"),
                            parent=self, position=InfoBarPosition.TOP)
            return
        try:
            fmt = ApiFormat(self._cmb_fmt.currentText())
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        from rca_core.llm import LlmProvider
        probe = LlmProvider(name="probe", api_format=fmt, endpoint=endpoint,
                            api_key=api_key, model=self._ipt_model.text().strip())
        from PySide6.QtCore import QThread

        class _Worker(QThread):
            # (worker, result) -- a BOUND METHOD is connected, so Qt queues
            # the call onto the GUI thread. A lambda would be a DIRECT
            # connection and the menu would be built off-thread.
            done = Signal(object, object)

            def __init__(self, p, parent=None):
                super().__init__(parent)
                self._p = p

            def run(self):
                from rca_core.llm import list_models
                try:
                    self.done.emit(self, list_models(self._p, timeout_sec=15))
                except Exception as exc:
                    self.done.emit(self, [])
                    log.warning("fetch models: %s", exc)

        self._btn_models.setEnabled(False)
        worker = _Worker(probe, self)
        worker.done.connect(self._on_models_fetched)
        # Keep a reference: a QThread that is garbage-collected while running
        # is destroyed mid-flight.
        self._models_worker = worker
        worker.start()

    def _on_models_fetched(self, worker, models):
        """Show the discovered models; picking one fills the model field."""
        try:
            self._btn_models.setEnabled(True)
        except Exception:
            pass
        self._models_worker = None
        models = [m for m in (models or []) if m]
        if not models:
            InfoBar.warning("", self._t("wizard.fetchModelsEmpty"),
                            parent=self, position=InfoBarPosition.TOP)
            return
        self._model_options = list(models)
        # The list is shown IN FULL when it arrives. The field's own text
        # filters it as the user types, but a freshly fetched list narrowed
        # by the model that is already in the field would show ONE entry
        # for a provider that serves twenty -- which is the opposite of
        # what "fetch the model list" is for.
        self._model_filtering = True
        try:
            self._cmb_models.blockSignals(True)
            self._cmb_models.clear()
            self._cmb_models.addItems(self._model_options)
            current = self._ipt_model.text().strip()
            idx = self._model_options.index(current) if current in self._model_options else -1
            self._cmb_models.setCurrentIndex(idx)
        finally:
            self._cmb_models.blockSignals(False)
            self._model_filtering = False
        self._cmb_models.setVisible(True)

    def _on_ok(self):
        name = self._ipt_name.text().strip()
        endpoint = self._ipt_endpoint.text().strip().rstrip("/")
        if not name or not endpoint:
            missing = []
            if not name:
                missing.append(self._t("wizard.fieldName"))
            if not endpoint:
                missing.append(self._t("wizard.fieldEndpoint"))
            # F-01 (REVIEW-2026-10-17): this used to read `self._tr.t(...)`,
            # but ProviderWizard.__init__ only ever assigns `self._t` — there
            # is no `_tr` on this class, so leaving the name or endpoint blank
            # raised AttributeError inside the clicked-slot and the wizard
            # became unsavable. Translator.t() takes the key alone, so the
            # `{reason}` variant below is interpolated with .format().
            msg = self._t("wizard.fillRequired") + ", ".join(missing)
            InfoBar.warning("", msg, parent=self, position=InfoBarPosition.TOP)
            return
        # API-REVIEW-2026-10-18: "https://" (the custom-provider placeholder)
        # strips to "https:" — non-empty, so the blank check passed and a
        # hostless endpoint was saved, then failed at every use with
        # err.badEndpoint ("missing host"). Require a host at save time; the
        # SSRF guard stays as the second line of defence.
        from urllib.parse import urlparse as _urlparse
        if not _urlparse(endpoint).netloc:
            InfoBar.warning(
                "", self._t("wizard.endpointNoHost"),
                parent=self, position=InfoBarPosition.TOP,
            )
            return
        try:
            fmt = ApiFormat(self._cmb_fmt.currentText())
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        extra_headers, headers_err = self._parse_extra_headers()
        if headers_err is not None:
            # Don't block save — user may have a legitimate reason to
            # save without headers — but warn so the mistake isn't
            # silent. The previous version just discarded the input.
            InfoBar.warning(
                "",
                self._t("wizard.extraHeadersIgnored").format(reason=headers_err),
                parent=self,
                position=InfoBarPosition.TOP,
            )
        self.created_provider = LlmProvider(
            id=self._edit_id,                       # preserve on edit; ignored when "" (add)
            name=name,
            api_format=fmt,
            endpoint=endpoint,
            api_key=self._ipt_key.text().strip(),
            model=self._ipt_model.text().strip(),
            extra_headers=extra_headers,
            # REVIEW-2026-10-19: the effort is a real field of the record,
            # not a free-form blob, so it survives an edit of this provider.
            reasoning_effort=self._effort_values[
                max(0, self._cmb_effort.currentIndex())],
            # REVIEW-2026-10-20: the cc-switch fields, carried over for the
            # same reason the effort is: the wizard REBUILDS the record from
            # a hand-listed set of fields, so a field that is not listed
            # here is a field the user loses the moment they edit this
            # provider.
            notes=self._ipt_notes.text().strip(),
            icon=self._ipt_icon.text().strip()[:8],
            profile=self._ipt_profile.text().strip(),
            full_url=self._chk_full_url.isChecked(),
            custom_endpoints=list(self._speed_custom_endpoints()),
            created_at=self._edit_created_at,       # same reasoning
            # Preserve is_current on edit. The wizard rebuilds the provider
            # from scratch and the LlmProvider default is False, which would
            # silently deactivate the user's currently-active provider and
            # reroute LLM calls/credentials to a different endpoint.
            is_current=(bool(self._existing.is_current) if self._existing else False),
            # REVIEW-2026-09-10: carry over sort_index and the health
            # counter as well. Both defaulted to 0 on the rebuilt object,
            # so store.update()'s sort by (sort_index, created_at) made an
            # edited provider jump back to the top of the drag order, and
            # consecutive_failures was reset — a provider showing a failure
            # state flipped back to healthy without ever succeeding.
            sort_index=(self._existing.sort_index if self._existing else 0),
            consecutive_failures=(self._existing.consecutive_failures if self._existing else 0),
        )
        self.accept()


# ---------------------------------------------------------------------------
# ProvidersPage — integrates the components above
# ---------------------------------------------------------------------------
class ProvidersPage(ScrollArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._t = win._t
        # All in-flight test workers, mapped to the card that started them.
        # We deliberately allow several to run concurrently (one per card):
        # each worker's done-signal carries its own card, so a slow test on
        # provider A never blocks or orphans the result of a quick test on
        # provider B. The previous design held a single `self._test_worker`
        # and forcibly disconnected any predecessor's done-signal — which
        # silently dropped that worker's result and froze its card in the
        # "testing" ⏳ state forever (H1: consecutive-test freeze).
        self._test_workers: dict = {}
        # Provider ids that are currently mid-test, so a `_refresh()` that
        # rebuilds the cards can re-apply the ⏳ state to the new card widget
        # (it otherwise only preserved health). This also prevents the
        # indeterminate state where a refresh replaces the visible card while
        # the worker still completes against the old one (H2). Since B-11 the
        # replaced card is parked or deleted rather than orphaned — the id set
        # is what lets the NEW card pick the ⏳ back up.
        self._testing_ids: set[str] = set()
        # REVIEW-2026-10-20: the in-flight quota queries, mapped to the
        # card each one belongs to. Same shape as _test_workers and for the
        # same reason: a QThread that is garbage collected while it is
        # running is a crash, not a failed query.
        self._quota_workers: dict = {}
        self._search_term = ""
        self._all_cards: list[ProviderCard] = []   # unfiltered list
        self.setObjectName("providersPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        self.setFocusPolicy(Qt.StrongFocus)

        root = QWidget()
        self.setWidget(root)
        self.lay = QVBoxLayout(root)
        self.lay.setContentsMargins(28, 20, 28, 28)
        self.lay.setSpacing(12)

        # Header with title + add button
        header = QHBoxLayout()
        self.lbl_title = TitleLabel(self._t("settings.llmProvider"))
        header.addWidget(self.lbl_title)
        header.addStretch(1)
        self.btn_add = PrimaryPushButton(FIF.ADD, self._t("settings.addProvider"))
        self.btn_add.clicked.connect(self._add_provider)
        header.addWidget(self.btn_add)
        self.lay.addLayout(header)

        # REVIEW-2026-10-20 (cc-switch's category counts + profile switch):
        # a summary line that says what is in the list, and a filter that
        # narrows it to one group.
        #
        # The counts are not decoration: a list of providers with no
        # indication of which one is active, which one is broken, and how
        # many there are, is a list the user has to read card by card. The
        # profile filter is the same idea as cc-switch's group tabs -- with
        # five relay accounts in two groups ("work", "personal"), showing
        # one group at a time is the difference between a usable list and a
        # scroll.
        summary_row = QHBoxLayout()
        self.lbl_summary = CaptionLabel("")
        summary_row.addWidget(self.lbl_summary)
        summary_row.addStretch(1)
        self._cmb_profile = ComboBox()
        self._cmb_profile.setFixedWidth(160)
        self._cmb_profile.setVisible(False)
        self._cmb_profile.currentTextChanged.connect(self._on_profile_changed)
        summary_row.addWidget(self._cmb_profile)
        self.lay.addLayout(summary_row)
        # The profile currently shown: None = every profile, "" = only the
        # ungrouped ones, a name = only that profile.
        self._profile_filter = None

        # Search bar (shown above the list)
        self._search_widget = QWidget()
        search_lay = QHBoxLayout(self._search_widget)
        search_lay.setContentsMargins(0, 0, 0, 0)
        self._search_input = LineEdit()
        self._search_input.setPlaceholderText(
            self._t("provider.searchPlaceholder") if self._t("provider.searchPlaceholder") != "provider.searchPlaceholder"
            else "Search providers... (Ctrl+F)"
        )
        self._search_input.textChanged.connect(self._on_search_changed)
        self._search_input.setFixedHeight(32)
        search_lay.addWidget(self._search_input)
        self._search_clear = ToolButton(FIF.CLOSE)
        self._search_clear.setFixedSize(28, 28)
        self._search_clear.setVisible(False)
        self._search_clear.clicked.connect(self._clear_search)
        search_lay.addWidget(self._search_clear)
        self._search_widget.setVisible(False)
        self.lay.addWidget(self._search_widget)

        self.list_widget = ProviderDragList()
        self.list_widget.orderChanged.connect(self._on_order_changed)
        # AUDIT-2026-09-27 [item B-11] (B-11): tell the list which of its
        # cards an in-flight connection test is still pointing at, so
        # set_cards() can delete the ones it replaces instead of orphaning
        # every card of every refresh — while a card whose test is still
        # running is parked (not destroyed) until that test finishes. A bound
        # method, not a lambda: the list holds it for the page's lifetime
        # either way, but this keeps the intent readable at the call site.
        self.list_widget.set_retain_hook(self._card_in_flight)
        self.lay.addWidget(self.list_widget)

        self.lbl_test = CaptionLabel("")
        self.lay.addWidget(self.lbl_test)
        self.lay.addStretch(1)

        self._refresh()

    def keyPressEvent(self, event):
        """Support Ctrl+F to open search."""
        if (event.modifiers() & Qt.ControlModifier) and event.key() == Qt.Key_F:
            self._show_search()
            event.accept()
        elif event.key() == Qt.Key_Escape:
            self._hide_search()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _show_search(self):
        self._search_widget.setVisible(True)
        self._search_input.setFocus()
        self._search_input.selectAll()

    def _hide_search(self):
        self._search_widget.setVisible(False)
        self._search_input.clear()
        self._search_term = ""
        self._apply_filter()

    def _clear_search(self):
        self._search_input.clear()

    def _on_search_changed(self, text: str):
        self._search_term = text.strip().lower()
        self._search_clear.setVisible(bool(text))
        self._apply_filter()

    def _apply_filter(self):
        """Filter cards by search term and by profile.

        cc-switch filters the list by name AND lets the user narrow it to
        one group; the note is searchable for the same reason it is
        displayed -- a provider the user can only find by its name is a
        provider they will not find.
        """
        q = self._search_term
        # Three states, not two: None means "every profile", "" means
        # "only the providers with no profile", and a name means "only
        # that profile". "All" and "Ungrouped" are DIFFERENT filters that
        # happen to share the empty profile string, so the distinction
        # cannot live in the filter value.
        profile = getattr(self, "_profile_filter", None)
        for card in self._all_cards:
            prov = card.provider
            if profile is not None:
                name = (getattr(prov, "profile", "") or "").strip()
                if name != profile:
                    card.setVisible(False)
                    continue
            if not q:
                card.setVisible(True)
                continue
            hay = " ".join([
                (prov.name or "") if prov else "",
                (prov.endpoint or "") if prov else "",
                (getattr(prov, "notes", "") or "") if prov else "",
                (getattr(prov, "model", "") or "") if prov else "",
            ]).lower()
            card.setVisible(q in hay)

    # ---- the summary line and the profile filter (cc-switch) -------------

    def _update_summary(self) -> None:
        """Say what the list holds, and refresh the profile filter.

        cc-switch shows the per-category counts above the provider list.
        The same information here: how many providers there are, which one
        is active, how many are healthy, and how many are failing -- the
        four questions a user answers by reading every card otherwise.
        """
        providers = [c.provider for c in self._all_cards if c.provider]
        total = len(providers)
        current = sum(1 for p in providers if p.is_current)
        healthy = sum(1 for p in providers
                      if (getattr(p, "consecutive_failures", 0) or 0) <= 0)
        failing = total - healthy
        self.lbl_summary.setText("  ·  ".join([
            self._t("provider.summary").format(n=total),
            self._t("provider.summaryCurrent").format(n=current),
            self._t("provider.summaryHealthy").format(n=healthy),
            self._t("provider.summaryFailed").format(n=failing),
        ]))
        self._refresh_profile_options(providers)

    def _refresh_profile_options(self, providers) -> None:
        """Rebuild the profile combo from the profiles actually in use.

        The options are DERIVED from the data rather than written down, so
        a provider the user just filed under a new group shows up without
        anything here having to know the name in advance.
        """
        names = []
        for p in providers:
            name = (getattr(p, "profile", "") or "").strip()
            if name and name not in names:
                names.append(name)
        names.sort()
        # The combo's items are (label, profile) pairs, and the profile is
        # kept in a parallel list -- an index into a translated label list
        # breaks the moment the wording changes. None = every profile,
        # "" = only the ungrouped ones.
        labels = [self._t("provider.profileAll")]
        values = [None]
        for name in names:
            labels.append(name)
            values.append(name)
        if any(not (getattr(p, "profile", "") or "").strip()
               for p in providers):
            labels.append(self._t("provider.profileUngrouped"))
            values.append("")
        if labels == getattr(self, "_profile_labels", None):
            return
        self._profile_labels = labels
        self._profile_values = values
        current = getattr(self, "_profile_filter", None)
        self._cmb_profile.blockSignals(True)
        try:
            self._cmb_profile.clear()
            self._cmb_profile.addItems(labels)
            if current in values:
                self._cmb_profile.setCurrentIndex(values.index(current))
            else:
                self._profile_filter = None
                self._cmb_profile.setCurrentIndex(0)
        finally:
            self._cmb_profile.blockSignals(False)
        self._cmb_profile.setVisible(len(labels) > 1)

    def _on_profile_changed(self, *_args) -> None:
        """Show one profile's providers, or all of them."""
        values = getattr(self, "_profile_values", None) or [None]
        idx = self._cmb_profile.currentIndex()
        self._profile_filter = values[idx] if 0 <= idx < len(values) else None
        self._apply_filter()

    def _build_cards(self) -> list[ProviderCard]:
        try:
            store = ProviderStore().load()
        except Exception:
            store = None
        if not store:
            return []
        cards = []
        for prov in store.providers:
            card = ProviderCard(prov, prov.is_current, self._t)
            card.btn_test.clicked.connect(lambda _=False, p=prov, c=card: self._test(p, c))
            card.btn_active.clicked.connect(lambda _=False, pid=prov.id: self._set_current(pid))
            card.btn_edit.clicked.connect(lambda _=False, pid=prov.id: self._edit(pid))
            card.btn_delete.clicked.connect(lambda _=False, pid=prov.id: self._delete(pid))
            cards.append(card)
        return cards

    def _live_card_for(self, provider_id: str):
        """Return the currently-shown provider card for *provider_id*, or
        None if the provider has been deleted (no live card to update)."""
        for c in self.list_widget._cards:
            if c.provider and c.provider.id == provider_id:
                return c
        return None

    def _refresh(self):
        if not hasattr(self, "list_widget"):
            return
        # Store health + testing state for cards that will be rebuilt, so a
        # language switch / add / delete doesn't silently drop in-flight
        # context. (Before the fix, an in-flight test's card could be
        # orphaned here while its worker was still running → H2 freeze.)
        old_health: dict[str, int] = {}
        for card in self._all_cards:
            if card.provider:
                old_health[card.provider.id] = card._health_failures

        # Snapshot which providers are mid-test BEFORE rebuilding, so we can
        # restore ⏳ on the freshly-built cards.
        testing_snapshot = set(self._testing_ids)

        cards = self._build_cards()
        # Restore health state + testing state
        for card in cards:
            if card.provider and card.provider.id in old_health:
                card.set_health(old_health[card.provider.id])
            if card.provider and card.provider.id in testing_snapshot:
                card.set_testing(True)

        self._all_cards = cards
        if not cards:
            # AUDIT-2026-09-27 [item B-11] (B-11): this branch used to return
            # without touching the list, so the LAST set of cards stayed in
            # the layout — the 0-provider screen showed the deleted providers
            # next to "no providers", and those widgets were never released
            # either. Empty the list on the way to the empty state; the cards
            # go through the same release/park path as any other refresh.
            self.list_widget.set_cards([])
            self._update_summary()
            self._show_empty()
            return
        self._hide_empty()
        self.list_widget.set_cards(cards)
        self._update_summary()
        self._apply_filter()
        # REVIEW-2026-10-20: the plan's remaining quota, fetched per
        # provider in the background. The card is already on screen; this
        # fills in the one number that says whether the next extraction can
        # start at all.
        self._start_quota_queries(cards)
        # The extract page's environment strip says "no provider is
        # configured" — and this page is where that becomes true or false.
        try:
            self.win.extract_page.refresh_environment_banner()
        except Exception:
            log.debug("could not refresh the environment banner",
                      exc_info=True)

    def _show_empty(self):
        if hasattr(self, "_empty") and self._empty is not None:
            return
        self._empty = QWidget()
        lay = QVBoxLayout(self._empty)
        lay.addStretch(1)
        lay.addWidget(BodyLabel(self._t("settings.noProviders")), 0, Qt.AlignCenter)
        lay.addStretch(1)
        self.lay.insertWidget(1, self._empty)

    def _hide_empty(self):
        if hasattr(self, "_empty") and self._empty is not None:
            # B-11: setParent(None) alone left the widget (and its BodyLabel)
            # alive with no owner — one leak per 0→N→0 cycle. Nothing can be
            # mid-test on the empty-state widget, so it can just be deleted.
            self._empty.setParent(None)
            self._empty.deleteLater()
            self._empty = None

    # ---- CRUD — all use a single shared store instance ----

    def _load_store(self):
        """Load (or return cached) store instance."""
        try:
            return ProviderStore().load()
        except Exception:
            return None

    def _confirm_no_external_change(self, seen_signature: str) -> int:
        """Ask what to do when the providers file changed underneath us.

        Returns one of ``_ConflictDialog``'s three answers. When nothing
        changed, the answer is OVERWRITE -- i.e. "go ahead and write".
        """
        if not seen_signature:
            return _ConflictDialog.OVERWRITE
        current = _store_signature()
        if current == seen_signature:
            return _ConflictDialog.OVERWRITE
        dlg = _ConflictDialog(self.win, self._t)
        dlg.exec()
        return dlg.choice()

    def _on_order_changed(self, order):
        store = self._load_store()
        if not store:
            return
        id_to_p = {p.id: p for p in store.providers}
        store.providers = [id_to_p[i] for i in order if i in id_to_p]
        # Persist the new visual order to sort_index, not just the list
        # order. ProviderStore.load() re-sorts by sort_index on every read;
        # without this the next refresh() (and the next app launch) would
        # snap the cards back to the original positions.
        for idx, p in enumerate(store.providers):
            p.sort_index = idx
        store.save()
        self.win.invalidate_provider_cache()
        # Refresh to rebuild cards
        self._refresh()
        self._sync_settings_page()

    def _add_provider(self):
        # Guard against double-click / rapid clicks: if a wizard is
        # already open, raise it instead of stacking a new instance.
        # The previous version would happily open N modals on top of
        # each other, each one writing to the store on close.
        if getattr(self, "_provider_wizard", None) is not None:
            try:
                if self._provider_wizard.isVisible():
                    self._provider_wizard.raise_()
                    self._provider_wizard.activateWindow()
                    return
            except RuntimeError:
                # Underlying C++ object was destroyed — fall through and
                # open a fresh wizard.
                self._provider_wizard = None
        wiz = ProviderWizard(self.win, self._t)
        self._provider_wizard = wiz
        try:
            if wiz.exec() and wiz.created_provider:
                # REVIEW-2026-10-20: the same external-change guard as the
                # edit path. A new provider is an APPEND, so the other
                # version's providers are not at risk -- but the file's
                # current id and order are, and the user deserves to know
                # the file moved under them.
                choice = self._confirm_no_external_change(wiz.store_signature)
                if choice == _ConflictDialog.CANCEL:
                    return
                if choice == _ConflictDialog.KEEP_DISK:
                    self._refresh()
                    return
                store = self._load_store()
                if store:
                    store.add(wiz.created_provider)     # saves internally
                    store.set_current(wiz.created_provider.id)   # and again
                    # UX-2026-10-18: the explicit save() that used to sit
                    # here was a THIRD write of the same file — add() and
                    # set_current() each persist on their own. Measured with
                    # 60 providers (the state a long-time user reaches):
                    # save() costs 70-84 ms because it fsyncs and renames,
                    # so one wizard confirmation used to pay that ~200 ms
                    # on the UI thread to produce one identical file. The
                    # state on disk is already correct after the two calls
                    # above; nothing is lost by dropping the third write.
                    self.win.invalidate_provider_cache()
                    self._refresh()
                    self._sync_settings_page()
        finally:
            self._provider_wizard = None

    def _set_current(self, pid):
        store = self._load_store()
        if store:
            # UX-2026-10-18: set_current() persists on its own; the save()
            # that used to follow wrote the identical file a second time
            # (70-84 ms of fsync+rename at 60 providers, on the UI thread).
            store.set_current(pid)
            self.win.invalidate_provider_cache()
            self._refresh()
            self._sync_settings_page()

    def _edit(self, pid):
        """Open the wizard in edit mode for an existing provider."""
        store = self._load_store()
        if not store:
            return
        existing = next((p for p in store.providers if p.id == pid), None)
        if existing is None:
            return
        wiz = ProviderWizard(self.win, self._t, existing=existing)
        if wiz.exec() and wiz.created_provider:
            # REVIEW-2026-10-20 (cc-switch's external-change guard): the
            # file may have been changed by something else while the modal
            # was open. Ask BEFORE writing, because after the write the
            # other version is gone.
            choice = self._confirm_no_external_change(wiz.store_signature)
            if choice == _ConflictDialog.CANCEL:
                return
            if choice == _ConflictDialog.KEEP_DISK:
                self._refresh()
                return
            # OVERWRITE: reload the store first, so the OTHER providers'
            # changes survive and only this one is replaced.
            store = self._load_store()
            if not store:
                return
            ok = store.update(wiz.created_provider)
            if not ok:
                # Should not happen now that the wizard preserves the id,
                # but log + surface rather than silently dropping the edit.
                print(f"[rca] provider update failed for id={pid!r}")
                InfoBar.error(
                    "",
                    self._t("wizard.updateFailed"),
                    parent=self,
                    position=InfoBarPosition.TOP,
                )
                return
            # UX-2026-10-18: update() persists internally — the save() that
            # used to follow wrote the identical file again.
            self.win.invalidate_provider_cache()
            self._refresh()
            self._sync_settings_page()

    def _delete(self, pid):
        store = self._load_store()
        if not store:
            return
        # FIX-2026-10-17 (F-02): this used to be a bare remove()+save() with
        # no confirmation. It is the one destructive action on this page —
        # the endpoint AND the stored API key go together and there is no
        # undo — while its siblings (history delete, history clear-all) have
        # asked via MessageBox since REVIEW-2026-09-20. This page just
        # drifted. The name is resolved BEFORE the dialog so a cancelled
        # dialog does not even touch the store.
        provider = next((p for p in store.providers if p.id == pid), None)
        name = provider.name if provider else pid
        confirm = MessageBox(
            self._t("providers.deleteConfirm"),
            self._t("providers.deleteConfirmHint").format(name=name),
            self.win,
        )
        if not confirm.exec():
            return
        store.remove(pid)
        # UX-2026-10-18: remove() persists internally — the explicit save()
        # was a second identical write of the same file.
        self.win.invalidate_provider_cache()
        self._refresh()
        self._sync_settings_page()

    def _sync_settings_page(self) -> None:
        """Push the freshly-changed active provider into the Settings page.

        Without this the Settings page keeps showing the old name /
        endpoint / model until the user switches tabs or restarts the
        app. Best-effort: a missing or misnamed settings page is
        silently ignored.
        """
        try:
            sp = getattr(self.win, "settings_page", None)
            if sp is not None and hasattr(sp, "refresh_active"):
                sp.refresh_active()
        except Exception as exc:
            print(f"[rca] sync settings page: {exc}")

    # ---- the plan's remaining quota (cc-switch's UsageFooter) -----------

    def _start_quota_queries(self, cards) -> None:
        """Ask each provider's plan how much of its quota is left.

        The query is a NETWORK round trip, so it runs on a worker thread
        per card and the result is applied to whichever card is on screen
        when it arrives -- the same "resolve the live card by provider id"
        rule the connection test uses, because a refresh between the query
        and its answer replaces the widget the answer was meant for.
        """
        from PySide6.QtCore import QThread

        class _QuotaWorker(QThread):
            done = Signal(object, object)      # (worker, result)

            def __init__(self, provider, parent=None):
                super().__init__(parent)
                self._p = provider

            def run(self):
                # An exception here would leave the card without a quota
                # and, worse, without emitting done() -- so the worker
                # would never be released. The card simply shows nothing,
                # which is what a provider with no published quota does.
                try:
                    from rca_core.llm import query_coding_plan_quota
                    result = query_coding_plan_quota(self._p, timeout_sec=12)
                except Exception:
                    log.exception("quota query failed for %r",
                                  getattr(self._p, "name", ""))
                    result = None
                self.done.emit(self, result)

        for card in cards:
            prov = card.provider
            if prov is None or not (getattr(prov, "api_key", "") or "").strip():
                continue
            w = _QuotaWorker(prov)
            self._quota_workers[w] = card
            w.done.connect(self._on_quota_done)
            w.finished.connect(self._on_quota_finished)
            w.start()

    def _on_quota_done(self, worker, result) -> None:
        """GUI-thread slot for the quota worker's answer."""
        card = self._quota_workers.get(worker)
        if card is None:
            return
        live = card
        if card.provider is not None:
            live = self._live_card_for(card.provider.id) or card
        try:
            live.set_quota(result)
        except RuntimeError:
            # The card was destroyed between the answer and this call.
            pass

    def _on_quota_finished(self, *args) -> None:
        """Drop the strong reference once the worker is done."""
        worker = args[0] if args else self.sender()
        if worker is not None:
            self._quota_workers.pop(worker, None)

    def _test(self, provider, card):
        card.set_testing(True)
        card.set_health(0)
        if provider is not None:
            self._testing_ids.add(provider.id)
        from PySide6.QtCore import QThread

        class _Worker(QThread):
            # REVIEW-2026-09-20: (worker, result) — carrying the worker lets
            # the page connect a BOUND METHOD. A bound method of a QObject is
            # resolved by Qt to a queued (auto) connection, so the handler
            # runs on the GUI thread. The single-payload version was wired
            # through a lambda, and a lambda has no receiver QObject → DIRECT
            # connection → `_on_test_done` mutated the card widgets AND wrote
            # the ProviderStore (update + save) from inside this thread.
            done = Signal(object, object)
            def __init__(self, p, parent=None):
                super().__init__(parent)
                self._p = p
            def run(self):
                from rca_core.llm import test_llm_connection
                # Audit fix (MEDIUM parity gap): validate the provider's
                # endpoint before issuing any authenticated request. The
                # extract path was hardened against SSRF + cleartext-key
                # leak via _validate_endpoint, but this probe path was
                # not, so clicking Test on a provider pointing at an
                # internal host or http:// would dial the target with
                # the API key in the clear.
                try:
                    if self._p is not None:
                        from rca_core.ssrf import validate_endpoint
                        ok, why = validate_endpoint(self._p.endpoint)
                        if not ok:
                            # Match the shape returned by test_llm_connection
                            # so _on_test_done renders a graceful failure
                            # instead of a crash.
                            self.done.emit(self, {
                                "ok": False,
                                "error": f"bad endpoint: {why}",
                            })
                            return
                except Exception as e:
                    log.exception("connection-test endpoint validation failed")
                    self.done.emit(self, {
                        "ok": False,
                        "error": f"endpoint validation error: {e}",
                    })
                    return
                # REVIEW-2026-09-20: BUG13 parity with the SettingsPage probe
                # worker — an exception escaping run() used to kill the thread
                # without ever emitting done(), leaving the card stuck on the
                # ⏳ "testing" badge (and the worker in _test_workers) forever.
                try:
                    self.done.emit(self, test_llm_connection(self._p, timeout_sec=8))
                except Exception as exc:
                    log.exception("connection test raised in worker thread")
                    self.done.emit(self, {"ok": False, "error": str(exc)})

        # FIX (H1): do NOT disconnect/quit any in-flight worker. Each test
        # runs to completion and reports to its own card via the worker→card
        # mapping below. Disconnecting the predecessor's done-signal was
        # the original freeze bug — its card would never receive the result
        # and stayed stuck in the ⏳ "testing" state forever. We keep a
        # strong reference in self._test_workers so the QThread is never
        # deallocated mid-run (the reason the old code retired workers).
        w = _Worker(provider)
        self._test_workers[w] = card
        # REVIEW-2026-09-20: bound methods of this page (a QObject) → queued
        # connections that run on the GUI thread. The card is resolved from
        # the worker→card map instead of a closure capture, so the handler
        # signature no longer needs the starting card at connect time.
        w.done.connect(self._on_worker_done)
        w.finished.connect(self._on_worker_finished)
        # B-11: second finish hook — no-argument, so it actually gets called
        # (see _release_parked_cards / _on_worker_finished). It releases any
        # card a mid-test _refresh() had to park.
        w.finished.connect(self._release_parked_cards)
        w.start()

    def _on_worker_done(self, worker, res):
        """GUI-thread slot for ``_Worker.done`` (see _test)."""
        self._on_test_done(worker, self._test_workers.get(worker), res)

    def _on_worker_finished(self, *args) -> None:
        """GUI-thread slot for ``QThread.finished``: drop the strong ref.

        The old lambda ran in the worker thread and popped the page's dict
        from there — a data race against the GUI thread's own reads of
        _test_workers (and closeEvent's clear()).

        AUDIT-2026-09-27 [item 1.15]: this slot used to take ONE required
        parameter and was connected straight to ``QThread.finished``, which
        carries NO argument. Every connection therefore raised TypeError inside
        the event loop and the body never ran, so ``_test_workers`` never
        actually drained — the leak B-11 had to work around. The sender is
        resolved with ``self.sender()`` and the parameter is now optional, so
        the slot works whether Qt passes the signal's (empty) argument list or
        a caller supplies the worker explicitly.
        """
        worker = args[0] if args else self.sender()
        if worker is not None:
            self._test_workers.pop(worker, None)

    def _card_in_flight(self, card) -> bool:
        """B-11: is this card still the target of a running test?

        Keyed on the page's own ``_testing_ids`` set rather than on
        ``_test_workers`` membership: the id is added in _test() and
        discarded in _on_test_done(), which is the exact window during which
        a result still has to reach a card, and it does not depend on
        _on_worker_finished() (see the note there).
        """
        pid = getattr(getattr(card, "provider", None), "id", "") or ""
        return bool(pid) and pid in self._testing_ids

    def _release_parked_cards(self) -> None:
        """B-11: the "on finish" half of the park.

        A no-argument bound method, so ``QThread.finished`` can call it and
        PySide6 still delivers it as a QUEUED connection on the GUI thread
        (a lambda would have connected DIRECT and run in the worker thread —
        the data race the _on_worker_finished docstring warns about). By the
        time it runs, ``done`` has already been handled and _testing_ids no
        longer names the provider, so every parked card is free to go.
        """
        try:
            self.list_widget.flush_parked()
        except RuntimeError:
            # Underlying C++ object already gone (window teardown).
            pass

    def _on_test_done(self, worker, card, res):
        # REVIEW-2026-09-20: `card` may be None when the worker had already
        # been dropped from _test_workers (window teardown) — guard the whole
        # handler instead of dereferencing card.provider deep inside it.
        if card is None:
            self._testing_ids.discard(
                getattr(getattr(worker, "_p", None), "id", "") or "")
            return
        # FIX (H2) + AUDIT-2026-09-27 [item B-11] (B-11): the `card` captured
        # at start time may no longer be the one on screen — a `_refresh()`
        # that ran mid-test REPLACED it (the old one is setParent(None)'d and
        # left parentless; it is now parked or deleted, never an orphan).
        # Writing to the replaced card would update a widget the user cannot
        # see while the real, visible card for the same provider stays stuck
        # in ⏳. Resolve the live card by provider id and update that instead;
        # if the provider was deleted mid-test, there is no live card and we
        # just clear the in-flight state.
        live = card
        if card is not None and card.provider is not None:
            pid = card.provider.id
            live = self._live_card_for(pid)
            if live is None:
                # Provider deleted mid-test — nothing visible to update.
                self._testing_ids.discard(pid)
                return
        if live is not None:
            live.set_testing(False)
        if card is not None and card.provider is not None:
            self._testing_ids.discard(card.provider.id)
        if res is None:
            self.lbl_test.setText("✗")
            return
        # Sprint B (REVIEW-2026-09-04): the SSRF-guard failure path in
        # _test() emits a plain dict, not an ExtractResult-shaped object —
        # normalise it so the attribute reads below cannot raise
        # AttributeError inside a Qt slot.
        if isinstance(res, dict):
            from types import SimpleNamespace as _NS
            res = _NS(ok=bool(res.get("ok")), latency_ms=0,
                      models_sample=[], status=None,
                      error_key=res.get("error") or "err.http")
        # Read the persisted streak from the provider record (re-fetched
        # by id so a concurrent rename/edit doesn't race the write below),
        # not from card._health_failures — _test() resets that to 0 at
        # the start of the in-progress test for the visual badge, which
        # would otherwise make every failure look like the first.
        store = self._load_store()
        # Sprint B (REVIEW-2026-09-04): don't clobber the resolved live
        # card here (the old code reused the `live` local for the store
        # record, which made the health writes below ambiguous).
        persist_rec = store.by_id(card.provider.id) if (store and card.provider is not None) else None
        old_streak = int(getattr(persist_rec, "consecutive_failures", None)
                         if persist_rec is not None
                         else getattr(card.provider, "consecutive_failures", 0) or 0)
        # Sprint B (REVIEW-2026-09-04): write the health badge to the LIVE
        # card (resolved above via _live_card_for), not the start-time
        # `card` — set_testing was already fixed to use the live card but
        # set_health still wrote the orphan, so after a card rebuild the
        # visible badge kept its stale ⚠/✗ state.
        badge_card = live if live is not None else card
        if res.ok:
            new_streak = 0
            badge_card.set_health(0)
            txt = "✓  " + str(res.latency_ms) + " ms"
            if res.models_sample:
                txt += "  ·  " + ", ".join(res.models_sample[:3])
        else:
            new_streak = min(old_streak + 1, 3)
            badge_card.set_health(new_streak)
            txt = "✗  " + self._t(getattr(res, 'error_key', None) or "err.http")
            if getattr(res, 'status', None):
                txt += f"  (HTTP {res.status})"
        # AUDIT-2026-09-29: on failure, put the provider's own words in a
        # tooltip. The badge is deliberately left as the short form -- a
        # multi-sentence upstream error in a card label would reflow every
        # card in the list -- but "err.http (HTTP 400)" on its own cannot tell
        # a user whether to fix the model name, the key, or their quota, and
        # that is the entire question this feature exists to answer. The body
        # arrives already redacted (llm.ConnectionResult.error_body), because
        # it is where a rejected key comes back echoed. Truncated so a
        # multi-KB HTML error page cannot build a giant tooltip, and cleared
        # on success so a previous failure never lingers.
        detail = ""
        if not getattr(res, "ok", False):
            detail = (getattr(res, "error_body", "") or "").strip()
            if len(detail) > 200:
                detail = detail[:200] + "…"
        try:
            self.lbl_test.setToolTip(detail)
        except Exception:
            pass
        # Persist the streak to the provider store so the badge survives
        # an app restart. Write the live by-id record (not card.provider,
        # which may be stale if a concurrent rename/edit rebuilt the cards)
        # and only touch consecutive_failures so the user's other fields
        # are preserved. Failure to write isn't fatal — we just log and
        # continue (the badge still updates in-memory for this session).
        if new_streak != old_streak and card.provider is not None:
            try:
                store = self._load_store()
                cur = store.by_id(card.provider.id) if store else None
                target = cur or card.provider
                target.consecutive_failures = new_streak
                if store:
                    # UX-2026-10-18: update() persists internally; the extra
                    # save() wrote the same file again.
                    store.update(target)
            except Exception as exc:
                print(f"[rca] persist health streak failed: {exc}")
        self.lbl_test.setText(txt)

    def retranslate(self):
        if hasattr(self, "lbl_title"):
            self.lbl_title.setText(self._t("settings.llmProvider"))
        if hasattr(self, "btn_add"):
            self.btn_add.setText(self._t("settings.addProvider"))
        # The search bar is hidden by default and only revealed on
        # Ctrl+F, so the original placeholder was set in whatever
        # language was active at startup — refresh it so a user who
        # switches languages mid-session sees the new translation.
        if hasattr(self, "_search_input"):
            self._search_input.setPlaceholderText(
                self._t("provider.searchPlaceholder")
                if self._t("provider.searchPlaceholder") != "provider.searchPlaceholder"
                else "Search providers... (Ctrl+F)"
            )
        if hasattr(self, "list_widget"):
            self._refresh()
