"""Pytest configuration including gold-standard test fixtures."""
from __future__ import annotations

import importlib
import os
import pytest


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "gold_smoke: gold-standard smoke tests (run on every PR)",
    )
    config.addinivalue_line(
        "markers",
        "gold: full gold-standard tests (run weekly / release)",
    )
    config.addinivalue_line(
        "markers",
        "gold_offline: gold tests using cached responses (zero API cost)",
    )


def pytest_collection_modifyitems(config, items):
    """Apply markers based on test file location and marker names."""
    for item in items:
        if "gold_smoke" in str(item.fspath):
            item.add_marker(pytest.mark.gold_smoke)
        elif "gold_metrics" in str(item.fspath):
            item.add_marker(pytest.mark.gold)
        elif "gold" in str(item.fspath):
            item.add_marker(pytest.mark.gold)


@pytest.fixture
def rca_offline_gold(request):
    """Force gold tests to use cached responses (no API calls).

    Set RCA_OFFLINE_GOLD=1 in the environment to activate.
    """
    return os.environ.get("RCA_OFFLINE_GOLD", "") == "1"


# AUDIT-2026-09-28: the desktop GUI tests were reading the developer's real
# ~/.range_chart_analyzer.json. `RangeChartFluentWindow.__init__` does
#     self.tr = Translator(self.cfg.get("lang", "zh"))
# so every GUI assertion depended on the language the developer happens to
# have selected. That is invisible locally -- the machine that writes the test
# has its own config -- and only shows up on a clean CI runner, where
# load_config() returns {} and the default "zh" wins.
#
# It was caught by the first CI run of the `qt` job:
#   test_history_thumbnail_actually_renders asserted "thumb" in the caption,
#   the caption is `image.historyThumbnail`, and on a fresh runner that
#   renders as 历史记录缩略图（仅供核对）.
#
# The lesson generalises past language: endpoint, model, chart_lang and
# enhance are in the same file, so a GUI assertion could equally well have
# depended on the endpoint the developer typed in. BOTH directions are
# isolated here -- load_config so nothing is INHERITED, save_config so nothing
# is WRITTEN back into a real user's settings.
#
# This is autouse rather than a helper each test calls on purpose: there are
# already five window recipes (tests_gui_fluent.py, tests/test_fluent_ui_
# 2026_09_05.py, tests/test_gui_2026_09_27.py, tests/test_gui_fluent_low_
# fixes.py, tests/test_gui_sprint_b.py), and "remember to call it in the new
# one" is exactly the allowlist rot this audit found three times already.
_TEST_CONFIG = {"lang": "en"}


@pytest.fixture(autouse=True)
def rca_isolate_user_config(monkeypatch):
    """Stop GUI tests from inheriting or writing the real user config.

    A no-op when the module is not importable, so the matrix job (no PySide6)
    is unaffected.
    """
    patched = False
    for modname in ("gui", "gui_fluent"):
        try:
            mod = importlib.import_module(modname)
        except Exception:
            # No Qt/tkinter in this environment. Nothing to isolate.
            continue
        if hasattr(mod, "load_config"):
            monkeypatch.setattr(mod, "load_config", lambda: dict(_TEST_CONFIG))
            patched = True
        if hasattr(mod, "save_config"):
            monkeypatch.setattr(mod, "save_config", lambda cfg: True)
    return patched
