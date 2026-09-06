"""Sprint B (REVIEW-2026-09-04) GUI regression tests.

Covers the reviewed GUI bugs that needed regression guards:

Item 3 — per-future timeout was dead code (as_completed only yields
         ALREADY-finished futures, so fut.result(timeout=...) never fired).
         Both GUI workers now collect under a real whole-batch budget via a
         testable pure helper (_collect_extraction_futures).
Item 4 — a fresh extraction did not clear ExtractPage._loaded_history_id,
         so "Apply Edits" after a new extraction overwrote the history
         record the user had merely been viewing.

The pure tests run everywhere (no Qt / display needed). The Qt tests use
the tests_gui_fluent.py headless pattern (QT_QPA_PLATFORM=offscreen) and
SKIP cleanly when PySide6 / qfluentwidgets are not installed.

Run:  python -m pytest tests/test_gui_sprint_b.py -q
"""
from __future__ import annotations

import concurrent.futures
import os
import queue
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.extractor import ExtractResult

try:
    import pytest  # noqa: F401
    _HAS_PYTEST = True
except Exception:  # pragma: no cover
    _HAS_PYTEST = False


def _have_pyside():
    try:
        import PySide6  # noqa: F401
        import qfluentwidgets  # noqa: F401
        return True
    except Exception:
        return False


PYSIDE = _have_pyside()


def _skip_no_pyside(obj):
    """skipUnless that also works when pytest is unavailable."""
    reason = "PySide6 / qfluentwidgets not installed"
    if _HAS_PYTEST:
        import pytest
        return pytest.mark.skipif(not PYSIDE, reason=reason)(obj)
    return unittest.skipUnless(PYSIDE, reason)(obj)


# ---------------------------------------------------------------------------
# Item 3 — real batch budget (pure helpers, no Qt)
# ---------------------------------------------------------------------------
class TestCollectExtractionFutures:
    """Regression: the old as_completed()+fut.result(timeout=...) loop could
    never time out because as_completed only yields finished futures."""

    def test_overrun_run_reported_as_timeout(self):
        import gui
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            def fast():
                return ExtractResult(ok=True, data={"n": 1}, raw="{}")
            def slow():
                time.sleep(1.0)
                return ExtractResult(ok=True, data={"n": 2}, raw="{}")
            futures = [ex.submit(fast), ex.submit(slow)]
            results = gui._collect_extraction_futures(futures, 0.15)
        finally:
            # Must NOT join: the caller must never block on overrun runs.
            ex.shutdown(wait=False)

        assert len(results) == 2
        ok_results = [r for r in results if r.ok]
        timeouts = [r for r in results if r.error_key == "err.timeout"]
        assert len(ok_results) == 1, f"expected the fast run to succeed: {results}"
        assert len(timeouts) == 1, (
            "Sprint B regression: a run that exceeds the batch budget must be "
            f"reported as err.timeout (got: {results})")
        assert "timeout" in (timeouts[0].error_body or "")

    def test_no_timeout_when_all_runs_finish(self):
        import gui
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            futures = [
                ex.submit(lambda i=i: ExtractResult(ok=True, data={"n": i}, raw="{}"))
                for i in range(2)
            ]
            results = gui._collect_extraction_futures(futures, 10.0)
        finally:
            ex.shutdown(wait=False)
        assert len(results) == 2
        assert all(r.ok for r in results)
        assert not any(r.error_key == "err.timeout" for r in results)

    def test_worker_budget_uses_params_timeout_sec(self):
        """The worker must honour params["timeout_sec"] — before Sprint B no
        caller ever put the key into params, so the budget read a magic
        inline default and the timeout branch was unreachable."""
        import gui
        assert gui.EXTRACT_TIMEOUT_SEC >= 10
        calls = {"n": 0}

        def fake_extract(**kw):
            calls["n"] += 1
            assert "timeout_sec" in kw, (
                "Sprint B regression: _on_extract must wire timeout_sec into "
                "params so the worker budget has a real source")
            time.sleep(0.8)
            return ExtractResult(ok=True, data={"species_ranges": []}, raw="{}")

        orig = gui.extract
        gui.extract = fake_extract
        try:
            app = gui.RangeChartApp.__new__(gui.RangeChartApp)  # no Tk needed
            app.msg_queue = queue.Queue()
            app._worker(params={"timeout_sec": -9.9},  # budget = -9.9 + 10 = 0.1s
                        mode="range_chart", runs=2)
            result = app.msg_queue.get(timeout=5)
        finally:
            gui.extract = orig

        assert not result.ok
        assert result.error_key == "err.timeout"
        assert calls["n"] == 2

    def test_fluent_worker_helper_timeout(self):
        """Same semantics for the Qt twin helper in gui_fluent (skipped
        without PySide6)."""
        if not PYSIDE:
            raise unittest.SkipTest("PySide6 / qfluentwidgets not installed")
        import gui_fluent
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            def slow():
                time.sleep(1.0)
                return ExtractResult(ok=True, data={"n": 1}, raw="{}")
            futures = [ex.submit(slow)]
            results, cancelled = gui_fluent._collect_extraction_futures(
                futures, 0.15)
        finally:
            ex.shutdown(wait=False)
        assert cancelled is False
        assert len(results) == 1
        assert results[0].error_key == "err.timeout"


# ---------------------------------------------------------------------------
# Items 9 + 10 — per-run raws + resolved mode reach the history layer
# ---------------------------------------------------------------------------
class TestWorkerAttachments:
    def test_gui_worker_attaches_raws_and_mode(self):
        import gui
        def fake_extract(**kw):
            return ExtractResult(ok=True,
                                 data={"species_ranges": [{"species": "A"}]},
                                 raw="RUNTXT")

        orig = gui.extract
        gui.extract = fake_extract
        try:
            app = gui.RangeChartApp.__new__(gui.RangeChartApp)
            app.msg_queue = queue.Queue()
            app._worker(params={"timeout_sec": 30}, mode="columnar_section",
                        runs=2)
            result = app.msg_queue.get(timeout=5)
        finally:
            gui.extract = orig

        assert result.ok
        # Sprint B item 9: getattr(result, "_raws") was a dead attribute —
        # the per-run raw responses must now be attached.
        raws = getattr(result, "_raws", None)
        assert raws == ["RUNTXT", "RUNTXT"]
        # Sprint B item 10: the resolved mode must ride on the result so
        # history records never store the raw "auto" dropdown value.
        assert getattr(result, "_mode", "") == "columnar_section"

    def test_gui_worker_single_run_attaches_mode(self):
        import gui
        def fake_extract(**kw):
            return ExtractResult(ok=True,
                                 data={"species_ranges": [{"species": "A"}]},
                                 raw="R")

        orig = gui.extract
        gui.extract = fake_extract
        try:
            app = gui.RangeChartApp.__new__(gui.RangeChartApp)
            app.msg_queue = queue.Queue()
            app._worker(params={"timeout_sec": 30}, mode="phylogenetic_tree",
                        runs=1)
            result = app.msg_queue.get(timeout=5)
        finally:
            gui.extract = orig

        assert result.ok
        assert getattr(result, "_mode", "") == "phylogenetic_tree"

    def test_save_to_history_persists_worker_mode_not_auto(self):
        """gui.py used to store the raw var_chart_type dropdown value ("auto")
        into the history record; the Fluent history filter only matches real
        modes."""
        import gui

        class _FakeStore:
            added = []
            def __init__(self, db=None):
                pass
            def add(self, rec, raw_responses=None):
                _FakeStore.added.append((rec, raw_responses))
                return 1

        app = gui.RangeChartApp.__new__(gui.RangeChartApp)
        app.result = {"species_ranges": [{"species": "A"}]}
        app.image_path = ""
        app._img_dims = (0, 0, False)
        app._active_provider = None

        result = ExtractResult(ok=True, data=app.result, raw="R")
        result._mode = "columnar_section"
        result._raws = ["R"]

        orig_store, orig_db = gui.HistoryStore, gui.Database
        gui.HistoryStore, gui.Database = _FakeStore, lambda: None
        try:
            app._save_to_history(result)
        finally:
            gui.HistoryStore, gui.Database = orig_store, orig_db

        assert _FakeStore.added, "history record was not written"
        rec, raw_responses = _FakeStore.added[-1]
        assert rec.mode == "columnar_section", (
            "Sprint B regression: history record must store the worker-"
            f"resolved mode, got {rec.mode!r}")
        assert raw_responses and raw_responses[0]["raw_text"] == "R"

    def test_save_to_history_shape_fallback_without_worker_mode(self):
        import gui

        class _FakeStore:
            added = []
            def __init__(self, db=None):
                pass
            def add(self, rec, raw_responses=None):
                _FakeStore.added.append((rec, raw_responses))
                return 1

        app = gui.RangeChartApp.__new__(gui.RangeChartApp)
        # Shape-based resolution: sections with id but no name -> columnar.
        app.result = {"sections": [{"id": "S1", "group": "g"}]}
        app.image_path = ""
        app._img_dims = (0, 0, False)
        app._active_provider = None

        result = ExtractResult(ok=True, data=app.result, raw="")  # no _mode

        orig_store, orig_db = gui.HistoryStore, gui.Database
        gui.HistoryStore, gui.Database = _FakeStore, lambda: None
        try:
            app._save_to_history(result)
        finally:
            gui.HistoryStore, gui.Database = orig_store, orig_db

        rec, _ = _FakeStore.added[-1]
        assert rec.mode == "columnar_section"

    def test_resolved_mode_shapes(self):
        import gui
        app = gui.RangeChartApp.__new__(gui.RangeChartApp)
        cases = [
            ({"nodes": [{"id": "n"}]}, "phylogenetic_tree"),
            ({"abundances": []}, "abundance_diagram"),
            ({"sections": [{"id": "S", "group": "g"}]}, "columnar_section"),
            ({"sections": [{"id": "S", "name": "x"}],
              "species_ranges": []}, "range_chart"),
            ({}, "range_chart"),
        ]
        for data, expected in cases:
            app.result = data
            assert app._resolved_mode() == expected, f"shape {data}"


# ---------------------------------------------------------------------------
# Item 3 — Qt ExtractWorker end-to-end (skipped without PySide6)
# ---------------------------------------------------------------------------
@_skip_no_pyside
class TestFluentExtractWorkerTimeout:
    def test_worker_emits_timeout_when_run_overruns_budget(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        import gui_fluent

        def fake_extract(**kw):
            time.sleep(1.0)
            return ExtractResult(ok=True, data={"species_ranges": []}, raw="{}")

        import rca_core.extractor as E
        orig_ext, orig_gf = E.extract, gui_fluent.extract
        E.extract = fake_extract
        gui_fluent.extract = fake_extract
        try:
            w = gui_fluent.ExtractWorker(
                params={"api_key": "k", "image_b64": "QUFB",
                        "media_type": "image/png", "timeout_sec": -9.9},
                mode="range_chart", runs=2)
            results = []
            w.finished_ok.connect(lambda r: results.append(r))
            w.start()
            deadline = time.time() + 5
            while not results and time.time() < deadline:
                app.processEvents()
                time.sleep(0.02)
            assert w.wait(5000)
            assert len(results) == 1
            assert not results[0].ok
            assert results[0].error_key == "err.timeout", (
                "Sprint B regression: over-running runs must surface as "
                "err.timeout instead of pinning the batch")
        finally:
            E.extract, gui_fluent.extract = orig_ext, orig_gf


# ---------------------------------------------------------------------------
# Item 2 — cooperative cancel before submit (skipped without PySide6)
# ---------------------------------------------------------------------------
@_skip_no_pyside
class TestFluentExtractWorkerCancel:
    def test_cancel_before_submit_prevents_runs(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        import gui_fluent

        calls = {"n": 0}
        def fake_extract(**kw):
            calls["n"] += 1
            return ExtractResult(ok=True, data={"species_ranges": []}, raw="{}")

        import rca_core.extractor as E
        orig_ext, orig_gf = E.extract, gui_fluent.extract
        E.extract = fake_extract
        gui_fluent.extract = fake_extract
        try:
            w = gui_fluent.ExtractWorker(
                params={"api_key": "k", "image_b64": "QUFB",
                        "media_type": "image/png", "timeout_sec": 30},
                mode="range_chart", runs=2)
            w.request_cancel()  # cancel BEFORE start -> before any submit
            results = []
            w.finished_ok.connect(lambda r: results.append(r))
            w.start()
            deadline = time.time() + 5
            while not results and time.time() < deadline:
                app.processEvents()
                time.sleep(0.02)
            assert w.wait(5000)
            assert len(results) == 1
            assert results[0].error_key == "err.cancelled"
            assert calls["n"] == 0, (
                "Sprint B regression: cancel before submit must not launch "
                "any extraction run")
        finally:
            E.extract, gui_fluent.extract = orig_ext, orig_gf


# ---------------------------------------------------------------------------
# Item 4 — fresh extraction clears _loaded_history_id (Qt window flow)
# ---------------------------------------------------------------------------
def _shiboken_delete(obj) -> bool:
    for _imp in ("shiboken6", "PySide6.shiboken"):
        try:
            mod = __import__(_imp, fromlist=["delete"])
            mod.delete(obj)
            return True
        except Exception:
            continue
    return False


def _destroy_window(win):
    """Eager C++ deletion of the Fluent window (WebEngine teardown order —
    see tests/test_gui_fluent_low_fixes.py for the full rationale)."""
    try:
        page = win.extract_page
        view = getattr(page, "phylotree", None)
        web_page = view.page() if view is not None else None
        if web_page is not None:
            view.setPage(None)
            _shiboken_delete(web_page)
    except Exception:
        pass
    win.close()
    if not _shiboken_delete(win):
        win.deleteLater()


@_skip_no_pyside
class TestLoadedHistoryIdLifecycle:
    def _make_window(self):
        import gui_fluent
        from rca_core.history import Database, HistoryStore
        tmp = tempfile.mkdtemp(prefix="rca_sprint_b_")
        db = Database(os.path.join(tmp, "hist.db"))
        hs = HistoryStore(db=db)
        win = gui_fluent.RangeChartFluentWindow()
        # Deterministic offline window: no provider lookup, no usage writes,
        # no history-page refresh side effects.
        win.current_provider = lambda: None
        win._provider_store = None
        win._usage_store = None
        win._history_store = hs
        win.history_page = None
        return win, hs

    def test_new_extraction_clears_loaded_history_id(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from rca_core.extractor import ExtractResult
        from rca_core.history import HistoryRecord
        import gui_fluent

        win, hs = self._make_window()
        try:
            old = HistoryRecord(
                timestamp=time.time(), source_file="old.png",
                mode="range_chart",
                result={"species_ranges": [{"species": "Old"}]},
            )
            old_id = hs.add(old)
            page = win.extract_page

            page.load_result(hs.get(old_id).result, "range_chart",
                             record_id=old_id)
            assert page._loaded_history_id == old_id
            assert page.image_b64 is None  # item 11: full image wipe
            assert page.media_type is None
            assert page.image_path is None

            # A fresh extraction lands (image-less on purpose).
            page._on_result(ExtractResult(
                ok=True,
                data={"species_ranges": [{"species": "New"}]},
                raw="R", truncated=False))

            assert page._loaded_history_id is None, (
                "Sprint B regression: a new extraction must clear "
                "_loaded_history_id, otherwise Apply Edits overwrites the "
                "record the user was merely viewing")

            # "Apply Edits"-time persistence must NOT touch the old record.
            page._persist_edits_to_history()
            assert hs.get(old_id).result == {
                "species_ranges": [{"species": "Old"}]
            }, "Sprint B regression: edits leaked into a stale history record"

            # And the fresh extraction must have produced its own record.
            # (history.list(search=...) matches source_file/notes/provider/
            # model only — never the result payload — and this image-less
            # record has source_file=None, so assert on the payload instead.)
            recs = hs.list(limit=50)
            assert recs, "history store is empty"
            assert any(
                (r.result or {}).get("species_ranges")
                and (r.result["species_ranges"] or [{}])[0].get("species") == "New"
                for r in recs
            ), "fresh extraction was not persisted"
        finally:
            _destroy_window(win)

    def test_load_result_wipes_ghost_image_state(self):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from rca_core.history import HistoryRecord
        import gui_fluent

        win, hs = self._make_window()
        try:
            page = win.extract_page
            page.image_path = "somewhere/ghost.png"
            page.image_b64 = "R0hPU1Q="
            page.media_type = "image/png"
            page._img_dims = (100, 80, False)

            rec = HistoryRecord(timestamp=time.time(),
                                mode="range_chart",
                                result={"species_ranges": []})
            rid = hs.add(rec)
            page.load_result(hs.get(rid).result, "range_chart", record_id=rid)

            assert page.image_path is None
            assert page.image_b64 is None, (
                "Sprint B regression: load_result must clear image_b64 too — "
                "a half-wipe allowed ghost-image extractions in the "
                "apparently image-less state")
            assert page.media_type is None
            assert page._img_dims == (0, 0, False)
        finally:
            _destroy_window(win)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
