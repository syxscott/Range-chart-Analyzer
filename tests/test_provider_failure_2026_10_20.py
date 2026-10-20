"""A failure that a retry cannot fix must not be retried.

REVIEW-2026-10-20. The retryable set was a list of HTTP STATUSES, so a
429 was retried three times whatever it meant. But a 429 can be a
throttle ("you are going too fast", retry after a wait) or an EXHAUSTED
PLAN (the weekly bucket is empty; retrying cannot succeed, and it
spends the OTHER bucket). MiniMax's Token Plan publishes both -- the
weekly one is what throttled the end-to-end regression -- and the two
need opposite decisions.

The classifier is borrowed from sst/opencode's
``packages/ai/src/provider-error.ts``: the vendor's CODE is stronger
evidence than the status, the provider's own ``x-should-retry`` header
beats everything, and an UNRECOGNISED failure retries (a transient
failure is exactly the one that arrives in a shape no classifier
anticipated).

These tests pin the DECISION, not the wording: the operator still reads
the provider's own message.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.provider_failure import (                        # noqa: E402
    AUTH, CONTENT_POLICY, CONTEXT_OVERFLOW, INVALID_REQUEST,
    NETWORK, PAYLOAD_TOO_LARGE, QUOTA_EXHAUSTED, RATE_LIMIT, SERVER,
    UNKNOWN, classify_provider_failure,
)


def _body(code, message="", extra=None):
    err = {"type": "error", "message": message}
    if code:
        err["code"] = code
    if extra:
        err.update(extra)
    return '{"type":"error","error":%s}' % (
        __import__("json").dumps(err),)


# ---------------------------------------------------------------------------
# The categories
# ---------------------------------------------------------------------------

def test_a_plain_429_is_a_throttle_and_retries():
    f = classify_provider_failure(429, _body("rate_limit", "slow down"))
    assert f.category == RATE_LIMIT
    assert f.retryable is True


def test_a_429_whose_plan_is_empty_does_not_retry():
    """The case this whole change exists for.

    Retrying it cannot succeed, and MiniMax's Token Plan has a 5-hour
    bucket and a weekly one: a retry of a dead weekly plan spends the
    5-hour bucket that the NEXT extraction needs.
    """
    f = classify_provider_failure(
        429, _body("insufficient_quota", "quota exceeded"))
    assert f.category == QUOTA_EXHAUSTED
    assert f.retryable is False


def test_a_quota_message_without_a_code_is_still_a_quota():
    f = classify_provider_failure(429, _body(None, "Insufficient balance"))
    assert f.category == QUOTA_EXHAUSTED
    assert f.retryable is False


def test_a_quota_message_in_chinese_is_still_a_quota():
    """MiniMax answers in Chinese as readily as in English."""
    f = classify_provider_failure(429, _body(None, "账户余额不足"))
    assert f.category == QUOTA_EXHAUSTED
    assert f.retryable is False


def test_a_throttle_message_wins_over_a_quota_message():
    """Both are "limit" words. A throttle is the one a retry can fix."""
    f = classify_provider_failure(429, _body(None, "rate limit exceeded"))
    assert f.category == RATE_LIMIT
    assert f.retryable is True


def test_a_context_overflow_does_not_retry():
    """A smaller request could work; the SAME request never will."""
    f = classify_provider_failure(
        400, _body("invalid_request_error",
                   "prompt is too long: 200000 tokens > 100000 maximum"))
    assert f.category == CONTEXT_OVERFLOW
    assert f.retryable is False


def test_a_payload_that_is_too_large_does_not_retry():
    f = classify_provider_failure(413, _body(None, "request entity too large"))
    assert f.category == PAYLOAD_TOO_LARGE
    assert f.retryable is False


def test_a_bad_key_does_not_retry():
    f = classify_provider_failure(401, _body("authentication_error",
                                             "invalid api key"))
    assert f.category == AUTH
    assert f.retryable is False


def test_a_policy_refusal_does_not_retry():
    f = classify_provider_failure(400, _body("content_filter", "blocked"))
    assert f.category == CONTENT_POLICY
    assert f.retryable is False


def test_a_busy_tier_retries():
    """The 529 MiniMax answers with when the tier is congested."""
    f = classify_provider_failure(529, "busy")
    assert f.category == SERVER
    assert f.retryable is True


def test_a_network_failure_retries():
    f = classify_provider_failure(None, "")
    assert f.category == NETWORK
    assert f.retryable is True


def test_an_unrecognised_failure_retries():
    """opencode's rule: the transient failures are exactly the ones that
    arrive in a shape no classifier anticipated."""
    f = classify_provider_failure(302, _body(None, "teapot"))
    assert f.category == UNKNOWN
    assert f.retryable is True


def test_an_unrecognised_4xx_is_still_a_client_error():
    """The conservative half of the rule, and it is deliberate.

    A 4xx means the server refused the request as it stands; sending the
    same request again gets the same refusal. The old policy did not
    retry anything outside {408, 425, 429, 500, 502, 503, 504, 529}, and
    widening that to every unknown status would trade a harmless
    non-retry for a request storm. The UNKNOWN bucket is for statuses
    outside the 4xx/5xx range.
    """
    f = classify_provider_failure(418, _body(None, "teapot"))
    assert f.category == INVALID_REQUEST
    assert f.retryable is False


# ---------------------------------------------------------------------------
# The provider gets the last word
# ---------------------------------------------------------------------------

def test_x_should_retry_false_wins():
    f = classify_provider_failure(
        500, _body(None, "boom"), {"x-should-retry": "false"})
    assert f.retryable is False


def test_x_should_retry_true_wins_over_a_terminal_category():
    f = classify_provider_failure(
        400, _body("invalid_request", "nope"), {"x-should-retry": "true"})
    assert f.retryable is True


def test_the_header_name_is_case_insensitive():
    f = classify_provider_failure(
        500, _body(None, "boom"), {"X-Should-Retry": "false"})
    assert f.retryable is False


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------

def test_the_providers_retry_after_is_carried_out():
    f = classify_provider_failure(429, _body("rate_limit", "slow down"),
                                  {"Retry-After": "120"})
    assert f.retry_after_sec == 120.0


def test_a_retry_after_in_milliseconds_is_carried_out():
    f = classify_provider_failure(429, _body("rate_limit", "slow down"),
                                  {"retry-after-ms": "2500"})
    assert f.retry_after_sec == 2.5


# ---------------------------------------------------------------------------
# The retry layers use it
# ---------------------------------------------------------------------------

def test_the_outer_retry_stops_after_one_attempt_on_an_exhausted_plan(monkeypatch):
    """The behaviour the operator sees: one request, and the reason."""
    import rca_core.llm as L

    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        return (None, False, 429,
                '{"base_resp":{"status_code":1008,'
                '"status_msg":"insufficient balance"}}', None)

    monkeypatch.setattr(L, "call_llm_api", fake)
    monkeypatch.setattr(L.time, "sleep", lambda *_a: None)
    out = L.call_llm_api_with_retry(
        provider=L.LlmProvider(name="p", endpoint="https://x.example",
                                api_key="k", model="m"),
        system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=100, retries=3)
    assert calls["n"] == 1, (
        "an exhausted plan was retried %d times; retrying it cannot work and "
        "it spends the other bucket" % (calls["n"] - 1))
    assert "not attempting it again" in (out[3] or "")
    assert "quota_exhausted" in (out[3] or "")


def test_the_outer_retry_still_retries_a_throttle(monkeypatch):
    import rca_core.llm as L

    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return (None, False, 429, '{"error":{"code":"rate_limit"}}', None)
        return ("ok", False, 200, "", None)

    monkeypatch.setattr(L, "call_llm_api", fake)
    monkeypatch.setattr(L.time, "sleep", lambda *_a: None)
    out = L.call_llm_api_with_retry(
        provider=L.LlmProvider(name="p", endpoint="https://x.example",
                                api_key="k", model="m"),
        system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=100, retries=3)
    assert calls["n"] == 3
    assert out[0] == "ok"


def test_the_transport_retry_stops_on_an_exhausted_plan(monkeypatch):
    """The layer that actually meets MiniMax's 429.

    This is the one that matters: the label call and the contact-sheet
    calls all go through ``extractor._call_llm``, and it is the layer
    that spent four attempts discovering that a dead weekly plan does
    not answer.
    """
    import rca_core.extractor as E

    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        return (None, False, 429,
                '{"error":{"code":"insufficient_quota"}}', None)

    monkeypatch.setattr(E, "call_llm_api", fake)
    monkeypatch.setattr(E.time, "sleep", lambda *_a: None)
    out = E._call_llm(
        provider=E.LlmProvider(name="p", endpoint="https://x.example",
                                api_key="k", model="m"),
        system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=100)
    assert calls["n"] == 1, (
        "the transport retry spent %d attempts on an exhausted plan"
        % calls["n"])
    assert "not attempting it again" in (out[3] or "")
    assert "quota_exhausted" in (out[3] or "")


def test_the_transport_retry_still_retries_a_throttle(monkeypatch):
    import rca_core.extractor as E

    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return (None, False, 429, '{"error":{"code":"rate_limit"}}', None)
        return ("ok", False, 200, "", None)

    monkeypatch.setattr(E, "call_llm_api", fake)
    monkeypatch.setattr(E.time, "sleep", lambda *_a: None)
    out = E._call_llm(
        provider=E.LlmProvider(name="p", endpoint="https://x.example",
                                api_key="k", model="m"),
        system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=100)
    assert calls["n"] == 3
    assert out[0] == "ok"


# ---------------------------------------------------------------------------
# The headers have to survive the REAL transport
# ---------------------------------------------------------------------------

def test_the_response_headers_are_captured_over_real_http():
    """The classifier is only as good as the headers it is given.

    So this goes through a real socket and a real urllib request: the
    point is that the capture works on the path the product uses, not
    only that the classifier can read a dict somebody handed it.
    """
    import http.server
    import socket
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(429)
            self.send_header("Retry-After", "42")
            self.send_header("X-Should-Retry", "false")
            self.send_header("Content-Type", "application/json")
            body = b'{"error":{"code":"rate_limit","message":"slow down"}}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        from rca_core.llm import _last_response_headers, _post_json
        payload, status, err = _post_json(
            "http://127.0.0.1:%d/v1/messages" % port,
            {"model": "m", "max_tokens": 1},
            {"content-type": "application/json"}, 10)
        assert status == 429
        headers = _last_response_headers()
        assert headers, "no headers were captured over a real HTTP response"
        folded = {k.lower(): v for k, v in headers.items()}
        assert folded.get("retry-after") == "42", (
            "the Retry-After was lost on the way out: %r" % (headers,))
        assert folded.get("x-should-retry") == "false"
        # And the classifier can now act on them.
        failure = classify_provider_failure(
            status, err.decode("utf-8", "replace"), headers)
        assert failure.retry_after_sec == 42.0
        assert failure.retryable is False, (
            "the provider said not to retry, and that was not honoured")
    finally:
        httpd.shutdown()
        httpd.server_close()
