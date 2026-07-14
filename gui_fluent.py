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
    ScrollArea, IndeterminateProgressRing, Pivot, setTheme, Theme, setThemeColor,
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

# New-style provider page (cc-switch alignment + drag-to-reorder).
from gui_fluent_providers import ProvidersPage  # noqa: E402

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
    """Atomic JSON dump: write to .tmp then os.replace (POSIX + Win).

    A crash mid-write used to truncate the live config and lose all
    settings; the atomic-rename pattern keeps the previous good file
    intact if the new write fails.
    """
    import json
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
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
        try:
            if runs <= 1:
                self.progress.emit("analyzing")
                self.finished_ok.emit(extract(mode=mode, **params))
                return
        except Exception as exc:  # BUG13: never let exceptions kill the worker
            self.finished_ok.emit(ExtractResult(
                ok=False, error_key="err.http", raw=str(exc)))
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
        if self.busy:
            return  # BUG9: prevent double-start of the worker thread
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
        elif getattr(result, "truncated", False):
            status += " - " + self._t("err.truncated")
        self.lbl_status.setText(status)
        # Auto-switch to Extract sub-interface + scroll so the result tables
        # are immediately visible (the user is looking at the loading spinner
        # area otherwise).
        from PySide6.QtCore import QTimer as _Q
        def _jump():
            try:
                self.win._nav_extract.click()
            except Exception:
                pass
        _Q.singleShot(50, _jump)

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
        # Friendly empty state when the model returned no data at all.
        total_rows = sum(len((self.result or {}).get(c["id"], []) or []) for c in configs)
        if total_rows == 0:
            self.pivot.addItem(
                routeKey="empty",
                text=self._t("results.empty"),
                onClick=lambda _=False: None,
            )
            empty = BodyLabel(self._t("results.empty"))
            empty.setAlignment(Qt.AlignCenter)
            self.stack_lay.addSpacing(20)
            self.stack_lay.addWidget(empty)
            hint = CaptionLabel(self._t("results.emptyHint") if "results.emptyHint" in
                                (getattr(self._t, "TRANSLATIONS", {}) or {})
                                else "The model returned no structured data. Try again with a clearer image or different caption.")
            hint.setAlignment(Qt.AlignCenter)
            self.stack_lay.addWidget(hint)
            self._refresh_conf()
            return
        for idx, cfg in enumerate(configs):
            items = (self.result or {}).get(cfg["id"], []) or []
            table = TableWidget()
            table.setBorderVisible(True)
            table.setBorderRadius(8)
            table.setWordWrap(False)
            table.setMinimumHeight(180)   # never collapse below 180px
            cols = ["#"] + [self._t(c) for c in cfg["cols"]]
            table.setColumnCount(len(cols))
            table.setHorizontalHeaderLabels(cols)
            table.setRowCount(max(1, len(items)))   # at least one row so the table is visible
            # Pad / truncate values to len(cols) so a future contributor who
            # adds a custom row extractor can't silently misalign cells
            # (mirrors build_table_export's M11 guard).
            n_cols = len(cols)
            for ri, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                cells = cfg["row"](item)
                cells = (list(cells) + [""] * n_cols)[:n_cols]
                values = [str(ri + 1)] + ["" if c is None else str(c) for c in cells]
                # cc-switch style: flag low-agreement rows (multi-run merge).
                low = (multi and cfg["id"] == "species_ranges"
                       and int(item.get("agreement_count", 0) or 0) <= n_runs / 2)
                for ci, val in enumerate(values):
                    if ci >= n_cols:
                        break   # extra safety against row lambda drift
                    cell = QTableWidgetItem(val)
                    if low:
                        cell.setBackground(Qt.yellow)
                    table.setItem(ri, ci, cell)
            table.resizeColumnsToContents()
            table.horizontalHeader().setStretchLastSection(True)
            self.tables[cfg["id"]] = table
            label = f"{self._t(cfg['title_key'])} ({len(items)})"
            self.pivot.addItem(routeKey=cfg["id"], text=label,
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
            self.lbl_imginfo.setText(self._t("image.none") if not self.image_path else self.lbl_imginfo.text())
        else:
            # Re-render the result tables so column headers + tab labels
            # follow the new language.
            self._render_result()


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
        self._ctype_keys = ["settings.chartType.auto",
                            "settings.chartType.rangeChart",
                            "settings.chartType.columnarSection"]
        self.cmb_ctype.addItems([self._t(k) for k in self._ctype_keys])
        self.cmb_ctype.setCurrentIndex(self._ctype_codes.index(cfg.get("chart_type", "auto")) if cfg.get("chart_type", "auto") in self._ctype_codes else 0)
        g.addWidget(self.cmb_ctype, r, 1); r += 1
        self.lbl_clang = StrongBodyLabel(self._t("settings.chartLang"))
        g.addWidget(self.lbl_clang, r, 0)
        self.cmb_clang = ComboBox()
        self._clang_codes = ["auto", "zh", "en", "ja", "ru"]
        # Language names stay stable (they are proper nouns), only the
        # "Auto" entry is translated.
        self._clang_names = lambda: [self._t("chartLang.auto"), "中文", "English", "日本語", "Русский"]
        self.cmb_clang.addItems(self._clang_names())
        self.cmb_clang.setCurrentIndex(self._clang_codes.index(cfg.get("chart_lang", "auto")) if cfg.get("chart_lang", "auto") in self._clang_codes else 0)
        g.addWidget(self.cmb_clang, r, 1); r += 1
        self.lbl_remember = StrongBodyLabel(self._t("settings.remember"))
        g.addWidget(self.lbl_remember, r, 0)
        self.sw_remember = SwitchButton()
        self.sw_remember.setChecked(bool(cfg.get("remember", True)))
        # BUG28: when remember is OFF, immediately clear the in-memory key
        # so extract() doesn't use a key the user thought they discarded.
        self.sw_remember.checkedChanged.connect(
            lambda on: self.ipt_key.setText("") if not on else None)
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
        # Refresh combobox option labels without losing the current choice.
        ci = self.cmb_ctype.currentIndex()
        self.cmb_ctype.clear()
        self.cmb_ctype.addItems([self._t(k) for k in self._ctype_keys])
        self.cmb_ctype.setCurrentIndex(max(0, ci))
        li = self.cmb_clang.currentIndex()
        self.cmb_clang.clear()
        self.cmb_clang.addItems(self._clang_names())
        self.cmb_clang.setCurrentIndex(max(0, li))


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

        # Keep references to the nav items so the sidebar labels can be
        # re-translated live on language switch.
        self._nav_extract = self.addSubInterface(
            self.extract_page, FIF.PHOTO, self._nav_text("tab.extract", "Extract"))
        self._nav_providers = self.addSubInterface(
            self.providers_page, FIF.CONNECT, self._t("settings.llmProvider"))
        self._nav_settings = self.addSubInterface(
            self.settings_page, FIF.SETTING, self._t("menu.settings"))
        self._nav_about = self.addSubInterface(
            self.about_page, FIF.INFO, self._nav_text("tab.about", "About"),
            position=NavigationItemPosition.BOTTOM)

        # Language switcher at the bottom of the nav.
        self._lang_btn = self._build_lang_button()
        self.navigationInterface.addWidget(
            routeKey="langSwitch",
            widget=self._lang_btn,
            onClick=self._cycle_lang,
            position=NavigationItemPosition.BOTTOM,
        )

    def _t(self, key):
        return self.tr.t(key)

    def _nav_text(self, key, fallback):
        """Return the translation, or a fallback when the key is missing
        (so a nav label never shows the raw 'tab.about' string)."""
        val = self.tr.t(key)
        return fallback if val == key else val

    def _build_about(self):
        page = QWidget()
        page.setObjectName("aboutPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 28)
        lay.setSpacing(10)
        self._about_title = TitleLabel("Range Chart Analyzer")
        self._about_body = BodyLabel(self._nav_text("about.desc", "Extract structured data from stratigraphic range charts."))
        self._about_caption = CaptionLabel("PySide6 + qfluentwidgets · rca_core")
        lay.addWidget(self._about_title)
        lay.addWidget(self._about_body)
        lay.addWidget(self._about_caption)
        lay.addStretch(1)
        return page

    def _build_lang_button(self):
        from qfluentwidgets import NavigationPushButton
        btn = NavigationPushButton(FIF.LANGUAGE, self._lang_label(), False)
        return btn

    def _lang_label(self):
        # Show the CURRENT language plus a hint of the next one.
        names = {"zh": "中文", "en": "English", "ja": "日本語"}
        return names.get(self.tr.lang, "中文")

    def _cycle_lang(self):
        order = ["zh", "en", "ja"]
        cur = self.tr.lang if self.tr.lang in order else "zh"
        nxt = order[(order.index(cur) + 1) % len(order)]
        self.tr.set_lang(nxt)
        self.cfg["lang"] = nxt
        save_config(self.cfg)
        # Re-translate the nav sidebar labels.
        self._nav_extract.setText(self._nav_text("tab.extract", "Extract"))
        self._nav_providers.setText(self._t("settings.llmProvider"))
        self._nav_settings.setText(self._t("menu.settings"))
        self._nav_about.setText(self._nav_text("tab.about", "About"))
        self._lang_btn.setText(self._lang_label())
        # Re-translate the About page.
        self._about_body.setText(self._nav_text("about.desc", "Extract structured data from stratigraphic range charts."))
        # Re-translate the three functional pages.
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
        # BUG12: stop any in-flight worker thread so a late signal
        # doesn't reach a destroyed ExtractPage widget.
        try:
            w = getattr(self.extract_page, "_worker", None)
            if w is not None:
                try:
                    w.finished_ok.disconnect()
                    w.progress.disconnect()
                except (TypeError, RuntimeError):
                    pass
                if w.isRunning():
                    w.quit()
                    w.wait(1000)
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
