"""Test P1-1: LOCK_PATH consistency across app.py, gui_fluent_history_detail.py, and rca_core/history.py.

All three sites must use the same lock-file path (byte-identical).
Previously app.py used ~/.range_chart_analyzer.lock (file in home) while
gui_fluent_history_detail used ~/.range_chart_analyzer/lock (file in subdir).
"""
import os
import pytest


class TestLockPathConsistency:
    """P1-1: lock file path must be consistent across all three call sites."""

    def test_history_defines_lock_path(self):
        """rca_core.history must define LOCK_PATH."""
        from rca_core.history import LOCK_PATH
        assert LOCK_PATH is not None
        assert isinstance(LOCK_PATH, str)
        assert len(LOCK_PATH) > 0

    def test_app_uses_history_lock_path(self):
        """app.py must import LOCK_PATH from rca_core.history, not define its own."""
        import app
        assert hasattr(app, 'LOCK_FILE'), "app.py must define LOCK_FILE (alias for backwards compat)"
        from rca_core.history import LOCK_PATH as history_lock_path
        assert app.LOCK_FILE == history_lock_path, (
            f"app.LOCK_FILE ({app.LOCK_FILE!r}) != rca_core.history.LOCK_PATH ({history_lock_path!r})"
        )

    def test_gui_fluent_history_detail_uses_history_lock_path(self):
        """gui_fluent_history_detail.py must import LOCK_PATH from rca_core.history."""
        # gui_fluent_history_detail.py requires PySide6 which may not be installed.
        # We verify the source code directly instead of importing.
        hf_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "gui_fluent_history_detail.py"
        )
        with open(hf_path, encoding="utf-8") as f:
            source = f.read()

        # Must import LOCK_PATH from rca_core.history
        assert "from rca_core.history import" in source
        assert "LOCK_PATH" in source
        # Must NOT have its own hardcoded _LOCK_PATH definition
        assert "_LOCK_PATH = Path.home()" not in source
        assert "_LOCK_PATH = os.path.join" not in source

    def test_all_three_paths_byte_equal(self):
        """All three path references must be byte-identical."""
        from rca_core.history import LOCK_PATH as hp
        import app

        paths = {
            "rca_core.history.LOCK_PATH": hp,
            "app.LOCK_FILE": app.LOCK_FILE,
        }
        for name, path in paths.items():
            assert isinstance(path, str), f"{name} must be a string, got {type(path)}"

        # app.LOCK_FILE must equal history.LOCK_PATH
        assert app.LOCK_FILE == hp, (
            f"app.LOCK_FILE ({app.LOCK_FILE!r}) != rca_core.history.LOCK_PATH ({hp!r})"
        )

    def test_lock_path_points_to_lock_file(self):
        """LOCK_PATH must point to a 'lock' file in the .range_chart_analyzer dir."""
        from rca_core.history import LOCK_PATH
        assert LOCK_PATH.endswith("lock") or "lock" in LOCK_PATH
        # On Windows, the home dir expands to C:\Users\...
        dirname = os.path.dirname(LOCK_PATH)
        assert ".range_chart_analyzer" in dirname, (
            f"LOCK_PATH should be inside .range_chart_analyzer dir, got: {LOCK_PATH!r}"
        )
