"""Tests for the pure-Python parts of gui_fluent_onboarding.

The Qt-dependent classes can't be exercised here (no PySide6 in CI),
but the marker-path helpers are pure stdlib and the module is
importable when PySide6 is missing. The Qt parts of the module are
gated by the _HAVE_QT flag and are exercised by the live GUI.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_pass = 0
_fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)


def test_module_imports_without_pyside6():
    """gui_fluent_onboarding should import even when PySide6 is missing."""
    import importlib
    # Force a fresh import so the try/except runs again.
    if "gui_fluent_onboarding" in sys.modules:
        del sys.modules["gui_fluent_onboarding"]
    try:
        m = importlib.import_module("gui_fluent_onboarding")
        check("module-imports", True)
        check("module-exposes-helper", hasattr(m, "onboarding_marker_path"))
        check("module-exposes-flag", hasattr(m, "_HAVE_QT"))
    except Exception as exc:
        check("module-imports", False)
        print("IMPORT ERROR:", exc)


def test_marker_path_in_home():
    from gui_fluent_onboarding import onboarding_marker_path
    p = onboarding_marker_path()
    check("marker-in-home", p.startswith(os.path.expanduser("~")))
    check("marker-filename", p.endswith("onboarding_seen"))


def test_seen_round_trip():
    """has_seen / mark_seen should round-trip correctly via a temp home."""
    from gui_fluent_onboarding import (
        has_seen_onboarding, mark_onboarding_seen, onboarding_marker_path,
    )
    td = tempfile.mkdtemp()
    # We can't actually redirect the home dir for a stdlib module that
    # captured expanduser at import time, so instead we just call the
    # real functions and clean up after.
    before = has_seen_onboarding()
    mark_onboarding_seen()
    after = has_seen_onboarding()
    check("seen-round-trip", (before is False) and (after is True))
    # Clean up.
    try:
        os.remove(onboarding_marker_path())
    except OSError:
        pass
    import shutil
    shutil.rmtree(td, ignore_errors=True)


test_module_imports_without_pyside6()
test_marker_path_in_home()
test_seen_round_trip()
print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)
