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

from PySide6.QtCore import Qt, QThread, Signal, QSize, QUrl
from PySide6.QtGui import QPixmap, QIcon, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog,
    QTableWidgetItem, QHeaderView, QFrame, QSizePolicy, QStackedLayout, QSplitter,
)
# Phylogenetic tree renderer: the D3.js template lives in
# rca_core/resources/phylogenetic_tree.html and is hosted inside an
# embedded QWebEngineView. Both modules ship with PySide6 (the
# QtWebEngine subpackage) but we import them defensively so the GUI
# still loads on slim Qt builds where WebEngine was stripped — the
# widget class exposes HAS_PHYLO_TREE_WIDGET for callers to skip.
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView  # type: ignore
    from PySide6.QtWebEngineCore import QWebEngineSettings  # type: ignore
    HAS_PHYLO_TREE_WIDGET = True
except Exception:  # pragma: no cover - Qt build without WebEngine
    HAS_PHYLO_TREE_WIDGET = False

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
from rca_core.ssrf import validate_endpoint as _validate_endpoint
# REVIEW-2026-09-10: the extract pre-flight uses the SAME policy as the
# core's live extract paths and the connection test — a local Ollama
# endpoint passed the connection test but was refused here.
from rca_core.ssrf import validate_endpoint_local_ok as _validate_extract_endpoint

# New-style provider page (cc-switch alignment + drag-to-reorder).
from gui_fluent_providers import ProvidersPage  # noqa: E402

try:
    from PIL import Image, ImageGrab  # type: ignore
    HAS_PIL = True
except Exception:
    HAS_PIL = False

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer.json")

# Sprint B (REVIEW-2026-09-04): per-run LLM extraction timeout (seconds).
# Neither GUI exposes a settings entry for this; rca_core.extractor
# clamps the value into [10, 300]. The worker previously computed its
# budget from params.get("timeout_sec", 120) although NO caller ever put
# the key into params, so the budget silently depended on an inline magic
# number. The constant is wired into the params dict at the single
# construction site (ExtractPage._on_extract) so the request timeout and
# the worker's collection budget share one source of truth.
EXTRACT_TIMEOUT_SEC = 120

# Sprint B (REVIEW-2026-09-04): module-level register for worker threads
# that outlived the window. closeEvent waits a bounded time for QThreads
# to finish; a thread still inside a urllib/SSL call cannot be cancelled
# (quit() is a no-op for a QThread that overrides run(), and terminate()
# is documented by Qt as unsafe). Letting the last Python reference to a
# still-running QThread be GC'd makes Qt6 abort the whole process with
# qFatal("Destroyed while thread is still running"), so workers that are
# still running after the bounded wait are parked here: the list holds a
# strong reference until the thread finishes naturally; the finished
# callback (installed by _park_orphaned_worker) then removes the worker
# and schedules deleteLater() for safe reclamation. No terminate() is
# ever used.
_orphaned_workers: list = []


def _park_orphaned_worker(w) -> None:
    """Keep *w* alive until its thread finishes, then free it.

    Must be called from the GUI thread. The strong reference in
    ``_orphaned_workers`` is what prevents the Qt6 qFatal crash; when the
    thread finishes naturally the register entry is dropped and
    ``deleteLater()`` (a slot of *w* itself, so the queued connection is
    invoked on *w*'s owning thread — the canonical Qt worker-teardown
    pattern) reclaims the wrapper. Never uses terminate().
    """
    if w is None:
        return
    if w in _orphaned_workers:
        return
    _orphaned_workers.append(w)

    def _drop_ref():
        try:
            _orphaned_workers.remove(w)
        except ValueError:
            pass

    try:
        # Order matters: drop the register reference first, then schedule
        # C++ deletion.
        w.finished.connect(_drop_ref)
        w.finished.connect(w.deleteLater)
    except (RuntimeError, TypeError):
        # Already-destroyed thread: drop the reference immediately so the
        # register cannot leak.
        _drop_ref()
        return
    if not w.isRunning():
        # The thread finished between the caller's isRunning() check and
        # the signal connections above — finished() already fired, so
        # release the register entry now.
        _drop_ref()


def load_config() -> dict:
    try:
        import json
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    # H3 fix (REVIEW-2026-11-07): new saves encrypt the legacy api_key
    # field at rest (see save_config). Legacy plaintext configs still
    # load: only envelope-marked values are decrypted, and a decrypt
    # failure keeps the raw value so a bad envelope can't wipe the field.
    key = cfg.get("api_key")
    if key:
        try:
            from rca_core.secrets_store import decrypt, is_obfuscated
            if is_obfuscated(key):
                cfg["api_key"] = decrypt(key)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    """Atomic JSON dump: write to .tmp then os.replace (POSIX + Win).

    A crash mid-write used to truncate the live config and lose all
    settings; the atomic-rename pattern keeps the previous good file
    intact if the new write fails.

    H3 fix (REVIEW-2026-11-07): the legacy api_key field used to sit in
    this JSON in PLAINTEXT. Encrypt it through rca_core.secrets_store
    (Fernet when cryptography+keyring are installed, machine-fingerprint
    obfuscation otherwise). A local copy is used so the caller's dict
    (save_all passes the live self.cfg) is not mutated, and
    is_obfuscated() prevents double-wrapping on re-save.
    """
    import json
    to_write = cfg
    key = (cfg.get("api_key") or "")
    if key:
        try:
            from rca_core.secrets_store import encrypt, is_obfuscated
            if not is_obfuscated(key):
                to_write = dict(cfg)
                to_write["api_key"] = encrypt(key)
        except Exception:
            # REVIEW-2026-09-10: fail CLOSED — an encrypt failure used to fall
            # through and persist the raw plaintext key. Drop it from the file
            # instead (the provider store keeps its own copy; the in-session
            # value is untouched) and leave a marker the settings page can show.
            to_write = dict(cfg)
            to_write["api_key"] = ""
            to_write["api_key_store_failed"] = True
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(to_write, f, ensure_ascii=False, indent=2)
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
def _collect_extraction_futures(futures, budget, *, should_cancel=None,
                                on_batch=None):
    """Sprint B (REVIEW-2026-09-04): collect extraction futures under a
    REAL whole-batch time budget.

    The previous implementation iterated ``as_completed(futures)`` and
    called ``fut.result(timeout=...)`` per future — but as_completed only
    yields futures that have ALREADY finished, so the per-future timeout
    could never fire and one stalled run pinned the whole batch forever.

    All runs execute concurrently, so ONE budget equal to the per-request
    LLM timeout (``timeout_sec``) plus a small grace window covers the
    batch. Futures that overrun it are reported as ``err.timeout``
    ExtractResults instead of being waited on indefinitely. Running
    threads cannot be killed safely, so the caller must NOT join the
    executor after this returns (use ``shutdown(wait=False)``).

    Returns ``(results, cancelled)``. ``cancelled`` is True when
    ``should_cancel`` fired between batches; every unfinished future was
    cancel()ed and only the results collected so far are returned.
    ``on_batch(done_count)`` runs after each completed batch so the caller
    can stream progress.

    Deliberately Qt-free so the timeout/cancel semantics are unit-testable
    without a QApplication (see tests/test_gui_sprint_b.py).
    """
    import concurrent.futures
    results: list = []
    pending = set(futures)
    deadline = time.perf_counter() + float(budget)
    while pending:
        remaining = deadline - time.perf_counter()
        done_set, pending = concurrent.futures.wait(
            pending, timeout=max(0.0, remaining),
            return_when=concurrent.futures.ALL_COMPLETED)
        for fut in done_set:
            try:
                results.append(fut.result())
            except Exception as exc:
                # extract() never raises, but defend against unforeseen
                # bugs in user code. Bug-12 fix: log so the exception
                # class + traceback is recoverable.
                log.exception("extract future raised in worker thread")
                results.append(ExtractResult(
                    ok=False, error_key="err.http", raw=str(exc)))
        if on_batch is not None:
            try:
                on_batch(len(results))
            except Exception:
                pass
        if pending and deadline - time.perf_counter() <= 0:
            # Budget exhausted: cancel whatever has not started and
            # report the still-running runs as timeouts.
            n_timed_out = len(pending)
            for unf in pending:
                unf.cancel()
            pending.clear()
            for _ in range(n_timed_out):
                results.append(ExtractResult(
                    ok=False, error_key="err.timeout",
                    error_body=f"per-future timeout after {budget:.0f}s",
                ))
            break
        if pending and should_cancel is not None and should_cancel():
            for unf in pending:
                unf.cancel()
            return results, True
    return results, False


class ExtractWorker(QThread):
    """Runs extract() N times concurrently, merges, emits the result.

    NEVER touches widgets — only emits signals the UI thread listens to.
    """
    finished_ok = Signal(object)   # ExtractResult
    progress = Signal(str)         # status text

    def __init__(self, params, mode, runs, auto_filename=""):
        super().__init__()
        self._params = params
        self._mode = mode
        self._runs = runs
        # Audit fix (LOW): cooperative cancel flag for window-close. The
        # previous code called QThread.terminate() on this worker if the
        # window was closed mid-urllib/SSL, which is documented by Qt as
        # dangerous — the thread can be killed while holding the Python
        # GIL or an OpenSSL mutex, deadlocking teardown. We set this flag
        # from closeEvent and check it between work steps; the worker
        # cooperatively exits so the thread is never forcibly terminated.
        self._auto_filename = auto_filename
        self._cancel_requested = False

    def request_cancel(self) -> None:
        """Ask the worker to stop at the next cancellation checkpoint.

        Sprint B (REVIEW-2026-09-04) honesty note: for a single-run
        extraction (runs <= 1) there is NO checkpoint inside the blocking
        urllib request, so the flag is only observed after extract()
        returns — a cancel during a single run can take up to the full
        network timeout to take effect. The multi-run path (runs > 1)
        checks the flag BEFORE each submit and between completion
        batches, so it stops promptly and reports uncollected runs as
        cancelled. Safe to call from any thread (the flag is a plain
        Python bool).
        """
        self._cancel_requested = True

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def run(self):
        import concurrent.futures
        params, mode, runs = self._params, self._mode, self._runs
        # UI-REVIEW-2026-09-07: resolve "auto" on the worker thread —
        # caption keywords first, then vision classification of the image
        # when nothing matched. Never on the UI thread.
        if mode == "auto":
            from rca_core.chart_mode import auto_detect_chart_mode_ex
            from rca_core.extractor import resolve_auto_mode
            self.progress.emit("classifying")
            mode, matched = auto_detect_chart_mode_ex(
                (params.get("caption") or "") + " " + (self._auto_filename or ""))
            if not matched:
                # REVIEW-2026-09-10: pass the SAME credentials the extraction
                # will use. Only `provider` was forwarded, so on the legacy-key
                # path (provider is None) the classifier ran against
                # DEFAULT_ENDPOINT with an EMPTY key — a guaranteed 401 — and
                # resolve_auto_mode then silently fell back to range_chart.
                # An abundance/zonation figure was extracted with the
                # range-chart prompt and presented as a successful result.
                mode, _cls = resolve_auto_mode(
                    caption=params.get("caption") or "",
                    filename=self._auto_filename or "",
                    image_b64=params.get("image_b64") or "",
                    media_type=params.get("media_type") or "image/png",
                    api_key=params.get("api_key") or "",
                    base_url=params.get("base_url") or "",
                    model=params.get("model") or "",
                    provider=params.get("provider"),
                    timeout_sec=params.get("timeout_sec"),
                )
                if _cls is not None and not getattr(_cls, "ok", False):
                    self.progress.emit("classify-failed")

        def prog(stage):
            self.progress.emit(stage)

        params["progress_callback"] = prog

        try:
            if runs <= 1:
                self.progress.emit("analyzing")
                # Sprint B (REVIEW-2026-09-04): the single-run fast path
                # has no cancellation checkpoint inside the blocking
                # urllib request (see request_cancel). Attach the resolved
                # mode so history persistence records the real chart kind
                # instead of the raw "auto" dropdown value (gui.py parity).
                result = extract(mode=mode, **params)
                try:
                    result._mode = mode
                except Exception:
                    pass
                self.finished_ok.emit(result)
                return
        except Exception as exc:  # BUG13: never let exceptions kill the worker
            # Bug-12 fix: log so an unexpected exception doesn't disappear.
            log.exception("ExtractWorker.run failed")
            self.finished_ok.emit(ExtractResult(
                ok=False, error_key="err.http", raw=str(exc)))
            return
        ok_datas, last_fail, partial_fails, any_trunc, raws = [], None, 0, False, []
        done = 0
        # P1 fix (2026-08-06): keep the image fingerprint + per-request
        # metadata of the first successful run so the merged result's
        # history record is stamped with the REAL image_sha256 instead of
        # an empty fingerprint (the previous merged ExtractResult carried
        # image_sha256="", breaking get_by_sha256 grouping for multi-run).
        first_ok_sha = ""
        first_ok_meta = None
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
        # Sprint B (REVIEW-2026-09-04): whole-batch collection budget =
        # per-request timeout_sec + 10s grace (see _collect_extraction_futures
        # for why as_completed()+fut.result(timeout=...) could never time out).
        budget = float(params.get("timeout_sec", EXTRACT_TIMEOUT_SEC)) + 10.0
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=runs)
        try:
            futures = []
            # Sprint B (REVIEW-2026-09-04): check the cooperative-cancel
            # flag BEFORE each submit so a cancel arriving during start-up
            # prevents the remaining runs from ever launching (the flag
            # was previously only read after futures had completed).
            for _i in range(runs):
                if self._cancel_requested:
                    break
                futures.append(executor.submit(extract, mode=mode, **params))
            # Runs never submitted because of the cancel are recorded as
            # cancelled failures so the merged bookkeeping stays
            # consistent with ``runs``.
            for _unstarted in range(runs - len(futures)):
                last_fail = ExtractResult(
                    ok=False, error_key="err.cancelled",
                    error_body="run not started: cancel requested",
                )
                partial_fails += 1
            if not futures:
                self.finished_ok.emit(ExtractResult(
                    ok=False, error_key="err.cancelled",
                    error_body="user cancelled the extraction",
                ))
                return

            def _on_batch(n_done):
                # Emit progress with elapsed seconds so a stalled run
                # doesn't freeze the UI on "1/N" indefinitely. The label
                # keeps the language-switch friendly i18n key.
                elapsed_s = int(time.perf_counter() - batch_t0)
                self.progress.emit(
                    f"analyzing:{n_done}/{runs}:{elapsed_s}s"
                )

            results, cancelled = _collect_extraction_futures(
                futures, budget,
                should_cancel=lambda: self._cancel_requested,
                on_batch=_on_batch,
            )
            if cancelled:
                # Phase K fix + Sprint B: honour user cancel between
                # completion batches — bail out as soon as the flag is
                # seen instead of waiting for every future.
                self.finished_ok.emit(ExtractResult(
                    ok=False, error_key="err.cancelled",
                    error_body="user cancelled the extraction",
                ))
                return
            for r in results:
                done += 1
                if r.ok and r.data is not None:
                    ok_datas.append(r.data)
                    any_trunc = any_trunc or bool(r.truncated)
                    if r.raw:
                        raws.append(r.raw)
                    # P1 fix (2026-08-06): capture fingerprint + request
                    # metadata from the first successful run.
                    if not first_ok_sha and getattr(r, "image_sha256", ""):
                        first_ok_sha = r.image_sha256
                    if first_ok_meta is None and getattr(r, "request_meta", None):
                        first_ok_meta = r.request_meta
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
        finally:
            # Sprint B (REVIEW-2026-09-04): never join overrunning threads.
            # shutdown(wait=False) lets this worker QThread finish (and
            # emit) while runs that already exceeded the budget drain in
            # the background — they were reported as err.timeout by the
            # collector and the UI must not stay pinned on them.
            executor.shutdown(wait=False)
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
        merged_res = ExtractResult(
            ok=True, data=merged,
            raw="\n---RUN---\n".join(raws)[:8000],
            truncated=any_trunc or bool(partial_fails),
            partial_failures=partial_fails,
            usage=merged_usage,
            latency_ms=total_latency,
            status=status_code,
            # P1 fix (2026-08-06): propagate the real image fingerprint and
            # request metadata so _on_result persists a correct audit record.
            image_sha256=first_ok_sha,
            request_meta=first_ok_meta or {},
        )
        # Sprint B (REVIEW-2026-09-04): the per-run raw responses were
        # collected but never attached, so the history layer's
        # getattr(result, "_raws") read a dead attribute and the
        # raw_responses table only ever saw the joined-truncated string.
        # ExtractResult is a plain (non-slots) dataclass, so the attribute
        # can be attached here.
        merged_res._raws = list(raws)
        # Sprint B: carry the resolved mode for history persistence parity
        # with gui.py (see _on_result / _current_mode).
        try:
            merged_res._mode = mode
        except Exception:
            pass
        self.finished_ok.emit(merged_res)


# NOTE: ConnTestWorker was removed — it was dead code (ProvidersPage
# defines its own inline _Worker instead).


# ---------------------------------------------------------------------------
# Phylogenetic tree renderer
# ---------------------------------------------------------------------------
if HAS_PHYLO_TREE_WIDGET:
    class PhyloTreeWidget(QWebEngineView):
        """Embeds rca_core/resources/phylogenetic_tree.html in a WebEngine view.

        The HTML template exposes ``window.setTreeData(jsonData)`` which the
        D3.js renderer consumes to draw an interactive phylogenetic tree.
        ``set_data(data)`` serialises the Python payload to JSON and invokes
        that function via ``runJavaScript()`` — no Python <-> JS bridging
        beyond the JSON wire format.

        The widget is fully independent from the table-based renderers used
        for range charts; the two share no state. The HTML is loaded as a
        ``file://`` URL resolved through ``importlib.resources`` so the
        template works the same in dev (``python main.py --ui fluent``) and
        when the project is installed as a wheel.

        Defensive notes:
          * Local content can load the D3.js CDN script because we leave
            ``LocalContentCanAccessRemoteUrls`` allowed (otherwise the
            tree renders blank with a CSP error in the dev console).
          * ``loadFinished`` is used to defer ``set_data()`` until after the
            template's window globals are bound; calling ``setTreeData`` on
            a half-loaded page silently no-ops.
        """

        _TEMPLATE_NAME = "phylogenetic_tree.html"

        def __init__(self, parent=None):
            super().__init__(parent)
            # Keep a reference to the latest payload so a set_data() call
            # made before the page finishes loading is replayed once the
            # HTML template is ready — without this, the first extraction
            # result after window-open would be silently dropped.
            self._pending_data = None
            self._page_ready = False
            # Enable the minimum features the D3.js template needs. We do
            # NOT enable JS navigation guards or anything that would let the
            # embedded page reach the host filesystem beyond the template.

            # FRONTEND-FIX (2026-07-27, #7): give the embedded page a
            # persistent QWebEngineProfile so its localStorage /
            # sessionStorage survive restarts on file://. Without an explicit
            # persistent profile QWebEngine uses an off-the-record one and
            # storage is lost on close. Scoped under AppDataLocation so
            # settings/theme/key persistence works across launches. No
            # registerJsObject / QWebChannel is introduced (intentionally).
            try:
                from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
                from PySide6.QtCore import QStandardPaths
                _storage = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
                if not _storage:
                    _storage = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer", "web")
                _profile = QWebEngineProfile("RangeChartAnalyzer", self)
                _profile.setPersistentStoragePath(os.path.join(_storage, "web"))
                self.setPage(QWebEnginePage(_profile, self))
            except Exception:
                # Defensive: if the Qt build lacks profile/page support, fall
                # back to the default page; the attribute settings below still
                # apply.
                pass

            settings = self.settings()
            # Qt6 moved WebEngine attributes into the ``WebAttribute`` enum and
            # renamed ``LocalContentCanAccessLocalResources`` to
            # ``LocalContentCanAccessFileUrls``. Resolve defensively so this
            # works across PySide2/PySide6 builds without AttributeError.
            _WebAttr = getattr(QWebEngineSettings, "WebAttribute", QWebEngineSettings)
            settings.setAttribute(_WebAttr.JavascriptEnabled, True)
            settings.setAttribute(_WebAttr.LocalContentCanAccessRemoteUrls, True)
            for _name in ("LocalContentCanAccessFileUrls",
                          "LocalContentCanAccessLocalResources"):
                _attr = getattr(_WebAttr, _name, None)
                if _attr is not None:
                    settings.setAttribute(_attr, True)
                    break
            # FRONTEND-FIX (#7): allow the embedded page to use
            # localStorage/sessionStorage at all (default is off). Required
            # for the frontend's per-session settings (theme, API key) to
            # persist on file://.
            _lsAttr = getattr(_WebAttr, "LocalStorageEnabled", None)
            if _lsAttr is not None:
                settings.setAttribute(_lsAttr, True)
            self.loadFinished.connect(self._on_load_finished)
            self._load_template()

        # -- template loading -------------------------------------------------
        def _resolve_template_url(self) -> QUrl:
            """Return a file:// URL pointing at the bundled HTML template.

            Uses ``importlib.resources.files()`` so the lookup works both
            when running from a source checkout and when rca_core is
            installed as a package. Falls back to a direct path lookup for
            editable installs that don't expose the data through the loader.
            """
            import importlib.resources as _resources
            try:
                resource = _resources.files("rca_core.resources").joinpath(
                    self._TEMPLATE_NAME)
                # Traversable.as_file() gives a real path on disk for
                # file:// loading; on Python 3.12+ files() returns a
                # MultiplexedPath that already exposes .read_text().
                with _resources.as_file(resource) as on_disk:
                    return QUrl.fromLocalFile(str(on_disk))
            except Exception as exc:
                # Bug-12 fix: surface the lookup failure so a missing
                # template doesn't produce a blank tree with no trace.
                log.warning(
                    "PhyloTreeWidget template lookup via importlib failed "
                    "(%s); falling back to relative path", exc)
                here = os.path.dirname(os.path.abspath(__file__))
                return QUrl.fromLocalFile(os.path.join(
                    here, "rca_core", "resources", self._TEMPLATE_NAME))

        def _load_template(self) -> None:
            self.setUrl(self._resolve_template_url())

        def _on_load_finished(self, ok: bool) -> None:
            self._page_ready = bool(ok)
            if ok and self._pending_data is not None:
                self._dispatch_data(self._pending_data)
                self._pending_data = None

        # -- public API -------------------------------------------------------
        def set_data(self, data: object) -> None:
            """Render ``data`` (a JSON-serialisable Python object).

            If the underlying page hasn't finished loading yet, the payload
            is stashed and dispatched on ``loadFinished``. This makes the
            widget safe to call immediately after construction.
            """
            if self._page_ready:
                self._dispatch_data(data)
            else:
                self._pending_data = data

        def clear(self) -> None:
            """Reset the pending queue; the rendered tree keeps its last
            state until the next ``set_data()`` call. Provided so callers
            can drop a stale result without triggering a render."""
            self._pending_data = None

        # -- internals --------------------------------------------------------
        def _dispatch_data(self, data: object) -> None:
            # json.dumps with default=str defends against odd datetime /
            # Decimal values slipping in from upstream; the HTML side
            # treats it as opaque JSON.
            import json as _json
            try:
                payload = _json.dumps(data, ensure_ascii=False, default=str)
            except (TypeError, ValueError) as exc:
                log.error("PhyloTreeWidget: payload not JSON-serialisable: %s", exc)
                return
            # Wrap in an IIFE so any exception inside setTreeData is
            # surfaced as a console error rather than silently swallowed
            # by runJavaScript's promise chain.
            script = (
                "(function(){try{if(typeof window.setTreeData==='function')"
                f"{{window.setTreeData({payload});}}else{{console.error("
                "'PhyloTreeWidget: window.setTreeData is not defined');"
                "}}return null;}}catch(e){{console.error("
                f"'PhyloTreeWidget setTreeData threw:',e);return null;}})()"
            )
            self.page().runJavaScript(script)


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
        # Sprint B (REVIEW-2026-09-04): id of the history record currently
        # loaded into the page (set by load_result). Reset to None whenever
        # a NEW extraction result arrives (see _on_result) so "Apply Edits"
        # after a fresh extraction can never write into a stale record.
        self._loaded_history_id = None
        # Generation counter for stale-result guarding. Bumped on every new
        # extraction start and on reset; the worker stamps the launch value on
        # its result, and _on_result drops results whose generation no longer
        # matches the live one (i.e. the user moved on).
        self._extract_gen = 0

        self.setObjectName("extractPage")
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea{border:none;background:transparent}")
        root = QWidget()
        self.setWidget(root)

        # Root: horizontal split via QSplitter so the user can drag the
        # divider. qfluentwidgets CardWidget + QVBoxLayout for each side.
        self._split = QSplitter(Qt.Horizontal)
        self._split.setContentsMargins(20, 16, 20, 16)
        self._split.setHandleWidth(6)
        self._split.setStretchFactor(0, 1)  # left: input panel
        self._split.setStretchFactor(1, 2)  # right: results panel (wider)

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
        self.preview.setCursor(Qt.PointingHandCursor)
        # UI-REVIEW-2026-09-05: the empty preview doubles as a dropzone —
        # dashed border + hint text + click-to-choose + drag-and-drop.
        # The previous flat bordered BodyLabel gave no affordance at all.
        self.preview.setText(self._t("image.dropHint"))
        self._style_preview_empty()
        self.preview.mousePressEvent = lambda _e: self._choose_image()
        ic.addWidget(self.preview)
        self.setAcceptDrops(True)

        # UI-REVIEW-2026-09-05: chart type / chart language inline next to
        # the image. The Settings page stays the single source of truth —
        # both combo pairs mirror each other (see attach_settings_selectors,
        # called by the main window once the settings page exists) — so
        # win.chart_type() / win.chart_lang() are unchanged.
        self._selector_sync = False
        sel_row = QHBoxLayout()
        self.lbl_ctype_inline = CaptionLabel(self._t("settings.chartType"))
        self.cmb_ctype_inline = ComboBox()
        self.lbl_clang_inline = CaptionLabel(self._t("settings.chartLang"))
        self.cmb_clang_inline = ComboBox()
        self.cmb_ctype_inline.currentIndexChanged.connect(self._on_inline_ctype)
        self.cmb_clang_inline.currentIndexChanged.connect(self._on_inline_clang)
        sel_row.addWidget(self.lbl_ctype_inline)
        sel_row.addWidget(self.cmb_ctype_inline, 1)
        sel_row.addWidget(self.lbl_clang_inline)
        sel_row.addWidget(self.cmb_clang_inline, 1)
        ic.addLayout(sel_row)
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

        self._split.addWidget(left_panel)

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
        # Wrap edit_row in a QWidget so the entire row can be hidden in
        # one setVisible() call when switching to a non-tabular mode
        # (e.g. phylogenetic_tree, which has no row-level edits).
        self.edit_row_widget = QWidget()
        self.edit_row_widget.setLayout(edit_row)
        right_lay.addWidget(self.edit_row_widget)

        # Storage for the inner TableWidget per table id (so apply edits
        # can read the cells back). The scroll area is also kept in
        # self.tables for show/hide.
        self._table_widgets: dict[str, object] = {}
        self._active_table_id: str = ""
        self._last_snapshot: dict | None = None  # for discard
        # UI-REVIEW-2026-09-05: export + row-edit actions are meaningless
        # without a result — start disabled and let _update_result_actions()
        # drive them from the result lifecycle.
        self._update_result_actions()

        self.stack = QFrame()
        # QStackedLayout (not QVBoxLayout) so only the current pivot's
        # table is rendered. The previous VBox+setVisible() pattern broke
        # on Windows when qfluentwidgets' Pivot didn't fire onClick,
        # leaving the right panel empty after the first render.
        self.stack_lay = QStackedLayout(self.stack)
        self.stack_lay.setContentsMargins(0, 0, 0, 0)
        self.tables = {}
        right_lay.addWidget(self.stack, 1)

        # Phylogenetic tree renderer: replaces the table stack when the
        # current mode is ``phylogenetic_tree``. The widget is created
        # once and reused across extractions; set_data() on the widget
        # buffers payloads until the WebEngine page finishes loading.
        # If WebEngine isn't available (slim Qt build), the widget is
        # None and the tree mode falls back to the empty-state page.
        self.phylotree: object | None = None
        if HAS_PHYLO_TREE_WIDGET:
            self.phylotree = PhyloTreeWidget()
            self.phylotree.setVisible(False)
            right_lay.addWidget(self.phylotree, 1)

        self._split.addWidget(right_panel)

        # Mount the splitter as the sole child of root
        root_lay = QHBoxLayout(root)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.addWidget(self._split)

    # ---- image ----
    def _style_preview_empty(self) -> None:
        """Dashed-border hint look for the dropzone (theme-aware)."""
        from qfluentwidgets import isDarkTheme
        if isDarkTheme():
            self.preview.setStyleSheet(
                "border:2px dashed rgba(255,255,255,0.25);border-radius:8px;"
                "background:rgba(255,255,255,0.03);color:rgba(255,255,255,0.55);")
        else:
            self.preview.setStyleSheet(
                "border:2px dashed rgba(0,0,0,0.22);border-radius:8px;"
                "background:rgba(0,0,0,0.02);color:rgba(0,0,0,0.45);")

    def _style_preview_loaded(self) -> None:
        from qfluentwidgets import isDarkTheme
        if isDarkTheme():
            self.preview.setStyleSheet(
                "border:1px solid rgba(255,255,255,0.12);border-radius:8px;")
        else:
            self.preview.setStyleSheet(
                "border:1px solid rgba(0,0,0,0.08);border-radius:8px;")

    def dragEnterEvent(self, e) -> None:  # noqa: N802 (Qt naming)
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802 (Qt naming)
        if not e.mimeData().hasUrls():
            return
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                self._cleanup_paste_tmp()
                self._load_image(path)
                e.acceptProposedAction()
                return

    def attach_settings_selectors(self, settings_page) -> None:
        """Fill the inline chart selector combos and wire two-way sync.

        Called by the main window after SettingsPage exists (ExtractPage is
        constructed first). Settings stays the source of truth: both combo
        pairs mirror each other and win.chart_type()/chart_lang() keep
        reading the Settings page.
        """
        sp = settings_page
        self.cmb_ctype_inline.addItems([self._t(k) for k in sp._ctype_keys])
        self.cmb_ctype_inline.setCurrentIndex(sp.cmb_ctype.currentIndex())
        self.cmb_clang_inline.addItems(sp._clang_names())
        self.cmb_clang_inline.setCurrentIndex(sp.cmb_clang.currentIndex())
        sp.cmb_ctype.currentIndexChanged.connect(self._on_settings_ctype)
        sp.cmb_clang.currentIndexChanged.connect(self._on_settings_clang)

    def _on_inline_ctype(self, idx: int) -> None:
        if self._selector_sync:
            return
        self._selector_sync = True
        try:
            sp = self.win.settings_page
            if sp.cmb_ctype.currentIndex() != idx:
                sp.cmb_ctype.setCurrentIndex(idx)
        finally:
            self._selector_sync = False

    def _on_inline_clang(self, idx: int) -> None:
        if self._selector_sync:
            return
        self._selector_sync = True
        try:
            sp = self.win.settings_page
            if sp.cmb_clang.currentIndex() != idx:
                sp.cmb_clang.setCurrentIndex(idx)
        finally:
            self._selector_sync = False

    def _on_settings_ctype(self, idx: int) -> None:
        if self._selector_sync:
            return
        self._selector_sync = True
        try:
            if self.cmb_ctype_inline.currentIndex() != idx:
                self.cmb_ctype_inline.setCurrentIndex(idx)
        finally:
            self._selector_sync = False

    def _on_settings_clang(self, idx: int) -> None:
        if self._selector_sync:
            return
        self._selector_sync = True
        try:
            if self.cmb_clang_inline.currentIndex() != idx:
                self.cmb_clang_inline.setCurrentIndex(idx)
        finally:
            self._selector_sync = False

    def _update_result_actions(self) -> None:
        """Enable export / row-edit actions only when a result exists."""
        has = bool(self.result)
        for b in (self.btn_export, self.btn_export_xlsx,
                  self.btn_add_row, self.btn_del_row,
                  self.btn_discard, self.btn_apply_edits):
            b.setEnabled(has)

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
            b64, mime, w, h, resized, decode_error = load_image_b64(path, self.win.max_edge(), enhance=self.win.enhance())
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
        self.preview.setText("")
        if not pix.isNull():
            self._style_preview_loaded()
            w = max(80, self.preview.width()) if self.preview.width() > 0 else 300
            self.preview.setPixmap(pix.scaled(
                QSize(w, 172),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.preview.setText(self._t("image.dropHint"))
            self._style_preview_empty()

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
        # Audit fix (LOW): unlink the empty mkstemp file if img.save fails
        # so we don't leak an orphan rca_paste_*.png on every error. gui.py
        # already does this; mirror that here.
        try:
            try:
                img.save(tmp)
            except Exception:
                InfoBar.error("", self._t("err.imageRead"), parent=self.win,
                              position=InfoBarPosition.TOP)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return
            self._cleanup_paste_tmp()
            # REVIEW-2026-11-07 (low): register tmp as the owned paste file
            # only AFTER _load_image succeeded. Before, a failed load left
            # _last_paste_tmp == tmp, so the except block below skipped the
            # unlink and the orphan rca_paste_*.png persisted until the NEXT
            # paste.
            self._load_image(tmp)
            self._last_paste_tmp = tmp
            InfoBar.success("", self._t("image.pasted"), parent=self.win,
                            position=InfoBarPosition.TOP)
        except Exception:
            # Any unexpected exception after mkstemp: still try to clean up
            # the tmp file (if it was never assigned to _last_paste_tmp)
            # so a paste-error path can't accumulate orphans either.
            try:
                if tmp and (not getattr(self, "_last_paste_tmp", None)
                            or self._last_paste_tmp != tmp):
                    os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- extraction ----
    def _set_busy(self, busy):
        self.btn_extract.setEnabled(not busy)
        self._sync_export_buttons(busy)
        self.spinner.setVisible(busy)
        if busy:
            self.lbl_status.setText(self._t("status.loading"))

    def _sync_export_buttons(self, busy):
        enabled = self.result is not None and not busy
        self.btn_export.setEnabled(enabled)
        self.btn_export_xlsx.setEnabled(enabled)

    def _bump_extract_gen(self) -> int:
        """Increment and return the stale-result generation counter.

        Audit fix (LOW): this is now called from every code path that
        should cancel a previously-launched worker's result, so the
        stale-result guard is real (not inert). The counter was
        previously only incremented inside _on_extract; that meant the
        guard condition `_extract_gen == launch_gen` was always true
        because busy serialised extractions, and the dead-code branch
        described by the original comments could never trigger.
        """
        self._extract_gen = getattr(self, "_extract_gen", 0) + 1
        return self._extract_gen

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
        # Audit fix (HIGH parity gap): validate the effective endpoint BEFORE
        # launching ExtractWorker so a provider pointing at http://127.0.0.1
        # or https://169.254.169.254 is rejected instead of being POSTed
        # with image + API key. Mirrors the guards in gui.py:_on_extract and
        # server.py:_handle_extract.
        active_endpoint = self.win.endpoint()
        ok, why = _validate_extract_endpoint(active_endpoint)
        if not ok:
            msg = f"Invalid endpoint: {why}"
            log.warning("Rejecting extract: %s", why)
            InfoBar.error("", msg, parent=self.win,
                          position=InfoBarPosition.TOP, duration=6000)
            self.lbl_status.setText(msg)
            return
        params = dict(
            api_key=legacy_key, image_b64=self.image_b64,
            media_type=self.media_type or "image/png",
            caption=self.txt_caption.toPlainText().strip(),
            chart_lang=self.win.chart_lang(),
            base_url=active_endpoint, model=self.win.model(),
            max_tokens=self.win.max_tokens(), provider=provider,
            # Sprint B (REVIEW-2026-09-04): wire the extraction timeout
            # into params — the worker's collection budget used to read
            # params.get("timeout_sec", ...) although no caller ever set
            # the key. See EXTRACT_TIMEOUT_SEC above (no settings entry
            # exists for it yet).
            timeout_sec=EXTRACT_TIMEOUT_SEC,
        )
        runs = self.win.runs()
        mode = self.win.chart_type()
        auto_filename = self.image_path or ""
        # UI-REVIEW-2026-09-07: when mode == "auto" it is resolved INSIDE
        # the worker thread (two-stage: caption keywords, then vision
        # classification of the image). Resolving here on the UI thread
        # would freeze the window for the length of a vision round-trip —
        # the same class of bug the settings-page test-connection fix
        # addressed.
        self.busy = True
        self._set_busy(True)
        # FIX (stale-result guard): bump the generation so any result from a
        # previously-launched worker is dropped. The closure captures the
        # launch-time gen; on result, it bails unless the live gen still matches
        # (i.e. the user didn't start a new extraction or reset meanwhile).
        # Also bumped on reset/load — see _bump_extract_gen(). Audit fix:
        # the prior code only bumped here, so the guard was effectively
        # inert because busy serialised extractions; bump on every state
        # change that should cancel an in-flight result.
        launch_gen = self._bump_extract_gen()
        self._worker = ExtractWorker(params, mode, runs,
                                     auto_filename=(self.image_path or ""))
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(
            lambda res, _g=launch_gen: (self._on_result(res)
                                        if getattr(self, "_extract_gen", 0) == _g else None))
        self._worker.start()

    def _on_progress(self, text):
        # Granular stages: submitting → uploading → thinking → parsing
        STAGE_KEYS = {
            "classifying": "status.classifying",
            "submitting": "status.submitting",
            "uploading":   "status.uploading",
            "thinking":    "status.thinking",
            "parsing":     "status.parsing",
        }
        if text in STAGE_KEYS:
            self.lbl_status.setText(self._t(STAGE_KEYS[text]))
        elif text.startswith("analyzing:"):
            self.lbl_status.setText(self._t("status.loading") + " (" + text.split(":", 1)[1] + ")")
        else:
            self.lbl_status.setText(self._t("status.loading"))

    def _on_result(self, result):
        self.busy = False
        self._set_busy(False)
        self.lbl_status.setText(self._t("status.parsing"))
        # Stale-result guard lives in the worker connection closure in
        # run_extraction() — it drops results whose generation no longer matches
        # the live one. Nothing to check here; just render.
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
        # Sprint B (REVIEW-2026-09-04): a fresh extraction replaces whatever
        # history record was loaded into the page. Clear the id NOW (before
        # persistence below) so a later "Apply Edits" updates/creates a
        # record for THIS extraction instead of silently overwriting the
        # record the user had merely been viewing (e.g. #42).
        self._loaded_history_id = None
        # UI-REVIEW-2026-09-05: a fresh result enables export / row edits.
        self._update_result_actions()
        # Push the new payload into the phylogenetic-tree widget so the
        # embeds the result as soon as the WebEngine page is ready (the
        # widget buffers the payload across the page-load race). This
        # is the "Task 9 step 4" entry point — _render_result() also
        # calls set_data() in the tree branch, but pushing here means
        # a reload via load_result() (which doesn't go through
        # _on_result()) still gets the latest data on the next render.
        if self.phylotree is not None:
            self.phylotree.set_data(self.result)
        # Step 0 (8.0→9.5): surface chimera_warnings as a visible warning
        # so the operator knows when a merged row was not observed in any single run.
        chimera_warnings = (self.result or {}).get("chimera_warnings") or []
        if chimera_warnings:
            n = len(chimera_warnings)
            msg = (self._t("results.chimera_warning")
                   .replace("{n}", str(n)))
            InfoBar.warning(
                "", msg,
                parent=self.win,
                position=InfoBarPosition.TOP, duration=8000,
            )
        # Audit fix (LOW): a render exception must not be swallowed by the
        # PySide signal machinery. Show an error toast + log so the user
        # sees something went wrong (even though self.result is set and
        # Export still operates on the data).
        try:
            self._render_result()
        except Exception as exc:
            log.exception("ExtractPage._render_result raised in _on_result")
            msg = f"Render failed: {exc}"
            try:
                InfoBar.error("", msg, parent=self.win,
                              position=InfoBarPosition.TOP, duration=6000)
            except Exception:
                pass
            self.lbl_status.setText(msg)
            return
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
                # Phase J fix: propagate the audit-trail fields populated
                # by rca_core.extractor (image_sha256, request_meta)
                # so GUI records carry the same provenance as the
                # server-side path. Previously these stayed empty,
                # defeating the 5-year audit trail.
                image_sha256=getattr(result, "image_sha256", "") or "",
                request_meta=getattr(result, "request_meta", None) or {},
                raws=getattr(result, "_raws", None)
            )
        except Exception as exc:
            # Persistence is best-effort: a failure here must not block the
            # user from seeing their result.
            log.warning("persist post-result failed: %s", exc)
        # Auto-switch to Extract sub-interface + scroll so the result tables
        # are immediately visible (the user is looking at the loading spinner
        # area otherwise).
        from PySide6.QtCore import QTimer as _Q
        def _jump():
            try:
                # FIX (auto-jump guard): only switch back to the Extract page
                # if the user is still looking at it. If they already navigated
                # to History/Usage/Settings during the wait, don't yank them
                # back — the singleShot timer can't be cancelled, so guard
                # here instead.
                if getattr(self.win, "stackedWidget", None) is None:
                    return
                if self.win.stackedWidget.currentWidget() is not self:
                    return
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
        if ct in ("range_chart", "columnar_section", "abundance_diagram",
                  "phylogenetic_tree"):
            return ct
        # Auto-detect by result shape. Phylogenetic-tree results carry a
        # ``nodes`` list (the primary list_key for PHYLOGENETIC_TREE_SCHEMA);
        # detect that before the columnar/abundance fallbacks so the tree
        # renderer is used when the user runs an "auto" extraction on a
        # tree image.
        if isinstance(self.result, dict):
            if isinstance(self.result.get("nodes"), list):
                return "phylogenetic_tree"
            if isinstance(self.result.get("abundances"), list):
                return "abundance_diagram"
            # UI-REVIEW-2026-09-07: zonation chart payloads must not be
            # mislabeled as range_chart in saved history records.
            if isinstance(self.result.get("correlations"), list)                     and self.result["correlations"]:
                return "zonation_chart"
            zns = self.result.get("zones")
            if isinstance(zns, list) and zns and isinstance(zns[0], dict)                     and ("rank" in zns[0] or "zonation" in zns[0]):
                return "zonation_chart"
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
        # Trees: the PhyloTreeWidget replaces the table stack & pivot
        # because the phylogenetic-tree data shape (nodes + root_ids +
        # legend + metadata) doesn't map to row/column tables. The
        # widget is reused across extractions; set_data() buffers the
        # payload if the WebEngine page hasn't finished initial load.
        is_tree_mode = (self._current_mode() == "phylogenetic_tree"
                        and self.phylotree is not None)
        for w in list(self.tables.values()):
            w.setParent(None)
            w.deleteLater()
        self.tables = {}
        # P1-2 fix: clear _table_widgets and delete old widgets to prevent
        # memory accumulation. Without this, old TableWidget instances are
        # retained indefinitely and _on_apply_edits iterates stale references.
        for table in list(self._table_widgets.values()):
            if table is not None and hasattr(table, 'deleteLater'):
                table.deleteLater()
        self._table_widgets = {}
        self.pivot.clear()
        while self.stack_lay.count():
            it = self.stack_lay.takeAt(0)
            if it.widget():
                it.widget().setParent(None)
        # Friendly empty state when the model returned no data at all.
        total_rows = sum(len((self.result or {}).get(c["id"], []) or []) for c in configs)
        if total_rows == 0:
            # In tree mode the empty-state guard doesn't apply (the tree
            # widget can render even with zero nodes — it just shows an
            # empty graph). Skip directly to the tree branch.
            if is_tree_mode:
                self._show_phylotree()
                return
            # REVIEW-2026-09-10: restore the table UI here too — mirroring the
            # table branch below (`if self.phylotree is not None`, since the
            # widget is absent in slim Qt builds). This branch returned early
            # without undoing what _show_phylotree() hid, so loading a
            # zero-row record after viewing a tree left the PREVIOUS chart's
            # tree on screen while the status line showed the newly loaded
            # record — the operator was looking at the wrong figure.
            if self.phylotree is not None:
                self.phylotree.setVisible(False)
            self.pivot.setVisible(True)
            self.edit_row_widget.setVisible(True)
            self.stack.setVisible(True)
            self.pivot.clear()
            self.pivot.addItem(
                routeKey="empty",
                text=self._t("results.empty"),
                onClick=lambda _=False: None,
            )
            empty = BodyLabel(self._t("results.empty"))
            empty.setAlignment(Qt.AlignCenter)
            hint = CaptionLabel(self._t("results.emptyHint"))
            hint.setAlignment(Qt.AlignCenter)
            # QStackedLayout has no addSpacing/addStretch; use a VBox widget
            # as the empty-state page so we can add spacing between the two
            # labels.
            empty_page = QWidget()
            ep = QVBoxLayout(empty_page)
            ep.setContentsMargins(0, 20, 0, 0)
            ep.addWidget(empty)
            ep.addWidget(hint)
            self.stack_lay.addWidget(empty_page)
            self._refresh_conf()
            return
        # FIX (HIGH-1): clear the pivot before repopulating. Previously this
        # method only *added* items, so a retranslate() that called
        # _render_result() followed by _rebuild_pivot() (which also clears +
        # adds) would temporarily double-populate the pivot. During that
        # window, currentItemChanged could fire _show_table on a stale scroll
        # widget that _rebuild_pivot had already takeAt()'d → wrapped-C++ crash
        # on some qfluent builds. Clearing here makes _render_result the
        # single source of pivot population and lets retranslate call it
        # alone.
        if is_tree_mode:
            # Table content is irrelevant for the tree — render the tree
            # and bail out before building any pivots/scroll areas.
            self._show_phylotree()
            return
        # Table mode: ensure the tree is hidden and the table UI is
        # visible. (If we just switched from tree mode, the tree widget
        # would still be visible otherwise.)
        if self.phylotree is not None:
            self.phylotree.setVisible(False)
        self.pivot.setVisible(True)
        self.edit_row_widget.setVisible(True)
        self.stack.setVisible(True)
        self.pivot.clear()
        # P1-2 (REVIEW-2026-07-25): clean up old table widgets before building new ones.
        # Previously _table_widgets was never cleared, so a second _render_result call
        # (e.g., after a new extraction) left stale widgets in memory with dangling
        # signal connections. The next "Apply Edits" would operate on the dead widget
        # and crash. Now we delete the old scroll area and inner table widget.
        for old_scroll in list(self.tables.values()):
            if old_scroll is not None:
                old_scroll.deleteLater()
        self.tables.clear()
        for old_table in list(self._table_widgets.values()):
            if old_table is not None:
                old_table.deleteLater()
        self._table_widgets.clear()
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
            # FIX (H1): block signals during the bulk fill so that setItem()
            # calls never fire itemChanged. Without this, the handler would
            # run on the very first setItem and (a) mark the table dirty and
            # (b) freeze a snapshot BEFORE every row is filled — corrupting
            # the Undo baseline with a half-built table. Re-enable signals
            # immediately after the loop; real user edits then flow through.
            table.blockSignals(True)
            try:
                for ri, item in enumerate(items):
                    if not isinstance(item, dict):
                        continue
                    cells = cfg["row"](item)
                    # Truncate/pad to the DATA-column count (n_cols - 1, since the
                    # leading "#" index column is prepended below). The previous
                    # `[:n_cols]` over-counted by one and could drop the last data
                    # column when the row extractor returned fewer values than
                    # expected.
                    n_data = max(1, n_cols - 1)
                    cells = (list(cells) + [""] * n_data)[:n_data]
                    values = [str(ri + 1)] + ["" if c is None else str(c) for c in cells]
                    # cc-switch style: flag low-agreement rows (multi-run merge).
                    # Audit fix (LOW): coerce agreement_count defensively so a
                    # hand-edited or history-loaded result carrying a string
                    # ('2.0') or non-numeric value cannot raise ValueError and
                    # silently abort the entire _render_result slot (table
                    # signals would have stayed blocked forever).
                    low = False
                    if multi and cfg["id"] == "species_ranges":
                        try:
                            ac = float(item.get("agreement_count", 0) or 0)
                            low = int(ac) <= n_runs / 2
                        except (TypeError, ValueError):
                            low = False
                    for ci, val in enumerate(values):
                        if ci >= n_cols:
                            break   # extra safety against row lambda drift
                        cell = QTableWidgetItem(val)
                        if low:
                            cell.setBackground(Qt.yellow)
                        table.setItem(ri, ci, cell)
            finally:
                # Audit fix (LOW): always re-enable signals so a malformed row
                # that raises mid-loop cannot leave the table's signals
                # permanently blocked. Without this try/finally the user's
                # edits would silently never reach _on_table_item_changed.
                table.blockSignals(False)
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

    def _show_phylotree(self) -> None:
        """Switch the right panel to the phylogenetic-tree renderer.

        Hides the table-specific UI (pivot + edit row + table stack) and
        pushes ``self.result`` into the PhyloTreeWidget. The widget buffers
        the payload if the embedded WebEngine page hasn't finished loading
        yet (Task 8 design), so it's safe to call this immediately after
        construction; the tree will render as soon as the template is ready.
        """
        if self.phylotree is None:
            # WebEngine unavailable — the user sees whatever the right
            # panel currently shows. Fall through silently rather than
            # crashing in slim Qt builds.
            log.warning("PhyloTreeWidget unavailable; skipping tree render")
            self._refresh_conf()
            return
        self.pivot.setVisible(False)
        self.edit_row_widget.setVisible(False)
        self.stack.setVisible(False)
        self.phylotree.setVisible(True)
        # Tree has no row-level edits; drop any stale snapshot / dirty
        # badge so a later switch back to a table mode doesn't replay
        # an edit against the wrong baseline.
        self._last_snapshot = None
        try:
            self.lbl_dirty.setText("")
        except Exception:
            pass
        self.phylotree.set_data(self.result)
        self._refresh_conf()

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
        self._update_result_actions()
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
                persisted = self._persist_edits_to_history()
            except Exception as exc:
                log.warning("persist edits failed: %s", exc)
                persisted = False
            # REVIEW-2026-09-10: only claim success when the write actually
            # landed (see the return contract in _persist_edits_to_history).
            if persisted:
                InfoBar.success(
                    "", self._t("edit.saved"), parent=self.win,
                    position=InfoBarPosition.TOP, duration=2000,
                )

    def _persist_edits_to_history(self) -> bool:
        """Update the most recent history record with the current result.

        Returns True when the edit was persisted, False when it was not
        (missing row / write error) — the caller must not report success
        on False.

        Best-effort: prefers the record we were loaded from (when the user
        reopened a historical entry via the History page) so the same row
        round-trips. Sprint B (REVIEW-2026-09-04): ``_loaded_history_id``
        is now reset to None by _on_result when a fresh extraction lands,
        so the fallback path below runs for fresh extractions — newest
        record for this image path, or (when nothing matches) a brand-new
        record. Previously a stale loaded id survived a new extraction and
        "Apply Edits" silently overwrote the record the user had only been
        viewing.
        """
        hs = getattr(self.win, "_history_store", None)
        if hs is None:
            return
        loaded_id = getattr(self, "_loaded_history_id", None)
        if loaded_id is not None:
            try:
                # REVIEW-2026-09-10: update_result returns False (it does not
                # raise) when the row is gone — e.g. the user deleted the
                # record on the History page after loading it. The return value
                # was ignored, so the caller showed the green "edit.saved"
                # toast while nothing had been written at all. Report it so the
                # caller can tell the user the edit was NOT persisted.
                if not hs.update_result(loaded_id, self.result):
                    InfoBar.warning(
                        "", self._t("edit.historyMissing"), parent=self.win,
                        position=InfoBarPosition.TOP, duration=5000,
                    )
                    return False
            except Exception as exc:
                InfoBar.error(
                    "", f"保存历史记录失败: {exc}",
                    parent=self.win,
                    position=InfoBarPosition.TOP, duration=5000,
                )
                return False
            return True
        if not self.image_path:
            # Sprint B: no loaded record and no source image to match
            # against — persist the edited result as a NEW record instead
            # of dropping the edits on the floor.
            self._save_new_history_record()
            return True
        records = hs.list(limit=20, search=os.path.basename(self.image_path))
        if not records:
            self._save_new_history_record()
            return True
        try:
            if not hs.update_result(records[0].id, self.result):
                InfoBar.warning(
                    "", self._t("edit.historyMissing"), parent=self.win,
                    position=InfoBarPosition.TOP, duration=5000,
                )
                return False
        except Exception as exc:
            InfoBar.error(
                "", f"保存历史记录失败: {exc}",
                parent=self.win,
                position=InfoBarPosition.TOP, duration=5000,
            )
            return False
        return True

    def _save_new_history_record(self) -> None:
        """Sprint B (REVIEW-2026-09-04): create a fresh history record for
        the current (edited) result. Best-effort — failures are logged,
        never raised into the caller's edit flow."""
        try:
            provider = self.win.current_provider()
        except Exception:
            provider = None
        try:
            self.win.save_to_history(
                result=self.result,
                mode=self._current_mode(),
                image_path=self.image_path or "",
                image_thumb=self._maybe_thumbnail(),
                image_w=(self._img_dims[0] if self._img_dims else 0) or 0,
                image_h=(self._img_dims[1] if self._img_dims else 0) or 0,
                provider=provider,
                model=(provider.model if provider else "") or "",
                runs=int((self.result or {}).get("runs", 1) or 1),
                confidence=float((self.result or {}).get("confidence", 0) or 0),
                partial_failures=0,
                duration_ms=0,
                status_code=None,
                raw=self.raw_text or "",
            )
        except Exception as exc:
            log.warning("persist edits (new history record) failed: %s", exc)

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
        # Audit fix (LOW): bump the stale-result generation so any result
        # from a still-running extraction is dropped — loading from history
        # IS a state change that should cancel in-flight results.
        self._bump_extract_gen()
        self.result = result
        self._loaded_history_id = record_id
        self.raw_text = ""
        # Drop any pending edit snapshot / dirty badge — the result we
        # just loaded *is* the baseline, the old snapshot belongs to the
        # previous in-memory result.
        self._last_snapshot = None
        # UI-REVIEW-2026-09-05: the loaded result enables export / row edits.
        self._update_result_actions()
        try:
            self.lbl_dirty.setText("")
        except Exception:
            pass
        # The loaded record has no image of its own (we only stored the
        # result JSON), so wipe the ENTIRE image state to keep "Export
        # JSON" honest about which file the result came from.
        # Sprint B (REVIEW-2026-09-04): only image_path used to be
        # cleared — image_b64 / media_type / dims stayed populated, so a
        # user could hit Extract in this apparently image-less state and
        # silently send the PREVIOUS image ("ghost image" extraction).
        # The user can re-upload to extract/export with a real source.
        self.image_path = None
        self.image_b64 = None
        self.media_type = None
        self._img_dims = (0, 0, False)
        self.lbl_imginfo.setText(self._t("image.none"))
        self.preview.clear()
        self.preview.setText(self._t("image.dropHint"))
        self._style_preview_empty()
        # If the loaded result is a columnar-section shape, update the
        # mode display so the user sees the right table set.
        try:
            self._render_result()
        except Exception as exc:
            log.warning("load_result render failed: %s", exc)

    def _export_prefix(self) -> str:
        """Filename prefix reflecting the current result's chart kind."""
        mode = self._current_mode()
        if mode == "abundance_diagram":
            return "abundance_diagram_"
        if mode == "columnar_section":
            return "columnar_section_"
        if mode == "phylogenetic_tree":
            return "phylogenetic_tree_"
        if mode == "zonation_chart":
            return "zonation_chart_"
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
        # Phylogenetic-tree branch: timestamped filename, dump self.result
        # directly as formatted JSON (no extra wrapper) so the output is a
        # drop-in for downstream tree consumers.
        if self._current_mode() == "phylogenetic_tree":
            ts = time.strftime("%Y%m%dT%H%M%S")
            default_name = f"phylogenetic_tree_{ts}.json"
            path, _ = QFileDialog.getSaveFileName(
                self, self._t("dialog.saveJson"), default_name,
                "JSON (*.json)")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.result, f, ensure_ascii=False, indent=2)
                InfoBar.success("", self._t("status.saved"), parent=self.win,
                                position=InfoBarPosition.TOP)
            except Exception as exc:
                InfoBar.error("", str(exc), parent=self.win,
                              position=InfoBarPosition.TOP, duration=5000)
            return
        path, _ = QFileDialog.getSaveFileName(
            self, self._t("dialog.saveJson"), self._export_prefix() + "result.json",
            "JSON (*.json)")
        if not path:
            return
        payload = {
            "source_file": os.path.basename(self.image_path) if self.image_path else None,
            "result": self.result,
        }
        # REVIEW-2026-09-10: this branch had no error handling at all (unlike
        # the tree branch above and _export_xlsx) — a windowed app has no
        # console, so a locked/read-only target meant "nothing happens". It
        # also wrote in place, so a failure mid-dump truncated the file the
        # user already had. Write to a temp file in the same directory and
        # replace, and report both outcomes.
        try:
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
            InfoBar.success("", self._t("status.saved"), parent=self.win,
                            position=InfoBarPosition.TOP)
        except Exception as exc:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            InfoBar.error("", str(exc), parent=self.win,
                          position=InfoBarPosition.TOP, duration=5000)

    def retranslate(self):
        self.lbl_title.setText("Extract" if self._t("upload.title") == "upload.title" else self._t("upload.title"))
        self.btn_choose.setText(self._t("image.choose"))
        self.btn_paste.setText(self._t("image.paste"))
        # UI-REVIEW-2026-09-05: inline chart selectors + dropzone hint.
        self.lbl_ctype_inline.setText(self._t("settings.chartType"))
        self.lbl_clang_inline.setText(self._t("settings.chartLang"))
        sp = getattr(self.win, "settings_page", None)
        if sp is not None:
            for i, key in enumerate(sp._ctype_keys):
                self.cmb_ctype_inline.setItemText(i, self._t(key))
            for i, name in enumerate(sp._clang_names()):
                self.cmb_clang_inline.setItemText(i, name)
        if not self.image_path:
            self.preview.setText(self._t("image.dropHint"))
            self._style_preview_empty()
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
            # follow the new language. _render_result() now also rebuilds
            # the pivot (clearing it first), so we deliberately do NOT call
            # _rebuild_pivot() afterwards — that used to double-populate
            # the pivot and could crash on some qfluent builds (HIGH-1).
            self._render_result()


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

        # Sprint B (REVIEW-2026-09-04): in-flight connection-test worker
        # (kept here so the QThread is never GC'd mid-run) + in-flight
        # guard so a double-click cannot stack two probes.
        self._conn_worker = None

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
        self.spin_maxtok.setRange(1, 100000)
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
        # UI-REVIEW-2026-09-05: zonation_chart added (radiolarian
        # biozonation / correlation charts) — full-stack mode.
        self._ctype_codes = ["auto", "range_chart", "columnar_section", "abundance_diagram", "phylogenetic_tree", "zonation_chart"]
        self._ctype_keys = ["settings.chartType.auto",
                            "settings.chartType.rangeChart",
                            "settings.chartType.columnarSection",
                            "settings.chartType.abundanceDiagram",
                            "settings.chartType.phylogeneticTree",
                            "settings.chartType.zonationChart"]
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

        self.lbl_enhance = StrongBodyLabel(self._t("settings.enhance"))
        self.sw_enhance = SwitchButton()
        self.sw_enhance.setChecked(bool(cfg.get("enhance", False)))
        g2.addWidget(self.lbl_enhance, 6, 0); g2.addWidget(self.sw_enhance, 6, 1, Qt.AlignLeft)
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
        # Sprint B (REVIEW-2026-09-04): the network probe used to run
        # directly in this button slot on the UI thread (worst case ~2x10s
        # of a frozen window). Mirror the ProvidersPage QThread _Worker
        # pattern: run the probe on a worker thread, marshal the result
        # back via a signal, and disable the button while in flight so
        # the test cannot be re-triggered.
        if getattr(self, "_conn_worker", None) is not None:
            return  # a test is already running
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
        self.btn_test.setEnabled(False)
        from PySide6.QtCore import QThread as _QThread

        class _Worker(_QThread):
            # (worker, result) — carrying the worker lets the handler be a
            # BOUND method of this page. A bound QObject method gives a
            # queued (auto) connection, so the handler runs on the GUI
            # thread; a plain-lambda connection would execute in the
            # worker thread (direct connection) and touch widgets
            # cross-thread.
            done = Signal(object, object)

            def __init__(self, p, parent=None):
                super().__init__(parent)
                self._p = p

            def run(self):
                from rca_core.llm import test_llm_connection
                # Parity with the ProvidersPage probe: validate the
                # endpoint BEFORE dialling so an SSRF/cleartext-key probe
                # never leaves the machine (same shape as the providers
                # worker so the handler can render a graceful failure).
                try:
                    from rca_core.ssrf import validate_endpoint
                    ok, why = validate_endpoint(self._p.endpoint)
                    if not ok:
                        self.done.emit(self, {
                            "ok": False,
                            "error": f"bad endpoint: {why}",
                        })
                        return
                except Exception as e:
                    log.exception("settings connection-test endpoint validation failed")
                    self.done.emit(self, {
                        "ok": False,
                        "error": f"endpoint validation error: {e}",
                    })
                    return
                try:
                    self.done.emit(self, test_llm_connection(self._p, timeout_sec=10))
                except Exception as exc:  # BUG13: never let the thread die silently
                    log.exception("settings connection test failed")
                    self.done.emit(self, {"ok": False, "error": str(exc)})

        w = _Worker(current)
        self._conn_worker = w
        # Bound methods of this QObject page -> queued connections that run
        # on the GUI thread (never touch widgets from the worker thread).
        w.done.connect(self._on_test_done)
        w.finished.connect(self._on_conn_thread_finished)
        w.start()

    def _forget_conn_worker(self, worker) -> None:
        """Drop the strong ref once the thread really finished."""
        if getattr(self, "_conn_worker", None) is worker:
            self._conn_worker = None

    def _on_conn_thread_finished(self) -> None:
        """Safety net (GUI thread): restore the button if the thread ever
        finished without emitting done() — run() catches everything, so
        this should not trigger, but a stuck-disabled button is worse."""
        w = getattr(self, "_conn_worker", None)
        if w is not None and not w.isRunning():
            self._forget_conn_worker(w)
            try:
                self.btn_test.setEnabled(True)
            except RuntimeError:
                pass

    def _on_test_done(self, worker, res) -> None:
        self._forget_conn_worker(worker)
        self.btn_test.setEnabled(True)
        if res is None:
            InfoBar.error(
                "", self._t("err.http"),
                parent=self.win, position=InfoBarPosition.TOP, duration=5000,
            )
            return
        # The SSRF-guard failure path emits a plain dict; normalise it so
        # the attribute reads below cannot crash inside a Qt slot.
        if isinstance(res, dict):
            from types import SimpleNamespace as _NS
            res = _NS(ok=bool(res.get("ok")), latency_ms=0,
                      models_sample=[], status=None,
                      error_key=res.get("error") or "err.http")
        if res.ok:
            InfoBar.success(
                "",
                f"OK · {res.latency_ms} ms · "
                f"{len(res.models_sample)} models",
                parent=self.win,
                position=InfoBarPosition.TOP, duration=3000,
            )
        else:
            err_key = res.error_key or "err.http"
            msg = self._t(err_key) if err_key.startswith("err.") else (err_key or "fail")
            if res.status:
                msg += f" (HTTP {res.status})"
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
                "enhance": bool(self.sw_enhance.isChecked()),
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
            log.warning("persistent storage unavailable: %s", exc)

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
        # UI-REVIEW-2026-09-05: wire the extract page's inline chart
        # selectors to the settings combos (two-way mirror) now that the
        # settings page exists.
        self.extract_page.attach_settings_selectors(self.settings_page)
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
            log.warning("history/usage pages unavailable: %s", exc)
            self.history_page = None
            self.usage_page = None

        # Keep references to the nav items so the sidebar labels can be
        # re-translated live on language switch.
        self._nav_extract = self.addSubInterface(
            self.extract_page, FIF.PHOTO, self._nav_text("tab.extract", ""))
        if self.history_page is not None:
            self._nav_history = self.addSubInterface(
                self.history_page, FIF.HISTORY,
                self._nav_text("tab.history", ""),
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
            self.about_page, FIF.INFO, self._nav_text("tab.about", ""),
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
            log.warning("onboarding init failed: %s", exc)

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
                       status_code: int | None, raw: str,
                       image_sha256: str = "",
                       request_meta: dict | None = None,
                       raws: list[str] | None = None) -> int | None:
        """Persist a completed extraction to the SQLite history table.
        Returns the new record id, or None on failure.

        Phase J fix: ``image_sha256`` and ``request_meta`` are now first-class
        parameters so the audit trail captures the same provenance the
        server-side path writes. ``raws`` (per-run raw text, for multi-run
        mode) populates the raw_responses table so a researcher can reparse
        each slot's exact LLM response years later.
        """
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
                # Phase J: pass audit-trail fields through.
                image_sha256=image_sha256 or "",
                request_meta=dict(request_meta or {}),
            )
            new_id = self._history_store.add(
                rec,
                # Per-run raw_responses (Phase J): multi-run mode
                # passes a list of raw texts per slot. Single-run
                # mode can pass None or a single-element list.
                raw_responses=[
                    {
                        "run_idx": i,
                        "raw_text": r or "",
                        "prompt_text": "",
                        "request_meta": dict(request_meta or {}),
                        "timestamp": int(__import__("time").time()),
                    }
                    for i, r in enumerate(raws or ([raw] if raw else []))
                ] or None,
            )
        except Exception as exc:
            log.warning("save_to_history failed: %s", exc)
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
            log.warning("history refresh after save: %s", exc)
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
            log.warning("record_usage failed: %s", exc)

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
        self._nav_about.setText(self._nav_text("tab.about", ""))
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

    def enhance(self):
        return bool(self.settings_page.sw_enhance.isChecked())

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
                    # REVIEW-2026-09-10: migrate the legacy ENDPOINT and MODEL
                    # too. Only the key was carried over, so a Tkinter user
                    # configured against a proxy (cfg endpoint/model) had the
                    # key copied into the seeded MiniMax provider — from then
                    # on every extraction went to
                    # api.minimaxi.com/anthropic with model MiniMax-M3 using
                    # the proxy key, silently breaking a working setup (and
                    # the Fluent UI has no field that even shows cfg's
                    # endpoint/model to explain it).
                    legacy_endpoint = (self.cfg.get("endpoint") or "").strip()
                    legacy_model = (self.cfg.get("model") or "").strip()
                    if legacy_endpoint:
                        current.endpoint = legacy_endpoint
                    if legacy_model:
                        current.model = legacy_model
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
            log.warning("sync ipt_key to provider: %s", exc)

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
            "enhance": self.enhance(),
            "api_key": s.ipt_key.text().strip() if remember else "",
        })
        save_config(self.cfg)
        # H3 fix (REVIEW-2026-11-07): the Tkinter GUI already warned (via
        # log) when at-rest protection is below Fernet; the Fluent save
        # path had no warning at all. Surface it once per session when a
        # key is actually being stored with the weaker protection.
        if remember and s.ipt_key.text().strip():
            try:
                from rca_core.secrets_store import encryption_status
                if encryption_status() in ("fingerprint", "plaintext") \
                        and not getattr(self, "_warned_key_protection", False):
                    self._warned_key_protection = True
                    InfoBar.warning(
                        "", self._t("settings.keyObfuscated"), parent=self,
                        position=InfoBarPosition.TOP, duration=6000)
            except Exception:
                pass

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
        # destroyed widget. Disconnect the worker's signals first (so the
        # finished_ok/progress/done callbacks can't fire on a teardown page),
        # then wait a bounded time for the thread to finish.
        # Sprint B (REVIEW-2026-09-04): quit() is a NO-OP for a QThread
        # that overrides run() (there is no inner event loop), and the
        # bounded waits (7s total) are shorter than a typical LLM request,
        # so a running worker used to be abandoned while the window's
        # Python objects were torn down — when the GC later collected the
        # still-running QThread wrapper, Qt6 aborted the whole process
        # with qFatal("Destroyed while thread is still running"). We now
        # request cooperative cancellation and, if the thread is STILL
        # running after the bounded wait, park it in the module-level
        # _orphaned_workers register which holds a strong reference until
        # the thread finishes naturally (then deleteLater()s it).
        # terminate() is never used.
        try:
            w = getattr(self.extract_page, "_worker", None)
            if w is not None:
                try:
                    w.finished_ok.disconnect()
                    w.progress.disconnect()
                except (TypeError, RuntimeError):
                    pass
                if w.isRunning():
                    # Cooperative cancellation first: the multi-run path
                    # honours it before each submit / between batches.
                    try:
                        w.request_cancel()
                    except Exception:
                        pass
                    if not w.wait(5000):
                        try:
                            log.warning(
                                "ExtractWorker did not honour cancel within 5s; "
                                "leaving thread to finish without UI callbacks."
                            )
                        except Exception:
                            pass
                        if not w.wait(2000):
                            # Still inside urllib/ssl with no Python-side
                            # cancellation point: park the worker so the
                            # wrapper outlives the GC and the thread can
                            # finish safely (no qFatal, no terminate()).
                            _park_orphaned_worker(w)
                    if w.isRunning():
                        _park_orphaned_worker(w)
        except Exception:
            pass
        # Same deal for the Settings page's connection-test worker (a
        # QThread with no cancel support; its urllib timeout bounds the
        # stragglers).
        try:
            sw = getattr(self.settings_page, "_conn_worker", None)
            if sw is not None:
                try:
                    sw.done.disconnect()
                except (TypeError, RuntimeError):
                    pass
                if sw.isRunning():
                    if not sw.wait(3000):
                        _park_orphaned_worker(sw)
                if sw.isRunning():
                    _park_orphaned_worker(sw)
        except Exception:
            pass
        # Same deal for the providers page's connection-test workers.
        # _test_workers is a dict[worker, card] (changed from a single
        # _test_worker by the H1 freeze fix) — clean up every live one.
        # Sprint B (REVIEW-2026-09-04): same orphan-parking as above so a
        # test still inside its 8s network timeout can never trigger the
        # Qt6 "Destroyed while thread is still running" qFatal on GC.
        try:
            tw_dict = getattr(self.providers_page, "_test_workers", None)
            if tw_dict is not None:
                for tw in list(tw_dict.keys()):
                    try:
                        tw.done.disconnect()
                        tw.finished.disconnect()
                    except (TypeError, RuntimeError):
                        pass
                for tw in list(tw_dict.keys()):
                    if tw.isRunning():
                        if not tw.wait(2000):
                            try:
                                log.warning(
                                    "Test worker did not finish in time; "
                                    "abandoning without terminate()."
                                )
                            except Exception:
                                pass
                            if not tw.wait(500):
                                _park_orphaned_worker(tw)
                    if tw.isRunning():
                        _park_orphaned_worker(tw)
                tw_dict.clear()
        except Exception:
            pass
        self.extract_page.busy = False
        super().closeEvent(event)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    setTheme(Theme.AUTO)
    setThemeColor("#2563eb")
    win = RangeChartFluentWindow()
    # QFluentWidgets enables the Windows 11 Mica effect by default
    # (window/fluent_window.py: setMicaEffectEnabled(True)). On machines where
    # Mica isn't supported (VMs, RDP, some GPUs/drivers, Windows 10) the effect
    # silently fails and the title bar + navigation panel render SOLID BLACK.
    # Turn it off so the window falls back to the normal themed background.
    win.setMicaEffectEnabled(False)
    # With Mica off, give the title bar + navigation panel an explicit
    # (non-pure-black) background — light mode -> light gray; dark mode ->
    # proper dark gray (rgb(32,32,32)) instead of #000000.
    win.setCustomBackgroundColor(QColor(243, 246, 250), QColor(32, 32, 32))
    win.show()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
