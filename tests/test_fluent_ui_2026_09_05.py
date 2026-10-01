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
        if importlib.util.find_spec("tests.test_gui_sprint_b"):
            mod = importlib.import_module("tests.test_gui_sprint_b")
        else:
            mod = importlib.import_module("test_gui_sprint_b")
        mod._destroy_window(win)
        # AUDIT-2026-09-27: drain the event loop so the C++ deletion actually
        # COMPLETES before the next test builds a window. shiboken.delete() only
        # schedules the destruction; WebEngine's own teardown is asynchronous,
        # so window N+1 could otherwise be constructed while window N was still
        # being torn down and die with an access violation partway through
        # SettingsPage.__init__ (seen ~1 run in 2 on this file). Draining is a
        # mitigation, not a proof — the teardown ordering itself is still the
        # WebEngine one documented in tests/test_gui_sprint_b.py.
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for _ in range(3):
                app.processEvents()

    @_requires_qt
    def test_empty_state_disables_export_actions(self):
        """The action gate, pinned to the contract that replaced a defective one.

        AUDIT-2026-09-27 [item 1.3] (B-03). This test used to assert that
        merely having ``page.result`` enabled all six buttons. That WAS the
        defect: a result carrying zero rendered tables left Add / Delete /
        Discard / Apply live, and every one of them returned silently — Apply
        iterated an empty dict and showed no toast at all. The edit row was
        also never hidden at construction, so a fresh launch showed four
        dead buttons, and nothing disabled the row-edit buttons while an
        extraction was running.

        The gate is now ``_update_result_actions()`` alone, and it requires a
        result AND a rendered table AND (for the exports) a pending image AND
        not-busy. The assertions below are therefore STRICTER than the ones
        they replace: three states are now pinned that the old test could not
        express.
        """
        win, _hs = self._make_window()
        try:
            page = win.extract_page
            edit_buttons = (page.btn_add_row, page.btn_del_row,
                            page.btn_discard, page.btn_apply_edits)
            all_buttons = (page.btn_export, page.btn_export_xlsx) + edit_buttons

            # 1. Fresh page: everything disabled AND the edit row not shown.
            #    `isHidden()` (not `isVisible()`) is the right probe here: it
            #    reports the widget's OWN explicit hidden flag, whereas
            #    isVisible() also folds in every ancestor — and this test
            #    never calls win.show(), so the whole tree is hidden and
            #    isVisible() would read False for a row that IS shown.
            for b in all_buttons:
                assert not b.isEnabled(), "actions must start disabled"
            assert page.edit_row_widget.isHidden(), (
                "the row-edit row must be hidden before any result, not shown "
                "with four dead buttons")

            # 2. A result with NO rendered table must NOT enable the row-edit
            #    buttons. This is the state the old test wrongly blessed.
            page.result = {"species_ranges": [{"species": "X"}]}
            page.tables = {}
            page._update_result_actions()
            for b in edit_buttons:
                assert not b.isEnabled(), (
                    "a result with no rendered table cannot be row-edited; "
                    "enabling these produced four buttons that silently no-op")
            assert page.edit_row_widget.isHidden(), \
                "the edit row must stay hidden while there is no table"

            # 3. A real result WITH a table and a pending image enables them.
            page.tables = {"species_ranges": object()}
            page.image_b64 = "QUFB"
            page._update_result_actions()
            assert not page.edit_row_widget.isHidden(), \
                "the edit row must appear once there is a result to edit"
            for b in all_buttons:
                assert b.isEnabled(), \
                    "actions must enable once a result, a table and an image exist"

            # 4. Going busy disables them again — previously only the Extract
            #    and export buttons were touched, so a row edit could land on
            #    the previous result mid-run.
            #    Mirror the production order: `_on_extract` sets the flag on
            #    the line BEFORE calling `_set_busy` (gui_fluent.py:1445/1503),
            #    and the gate reads `self.busy`, so the test has to do the same
            #    or it is asserting against a stale flag.
            page.busy = True
            page._set_busy(True)
            try:
                for b in all_buttons:
                    assert not b.isEnabled(), \
                        "no action may stay live while a worker is running"
            finally:
                page.busy = False
                page._set_busy(False)
        finally:
            self._destroy_window(win)

    @_requires_qt
    def test_extract_page_owns_the_chart_selectors(self):
        """D2: the Extract page's inline selectors are the canonical state.

        This used to be ``test_inline_selectors_mirror_settings`` and it
        asserted the OPPOSITE direction — that a change on the inline combo
        writes through to Settings "as the source of truth". AUDIT-2026-09-27
        reversed that: the control sitting next to "choose an image" is the one
        the user's action depends on, and the Settings pair became a HIDDEN
        MIRROR. The old contract made the contextually-right control a pure
        view of a control the user had to go elsewhere to change, and kept two
        copies in sync by hand.

        The cross-page INDEX equality is deliberately no longer asserted. It is
        still true, but it is now a consequence of the mirror, not the
        contract; pinning it as the contract is what let the two drift apart in
        the first place. What is pinned instead:
          * the Extract page OWNS the code tables,
          * the window accessors read the Extract page,
          * a change on the inline combo propagates to the mirror,
          * a change on the mirror is pulled back (so the two can never
            disagree even if something drives the hidden control directly).
        """
        win, _hs = self._make_window()
        try:
            page = win.extract_page
            sp = win.settings_page
            # UI-REVIEW-2026-09-05: zonation_chart added -> 6 options.
            assert page.cmb_ctype_inline.count() == 6
            assert page.cmb_clang_inline.count() == 5
            # D2: the tables live on the Extract page now.
            assert page._ctype_codes[0] == "auto"
            assert "zonation_chart" in page._ctype_codes
            assert page._clang_codes == ["auto", "zh", "en", "ja", "ru"]
            # The window accessors read the Extract page.
            assert win.chart_type() == page.chart_type_code()
            assert win.chart_lang() == page.chart_lang_code()
            # The mirror row is collapsed, not destroyed.
            assert sp._selector_mirror_holder.isHidden(), (
                "the Settings copy of the chart selectors must be hidden — it "
                "is state now, and showing state as an editable control is how "
                "the two drifted")

            # inline change -> mirror follows
            page.cmb_ctype_inline.setCurrentIndex(1)
            assert win.chart_type() == "range_chart"
            assert sp.cmb_ctype.currentIndex() == 1

            # mirror changed directly -> pulled back, so they cannot disagree
            sp.cmb_ctype.setCurrentIndex(3)
            assert page.cmb_ctype_inline.currentIndex() == 3
            assert win.chart_type() == page._ctype_codes[3]

            # same for the language pair
            page.cmb_clang_inline.setCurrentIndex(2)
            assert win.chart_lang() == "en"
            assert sp.cmb_clang.currentIndex() == 2
        finally:
            self._destroy_window(win)


if __name__ == "__main__":
    unittest.main()
