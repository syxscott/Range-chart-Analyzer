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

import base64  # AUDIT-2026-09-27 [item 2.4]: see _show_history_thumbnail
import logging
import os
import sys
import tempfile
import time
from typing import Any  # AUDIT-2026-09-27 [item 7.2]: used in annotations below
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
    # imported locally in _show_figure_full, where the dialog is built.)
    # image card's zoom row. (QDialog/QDialogButtonBox are Qt, and are
    ToolButton,
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

# AUDIT-2026-09-27 [item 8.1]: one named QWebEngineProfile per PROCESS,
# parented to the QApplication rather than to any view. The profile has to
# outlive every page built from it — Qt destroys child QObjects in the order
# they were added, so a profile parented to the view was destroyed BEFORE the
# page that used it, producing at teardown:
#   "Release of profile requested but WebEnginePage still not deleted.
#    Expect troubles!"
# and then a native access violation. Parented to the application, it is
# destroyed last, and every window in the process also shares one profile
# instead of each creating its own under the same storage name (the test
# suites build many windows per process).
_shared_web_profile_obj = None


def _shared_web_profile():
    """Return the process-wide named profile, creating it on first use.

    Returns ``None`` when QtWebEngineCore is unavailable, so the caller can
    fall back to the default page exactly as before.
    """
    global _shared_web_profile_obj
    if _shared_web_profile_obj is not None:
        return _shared_web_profile_obj
    from PySide6.QtCore import QStandardPaths
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineCore import QWebEngineProfile
    app = QApplication.instance()
    if app is None:
        return None
    storage = QStandardPaths.writableLocation(
        QStandardPaths.AppDataLocation)
    if not storage:
        storage = os.path.join(os.path.expanduser("~"),
                              ".range_chart_analyzer", "web")
    prof = QWebEngineProfile("RangeChartAnalyzer", app)
    prof.setPersistentStoragePath(os.path.join(storage, "web"))
    _shared_web_profile_obj = prof
    return prof


def _wait_worker_briefly(w, budget_ms: int) -> bool:
    """Wait up to *budget_ms* for *w*, WITHOUT freezing the GUI thread.

    AUDIT-2026-09-27 P1: ``closeEvent`` used to call ``QThread.wait()``
    directly, which blocks the Qt event loop — no repaint, no input, no
    message pump — so closing the window during an in-flight extraction
    produced a visible "Not Responding" window for the full budget. The worst
    case was additive across workers: 5 s + 2 s for the extract worker, 3 s
    for the settings connection test, then 2 s + 0.5 s for EVERY live provider
    test, i.e. ~17.5 s with three probes running. The cooperative-cancel
    checkpoint cannot help either: ``request_cancel`` sets a flag that is only
    read *after* the blocking ``urllib`` call returns, so a single-run
    extraction always burns the whole budget.

    Pumping events inside the wait keeps the window responsive while still
    giving a worker that is about to finish the chance to be reclaimed
    cleanly. Callers pass a SHARE of one total budget rather than a per-worker
    allowance, so N workers cannot multiply the freeze.

    Returns True when the thread finished within the budget.
    """
    if w is None:
        return True
    try:
        from PySide6.QtCore import QCoreApplication, QElapsedTimer
    except Exception:  # pragma: no cover - Qt always present in this module
        return not w.isRunning()
    clock = QElapsedTimer()
    clock.start()
    while w.isRunning() and clock.elapsed() < budget_ms:
        QCoreApplication.processEvents()
        # Short slices: a long single processEvents() would block again.
        if w.wait(min(50, max(1, budget_ms - clock.elapsed()))):
            break
    QCoreApplication.processEvents()
    return not w.isRunning()


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
    """Atomic JSON dump: write to a unique temp file then os.replace.

    A crash mid-write used to truncate the live config and lose all
    settings; the atomic-rename pattern keeps the previous good file
    intact if the new write fails.

    H3 fix (REVIEW-2026-11-07): the legacy api_key field used to sit in
    this JSON in PLAINTEXT. Encrypt it through rca_core.secrets_store
    (Fernet when cryptography+keyring are installed, machine-fingerprint
    obfuscation otherwise). A local copy is used so the caller's dict
    (save_all passes the live self.cfg) is not mutated, and
    is_obfuscated() prevents double-wrapping on re-save.

    REVIEW-2026-09-20 (three fixes to this one writer):
      * Unique temp name. The previous code wrote a FIXED
        ``CONFIG_PATH + ".tmp"`` — the SAME path the Tkinter GUI (gui.py)
        uses. Both front-ends share this config file, so two instances (or a
        Tk save racing a Fluent save) wrote into one temp file and the second
        ``os.replace`` moved a half-written blob into place. mkstemp gives
        every writer its own name in the destination directory.
      * Errors are RAISED, not swallowed. The old bare ``except`` turned a
        read-only home / full disk / locked file into "settings silently
        reverted on the next launch"; the callers (save_all / _save_settings)
        now surface the failure through an InfoBar / status bar.
      * Merge-before-write. Callers pass a dict built from the widget state
        only, so any key this build does not know about (added by a newer
        version, by server.py, or a hand edit) used to vanish on the first
        save. The on-disk config is loaded first and the caller's keys are
        layered on top.
    """
    import json
    merged = load_config()
    merged.update(cfg or {})
    to_write = merged
    key = (merged.get("api_key") or "")
    if key:
        try:
            from rca_core.secrets_store import encrypt, is_obfuscated
            if not is_obfuscated(key):
                to_write = dict(merged)
                to_write["api_key"] = encrypt(key)
        except Exception:
            # REVIEW-2026-09-10: fail CLOSED — an encrypt failure used to fall
            # through and persist the raw plaintext key. Drop it from the file
            # instead (the provider store keeps its own copy; the in-session
            # value is untouched) and leave a marker the settings page can show.
            to_write = dict(merged)
            to_write["api_key"] = ""
            to_write["api_key_store_failed"] = True
    cfg_dir = os.path.dirname(CONFIG_PATH) or "."
    fd, tmp = tempfile.mkstemp(prefix=".range_chart_analyzer.", suffix=".tmp",
                               dir=cfg_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
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
        # REVIEW-2026-09-20: re-raise so the caller can tell the user their
        # settings were NOT persisted (the silent failure was the bug).
        log.exception("save_config failed for %s", CONFIG_PATH)
        raise


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
    # REVIEW-2026-09-20: the signal carries (worker, result) — exactly the
    # SettingsPage `_Worker.done = Signal(object, object)` pattern. Carrying
    # the worker makes it possible to connect the page's BOUND METHOD
    # (`self._on_worker_result`): a bound method of a QObject gives a queued
    # (auto) connection, so the handler runs on the GUI thread. The old
    # single-payload signal was connected through a lambda, and a plain
    # function/lambda has no receiver QObject — Qt then uses a DIRECT
    # connection, so `_on_result()` (InfoBar + labels + tables + history
    # writes) executed inside this QThread. Touching widgets off the GUI
    # thread is the crash class this module's header explicitly forbids.
    finished_ok = Signal(object, object)  # (worker, ExtractResult)
    progress = Signal(str)         # status text

    def __init__(self, params, mode, runs, auto_filename="", gen=None):
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
        # REVIEW-2026-09-20: the stale-result generation the page had when
        # this worker was launched. The page's slot compares it against the
        # live counter (see ExtractPage._on_worker_result) instead of the
        # launch-time closure that used to carry it — the closure could only
        # be wired through a lambda, and a lambda connection is direct.
        self.gen = gen

    def _emit_result(self, result) -> None:
        """Emit (self, result) on ``finished_ok``.

        Single emission point so every path (fast single run, merged
        multi-run, cancel, unexpected exception) carries the worker the
        receiving slot needs to resolve the queued connection.
        """
        self.finished_ok.emit(self, result)

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
                self._emit_result(result)
                return
            # AUDIT-2026-09-27 [item 1.1] (P0): "never let exceptions kill the
            # worker" used to be guaranteed by ONE `except`, the sibling of the
            # `try` above — which therefore covered ONLY the `if runs <= 1:`
            # branch (that branch `return`s from inside the try). The whole
            # multi-run region that follows ran completely unguarded: the usage
            # accumulation (`int(u.get("input_tokens") or 0)` raises ValueError
            # on a non-numeric value), `merge_results(...)`, the merged
            # `ExtractResult(...)`. A single raise there meant `finished_ok`
            # never fired, so `_on_worker_result` / `_on_result` never ran,
            # `self.busy` stayed True forever, the Extract button stayed
            # disabled and the spinner turned permanently — and under PySide6 an
            # exception escaping `QThread.run()` can abort the whole process.
            #
            # The fix is the delegating call below, NOT a second inline guard:
            # the multi-run body moved verbatim into `_run_multi()`, whose body
            # sits at the same 8-space indent, so no statement inside it was
            # re-indented. The single `except` now spans BOTH paths, which is
            # what its own comment always claimed. Two paths, one guard, no
            # half-covered region.
            self._run_multi(mode=mode, runs=runs, params=params)
            return
        except Exception as exc:  # BUG13: never let exceptions kill the worker
            # Bug-12 fix: log so an unexpected exception doesn't disappear.
            # AUDIT-2026-09-27 [item 1.1]: this handler now spans BOTH the
            # single-run fast path and the multi-run merge, which it did not
            # before — see the note above the `_run_multi` call. A raise in
            # the merge used to leave the GUI permanently busy.
            log.exception("ExtractWorker.run failed")
            self._emit_result(ExtractResult(
                ok=False, error_key="err.http", raw=str(exc)))
            return

    def _run_multi(self, *, mode: str, runs: int, params: dict[str, Any]) -> None:
        """The multi-run path of :meth:`run`, delegated so the one
        ``except Exception`` in ``run()`` covers it too.

        AUDIT-2026-09-27 [item 1.1]: extracted verbatim from ``run()``. A
        method body is indented exactly like the old block was (8 spaces), so
        this is a pure move — the safest way to widen an existing guard's
        scope without re-indenting 150 lines and risking a silent change.
        Emits the merged result through ``_emit_result``, or the collected
        failure when every run failed.

        ``concurrent`` is imported HERE, not inherited: the module has no
        top-level ``import concurrent.futures`` — both existing users
        (``_collect_extraction_futures`` and the old ``run``) import it
        function-locally — so moving the body out of ``run()`` left the name
        out of scope and every multi-run request died with
        ``NameError: name 'concurrent' is not defined``.
        """
        import concurrent.futures

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
                self._emit_result(ExtractResult(
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
                self._emit_result(ExtractResult(
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
            self._emit_result(last_fail or ExtractResult(ok=False, error_key="err.empty"))
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
        self._emit_result(merged_res)


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
            #
            # AUDIT-2026-09-27 [item 8.1]: the profile used to be constructed
            # with `self` (the view) as its QObject parent, immediately
            # BEFORE the page that uses it. Qt destroys child QObjects in
            # addition order, so the PROFILE was destroyed first while the
            # PAGE was still alive. QtWebEngine says so at teardown —
            # "Release of profile requested but WebEnginePage still not
            # deleted. Expect troubles!" — and the process then dies with a
            # native access violation. It was reproducible on a bare
            # create-window / close / exit script, which is what finally
            # separated it from the test suites it used to surface in.
            #
            # Two defects in one line: the ordering, and the fact that EVERY
            # window built its own profile under the SAME storage name (the
            # test suites create many windows per process). Both go away by
            # building the named profile once per process, parented to the
            # QApplication — which also matches the stated intent, since
            # localStorage is supposed to survive the window closing.
            try:
                from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
                from PySide6.QtCore import QStandardPaths
                _profile = _shared_web_profile()
                if _profile is not None:
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
            # The payload becomes JS SOURCE, not a JSON document handed to
            # JSON.parse, so the two line separators JSON allows raw must be
            # escaped: they are literal newlines to a JS tokenizer.
            payload = (payload.replace("\u2028", "\\u2028")
                              .replace("\u2029", "\\u2029"))
            # Wrap in an IIFE so any exception inside setTreeData is
            # surfaced as a console error rather than silently swallowed
            # by runJavaScript's promise chain.
            #
            # AUDIT-2026-09-27 [item 2.5] (found only by a real run): the
            # original built this by f-string brace doubling, and the `catch`
            # half was a PLAIN string -- so its `}}` and `{{` were emitted
            # literally instead of collapsing to one brace each. The result
            # closed `try` one brace early and left `catch` orphaned:
            #
            #   (function(){try{...}return null;}}catch(e){{...}})()
            #                             ^^^^^^^^^^ try already closed here
            #
            # WebEngine rejected that as "SyntaxError: Missing catch or
            # finally after try" for EVERY payload -- 66/66 recorded real
            # results, verified by re-generating each script and running
            # `node --check`. PhyloTreeWidget had therefore never rendered a
            # tree, for any input, since it was added. runJavaScript swallows
            # the parse error into the JS console, so the Python side stayed
            # green.
            #
            # Fixed by writing the script as PLAIN strings only: braces are
            # then literal, so what you read is what runs, and the payload is
            # concatenated rather than interpolated (no escape games at all).
            script = (
                "(function(){"
                "try{"
                "if(typeof window.setTreeData==='function'){"
                "window.setTreeData(" + payload + ");"
                "}else{console.error("
                "'PhyloTreeWidget: window.setTreeData is not defined');}"
                "return null;"
                "}catch(e){console.error("
                "'PhyloTreeWidget setTreeData threw:',e);return null;}"
                "})()"
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
        # AUDIT-2026-09-27 [item 2.1]: the stored history thumbnail, kept in a
        # DISPLAY-ONLY slot. It must never land in `image_b64`: that field is
        # what `_on_extract` sends to the model, and tests/test_gui_sprint_b.py
        # pins `image_b64 is None` after load_result so the app cannot silently
        # re-extract the PREVIOUS image ("ghost image"). Reviewing a past
        # result and seeing no figure at all was the cost of that guarantee;
        # this attribute removes the cost without touching the guarantee.
        self._display_thumb_b64: str | None = None
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
        # REVIEW-2026-09-20: mode the worker resolved for the CURRENT result
        # ("auto" extractions are resolved inside the thread; a history load
        # carries the stored mode). None → _current_mode() falls back to shape
        # detection. Kept in sync by _on_result() and load_result().
        self._resolved_mode = None

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
        # AUDIT-2026-09-27 [item 2.2] (U-01): the source pixmap and the
        # user's magnification. See _render_pixmap / _fit_preview.
        self._preview_src = None
        self._preview_zoom = 1.0
        # UI-REVIEW-2026-09-05: the empty preview doubles as a dropzone —
        # dashed border + hint text + click-to-choose + drag-and-drop.
        # The previous flat bordered BodyLabel gave no affordance at all.
        self.preview.setText(self._t("image.dropHint"))
        self._style_preview_empty()
        self.preview.mousePressEvent = lambda _e: self._choose_image()
        ic.addWidget(self.preview)

        # AUDIT-2026-09-27 [item 2.2] (U-01): zoom + full-resolution view.
        # A 172px thumbnail of a 2000-4000px range chart makes the taxon
        # labels — the information the figure exists to convey — illegible.
        # Three affordances, all keyboard-reachable and all no-ops until an
        # image is loaded: zoom in, reset to fit, and a full-resolution
        # scrollable dialog. Wheel-over-preview zooms too, with the same
        # modifier-free mapping the web frontend uses.
        zoom_row = QHBoxLayout()
        zoom_row.setSpacing(6)
        self.btn_zoom_in = ToolButton(FIF.ADD)
        self.btn_zoom_in.setToolTip(self._t("image.zoomIn"))
        self.btn_zoom_in.clicked.connect(lambda: self._zoom_preview(1.5))
        zoom_row.addWidget(self.btn_zoom_in)
        self.btn_zoom_out = ToolButton(FIF.REMOVE)
        self.btn_zoom_out.setToolTip(self._t("image.zoomOut"))
        self.btn_zoom_out.clicked.connect(lambda: self._zoom_preview(1 / 1.5))
        zoom_row.addWidget(self.btn_zoom_out)
        self.btn_zoom_fit = ToolButton(FIF.SYNC)
        self.btn_zoom_fit.setToolTip(self._t("image.zoomFit"))
        self.btn_zoom_fit.clicked.connect(lambda: self._zoom_preview(1 / 99))
        zoom_row.addWidget(self.btn_zoom_fit)
        zoom_row.addStretch(1)
        self.btn_figure_full = PushButton(FIF.FULL_SCREEN, self._t("image.viewFull"))
        self.btn_figure_full.clicked.connect(self._show_figure_full)
        zoom_row.addWidget(self.btn_figure_full)
        ic.addLayout(zoom_row)
        # Rescale rather than clip when the panel is resized (B-15).
        try:
            self.preview.resizeEvent = lambda _e: self._on_preview_resize()
        except Exception:
            pass
        # Gate the whole row on "is there a figure to look at".
        for _b in (self.btn_zoom_in, self.btn_zoom_out, self.btn_zoom_fit,
                    self.btn_figure_full):
            _b.setEnabled(False)
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
        # AUDIT-2026-09-27 [item 3.1] (D2): the canonical chart-type and
        # chart-language tables now live HERE, next to the control the user
        # actually touches. They used to live on SettingsPage, which made the
        # contextually-right selector a pure view of a control the user had to
        # go elsewhere to change — and the two-way mirror had to be kept
        # honest by hand. SettingsPage keeps a hidden mirror (see
        # set_selector_mirror_collapsed) so _cycle_lang's retranslate order and
        # the existing tests that address settings_page.cmb_ctype still work.
        self._ctype_codes = ["auto", "range_chart", "columnar_section",
                             "abundance_diagram", "phylogenetic_tree",
                             "zonation_chart"]
        self._ctype_keys = ["settings.chartType.auto",
                            "settings.chartType.rangeChart",
                            "settings.chartType.columnarSection",
                            "settings.chartType.abundanceDiagram",
                            "settings.chartType.phylogeneticTree",
                            "settings.chartType.zonationChart"]
        self._clang_codes = ["auto", "zh", "en", "ja", "ru"]
        self._clang_names = lambda: [self._t("chartLang.auto"), "中文", "English",
                                     "日本語", "Русский"]
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
        #
        # AUDIT-2026-09-27 [items 1.11 / 2.3 / 2.4]:
        #  * a CANCEL button. `request_cancel` existed and was wired ONLY from
        #    closeEvent, so a runs=5 extraction (5 x 20 s-2 min) could only be
        #    abandoned by closing the window. It is cooperative: the flag is
        #    read before each submit and between batches, so a single in-flight
        #    request still has to return first — the button says so rather than
        #    pretending to be instant.
        #  * the two EXPORT buttons moved OUT of the run row. They are output
        #    actions, not run actions, and sitting to the LEFT of the primary
        #    Extract button meant the eye landed on two disabled controls
        #    first. They now live in the right panel's header, next to the
        #    result they belong to.
        run_row = QHBoxLayout()
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(24, 24)
        self.spinner.setVisible(False)
        self.lbl_status = CaptionLabel(self._t("status.ready"))
        self.btn_extract = PrimaryPushButton(FIF.PLAY, self._t("action.extract"))
        self.btn_extract.clicked.connect(self._on_extract)
        self.btn_cancel = PushButton(FIF.CANCEL, self._t("action.cancel"))
        self.btn_cancel.setToolTip(self._t("action.cancelHint"))
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_cancel.setVisible(False)
        run_row.addWidget(self.spinner)
        run_row.addWidget(self.lbl_status)
        run_row.addStretch(1)
        run_row.addWidget(self.btn_cancel)
        run_row.addWidget(self.btn_extract)
        left_lay.addLayout(run_row)
        left_lay.addStretch(1)

        self._split.addWidget(left_panel)

        # ---- Right panel: confidence + pivot + results ----
        right_panel = QWidget()
        right_lay = QVBoxLayout(right_panel)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(10)

        # AUDIT-2026-09-27 [item 2.4] (U-04): the two export buttons move
        # here from the run row. They are OUTPUT actions belonging to the
        # result, and in the run row they sat immediately LEFT of the primary
        # Extract button, so the eye met two disabled controls before the one
        # control that starts work. Distinct icons too: both used FIF.SAVE, so
        # they were told apart only by their labels.
        self.result_head = QHBoxLayout()
        self.result_head.setSpacing(8)
        self.lbl_conf = StrongBodyLabel("")
        self.result_head.addWidget(self.lbl_conf)
        self.result_head.addStretch(1)
        self.btn_export = PushButton(FIF.DOCUMENT, self._t("action.exportJson"))
        self.btn_export.setToolTip(self._t("action.exportJson"))
        self.btn_export.clicked.connect(self._export_json)
        self.result_head.addWidget(self.btn_export)
        self.btn_export_xlsx = PushButton(FIF.SAVE_AS, self._t("export.xlsx"))
        self.btn_export_xlsx.setToolTip(self._t("export.xlsx"))
        self.btn_export_xlsx.clicked.connect(self._export_xlsx)
        self.result_head.addWidget(self.btn_export_xlsx)
        right_lay.addLayout(self.result_head)

        # AUDIT-2026-09-27 [item 2.5] (U-03): a real empty state. Before this
        # the right panel was ~55% of the window and completely blank on a
        # fresh launch except for four greyed-out row-edit buttons with no
        # explanation and no next step. `history.empty` / `history.emptyHint`
        # already existed in all three locales and were wired to NOTHING.
        self.empty_state = BodyLabel(self._t("extract.emptyHint"))
        self.empty_state.setAlignment(Qt.AlignCenter)
        self.empty_state.setWordWrap(True)
        right_lay.addWidget(self.empty_state, 1)

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
        # AUDIT-2026-09-27 [item 1.3] (B-03): hide it from the start. A fresh
        # launch used to show four enabled-looking buttons above an empty
        # result area, and every one of them returned silently. Visibility is
        # owned solely by _update_result_actions() from here on.
        self.edit_row_widget.setVisible(False)
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
        # AUDIT-2026-09-27 [item 1.3]: self.tables must exist BEFORE the
        # gate runs. The gate consults it (a result with no rendered
        # table cannot be row-edited), and the real initialisation sat
        # nine lines further down with the QStackedLayout, so the first
        # call raised AttributeError and the page never came up.
        self.tables = {}
        self._update_result_actions()

        self.stack = QFrame()
        # QStackedLayout (not QVBoxLayout) so only the current pivot's
        # table is rendered. The previous VBox+setVisible() pattern broke
        # on Windows when qfluentwidgets' Pivot didn't fire onClick,
        # leaving the right panel empty after the first render.
        self.stack_lay = QStackedLayout(self.stack)
        self.stack_lay.setContentsMargins(0, 0, 0, 0)
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
        # AUDIT-2026-09-27 [item 1.6] (B-06): this used to accept
        # `hasUrls() or hasImage()`, but `dropEvent` returns immediately when
        # there are no URLs — so dragging an <img> out of a browser showed the
        # COPY cursor and then did nothing. Accept only what the drop handler
        # can actually consume; `hasImage()` is honoured further down only if a
        # caller materialises it to a temp file first.
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802 (Qt naming)
        if e.mimeData().hasUrls():
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
        constructed first).

        AUDIT-2026-09-27 [item 3.1] (D2): the INLINE combos on this page are
        now the canonical state; the Settings page's pair became a hidden
        mirror. The tables used to live on SettingsPage and
        ``win.chart_type()`` read ``settings_page._ctype_codes[...]``, so the
        contextually-right control (next to "choose an image") was a pure view
        of a control the user had to go to Settings to change — and the two-way
        mirror had to be kept honest by hand. Reversing it means the code
        tables have exactly one owner, next to the action they affect.

        The Settings copies are kept rather than deleted for two reasons:
        ``_cycle_lang`` retranslate ordering depends on their ``clear()`` +
        ``setCurrentIndex()`` pair running LAST (it is the self-heal), and
        deleting a control the other page's tests address is a needless
        compatibility risk. They are simply hidden and driven from here.
        """
        sp = settings_page
        self.cmb_ctype_inline.addItems([self._t(k) for k in self._ctype_keys])
        self.cmb_ctype_inline.setCurrentIndex(
            self._ctype_codes.index(sp.cfg_chart_type())
            if sp.cfg_chart_type() in self._ctype_codes else 0)
        self.cmb_clang_inline.addItems(self._clang_names())
        self.cmb_clang_inline.setCurrentIndex(
            self._clang_codes.index(sp.cfg_chart_lang())
            if sp.cfg_chart_lang() in self._clang_codes else 0)
        # AUDIT-2026-09-27 [item 3.1] (D2): this pair was wired to
        # _push_ctype_to_settings / _push_clang_to_settings, which is the
        # INLINE->MIRROR direction — so a change to the hidden mirror fired a
        # handler that read the inline index straight back and wrote it over
        # the value the user had just set. The two copies could therefore
        # drift, which is the exact failure D2 exists to remove. The real
        # reverse handlers (_on_settings_ctype / _on_settings_clang, which
        # carry the _selector_sync reentrancy guard) were already written and
        # simply never connected.
        sp.cmb_ctype.currentIndexChanged.connect(self._on_settings_ctype)
        sp.cmb_clang.currentIndexChanged.connect(self._on_settings_clang)
        sp.set_selector_mirror_collapsed(True)
        # Seed the mirror from the canonical inline pair (not the other way
        # round: the Extract page is the source of truth after D2).
        self._push_ctype_to_settings()
        self._push_clang_to_settings()

    # ---- canonical chart-type / chart-language state (D2) ----

    def chart_type_code(self) -> str:
        """The selected chart-type code — the single source of truth (D2)."""
        idx = self.cmb_ctype_inline.currentIndex()
        if 0 <= idx < len(self._ctype_codes):
            return self._ctype_codes[idx]
        return self._ctype_codes[0] if self._ctype_codes else "auto"

    def chart_lang_code(self) -> str:
        """The selected chart-language code — the single source of truth (D2)."""
        idx = self.cmb_clang_inline.currentIndex()
        if 0 <= idx < len(self._clang_codes):
            return self._clang_codes[idx]
        return self._clang_codes[0] if self._clang_codes else "auto"

    def _push_ctype_to_settings(self, *_a) -> None:
        """Mirror the canonical inline chart type into the hidden Settings pair."""
        sp = getattr(self.win, "settings_page", None)
        if sp is None:
            return
        idx = self.cmb_ctype_inline.currentIndex()
        if sp.cmb_ctype.currentIndex() != idx:
            sp.cmb_ctype.setCurrentIndex(idx)

    def _push_clang_to_settings(self, *_a) -> None:
        sp = getattr(self.win, "settings_page", None)
        if sp is None:
            return
        idx = self.cmb_clang_inline.currentIndex()
        if sp.cmb_clang.currentIndex() != idx:
            sp.cmb_clang.setCurrentIndex(idx)

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

    def _render_pixmap(self, pix) -> bool:
        """Fit *pix* into the preview label and mark it loaded.

        AUDIT-2026-09-27 [items 1.16 / 2.2] (U-01 / B-15). Two defects lived
        here. The scale was computed ONCE, at load time, from
        ``preview.width()`` — so dragging the splitter narrower pushed the label
        below the pixmap's sizeHint and Qt CLIPPED it instead of rescaling. And
        the result was a 172px-tall thumbnail, which for a 2000-4000px range
        chart is a 13-15x downscale: the taxon labels, which carry most of the
        information in such a figure, were simply illegible.

        The SOURCE pixmap is now kept and re-fitted on every resize, and
        ``_preview_zoom`` carries the user's chosen magnification. The widget is
        deliberately NOT replaced with a QGraphicsView: nine call sites across
        this file and the history detail dialog reference ``self.preview`` as a
        QLabel, and swapping its type would ripple through all of them for no
        extra capability here. ``_preview_zoom > 1`` routes the user to
        ``_show_figure_full()`` for panning, which is where a canvas belongs.

        Returns False for a null pixmap (caller falls back to the empty state).
        """
        try:
            if pix is None or pix.isNull():
                return False
            self._preview_src = pix
            self._preview_zoom = 1.0
            self._style_preview_loaded()
            self.preview.setText("")
            self._fit_preview()
            # The zoom / full-view row is a no-op until there is a figure.
            for _b in (self.btn_zoom_in, self.btn_zoom_out, self.btn_zoom_fit,
                       self.btn_figure_full):
                try:
                    _b.setEnabled(True)
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _fit_preview(self) -> None:
        """Scale the stored source pixmap to the label at the current zoom.

        Called on every resize, so narrowing the splitter (or the window)
        RESCALES instead of clipping — that was the B-15 symptom.
        """
        src = getattr(self, "_preview_src", None)
        if src is None or src.isNull():
            return
        avail_w = self.preview.width() if self.preview.width() > 0 else 300
        fit_h = max(60, (self.preview.height() or 180) - 8)
        scaled = src.scaled(
            int(max(80, avail_w) * max(0.1, self._preview_zoom)),
            int(fit_h * max(0.1, self._preview_zoom)),
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview.setPixmap(scaled)
        # A magnified view needs the label to be able to grow; a fit view does
        # not, and must stay at its 180px so the panel layout is unchanged.
        if self._preview_zoom <= 1.0:
            self.preview.setFixedHeight(180)

    def _on_preview_resize(self) -> None:
        self._fit_preview()

    def _zoom_preview(self, factor: float) -> None:
        """Magnify / restore the preview in place (1.0 = fit)."""
        if getattr(self, "_preview_src", None) is None:
            return
        self._preview_zoom = max(1.0, min(6.0, self._preview_zoom * factor))
        if self._preview_zoom <= 1.0:
            self.preview.setFixedHeight(180)
        else:
            self.preview.setFixedHeight(
                int(max(180, self.preview.height() or 180) * min(3.0, factor)))
        self._fit_preview()

    def _show_figure_full(self) -> None:
        """Open the stored figure at FULL resolution in a scrollable dialog.

        AUDIT-2026-09-27 [item 2.2] (U-01). This is the audit surface the app
        never had: the whole point is checking an extracted range against the
        figure it came from, and the only view of that figure was a 172px
        thumbnail — or, when reviewing a past record, nothing at all. A
        full-resolution, zoomable, keyboard-scrollable dialog is the smallest
        honest fix and needs no change to the extraction flow.

        Best effort: a dialog that fails to build must not break the page.
        """
        src = getattr(self, "_preview_src", None)
        if src is None or src.isNull():
            return
        try:
            from PySide6.QtWidgets import QDialog, QDialogButtonBox, QScrollArea
            dlg = QDialog(self)
            dlg.setWindowTitle(self._t("image.fullTitle"))
            lay = QVBoxLayout(dlg)
            area = QScrollArea()
            area.setWidgetResizable(True)
            holder = BodyLabel()
            holder.setAlignment(Qt.AlignCenter)
            # Natural size: the FULL-resolution figure, scrollable.
            holder.setPixmap(src)
            area.setWidget(holder)
            lay.addWidget(area, 1)
            box = QDialogButtonBox(QDialogButtonBox.Close)
            box.rejected.connect(dlg.reject)
            box.accepted.connect(dlg.accept)
            lay.addWidget(box)
            dlg.resize(1000, 760)
            dlg.exec()
        except Exception as exc:
            log.warning("full-figure dialog failed: %s", exc)

    def _show_history_thumbnail(self, thumb_b64: str) -> None:
        """Render a stored history thumbnail in the preview, clearly labelled.

        AUDIT-2026-09-27 [item 2.1] (U-02). Reviewing a past extraction is the
        core audit workflow and the figure was simply absent; the stored
        ``image_thumbnail`` (~200-256px) makes it visible again. It is drawn
        through the SAME preview path as a live image so the resize/zoom
        behaviour stays identical, and the caption says it is a stored
        thumbnail so nobody mistakes it for the file that was extracted.

        Best effort: a corrupt blob must not break loading the result, so a
        failure falls back to the ordinary "no image" state.

        AUDIT-2026-09-27 [item 2.4] (found by the probe in item 2.1's own
        test): this function was DEAD from the day it was added. It calls
        ``base64.b64decode`` but the module never imported ``base64`` at
        module level, so every call raised ``NameError`` on the very first
        line of the try — and the blanket ``except Exception`` below caught
        it and reset to the drop hint. The thumbnail therefore never
        rendered, for any record, ever, and nothing failed visibly: the
        broad except turned a missing import into a plausible-looking
        "the blob was corrupt" outcome, which is exactly the class of bug a
        silent-fallback path is supposed to be reviewed for.
        """
        try:
            from PySide6.QtGui import QPixmap
            pm = QPixmap()
            if not pm.loadFromData(base64.b64decode(thumb_b64)):
                raise ValueError("thumbnail blob did not decode")
            self._render_pixmap(pm)
            # Deliberately a distinct message from image.none: the reviewer
            # must know this is a stored thumbnail, not a re-loadable file.
            self.lbl_imginfo.setText(self._t("image.historyThumbnail"))
        except Exception as exc:
            # Now it cannot hide a defect in the code above: a bad blob is a
            # ValueError from loadFromData, and everything else is logged.
            log.warning("history thumbnail could not be shown: %s", exc)
            self._display_thumb_b64 = None
            self.lbl_imginfo.setText(self._t("image.none"))
            self.preview.clear()
            self.preview.setText(self._t("image.dropHint"))
            self._style_preview_empty()

    def _on_cancel(self) -> None:
        """Ask the worker to stop (cooperative).

        AUDIT-2026-09-27 [item 2.3] (B-16). ``request_cancel`` existed and was
        reachable only from ``closeEvent``, so an extraction could be abandoned
        only by closing the window. Two honest limitations, both already
        documented on ``request_cancel`` and now surfaced in the UI rather
        than discovered:

        * the flag is read BEFORE each submit and between batches, so any run
          already in flight still has to finish its HTTP request;
        * a single-run extraction has no checkpoint at all inside the blocking
          ``urllib`` call, so cancelling one waits out that request.

        The button therefore disables itself and says it is waiting, instead
        of leaving the user clicking a control that cannot act yet.
        """
        w = self._worker
        if w is None or not w.isRunning():
            return
        try:
            w.request_cancel()
        except Exception as exc:
            log.warning("request_cancel failed: %s", exc)
            return
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText(self._t("status.cancelling"))

    def has_pending_image(self) -> bool:
        """True when an extractable image is armed.

        AUDIT-2026-09-27 [items 1.3 / B-15]: ``load_image_b64`` could succeed
        and then ``QPixmap(path)`` return null, so the preview reverted to the
        drop hint — the UI said "no image" while the app was fully armed to
        extract. Gating the actions on the DATA rather than on what the
        preview happens to show removes that contradiction.
        """
        return bool(self.image_b64)

    def _update_result_actions(self) -> None:
        """The single gate for the six action buttons.

        AUDIT-2026-09-27 [item 1.3] (B-03). This used to be
        ``bool(self.result)`` for all six, with two more partial gates
        elsewhere (``_sync_export_buttons`` for the exports, ``_set_busy`` for
        the Extract button), which produced three wrong states:

        * a ZERO-ROW result left Add / Delete / Discard / Apply enabled, and
          every one of them returned silently — Apply iterated an empty dict
          and showed no toast at all, so four live buttons did nothing;
        * ``edit_row_widget`` was never hidden at construction, so a fresh
          launch showed the whole row with four dead buttons;
        * while an extraction was running, only the Extract and export buttons
          were disabled — the four row-edit buttons stayed live and could
          trigger ``_render_result()`` + ``_persist_edits_to_history()``
          against the PREVIOUS result mid-flight.

        Visibility AND enablement are both decided here now. The row is hidden
        rather than deleted (``setVisible``), so the widgets stay parented and
        ``isEnabled()`` remains the assertion surface the existing suite uses.
        """
        has_result = bool(self.result)
        has_tables = bool(self.tables)
        ready = has_result and has_tables and not self.busy

        # Row-edit row: visible only when there is a result AND a rendered
        # table. A result with no table is the zero-row case — showing four
        # buttons over an empty area is exactly the B-03 complaint, and
        # hiding only on `has_result` would have left that half-open.
        try:
            self.edit_row_widget.setVisible(has_result and has_tables)
        except Exception:
            pass
        # AUDIT-2026-09-27 [item 2.5] (U-03): the empty state owns the
        # right panel until there is something to show, and the Pivot is
        # hidden with it — an empty Pivot renders as a stray tab strip.
        try:
            self.empty_state.setVisible(not has_result)
            self.pivot.setVisible(has_result)
        except Exception:
            pass
        for b in (self.btn_add_row, self.btn_del_row,
                  self.btn_discard, self.btn_apply_edits):
            b.setEnabled(ready)

        # Exports: a result to export AND an image the app is actually armed
        # with (B-15) AND not mid-run.
        armed = bool(self.result) and self.has_pending_image() and not self.busy
        self.btn_export.setEnabled(armed)
        self.btn_export_xlsx.setEnabled(armed and self._xlsx_available())

    def _xlsx_available(self) -> bool:
        """Whether XLSX export can actually run, for a tooltip / disabled state.

        AUDIT-2026-09-27 [item 1.7] (B-07): the button was never gated, and
        its ``except ImportError`` pre-check was dead code — ``rca_core``
        re-exports ``to_xlsx`` unconditionally and the real failure is a
        ``RuntimeError`` raised INSIDE ``exporter.to_xlsx`` when openpyxl is
        missing. So the user walked through a save dialog, typed a filename,
        confirmed, and only then learned it could not work. Ask the exporter
        instead of guessing.
        """
        try:
            import openpyxl  # noqa: F401
            return True
        except Exception:
            return False

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
            self._render_pixmap(pix)
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
        # AUDIT-2026-09-27 [item 2.3]: the cancel control is offered exactly
        # while a worker is running, and re-armed each time so a second run is
        # cancellable too.
        self.btn_cancel.setVisible(busy)
        self.btn_cancel.setEnabled(busy)
        # AUDIT-2026-09-27 [item 1.3]: the row-edit buttons used to keep
        # their previous enabled state while a worker was in flight, so
        # Add/Delete/Discard/Apply could fire _render_result() +
        # _persist_edits_to_history() against the PREVIOUS result mid-run.
        # Re-derive the whole set through the one gate.
        self._update_result_actions()
        self.spinner.setVisible(busy)
        if busy:
            self.lbl_status.setText(self._t("status.loading"))

    def _sync_export_buttons(self, busy):
        """Delegate to the single gate — kept so existing call sites work.

        AUDIT-2026-09-27 [item 1.3]: this was a SECOND source of truth for
        the two export buttons (``self.result is not None and not busy``),
        which is how a partial update could leave them out of step with the
        row-edit buttons. It now only forwards to
        ``_update_result_actions()``, and the export rule itself lives there.
        """
        self._update_result_actions()

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
        # previously-launched worker is dropped. The launch-time gen rides on
        # the worker (`ExtractWorker.gen`) and the receiving slot re-checks it
        # against the live counter, so a result from a superseded worker is
        # discarded (i.e. the user started a new extraction, reset, or loaded
        # from history meanwhile). Also bumped on reset/load — see
        # _bump_extract_gen(). Audit fix: the prior code only bumped here, so
        # the guard was effectively inert because busy serialised extractions;
        # bump on every state change that should cancel an in-flight result.
        # REVIEW-2026-09-20: the guard used to live in a lambda connected to
        # finished_ok. A lambda has no receiver QObject, so Qt used a DIRECT
        # connection and _on_result() ran inside the worker thread — touching
        # InfoBar / labels / tables / the history store off the GUI thread.
        # The worker is now connected to a bound method (queued connection)
        # and performs the same gen check on the GUI thread.
        launch_gen = self._bump_extract_gen()
        self._worker = ExtractWorker(params, mode, runs,
                                     auto_filename=(self.image_path or ""),
                                     gen=launch_gen)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_worker_result)
        self._worker.start()

    def _on_worker_result(self, worker, result):
        """GUI-thread slot for ``ExtractWorker.finished_ok``.

        Bound method of this page → Qt's auto connection queues the call onto
        the GUI thread (the worker emits from its own thread). Drops results
        from a superseded worker before handing them to ``_on_result``.
        """
        gen = getattr(worker, "gen", None)
        if gen is not None and getattr(self, "_extract_gen", 0) != gen:
            # Stale: the user started a newer extraction / reset / loaded a
            # history record after this worker was launched.
            log.debug("dropping stale extract result (gen %s != %s)",
                      gen, getattr(self, "_extract_gen", 0))
            return
        self._on_result(result)

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
        # Stale-result guard lives in _on_worker_result() — the queued slot
        # connected to ExtractWorker.finished_ok — which drops results whose
        # generation no longer matches the live one. Nothing to check here;
        # just render.
        # REVIEW-2026-09-20: remember the mode the worker actually resolved
        # (it is attached as `_mode` by ExtractWorker.run) so _current_mode()
        # reports what this result IS instead of re-deriving it from the
        # dropdown / a stale cfg snapshot.
        self._resolved_mode = getattr(result, "_mode", None) or None
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
            # AUDIT-2026-09-27 [item 4.3]: re-gate AFTER the render, which is
            # what fills self.tables. The gate ran before it above and so
            # always saw an empty table dict.
            self._update_result_actions()
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
            # user from seeing their result. But it must not be SILENT
            # either -- the handler bound `exc` and then discarded it, which
            # is the same swallow that hid the missing-base64 NameError in
            # _show_history_thumbnail. A history write that fails here looks
            # identical, to the user and to a test, to one that succeeded.
            log.exception("history/usage persistence failed: %s", exc)
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
        """Return the mode string for the current extraction.

        REVIEW-2026-09-20: read the LIVE chart-type selector
        (``win.chart_type()`` — the very value ``_on_extract()`` handed to the
        worker) instead of ``win.cfg["chart_type"]``. AUDIT-2026-09-27
        corrected this sentence: it said cfg is "only refreshed when the user
        presses Save settings", which stopped being true when close-time
        saving landed in ``closeEvent``. The reason for reading the LIVE
        selector holds either way - a cfg lagging the combo rendered the
        result with the wrong table set AND wrote the wrong ``mode`` into the
        history / usage records (the two views disagreed with each other).
        Resolution order:
          0. (added AUDIT-2026-09-27) a LOADED record's stored mode — the
             result already exists, so its mode is a fact, not a preference;
          1. the live selector value, when it is a concrete mode;
          2. the mode the worker resolved for THIS result ("auto" path), or the
             mode a loaded history record was stored under;
          3. shape detection of the result payload (legacy / unknown).

        AUDIT-2026-09-27 [item 1.4] (B-04): a LOADED record's stored mode now
        wins outright, as step 0 above. The resolution order used to put the
        LIVE combo first, which contradicted this docstring's own step 2 and
        the comment in ``load_result`` ("the stored mode of the loaded record
        IS the authoritative mode for this payload"). Concretely: open a
        ``range_chart`` record from History, change the combo to
        ``abundance_diagram`` for the NEXT run, press Export JSON — the file
        was named ``abundance_diagram_result.json`` and the tree branch was
        mis-evaluated. The combo describes what to extract NEXT; a loaded
        result already exists and its mode is a fact about it.
        """
        resolved = getattr(self, "_resolved_mode", None)
        known = ("range_chart", "columnar_section", "abundance_diagram",
                 "phylogenetic_tree", "zonation_chart")
        if getattr(self, "_loaded_history_id", None) and resolved in known:
            return resolved
        ct = None
        try:
            ct = self.win.chart_type()
        except Exception:
            ct = None
        if ct not in known:
            # "auto" (or an accessor failure / a combo value this build does
            # not know) → fall through to the resolved mode + shape detection.
            if resolved in known:
                return resolved
        else:
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
            # AUDIT-2026-09-27 [item 2.6] (found by a real run over 66
            # recorded results, not by a test): this line used to be
            # `self.edit_row_widget.setVisible(True)`, copied from the
            # REVIEW-2026-09-10 "restore the table UI" intent. But this
            # branch is reached when there are ZERO rows, so the row-edit
            # controls (Add / Delete / Discard / Apply) had nothing to act
            # on. The B-03 gate correctly disabled all four, which left the
            # user staring at a permanently dead control strip — the very
            # symptom B-03 was raised to remove, in the one state the gate
            # had not been wired into. 29 of the 66 real recordings are
            # map/paleomap payloads that land here, so this was the common
            # case, not an edge case. Restore the pivot and the stack, but
            # leave the edit row hidden: it is a control for rows.
            self.edit_row_widget.setVisible(False)
            self.stack.setVisible(True)
            self.pivot.clear()
            self.pivot.addItem(
                routeKey="empty",
                text=self._t("results.empty"),
                onClick=lambda _=False: None,
            )
            empty = BodyLabel(self._t("results.empty"))
            empty.setAlignment(Qt.AlignCenter)
            # AUDIT-2026-09-27 [item 4.1] (measured over 66 recorded real
            # responses): 7 of them carried genuinely useful tables that this
            # view cannot display — paleomap results holding continents,
            # tectonic_features, fossil_sites (with lat/lon) and more, 24
            # tables in total. detect_tableless_mode() drops them because it
            # matches on a whitelist of table KEYS, not on whether the payload
            # actually contains any table, and those keys never appear in a
            # palaeomap. The panel then said "No results yet" over data the
            # user had paid for, and the only way back to it was the JSON
            # export — which nothing on screen mentioned.
            #
            # Rendering those tables properly needs paleomap configs in
            # exporter.py AND a matching branch in js/table.js (the browser
            # currently falls through to the four range-chart shapes for the
            # same payload, i.e. four EMPTY sheets). Shipping only the gate
            # change would make this panel claim to have tables and show four
            # blank ones, so until both surfaces carry the configs, the honest
            # minimum is to SAY the data exists and where to get it.
            _dropped = [k for k, v in (self.result or {}).items()
                        if isinstance(v, list) and v
                        and isinstance(v[0], dict)]
            if _dropped:
                # .replace("{n}", ...) matches results.chimera_warning, and
                # unlike str.format it cannot trip over a stray brace in a
                # translation.
                hint = CaptionLabel(self._t("results.tablesNotShown").replace(
                    "{n}", str(len(_dropped))))
                hint.setAlignment(Qt.AlignCenter)
                hint.setWordWrap(True)
            else:
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
        self._render_result()
        # AUDIT-2026-09-27 [item 4.3]: ordering, see above.
        self._update_result_actions()
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
            # AUDIT-2026-09-27 [item 4.3]: see above.
            self._update_result_actions()
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
                    record_id: int | None = None,
                    thumbnail_b64: str | None = None) -> None:
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
        # REVIEW-2026-09-20: the stored mode of the loaded record is the
        # authoritative mode for this payload (an old record may carry the
        # raw "auto" the pre-Sprint-B GUI used to persist → keep None so
        # _current_mode() falls back to shape detection).
        self._resolved_mode = mode if mode and mode != "auto" else None
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
        # The loaded record has no extractable image FILE, so wipe the image
        # state to keep "Export JSON" honest about where the result came from.
        # Sprint B (REVIEW-2026-09-04): only image_path used to be
        # cleared — image_b64 / media_type / dims stayed populated, so a
        # user could hit Extract in this apparently image-less state and
        # silently send the PREVIOUS image ("ghost image" extraction).
        # The user can re-upload to extract/export with a real source.
        self.image_path = None
        self.image_b64 = None
        self.media_type = None
        self._img_dims = (0, 0, False)
        # AUDIT-2026-09-27 [item 2.1] (U-02): the comment above used to say
        # "we only stored the result JSON" — FALSE, and stale since the
        # thumbnail landed: `HistoryRecord.image_thumbnail` is written on every
        # save and already rendered by the history detail dialog. The code then
        # followed the wrong premise and threw the figure away, so the one
        # screen where a user is most likely to AUDIT an old extraction was the
        # only one with no figure on it. `image_thumbnail_b64` is only 200-256px
        # and exists to orient the reviewer, not to be re-extracted — which is
        # why it goes to the display-only slot and never to `image_b64`.
        self._display_thumb_b64 = thumbnail_b64 or None
        if self._display_thumb_b64:
            self._show_history_thumbnail(self._display_thumb_b64)
        else:
            # AUDIT-2026-09-27 [item 2.2]: with no figure, the zoom / full-view
            # row has nothing to act on — disable it rather than leaving four
            # live buttons that silently do nothing.
            self._preview_src = None
            self._preview_zoom = 1.0
            for _b in (self.btn_zoom_in, self.btn_zoom_out, self.btn_zoom_fit,
                       self.btn_figure_full):
                try:
                    _b.setEnabled(False)
                except Exception:
                    pass
            self.lbl_imginfo.setText(self._t("image.none"))
            self.preview.clear()
            self.preview.setText(self._t("image.dropHint"))
            self._style_preview_empty()
        # If the loaded result is a columnar-section shape, update the
        # mode display so the user sees the right table set.
        try:
            self._render_result()
            # AUDIT-2026-09-27 [item 4.3]: see _on_result — the gate above ran
            # before self.tables existed.
            self._update_result_actions()
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
        # AUDIT-2026-09-27 [item 1.7] (B-07): the old `except ImportError`
        # around the next import was DEAD CODE — `rca_core/__init__.py`
        # re-exports `to_xlsx` unconditionally, and the real failure is a
        # `RuntimeError` raised INSIDE `exporter.to_xlsx` when openpyxl is
        # missing. So the check never fired: the user walked through the save
        # dialog, typed a filename, confirmed, and only then learned it could
        # not work. Gate on the dependency itself, BEFORE the dialog. (The
        # button is also disabled by _update_result_actions, but a keyboard
        # route or a stale enabled state can still land here.)
        if not self._xlsx_available():
            InfoBar.error(
                "", self._t("export.xlsxMissingOpenpyxl"),
                parent=self.win, position=InfoBarPosition.TOP, duration=5000,
            )
            return
        from rca_core import to_xlsx as _to_xlsx
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
            # AUDIT-2026-09-27 [item 4.3]: see above.
            self._update_result_actions()


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
        # AUDIT-2026-09-27 [item 1.2] (P0): the API key no longer has its own
        # "Save settings" button. There were TWO identical PrimaryPushButtons
        # labelled settings.save — one per card — and BOTH wrote the whole
        # config, so the user could not tell which card a button saved, the
        # key-card button silently persisted unrelated unsaved spinbox edits,
        # and with "remember" off either button blanked cfg["api_key"]. An API
        # key is not a document: it commits on Enter / focus-out, like a
        # password field should. Nothing references `btn_save_key` any more —
        # see the two call sites updated for this change (SettingsPage.
        # retranslate and tests_gui_fluent.py's language-relabel check).
        self.ipt_key.editingFinished.connect(self._save_key)
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

        # AUDIT-2026-09-27 [item 3.1] (D2): the chart-type and chart-language
        # mirror rows had no parent at all once their g2.addWidget lines went
        # away, so they were invisible-but-unowned and the collapse switch had
        # nothing to act on. They are state, not a second editable control, so
        # they live in their own holder that one call can hide. Not destroyed:
        # _cycle_lang's retranslate still walks them and the tests still
        # address settings_page.cmb_ctype.
        self._selector_mirror_holder = QWidget()
        mirror_lay = QVBoxLayout(self._selector_mirror_holder)
        mirror_lay.setContentsMargins(0, 0, 0, 0)
        mirror_lay.setSpacing(8)
        mirror_grid = QGridLayout()
        mirror_grid.setHorizontalSpacing(12)
        mirror_grid.setVerticalSpacing(10)
        mirror_grid.setColumnStretch(1, 1)
        self._mirror_grid = mirror_grid
        self.lbl_ctype = StrongBodyLabel(self._t("settings.chartType"))
        # AUDIT-2026-09-27 [item 3.1] (D2): this combo is now a HIDDEN
        # MIRROR of the Extract page's canonical one. The code tables moved
        # there; aliased here so the i18n retranslate path, which still
        # walks both pages in order, keeps working.
        self.cmb_ctype = ComboBox()
        self._ctype_codes = list(self.win.extract_page._ctype_codes)
        self._ctype_keys = list(self.win.extract_page._ctype_keys)
        self.cmb_ctype.addItems([self._t(k) for k in self._ctype_keys])
        cur = cfg.get("chart_type", "auto")
        self.cmb_ctype.setCurrentIndex(
            self._ctype_codes.index(cur) if cur in self._ctype_codes else 0)

        self.lbl_clang = StrongBodyLabel(self._t("settings.chartLang"))
        # AUDIT-2026-09-27 [item 3.1] (D2): hidden mirror, as above.
        self.cmb_clang = ComboBox()
        self._clang_codes = list(self.win.extract_page._clang_codes)
        self._clang_names = self.win.extract_page._clang_names
        self.cmb_clang.addItems(self._clang_names())
        cur = cfg.get("chart_lang", "auto")
        self.cmb_clang.setCurrentIndex(
            self._clang_codes.index(cur) if cur in self._clang_codes else 0)
        mirror_grid.addWidget(self.lbl_ctype, 0, 0)
        mirror_grid.addWidget(self.cmb_ctype, 0, 1)
        mirror_grid.addWidget(self.lbl_clang, 1, 0)
        mirror_grid.addWidget(self.cmb_clang, 1, 1)
        mirror_lay.addLayout(mirror_grid)
        cv.addWidget(self._selector_mirror_holder)

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
        """Commit the API key: into the active provider AND the legacy cfg field.

        AUDIT-2026-09-27 [item 1.2]: wired to ``ipt_key.editingFinished``
        instead of a "Save settings" button. Both halves are explicit now —
        the generation settings are persisted through ``save_generation()``
        and the secret through ``save_api_key()`` — so committing the key can
        no longer be confused with saving a spinbox, and saving a spinbox can
        no longer destroy the key.
        """
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
            # Mirror the legacy cfg field so the Extract page can pick it up,
            # then persist the two halves separately.
            self.win.cfg["api_key"] = current.api_key
            self.win.save_generation()
            self.win.save_api_key()
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

    def cfg_chart_type(self) -> str:
        """The chart-type code currently persisted in the shared cfg."""
        return (self.win.cfg or {}).get("chart_type", "auto")

    def cfg_chart_lang(self) -> str:
        """The chart-language code currently persisted in the shared cfg."""
        return (self.win.cfg or {}).get("chart_lang", "auto")

    def set_selector_mirror_collapsed(self, collapsed: bool = True) -> None:
        """Hide / show the chart-type + chart-language mirror row.

        AUDIT-2026-09-27 [item 3.1] (D2). Since the Extract page became the
        canonical home for these two, the copies here are no longer a second
        control the user is expected to edit — they are state, and showing
        state as an editable control is how the two drifted in the first
        place. The holder is COLLAPSED rather than the widgets destroyed,
        because ``_cycle_lang`` retranslate still walks them and the existing
        tests still address ``settings_page.cmb_ctype``.

        The holder is built unconditionally in ``_build``, and the only caller
        (``ExtractPage._adopt_chart_selectors``) runs after that, so the
        previous ``getattr`` + blanket ``except: pass`` had nothing left to
        guard and only hid real errors.
        """
        self._selector_mirror_holder.setVisible(not collapsed)

    def _save_advanced(self) -> None:
        """Persist the generation parameters.

        AUDIT-2026-09-27 [item 1.2]: calls ``save_generation()`` only. The old
        combined ``save_all()`` also rewrote ``api_key`` from the live field,
        so with "remember" off this button blanked the stored credential.

        B-08: the combo indices are bound-checked here too, not only in the
        accessors. ``retranslate`` calls ``clear()`` before re-adding items,
        which drives ``currentIndex()`` to -1; indexing the code list with it
        raised IndexError or silently selected the wrong mode. Today nothing
        yields the event loop between those calls, so the -1 is only one
        statement of luck away from being read here.
        """
        try:
            ctype_idx = self.cmb_ctype.currentIndex()
            clang_idx = self.cmb_clang.currentIndex()
            ctype_codes = self._ctype_codes
            clang_codes = self._clang_codes
            if not (0 <= ctype_idx < len(ctype_codes)):
                ctype_idx = 0
            if not (0 <= clang_idx < len(clang_codes)):
                clang_idx = 0
            self.win.cfg.update({
                "max_tokens": int(self.spin_maxtok.value()),
                "max_edge": int(self.spin_maxedge.value()),
                "runs": int(self.spin_runs.value()),
                "chart_type": ctype_codes[ctype_idx],
                "chart_lang": clang_codes[clang_idx],
                "remember": bool(self.sw_remember.isChecked()),
                "enhance": bool(self.sw_enhance.isChecked()),
            })
            self.win.save_generation()
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
        # AUDIT-2026-09-27 [item 1.2]: btn_save_key no longer exists (the key
        # field self-commits on Enter / focus-out), so drop its retranslate.
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
            # AUDIT-2026-09-27 [item 2.1] (U-02): pass the record's stored
            # thumbnail so the reviewer can see the figure the result came
            # from. It is display-only (see _show_history_thumbnail) and does
            # not re-arm extraction.
            self.extract_page.load_result(
                rec.result, rec.mode or "range_chart", record_id=rec.id,
                thumbnail_b64=getattr(rec, "image_thumbnail_b64", None))
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
        # REVIEW-2026-09-20: save_config now raises on a failed write instead
        # of swallowing it. The language switch itself must still happen (the
        # session is usable), but the user is told the choice will not survive
        # the next launch.
        try:
            save_config(self.cfg)
        except Exception as exc:
            try:
                InfoBar.error(
                    "", self._t("err.exportFailed") + str(exc), parent=self,
                    position=InfoBarPosition.TOP, duration=5000,
                )
            except Exception:
                pass
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
        # AUDIT-2026-09-27 [item 3.1] (D2): the Extract page owns this.
        page = getattr(self, "extract_page", None)
        if page is not None:
            return page.chart_type_code()
        idx = self.settings_page.cmb_ctype.currentIndex()
        codes = self.settings_page._ctype_codes
        if 0 <= idx < len(codes):
            return codes[idx]
        return codes[0] if codes else "auto"

    def chart_lang(self):
        # AUDIT-2026-09-27 [item 3.1] (D2): the Extract page owns this.
        page = getattr(self, "extract_page", None)
        if page is not None:
            return page.chart_lang_code()
        idx = self.settings_page.cmb_clang.currentIndex()
        codes = self.settings_page._clang_codes
        if 0 <= idx < len(codes):
            return codes[idx]
        return codes[0] if codes else "auto"

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

    def save_generation(self):
        """Persist the NON-SECRET settings into the shared config file.

        AUDIT-2026-09-27 [item 1.2] (P0): this is the former ``save_all`` with
        the ``api_key`` line removed, and it is the ONLY thing the close path
        and the "Save settings" button call. The old shape wrote all nine
        keys from the live widgets on every save, which meant:

        * pressing the button on the API-Key card also silently persisted
          every spinbox / switch the user had edited but not yet saved;
        * and because it wrote ``"api_key": ... if remember else ""``, pressing
          ANY save while "remember" was off wiped ``cfg["api_key"]`` — the
          user's stored credential destroyed by clicking an unrelated button.

        Splitting the secret out is what makes "save the settings" and "save
        the key" two different operations, so neither can surprise the other.
        REVIEW-2026-09-20 still holds: ``save_config`` propagates write errors
        and every caller shows an InfoBar, so "saved" is only claimed when the
        file really was written.
        """
        s = self.settings_page
        # endpoint / model live in the provider store, not in cfg.
        self.cfg.update({
            "lang": self.tr.lang,
            "max_tokens": self.max_tokens(),
            "max_edge": self.max_edge(),
            "runs": self.runs(),
            "chart_lang": self.chart_lang(),
            "chart_type": self.chart_type(),
            "remember": s.sw_remember.isChecked(),
            "enhance": self.enhance(),
        })
        save_config(self.cfg)

    def save_api_key(self):
        """Mirror the live API-key field into ``cfg`` and persist it.

        AUDIT-2026-09-27 [item 1.2]: separated from :meth:`save_generation` so
        the key is written only when the user actually edited it (the field
        commits on Enter / focus-out, or via the wizard). ``remember`` off
        still means "do not store it", and that is now reached ONLY through
        this explicit path rather than as a side effect of saving a spinbox.

        The ``remember`` flag itself lives in the generation half; read it
        from the persisted config so the two halves cannot disagree.
        """
        s = self.settings_page
        remember = bool(self.cfg.get("remember"))
        text = s.ipt_key.text().strip()
        self.cfg["api_key"] = text if remember else ""
        save_config(self.cfg)
        # H3 fix (REVIEW-2026-11-07): the Tkinter GUI already warned (via
        # log) when at-rest protection is below Fernet; the Fluent save
        # path had no warning at all. Surface it once per session when a
        # key is actually being stored with the weaker protection.
        if remember and text:
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

    def save_all(self):
        """Backwards-compatible alias for :meth:`save_generation`.

        AUDIT-2026-09-27 [item 1.2]: kept because external callers and older
        tests still use this name, but it deliberately does NOT write the API
        key any more — that is what made the old combined save destructive.
        Use :meth:`save_api_key` when the key really changed.
        """
        self.save_generation()

    def closeEvent(self, event):
        try:
            # Before the cfg blob is written, mirror the live ipt_key into the
            # provider store. The provider record is the one the next launch
            # reads from when populating ipt_key. Without this sync a user who
            # types a key and closes the app without committing the field sees
            # it empty next time.
            #
            # AUDIT-2026-09-27 [item 1.2]: the old comment here said
            # "save_all() already copies it into cfg["api_key"]" — that is no
            # longer true, and deliberately so: save_all() is now
            # save_generation(), which never touches the secret. Closing the
            # window must not be able to blank a stored API key.
            try:
                self._sync_ipt_key_to_provider()
            except Exception:
                pass
            self.save_generation()
            self.extract_page._cleanup_paste_tmp()
        except Exception as exc:
            # REVIEW-2026-09-20: save_all() now propagates write failures
            # (see save_config). The window is closing, so an InfoBar is
            # pointless — but a silent teardown loses the only breadcrumb of
            # "settings did not persist"; log it instead.
            log.exception("close-time settings save failed: %s", exc)
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
        #
        # AUDIT-2026-09-27 P1: the waits below used to be bare
        # ``QThread.wait()``, which blocks the event loop and froze the window
        # for the whole budget — and the budget was ADDITIVE per worker
        # (~17.5 s with the extract worker, the settings probe and three
        # provider probes in flight). They now go through
        # ``_wait_worker_briefly`` (which pumps events so the window keeps
        # painting) and share ONE total allowance, so the number of workers
        # can no longer multiply the freeze. Anything still running when its
        # share runs out is parked, which was already the safe path.
        _CLOSE_WAIT_TOTAL_MS = 2500
        _spent_ms = 0

        def _wait(w, default_ms: int) -> bool:
            nonlocal _spent_ms
            from PySide6.QtCore import QElapsedTimer
            allowed = min(default_ms, max(0, _CLOSE_WAIT_TOTAL_MS - _spent_ms))
            if allowed <= 0 or w is None:
                return w is not None and not w.isRunning()
            clock = QElapsedTimer()
            clock.start()
            done = _wait_worker_briefly(w, allowed)
            _spent_ms += min(clock.elapsed(), allowed)
            return done

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
                    if not _wait(w, 1500):
                        try:
                            log.warning(
                                "ExtractWorker did not honour cancel in time; "
                                "leaving thread to finish without UI callbacks."
                            )
                        except Exception:
                            pass
                    # Whatever happened, the register keeps the wrapper alive
                    # so the thread can finish safely (no qFatal, no
                    # terminate()) and deleteLater() reclaims it.
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
                    _wait(sw, 500)
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
                        # 500 ms each, and only from what is left of the
                        # shared total — three probes must not cost 7.5 s.
                        if not _wait(tw, 500):
                            try:
                                log.warning(
                                    "Test worker did not finish in time; "
                                    "abandoning without terminate()."
                                )
                            except Exception:
                                pass
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
