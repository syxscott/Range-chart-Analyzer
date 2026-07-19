"""SQLite-backed storage for history and token usage.

Single persistent connection per Database instance (Bug-5 fix). WAL mode
is enabled so the GUI's history-page reader does not block when a
background worker writes a new record, and the reader does not pay the
per-call connect/close overhead (~10 ms each) the previous design did.

Thread-safety: the connection is created with ``check_same_thread=False``
so any thread may borrow it. Reads run in parallel; writes are serialised
by an ``RLock``. The GIL keeps ``cursor.execute`` atomic for one SQL
statement, but transactions with multiple statements still need the lock.

The DB lives at ``~/.range_chart_analyzer/rca.db``. Two tables:

* ``history`` — one row per completed extraction. Holds the normalized
  result as a JSON blob, an image thumbnail, and run metadata so the
  History page can render a preview without re-running anything.
* ``usage`` — one row per API call (across all runs of a single
  extraction). Token counts, latency, status, provider / model used.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator


def default_db_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    return os.path.join(base, "rca.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    source_file TEXT,
    image_thumbnail BLOB,
    image_width INTEGER,
    image_height INTEGER,
    provider_id TEXT,
    provider_name TEXT,
    model TEXT,
    mode TEXT,
    runs INTEGER DEFAULT 1,
    result_json TEXT NOT NULL,
    raw_json TEXT,
    confidence REAL DEFAULT 0,
    partial_failures INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    status_code INTEGER,
    notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_ts ON history(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_history_mode ON history(mode);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    provider_id TEXT,
    provider_name TEXT,
    model TEXT,
    endpoint TEXT,
    mode TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    input_tokens_estimated INTEGER NOT NULL DEFAULT 0,
    output_tokens_estimated INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL,
    latency_ms INTEGER,
    first_token_ms INTEGER,
    status_code INTEGER,
    error_message TEXT,
    request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider_id, timestamp DESC);
"""


class Database:
    """Thin SQLite wrapper. Use one instance per app; pass it to HistoryStore
    / UsageStore to share a single file handle.

    Bug-5 fix: holds a single persistent connection (WAL + check_same_thread
    False). Reads are concurrent; writes serialise through ``self._lock``.
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or default_db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        # check_same_thread=False so any thread may borrow the connection.
        # Combined with WAL, multiple readers don't block each other and
        # the GIL keeps single-statement executes atomic. Multi-statement
        # transactions must be wrapped in ``self.transaction()``.
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL gives concurrent readers + a single writer without locking
        # the whole file. safe even on Windows for our access pattern.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.DatabaseError:
            pass
        self._conn.execute("PRAGMA foreign_keys=ON;")
        # Schema + migrations run inside the lock because they issue
        # multi-statement transactions via executescript().
        with self.transaction() as conn:
            conn.executescript(SCHEMA)
            self._apply_migrations(conn)
        # Run a quick health probe so we fail fast on corrupt files
        # instead of the first real query.
        try:
            self._conn.execute("SELECT 1").fetchone()
        except sqlite3.DatabaseError:
            # Reopen in case the connection was invalidated by an
            # interrupted write on a previous launch.
            self._conn.close()
            self._conn = sqlite3.connect(
                self.path, timeout=30, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __del__(self) -> None:
        # Best-effort cleanup. Don't raise from __del__.
        try:
            self.close()
        except Exception:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Borrow the persistent connection under the lock.

        Bug-5 fix: this used to open a fresh sqlite3.connect() per call,
        which serialised all callers behind the RLock and paid ~10 ms
        of connect/close overhead each time. Now it yields the single
        persistent connection so multiple short reads can share it.

        Use ``transaction()`` instead when you need a multi-statement
        write — this context manager does NOT start a transaction, so
        a writer racing with a reader here may interleave.
        """
        with self._lock:
            yield self._conn

    # -- Schema migrations ------------------------------------------------
    # The on-disk schema is versioned in `_schema_version`. New columns
    # or tables are added by appending a (from_version, to_version, sql)
    # tuple to _MIGRATIONS; opening an old DB runs them in order so a
    # user's existing history / usage records survive an app upgrade.
    #
    # We never delete or rename columns — only additive changes — so
    # downgrades remain a no-op (new columns just become unused).
    #
    # Bug-14 fix: _CURRENT_SCHEMA_VERSION is auto-derived from
    # len(_MIGRATIONS) at import time. New migrations no longer require
    # bumping a hand-edited constant.

    _MIGRATIONS: list[tuple[int, int, str]] = []
    _CURRENT_SCHEMA_VERSION: int = 0  # populated below at import time

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        """Ensure the DB schema is at _CURRENT_SCHEMA_VERSION.

        Creates the _schema_version table on first use, reads the stored
        version, then runs every migration whose `from_version` matches
        the current state. Migrations are atomic per-step (one
        COMMIT per migration) so a partially-upgraded DB can resume on
        next launch.
        """
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version ("
            " version INTEGER PRIMARY KEY"
            ")"
        )
        row = conn.execute(
            "SELECT version FROM _schema_version LIMIT 1"
        ).fetchone()
        current = int(row["version"]) if row else 0
        for from_v, _to_v, sql in self._MIGRATIONS:
            if current == from_v:
                conn.executescript(sql)
                current = from_v + 1
        # Record the final state on disk. INSERT OR REPLACE handles both
        # first-launch (no row) and subsequent launches (idempotent).
        conn.execute(
            "INSERT OR REPLACE INTO _schema_version(version) VALUES (?)",
            (self._CURRENT_SCHEMA_VERSION,),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Borrow the connection inside a write lock + transaction.

        Multi-statement writes MUST go through here so the BEGIN/COMMIT
        pair is held under the lock; otherwise two writers can interleave
        their statements and corrupt the DB.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, params_list: list[Any]) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.executemany(sql, params_list)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        # Acquire the RLock even for reads. On a single shared connection
        # (check_same_thread=False) two threads calling execute() concurrently
        # corrupt cursor state ("Recursive use of cursors" / "cannot start a
        # transaction within a transaction"). WAL only parallelizes readers
        # across SEPARATE connections - with one connection, all access must
        # serialize. The RLock is reentrant so nesting under transaction() is
        # safe.
        with self._lock:
            cur = self._conn.execute(sql, params)
            return list(cur)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()


# Bug-14 fix: auto-derive the target schema version from the migration
# list. Empty list → 0 (initial SCHEMA above is the baseline). Each
# appended migration bumps the target by 1.
Database._CURRENT_SCHEMA_VERSION = len(Database._MIGRATIONS)
