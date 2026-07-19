"""gui_fluent_history_detail.py — HistoryDetailDialog.

A modal that shows the full content of a HistoryRecord: metadata, the
extracted result rendered with the same tables the Extract page uses,
the source-image thumbnail (if stored), and the raw model response.

Interaction model (mirrors cc-switch's RequestDetailPanel.tsx):
  - The user double-clicks a row in HistoryPage to open this dialog.
  - Result rendering is delegated to js/table.js's `rcaRenderResults`
    running inside QWebEngineView, so the detail view looks identical
    to the live Extract page (column order, agreement pills, raw toggle,
    confidence ring).

QWebEngineView lives in PySide6-Addons (see requirements.txt). When that
package is unavailable (e.g. a slim install) the dialog falls back to a
plain read-only TreeView — better than crashing the History page on
double-click.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from qfluentwidgets import (
    BodyLabel, CaptionLabel, CardWidget, FluentIcon as FIF,
    PrimaryPushButton, PushButton, ScrollArea, StrongBodyLabel,
    TitleLabel,
)

from rca_core.history import HistoryRecord
from rca_core.i18n import Translator

logger = logging.getLogger(__name__)

# Resolve project root (one directory up from this file's directory).
_PROJECT_ROOT = Path(__file__).resolve().parent
_JS_TABLE = _PROJECT_ROOT / "js" / "table.js"
_JS_AGGREGATE = _PROJECT_ROOT / "js" / "aggregate.js"
_JS_I18N = _PROJECT_ROOT / "js" / "i18n.js"


def _has_webengine() -> bool:
    """True iff QtWebEngineWidgets is importable."""
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Minimal CSS — subset of css/style.css that the table rendering actually
# needs (table chrome, pill colors, confidence ring). Inline into the
# WebEngine page so we don't have to ship a base URL or remote-load files.
# ---------------------------------------------------------------------------
_INLINE_CSS = """
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
       color: #1f2937; background: #ffffff; margin: 0; padding: 16px; }
table.data-table { width: 100%; border-collapse: collapse; font-size: 13px;
                    background: #fff; border: 1px solid #e5e7eb;
                    border-radius: 8px; overflow: hidden; }
table.data-table thead th { background: rgba(255,255,255,0.9);
        color: #1f2937; font-weight: 600; font-size: 11px;
        text-transform: uppercase; letter-spacing: 0.05em; text-align: left;
        padding: 10px 14px; border-bottom: 2px solid #e5e7eb; white-space: nowrap; }
table.data-table tbody td, table.data-table tbody th[scope='row'] {
        padding: 8px 14px; border-bottom: 1px solid #f3f4f6; }
table.data-table tbody tr:last-child td { border-bottom: none; }
.cell-empty { color: #9ca3af; }
.cell-species { font-style: italic; }
.cell-num { font-family: ui-monospace, 'Consolas', monospace; }
.row-low-agreement td { background: rgba(239,68,68,0.06); }
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px;
        font-size: 11px; font-weight: 600; }
.pill-good { background: rgba(34,197,94,0.15); color: #15803d; }
.pill-mid  { background: rgba(245,158,11,0.15); color: #b45309; }
.pill-low  { background: rgba(239,68,68,0.15); color: #b91c1c; }
.result-section { margin-top: 18px; }
.result-section-head { display: flex; align-items: center;
        justify-content: space-between; padding: 4px 2px; }
.result-section-head h3 { font-size: 14px; font-weight: 600; margin: 0; }
.result-count { color: #6b7280; font-weight: 400; margin-left: 4px; }
.table-actions { display: flex; gap: 6px; }
/* Toolbar (confidence ring + export button) */
.results-toolbar { display: flex; align-items: center; justify-content: space-between;
        padding: 10px 16px; background: #f9fafb; border: 1px solid #e5e7eb;
        border-radius: 10px; margin-bottom: 16px; }
.rt-left { display: flex; align-items: center; gap: 12px; }
.rt-actions { display: flex; gap: 6px; }
/* Confidence ring: SVG circle must be hollow (fill:none) or it renders black */
.confidence-ring {
    width: 44px; height: 44px;
    display: inline-flex; align-items: center; justify-content: center;
    position: relative; flex-shrink: 0;
}
.confidence-ring svg { transform: rotate(-90deg); width: 100%; height: 100%; }
.confidence-ring .track { stroke: #e7e5e4; fill: none; stroke-width: 4px; }
.confidence-ring .bar { stroke: #2563eb; fill: none; stroke-width: 4px;
        stroke-linecap: round; }
.confidence-ring.high .bar { stroke: #10b981; }
.confidence-ring.mid  .bar { stroke: #b45309; }
.confidence-ring.low  .bar { stroke: #ef4444; }
.confidence-ring .num {
    position: absolute; font-size: 12px; font-weight: 650;
    font-variant-numeric: tabular-nums; color: #1f2937;
}
.confidence-ring .label {
    font-size: 11px; color: #6b7280; text-transform: uppercase;
    letter-spacing: 0.05em; margin-left: 8px;
}
.btn { padding: 4px 10px; border-radius: 6px; border: 1px solid #e5e7eb;
       background: #ffffff; color: #334155; cursor: pointer; font-size: 12px; }
.btn:hover { background: #f1f5f9; color: #0f172a; }
.btn { padding: 4px 10px; border-radius: 6px; border: 1px solid #e5e7eb;
       background: #ffffff; color: #334155; cursor: pointer; font-size: 12px; }
.btn:hover { background: #f1f5f9; color: #0f172a; }
details { margin-top: 20px; }
details pre { font-family: ui-monospace, 'Consolas', monospace; font-size: 11px;
        white-space: pre-wrap; word-break: break-word; background: #f8fafc;
        padding: 10px; border-radius: 8px; max-height: 320px; overflow: auto;
        border: 1px solid #e5e7eb; }
"""


def _read_js(path: Path) -> str:
    """Read a JS file and strip the `'use strict';` directive (we wrap
    everything in an IIFE so strict-mode inside individual files would
    be fine, but stripping it removes any cross-file ordering foot-guns)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Could not read %s: %s — detail dialog will be empty", path, exc)
        return ""
    return text.replace("'use strict';", "").replace('"use strict";', "")


def _build_webengine_html(rec: HistoryRecord, tr: Translator) -> str:
    """Build the self-contained HTML page that loads table.js / aggregate.js /
    i18n.js and calls rcaRenderResults on the record's result.

    All three JS files are inlined so the WebEngine page needs no external
    network access and works offline (matches the project's "100% local"
    philosophy for the desktop app)."""
    i18n_js = _read_js(_JS_I18N)
    aggregate_js = _read_js(_JS_AGGREGATE)
    table_js = _read_js(_JS_TABLE)

    # Serialize i18n + the record's data into JSON once, escape the closing
    # </script> tag, and embed as JS literals. The page bootstraps by
    # calling rcaRenderResults and writing the output to #result-host.
    #
    # XSS hardening (H3 fix): json.dumps does NOT escape '<' or '/', so a
    # literal '</script>' inside any of these payloads would close the <script>
    # block in the HTML parser's eyes and let attacker-controlled markup/JS run
    # inside QWebEngineView. Replacing '</' with '\\/ '</\' breaks that sequence
    # inside the JSON string (which is still valid JSON — \/ is an escaped slash)
    # without changing the parsed value. This is the standard inline-JSON defense
    # recommended by Google/Mozilla.
    _defuse = lambda s: s.replace("</", "<\\/")
    i18n_payload = _defuse(json.dumps(getattr(tr, "translations", {}) or {}, ensure_ascii=False))
    lang_code = getattr(tr, "lang", "zh")
    result_payload = _defuse(json.dumps(rec.result or {}, ensure_ascii=False, default=str))
    raw_payload = _defuse(json.dumps(rec.raw or "", ensure_ascii=False))

    return f"""<!doctype html>
<html lang="{lang_code}">
<head>
<meta charset="utf-8">
<style>{_INLINE_CSS}</style>
</head>
<body>
<div id="result-host">…</div>
<script>
// --- inlined i18n.js ---
{i18n_js}
// --- inlined aggregate.js ---
{aggregate_js}
// --- inlined table.js ---
{table_js}
// --- bootstrap ---
// js/i18n.js declares `RCA_I18N` (const) and `RCA_LANG` (let) at module
// top level — both are visible to the rest of the script. The `t()`
// function defined at module level is also in scope here because we are
// inside the same `<script>` block. So we do NOT redeclare RCA_I18N
// (which would throw a SyntaxError in WebEngine and abort the render).
// Just override RCA_LANG for the current user's language, then call
// rcaRenderResults.
RCA_LANG = {json.dumps(lang_code)};
// table.js's `rcaTableConfigs` is the only function rcaRenderResults
// needs from table.js; everything else (rcaEsc, rcaEscAttr) is hoisted.
const __data = {result_payload};
const __raw = {raw_payload};
try {{
  document.getElementById('result-host').innerHTML = rcaRenderResults(__data, __raw);
}} catch (err) {{
  document.getElementById('result-host').innerHTML =
    '<pre style="color:#b91c1c;">Render error: ' + String(err) + '</pre>';
}}
</script>
</body>
</html>"""


def _section_card(title: str, body: QWidget) -> CardWidget:
    """Wrap a body widget inside a labeled card."""
    card = CardWidget()
    v = QVBoxLayout(card)
    v.setContentsMargins(16, 14, 16, 14)
    v.setSpacing(8)
    title_lbl = StrongBodyLabel(title)
    v.addWidget(title_lbl)
    v.addWidget(body)
    return card


def _kv_grid(rows: list[tuple[str, str]]) -> QWidget:
    """Render a 2-column key/value grid as a QWidget."""
    w = QWidget()
    grid = QVBoxLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(6)
    for k, v in rows:
        row = QHBoxLayout()
        k_lbl = CaptionLabel(k)
        k_lbl.setMinimumWidth(96)
        v_lbl = BodyLabel(v or "-")
        v_lbl.setWordWrap(True)
        v_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(k_lbl)
        row.addWidget(v_lbl, 1)
        wrap = QWidget()
        wrap_lay = QHBoxLayout(wrap)
        wrap_lay.setContentsMargins(0, 0, 0, 0)
        wrap_lay.addLayout(row)
        grid.addWidget(wrap)
    grid.addStretch(1)
    return w


def _tree_widget_for_result(result: dict[str, Any]) -> QTreeWidget:
    """Fallback renderer used when QWebEngineView is unavailable.

    Renders each top-level list in the result as a flat list of records.
    Not as polished as the WebEngine page, but the user can still see
    every field and copy anything out."""
    tree = QTreeWidget()
    tree.setColumnCount(2)
    tree.setHeaderLabels(["Field", "Value"])
    tree.setRootIsDecorated(True)
    tree.setUniformRowHeights(True)
    for key, val in (result or {}).items():
        if isinstance(val, list):
            root = QTreeWidgetItem([str(key), f"({len(val)})"])
            tree.addTopLevelItem(root)
            for i, item in enumerate(val[:500]):  # safety cap
                if isinstance(item, dict):
                    sub = QTreeWidgetItem([f"  #{i}", ", ".join(
                        f"{k}={v}" for k, v in item.items()
                        if not isinstance(v, (dict, list))
                    )])
                    root.addChild(sub)
                else:
                    root.addChild(QTreeWidgetItem([f"  #{i}", str(item)]))
        elif isinstance(val, dict):
            root = QTreeWidgetItem([str(key), "{…}"])
            tree.addTopLevelItem(root)
            for k, v in val.items():
                root.addChild(QTreeWidgetItem([f"  {k}", str(v)]))
        else:
            tree.addTopLevelItem(QTreeWidgetItem([str(key), str(val)]))
    tree.expandAll()
    for i in range(tree.columnCount()):
        tree.resizeColumnToContents(i)
    return tree


def _thumbnail_widget(thumb_bytes: bytes) -> QWidget:
    """Render the stored thumbnail bytes, or return an empty QLabel if
    the record has no thumbnail."""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    if not thumb_bytes:
        lbl = CaptionLabel("(no preview)")
        v.addWidget(lbl)
        v.addStretch(1)
        return w
    pix = QPixmap()
    pix.loadFromData(thumb_bytes, "PNG")
    if pix.isNull():
        # Try JPEG fallback (HistoryStore stores JPEG or PNG depending on source).
        pix.loadFromData(thumb_bytes, "JPG")
    if pix.isNull():
        lbl = CaptionLabel("(could not decode thumbnail)")
        v.addWidget(lbl)
        v.addStretch(1)
        return w
    img_lbl = QLabel()
    img_lbl.setPixmap(
        pix.scaledToWidth(420, Qt.SmoothTransformation)
    )
    img_lbl.setAlignment(Qt.AlignCenter)
    v.addWidget(img_lbl)
    v.addStretch(1)
    return w


class HistoryDetailDialog(QDialog):
    """Modal that shows the full content of one HistoryRecord.

    Triggered by a double-click on a row in HistoryPage. Closing the
    dialog does NOT touch the underlying record or selection state."""

    def __init__(self, parent: QWidget, rec: HistoryRecord, tr: Translator) -> None:
        super().__init__(parent)
        self._rec = rec
        self._tr = tr
        self.setWindowTitle(self._t("history.detail.title"))
        self.resize(900, 720)
        # Ensure the dialog fills its area with a solid background so no
        # unpainted black regions appear during resize or on Windows compositor
        # artefacts when the modal closes.
        self.setStyleSheet("QDialog { background: #ffffff; }")
        # Let the dialog be re-sized smaller (the WebEngine area will scroll).
        self.setSizeGripEnabled(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        title = TitleLabel(self._t("history.detail.title"))
        sub = CaptionLabel(f"#{rec.id} · {rec.provider_name or '-'} · {rec.model or '-'}")
        sub.setStyleSheet("color:#6b7280;")
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_col.addWidget(title)
        title_col.addWidget(sub)
        header.addLayout(title_col, 1)
        outer.addLayout(header)

        # Scrollable body so the dialog stays usable on small screens.
        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(12)

        # --- Section 1: Basic information ---
        import time as _time
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(rec.timestamp or 0))
        mode_label = {
            "range_chart": "Range chart",
            "columnar_section": "Columnar section",
            "abundance_diagram": "Abundance / pollen",
        }.get(rec.mode, rec.mode or "-")
        conf = f"{(rec.confidence or 0) * 100:.0f}%" if rec.confidence else "-"
        rows = [
            (self._t("history.detail.time"), ts),
            (self._t("history.detail.provider"), rec.provider_name or "-"),
            (self._t("history.detail.model"), rec.model or "-"),
            (self._t("history.detail.mode"), mode_label),
            (self._t("history.detail.runs"), str(rec.runs or 1)),
            (self._t("history.detail.confidence"), conf),
            (self._t("history.detail.status"),
             str(rec.status_code) if rec.status_code else "-"),
            (self._t("history.detail.duration"),
             f"{rec.duration_ms} ms" if rec.duration_ms else "-"),
            (self._t("history.detail.sourceFile"), rec.source_file or "-"),
        ]
        basic = _section_card(self._t("history.detail.basicInfo"),
                              _kv_grid(rows))
        body_lay.addWidget(basic)

        # --- Section 2: Source image thumbnail ---
        if rec.image_thumbnail:
            img_card = _section_card(
                self._t("history.detail.image"),
                _thumbnail_widget(rec.image_thumbnail),
            )
            body_lay.addWidget(img_card)

        # --- Section 3: Extracted result (WebEngine, with fallback) ---
        result_holder = QWidget()
        result_lay = QVBoxLayout(result_holder)
        result_lay.setContentsMargins(0, 0, 0, 0)
        result_lay.setSpacing(0)
        if _has_webengine():
            try:
                from PySide6.QtWebEngineWidgets import QWebEngineView
                view = QWebEngineView()
                view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
                view.setMinimumHeight(360)
                html = _build_webengine_html(rec, tr)
                view.setHtml(html, QUrl.fromLocalFile(str(_PROJECT_ROOT) + "/"))
                result_lay.addWidget(view)
            except Exception as exc:
                logger.exception("Failed to build WebEngine view: %s", exc)
                result_lay.addWidget(_tree_widget_for_result(rec.result))
        else:
            # PySide6-Addons not installed — show the tree fallback.
            note = CaptionLabel(
                "(Install PySide6-Addons to render the result as tables; "
                "showing raw tree view instead.)"
            )
            note.setStyleSheet("color:#b45309;")
            result_lay.addWidget(note)
            result_lay.addWidget(_tree_widget_for_result(rec.result), 1)

        result_card = _section_card(self._t("history.detail.result"), result_holder)
        body_lay.addWidget(result_card, 1)

        # --- Section 4: Raw model response (collapsible) ---
        if rec.raw:
            raw_text = QLabel(rec.raw)
            raw_text.setWordWrap(True)
            raw_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
            raw_text.setStyleSheet(
                "font-family: ui-monospace, 'Consolas', monospace; font-size: 11px;"
                " white-space: pre-wrap; word-break: break-word; background: #f8fafc;"
                " padding: 10px; border-radius: 8px; border: 1px solid #e5e7eb;"
                " max-height: 320px;"
            )
            raw_card = _section_card(self._t("history.detail.raw"), raw_text)
            body_lay.addWidget(raw_card)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # Footer with Close button (primary action).
        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_close = PrimaryPushButton(self._t("history.detail.close"))
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        outer.addLayout(footer)

    def _t(self, key: str) -> str:
        try:
            return self._tr.t(key)
        except Exception:
            return key