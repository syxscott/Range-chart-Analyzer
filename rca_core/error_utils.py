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
    """Try to extract a machine-readable error code from the body."""
    if not body_bytes:
        return None
    try:
        data = json.loads(body_bytes.decode("utf-8", errors="replace"))
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

    return min(delay, max_delay)


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
        on_retry: Optional callback called before each retry.

    Returns:
        The function's return value.

    Raises:
        The last exception if all retries fail and an exception occurred.
        Returns None if all retries failed without an exception (should not happen
        in normal use since retryable check should eventually pass or exhaust retries).
    """
    last_exc: Exception | None = None
    last_result: T | None = None

    for attempt in range(max_retries + 1):
        try:
            result = func()
            # Check if result is acceptable (retryable=None means all results are acceptable)
            if retryable is None or retryable(result):
                return result
            # Result is not retryable - this is NOT an error, return it directly
            return result
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries:
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
    # No exception occurred but also no successful result.
    # This happens when retryable never returns True and no exception was raised.
    # Return the last result if available, otherwise return None.
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
