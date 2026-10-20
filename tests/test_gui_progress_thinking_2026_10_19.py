"""The extraction page's progress bar and the model's own thinking.

The occurrence-matrix path makes FOUR calls for a typical figure (one for the
axis labels, one per contact sheet) and each of them can take a minute:
measured on the reference figure, the label call alone spent 28 s on the
"low" reasoning level, 48 s on "high" and 69-102 s on "max". A spinner that
says "working" for two minutes tells the operator nothing, and there is no
way to tell a slow extraction from a hung one.

So the page carries a determinate bar (the fraction of the matrix cells the
reader has actually read) and ONE elided line of the model's own thinking,
which arrives while the model is still thinking because the two-stage path
streams its calls.

Everything here is Qt-gated, and the file name carries "gui" so the `qt`
job's `-k "gui or fluent ..."` filter picks it up.
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                                  # noqa: E402


def _qt_available():
    try:
        import PySide6            # noqa: F401
        import qfluentwidgets     # noqa: F401
        return True
    except Exception:
        return False


_requires_qt = pytest.mark.skipif(
    not _qt_available(), reason="PySide6/qfluentwidgets unavailable")


@pytest.fixture(scope="module")
def page():
    """The real extract page, built the way the existing GUI suites build it.

    A full window (temp history DB, stubbed stores) rather than a stub
    object: the point of these assertions is that the WIDGETS exist, are
    sized compactly and react to the events.
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import gui_fluent
    from rca_core.history import Database, HistoryStore
    tmp = tempfile.mkdtemp(prefix="rca_progress_")
    hs = HistoryStore(db=Database(os.path.join(tmp, "hist.db")))
    win = gui_fluent.RangeChartFluentWindow()
    win.current_provider = lambda: None
    win._provider_store = None
    win._usage_store = None
    win._history_store = hs
    win.history_page = None
    yield win.extract_page
    try:
        import importlib
        import importlib.util
        if importlib.util.find_spec("tests.test_gui_sprint_b"):
            mod = importlib.import_module("tests.test_gui_sprint_b")
        else:
            mod = importlib.import_module("test_gui_sprint_b")
        mod._destroy_window(win)
    except Exception:
        pass
    app = QApplication.instance()
    if app is not None:
        for _ in range(3):
            app.processEvents()


@_requires_qt
def test_the_bar_and_the_thinking_line_exist(page):
    assert page.progress_bar is not None
    assert page.lbl_thinking is not None


@_requires_qt
def test_they_take_almost_no_room(page):
    """The user asked for a progress indicator that does not eat the page."""
    assert page.progress_bar.maximumHeight() <= 8, \
        "the bar must stay a hairline, not a block"
    assert page.lbl_thinking.maximumHeight() <= 24, \
        "the thinking must stay ONE line"


@_requires_qt
def test_they_are_hidden_until_a_run_starts(page):
    # `isVisible()` is False for any child of a window that was never shown,
    # so the EXPLICIT hidden state is what this is about.
    assert page.progress_bar.isHidden() is True
    assert page.lbl_thinking.isHidden() is True


@_requires_qt
def test_a_run_shows_them_and_resets_the_bar(page):
    page.progress_bar.setValue(750)
    page._set_busy(True)
    try:
        assert page.progress_bar.isHidden() is False
        assert page.progress_bar.value() == 0, \
            "a second run must not start with a bar three-quarters full"
    finally:
        page._set_busy(False)


@_requires_qt
def test_the_fraction_drives_the_bar(page):
    page._set_busy(True)
    try:
        page._on_progress("frac:0.667")
        assert 660 <= page.progress_bar.value() <= 670, \
            page.progress_bar.value()
    finally:
        page._set_busy(False)


@_requires_qt
def test_a_fraction_out_of_range_is_clamped(page):
    page._set_busy(True)
    try:
        page._on_progress("frac:5.0")
        assert page.progress_bar.value() == page.progress_bar.maximum()
        page._on_progress("frac:-1.0")
        assert page.progress_bar.value() == 0
    finally:
        page._set_busy(False)


@_requires_qt
def test_the_call_tokens_reach_the_status_line(page):
    page._set_busy(True)
    try:
        page._on_progress("call:cells:2/3")
        text = page.lbl_status.text()
        assert "2" in text and "3" in text, text
        page._on_progress("call:labels")
        assert page.lbl_status.text() != text
    finally:
        page._set_busy(False)


@_requires_qt
def test_the_thinking_is_shown_as_one_elided_line(page):
    page._set_busy(True)
    try:
        page._on_thinking("We need inspect the image carefully. ")
        page._on_thinking("Rows visible 22, authorities exact.")
        text = page.lbl_thinking.text()
        assert "Rows visible 22" in text
        assert "\n" not in text, "the thinking must be one line"
        assert len(text) <= 200, "the thinking must be elided"
    finally:
        page._set_busy(False)


@_requires_qt
def test_the_thinking_keeps_the_newest_text(page):
    page._set_busy(True)
    try:
        page._on_thinking("OLD " * 900)
        page._on_thinking("NEWEST")
        text = page.lbl_thinking.text()
        assert "NEWEST" in text
        assert len(text) <= 200, "the visible line must stay short"
        assert len(page._thinking_buf) <= 1200, \
            "the buffer must be bounded, or a long reasoning level grows it " \
            "without limit"
    finally:
        page._set_busy(False)


@_requires_qt
def test_an_empty_chunk_does_nothing(page):
    page._set_busy(True)
    try:
        page._on_thinking("")
        assert page.lbl_thinking.text() == ""
        assert page.lbl_thinking.isHidden() is True
    finally:
        page._set_busy(False)


@_requires_qt
def test_a_new_run_clears_the_previous_thinking(page):
    """A stale tooltip would answer with the PREVIOUS run's thinking."""
    page._set_busy(True)
    page._on_thinking("the previous run")
    page._set_busy(False)
    page._set_busy(True)
    try:
        assert page.lbl_thinking.text() == ""
        assert page.lbl_thinking.toolTip() == ""
    finally:
        page._set_busy(False)


# --- the settings page's effort picker --------------------------------------

@pytest.fixture(scope="module")
def settings_page(tmp_path_factory):
    """The settings page, with a REAL provider behind it.

    The picker writes to the provider RECORD (the effort is a property of the
    model, and the provider is what carries the model), so the test needs a
    real store to write to -- a stubbed one would prove nothing.
    """
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import gui_fluent
    from rca_core.history import Database, HistoryStore
    from rca_core.llm import LlmProvider, ProviderStore

    tmp = tmp_path_factory.mktemp("rca_effort")
    store = ProviderStore(str(tmp / "providers.json"))
    prov = LlmProvider(id="p1", name="MiniMax", api_format="anthropic",
                        endpoint="https://api.minimaxi.com/anthropic",
                        api_key="sk-test", model="MiniMax-M3.1-Flash",
                        reasoning_effort="low")
    store.add(prov)
    store.set_current("p1")

    hs = HistoryStore(db=Database(str(tmp / "hist.db")))
    win = gui_fluent.RangeChartFluentWindow()
    win.current_provider = lambda: store.get_current()
    win._provider_store = store
    win._usage_store = None
    win._history_store = hs
    win.history_page = None
    yield win.settings_page, store
    try:
        import importlib
        import importlib.util
        if importlib.util.find_spec("tests.test_gui_sprint_b"):
            mod = importlib.import_module("tests.test_gui_sprint_b")
        else:
            mod = importlib.import_module("test_gui_sprint_b")
        mod._destroy_window(win)
    except Exception:
        pass
    app = QApplication.instance()
    if app is not None:
        for _ in range(3):
            app.processEvents()


@_requires_qt
def test_the_settings_page_offers_the_models_own_levels(settings_page):
    page, _store = settings_page
    page.refresh_effort_options()
    assert page._effort_values == ["default", "low", "medium", "high",
                                   "xhigh", "max"]
    assert page.cmb_effort.isEnabled() is True


@_requires_qt
def test_the_stored_level_is_shown(settings_page):
    page, _store = settings_page
    page.refresh_effort_options()
    assert page._effort_values[page.cmb_effort.currentIndex()] == "low"


@_requires_qt
def test_choosing_a_level_writes_it_to_the_provider(settings_page):
    page, store = settings_page
    page.refresh_effort_options()
    page.cmb_effort.setCurrentIndex(page._effort_values.index("high"))
    assert store.get_current().reasoning_effort == "high", \
        "the level must be persisted on the provider record"


@_requires_qt
def test_a_model_without_reasoning_control_disables_the_picker(settings_page):
    page, store = settings_page
    prov = store.get_current()
    prov.model = "gpt-4o"
    store.update(prov)
    try:
        page.refresh_effort_options()
        assert page.cmb_effort.isEnabled() is False
        assert page._effort_values == [""]
    finally:
        prov.model = "MiniMax-M3.1-Flash"
        store.update(prov)
        page.refresh_effort_options()
