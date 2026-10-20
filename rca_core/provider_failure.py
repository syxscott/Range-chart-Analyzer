"""Classify a provider failure, and say whether retrying can help.

BORROWED (2026-10-20) from sst/opencode's
``packages/ai/src/provider-error.ts`` -- the most careful error
classifier among the four projects compared. It separates the failures a
retry CAN fix from the ones it cannot, and it lets the provider have the
last word.

Why it matters HERE, measured rather than assumed:

* The retryable set was ``{408, 425, 429, 500, 502, 503, 504, 529}`` --
  429 unconditionally. But a 429 can be a THROTTLE (retry after a wait)
  or an EXHAUSTED PLAN (the weekly bucket is empty; retrying cannot help
  and it spends the OTHER bucket). MiniMax's Token Plan publishes both,
  and the weekly one is exactly what throttled the end-to-end
  regression.
* An authentication failure and a context overflow are also out of any
  retry's reach, and we used to spend four attempts discovering that.
* opencode's rule for anything UNRECOGNISED is "retry": a transient
  failure is exactly the one that arrives in a shape no classifier
  anticipated, so an unknown failure retries.

The classification does not change the error MESSAGE -- the provider's
own words are still what the operator reads. It changes the DECISION:
whether to spend another attempt, and how long to wait first.

The vocabulary below is opencode's, narrowed to the vendors this product
actually talks to (Anthropic-compatible, OpenAI-compatible, Gemini) plus
MiniMax's Chinese wording. opencode's table is the accumulated result of
many vendors' phrasing, and the patterns that name a vendor are the
ones worth carrying.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# --------------------------------------------------------------------------
# The categories
# --------------------------------------------------------------------------

#: A throttle: the request was too fast, and a later one will do.
RATE_LIMIT = "rate_limit"
#: The plan is exhausted. Retrying cannot help and spends another bucket.
QUOTA_EXHAUSTED = "quota_exhausted"
#: The request does not fit the model's context. Only a smaller request can.
CONTEXT_OVERFLOW = "context_overflow"
#: The request body is too large for the endpoint.
PAYLOAD_TOO_LARGE = "payload_too_large"
#: The credential is wrong, missing, or not allowed.
AUTH = "auth"
#: The request was refused by a safety or usage policy.
CONTENT_POLICY = "content_policy"
#: The request itself is malformed. Retrying it sends the same mistake.
INVALID_REQUEST = "invalid_request"
#: The provider failed on its own side.
SERVER = "server"
#: The request never reached the provider.
NETWORK = "network"
#: Nothing recognised it.
UNKNOWN = "unknown"

#: A stream that ended before the model said it was finished. The
#: HTTP status is 200 -- the server answered -- but the answer is a
#: PREFIX, and a prefix of a table is a wrong table, not a short one.
INCOMPLETE_STREAM = "incomplete_stream"

#: The categories a retry can fix. Everything else is a request the
#: provider will refuse in exactly the same way next time.
RETRYABLE_CATEGORIES = frozenset({RATE_LIMIT, SERVER, NETWORK, UNKNOWN,
                                  INCOMPLETE_STREAM})

#: The statuses that were retryable before the classifier existed. It
#: lives HERE now so that the two can never drift: the classifier's
#: status fallback is this set, and the retry layers ask the classifier.
#: (llm.py keeps the name ``_RETRYABLE_STATUS`` for the tests that pin
#: it.)
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

#: The marker our own stream reader leaves when the model never finished.
INCOMPLETE_STREAM_MARKER = "the connection ended before the model finished"


@dataclass(frozen=True)
class ProviderFailure:
    """What went wrong, and whether another attempt could help."""

    category: str
    retryable: bool
    code: Optional[str] = None
    reason: str = ""
    #: The provider's own Retry-After, when it sent one.
    retry_after_sec: Optional[float] = None

    @property
    def is_quota(self) -> bool:
        return self.category == QUOTA_EXHAUSTED


# --------------------------------------------------------------------------
# The vendor vocabularies
# --------------------------------------------------------------------------

#: Codes that mean "your plan is out", not "you went too fast".
_QUOTA_CODES = frozenset({
    "insufficient_quota", "usage_not_included", "billing_error",
    "gousagelimiterror", "freeusagelimiterror", "creditlimitexceeded",
    "quota_exceeded", "insufficient_balance", "insufficient_user_quota",
    "insufficient_user_balance", "exceeded_quota", "quota_exhausted",
    "no_quota", "rate_limit_exceeded",
})

#: Codes that mean "the credential is wrong".
_AUTH_CODES = frozenset({
    "authentication_error", "permission_error", "permission_denied",
    "api_key_invalid", "invalid_api_key", "invalid_token", "unauthorized",
    "invalid_authorization",
})

#: Codes that mean "the provider failed, and may not next time".
_SERVER_CODES = frozenset({
    "api_error", "internal_error", "internalserverexception",
    "modelstreamerrorexception", "overloaded_error", "server_error",
    "server_is_overloaded", "slow_down", "serviceunavailableexception",
    "service_unavailable", "overloaded", "busy",
})

#: Codes that mean "the request is malformed".
_INVALID_REQUEST_CODES = frozenset({
    "model_not_found", "invalid_prompt", "invalid_request",
    "invalid_request_error", "validationexception", "invalid_param",
    "invalid_parameter", "invalid_argument",
})

#: Codes that mean "a safety or usage policy refused this".
_CONTENT_POLICY_CODES = frozenset({
    "content_filter", "content_moderation", "responsibleaipolicyviolation",
    "content_policy_violation", "image_content_policy_violation", "refusal",
})

#: How different vendors SAY "the prompt does not fit the context".
_CONTEXT_OVERFLOW_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"prompt is too long",
    r"input is too long for requested model",
    r"exceeded this model context window limit",
    r"exceeds the context window",
    r"exceeds (?:the )?(?:model'?s )?maximum context length",
    r"input token count.*exceeds the maximum",
    r"number of input tokens exceeds maximum length",
    r"tokens in request more than max tokens allowed",
    r"maximum prompt length is \d+",
    r"reduce the length of the messages",
    r"maximum context length is \d+ tokens",
    r"input length.*exceeds.*context length",
    r"context window exceeds limit",
    r"exceeded model token limit",
    r"context[_ ]length[_ ]exceeded",
    r"too large for model with \d+ maximum context length",
    r"model_context_window_exceeded",
    r"range of input length should be",
    r"token limit exceeded",
    # Anthropic's own wording.
    r"prompt is too long: \d+ tokens > \d+ maximum",
    # MiniMax's wording, measured on 2026-10-20.
    r"input length exceeds",
    # Chinese, because MiniMax answers in it.
    r"超长|超出.*上下文|超过.*最大",
))

#: How vendors SAY "the body is too large".
_PAYLOAD_TOO_LARGE_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"request entity too large",
    r"payload too large",
    r"request too large",
))

#: How vendors SAY "you went too fast". Checked BEFORE the quota
#: patterns, because a throttling message is also a kind of "limit".
_RATE_LIMIT_TEXT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"rate increased too quickly",
    r"rate[-_\s]?limit",
    r"too[_\s]?many[_\s]?requests",
    r"请求过于频繁",
    r"限流",
))

#: How vendors SAY "the plan is out". MiniMax is a Chinese vendor and
#: answers in Chinese as readily as in English.
_QUOTA_TEXT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"insufficient[-_\s]?(?:quota|balance)",
    r"quota[-_\s]?exceeded",
    r"budget exceeded",
    r"usage limit",
    r"limit exhausted",
    r"package has expired",
    r"plan does not yet include",
    r"credit balance is not enough",
    r"your credit balance is too low",
    r"账户余额不足",
    r"额度不足",
    r"配额不足",
    r"套餐.*(?:已用尽|已过期|不支持)",
    r"余额不足",
))

#: How vendors SAY "the credential is wrong".
_AUTH_TEXT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"incorrect api key",
    r"invalid api key",
    r"unauthorized",
    r"authentication",
    r"密钥.*(?:无效|错误|不正确)",
    r"未授权|鉴权失败",
))

#: How vendors SAY "the provider failed on its own side".
_SERVER_TEXT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\b(?:try again|(?:please |you can )?retry (?:the |this |your )?request"
    r"|temporarily unavailable|overloaded|service[-_\s]?unavailable"
    r"|(?:server|internal)[-_\s]?error|server (?:is )?busy"
    r"|resource[-_\s]?exhausted|upstream (?:connect|connection|request))\b",
    r"服务(?:暂时)?不可用|服务器(?:内部)?错误|过载",
))

#: A gateway that rewrote the upstream code keeps the original as a
#: ``[code]`` label at the start of the message.
_GATEWAY_CODE_LABEL = re.compile(r"^[^:\n]+: \[([A-Za-z0-9_.\-]+)\]")


def _fold(headers: Optional[dict]) -> dict:
    """Header names are case-insensitive (RFC 9110 section 5.1)."""
    if not headers:
        return {}
    try:
        return {str(k).lower(): v for k, v in headers.items()}
    except AttributeError:
        return {}


def _all_codes(body: Optional[str]) -> list[str]:
    """Every machine code in the body, lowercased.

    The body's shape differs per vendor -- ``{"error": {"code": ...}}``,
    ``{"code": ...}``, MiniMax's ``{"base_resp": {"status_code": N}}`` --
    so this COLLECTS rather than picks, and the caller matches a set. A
    single code is not always the decisive one: a gateway may put its own
    code first and the upstream's second.
    """
    out: list[str] = []
    if not body:
        return out
    try:
        data = json.loads(body)
    except Exception:
        # Not JSON: the message may still carry a code in brackets.
        m = _GATEWAY_CODE_LABEL.match(body.strip())
        if m:
            out.append(m.group(1).lower())
        return out
    if not isinstance(data, dict):
        return out

    def _add(value: Any) -> None:
        if isinstance(value, str) and value:
            out.append(value.lower())
        elif isinstance(value, int) and not isinstance(value, bool):
            out.append(str(value))

    for key in ("code", "error_code", "type"):
        if key in data:
            _add(data[key])
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("code", "error_code", "type", "status"):
            if key in err:
                _add(err[key])
    # MiniMax's envelope.
    base = data.get("base_resp")
    if isinstance(base, dict):
        _add(base.get("status_code"))
        msg = base.get("status_msg")
        if isinstance(msg, str):
            out.append(msg.lower())
    return out


def _message_text(body: Optional[str]) -> str:
    """The human-readable part of the body, whatever shape it has."""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except Exception:
        return body
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "status_msg", "detail"):
            val = err.get(key)
            if isinstance(val, str) and val:
                return val
    base = data.get("base_resp")
    if isinstance(base, dict):
        val = base.get("status_msg")
        if isinstance(val, str) and val:
            return val
    for key in ("message", "detail", "status_msg"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _retry_after_from(headers: Optional[dict]) -> Optional[float]:
    """The provider's own Retry-After, in seconds."""
    folded = _fold(headers)
    raw = folded.get("retry-after-ms")
    if raw:
        try:
            return float(raw) / 1000.0
        except (TypeError, ValueError):
            pass
    raw = folded.get("retry-after")
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    return None


def classify_provider_failure(status: Optional[int], body: Optional[str] = None,
                              headers: Optional[dict] = None) -> ProviderFailure:
    """Classify one failed call, and say whether another attempt can help.

    ``status`` is the HTTP status, or None for a network failure.
    ``body`` is the response body (already truncated, as stored).
    ``headers`` are the response headers, if the caller has them.

    The order of the checks matters and is the whole point:

    1. ``x-should-retry`` -- the provider's own verdict, if it sent one.
    2. The CODE, when the vendor sent a typed one. A code is stronger
       evidence than a status: a 429 with ``insufficient_quota`` is a
       dead plan, and a 400 with ``invalid_request`` is a malformed
       request.
    3. The MESSAGE, for the vendors that only say it in prose.
    4. The STATUS, as the last resort.
    """
    folded = _fold(headers)
    codes = _all_codes(body)
    text = _message_text(body) or (body or "")

    # 1. The provider's own verdict.
    override = folded.get("x-should-retry")
    if override is not None:
        want = str(override).strip().lower()
        if want in ("true", "1", "yes"):
            return ProviderFailure(UNKNOWN, True, None, "x-should-retry: true",
                                   _retry_after_from(headers))
        if want in ("false", "0", "no"):
            return ProviderFailure(UNKNOWN, False, None, "x-should-retry: false",
                                   _retry_after_from(headers))

    code = codes[0] if codes else None
    code_set = set(codes)
    retry_after = _retry_after_from(headers)

    # 2. A typed code.
    if code_set & _QUOTA_CODES:
        return ProviderFailure(QUOTA_EXHAUSTED, False, code,
                                "the plan is exhausted", retry_after)
    if code_set & _AUTH_CODES:
        return ProviderFailure(AUTH, False, code,
                                "authentication failed", retry_after)
    if code_set & _CONTENT_POLICY_CODES:
        return ProviderFailure(CONTENT_POLICY, False, code,
                                "refused by policy", retry_after)
    # REVIEW-2026-10-20: the context and payload checks come BEFORE the
    # generic "the request is malformed" code, because a message that says
    # "the prompt does not fit" is MORE SPECIFIC than a code that says
    # "invalid_request". Anthropic sends `invalid_request_error` for a
    # prompt that is too long, and the generic code would have had the
    # last word -- and the wrong one: a context overflow and a malformed
    # request need different answers from the operator.
    if any(p.search(text) for p in _CONTEXT_OVERFLOW_PATTERNS):
        return ProviderFailure(CONTEXT_OVERFLOW, False, code,
                                "the request does not fit the context",
                                retry_after)
    if any(p.search(text) for p in _PAYLOAD_TOO_LARGE_PATTERNS):
        return ProviderFailure(PAYLOAD_TOO_LARGE, False, code,
                                "the request body is too large", retry_after)
    if code_set & _SERVER_CODES:
        return ProviderFailure(SERVER, True, code,
                                "the provider failed", retry_after)
    if code_set & _INVALID_REQUEST_CODES:
        return ProviderFailure(INVALID_REQUEST, False, code,
                                "the request is malformed", retry_after)

    # 3. The message, for the vendors that only say it in prose.
    if any(p.search(text) for p in _RATE_LIMIT_TEXT_PATTERNS):
        return ProviderFailure(RATE_LIMIT, True, code, "throttled", retry_after)
    if any(p.search(text) for p in _QUOTA_TEXT_PATTERNS):
        return ProviderFailure(QUOTA_EXHAUSTED, False, code,
                                "the plan is exhausted", retry_after)
    if any(p.search(text) for p in _AUTH_TEXT_PATTERNS):
        return ProviderFailure(AUTH, False, code,
                                "authentication failed", retry_after)
    if any(p.search(text) for p in _SERVER_TEXT_PATTERNS):
        return ProviderFailure(SERVER, True, code,
                                "the provider failed", retry_after)

    # 4. Our OWN marker for a stream that never finished. The status is
    # 200 -- the server answered -- so the status fallback would call
    # this a success, and a prefix of the answer is not an answer.
    if INCOMPLETE_STREAM_MARKER in text:
        return ProviderFailure(INCOMPLETE_STREAM, True, code,
                                "the model stopped mid-answer", retry_after)

    # 5. The status, as the last resort.
    if status is None:
        return ProviderFailure(NETWORK, True, code,
                                "the request never arrived", retry_after)
    if status in (401, 403):
        return ProviderFailure(AUTH, False, code,
                                "authentication failed", retry_after)
    if status == 402:
        return ProviderFailure(QUOTA_EXHAUSTED, False, code,
                                "the plan is exhausted", retry_after)
    if status == 413:
        return ProviderFailure(PAYLOAD_TOO_LARGE, False, code,
                                "the request body is too large", retry_after)
    if status == 429:
        return ProviderFailure(RATE_LIMIT, True, code, "throttled", retry_after)
    if 400 <= status < 500:
        if status in (408, 425):
            return ProviderFailure(RATE_LIMIT, True, code, "transient",
                                   retry_after)
        return ProviderFailure(INVALID_REQUEST, False, code,
                                "the request is malformed", retry_after)
    if status in RETRYABLE_STATUS:
        return ProviderFailure(SERVER, True, code,
                                "the provider failed", retry_after)
    # opencode's rule: an unrecognised failure retries, because the
    # transient failures are exactly the ones that arrive in shapes no
    # classifier anticipated.
    return ProviderFailure(UNKNOWN, True, code, "unrecognised failure",
                           retry_after)
