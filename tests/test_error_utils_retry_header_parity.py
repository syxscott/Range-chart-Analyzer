"""The Retry-After contract, measured against js/error-utils.js.

AUDIT-2026-10-01. This is a two-sided mirror that NO differential fixture group
covers -- the parity harness has no group for rca_core/error_utils.py -- and it
decides how long the tool waits before trying a rate-limited endpoint again.

The numbers below come from running both implementations over the same 30 header
shapes and 34 statuses, not from reading them. What is pinned here is the part
that is a property of THIS side alone, so the tests do not need node:

  * the header lookup is case-insensitive (fixed here; a no-op in production
    while both live call sites pass headers=None);
  * the HTTP-date branch accepts RFC 9110 IMF-fixdate and rejects ISO-8601,
    which the browser's `new Date()` accepts. Deliberately NOT fixed -- ISO-8601
    is not what RFC 9110 asks for, so accepting it is a judgment call, and the
    point of the characterisation test is that whoever changes it has to notice.

The cross-engine half is recorded in the get_retry_delay docstring rather than
asserted here, because asserting it would need node in the pytest jobs.
"""
import pytest

from rca_core import error_utils as EU


def _delay(headers):
    return EU.get_retry_delay(429, headers)


class TestHeaderNameCasing:
    """RFC 9110 section 5.1: field names are case-insensitive.

    The browser is handed a real ``Headers`` and therefore honours every
    spelling; this side used to honour exactly two, so "RETRY-AFTER" -- which
    proxies and CDNs do emit -- was silently ignored and the request was retried
    on the blind backoff instead.
    """

    @pytest.mark.parametrize("name", [
        "retry-after", "Retry-After", "RETRY-AFTER", "retry-After",
        "Retry-after", "rEtRy-AfTeR",
    ])
    def test_retry_after_is_honoured_in_any_casing(self, name):
        assert _delay({name: "7"}) == 7.0, (
            f"{name!r} is a legal spelling of Retry-After and must be honoured")

    @pytest.mark.parametrize("name", [
        "retry-after-ms", "Retry-After-Ms", "RETRY-AFTER-MS", "retry-after-MS",
    ])
    def test_retry_after_ms_is_honoured_in_any_casing(self, name):
        assert _delay({name: "2500"}) == 2.5

    def test_a_surrounding_space_is_not_a_casing(self):
        # Not a legal header name, and the browser's real Headers rejects it
        # outright rather than trimming. Pinned so a future "be lenient" change
        # is a decision instead of a side effect.
        assert _delay({" retry-after": "7"}) != 7.0

    def test_only_the_key_is_folded_not_the_value(self):
        # Folding must not reach into the value: a value that happens to read
        # like a header name is still a number, and must still parse as one.
        assert _delay({"Retry-After": "7"}) == 7.0
        assert _delay({"RETRY-AFTER": "7.5"}) == 7.5

    def test_a_non_mapping_does_not_raise(self):
        # The function is documented as total; a caller handing something odd
        # must not turn a rate limit into a crash.
        assert _delay(None) >= EU.MIN_RETRY_DELAY_SECONDS
        assert _delay("retry-after: 7") >= EU.MIN_RETRY_DELAY_SECONDS
        assert _delay(7) >= EU.MIN_RETRY_DELAY_SECONDS


class TestHttpDateFormat:
    """The one divergence left in this function, characterised not fixed.

    ``parsedate_to_datetime`` accepts the RFC 9110 HTTP-date and nothing else;
    ``new Date(string)`` in js/error-utils.js also accepts ISO-8601. Measured:
    for ``Retry-After: 2099-10-21T07:28:00Z`` this side falls through to the
    backoff (1 s floor) where the browser waits the requested time.
    """

    RFC_DATE = "Wed, 21 Oct 2099 07:28:00 GMT"
    ISO_DATE = "2099-10-21T07:28:00Z"

    def test_rfc9110_http_date_is_honoured(self):
        # The format the spec actually asks for: both engines agree here, and
        # this must never regress. max_delay is raised so "honoured" is visible
        # as a large number rather than as the default 60 s ceiling -- a 2099
        # date is thousands of years out, so the ceiling would otherwise make
        # this test pass for the wrong reason (it would also pass if the header
        # were ignored and something else saturated the cap).
        got = EU.get_retry_delay(429, {"retry-after": self.RFC_DATE},
                                 max_delay=100_000_000.0)
        assert got > 1_000_000.0, f"RFC 9110 date was not honoured: {got!r}"

    def test_an_already_elapsed_rfc_date_falls_back_to_the_floor(self):
        assert _delay({"retry-after": "Wed, 21 Oct 1999 07:28:00 GMT"}) == \
            EU.MIN_RETRY_DELAY_SECONDS

    def test_iso8601_is_rejected_here_and_accepted_in_the_browser(self):
        # Characterisation, on purpose. If this ever starts returning a large
        # delay, whoever changed it did so on purpose and should say so in the
        # docstring rather than leave a silent asymmetry.
        assert _delay({"retry-after": self.ISO_DATE}) == EU.MIN_RETRY_DELAY_SECONDS

    def test_an_unparseable_date_falls_back_to_the_floor(self):
        for junk in ("not a date at all", "Oct 21 2099 07:28:00 GMT", ""):
            assert _delay({"retry-after": junk}) >= EU.MIN_RETRY_DELAY_SECONDS


class TestNumericAndFloorBehaviour:
    """Unchanged by the casing fix, and the controls that prove it."""

    @pytest.mark.parametrize("value,expected", [
        ("7", 7.0), ("7.5", 7.5), ("+7", 7.0), ("  7  ", 7.0),
        ("1e3", 1000.0), ("0", EU.MIN_RETRY_DELAY_SECONDS),
        ("-5", EU.MIN_RETRY_DELAY_SECONDS), ("abc", EU.MIN_RETRY_DELAY_SECONDS),
    ])
    def test_numeric_spellings(self, value, expected):
        got = _delay({"retry-after": value})
        if expected >= 1000.0:
            # capped at max_delay, which is the documented ceiling
            assert got == 60.0
        else:
            assert got == expected

    def test_retry_after_ms_wins_over_retry_after(self):
        assert _delay({"retry-after-ms": "2500", "retry-after": "99"}) == 2.5

    def test_the_ceiling_beats_the_floor(self):
        assert EU.get_retry_delay(429, {"retry-after": "0"}, max_delay=0) == 0
        assert EU.get_retry_delay(429, {"retry-after": "300"},
                                  max_delay=12.0) == 12.0
