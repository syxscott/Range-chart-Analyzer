"""A busy tier and a dead socket do not cost the same, so they must not be
retried the same number of times.

MiniMax answers a congested tier with HTTP 529 ``overloaded_error`` -- "busy,
come back" -- and it comes back in ABOUT A SECOND. A read timeout, by
contrast, has already cost the whole per-call timeout.

The transport wrapper gave both exactly one retry. Measured on
MiniMax-M3.1-Flash at a congested tier, the reference figure's three-run
regression lost 31 of 93 cells to bare 529s (precision/recall 0.333 and
0.667) and a third run failed outright -- one retry is not enough for a busy
tier, and retrying a one-second answer is nearly free.

The budget is not free, though: the two-stage path makes four calls against
the server's single-run budget of 745 s, and a timeout costs 90 s of it. So
the counts are split -- a server that ANSWERED gets more tries, a
network-level failure keeps the old count -- and the worst case is unchanged
(at most two timeouts per call, four calls, 2 x 90 s = 720 s < 745 s).
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.extractor as E                                # noqa: E402


def _run(answers, **kw):
    """Drive _call_llm over a scripted sequence of answers.

    ``answers`` is a list of (text, status) pairs; the last one repeats.
    Returns (calls made, returned 5-tuple).
    """
    calls = []

    def fake(**_kw):
        idx = min(len(calls), len(answers) - 1)
        calls.append(1)
        text, status = answers[idx]
        return (text, False, status, "boom" if status else "", {})

    with patch.object(E, "call_llm_api", side_effect=fake), \
            patch.object(E.time, "sleep"):
        result = E._call_llm(provider=None, system_prompt="s",
                             image_b64="QUFB", media_type="image/png",
                             user_text="hi", max_tokens=16, **kw)
    return len(calls), result


def test_a_529_gets_more_than_one_retry():
    calls, (text, _t, status, _e, _u) = _run(
        [(None, 529), (None, 529), (None, 529), ("OK", 200)])
    assert calls == 4, \
        "a busy tier that answers in a second deserves three more shots"
    assert text == "OK"
    assert status == 200


def test_a_529_that_never_clears_stops_at_the_budget():
    calls, _result = _run([(None, 529)])
    assert calls == E.TRANSPORT_ATTEMPTS, \
        "the retry chain must be bounded, not open-ended"


def test_a_network_timeout_keeps_the_small_budget():
    """A timeout already cost the whole per-call timeout."""
    calls, _result = _run([(None, None)])
    assert calls == E.TRANSPORT_TIMEOUT_ATTEMPTS, \
        "a timeout must not be retried as often as a one-second 529"
    assert E.TRANSPORT_TIMEOUT_ATTEMPTS < E.TRANSPORT_ATTEMPTS


def test_the_worst_case_still_fits_the_server_budget():
    """At most two timeouts per call, four calls, 2 x 90 s < the 745 s budget."""
    from rca_core.matrix import MAX_SHEET_CALLS, MATRIX_CALL_TIMEOUT_SEC
    calls_in_a_run = 1 + MAX_SHEET_CALLS          # labels + one per sheet
    worst = calls_in_a_run * E.TRANSPORT_TIMEOUT_ATTEMPTS * MATRIX_CALL_TIMEOUT_SEC
    assert worst < 745, \
        "the retry chain must not be able to outrun the server's budget"


def test_a_time_budget_stops_the_retry_chain():
    """A deadline is the outer bound, and it is checked BEFORE each retry.

    This is what makes the cheap/expensive split work: a 529 costs a second
    and can be ridden out, while a read timeout costs the whole per-call
    ceiling and must not be repeated past the caller's budget.
    """
    calls = []

    def fake(**_kw):
        calls.append(1)
        return (None, False, 529, "busy", {})

    with patch.object(E, "call_llm_api", side_effect=fake), \
            patch.object(E.time, "sleep"):
        E._call_llm(provider=None, system_prompt="s", image_b64="QUFB",
                    media_type="image/png", user_text="hi", max_tokens=16,
                    timeout_sec=90, time_budget_sec=1.0)
    assert len(calls) == 1, \
        "a retry that cannot fit inside the budget must not be made"


def test_a_client_error_is_never_retried():
    calls, _result = _run([(None, 400)])
    assert calls == 1, "400 is the caller's mistake; a retry burns quota"


def test_a_success_is_not_retried():
    calls, (text, _t, status, _e, _u) = _run([("OK", 200)])
    assert calls == 1
    assert text == "OK"


def test_the_retry_notes_say_how_many_were_spent():
    _calls, (_text, _t, _status, err_body, _u) = _run(
        [(None, 529), ("OK", 200)])
    assert "transport retry" in (err_body or "")
    assert "1/3" in (err_body or ""), \
        "the annotation must advertise the real number of retries"
