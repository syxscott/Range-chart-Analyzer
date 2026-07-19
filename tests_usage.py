"""Tests for rca_core.usage (token estimator + parsers + store)."""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core.db import Database
from rca_core.usage import (
    UsageRecord, UsageStore, estimate_tokens,
    parse_anthropic_usage, parse_openai_usage, parse_gemini_usage,
    parse_usage,
)

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


# --- estimator ---

def test_estimator_empty():
    check("est-empty", estimate_tokens("") == 0)


def test_estimator_english():
    # 20 chars of English ~= 5 tokens
    n = estimate_tokens("Hello world this is t")
    check("est-english-20ch", 4 <= n <= 7)


def test_estimator_cjk():
    # 10 CJK characters ~= 5-10 tokens (CJK ≈ 1 token each)
    n = estimate_tokens("你好世界中文测试")
    check("est-cjk-10ch", 5 <= n <= 10)


def test_estimator_mixed():
    n = estimate_tokens("Hello 你好 world 世界 test 测试")
    check("est-mixed-nonzero", n > 0)


# --- parsers ---

def test_parse_anthropic():
    payload = {
        "id": "msg_01",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
        },
    }
    u = parse_anthropic_usage(payload)
    check("pa-inp", u["input_tokens"] == 100)
    check("pa-out", u["output_tokens"] == 50)
    check("pa-cr", u["cache_read_tokens"] == 30)
    check("pa-cc", u["cache_creation_tokens"] == 10)


def test_parse_anthropic_no_usage():
    check("pa-missing", parse_anthropic_usage({"id": "x"}) is None)


def test_parse_openai():
    payload = {"usage": {"prompt_tokens": 200, "completion_tokens": 80, "cached_tokens": 40}}
    u = parse_openai_usage(payload)
    check("po-inp", u["input_tokens"] == 200)
    check("po-out", u["output_tokens"] == 80)
    check("po-cr", u["cache_read_tokens"] == 40)


def test_parse_gemini():
    payload = {"usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 20, "cachedContentTokenCount": 5}}
    u = parse_gemini_usage(payload)
    check("pg-inp", u["input_tokens"] == 50)
    check("pg-out", u["output_tokens"] == 20)
    check("pg-cr", u["cache_read_tokens"] == 5)


def test_parse_usage_dispatch():
    # Anthropic-shape payload, hint=anthropic
    payload = {"usage": {"input_tokens": 5, "output_tokens": 5}}
    u = parse_usage(payload, "anthropic")
    check("pu-inp", u["input_tokens"] == 5)


def test_parse_usage_all_zero():
    check("pu-zero", parse_usage({"usage": {"input_tokens": 0, "output_tokens": 0}}, "anthropic") is None)


# --- store ---

def fresh_store():
    td = tempfile.mkdtemp()
    db = Database(path=os.path.join(td, "test.db"))
    return UsageStore(db=db), td


def test_record_and_count():
    s, td = fresh_store()
    try:
        rid = s.record(UsageRecord(input_tokens=10, output_tokens=5, model="gpt-4o"))
        check("rec-id", rid > 0)
        check("rec-count", s.count() == 1)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_summary_aggregates():
    s, td = fresh_store()
    try:
        for i in range(3):
            s.record(UsageRecord(
                timestamp=time.time(),
                provider_id="p1", provider_name="Provider1", model="gpt-4o",
                input_tokens=100, output_tokens=50, status_code=200,
            ))
        s.record(UsageRecord(
            timestamp=time.time(),
            provider_id="p2", provider_name="Provider2", model="claude-sonnet-4",
            input_tokens=200, output_tokens=100, status_code=200,
        ))
        s.record(UsageRecord(
            timestamp=time.time(),
            provider_id="p1", provider_name="Provider1",  # same as loop
            model="gpt-4o",
            input_tokens=10, output_tokens=5, status_code=500,
        ))
        sm = s.summary()
        check("sum-requests", sm.total_requests == 5)
        check("sum-input", sm.total_input_tokens == 510)   # 3*100 + 200 + 10
        check("sum-output", sm.total_output_tokens == 255)  # 3*50  + 100 + 5
        check("sum-success-count", sm.success_count == 4)
        check("sum-providers", len(sm.by_provider) == 2)
        check("sum-models", len(sm.by_model) == 2)
        check("sum-days", len(sm.by_day) >= 1)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_summary_cache_hit_rate():
    s, td = fresh_store()
    try:
        # 50 input + 100 cache_creation + 50 cache_read → hit = 50/200 = 0.25
        s.record(UsageRecord(input_tokens=50, cache_read_tokens=50, cache_creation_tokens=100))
        sm = s.summary()
        check("cache-hit-rate", abs(sm.cache_hit_rate - 0.25) < 1e-6)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_summary_estimated_flag():
    s, td = fresh_store()
    try:
        s.record(UsageRecord(input_tokens=10, output_tokens=5, input_tokens_estimated=True))
        sm = s.summary()
        check("sum-estimated", sm.estimated_rows == 1)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_summary_time_range():
    s, td = fresh_store()
    try:
        old = time.time() - 86400 * 7  # 7 days ago
        s.record(UsageRecord(timestamp=old, input_tokens=100, output_tokens=50))
        s.record(UsageRecord(timestamp=time.time(), input_tokens=10, output_tokens=5))
        sm_all = s.summary()
        sm_recent = s.summary(start_ts=time.time() - 3600)
        check("sum-all-req", sm_all.total_requests == 2)
        check("sum-recent-req", sm_recent.total_requests == 1)
        check("sum-recent-inp", sm_recent.total_input_tokens == 10)
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


def test_list_ordering():
    s, td = fresh_store()
    try:
        for i in range(5):
            s.record(UsageRecord(timestamp=time.time() + i, model=f"m{i}"))
        rows = s.list()
        check("list-count", len(rows) == 5)
        check("list-newest", rows[0].model == "m4")
    finally:
        import shutil; shutil.rmtree(td, ignore_errors=True)


test_estimator_empty()
test_estimator_english()
test_estimator_cjk()
test_estimator_mixed()
test_parse_anthropic()
test_parse_anthropic_no_usage()
test_parse_openai()
test_parse_gemini()
test_parse_usage_dispatch()
test_parse_usage_all_zero()
test_record_and_count()
test_summary_aggregates()
test_summary_cache_hit_rate()
test_summary_estimated_flag()
test_summary_time_range()
test_list_ordering()
print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)

