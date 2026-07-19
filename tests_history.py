"""Tests for rca_core.history (CRUD over the history table)."""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core.db import Database
from rca_core.history import HistoryRecord, HistoryStore

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


def fresh_store():
    td = tempfile.mkdtemp()
    db = Database(path=os.path.join(td, "test.db"))
    return HistoryStore(db=db), td


def test_add_and_get():
    store, td = fresh_store()
    try:
        rec = HistoryRecord(
            timestamp=time.time(),
            source_file="chart1.png",
            provider_name="MiniMax M3",
            model="MiniMax-M3",
            mode="range_chart",
            runs=1,
            result={"sections": [{"name": "A"}], "species_ranges": [], "confidence": 0.9},
            confidence=0.9,
        )
        rid = store.add(rec)
        check("history-add-id", rid > 0)
        loaded = store.get(rid)
        check("history-get-source", loaded.source_file == "chart1.png")
        check("history-get-result-shape", loaded.result.get("sections", [{}])[0].get("name") == "A")
        check("history-get-confidence", abs(loaded.confidence - 0.9) < 1e-6)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_list_ordering_desc():
    store, td = fresh_store()
    try:
        for i in range(5):
            store.add(HistoryRecord(timestamp=time.time() + i, source_file=f"f{i}.png", result={"x": 1}))
        rows = store.list()
        check("history-list-count", len(rows) == 5)
        check("history-list-newest-first", rows[0].source_file == "f4.png")
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_update_notes():
    store, td = fresh_store()
    try:
        rid = store.add(HistoryRecord(timestamp=time.time(), result={"x": 1}, source_file="a"))
        ok = store.update_notes(rid, "tagged by me")
        check("history-update-notes-ok", ok is True)
        loaded = store.get(rid)
        check("history-update-notes-value", loaded.notes == "tagged by me")
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_update_result():
    store, td = fresh_store()
    try:
        rid = store.add(HistoryRecord(timestamp=time.time(), result={"x": 1}, source_file="a"))
        store.update_result(rid, {"sections": [{"name": "Edited"}], "species_ranges": []})
        loaded = store.get(rid)
        check("history-update-result-shape",
              loaded.result.get("sections", [{}])[0].get("name") == "Edited")
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_delete():
    store, td = fresh_store()
    try:
        rid = store.add(HistoryRecord(timestamp=time.time(), result={"x": 1}, source_file="a"))
        check("history-delete-pre-count", store.count() == 1)
        ok = store.delete(rid)
        check("history-delete-ok", ok is True)
        check("history-delete-post-count", store.count() == 0)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_search_filter():
    store, td = fresh_store()
    try:
        store.add(HistoryRecord(timestamp=time.time(), source_file="alpha.png", result={}, notes="hello world"))
        store.add(HistoryRecord(timestamp=time.time(), source_file="beta.png", result={}, notes=""))
        rows = store.list(search="hello")
        check("history-search-found", len(rows) == 1)
        check("history-search-correct", rows[0].source_file == "alpha.png")
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_mode_filter():
    store, td = fresh_store()
    try:
        store.add(HistoryRecord(timestamp=time.time(), mode="range_chart", result={}))
        store.add(HistoryRecord(timestamp=time.time(), mode="columnar_section", result={}))
        rows = store.list(mode="columnar_section")
        check("history-mode-filter-count", len(rows) == 1)
        check("history-mode-filter-mode", rows[0].mode == "columnar_section")
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_thumbnail_round_trip():
    """Image bytes survive a save / load round trip."""
    store, td = fresh_store()
    try:
        # 1x1 transparent PNG (real 67-byte PNG)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        rid = store.add(HistoryRecord(
            timestamp=time.time(),
            image_thumbnail=png_bytes,
            image_width=1, image_height=1,
            result={},
        ))
        loaded = store.get(rid)
        check("history-thumb-bytes-match", loaded.image_thumbnail == png_bytes)
        check("history-thumb-width", loaded.image_width == 1)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_clear():
    store, td = fresh_store()
    try:
        for _ in range(3):
            store.add(HistoryRecord(timestamp=time.time(), result={}))
        check("history-clear-pre", store.count() == 3)
        n = store.clear()
        check("history-clear-rows", n == 3)
        check("history-clear-post", store.count() == 0)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_history_detail_i18n_keys_present():
    """The HistoryDetailDialog uses these keys; they must exist in every
    language so a non-zh user sees a translated dialog (not raw key names)."""
    from rca_core.i18n import TRANSLATIONS
    detail_keys = [
        "history.detail.title",
        "history.detail.basicInfo",
        "history.detail.time",
        "history.detail.provider",
        "history.detail.model",
        "history.detail.mode",
        "history.detail.runs",
        "history.detail.confidence",
        "history.detail.status",
        "history.detail.duration",
        "history.detail.image",
        "history.detail.result",
        "history.detail.raw",
        "history.detail.close",
        "history.action.view",
    ]
    zh = set(TRANSLATIONS["zh"])
    for lang in TRANSLATIONS:
        for k in detail_keys:
            check(f"detail-key-{lang}-{k}", k in TRANSLATIONS[lang])
        check(f"detail-i18n-parity-{lang}",
              set(TRANSLATIONS[lang]) >= set(detail_keys))
    # Sanity: ZH set is a strict superset of detail_keys.
    check("detail-keys-in-zh", zh >= set(detail_keys))


def test_history_detail_translator_exposes_translations():
    """The detail dialog serializes `tr.translations` into the WebEngine
    page as the `RCA_I18N` object. The WebEngine page then looks up
    keys via `RCA_I18N[RCA_LANG]`, so the exposed dict must be the
    top-level {zh, en, ja} mapping — not a single language's inner dict.
    """
    from rca_core.i18n import TRANSLATIONS, Translator
    for lang in ("zh", "en", "ja"):
        tr = Translator(lang)
        check(f"translator-translations-{lang}-exists",
              hasattr(tr, "translations"))
        # Top-level dict must expose all three languages so the WebEngine
        # page can switch on the fly (it knows the user's language via
        # the separately-serialized RCA_LANG value).
        check(f"translator-translations-{lang}-has-all-lang-blocks",
              all(l in tr.translations for l in ("zh", "en", "ja")))
        # And the *active* language block must contain the detail key.
        check(f"translator-translations-{lang}-active-lang-has-detail-key",
              tr.translations[lang].get("history.detail.title") ==
              TRANSLATIONS[lang]["history.detail.title"])


def test_history_detail_record_to_dict_fields():
    """HistoryRecord.to_dict() must surface every field the detail dialog
    renders. If a field is renamed on the dataclass and the dialog forgets
    to update, this test catches it before users see 'undefined' in the UI."""
    rec = HistoryRecord(
        id=42,
        timestamp=1700000000.0,
        source_file="chart.png",
        image_thumbnail=b"\x89PNG\r\n\x1a\n",
        image_width=640,
        image_height=480,
        provider_id="prov-1",
        provider_name="Acme",
        model="m-7",
        mode="range_chart",
        runs=3,
        result={"sections": [{"name": "S1"}], "species_ranges": []},
        raw='{"sections":[{"name":"S1"}]}',
        confidence=0.85,
        partial_failures=1,
        duration_ms=1234,
        status_code=200,
        notes="some note",
    )
    d = rec.to_dict()
    needed = [
        "id", "timestamp", "source_file", "image_thumbnail_b64",
        "image_width", "image_height", "provider_id", "provider_name",
        "model", "mode", "runs", "result", "raw", "confidence",
        "partial_failures", "duration_ms", "status_code", "notes",
    ]
    for k in needed:
        check(f"record-field-{k}", k in d)
    # to_dict base64-encodes the thumbnail — verify it round-trips.
    import base64 as _b64
    check("record-thumb-roundtrip",
          _b64.b64decode(d["image_thumbnail_b64"]) == b"\x89PNG\r\n\x1a\n")


def test_history_detail_html_contains_bootstrap():
    """Build the WebEngine HTML and verify the embedded JS includes the
    payload and the render call. We don't spin up Qt here — just check the
    template substitution succeeded end-to-end (so a regression in the
    f-string breaks this test rather than producing a silent empty page)."""
    from rca_core.history import HistoryRecord
    from rca_core.i18n import Translator
    try:
        # Import the dialog module. If QWebEngineWidgets is missing this
        # still succeeds — only `_has_webengine()` returns False. We test
        # the HTML builder directly, which doesn't depend on Qt at all.
        from gui_fluent_history_detail import _build_webengine_html
    except Exception as exc:
        check("detail-import", False)
        print("import failed:", exc)
        return
    check("detail-import", True)
    rec = HistoryRecord(
        result={"sections": [{"name": "S1"}], "species_ranges": [],
                "confidence": 0.5},
        raw="raw-text",
    )
    tr = Translator("en")
    html = _build_webengine_html(rec, tr)
    # Must contain the bootstrap script and our result JSON.
    check("detail-html-has-render", "rcaRenderResults" in html)
    check("detail-html-has-data", '"sections"' in html or "sections" in html)
    # i18n.js is inlined into the page; verify one of the i18n keys that
    # table.js's rcaRenderResults depends on (`sec.sections`) is present.
    # The earlier version duplicated `RCA_I18N` as a `const` in bootstrap,
    # which crashed the script with SyntaxError — we now rely on the
    # inlined i18n.js to provide it. Asserting the key shows up proves
    # i18n.js survived inlining.
    check("detail-html-has-i18n-key", "sec.sections" in html)
    check("detail-html-has-inline-css", ".pill-good" in html)
    # Sanity: the page shouldn't include any unescaped </script> that would
    # break out of the inline JS block.
    check("detail-html-no-early-script-close",
          html.count("</script>") == 1)
    # Regression for the "result not visible" bug: the previous bootstrap
    # redeclared `const RCA_I18N = {...}` AFTER inlining js/i18n.js which
    # already declares it, causing WebEngine to throw SyntaxError and
    # render nothing. The fix is to reuse the inlined module's identifiers
    # instead of redeclaring them. Asserting exactly one `const RCA_I18N`
    # in the HTML pins this contract.
    import re as _re
    rca_18n_decls = _re.findall(r"\bconst\s+RCA_I18N\b", html)
    check("detail-html-no-duplicate-rca-i18n", len(rca_18n_decls) == 1)
    # RCA_LANG should be reassigned (let), not redeclared (const).
    check("detail-html-rca-lang-reassigned",
          _re.search(r"\bRCA_LANG\s*=\s*['\"]", html) is not None)


test_add_and_get()
test_list_ordering_desc()
test_update_notes()
test_update_result()
test_delete()
test_search_filter()
test_mode_filter()
test_thumbnail_round_trip()
test_clear()
test_history_detail_i18n_keys_present()
test_history_detail_translator_exposes_translations()
test_history_detail_record_to_dict_fields()
test_history_detail_html_contains_bootstrap()
print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)

