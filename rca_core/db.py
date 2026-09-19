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


class DatabaseClosedError(sqlite3.ProgrammingError):
    """Raised when a :class:`Database` is used after ``close()``.

    REVIEW-2026-09-20 (finding 6): closing a Database sets ``_conn`` to
    ``None``, so the next query surfaced as a bare
    ``AttributeError: 'NoneType' object has no attribute 'execute'`` — no
    hint that the handle was closed, and nothing in the app's
    ``except sqlite3.Error`` handlers caught it (the History / Usage pages
    therefore crashed instead of showing the storage error).

    It subclasses ``sqlite3.ProgrammingError`` — the exact type SQLite itself
    raises for "Cannot operate on a closed database" — so every existing
    ``except sqlite3.DatabaseError`` / ``sqlite3.Error`` / ``Exception``
    handler keeps working; only the message becomes explicit.
    """


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
    notes TEXT DEFAULT '',
    -- P1-5 (REVIEW-2026-07-25): provenance fields for edit tracking
    last_edited_at TEXT,
    last_editor TEXT,
    edit_count INTEGER DEFAULT 0,
    edit_provenance TEXT,
    -- P1-1 (REVIEW-2026-07-27): mandatory image fingerprint for 5-year audits
    image_sha256 TEXT,
    -- P1-2 (REVIEW-2026-07-27): per-request metadata (model, max_tokens, etc.)
    request_meta TEXT
);

-- P2 (REVIEW-2026-07-27): raw_responses — full LLM responses per run, no truncation
CREATE TABLE IF NOT EXISTS raw_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL,
    run_idx INTEGER NOT NULL DEFAULT 0,
    raw_text TEXT,
    prompt_text TEXT,
    request_meta TEXT,
    timestamp INTEGER,
    FOREIGN KEY (record_id) REFERENCES history(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_raw_responses_record ON raw_responses(record_id, run_idx);

-- P3 (REVIEW-2026-07-27): record_edits — immutable audit trail of all edits
CREATE TABLE IF NOT EXISTS record_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,
    editor TEXT,
    edit_type TEXT NOT NULL,
    row_idx INTEGER,
    col_name TEXT,
    -- REVIEW-2026-09-20 (finding 8): these two were declared ``JSON``.
    -- JSON affinity keeps the value's own storage class, so a bound TEXT
    -- that parses as a JSON scalar is stored AS that scalar (verified on
    -- SQLite 3.45: ``typeof(before)`` returned ``integer`` for
    -- ``json.dumps(12) == "12"`` and ``real`` for ``"12.5"``). The audit row
    -- for an edit of a numeric bed / token / age value therefore came back
    -- as an int/float, ``json.loads()`` raised TypeError on it and the
    -- history detail dialog displayed "no previous value" — silently losing
    -- the audited datum. TEXT stores exactly what was written. Old
    -- databases keep their ``JSON`` columns (SQLite cannot retype a column);
    -- ``history.HistoryStore.get_edits`` accepts both shapes, so no migration
    -- is needed and the column names / order stay unchanged.
    before TEXT,
    after TEXT,
    FOREIGN KEY (record_id) REFERENCES history(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_record_edits_record ON record_edits(record_id, timestamp);
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
        # REVIEW-2026-09-10: dirname(":memory:") and dirname("rel.db") are "",
        # and makedirs("") raises FileNotFoundError — so the SQLite-idiomatic
        # ":memory:" path was unusable, as was any relative path. `path` is a
        # documented public parameter (and Database is re-exported from
        # rca_core), so this affected callers outside the app too.
        _parent = os.path.dirname(self.path)
        if _parent:
            os.makedirs(_parent, exist_ok=True)
        self._lock = threading.RLock()
        # REVIEW-2026-09-20 (finding 6/7): explicit lifecycle + transaction
        # nesting state. ``_closed`` turns the post-close AttributeError into
        # a named sqlite3 error; ``_tx_depth`` / ``_tx_owner`` let
        # ``transaction()`` be re-entered by the thread that already holds it
        # instead of dying on "cannot start a transaction within a
        # transaction".
        self._closed = False
        self._tx_depth = 0
        self._tx_owner: int | None = None
        # check_same_thread=False so any thread may borrow the connection.
        # Combined with WAL, multiple readers don't block each other and
        # the GIL keeps single-statement executes atomic. Multi-statement
        # transactions must be wrapped in ``self.transaction()``.
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_setup()
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
            # LOW finding 2026-07-20: every reopen path must re-apply
            # PRAGMAs and re-run SCHEMA / migrations so the recovered
            # connection behaves identically to the first open (WAL for
            # concurrent readers, FK enforcement on, app tables visible).
            # Without this, a fallback reopen ran with default journal
            # mode and FK off — directly undermining the Bug-5 fix the
            # same file introduced.
            self._apply_setup()

    def _apply_setup(self) -> None:
        """Idempotent connection setup: pragmas + schema + migrations.

        Called both on initial open and on every recovery reopen so the
        reopened connection is indistinguishable from the first one.
        Idempotency is preserved by SQLite (PRAGMA journal_mode=WAL on a
        WAL file is a no-op; CREATE TABLE IF NOT EXISTS / executescript
        are no-ops on existing tables).
        """
        # WAL gives concurrent readers + a single writer without locking
        # the whole file. safe even on Windows for our access pattern.
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
        except sqlite3.DatabaseError:
            pass
        # FK enforcement is per-connection and defaults off, so it must
        # be re-issued on every (re)open.
        self._conn.execute("PRAGMA foreign_keys=ON;")
        # Schema + migrations run under the lock. They issue their own
        # transactions via executescript(), so they must NOT run inside the
        # explicit BEGIN IMMEDIATE opened by transaction() — nesting
        # executescript() there auto-commits the outer transaction and breaks
        # atomicity (the SCHEMA DDL would commit before the migrations run).
        # We hold only the lock; executescript() manages its own commit.
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._apply_migrations(self._conn)
            # P1-5 (REVIEW-2026-07-25): add edit-provenance columns to
            # pre-existing databases that predate this schema. Idempotent
            # — silent no-op if columns already exist.
            self._add_provenance_columns(self._conn)
            self._conn.commit()

    def _add_provenance_columns(self, conn: sqlite3.Connection) -> None:
        """Add edit_provenance columns to existing history table.

        SQLite has no IF NOT EXISTS for ALTER TABLE ADD COLUMN in older
        versions; we work around by inspecting pragma_table_info.
        """
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(history)").fetchall()
        }
        additions = []
        for col, decl in (
            ("last_edited_at", "TEXT"),
            ("last_editor", "TEXT"),
            ("edit_provenance", "TEXT"),
            ("image_sha256", "TEXT"),
            ("request_meta", "TEXT"),
        ):
            if col not in existing:
                additions.append(f"ALTER TABLE history ADD COLUMN {col} {decl}")
        if "edit_count" not in existing:
            additions.append(
                "ALTER TABLE history ADD COLUMN edit_count INTEGER DEFAULT 0"
            )
        for stmt in additions:
            try:
                conn.execute(stmt)
            except sqlite3.DatabaseError:
                # Defensive: pragma above should have caught this; if not,
                # the user can re-create by renaming the DB.
                pass

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            # __init__ failed before the lock existed (bad path, unreadable
            # file): __del__ must still be able to call us quietly.
            self._conn = None
            self._closed = True
            return
        with lock:
            self._closed = True
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        """Return the live connection or raise :class:`DatabaseClosedError`.

        REVIEW-2026-09-20 (finding 6): a long-lived ``Database`` shared with a
        HistoryStore/UsageStore can outlive its owner (window closed, worker
        finishing late). Every access used to dereference ``self._conn``
        unconditionally and raise
        ``AttributeError: 'NoneType' object has no attribute 'execute'``.
        """
        conn = getattr(self, "_conn", None)
        if conn is None or self._closed:
            raise DatabaseClosedError(
                f"Database handle for {getattr(self, 'path', '?')!r} is closed; "
                "create a new Database() before running statements."
            )
        return conn

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

        Raises ``DatabaseClosedError`` when the handle has been closed.
        """
        with self._lock:
            yield self._require_conn()

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

        REVIEW-2026-09-20 (finding 7): nesting is now supported. A helper
        that opens its own ``transaction()`` while an outer one is already
        open used to die on ``sqlite3.OperationalError: cannot start a
        transaction within a transaction`` — and, worse, its ``commit()``
        flushed the OUTER block half way through. A depth counter tracks the
        re-entry (the RLock guarantees only the owning thread can be inside),
        so only the outermost frame issues BEGIN / COMMIT / ROLLBACK; inner
        frames simply join the running transaction.

        Note on ``execute()`` inside a transaction (finding 7, behaviour kept
        on purpose): ``execute()`` / ``executemany()`` commit immediately
        after their statement, which ENDS the enclosing ``BEGIN IMMEDIATE``
        block. Callers that must stay atomic have to use ``run()`` /
        ``query()`` (see the ``REVIEW-2026-07-31`` notes in history.py).
        Converting ``execute()`` into a no-commit-inside-a-transaction today
        would change the semantics of every existing call site, so it is
        documented rather than changed.
        """
        with self._lock:
            conn = self._require_conn()
            outermost = self._tx_depth == 0
            if outermost:
                self._tx_owner = threading.get_ident()
                conn.execute("BEGIN IMMEDIATE")
            else:
                # Same thread (the RLock is reentrant and held by it): join
                # the outer transaction instead of starting a second one.
                if self._tx_owner != threading.get_ident():  # pragma: no cover
                    raise DatabaseClosedError(
                        "transaction() held by another thread — unreachable "
                        "while the write lock is exclusive; please report."
                    )
            self._tx_depth += 1
            try:
                yield conn
                if outermost:
                    conn.commit()
            except Exception:
                if outermost:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise
            finally:
                self._tx_depth -= 1
                if outermost:
                    self._tx_owner = None

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Run one statement and COMMIT immediately.

        WARNING (finding 7): inside ``transaction()`` this commits the outer
        transaction after a single statement. Use ``run()`` there.
        """
        with self._lock:
            conn = self._require_conn()
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute WITHOUT committing — for use inside ``transaction()``.

        REVIEW-2026-07-31: ``execute()`` auto-commits, which silently ends
        an enclosing ``BEGIN IMMEDIATE ... COMMIT`` block and destroys its
        atomicity — a later failure could not roll the earlier statements
        back. Multi-statement writes inside ``transaction()`` must use
        ``run()``; the outer context manager commits (or rolls back) the
        whole block.
        """
        with self._lock:
            return self._require_conn().execute(sql, params)

    def executemany(self, sql: str, params_list: list[Any]) -> sqlite3.Cursor:
        with self._lock:
            conn = self._require_conn()
            cur = conn.executemany(sql, params_list)
            conn.commit()
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
            cur = self._require_conn().execute(sql, params)
            return list(cur)

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._require_conn().execute(sql, params)
            return cur.fetchone()


# P2/P3 migrations (REVIEW-2026-07-27): add raw_responses and record_edits tables.
# Note: image_sha256 and request_meta columns are added by _add_provenance_columns
# (called before _apply_migrations in _apply_setup), so we don't ADD them here.
Database._MIGRATIONS.append((
    0, 1,
    """
    -- Migration (0,1) previously recreated raw_responses / record_edits here.
    -- Those tables are now part of SCHEMA (created unconditionally with
    -- IF NOT EXISTS), so recreating them was redundant. Kept only as a
    -- no-op version bump for legacy databases that predate this schema.
    """
))

# Bug-14 fix: auto-derive the target schema version from the migration
# list. Empty list → 0 (initial SCHEMA above is the baseline). Each
# appended migration bumps the target by 1.
Database._CURRENT_SCHEMA_VERSION = len(Database._MIGRATIONS)
