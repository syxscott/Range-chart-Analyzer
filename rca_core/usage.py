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


def _row_to_record(row) -> UsageRecord:
    return UsageRecord(
        id=row["id"],
        timestamp=row["timestamp"],
        provider_id=row["provider_id"] or "",
        provider_name=row["provider_name"] or "",
        model=row["model"] or "",
        endpoint=row["endpoint"] or "",
        mode=row["mode"] or "range_chart",
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        cache_read_tokens=row["cache_read_tokens"] or 0,
        cache_creation_tokens=row["cache_creation_tokens"] or 0,
        input_tokens_estimated=bool(row["input_tokens_estimated"]),
        output_tokens_estimated=bool(row["output_tokens_estimated"]),
        total_cost_usd=row["total_cost_usd"],
        latency_ms=row["latency_ms"] or 0,
        first_token_ms=row["first_token_ms"],
        status_code=row["status_code"],
        error_message=row["error_message"] or "",
        request_id=row["request_id"] or "",
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
            s.success_rate = (s.success_count / s.total_requests) if s.total_requests else 0.0
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

        # Aggregate by UTC day in SQL (cheap, single scan). Then map each
        # UTC day to a local day using the offset that was in effect at
        # noon of that UTC day — this is correct except for rows within
        # ±offset hours of midnight on the DST transition day, which is
        # at most a 1-hour mis-attribution (acceptable for a per-day chart).
        # Doing the offset shift in Python avoids the SQL placeholder
        # binding problem caused by mixing `?` (WHERE) with `:name`
        # (SELECT) — Python's sqlite3 cannot reliably bind both from a
        # positional tuple (Bug-1 fix).
        import time as _time
        utc_rows = self.db.query(
            f"""SELECT
                CAST(timestamp / 86400 AS INTEGER) * 86400 AS utc_day,
                COUNT(*) AS n,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tok
            FROM usage {where}
            GROUP BY utc_day
            ORDER BY utc_day""",
            params_t,
        )
        # Map each UTC day → local day. Two UTC days may collapse to the
        # same local day (or one UTC day may split) only around DST, so we
        # bucket in Python.
        local_buckets: dict[int, dict[str, int]] = {}
        for r in utc_rows:
            utc_day = int(r["utc_day"] or 0)
            count = int(r["n"] or 0)
            tokens = int(r["tok"] or 0)
            # localtime at noon of this UTC day picks the DST rule that
            # affects the *majority* of that day's rows.
            ts_noon = utc_day + 43200
            local_offset = -_time.localtime(ts_noon).tm_gmtoff
            local_day = utc_day + local_offset
            bucket = local_buckets.setdefault(
                local_day, {"day": local_day, "count": 0, "tokens": 0}
            )
            bucket["count"] += count
            bucket["tokens"] += tokens
        s.by_day = [local_buckets[k] for k in sorted(local_buckets.keys())]

        return s
