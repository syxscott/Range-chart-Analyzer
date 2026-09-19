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
from typing import Callable, TypeVar

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
        """Get the best message for user display."""
        if self.message_carries_body or not self.body:
            base = self.message
        else:
            prefix = f"HTTP {self.status}" if self.status else "Network error"
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
    """Best-effort decode of error body bytes."""
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
                    return str(data[key])
            error = data.get("error", {})
            if isinstance(error, dict):
                for key in ("error_code", "code", "type"):
                    if key in error:
                        return str(error[key])
    except Exception:
        pass
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
    """Format a normalized error for user display."""
    msg = normalized.display_message
    if prefix and normalized.status:
        return f"{prefix} ({normalized.status}): {msg}"
    if normalized.status:
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

    Respects Retry-After headers when present, with smart fallback.
    Mirrors DSH's provider-retry.js getRetryDelayMs() logic.
    """
    delay: float | None = None

    # 1. Check Retry-After-MS header (milliseconds)
    if headers:
        retry_after_ms = headers.get("retry-after-ms") or headers.get("Retry-After-Ms")
        if retry_after_ms:
            try:
                delay = float(retry_after_ms) / 1000.0
            except (ValueError, TypeError):
                pass

        # 2. Check Retry-After header (seconds or HTTP date)
        if delay is None:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        dt = parsedate_to_datetime(retry_after)
                        delay = max(0.0, (dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
                    except Exception:
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
