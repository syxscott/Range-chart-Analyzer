"""The error-code and display contract, measured against js/error-utils.js.

AUDIT-2026-10-01. Companion to test_error_utils_retry_header_parity.py, which
covers the retry-hint / retry-delay half. This covers the half that reaches the
operator: the machine code lifted out of an error body and the text that ends up
in the badge.

Two divergences came out of running both implementations over 1960
(status x body x message) combinations, 15680 field comparisons:

  1. A provider that returns a NON-STRING error code. This side used ``str(v)``
     and the browser ``String(v)``, and they disagree from the very first
     value: null -> "None" vs "null", true -> "True" vs "true", [1] -> "[1]" vs
     "1", {"deep": 1} -> "{'deep': 1}" vs "[object Object]". The array row is
     the one that loses information rather than formatting, because "1" is
     indistinguishable from a real numeric code, and the object row put a Python
     repr inside a user-visible string -- the same defect _stringify_scalar was
     written to stop elsewhere in this codebase.
  2. ``status = 0``. The "is there a status" test was truthiness here and
     ``!== null`` in the browser, so the desktop rendered "Network error" and
     dropped the caller's prefix while the browser rendered "HTTP 0" and kept
     it.

Both are fixed. What is pinned here is the Python half, so the tests do not need
node; the cross-engine measurement is recorded in the two docstrings.
"""
import pytest

from rca_core import error_utils as EU


def norm(status, body, message=""):
    body_bytes = None if body is None else body.encode("utf-8")
    return EU.normalize_http_error(status, body_bytes, message)


class TestErrorCodeAcceptance:
    """A code is a string or an integer; anything else is not a code."""

    @pytest.mark.parametrize("body,expected", [
        # strings, in every location the extractor looks
        ('{"error": {"code": "rate_limit_exceeded"}}', "rate_limit_exceeded"),
        ('{"error_code": "top"}', "top"),
        ('{"code": "plain"}', "plain"),
        ('{"type": "typed"}', "typed"),
        ('{"error.type": "dotted_literal_key"}', "dotted_literal_key"),
        ('{"error": {"type": "nested"}}', "nested"),
        ('{"error": {"error_code": "nested_ec"}}', "nested_ec"),
        # integers are legitimate ids and both engines already agreed on them
        ('{"code": 7}', "7"),
        ('{"code": 0}', "0"),
        ('{"code": -3}', "-3"),
        # everything else is not a code, and must not be rendered as one
        ('{"code": null}', None),
        ('{"code": true}', None),
        ('{"code": false}', None),
        ('{"code": [1]}', None),
        ('{"code": {"deep": 1}}', None),
        ('{"code": 7.5}', None),
        ('{"code": ""}', None),
        ('{"code": 1e3}', None),
    ])
    def test_accepted_and_rejected_shapes(self, body, expected):
        assert norm(500, body).error_code == expected

    def test_a_python_repr_can_never_reach_the_badge(self):
        # The specific harm: "[{'deep': 1}]" is not a code anyone can look up.
        for body in ('{"code": {"deep": 1}}', '{"code": [1]}', '{"code": null}'):
            code = norm(500, body).error_code
            assert code is None, f"{body} produced a fabricated code {code!r}"
            assert "{" not in (norm(500, body).display_message or "") or code is None

    def test_an_unusable_key_does_not_stop_the_search(self):
        # `code` present but null, and the nested `error.type` is a real one:
        # the caller should get the usable code, not stop at the first key.
        assert norm(500, '{"code": null, "error": {"type": "real"}}').error_code \
            == "real"

    def test_the_order_of_preference_is_unchanged(self):
        assert norm(500, '{"code": "c", "error_code": "ec"}').error_code == "ec"
        assert norm(500, '{"type": "t", "code": "c"}').error_code == "c"

    def test_non_dict_and_malformed_bodies_yield_no_code(self):
        for body in ("not json", "[1,2]", '"a string"', "42", "true", "null",
                     "", "   ", None):
            assert norm(500, body).error_code is None


class TestStatusZeroIsAStatus:
    """0 is falsy in Python and present in JavaScript. Pick one, pick it twice.

    Note where the distinction is VISIBLE. display_message only prefixes the
    body, and with no body it returns the message unchanged, so the "is there a
    status" question surfaces in format_provider_error, which is where the
    truthiness bug was.
    """

    def test_status_zero_renders_as_a_status(self):
        n = norm(0, None, "request failed")
        assert n.display_message == "request failed"
        assert EU.format_provider_error(n) == "HTTP 0: request failed"
        assert EU.format_provider_error(n, "upstream") == "upstream (0): request failed"

    def test_no_status_is_still_a_network_error(self):
        n = norm(None, None, "request failed")
        assert n.display_message == "request failed"
        assert EU.format_provider_error(n) == "request failed"
        assert EU.format_provider_error(n, "upstream") == "request failed"

    def test_none_and_zero_are_distinguishable_end_to_end(self):
        assert EU.format_provider_error(norm(None, None, "m")) \
            != EU.format_provider_error(norm(0, None, "m"))

    def test_a_body_with_status_zero_keeps_the_status_prefix(self):
        # The branch that used the truthiness test on the prefix as well.
        assert norm(0, "body text", "").display_message == "HTTP 0: body text"


class TestCodePrefixInTheBadge:
    """The `[CODE]` prefix is the part an operator can paste into provider docs."""

    def test_a_real_code_is_prefixed(self):
        # The message does NOT carry the body, so display_message takes the
        # body branch and the status prefix is applied too.
        got = norm(429, '{"error": {"code": "rate_limit_exceeded"}}', "boom") \
            .display_message
        assert got.startswith("[rate_limit_exceeded] HTTP 429: "), got

    def test_no_code_means_no_brackets(self):
        got = norm(429, "no code here", "boom").display_message
        assert got == "HTTP 429: no code here"
        assert "[" not in got

    def test_a_rejected_value_leaves_no_bracket_prefix(self):
        # "starts with [" rather than "contains no [": the body itself may
        # contain brackets ({"code": [1]}), and what must not appear is the
        # fabricated `[CODE] ` lead-in.
        for body in ('{"code": null}', '{"code": [1]}', '{"code": true}'):
            n = norm(429, body, "boom")
            assert n.error_code is None, body
            assert not n.display_message.startswith("["), f"{body} -> {n.display_message!r}"

    def test_a_message_that_carries_the_body_shows_the_message(self):
        # The other display_message branch, as the control for the two above.
        body = '{"error": {"code": "c"}}'
        got = norm(429, body, f"provider said: {body}").display_message
        assert got == "[c] provider said: " + body


class TestDecodeBody:
    """The bytes path exists only on this side; the browser gets decoded text."""

    def test_utf8_round_trips(self):
        assert EU._decode_body('{"a": "中"}'.encode("utf-8")) == '{"a": "中"}'

    def test_invalid_bytes_are_replaced_not_raised(self):
        out = EU._decode_body(b'{"a": "\xff"}')
        assert "�" in out

    def test_empty_and_none(self):
        assert EU._decode_body(b"") == ""
        assert EU._decode_body(None) == ""
