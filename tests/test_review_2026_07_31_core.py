"""Core regression tests for REVIEW-2026-07-31 fixes.

Covers:
  * safe_json_loads wrapper promotion ({"data": {...}} payloads surface at
    the top level at Levels 3/4/6);
  * usage by_day timezone sign (rows land on the correct LOCAL day);
  * HistoryStore.update_result is atomic (a failing audit INSERT rolls the
    result UPDATE back);
  * gui.py _save_to_history is a real RangeChartApp method (the previous
    misindentation made it dead code that raised AttributeError);
  * server.py multi-run usage aggregation is built before the audit write
    (no more swallowed UnboundLocalError).
"""
from __future__ import annotations

import json
import os
import sys
import time
import pytest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


class TestJsonWrapperPromotion:
    def test_level3_pure_wrapper(self):
        from rca_core.json_utils import safe_json_loads
        r = safe_json_loads('{"data": {"species_ranges": [{"species": "A"}], "confidence": 0.9}}')
        assert "species_ranges" in r
        assert r["confidence"] == 0.9

    def test_level3_wrapper_with_siblings(self):
        from rca_core.json_utils import safe_json_loads
        r = safe_json_loads(
            '{"data": {"species_ranges": []}, "confidence": 0.8, "note": "x"}'
        )
        assert "species_ranges" in r
        assert r["confidence"] == 0.8
        assert r["note"] == "x"

    def test_level4_prose_wrapper(self):
        """Schema restated in prose first, payload inside {"data": ...}
        (the M2 regression case)."""
        from rca_core.json_utils import safe_json_loads
        text = ('Schema: {"properties": {"species_ranges": {"type": "array", '
                '"items": {"type": "object"}}}, "required": ["species_ranges"]}. '
                'Result: {"data": {"species_ranges": [{"species": "B", '
                '"range_base": "1", "range_top": "5"}], "confidence": 0.8}}')
        r = safe_json_loads(text)
        assert len(r.get("species_ranges", [])) == 1
        assert r["species_ranges"][0]["species"] == "B"

    def test_level6_prose_wrapper(self):
        from rca_core.json_utils import safe_json_loads
        r = safe_json_loads('Here is the result: {"data": {"sections": [{"name": "X"}]}}')
        assert len(r.get("sections", [])) == 1

    def test_no_wrapper_no_change(self):
        from rca_core.json_utils import safe_json_loads
        r = safe_json_loads('{"species_ranges": [{"species": "C"}]}')
        assert r["species_ranges"][0]["species"] == "C"


class TestUsageByDayTimezone:
    def _make_usage_store(self, tmp_path):
        from rca_core.db import Database
        from rca_core.usage import UsageStore
        db = Database(path=str(tmp_path / "usage.db"))
        return UsageStore(db=db)

    def test_by_day_east_positive_offset(self, tmp_path, monkeypatch):
        """Rows at local 00:00-08:00 (UTC+8) must land on the SAME local
        day, not the previous one (the old sign bug)."""
        import time as _time
        from rca_core.usage import UsageRecord

        class FakeLocaltime:
            def __init__(self):
                self.tm_gmtoff = 8 * 3600  # UTC+8 (east-positive)

            def __call__(self, ts):
                return self

        monkeypatch.setattr(_time, "localtime", FakeLocaltime())
        import datetime
        # UTC 2026-07-31 18:00 = Beijing 2026-08-01 02:00 (local day 08-01).
        utc_ts = int(datetime.datetime(
            2026, 7, 31, 18, 0, tzinfo=datetime.timezone.utc).timestamp())
        store = self._make_usage_store(tmp_path)
        store.record(UsageRecord(
            timestamp=utc_ts, provider_id="p", provider_name="p",
            model="m", mode="range_chart",
            input_tokens=100, output_tokens=50,
        ))
        s = store.summary()
        day_keys = [d["day"] for d in s.by_day]
        # AUDIT-2026-09-27 P2: the expected key is now the epoch of LOCAL
        # MIDNIGHT of the day holding the majority of this UTC day, which is
        # what the consumer (`time.strftime("%m-%d", time.localtime(d["day"]))`
        # in gui_fluent_pages.py) needs in order to print the right date.
        #
        # It used to be `utc_midnight + offset`, i.e. a SHIFTED timestamp,
        # while the consumer re-applies the zone — so the offset was counted
        # TWICE. That is invisible east of UTC and wrong west of it, which is
        # why REVIEW-2026-07-31's SIGN fix (east-positive, below) looked
        # correct and this offset was never revisited. The sign itself is
        # still right and is still asserted here.
        off = _time.localtime(utc_ts + 43200).tm_gmtoff
        majority = (utc_ts // 86400) * 86400 + off + 43200
        expected_day = (majority // 86400) * 86400 - off
        assert expected_day in day_keys, day_keys
        # The invariant the consumer depends on, stated without touching
        # ``_time.localtime`` (this test monkeypatches it with a stub that
        # carries only ``tm_gmtoff``): adding the zone offset to the key must
        # land exactly on a UTC midnight, which is what "00:00 local" means.
        assert (expected_day + off) % 86400 == 0, (expected_day, off)
        bucket = next(d for d in s.by_day if d["day"] == expected_day)
        assert bucket["count"] == 1

    def test_by_day_west_negative_offset(self, tmp_path, monkeypatch):
        import time as _time
        import datetime
        from rca_core.usage import UsageRecord

        class FakeLocaltime:
            def __init__(self):
                self.tm_gmtoff = -5 * 3600  # UTC-5 (east-negative)

            def __call__(self, ts):
                return self

        monkeypatch.setattr(_time, "localtime", FakeLocaltime())
        # UTC 2026-07-31 22:00 = NY 2026-07-31 17:00 (same local day).
        utc_ts = int(datetime.datetime(
            2026, 7, 31, 22, 0, tzinfo=datetime.timezone.utc).timestamp())
        store = self._make_usage_store(tmp_path)
        store.record(UsageRecord(
            timestamp=utc_ts, provider_id="p", provider_name="p",
            model="m", mode="range_chart",
            input_tokens=100, output_tokens=50,
        ))
        s = store.summary()
        day_keys = [d["day"] for d in s.by_day]
        # AUDIT-2026-09-27 P2: see the east-offset test above - the key is the
        # epoch of LOCAL MIDNIGHT, not a zone-shifted timestamp. West of UTC
        # this is the case the old formula got wrong: with the double
        # application, UTC 2026-07-31 22:00 was filed under 2026-07-30.
        off = _time.localtime(utc_ts + 43200).tm_gmtoff
        majority = (utc_ts // 86400) * 86400 + off + 43200
        expected_day = (majority // 86400) * 86400 - off
        assert expected_day in day_keys, day_keys
        # UTC 2026-07-31 22:00 in UTC-5 is 17:00 local on the SAME day, so the
        # bucket must resolve to 2026-07-31 - which, with the old double-offset
        # formula, came out as 2026-07-30. Pure arithmetic here because
        # ``_time.localtime`` is stubbed in this test.
        assert (expected_day + off) % 86400 == 0, (expected_day, off)
        local_date = _time.strftime(
            "%Y-%m-%d",
            _time.gmtime(expected_day + off))     # +off == local wall clock
        assert local_date == "2026-07-31", local_date


class TestHistoryAtomicity:
    def _make_store(self, tmp_path):
        from rca_core.db import Database
        from rca_core.history import HistoryRecord, HistoryStore
        db = Database(path=str(tmp_path / "hist.db"))
        store = HistoryStore(db=db)
        rec_id = store.add(HistoryRecord(
            timestamp=time.time(), mode="range_chart", runs=1,
            result={"species_ranges": [{"species": "Before"}]},
        ))
        return store, rec_id

    def test_update_result_rolls_back_on_failure(self, tmp_path, monkeypatch):
        """If the audit INSERT fails, the result UPDATE must NOT persist
        (single transaction)."""
        store, rec_id = self._make_store(tmp_path)
        from rca_core import history as hist_module
        original = hist_module.Database.run

        def failing_run(self, sql, params=()):
            if sql.startswith("INSERT INTO record_edits"):
                raise RuntimeError("audit insert failed")
            return original(self, sql, params)

        monkeypatch.setattr(hist_module.Database, "run", failing_run)
        with pytest.raises(RuntimeError):
            store.update_result(rec_id, {"species_ranges": [{"species": "After"}]})
        # The UPDATE must have been rolled back with the failed INSERT.
        rec = store.get(rec_id)
        assert rec.result == {"species_ranges": [{"species": "Before"}]}, (
            "result UPDATE survived the failed audit INSERT — transaction "
            "atomicity broken"
        )

    def test_update_result_success_path(self, tmp_path):
        store, rec_id = self._make_store(tmp_path)
        assert store.update_result(rec_id, {"species_ranges": [{"species": "After"}]})
        rec = store.get(rec_id)
        assert rec.result == {"species_ranges": [{"species": "After"}]}


class TestGuiSaveToHistoryIsMethod:
    def test_method_exists_on_class(self):
        """gui.py: _save_to_history must be a RangeChartApp method — the
        previous misindentation defined it inside `if __name__ ==
        "__main__":`, so every successful Tkinter extraction raised
        AttributeError (silently swallowed) and the audit trail was never
        written."""
        import ast
        src = (REPO / "gui.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "RangeChartApp")
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        assert "_save_to_history" in methods
        assert "_maybe_thumbnail" in methods

    def test_module_imports_cleanly(self):
        import gui  # noqa: F401
        assert hasattr(gui.RangeChartApp, "_save_to_history")

    def test_current_provider_assigns_active_provider(self):
        """_current_provider must set self._active_provider so the history
        record carries provider_id / provider_name / model."""
        import gui
        src = (REPO / "gui.py").read_text(encoding="utf-8")
        assert "_active_provider" in src
        assert "self._active_provider = store.get_current()" in src or \
            "self._active_provider = self._current_provider()" in src


class TestServerCSPHeader:
    """UI-REVIEW-2026-08-01 (B1) regression: the static-file CSP must
    allow data: images (upload preview) and inline style attributes
    (result renderer) or the default backend deployment breaks the
    upload->extract flow."""

    def test_static_csp_allows_data_images_and_inline_styles(self):
        import threading
        import urllib.request
        from server import _make_bounded_server

        httpd = _make_bounded_server("127.0.0.1", 0, 4)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
                csp = r.headers.get("Content-Security-Policy") or ""
            assert "img-src 'self' data:" in csp, (
                f"upload preview (data: URLs) blocked by CSP: {csp!r}"
            )
            assert "style-src 'self' 'unsafe-inline'" in csp, (
                f"inline style attributes blocked by CSP: {csp!r}"
            )
            assert "script-src" not in csp, (
                "script-src should stay unset so it falls back to "
                "'self' (no inline scripts allowed)"
            )
        finally:
            httpd.shutdown()


class TestServerMergedUsageOrder:
    def test_merged_usage_built_before_audit_write(self):
        """server.py: merged_usage must be assigned BEFORE the try block
        that writes the history record (the old order raised a swallowed
        UnboundLocalError, so multi-run audit records were never written)."""
        src = (REPO / "server.py").read_text(encoding="utf-8")
        audit_idx = src.index("merged_usage = {")
        usage_idx = src.index("usage=merged_usage")
        assert audit_idx < usage_idx, (
            "merged_usage is still assigned after the audit write — "
            "UnboundLocalError regression"
        )
