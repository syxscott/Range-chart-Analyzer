"""A guard that has been wrong three times needs its own regression test.

rca_core/redact.py's docstring records three separate occasions on which this
guard silently NARROWED while looking wider, and names the failing input for
each:

  REVIEW-2026-09-10  the value character class excluded ".", "+", "/" and "="
                    -- exactly what a JWT or base64 key is made of -- so
                    "x-api-key=sk-abc.defghijklmnop" went untouched and only the
                    FIRST segment of a Bearer JWT was redacted (payload and
                    signature survived)
  AUDIT-2026-09-27   a field-name alternative required the separator to follow
                    the name DIRECTLY, so a JSON body's closing quote let
                    {"api_key": "Zq7X..."} through while ?api_key=Zq7X... was
                    redacted: one field, two serializations, opposite outcomes.
                    ?key=, secret= and token= were missing entirely, as was a
                    bare Authorization: header
  AUDIT-2026-09-27   while extending the alternation the value class was written
                    as a sibling branch; "|" binds loosest, so every branch but
                    the last lost its value matcher -- silently narrowing the
                    guard while it looked wider

Those inputs are the corpus, and they are free: the module already writes them
down. Measured over all of them plus every other branch of the alternation and
the two vendor formats, nothing leaks and nothing over-redacts -- so this file
records that the CURRENT pattern is right, and pins it so the next edit that
"just adds one alternative" cannot quietly undo it.

The controls matter as much as the leaks. The module is DELIBERATELY not chasing
"a random 24-character string in free prose": a redacted diagnosis is worth less
than a visible one, and \\bkey must not match inside "monkey". A guard that
stops at "the output changed" would have deleted that decision.
"""
import pytest

from rca_core.redact import redact_error_body as redact

# (id, text, must_be_redacted)
LEAK_CORPUS = [
    # the three historical failures, verbatim from the module docstring
    ("value_class", "x-api-key=sk-abc.defghijklmnop"),
    ("bearer_jwt",
     "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36P"),
    ("ccs_prefix", "ccs-Zq7Xr2Lp9Tn4Vb8Kd1Hs6Wc3Yg5Mj"),
    ("pk_prefix", "pk-Zq7Xr2Lp9Tn4Vb8Kd1Hs6Wc3Yg5Mj"),
    # one field, two serializations -- the exact pair the docstring says went
    # opposite ways
    ("json_field", '{"api_key": "Zq7Xr2Lp9Tn4Vb8K"}'),
    ("query_field", "?api_key=Zq7Xr2Lp9Tn4Vb8K"),
    ("secret_field", "secret=Zq7Xr2Lp9Tn4Vb8Kd"),
    ("token_field", "token=Zq7Xr2Lp9Tn4Vb8Kd1H"),
    ("bare_key_field", "key=Zq7Xr2Lp9Tn4Vb8Kd1H"),
    ("authorization_header", "Authorization: Zq7Xr2Lp9Tn4Vb8Kd1Hs6"),
    # the fixed vendor formats the prefix alternation cannot express
    ("google_key", "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"),
    ("aws_key", "AKIAIOSFODNN7EXAMPLE"),
    # every OTHER branch of the alternation, each carrying its value
    ("access_key", "access_key=Zq7Xr2Lp9Tn4Vb8Kd1Hs"),
    ("x_auth_token", "x-auth-token: Zq7Xr2Lp9Tn4Vb8Kd1"),
    ("private_key", "private-key=Zq7Xr2Lp9Tn4Vb8Kd1Hs"),
    ("session_token", "session_token = Zq7Xr2Lp9Tn4Vb8Kd"),
    ("api_key_json_nested", '{"nested": {"secret": "Zq7Xr2Lp9Tn4Kd1Hs6W"}}'),
    ("bearer_in_json", '{"authorization": "Bearer Zq7Xr2Lp9Tn4Vb8Kd1H"}'),
]

# Ordinary prose and diagnostics must stay READABLE. This is a decision, not an
# oversight: a redacted diagnosis is worth less than a visible one.
PROSE = [
    "plain error message",
    "The model could not read this figure; try again.",
    "monkey=banana",                 # \bkey must not match inside a word
    "key=7",                         # below the 8-character value floor
    "HTTP 429: rate limited, retry in 1s",
    "upstream returned 503 after 3 attempts",
    "range_top='Bed 9' range_base='Bed 7'",
]


class TestTheHistoricalLeaksStayRedacted:
    @pytest.mark.parametrize("cid,text", LEAK_CORPUS, ids=[c[0] for c in LEAK_CORPUS])
    def test_the_text_changes(self, cid, text):
        out = redact(text)
        assert out != text, f"{cid}: nothing was redacted -- {text!r} -> {out!r}"

    @pytest.mark.parametrize("cid,text", LEAK_CORPUS, ids=[c[0] for c in LEAK_CORPUS])
    def test_the_secret_tail_does_not_survive(self, cid, text):
        """Partial redaction is still a leak: the historical Bearer failure was
        exactly the payload and signature surviving after the first segment."""
        out = redact(text)
        tail = text.split("=")[-1].split(":")[-1].split()[-1].strip('"}{,]')
        if len(tail) >= 8:
            assert tail not in out, f"{cid}: {tail!r} survived in {out!r}"


class TestOrdinaryProseStaysReadable:
    @pytest.mark.parametrize("text", PROSE)
    def test_nothing_is_redacted(self, text):
        assert redact(text) == text

    def test_a_non_string_is_empty_not_an_exception(self):
        # The function is called from error paths with str(exc) and upstream
        # bodies; raising there would replace a diagnosis with a traceback.
        for value in (None, 5, [], {}, object(), b"bytes"):
            assert redact(value) == ""


class TestThePatternItself:
    def test_the_value_class_is_inside_the_outer_group(self):
        """The docstring's third entry: a sibling branch loses every other
        alternative's value matcher. Asserted structurally, because the failure
        is invisible in the rendered text."""
        import re
        from rca_core.redact import API_KEY_RE
        assert isinstance(API_KEY_RE, re.Pattern)
        # exactly one top-level alternation, with the value class inside it
        assert API_KEY_RE.pattern.count(r"[A-Za-z0-9._+\-/=]{8,}") == 1
        # the top-level alternation must not end with a bare value class
        assert not API_KEY_RE.pattern.rstrip("|)").endswith(r"{8,}")

    def test_both_serializations_of_one_field_agree(self):
        """The docstring's second entry: one field, two serializations,
        opposite outcomes. Asserted as a PAIR so a future edit that fixes the
        query form cannot break the JSON form unnoticed."""
        pairs = [
            ('{"api_key": "Zq7Xr2Lp9Tn4Vb8K"}', "?api_key=Zq7Xr2Lp9Tn4Vb8K"),
            ('{"token": "Zq7Xr2Lp9Tn4Vb8K"}', "token=Zq7Xr2Lp9Tn4Vb8K"),
            ('{"secret": "Zq7Xr2Lp9Tn4Vb8K"}', "secret=Zq7Xr2Lp9Tn4Vb8K"),
            ('{"key": "Zq7Xr2Lp9Tn4Vb8K"}', "key=Zq7Xr2Lp9Tn4Vb8K"),
        ]
        for json_form, query_form in pairs:
            a, b = redact(json_form), redact(query_form)
            assert (a != json_form) == (b != query_form), (
                f"one field, two serializations, opposite outcomes: "
                f"{json_form!r} -> {a!r} but {query_form!r} -> {b!r}")
