"""Range Chart Analyzer - Fluent desktop GUI (PySide6 + qfluentwidgets).

A modern, QQ/PyCharm-grade desktop frontend over the shared rca_core.
Opt-in alternative to the stdlib Tkinter GUI (gui.py); launch with
`python main.py --ui fluent`. If PySide6 / qfluentwidgets are missing,
main.py falls back to the Tkinter GUI.

Threading: extraction + connection-test run on QThread workers; results
marshal back to the UI thread via Qt signals (touching widgets off the UI
thread crashes Qt). All LLM / merge / i18n logic is reused from rca_core.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QTableWidgetItem, QHeaderView, QFrame, QSizePolicy,
)

from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon as FIF,
    LineEdit, PasswordLineEdit, PushButton, PrimaryPushButton, ComboBox,
    SpinBox, SwitchButton, TableWidget, BodyLabel, TitleLabel, SubtitleLabel,
    StrongBodyLabel, CaptionLabel, CardWidget, TextEdit, InfoBar, InfoBarPosition,
    MessageBoxBase, ScrollArea, IndeterminateProgressRing,
    SearchLineEdit, setTheme, Theme, setThemeColor, ToolButton, Pivot,
)

from rca_core import (
    Translator, build_table_export, get_configs_for_result, load_image_b64,
    to_csv, to_tsv, extract, merge_results, ProviderStore,
)
from rca_core.aggregate import COLUMNAR_SECTION_SCHEMA, RANGE_CHART_SCHEMA
from rca_core.extractor import (
    DEFAULT_ENDPOINT, DEFAULT_MAX_TOKENS, DEFAULT_MAX_EDGE, DEFAULT_MODEL,
    ExtractResult, clamp_max_tokens,
)
from rca_core.llm import ApiFormat, LlmProvider, PROVIDER_PRESETS, test_llm_connection

try:
    from PIL import Image, ImageGrab  # type: ignore
    HAS_PIL = True
except Exception:
    HAS_PIL = False

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer.json")


def load_config() -> dict:
    try:
        import json
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        import json
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Worker threads (extraction + connection test run off the UI thread)
# ---------------------------------------------------------------------------
class ExtractWorker(QThread):
    """Runs extract() N times concurrently, merges, emits the result.

    NEVER touches widgets — only emits signals the UI thread listens to.
    """
    finished_ok = Signal(object)   # ExtractResult
    progress = Signal(str)         # status text

    def __init__(self, params, mode, runs):
        super().__init__()
        self._params = params
        self._mode = mode
        self._runs = runs

    def run(self):
        import concurrent.futures
        params, mode, runs = self._params, self._mode, self._runs
        if runs <= 1:
            self.progress.emit("analyzing")
            self.finished_ok.emit(extract(mode=mode, **params))
            return
        ok_datas, last_fail, partial_fails, any_trunc, raws = [], None, 0, False, []
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=runs) as ex:
            futures = [ex.submit(extract, mode=mode, **params) for _ in range(runs)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    r = fut.result()
                except Exception as exc:
                    r = ExtractResult(ok=False, error_key="err.http", raw=str(exc))
                done += 1
                self.progress.emit(f"analyzing:{done}/{runs}")
                if r.ok and r.data is not None:
                    ok_datas.append(r.data)
                    any_trunc = any_trunc or bool(r.truncated)
                    if r.raw:
                        raws.append(r.raw)
                else:
                    last_fail = r
                    partial_fails += 1
        if not ok_datas:
            self.finished_ok.emit(last_fail or ExtractResult(ok=False, error_key="err.empty"))
            return
        schema = COLUMNAR_SECTION_SCHEMA if mode == "columnar_section" else RANGE_CHART_SCHEMA
        merged = merge_results(ok_datas, total_runs=runs, schema=schema)
        self.finished_ok.emit(ExtractResult(
            ok=True, data=merged, raw="\n---RUN---\n".join(raws)[:8000],
            truncated=any_trunc or bool(partial_fails), partial_failures=partial_fails,
        ))


class ConnTestWorker(QThread):
    """Runs test_llm_connection() off the UI thread."""
    done = Signal(object)  # ConnectionResult

    def __init__(self, provider):
        super().__init__()
        self._provider = provider

    def run(self):
        self.done.emit(test_llm_connection(self._provider, timeout_sec=8))


# ---------------------------------------------------------------------------
# Provider preset wizard (modal dialog)
# ---------------------------------------------------------------------------
class ProviderWizard(MessageBoxBase):
    """Two-step provider add: pick a preset (searchable grid) or fill a
    custom form. Returns the created LlmProvider via .created_provider.
    """
    def __init__(self, parent, translate):
        super().__init__(parent)
        self._t = translate
        self.created_provider = None
        self.titleLabel = SubtitleLabel(self._t("wizard.choosePreset"), self)
        self.viewLayout.addWidget(self.titleLabel)

        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText(self._t("settings.searchHint"))
        self.search.textChanged.connect(self._render_presets)
        self.viewLayout.addWidget(self.search)

        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(340)
        self.scroll.setStyleSheet("QScrollArea{border:none;background:transparent}")
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.scroll.setWidget(self.grid_host)
        self.viewLayout.addWidget(self.scroll)

        # Custom form (hidden until "custom" chosen).
        self.form = QWidget()
        fl = QGridLayout(self.form)
        fl.setContentsMargins(0, 8, 0, 0)
        fl.addWidget(BodyLabel(self._t("wizard.fieldName")), 0, 0)
        self.ipt_name = LineEdit(); fl.addWidget(self.ipt_name, 0, 1)
        fl.addWidget(BodyLabel(self._t("wizard.fieldFormat")), 1, 0)
        self.cmb_fmt = ComboBox(); self.cmb_fmt.addItems([f.value for f in ApiFormat])
        fl.addWidget(self.cmb_fmt, 1, 1)
        fl.addWidget(BodyLabel(self._t("wizard.fieldEndpoint")), 2, 0)
        self.ipt_endpoint = LineEdit(); fl.addWidget(self.ipt_endpoint, 2, 1)
        fl.addWidget(BodyLabel(self._t("settings.apiKey")), 3, 0)
        self.ipt_key = PasswordLineEdit(); fl.addWidget(self.ipt_key, 3, 1)
        fl.addWidget(BodyLabel(self._t("settings.model")), 4, 0)
        self.ipt_model = LineEdit(); fl.addWidget(self.ipt_model, 4, 1)
        self.form.setVisible(False)
        self.viewLayout.addWidget(self.form)

        self.yesButton.setText(self._t("wizard.add"))
        self.cancelButton.setText(self._t("wizard.cancel"))
        self.widget.setMinimumWidth(620)
        self._render_presets()

    def _render_presets(self):
        # Clear grid
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        q = self.search.text().strip().lower()
        col_count = 3
        r = c = 0
        for preset in PROVIDER_PRESETS:
            if q and q not in preset.name.lower() and q not in preset.category.lower():
                continue
            btn = PushButton(preset.name)
            btn.setFixedHeight(34)
            btn.clicked.connect(lambda _=False, p=preset: self._pick(p))
            self.grid.addWidget(btn, r, c)
            c += 1
            if c >= col_count:
                c = 0
                r += 1
        # "custom" option
        custom = PushButton(self._t("wizard.customProvider"))
        custom.setFixedHeight(34)
        custom.clicked.connect(self._show_custom)
        self.grid.addWidget(custom, r, c)

    def _pick(self, preset):
        self.ipt_name.setText(preset.name)
        self.cmb_fmt.setCurrentText(preset.api_format.value)
        self.ipt_endpoint.setText(preset.endpoint)
        self.ipt_model.setText(preset.model)
        self._show_custom()

    def _show_custom(self):
        self.scroll.setVisible(False)
        self.search.setVisible(False)
        self.form.setVisible(True)
        self.titleLabel.setText(self._t("wizard.configure"))

    def validate(self):
        name = self.ipt_name.text().strip()
        endpoint = self.ipt_endpoint.text().strip().rstrip("/")
        if not name or not endpoint:
            return False
        try:
            fmt = ApiFormat(self.cmb_fmt.currentText())
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        self.created_provider = LlmProvider(
            name=name, api_format=fmt, endpoint=endpoint,
            api_key=self.ipt_key.text().strip(), model=self.ipt_model.text().strip(),
        )
        return True


# ---------------------------------------------------------------------------
# Extract page — image input, caption, run button, results
# ---------------------------------------------------------------------------
class ExtractPage(ScrollArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._t = win._t
        self.image_path = None
        self.image_b64 = None
        self.media_type = None
        self._img_dims = (0, 0, False)
        self.result = None
        self.raw_text = ""
        self._last_paste_tmp = None
        self._worker = None

        self.setObjectName("extractPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        root = QWidget()
        self.setWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(28, 20, 28, 28)
        lay.setSpacing(16)

        self.lbl_title = TitleLabel(self._t("upload.title") if self._t("upload.title") != "upload.title" else "Extract")
        lay.addWidget(self.lbl_title)

        # Image card
        img_card = CardWidget()
        ic = QVBoxLayout(img_card)
        ic.setContentsMargins(20, 18, 20, 18)
        ic.setSpacing(12)
        btn_row = QHBoxLayout()
        self.btn_choose = PushButton(FIF.PHOTO, self._t("image.choose"))
        self.btn_choose.clicked.connect(self._choose_image)
        self.btn_paste = PushButton(FIF.PASTE, self._t("image.paste"))
        self.btn_paste.clicked.connect(self._paste_image)
        btn_row.addWidget(self.btn_choose)
        btn_row.addWidget(self.btn_paste)
        btn_row.addStretch(1)
        ic.addLayout(btn_row)
        self.lbl_imginfo = CaptionLabel(self._t("image.none"))
        ic.addWidget(self.lbl_imginfo)
        self.preview = BodyLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(180)
        self.preview.setStyleSheet("border:1px solid rgba(0,0,0,0.08);border-radius:8px;")
        ic.addWidget(self.preview)
        lay.addWidget(img_card)

        # Caption card
        cap_card = CardWidget()
        cc = QVBoxLayout(cap_card)
        cc.setContentsMargins(20, 18, 20, 18)
        cc.setSpacing(8)
        self.lbl_caption = StrongBodyLabel(self._t("caption.label"))
        cc.addWidget(self.lbl_caption)
        self.txt_caption = TextEdit()
        self.txt_caption.setFixedHeight(80)
        cc.addWidget(self.txt_caption)
        lay.addWidget(cap_card)

        # Run row
        run_row = QHBoxLayout()
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(24, 24)
        self.spinner.setVisible(False)
        self.lbl_status = CaptionLabel(self._t("status.ready"))
        self.btn_extract = PrimaryPushButton(FIF.PLAY, self._t("action.extract"))
        self.btn_extract.clicked.connect(self._on_extract)
        run_row.addWidget(self.spinner)
        run_row.addWidget(self.lbl_status)
        run_row.addStretch(1)
        self.btn_export = PushButton(FIF.SAVE, self._t("action.exportJson"))
        self.btn_export.clicked.connect(self._export_json)
        run_row.addWidget(self.btn_export)
        run_row.addWidget(self.btn_extract)
        lay.addLayout(run_row)

        # Confidence + results
        self.lbl_conf = StrongBodyLabel("")
        lay.addWidget(self.lbl_conf)
        self.pivot = Pivot()
        lay.addWidget(self.pivot)
        self.stack = QFrame()
        self.stack_lay = QVBoxLayout(self.stack)
        self.stack_lay.setContentsMargins(0, 0, 0, 0)
        self.tables = {}
        lay.addWidget(self.stack, 1)

    # ---- image ----
    def _cleanup_paste_tmp(self):
        prev = self._last_paste_tmp
        if prev and os.path.isfile(prev):
            try:
                os.unlink(prev)
            except OSError:
                pass
        self._last_paste_tmp = None

    def _load_image(self, path):
        try:
            b64, mime, w, h, resized = load_image_b64(path, self.win.max_edge())
        except Exception:
            InfoBar.error("", self._t("err.imageRead"), parent=self.win,
                          position=InfoBarPosition.TOP)
            return
        self.image_path = path
        self.image_b64 = b64
        self.media_type = mime
        self._img_dims = (w, h, resized)
        name = os.path.basename(path)
        dims = f"{self._t('image.dims')}: {w} x {h}" if w else ""
        tail = " " + self._t("image.resized") if resized else ""
        self.lbl_imginfo.setText(f"{name}\n{dims}{tail}")
        pix = QPixmap(path)
        if not pix.isNull():
            self.preview.setPixmap(pix.scaled(
                QSize(max(80, self.preview.width() - 8), 172),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _choose_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self._t("dialog.chooseImage"), "",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*.*)")
        if not path:
            return
        self._cleanup_paste_tmp()
        self._load_image(path)

    def _paste_image(self):
        if not HAS_PIL:
            InfoBar.warning("", self._t("image.noClipboard"), parent=self.win,
                            position=InfoBarPosition.TOP)
            return
        try:
            img = ImageGrab.grabclipboard()
        except Exception:
            img = None
        if img is None or isinstance(img, list):
            InfoBar.warning("", self._t("image.noClipboard"), parent=self.win,
                            position=InfoBarPosition.TOP)
            return
        fd, tmp = tempfile.mkstemp(prefix="rca_paste_", suffix=".png")
        os.close(fd)
        try:
            img.save(tmp)
        except Exception:
            InfoBar.error("", self._t("err.imageRead"), parent=self.win,
                          position=InfoBarPosition.TOP)
            return
        self._cleanup_paste_tmp()
        self._last_paste_tmp = tmp
        self._load_image(tmp)
        InfoBar.success("", self._t("image.pasted"), parent=self.win,
                        position=InfoBarPosition.TOP)

    # ---- extraction ----
    def _set_busy(self, busy):
        self.btn_extract.setEnabled(not busy)
        self.spinner.setVisible(busy)
        if busy:
            self.lbl_status.setText(self._t("status.loading"))

    def _on_extract(self):
        if not self.image_b64:
            InfoBar.warning("", self._t("err.noImage"), parent=self.win,
                            position=InfoBarPosition.TOP)
            return
        provider = self.win.current_provider()
        legacy_key = self.win.api_key()
        if provider is not None and not (provider.api_key or "").strip():
            provider = None
        if provider is None and not legacy_key:
            InfoBar.warning("", self._t("err.noKey"), parent=self.win,
                            position=InfoBarPosition.TOP)
            return
        params = dict(
            api_key=legacy_key, image_b64=self.image_b64,
            media_type=self.media_type or "image/png",
            caption=self.txt_caption.toPlainText().strip(),
            chart_lang=self.win.chart_lang(),
            base_url=self.win.endpoint(), model=self.win.model(),
            max_tokens=self.win.max_tokens(), provider=provider,
        )
        runs = self.win.runs()
        mode = self.win.chart_type()
        if mode == "auto":
            cap = (params["caption"] + " " + (self.image_path or "")).lower()
            mode = "columnar_section" if any(
                k in cap for k in ("column", "section", "柱状", "柱状图", "col")
            ) else "range_chart"
        self._set_busy(True)
        self._worker = ExtractWorker(params, mode, runs)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_result)
        self._worker.start()

    def _on_progress(self, text):
        if text.startswith("analyzing:"):
            self.lbl_status.setText(self._t("status.loading") + " (" + text.split(":", 1)[1] + ")")
        else:
            self.lbl_status.setText(self._t("status.loading"))

    def _on_result(self, result):
        self._set_busy(False)
        if not result.ok:
            msg = self._t(result.error_key or "err.http")
            if result.status:
                msg += f" (HTTP {result.status})"
            self.lbl_status.setText(msg)
            InfoBar.error("", msg, parent=self.win, position=InfoBarPosition.TOP,
                          duration=6000)
            return
        self.result = result.data
        self.raw_text = result.raw
        self._render_result()
        status = self._t("status.done")
        if getattr(result, "partial_failures", 0):
            pf = result.partial_failures
            total = int((self.result or {}).get("runs", pf + 1) or (pf + 1))
            status += f" - {pf}/{total} failed"
        elif result.truncated:
            status += " - " + self._t("err.truncated")
        self.lbl_status.setText(status)

    def _render_result(self):
        multi = self.result and int(self.result.get("runs", 1) or 1) > 1
        n_runs = int((self.result or {}).get("runs", 1) or 1)
        configs = get_configs_for_result(self.result)
        for w in list(self.tables.values()):
            w.setParent(None)
            w.deleteLater()
        self.tables = {}
        self.pivot.clear()
        while self.stack_lay.count():
            it = self.stack_lay.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        for idx, cfg in enumerate(configs):
            table = TableWidget()
            table.setBorderVisible(True)
            table.setBorderRadius(8)
            table.setWordWrap(False)
            cols = ["#"] + [self._t(c) for c in cfg["cols"]]
            table.setColumnCount(len(cols))
            table.setHorizontalHeaderLabels(cols)
            items = (self.result or {}).get(cfg["id"], []) or []
            table.setRowCount(len(items))
            for ri, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                cells = cfg["row"](item)
                values = [str(ri + 1)] + ["" if c is None else str(c) for c in cells]
                low = False
                if multi and cfg["id"] == "species_ranges":
                    ac = int(item.get("agreement_count", 0) or 0)
                    if ac <= n_runs / 2:
                        low = True
                for ci, val in enumerate(values):
                    cell = QTableWidgetItem(val)
                    if low:
                        cell.setBackground(Qt.yellow)
                    table.setItem(ri, ci, cell)
            table.resizeColumnsToContents()
            table.horizontalHeader().setStretchLastSection(True)
            self.tables[cfg["id"]] = table
            self.pivot.addItem(routeKey=cfg["id"], text=self._t(cfg["title_key"]),
                               onClick=lambda k=cfg["id"]: self._show_table(k))
            table.setVisible(idx == 0)
            self.stack_lay.addWidget(table)
        if configs:
            self.pivot.setCurrentItem(configs[0]["id"])
        self._refresh_conf()

    def _show_table(self, key):
        for k, w in self.tables.items():
            w.setVisible(k == key)

    def _refresh_conf(self):
        if not self.result:
            self.lbl_conf.setText("")
            return
        pct = round((self.result.get("confidence", 0) or 0) * 100)
        self.lbl_conf.setText(f"{self._t('status.confidence')}: {pct}%")

    def _export_json(self):
        if not self.result:
            return
        import json
        path, _ = QFileDialog.getSaveFileName(
            self, self._t("dialog.saveJson"), "range_chart_result.json",
            "JSON (*.json)")
        if not path:
            return
        payload = {
            "source_file": os.path.basename(self.image_path) if self.image_path else None,
            "result": self.result,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        InfoBar.success("", self._t("status.saved"), parent=self.win,
                        position=InfoBarPosition.TOP)

    def retranslate(self):
        self.lbl_title.setText("Extract" if self._t("upload.title") == "upload.title" else self._t("upload.title"))
        self.btn_choose.setText(self._t("image.choose"))
        self.btn_paste.setText(self._t("image.paste"))
        self.lbl_caption.setText(self._t("caption.label"))
        self.btn_extract.setText(self._t("action.extract"))
        self.btn_export.setText(self._t("action.exportJson"))
        if not self.result:
            self.lbl_status.setText(self._t("status.ready"))


# ---------------------------------------------------------------------------
# Providers page
# ---------------------------------------------------------------------------
class ProvidersPage(ScrollArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._t = win._t
        self._test_worker = None
        self.setObjectName("providersPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        root = QWidget()
        self.setWidget(root)
        self.lay = QVBoxLayout(root)
        self.lay.setContentsMargins(28, 20, 28, 28)
        self.lay.setSpacing(16)

        head = QHBoxLayout()
        self.lbl_title = TitleLabel(self._t("settings.llmProvider"))
        head.addWidget(self.lbl_title)
        head.addStretch(1)
        self.btn_add = PrimaryPushButton(FIF.ADD, self._t("settings.addProvider"))
        self.btn_add.clicked.connect(self._add_provider)
        head.addWidget(self.btn_add)
        self.lay.addLayout(head)

        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(8)
        self.lay.addWidget(self.list_host)

        self.lbl_test = CaptionLabel("")
        self.lay.addWidget(self.lbl_test)
        self.lay.addStretch(1)
        self._refresh()

    def _refresh(self):
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        try:
            store = ProviderStore().load()
        except Exception:
            store = None
        if not store or not store.providers:
            self.list_lay.addWidget(BodyLabel(self._t("settings.noProviders")))
            return
        for prov in store.providers:
            self.list_lay.addWidget(self._provider_row(prov, store))

    def _provider_row(self, prov, store):
        card = CardWidget()
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        dot = "●" if prov.is_current else "○"
        has_key = "✓" if prov.api_key else "…"
        label = BodyLabel(f"{dot} {prov.name}  ·  {prov.model or '-'}  ·  {prov.api_format.value}  ·  {has_key}")
        row.addWidget(label)
        row.addStretch(1)
        btn_test = PushButton(self._t("settings.testConnection"))
        btn_test.clicked.connect(lambda _=False, p=prov: self._test(p))
        row.addWidget(btn_test)
        if not prov.is_current:
            btn_set = PushButton(self._t("wizard.setActive"))
            btn_set.clicked.connect(lambda _=False, pid=prov.id: self._set_current(pid))
            row.addWidget(btn_set)
        btn_del = ToolButton(FIF.DELETE)
        btn_del.clicked.connect(lambda _=False, pid=prov.id: self._delete(pid))
        row.addWidget(btn_del)
        return card

    def _add_provider(self):
        wiz = ProviderWizard(self.win, self._t)
        if wiz.exec() and wiz.created_provider:
            store = ProviderStore().load()
            store.add(wiz.created_provider)
            store.set_current(wiz.created_provider.id)
            self._refresh()

    def _set_current(self, pid):
        store = ProviderStore().load()
        store.set_current(pid)
        self._refresh()

    def _delete(self, pid):
        store = ProviderStore().load()
        store.remove(pid)
        self._refresh()

    def _test(self, provider):
        self.lbl_test.setText("⏳ " + self._t("settings.testing"))
        self._test_worker = ConnTestWorker(provider)
        self._test_worker.done.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, res):
        if res.ok:
            txt = "✓  " + str(res.latency_ms) + " ms"
            if res.models_sample:
                txt += "  ·  " + ", ".join(res.models_sample[:3])
        else:
            txt = "✗  " + self._t(res.error_key or "err.http")
            if res.status:
                txt += f"  (HTTP {res.status})"
        self.lbl_test.setText(txt)

    def retranslate(self):
        self.lbl_title.setText(self._t("settings.llmProvider"))
        self.btn_add.setText(self._t("settings.addProvider"))
        self._refresh()


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
class SettingsPage(ScrollArea):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._t = win._t
        cfg = win.cfg
        self.setObjectName("settingsPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        root = QWidget()
        self.setWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(28, 20, 28, 28)
        lay.setSpacing(16)

        self.lbl_title = TitleLabel(self._t("menu.settings"))
        lay.addWidget(self.lbl_title)

        card = CardWidget()
        g = QGridLayout(card)
        g.setContentsMargins(20, 18, 20, 18)
        g.setVerticalSpacing(14)
        g.setHorizontalSpacing(12)
        r = 0
        self.lbl_key = StrongBodyLabel(self._t("settings.apiKey"))
        g.addWidget(self.lbl_key, r, 0)
        self.ipt_key = PasswordLineEdit()
        self.ipt_key.setText(cfg.get("api_key", ""))
        g.addWidget(self.ipt_key, r, 1); r += 1
        self.lbl_endpoint = StrongBodyLabel(self._t("settings.endpoint"))
        g.addWidget(self.lbl_endpoint, r, 0)
        self.ipt_endpoint = LineEdit()
        self.ipt_endpoint.setText(cfg.get("endpoint", DEFAULT_ENDPOINT))
        g.addWidget(self.ipt_endpoint, r, 1); r += 1
        self.lbl_model = StrongBodyLabel(self._t("settings.model"))
        g.addWidget(self.lbl_model, r, 0)
        self.ipt_model = LineEdit()
        self.ipt_model.setText(cfg.get("model", DEFAULT_MODEL))
        g.addWidget(self.ipt_model, r, 1); r += 1
        self.lbl_maxtok = StrongBodyLabel(self._t("settings.maxTokens"))
        g.addWidget(self.lbl_maxtok, r, 0)
        self.spin_maxtok = SpinBox()
        self.spin_maxtok.setRange(1, 32000)
        self.spin_maxtok.setValue(int(cfg.get("max_tokens", DEFAULT_MAX_TOKENS)))
        g.addWidget(self.spin_maxtok, r, 1); r += 1
        self.lbl_maxedge = StrongBodyLabel(self._t("settings.maxEdge"))
        g.addWidget(self.lbl_maxedge, r, 0)
        self.spin_maxedge = SpinBox()
        self.spin_maxedge.setRange(0, 8000)
        self.spin_maxedge.setValue(int(cfg.get("max_edge", DEFAULT_MAX_EDGE)))
        g.addWidget(self.spin_maxedge, r, 1); r += 1
        self.lbl_runs = StrongBodyLabel(self._t("settings.runs"))
        g.addWidget(self.lbl_runs, r, 0)
        self.spin_runs = SpinBox()
        self.spin_runs.setRange(1, 5)
        self.spin_runs.setValue(int(cfg.get("runs", 1)))
        g.addWidget(self.spin_runs, r, 1); r += 1
        self.lbl_ctype = StrongBodyLabel(self._t("settings.chartType"))
        g.addWidget(self.lbl_ctype, r, 0)
        self.cmb_ctype = ComboBox()
        self._ctype_codes = ["auto", "range_chart", "columnar_section"]
        self.cmb_ctype.addItems(["Auto", "Range Chart", "Columnar Section"])
        self.cmb_ctype.setCurrentIndex(self._ctype_codes.index(cfg.get("chart_type", "auto")) if cfg.get("chart_type", "auto") in self._ctype_codes else 0)
        g.addWidget(self.cmb_ctype, r, 1); r += 1
        self.lbl_clang = StrongBodyLabel(self._t("settings.chartLang"))
        g.addWidget(self.lbl_clang, r, 0)
        self.cmb_clang = ComboBox()
        self._clang_codes = ["auto", "zh", "en", "ja", "ru"]
        self.cmb_clang.addItems(["Auto", "Chinese", "English", "Japanese", "Russian"])
        self.cmb_clang.setCurrentIndex(self._clang_codes.index(cfg.get("chart_lang", "auto")) if cfg.get("chart_lang", "auto") in self._clang_codes else 0)
        g.addWidget(self.cmb_clang, r, 1); r += 1
        self.lbl_remember = StrongBodyLabel(self._t("settings.remember"))
        g.addWidget(self.lbl_remember, r, 0)
        self.sw_remember = SwitchButton()
        self.sw_remember.setChecked(bool(cfg.get("remember", True)))
        g.addWidget(self.sw_remember, r, 1, Qt.AlignLeft); r += 1
        g.setColumnStretch(1, 1)
        lay.addWidget(card)

        self.btn_save = PrimaryPushButton(FIF.SAVE, self._t("settings.save"))
        self.btn_save.clicked.connect(self._save)
        lay.addWidget(self.btn_save, 0, Qt.AlignLeft)
        lay.addStretch(1)

    def _save(self):
        self.win.save_all()
        InfoBar.success("", self._t("settings.saved"), parent=self.win,
                        position=InfoBarPosition.TOP)

    def retranslate(self):
        self.lbl_title.setText(self._t("menu.settings"))
        self.lbl_key.setText(self._t("settings.apiKey"))
        self.lbl_endpoint.setText(self._t("settings.endpoint"))
        self.lbl_model.setText(self._t("settings.model"))
        self.lbl_maxtok.setText(self._t("settings.maxTokens"))
        self.lbl_maxedge.setText(self._t("settings.maxEdge"))
        self.lbl_runs.setText(self._t("settings.runs"))
        self.lbl_ctype.setText(self._t("settings.chartType"))
        self.lbl_clang.setText(self._t("settings.chartLang"))
        self.lbl_remember.setText(self._t("settings.remember"))
        self.btn_save.setText(self._t("settings.save"))


# ---------------------------------------------------------------------------
# Main FluentWindow
# ---------------------------------------------------------------------------
class RangeChartFluentWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.tr = Translator(self.cfg.get("lang", "zh"))

        self.setWindowTitle("Range Chart Analyzer")
        self.resize(1240, 860)
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            ico = os.path.join(base, "assets", "logo.png")
            if os.path.isfile(ico):
                self.setWindowIcon(QIcon(ico))
        except Exception:
            pass

        # Pages
        self.extract_page = ExtractPage(self)
        self.providers_page = ProvidersPage(self)
        self.settings_page = SettingsPage(self)
        self.about_page = self._build_about()

        self.addSubInterface(self.extract_page, FIF.PHOTO, self._t("tab.extract") if self._t("tab.extract") != "tab.extract" else "Extract")
        self.addSubInterface(self.providers_page, FIF.CONNECT, self._t("settings.llmProvider"))
        self.addSubInterface(self.settings_page, FIF.SETTING, self._t("menu.settings"))
        self.addSubInterface(self.about_page, FIF.INFO, "About",
                             position=NavigationItemPosition.BOTTOM)

        # Language switcher at the bottom of the nav.
        self.navigationInterface.addWidget(
            routeKey="langSwitch",
            widget=self._build_lang_button(),
            onClick=self._cycle_lang,
            position=NavigationItemPosition.BOTTOM,
        )

    def _t(self, key):
        return self.tr.t(key)

    def _build_about(self):
        page = QWidget()
        page.setObjectName("aboutPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.setSpacing(10)
        lay.addWidget(TitleLabel("Range Chart Analyzer"))
        lay.addWidget(BodyLabel("从地层沿线图中提取结构化数据的工具。"))
        lay.addWidget(CaptionLabel("PySide6 + qfluentwidgets · MiniMax M3 · rca_core"))
        lay.addStretch(1)
        return page

    def _build_lang_button(self):
        from qfluentwidgets import NavigationPushButton
        btn = NavigationPushButton(FIF.LANGUAGE, "中/EN/日", False)
        return btn

    def _cycle_lang(self):
        order = ["zh", "en", "ja"]
        cur = self.tr.lang if self.tr.lang in order else "zh"
        nxt = order[(order.index(cur) + 1) % len(order)]
        self.tr.set_lang(nxt)
        self.cfg["lang"] = nxt
        for p in (self.extract_page, self.providers_page, self.settings_page):
            if hasattr(p, "retranslate"):
                p.retranslate()

    # ---- accessors used by pages (read from Settings widgets) ----
    def api_key(self):
        return self.settings_page.ipt_key.text().strip()

    def endpoint(self):
        return self.settings_page.ipt_endpoint.text().strip() or DEFAULT_ENDPOINT

    def model(self):
        return self.settings_page.ipt_model.text().strip() or DEFAULT_MODEL

    def max_tokens(self):
        return clamp_max_tokens(self.settings_page.spin_maxtok.value())

    def max_edge(self):
        return max(0, int(self.settings_page.spin_maxedge.value()))

    def runs(self):
        return max(1, min(int(self.settings_page.spin_runs.value()), 5))

    def chart_type(self):
        return self.settings_page._ctype_codes[self.settings_page.cmb_ctype.currentIndex()]

    def chart_lang(self):
        return self.settings_page._clang_codes[self.settings_page.cmb_clang.currentIndex()]

    def current_provider(self):
        try:
            return ProviderStore().load().get_current()
        except Exception:
            return None

    def save_all(self):
        s = self.settings_page
        remember = s.sw_remember.isChecked()
        self.cfg.update({
            "lang": self.tr.lang,
            "endpoint": s.ipt_endpoint.text().strip() or DEFAULT_ENDPOINT,
            "model": s.ipt_model.text().strip() or DEFAULT_MODEL,
            "max_tokens": self.max_tokens(),
            "max_edge": self.max_edge(),
            "runs": self.runs(),
            "chart_lang": self.chart_lang(),
            "chart_type": self.chart_type(),
            "remember": remember,
            "api_key": s.ipt_key.text().strip() if remember else "",
        })
        save_config(self.cfg)

    def closeEvent(self, event):
        try:
            self.save_all()
            self.extract_page._cleanup_paste_tmp()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    setTheme(Theme.AUTO)
    setThemeColor("#2563eb")
    win = RangeChartFluentWindow()
    win.show()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
