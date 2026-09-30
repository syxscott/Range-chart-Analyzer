"""Token usage tracking + lightweight estimator.

The LLM stack records one ``UsageRecord`` per call (across all runs of
a single extraction). Aggregated ``UsageSummary`` is what the Usage
page renders.

Token sources, in priority order:
  1. The API response's ``usage`` field (Anthropic / OpenAI / Gemini all
     emit one when the gateway forwards it).
  2. Local estimation via ``estimate_tokens()`` — only ~20% error on
     English/CJK/code mixes, and we mark the row as ``*_tokens_estimated``
     so the UI can flag it.

The estimator intentionally uses a pure-Python heuristic rather than
tiktoken so the GUI has zero extra install footprint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .db import Database


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x20000, 0x2A6DF), # CJK Extension B
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """Rough token count for English / CJK / code mix.

    Empirical rules (averaged over GPT-2 / GPT-4 tokenizers):
      - CJK ideograph ≈ 1.0 token
      - Latin word / number ≈ 1 token per 4 chars
      - Whitespace / punctuation ≈ 0.25 token
    Returns at least 1 when ``text`` is non-empty so callers can
    distinguish "empty" from "1 token".
    """
    if not text:
        return 0
    cjk = 0
    latin = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        elif ch.isascii() and ch.isalnum():
            latin += 1
        else:
            other += 1
    n = cjk + latin // 4 + max(1, other) // 4
    return max(1, n) if text else 0


# ---------------------------------------------------------------------------
# Usage parsers — best-effort, return None when nothing is found
# ---------------------------------------------------------------------------

def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def parse_anthropic_usage(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    u = payload.get("usage")
    if not isinstance(u, dict):
        return None
    inp = _int(u.get("input_tokens"))
    out = _int(u.get("output_tokens"))
    if inp == 0 and out == 0:
        return None
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": _int(u.get("cache_read_input_tokens")),
        "cache_creation_tokens": _int(u.get("cache_creation_input_tokens")),
    }


def parse_openai_usage(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    u = payload.get("usage")
    if not isinstance(u, dict):
        return None
    inp = _int(u.get("prompt_tokens"))
    out = _int(u.get("completion_tokens"))
    if inp == 0 and out == 0:
        return None
    # OpenAI's real schema nests cached tokens at
    # usage.prompt_tokens_details.cached_tokens. The previous code read
    # usage.cached_tokens (a non-existent top-level key), so cache hits were
    # always 0 on the official endpoint. Fall back to the top-level forms for
    # OpenAI-compatible proxies that flatten it.
    cache_read = 0
    ptd = u.get("prompt_tokens_details")
    if isinstance(ptd, dict):
        cache_read = _int(ptd.get("cached_tokens"))
    if not cache_read:
        cache_read = _int(u.get("cached_tokens") or u.get("cache_read_input_tokens"))
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": 0,
    }


def parse_gemini_usage(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        return None
    inp = _int(meta.get("promptTokenCount"))
    out = _int(meta.get("candidatesTokenCount"))
    if inp == 0 and out == 0:
        return None
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": _int(meta.get("cachedContentTokenCount")),
        "cache_creation_tokens": 0,
    }


def parse_usage(payload: Any, fmt_hint: str = "") -> dict[str, int] | None:
    """Try every parser; return first hit. ``fmt_hint`` orders the
    candidates so we don't waste time on the wrong schema first."""
    ordered: list
    if fmt_hint == "anthropic":
        ordered = [parse_anthropic_usage, parse_openai_usage, parse_gemini_usage]
    elif fmt_hint == "openai":
        ordered = [parse_openai_usage, parse_anthropic_usage, parse_gemini_usage]
    elif fmt_hint == "gemini":
        ordered = [parse_gemini_usage, parse_openai_usage, parse_anthropic_usage]
    else:
        ordered = [parse_anthropic_usage, parse_openai_usage, parse_gemini_usage]
    for fn in ordered:
        try:
            r = fn(payload)
        except Exception:
            continue
        if r:
            return r
    return None


def usage_from_text(text: str, *, side: str) -> dict[str, int]:
    """Estimate usage when no usage block is available. ``side`` is 'in' or
    'out'. Callers must set ``input_tokens_estimated`` or
    ``output_tokens_estimated`` on the UsageRecord themselves."""
    n = estimate_tokens(text)
    return {
        "input_tokens": n if side == "in" else 0,
        "output_tokens": n if side == "out" else 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Record + store
# ---------------------------------------------------------------------------

@dataclass
class UsageRecord:
    id: int = 0
    timestamp: float = 0.0
    provider_id: str = ""
    provider_name: str = ""
    model: str = ""
    endpoint: str = ""
    mode: str = "range_chart"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    input_tokens_estimated: bool = False
    output_tokens_estimated: bool = False
    total_cost_usd: float | None = None
    latency_ms: int = 0
    first_token_ms: int | None = None
    status_code: int | None = None
    error_message: str = ""
    request_id: str = ""


@dataclass
class UsageSummary:
    total_requests: int = 0
    # REVIEW-2026-09-20 (finding 11): requests whose status code is actually
    # known. Rows written before status tracking existed (or by a path that
    # never saw an HTTP response) carry status_code NULL: they are neither a
    # success nor a failure, so counting them in the success-rate denominator
    # silently dragged the rate down — a DB full of legacy rows reported 20 %
    # "success" with zero failures. total_requests stays the true row count
    # for the "requests" card; success_rate is now success_count /
    # rated_requests.
    rated_requests: int = 0
    success_count: int = 0
    success_rate: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    estimated_rows: int = 0
    cache_hit_rate: float = 0.0   # cache_read / (input + cache_creation + cache_read)
    by_provider: list[dict[str, Any]] = field(default_factory=list)
    by_model: list[dict[str, Any]] = field(default_factory=list)
    by_day: list[dict[str, Any]] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0


def _field(row, key, default=None):
    """Read one column, tolerating a column an older build never wrote.

    AUDIT-2026-10-01 [item 9.7]: ``usage`` is created with CREATE TABLE IF NOT
    EXISTS and is never migrated -- db.py has _add_provenance_columns for
    ``history`` and nothing equivalent for ``usage`` -- so a database written
    by an older build keeps whatever schema it had, and ``row["status_code"]``
    raises IndexError instead of returning anything. Measured: dropping any
    one of the 19 columns crashes the reader, and Database() does NOT add the
    missing one back.

    That is reachable, not theoretical: the note on UsageSummary.rated_requests
    says outright that "rows written before status tracking existed ... carry
    status_code NULL", i.e. the column did not always exist. The
    cache_read/cache_creation token columns, the two *_estimated flags,
    total_cost_usd, first_token_ms and request_id read as later additions for
    the same reason.

    rca_core.history._row_to_record already copes, by guarding with
    ``"image_sha256" in row.keys()`` and returning "" / {} -- and history is
    the table that DOES have a migration. This is the same tolerance applied
    to the table that does not.

    The default is returned only when the column is absent or NULL, and the
    caller's existing ``or`` fallbacks are left in place, so behaviour for a
    database that has every column is byte-for-byte unchanged.
    """
    try:
        keys = row.keys()
    except AttributeError:  # a plain tuple/dict row
        keys = row
    if key in keys:
        value = row[key]
        return default if value is None else value
    return default


def _row_to_record(row) -> UsageRecord:
    return UsageRecord(
        id=row["id"],
        timestamp=row["timestamp"],
        provider_id=_field(row, "provider_id") or "",
        provider_name=_field(row, "provider_name") or "",
        model=_field(row, "model") or "",
        endpoint=_field(row, "endpoint") or "",
        mode=_field(row, "mode") or "range_chart",
        input_tokens=_field(row, "input_tokens") or 0,
        output_tokens=_field(row, "output_tokens") or 0,
        cache_read_tokens=_field(row, "cache_read_tokens") or 0,
        cache_creation_tokens=_field(row, "cache_creation_tokens") or 0,
        input_tokens_estimated=bool(_field(row, "input_tokens_estimated")),
        output_tokens_estimated=bool(_field(row, "output_tokens_estimated")),
        total_cost_usd=_field(row, "total_cost_usd"),
        latency_ms=_field(row, "latency_ms") or 0,
        first_token_ms=_field(row, "first_token_ms"),
        status_code=_field(row, "status_code"),
        error_message=_field(row, "error_message") or "",
        request_id=_field(row, "request_id") or "",
    )


class UsageStore:
    """CRUD + aggregation over the usage table."""

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def record(self, rec: UsageRecord) -> int:
        if not rec.timestamp:
            rec.timestamp = time.time()
        cur = self.db.execute(
            """INSERT INTO usage (
                timestamp, provider_id, provider_name, model, endpoint, mode,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                input_tokens_estimated, output_tokens_estimated,
                total_cost_usd, latency_ms, first_token_ms, status_code,
                error_message, request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rec.timestamp, rec.provider_id, rec.provider_name,
                rec.model, rec.endpoint, rec.mode,
                rec.input_tokens, rec.output_tokens,
                rec.cache_read_tokens, rec.cache_creation_tokens,
                int(rec.input_tokens_estimated), int(rec.output_tokens_estimated),
                rec.total_cost_usd, rec.latency_ms, rec.first_token_ms,
                rec.status_code, rec.error_message, rec.request_id,
            ),
        )
        rec.id = int(cur.lastrowid)
        return rec.id

    def list(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        start_ts: float | None = None,
        end_ts: float | None = None,
        provider_id: str | None = None,
    ) -> list[UsageRecord]:
        sql = "SELECT * FROM usage WHERE 1=1"
        params: list[Any] = []
        if start_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND timestamp < ?"
            params.append(end_ts)
        if provider_id:
            sql += " AND provider_id = ?"
            params.append(provider_id)
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [_row_to_record(r) for r in self.db.query(sql, tuple(params))]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM usage")
        return int(row["n"] if row else 0)

    def clear(self) -> int:
        cur = self.db.execute("DELETE FROM usage")
        return cur.rowcount

    def summary(
        self,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> UsageSummary:
        """Aggregate over the time window (or all time)."""
        where = "WHERE 1=1"
        params: list[Any] = []
        if start_ts is not None:
            where += " AND timestamp >= ?"
            params.append(start_ts)
        if end_ts is not None:
            where += " AND timestamp < ?"
            params.append(end_ts)
        params_t = tuple(params)

        row = self.db.query_one(
            f"""SELECT
                COUNT(*) AS n,
                COALESCE(SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), 0) AS ok,
                COALESCE(SUM(CASE WHEN status_code IS NULL THEN 0 ELSE 1 END), 0) AS rated,
                COALESCE(SUM(input_tokens), 0) AS inp,
                COALESCE(SUM(output_tokens), 0) AS outp,
                COALESCE(SUM(cache_read_tokens), 0) AS cr,
                COALESCE(SUM(cache_creation_tokens), 0) AS cc,
                COALESCE(SUM(CASE WHEN input_tokens_estimated = 1 OR output_tokens_estimated = 1 THEN 1 ELSE 0 END), 0) AS est
            FROM usage {where}""",
            params_t,
        )
        s = UsageSummary()
        if row:
            s.total_requests = int(row["n"] or 0)
            s.success_count = int(row["ok"] or 0)
            s.rated_requests = int(row["rated"] or 0)
            # Finding 11: denominator = rows with a known status code only.
            s.success_rate = (s.success_count / s.rated_requests) if s.rated_requests else 0.0
            s.total_input_tokens = int(row["inp"] or 0)
            s.total_output_tokens = int(row["outp"] or 0)
            s.total_cache_read_tokens = int(row["cr"] or 0)
            s.total_cache_creation_tokens = int(row["cc"] or 0)
            s.estimated_rows = int(row["est"] or 0)
            denom = s.total_input_tokens + s.total_cache_creation_tokens + s.total_cache_read_tokens
            s.cache_hit_rate = (s.total_cache_read_tokens / denom) if denom > 0 else 0.0
        s.start_ts = start_ts or 0.0
        s.end_ts = end_ts or 0.0

        for r in self.db.query(
            f"""SELECT provider_id, provider_name,
                COUNT(*) AS n,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tok,
                COALESCE(AVG(latency_ms), 0) AS avg_lat
            FROM usage {where}
            GROUP BY provider_id, provider_name
            ORDER BY tok DESC""",
            params_t,
        ):
            s.by_provider.append({
                "provider_id": r["provider_id"] or "",
                "provider_name": r["provider_name"] or "(unknown)",
                "count": int(r["n"] or 0),
                "tokens": int(r["tok"] or 0),
                "avg_latency_ms": int(r["avg_lat"] or 0),
            })

        for r in self.db.query(
            f"""SELECT model,
                COUNT(*) AS n,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tok
            FROM usage {where}
            GROUP BY model
            ORDER BY tok DESC""",
            params_t,
        ):
            s.by_model.append({
                "model": r["model"] or "(unknown)",
                "count": int(r["n"] or 0),
                "tokens": int(r["tok"] or 0),
            })

        # Aggregate by LOCAL day, per row.
        #
        # AUDIT-2026-10-02 [item 10.1]: this used to group by UTC day in SQL
        # and then file the whole UTC day under the local date holding the
        # MAJORITY of that day's rows. A UTC day is not a local day, so for
        # every row whose own local date differs from the day's majority, the
        # bar landed on a date the call was not made on. Measured over one
        # UTC day with a single request planted in each hour, the old rule
        # misfiled 8 of 24 hours at UTC+8, 9 at UTC+9, 11 at UTC+13, 5 at
        # UTC-5 and 8 at UTC-8 -- and 0 at UTC+0, which is the one zone CI
        # runs in.
        #
        # The two earlier revisions of this block each moved the error rather
        # than removing it. REVIEW-2026-07-31 fixed a negated ``tm_gmtoff``.
        # AUDIT-2026-09-27 P2 fixed a key that carried the zone offset while
        # the only consumer re-applied the zone
        # (``time.strftime("%m-%d", time.localtime(d["day"]))`` in
        # gui_fluent_pages.py:790), so the offset counted twice -- and its
        # note claimed the author's own UTC+8 "landed on the right date
        # anyway", which is true of the old formula's WEST end and false of
        # its EAST end. None of the three tests covering this could see any
        # of it, because each one recomputed the implementation's own
        # formula to build its expected value
        # (tests_bugfixes.py:100, tests/test_review_2026_07_31_core.py:111
        # and :152, tests/test_audit_2026_09_27.py:603).
        #
        # The rule is now the plain one: a request belongs to the local
        # calendar day it was made on. ``rca_local_gmtoff`` is a per-ROW
        # lookup rather than one offset for the whole query, because the
        # whole defect is that "one offset" cannot be right -- and it is
        # memoised per UTC day, since tm_gmtoff is constant within a day, so
        # the Python callback runs once per distinct day in the table and not
        # once per row.
        #
        # Kept from the old code: the offset is read at NOON of the day, so
        # a DST transition moves at most the transition day's own rows, and
        # only for zones that change offset at local midnight. Same accepted
        # caveat, now bounded to one day instead of every day.
        #
        # Also kept: the shift happens in SQL-adjacent Python rather than via
        # a named SQLite parameter, because Python's sqlite3 cannot reliably
        # bind ``?`` (WHERE) and ``:name`` (SELECT) from one positional tuple
        # (Bug-1 fix).
        import time as _time
        offset_cache: dict[int, int] = {}

        def _gmtoff(ts: float) -> int:
            day = int(ts) // 86400
            off = offset_cache.get(day)
            if off is None:
                off = _time.localtime(day * 86400 + 43200).tm_gmtoff
                offset_cache[day] = off
            return off

        # Registration and the query that uses it are done under one hold of
        # the RLock, so a concurrent summary() sharing this Database cannot
        # slip its own registration in between. Both closures would compute
        # the same value, but holding the lock across the pair costs nothing.
        with self.db.connect() as conn:
            conn.create_function("rca_local_gmtoff", 1, _gmtoff)
            local_rows = self.db.query(
                f"""SELECT
                CAST((timestamp + rca_local_gmtoff(timestamp)) / 86400 AS INTEGER) * 86400 AS local_day,
                COUNT(*) AS n,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tok
            FROM usage {where}
            GROUP BY local_day
            ORDER BY local_day""",
                params_t,
            )
        # ``local_day`` is the UTC-midnight-aligned epoch of the local
        # calendar date. The key the consumer re-localises has to be the
        # epoch of LOCAL MIDNIGHT of that same date, i.e. minus the offset --
        # which is also what makes ``(key + offset) % 86400 == 0`` hold.
        s.by_day = [
            {
                "day": int(r["local_day"] or 0) - _gmtoff(int(r["local_day"] or 0) + 43200),
                "count": int(r["n"] or 0),
                "tokens": int(r["tok"] or 0),
            }
            for r in local_rows
        ]

        return s
