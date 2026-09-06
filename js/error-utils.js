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
 * Normalize an HTTP error into a structured form.
 * Handles various error body formats: empty, plain text, JSON, arbitrary bytes.
 * @param {number|null} status - HTTP status code
 * @param {string|null} bodyText - Decoded error body string
 * @param {string} message - Error message from the transport layer
 * @returns {NormalizedError}
 */
function normalizeError(status, bodyText, message) {
    const body = (bodyText || '').substring(0, MAX_ERROR_BODY_CHARS);
    const messageCarriesBody = !body || message.indexOf(body) >= 0;
    const errorCode = extractErrorCode(bodyText);
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
 * @param {string|null} bodyText
 * @returns {string|null}
 */
function extractErrorCode(bodyText) {
    if (!bodyText) return null;
    try {
        const data = JSON.parse(bodyText);
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

    return Math.min(delay, maxDelay);
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
    RETRYABLE_STATUS,
    MAX_ERROR_BODY_CHARS,
};
