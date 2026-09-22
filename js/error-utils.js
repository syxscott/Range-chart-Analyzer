// error-utils.js - Error normalization and retry utilities for frontend.
// Inspired by DeepSeek Harness's pi-ai error handling patterns:
// - Unified error normalization across different LLM providers
// - Smart retry with jitter and Retry-After header support
// - Consistent error formatting for user display

'use strict';

// ---------------------------------------------------------------------------
// Error Normalization
// ---------------------------------------------------------------------------

/**
 * @typedef {Object} NormalizedError
 * @property {number|null} status - HTTP status code (null for network errors)
 * @property {string} body - Decoded error body string
 * @property {string} message - Original error message
 * @property {boolean} messageCarriesBody - Whether message already contains body
 * @property {string|null} errorCode - Provider-specific error code
 * @property {string|null} retryHint - "retry" | "retry_with_delay" | "do_not_retry" | null
 */

/** Status codes worth retrying (transient upstream errors). */
const RETRYABLE_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

/** Maximum error body length to store. */
const MAX_ERROR_BODY_CHARS = 2000;

/**
 * REVIEW-2026-09-20 #108 (mirror of rca_core/error_utils.py
 * MIN_RETRY_DELAY_SECONDS): floor for every computed retry delay. A provider
 * legitimately answers a 429 with ``Retry-After: 0`` (or ``Retry-After-Ms: 0``,
 * or an HTTP-date that has already passed), which used to be honoured
 * literally: the retry loop then hammered the endpoint back-to-back with zero
 * spacing, which is exactly what turns a rate limit into a longer BAN.
 * The floor is applied BELOW the ceiling, so a caller that caps under it
 * (``maxDelay = 0``, i.e. the tests that must not sleep) still gets its
 * ceiling — see getRetryDelay().
 */
const MIN_RETRY_DELAY_SECONDS = 1.0;

/**
 * Normalize an HTTP error into a structured form.
 * Handles various error body formats: empty, plain text, JSON, arbitrary bytes.
 * @param {number|null} status - HTTP status code
 * @param {string|null} bodyText - Decoded error body string
 * @param {string} message - Error message from the transport layer
 * @returns {NormalizedError}
 */
function normalizeError(status, bodyText, message) {
    const body = (bodyText || '').substring(0, MAX_ERROR_BODY_CHARS);
    // REVIEW-2026-11-07 (low): test the STORED (truncated) form, not the full
    // decoded text — mirrors _decode_body/normalize_http_error in
    // rca_core/error_utils.py. A message that embeds the full body also
    // embeds its truncated prefix, so this covers both cases.
    const messageCarriesBody = !body || message.indexOf(body) >= 0;
    // REVIEW-2026-09-20 #110: parse exactly what is stored (see
    // extractErrorCode()).
    const errorCode = extractErrorCode(body);
    const retryHint = getRetryHint(status, errorCode);

    return {
        status,
        body,
        message,
        messageCarriesBody,
        errorCode,
        retryHint,
    };
}

/**
 * Try to extract a machine-readable error code from the body.
 *
 * REVIEW-2026-09-20 #110 (mirror of rca_core/error_utils.py
 * _extract_error_code): callers must hand in the body AFTER the
 * MAX_ERROR_BODY_CHARS trim (normalizeError() does that), so the ``[CODE]``
 * prefix the user sees can only ever come from text that is actually
 * displayed — and a huge hostile body costs one bounded parse instead of a
 * second unbounded one. A body truncated mid-JSON simply yields no code,
 * like any other non-JSON error body.
 * @param {string|null} body - Already-truncated error body text
 * @returns {string|null}
 */
function extractErrorCode(body) {
    if (!body) return null;
    try {
        const data = JSON.parse(body);
        if (typeof data === 'object' && data !== null) {
            // Common error code locations
            for (const key of ['error_code', 'code', 'type', 'error.type']) {
                if (key in data) return String(data[key]);
            }
            // Nested error object
            const error = data.error;
            if (typeof error === 'object' && error !== null) {
                for (const key of ['error_code', 'code', 'type']) {
                    if (key in error) return String(error[key]);
                }
            }
        }
    } catch (_e) {
        // Not JSON, ignore
    }
    return null;
}

/**
 * Determine if and how to retry based on status and error code.
 * @param {number|null} status
 * @param {string|null} errorCode
 * @returns {string|null}
 */
function getRetryHint(status, errorCode) {
    if (status === null) return 'retry';  // Network error
    if (status === 429) return 'retry_with_delay';
    if (status === 401 || status === 403 || status === 400) return 'do_not_retry';
    if (status >= 500 && status < 600) return 'retry';
    if (RETRYABLE_STATUS.has(status)) return 'retry';
    return null;
}

/**
 * Check if an error should be retried.
 * @param {NormalizedError} error
 * @returns {boolean}
 */
function isRetryable(error) {
    if (error.status === null) return true;
    return RETRYABLE_STATUS.has(error.status);
}

/**
 * Get the best message for user display.
 * @param {NormalizedError} error
 * @returns {string}
 */
function getDisplayMessage(error) {
    let base;
    if (error.messageCarriesBody || !error.body) {
        base = error.message;
    } else {
        const prefix = error.status !== null ? `HTTP ${error.status}` : 'Network error';
        base = `${prefix}: ${error.body}`;
    }
    if (error.errorCode) {
        base = `[${error.errorCode}] ${base}`;
    }
    return base;
}

/**
 * Format a normalized error for user display.
 * @param {NormalizedError} error
 * @param {string|null} [prefix]
 * @returns {string}
 */
function formatError(error, prefix) {
    const msg = getDisplayMessage(error);
    if (prefix && error.status !== null) {
        return `${prefix} (${error.status}): ${msg}`;
    }
    if (error.status !== null) {
        return `HTTP ${error.status}: ${msg}`;
    }
    return msg;
}

// ---------------------------------------------------------------------------
// Retry with Exponential Backoff + Jitter
// ---------------------------------------------------------------------------

/**
 * Calculate retry delay with exponential backoff and jitter.
 * Respects Retry-After headers when present.
 * Mirrors DSH's provider-retry.js getRetryDelayMs() logic.
 *
 * @param {Object} options
 * @param {number|null} options.status - HTTP status code
 * @param {Headers|Object|null} options.headers - Response headers
 * @param {number} [options.attempt=0] - Current retry attempt (0-indexed)
 * @param {number} [options.initialDelay=0.8] - Initial backoff delay in seconds
 * @param {number} [options.backoffFactor=1.6] - Multiplicative backoff factor
 * @param {number} [options.maxDelay=60.0] - Maximum delay cap in seconds
 * @returns {number} Recommended delay in seconds before next retry
 */
function getRetryDelay({
    status,
    headers = null,
    attempt = 0,
    initialDelay = 0.8,
    backoffFactor = 1.6,
    maxDelay = 60.0,
}) {
    let delay = null;

    // 1. Check Retry-After-MS header (milliseconds)
    if (headers) {
        const retryAfterMs = headers.get && headers.get('retry-after-ms')
            ? headers.get('retry-after-ms')
            : headers['retry-after-ms'];
        if (retryAfterMs) {
            const parsed = parseFloat(retryAfterMs);
            if (!isNaN(parsed)) {
                delay = parsed / 1000.0;
            }
        }

        // 2. Check Retry-After header (seconds or HTTP date)
        if (delay === null) {
            const retryAfter = headers.get && headers.get('retry-after')
                ? headers.get('retry-after')
                : headers['retry-after'];
            if (retryAfter) {
                // Try parsing as float (seconds)
                const parsed = parseFloat(retryAfter);
                if (!isNaN(parsed)) {
                    delay = parsed;
                } else {
                    // Try parsing as HTTP date
                    try {
                        const date = new Date(retryAfter);
                        if (!isNaN(date.getTime())) {
                            const now = Date.now();
                            delay = Math.max(0, (date.getTime() - now) / 1000);
                        }
                    } catch (_e) {
                        // Not a valid date
                    }
                }
            }
        }
    }

    // 3. Fall back to exponential backoff with jitter
    if (delay === null) {
        const baseDelay = initialDelay * Math.pow(backoffFactor, attempt);
        // Add jitter: DSH pattern: multiply by (1 - random * 0.25)
        // This gives 0.75x to 1.0x of the base delay
        const jitter = 1.0 - Math.random() * 0.25;
        delay = baseDelay * jitter;
    }

    // REVIEW-2026-09-20 #108 (mirror of get_retry_delay()): cap first, then
    // lift to the 1 s floor. A ``Retry-After: 0`` (or an already-elapsed HTTP
    // date) is honoured as "no information" rather than as "hammer me again
    // immediately"; a caller that caps below the floor (``maxDelay = 0``, i.e.
    // the tests that must not sleep) still gets its ceiling because the floor
    // itself is clamped to it.
    delay = Math.min(delay, maxDelay);
    return Math.max(delay, Math.min(MIN_RETRY_DELAY_SECONDS, maxDelay));
}

/**
 * Sleep for a given number of milliseconds, respecting an AbortSignal.
 * @param {number} ms
 * @param {AbortSignal|null} signal
 * @returns {Promise<void>}
 */
function abortableSleep(ms, signal) {
    return new Promise((resolve, reject) => {
        if (signal?.aborted) {
            reject(new DOMException('Aborted', 'AbortError'));
            return;
        }
        const onAbort = () => {
            clearTimeout(timeout);
            reject(new DOMException('Aborted', 'AbortError'));
        };
        const timeout = setTimeout(() => {
            signal?.removeEventListener('abort', onAbort);
            resolve();
        }, Math.max(0, ms));
        signal?.addEventListener('abort', onAbort, { once: true });
    });
}

/**
 * Execute a function with exponential backoff retry.
 *
 * @param {Function} func - Async function that returns a result or throws
 * @param {Object} options
 * @param {number} [options.maxRetries=3]
 * @param {number} [options.initialDelay=0.8]
 * @param {number} [options.backoffFactor=1.6]
 * @param {number} [options.maxDelay=60.0]
 * @param {Function|null} [options.retryable=null] - Predicate: (result) => boolean
 * @param {Function|null} [options.onRetry=null] - Callback: (attempt, delay, error)
 * @param {AbortSignal|null} [options.signal=null] - AbortSignal to cancel retries
 * @returns {Promise<*>} The function's return value
 *
 * REVIEW-2026-09-20 #109 (mirror of retry_with_backoff(), and of the
 * `if attempt == retries - 1: return` guard in
 * rca_core/llm.py:call_llm_api_with_retry): the loop must NOT sleep once more
 * after the LAST attempt — previously the "result deemed retryable" path fell
 * through to the delay/sleep block even when no retry could follow, so a
 * 3-retry loop slept 4 times and ``on_retry`` reported a retry that never
 * happened. Both failure paths now ``break`` before the delay is computed, so
 * onRetry() and the sleep run exactly once per ACTUAL retry.
 *
 * The delay itself comes from getRetryDelay(), i.e. it carries the 1 s floor
 * (#108) — matching the two Python NETWORK retry loops (extractor.py
 * `_call_llm` and llm.py `call_llm_api_with_retry`, both of which go through
 * error_utils.get_retry_delay) — which is what this browser path mirrors.
 */
async function retryWithBackoff(func, {
    maxRetries = 3,
    initialDelay = 0.8,
    backoffFactor = 1.6,
    maxDelay = 60.0,
    retryable = null,
    onRetry = null,
    signal = null,
} = {}) {
    let lastError = null;
    let lastResult = null;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
        try {
            const result = await func();
            // Sprint B (REVIEW-2026-09-04): implement the documented
            // predicate semantics (mirrors the parallel fix to Python
            // rca_core/error_utils.retry_with_backoff). Previously BOTH
            // branches returned the result immediately, so the `retryable`
            // predicate was dead code:
            //   - retryable === null            -> accept the result;
            //   - retryable(result) === false   -> result is acceptable,
            //     return it immediately, no further retries;
            //   - retryable(result) === true    -> result warrants retry;
            //     keep retrying until retries are exhausted, then fall
            //     through and return the LAST result (no throw).
            if (retryable === null || !retryable(result)) {
                return result;
            }
            lastResult = result;
            if (attempt >= maxRetries) {
                break;
            }
        } catch (error) {
            lastError = error;
            if (attempt >= maxRetries) {
                break;
            }
        }

        const delay = getRetryDelay({
            status: lastError?.status || null,
            // FE-FIX-2026-09-22 (item 5 companion): transports that throw
            // with `err.headers` (js/minimax.js direct mode now surfaces
            // 429/408 for retry) get their Retry-After / Retry-After-Ms
            // honoured here — previously the loop only forwarded `status`,
            // so getRetryDelay's whole header branch was unreachable from
            // every caller of retryWithBackoff. Errors without headers keep
            // the exponential-backoff fallback (headers: null).
            headers: lastError?.headers || null,
            attempt,
            initialDelay,
            backoffFactor,
            maxDelay,
        });

        if (onRetry) {
            onRetry(attempt + 1, delay, lastError);
        }

        if (signal?.aborted) {
            throw new DOMException('Aborted', 'AbortError');
        }

        await abortableSleep(delay * 1000, signal);
    }

    // All retries exhausted
    if (lastError !== null) {
        throw lastError;
    }
    // The retryable predicate kept refusing every result. Return the last
    // result observed (null when func never produced one), matching the
    // Python docstring: "Returns None if all retries failed without an
    // exception".
    return lastResult;
}

// ---------------------------------------------------------------------------
// HTTP Error Extraction from fetch() Response
// ---------------------------------------------------------------------------

/**
 * Extract error info from a failed fetch() Response.
 * @param {Response} response
 * @returns {Promise<{status: number, body: string}>}
 */
async function extractFetchError(response) {
    let body = '';
    try {
        const text = await response.text();
        // Try to parse as JSON first
        try {
            const json = JSON.parse(text);
            // Extract meaningful error message
            if (json.error?.message) {
                body = json.error.message;
            } else if (json.error?.type) {
                body = json.error.type;
            } else if (json.message) {
                body = json.message;
            } else if (json.error) {
                body = typeof json.error === 'string' ? json.error : JSON.stringify(json.error);
            } else {
                body = text.substring(0, MAX_ERROR_BODY_CHARS);
            }
        } catch (_e) {
            // Not JSON, use raw text
            body = text.substring(0, MAX_ERROR_BODY_CHARS);
        }
    } catch (_e) {
        // Couldn't read body
        body = '';
    }
    return {
        status: response.status,
        body,
    };
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

// Export for use in other modules
window.RCAErrorUtils = {
    normalizeError,
    formatError,
    getDisplayMessage,
    isRetryable,
    getRetryHint,
    getRetryDelay,
    retryWithBackoff,
    abortableSleep,
    extractFetchError,
    // REVIEW-2026-09-20 #110 / #108: exported so the parity tests can pin the
    // truncated-body contract and the delay floor directly.
    extractErrorCode,
    RETRYABLE_STATUS,
    MAX_ERROR_BODY_CHARS,
    MIN_RETRY_DELAY_SECONDS,
};
