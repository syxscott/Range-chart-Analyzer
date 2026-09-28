"""Headless smoke tests for the Fluent (PySide6) GUI.

Guarded by importorskip so machines WITHOUT PySide6 / qfluentwidgets
skip cleanly (never fail). Runs under QT_QPA_PLATFORM=offscreen so no
display is required.

Run:  QT_QPA_PLATFORM=offscreen python tests_gui_fluent.py
      (or via pytest)
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_pass = 0
_fail = 0

# AUDIT-2026-09-27 [item 0.1]: accumulator of check() failures for the
# CURRENT test, drained by the pytest guard fixture below. Previously
# ``check()`` only PRINTED, so under pytest every soft assertion below
# passed unconditionally — including all four page-objectName pins. This
# file also had NO autouse fixture, unlike tests_core.py which fixed the
# identical bug there, and its two halves were DISJOINT: ``run_all()``
# never called any ``test_*`` function and pytest only ran the ``test_*``
# functions, so no single path ever executed both sets. Net effect: the
# desktop GUI's structural contract was asserted by NOTHING.
_failures: list[str] = []


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)
        _failures.append(name)


try:
    import pytest

    @pytest.fixture(autouse=True)
    def _check_guard():
        """Turn every soft-assert (check) into a real pytest failure.

        Mirrors ``tests_core.py``'s guard. Without it the window-contract
        checks (``has-extract-page``, ``lang-nav-relabels``,
        ``providers-has-test-workers``, …) were decoration.
        """
        _failures.clear()
        yield
        if _failures:
            raise AssertionError(
                "check() failures in this test: " + ", ".join(_failures))
except ImportError:
    # Standalone mode: the fixture is unavailable and the
    # ``if __name__ == "__main__"`` driver reports via the _fail counter.
    pass


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

    # Stub both bindings: gui_fluent.py did `from rca_core import extract` which
    # created an independent module-level binding in gui_fluent, so patching
    # rca_core.extractor.extract alone does NOT intercept the call site.
    import rca_core.extractor as E
    orig_ext = E.extract
    orig_gf = gui_fluent.extract
    E.extract = fake_extract
    gui_fluent.extract = fake_extract
    try:
        w = gui_fluent.ExtractWorker(
            params={"api_key": "k", "image_b64": "QUFB", "media_type": "image/png"},
            mode="range_chart", runs=1)
        results = []
        w.finished_ok.connect(lambda _w, r: results.append(r))
        w.start()
        # Wait for thread to finish (max 5s) AND pump Qt event loop so the
        # cross-thread queued signal gets delivered to the main thread.
        import time as _time
        deadline = _time.time() + 5
        while (not results) and _time.time() < deadline:
            app.processEvents()
            _time.sleep(0.05)
        assert w.wait(5000), "worker did not finish in 5s"
        assert len(results) == 1, f"expected 1 result, got {len(results)}"
        assert results[0].ok
        assert captured.get("image_b64") == "QUFB"
    finally:
        E.extract = orig_ext
        gui_fluent.extract = orig_gf


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
    orig_ext = E.extract
    orig_gf = gui_fluent.extract
    E.extract = fake_extract
    gui_fluent.extract = fake_extract
    try:
        w = gui_fluent.ExtractWorker(
            params={"api_key": "k", "image_b64": "QUFB", "media_type": "image/png"},
            mode="range_chart", runs=1)
        results = []
        w.finished_ok.connect(lambda _w, r: results.append(r))
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
        E.extract = orig_ext
        gui_fluent.extract = orig_gf


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
    """sections[0] with BOTH 'id' and 'name' -> columnar (id wins).

    AUDIT-2026-09-27 [item 0.1]: this asserted the OPPOSITE ("-> range-chart
    (avoid mis-route)") and had been failing since the core was changed,
    unnoticed because ``check()`` only printed.

    The CODE is right and the TEST was stale, confirmed three ways:
    ``rca_core/exporter._looks_columnar`` documents in place that ``id`` is
    the definitive columnar marker "even if name is also present (LLM being
    verbose)"; the JS mirror ``js/table.js`` uses the identical
    ``'id' in sects[0]`` rule; and ``tests_frontend.js`` already pins that
    rule. Range-chart sections carry a measured-section ``name`` and never an
    ``id``, so an ``id`` means a column LABEL and the columnar tables are the
    right destination — mis-routing it to the range-chart tables is exactly
    the failure this rule prevents.
    """
    from rca_core.exporter import _looks_columnar
    data = {"sections": [{"id": "Ki-1", "name": "A"}]}
    check("col-detect-both", _looks_columnar(data) is True)


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
def test_norm_preserves_qualifiers_and_collapses_whitespace():
    """_norm must collapse whitespace and PRESERVE open-nomenclature
    qualifiers.

    AUDIT-2026-09-27 [item 0.1]: this used to be named ``..._strips_sp_cf`` and
    assert the OPPOSITE — ``_norm("Neoalbaillella sp.") == "neoalbaillella"`` —
    and it had been FAILING that way ever since ``_norm`` was changed to keep
    qualifiers. Nothing noticed, because ``check()`` in this file only printed
    (see the module docstring): the desktop GUI's structural contract was
    asserted by nothing at all.

    The CODE is right and the TEST was stale. ``rca_core/aggregate.py``'s
    ``_norm`` documents the reason in place: the qualifier is carried as a
    separate component of the dedup key by ``_extract_qualifiers``, so an
    indeterminate "Genus sp." can never collapse into the identified "Genus" —
    which is an ICZN-correctness property, not cosmetics. The assertion now
    pins the deliberate behaviour.
    """
    from rca_core.aggregate import _norm
    check("norm-keep-sp", _norm("Neoalbaillella sp.") == "neoalbaillella sp.")
    check("norm-keep-cf",
          _norm("Entactinia cf. sashidai") == "entactinia cf. sashidai")
    check("norm-collapse-ws", _norm("  Hello   World  ") == "hello world")
    check("norm-lowercases", _norm("Neoalbaillella optima") == "neoalbaillella optima")


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





def _shiboken_delete(obj):
    """C++-level delete, as used by the other GUI suites.

    See ``tests/test_gui_sprint_b.py::_destroy_window`` and
    ``tests/test_gui_fluent_low_fixes.py`` for the full rationale: QtWebEngine
    must release its page BEFORE the view and the window go away, otherwise
    the process aborts with an access violation (0xC0000005) at teardown.
    """
    try:
        import shiboken6
    except Exception:
        try:
            import shiboken2 as shiboken6  # type: ignore
        except Exception:
            return False
    try:
        shiboken6.delete(obj)
        return True
    except Exception:
        return False


def _destroy_window(win):
    """Eager, WebEngine-safe teardown of a RangeChartFluentWindow.

    AUDIT-2026-09-27 [item 0.1]: a plain ``win.close(); win.deleteLater()``
    left the QWebEngineView's page to be released by the GC with no event
    loop left to service it, so the process died with 0xC0000005 AFTER every
    assertion had run — standalone reported -1073741819 whatever the checks
    said, and under pytest it crashed the whole session. Reusing the proven
    recipe from the other GUI suites (detach the page, delete it at C++ level,
    then delete the window) makes the exit code mean something again.
    """
    try:
        page = getattr(win, "extract_page", None)
        view = getattr(page, "phylotree", None) if page is not None else None
        web_page = view.page() if view is not None else None
        if web_page is not None:
            view.setPage(None)
            _shiboken_delete(web_page)
    except Exception:
        pass
    try:
        win.close()
    except Exception:
        pass
    if not _shiboken_delete(win):
        try:
            win.deleteLater()
        except Exception:
            pass
    _settle_after_teardown()


def _settle_after_teardown():
    """Drain the event loop after a window is destroyed (best effort)."""
    try:
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    app = QApplication.instance()
    if app is None:
        return
    try:
        for _ in range(2):
            app.sendPostedEvents(None, QEventLoop.DeferredDelete)
            loop = QEventLoop()
            QTimer.singleShot(80, loop.quit)
            loop.exec()
        app.processEvents()
    except Exception:
        pass


def _run_test_functions():
    """AUDIT-2026-09-27 [item 0.2]: run every ``test_*`` function in this
    module.

    ``run_all()`` used to execute ONLY its own inline window checks, so the
    eleven ``test_*`` functions never ran in standalone mode; conversely
    pytest only ever ran the ``test_*`` functions, so the window checks
    never ran under pytest. Calling the same functions both ways is what
    makes the two halves agree. A raising test is recorded as a failure
    rather than aborting the remaining ones.
    """
    import inspect
    import sys as _sys
    global _fail
    mod = _sys.modules[__name__]
    names = [n for n, f in vars(mod).items()
             if n.startswith("test_") and inspect.isfunction(f)]
    for name in sorted(names):
        before = _fail
        try:
            vars(mod)[name]()
        except Exception as exc:  # noqa: BLE001 - report, keep going
            _fail += 1
            _failures.append("%s raised %s: %s"
                             % (name, type(exc).__name__, exc))
            print("FAIL", name, "raised", type(exc).__name__, exc)
        if _fail > before:
            print("  -> %s had failures" % name)


# ---- Window contract (was inline in run_all(); see item 0.2) -------------
def test_window_contract():
    """The main window's structural contract.

    AUDIT-2026-09-27 [item 0.2]: these checks used to live INSIDE
    ``run_all()``. That made them invisible to pytest (which only collects
    ``test_*`` functions), so a deliberate break of e.g.
    ``has-extract-page`` failed standalone and passed under pytest. Moving
    the body into a real test function is what makes both paths execute the
    same assertions — verified by deliberately breaking one and watching
    BOTH runners report it.
    """
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
        # AUDIT-2026-09-27 [item 2.7]: this asserted membership in a
        # hardcoded ("auto", "range_chart", "columnar_section") — three of the
        # SIX modes the app supports, so it failed as soon as the persisted
        # config held any other one (a real run left `abundance_diagram`
        # there, and the second run of this file read it back and went red).
        # Asserted against the Extract page's own code table instead, which
        # is the single source of truth for chart types since D2 — so adding
        # a mode can never make this test rot again.
        _codes = win.extract_page._ctype_codes
        check("chart-type-valid(%s)" % win.chart_type(),
              win.chart_type() in _codes)
        check("chart-type-table-has-six(%s)" % len(_codes), len(_codes) == 6)

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
        # AUDIT-2026-09-27 [item 1.2]: this used to read
        # `settings_page.btn_save_key.text()`. That button is gone — the API
        # key field self-commits on Enter / focus-out, because two identical
        # `settings.save` buttons both wrote the whole config and either one
        # blanked cfg["api_key"] when "remember" was off. Assert on a label
        # that still exists AND whose zh/en strings actually differ:
        # `settings.apiKey` is "API Key" in both locales, so switching to it
        # would have made this check pass for the wrong reason (or fail, as it
        # did) without testing retranslate at all.
        zh_btn = win.settings_page.lbl_active_title.text()
        win._cycle_lang()  # -> en
        check("lang-nav-relabels", win._nav_settings.text() != zh_nav)
        check("lang-page-relabels",
              win.settings_page.lbl_active_title.text() != zh_btn)
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
        # Audit fix: gui_fluent.ConnTestWorker was removed (it was dead
        # code; ProvidersPage defines its own inline _Worker instead). The
        # test should no longer check for the removed module-level class —
        # verify that the providers page actually defines the inline worker.
        # _test_workers is set in __init__ (it's an instance attribute).
        check("providers-has-test-workers",
              isinstance(getattr(win.providers_page, "_test_workers", None), dict))
    finally:
        # AUDIT-2026-09-27 [item 0.1]: WebEngine-safe teardown — a plain
        # close/deleteLater leaves the process aborting at shutdown.
        _destroy_window(win)


def run_all():
    if not _have_deps():
        print("SKIP tests_gui_fluent: PySide6 / qfluentwidgets not installed")
        return 0

    # AUDIT-2026-09-27 [item 0.2]: every test_* function, including the window
    # contract, runs here — so standalone and pytest execute the SAME set.
    _run_test_functions()

    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    import sys
    rc = run_all()
    # AUDIT-2026-09-27 [item 0.1]: the process still aborts with an access
    # violation (0xC0000005) during INTERPRETER shutdown, long after
    # run_all() returned: the QApplication and its QWebEngineView-based
    # PhyloTreeWidget are released by the GC during teardown, with no event
    # loop left to service the deferred deletes. Every verdict is already
    # final at this point, so skip interpreter shutdown entirely and report
    # the real result. This is not masking a failure — the checks all ran
    # and `rc` reflects them; what is being skipped is a Qt teardown crash
    # that would otherwise overwrite a correct exit code with -1073741819.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
