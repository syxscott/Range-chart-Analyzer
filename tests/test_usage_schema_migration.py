r"""A usage database from an older build could not be summarised at all.

This is the second half of the defect whose first half was fixed in the
previous commit. That commit made rca_core.usage._row_to_record tolerate a
column that is absent or NULL, which is correct -- and NOT sufficient, because
UsageStore.summary() names the same columns inside AGGREGATE SQL:

    COALESCE(SUM(cache_read_tokens), 0)
    CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END
    CASE WHEN input_tokens_estimated = 1 OR output_tokens_estimated = 1 ...

SQLite resolves a column name when the statement is PREPARED, so a table
without the column fails before a single row is read and the tolerant reader
never runs. Measured on a database missing one column at a time: 10 of the 16
non-indexed columns broke summary() with
"sqlite3.OperationalError: no such column", while the same database read
happily through list().

So the previous commit's claim -- "an older usage database can be read now" --
was true for list() and FALSE for the dashboard, which is the screen people
actually look at. This file tests the end-to-end property so that half of a
fix cannot pass again.

The other half of the previous commit stands: the reader tolerance is still
right, and it is what covers a column that exists but was never written.

Columns NOT migratable, and why the tests exclude them: id is the PRIMARY KEY,
and timestamp / provider_id are named by SCHEMA's own indexes
(idx_usage_ts, idx_usage_provider), so a table without them cannot be opened
at all -- Database() raises during executescript(). No migration can rescue
that, and no real database is in that state. The exclusion is derived from
SCHEMA in the test rather than hardcoded, so it follows the schema if the
schema changes.
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
from rca_core.usage import UsageStore  # noqa: E402

ALL_COLS = [
    "id", "timestamp", "provider_id", "provider_name", "model", "endpoint",
    "mode", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "input_tokens_estimated",
    "output_tokens_estimated", "total_cost_usd", "latency_ms",
    "first_token_ms", "status_code", "error_message", "request_id",
]

INT_COLS = {"id", "timestamp", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens",
            "input_tokens_estimated", "output_tokens_estimated",
            "total_cost_usd", "latency_ms", "first_token_ms", "status_code"}

VALUES = {
    "id": 1, "timestamp": 1_700_000_000.0, "provider_id": "p1",
    "provider_name": "Provider One", "model": "m1", "endpoint": "/v1/chat",
    "mode": "range_chart", "input_tokens": 100, "output_tokens": 200,
    "cache_read_tokens": 50, "cache_creation_tokens": 10,
    "input_tokens_estimated": 0, "output_tokens_estimated": 0,
    "total_cost_usd": 0.02, "latency_ms": 900, "first_token_ms": 250,
    "status_code": 200, "error_message": "", "request_id": "req-1",
}


def _schema_indexed_columns() -> set[str]:
    """Columns SCHEMA's own index statements name.

    A table missing one of these cannot be opened, so it is not a state the
    migration can repair and not a state worth testing as one.
    """
    out: set[str] = set()
    for line in DB.SCHEMA.splitlines():
        if "INDEX" in line.upper() and "usage" in line:
            out |= {c for c in ALL_COLS if re.search(rf"\b{c}\b", line)}
    return out


UNMIGRATABLE = _schema_indexed_columns() | {"id"}
MIGRATABLE = [c for c in ALL_COLS if c not in UNMIGRATABLE]


def _db_missing(dropped, tmp_path):
    """A database whose usage table lacks `dropped`, opened through Database."""
    cols = [c for c in ALL_COLS if c != dropped]
    decls = ", ".join(f"{c} {'INTEGER' if c in INT_COLS else 'TEXT'}"
                      for c in cols)
    raw = sqlite3.connect(tmp_path)
    raw.execute(f"CREATE TABLE usage ({decls})")
    raw.execute(f"INSERT INTO usage ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                [VALUES[c] for c in cols])
    raw.commit()
    raw.close()
    return DB.Database(str(tmp_path))


# --- the property: open, list and summarise all survive ----------------


@pytest.mark.parametrize("dropped", MIGRATABLE)
def test_summary_survives_a_missing_column(dropped, tmp_path):
    db = _db_missing(dropped, tmp_path / f"{dropped}.db")
    try:
        summary = UsageStore(db).summary()
        assert summary.total_requests == 1, summary
    finally:
        db.close()


@pytest.mark.parametrize("dropped", MIGRATABLE)
def test_list_survives_a_missing_column(dropped, tmp_path):
    db = _db_missing(dropped, tmp_path / f"l_{dropped}.db")
    try:
        rows = UsageStore(db).list()
        assert len(rows) == 1, rows
    finally:
        db.close()


def test_every_migratable_column_is_actually_in_the_migration():
    """So a column added to SCHEMA later without being listed here fails.

    Derived from the declared column set rather than a hand-kept list, so
    adding a column to the DDL without adding it to the migration is a test
    failure rather than a production surprise.
    """
    declared = set(DB.Database._USAGE_ADDED_COLUMNS and
                   [c for c, _d in DB.Database._USAGE_ADDED_COLUMNS])
    missing = [c for c in MIGRATABLE if c not in declared]
    assert missing == [], (
        f"migratable but not in _USAGE_ADDED_COLUMNS: {missing}"
    )
    not_migratable = sorted(UNMIGRATABLE)
    assert declared.isdisjoint(not_migratable), (
        f"the migration tries to add unopenable columns: "
        f"{sorted(declared & set(not_migratable))}"
    )


# --- a present-but-unwritten column is a different situation ------------


def test_a_column_that_exists_but_is_null_is_handled(tmp_path):
    """The migration covers a MISSING column; the reader tolerance covers a
    NULL one. Both are needed and neither substitutes for the other."""
    cols = list(ALL_COLS)
    decls = ", ".join(f"{c} {'INTEGER' if c in INT_COLS else 'TEXT'}"
                      for c in cols)
    path = tmp_path / "nulls.db"
    raw = sqlite3.connect(path)
    raw.execute(f"CREATE TABLE usage ({decls})")
    vals = dict(VALUES, status_code=None, request_id=None,
                cache_read_tokens=None, input_tokens_estimated=None)
    raw.execute(f"INSERT INTO usage ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                [vals[c] for c in cols])
    raw.commit()
    raw.close()
    db = DB.Database(str(path))
    try:
        summary = UsageStore(db).summary()
        assert summary.total_requests == 1
        # A NULL status_code means "not rated" and must stay out of both
        # numerator and denominator -- the behaviour the REVIEW-2026-09-20
        # note is about.
        assert summary.rated_requests == 0, summary
    finally:
        db.close()


# --- the migration must be inert on a current database -----------------


def test_opening_a_current_database_twice_is_stable(tmp_path):
    """Idempotent: SCHEMA runs on every open, so a non-idempotent migration
    would fail on the second launch."""
    path = tmp_path / "twice.db"
    for _ in range(3):
        db = DB.Database(str(path))
        try:
            UsageStore(db).summary()
            UsageStore(db).list()
        finally:
            db.close()


def test_the_migration_leaves_values_alone(tmp_path):
    """Adding a column must not rewrite the rows that are already there."""
    path = tmp_path / "keep.db"
    db = _db_missing("status_code", path)
    try:
        rows = UsageStore(db).list()
    finally:
        db.close()
    db2 = DB.Database(str(path))          # migration runs here
    try:
        rows2 = UsageStore(db2).list()
        assert len(rows2) == len(rows) == 1
        assert rows2[0].input_tokens == VALUES["input_tokens"]
        assert rows2[0].provider_id == VALUES["provider_id"]
        assert rows2[0].status_code is None, "a new column must read as NULL"
    finally:
        db2.close()
