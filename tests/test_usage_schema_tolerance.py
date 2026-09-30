r"""A usage database from an older build could not be read at all.

usage._row_to_record read all 19 columns with row["name"], and the usage table
is created with CREATE TABLE IF NOT EXISTS and is never migrated. Measured:

  * dropping ANY ONE of the 19 columns makes the reader raise
    IndexError: No item with that key
  * Database() does not add the missing column back (18 of 19 survive an open
    of a database that lacked status_code)
  * rca_core.history._row_to_record SURVIVES the equivalent situation, because
    it guards with "image_sha256" in row.keys()

So a user who ran a build before a column existed, then upgraded, has a
database the usage screen cannot read at all -- while the history screen on the
SAME file works. And the reachability is stated in the module's own comment:
"rows written before status tracking existed ... carry status_code NULL", i.e.
the column did not always exist. The cache_read / cache_creation token columns,
both *_estimated flags, total_cost_usd, first_token_ms and request_id read as
later additions for the same reason.

The fix is the tolerance history already uses, applied to the table that does
not have a migration. Deliberately NOT a schema migration: adding ALTER TABLE
for usage would be a second mechanism for a problem the reader can solve, and
db.py's own comment notes SQLite has no IF NOT EXISTS for ADD COLUMN.

Behaviour for a database that has every column is unchanged -- the tests below
assert that separately, because "now it tolerates missing columns" and "now it
reads the same values" are different claims.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import usage as U  # noqa: E402

ALL_COLS = [
    "id", "timestamp", "provider_id", "provider_name", "model", "endpoint",
    "mode", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "input_tokens_estimated",
    "output_tokens_estimated", "total_cost_usd", "latency_ms",
    "first_token_ms", "status_code", "error_message", "request_id",
]

# Columns whose absence the module's own comments attribute to a later change.
LATER_ADDITIONS = [
    "status_code", "first_token_ms", "request_id",
    "cache_read_tokens", "cache_creation_tokens",
    "input_tokens_estimated", "output_tokens_estimated",
    "total_cost_usd",
]

VALUES = {
    "id": 7, "timestamp": 1000.0, "provider_id": "p1",
    "provider_name": "Provider One", "model": "m1", "endpoint": "/v1/x",
    "mode": "abundance", "input_tokens": 10, "output_tokens": 20,
    "cache_read_tokens": 5, "cache_creation_tokens": 1,
    "input_tokens_estimated": 1, "output_tokens_estimated": 0,
    "total_cost_usd": 0.0125, "latency_ms": 300, "first_token_ms": 120,
    "status_code": 200, "error_message": "", "request_id": "req-1",
}

INT_COLS = {"id", "timestamp", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens",
            "input_tokens_estimated", "output_tokens_estimated",
            "total_cost_usd", "latency_ms", "first_token_ms", "status_code"}


def _row_with(cols):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    decls = ", ".join(f"{c} {'INTEGER' if c in INT_COLS else 'TEXT'}"
                      for c in cols)
    conn.execute(f"CREATE TABLE usage ({decls})")
    conn.execute(f"INSERT INTO usage ({', '.join(cols)}) "
                 f"VALUES ({', '.join('?' * len(cols))})",
                 [VALUES[c] for c in cols])
    row = conn.execute("SELECT * FROM usage").fetchone()
    return conn, row


# --- the full-schema case: nothing changed ------------------------------


def test_a_complete_row_still_reads_exactly_the_same_values():
    _conn, row = _row_with(ALL_COLS)
    try:
        rec = U._row_to_record(row)
    finally:
        _conn.close()
    assert rec.id == 7
    assert rec.mode == "abundance"
    assert rec.input_tokens == 10
    assert rec.output_tokens == 20
    assert rec.cache_read_tokens == 5
    assert rec.input_tokens_estimated is True
    assert rec.output_tokens_estimated is False
    assert rec.total_cost_usd == pytest.approx(0.0125)
    assert rec.latency_ms == 300
    assert rec.first_token_ms == 120
    assert rec.status_code == 200
    assert rec.request_id == "req-1"
    assert rec.error_message == ""


# --- the defect: one missing column must not be fatal -------------------


@pytest.mark.parametrize("dropped", LATER_ADDITIONS)
def test_a_later_added_column_may_be_absent(dropped):
    cols = [c for c in ALL_COLS if c != dropped]
    conn, row = _row_with(cols)
    try:
        rec = U._row_to_record(row)
    finally:
        conn.close()
    assert rec.id == 7, f"losing {dropped} lost the row's identity"


# id and timestamp are PRIMARY KEY / NOT NULL: a usage row cannot lack them,
# and inventing an id or a time would fabricate the record's identity, which
# is worse than raising. So the tolerance stops at everything else.
REQUIRED = {"id", "timestamp"}
TOLERANT_COLS = [c for c in ALL_COLS if c not in REQUIRED]


@pytest.mark.parametrize("dropped", TOLERANT_COLS)
def test_no_tolerated_column_raises_when_absent(dropped):
    """General form, so a column added tomorrow fails on its own rather than
    only the ones this commit happens to know about."""
    cols = [c for c in ALL_COLS if c != dropped]
    conn, row = _row_with(cols)
    try:
        U._row_to_record(row)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"dropping {dropped!r} raised {type(exc).__name__}: {exc}")
    finally:
        conn.close()


@pytest.mark.parametrize("dropped", sorted(REQUIRED))
def test_identity_columns_still_have_to_exist(dropped):
    """The deliberate limit of the tolerance.

    A default here would be a made-up record id or a made-up timestamp, and
    usage rows are what the cost dashboard and the history audit are built
    from. Refusing to read a row that cannot identify itself is the right
    failure, and saying so in a test stops a future "let me just default it"
    from looking harmless.
    """
    cols = [c for c in ALL_COLS if c != dropped]
    conn, row = _row_with(cols)
    try:
        with pytest.raises(IndexError):
            U._row_to_record(row)
    finally:
        conn.close()


def test_several_columns_missing_at_once():
    """An old database is missing all of them, not one."""
    cols = [c for c in ALL_COLS if c not in LATER_ADDITIONS]
    conn, row = _row_with(cols)
    try:
        rec = U._row_to_record(row)
    finally:
        conn.close()
    # Absent token columns read as zero, absent status as None -- which is what
    # UsageSummary.rated_requests already means by "status not known".
    assert rec.status_code is None
    assert rec.cache_read_tokens == 0
    assert rec.request_id == ""
    assert rec.mode == "abundance"
    assert rec.id == 7


def test_null_values_take_the_same_path_as_missing_ones():
    """A present-but-NULL column and an absent column must agree, or the
    legacy handling in the summary (status NULL means "not rated") would
    behave differently depending on the build that wrote the row."""
    cols = list(ALL_COLS)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    decls = ", ".join(f"{c} {'INTEGER' if c in INT_COLS else 'TEXT'}"
                      for c in cols)
    conn.execute(f"CREATE TABLE usage ({decls})")
    nulls = dict(VALUES, status_code=None, request_id=None,
                 first_token_ms=None, total_cost_usd=None)
    conn.execute(f"INSERT INTO usage ({', '.join(cols)}) "
                 f"VALUES ({', '.join('?' * len(cols))})",
                 [nulls[c] for c in cols])
    null_row = conn.execute("SELECT * FROM usage").fetchone()
    conn.close()

    _c2, absent_row = _row_with([c for c in cols if c not in (
        "status_code", "request_id", "first_token_ms", "total_cost_usd")])
    null_rec = U._row_to_record(null_row)
    absent_rec = U._row_to_record(absent_row)
    for field in ("status_code", "request_id", "first_token_ms",
                  "total_cost_usd"):
        assert getattr(null_rec, field) == getattr(absent_rec, field), field


# --- the asymmetry with history, which is the point --------------------


def test_history_reader_was_never_the_one_that_crashed():
    """Stated so the reason this commit exists stays legible: history has BOTH
    a migration and a tolerant reader, usage had neither."""
    from rca_core import history as H  # noqa: F401

    hcols = [
        "id", "timestamp", "source_file", "image_thumbnail", "image_width",
        "image_height", "provider_id", "provider_name", "model", "mode",
        "runs", "result_json", "raw_json", "confidence", "partial_failures",
        "duration_ms", "status_code", "notes",
    ]
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE history (id INTEGER, timestamp REAL, source_file TEXT,"
        " image_thumbnail BLOB, image_width INTEGER, image_height INTEGER,"
        " provider_id TEXT, provider_name TEXT, model TEXT, mode TEXT,"
        " runs INTEGER, result_json TEXT, raw_json TEXT, confidence REAL,"
        " partial_failures INTEGER, duration_ms INTEGER, status_code INTEGER,"
        " notes TEXT)")
    conn.execute("INSERT INTO history VALUES "
                 "(1, 1000.0, 'f.png', NULL, 10, 10, 'p', 'P', 'm',"
                 " 'range_chart', 1, '{}', '', 0.5, 0, 10, 200, '')")
    row = conn.execute("SELECT * FROM history").fetchone()
    try:
        rec = H._row_to_record(row)
    finally:
        conn.close()
    assert rec.image_sha256 == ""
    assert rec.request_meta == {}
