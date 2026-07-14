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

from PySide6.QtCore import (
    Qt, QMimeData, QPoint, QSize, Signal, QTimer,
)
from PySide6.QtGui import QDrag, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QScrollArea, QVBoxLayout, QWidget, QLineEdit,
)

from qfluentwidgets import (
    CardWidget, ComboBox, FluentIcon as FIF, InfoBar, InfoBarPosition,
    LineEdit, PasswordLineEdit, PrimaryPushButton, PushButton,
    ScrollArea, SearchLineEdit, StrongBodyLabel, BodyLabel, CaptionLabel,
    TitleLabel, ToolButton, isDarkTheme,
)

from rca_core import (
    PROVIDER_PRESETS, ApiFormat, LlmProvider, ProviderStore, Translator,
)
from rca_core.extractor import DEFAULT_ENDPOINT, DEFAULT_MODEL
from rca_core.llm import test_llm_connection


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
        self._health_failures = 0   # consecutive failures from last test
        self.setObjectName("providerCard")
        self.setFixedHeight(82)
        self.setAttribute(Qt.WA_Hover, True)

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
        self.lbl_name = StrongBodyLabel(provider.name or "Provider")
        name_row.addWidget(self.lbl_name)
        self.lbl_dot = CaptionLabel("●" if is_active else "○")
        self.lbl_dot.setStyleSheet(
            "color:#2563eb;" if is_active else "color:rgba(120,120,120,0.7);"
        )
        name_row.addWidget(self.lbl_dot)
        self._health_badge = HealthBadge(0)
        name_row.addWidget(self._health_badge)
        name_row.addStretch(1)
        mid.addLayout(name_row)
        sub = provider.endpoint or "(" + translate("provider.noEndpoint") + ")"
        self.lbl_sub = CaptionLabel(sub)
        mid.addWidget(self.lbl_sub)
        row.addLayout(mid, 1)

        # Action buttons — hidden until card is hovered (cc-switch style)
        self._actions_widget = QWidget()
        self._actions_layout = QHBoxLayout(self._actions_widget)
        self._actions_layout.setContentsMargins(0, 0, 0, 0)
        self._actions_layout.setSpacing(4)
        self._actions_widget.setVisible(False)

        self.btn_test = PushButton(translate("settings.testConnection"))
        self.btn_test.setFixedHeight(28)
        self.btn_test.setStyleSheet(
            "PushButton{background:transparent;color:#475569;border:1px solid #e2e8f0;padding:0 8px;border-radius:6px}"
            "PushButton:hover{color:#0f172a;background:#f1f5f9}"
        )
        self.btn_active = PushButton(translate("wizard.setActive"))
        self.btn_active.setFixedHeight(28)
        self.btn_active.setStyleSheet(
            "PushButton{background:transparent;color:#475569;border:1px solid #e2e8f0;padding:0 8px;border-radius:6px}"
            "PushButton:hover{color:#0f172a;background:#f1f5f9}"
        )
        self.btn_edit = ToolButton(FIF.EDIT)
        self.btn_edit.setFixedSize(28, 28)
        self.btn_edit.setStyleSheet(
            "ToolButton{background:transparent;color:#94a3b8;border:none;border-radius:6px;padding:2px}"
            "ToolButton:hover{color:#0f172a;background:#f1f5f9}"
        )
        self.btn_delete = ToolButton(FIF.DELETE)
        self.btn_delete.setFixedSize(28, 28)
        self.btn_delete.setStyleSheet(
            "ToolButton{background:transparent;color:#94a3b8;border:none;border-radius:6px;padding:2px}"
            "ToolButton:hover{color:#dc2626;background:#fef2f2}"
        )
        self._actions_layout.addWidget(self.btn_test)
        self._actions_layout.addWidget(self.btn_active)
        self._actions_layout.addWidget(self.btn_edit)
        self._actions_layout.addWidget(self.btn_delete)
        row.addWidget(self._actions_widget)

        self.set_active(is_active)

    def set_active(self, active: bool):
        self._active = active
        self.lbl_dot.setText("●" if active else "○")
        self.lbl_dot.setStyleSheet(
            "color:#2563eb;" if active else "color:rgba(120,120,120,0.7);"
        )
        if active:
            self.setStyleSheet(
                "#providerCard{border:1.5px solid rgba(37,99,235,0.55);"
                "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 rgba(37,99,235,0.08),stop:1 transparent)}"
            )
            self.btn_active.setVisible(False)
        else:
            self.setStyleSheet(
                "#providerCard{border:1px solid rgba(0,0,0,0.08)}"
                "#providerCard:hover{border-color:rgba(37,99,235,0.3)}"
            )
            self.btn_active.setVisible(True)

    def set_testing(self, testing: bool):
        self._testing = testing
        self.btn_test.setEnabled(not testing)
        self.btn_test.setText("⏳" if testing else self._t("settings.testConnection"))

    def set_health(self, consecutive_failures: int):
        """Update the health badge from connection test result."""
        self._health_failures = consecutive_failures
        self._health_badge.set_failures(consecutive_failures)

    def enterEvent(self, event):
        """Show action buttons when mouse hovers over card."""
        self._actions_widget.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Hide action buttons when mouse leaves card."""
        self._actions_widget.setVisible(False)
        super().leaveEvent(event)


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
        self.setAcceptDrops(True)

    def set_cards(self, cards: list[ProviderCard]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._cards = list(cards)
        for c in cards:
            c.setParent(self)
            c.setFixedWidth(max(400, self.width()))
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
        # Re-layout.
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        for c in self._cards:
            c.setParent(self)
            c.setFixedWidth(max(400, self.width()))
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
        self.setWindowTitle(self._t("wizard.choosePreset") if existing is None
                           else "Configure: " + (existing.name or ""))
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
        fl.addWidget(self._ipt_name, r - 1, 0, 1, 2)

        lbl = BodyLabel(self._t("wizard.fieldFormat"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._cmb_fmt = ComboBox()
        self._cmb_fmt.addItems([f.value for f in ApiFormat])
        fl.addWidget(self._cmb_fmt, r - 1, 0, 1, 2)

        lbl = BodyLabel(self._t("wizard.fieldEndpoint"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_endpoint = LineEdit()
        self._ipt_endpoint.setPlaceholderText("https://")
        fl.addWidget(self._ipt_endpoint, r - 1, 0, 1, 2)

        lbl = BodyLabel(self._t("settings.apiKey"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_key = PasswordLineEdit()
        self._ipt_key.setPlaceholderText("sk-…")
        fl.addWidget(self._ipt_key, r - 1, 0, 1, 2)

        # API Key field name selector (cc-switch alignment)
        lbl = BodyLabel(self._t("wizard.apiKeyField"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._cmb_key_field = ComboBox()
        self._cmb_key_field.addItems(["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"])
        self._cmb_key_field.setCurrentIndex(0)
        fl.addWidget(self._cmb_key_field, r - 1, 0, 1, 2)

        lbl = BodyLabel(self._t("settings.model"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_model = LineEdit()
        fl.addWidget(self._ipt_model, r - 1, 0, 1, 2)

        # Extra headers field
        lbl = BodyLabel(self._t("wizard.extraHeaders"))
        fl.addWidget(lbl, r, 0, 1, 2); r += 1
        self._ipt_extra_headers = LineEdit()
        self._ipt_extra_headers.setPlaceholderText('{"X-Custom-Header": "value"}')
        fl.addWidget(self._ipt_extra_headers, r - 1, 0, 1, 2)

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

    def _parse_extra_headers(self) -> dict:
        """Parse extra_headers from the text field."""
        text = self._ipt_extra_headers.text().strip()
        if not text:
            return {}
        try:
            import json
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {}

    def _on_ok(self):
        name = self._ipt_name.text().strip()
        endpoint = self._ipt_endpoint.text().strip().rstrip("/")
        if not name or not endpoint:
            missing = []
            if not name:
                missing.append(self._t("wizard.fieldName"))
            if not endpoint:
                missing.append(self._t("wizard.fieldEndpoint"))
            msg = "Please fill in: " + ", ".join(missing)
            InfoBar.warning("", msg, parent=self, position=InfoBarPosition.TOP)
            return
        try:
            fmt = ApiFormat(self._cmb_fmt.currentText())
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        extra_headers = self._parse_extra_headers()
        self.created_provider = LlmProvider(
            name=name,
            api_format=fmt,
            endpoint=endpoint,
            api_key=self._ipt_key.text().strip(),
            model=self._ipt_model.text().strip(),
            extra_headers=extra_headers,
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
        self._test_worker = None
        self._test_target: LlmProvider | None = None
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

    def _refresh(self):
        if not hasattr(self, "list_widget"):
            return
        # Store health state for cards that will be rebuilt
        old_health: dict[str, int] = {}
        for card in self._all_cards:
            if card.provider:
                old_health[card.provider.id] = card._health_failures

        cards = self._build_cards()
        # Restore health state
        for card in cards:
            if card.provider and card.provider.id in old_health:
                card.set_health(old_health[card.provider.id])

        self._all_cards = cards
        if not cards:
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
            self._empty.setParent(None)
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
        store.save()
        self.win.invalidate_provider_cache()
        # Refresh to rebuild cards
        self._refresh()

    def _add_provider(self):
        wiz = ProviderWizard(self.win, self._t)
        if wiz.exec() and wiz.created_provider:
            store = self._load_store()
            if store:
                store.add(wiz.created_provider)
                store.set_current(wiz.created_provider.id)
                store.save()
                self.win.invalidate_provider_cache()
                self._refresh()

    def _set_current(self, pid):
        store = self._load_store()
        if store:
            store.set_current(pid)
            store.save()
            self.win.invalidate_provider_cache()
            self._refresh()

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
            store.update(wiz.created_provider)
            store.save()
            self.win.invalidate_provider_cache()
            self._refresh()

    def _delete(self, pid):
        store = self._load_store()
        if store:
            store.remove(pid)
            store.save()
            self.win.invalidate_provider_cache()
            self._refresh()

    def _test(self, provider, card):
        card.set_testing(True)
        card.set_health(0)
        self._test_target = provider
        from PySide6.QtCore import QThread

        class _Worker(QThread):
            done = Signal(object)
            def __init__(self, p, parent=None):
                super().__init__(parent)
                self._p = p
            def run(self):
                from rca_core.llm import test_llm_connection
                self.done.emit(test_llm_connection(self._p, timeout_sec=8))

        self._test_worker = _Worker(provider)
        self._test_worker.done.connect(lambda res: self._on_test_done(card, res))
        self._test_worker.start()

    def _on_test_done(self, card, res):
        card.set_testing(False)
        if res is None:
            self.lbl_test.setText("✗")
            return
        if res.ok:
            card.set_health(0)
            txt = "✓  " + str(res.latency_ms) + " ms"
            if res.models_sample:
                txt += "  ·  " + ", ".join(res.models_sample[:3])
        else:
            # Count consecutive failures (capped at 3 for badge display)
            failures = getattr(res, 'consecutive_failures', 1)
            card.set_health(min(failures, 3))
            txt = "✗  " + self._t(getattr(res, 'error_key', None) or "err.http")
            if getattr(res, 'status', None):
                txt += f"  (HTTP {res.status})"
        self.lbl_test.setText(txt)

    def retranslate(self):
        if hasattr(self, "lbl_title"):
            self.lbl_title.setText(self._t("settings.llmProvider"))
        if hasattr(self, "btn_add"):
            self.btn_add.setText(self._t("settings.addProvider"))
        if hasattr(self, "list_widget"):
            self._refresh()
