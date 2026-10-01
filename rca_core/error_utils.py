"""Error normalization and retry utilities.

Inspired by DeepSeek Harness's pi-ai error handling patterns:
- Unified error normalization across different LLM providers
- Smart retry with jitter and Retry-After header support
- Consistent error formatting for user display

This module provides:
- NormalizedError: structured error representation
- retry_with_backoff(): exponential backoff with jitter
- format_provider_error(): human-readable error messages
"""

from __future__ import annotations

import datetime
import json
import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Status codes worth retrying (transient upstream errors).
# 401/403/400 are authentication/client errors - retrying is futile.
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Maximum error body length to store.
MAX_ERROR_BODY_CHARS = 2000

# REVIEW-2026-09-20 #108: floor for every computed retry delay. A provider
# legitimately answers a 429 with ``Retry-After: 0`` (or ``Retry-After-Ms: 0``,
# or an HTTP-date that has already passed), which used to be honoured
# literally: the retry loop then hammered the endpoint back-to-back with zero
# spacing, which is exactly what turns a rate limit into a longer BAN. One
# second is also the smallest delay that survives the ``Retry-After`` round
# trip of most gateways, and a caller that genuinely wants no wait can still
# pass ``max_delay=0`` (the clamp is applied BELOW the ceiling, so a ceiling
# under the floor wins — tests rely on that).
MIN_RETRY_DELAY_SECONDS = 1.0

@dataclass
class NormalizedError:
    """Structured error representation across different provider error formats.

    Mirrors the pattern from DSH's pi-ai error-body.js normalization.
    """

    # HTTP status code (None for network errors)
    status: int | None = None

    # Decoded error body string
    body: str = ""

    # Original error message
    message: str = ""

    # Whether the message already contains the body
    message_carries_body: bool = False

    # Provider-specific error code
    error_code: str | None = None

    # Retry suggestion (None, "retry", "do_not_retry")
    retry_hint: str | None = None

    @property
    def is_retryable(self) -> bool:
        """Check if this error should be retried."""
        if self.status is None:
            return True
        return self.status in RETRYABLE_STATUS

    @property
    def display_message(self) -> str:
        """Get the best message for user display.

        AUDIT-2026-10-01: the "is there a status" test was truthiness here and
        ``!== null`` in js/error-utils.js, so a ``status`` of 0 was "no status"
        on this side and a status on the browser: the desktop rendered
        "Network error: ..." and dropped the caller's prefix, while the browser
        rendered "HTTP 0: ...". 0 is not an HTTP status, but a caller that
        uses it as a "no status" sentinel should be SPELLED that way (None), and
        a caller that means it should see the same thing on both transports.
        """
        if self.message_carries_body or not self.body:
            base = self.message
        else:
            prefix = f"HTTP {self.status}" if self.status is not None else "Network error"
            base = f"{prefix}: {self.body}"
        if self.error_code:
            base = f"[{self.error_code}] {base}"
        return base


def normalize_http_error(
    status: int | None,
    body_bytes: bytes | None,
    message: str,
) -> NormalizedError:
    """Normalize an HTTP error into a structured form."""
    body = _decode_body(body_bytes)
    stored = body[:MAX_ERROR_BODY_CHARS]
    # REVIEW-2026-11-07 (low): check the STORED (truncated) form, not the
    # full decoded body. Before, a message that embedded the body as the
    # caller had truncated it (or as the retry suffix re-emitted it) failed
    # the `message.find(full_body)` test, display_message then preferred
    # the raw prefix path, and the message's own context was lost. A
    # message containing the full body also contains its truncated prefix,
    # so testing `stored` covers both cases.
    message_carries_body = (not stored) or (stored in message)
    error_code = _extract_error_code(body_bytes, body)
    retry_hint = _get_retry_hint(status, error_code)

    return NormalizedError(
        status=status,
        body=stored,
        message=message,
        message_carries_body=message_carries_body,
        error_code=error_code,
        retry_hint=retry_hint,
    )


def _decode_body(body_bytes: bytes | None) -> str:
    """Best-effort decode of error body bytes.

    AUDIT-2026-10-01: the ``latin-1`` entry in that tuple is DEAD, and the loop
    reads as though a legacy latin-1 provider body decodes correctly when it
    does not. ``errors="replace"`` makes the utf-8 decode unraisable, so the
    first iteration always returns; measured, a body of invalid utf-8 and a body
    containing a raw latin-1 byte both come out with U+FFFD in the same place.
    The browser side agrees, because ``fetch().text()`` also replaces.

    Left in place rather than deleted, and flagged here for the same reason
    rca_core/bed_parser.py keeps its own deliberately-dead unit entry: a
    reader should not have to re-measure it. The behaviour is unchanged.
    """
    if not body_bytes:
        return ""
    for encoding in ("utf-8", "latin-1"):
        try:
            return body_bytes.decode(encoding, errors="replace")[:MAX_ERROR_BODY_CHARS]
        except Exception:
            continue
    return ""


def _extract_error_code(body_bytes: bytes | None, body: str) -> str | None:
    """Try to extract a machine-readable error code from the body.

    REVIEW-2026-09-20 #110: this used to re-decode ``body_bytes`` and parse
    the FULL payload while ``_decode_body`` had already truncated the body it
    stores/returns at ``MAX_ERROR_BODY_CHARS`` — so the ``[CODE]`` prefix the
    user sees could come from text that is no longer displayed (and a huge
    hostile body cost a second, unbounded parse). ``body`` is now the single
    source of truth: what gets parsed is exactly what is stored, which is also
    what makes the code consistent with the ``display_message`` the operator
    can actually correlate against a provider status page. A body truncated
    mid-JSON simply yields no code, like any other non-JSON error body.
    ``body_bytes`` stays in the signature for the existing callers.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            for key in ("error_code", "code", "type", "error.type"):
                if key in data:
                    code = _usable_error_code(data[key])
                    if code is not None:
                        return code
            error = data.get("error", {})
            if isinstance(error, dict):
                for key in ("error_code", "code", "type"):
                    if key in error:
                        code = _usable_error_code(error[key])
                        if code is not None:
                            return code
    except Exception:
        pass
    return None


def _usable_error_code(value: Any) -> str | None:
    """The machine code to show, or ``None`` when the value is not one.

    AUDIT-2026-10-01: this used to be a bare ``str(value)`` on this side and a
    bare ``String(value)`` in js/error-utils.js, and for a provider that returns
    a NON-STRING code the two produced different text in the operator's badge:

        body                     this side            browser
        {"code": null}           "None"              "null"
        {"code": true}           "True"              "true"
        {"code": [1]}            "[1]"               "1"
        {"code": {"deep": 1}}    "{'deep': 1}"       "[object Object]"

    The array row is the one that loses information rather than just formatting:
    ``String([1])`` is ``"1"``, indistinguishable from a real numeric code, so
    the badge asserts a code the provider never sent. The object row put a
    Python repr inside a user-visible string, which is the same defect
    ``_stringify_scalar`` was written to stop elsewhere in this codebase.

    So a code is accepted only when it is a string, or an integer (some
    providers use numeric ids, and both engines already agree on those). A
    float is excluded rather than coerced: ``str(7.0)`` is ``"7.0"`` where
    ``String(7)`` is ``"7"``, and a float error code is not a thing. ``None`` is
    returned for everything else, which also lets the caller keep looking
    instead of stopping at the first present-but-unusable key.
    """
    if isinstance(value, str):
        return value or None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return None


def _get_retry_hint(status: int | None, error_code: str | None) -> str | None:
    """Determine if and how to retry based on status and error code."""
    if status is None:
        return "retry"
    if status == 429:
        return "retry_with_delay"
    if status in (401, 403, 400):
        return "do_not_retry"
    if 500 <= status < 600:
        return "retry"
    if status in RETRYABLE_STATUS:
        return "retry"
    return None


def format_provider_error(
    normalized: NormalizedError,
    prefix: str | None = None,
) -> str:
    """Format a normalized error for user display.

    AUDIT-2026-10-01: ``if normalized.status`` was truthiness again, so a
    ``status`` of 0 dropped BOTH the caller's prefix and the ``HTTP n`` prefix
    here, where js/error-utils.js#formatError keyed the same two branches on a
    real "is there a status" test. See NormalizedError.display_message.
    """
    msg = normalized.display_message
    has_status = normalized.status is not None
    if prefix and has_status:
        return f"{prefix} ({normalized.status}): {msg}"
    if has_status:
        return f"HTTP {normalized.status}: {msg}"
    return msg

def get_retry_delay(
    status: int | None,
    headers: dict | None = None,
    attempt: int = 0,
    initial_delay: float = 0.8,
    backoff_factor: float = 1.6,
    max_delay: float = 60.0,
) -> float:
    """Calculate retry delay with exponential backoff and jitter.

    Respects Retry-After headers **when they are passed in**, with smart
    fallback. Mirrors DSH's provider-retry.js getRetryDelayMs() logic.

    AUDIT-2026-09-28: the "when they are passed in" is load-bearing, and the
    bare "Respects Retry-After headers when present" this used to say is how
    that came to be trusted as a live guarantee. The test suite pins the
    header behaviour at THIS level -- ``get_retry_delay(429, {"retry-after":
    "7"}) == 7.0`` and a zero Retry-After never meaning zero backoff -- and
    all of that is correct. But only ONE of the three production call sites
    can supply headers at all:

        error_utils.retry_with_backoff   headers=get_headers() or None  OK
        llm.call_llm_api_with_retry      headers=None   <-- LLM calls
        extractor transport retry        headers=None   <-- image transport

    The two that actually hit a provider's rate limit always pass None,
    because ``call_llm_api`` reads the response and never captures
    ``resp.headers`` -- so a server answering ``429 Retry-After: 120`` is
    backed off by ~0.8 s and retried three times, which is the opposite of
    what Retry-After asks for and can extend the limit. Carrying the headers
    out means changing that call's return tuple (consumed in several places)
    and its js/error-utils.js mirror, so it is left for the owner rather than
    changed here. Until then: the function honours Retry-After, and the LLM
    path does not use it.

    AUDIT-2026-10-01: measured, and this note understated the gap in two ways.
    Both matter to whoever picks the refactor up.

      1. The BROWSER is not in the same position. js/minimax.js's direct-mode
         transport surfaces 429/408 and rides ``err.headers`` through
         retryWithBackoff into getRetryDelay (its comment at the throw site
         says so), and a real ``Headers`` is case-insensitive. So the two
         transports DISAGREE today: the same 429 is backed off per the server's
         Retry-After in the browser and per the blind backoff on the desktop.
         This is a live divergence, not a shared limitation.
      2. Carrying the headers out is NECESSARY BUT NOT SUFFICIENT. Two further
         divergences sit inside this function and would survive it. The header
         lookup was case-sensitive over a plain dict (fixed here -- a no-op in
         production while both call sites pass None, and pinned by
         tests/test_error_utils_retry_header_parity.py), and the HTTP-date
         branch still differs: ``parsedate_to_datetime`` accepts only the
         RFC 9110 IMF-fixdate, while js/error-utils.js uses ``new Date()``,
         which also accepts ISO-8601. That one is deliberately left alone --
         accepting a non-spec format is a judgment call, not a mirror fix.

    So the deferral is a THREE-part job, not one: capture the headers, keep the
    lookup case-insensitive, and decide the date format.
    """
    delay: float | None = None

    # AUDIT-2026-10-01: header names are case-INSENSITIVE (RFC 9110 §5.1), and
    # this function receives a plain dict, so the exact-spelling `.get()` pairs
    # it used to do honoured two spellings out of the legal set. Measured
    # against js/error-utils.js: the browser is handed a real `Headers`, whose
    # `.get()` is case-insensitive, so it honoured Retry-After for
    # "retry-after", "Retry-After", "RETRY-AFTER", "retry-After" and
    # "rEtRy-AfTeR" alike, while this side silently fell through to the
    # exponential backoff (1 s floor) for everything except the two spellings
    # listed below -- including "RETRY-AFTER", which plenty of proxies and
    # CDNs emit. One lookup over a lower-cased view of the keys is the whole
    # fix, and it is a no-op in production today: both live call sites pass
    # headers=None (llm.py:2001, extractor.py:2281), so nothing that ships
    # reaches this branch. It matters for the refactor the docstring above
    # defers -- carrying the headers out is not sufficient on its own.
    #
    # The values are looked up as-is, only the KEYS are folded, so a value
    # that happens to look like a header name is unaffected.
    folded = None
    if headers:
        try:
            folded = {str(k).lower(): v for k, v in headers.items()}
        except AttributeError:  # a non-mapping was handed in
            folded = None

    # 1. Check Retry-After-MS header (milliseconds)
    if folded:
        retry_after_ms = folded.get("retry-after-ms")
        if retry_after_ms:
            try:
                delay = float(retry_after_ms) / 1000.0
            except (ValueError, TypeError):
                pass

        # 2. Check Retry-After header (seconds or HTTP date)
        if delay is None:
            retry_after = folded.get("retry-after")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        dt = parsedate_to_datetime(retry_after)
                        delay = max(0.0, (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
                    except Exception:
                        # AUDIT-2026-10-01: measured, and NOT fixed here.
                        # parsedate_to_datetime accepts the RFC 9110 HTTP-date
                        # (IMF-fixdate, "Wed, 21 Oct 2026 07:28:00 GMT") and
                        # nothing else, while js/error-utils.js goes through
                        # `new Date(string)`, which also accepts ISO-8601. So
                        # for `Retry-After: 2099-10-21T07:28:00Z` this side
                        # ignores the header and backs off ~1 s where the
                        # browser waits the requested time. ISO-8601 is not
                        # what RFC 9110 asks for, so accepting it is a
                        # judgment call about gateways and CDNs rather than a
                        # mirror bug, and it is left for the owner. Recorded
                        # as a characterisation test in
                        # tests/test_error_utils_retry_header_parity.py.
                        pass

    # 3. Fall back to exponential backoff with jitter
    if delay is None:
        base_delay = initial_delay * (backoff_factor ** attempt)
        # Add jitter: DSH pattern: multiply by (1 - random * 0.25)
        jitter = 1.0 - random.random() * 0.25
        delay = base_delay * jitter

    # REVIEW-2026-09-20 #108: cap first, then lift to the 1s floor. A
    # ``Retry-After: 0`` (or an already-elapsed HTTP date) is honoured as
    # "no information" rather than as "hammer me again immediately"; a caller
    # that caps below the floor (``max_delay=0``, i.e. the tests that must not
    # sleep) still gets its ceiling because the floor is clamped to it.
    delay = min(delay, max_delay)
    return max(delay, min(MIN_RETRY_DELAY_SECONDS, max_delay))


def retry_with_backoff(
    func: Callable[[], T],
    *,
    max_retries: int = 3,
    initial_delay: float = 0.8,
    backoff_factor: float = 1.6,
    max_delay: float = 60.0,
    retryable: Callable[[T], bool] | None = None,
    on_retry: Callable[[int, float, Exception | None], None] | None = None,
) -> T:
    """Execute a function with exponential backoff retry.

    Args:
        func: The function to execute.
        max_retries: Maximum number of retry attempts.
        initial_delay: Initial delay in seconds.
        backoff_factor: Exponential backoff multiplier.
        max_delay: Maximum delay cap in seconds.
        retryable: Optional predicate to determine if result warrants retry.
            ``None`` means every result is acceptable (return immediately).
        on_retry: Optional callback called before each retry.

    Returns:
        The function's return value. When ``retryable`` keeps returning
        True and the attempts are exhausted without an exception, the LAST
        observed result is returned.

    Raises:
        The last exception if all retries fail and an exception occurred.

    Sprint B (REVIEW-2026-09-04): implemented the documented predicate
    semantics. The previous code returned ``result`` in BOTH branches of
    the check (``if retryable(result): return result`` / ``return result``),
    so a result the predicate deemed retry-worthy never actually triggered
    another attempt — the retryable parameter was dead logic. Now:
    ``retryable(result)`` False -> return the result immediately (final);
    True -> keep retrying until the attempts are exhausted.

    REVIEW-2026-09-20 #109: the loop used to sleep after EVERY iteration that
    did not return, including the LAST attempt when the failure came from the
    ``retryable`` predicate rather than an exception (the exception path
    already ``break``-ed out before the sleep). Callers therefore paid one
    more back-off wait for a retry that was never going to happen — with the
    defaults a 3-retry loop that kept answering "retryable" slept 4 times
    instead of 3. The final attempt now breaks out before the delay is
    computed, so ``on_retry`` and ``sleep`` run exactly once per ACTUAL retry.
    """
    last_exc: Exception | None = None
    last_result: T | None = None

    for attempt in range(max_retries + 1):
        try:
            result = func()
            # Check if result is acceptable (retryable=None means all results
            # are acceptable). A non-retryable result is final, NOT an error:
            # return it directly. A retryable result means "keep trying".
            if retryable is None or not retryable(result):
                return result
            last_result = result
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries:
                break

        if attempt >= max_retries:
            # Last attempt and the result was retryable: nothing follows, so
            # do not sleep once more before returning it.
            break

        # Calculate delay for next retry
        delay = initial_delay * (backoff_factor ** attempt)
        jitter = 1.0 - random.random() * 0.25
        delay *= jitter
        delay = min(delay, max_delay)

        if on_retry:
            on_retry(attempt + 1, delay, last_exc)

        time.sleep(delay)

    # All retries exhausted
    if last_exc is not None:
        raise last_exc
    # No exception occurred but every result was deemed retryable.
    # Return the last observed result (None only if func never returned,
    # which cannot happen without raising).
    return last_result


def retry_http_request(
    request_func: Callable[[], tuple[int | None, bytes | None, bytes | None]],
    *,
    max_retries: int = 3,
    initial_delay: float = 0.8,
    backoff_factor: float = 1.6,
    max_delay: float = 60.0,
    get_headers: Callable[[], dict | None] | None = None,
) -> tuple[int | None, bytes | None, bytes | None]:
    """Retry an HTTP request with smart backoff.

    Specifically designed for LLM API calls. Handles Retry-After headers.
    """
    for attempt in range(max_retries + 1):
        status, body, err_body = request_func()

        # Success
        if err_body is None or len(err_body) == 0:
            if status is None or status < 400:
                return status, body, err_body

        # Check if retryable
        if status is not None and status not in RETRYABLE_STATUS:
            return status, body, err_body

        # Network error - only retry once more
        if status is None and attempt >= 1:
            return status, body, err_body

        if attempt >= max_retries:
            return status, body, err_body

        # Get headers for Retry-After
        headers = get_headers() if get_headers else None

        # Calculate delay
        delay = get_retry_delay(
            status=status,
            headers=headers,
            attempt=attempt,
            initial_delay=initial_delay,
            backoff_factor=backoff_factor,
            max_delay=max_delay,
        )

        # Add retry annotation to error body
        suffix = f"[retry {attempt + 1}/{max_retries + 1} after {delay:.1f}s]"
        if err_body:
            err_body = err_body + suffix.encode("utf-8") if isinstance(err_body, bytes) else (err_body + suffix).encode("utf-8")
        else:
            err_body = suffix.encode("utf-8")

        time.sleep(delay)

    return status, body, err_body
