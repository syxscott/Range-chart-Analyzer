"""Regression tests for P1-5: HistoryStore.update_result must persist
edit provenance (timestamp + editor + edit_count + edit_provenance JSON
chain) so operators downstream can distinguish model-emitted vs
operator-edited fields.

REVIEW-2026-07-25 P1-5.
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.db import Database
from rca_core.history import HistoryStore, HistoryRecord


def _gc_temp():
    """Force GC + small sleep so SQLite releases the file handle on
    Windows before the TemporaryDirectory teardown."""
    gc.collect()
    import time as _t
    _t.sleep(0.05)


class TestHistoryEditProvenance:
    def test_first_edit_records_provenance(self):
        tmp = tempfile.mkdtemp(prefix="rca_p15_")
        try:
            db = Database(path=os.path.join(tmp, "t.db"))
            try:
                store = HistoryStore(db=db)
                rec = HistoryRecord(
                    timestamp=1.0, source_file="x.png",
                    provider_id="p", provider_name="P", model="m",
                    mode="range_chart", runs=1,
                    result={"species_ranges": [], "confidence": 0.5},
                    raw="{}",
                )
                rid = store.add(rec)
                assert store.update_result(
                    rid, {"species_ranges": [], "confidence": 0.9},
                    editor="user",
                )
                row = db.execute(
                    "SELECT edit_count, last_editor, last_edited_at, edit_provenance "
                    "FROM history WHERE id = ?", (rid,)
                ).fetchone()
                assert row[0] == 1, f"edit_count must be 1, got {row[0]}"
                assert row[1] == "user"
                assert row[2] is not None
                chain = __import__("json").loads(row[3])
                assert isinstance(chain, list) and len(chain) == 1
                assert chain[0]["editor"] == "user"
            finally:
                try:
                    db.close()
                except Exception:
                    pass
                _gc_temp()
        finally:
            # Best-effort cleanup; swallow Windows file-lock errors.
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_repeated_edits_accumulate(self):
        tmp = tempfile.mkdtemp(prefix="rca_p15_")
        try:
            db = Database(path=os.path.join(tmp, "t.db"))
            try:
                store = HistoryStore(db=db)
                rec = HistoryRecord(
                    timestamp=1.0, source_file="x.png",
                    provider_id="p", provider_name="P", model="m",
                    mode="range_chart", runs=1,
                    result={}, raw="{}",
                )
                rid = store.add(rec)
                store.update_result(rid, {}, editor="user")
                store.update_result(rid, {}, editor="gui_fluent")
                store.update_result(rid, {}, editor="js/app.js")
                row = db.execute(
                    "SELECT edit_count, edit_provenance FROM history WHERE id=?",
                    (rid,),
                ).fetchone()
                assert row[0] == 3, f"edit_count must be 3, got {row[0]}"
                import json
                chain = json.loads(row[1])
                assert len(chain) == 3
                assert [c["editor"] for c in chain] == ["user", "gui_fluent", "js/app.js"]
            finally:
                try:
                    db.close()
                except Exception:
                    pass
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    def test_columns_added_to_pre_existing_db(self):
        """A database file from an older version (without the new
        columns) must be auto-migrated when Database is opened."""
        import sqlite3
        tmp = tempfile.mkdtemp(prefix="rca_p15_legacy_")
        try:
            db_path = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(db_path)
            conn.executescript("""
                CREATE TABLE history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    source_file TEXT,
                    image_thumbnail BLOB,
                    image_width INTEGER, image_height INTEGER,
                    provider_id TEXT, provider_name TEXT,
                    model TEXT, mode TEXT, runs INTEGER DEFAULT 1,
                    result_json TEXT NOT NULL, raw_json TEXT,
                    confidence REAL DEFAULT 0,
                    partial_failures INTEGER DEFAULT 0,
                    duration_ms INTEGER DEFAULT 0,
                    status_code INTEGER,
                    notes TEXT DEFAULT ''
                );
            """)
            conn.commit()
            conn.close()
            db = Database(path=db_path)
            try:
                row = db.execute("PRAGMA table_info(history)").fetchall()
                cols = {r["name"] for r in row}
                for col in ("last_edited_at", "last_editor", "edit_count",
                            "edit_provenance"):
                    assert col in cols, f"migration failed to add {col}"
            finally:
                try:
                    db.close()
                except Exception:
                    pass
                _gc_temp()
        finally:
            import shutil
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass