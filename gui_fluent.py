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

import logging
import os
import sys
import tempfile
import time

# Bug-12 fix: module-level logger so bare `except Exception:` blocks
# below have somewhere to report what they swallowed. Without this, a
# silently failing callback leaves no trace.
log = logging.getLogger("rca.gui_fluent")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt, QThread, Signal, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QTableWidgetItem, QHeaderView, QFrame, QSizePolicy, QStackedLayout,
)

from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon as FIF,
    LineEdit, PasswordLineEdit, PushButton, PrimaryPushButton, ComboBox,
    SpinBox, SwitchButton, TableWidget, BodyLabel, TitleLabel, SubtitleLabel,
    StrongBodyLabel, CaptionLabel, CardWidget, TextEdit, InfoBar, InfoBarPosition,
    ScrollArea, IndeterminateProgressRing, Pivot, MessageBox, setTheme, Theme, setThemeColor,
)

from rca_core import (
    Translator, build_table_export, get_configs_for_result, load_image_b64,
    to_csv, to_tsv, extract, merge_results, ProviderStore,
)
from rca_core.aggregate import COLUMNAR_SECTION_SCHEMA, RANGE_CHART_SCHEMA, SCHEMA_BY_MODE
from rca_core.extractor import (
    DEFAULT_ENDPOINT, DEFAULT_MAX_TOKENS, DEFAULT_MAX_EDGE, DEFAULT_MODEL,
    ExtractResult, clamp_max_tokens,
)
from rca_core.llm import test_llm_connection

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
            # Bug-12 fix: log so an unexpected exception doesn't disappear.
            log.exception("ExtractWorker.run failed")
            self.finished_ok.emit(ExtractResult(
                ok=False, error_key="err.http", raw=str(exc)))
            return
        ok_datas, last_fail, partial_fails, any_trunc, raws = [], None, 0, False, []
        done = 0
        # Track the HTTP status from the most recent successful run so we
        # can surface it on the merged result. The previous code did
        # `ok_datas[0] and 200` which is always the literal 200 (a dict
        # is truthy), throwing away whatever the API actually returned.
        last_ok_status = None
        # Accumulate usage and latency across successful runs.
        total_in, total_out = 0, 0
        total_cr, total_cc = 0, 0
        est_in, est_out = False, False
        # Runs execute concurrently, so batch latency is wall-clock time, not
        # the sum of each run's latency (which would over-count Nx for N
        # parallel runs).
        total_latency = 0
        batch_t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=runs) as ex:
            futures = [ex.submit(extract, mode=mode, **params) for _ in range(runs)]
            # Per-future hard timeout. The user's `timeout_sec` is the
            # per-request LLM timeout, but we add a small grace window
            # (10s) for Python/JIT overhead. A future that exceeds this is
            # recorded as a timeout failure rather than blocking the UI
            # forever (the previous version had no per-future timeout —
            # one stalled run could pin the whole batch indefinitely).
            per_future_timeout = params.get("timeout_sec", 120) + 10
            for fut in concurrent.futures.as_completed(futures):
                try:
                    r = fut.result(timeout=per_future_timeout)
                except concurrent.futures.TimeoutError:
                    r = ExtractResult(
                        ok=False, error_key="err.timeout",
                        error_body=f"per-future timeout after {per_future_timeout}s",
                    )
                except Exception as exc:
                    # Bug-12 fix: log so the exception class + traceback
                    # is recoverable when the user reports an empty error.
                    log.exception("extract future raised in ExtractWorker thread")
                    r = ExtractResult(ok=False, error_key="err.http", raw=str(exc))
                done += 1
                # Emit progress with elapsed seconds so a stalled run
                # doesn't freeze the UI on "1/N" indefinitely. The label
                # keeps the language-switch friendly i18n key.
                elapsed_s = int(time.perf_counter() - batch_t0)
                self.progress.emit(
                    f"analyzing:{done}/{runs}:{elapsed_s}s"
                )
                if r.ok and r.data is not None:
                    ok_datas.append(r.data)
                    any_trunc = any_trunc or bool(r.truncated)
                    if r.raw:
                        raws.append(r.raw)
                    # Capture the real HTTP status from the API response so
                    # the merged result carries the actual upstream code
                    # (200 / 201 / etc.), not a hardcoded constant.
                    if r.status is not None:
                        last_ok_status = r.status
                    # Accumulate usage from this successful run.
                    u = r.usage or {}
                    total_in += int(u.get("input_tokens") or 0)
                    total_out += int(u.get("output_tokens") or 0)
                    total_cr += int(u.get("cache_read_tokens") or 0)
                    total_cc += int(u.get("cache_creation_tokens") or 0)
                    est_in = est_in or bool(u.get("estimated"))
                    est_out = est_out or bool(u.get("estimated"))
                else:
                    last_fail = r
                    partial_fails += 1
        total_latency = int((time.perf_counter() - batch_t0) * 1000)
        if not ok_datas:
            self.finished_ok.emit(last_fail or ExtractResult(ok=False, error_key="err.empty"))
            return
        schema = SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)
        merged = merge_results(ok_datas, total_runs=runs, schema=schema)
        # Use last failure's status if any runs failed; otherwise from a success.
        status_code = (getattr(last_fail, "status", None) if last_fail
                       else last_ok_status)
        merged_usage: dict[str, Any] = {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "cache_read_tokens": total_cr,
            "cache_creation_tokens": total_cc,
        }
        if est_in or est_out:
            merged_usage["estimated"] = True
        self.finished_ok.emit(ExtractResult(
            ok=True, data=merged,
            raw="\n---RUN---\n".join(raws)[:8000],
            truncated=any_trunc or bool(partial_fails),
            partial_failures=partial_fails,
            usage=merged_usage,
            latency_ms=total_latency,
            status=status_code,
        ))


class ConnTestWorker(QThread):
    """Runs test_llm_connection() off the UI thread."""
    done = Signal(object)  # ConnectionResult

    def __init__(self, provider):
        super().__init__()
        self._provider = provider

    # NOTE: a previous ConnTestWorker class lived here but was never used
# (ProvidersPage defines its own inline _Worker instead). Removed in
# audit pass to keep the module lean.


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
        self.busy = False

        self.setObjectName("extractPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        root = QWidget()
        self.setWidget(root)

        # Root: horizontal split — left = input, right = results
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(20, 16, 20, 16)
        root_lay.setSpacing(16)

        # ---- Left panel: title + image + caption + run controls ----
        left_panel = QWidget()
        left_lay = QVBoxLayout(left_panel)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(12)

        self.lbl_title = TitleLabel(self._t("upload.title") if self._t("upload.title") != "upload.title" else "Extract")
        left_lay.addWidget(self.lbl_title)

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
        left_lay.addWidget(img_card)

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
        left_lay.addWidget(cap_card)

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
        self.btn_export_xlsx = PushButton(FIF.SAVE, self._t("export.xlsx"))
        self.btn_export_xlsx.clicked.connect(self._export_xlsx)
        run_row.addWidget(self.btn_export_xlsx)
        run_row.addWidget(self.btn_extract)
        left_lay.addLayout(run_row)
        left_lay.addStretch(1)

        root_lay.addWidget(left_panel, 1)   # left takes 1 share

        # ---- Right panel: confidence + pivot + results ----
        right_panel = QWidget()
        right_lay = QVBoxLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(10)

        self.lbl_conf = StrongBodyLabel("")
        right_lay.addWidget(self.lbl_conf)
        self.pivot = Pivot()
        # qfluentwidgets' Pivot fires `currentItemChanged(routeKey)` on
        # every selection change, including user clicks. The onClick
        # callback passed to addItem is unreliable on Windows (qfluent
        # calls it only on the very first click in some builds, then
        # switches to setCurrentItem-only afterwards). Connect to the
        # signal once so every pivot change routes through _show_table.
        self.pivot.currentItemChanged.connect(self._show_table)
        right_lay.addWidget(self.pivot)

        # Edit-mode action row (Add Row / Delete Row / Apply Edits).
        # The user can edit cells by double-clicking; these buttons
        # commit the changes back into self.result and persist them
        # into the history record.
        edit_row = QHBoxLayout()
        edit_row.setSpacing(6)
        self.btn_add_row = PushButton(FIF.ADD, self._t("edit.addRow"))
        self.btn_add_row.clicked.connect(self._on_add_row)
        edit_row.addWidget(self.btn_add_row)
        self.btn_del_row = PushButton(FIF.REMOVE, self._t("edit.deleteRow"))
        self.btn_del_row.clicked.connect(self._on_delete_row)
        edit_row.addWidget(self.btn_del_row)
        self.btn_discard = PushButton(FIF.CANCEL, self._t("edit.discard"))
        self.btn_discard.clicked.connect(self._on_discard_edits)
        edit_row.addWidget(self.btn_discard)
        edit_row.addStretch(1)
        # "Modified" badge: lights up when any cell has been touched.
        from qfluentwidgets import StateToolTip
        self.lbl_dirty = CaptionLabel("")
        self.lbl_dirty.setStyleSheet("color:#f59e0b;")
        edit_row.addWidget(self.lbl_dirty)
        self.btn_apply_edits = PrimaryPushButton(FIF.SAVE, self._t("edit.apply"))
        self.btn_apply_edits.clicked.connect(self._on_apply_edits)
        edit_row.addWidget(self.btn_apply_edits)
        right_lay.addLayout(edit_row)

        # Storage for the inner TableWidget per table id (so apply edits
        # can read the cells back). The scroll area is also kept in
        # self.tables for show/hide.
        self._table_widgets: dict[str, object] = {}
        self._active_table_id: str = ""
        self._last_snapshot: dict | None = None  # for discard

        self.stack = QFrame()
        # QStackedLayout (not QVBoxLayout) so only the current pivot's
        # table is rendered. The previous VBox+setVisible() pattern broke
        # on Windows when qfluentwidgets' Pivot didn't fire onClick,
        # leaving the right panel empty after the first render.
        self.stack_lay = QStackedLayout(self.stack)
        self.stack_lay.setContentsMargins(0, 0, 0, 0)
        self.tables = {}
        right_lay.addWidget(self.stack, 1)

        root_lay.addWidget(right_panel, 2)   # right takes 2 shares (wider)

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
            b64, mime, w, h, resized, decode_error = load_image_b64(path, self.win.max_edge())
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
            w = max(80, self.preview.width()) if self.preview.width() > 0 else 300
            self.preview.setPixmap(pix.scaled(
                QSize(w, 172),
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
            if any(k in cap for k in ("pollen", "abundance", "percentage diagram", "palyno", "孢粉", "花粉", "丰度", "百分比")):
                mode = "abundance_diagram"
            elif any(k in cap for k in ("column", "columns", "columnar", "col_section", "col_sections", "柱状", "柱状図", "柱状图")):
                mode = "columnar_section"
            else:
                mode = "range_chart"
        self.busy = True
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
        self.busy = False
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
        # ---- Persist to history + record usage for the Usage page ----
        try:
            provider = None
            try:
                provider = self.win.current_provider()
            except Exception:
                provider = None
            mode = self._current_mode()
            usage = (getattr(result, "usage", {}) or {})
            # Record a single usage row per run (the merged result already
            # aggregates token counts across all runs).
            self.win.record_usage(
                provider=provider,
                model=(provider.model if provider else "") or "",
                mode=mode,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read=int(usage.get("cache_read_tokens", 0) or 0),
                cache_creation=int(usage.get("cache_creation_tokens", 0) or 0),
                in_estimated=bool(usage.get("estimated")),
                out_estimated=bool(usage.get("estimated")),
                latency_ms=int(getattr(result, "latency_ms", 0) or 0),
                status_code=result.status,
            )
            # Persist a history record (with a thumbnail if Pillow is up).
            thumb = self._maybe_thumbnail()
            self.win.save_to_history(
                result=self.result, mode=mode,
                image_path=self.image_path or "",
                image_thumb=thumb,
                image_w=self._img_dims[0] or 0,
                image_h=self._img_dims[1] or 0,
                provider=provider, model=(provider.model if provider else "") or "",
                runs=int((self.result or {}).get("runs", 1) or 1),
                confidence=float((self.result or {}).get("confidence", 0) or 0),
                partial_failures=int(getattr(result, "partial_failures", 0) or 0),
                duration_ms=int(getattr(result, "latency_ms", 0) or 0),
                status_code=result.status,
                raw=result.raw or "",
            )
        except Exception as exc:
            # Persistence is best-effort: a failure here must not block the
            # user from seeing their result.
            print(f"[rca] persist post-result: {exc}")
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

    def _current_mode(self) -> str:
        """Return the mode string for the current extraction."""
        try:
            ct = self.win.cfg.get("chart_type", "auto")
        except Exception:
            ct = "auto"
        if ct in ("range_chart", "columnar_section", "abundance_diagram"):
            return ct
        # Auto-detect by result shape.
        if isinstance(self.result, dict):
            if isinstance(self.result.get("abundances"), list):
                return "abundance_diagram"
            sects = self.result.get("sections") or []
            if sects and isinstance(sects[0], dict) and "id" in sects[0] and "name" not in sects[0]:
                return "columnar_section"
        return "range_chart"

    def _maybe_thumbnail(self) -> bytes | None:
        """Return a small JPEG thumbnail of the loaded image, or None.

        Best-effort: Pillow is the only dependency; if it's missing we
        return None and the history record just has no preview."""
        if not self.image_path:
            return None
        try:
            from io import BytesIO
            from PIL import Image
            # Use a context manager so the file handle is released even
            # if save() raises — otherwise Windows can briefly hold the
            # file lock and the next handle on the same path gets
            # PermissionError. The previous code relied on GC.
            with Image.open(self.image_path) as img:
                img.thumbnail((256, 256))
                buf = BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                return buf.getvalue()
        except Exception:
            return None

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
            hint = CaptionLabel(self._t("results.emptyHint"))
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
            # In-place editing: double-click a cell to edit, Enter to commit.
            # Edits stay in the table widget's internal model; the user
            # clicks "Apply Edits" to push them into the result dict.
            from PySide6.QtWidgets import QAbstractItemView
            table.setEditTriggers(
                QAbstractItemView.DoubleClicked
                | QAbstractItemView.EditKeyPressed
                | QAbstractItemView.AnyKeyPressed
            )
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
            # Wrap table in a scroll area so wide content scrolls horizontally
            # instead of being squished by setStretchLastSection.
            scroll = ScrollArea()
            scroll.setWidget(table)
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            table.setStyleSheet("border: none;")
            self.tables[cfg["id"]] = scroll   # store scroll area, not raw table
            self._table_widgets[cfg["id"]] = table  # also keep the inner widget
            # Watch for cell edits to drive the "Modified" badge.
            try:
                table.itemChanged.connect(self._on_table_item_changed)
            except Exception:
                pass
            label = f"{self._t(cfg['title_key'])} ({len(items)})"
            # The onClick callback is intentionally NOT passed here —
            # currentItemChanged (wired up at Pivot creation) handles
            # every tab switch reliably across all qfluentwidgets builds.
            self.pivot.addItem(routeKey=cfg["id"], text=label)
            self.stack_lay.addWidget(scroll)   # all in stacked layout
        if configs:
            self.pivot.setCurrentItem(configs[0]["id"])
            # setCurrentItem triggers currentItemChanged → _show_table.
            # We don't need a manual fallback anymore.
        self._refresh_conf()

    def _show_table(self, key):
        """Switch the stacked layout to the table for ``key``.

        Uses QStackedLayout.setCurrentWidget which is reliable on all
        platforms and doesn't depend on qfluentwidgets Pivot firing its
        onClick callback (it sometimes doesn't on Windows).
        """
        w = self.tables.get(key)
        if w is not None:
            self.stack_lay.setCurrentWidget(w)
        self._active_table_id = key or ""

    def _rebuild_pivot(self) -> None:
        """Rebuild pivot tab labels from the current result's configs.

        Called after a language switch so tab labels (e.g. "Species Ranges (12)")
        pick up the new translation without requiring a full _render_result.
        Preserves which tab is currently active.
        """
        if not self.result:
            return
        configs = get_configs_for_result(self.result)
        current_key = self._active_table_id
        self.pivot.clear()
        for cfg in configs:
            items = (self.result or {}).get(cfg["id"], []) or []
            label = f"{self._t(cfg['title_key'])} ({len(items)})"
            self.pivot.addItem(routeKey=cfg["id"], text=label)
        # Restore the previously active tab, falling back to the first tab.
        if current_key and current_key in self.tables:
            self.pivot.setCurrentItem(current_key)
        elif configs:
            self.pivot.setCurrentItem(configs[0]["id"])

    # ---- table edit handlers ----

    def _on_table_item_changed(self, _item) -> None:
        """Mark the result as dirty when any cell is touched."""
        if self._last_snapshot is None and isinstance(self.result, dict):
            import copy
            try:
                self._last_snapshot = copy.deepcopy(self.result)
            except Exception:
                self._last_snapshot = self.result
        try:
            self.lbl_dirty.setText(self._t("edit.dirty"))
        except Exception:
            pass

    def _current_table(self):
        """Return the inner TableWidget for the active pivot tab."""
        if not self._active_table_id:
            return None
        return self._table_widgets.get(self._active_table_id)

    def _current_cfg(self) -> dict | None:
        if not self._active_table_id or not isinstance(self.result, dict):
            return None
        cfgs = get_configs_for_result(self.result)
        for cfg in cfgs:
            if cfg["id"] == self._active_table_id:
                return cfg
        return None

    def _on_add_row(self) -> None:
        table = self._current_table()
        cfg = self._current_cfg()
        if not table or not cfg or not isinstance(self.result, dict):
            return
        new_row_idx = table.rowCount()
        table.insertRow(new_row_idx)
        cols = cfg.get("data_keys") or cfg["cols"]
        for ci in range(len(cols)):
            table.setItem(new_row_idx, ci + 1, QTableWidgetItem(""))
        # Show the new row to the user.
        table.scrollToBottom()
        try:
            self.lbl_dirty.setText(self._t("edit.dirty"))
        except Exception:
            pass

    def _on_delete_row(self) -> None:
        table = self._current_table()
        if not table:
            return
        row = table.currentRow()
        if row < 0:
            InfoBar.info(
                "", self._t("edit.deleteRow"), parent=self.win,
                position=InfoBarPosition.TOP, duration=2000,
            )
            return
        table.removeRow(row)
        try:
            self.lbl_dirty.setText(self._t("edit.dirty"))
        except Exception:
            pass

    def _on_discard_edits(self) -> None:
        if not isinstance(self.result, dict):
            return
        if self._last_snapshot is None:
            # Nothing to discard.
            return
        confirm = MessageBox(
            self._t("edit.confirmDiscard"),
            self._t("edit.confirmDiscardHint"),
            self.win,
        )
        if not confirm.exec():
            return
        self.result = self._last_snapshot
        self._last_snapshot = None
        self._render_result()
        try:
            self.lbl_dirty.setText("")
        except Exception:
            pass
        InfoBar.success(
            "", self._t("edit.discard"), parent=self.win,
            position=InfoBarPosition.TOP, duration=2000,
        )

    def _on_apply_edits(self) -> None:
        if not isinstance(self.result, dict):
            return
        from rca_core import apply_table_edits
        any_applied = False
        for tid, table in (self._table_widgets or {}).items():
            if table is None:
                continue
            # Walk each row and build a list of [index, cell, cell, ...]
            rows: list[list[str]] = []
            for r in range(table.rowCount()):
                row_cells: list[str] = []
                for c in range(table.columnCount()):
                    item = table.item(r, c)
                    row_cells.append(item.text() if item else "")
                rows.append(row_cells)
            try:
                apply_table_edits(self.result, tid, rows)
                any_applied = True
            except Exception as exc:
                InfoBar.error(
                    "", f"{tid}: {exc}", parent=self.win,
                    position=InfoBarPosition.TOP, duration=4000,
                )
        if any_applied:
            self._last_snapshot = None
            try:
                self.lbl_dirty.setText("")
            except Exception:
                pass
            self._refresh_conf()
            # Re-render so the table reflects the cleaned-up values
            # (e.g. split formations back to one cell, dropped placeholder
            # rows, etc.).
            self._render_result()
            # If a history record exists for the current result, push
            # the edits back so reloading the record from history shows
            # the latest version. The newest record for this image (if
            # any) is updated in-place.
            try:
                self._persist_edits_to_history()
            except Exception as exc:
                print(f"[rca] persist edits: {exc}")
            InfoBar.success(
                "", self._t("edit.saved"), parent=self.win,
                position=InfoBarPosition.TOP, duration=2000,
            )

    def _persist_edits_to_history(self) -> None:
        """Update the most recent history record with the current result.

        Best-effort: prefers the record we were loaded from (when the user
        reopened a historical entry via the History page) so the same row
        round-trips; falls back to the most-recent record for this image
        path when the current result came from a fresh extraction.
        """
        hs = getattr(self.win, "_history_store", None)
        if hs is None:
            return
        loaded_id = getattr(self, "_loaded_history_id", None)
        if loaded_id is not None:
            hs.update_result(loaded_id, self.result)
            return
        if not self.image_path:
            return
        records = hs.list(limit=20, search=os.path.basename(self.image_path))
        if not records:
            return
        hs.update_result(records[0].id, self.result)

    def _refresh_conf(self):
        if not self.result:
            self.lbl_conf.setText("")
            return
        pct = round((self.result.get("confidence", 0) or 0) * 100)
        self.lbl_conf.setText(f"{self._t('status.confidence')}: {pct}%")

    def load_result(self, result: dict, mode: str = "range_chart",
                    record_id: int | None = None) -> None:
        """Restore a result dict (e.g. loaded from history) and re-render.

        When ``record_id`` is given (loaded from the History page), the next
        "Apply Edits" push goes back to that exact record via
        ``_persist_edits_to_history``. Without it, edits to a loaded
        historical result would silently fail to round-trip (the old code
        matched by image-path search and only updated the most-recent
        record, often a different one).
        """
        self.result = result
        self._loaded_history_id = record_id
        self.raw_text = ""
        # Drop any pending edit snapshot / dirty badge — the result we
        # just loaded *is* the baseline, the old snapshot belongs to the
        # previous in-memory result.
        self._last_snapshot = None
        try:
            self.lbl_dirty.setText("")
        except Exception:
            pass
        # The loaded record has no image path of its own (we only stored
        # the result JSON), so wipe the preview path to keep "Export
        # JSON" honest about which file the result came from. The user
        # can re-upload to export with a real source_file.
        self.image_path = None
        self.lbl_imginfo.setText(self._t("image.none"))
        self.preview.clear()
        # If the loaded result is a columnar-section shape, update the
        # mode display so the user sees the right table set.
        try:
            self._render_result()
        except Exception as exc:
            print(f"[rca] load_result render failed: {exc}")

    def _export_prefix(self) -> str:
        """Filename prefix reflecting the current result's chart kind."""
        mode = self._current_mode()
        if mode == "abundance_diagram":
            return "abundance_diagram_"
        if mode == "columnar_section":
            return "columnar_section_"
        return "range_chart_"

    def _export_xlsx(self) -> None:
        if not self.result:
            return
        try:
            from rca_core import to_xlsx as _to_xlsx
        except ImportError:
            InfoBar.error(
                "", self._t("export.xlsxMissingOpenpyxl"),
                parent=self.win, position=InfoBarPosition.TOP, duration=5000,
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, self._t("export.xlsx"), self._export_prefix() + "result.xlsx",
            "Excel (*.xlsx)")
        if not path:
            return
        try:
            _to_xlsx(self.result, file_or_path=path, translate=self._t)
            InfoBar.success(
                "", self._t("export.xlsxDone"), parent=self.win,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.error(
                "", str(exc), parent=self.win,
                position=InfoBarPosition.TOP, duration=5000,
            )

    def _export_json(self):
        if not self.result:
            return
        import json
        path, _ = QFileDialog.getSaveFileName(
            self, self._t("dialog.saveJson"), self._export_prefix() + "result.json",
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
        self.btn_export_xlsx.setText(self._t("export.xlsx"))
        self.btn_add_row.setText(self._t("edit.addRow"))
        self.btn_del_row.setText(self._t("edit.deleteRow"))
        self.btn_discard.setText(self._t("edit.discard"))
        self.btn_apply_edits.setText(self._t("edit.apply"))
        if not self.result:
            self.lbl_status.setText(self._t("status.ready"))
            self.lbl_imginfo.setText(self._t("image.none") if not self.image_path else self.lbl_imginfo.text())
        else:
            # Re-render the result tables so column headers + tab labels
            # follow the new language.
            self._render_result()
        # HIGH-1: rebuild the pivot tabs so their translated labels update
        # when the user switches language with an existing result loaded.
        if self.result is not None:
            self._rebuild_pivot()


class SettingsPage(ScrollArea):
    """Quick-edit view of the *currently active* LLM provider.

    This page is intentionally simpler than the legacy API settings
    panel: it shows the active provider's name, format, endpoint, and
    model as a read-only summary, with a single field to update the API
    key. All other configuration (adding / removing / reordering
    providers) lives on the dedicated Providers page. This avoids the
    "two parallel config UIs that disagree" trap that the legacy layout
    fell into.
    """

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

        # ----- Active provider quick-edit -----
        self.card_active = CardWidget()
        ca = QVBoxLayout(self.card_active)
        ca.setContentsMargins(20, 18, 20, 18)
        ca.setSpacing(10)
        self.lbl_active_title = StrongBodyLabel(self._t("settings.activeConfig"))
        self.lbl_active_hint = CaptionLabel(self._t("settings.activeConfigHint"))
        ca.addWidget(self.lbl_active_title)
        ca.addWidget(self.lbl_active_hint)

        g = QGridLayout()
        g.setVerticalSpacing(10)
        g.setHorizontalSpacing(12)

        # Read-only summary labels for the four fixed fields.
        self.lbl_name = StrongBodyLabel(self._t("wizard.fieldName"))
        self.val_name = BodyLabel("-")
        g.addWidget(self.lbl_name, 0, 0); g.addWidget(self.val_name, 0, 1)
        self.lbl_fmt = StrongBodyLabel(self._t("wizard.fieldFormat"))
        self.val_fmt = BodyLabel("-")
        g.addWidget(self.lbl_fmt, 1, 0); g.addWidget(self.val_fmt, 1, 1)
        self.lbl_endpoint = StrongBodyLabel(self._t("wizard.fieldEndpoint"))
        self.val_endpoint = BodyLabel("-")
        self.val_endpoint.setWordWrap(True)
        g.addWidget(self.lbl_endpoint, 2, 0); g.addWidget(self.val_endpoint, 2, 1)
        self.lbl_model = StrongBodyLabel(self._t("settings.model"))
        self.val_model = BodyLabel("-")
        g.addWidget(self.lbl_model, 3, 0); g.addWidget(self.val_model, 3, 1)

        # Editable API key + test button.
        self.lbl_key = StrongBodyLabel(self._t("settings.apiKey"))
        self.ipt_key = PasswordLineEdit()
        self.ipt_key.setPlaceholderText(self._t("settings.apiKey"))
        g.addWidget(self.lbl_key, 4, 0); g.addWidget(self.ipt_key, 4, 1)
        g.setColumnStretch(1, 1)
        ca.addLayout(g)

        # Action row
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_save_key = PrimaryPushButton(FIF.SAVE, self._t("settings.save"))
        self.btn_save_key.clicked.connect(self._save_key)
        actions.addWidget(self.btn_save_key)
        self.btn_test = PushButton(FIF.SEND, self._t("settings.testConnection"))
        self.btn_test.clicked.connect(self._on_test_connection)
        actions.addWidget(self.btn_test)
        actions.addStretch(1)
        self.btn_open_providers = PushButton(self._t("settings.llmProvider"))
        self.btn_open_providers.clicked.connect(lambda: self.win.switch_to_providers())
        actions.addWidget(self.btn_open_providers)
        ca.addLayout(actions)
        lay.addWidget(self.card_active)

        # ----- Generation / chart settings (kept; orthogonal to providers) -----
        self.card_advanced = CardWidget()
        cv = QVBoxLayout(self.card_advanced)
        cv.setContentsMargins(20, 18, 20, 18)
        cv.setSpacing(10)
        self.lbl_advanced = StrongBodyLabel(self._t("settings.advanced"))
        cv.addWidget(self.lbl_advanced)

        g2 = QGridLayout()
        g2.setVerticalSpacing(10); g2.setHorizontalSpacing(12)

        self.lbl_maxtok = StrongBodyLabel(self._t("settings.maxTokens"))
        self.spin_maxtok = SpinBox()
        self.spin_maxtok.setRange(1, 32000)
        self.spin_maxtok.setValue(int(cfg.get("max_tokens", DEFAULT_MAX_TOKENS)))
        g2.addWidget(self.lbl_maxtok, 0, 0); g2.addWidget(self.spin_maxtok, 0, 1)

        self.lbl_maxedge = StrongBodyLabel(self._t("settings.maxEdge"))
        self.spin_maxedge = SpinBox()
        self.spin_maxedge.setRange(0, 8000)
        self.spin_maxedge.setValue(int(cfg.get("max_edge", DEFAULT_MAX_EDGE)))
        g2.addWidget(self.lbl_maxedge, 1, 0); g2.addWidget(self.spin_maxedge, 1, 1)

        self.lbl_runs = StrongBodyLabel(self._t("settings.runs"))
        self.spin_runs = SpinBox()
        self.spin_runs.setRange(1, 5)
        self.spin_runs.setValue(int(cfg.get("runs", 1)))
        g2.addWidget(self.lbl_runs, 2, 0); g2.addWidget(self.spin_runs, 2, 1)

        self.lbl_ctype = StrongBodyLabel(self._t("settings.chartType"))
        self.cmb_ctype = ComboBox()
        self._ctype_codes = ["auto", "range_chart", "columnar_section", "abundance_diagram"]
        self._ctype_keys = ["settings.chartType.auto",
                            "settings.chartType.rangeChart",
                            "settings.chartType.columnarSection",
                            "settings.chartType.abundanceDiagram"]
        self.cmb_ctype.addItems([self._t(k) for k in self._ctype_keys])
        cur = cfg.get("chart_type", "auto")
        self.cmb_ctype.setCurrentIndex(self._ctype_codes.index(cur) if cur in self._ctype_codes else 0)
        g2.addWidget(self.lbl_ctype, 3, 0); g2.addWidget(self.cmb_ctype, 3, 1)

        self.lbl_clang = StrongBodyLabel(self._t("settings.chartLang"))
        self.cmb_clang = ComboBox()
        self._clang_codes = ["auto", "zh", "en", "ja", "ru"]
        self._clang_names = lambda: [self._t("chartLang.auto"), "中文", "English", "日本語", "Русский"]
        self.cmb_clang.addItems(self._clang_names())
        cur = cfg.get("chart_lang", "auto")
        self.cmb_clang.setCurrentIndex(self._clang_codes.index(cur) if cur in self._clang_codes else 0)
        g2.addWidget(self.lbl_clang, 4, 0); g2.addWidget(self.cmb_clang, 4, 1)

        self.lbl_remember = StrongBodyLabel(self._t("settings.remember"))
        self.sw_remember = SwitchButton()
        self.sw_remember.setChecked(bool(cfg.get("remember", True)))
        g2.addWidget(self.lbl_remember, 5, 0); g2.addWidget(self.sw_remember, 5, 1, Qt.AlignLeft)
        g2.setColumnStretch(1, 1)
        cv.addLayout(g2)

        self.btn_save_advanced = PrimaryPushButton(FIF.SAVE, self._t("settings.save"))
        self.btn_save_advanced.clicked.connect(self._save_advanced)
        cv.addWidget(self.btn_save_advanced, 0, Qt.AlignLeft)
        lay.addWidget(self.card_advanced)
        lay.addStretch(1)

        self.refresh_active()

    # ---- helpers ----

    def refresh_active(self) -> None:
        """Pull the active provider out of the ProviderStore and update labels."""
        # Use the window's current_provider() so the lazy-loaded store gets
        # initialized on first access. Reading self.win._provider_store
        # directly here misses the lazy init path and the labels show "-"
        # for an already-configured provider.
        try:
            current = self.win.current_provider()
        except Exception:
            current = None
        if current is None:
            self.val_name.setText("-")
            self.val_fmt.setText("-")
            self.val_endpoint.setText(self._t("provider.noEndpoint"))
            self.val_model.setText("-")
            self.ipt_key.setText("")
        else:
            self.val_name.setText(current.name or "-")
            self.val_fmt.setText(
                current.api_format.value
                if hasattr(current.api_format, "value")
                else str(current.api_format)
            )
            self.val_endpoint.setText(current.endpoint or self._t("provider.noEndpoint"))
            self.val_model.setText(current.model or "-")
            # Pre-fill the key field with the existing key (masked by PasswordLineEdit).
            self.ipt_key.setText(current.api_key or "")

    def _save_key(self) -> None:
        """Write the new API key into the active provider and persist."""
        try:
            current = self.win.current_provider()
            if not current:
                InfoBar.warning(
                    "", self._t("errors.noProviders"),
                    parent=self.win, position=InfoBarPosition.TOP, duration=3000,
                )
                return
            current.api_key = self.ipt_key.text().strip()
            # Re-fetch the live store (current_provider's lazy path) so we
            # update the same object the rest of the app holds onto.
            store = self.win._provider_store
            ok = store.update(current) if store is not None else False
            if not ok:
                raise RuntimeError("provider not found")
            # Mirror the legacy cfg field so the Extract page can pick it up.
            self.win.cfg["api_key"] = current.api_key
            self.win.save_all()
            InfoBar.success(
                "", self._t("settings.saved"), parent=self.win,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.error(
                "", str(exc), parent=self.win,
                position=InfoBarPosition.TOP, duration=4000,
            )

    def _on_test_connection(self) -> None:
        try:
            current = self.win.current_provider()
        except Exception:
            current = None
        if not current:
            InfoBar.warning(
                "", self._t("errors.noProviders"),
                parent=self.win, position=InfoBarPosition.TOP, duration=3000,
            )
            return
        # Make sure the in-edit-field key overrides whatever's in the
        # provider record (the user may have just typed a new key but
        # not yet saved).
        typed = self.ipt_key.text().strip()
        if typed:
            current.api_key = typed
        from rca_core.llm import test_llm_connection
        result = test_llm_connection(current, timeout_sec=10)
        if result.ok:
            InfoBar.success(
                "",
                f"OK · {result.latency_ms} ms · "
                f"{len(result.models_sample)} models",
                parent=self.win,
                position=InfoBarPosition.TOP, duration=3000,
            )
        else:
            err_key = result.error_key or "err.http"
            msg = self._t(err_key) if err_key.startswith("err.") else (err_key or "fail")
            if result.status:
                msg += f" (HTTP {result.status})"
            InfoBar.error(
                "", msg, parent=self.win,
                position=InfoBarPosition.TOP, duration=5000,
            )

    def _save_advanced(self) -> None:
        try:
            ctype_idx = self.cmb_ctype.currentIndex()
            clang_idx = self.cmb_clang.currentIndex()
            self.win.cfg.update({
                "max_tokens": int(self.spin_maxtok.value()),
                "max_edge": int(self.spin_maxedge.value()),
                "runs": int(self.spin_runs.value()),
                "chart_type": self._ctype_codes[ctype_idx],
                "chart_lang": self._clang_codes[clang_idx],
                "remember": bool(self.sw_remember.isChecked()),
            })
            self.win.save_all()
            InfoBar.success(
                "", self._t("settings.saved"), parent=self.win,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.error(
                "", str(exc), parent=self.win,
                position=InfoBarPosition.TOP, duration=4000,
            )

    def retranslate(self):
        self.lbl_title.setText(self._t("menu.settings"))
        self.lbl_active_title.setText(self._t("settings.activeConfig"))
        self.lbl_active_hint.setText(self._t("settings.activeConfigHint"))
        self.lbl_name.setText(self._t("wizard.fieldName"))
        self.lbl_fmt.setText(self._t("wizard.fieldFormat"))
        self.lbl_endpoint.setText(self._t("wizard.fieldEndpoint"))
        self.lbl_model.setText(self._t("settings.model"))
        self.lbl_key.setText(self._t("settings.apiKey"))
        self.btn_save_key.setText(self._t("settings.save"))
        self.btn_test.setText(self._t("settings.testConnection"))
        self.btn_open_providers.setText(self._t("settings.llmProvider"))
        self.lbl_advanced.setText(self._t("settings.advanced"))
        self.lbl_maxtok.setText(self._t("settings.maxTokens"))
        self.lbl_maxedge.setText(self._t("settings.maxEdge"))
        self.lbl_runs.setText(self._t("settings.runs"))
        self.lbl_ctype.setText(self._t("settings.chartType"))
        self.lbl_clang.setText(self._t("settings.chartLang"))
        self.lbl_remember.setText(self._t("settings.remember"))
        self.btn_save_advanced.setText(self._t("settings.save"))
        # Refresh combobox option labels without losing the current choice.
        ci = self.cmb_ctype.currentIndex()
        self.cmb_ctype.clear()
        self.cmb_ctype.addItems([self._t(k) for k in self._ctype_keys])
        self.cmb_ctype.setCurrentIndex(max(0, ci))
        li = self.cmb_clang.currentIndex()
        self.cmb_clang.clear()
        self.cmb_clang.addItems(self._clang_names())
        self.cmb_clang.setCurrentIndex(max(0, li))
        self.refresh_active()


# ---------------------------------------------------------------------------
# Main FluentWindow
# ---------------------------------------------------------------------------
class RangeChartFluentWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.tr = Translator(self.cfg.get("lang", "zh"))
        self._provider_store = None

        # Persistent stores (history + token usage) — created early so the
        # Extract page can write to them on success.
        try:
            from rca_core import Database, HistoryStore, UsageStore
            self._db = Database()
            self._history_store = HistoryStore(db=self._db)
            self._usage_store = UsageStore(db=self._db)
        except Exception as exc:
            # Storage is non-fatal: the user can still extract, they just
            # won't get history / usage. Surface the error on the status
            # bar when the extract page is built.
            self._db = None
            self._history_store = None
            self._usage_store = None
            print(f"[rca] persistent storage unavailable: {exc}")

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
        # History + Usage are lazy-imported so the GUI still starts when
        # the new modules are mid-migration.
        try:
            from gui_fluent_pages import HistoryPage, UsagePage
            self.history_page = HistoryPage(
                self, self._history_store, self.tr,
            )
            self.usage_page = UsagePage(
                self, self._usage_store, self.tr,
            )
        except Exception as exc:
            print(f"[rca] history/usage pages unavailable: {exc}")
            self.history_page = None
            self.usage_page = None

        # Keep references to the nav items so the sidebar labels can be
        # re-translated live on language switch.
        self._nav_extract = self.addSubInterface(
            self.extract_page, FIF.PHOTO, self._nav_text("tab.extract", "Extract"))
        if self.history_page is not None:
            self._nav_history = self.addSubInterface(
                self.history_page, FIF.HISTORY,
                self._nav_text("tab.history", "History"),
            )
        if self.usage_page is not None:
            self._nav_usage = self.addSubInterface(
                self.usage_page, FIF.PIE_SINGLE,
                self._nav_text("tab.usage", "Usage"),
            )
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

        # First-launch onboarding. Delayed so the main window is fully
        # painted before the dialog pops up; otherwise the modal can
        # race with the show-event and end up behind the window.
        try:
            from PySide6.QtCore import QTimer as _Q
            from gui_fluent_onboarding import maybe_show_onboarding
            _Q.singleShot(500, lambda: maybe_show_onboarding(self, self.tr))
        except Exception as exc:
            print(f"[rca] onboarding init failed: {exc}")

    # ---- navigation helpers (used by the History / Settings pages) ----

    def switch_to_providers(self) -> None:
        """Used by the Settings page's "Manage providers" button."""
        try:
            self.switchTo(self.providers_page)
        except Exception:
            try:
                self.stackedWidget.setCurrentWidget(self.providers_page)
            except Exception:
                pass

    def load_from_history(self, rec) -> None:
        """Push a historical record back into the Extract page so the user
        can re-export / continue editing. Falls back to a toast when the
        Extract page doesn't expose ``load_result``."""
        # If a worker is mid-flight its eventual finished_ok signal will
        # overwrite whatever we load here. The right thing is to refuse
        # the load and let the user retry once the extraction finishes,
        # not silently nuke the historical record they wanted to see.
        if getattr(self.extract_page, "busy", False):
            InfoBar.warning(
                "", self._t("status.loading"),
                parent=self, position=InfoBarPosition.TOP, duration=3000,
            )
            return
        try:
            self.extract_page.load_result(rec.result, rec.mode or "range_chart", record_id=rec.id)
            self.switchTo(self.extract_page)
            InfoBar.success(
                "", f"#{rec.id}", parent=self,
                position=InfoBarPosition.TOP, duration=2000,
            )
        except Exception as exc:
            InfoBar.warning(
                "", str(exc), parent=self,
                position=InfoBarPosition.TOP, duration=3000,
            )

    def save_to_history(self, *, result: dict, mode: str, image_path: str,
                       image_thumb: bytes | None, image_w: int, image_h: int,
                       provider, model: str, runs: int, confidence: float,
                       partial_failures: int, duration_ms: int,
                       status_code: int | None, raw: str) -> int | None:
        """Persist a completed extraction to the SQLite history table.
        Returns the new record id, or None on failure."""
        if self._history_store is None:
            # Persistent storage is non-fatal (the app still runs), but
            # the user must know their record is gone — otherwise they
            # navigate to the History tab, see an empty list, and think
            # the extraction was lost.
            InfoBar.warning(
                "", self._t("status.historySaveUnavailable"),
                parent=self, position=InfoBarPosition.TOP, duration=4000,
            )
            return None
        try:
            from rca_core import HistoryRecord
            rec = HistoryRecord(
                timestamp=__import__("time").time(),
                source_file=image_path or "",
                image_thumbnail=image_thumb,
                image_width=image_w,
                image_height=image_h,
                provider_id=provider.id if provider else "",
                provider_name=provider.name if provider else "",
                model=model or "",
                mode=mode,
                runs=runs,
                result=result,
                raw=raw or "",
                confidence=confidence,
                partial_failures=partial_failures,
                duration_ms=duration_ms,
                status_code=status_code,
            )
            new_id = self._history_store.add(rec)
        except Exception as exc:
            print(f"[rca] save_to_history failed: {exc}")
            # Surface the failure on-screen too, not just in the console.
            InfoBar.error(
                "", f"{self._t('err.http')}: {exc}",
                parent=self, position=InfoBarPosition.TOP, duration=4000,
            )
            return None
        # The History page keeps its own row count and doesn't observe
        # the DB; refresh it now so the new record shows up without the
        # user having to switch tabs and click the refresh button.
        try:
            if self.history_page is not None:
                self.history_page.refresh()
        except Exception as exc:
            print(f"[rca] history refresh after save: {exc}")
        return new_id

    def record_usage(self, *, provider, model: str, mode: str,
                    input_tokens: int, output_tokens: int,
                    cache_read: int = 0, cache_creation: int = 0,
                    in_estimated: bool = False, out_estimated: bool = False,
                    latency_ms: int = 0, status_code: int | None = None,
                    error_message: str = "") -> None:
        if self._usage_store is None:
            return
        try:
            from rca_core import UsageRecord
            self._usage_store.record(UsageRecord(
                provider_id=provider.id if provider else "",
                provider_name=provider.name if provider else "",
                model=model or "",
                endpoint=provider.endpoint if provider else "",
                mode=mode,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                input_tokens_estimated=in_estimated,
                output_tokens_estimated=out_estimated,
                latency_ms=latency_ms,
                status_code=status_code,
                error_message=error_message,
            ))
        except Exception as exc:
            print(f"[rca] record_usage failed: {exc}")

    def _t(self, key):
        return self.tr.t(key)

    def _nav_text(self, key, fallback):
        """Return the translation, or a fallback when the key is missing
        (so a nav label never shows the raw 'tab.about' string)."""
        val = self.tr.t(key)
        return fallback if val == key else val

    def _build_about(self):
        # Use the upgraded About page from the onboarding module — it
        # shows version, license, and clickable link buttons.
        try:
            from gui_fluent_onboarding import _build_about as _build
            return _build(self, self.tr)
        except Exception:
            # Fallback to the legacy minimal About body.
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
        if getattr(self, "_nav_history", None) is not None:
            self._nav_history.setText(self._nav_text("tab.history", "History"))
        if getattr(self, "_nav_usage", None) is not None:
            self._nav_usage.setText(self._nav_text("tab.usage", "Usage"))
        self._nav_providers.setText(self._t("settings.llmProvider"))
        self._nav_settings.setText(self._t("menu.settings"))
        self._nav_about.setText(self._nav_text("tab.about", "About"))
        self._lang_btn.setText(self._lang_label())
        # Re-translate the About page.
        # The about page is only set up when the legacy fallback runs;
        # when gui_fluent_onboarding supplies the page, _about_body isn't
        # created. Guard so a language switch never crashes.
        if getattr(self, "_about_body", None) is not None:
            self._about_body.setText(self._nav_text(
                "about.desc",
                "Extract structured data from stratigraphic range charts.",
            ))
        # Re-translate the three functional pages.
        for p in (self.extract_page, self.providers_page, self.settings_page):
            if hasattr(p, "retranslate"):
                p.retranslate()
        # Re-translate the lazy-loaded history / usage pages.
        if self.history_page is not None and hasattr(self.history_page, "set_translator"):
            self.history_page.set_translator(self.tr)
        if self.usage_page is not None and hasattr(self.usage_page, "set_translator"):
            self.usage_page.set_translator(self.tr)

    # ---- accessors used by pages (read from Settings widgets) ----
    def api_key(self):
        return self.settings_page.ipt_key.text().strip()

    def endpoint(self):
        try:
            p = self.current_provider()
            return (p.endpoint or "").strip() or DEFAULT_ENDPOINT
        except Exception:
            return DEFAULT_ENDPOINT

    def model(self):
        try:
            p = self.current_provider()
            return (p.model or "").strip() or DEFAULT_MODEL
        except Exception:
            return DEFAULT_MODEL

    def max_tokens(self):
        return clamp_max_tokens(self.settings_page.spin_maxtok.value())

    def max_edge(self):
        val = self.settings_page.spin_maxedge.value()
        return max(0, int(val)) if val is not None else DEFAULT_MAX_EDGE

    def runs(self):
        val = self.settings_page.spin_runs.value()
        return max(1, min(int(val), 5)) if val is not None else 1

    def chart_type(self):
        return self.settings_page._ctype_codes[self.settings_page.cmb_ctype.currentIndex()]

    def chart_lang(self):
        return self.settings_page._clang_codes[self.settings_page.cmb_clang.currentIndex()]

    def current_provider(self):
        try:
            if self._provider_store is None:
                self._provider_store = ProviderStore().load()
            current = self._provider_store.get_current()
            if current is not None and not (current.api_key or "").strip():
                # One-shot migration: the cfg's legacy api_key is the
                # only place the key is still alive (e.g. user typed it
                # and closed the app without hitting "Save settings",
                # so the provider record never picked it up). Seed the
                # provider from cfg and persist so the next launch
                # reads it from the canonical provider store.
                legacy = (self.cfg.get("api_key") or "").strip()
                if legacy:
                    current.api_key = legacy
                    try:
                        self._provider_store.update(current)
                    except Exception:
                        pass
            return current
        except Exception:
            return None

    def invalidate_provider_cache(self):
        self._provider_store = None

    def _sync_ipt_key_to_provider(self) -> None:
        """Mirror the settings page's live API-key field into the provider
        record and persist. Called on app close (and elsewhere) so the
        provider store always reflects what the user just typed, even
        if they didn't explicitly hit the "Save settings" button.

        Without this the ipt_key only writes to ``cfg["api_key"]`` on
        save_all(); the provider record stays at its last-saved value,
        and the next launch (which reads from the provider, not cfg)
        shows an empty key field — which looks like the key was
        forgotten, even though it's still sitting in cfg.
        """
        try:
            s = getattr(self, "settings_page", None)
            if s is None or not hasattr(s, "ipt_key"):
                return
            current = self.current_provider()
            if current is None:
                return
            typed = s.ipt_key.text().strip()
            if typed and typed != (current.api_key or ""):
                current.api_key = typed
                store = self._provider_store
                if store is not None:
                    store.update(current)
        except Exception as exc:
            print(f"[rca] sync ipt_key to provider: {exc}")

    def save_all(self):
        s = self.settings_page
        remember = s.sw_remember.isChecked()
        # endpoint / model live in the provider store, not in cfg.
        # Only save api_key into cfg (the legacy field) when "remember" is on.
        self.cfg.update({
            "lang": self.tr.lang,
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
            # Before save_all() writes the cfg blob, mirror the live
            # ipt_key into the provider store. save_all() already
            # copies it into cfg["api_key"] (so the legacy field stays
            # populated), but the provider record is the one the next
            # launch reads from when populating ipt_key. Without this
            # sync a user who types a key and closes the app without
            # hitting "Save settings" sees an empty field next time.
            try:
                self._sync_ipt_key_to_provider()
            except Exception:
                pass
            self.save_all()
            self.extract_page._cleanup_paste_tmp()
        except Exception:
            pass
        # Stop any in-flight worker so a late signal doesn't reach a
        # destroyed widget. First try the cooperative cancel event (the
        # worker polls its own _abort Event to break out of the network
        # call promptly), then disconnect signals, then wait. terminate()
        # is a last-resort hard kill (bounded wait so exit never blocks
        # indefinitely).
        try:
            w = getattr(self.extract_page, "_worker", None)
            if w is not None:
                # Cooperative cancel: signal the worker to abort the
                # in-flight request. The network call uses a short timeout
                # so it returns promptly once cancelled.
                abort_evt = getattr(w, "_abort", None)
                if abort_evt is not None:
                    try:
                        abort_evt.set()
                    except Exception:
                        pass
                try:
                    w.finished_ok.disconnect()
                    w.progress.disconnect()
                except (TypeError, RuntimeError):
                    pass
                if w.isRunning():
                    w.quit()
                    if not w.wait(5000):
                        w.terminate()
                        w.wait(2000)
        except Exception:
            pass
        # Same deal for the providers page's connection-test worker -
        # it lives on its own QThread and would otherwise leak past
        # window close.
        try:
            tw = getattr(self.providers_page, "_test_worker", None)
            if tw is not None:
                abort_evt = getattr(tw, "_abort", None)
                if abort_evt is not None:
                    try:
                        abort_evt.set()
                    except Exception:
                        pass
                try:
                    tw.done.disconnect()
                except (TypeError, RuntimeError):
                    pass
                if tw.isRunning():
                    tw.quit()
                    if not tw.wait(2000):
                        tw.terminate()
                        tw.wait(1000)
        except Exception:
            pass
        self.extract_page.busy = False
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
