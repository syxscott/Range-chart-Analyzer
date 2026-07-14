"""Headless smoke tests for the Fluent (PySide6) GUI.

Guarded by importorskip so machines WITHOUT PySide6 / qfluentwidgets
skip cleanly (never fail). Runs under QT_QPA_PLATFORM=offscreen so no
display is required.

Run:  QT_QPA_PLATFORM=offscreen python tests_gui_fluent.py
      (or via pytest)
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


def _have_deps():
    try:
        import PySide6  # noqa: F401
        import qfluentwidgets  # noqa: F401
        return True
    except Exception:
        return False


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])

# ---- T4: ExtractWorker lifecycle ----
def test_extract_worker_emits_on_success():
    """ExtractWorker must emit finished_ok with an ExtractResult on success."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QThread
    app = QApplication.instance() or QApplication([])
    import gui_fluent

    captured = {}
    def fake_extract(**kw):
        captured.update(kw)
        from rca_core.extractor import ExtractResult
        return ExtractResult(ok=True, data={"confidence": 0.5}, raw="{}", truncated=False)

    # Stub the module-level extract() so the worker calls our fake.
    import rca_core.extractor as E
    orig = E.extract
    E.extract = fake_extract
    try:
        w = gui_fluent.ExtractWorker(
            params={"api_key": "k", "image_b64": "QUFB", "media_type": "image/png"},
            mode="range_chart", runs=1)
        results = []
        w.finished_ok.connect(lambda r: results.append(r))
        w.start()
        # Wait for thread to finish (max 5s).
        assert w.wait(5000), "worker did not finish in 5s"
        assert len(results) == 1, f"expected 1 result, got {len(results)}"
        assert results[0].ok
        assert captured.get("image_b64") == "QUFB"
    finally:
        E.extract = orig


def test_extract_worker_emits_on_failure():
    """ExtractWorker must emit finished_ok with ok=False on extract failure."""
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app = QApplication.instance() or QApplication([])
    import gui_fluent

    def fake_extract(**kw):
        from rca_core.extractor import ExtractResult
        return ExtractResult(ok=False, error_key="err.network", raw="")

    import rca_core.extractor as E
    orig = E.extract
    E.extract = fake_extract
    try:
        w = gui_fluent.ExtractWorker(
            params={"api_key": "k", "image_b64": "QUFB", "media_type": "image/png"},
            mode="range_chart", runs=1)
        results = []
        w.finished_ok.connect(lambda r: results.append(r))
        w.start()
        # Pump the Qt event loop so the queued signal is delivered.
        deadline = time.time() + 5
        while len(results) == 0 and time.time() < deadline:
            app.processEvents()
            time.sleep(0.05)
        w.wait(1000)
        assert len(results) == 1, f"expected 1 result, got {len(results)}"
        assert not results[0].ok
        assert results[0].error_key == "err.network"
        # quit the thread for cleanup
        w.quit()
        w.wait(500)
    finally:
        E.extract = orig


# ---- T5 + T6: exporter unit tests ----
def test_looks_columnar_id_only():
    """sections[0] with 'id' but no 'name' -> columnar."""
    from rca_core.exporter import _looks_columnar
    data = {"sections": [{"id": "Ki-1", "group": "L"}]}
    check("col-detect-id-only", _looks_columnar(data) is True)


def test_looks_columnar_name_only():
    """sections[0] with 'name' but no 'id' -> range-chart."""
    from rca_core.exporter import _looks_columnar
    data = {"sections": [{"name": "A"}]}
    check("col-detect-name-only", _looks_columnar(data) is False)


def test_looks_columnar_both():
    """sections[0] with both 'id' and 'name' -> range-chart (avoid mis-route)."""
    from rca_core.exporter import _looks_columnar
    data = {"sections": [{"id": "Ki-1", "name": "A"}]}
    check("col-detect-both", _looks_columnar(data) is False)


def test_build_table_export_padding():
    """Row extractor returning fewer cells than cols must be padded."""
    from rca_core.exporter import build_table_export
    data = {"species_ranges": [{"species": "X"}]}  # missing section/range_base/...
    headers, rows = build_table_export(data, "species_ranges", lambda k: k)
    # headers = ["#", "species", "section", "range_base", "range_top", "biozone"]
    # row cells should be padded to 5 (matching cols)
    check("export-padded-len", len(rows[0]) == len(headers))


def test_build_table_export_truncation():
    """Row extractor returning more cells than cols must be truncated."""
    from rca_core.exporter import build_table_export
    # Inject a custom config with a row lambda that returns too many cells.
    import rca_core.exporter as X
    orig_fn = X._range_chart_tables
    def patched(data):
        cfg = orig_fn(data)
        for c in cfg:
            if c["id"] == "species_ranges":
                c["row"] = lambda r: [r.get("species","")] * 10  # way too many
        return cfg
    X._range_chart_tables = patched
    try:
        data = {"species_ranges": [{"species": "X", "section": "A",
                                      "range_base": "1", "range_top": "2", "biozone": "Z"}]}
        headers, rows = build_table_export(data, "species_ranges", lambda k: k)
        check("export-truncated-len", len(rows[0]) == len(headers))
    finally:
        X._range_chart_tables = orig_fn


# ---- T12 + T13: aggregate + json_utils edge cases ----
def test_norm_strips_sp_cf():
    """_norm must strip trailing 'sp.' / 'cf.' and collapse whitespace."""
    from rca_core.aggregate import _norm
    check("norm-strip-sp", _norm("Neoalbaillella sp.") == "neoalbaillella")
    check("norm-strip-cf", _norm("Entactinia cf. sashidai") == "entactinia sashidai")
    check("norm-collapse-ws", _norm("  Hello   World  ") == "hello world")


def test_balanced_json_empty_object():
    """extract_balanced_json_object must return '{}' for a bare empty object."""
    from rca_core.json_utils import extract_balanced_json_object
    check("balanced-empty-obj", extract_balanced_json_object("{}") == "{}")


def test_balanced_json_multiple_top_level():
    """extract_balanced_json_object must return the FIRST balanced object."""
    from rca_core.json_utils import extract_balanced_json_object
    text = 'noise {"a":1} more {"b":2}'
    check("balanced-first-wins", extract_balanced_json_object(text) == '{"a":1}')


def test_balanced_json_escaped_quotes():
    """extract_balanced_json_object must handle escaped quotes inside strings."""
    from rca_core.json_utils import extract_balanced_json_object
    text = '{"a":"he said \\"hi\\"","b":2}'
    result = extract_balanced_json_object(text)
    check("balanced-escaped-quotes", result == text)





def run_all():
    if not _have_deps():
        print("SKIP tests_gui_fluent: PySide6 / qfluentwidgets not installed")
        return 0

    _app()
    import gui_fluent

    check("module-has-main", hasattr(gui_fluent, "main"))

    win = gui_fluent.RangeChartFluentWindow()
    try:
        names = {win.extract_page.objectName(),
                 win.providers_page.objectName(),
                 win.settings_page.objectName(),
                 win.about_page.objectName()}
        check("has-extract-page", "extractPage" in names)
        check("has-providers-page", "providersPage" in names)
        check("has-settings-page", "settingsPage" in names)
        check("has-about-page", "aboutPage" in names)

        # Settings accessors reflect defaults.
        check("max-tokens-accessor", isinstance(win.max_tokens(), int))
        check("runs-in-range", 1 <= win.runs() <= 5)
        check("chart-type-valid", win.chart_type() in ("auto", "range_chart", "columnar_section"))

        # Language cycle switches + re-translates without error.
        before = win.tr.lang
        win._cycle_lang()
        check("lang-cycles", win.tr.lang != before)

        # Language switch must re-translate the sidebar nav labels + the
        # active-page widgets (regression: they used to stay in the old
        # language). Force zh->en and assert the nav + a page label changed.
        while win.tr.lang != "zh":
            win._cycle_lang()
        zh_nav = win._nav_settings.text()
        zh_btn = win.settings_page.btn_save.text()
        win._cycle_lang()  # -> en
        check("lang-nav-relabels", win._nav_settings.text() != zh_nav)
        check("lang-page-relabels", win.settings_page.btn_save.text() != zh_btn)
        # T1: lang button text must reflect the actual current language,
        # not just be one of the three valid options.
        check("lang-btn-shows-current",
              win._lang_btn.text() in ("English", "中文", "日本語"))
        lang_to_label = {"zh": "中文", "en": "English", "ja": "日本語"}
        expected = lang_to_label.get(win.tr.lang, win.tr.lang)
        check("lang-btn-matches-tr-lang", win._lang_btn.text() == expected)
        check("lang-ctype-combo-relabels",
              win.settings_page.cmb_ctype.itemText(0) not in ("", None))

        # Extract worker class exists and is a QThread.
        from PySide6.QtCore import QThread
        check("extract-worker-is-qthread", issubclass(gui_fluent.ExtractWorker, QThread))
        check("conntest-worker-is-qthread", issubclass(gui_fluent.ConnTestWorker, QThread))
    finally:
        win.close()
        win.deleteLater()

    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    import sys
    rc = run_all()
    sys.exit(rc)
