"""P1-8 (2026-11): downgrade safety and corrupt-file preservation.

Two real defects, both with data-loss potential:

1. ``_apply_migrations`` ended with an unconditional
   ``INSERT OR REPLACE INTO _schema_version``. An OLD build opening a database
   written by a NEWER build therefore lowered the on-disk version. On the
   next upgrade no migration's ``from_v`` matched, so the migration chain was
   silently skipped while the file already carried the new columns. The
   comment claiming "downgrades remain a no-op" was false.
2. The health-probe fallback was a single close + reopen + ``_apply_setup``.
   On a genuinely corrupt file ``_apply_setup()`` raised BEFORE the probe
   (measured: ``SELECT 1`` returns (1,) from 4096 random bytes, while
   ``executescript(SCHEMA)`` raises "file is not a database"), so the
   exception escaped ``__init__`` and the same bad file was retried on every
   launch. There was no attempt to preserve the user's bytes.

Every case uses ``tempfile.TemporaryDirectory()`` and calls ``db.close()``
so Windows releases the SQLite file lock before the directory is removed.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core import db as DB  # noqa: E402
from rca_core import diagnostics  # noqa: E402


def _disk_version(path: str) -> int:
    """Read the version row WITHOUT going through Database (which migrates)."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return int(conn.execute(
            "SELECT version FROM _schema_version LIMIT 1").fetchone()[0])
    finally:
        conn.close()


def _make_full_db_with_version(path: str, version: int) -> None:
    """A realistic 'written by another build' database: full SCHEMA + version."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DB.SCHEMA)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _schema_version "
            "(version INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _schema_version")
        conn.execute("INSERT INTO _schema_version(version) VALUES (?)",
                     (version,))
        conn.execute(
            "INSERT INTO history (timestamp, result_json) VALUES (?, ?)",
            (1_700_000_000.0, '{"species_ranges": []}'))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. Downgrade
# ---------------------------------------------------------------------------
def test_downgrade_keeps_the_higher_version_and_warns():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "rca.db")
        newer = int(DB.Database._CURRENT_SCHEMA_VERSION) + 3
        _make_full_db_with_version(path, newer)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            db = DB.Database(path=path)
        try:
            # The version row must NOT have been lowered.
            assert _disk_version(path) == newer
            assert db.schema_warning, "a downgrade must be reported, not silent"
            assert "v%d" % newer in db.schema_warning
            assert any(issubclass(w.category, RuntimeWarning)
                       for w in caught), "a RuntimeWarning must be emitted"
            # Reads still work on the old build.
            rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
            assert "history" in {r["name"] for r in rows}
            assert db.query_one("SELECT COUNT(*) AS n FROM history")["n"] == 1
        finally:
            db.close()

        # ... and it stays that way across a second open (no delayed rewrite).
        db2 = DB.Database(path=path)
        try:
            assert _disk_version(path) == newer
            assert db2.schema_warning
        finally:
            db2.close()


def test_fresh_db_has_current_version_and_no_warning():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "rca.db")
        db = DB.Database(path=path)
        try:
            assert _disk_version(path) == DB.Database._CURRENT_SCHEMA_VERSION
            assert db.schema_warning is None
            assert db.recovery_note is None
            assert db.query_one("SELECT 1 AS n")["n"] == 1
        finally:
            db.close()


def test_older_db_still_migrates_to_current_version():
    """The downgrade guard must not accidentally disable forward migration.

    NOTE on ``MAX(version)``: ``_apply_migrations`` ends with
    ``INSERT OR REPLACE INTO _schema_version(version) VALUES (?)``. On a
    database that already stored a DIFFERENT version number this inserts a
    SECOND row rather than replacing the first (the primary key differs), so
    ``LIMIT 1`` can still return the old value. That pre-existing quirk is
    deliberately NOT changed here — this wave's contract is "do not alter
    existing migration semantics". What matters is that the forward
    migration still ran and recorded the current target.
    """
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "rca.db")
        _make_full_db_with_version(path, 0)
        db = DB.Database(path=path)
        try:
            assert db.schema_warning is None, "an older DB is not a downgrade"
            assert int(db.query_one(
                "SELECT MAX(version) AS v FROM _schema_version")["v"]) == (
                    DB.Database._CURRENT_SCHEMA_VERSION)
            assert db.query_one("SELECT 1 AS n")["n"] == 1
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 2. Corruption
# ---------------------------------------------------------------------------
def test_corrupt_db_is_preserved_and_replaced_with_a_usable_empty_db():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "rca.db")
        original = os.urandom(4096)
        with open(path, "wb") as f:
            f.write(original)

        db = DB.Database(path=path)
        try:
            preserved = sorted(Path(td).glob("rca.db.corrupt-*"))
            assert preserved, "the corrupt file must be preserved, not deleted"
            assert len(preserved) == 1
            assert preserved[0].read_bytes() == original, (
                "the preserved file must contain the ORIGINAL bytes")
            assert db.recovery_note, "recovery must be explained to the caller"
            assert str(preserved[0]) in db.recovery_note
            assert "备份" in db.recovery_note
            # A fresh, working database now lives at the original path.
            assert os.path.isfile(path)
            assert Path(path).read_bytes() != original
            assert db.query_one("SELECT 1 AS n")["n"] == 1
            tables = {r["name"] for r in db.query(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"history", "usage", "_schema_version"} <= tables
            assert _live_version(db) == (
                DB.Database._CURRENT_SCHEMA_VERSION)
        finally:
            db.close()


def _live_version(db) -> int:
    """Read the version through the live connection (no extra file open)."""
    return int(db.query_one(
        "SELECT version FROM _schema_version LIMIT 1")["version"])


def test_a_schema_error_is_not_mistaken_for_a_corrupt_file():
    """Regression: an old-shape DB with a missing indexed column must RAISE.

    ``SCHEMA`` creates ``idx_history_ts ON history(timestamp DESC)``. A
    hand-made history table without ``timestamp`` makes that DDL raise
    ``sqlite3.OperationalError``. Treating any DatabaseError as corruption
    would rename the (perfectly valid) file and silently replace the user's
    history, so the original exception must propagate untouched and no
    ``.corrupt-*`` file may appear.
    """
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "rca.db")
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, "
                         "result_json TEXT)")
            conn.execute("INSERT INTO history (result_json) VALUES ('{}')")
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(sqlite3.OperationalError):
            DB.Database(path=path)

        assert not list(Path(td).glob("rca.db.corrupt-*")), (
            "a schema mismatch must never trigger file preservation")
        # The PRAGMAs/DDL that ran before the failing index already touched
        # the file header (this was true BEFORE this change too), so assert
        # on the DATA, not on byte equality: the user's row must survive.
        conn = sqlite3.connect(path)
        try:
            assert conn.execute(
                "SELECT result_json FROM history").fetchone()[0] == "{}"
        finally:
            conn.close()


def test_memory_db_never_renames_and_never_errors():
    with tempfile.TemporaryDirectory() as td:
        before = set(os.listdir(td))
        db = DB.Database(path=":memory:")
        try:
            assert db.schema_warning is None
            assert db.recovery_note is None
            assert db.query_one("SELECT 1 AS n")["n"] == 1
            # Force the recovery branch explicitly: it must be a no-op for
            # ":memory:" rather than trying to rename a path that has no file.
            db._quarantine_corrupt_db()
            assert db.query_one("SELECT 2 AS n")["n"] == 2
            assert db.recovery_note is None
        finally:
            db.close()
        assert set(os.listdir(td)) == before, (
            "Database(':memory:') must not create or rename any file")


# ---------------------------------------------------------------------------
# 3. doctor sees the downgrade WITHOUT writing anything
# ---------------------------------------------------------------------------
def test_doctor_reports_a_newer_schema_and_stays_read_only(tmp_path, monkeypatch):
    data = tmp_path / ".range_chart_analyzer"
    data.mkdir()
    path = data / "rca.db"
    newer = int(DB.Database._CURRENT_SCHEMA_VERSION) + 2
    _make_full_db_with_version(str(path), newer)
    before_bytes = path.read_bytes()
    before_files = sorted(p.name for p in data.iterdir())

    monkeypatch.setattr(diagnostics, "_data_dir", lambda: data)
    buf = io.StringIO()
    rc = diagnostics.run_doctor(stream=buf)
    text = buf.getvalue()

    assert rc == 1
    assert "降级" in text or "v%d" % newer in text
    assert "rca.db" in text
    # read-only: no migration, no version rewrite, no new files
    assert path.read_bytes() == before_bytes
    assert _disk_version(str(path)) == newer
    assert sorted(p.name for p in data.iterdir()) == before_files


def test_doctor_ignores_a_legacy_db_without_a_version_table(tmp_path, monkeypatch):
    """A pre-versioning database is NOT a downgrade; doctor must not warn."""
    data = tmp_path / ".range_chart_analyzer"
    data.mkdir()
    path = data / "rca.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(DB.SCHEMA)
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(diagnostics, "_data_dir", lambda: data)
    buf = io.StringIO()
    diagnostics.run_doctor(stream=buf)
    assert "降级运行" not in buf.getvalue()
    assert not (data / "_schema_version").exists()
