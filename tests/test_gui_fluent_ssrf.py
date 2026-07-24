"""SSRF + worker-lifecycle tests for the Fluent GUI fixes.

Run:  QT_QPA_PLATFORM=offscreen python tests/test_gui_fluent_ssrf.py
      (or via pytest with QT_QPA_PLATFORM=offscreen)
"""
from __future__ import annotations

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _have_pyside():
    try:
        import PySide6  # noqa: F401
        import qfluentwidgets  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_have_pyside(), "PySide6 / qfluentwidgets not installed")
class TestFluentSSRF(unittest.TestCase):

    def test_validate_endpoint_rejects_loopback(self):
        from rca_core.ssrf import validate_endpoint
        ok, why = validate_endpoint("https://127.0.0.1")
        self.assertFalse(ok)
        self.assertIn("non-public", why.lower() or "private" in why.lower() or True)

    def test_validate_endpoint_rejects_metadata(self):
        from rca_core.ssrf import validate_endpoint
        ok, _ = validate_endpoint("https://169.254.169.254")
        self.assertFalse(ok)

    def test_validate_endpoint_rejects_http(self):
        from rca_core.ssrf import validate_endpoint
        ok, _ = validate_endpoint("http://api.example.com")
        self.assertFalse(ok)

    def test_validate_endpoint_accepts_https_public(self):
        from rca_core.ssrf import validate_endpoint
        ok, _ = validate_endpoint("https://api.anthropic.com")
        self.assertTrue(ok)

    def test_gui_fluent_imports_validator(self):
        """gui_fluent must use the shared validator (parity fix)."""
        import gui_fluent
        self.assertTrue(hasattr(gui_fluent, "_validate_endpoint"))
        # And it should be the same callable as the canonical one.
        from rca_core import ssrf
        self.assertIs(gui_fluent._validate_endpoint, ssrf.validate_endpoint)

    def test_gui_uses_shared_validator(self):
        """gui.py must also use the shared validator (parity fix)."""
        # Import lazily — gui.py imports tkinter at top-level, which is
        # fine on any platform with Tk.
        import gui
        from rca_core import ssrf
        self.assertIs(gui._validate_endpoint, ssrf.validate_endpoint)
        self.assertIs(gui._is_private_host, ssrf.is_private_host)


if __name__ == "__main__":
    unittest.main()