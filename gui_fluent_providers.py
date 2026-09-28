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
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget, QLineEdit,
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
    LineEdit, PasswordLineEdit, PillPushButton, PrimaryPushButton, PushButton,
    ScrollArea, SearchLineEdit, StrongBodyLabel, BodyLabel, CaptionLabel,
    TitleLabel, ToolButton, TransparentToolButton, isDarkTheme, qconfig,
)

from rca_core import (
    PROVIDER_PRESETS, ApiFormat, LlmProvider, ProviderStore, Translator,
)
from rca_core.extractor import DEFAULT_ENDPOINT, DEFAULT_MODEL
from rca_core.llm import test_llm_connection
from rca_core.ssrf import validate_endpoint as _validate_endpoint


def _icon_char(name: str) -> str:
    """Derive a 1–2 char glyph from the provider name for the icon tile."""
    if not name:
        return "?"
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


# ---------------------------------------------------------------------------
# Per-provider icon tile (colored rounded rectangle + glyph)
# ---------------------------------------------------------------------------
class ProviderIconTile(QLabel):
    def __init__(self, name, parent=None):
        super().__init__(_icon_char(name), parent)
        self.setFixedSize(40, 40)
        self.setAlignment(Qt.AlignCenter)
        self.setObjectName("provIconTile")
        h = abs(hash(name or "")) % 360
        self.setStyleSheet(
            f"#provIconTile{{background-color:hsl({h},55%,47%);"
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
        self.setMaximumHeight(120)
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

        row.addWidget(ProviderIconTile(provider.name))

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
        self._ipt_endpoint = LineEdit()
        self._ipt_endpoint.setPlaceholderText("https://")
        fl.addWidget(self._ipt_endpoint, r, 0, 1, 2); r += 1

        lbl = BodyLabel(self._t("settings.apiKey"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_key = PasswordLineEdit()
        self._ipt_key.setPlaceholderText("sk-…")
        fl.addWidget(self._ipt_key, r, 0, 1, 2); r += 1

        # API Key field name selector (cc-switch alignment)
        lbl = BodyLabel(self._t("wizard.apiKeyField"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._cmb_key_field = ComboBox()
        self._cmb_key_field.addItems(["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"])
        self._cmb_key_field.setCurrentIndex(0)
        fl.addWidget(self._cmb_key_field, r, 0, 1, 2); r += 1

        lbl = BodyLabel(self._t("settings.model"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_model = LineEdit()
        fl.addWidget(self._ipt_model, r, 0, 1, 2); r += 1

        # Extra headers field
        lbl = BodyLabel(self._t("wizard.extraHeaders"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_extra_headers = LineEdit()
        self._ipt_extra_headers.setPlaceholderText('{"X-Custom-Header": "value"}')
        fl.addWidget(self._ipt_extra_headers, r, 0, 1, 2); r += 1

        fl.setColumnStretch(1, 1)
        self._form.setVisible(False)
        self._root.addWidget(self._form)

        # ---- footer ----
        footer = QHBoxLayout()
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
        custom.clicked.connect(self._show_form)
        self._grid.addWidget(custom, r, c)

    def _pick_preset(self, preset):
        self._ipt_name.setText(preset.name)
        self._cmb_fmt.setCurrentText(preset.api_format.value)
        self._ipt_endpoint.setText(preset.endpoint)
        self._ipt_model.setText(preset.model)
        self._preset = preset
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

    def _prefill(self, existing: LlmProvider):
        self._ipt_name.setText(existing.name)
        self._cmb_fmt.setCurrentText(
            existing.api_format.value if hasattr(existing.api_format, 'value')
            else str(existing.api_format)
        )
        self._ipt_endpoint.setText(existing.endpoint)
        self._ipt_model.setText(existing.model)
        self._ipt_key.setText(existing.api_key)
        # Restore extra_headers as JSON string
        if existing.extra_headers:
            import json
            try:
                self._ipt_extra_headers.setText(json.dumps(existing.extra_headers))
            except Exception:
                pass
        self._show_form()

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

    def _on_ok(self):
        name = self._ipt_name.text().strip()
        endpoint = self._ipt_endpoint.text().strip().rstrip("/")
        if not name or not endpoint:
            missing = []
            if not name:
                missing.append(self._t("wizard.fieldName"))
            if not endpoint:
                missing.append(self._t("wizard.fieldEndpoint"))
            msg = self._tr.t("wizard.fillRequired") + ", ".join(missing)
            InfoBar.warning("", msg, parent=self, position=InfoBarPosition.TOP)
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
                self._tr.t("wizard.extraHeadersIgnored", {"reason": headers_err}),
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
        """Filter cards by search term, matching cc-switch search UX."""
        q = self._search_term
        for card in self._all_cards:
            if not q:
                card.setVisible(True)
            else:
                name_match = q in (card.provider.name or "").lower()
                endpoint_match = q in (card.provider.endpoint or "").lower()
                card.setVisible(name_match or endpoint_match)

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
            self._show_empty()
            return
        self._hide_empty()
        self.list_widget.set_cards(cards)
        self._apply_filter()

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
                store = self._load_store()
                if store:
                    store.add(wiz.created_provider)
                    store.set_current(wiz.created_provider.id)
                    store.save()
                    self.win.invalidate_provider_cache()
                    self._refresh()
                    self._sync_settings_page()
        finally:
            self._provider_wizard = None

    def _set_current(self, pid):
        store = self._load_store()
        if store:
            store.set_current(pid)
            store.save()
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
            store.save()
            self.win.invalidate_provider_cache()
            self._refresh()
            self._sync_settings_page()

    def _delete(self, pid):
        store = self._load_store()
        if store:
            store.remove(pid)
            store.save()
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
                    store.update(target)
                    store.save()
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
