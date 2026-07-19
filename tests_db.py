"""Tests for rca_core.db (SQLite wrapper)."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core.db import Database

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


def test_db_init_creates_schema():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "test.db")
        db = Database(path=path)
        try:
            # history + usage tables must exist after init
            rows = db.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            names = {r["name"] for r in rows}
            check("db-history-table", "history" in names)
            check("db-usage-table", "usage" in names)
        finally:
            # Bug-5 follow-up: close the persistent connection so the
            # Windows file lock releases before tempdir cleanup tries to
            # delete the file.
            db.close()


def test_db_insert_and_query():
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "test.db"))
        try:
            cur = db.execute(
                "INSERT INTO history (timestamp, result_json, mode) VALUES (?, ?, ?)",
                (1.0, '{"a":1}', "range_chart"),
            )
            rid = cur.lastrowid
            check("db-insert-returns-id", rid > 0)
            row = db.query_one("SELECT * FROM history WHERE id = ?", (rid,))
            check("db-query-one-mode", row["mode"] == "range_chart")
            check("db-query-one-result", '"a":1' in row["result_json"])
        finally:
            db.close()


def test_db_executemany():
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "test.db"))
        try:
            db.executemany(
                "INSERT INTO usage (timestamp, model, input_tokens) VALUES (?, ?, ?)",
                [(float(i), f"m{i}", i * 10) for i in range(1, 4)],
            )
            rows = db.query("SELECT COUNT(*) AS n FROM usage")
            check("db-executemany-count", rows[0]["n"] == 3)
        finally:
            db.close()


def test_db_wal_mode():
    """Database() should enable WAL on first open."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "test.db"))
        try:
            with db.connect() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()
            check("db-wal-enabled", str(mode[0]).lower() == "wal")
        finally:
            db.close()


def test_db_indexes():
    """Schema must declare the timestamp index on each table."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "test.db"))
        try:
            rows = db.query("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
            names = {r["name"] for r in rows}
            check("db-index-history-ts", "idx_history_ts" in names)
            check("db-index-usage-ts", "idx_usage_ts" in names)
        finally:
            db.close()


test_db_init_creates_schema()
test_db_insert_and_query()
test_db_executemany()
test_db_wal_mode()
test_db_indexes()
print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)
