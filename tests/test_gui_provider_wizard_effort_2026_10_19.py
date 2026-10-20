"""The provider wizard must not drop a field when it rebuilds the record.

``ProviderWizard._on_ok`` constructs a BRAND NEW ``LlmProvider`` from a
hand-listed set of fields. Every field it forgets is silently reset the moment
the user edits that provider in the settings dialog.

That is not hypothetical: the reasoning effort is exactly such a field, and it
is the one that decides whether MiniMax's M3.1 answers at all (M3.1 always
thinks, defaults to effort=max, and the thinking tokens consume max_tokens --
measured: read timeout at 90 s, 2 of 2 runs, while effort=low answered in
28.3 s). A wizard that rebuilt the record without it would put the user back
on the timing-out configuration with nothing on screen to explain why.

The GUI needs a display, so this runs on Qt's offscreen platform.

Qt-gated, like every other GUI suite here: PySide6 is deliberately NOT
installed in the CI matrix job (AUDIT-2026-09-27 [item 0.4] -- ~200 MB of Qt
plus a headless WebEngine setup does not belong on every Python version), and
the ``qt`` job is the gate that actually protects the desktop GUI. The file
name carries "gui" so the ``qt`` job's ``-k "gui or fluent ..."`` filter
picks it up, and the module skips CLEANLY where Qt is absent -- an ERROR
there takes the whole matrix job down with it, which is what the first
version of this file did.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                                  # noqa: E402

from rca_core.llm import LlmProvider, REASONING_EFFORT_LEVELS  # noqa: E402


def _qt_available():
    """The same gate the other GUI suites use (tests/test_fluent_ui_2026_09_05.py)."""
    try:
        import PySide6            # noqa: F401
        import qfluentwidgets     # noqa: F401
        return True
    except Exception:
        return False


_requires_qt = pytest.mark.skipif(
    not _qt_available(), reason="PySide6/qfluentwidgets unavailable")


@pytest.fixture(scope="module")
def qapp():
    # PySide6, NOT PyQt6: the GUI imports PySide6, and loading BOTH Qt
    # bindings in one process makes the second one fail with a DLL load
    # error ("找不到指定的程序") -- the two ship conflicting Qt DLLs.
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _existing(effort):
    return LlmProvider(
        id="p1", name="MiniMax", api_format="anthropic",
        endpoint="https://api.minimaxi.com/anthropic", api_key="sk-test",
        model="MiniMax-M3.1-Flash", reasoning_effort=effort)


def _wizard(qapp, existing):
    from gui_fluent_providers import ProviderWizard
    w = ProviderWizard(None, lambda key: key, existing=existing)
    return w


@_requires_qt
def test_the_effort_is_shown_when_editing(qapp):
    w = _wizard(qapp, _existing("low"))
    try:
        assert w._effort_values[w._cmb_effort.currentIndex()] == "low", \
            "the stored effort must be shown in the combo, or the user " \
            "cannot see why the model is slow"
    finally:
        w.close()


@_requires_qt
def test_the_effort_survives_an_edit(qapp):
    """The rebuild must carry the field over."""
    w = _wizard(qapp, _existing("low"))
    try:
        w._ipt_name.setText("MiniMax")
        w._ipt_endpoint.setText("https://api.minimaxi.com/anthropic")
        w._ipt_key.setText("sk-test")
        w._ipt_model.setText("MiniMax-M3.1-Flash")
        w._on_ok()
        assert w.created_provider is not None, "the wizard refused to save"
        assert w.created_provider.reasoning_effort == "low", \
            "editing the provider silently reset the effort -- M3.1 would " \
            "go back to effort=max and stop answering"
    finally:
        w.close()


@_requires_qt
def test_a_changed_effort_is_saved(qapp):
    w = _wizard(qapp, _existing("low"))
    try:
        w._ipt_name.setText("MiniMax")
        w._ipt_endpoint.setText("https://api.minimaxi.com/anthropic")
        w._ipt_key.setText("sk-test")
        w._ipt_model.setText("MiniMax-M3.1-Flash")
        w._cmb_effort.setCurrentIndex(w._effort_values.index("high"))
        w._on_ok()
        assert w.created_provider.reasoning_effort == "high"
    finally:
        w.close()


@_requires_qt
def test_the_default_row_means_do_not_send(qapp):
    w = _wizard(qapp, _existing(""))
    try:
        # REVIEW-2026-10-19: the levels are the MODEL's, and minimax-code's
        # catalog puts "default" FIRST in M3.1-Flash's option list -- it is a
        # visible level, not an absence, because a level the user cannot see
        # is a level the user cannot reason about. "default" means "let the
        # provider decide", and it sends nothing.
        assert w._effort_values[w._cmb_effort.currentIndex()] == "default", \
            "a provider with no stored effort must open on the 'default' row"
        w._ipt_name.setText("MiniMax")
        w._ipt_endpoint.setText("https://api.minimaxi.com/anthropic")
        w._ipt_key.setText("sk-test")
        w._ipt_model.setText("MiniMax-M3")
        w._on_ok()
        assert w.created_provider.reasoning_effort == "default"
    finally:
        w.close()


@_requires_qt
def test_every_documented_level_is_offered(qapp):
    from rca_core.llm import REASONING_EFFORT_LEVELS, REASONING_DEFAULT
    w = _wizard(qapp, _existing(""))
    try:
        w._ipt_model.setText("MiniMax-M3.1-Flash")
        assert w._effort_values == [REASONING_DEFAULT] + list(REASONING_EFFORT_LEVELS)
    finally:
        w.close()


@_requires_qt
def test_a_model_without_reasoning_control_offers_nothing(qapp):
    """M3's thinking is an on/off switch, and any other model takes none."""
    w = _wizard(qapp, _existing(""))
    try:
        w._ipt_model.setText("gpt-4o")
        assert w._effort_values == [""], \
            "a model with no reasoning control must not offer a level"
        assert w._cmb_effort.isEnabled() is False
    finally:
        w.close()
