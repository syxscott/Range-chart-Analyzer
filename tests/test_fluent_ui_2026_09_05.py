"""Regression tests for UI-REVIEW-2026-09-05 Fluent GUI fixes."""

import importlib
import unittest


def _qt_available():
    try:
        import PySide6  # noqa: F401
        import qfluentwidgets  # noqa: F401
        return True
    except Exception:
        return False


import pytest

_requires_qt = pytest.mark.skipif(not _qt_available(), reason="PySide6/qfluentwidgets unavailable")


@_requires_qt
class TestProvenancePortFallback(unittest.TestCase):
    # gui_fluent_history_detail imports PySide6 at module level - these
    # tests need the same skip guard as the Qt-bound classes below.
    """_read_lock_port must return None (not 8000) when no lock exists."""

    def test_no_lock_returns_none(self):
        import gui_fluent_history_detail as hd
        original = hd.LOCK_PATH
        try:
            hd.LOCK_PATH = "Z:/definitely/not/a/lock/file"
            assert hd._read_lock_port() is None
        finally:
            hd.LOCK_PATH = original

    def test_lock_with_content_returns_port(self):
        import tempfile, os
        import gui_fluent_history_detail as hd
        original = hd.LOCK_PATH
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".lock", delete=False) as f:
                f.write("127.0.0.1:8123")
                tmp = f.name
            hd.LOCK_PATH = tmp
            assert hd._read_lock_port() == 8123
        finally:
            hd.LOCK_PATH = original
            os.unlink(tmp)


class TestI18nNewKeys(unittest.TestCase):
    def test_new_keys_present_in_all_languages(self):
        from rca_core.i18n import TRANSLATIONS
        for lang in ("zh", "en", "ja"):
            for key in ("image.dropHint",
                        "history.filter.abundance",
                        "history.filter.phylo",
                        "history.detail.provenance.noServer"):
                assert key in TRANSLATIONS[lang], (lang, key)
                assert TRANSLATIONS[lang][key], (lang, key)


@_requires_qt
class TestDetailDialogInlineCss(unittest.TestCase):
    def test_inline_css_uses_grid_ring(self):
        import gui_fluent_history_detail as hd
        import inspect
        src = inspect.getsource(hd)
        assert "grid-area: 1 / 1" in src
        # the old abspos .num strategy must be gone
        assert "position: absolute; font-size: 12px" not in src


class TestExtractPageUiStates(unittest.TestCase):
    """Window-level states use the same offline pattern as
    tests/test_gui_sprint_b.py::_make_window (temp DB, stubbed stores,
    eager WebEngine teardown) so the test is safe in full-suite runs."""

    def _make_window(self):
        import os
        import tempfile
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        import gui_fluent
        from rca_core.history import Database, HistoryStore
        tmp = tempfile.mkdtemp(prefix="rca_fluent_ui_")
        db = Database(os.path.join(tmp, "hist.db"))
        hs = HistoryStore(db=db)
        win = gui_fluent.RangeChartFluentWindow()
        win.current_provider = lambda: None
        win._provider_store = None
        win._usage_store = None
        win._history_store = hs
        win.history_page = None
        return win, hs

    def _destroy_window(self, win):
        # Reuse the teardown helpers proven suite-safe in
        # tests/test_gui_sprint_b.py (WebEngine teardown ordering).
        import importlib
        mod = importlib.import_module("tests.test_gui_sprint_b")             if importlib.util.find_spec("tests.test_gui_sprint_b")             else importlib.import_module("test_gui_sprint_b")
        mod._destroy_window(win)

    @_requires_qt
    def test_empty_state_disables_export_actions(self):
        win, _hs = self._make_window()
        try:
            page = win.extract_page
            for b in (page.btn_export, page.btn_export_xlsx,
                      page.btn_add_row, page.btn_del_row,
                      page.btn_discard, page.btn_apply_edits):
                assert not b.isEnabled(), "export/edit actions must start disabled"
            # a result enables them again
            page.result = {"species_ranges": [{"species": "X"}]}
            page._update_result_actions()
            for b in (page.btn_export, page.btn_export_xlsx,
                      page.btn_add_row, page.btn_del_row,
                      page.btn_discard, page.btn_apply_edits):
                assert b.isEnabled(), "actions must enable once a result exists"
        finally:
            self._destroy_window(win)

    @_requires_qt
    def test_inline_selectors_mirror_settings(self):
        win, _hs = self._make_window()
        try:
            page = win.extract_page
            # UI-REVIEW-2026-09-05: zonation_chart added → 6 options.
            assert page.cmb_ctype_inline.count() == 6
            assert page.cmb_clang_inline.count() == 5
            assert page.cmb_ctype_inline.currentIndex() ==                 win.settings_page.cmb_ctype.currentIndex()
            # inline change must write through to settings (source of truth)
            page.cmb_ctype_inline.setCurrentIndex(1)
            assert win.settings_page.cmb_ctype.currentIndex() == 1
            # and back
            win.settings_page.cmb_ctype.setCurrentIndex(0)
            assert page.cmb_ctype_inline.currentIndex() == 0
        finally:
            self._destroy_window(win)


if __name__ == "__main__":
    unittest.main()
