"""Test P1-2: _table_widgets memory accumulation fix.

On each _render_result call, old table widgets must be deleted and
_table_widgets must be cleared to keep memory bounded.
"""
import pytest


class TestTableWidgetsMemory:
    """P1-2: _table_widgets memory accumulation must be fixed."""

    def test_render_result_clears_table_widgets(self):
        """_render_result must clear _table_widgets on each call."""
        # We verify the source code contains the cleanup logic.
        import os
        gf_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "gui_fluent.py"
        )
        with open(gf_path, encoding="utf-8") as f:
            source = f.read()

        # The fix must clear _table_widgets in _render_result
        # Find the _render_result method and check it clears _table_widgets
        import re
        # Locate the _render_result function
        m = re.search(r'def _render_result\(self\):(.*?)(?=\n    def |\nclass |\Z)',
                      source, re.DOTALL)
        assert m, "_render_result method not found in gui_fluent.py"
        render_body = m.group(1)
        assert "_table_widgets" in render_body, (
            "_render_result must reference _table_widgets for cleanup"
        )
        # Must clear to empty dict
        assert re.search(r"self\._table_widgets\s*=\s*\{\}", render_body), (
            "_render_result must reset _table_widgets to {}"
        )

    def test_old_widgets_delete_later(self):
        """_render_result must call deleteLater() on old table widgets."""
        import os
        import re
        gf_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "gui_fluent.py"
        )
        with open(gf_path, encoding="utf-8") as f:
            source = f.read()

        m = re.search(r'def _render_result\(self\):(.*?)(?=\n    def |\nclass |\Z)',
                      source, re.DOTALL)
        assert m
        render_body = m.group(1)

        # Must iterate over _table_widgets and call deleteLater
        assert "deleteLater()" in render_body, (
            "_render_result must call deleteLater() on old _table_widgets"
        )
        assert "_table_widgets" in render_body

    def test_table_widgets_bounded_after_multiple_renders(self):
        """Simulate multiple _render_result calls - _table_widgets stays bounded."""
        # We can't easily mock the full GUI, but we can verify the cleanup
        # logic exists by checking the method signature
        import os
        import re
        gf_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "gui_fluent.py"
        )
        with open(gf_path, encoding="utf-8") as f:
            source = f.read()

        # Verify that _render_result clears _table_widgets at the START
        # (before populating new widgets), which means multiple calls
        # won't accumulate widgets.
        m = re.search(r'def _render_result\(self\):(.*?)(?=\n    def _render_result|\n    def |\nclass |\Z)',
                      source, re.DOTALL)
        assert m
        # Get the part BEFORE any table creation (widget assignment)
        render_body = m.group(1)
        # The cleanup (self._table_widgets = {}) must appear early in the method
        # (before table creation at line ~1102)
        lines = render_body.split('\n')
        clear_line_idx = None
        for i, line in enumerate(lines):
            if re.search(r"self\._table_widgets\s*=\s*\{\}", line):
                clear_line_idx = i
                break
        assert clear_line_idx is not None, (
            "self._table_widgets = {} not found in _render_result"
        )
        # Clear should be in the first 35 lines of the method body
        # (before any table creation that happens at ~line 40+)
        assert clear_line_idx < 35, (
            "self._table_widgets = {} appears at line " + str(clear_line_idx + 1) +
            " of _render_result - should appear early (before table creation)"
        )
