"""Result cache for VLM extraction calls — idempotent rerun, zero LLM cost.

Stores raw (provider, model, image_b64, prompt_version, ...) → raw model
output in a SQLite database under the user's config dir
(~/.range_chart_analyzer/extract_cache.sqlite). On a cache hit the server
returns the stored result without calling the LLM, making reruns and
parameter tweaks near-instant.

Cache key = sha256 of the normalized request fields so identical inputs
collide regardless of key order. PROMPT_VERSION (from prompt.py) is part of
the key, so upgrading the prompt invalidates stale entries. The store is
LRU-bounded to the most recent N entries (default 200).

Never stores credentials — only the request shape and output.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time

# Keep consistent with the project's data dir.
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
_DB_PATH = os.path.join(_CACHE_DIR, "extract_cache.sqlite")

# Number of recent entries retained; older ones are evicted on write.
DEFAULT_MAX_ENTRIES = 200

# Schema version — bump on breaking change and the table is recreated.
_SCHEMA_VERSION = 1


def _ensure_dir() -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)


def _rowid_for(conn: sqlite3.Connection, key: str) -> int | None:
    cur = conn.execute("SELECT id FROM extract_cache WHERE k = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


class ResultCache:
    """Thread-safe SQLite-backed LRU cache of VLM extraction results.

    Usage::

        cache = ResultCache()
        hit = cache.get(key_dict)
        if hit is None:
            hit = call_llm(...)
            cache.put(key_dict, hit)
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES,
                 db_path: str = _DB_PATH):
        self._max = max_entries
        self._path = db_path
        self._lock = threading.RLock()
        _ensure_dir()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_table()

    def _create_table(self) -> None:
        with self._lock:
            # Schema-version guard.
            cur = self._conn.execute(
                "PRAGMA user_version")
            ver = cur.fetchone()[0]
            if ver < _SCHEMA_VERSION:
                self._conn.execute("DROP TABLE IF EXISTS extract_cache")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS extract_cache (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    k     TEXT NOT NULL UNIQUE,
                    v     NOT NULL,
                    ts    REAL NOT NULL
                )
                """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_ts ON extract_cache(ts)")
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._conn.commit()

    @staticmethod
    def make_key(**fields: object) -> str:
        """Build a stable cache key from request fields.

        Field dict is sorted and JSON-serialized with sorted_keys so the
        same logical request always produces the same key regardless of
        insertion order. ``image_b64`` may be large; it is hashed first to
        keep the key small.
        """
        normalized = {}
        for k_ in sorted(fields.keys()):
            v_ = fields[k_]
            # Hash large binary-ish strings (images).
            if k_ == "image_b64" and isinstance(v_, str) and len(v_) > 64:
                normalized[k_] = hashlib.sha256(v_.encode("utf-8")).hexdigest()
            else:
                normalized[k_] = v_
        blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                          default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict | None:
        """Return the cached result dict, or None on miss."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT v FROM extract_cache WHERE k = ?", (key,))
            row = cur.fetchone()
            if row is None:
                return None
            # Update timestamp (LRU touch).
            self._conn.execute(
                "UPDATE extract_cache SET ts = ? WHERE k = ?",
                (time.time(), key))
            self._conn.commit()
            try:
                return json.loads(row[0])
            except Exception:
                return None

    def put(self, key: str, value: dict) -> None:
        """Insert or replace a cached result, then evict to the LRU bound."""
        with self._lock:
            ts = time.time()
            blob = json.dumps(value, ensure_ascii=False, default=str)
            existing = _rowid_for(self._conn, key)
            if existing is None:
                self._conn.execute(
                    "INSERT INTO extract_cache (k, v, ts) VALUES (?, ?, ?)",
                    (key, blob, ts))
            else:
                self._conn.execute(
                    "UPDATE extract_cache SET v = ?, ts = ? WHERE k = ?",
                    (blob, ts, key))
            self._evict()
            self._conn.commit()

    def _evict(self) -> None:
        """Drop oldest entries beyond the LRU bound."""
        self._conn.execute(
            """
            DELETE FROM extract_cache
            WHERE id NOT IN (
                SELECT id FROM extract_cache ORDER BY ts DESC LIMIT ?
            )
            """, (self._max,))

    def clear(self) -> int:
        """Remove all entries; returns the count deleted."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM extract_cache")
            self._conn.commit()
            return cur.rowcount or 0

    def size(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM extract_cache")
            return cur.fetchone()[0]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "ResultCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# Module-level singleton for the server to import.
_singleton: ResultCache | None = None
_singleton_lock = threading.Lock()


def get_cache() -> ResultCache:
    """Return the process-wide cache singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ResultCache()
        return _singleton
