"""A 529 must be retried: it is the endpoint saying "the tier is busy".

MiniMax returns `overloaded_error` with HTTP 529, and it was absent from the
retryable set, so a transient congestion blip became a failed extraction. On
the two-stage path's four sequential calls the per-call rate compounds into the
run's rate -- measured: MiniMax-M3.1-Flash answered 7 of 8 single requests
and then failed every run of a 4-call measurement, because one congested call
in four is enough.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.llm as L                                  # noqa: E402


def _provider():
    return L.LlmProvider(api_format=L.ApiFormat.OPENAI,
                         endpoint="https://example.invalid/v1",
                         api_key="sk-test", model="m")


def test_529_is_retryable():
    assert 529 in L._RETRYABLE_STATUS, \
        "the overloaded answer is the textbook 'try again' answer"


def test_a_529_is_actually_retried():
    calls = []

    def fake(**kw):
        calls.append(1)
        if len(calls) == 1:
            return (None, False, 529,
                    '{"type":"error","error":{"type":"overloaded_error"}}', {})
        return ("OK", False, 200, "", {})

    with patch.object(L, "call_llm_api", side_effect=fake):
        text, truncated, status, err, _u = L.call_llm_api_with_retry(
            provider=_provider(), system_prompt="s", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=16,
            retries=3, initial_backoff_sec=0.0)
    assert len(calls) == 2, "a 529 must be retried, not returned to the caller"
    assert status == 200
    assert text == "OK"


def test_a_400_is_still_not_retried():
    """The exclusion of client errors must survive the change."""
    calls = []

    def fake(**kw):
        calls.append(1)
        return (None, False, 400, "bad request", {})

    with patch.object(L, "call_llm_api", side_effect=fake):
        _t, _tr, status, err, _u = L.call_llm_api_with_retry(
            provider=_provider(), system_prompt="s", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=16,
            retries=3, initial_backoff_sec=0.0)
    assert len(calls) == 1, calls
    assert status == 400
    assert "retry" not in err.lower(), err
