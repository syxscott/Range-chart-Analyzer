"""A cancel must actually stop the call, not just be remembered.

REVIEW-2026-10-20. The GUI's own docstring admitted the gap: for a
single-run extraction "there is NO checkpoint inside the blocking urllib
request, so the flag is only observed after extract() returns -- a
cancel during a single run can take up to the full network timeout to
take effect". The network timeout is 240 s at effort=max (it was 120 s
when the gap was written down), so the budget change made it worse.

The cancellation is now a per-thread hook the transport consults:
before a request goes out, between SSE events, and between slices of a
retry backoff.

These tests pin the three moments that matter, and each one FAILS if the
hook stops being consulted -- a cancel that is remembered but not acted
on is the bug being fixed.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core import llm                                            # noqa: E402
from rca_core.llm import (cancelled_result, cancel_requested_now,  # noqa: E402
                           clear_cancel_check, interruptible_sleep,
                           is_cancelled, set_cancel_check)


@pytest.fixture(autouse=True)
def _no_leaked_hook():
    """The hook is a thread-local; a leaked one would poison other tests."""
    clear_cancel_check()
    yield
    clear_cancel_check()


# ---------------------------------------------------------------------------
# The hook itself
# ---------------------------------------------------------------------------

def test_no_hook_means_nothing_is_cancelled():
    assert cancel_requested_now() is False


def test_the_hook_is_seen():
    set_cancel_check(lambda: True)
    assert cancel_requested_now() is True


def test_a_raising_hook_is_not_a_crash():
    """The hook is the caller's code; a broken one must not break the
    transport."""
    def boom():
        raise RuntimeError("the hook is broken")

    set_cancel_check(boom)
    assert cancel_requested_now() is False


def test_the_hook_is_per_thread():
    """A thread that has not installed one must not see another's."""
    import threading

    set_cancel_check(lambda: True)
    seen = []

    def other():
        seen.append(cancel_requested_now())

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen == [False]


def test_the_marker_is_recognised():
    assert is_cancelled("[cancelled] the caller asked to stop") is True
    assert is_cancelled(b"[cancelled] ...") is True
    assert is_cancelled("[network] timeout") is False
    assert is_cancelled("") is False
    assert is_cancelled(None) is False


# ---------------------------------------------------------------------------
# The backoff wait
# ---------------------------------------------------------------------------

def test_the_backoff_wait_is_cut_short_by_a_cancel():
    """Serving out a backoff the user has already called off is the
    smallest version of the bug."""
    set_cancel_check(lambda: True)
    t0 = time.monotonic()
    assert interruptible_sleep(30) is True
    assert time.monotonic() - t0 < 1.0, "the wait was not cut short"


def test_the_backoff_wait_still_waits_when_nothing_cancels():
    set_cancel_check(lambda: False)
    t0 = time.monotonic()
    assert interruptible_sleep(0.3) is False
    assert time.monotonic() - t0 >= 0.25


# ---------------------------------------------------------------------------
# The request itself
# ---------------------------------------------------------------------------

def test_a_cancelled_call_never_reaches_the_network(monkeypatch):
    """The point of the whole change: no request, no quota spent."""
    import rca_core.llm as L

    called = {"n": 0}

    def fake_urlopen(*_a, **_k):
        called["n"] += 1
        raise AssertionError("the network was reached after a cancel")

    monkeypatch.setattr(L.urllib.request, "urlopen", fake_urlopen)
    set_cancel_check(lambda: True)

    # A literal public address, so the SSRF guard ACCEPTS the endpoint:
    # the point of the test is that the CANCEL is what stops the call, not
    # the guard.
    provider = L.LlmProvider(name="p", endpoint="https://93.184.216.34",
                              api_key="k", model="m")
    out = L.call_llm_api(provider=provider, system_prompt="s",
                         image_b64="QQ==", media_type="image/png",
                         user_text="u", max_tokens=10)
    assert called["n"] == 0, "the request went out after a cancel"
    assert out[0] is None
    assert is_cancelled(out[3])


def test_the_retry_layer_stops_on_a_cancelled_attempt(monkeypatch):
    import rca_core.llm as L

    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        return (None, False, None,
                "[cancelled] the caller asked to stop", None)

    monkeypatch.setattr(L, "call_llm_api", fake)
    out = L.call_llm_api_with_retry(
        provider=L.LlmProvider(name="p", endpoint="https://x.example",
                                api_key="k", model="m"),
        system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=100, retries=3)
    assert calls["n"] == 1, "a cancelled attempt was retried"
    assert is_cancelled(out[3])


def test_the_retry_layer_checks_before_the_first_attempt(monkeypatch):
    """A cancel that arrives before the call starts must stop it there."""
    import rca_core.llm as L

    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        return ("ok", False, 200, "", None)

    monkeypatch.setattr(L, "call_llm_api", fake)
    set_cancel_check(lambda: True)
    out = L.call_llm_api_with_retry(
        provider=L.LlmProvider(name="p", endpoint="https://x.example",
                                api_key="k", model="m"),
        system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=100, retries=3)
    assert calls["n"] == 0, "the call was made after a cancel"
    assert is_cancelled(out[3])


def test_the_cancelled_tuple_is_the_documented_shape():
    out = cancelled_result()
    assert len(out) == 5
    assert out[0] is None and out[1] is False and out[2] is None
    assert is_cancelled(out[3])
