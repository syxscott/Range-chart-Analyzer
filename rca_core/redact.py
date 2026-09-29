"""Credential redaction for anything that leaves the process.

AUDIT-2026-09-29: this used to live inside server.py as ``_API_KEY_RE`` +
``_redact_error_body``, and it is now shared. The reason is the lesson this
review wave kept relearning: a guard that exists on one path is worse than no
guard, because it reads as "handled". The provider connection test
(``rca_core.llm.test_llm_connection``) was about to start surfacing upstream
error bodies -- which is exactly the content that echoes a rejected key back --
and the honest options were either to copy the regex (two copies drift, and
one of them keeps the bug this file's history is full of) or to have one.

The known history of the pattern below, kept because each entry is a way it
has already been wrong once:

  REVIEW-2026-09-10  the value character class excluded ".", "+", "/" and "="
    -- exactly what a JWT or base64 key is made of -- so
    "x-api-key=sk-abc.defghijklmnop" was untouched and only the FIRST segment
    of a Bearer JWT was redacted (payload + signature survived). The prefix
    alternation also missed this repo's own third-party presets ("ccs-",
    "pk-") and the Google/AWS fixed formats.
  AUDIT-2026-09-27 [P2] a field-name alternative required the separator to
    follow the name DIRECTLY, so a JSON body's closing quote let
    {"api_key": "Zq7X..."} through while ?api_key=Zq7X... was redacted: one
    field, two serializations, opposite outcomes. `?key=`, `secret=` and
    `token=` were missing entirely, as was a bare `Authorization:` header.
  AUDIT-2026-09-27  while extending the alternation the author wrote the value
    character class as a sibling branch. `|` binds loosest, so
    `A|B|C[VALUE]` parses as `A|B|(C[VALUE])` and every branch except the last
    lost its value matcher -- silently narrowing the guard while it looked
    wider. That is why the value class sits INSIDE the outer group below, and
    why "add one more alternative" is not a safe edit to this file.

Deliberately NOT chased: a random 24-character string in free prose. It is
indistinguishable from a word, and tests/test_review_2026_09_10.py pins
`redact("plain error message") == "plain error message"` -- a redacted
diagnosis is worth less than a visible one. `\\bkey` does not match inside
"monkey", so the `key=` alternative cannot swallow ordinary English.
"""
from __future__ import annotations

import re

__all__ = ["redact_error_body", "API_KEY_RE"]

#: One compiled pattern, used by every caller. The value class
#: ``[A-Za-z0-9._+\-/=]{8,}`` is INSIDE the outer group on purpose -- see the
#: module docstring for what happens if it is not.
API_KEY_RE = re.compile(
    r"(?:"
    r"sk-|ccs-|pk-|Bearer\s+"
    r"|(?:x-)?(?:api|access|auth|session|secret|private)[_-]?key[\"']?\s*[:=]\s*[\"']?"
    r"|(?:x-)?(?:auth|access|session|bearer|api)[_-]?token[\"']?\s*[:=]\s*[\"']?"
    r"|secret[\"']?\s*[:=]\s*[\"']?"
    r"|\btoken[\"']?\s*[:=]\s*[\"']?"
    r"|\bkey[\"']?\s*[:=]\s*[\"']?"
    r"|authorization[\"']?\s*[:=]\s*[\"']?"
    r")[A-Za-z0-9._+\-/=]{8,}"
    r"|AIza[0-9A-Za-z_\-]{20,}"
    r"|AKIA[0-9A-Z]{16}",
    re.IGNORECASE,
)


def redact_error_body(body):
    """Remove API-key-like tokens from a string before it leaves the process.

    ``body`` is often ``str(exc)`` or an upstream JSON body, so it is typed
    defensively: a non-string yields ``""`` rather than raising inside an
    error path. The pattern has no capture group (the alternation covers whole
    tokens), so the WHOLE match is replaced -- an old ``\\1`` reference raised
    "invalid group reference" on every call.
    """
    if not isinstance(body, str):
        return ""
    return API_KEY_RE.sub("[REDACTED]", body)
