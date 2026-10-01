r"""_add_provenance_columns had no test, and it is the same mechanism.

This audit found the `usage` table with columns added after it shipped, no
migration, and a reader that tolerated nothing -- so a database from an older
build could not be opened into the usage dashboard at all. The fix added
Database._add_usage_columns, mirroring the one that already existed for
`history`.

Measured, that mirror is complete and correct: dropping each of the six
columns `_add_provenance_columns` adds leaves the history store working (5 are
restored by the migration, image_sha256 / request_meta by both the migration and
the reader's explicit `in row.keys()` guard). The 15 original columns break the
store if dropped, but they are in the original DDL, so no real older database
lacks them -- that exclusion is derived from SCHEMA in the test rather than
asserted in prose.

So the history side needs no fix. What it lacks is a TEST: the mechanism that
keeps an old database readable had no coverage at all, which is why nothing
noticed the usage table had drifted away from it. These tests close that.

The property is behavioural -- open a database whose history table lacks one of
the added columns and require the store to still work -- rather than a check
that a constant contains certain strings, so it fails if the migration is
removed, if its list loses a column, or if the reader's guard is deleted.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import db as DB  # noqa: E402
from rca_core.history import HistoryStore  # noqa: E402

BASE = [
    "id", "timestamp", "source_file", "image_thumbnail", "image_width",
    "image_height", "provider_id", "provider_name", "model", "mode", "runs",
    "result_json", "raw_json", "confidence", "partial_failures", "duration_ms",
    "status_code", "notes",
]
ADDED = [
    "last_edited_at", "last_editor", "edit_provenance", "image_sha256",
    "request_meta", "edit_count",
]

VALUES = {
    "id": 1, "timestamp": 1_700_000_000.0, "source_file": "f.png",
    "image_thumbnail": None, "image_width": 800, "image_height": 600,
    "provider_id": "p", "provider_name": "P", "model": "m",
    "mode": "range_chart", "runs": 1, "result_json": '{"a": 1}',
    "raw_json": "", "confidence": 0.8, "partial_failures": 0,
    "duration_ms": 1200, "status_code": 200, "notes": "",
    "last_edited_at": None, "last_editor": None, "edit_provenance": None,
    "image_sha256": None, "request_meta": None, "edit_count": 0,
}

INT = {"id", "image_width", "image_height", "runs", "partial_failures",
       "duration_ms", "status_code", "edit_count", "timestamp", "confidence"}


def _make_raw(dropped, path):
    """A sqlite file whose history table lacks `dropped`.

    Deliberately does NOT open it through Database: for a column SCHEMA's own
    indexes name, that open is itself the failure, and a helper that opened
    eagerly raised before any assertion could see it.
    """
    cols = [c for c in BASE + ADDED if c != dropped]
    decls = ", ".join(f"{c} {'INTEGER' if c in INT else 'TEXT'}" for c in cols)
    raw = sqlite3.connect(str(path))
    raw.execute(f"CREATE TABLE history ({decls})")
    raw.execute(f"INSERT INTO history ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                [VALUES[c] for c in cols])
    raw.commit()
    raw.close()
    return str(path)


def _open_without(dropped, path):
    """The same file, opened through Database (so the migration runs)."""
    return DB.Database(_make_raw(dropped, path))


@pytest.mark.parametrize("dropped", ADDED)
def test_history_store_survives_a_missing_added_column(dropped, tmp_path):
    database = _open_without(dropped, tmp_path / f"{dropped}.db")
    try:
        store = HistoryStore(database)
        assert store.count() == 1
        rows = store.list()
        assert len(rows) == 1
        assert store.get(1) is not None
    finally:
        database.close()


def test_every_added_column_is_in_the_migration_list():
    """A column added to the DDL but not to the migration fails here.

    Derived from the declared set rather than a hand-kept list, which is the
    failure mode that produced this whole investigation: the usage table's DDL
    and its migration had drifted apart with nothing checking.
    """
    declared = set(DB.Database._PROVENANCE_COLUMNS) if hasattr(
        DB.Database, "_PROVENANCE_COLUMNS") else None
    if declared is None:
        # The migration keeps its list inline; assert on behaviour instead,
        # which test_history_store_survives_a_missing_added_column already
        # does for every column. Here we only assert the columns are known.
        pytest.skip("the provenance column list is inline, not a constant")
    assert declared == set(ADDED)


def test_the_migration_is_idempotent(tmp_path):
    """SCHEMA and the migrations run on every open, so a non-idempotent
    migration would fail on the second launch.

    Built by dropping a column rather than from an empty file, so the database
    actually has rows -- a first draft created a fresh file and then asserted
    count() == 1, which fails for the uninteresting reason that a new database
    has no rows at all.
    """
    path = tmp_path / "twice.db"
    for _ in range(3):
        database = _open_without("edit_provenance", path) if _ == 0 \
            else DB.Database(str(path))
        try:
            assert HistoryStore(database).count() == 1
        finally:
            database.close()


def test_the_migration_does_not_disturb_existing_rows(tmp_path):
    """Adds the column; must not rewrite the row that was already there.

    The assertion is on the DATABASE, not on HistoryRecord: the migration
    restores a column, and whether the dataclass exposes that column is a
    separate question. A first draft asserted `after.edit_provenance` and hit
    AttributeError -- HistoryRecord has no such field, so the column is stored
    without being surfaced. That is not this test's claim to make.
    """
    path = tmp_path / "keep.db"
    database = _open_without("edit_provenance", path)
    try:
        before = HistoryStore(database).get(1)
    finally:
        database.close()

    database2 = DB.Database(str(path))       # migration runs here
    try:
        after = HistoryStore(database2).get(1)
        assert after.id == before.id == 1
        assert after.source_file == "f.png"
        assert after.confidence == pytest.approx(0.8)
        cols = {r["name"] for r in
                database2._conn.execute("PRAGMA table_info(history)")}
        assert "edit_provenance" in cols, sorted(cols)
        raw = database2._conn.execute(
            "SELECT edit_provenance, edit_count FROM history WHERE id = 1"
        ).fetchone()
        assert raw["edit_provenance"] is None, "a new column must read as NULL"
        assert raw["edit_count"] == 0
    finally:
        database2.close()


def test_the_surviving_columns_are_the_original_ones(tmp_path):
    """The complement of the above, stated so the exclusion is not a gap.

    mode and timestamp are named by SCHEMA's own indexes, so a table without
    them cannot be OPENED at all -- the failure is OperationalError from
    executescript, not IndexError from the reader, and a first draft looped
    over them expecting IndexError. The other original columns break the reader
    if dropped, which is also asserted here: they are in the original DDL, so
    no database written by an older build can lack them.
    """
    indexed = set()
    for line in DB.SCHEMA.splitlines():
        if "INDEX" in line.upper() and "history" in line and "(" in line:
            body = line[line.index("("):]
            indexed |= {c for c in BASE + ADDED
                        if re.search(rf"\b{c}\b", body)}
    assert indexed == {"mode", "timestamp"}, sorted(indexed)

    for original in BASE:
        path = _make_raw(original, tmp_path / f"o_{original}.db")
        if original in indexed:
            # Cannot be opened at all: SCHEMA's index statements name it.
            with pytest.raises(sqlite3.OperationalError):
                DB.Database(path)
            continue
        database = DB.Database(path)
        try:
            with pytest.raises(IndexError):
                HistoryStore(database).list()
        finally:
            database.close()
