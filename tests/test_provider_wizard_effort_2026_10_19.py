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
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                                  # noqa: E402

from rca_core.llm import LlmProvider, REASONING_EFFORT_LEVELS  # noqa: E402


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


def test_the_effort_is_shown_when_editing(qapp):
    w = _wizard(qapp, _existing("low"))
    try:
        assert w._effort_values[w._cmb_effort.currentIndex()] == "low", \
            "the stored effort must be shown in the combo, or the user " \
            "cannot see why the model is slow"
    finally:
        w.close()


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


def test_the_default_row_means_do_not_send(qapp):
    w = _wizard(qapp, _existing(""))
    try:
        assert w._effort_values[w._cmb_effort.currentIndex()] == "", \
            "a provider with no effort must open on the 'not sent' row"
        w._ipt_name.setText("MiniMax")
        w._ipt_endpoint.setText("https://api.minimaxi.com/anthropic")
        w._ipt_key.setText("sk-test")
        w._ipt_model.setText("MiniMax-M3")
        w._on_ok()
        assert w.created_provider.reasoning_effort == ""
    finally:
        w.close()


def test_every_documented_level_is_offered(qapp):
    w = _wizard(qapp, _existing(""))
    try:
        assert w._effort_values == [""] + list(REASONING_EFFORT_LEVELS)
    finally:
        w.close()
