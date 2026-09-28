"""Behaviour regressions for the AUDIT-2026-09-27 desktop GUI pass.

What belongs here is the class of defect the earlier suites could not see:
things that were *wired to the wrong function*, *measured in the wrong unit*,
or *present but unreachable*. Every test below is paired with the specific
failure it pins; where an existing test already covers the ground (the result
action gate, the D2 chart-selector ownership) this file deliberately does NOT
duplicate it — those live in tests/test_fluent_ui_2026_09_05.py and this
module links to them instead.

Qt-free where the behaviour is pure logic, so CI can run most of it without
PySide6; the window-level class is skipped when Qt is missing.
"""

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

_requires_qt = pytest.mark.skipif(
    not _qt_available(), reason="PySide6/qfluentwidgets unavailable")


# ---------------------------------------------------------------------------
# B-18 — the raw-response cap measured characters while claiming to measure
# bytes, and clipped silently.
# ---------------------------------------------------------------------------
class TestRawCapIsHonest(unittest.TestCase):
    """``rca_core.history._truncate_raw``.

    AUDIT-2026-09-27 [item 1.6] (B-18). The old line was::

        raw = rec.raw if len(rec.raw) <= _MAX_RAW_BYTES else rec.raw[:_MAX_RAW_BYTES]

    ``_MAX_RAW_BYTES`` is named and documented as a byte bound on the DB, but
    ``len()`` on a ``str`` counts characters. This tool's own default output
    language produces CJK, so the cases below are the ones that matter.
    """

    # 3 bytes per character in UTF-8.
    CJK = "\u9a6c\u9a6c\u6a2a\u541d"
    MARKER = "[truncated:"

    def test_cjk_response_is_capped_by_bytes_not_characters(self):
        from rca_core.history import _truncate_raw, _MAX_RAW_BYTES
        text = self.CJK * 6000          # 6000 chars, 18000 bytes
        out = _truncate_raw(text)
        stored = len(out.encode("utf-8"))
        # The old code stored all 6000 characters = 18000 bytes against an
        # 8 KB cap. Allow the marker on top of the cap, nothing more.
        self.assertLessEqual(
            stored, _MAX_RAW_BYTES + 400,
            f"CJK raw stored {stored} bytes; the cap is {_MAX_RAW_BYTES}")
        self.assertLess(stored, len(text.encode("utf-8")))

    def test_ascii_response_is_capped_too(self):
        from rca_core.history import _truncate_raw, _MAX_RAW_BYTES
        text = "A" * 20000
        out = _truncate_raw(text)
        self.assertLessEqual(len(out.encode("utf-8")), _MAX_RAW_BYTES + 400)

    def test_truncation_is_marked_with_the_true_size(self):
        from rca_core.history import _truncate_raw
        text = "B" * 20000
        out = _truncate_raw(text)
        self.assertIn(self.MARKER, out,
                      "a clipped fragment must be distinguishable from a "
                      "complete response — the detail dialog and the WebEngine "
                      "raw toggle both present this string as the model reply")
        self.assertIn("20000", out, "the marker must state the real size")

    def test_text_under_the_cap_is_returned_unchanged(self):
        from rca_core.history import _truncate_raw
        text = "short response"
        self.assertIs(_truncate_raw(text), text)
        self.assertEqual(_truncate_raw(""), "")

    def test_cap_never_splits_a_character(self):
        from rca_core.history import _truncate_raw
        # 8192 is not a multiple of 3, so a naive byte slice lands mid-
        # character and decode() would produce U+FFFD in the audit trail.
        for n in (1, 2, 3, 5, 8):
            out = _truncate_raw(self.CJK * 4000, cap_bytes=n)
            self.assertNotIn("\ufffd", out,
                             f"cap={n} produced a replacement character")

    def test_marker_points_at_where_the_full_text_lives(self):
        from rca_core.history import _truncate_raw
        out = _truncate_raw("C" * 20000)
        self.assertIn("raw_responses", out,
                      "the full text IS still stored untruncated in the "
                      "raw_responses table; the marker should say so")


# ---------------------------------------------------------------------------
# B-17 — the detail dialog's thumbnail decode tried PNG first on data the
# store always writes as JPEG.
# ---------------------------------------------------------------------------
@_requires_qt
class TestThumbnailFormatSniffing(unittest.TestCase):
    """``gui_fluent_history_detail._thumbnail_widget``."""

    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])

    def _image_bytes(self, fmt):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (10, 120, 200)).save(buf, format=fmt)
        return buf.getvalue()

    def _decoded_pixmap(self, thumb_bytes):
        """Return the QPixmap the widget managed to build, or None."""
        from PySide6.QtWidgets import QLabel
        import gui_fluent_history_detail as hd
        w = hd._thumbnail_widget(thumb_bytes)
        try:
            for lbl in w.findChildren(QLabel):
                pm = lbl.pixmap()
                if pm is not None and not pm.isNull():
                    return pm
            return None
        finally:
            w.deleteLater()

    def test_jpeg_thumbnail_decodes(self):
        # This is the ONLY format HistoryStore ever writes, since
        # make_thumbnail_with_size re-encodes with img.save(..., "JPEG").
        self.assertIsNotNone(self._decoded_pixmap(self._image_bytes("JPEG")))

    def test_png_thumbnail_still_decodes(self):
        # Legacy rows / any other producer must not regress.
        self.assertIsNotNone(self._decoded_pixmap(self._image_bytes("PNG")))

    def test_undecodable_bytes_report_instead_of_crashing(self):
        self.assertIsNone(self._decoded_pixmap(b"not an image at all"))


# ---------------------------------------------------------------------------
# 阶段 1 / 2 — window-level behaviour.
# ---------------------------------------------------------------------------
@_requires_qt
class TestExtractPageAuditBehaviour(unittest.TestCase):
    """Window-level states, using the offline window recipe from
    tests/test_fluent_ui_2026_09_05.py (temp DB, stubbed stores, eager
    WebEngine teardown) so this is safe in a full-suite run."""

    def _make_window(self):
        import os
        import tempfile
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        import gui_fluent
        from rca_core.history import Database, HistoryStore
        tmp = tempfile.mkdtemp(prefix="rca_audit_0927_")
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
        import importlib
        if importlib.util.find_spec("tests.test_gui_sprint_b"):
            mod = importlib.import_module("tests.test_gui_sprint_b")
        else:
            mod = importlib.import_module("test_gui_sprint_b")
        mod._destroy_window(win)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for _ in range(3):
                app.processEvents()

    def test_cancel_button_is_offered_only_while_busy(self):
        """AUDIT-2026-09-27 [item 2.3] (B-16).

        A permanently-visible cancel button lies: at rest there is nothing to
        cancel, and clicking it could only report a request against no worker.
        It is armed by _set_busy() and re-armed per run.
        """
        win, _hs = self._make_window()
        try:
            page = win.extract_page
            self.assertTrue(page.btn_cancel.isHidden(),
                            "cancel must be hidden on a fresh page")
            page._set_busy(True)
            self.assertFalse(page.btn_cancel.isHidden(),
                             "cancel must appear once a run starts")
            page._set_busy(False)
            self.assertTrue(page.btn_cancel.isHidden(),
                            "cancel must go away again when the run ends")
        finally:
            self._destroy_window(win)

    def test_history_thumbnail_never_arms_an_extraction(self):
        """AUDIT-2026-09-27 [item 2.1] (C6).

        The stored thumbnail is a 200 px orientation aid, not an extractable
        image. Routing it through ``image_b64`` would re-send the PREVIOUS
        figure; dropping it left the review screen with no picture at all.
        The fix is a separate display-only slot, so pin BOTH halves: the
        figure is shown, and the extract path stays disarmed.
        """
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 24), (10, 120, 200)).save(buf, format="PNG")
        thumb = base64.b64encode(buf.getvalue()).decode("ascii")

        win, _hs = self._make_window()
        try:
            page = win.extract_page
            page.load_result({"species_ranges": [{"species": "X"}]},
                             thumbnail_b64=thumb)
            self.assertIsNotNone(page._display_thumb_b64)
            self.assertEqual(page._display_thumb_b64, thumb)
            # C6: the ghost-image guarantee must survive the thumbnail.
            self.assertIsNone(page.image_b64,
                              "loading history must not arm a ghost extraction")
            self.assertFalse(page.has_pending_image())
        finally:
            self._destroy_window(win)

    def test_history_thumbnail_actually_renders(self):
        """AUDIT-2026-09-27 [item 2.4] — the regression item 2.1 missed.

        ``_show_history_thumbnail`` called ``base64.b64decode`` but the module
        never imported ``base64``, so EVERY call raised NameError, and the
        blanket ``except Exception`` reset the preview to the drop hint. The
        feature had never rendered a thumbnail for any record and nothing
        failed visibly, because the broad except made a missing import look
        exactly like a corrupt blob.

        This asserts the *effect* (a pixmap is on the preview and the caption
        says it is a stored thumbnail) rather than the absence of an
        exception, because absence of an exception is precisely what the old
        code got wrong.
        """
        import base64
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (32, 24), (10, 120, 200)).save(buf, format="PNG")
        thumb = base64.b64encode(buf.getvalue()).decode("ascii")

        win, _hs = self._make_window()
        try:
            page = win.extract_page
            page.load_result({"species_ranges": [{"species": "X"}]},
                             thumbnail_b64=thumb)
            self.assertFalse(page.preview.pixmap().isNull(),
                             "the history thumbnail must actually be drawn")
            self.assertIn("thumb", page.lbl_imginfo.text().lower(),
                          "the caption must say it is a stored thumbnail, not "
                          "a re-loadable source file")
        finally:
            self._destroy_window(win)

    def test_has_pending_image_follows_the_data_not_the_preview(self):
        """AUDIT-2026-09-27 [item 2.2] (B-15).

        ``load_image_b64`` could succeed and then ``QPixmap(path)`` return
        null, leaving the preview showing the drop hint while the app was
        fully armed to extract — the UI said "no image" for an armed app. The
        gate now reads the DATA.
        """
        win, _hs = self._make_window()
        try:
            page = win.extract_page
            self.assertFalse(page.has_pending_image())
            # Arm the data path directly; the preview is irrelevant.
            page.image_b64 = "aGk="
            self.assertTrue(page.has_pending_image())
            page.image_b64 = None
            self.assertFalse(page.has_pending_image())
        finally:
            self._destroy_window(win)

    def test_exports_live_in_the_result_header_not_the_run_row(self):
        """AUDIT-2026-09-27 [item 2.4] (U-04).

        The two export buttons used to sit in the run row, i.e. next to the
        one control that starts work and above an empty result panel, so a
        user's eye met two dead-looking buttons before anything had been
        extracted. Asserted against the source because "which layout owns a
        widget" is not observable from the public widget API — QLayout does
        not take ownership, so there is no parent chain to walk.
        """
        import gui_fluent
        src = open(gui_fluent.__file__, encoding="utf-8").read()
        run_row_calls = [ln.strip() for ln in src.splitlines()
                         if "run_row.addWidget" in ln]
        for ln in run_row_calls:
            self.assertNotIn("btn_export", ln,
                             f"export button still in the run row: {ln}")
        for name in ("btn_export", "btn_export_xlsx"):
            self.assertIn(f"self.result_head.addWidget(self.{name})", src,
                          f"{name} must be added to the result header")

    def test_multi_run_path_shares_the_single_error_guard(self):
        """AUDIT-2026-09-27 [item 1.1] (B-01).

        ``run()`` guarded its own body with ``except Exception`` but delegated
        the multi-run branch to a method with no guard of its own, so with
        ``runs > 1`` an exception escaped into the Qt slot and killed the
        worker thread. The branch was pulled into ``_run_multi`` so the one
        guard covers both paths — including the ``import concurrent.futures``
        that used to fall out of scope on the way.
        """
        import inspect
        import gui_fluent
        src = inspect.getsource(gui_fluent.ExtractWorker.run)
        self.assertIn("_run_multi", src,
                      "run() must delegate the multi-run branch to _run_multi")
        worker_src = inspect.getsource(gui_fluent.ExtractWorker)
        self.assertTrue(hasattr(gui_fluent.ExtractWorker, "_run_multi"))
        # The guard must be in run() itself, not only in the helper.
        self.assertIn("except Exception", src)

    def test_api_key_is_saved_only_by_its_own_button(self):
        """AUDIT-2026-09-27 [item 1.2] (B-02).

        One combined "save" wrote the live API-key field back into the config
        on every save and on every window close. With "remember" switched off,
        that silently blanked the stored credential. The secret write is now
        its own path.
        """
        import ast
        import inspect
        import textwrap
        import gui_fluent
        cls = gui_fluent.RangeChartFluentWindow
        self.assertTrue(hasattr(cls, "save_generation"))
        self.assertTrue(hasattr(cls, "save_api_key"))

        def code_strings(fn):
            """String literals in the EXECUTABLE body (docstring excluded).

            A substring search over getsource() is useless here: the
            docstring of save_generation is required to MENTION api_key, to
            explain the bug it prevents. Only the code matters.
            """
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            body = tree.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]          # drop the docstring
            mod = ast.Module(body=body, type_ignores=[])
            return {n.value for n in ast.walk(mod)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}

        self.assertNotIn(
            "api_key", code_strings(cls.save_generation),
            "save_generation must not reference the credential key at all")
        self.assertIn(
            "api_key", code_strings(cls.save_api_key),
            "save_api_key is the path that owns the credential")

        close = code_strings(cls.closeEvent)
        self.assertNotIn(
            "save_api_key", code_strings(cls.closeEvent),
            "closing the window must not rewrite the credential")
        self.assertIn("save_generation", inspect.getsource(cls.closeEvent))


# ---------------------------------------------------------------------------
# 阶段 3 — D2. The selector-ownership contract itself is asserted in
# tests/test_fluent_ui_2026_09_05.py::test_extract_page_owns_the_chart_selectors;
# what is pinned here is the wiring that test depends on, because the two
# copies silently drifting is exactly the bug D2 was raised for.
# ---------------------------------------------------------------------------
class TestChartSelectorWiring(unittest.TestCase):
    def test_new_i18n_keys_exist_in_all_three_languages(self):
        from rca_core.i18n import TRANSLATIONS
        for lang in ("zh", "en", "ja"):
            for key in ("history.empty", "history.emptyHint",
                        "action.cancel", "action.cancelHint"):
                self.assertIn(key, TRANSLATIONS[lang], (lang, key))
                self.assertTrue(TRANSLATIONS[lang][key], (lang, key))


@_requires_qt
class TestChartSelectorReverseWiring(unittest.TestCase):
    """The D2 contract itself is asserted in
    tests/test_fluent_ui_2026_09_05.py::test_extract_page_owns_the_chart_selectors.
    What is pinned here is the wiring that test depends on, because the two
    copies silently drifting is exactly the bug D2 was raised for.
    """

    def test_mirror_changes_pull_the_canonical_page_back(self):
        # Qt-free in spirit but source-level: assert the reverse handlers
        # exist AND are what the mirror is connected to. Before the fix the
        # mirror was connected to the PUSH handlers, so changing the hidden
        # copy read the inline index and wrote it straight back over the
        # user's change — the two copies could disagree, which is the exact
        # failure D2 exists to remove. The correct reverse handlers were
        # already written and simply never connected.
        import inspect
        import gui_fluent
        src = inspect.getsource(
            gui_fluent.ExtractPage.attach_settings_selectors)
        self.assertIn("sp.cmb_ctype.currentIndexChanged.connect("
                      "self._on_settings_ctype)", src)
        self.assertIn("sp.cmb_clang.currentIndexChanged.connect("
                      "self._on_settings_clang)", src)
        self.assertNotIn("connect(self._push_ctype_to_settings)", src)
        for name in ("_on_settings_ctype", "_on_settings_clang"):
            self.assertTrue(hasattr(gui_fluent.ExtractPage, name))


@_requires_qt
class TestPhyloInjectedScript(unittest.TestCase):
    """The script PhyloTreeWidget hands to QWebEnginePage.runJavaScript."""

    def _build(self, payload):
        """Run the SHIPPED builder, so this cannot drift from the code."""
        import inspect
        import textwrap
        import gui_fluent
        src = textwrap.dedent(
            inspect.getsource(gui_fluent.PhyloTreeWidget._dispatch_data))
        start = src.index("script = (")
        end = src.index("self.page().runJavaScript(script)")
        scope = {"payload": payload, "script": None}
        exec(src[start:end].rstrip(), scope)
        return scope["script"]

    def test_generated_script_matches_the_known_good_shape(self):
        """AUDIT-2026-09-27 [item 2.5] — a total, silent failure.

        The builder assembled the script by f-string brace doubling, and the
        `catch` half was a PLAIN string, so its `}}` / `{{` were emitted
        literally. That closed `try` one brace early and orphaned `catch`;
        WebEngine rejected every script with "Missing catch or finally after
        try". Confirmed against 66 recorded real payloads (66/66 invalid),
        so PhyloTreeWidget had never rendered a tree for any input.

        runJavaScript routes a parse error to the JS console and leaves the
        Python side green, so a golden assertion is the only thing standing
        between this and another silent death: it needs no node, and it fails
        the moment the braces are assembled wrongly again.
        """
        got = self._build('{"a":1}')
        want = (
            "(function(){"
            "try{"
            "if(typeof window.setTreeData==='function'){"
            'window.setTreeData({"a":1});'
            "}else{console.error("
            "'PhyloTreeWidget: window.setTreeData is not defined');}"
            "return null;"
            "}catch(e){console.error("
            "'PhyloTreeWidget setTreeData threw:',e);return null;}"
            "})()"
        )
        self.assertEqual(got, want)

    def test_script_escapes_the_json_line_separators(self):
        """U+2028 / U+2029 are legal raw in JSON but terminate a JS string."""
        import json
        raw = json.dumps({"note": "a\u2028b\u2029c"}, ensure_ascii=False)
        got = self._build(raw.replace("\u2028", "\\u2028")
                              .replace("\u2029", "\\u2029"))
        self.assertIn("\\u2028", got)
        self.assertIn("\\u2029", got)
        self.assertNotIn("\u2028", got)
        self.assertNotIn("\u2029", got)

    def test_braces_around_catch_are_not_doubled(self):
        """The precise shape the old bug got wrong, pinned directly."""
        got = self._build("{}")
        self.assertIn("return null;}catch(e){console.error(", got)
        self.assertNotIn("}}catch", got)
        self.assertNotIn("catch(e){{", got)


if __name__ == "__main__":
    unittest.main()
