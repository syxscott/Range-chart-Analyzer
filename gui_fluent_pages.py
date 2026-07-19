"""New pages for the Fluent GUI: History + Usage.

These two pages sit alongside the existing Extract / Providers / Settings
pages in the main window's navigation. They share the app's HistoryStore
+ UsageStore instances and retranslate themselves when the language
switches.

Charts
------
For the Usage page we render three lightweight charts via matplotlib
(must be installed). When matplotlib is missing the page falls back to
plain text tables so the screen still shows meaningful data. The
charts auto-size to the page width and re-render on theme change.
"""

from __future__ import annotations

import io
import logging
import os
import time
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QImage, QPainter, QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QFileDialog, QMessageBox, QPlainTextEdit, QTableWidgetItem,
)

from qfluentwidgets import (
    ScrollArea, CardWidget, TitleLabel, SubtitleLabel, BodyLabel,
    CaptionLabel, StrongBodyLabel, PrimaryPushButton, PushButton,
    ToolButton, FluentIcon as FIF, InfoBar, InfoBarPosition,
    TableWidget, HeaderCardWidget, SegmentedWidget, EditableComboBox,
    LineEdit, MessageBox,
)

from rca_core import (
    HistoryStore, HistoryRecord, UsageStore, UsageRecord, Database,
    to_xlsx, to_csv, result_to_json, Translator,
)


# ---------------------------------------------------------------------------
# Matplotlib import (optional)
# ---------------------------------------------------------------------------

def _mpl():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        return plt
    except Exception:
        return None


def _fig_to_pixmap(fig, dpi: int = 100) -> QPixmap | None:
    """Render a matplotlib figure to a QPixmap at the requested dpi."""
    try:
        buf = io.BytesIO()
        # Do NOT use bbox_inches='tight' — it causes the figure to resize
        # unpredictably across Windows DPI scales, producing clipped or
        # very small output. We use explicit subplots_adjust() instead.
        fig.savefig(buf, format="png", dpi=dpi)
        plt = _mpl()
        plt.close(fig)
        buf.seek(0)
        img = QImage.fromData(buf.getvalue())
        if img.isNull():
            return None
        return QPixmap.fromImage(img)
    except Exception as exc:
        logging.warning("Failed to render matplotlib figure: %s", exc)
        return None


# ---------------------------------------------------------------------------
# History page
# ---------------------------------------------------------------------------

class HistoryPage(ScrollArea):
    """Browse past extractions: load, edit notes, export, delete."""

    def __init__(self, win, history_store: HistoryStore, translator: Translator) -> None:
        super().__init__(win)
        self._win = win
        self._store = history_store
        self._tr = translator
        self._records: list[HistoryRecord] = []
        self._filter_mode = "all"
        self._search_text = ""

        self.setObjectName("historyPage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        self.setWidget(container)
        v = QVBoxLayout(container)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(14)

        self.lbl_title = TitleLabel(self._t("history.title"))
        self.lbl_subtitle = CaptionLabel(self._t("history.subtitle"))
        v.addWidget(self.lbl_title)
        v.addWidget(self.lbl_subtitle)

        # Filter row
        bar = QHBoxLayout()
        self.search = LineEdit()
        self.search.setPlaceholderText(self._t("history.searchPlaceholder"))
        self.search.textChanged.connect(self._on_search)
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 1)

        self.filter_combo = SegmentedWidget()
        for label, key in [
            ("history.filter.all", "all"),
            ("history.filter.rangeChart", "range_chart"),
            ("history.filter.columnar", "columnar_section"),
        ]:
            self.filter_combo.addItem(routeKey=key, text=self._t(label))
        self.filter_combo.setCurrentItem("all")
        self.filter_combo.currentItemChanged.connect(self._on_filter)
        bar.addWidget(self.filter_combo)

        self.btn_refresh = ToolButton(FIF.SYNC)
        self.btn_refresh.setToolTip(self._t("action.refresh"))
        self.btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(self.btn_refresh)

        self.btn_clear = PushButton(self._t("history.action.deleteAll"))
        self.btn_clear.clicked.connect(self._on_clear)
        bar.addWidget(self.btn_clear)
        v.addLayout(bar)

        # Table
        self.table = TableWidget()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setMinimumHeight(420)
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            self._t("history.col.id"),
            self._t("history.col.time"),
            self._t("history.col.file"),
            self._t("history.col.provider"),
            self._t("history.col.model"),
            self._t("history.col.mode"),
            self._t("history.col.confidence"),
            self._t("history.col.actions"),
        ])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(TableWidget.NoEditTriggers)
        v.addWidget(self.table, 1)

        # Notes editor (visible when a row is selected)
        self.notes_card = CardWidget()
        notes_v = QVBoxLayout(self.notes_card)
        notes_v.setContentsMargins(16, 14, 16, 14)
        self.notes_lbl = StrongBodyLabel(self._t("history.col.notes"))
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(self._t("history.notesPlaceholder"))
        self.notes_edit.setMaximumHeight(80)
        self.notes_edit.textChanged.connect(self._on_notes_changed)
        notes_v.addWidget(self.notes_lbl)
        notes_v.addWidget(self.notes_edit)
        notes_btn_row = QHBoxLayout()
        self.btn_save_notes = PrimaryPushButton(self._t("action.save"))
        self.btn_save_notes.clicked.connect(self._on_save_notes)
        self.btn_save_notes.setEnabled(False)
        notes_btn_row.addStretch(1)
        notes_btn_row.addWidget(self.btn_save_notes)
        notes_v.addLayout(notes_btn_row)
        v.addWidget(self.notes_card)

        self.table.currentCellChanged.connect(self._on_row_selected)
        # Double-click opens the HistoryDetailDialog (mirrors cc-switch's
        # request-detail interaction). Single-click still drives
        # currentCellChanged → notes editor load, so the two don't fight.
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self.refresh()

    def _t(self, key: str) -> str:
        return self._tr.t(key)

    def set_translator(self, tr: Translator) -> None:
        self._tr = tr
        self.lbl_title.setText(self._t("history.title"))
        self.lbl_subtitle.setText(self._t("history.subtitle"))
        self.search.setPlaceholderText(self._t("history.searchPlaceholder"))
        # The segmented filter's items were created in the original
        # language; re-translate them on language switch so the labels
        # follow the user's new language (without losing the selection).
        cur = self.filter_combo.currentItem()
        for label, key in [
            ("history.filter.all", "all"),
            ("history.filter.rangeChart", "range_chart"),
            ("history.filter.columnar", "columnar_section"),
        ]:
            self.filter_combo.setItemText(key, self._t(label))
        if cur:
            self.filter_combo.setCurrentItem(cur)
        for i, k in enumerate([
            "history.col.id", "history.col.time", "history.col.file",
            "history.col.provider", "history.col.model", "history.col.mode",
            "history.col.confidence", "history.col.actions",
        ]):
            # setHorizontalHeaderItem wants a QTableWidgetItem, not a plain
            # string — the previous code passed self._t(k) which is a
            # str, causing TypeError whenever the language switched and
            # re-translated the headers.
            self.table.setHorizontalHeaderItem(i, QTableWidgetItem(self._t(k)))
        self.btn_clear.setText(self._t("history.action.deleteAll"))
        self.notes_lbl.setText(self._t("history.col.notes"))
        self.notes_edit.setPlaceholderText(self._t("history.notesPlaceholder"))
        self.btn_save_notes.setText(self._t("action.save"))
        self.refresh()

    # ---- data ----

    def refresh(self) -> None:
        try:
            self._records = self._store.list(
                limit=200,
                mode=None if self._filter_mode == "all" else self._filter_mode,
                search=self._search_text or None,
            )
        except Exception as exc:
            self._records = []
            InfoBar.error(
                "", str(exc), parent=self._win,
                position=InfoBarPosition.TOP, duration=4000,
            )
        self._populate_table()

    def _populate_table(self) -> None:
        self.table.setRowCount(len(self._records))
        for ri, rec in enumerate(self._records):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(rec.timestamp or 0))
            mode_label = {"range_chart": "Range", "columnar_section": "Columnar",
                          "abundance_diagram": "Abundance"}.get(rec.mode, rec.mode or "-")
            cells = [
                str(rec.id),
                ts,
                rec.source_file or "-",
                rec.provider_name or "-",
                rec.model or "-",
                mode_label,
                f"{(rec.confidence or 0) * 100:.0f}%" if rec.confidence else "-",
                "",  # actions cell filled below
            ]
            for ci, txt in enumerate(cells):
                from PySide6.QtWidgets import QTableWidgetItem
                item = QTableWidgetItem(txt)
                if ci == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(ri, ci, item)
            # Action buttons as a small frame in the last cell.
            btn_frame = QFrame()
            btn_layout = QHBoxLayout(btn_frame)
            btn_layout.setContentsMargins(0, 0, 0, 0)
            btn_layout.setSpacing(4)
            btn_load = ToolButton(FIF.DOWNLOAD)
            btn_load.setToolTip(self._t("history.action.load"))
            btn_load.clicked.connect(lambda _=False, r=rec: self._on_load(r))
            btn_export = ToolButton(FIF.SAVE)
            btn_export.setToolTip(self._t("history.action.export"))
            btn_export.clicked.connect(lambda _=False, r=rec: self._on_export(r))
            btn_del = ToolButton(FIF.DELETE)
            btn_del.setToolTip(self._t("history.action.delete"))
            btn_del.clicked.connect(lambda _=False, r=rec: self._on_delete(r))
            btn_layout.addWidget(btn_load)
            btn_layout.addWidget(btn_export)
            btn_layout.addWidget(btn_del)
            btn_layout.addStretch(1)
            self.table.setCellWidget(ri, len(cells) - 1, btn_frame)
        self.table.resizeColumnsToContents()

    # ---- handlers ----

    def _on_filter(self, key: str) -> None:
        self._filter_mode = key or "all"
        self.refresh()

    def _on_search(self, text: str) -> None:
        self._search_text = text.strip()
        self.refresh()

    def _on_clear(self) -> None:
        if not self._records:
            return
        confirm = MessageBox(
            self._t("history.confirmDeleteAll"),
            self._t("history.confirmDeleteAllHint"),
            self._win,
        )
        if not confirm.exec():
            return
        try:
            self._store.clear()
            # Drop the in-memory notes-edit state — all rows just went
            # away, the editor shouldn't pretend to still be attached.
            self._selected_rec = None
            self.notes_edit.blockSignals(True)
            self.notes_edit.setPlainText("")
            self.notes_edit.blockSignals(False)
            self.btn_save_notes.setEnabled(False)
            self.refresh()
        except Exception as exc:
            InfoBar.error("", str(exc), parent=self._win,
                          position=InfoBarPosition.TOP, duration=4000)

    def _on_row_selected(self, row: int, _col: int, _prev_row: int, _prev_col: int) -> None:
        if row < 0 or row >= len(self._records):
            # Clear the notes editor so it doesn't show stale data from a previously
            # selected row, and disable Save so the user can't accidentally overwrite it.
            self._selected_rec = None
            self.notes_edit.blockSignals(True)
            self.notes_edit.setPlainText("")
            self.notes_edit.blockSignals(False)
            self.btn_save_notes.setEnabled(False)
            return
        rec = self._records[row]
        # Block signals while we set the value so the textChanged handler
        # doesn't fire and mark the editor as dirty.
        self.notes_edit.blockSignals(True)
        self.notes_edit.setPlainText(rec.notes or "")
        self.notes_edit.blockSignals(False)
        self.btn_save_notes.setEnabled(False)
        self._selected_rec = rec

    def _on_notes_changed(self) -> None:
        if not getattr(self, "_selected_rec", None):
            return
        dirty = (self.notes_edit.toPlainText() !=
                 (self._selected_rec.notes or ""))
        self.btn_save_notes.setEnabled(dirty)

    def _on_save_notes(self) -> None:
        rec = getattr(self, "_selected_rec", None)
        if not rec:
            return
        try:
            self._store.update_notes(rec.id, self.notes_edit.toPlainText())
            rec.notes = self.notes_edit.toPlainText()
            self.btn_save_notes.setEnabled(False)
            InfoBar.success(
                "", self._t("status.saved"), parent=self._win,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.error("", str(exc), parent=self._win,
                          position=InfoBarPosition.TOP, duration=4000)

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        """Open the HistoryDetailDialog for the row that was double-clicked.

        Skips double-clicks on the actions column (the last column already
        has its own buttons) so a misclick on a button doesn't open the
        dialog AND trigger the button's action.
        """
        if row < 0 or row >= len(self._records):
            return
        if _col == 7:  # actions column
            return
        rec = self._records[row]
        try:
            # Lazy import: HistoryDetailDialog lives in its own module so
            # the heavy WebEngine dep doesn't drag into the rest of the
            # pages if the user only ever visits Extract / Usage.
            from gui_fluent_history_detail import HistoryDetailDialog
            dlg = HistoryDetailDialog(self._win, rec, self._tr)
            dlg.exec()
            # Force parent repaint to avoid residual border artefacts on Windows
            # after the modal dialog closes (the compositor may cache the
            # inactive state of the parent until the next paint event).
            self._win.update()
            self._win.repaint()
        except Exception as exc:
            InfoBar.error(
                "", str(exc), parent=self._win,
                position=InfoBarPosition.TOP, duration=4000,
            )

    def _on_load(self, rec: HistoryRecord) -> None:
        """Push the historical result back into the Extract page."""
        try:
            self._win.load_from_history(rec)
        except AttributeError:
            # Older build — fall back to status update.
            InfoBar.info("", f"Loaded record #{rec.id}",
                         parent=self._win, position=InfoBarPosition.TOP,
                         duration=2000)

    def _on_export(self, rec: HistoryRecord) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self._win,
            self._t("export.xlsx"),
            f"history_{rec.id}.xlsx",
            "Excel (*.xlsx);;JSON (*.json);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".xlsx"):
                to_xlsx(rec.result, file_or_path=path, translate=self._t)
            elif path.lower().endswith(".json"):
                from json import dump
                with open(path, "w", encoding="utf-8") as f:
                    dump(rec.to_dict(), f, ensure_ascii=False, indent=2, default=str)
            else:
                # CSV: dump sections only (multi-table export to CSV isn't
                # practical; the user gets the same flat table format
                # extract page uses).
                with open(path, "w", encoding="utf-8") as f:
                    f.write("id,name\n")
                    for sec in rec.result.get("sections", []):
                        f.write(f"{rec.id},{sec.get('name','')}\n")
            InfoBar.success(
                "", self._t("status.saved"), parent=self._win,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.error("", str(exc), parent=self._win,
                          position=InfoBarPosition.TOP, duration=4000)

    def _on_delete(self, rec: HistoryRecord) -> None:
        confirm = MessageBox(
            self._t("history.confirmDelete"),
            self._t("history.confirmDeleteHint"),
            self._win,
        )
        if not confirm.exec():
            return
        try:
            self._store.delete(rec.id)
            # If the deleted record was the one whose notes we're editing,
            # clear the editor so it doesn't show ghost data for a row
            # that's no longer in the table.
            if getattr(self, "_selected_rec", None) and self._selected_rec.id == rec.id:
                self._selected_rec = None
                self.notes_edit.blockSignals(True)
                self.notes_edit.setPlainText("")
                self.notes_edit.blockSignals(False)
                self.btn_save_notes.setEnabled(False)
            self.refresh()
        except Exception as exc:
            InfoBar.error("", str(exc), parent=self._win,
                          position=InfoBarPosition.TOP, duration=4000)


# ---------------------------------------------------------------------------
# Usage page
# ---------------------------------------------------------------------------

class UsagePage(ScrollArea):
    """Token usage dashboard with summary cards, charts, and recent calls."""

    def __init__(self, win, usage_store: UsageStore, translator: Translator) -> None:
        super().__init__(win)
        self._win = win
        self._store = usage_store
        self._tr = translator
        self._range_key = "7d"

        self.setObjectName("usagePage")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        self.setWidget(container)
        v = QVBoxLayout(container)
        v.setContentsMargins(28, 24, 28, 24)
        v.setSpacing(14)

        self.lbl_title = TitleLabel(self._t("usage.title"))
        self.lbl_subtitle = CaptionLabel(self._t("usage.subtitle"))
        v.addWidget(self.lbl_title)
        v.addWidget(self.lbl_subtitle)

        # Range bar
        bar = QHBoxLayout()
        self.range_combo = SegmentedWidget()
        for key in ("today", "1d", "7d", "30d", "all"):
            self.range_combo.addItem(routeKey=key, text=self._t(f"usage.range.{key}"))
        self.range_combo.setCurrentItem("7d")
        self.range_combo.currentItemChanged.connect(self._on_range_change)
        bar.addWidget(self.range_combo)
        bar.addStretch(1)
        self.btn_clear = PushButton(self._t("history.action.deleteAll"))
        self.btn_clear.clicked.connect(self._on_clear)
        bar.addWidget(self.btn_clear)
        v.addLayout(bar)

        # Summary cards — store (card, value_label) tuples
        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(12)
        self.card_requests = self._make_card("usage.summary.requests", "0")
        self.card_success = self._make_card("usage.summary.successRate", "0%")
        self.card_input = self._make_card("usage.summary.inputTokens", "0")
        self.card_output = self._make_card("usage.summary.outputTokens", "0")
        self.card_cache = self._make_card("usage.summary.cacheHit", "0%")
        self.card_estimated = self._make_card("usage.summary.estimated", "0")
        for c, _ in (self.card_requests, self.card_success, self.card_input,
                      self.card_output, self.card_cache, self.card_estimated):
            self.cards_row.addWidget(c, 1)
        v.addLayout(self.cards_row)

        # Chart row
        self.chart_label = QLabel()
        self.chart_label.setAlignment(Qt.AlignCenter)
        self.chart_label.setMinimumHeight(280)
        self.chart_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        v.addWidget(self.chart_label)

        # Recent calls table
        self.lbl_recent = StrongBodyLabel(self._t("usage.chart.tokensPerDay"))
        v.addWidget(self.lbl_recent)
        self.table = TableWidget()
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels([
            self._t("usage.table.time"),
            self._t("usage.table.provider"),
            self._t("usage.table.model"),
            self._t("usage.table.tokens"),
            self._t("usage.table.latency"),
            self._t("usage.table.status"),
        ])
        self.table.verticalHeader().hide()
        v.addWidget(self.table, 1)

        self.refresh()

    def _make_card(self, label_key: str, initial_value: str) -> tuple[CardWidget, TitleLabel]:
        """Returns (card, value_label) so callers have a direct reference."""
        card = CardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        lbl_label = CaptionLabel(self._t(label_key))
        lbl_value = TitleLabel(initial_value)
        lbl_value.setObjectName(f"card_value_{label_key}")
        layout.addWidget(lbl_label)
        layout.addWidget(lbl_value)
        return card, lbl_value

    def _t(self, key: str) -> str:
        return self._tr.t(key)

    def set_translator(self, tr: Translator) -> None:
        self._tr = tr
        self.lbl_title.setText(self._t("usage.title"))
        self.lbl_subtitle.setText(self._t("usage.subtitle"))
        for key in ("today", "1d", "7d", "30d", "all"):
            self.range_combo.setItemText(key, self._t(f"usage.range.{key}"))
        self.btn_clear.setText(self._t("history.action.deleteAll"))
        self.lbl_recent.setText(self._t("usage.chart.tokensPerDay"))
        for i, k in enumerate([
            "usage.table.time", "usage.table.provider", "usage.table.model",
            "usage.table.tokens", "usage.table.latency", "usage.table.status",
        ]):
            self.table.setHorizontalHeaderItem(i, QTableWidgetItem(self._t(k)))
        self.refresh()

    # ---- data ----

    def _range_to_ts(self) -> tuple[float | None, float | None]:
        now = time.time()
        if self._range_key == "today":
            # Use localtime to get DST-aware midnight (spring-forward/fall-back
            # days may have != 86400 seconds).
            lt = time.localtime(now)
            today_midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
            return today_midnight, now
        if self._range_key == "1d":
            return now - 86400, now
        if self._range_key == "7d":
            return now - 7 * 86400, now
        if self._range_key == "30d":
            return now - 30 * 86400, now
        return None, None  # all time

    def refresh(self) -> None:
        start, end = self._range_to_ts()
        try:
            summary = self._store.summary(start_ts=start, end_ts=end)
            rows = self._store.list(start_ts=start, end_ts=end, limit=200)
        except Exception as exc:
            summary = None
            rows = []
            InfoBar.error("", str(exc), parent=self._win,
                          position=InfoBarPosition.TOP, duration=4000)
        if summary is not None:
            self._render_cards(summary)
            self._render_chart(summary)
        self._render_table(rows)

    def _render_cards(self, summary) -> None:
        cards_and_labels = (
            (self.card_requests[0], self.card_requests[1], str(summary.total_requests) if summary else "-"),
            (self.card_success[0], self.card_success[1], f"{summary.success_rate * 100:.1f}%" if summary else "-"),
            (self.card_input[0], self.card_input[1], _format_int(summary.total_input_tokens) if summary else "-"),
            (self.card_output[0], self.card_output[1], _format_int(summary.total_output_tokens) if summary else "-"),
            (self.card_cache[0], self.card_cache[1], f"{summary.cache_hit_rate * 100:.1f}%" if summary else "-"),
            (self.card_estimated[0], self.card_estimated[1], str(summary.estimated_rows) if summary else "-"),
        )
        for _card, lbl_value, value in cards_and_labels:
            lbl_value.setText(value)

    def _render_chart(self, summary) -> None:
        plt = _mpl()
        if plt is None or not summary or not summary.by_day:
            self.chart_label.clear()
            self.chart_label.setText("-" if (not summary or not summary.by_day)
                                      else self._t("usage.empty"))
            return
        try:
            days = [time.strftime("%m-%d", time.localtime(d["day"])) for d in summary.by_day]
            toks = [d["tokens"] for d in summary.by_day]
            calls = [d["count"] for d in summary.by_day]
            # 8.0" × 3.0" gives matplotlib enough room for dual-axis labels
            # without tight_layout clipping them on Windows with high-DPI scaling.
            fig, ax1 = plt.subplots(figsize=(8.0, 3.0), dpi=120)
            ax1.bar(days, toks, color="#3B82F6", alpha=0.85, label="tokens", width=0.6)
            ax1.set_ylabel("Tokens", color="#1e293b", fontsize=9)
            ax1.tick_params(axis="y", labelcolor="#1e293b", labelsize=8)
            ax1.tick_params(axis="x", labelcolor="#1e293b", labelsize=8)
            ax1.set_xticks(range(len(days)))
            ax1.set_xticklabels(days, fontsize=8)
            ax2 = ax1.twinx()
            ax2.plot(range(len(days)), calls, color="#10B981", marker="o",
                     linewidth=1.5, markersize=4, label="calls")
            ax2.set_ylabel("Calls", color="#047857", fontsize=9)
            ax2.tick_params(axis="y", labelcolor="#047857", labelsize=8)
            ax2.set_ylim(bottom=0)
            # Add spacing so axis labels aren't flush against the figure edge.
            fig.subplots_adjust(left=0.10, right=0.92, bottom=0.18, top=0.92)
            pix = _fig_to_pixmap(fig)
            if pix is not None:
                # Scale to a fixed display size that fits the card layout.
                # Use devicePixelRatio to render at native resolution on high-DPI
                # displays so the chart is crisp, then display at 800×300 logical px.
                dpr = self.chart_label.devicePixelRatio()
                display_w = 800
                display_h = 300
                scaled = pix.scaled(
                    int(display_w * dpr), int(display_h * dpr),
                    Qt.KeepAspectRatio, Qt.FastTransformation
                )
                scaled.setDevicePixelRatio(dpr)
                self.chart_label.setPixmap(scaled)
            else:
                self.chart_label.setText(self._t("usage.empty"))
        except Exception as exc:
            self.chart_label.setText(str(exc))

    def _render_table(self, rows: list[UsageRecord]) -> None:
        from PySide6.QtWidgets import QTableWidgetItem
        self.table.setRowCount(len(rows))
        for ri, rec in enumerate(rows):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rec.timestamp or 0))
            tokens = rec.input_tokens + rec.output_tokens
            flag = f"  ({self._t('usage.estimated')})" if (
                rec.input_tokens_estimated or rec.output_tokens_estimated) else ""
            status = str(rec.status_code) if rec.status_code else "-"
            cells = [ts, rec.provider_name or "-", rec.model or "-",
                     f"{_format_int(tokens)}{flag}",
                     f"{rec.latency_ms} ms", status]
            for ci, txt in enumerate(cells):
                self.table.setItem(ri, ci, QTableWidgetItem(txt))
        self.table.resizeColumnsToContents()

    # ---- handlers ----

    def _on_range_change(self, key: str) -> None:
        self._range_key = key or "all"
        self.refresh()

    def _on_clear(self) -> None:
        confirm = MessageBox(
            self._t("usage.confirmClear"),
            self._t("usage.confirmClearHint"),
            self._win,
        )
        if not confirm.exec():
            return
        try:
            self._store.clear()
            self.refresh()
            InfoBar.success(
                "", self._t("usage.cleared"), parent=self._win,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.error("", str(exc), parent=self._win,
                          position=InfoBarPosition.TOP, duration=4000)


def _format_int(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
