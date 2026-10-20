"""Multi-LLM provider abstraction.

Providers carry the credentials + wire-format needed to talk to a variety of
LLM services. A provider's ``api_format`` selects one of the supported
transports (Anthropic / OpenAI / Gemini), each of which is implemented as a
single Python function. New formats are added by extending the ``ApiFormat``
enum and dispatching in ``call_llm_api``.

Preset catalog
--------------
``PROVIDER_PRESETS`` ships a curated list of common LLM endpoints ( Anthropic,
OpenAI, Gemini, MiniMax, DeepSeek, Qwen, Kimi, Zhipu, OpenRouter, Together,
Groq, SiliconFlow, Mistral, Anthropic-compatible aggregators, etc.). Each preset
is a template ``LlmProvider`` — the user picks one, fills in an API key, and the
provider is ready to use.

Storage
-------
``ProviderStore`` persists a list of user-configured providers to
``~/.range_chart_analyzer/providers.json``. The active provider is identified by
``current_id`` (single-active-per-app, mirroring cc-switch's ``is_current`` flag).
Atomic write (tmp + rename) guards against half-written files on crash.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional

# AUDIT-2026-09-29: shared with server.py on purpose -- see
# rca_core/redact.py for why a second copy of that pattern is a liability.
from .provider_failure import (RETRYABLE_STATUS, classify_provider_failure)
from .redact import redact_error_body

from .ssrf import (
    _NoRedirect,
    make_pinning_opener,
)


class LLMCallError(Exception):
    """Raised when an LLM API call fails due to SSRF validation or other call-level errors."""
    pass

# Local imports kept lazy so this module can be imported on Python versions
# or in environments that don't have the rest of rca_core available.
def _usage_helpers():
    from .usage import estimate_tokens, parse_usage
    return estimate_tokens, parse_usage
def estimate_tokens(text: str) -> int:
    """Forwarded to rca_core.usage.estimate_tokens; indirection keeps
    this module importable in isolation (e.g. for some unit tests)."""
    fn, _ = _usage_helpers()
    return fn(text)
def parse_usage(payload, fmt_hint: str = ""):
    fn, p = _usage_helpers()
    return p(payload, fmt_hint)

# ---------------------------------------------------------------------------
# API format
# ---------------------------------------------------------------------------


class ApiFormat(str, Enum):
    """Wire-format / auth scheme for a provider."""

    ANTHROPIC = "anthropic"  # /v1/messages, x-api-key + anthropic-version
    OPENAI = "openai"  # /v1/chat/completions, Authorization: Bearer
    GEMINI = "gemini"  # :generateContent, key in query or header


# ---------------------------------------------------------------------------
# Reasoning effort
# ---------------------------------------------------------------------------

#: The reasoning levels the UI offers, weakest first.
#:
#: The SET is universal and the per-model map says which of them a given model
#: accepts -- the shape cc-switch uses (src/config/piThinkingProfiles.ts).
#: That shape is strictly more expressive than a per-model list of options,
#: because it can say two things a list cannot:
#:
#:   * that a model accepts only SOME of the levels, and
#:   * that a level's NAME on the wire differs from its name in the UI --
#:     cc-switch's Gemini profile maps ``low`` to ``"LOW"`` and ``high`` to
#:     ``"HIGH"``, and a per-model option list has no way to say that.
#:
#: ``default`` is ours rather than cc-switch's: it is the "let the provider
#: decide" row, and minimax-code's catalogue puts it FIRST in M3.1-Flash's
#: option list -- a level the user cannot see is a level the user cannot
#: reason about.
REASONING_LEVELS = ("default", "off", "on", "minimal", "low", "medium", "high",
                    "xhigh", "max")

#: The concrete effort names -- the levels that carry a DEPTH. ``default``,
#: ``off`` and ``minimal`` are not depths: the first means "let the provider
#: decide", the second "do not think" and the third "think a little". Kept as
#: a public name because the GUI and the JS mirror both need the list of
#: names a provider can carry.
REASONING_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: The "let the provider decide" level.
REASONING_DEFAULT = "default"

#: The map value that means "this level is offered, but sends nothing".
#:
#: cc-switch's maps do not need this: every level there maps to a concrete
#: wire value, and the levels it does not support are simply absent. We need
#: it because MiniMax's catalogue has a "default" row, and the provider's own
#: default is its MAXIMUM effort -- which is the configuration that times out
#: on a dense figure -- so "default" must be distinguishable from every
#: concrete level.
REASONING_OMIT = ""

#: How a model wants its thinking depth expressed on the wire.
#:
#:   * ``effort``  -- a named level: ``output_config: {effort: <level>}``.
#:   * ``switch`` -- an on/off: ``thinking: {type: adaptive|disabled}``.
#:
#: These are different MECHANISMS, not two spellings of one: OpenRouter's
#: Messages API rejects ``thinking.type=enabled`` without an integer
#: ``budget_tokens``, and its Claude 4.6 migration note says adaptive and the
#: budget mode are not interchangeable (ZCode's
#: docs/openrouter-reasoning-request-contract.md). minimax-code sends
#: ``thinking: {type: 'adaptive'}`` ALONGSIDE ``output_config.effort`` on the
#: Anthropic-compatible endpoint.
REASONING_STYLE_EFFORT = "effort"
REASONING_STYLE_SWITCH = "switch"


@dataclass(frozen=True)
class ReasoningProfile:
    """One model's reasoning options.

    The levels belong to the MODEL, not to the program. minimax-code declares
    them per model in its catalog and its TUI asks the runtime for the list
    rather than hard-coding one; the resolution rule below is a transcription
    of that runtime's ``resolveTuiEffortChoice``.
    """

    #: UI level -> the value that goes on the wire.
    #:
    #: A MISSING key means "this profile says nothing about that level"; a
    #: ``None`` value means "the provider does NOT support it" and the level
    #: is not offered. The distinction is deliberate, and it is cc-switch's.
    map: Mapping[str, str | None]
    #: The level the model uses when the user has not chosen.
    default: str
    #: ``effort`` (a named level) or ``switch`` (thinking on / off).
    style: str

    def levels(self) -> list[str]:
        """The levels to OFFER, in the universal order.

        A level whose wire value is ``None`` is not offered: showing a control
        the provider would reject (or silently ignore) is worse than not
        showing it.
        """
        return [lv for lv in REASONING_LEVELS
                if lv in self.map and self.map[lv] is not None]

    def wire_value(self, level: str) -> str | None:
        """The wire value of ``level``, or ``None`` when it is unsupported."""
        if level not in self.map:
            return None
        return self.map[level]


#: The per-model reasoning catalogue.
#:
#: M3.1 takes the five named levels, and "off"/"minimal" are NOT among them --
#: M3.1 always thinks. M3 does NOT take an effort at all: its thinking is an
#: on/off switch, and -- as minimax-code puts it -- "Responses effort values
#: do not change thinking depth", so offering M3 an effort list would
#: advertise a control that does nothing.
REASONING_PROFILES: dict[str, ReasoningProfile] = {
    "MiniMax-M3.1-Flash": ReasoningProfile(
        map={
            REASONING_DEFAULT: REASONING_OMIT,
            "low": "low", "medium": "medium", "high": "high",
            "xhigh": "xhigh", "max": "max",
        },
        default=REASONING_DEFAULT,
        style=REASONING_STYLE_EFFORT),
    "MiniMax-M3.1-Flash-Preview": ReasoningProfile(
        map={
            REASONING_DEFAULT: REASONING_OMIT,
            "low": "low", "medium": "medium", "high": "high",
            "xhigh": "xhigh", "max": "max",
        },
        default=REASONING_DEFAULT,
        style=REASONING_STYLE_EFFORT),
    "MiniMax-M3": ReasoningProfile(
        map={REASONING_DEFAULT: REASONING_OMIT, "on": "on", "off": "off"},
        default=REASONING_DEFAULT,
        style=REASONING_STYLE_SWITCH),
}


def reasoning_profile_for(model: object) -> ReasoningProfile | None:
    """The reasoning profile of ``model``, or ``None`` when it takes none.

    The lookup is case- and prefix-tolerant on purpose: the catalogue names
    ``MiniMax-M3.1-Flash`` while users type ``minimax-m3.1-flash`` and the
    preview suffix, and a model that quietly falls back to "no reasoning
    control" because of a capital letter is a model whose thinking depth
    silently reverted to the vendor default -- the configuration that times
    out.
    """
    if not isinstance(model, str):
        return None
    name = model.strip().lower()
    if not name:
        return None
    if name in REASONING_PROFILES:
        return REASONING_PROFILES[name]
    for key, profile in REASONING_PROFILES.items():
        if name.startswith(key.lower()):
            return profile
    return None


def reasoning_options_for(model: object) -> list[str]:
    """The levels the GUI may offer for ``model`` (empty when it takes none)."""
    profile = reasoning_profile_for(model)
    return profile.levels() if profile else []


def resolve_reasoning_choice(model: object, selected: object = None) -> str:
    """Resolve the level to USE for ``model``, or ``""`` for "send nothing".

    This is minimax-code's ``resolveTuiEffortChoice``, transcribed:

    * a model with no profile gets nothing -- there is no control to offer;
    * a valid stored choice wins;
    * the catalogue's DEFAULT wins over the midpoint, and can be fixed without
      editable options;
    * without a default, an even-length list uses the HIGHER midpoint, so a
      two-choice on/off model stays enabled until the user says otherwise.

    The returned value is a UI level NAME. What goes on the wire is that
    level's MAPPED value, which may be a different string entirely.
    """
    profile = reasoning_profile_for(model)
    if profile is None:
        return ""
    allowed = profile.levels()
    if isinstance(selected, str):
        trimmed = selected.strip()
        if trimmed and trimmed in allowed:
            return trimmed
    if profile.default in allowed:
        return profile.default
    if not allowed:
        return ""
    return allowed[len(allowed) // 2]


def normalize_reasoning_effort(value: object) -> str:
    """Return a usable effort level, or ``""`` for "send nothing".

    A provider record is user-editable JSON, so this field can hold anything.
    Sending an unknown level upstream risks a 400 that would take the whole
    extraction down, and a typo must never turn into a request. An
    unrecognised value is therefore DROPPED, not passed through -- the
    request then behaves exactly as it did before this field existed, which
    is the safe failure: the model applies its own default and the run
    succeeds or fails for its own reasons.
    """
    if not isinstance(value, str):
        return ""
    v = value.strip().lower()
    return v if v in REASONING_LEVELS else ""


def _reasoning_request_patch(provider: "LlmProvider",
                             api_format: "ApiFormat") -> dict[str, Any]:
    """The request fields that carry this provider's reasoning choice.

    minimax-code's ``resolveModelThinkingProtocol`` does this per API format
    and so does this:

    * ``anthropic-messages`` -- ``thinking: {type: 'adaptive'}`` PLUS
      ``output_config: {effort: <level>}``. The ``thinking`` block is not
      decoration: OpenRouter's Messages API rejects ``thinking.type=enabled``
      without an integer ``budget_tokens``, and its Claude 4.6 migration note
      says adaptive and the budget mode are DIFFERENT mechanisms
      (ZCode's ``docs/openrouter-reasoning-request-contract.md``,
      2026-09-14). ``adaptive`` is the one that needs no budget.
    * ``openai-completions`` -- ``reasoning_effort: <level>`` at the top level.
    * ``openai-responses`` -- ``reasoning: {effort: <level>}``.

    The user's LEVEL goes on the wire as the level's MAPPED value, which is
    not always the same string: cc-switch maps Gemini's ``low`` to ``"LOW"``
    and its ``high`` to ``"HIGH"``, and a provider that takes only some of
    the levels says so with a ``None`` in its map.

    A model with no profile sends NOTHING. A level the model does not
    understand is a 400, and a provider that takes no reasoning control must
    behave exactly as it did before this field existed.
    """
    profile = reasoning_profile_for(getattr(provider, "model", ""))
    if profile is None:
        return {}
    choice = resolve_reasoning_choice(
        getattr(provider, "model", ""),
        getattr(provider, "reasoning_effort", "") or "")
    if not choice:
        return {}
    wire = profile.wire_value(choice)
    if wire is None:
        # The level is not in the profile (or is mapped to None): sending
        # it would be a guess, and a guess here is a 400.
        return {}
    if profile.style == REASONING_STYLE_SWITCH:
        # M3's thinking is an on/off switch, not a depth: minimax-code puts
        # it in the wire as `thinking: {type: adaptive|disabled}` and notes
        # that "Responses effort values do not change thinking depth".
        on = wire == "on"
        if api_format == ApiFormat.ANTHROPIC:
            return {"thinking": {"type": "adaptive" if on else "disabled"}}
        if api_format == ApiFormat.OPENAI:
            return {"reasoning_effort": "high" if on else "none"}
        return {"reasoning": {"effort": "high" if on else "none"}}
    if wire == REASONING_OMIT:
        # "default" means "let the provider decide" -- so nothing is sent.
        # (For MiniMax's M3.1 the provider's own default is `max`, which is
        # the slow one; the GUI says so next to the option.)
        return {}
    if api_format == ApiFormat.ANTHROPIC:
        return {"thinking": {"type": "adaptive"},
                "output_config": {"effort": wire}}
    if api_format == ApiFormat.OPENAI:
        return {"reasoning_effort": wire}
    return {"reasoning": {"effort": wire}}


# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelMetadata:
    """What a model can do, so the GUI can say it instead of guessing.

    cc-switch fills a model's metadata in when it is picked
    (``modelMetadataFill``) and shows the context window next to the model
    field (``GptContextWindowHint``). It matters here for a measurable
    reason: MiniMax-M3.1-Flash answers a larger ``max_tokens`` with
    ``does not support max tokens > 524288``, and a user who types 1000000
    gets a 400 instead of a clear limit. The ceiling belongs on screen.
    """

    #: The largest ``max_tokens`` the model accepts.
    max_output: int = 0
    #: The model's context window, in tokens.
    context_window: int = 0
    #: A short, human note.
    note: str = ""


#: Per-model metadata. The values are sourced, not guessed:
#:
#: * ``max_output`` for M3.1-Flash is 524288 -- MEASURED: the endpoint
#:   rejects a larger value with "does not support max tokens > 524288
#:   (2013)".
#: * the context windows are the vendor's published figures, the same ones
#:   cc-switch's catalogue carries.
#:
#: The ``note`` is an i18n KEY, not a sentence: the GUI has three
#: languages and a Chinese note in an English interface is a defect. The
#: keys live in the i18n table as ``model.noteThinking`` and
#: ``model.noteThinkingSwitch``; the GUI falls back to the raw string when
#: a key is missing, so a catalogue entry is never silently blank.
MODEL_METADATA: dict[str, ModelMetadata] = {
    "MiniMax-M3.1-Flash": ModelMetadata(
        max_output=524288, context_window=1000000,
        note="model.noteThinking"),
    "MiniMax-M3.1-Flash-Preview": ModelMetadata(
        max_output=524288, context_window=1000000,
        note="model.noteThinking"),
    "MiniMax-M3": ModelMetadata(
        max_output=128000, context_window=512000,
        note="model.noteThinkingSwitch"),
    "MiniMax-M2.7": ModelMetadata(max_output=128000, context_window=200000),
    "MiniMax-M2.5": ModelMetadata(max_output=128000, context_window=200000),
}


def model_metadata_for(model: object) -> ModelMetadata | None:
    """The metadata of ``model``, or ``None`` when it is not catalogued."""
    if not isinstance(model, str):
        return None
    name = model.strip().lower()
    if not name:
        return None
    if name in MODEL_METADATA:
        return MODEL_METADATA[name]
    for key, meta in MODEL_METADATA.items():
        if name.startswith(key.lower()):
            return meta
    return None


# ---------------------------------------------------------------------------
# Quota / balance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaTier:
    """One window of a plan's quota (a 5-hour bucket, a weekly bucket)."""

    #: ``"five_hour"`` / ``"weekly_limit"`` -- the tiers cc-switch shows.
    name: str
    #: How MUCH of the window is used, in percent (0-100).
    used_percent: float
    #: When the window resets, in epoch milliseconds.
    resets_at: Optional[int] = None
    #: For a BALANCE account: the amount left, the total, and the unit.
    remaining: Optional[float] = None
    total: Optional[float] = None
    used: Optional[float] = None
    unit: str = "%"


@dataclass(frozen=True)
class QuotaResult:
    """The outcome of one quota query.

    ``ok`` distinguishes a DETERMINISTIC failure (no plan, bad key, unknown
    provider) from a transient one, the way cc-switch's two error channels do:
    the first is shown with its reason, the second is retried.
    """

    ok: bool
    tiers: list[QuotaTier] = field(default_factory=list)
    error: str = ""
    #: Epoch milliseconds of the query.
    queried_at: int = 0
    #: The provider's own words, when it had any (a plan name, a level).
    detail: str = ""


#: MiniMax's programming-plan quota endpoint. The route and the response
#: shape were confirmed against our own Token Plan key (2026-10-20): HTTP 200
#: with ``model_remains`` and a ``base_resp`` status.
_MINIMAX_CODING_PLAN_URL = (
    "https://api.minimaxi.com/v1/api/openplatform/coding_plan/remains")


def _parse_minimax_plan_tiers(payload: dict[str, Any]) -> list[QuotaTier]:
    """MiniMax's plan response -> quota tiers.

    MEASURED shape (our own key, 2026-10-20)::

        {"model_remains": [{"model_name": "general",
                             "current_interval_remaining_percent": 99,
                             "current_interval_status": 1,
                             "current_weekly_status": 1,
                             "current_weekly_remaining_percent": 82,
                             "end_time": 1792479600000,
                             "weekly_end_time": 1792944000000}, ...],
         "base_resp": {"status_code": 0, "status_msg": "success"}}

    Only the ``general`` model is the programming plan; ``video`` and the
    rest are not, so they are skipped. The weekly bucket exists only when
    ``current_weekly_status == 1`` -- on a plan without a weekly limit the
    field is 3 and the remaining percent is a constant 100, which would
    render a fake "0% used" bucket.
    """
    tiers: list[QuotaTier] = []
    items = payload.get("model_remains")
    if not isinstance(items, list):
        return tiers
    item = next((x for x in items
                 if isinstance(x, dict) and x.get("model_name") == "general"),
                None)
    if item is None:
        return tiers
    remaining = _as_float(item.get("current_interval_remaining_percent"))
    if remaining is not None:
        tiers.append(QuotaTier(
            name="five_hour",
            used_percent=100.0 - remaining,
            resets_at=_as_int(item.get("end_time")),
        ))
    if item.get("current_weekly_status") == 1:
        weekly = _as_float(item.get("current_weekly_remaining_percent"))
        if weekly is not None:
            tiers.append(QuotaTier(
                name="weekly_limit",
                used_percent=100.0 - weekly,
                resets_at=_as_int(item.get("weekly_end_time")),
            ))
    return tiers


def _as_float(value: Any) -> Optional[float]:
    """A number that may have arrived as a string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> Optional[int]:
    """A number that may have arrived as a string."""
    f = _as_float(value)
    return int(f) if f is not None else None


def _quota_get_json(url: str, headers: dict[str, str],
                    timeout_sec: int) -> tuple[Optional[dict], Optional[int], str]:
    """GET a quota endpoint. Returns (payload, status, error).

    The error string distinguishes the two failure kinds cc-switch separates:
    a transport failure (timeout, refused) is TRANSIENT and worth retrying,
    and anything that came back with a status is DETERMINISTIC.
    """
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read(200000)
            status = resp.status
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(4000)
        except Exception:
            pass
        return None, e.code, ("HTTP %d: %s" % (e.code, _decode_err_body(body)[:300]))
    except TimeoutError as e:
        return None, None, "timeout: %s" % e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        return None, None, ("network: %s" % (reason or e))
    except Exception as e:
        return None, None, "%s: %s" % (type(e).__name__, e)
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as e:
        return None, status, "could not parse the response: %s" % e
    if not isinstance(payload, dict):
        return None, status, "the response was not a JSON object"
    return payload, status, ""


def query_coding_plan_quota(provider: LlmProvider,
                            timeout_sec: int = 15) -> QuotaResult:
    """The plan / balance of a provider, in the tiers the GUI shows.

    MiniMax's programming plan is the one this product's users actually
    have, and its route was confirmed against a live Token Plan key
    (2026-10-20). The account-balance providers below are the ones with a
    public route and a documented shape.

    A provider with no known quota route returns ``ok=False`` with a
    reason -- that is a DETERMINISTIC answer ("this provider does not
    publish a quota"), not a failure to try again.
    """
    now = int(time.time() * 1000)
    if provider is None or not (provider.api_key or "").strip():
        return QuotaResult(ok=False, error="no API key", queried_at=now)
    endpoint = (provider.endpoint or "").lower()
    key = provider.api_key

    if "minimaxi.com" in endpoint or "minimax.io" in endpoint:
        host = "api.minimax.io" if "minimax.io" in endpoint else "api.minimaxi.com"
        url = "https://%s/v1/api/openplatform/coding_plan/remains" % host
        payload, status, err = _quota_get_json(
            url, {"Authorization": "Bearer %s" % key,
                  "Accept": "application/json"}, timeout_sec)
        if payload is None:
            if status in (401, 403):
                return QuotaResult(ok=False, tiers=[], error=err or "auth",
                                   queried_at=now)
            return QuotaResult(ok=False, error=err or "query failed",
                               queried_at=now)
        base = payload.get("base_resp")
        if isinstance(base, dict):
            code = base.get("status_code")
            if code not in (None, 0):
                return QuotaResult(
                    ok=False,
                    error="API error (%s): %s" % (code, base.get("status_msg") or ""),
                    queried_at=now)
        return QuotaResult(ok=True,
                           tiers=_parse_minimax_plan_tiers(payload),
                           queried_at=now)

    if "openrouter.ai" in endpoint:
        payload, status, err = _quota_get_json(
            "https://openrouter.ai/api/v1/credits",
            {"Authorization": "Bearer %s" % key, "Accept": "application/json"},
            timeout_sec)
        if payload is None:
            return QuotaResult(ok=False, error=err or "query failed",
                               queried_at=now)
        data = payload.get("data") if isinstance(payload.get("data"), dict) \
            else payload
        credits = _as_float(data.get("total_credits"))
        usage = _as_float(data.get("total_usage"))
        if credits is None or usage is None:
            return QuotaResult(ok=False, error="the response had no credit "
                                               "amounts", queried_at=now)
        return QuotaResult(ok=True, queried_at=now, tiers=[QuotaTier(
            name="balance", used_percent=0.0, remaining=credits - usage,
            total=credits, used=usage, unit="USD")])

    if "api.deepseek.com" in endpoint:
        payload, status, err = _quota_get_json(
            "https://api.deepseek.com/user/balance",
            {"Authorization": "Bearer %s" % key, "Accept": "application/json"},
            timeout_sec)
        if payload is None:
            return QuotaResult(ok=False, error=err or "query failed",
                               queried_at=now)
        tiers: list[QuotaTier] = []
        infos = payload.get("balance_infos")
        if isinstance(infos, list):
            for info in infos:
                if not isinstance(info, dict):
                    continue
                total = _as_float(info.get("total_balance"))
                if total is None:
                    continue
                tiers.append(QuotaTier(
                    name=str(info.get("currency") or "CNY"),
                    used_percent=0.0, remaining=total, unit="CNY"))
            # An account can list a CNY and a USD wallet; the empty one is
            # noise, and cc-switch drops it for the same reason.
            if any(t.remaining is not None and t.remaining > 0 for t in tiers):
                tiers = [t for t in tiers
                         if t.remaining is None or t.remaining > 0]
        return QuotaResult(ok=True, tiers=tiers, queried_at=now)

    return QuotaResult(ok=False, error="this provider publishes no quota",
                       queried_at=now)


def measure_endpoint_latency(provider: LlmProvider,
                             timeout_sec: int = 10) -> tuple[Optional[int],
                                                            Optional[int], str]:
    """How long the endpoint takes to answer, in milliseconds.

    Returns ``(latency_ms, status, error)``. The latency is measured even
    when the answer is an error: a 404 from a gateway that does not list
    models still says the endpoint is alive and how far away it is, which is
    what the speed test is for. Only a transport failure has no latency.
    """
    if provider is None or not (provider.endpoint or "").strip():
        return None, None, "no endpoint"
    # A cheap GET: the model listing, which every OpenAI-compatible gateway
    # serves and which costs nothing. The latency is what matters here --
    # whether it answers 200 or 404 says the endpoint is alive either way.
    if getattr(provider, "full_url", False):
        target = (provider.endpoint or "").strip()
    elif provider.api_format == ApiFormat.GEMINI:
        target = _gemini_path(_api_base(provider.endpoint), "/v1beta/models")
    else:
        target = _endpoint_path(_api_base(provider.endpoint), "/v1/models")
    headers = {"Accept": "application/json"}
    if (provider.api_key or "").strip():
        if provider.api_format == ApiFormat.ANTHROPIC:
            headers["x-api-key"] = provider.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = "Bearer %s" % provider.api_key
    headers.update(provider.extra_headers or {})
    req = urllib.request.Request(target, method="GET", headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            resp.read(2048)
            dt = int((time.perf_counter() - t0) * 1000)
            return dt, resp.status, ""
    except urllib.error.HTTPError as e:
        dt = int((time.perf_counter() - t0) * 1000)
        try:
            e.read(2048)
        except Exception:
            pass
        return dt, e.code, "HTTP %d" % e.code
    except Exception as e:
        return None, None, "%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------------------
# Provider dataclass
# ---------------------------------------------------------------------------


@dataclass
class LlmProvider:
    """A single configured LLM endpoint.

    Mirrors the structure of cc-switch's ``Provider`` record but flattened for
    our single-app use case. ``extra_headers`` and ``extra_body`` let advanced
    users accommodate proxies or non-standard gateways.
    """

    id: str = ""
    name: str = "New provider"
    api_format: ApiFormat = ApiFormat.ANTHROPIC
    endpoint: str = ""
    # AUDIT-2026-09-27 P2: repr=False. As a plain dataclass field this was
    # rendered IN CLEARTEXT by repr()/str() -- measured, and the neighbouring
    # `_unreadable_envelope` below already uses repr=False for the same
    # reason, so the omission was an oversight rather than a decision. No code
    # in the repo formats a provider into a log or an exception today (an AST
    # scan of every logging/print call found none), so nothing depended on
    # the key being visible; it is one debug line or one traceback away from
    # writing a live credential into a log file.
    #
    # to_dict()/from_dict() are unaffected: dataclass fields are still
    # compared, serialised and loaded, so persistence and equality are
    # unchanged. Only the diagnostic rendering loses the value.
    api_key: str = field(default="", repr=False)
    model: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    # REVIEW-2026-10-19: reasoning effort for the MiniMax M3.1 family.
    #
    # MiniMax's M3.1 ALWAYS thinks and its effort level DEFAULTS TO ``max``;
    # thinking tokens are billed against and CONSUME ``max_tokens``. Measured
    # on the reference figure (777x543, 22x18, 93 filled cells):
    #
    #   MiniMax-M3          (thinking off by default)  label call 6.5-13.1 s
    #   MiniMax-M3.1-Flash (effort=max, the default) label call TIMED OUT at
    #                     90 s, 2 of 2
    #   MiniMax-M3.1-Flash (effort=low)               label call 28.3 s, and
    #                     the answer was complete (22 rows / 18 columns / 3
    #                     legend entries)
    #
    # The failure mode is not obvious from the outside: with max_tokens=2000
    # the call returned HTTP 200 in 23.3 s with stop_reason=max_tokens and an
    # EMPTY body, because the thinking had eaten the whole budget before the
    # model wrote a single character of the answer. Raising max_tokens does
    # not help (the thinking is unbounded), and lowering the effort does.
    #
    # The shape is ``output_config: {"effort": ...}`` on the Anthropic-
    # compatible endpoint -- NOT ``thinking: {budget_tokens: N}``, which is
    # not in M3.1's documented vocabulary. Empty string means "send nothing"
    # and is the right default for every other provider and for M3 itself.
    reasoning_effort: str = ""
    is_current: bool = False
    created_at: float = 0.0
    sort_index: int = 0
    # Consecutive connection-test failures (0..3+). Persisted so the
    # health badge survives an app restart — the previous version kept
    # this only on the in-memory ProviderCard widget, so a user who
    # saw a ⚠ before quitting saw a clean ✓ on next launch and might
    # mistake a broken endpoint for a healthy one. Capped at 3 by the
    # GUI (the badge displays ✗, ✗2, ✗3+) so any value above 3 is fine
    # here but not very informative.
    consecutive_failures: int = 0

    # ---- REVIEW-2026-10-20: the cc-switch borrowings ----------------------
    #
    # A free-text note on the provider: what the account is for, which plan
    # it is on, when it expires. cc-switch shows it on the card and makes it
    # searchable, which is what makes a list of five relay accounts usable
    # instead of a list of five opaque names.
    notes: str = ""

    # Extra endpoints for THIS provider, offered by the speed test. The
    # built-in endpoint stays the first candidate; the rest are alternates
    # the user measured and kept.
    custom_endpoints: list[str] = field(default_factory=list)

    # The endpoint is a FULL request URL, used verbatim, instead of a prefix
    # that the canonical ``/v1/...`` path is appended to. Only gateways with
    # a non-standard path need it -- cc-switch gates it behind the same
    # warning.
    full_url: bool = False

    # The profile this provider belongs to. A profile is a named GROUP of
    # providers ("work", "personal"), and switching a profile switches the
    # active provider and shows only that group's cards.
    profile: str = ""

    # A custom glyph for the provider's card icon, overriding the two-letter
    # abbreviation derived from the name. cc-switch lets the user upload a
    # provider logo; a desktop app with no asset pipeline takes the same
    # idea as ONE GLYPH, which needs no file, no path portability question
    # and no image decoder -- and is what a user with five relay accounts
    # actually needs (a coloured emoji, not five grey "MM" tiles).
    # Empty means "derive it from the name", which is the default.
    icon: str = ""

    # AUDIT-2026-09-27 P1: the ``fer:v1:``/``obf:v1:`` envelope of a key this
    # process could NOT decrypt, kept verbatim so ``to_dict`` can write it back
    # unchanged. Not a dataclass field: it is runtime provenance about the
    # envelope, never user data, and it must not be serialised as a key of its
    # own. ``api_key`` is "" in this state, so the UI prompts for re-entry, but
    # the ciphertext survives every ``ProviderStore.save()`` in the meantime.
    _unreadable_envelope: str = field(default="", repr=False, compare=False)

    @property
    def needs_key_reentry(self) -> bool:
        """True when a stored key exists but could not be decrypted this launch.

        The GUI shows a re-entry prompt; it must NOT have already overwritten
        the stored key by the time it does.
        """
        return bool(self._unreadable_envelope)

    def __repr__(self) -> str:
        """Render the provider WITHOUT its credentials.

        AUDIT-2026-09-27 P2: ``api_key`` alone was made ``repr=False``, but
        this class's docstring invites the opposite leak --
        ``extra_headers`` exists "to accommodate proxies or non-standard
        gateways", which is exactly where a user puts
        ``Authorization: Bearer ...``, and ``extra_body`` is free-form and
        routinely carries another ``api_key``. Measured: repr() rendered all
        three in cleartext while to_dict() encrypted only the first.

        The header and body NAMES are still shown, because the useful part of
        a diagnostic is knowing WHICH proxy headers are set; only the values
        are masked. Masking selectively -- by whether the header NAME looks
        secret-ish -- would hand the safety of a credential to a name the
        user chose, and "X-Whatever" is a perfectly good place to put a token.

        A dataclass-generated __repr__ would otherwise win; dataclasses
        ._set_new_attribute does not touch an attribute the class body
        already defines, so this one is kept.
        """
        def _masked(d: Any) -> Any:
            if not isinstance(d, dict):
                return d
            return {k: ("***" if v not in (None, "") else v)
                    for k, v in d.items()}

        return (
            f"{type(self).__name__}(id={self.id!r}, name={self.name!r}, "
            f"api_format={self.api_format!r}, endpoint={self.endpoint!r}, "
            f"api_key={('***' if self.api_key else '')!r}, model={self.model!r}, "
            f"extra_headers={_masked(self.extra_headers)!r}, "
            f"extra_body={_masked(self.extra_body)!r}, "
            f"is_current={self.is_current!r}, "
            f"created_at={self.created_at!r}, sort_index={self.sort_index!r}, "
            f"consecutive_failures={self.consecutive_failures!r})"
        )

    def __post_init__(self) -> None:
        if isinstance(self.api_format, str):
            try:
                self.api_format = ApiFormat(self.api_format)
            except ValueError:
                self.api_format = ApiFormat.ANTHROPIC
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = time.time()

    @property
    def display_label(self) -> str:
        fmt = self.api_format.value if isinstance(self.api_format, ApiFormat) else str(self.api_format)
        return f"{self.name}  ·  {self.model or '-'}  ·  {fmt}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["api_format"] = self.api_format.value
        # The unreadable-envelope marker is runtime provenance, not stored state.
        d.pop("_unreadable_envelope", None)
        # P2-2 (REVIEW-2026-07-25): obfuscate the API key at rest.
        from rca_core.secrets_store import encrypt
        if d.get("api_key"):
            d["api_key"] = encrypt(d["api_key"])
        elif self._unreadable_envelope:
            # AUDIT-2026-09-27 P1: a key we could not decrypt must be written
            # back BYTE-FOR-BYTE. This branch used to be absent, so `save()`
            # replaced an undecryptable `fer:v1:` envelope with "" and the
            # user's paid credential was destroyed the first time anything
            # touched the store - switching the current provider was enough.
            d["api_key"] = self._unreadable_envelope
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LlmProvider":
        from rca_core.secrets_store import decrypt_or_none, is_obfuscated
        d = dict(d)
        fmt_raw = d.get("api_format", "anthropic")
        try:
            fmt = ApiFormat(fmt_raw)
        except ValueError:
            fmt = ApiFormat.ANTHROPIC
        d.pop("api_format", None)
        # P2-2 (REVIEW-2026-07-25): decode obfuscated key (or pass
        # legacy plaintext through untouched).
        raw_key = d.get("api_key") or ""
        unreadable = ""
        if raw_key and is_obfuscated(raw_key):
            plain = decrypt_or_none(raw_key)
            if plain is None:
                # AUDIT-2026-09-27 P1: keep the envelope so ``to_dict`` can
                # round-trip it. Blank in memory so nothing tries to call the
                # API with ciphertext, and flag it so the user is prompted.
                d["api_key"] = ""
                unreadable = raw_key
            else:
                d["api_key"] = plain
        elif raw_key:
            d["api_key"] = raw_key
        provider = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        provider._unreadable_envelope = unreadable
        provider.api_format = fmt
        return provider


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@dataclass
class ProviderPreset:
    """Template for a new provider — presented in the preset grid.

    Inspired by cc-switch's provider preset design with additional fields
    for API key URLs and backup endpoint candidates.
    """

    name: str
    api_format: ApiFormat
    endpoint: str
    model: str
    category: str = "official"  # official | cn_official | aggregator | third_party
    doc_url: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    api_key_hint: str = "API Key"
    # cc-switch inspired: direct link to get API key
    api_key_url: str = ""
    # cc-switch inspired: backup endpoint candidates for failover
    endpoint_candidates: list[str] = field(default_factory=list)


def _p(
    name: str,
    fmt: ApiFormat,
    endpoint: str,
    model: str,
    *,
    category: str = "official",
    doc_url: str = "",
    extra_headers: dict[str, str] | None = None,
    api_key_hint: str = "API Key",
    api_key_url: str = "",
    endpoint_candidates: list[str] | None = None,
) -> ProviderPreset:
    return ProviderPreset(
        name=name,
        api_format=fmt,
        endpoint=endpoint,
        model=model,
        category=category,
        doc_url=doc_url,
        extra_headers=extra_headers or {},
        api_key_hint=api_key_hint,
        api_key_url=api_key_url,
        endpoint_candidates=endpoint_candidates or [],
    )


PROVIDER_PRESETS: list[ProviderPreset] = [
    # --- Anthropic format: official + aggregators ---
    _p("Claude Official", ApiFormat.ANTHROPIC,
       "https://api.anthropic.com", "claude-opus-4-1",
       category="official",
       doc_url="https://console.anthropic.com/settings/keys",
       api_key_url="https://console.anthropic.com/settings/keys",
       api_key_hint="sk-ant-..."),
    # DSH verified: minimaxi.com for CN, minimax.io for international
    _p("MiniMax M3", ApiFormat.ANTHROPIC,
       "https://api.minimaxi.com/anthropic", "MiniMax-M3",
       category="cn_official",
       doc_url="https://platform.minimaxi.com/user-center/payment/token-plan",
       api_key_url="https://platform.minimaxi.com/subscribe/coding-plan",
       api_key_hint="MiniMax API Key"),
    _p("Shengsuanyun (神算云)", ApiFormat.ANTHROPIC,
       "https://api.shengsuanyun.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.shengsuanyun.com", api_key_hint="..."),
    _p("PatewayAI", ApiFormat.ANTHROPIC,
       "https://api.patewayai.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.patewayai.com", api_key_hint="..."),
    _p("Huoshan Agentplan", ApiFormat.ANTHROPIC,
       "https://ark.cn-beijing.volces.com/api/v3", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://console.volcengine.com/ark", api_key_hint="..."),
    _p("BytePlus", ApiFormat.ANTHROPIC,
       "https://ark.ap-southeast-1.bytepluses.com/api/v3", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://console.byteplus.com", api_key_hint="..."),
    _p("DouBaoSeed", ApiFormat.ANTHROPIC,
       "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-1-6-250615",
       category="cn_official",
       doc_url="https://console.volcengine.com/ark", api_key_hint="..."),
    _p("CCSub", ApiFormat.ANTHROPIC,
       "https://api.ccsub.io", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://ccsub.io/dashboard", api_key_hint="ccs-..."),
    _p("SubRouter", ApiFormat.ANTHROPIC,
       "https://api.subrouter.net", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://subrouter.net", api_key_hint="..."),
    _p("Unity2.ai", ApiFormat.ANTHROPIC,
       "https://api.unity2.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://unity2.ai", api_key_hint="..."),
    # cc-switch verified endpoint: api.qnaigc.com (not api.qiniu.com)
    _p("Qiniu (七牛)", ApiFormat.ANTHROPIC,
       "https://api.qnaigc.com", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://portal.qiniu.com",
       api_key_url="https://portal.qiniu.com",
       endpoint_candidates=["https://api.qnaigc.com", "https://api.modelink.ai"]),
    _p("FennoAI", ApiFormat.ANTHROPIC,
       "https://api.fenno.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://fenno.ai", api_key_hint="..."),
    _p("ZetaAPI", ApiFormat.ANTHROPIC,
       "https://api.zetaapi.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://zetaapi.com", api_key_hint="..."),
    _p("TeamoRouter", ApiFormat.ANTHROPIC,
       "https://api.teamorouter.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://teamorouter.com", api_key_hint="..."),
    _p("Amux", ApiFormat.ANTHROPIC,
       "https://api.amux.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://amux.ai", api_key_hint="..."),
    _p("PackyCode", ApiFormat.ANTHROPIC,
       "https://api.packycode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.packycode.com/dashboard", api_key_hint="pk-..."),
    _p("CherryIN (樱鹿)", ApiFormat.ANTHROPIC,
       "https://api.cherryin.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cherryin.ai", api_key_hint="..."),
    # cc-switch verified: SiliconFlow has two endpoints
    _p("SiliconFlow (硅基流动)", ApiFormat.ANTHROPIC,
       "https://api.siliconflow.cn", "Pro/MiniMaxAI/MiniMax-M2.7",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak",
       api_key_url="https://cloud.siliconflow.cn/account/ak",
       endpoint_candidates=["https://api.siliconflow.cn", "https://api.siliconflow.com"],
       api_key_hint="sk-..."),
    _p("DMXAPI", ApiFormat.ANTHROPIC,
       "https://api.dmxapi.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://api.dmxapi.cn", api_key_hint="..."),
    _p("APIKEY.FUN", ApiFormat.ANTHROPIC,
       "https://apikey.fun", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apikey.fun", api_key_hint="..."),
    _p("APINebula", ApiFormat.ANTHROPIC,
       "https://api.apinebula.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apinebula.com", api_key_hint="..."),
    _p("AtlasCloud", ApiFormat.ANTHROPIC,
       "https://api.atlascloud.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://atlascloud.ai", api_key_hint="..."),
    _p("SudoCode", ApiFormat.ANTHROPIC,
       "https://api.sudocode.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sudocode.ai", api_key_hint="..."),
    _p("ClaudeAPI", ApiFormat.ANTHROPIC,
       "https://api.claudeapi.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://claudeapi.com", api_key_hint="..."),
    _p("Code0", ApiFormat.ANTHROPIC,
       "https://api.code0.tech", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://code0.tech", api_key_hint="..."),
    _p("NekoCode", ApiFormat.ANTHROPIC,
       "https://api.nekocode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://nekocode.com", api_key_hint="..."),
    _p("ClaudeCN", ApiFormat.ANTHROPIC,
       "https://api.claude-cn.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://claude-cn.com", api_key_hint="..."),
    _p("RunAPI", ApiFormat.ANTHROPIC,
       "https://api.runapi.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://runapi.com", api_key_hint="..."),
    _p("RelaxyCode", ApiFormat.ANTHROPIC,
       "https://api.relaxycode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://relaxycode.com", api_key_hint="..."),
    _p("Cubence", ApiFormat.ANTHROPIC,
       "https://api.cubence.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cubence.com", api_key_hint="..."),
    _p("AIGoCode", ApiFormat.ANTHROPIC,
       "https://api.aigocode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aigocode.com", api_key_hint="..."),
    _p("RightCode", ApiFormat.ANTHROPIC,
       "https://api.rightcode.cn", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://rightcode.cn", api_key_hint="..."),
    _p("AICodeMirror", ApiFormat.ANTHROPIC,
       "https://api.aicodemirror.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aicodemirror.com", api_key_hint="..."),
    _p("CrazyRouter", ApiFormat.ANTHROPIC,
       "https://api.crazyrouter.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://crazyrouter.com", api_key_hint="..."),
    _p("SSSAiCode", ApiFormat.ANTHROPIC,
       "https://api.sssaicode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sssaicode.com", api_key_hint="..."),
    _p("Compshare (超算)", ApiFormat.ANTHROPIC,
       "https://api.compshare.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Compshare Coding Plan", ApiFormat.ANTHROPIC,
       "https://api.compshare.cn", "claude-codex-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Micu", ApiFormat.ANTHROPIC,
       "https://api.micu.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://micu.ai", api_key_hint="..."),
    _p("ETok.ai", ApiFormat.ANTHROPIC,
       "https://api.etok.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://etok.ai", api_key_hint="..."),
    _p("E-FlowCode", ApiFormat.ANTHROPIC,
       "https://api.eflowcode.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://eflowcode.com", api_key_hint="..."),
    _p("OpenRouter", ApiFormat.ANTHROPIC,
       "https://openrouter.ai/api", "anthropic/claude-sonnet-4",
       category="aggregator",
       doc_url="https://openrouter.ai/settings/keys",
       api_key_hint="sk-or-..."),
    _p("TheRouter", ApiFormat.ANTHROPIC,
       "https://api.therouter.io", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://therouter.io", api_key_hint="..."),
    _p("Novita AI", ApiFormat.ANTHROPIC,
       "https://api.novita.ai", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://novita.ai", api_key_hint="..."),
    _p("PIPELLM", ApiFormat.ANTHROPIC,
       "https://api.pipellm.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://pipellm.com", api_key_hint="..."),
    # cc-switch verified
    _p("Longcat (龙猫)", ApiFormat.ANTHROPIC,
       "https://api.longcat.ai/anthropic", "LongCat-2.0",
       category="cn_official",
       doc_url="https://longcat.ai",
       api_key_url="https://longcat.chat/platform/api_keys",
       api_key_hint="..."),
    _p("BaiLing (百灵)", ApiFormat.ANTHROPIC,
       "https://api.bailing.ai", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://bailing.ai", api_key_hint="..."),
    _p("AiHubMix", ApiFormat.ANTHROPIC,
       "https://api.aihubmix.com", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aihubmix.com", api_key_hint="..."),
    _p("KAT-Coder", ApiFormat.ANTHROPIC,
       "https://api.katcoder.com", "claude-sonnet-4-20250514",
       category="specialty",
       doc_url="https://katcoder.com", api_key_hint="..."),
    _p("Xiaomi MiMo (小米)", ApiFormat.ANTHROPIC,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("Xiaomi MiMo Token Plan (CN)", ApiFormat.ANTHROPIC,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    # cc-switch verified: kimi-k2.7-code is the correct coding model
    _p("Kimi", ApiFormat.ANTHROPIC,
       "https://api.moonshot.cn/anthropic", "kimi-k2.7-code",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys",
       api_key_url="https://platform.kimi.com?aff=cc-switch",
       api_key_hint="sk-..."),
    _p("Kimi For Coding", ApiFormat.ANTHROPIC,
       "https://api.moonshot.cn/anthropic", "kimi-k2.7-code",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys",
       api_key_url="https://www.kimi.com/code/?aff=cc-switch",
       api_key_hint="sk-..."),

    # --- OpenAI format (Codex file entries) ---
    _p("OpenAI Official", ApiFormat.OPENAI,
       "https://api.openai.com/v1", "gpt-4o",
       category="official",
       doc_url="https://platform.openai.com/api-keys",
       api_key_url="https://platform.openai.com/api-keys",
       api_key_hint="sk-..."),
    # cc-switch verified: use deepseek-v4-pro as the default model
    _p("DeepSeek", ApiFormat.OPENAI,
       "https://api.deepseek.com/v1", "deepseek-chat",
       category="cn_official",
       doc_url="https://platform.deepseek.com/api_keys",
       api_key_url="https://platform.deepseek.com",
       api_key_hint="sk-..."),
    # cc-switch verified
    _p("Zhipu GLM (智谱)", ApiFormat.OPENAI,
       "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus",
       category="cn_official",
       doc_url="https://open.bigmodel.cn/usercenter/apikeys",
       api_key_url="https://open.bigmodel.cn/claude-code?ic=RRVJPB5SII",
       api_key_hint="<public>.<secret>"),
    _p("Zhipu GLM en", ApiFormat.OPENAI,
       "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus",
       category="cn_official",
       doc_url="https://open.bigmodel.cn/usercenter/apikeys",
       api_key_hint="<public>.<secret>"),
    _p("Baidu Qianfan (千帆)", ApiFormat.OPENAI,
       "https://qianfan.baidubce.com/v2", "ernie-4.0-8k",
       category="cn_official",
       doc_url="https://console.bce.baidu.com/qianfan/ais", api_key_hint="..."),
    _p("Bailian (百炼)", ApiFormat.OPENAI,
       "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max",
       category="cn_official",
       doc_url="https://bailian.console.aliyun.com/", api_key_hint="sk-..."),
    _p("Bailian For Coding", ApiFormat.OPENAI,
       "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-coder-plus",
       category="cn_official",
       doc_url="https://bailian.console.aliyun.com/", api_key_hint="sk-..."),
    _p("Kimi / Moonshot (月之暗面)", ApiFormat.OPENAI,
       "https://api.moonshot.cn/v1", "moonshot-v1-auto",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),
    _p("Kimi For Coding", ApiFormat.OPENAI,
       "https://api.moonshot.cn/v1", "kimi-k2-turbo-preview",
       category="cn_official",
       doc_url="https://platform.moonshot.cn/console/api-keys", api_key_hint="sk-..."),
    _p("StepFun (阶跃星辰)", ApiFormat.OPENAI,
       "https://api.stepfun.com/v1", "step-1-8k",
       category="cn_official",
       doc_url="https://platform.stepfun.com", api_key_hint="..."),
    _p("StepFun en", ApiFormat.OPENAI,
       "https://api.stepfun.com/v1", "step-2-8k",
       category="cn_official",
       doc_url="https://platform.stepfun.com", api_key_hint="..."),
    _p("ModelScope (魔搭)", ApiFormat.OPENAI,
       "https://api-inference.modelscope.cn/v1", "qwen2.5-72b-instruct",
       category="cn_official",
       doc_url="https://modelscope.cn", api_key_hint="..."),
    _p("豆包 / Doubao (Coding Plan)", ApiFormat.OPENAI,
       "https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k",
       category="cn_official",
       doc_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey", api_key_hint="..."),
    _p("BaiLing (百灵)", ApiFormat.OPENAI,
       "https://api.bailing.ai", "doubao-pro-32k",
       category="cn_official",
       doc_url="https://bailing.ai", api_key_hint="..."),
    _p("Xiaomi MiMo (小米)", ApiFormat.OPENAI,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("Xiaomi MiMo Token Plan (CN)", ApiFormat.OPENAI,
       "https://api.xiaomimimo.com", "mimo-v2",
       category="cn_official",
       doc_url="https://xiaomimimo.com", api_key_hint="..."),
    _p("SiliconFlow (硅基流动)", ApiFormat.OPENAI,
       "https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("SiliconFlow en", ApiFormat.OPENAI,
       "https://api.siliconflow.cn/v1", "meta-llama/Llama-3.3-70B-Instruct",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("Novita AI", ApiFormat.OPENAI,
       "https://api.novita.ai/v3", "meta-llama/Llama-3.3-70B-Instruct",
       category="third_party",
       doc_url="https://novita.ai", api_key_hint="..."),
    _p("Nvidia (NIM)", ApiFormat.OPENAI,
       "https://integrate.api.nvidia.com/v1", "meta/llama-3.1-70b-instruct",
       category="third_party",
       doc_url="https://build.nvidia.com", api_key_hint="nvapi-..."),
    _p("OpenCode Go", ApiFormat.OPENAI,
       "https://api.opencode.ai/v1", "claude-sonnet-4-20250514",
       category="third_party",
       doc_url="https://opencode.ai", api_key_hint="..."),
    _p("AiHubMix", ApiFormat.OPENAI,
       "https://api.aihubmix.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aihubmix.com", api_key_hint="..."),
    _p("CherryIN (樱鹿)", ApiFormat.OPENAI,
       "https://api.cherryin.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cherryin.ai", api_key_hint="..."),
    _p("DMXAPI", ApiFormat.OPENAI,
       "https://api.dmxapi.cn", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://api.dmxapi.cn", api_key_hint="..."),
    _p("PackyCode", ApiFormat.OPENAI,
       "https://api.packycode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://www.packycode.com/dashboard", api_key_hint="pk-..."),
    _p("APIKEY.FUN", ApiFormat.OPENAI,
       "https://apikey.fun/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apikey.fun", api_key_hint="..."),
    _p("APINebula", ApiFormat.OPENAI,
       "https://api.apinebula.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://apinebula.com", api_key_hint="..."),
    _p("AtlasCloud", ApiFormat.OPENAI,
       "https://api.atlascloud.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://atlascloud.ai", api_key_hint="..."),
    _p("SudoCode", ApiFormat.OPENAI,
       "https://api.sudocode.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sudocode.ai", api_key_hint="..."),
    _p("ClaudeCN", ApiFormat.OPENAI,
       "https://api.claude-cn.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://claude-cn.com", api_key_hint="..."),
    _p("RunAPI", ApiFormat.OPENAI,
       "https://api.runapi.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://runapi.com", api_key_hint="..."),
    _p("RelaxyCode", ApiFormat.OPENAI,
       "https://api.relaxycode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://relaxycode.com", api_key_hint="..."),
    _p("Cubence", ApiFormat.OPENAI,
       "https://api.cubence.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://cubence.com", api_key_hint="..."),
    _p("AIGoCode", ApiFormat.OPENAI,
       "https://api.aigocode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aigocode.com", api_key_hint="..."),
    _p("RightCode", ApiFormat.OPENAI,
       "https://api.rightcode.cn/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://rightcode.cn", api_key_hint="..."),
    _p("AICodeMirror", ApiFormat.OPENAI,
       "https://api.aicodemirror.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://aicodemirror.com", api_key_hint="..."),
    _p("CrazyRouter", ApiFormat.OPENAI,
       "https://api.crazyrouter.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://crazyrouter.com", api_key_hint="..."),
    _p("SSSAiCode", ApiFormat.OPENAI,
       "https://api.sssaicode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://sssaicode.com", api_key_hint="..."),
    _p("Compshare (超算)", ApiFormat.OPENAI,
       "https://api.compshare.cn/v1", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Compshare Coding Plan", ApiFormat.OPENAI,
       "https://api.compshare.cn/v1", "claude-codex-4-20250514",
       category="cn_official",
       doc_url="https://www.compshare.cn", api_key_hint="..."),
    _p("Micu", ApiFormat.OPENAI,
       "https://api.micu.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://micu.ai", api_key_hint="..."),
    _p("ETok.ai", ApiFormat.OPENAI,
       "https://api.etok.ai/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://etok.ai", api_key_hint="..."),
    _p("E-FlowCode", ApiFormat.OPENAI,
       "https://api.eflowcode.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://eflowcode.com", api_key_hint="..."),
    _p("PIPELLM", ApiFormat.OPENAI,
       "https://api.pipellm.com/v1", "claude-sonnet-4-20250514",
       category="aggregator",
       doc_url="https://pipellm.com", api_key_hint="..."),
    _p("OpenRouter", ApiFormat.OPENAI,
       "https://openrouter.ai/api/v1", "gpt-4o",
       category="aggregator",
       doc_url="https://openrouter.ai/settings/keys",
       api_key_hint="sk-or-..."),
    _p("TheRouter", ApiFormat.OPENAI,
       "https://api.therouter.io/v1", "gpt-4o",
       category="aggregator",
       doc_url="https://therouter.io", api_key_hint="..."),
    _p("Together AI", ApiFormat.OPENAI,
       "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo",
       category="third_party",
       doc_url="https://api.together.xyz/settings/api-keys", api_key_hint="..."),
    _p("Groq", ApiFormat.OPENAI,
       "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
       category="third_party",
       doc_url="https://console.groq.com/keys", api_key_hint="gsk-..."),
    _p("SiliconFlow 硅基流动", ApiFormat.OPENAI,
       "https://api.siliconflow.cn/v1", "Qwen/Qwen3-8B",
       category="cn_official",
       doc_url="https://cloud.siliconflow.cn/account/ak", api_key_hint="sk-..."),
    _p("Mistral", ApiFormat.OPENAI,
       "https://api.mistral.ai/v1", "mistral-large-latest",
       category="official",
       doc_url="https://console.mistral.ai/api-keys/", api_key_hint="..."),
    _p("通义千问 / Qwen", ApiFormat.OPENAI,
       "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max",
       category="cn_official",
       doc_url="https://dashscope.console.aliyun.com/apiKey", api_key_hint="sk-..."),
    _p("Longcat (龙猫)", ApiFormat.OPENAI,
       "https://api.longcat.ai/v1", "claude-sonnet-4-20250514",
       category="cn_official",
       doc_url="https://longcat.ai", api_key_hint="..."),

    # --- Gemini format ---
    _p("Google Gemini", ApiFormat.GEMINI,
       "https://generativelanguage.googleapis.com", "gemini-2.5-pro",
       category="official",
       doc_url="https://aistudio.google.com/app/apikey", api_key_hint="..."),
    _p("Gemini Native (Anthropic)", ApiFormat.ANTHROPIC,
       "https://generativelanguage.googleapis.com/v1beta", "claude-sonnet-4-20250514",
       category="specialty",
       doc_url="https://aistudio.google.com/app/apikey",
       api_key_hint="Set x-api-key=<token> in extra_headers"),
    _p("Gemini via SubRouter", ApiFormat.GEMINI,
       "https://gemini.subrouter.net", "gemini-2.5-pro",
       category="aggregator",
       doc_url="https://subrouter.net", api_key_hint="..."),
    _p("Gemini via Unity2.ai", ApiFormat.GEMINI,
       "https://api.unity2.ai", "gemini-2.5-pro",
       category="aggregator",
       doc_url="https://unity2.ai", api_key_hint="..."),
    _p("Gemini Native (Custom)", ApiFormat.GEMINI,
       "https://generativelanguage.googleapis.com", "gemini-2.5-flash",
       category="official",
       doc_url="https://aistudio.google.com/app/apikey", api_key_hint="..."),
]

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _default_store_path() -> str:
    """Where the provider store lives, unless the environment says otherwise.

    REVIEW-2026-10-20: the ``RCA_PROVIDER_STORE`` override exists because
    of a real accident. A probe script tried to redirect the store by
    replacing the module attribute ``_default_store_path`` -- and that
    does NOTHING, because ``ProviderStore.path`` is declared as
    ``field(default_factory=_default_store_path)`` and the factory is
    BOUND AT CLASS-CREATION TIME. The class kept pointing at the original
    function, so the probe silently wrote to the USER'S REAL store, and
    emptied it: five providers, two of them paid MiniMax keys. The only
    recovery was a backup that had been taken by hand the day before.

    So: the redirect is an ENVIRONMENT VARIABLE, which cannot be defeated
    by a binding subtlety, and the store backs itself up on every change
    (see ``ProviderStore._backup_before_replace``) so the same accident is
    recoverable from the store itself.

    The override is also the supported way to isolate a test or a probe
    from the real store -- the CI workflow already isolates the HOME
    directory on Linux for the same reason.
    """
    override = (os.environ.get("RCA_PROVIDER_STORE") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    return os.path.join(base, "providers.json")


def _chmod_user_only(path: str) -> None:
    """Restrict a file to owner read/write (0600) on POSIX. No-op on
    Windows (ACLs are inherited from the home directory).

    Bug-9 fix: ``providers.json`` contains plaintext API keys; if the
    file is created without an explicit mode, the user's umask may leave
    it world-readable (e.g. umask 022 → mode 0644). Tightening here
    closes that leak on Linux/macOS. Best-effort — if chmod fails (e.g.
    on a read-only mount), we don't crash the save.
    """
    try:
        if hasattr(os, "chmod"):
            os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass
class ProviderStore:
    """Read/write/switch LLM providers on disk."""

    path: str = field(default_factory=_default_store_path)
    providers: list[LlmProvider] = field(default_factory=list)
    current_id: str = ""
    # M7: in-process lock guarding concurrent reads and writes. The default
    # is a module-level RLock so nested calls (e.g. set_current from a
    # callback that itself calls add) don't deadlock.
    lock: "threading.RLock" = field(default_factory=threading.RLock, repr=False)

    # -- persistence -----------------------------------------------------

    #: How many rolling backups the store keeps. Five is the same number
    #: ``scripts/update_ics.py`` keeps for the canonical table, and the
    #: same rotation shape (``.bak``, ``.bak.1`` ... ``.bak.4``).
    BACKUP_COUNT = 5

    def _config_fingerprint(self, data: dict) -> str:
        """A digest of the store's CONFIGURATION.

        Three things are deliberately excluded:

        * the API key ciphertext -- it is re-encrypted with a random IV on
          every write, so including it would make every save look like a
          change and the backup chain would fill with copies of the same
          configuration;
        * ``current_id`` -- that is a POINTER, not configuration. The
          store rewrites the file every time the user switches the active
          provider, and a switch must not cost the user the previous
          configuration: five provider switches would otherwise push their
          real providers out of the chain entirely.
        * ``is_current`` -- the same pointer, stored on each provider. It
          is DERIVED from ``current_id`` on every load, so it changes with
          the switch and belongs with it.
        """
        import hashlib
        rows = []
        for p in data.get("providers") or []:
            rows.append({k: v for k, v in p.items()
                         if k not in ("api_key", "is_current")})
        payload = json.dumps(rows, sort_keys=True, ensure_ascii=False,
                             default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _rotate_backups(self) -> str:
        """Shift ``.bak`` -> ``.bak.1`` -> ... and return the free slot.

        The same rotation ``scripts/update_ics.py`` uses, for the same
        reason: a single fixed-name ``.bak`` is clobbered by every write,
        so after a few writes the true previous state is gone. Each step
        is an ``os.replace``, so a crash mid-rotation cannot lose a
        generation.
        """
        newest = self.path + ".bak"
        if os.path.exists(newest):
            oldest = "%s.bak.%d" % (self.path, self.BACKUP_COUNT - 1)
            if os.path.exists(oldest):
                try:
                    os.remove(oldest)
                except OSError:
                    pass
            for k in range(self.BACKUP_COUNT - 2, 0, -1):
                src = "%s.bak.%d" % (self.path, k)
                if os.path.exists(src):
                    try:
                        os.replace(src, "%s.bak.%d" % (self.path, k + 1))
                    except OSError:
                        pass
            try:
                os.replace(newest, newest + ".1")
            except OSError:
                pass
        return newest

    def _backup_before_replace(self, data: dict) -> None:
        """Snapshot the file that is about to be replaced.

        REVIEW-2026-10-20: the store had NO history of its own. A probe
        script emptied it, and the only way back was a backup somebody had
        taken by hand the day before -- a lucky accident, not a design.

        The snapshot is taken only when the CONFIGURATION changes, so a
        save that merely switches the active provider does not spend a
        generation. It lands next to the store, in the same 0700
        directory, with the same 0600 mode, because it holds the same
        encrypted keys -- and it is a BYTE-LEVEL copy of the file, not a
        re-serialisation of the data, for the reason
        ``scripts/update_ics.py`` gives: a backup is the OLD state.
        """
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(existing, dict):
            return
        if self._config_fingerprint(existing) == self._config_fingerprint(data):
            return
        slot = self._rotate_backups()
        tmp = "%s.%d.tmp" % (slot, os.getpid())
        try:
            with open(self.path, "rb") as src, open(tmp, "wb") as dst:
                dst.write(src.read())
                dst.flush()
                os.fsync(dst.fileno())
            _chmod_user_only(tmp)
            os.replace(tmp, slot)
            _chmod_user_only(slot)
        except OSError:
            # A failed backup must not stop the save: refusing to write
            # because the backup failed would turn a recoverable problem
            # into a lost configuration.
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def load(self) -> "ProviderStore":
        self.providers = []
        self.current_id = ""
        data = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            self._seed_defaults()
            self.save()
            return self
        except json.JSONDecodeError:
            # M6: don't silently overwrite a corrupted JSON. Back up the bad
            # file with a timestamp suffix so the user can recover, then seed
            # defaults so the app can still start.
            self._quarantine_corrupt_file()
            self._seed_defaults()
            self.save()
            return self
        if not isinstance(data, dict):
            # M6: handle schema-invalid cases (top level must be an object).
            self._quarantine_corrupt_file()
            self._seed_defaults()
            self.save()
            return self
        raw_list = data.get("providers") or []
        self.current_id = data.get("current_id", "")
        for raw in raw_list:
            try:
                self.providers.append(LlmProvider.from_dict(raw))
            except Exception:
                continue
        self.providers.sort(key=lambda p: (p.sort_index, p.created_at, p.id))
        # Fall back to first provider if current_id is stale.
        if self.providers and not any(p.id == self.current_id for p in self.providers):
            self.current_id = self.providers[0].id
            self.providers[0].is_current = True
        # API-REVIEW-2026-10-18: the file is user-editable, so is_current is
        # an untrusted flag — a hand-edited providers.json can carry several
        # True flags. get_current() disambiguates via the AND condition, but
        # the card grid highlights on the raw flag (ProviderCard(prov,
        # prov.is_current)), so two providers would both render as active.
        # Normalise here: exactly the current_id entry keeps the flag.
        seen_current = False
        for p in self.providers:
            if p.id == self.current_id and not seen_current:
                p.is_current = True
                seen_current = True
            else:
                p.is_current = False
        return self

    def save(self) -> None:
        # Process-level guard around the tmp+rename. Even with a per-instance
        # RLock, two ProviderStore instances pointing at the same file can
        # race the tmp+rename sequence (each writes a fixed ``<path>.tmp``
        # name, so the second writer clobbers the first writer's tmp before
        # it can ``os.replace``). A module-level lock serializes *all*
        # concurrent saves across instances; the per-instance RLock still
        # serializes nested CRUD within a single store.
        with _PROVIDER_STORE_SAVE_LOCK:
            # AUDIT-2026-09-30: mode=0o700 added. This is the same directory
            # rca_core/secrets_store.py::_base_dir() creates, and that function
            # states the invariant in its own docstring: the directory "used to
            # be created with os.makedirs(..., exist_ok=True) and left at the
            # process umask default (0777 & ~umask, i.e. usually 0755).
            # Everything this module stores in it is key material, so it is now
            # 0700 best-effort." It was enforcing that for its own two call
            # sites while this one -- which is quite likely the FIRST to run on
            # a fresh install, since saving a provider needs no key material --
            # created it with no mode at all. Whichever writer ran first decided
            # the mode for the whole install, and this one handed a 0755
            # directory to the module holding the Fernet key and the salt.
            # No-op on Windows, where mode bits are advisory (the real ACL comes
            # from the profile directory); the existing _chmod_user_only call
            # further down documents the same caveat.
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            data = {
                "version": 1,
                "current_id": self.current_id,
                "providers": [p.to_dict() for p in self.providers],
            }
            tmp = self.path + ".tmp"
            # REVIEW-2026-10-20: take the history BEFORE the file is
            # replaced, so an accidental wipe is recoverable from the
            # store itself rather than from a copy somebody happened to
            # make by hand.
            self._backup_before_replace(data)
            # REVIEW-2026-09-20 #25: without the finally a failed write or a
            # failed os.replace (locked target on Windows, disk full,
            # permissions) left ``providers.json.tmp`` behind forever - a
            # stale file holding every API key in cleartext next to the real
            # store. Clean it up on every path; after a successful replace the
            # name no longer exists so the removal is skipped.
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                # Bug-9 fix: tighten permissions on POSIX *before* the rename so
                # the final file is never readable by other users. On Windows
                # this is a no-op (the ACL model differs); the worst case is
                # readable only to the current user via the inherited DACL.
                _chmod_user_only(tmp)
                os.replace(tmp, self.path)
                _chmod_user_only(self.path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    # -- defaults --------------------------------------------------------

    def _seed_defaults(self) -> None:
        """Seed a sensible default (Anthropic-compatible MiniMax) so the app
        works out-of-the-box with the legacy single-config contract."""
        mini = next((p for p in PROVIDER_PRESETS if p.name == "MiniMax M3"), None)
        if mini is None:
            return
        provider = LlmProvider(
            name=mini.name,
            api_format=mini.api_format,
            endpoint=mini.endpoint,
            # Read the MiniMax-specific env var (not ANTHROPIC_API_KEY, which
            # previously mis-attributed the user's Anthropic key to MiniMax -
            # a functional break AND a third-party key leak). Empty by default;
            # the user pastes their MiniMax key in the UI.
            api_key=os.environ.get("MINIMAX_API_KEY", ""),
            model=mini.model,
            extra_headers=dict(mini.extra_headers),
            is_current=True,
        )
        self.providers = [provider]
        self.current_id = provider.id

    def _quarantine_corrupt_file(self) -> None:
        """Move the corrupt providers.json aside with a timestamp suffix so
        the user can recover their previous config (M6). The default seed
        will repopulate the live file at the original path."""
        if not self.path or not os.path.isfile(self.path):
            return
        ts = time.strftime("%Y%m%dT%H%M%S")
        target = f"{self.path}.corrupt-{ts}"
        try:
            os.replace(self.path, target)
        except OSError as exc:  # pragma: no cover - defensive
            sys.stderr.write(
                f"[rca_core] failed to quarantine corrupt providers.json ({self.path}): {exc}\n"
            )

    # -- CRUD ------------------------------------------------------------

    def add(self, provider: LlmProvider) -> LlmProvider:
        with self.lock:
            # M9: don't clobber an existing created_at when the caller passed
            # a loaded-from-disk provider.
            if provider.created_at == 0:
                provider.created_at = time.time()
            provider.sort_index = max(
                (p.sort_index for p in self.providers), default=0
            ) + 1
            if not provider.id:
                provider.id = str(uuid.uuid4())
            self.providers.append(provider)
            self.save()
            return provider

    def remove(self, provider_id: str) -> None:
        with self.lock:
            self.providers = [p for p in self.providers if p.id != provider_id]
            if self.current_id == provider_id:
                # L-2 fix: clear ALL is_current flags first, then set new one.
                for p in self.providers:
                    p.is_current = False
                self.current_id = self.providers[0].id if self.providers else ""
                if self.providers:
                    self.providers[0].is_current = True
            self.save()

    def update(self, provider: LlmProvider) -> bool:
        """Replace an existing provider by id. Returns False when no match —
        M8 surfaces the miss instead of silently dropping the edit."""
        with self.lock:
            for i, p in enumerate(self.providers):
                if p.id == provider.id:
                    # Preserve the active flag: the wizard rebuilds the provider
                    # with is_current=False (default), so naively replacing
                    # would deactivate an active provider and get_current()
                    # would silently fall back to the first list entry,
                    # rerouting calls/credentials to a different endpoint.
                    provider.is_current = p.is_current
                    self.providers[i] = provider
                    self.save()
                    return True
            return False

    def set_current(self, provider_id: str) -> None:
        with self.lock:
            found = False
            for p in self.providers:
                p.is_current = (p.id == provider_id)
                if p.is_current:
                    found = True
            if found:
                self.current_id = provider_id
            else:
                # id doesn't exist — don't persist a stale pointer. Fall back to
                # the first provider remain-ingly active.
                if self.providers:
                    self.providers[0].is_current = True
                    self.current_id = self.providers[0].id
                else:
                    self.current_id = ""
            self.save()

    # -- queries ---------------------------------------------------------

    def get_current(self) -> LlmProvider | None:
        for p in self.providers:
            if p.id == self.current_id and p.is_current:
                return p
        # Fallback to first.
        if self.providers:
            return self.providers[0]
        return None

    def by_id(self, provider_id: str) -> LlmProvider | None:
        for p in self.providers:
            if p.id == provider_id:
                return p
        return None

    # -- legacy compat ---------------------------------------------------

    def to_legacy_config(self) -> dict[str, Any]:
        """Return the legacy flat config (endpoint / model / api_key /
        extra_headers) of the active provider, for callers that haven't yet
        adopted the provider abstraction."""
        p = self.get_current()
        if not p:
            return {}
        return {
            "endpoint": p.endpoint,
            "model": p.model,
            "api_key": p.api_key,
            "extra_headers": dict(p.extra_headers),
            "api_format": p.api_format.value,
        }


# ---------------------------------------------------------------------------
# Multi-format API call
# ---------------------------------------------------------------------------


# Process-level lock guarding ProviderStore.save() across all instances.
# A single fixed ``<path>.tmp`` is shared across every store pointing at
# the same file, so two concurrent saves can race the tmp+rename sequence
# (each writer overwrites the other's tmp before the other's rename).
# Pairing the per-instance RLock (above) with this module-level lock
# guarantees mutual exclusion for the write step itself.
_PROVIDER_STORE_SAVE_LOCK = threading.Lock()


def _coerce_text(value: Any) -> str:
    """Coerce a provider text field to str.

    REVIEW-2026-09-10: a single null/numeric text part used to raise
    ``TypeError: can only concatenate str (not "NoneType") to str`` out of
    the never-raises ``call_llm_api``, poisoning an otherwise good reply.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _summarise_empty_payload(payload: Any) -> str:
    """Describe a 2xx response that carried no usable text.

    REVIEW-2026-09-10: relay/gateway presets commonly answer HTTP 200 with an
    error object in the body (``{"error": {"message": "insufficient balance"}}``)
    and some providers return a refusal with an empty content list. Both used
    to reach the caller as "successful but empty", so the user saw a generic
    parse error instead of the real reason. Surface a short excerpt.
    """
    try:
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("type") or ""
                if msg:
                    return "upstream error in a 200 response: " + str(msg)[:500]
            if err:
                return "upstream error in a 200 response: " + str(err)[:500]
            stop = payload.get("stop_reason") or payload.get("finish_reason") or ""
            if stop:
                return ("response contained no text blocks "
                        f"(stop_reason={stop})")
            # REVIEW-2026-09-20 #21: Gemini reports a safety/PII refusal as a
            # 200 with empty parts plus ``promptFeedback.blockReason`` - without
            # this the user saw "no text" instead of "blocked: SAFETY".
            feedback = payload.get("promptFeedback")
            if isinstance(feedback, dict) and feedback.get("blockReason"):
                return ("response blocked upstream "
                        f"(blockReason={feedback['blockReason']})")
            cands = payload.get("candidates")
            if isinstance(cands, list) and cands and isinstance(cands[0], dict):
                fr = cands[0].get("finishReason")
                if fr:
                    return ("response contained no text blocks "
                            f"(finishReason={fr})")
        return "response contained no text blocks"
    except Exception:
        return "response contained no text blocks"


def _decode_err_body(err_body: bytes) -> str:
    """Best-effort decode of the upstream error bytes for surfacing in the UI.

    The body may be empty, plain text, JSON, or arbitrary bytes. We try
    utf-8 first then latin-1 so 5xx diagnostic info isn't silently dropped.
    """
    if not err_body:
        return ""
    try:
        return err_body.decode("utf-8", errors="replace")[:2000]
    except Exception:
        try:
            return err_body.decode("latin-1", errors="replace")[:2000]
        except Exception:
            return ""


# REVIEW-2026-09-20 #24: ceiling for a single response body. A chart
# extraction answers with a few tens of KB of JSON; anything beyond this is a
# misconfigured endpoint (an HTML error page from a proxy, a streaming
# download, a hostile host) and ``resp.read()`` would have buffered it all in
# memory before the JSON parse even started.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _read_limited(resp, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read at most ``limit`` bytes from a response / HTTPError object.

    Returns the body when it fits; raises ValueError when it does not, so the
    callers report a bounded diagnostic instead of an out-of-memory process.
    """
    try:
        data = resp.read(limit + 1)
    except TypeError:
        # ``http.client`` always accepts a length, but lightweight response
        # doubles (tests, local stubs) expose a bare ``read()``; degrade to it
        # instead of failing the request outright.
        data = resp.read()
    data = data or b""
    if len(data) > limit:
        raise ValueError(f"response body exceeds {limit} bytes")
    return data


#: The headers of the most recent response, per thread.
#:
#: REVIEW-2026-10-20: the headers are the only place a provider can say
#: "come back in 120 seconds" (Retry-After) or "do not retry this"
#: (x-should-retry). An out-parameter was tried first and broke thirteen
#: existing tests, because they mock the internal helpers with fixed-arity
#: fakes -- which is the signal that the parameter was the invasive way
#: to do it. A thread-local slot needs no signature change at all, and it
#: is safe: the retry layer reads it immediately after the call returns,
#: on the same thread, with nothing in between.
_LAST_RESPONSE_HEADERS = threading.local()


def _last_response_headers() -> dict:
    """The headers of the last response on THIS thread, or {}."""
    return getattr(_LAST_RESPONSE_HEADERS, "headers", None) or {}


def _capture_response_headers(resp) -> None:
    """Record a response's headers in this thread's slot.

    REVIEW-2026-10-20: the headers are the only place a provider can say
    "come back in 120 seconds" (Retry-After) or "do not retry this"
    (x-should-retry). They are kept with their ORIGINAL spelling, because
    ``error_utils.get_retry_delay`` folds the keys itself and the
    browser's ``Headers.get`` is case-insensitive -- the two sides then
    agree.

    Best-effort by construction: a response without headers, or a header
    object that refuses to be read, must not turn a failed call into a
    crash.
    """
    try:
        raw = getattr(resp, "headers", None)
        if raw is None:
            return
        items = getattr(raw, "items", None)
        if callable(items):
            captured = {str(k): str(v) for k, v in items()}
        else:
            captured = {str(k): str(v) for k, v in dict(raw).items()}
        _LAST_RESPONSE_HEADERS.headers = captured
    except Exception:
        # The headers are an optimisation, never a requirement.
        pass


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: int,
               progress_callback=None):
    """Fire a POST and return (payload_bytes, status_code). Never raises.

    Returns ``(None, status, body_bytes)`` for HTTPError so callers can
    surface the upstream error body instead of just the status code (H7).
    For network-layer failures (DNS, refused connection, timeout, etc.) the
    status is ``None`` and ``body_bytes`` carries a short diagnostic string
    so the caller can distinguish timeout from connection-refused in logs.

    ``progress_callback``, if given, is called with stage strings
    ``"submitting"`` and ``"thinking"`` to allow the UI to show granular
    progress rather than a generic spinner.
    """
    try:
        # REVIEW-2026-09-20 #20: a provider whose ``extra_body`` holds a
        # non-serialisable value (a datetime, an object pasted in by hand into
        # providers.json) raised TypeError here - outside the try below - and
        # escaped the documented never-raises contract.
        data = json.dumps(body).encode("utf-8")
    except Exception as e:
        return None, None, f"[request] body not serialisable: {type(e).__name__}: {e}".encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    if progress_callback:
        progress_callback("submitting")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if progress_callback:
                progress_callback("thinking")
            # REVIEW-2026-10-20: carry the RESPONSE HEADERS out.
            #
            # The provider's own Retry-After is the one delay it knows to be
            # right, and the repo has documented for weeks that both live
            # call sites pass headers=None, so a 429 Retry-After: 120 is
            # backed off by ~0.8 s here while the BROWSER waits the
            # requested time -- the two transports disagree about the same
            # failure. The headers travel in a THREAD-LOCAL slot rather
            # than in the return tuple, because that tuple
            # (raw, truncated, status, err_body, usage) is consumed in a
            # dozen places and changing its arity would be a much bigger
            # change than the problem deserves -- and an out-parameter on
            # the internal helpers broke thirteen tests that mock them
            # with fixed-arity fakes.
            _capture_response_headers(resp)
            try:
                # REVIEW-2026-09-20 #24: bounded read (see _read_limited).
                return _read_limited(resp), resp.status, b""
            except ValueError as e:
                return None, resp.status, str(e).encode("utf-8")
    except urllib.error.HTTPError as e:
        # Best-effort read of the upstream error body. Don't fail if the
        # server closed the connection.
        # REVIEW-2026-11-07 (low): the upstream DID answer (4xx/5xx), so the
        # "thinking" stage is accurate — without this the UI spinner stayed
        # on "submitting" forever for auth/quota errors.
        if progress_callback:
            progress_callback("thinking")
        # An HTTPError IS a response object, so it carries the same headers
        # a 2xx would — and on a 429 that is exactly where Retry-After
        # lives.
        _capture_response_headers(e)
        err_body = b""
        try:
            try:
                err_body = _read_limited(e) or b""
            except ValueError as exc:
                err_body = str(exc).encode("utf-8")
        except Exception:
            err_body = b""
        finally:
            # REVIEW-2026-09-20 #24: HTTPError IS a response object; close it
            # explicitly so the (possibly chunked) connection is released
            # instead of waiting for the GC on this hot failure path.
            try:
                e.close()
            except Exception:
                pass
        # REVIEW-2026-11-07 (low): every return path of this function yields
        # bytes for the body slot (b"" / e.read() / msg.encode above) —
        # keep it that way so _decode_err_body's double-decode stays simple.
        return None, e.code, err_body
    except TimeoutError as e:
        # Python 3.10+ raises TimeoutError from socket.timeout; older code
        # surfaces it as URLError(timeout=...). Mark it explicitly so logs
        # don't say "connection refused" when the real cause was a slow
        # upstream.
        return None, None, f"[network] timeout: {e}".encode("utf-8")
    except urllib.error.URLError as e:
        # URLError.reason distinguishes DNS from "refused" from "no route".
        reason = getattr(e, "reason", None)
        msg = f"[network] {type(reason).__name__ if reason else 'URLError'}: {reason or e}"
        return None, None, msg.encode("utf-8")
    except Exception as e:
        # Last-resort guard: log the exception class + str so a future
        # contributor can triage without re-running.
        return None, None, f"[network] {type(e).__name__}: {e}".encode("utf-8")


def _sse_events(resp):
    """Yield the JSON payload of each ``data:`` line of an SSE response.

    The Anthropic-compatible stream sends an ``event: <type>`` line followed
    by a ``data: <json>`` line, and the JSON carries its own ``type`` -- so
    the data line is the one to parse. Measured against MiniMax-M3.1-Flash
    (2026-10-19): ``text/event-stream``, ``content_block_delta`` events with
    ``delta.type == "thinking_delta"`` carrying the model's thinking text.
    """
    for raw in resp:
        try:
            line = raw.decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not line or line.startswith("event:") or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            yield json.loads(line)
        except Exception:
            # A malformed event must not kill the stream: the rest of it is
            # still worth reading.
            continue


def _read_response_stream(target: str, body: dict[str, Any],
                           headers: dict[str, str], timeout_sec: int,
                           progress_callback=None,
                           thinking_callback=None):
    """Read an SSE reply and assemble it into the usual 5-tuple.

    Returns the same shape as ``_read_response`` so the callers above do not
    have to know whether the reply came as one JSON body or as a stream.

    ``thinking_callback`` receives each chunk of the model's THINKING as it
    arrives. That content is in the reply either way -- an Anthropic reply
    carries ``content: [{type: 'thinking', ...}, {type: 'text', ...}]`` and the
    non-streaming reader concatenates only the text blocks -- so the only
    thing streaming adds is that it arrives WHILE the model is still
    thinking, which is the difference between a progress bar that says
    "working" and one that shows what the model is doing.

    If the endpoint answers with a plain JSON body instead of a stream (it
    may ignore ``stream: true``), the body is parsed the ordinary way, so
    the caller gets an answer either way.
    """
    try:
        data = json.dumps(body).encode("utf-8")
    except Exception as e:
        return None, False, None, (
            f"[request] body not serialisable: {type(e).__name__}: {e}"), None
    req = urllib.request.Request(target, data=data, headers=headers,
                                 method="POST")
    if progress_callback:
        progress_callback("submitting")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout_sec)
    except urllib.error.HTTPError as e:
        err_body = b""
        try:
            err_body = _read_limited(e) or b""
        except Exception:
            err_body = b""
        finally:
            try:
                e.close()
            except Exception:
                pass
        if progress_callback:
            progress_callback("thinking")
        return None, False, e.code, _decode_err_body(err_body), None
    except TimeoutError as e:
        return None, False, None, f"[network] timeout: {e}", None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        msg = (f"[network] {type(reason).__name__ if reason else 'URLError'}: "
               f"{reason or e}")
        return None, False, None, msg, None
    except Exception as e:
        return None, False, None, f"[network] {type(e).__name__}: {e}", None

    with resp:
        if progress_callback:
            progress_callback("thinking")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/event-stream" not in ctype:
            # The endpoint ignored `stream: true`. Read it as one body.
            try:
                payload_bytes = _read_limited(resp)
            except ValueError as e:
                return None, False, resp.status, str(e), None
            try:
                payload = json.loads(payload_bytes.decode("utf-8-sig"))
            except Exception:
                return (None, False, resp.status,
                        _decode_err_body(payload_bytes), None)
            # The status has to be carried through: a caller that sees
            # status=None reads it as a NETWORK failure, and a 200 that
            # answered is not the same thing.
            text, truncated, _status, err, usage = _assemble_anthropic_payload(
                payload)
            return text, truncated, resp.status, err, usage

        raw_text = ""
        thinking_chars = 0
        truncated = False
        usage_obj = None
        # REVIEW-2026-10-20 (borrowed from opencode's provider-error.ts,
        # which classifies an "incomplete-stream" as retryable): did the
        # model SAY it was finished?
        #
        # A stream that ends cleanly -- the server closes the connection
        # without an error, without a `message_stop` and without a
        # stop_reason -- leaves `raw_text` holding a PARTIAL answer. That
        # partial answer used to be returned as if it were complete, and a
        # truncated table looks exactly like a complete one: the reader
        # gets a shorter range chart and never learns why.
        saw_stop = False
        stream_error = ""
        for ev in _sse_events(resp):
            etype = ev.get("type") or ""
            if etype == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type") or ""
                if dtype == "thinking_delta":
                    chunk = _coerce_text(delta.get("thinking"))
                    if chunk:
                        thinking_chars += len(chunk)
                        if thinking_callback:
                            try:
                                thinking_callback(chunk)
                            except Exception:
                                pass
                elif dtype == "text_delta":
                    raw_text += _coerce_text(delta.get("text"))
            elif etype == "message_start":
                msg = ev.get("message") or {}
                if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                    usage_obj = msg["usage"]
            elif etype == "message_delta":
                delta = ev.get("delta") or {}
                if delta.get("stop_reason"):
                    saw_stop = True
                    if delta.get("stop_reason") == "max_tokens":
                        truncated = True
                if isinstance(ev.get("usage"), dict):
                    usage_obj = ev["usage"]
            elif etype == "message_stop":
                saw_stop = True
                break
            elif etype == "error":
                err_obj = ev.get("error")
                if isinstance(err_obj, dict):
                    stream_error = _coerce_text(
                        err_obj.get("message") or err_obj.get("type"))
                else:
                    stream_error = _coerce_text(err_obj)
                break
        usage = None
        if usage_obj:
            usage = dict(usage_obj)
            usage["estimated"] = False
        if stream_error:
            return (None, truncated, resp.status,
                    "[stream] the model reported an error: %s" % stream_error,
                    usage)
        if not raw_text.strip():
            return (None, truncated, resp.status,
                    ("the model produced no text" if not thinking_chars
                     else "the model produced no text (it spent the whole "
                          "answer budget thinking)"), usage)
        if not saw_stop:
            # The model never said it was done. Whatever arrived is a
            # PREFIX of the answer, and a prefix of a table is a wrong
            # table, not a short one.
            return (None, truncated, resp.status,
                    "[stream] the connection ended before the model finished",
                    usage)
        return raw_text, truncated, resp.status, "", usage


def _assemble_anthropic_payload(payload: Any):
    """Turn a non-streaming Anthropic payload into the usual 5-tuple.

    Split out of ``_read_response`` so the streaming path can reuse it for the
    case where the endpoint answered with one JSON body instead of a stream.
    """
    if not isinstance(payload, dict):
        return None, False, None, "[response] body was not a JSON object", None
    raw_text = ""
    for c in payload.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            raw_text += _coerce_text(c.get("text"))
    truncated = payload.get("stop_reason") == "max_tokens"
    if not raw_text.strip():
        return (None, truncated, None, _summarise_empty_payload(payload),
                payload)
    return raw_text, truncated, None, "", payload


def _get_json(url: str, headers: dict[str, str], timeout_sec: int):
    """Fire a GET and return (payload_bytes, status_code, err_body). Never raises.

    Mirrors ``_post_json`` but uses HTTP GET, the only method that real
    OpenAI / Anthropic / Gemini ``/models`` listing endpoints accept.
    POSTing to those endpoints returns 405 (Method Not Allowed) on the
    official gateways and the connection test incorrectly reports the
    endpoint as broken.

    The return shape matches ``_post_json`` so callers can swap one for the
    other transparently.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            try:
                # REVIEW-2026-09-20 #24: bounded read, same as _post_json.
                return _read_limited(resp), resp.status, b""
            except ValueError as e:
                return None, resp.status, str(e).encode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = b""
        try:
            try:
                err_body = _read_limited(e) or b""
            except ValueError as exc:
                err_body = str(exc).encode("utf-8")
        except Exception:
            err_body = b""
        finally:
            # REVIEW-2026-09-20 #24: release the error response explicitly.
            try:
                e.close()
            except Exception:
                pass
        return None, e.code, err_body
    except TimeoutError as e:
        return None, None, f"[network] timeout: {e}".encode("utf-8")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        msg = f"[network] {type(reason).__name__ if reason else 'URLError'}: {reason or e}"
        return None, None, msg.encode("utf-8")
    except Exception as e:
        return None, None, f"[network] {type(e).__name__}: {e}".encode("utf-8")


# Opener that pins DNS (TOCTOU / DNS-rebinding mitigation) AND raises on any
# redirect instead of following it. Installed as urllib's default opener so
# EVERY outbound call made through ``urllib.request.urlopen`` (including future
# ones) refuses 3xx redirects and connects to the single validated IP. This is
# the same opener server.py installs, so the GUI path (which imports llm.py
# directly, without going through server.py) gets identical SSRF protection.
# urllib openers are thread-safe for concurrent ``.open()`` calls, so a single
# global opener is fine for the app's thread pool. Tests that monkeypatch
# ``urllib.request.urlopen`` (tests_llm.py) bypass this opener and keep working.
urllib.request.install_opener(make_pinning_opener())


# A trailing API-VERSION segment is stripped: ``/v1`` AND ``/v1beta``
# (REVIEW-2026-09-20 #22). Every caller re-appends its own canonical version,
# so keeping the suffix produced the double-path bug: the shipped Gemini
# preset (``https://generativelanguage.googleapis.com/v1beta``) hit
# ``/v1beta/v1beta/models/…:generateContent`` and 404'd out of the box, and an
# OpenAI-compatible base ending in ``/v1beta`` got
# ``/v1beta/v1/chat/completions``.
# The OpenAI/Anthropic callers both add their own canonical ``/v1/...`` so
# stripping a redundant ``/v1`` suffix avoids the ``/v1/v1`` double-path bug
# that 404'd ~48 OpenAI presets. Non-version trailing segments
# (``/anthropic``, ``/compatible-mode``, ``/openai``) are preserved.
_V1_TAIL = re.compile(r"/v1(?:beta|alpha)?/?$", re.IGNORECASE)


def _api_base(endpoint: str) -> str:
    """Normalize an endpoint by stripping a trailing ``/v1``/``/v1beta``
    segment so per-format callers can append their canonical ``/v1/...`` (or
    ``/v1beta/...``) path without producing a double version segment.

    Provider-specific versions (``/v2`` .. ``/v4``) are NOT stripped — those
    are the host's real API version and are handled by
    :data:`_BASE_VERSION_TAIL` in :func:`_endpoint_path`.
    """
    return _V1_TAIL.sub("", (endpoint or "").rstrip("/"))


# A base URL that already ends in a version segment. REVIEW-2026-09-10: eight
# shipped presets publish such a base (Zhipu /api/paas/v4, Baidu Qianfan /v2,
# Volcengine Ark /api/v3, Novita /v3, BytePlus/Huoshan/DouBaoSeed /api/v3) and
# the OpenAI/Anthropic SDK convention is to append ONLY the resource path
# ("/chat/completions", "/messages"). Appending the full "/v1/..." produced
# /api/paas/v4/v1/chat/completions, which those hosts do not serve - a valid
# key then failed the provider Test and every extraction.
_BASE_VERSION_TAIL = re.compile(r"/v\d+(?:\.\d+)?(?:beta|alpha)?$", re.IGNORECASE)


def _endpoint_path(base: str, resource: str) -> str:
    """Join a canonical versioned ``resource`` path onto a provider base URL.

    ``resource`` is the path this client would use against a bare host
    ("/v1/messages", "/v1/chat/completions", "/v1/models"). When the base
    already carries a version segment, only the resource suffix is appended so
    the version is not duplicated.
    """
    if _BASE_VERSION_TAIL.search(base):
        return base + resource[len("/v1"):]
    return base + resource


# Leading ``/v1beta`` of the canonical Gemini resource paths.
_GEMINI_VERSION_PREFIX = re.compile(r"^/v1beta(?=/)", re.IGNORECASE)


def _gemini_path(base: str, resource: str) -> str:
    """Join a canonical ``/v1beta/...`` Gemini resource onto a provider base.

    REVIEW-2026-09-20 #22: the Gemini callers used to concatenate
    ``f"{base}/v1beta/..."`` unconditionally, so any host that publishes its
    own version segment (``https://gemini-proxy.example.com/v2``) received the
    double path ``/v2/v1beta/models/...``. ``_api_base`` already removes a
    trailing ``/v1beta``, so this covers the remaining ``/vN`` bases; it is the
    Gemini twin of :func:`_endpoint_path` (which only knows how to strip a
    literal ``/v1`` prefix).
    """
    if _BASE_VERSION_TAIL.search(base):
        return base + _GEMINI_VERSION_PREFIX.sub("", resource, count=1)
    return base + resource


def _request_target(provider: LlmProvider, resource: str) -> str:
    """The URL a request goes to, honouring the provider's ``full_url`` flag.

    By default the endpoint is a PREFIX and the canonical path is appended
    to it, which is what every vendor's base URL is. A gateway with a
    non-standard layout (``/custom/path/messages``) cannot be reached that
    way, so a provider may declare its endpoint to be a FULL request URL and
    have it used verbatim. cc-switch gates the same thing behind the same
    warning, for the same reason.
    """
    endpoint = (provider.endpoint or "").strip()
    if getattr(provider, "full_url", False) and endpoint:
        return endpoint
    return _endpoint_path(_api_base(endpoint), resource)


def _call_anthropic(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
    progress_callback=None,
    stream: bool = False,
    thinking_callback=None,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    # P0-2 (REVIEW-2026-07-25): validate endpoint before any network call.
    # The server.py path validated before dispatching, but the GUI path loads
    # providers from disk without re-validation. This unifies both paths.
    # REVIEW-2026-07-31: use validate_endpoint_local_ok — the SAME policy
    # as test_llm_connection and _call_gemini. validate_endpoint_or_raise
    # rejected loopback + plain-http, so a local Ollama
    # (http://127.0.0.1:11434) passed the connection test but every real
    # extraction failed — the two paths disagreed (Ollama is a legitimate
    # desktop use case).
    from .ssrf import validate_endpoint_local_ok
    ok_ep, why_ep = validate_endpoint_local_ok(provider.endpoint)
    if not ok_ep:
        return None, False, None, f"endpoint rejected by SSRF guard: {why_ep}", None
    if not image_b64:
        return None, False, None, "", None
    target = _request_target(provider, "/v1/messages")
    body: dict[str, Any] = {
        "model": provider.model,
        "max_tokens": max_tokens or 4000,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type or "image/png", "data": image_b64}},
                    {"type": "text", "text": user_text},
                ],
            }
        ],
    }
    # REVIEW-2026-10-19: reasoning effort, BEFORE ``extra_body`` so a user
    # who put ``output_config`` in extra_body by hand still wins -- extra_body
    # is the documented escape hatch and must keep the last word.
    body.update(_reasoning_request_patch(provider, ApiFormat.ANTHROPIC))
    body.update(provider.extra_body or {})
    headers = {
        "x-api-key": provider.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    headers.update(provider.extra_headers or {})
    if progress_callback:
        progress_callback("uploading")
    if stream:
        # REVIEW-2026-10-19: the stream carries the model's THINKING as it is
        # produced, which is the only way a progress indicator can show what
        # the model is actually doing instead of a spinner that says
        # "working" for a hundred seconds. The reply is assembled into the
        # same 5-tuple, so nothing above this line changes.
        body["stream"] = True
        text, truncated, status, err_body, usage = _read_response_stream(
            target, body, headers, timeout_sec, progress_callback,
            thinking_callback,
        )
        return text, truncated, status, err_body, usage
    text, truncated, status, err_body, payload = _read_response(
        target, body, headers, timeout_sec, progress_callback
    )
    usage = None
    if payload is not None:
        u = parse_usage(payload, "anthropic")
        if u:
            usage = dict(u); usage["estimated"] = False
    return text, truncated, status, err_body, usage


def _call_openai(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
    progress_callback=None,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    # P0-2 (REVIEW-2026-07-25): validate endpoint before any network call.
    # The server.py path validated before dispatching, but the GUI path loads
    # providers from disk without re-validation. This unifies both paths.
    # REVIEW-2026-07-31: validate_endpoint_local_ok — same policy as the
    # connection test and the Gemini path (loopback + plain-http allowed
    # for local Ollama-style endpoints; everything else stays
    # SSRF-rejected).
    from .ssrf import validate_endpoint_local_ok
    ok_ep, why_ep = validate_endpoint_local_ok(provider.endpoint)
    if not ok_ep:
        return None, False, None, f"endpoint rejected by SSRF guard: {why_ep}", None
    if not image_b64:
        return None, False, None, "", None
    target = _request_target(provider, "/v1/chat/completions")
    # OpenAI reasoning models (o1/o3/o4-mini) reject `max_tokens` and require
    # `max_completion_tokens`; sending the legacy name 400s the request.
    # They ALSO reject `role:system` (must be `developer` or merged into the
    # user message) and `response_format={"type":"json_object"}` (must rely on
    # native structured-output / json_schema). Without these adjustments the
    # reasoning path 400s every call.
    model = provider.model
    # REVIEW-2026-11-07 (low): some aggregators list reasoning models with a
    # capital letter ("O1"). The case-sensitive regex then missed them, the
    # request kept `max_tokens` + `role:system`, and OpenAI 400'd it.
    is_reasoning = bool(model and re.match(r"^o\d", model, re.IGNORECASE))
    messages: list[dict[str, Any]] = []
    # Sprint B (REVIEW-2026-09-04): reasoning models reject `role:system`,
    # but the previous code silently DROPPED the system prompt on this path,
    # losing the strict-JSON contract that keeps outputs parseable. Merge it
    # into the head of the user message instead. We deliberately avoid the
    # `developer` role: aggregator/relay providers ("中转") commonly forward
    # to whatever upstream they happen to route to, and `developer` is not
    # universally accepted there, while a leading text block always is.
    user_text_full = (
        f"{system_prompt}\n\n{user_text}"
        if (is_reasoning and system_prompt) else user_text
    )
    if not is_reasoning and system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    user_msg: dict[str, Any] = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:{media_type or 'image/png'};base64,{image_b64}"}},
            {"type": "text", "text": user_text_full},
        ],
    }
    messages.append(user_msg)
    body: dict[str, Any] = {
        "model": model,
        ("max_completion_tokens" if is_reasoning else "max_tokens"): max_tokens or 4000,
        "messages": messages,
    }
    if not is_reasoning:
        # FIX (json-object): ask OpenAI-compatible APIs to emit only valid JSON.
        # This single field eliminates the vast majority of markdown-fence /
        # prose-wrapped / truncated outputs that the fallback chain otherwise
        # has to clean up. Reasoning models reject this field — see above.
        body["response_format"] = {"type": "json_object"}
    body.update(provider.extra_body or {})
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "content-type": "application/json",
    }
    headers.update(provider.extra_headers or {})
    if progress_callback:
        progress_callback("uploading")
    payload_bytes, status, err_body = _post_json(
        target, body, headers, timeout_sec, progress_callback
    )
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str, None
    try:
        # AUDIT-2026-09-28: "utf-8-sig", not "utf-8", at all FIVE provider
        # payload decodes (this one, 1662, 1724, 2074, 2126). A gateway or
        # proxy that prepends a UTF-8 BOM made json.loads raise
        # "Unexpected UTF-8 BOM" on a perfectly good 2xx body, and the
        # except below then reported the whole response as an error body.
        # rca_core/json_utils.safe_json_loads got the same treatment earlier
        # the same day; this layer bypasses that chain, so it needed its own
        # fix -- the same guard missing on a sibling path. utf-8-sig is a
        # no-op when there is no BOM, so nothing else changes.
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except Exception:
        # REVIEW-2026-09-10: a 2xx that is not JSON (a WAF page, an SSE
        # stream) must hand its bytes back as the error body, not vanish.
        return None, False, status, _decode_err_body(payload_bytes), None
    raw_text = ""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, str):
        # Relays that stringify the array instead of emitting it.
        try:
            choices = json.loads(choices)
        except Exception:
            choices = []
    truncated = False
    for ch in (choices or []) if isinstance(choices, list) else []:
        # REVIEW-2026-09-20 #20: ``payload`` may be a list/str/None (a
        # malformed 2xx), ``message`` may be a string, and ``content`` may be
        # null or a number. Every one of those used to raise AttributeError /
        # TypeError out of a function documented as never raising - the whole
        # reply was lost instead of being reported as an empty response.
        if not isinstance(ch, dict):
            continue
        msg = ch.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        # Some OpenAI-compatible endpoints return content as a list of parts
        # ({"type":"text","text":...}) rather than a plain string; collapse
        # it to text so downstream JSON parsing doesn't receive a list.
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    raw_text += _coerce_text(part.get("text"))
                elif isinstance(part, str):
                    raw_text += part
        else:
            raw_text += _coerce_text(content)
        if raw_text.strip():
            truncated = ch.get("finish_reason") == "length"
            break
    if not raw_text.strip():
        # REVIEW-2026-09-20 #21: mirrors _read_response. Returning "" told the
        # caller "the model answered, it was just empty", so the UI reported a
        # parse error and the real reason (an error object inside a 2xx, e.g.
        # "insufficient balance") was thrown away.
        return (None, truncated, status, _summarise_empty_payload(payload),
                payload if isinstance(payload, dict) else None)
    usage = None
    u = parse_usage(payload, "openai")
    if u:
        usage = dict(u); usage["estimated"] = False
    return raw_text, truncated, status, err_str, usage


def _is_safe_endpoint(endpoint: str) -> bool:
    """SSRF check for every LLM call path (extract + connection test).

    REVIEW-2026-07-31: the policy lives in
    :func:`rca_core.ssrf.validate_endpoint_local_ok` — the single shared
    source of truth — so the connection test, ``_call_anthropic``,
    ``_call_openai`` and ``_call_gemini`` all agree. Private, link-local,
    reserved, and NAT64-embedded IPv4 addresses are rejected; loopback
    (127.0.0.0/8, ::1, ``localhost``) is PERMITTED because local models
    (e.g. Ollama at ``http://127.0.0.1:11434``) are a legitimate desktop
    use case. The server.py caller also validates the endpoint before
    dispatch, but the GUI path loads providers from disk without
    re-validation, so this is defense-in-depth.
    """
    from .ssrf import validate_endpoint_local_ok
    ok, _why = validate_endpoint_local_ok(endpoint)
    return ok


def _call_gemini(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int,
    progress_callback=None,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    if not image_b64:
        return None, False, None, "", None
    # F-26 fix: validate the endpoint before using it. The server.py caller
    # already validated, but this guards against the GUI path (providers loaded
    # from disk) and any future caller that bypasses server.py.
    if not _is_safe_endpoint(provider.endpoint):
        return None, False, None, "bad endpoint", None
    # Gemini accepts the API key via query param (key=) or header
    # (x-api-key). We default to the header to keep the key out of server,
    # proxy, and Referer logs — matching the safe pattern used by the other
    # format callers. Callers can override via provider.extra_headers.
    model = provider.model or "gemini-2.5-pro"
    base = _api_base(provider.endpoint)
    # URL-encode the model name so tuned models like ``tunedModels/foo``
    # (and any future model id that contains a reserved char) don't break
    # the URL parser or accidentally introduce an extra path segment.
    safe_model = urllib.parse.quote(model, safe="")
    if getattr(provider, "full_url", False):
        # A FULL-URL endpoint is the request URL, verbatim.
        target = (provider.endpoint or "").strip()
    else:
        target = _gemini_path(base, f"/v1beta/models/{safe_model}:generateContent")
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": media_type or "image/png", "data": image_b64}},
                    {"text": user_text},
                ],
            }
        ],
        "generation_config": {
            "max_output_tokens": max_tokens or 4000,
            # Force Gemini to emit JSON directly. Without this, Gemini
            # sometimes wraps its output in markdown fences or free-form
            # prose, and the model's own internal chain-of-thought ("thought")
            # parts leak into the response. ``response_mime_type`` cleanly
            # separates the two output streams downstream.
            "response_mime_type": "application/json",
        },
    }
    body.update(provider.extra_body or {})
    # REVIEW-2026-09-20 #27: ``call_llm_api`` folds temperature/seed into
    # ``extra_body``, and the update above dropped them at the ROOT of this
    # payload - but the Gemini REST schema keeps both inside
    # ``generation_config``. Official Google answers an unknown root field with
    # 400 ``Unknown name`` (or silently ignores it), so the reproducibility
    # knobs were a no-op on this format. Move them in; a value the user set
    # directly inside ``generation_config`` still wins.
    # Copy before mutating: ``body["generation_config"]`` may be the very dict
    # owned by ``provider.extra_body``, and editing it in place would leak the
    # per-call sampling into the persisted provider.
    gen_cfg = body.get("generation_config")
    gen_cfg = dict(gen_cfg) if isinstance(gen_cfg, dict) else {}
    for _key in ("temperature", "seed"):
        if _key in body:
            gen_cfg.setdefault(_key, body.pop(_key))
    body["generation_config"] = gen_cfg
    headers = {
        "content-type": "application/json",
        # Google's official Generative Language API authenticates via
        # `x-goog-api-key` (or ?key=); `x-api-key` is an Anthropic convention
        # the official endpoint ignores, so the "Google Gemini" preset 401'd.
        # Send both so official Google + third-party Gemini proxies both work.
        "x-goog-api-key": provider.api_key,
        "x-api-key": provider.api_key,
    }
    headers.update(provider.extra_headers or {})
    if progress_callback:
        progress_callback("uploading")
    payload_bytes, status, err_body = _post_json(
        target, body, headers, timeout_sec, progress_callback
    )
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str, None
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except Exception:
        # REVIEW-2026-09-20 #20: a 2xx that is not JSON (a WAF page, an SSE
        # stream) used to be reported with the *stale* empty err_str, so the
        # operator saw "HTTP 200" and nothing else. Mirror _read_response.
        return None, False, status, _decode_err_body(payload_bytes), None
    # REVIEW-2026-09-20 #20: a malformed 2xx whose root is a list/str/number
    # (relays do this) raised AttributeError out of a documented never-raising
    # function, so the whole reply vanished instead of reporting "no text".
    payload_obj = payload if isinstance(payload, dict) else {}
    candidates = payload_obj.get("candidates") or []
    if isinstance(candidates, str):
        try:
            candidates = json.loads(candidates)
        except Exception:
            candidates = []
    if not isinstance(candidates, list):
        candidates = []
    first = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    raw_text = ""
    content = first.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    for part in (parts or []) if isinstance(parts, list) else []:
        if not isinstance(part, dict):
            continue
        # Gemini 2.5+ emits a separate ``thought`` part alongside the
        # real output — chain-of-thought / planning that the model
        # surfaces as text. Skip it: the JSON downstream parser would
        # otherwise see e.g. ``internal reasoning...{"answer":42}``.
        if part.get("thought"):
            continue
        if "text" in part:
            # ``part["text"]`` may legitimately be null/numeric from a broken
            # proxy; ``raw_text += None`` used to raise TypeError here.
            raw_text += _coerce_text(part.get("text"))
    finish = first.get("finishReason")
    truncated = finish in ("MAX_TOKENS", "LENGTH")
    usage = None
    u = parse_usage(payload_obj, "gemini")
    if u:
        usage = dict(u); usage["estimated"] = False
    if not raw_text.strip():
        # REVIEW-2026-09-20 #21: same contract as the Anthropic / OpenAI paths -
        # an empty 200 is NOT "the model answered with nothing"; hand the
        # upstream reason (blockReason, an in-body error) back as err_body.
        return None, truncated, status, _summarise_empty_payload(payload_obj), None
    return raw_text, truncated, status, err_str, usage


def _read_response(
    target: str, body: dict[str, Any], headers: dict[str, str], timeout_sec: int,
    progress_callback=None,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    """Anthropic-format specific reader. Returns the parsed payload as the
    5th element so the caller can extract usage without re-parsing."""
    payload_bytes, status, err_body = _post_json(
        target, body, headers, timeout_sec, progress_callback
    )
    err_str = _decode_err_body(err_body)
    if payload_bytes is None:
        return None, False, status, err_str, None
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except Exception:
        # REVIEW-2026-09-10: a 2xx whose body is not JSON (a WAF/login HTML
        # page, an SSE stream) discarded the bytes entirely, so the caller
        # reported err.http with an EMPTY error_body - nothing to diagnose.
        # Hand the decoded text back as the error body (already capped).
        return None, False, status, _decode_err_body(payload_bytes), None
    raw_text = ""
    for c in payload.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text":
            # Concatenate ALL text blocks, not just the first. Anthropic
            # can legitimately split a long response across multiple
            # content blocks (e.g. ``{"text":"{..."} + {"text":"...}"}``),
            # and the previous ``break`` discarded the tail.
            # REVIEW-2026-09-10: the value may be null/numeric (a malformed
            # block), and `"" + None` raised TypeError out of a function
            # documented as never raising - one bad block poisoned an
            # otherwise good reply.
            raw_text += _coerce_text(c.get("text"))
    truncated = payload.get("stop_reason") == "max_tokens"
    if not raw_text.strip():
        # REVIEW-2026-09-10: a 200 with no usable text (a refusal, an empty
        # content list, or an error object delivered inside the body - which
        # many relay/gateway presets do) used to return "" and be treated as
        # a SUCCESSFUL empty result: the caller then reported err.parse with
        # raw="" and the upstream reason was lost. Return None (the caller's
        # "no text" signal) and carry the payload excerpt as the error body so
        # the user sees the real cause.
        return None, truncated, status, _summarise_empty_payload(payload), payload
    return raw_text, truncated, status, err_str, payload


def call_llm_api(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int = 120,
    capture_error_body: bool = False,
    progress_callback=None,
    temperature: float | None = None,
    seed: int | None = None,
    stream: bool = False,
    thinking_callback=None,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    """Dispatch a call on the provider's API format. Never raises.

    Returns ``(raw_text, truncated, status, err_body, usage)``:

    * ``raw_text``    — model text response (None on error / no image)
    * ``truncated``   — True when the model hit the max_tokens limit
    * ``status``      — HTTP status (None on network failure)
    * ``err_body``    — decoded, truncated upstream error body
    * ``usage``       — token usage dict
      ``{input_tokens, output_tokens, cache_read_tokens,
        cache_creation_tokens, estimated}``
      or ``None`` when the API didn't return a usage block and we
      couldn't estimate it (no text).

    ``progress_callback``, if given, is called with stage strings
    ``"submitting"``, ``"uploading"``, ``"thinking"`` to allow the UI to
    show granular extraction progress.

    ``stream`` asks for the Anthropic-compatible endpoint's SSE reply, and
    ``thinking_callback`` then receives the model's THINKING as it is
    produced. The reply is assembled into the same 5-tuple, so this changes
    only WHEN the answer arrives, not what it is. Measured against
    MiniMax-M3.1-Flash (2026-10-19): the thinking arrives as
    ``content_block_delta`` events with ``delta.type == "thinking_delta"``,
    a few hundred milliseconds apart.

    ``temperature`` and ``seed`` (P1 fix): first-class reproducibility
    parameters. When supplied, they're injected into the request body
    on top of any ``provider.extra_body`` so the call is reproducible
    across sessions. ``None`` (default) means "use the API default"
    — typically what production callers want. The audit trail reads
    these values out of ``provider.extra_body`` for request_meta.
    """
    # If temperature/seed were supplied at this layer, fold them into
    # a copy of the provider's extra_body so the per-call sampling is
    # auditable in request_meta without mutating the caller's provider.
    if temperature is not None or seed is not None:
        provider = LlmProvider(
            name=provider.name,
            api_format=provider.api_format,
            endpoint=provider.endpoint,
            api_key=provider.api_key,
            model=provider.model,
            extra_headers=dict(provider.extra_headers or {}),
            extra_body={
                **dict(provider.extra_body or {}),
                **({"temperature": temperature} if temperature is not None else {}),
                **({"seed": seed} if seed is not None else {}),
            },
            # REVIEW-2026-10-19: this copy rebuilt the provider from a fixed
            # field list, so a provider that carried a reasoning effort LOST
            # it whenever the caller passed a temperature or a seed -- and
            # M3.1 then silently reverted to effort=max, which is the
            # configuration that times out. The field has to travel with
            # the copy.
            reasoning_effort=provider.reasoning_effort,
            id=provider.id,
        )
    dispatch = {
        ApiFormat.ANTHROPIC: _call_anthropic,
        ApiFormat.OPENAI: _call_openai,
        ApiFormat.GEMINI: _call_gemini,
    }
    fn = dispatch.get(provider.api_format) or _call_anthropic
    # REVIEW-2026-10-19: streaming is implemented for the Anthropic-compatible
    # endpoint, which is the one MiniMax and Claude both speak. The other two
    # formats have no streaming reader here, so a request that asks for one
    # falls back to the ordinary single-body call rather than raising.
    extra: dict[str, Any] = {}
    if stream and thinking_callback is not None \
            and provider.api_format == ApiFormat.ANTHROPIC:
        extra = {"stream": True, "thinking_callback": thinking_callback}
    raw_text, truncated, status, err_body, usage = fn(
        provider=provider,
        system_prompt=system_prompt,
        image_b64=image_b64,
        media_type=media_type,
        user_text=user_text,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        progress_callback=progress_callback,
        **extra,
    )
    if not capture_error_body:
        # REVIEW-2026-09-10: this was a no-op (`if capture_error_body and not
        # err_body: err_body = ""` - the branch only ran when the body was
        # already empty), so the documented privacy switch did nothing and the
        # decoded upstream body was returned on every path regardless. Blank
        # it, keeping a marker so the caller still knows WHY there is no body.
        err_body = "[upstream error body not captured]" if err_body else ""
    # When the API didn't return a usage block but we got text back, fall
    # back to local estimation. Flag the row so the UI can label it.
    # Include an image token estimate so vision calls aren't under-counted
    # by ~85-1100 tokens (the typical range for a single image input at
    # current model pricings).
    if usage is None and raw_text:
        est_in = estimate_tokens(user_text + " " + system_prompt)
        if image_b64:
            # ~170 tokens for low-res / ~85 per 512px tile (Anthropic / OpenAI
            # vision pricing, rounded up). Using 170 keeps us in the same
            # ballpark as the API's actual charge for a typical chart image
            # without needing a real tokenizer.
            est_in += 170
        est_out = estimate_tokens(raw_text)
        usage = {
            "input_tokens": est_in,
            "output_tokens": est_out,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "estimated": True,
        }
    if usage is not None:
        _annotate_image_tokens(usage, image_b64)
    return raw_text, truncated, status, err_body, usage


def _image_geometry(image_b64: str) -> tuple[int, int] | None:
    """``(width, height)`` in pixels, or None when the header cannot be read.

    Decodes in full rather than truncating the base64: a truncated stream
    raises on its padding, and a JPEG's SOF marker sits at a format-dependent
    offset, so a "just the header" prefix is not a safe shortcut. These images
    are well under a megabyte.
    """
    if not image_b64:
        return None
    try:
        import base64 as _b64
        import io as _io

        from PIL import Image as _Image  # lazy: keeps this module importable
        with _Image.open(_io.BytesIO(_b64.b64decode(image_b64))) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# A resolution threshold was proposed here and REJECTED. Recording the negative
# result so nobody re-derives it.
#
# The claim was: a figure below some long-edge pixel count is too coarse to
# read taxon labels off, so warn the user instead of returning a confident
# empty result. The motivating measurement is real -- Motoyama 2019 Fig. 1 at
# 1751x2892 extracts fine, and the SAME figure downsampled to half resolution
# extracts ZERO zones, not a few wrong ones. The step is a cliff.
#
# It does not become a size rule, because the evidence contradicts every size
# rule tried:
#
#     Motoyama Fig. 1 @ half resolution   875 x 1446   -> 0 zones   FAIL
#     Bjorklund 2019 Fig. 4               1331 x  788  -> 189 rows  PASS
#     Matsuoka 2019 Fig. 1                1096 x 1316  ->  41 rows  PASS
#     Matsuzaki 2020 Fig. 10             1316 x  840  ->  39 rows  PASS
#
# An 875px edge fails while an 788px edge passes, so neither edge nor area nor
# long-edge separates the cases. What actually decides it is how many pixels
# land on a ROTATED taxon label, which depends on the figure's aspect ratio and
# on where the model is looking -- not on any single number. A threshold here
# would have been a plausible-looking constant that fires on figures that work
# and stays quiet on ones that do not, which is worse than saying nothing.
#
# What is reported instead is the measurement itself: ``image_pixels`` in the
# usage block, unconditionally, so a user comparing two runs can see the input
# size that produced each result. Turning that into a real warning needs a
# corpus of figures with known outcomes, not two data points.
# ---------------------------------------------------------------------------


def estimate_image_tokens(image_b64: str) -> int | None:
    """Rough vision-token cost of one image, from its real pixel size.

    Anthropic's published rule: the image is fitted to at most 1568px on the
    long edge, then costs ~750 tokens per (w/750) x (h/750) tile, capped near
    1600. That is a MODEL-SPECIFIC formula, so the number is an estimate and
    is only ever reported as one.

    Returns None when the size cannot be read, which is a normal outcome
    (Pillow missing, a format the header scan does not cover) and must never
    raise.

    NOTE ON WHAT THIS NUMBER IS WORTH. Measured on ten real figures it came
    back as exactly 1600 for every one of them, from 1096x1316 to 1623x1310 --
    once both edges exceed 750px the tile count is 4, and 4 x 750 exceeds the
    cap. So the capped figure is real (the vision cost really is flat at this
    scale) but it cannot RANK figures by cost. ``_annotate_image_tokens``
    therefore also reports the pixel size and whether the cap bound, so a
    constant 1600 is readable as "capped" rather than being mistaken for a
    measurement that lost its discriminating power.
    """
    size = _image_geometry(image_b64)
    if not size:
        return None
    w, h = size
    if not w or not h:
        return None
    longest, shortest = max(w, h), min(w, h)
    if longest > 1568:
        scale = 1568.0 / longest
        w, h = int(w * scale), int(h * scale)
        longest, shortest = max(w, h), min(w, h)
    tiles = ((longest + 749) // 750) * ((shortest + 749) // 750)
    return min(tiles * 750, 1600)


def _annotate_image_tokens(usage: dict[str, Any], image_b64: str) -> None:
    """Attach the image estimate SEPARATELY from ``input_tokens``.

    AUDIT-2026-10-15: measured against the MiniMax Anthropic-compatible
    endpoint. A 1751x2892 chart reported ``input_tokens=97`` with
    ``cache_read_tokens=1920`` — 97 is the text, and the image was billed
    somewhere the usage block does not name. A smaller figure (343 KB) on the
    same endpoint reported 9606. So ``input_tokens`` means different things on
    different calls and providers, and the app's usage display was
    under-reporting vision cost with no way for the user to tell.

    The estimate is therefore reported as its own field and NEVER added into
    ``input_tokens``: for a provider that already bills the image there, adding
    it would double-count, and quietly inflating a number the user is billed on
    is worse than showing it separately.

    ``image_pixels`` and ``image_tokens_capped`` exist because the capped
    estimate is a CONSTANT for every figure in the size range this tool
    actually sees (see ``estimate_image_tokens``). Reporting 1600 with no
    context looks like a measurement that stopped working; with the pixel size
    and the cap flag beside it, it reads as what it is.
    """
    usage.setdefault("image_tokens_estimated", None)
    if not image_b64:
        return
    size = _image_geometry(image_b64)
    if size:
        usage["image_pixels"] = [size[0], size[1]]
    est = estimate_image_tokens(image_b64)
    if est is None:
        return
    usage["image_tokens_estimated"] = est
    usage["image_tokens_are_estimate"] = True
    if size:
        longest, shortest = max(size), min(size)
        if longest > 1568:
            scale = 1568.0 / longest
            longest, shortest = sorted((int(size[0] * scale),
                                       int(size[1] * scale)), reverse=True)
        uncapped = ((longest + 749) // 750) * ((shortest + 749) // 750) * 750
        usage["image_tokens_uncapped"] = uncapped
        usage["image_tokens_capped"] = bool(uncapped > est)


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------
# Status codes worth retrying (transient upstream errors).
# 401/403/400 are authentication / client errors - retrying them is futile
# and just wastes quota, so they're excluded.
#
# REVIEW-2026-10-19: 529 was missing, and it is the one that matters. 529 is
# the "overloaded" answer -- MiniMax returns
# `{"type":"error","error":{"type":"overloaded_error", ...}}` with it, meaning
# "the tier is busy, come back". Measured on MiniMax-M3.1-Flash: 1 of 8
# requests came back 529 while the model name was perfectly valid. Not
# retrying it turned a transient congestion blip into a failed EXTRACTION, and
# on the two-stage path -- four sequential calls -- the per-call rate compounds
# into the run's rate: at 12% per-call failure, only about half the runs
# finished. A 529 is the textbook "try again" answer.
# REVIEW-2026-10-20: the set now lives in provider_failure, so the
# classifier's status fallback and this name cannot drift apart. The
# classifier is what the retry layers actually ask.
_RETRYABLE_STATUS = RETRYABLE_STATUS

# Lazy import to avoid circular dependency and keep this module importable in isolation.
def _get_error_utils():
    from . import error_utils
    return error_utils


def call_llm_api_with_retry(
    *,
    provider: LlmProvider,
    system_prompt: str,
    image_b64: str,
    media_type: str,
    user_text: str,
    max_tokens: int,
    timeout_sec: int = 120,
    capture_error_body: bool = False,
    progress_callback=None,
    retries: int = 3,
    backoff_factor: float = 1.6,
    initial_backoff_sec: float = 0.8,
    temperature: float | None = None,
    seed: int | None = None,
) -> tuple[str | None, bool, int | None, str, dict | None]:
    """call_llm_api wrapped with exponential-backoff retry on transient errors.

    Returns the same 5-tuple as ``call_llm_api``. The retry annotations
    (the ``[retry N/M after Xs]`` suffix) are appended to ``err_body`` so
    the operator can see how many attempts were spent before giving up.

    REVIEW-2026-09-20 #26: ``progress_callback`` is forwarded to every
    attempt now. Before, the retry path dropped it, so GUI/server callers that
    went through this wrapper lost the granular "submitting / uploading /
    thinking" stages and the UI froze on one spinner for the whole retry chain.
    A callback that raises is swallowed - the stage report is informational
    only and must not abort the call.

    REVIEW-2026-11-07 (low): temperature/seed are forwarded now — before,
    the retry chain silently dropped sampling parameters, so callers had
    to pre-merge them into provider.extra_body to get reproducibility.
    NOTE for cache-key builders: a call made with top-level temperature/
    seed differs from one without, so include them in the key (the server
    path already covers this via the extra_body fields in make_key).
    """
    last: tuple[str | None, bool, int | None, str, dict | None] = (None, False, None, "", None)
    if progress_callback is not None:
        # A UI callback that raises (e.g. a widget already torn down) must not
        # abort the extraction; drop the stage report, keep the call.
        _user_cb = progress_callback

        def progress_callback(stage, _cb=_user_cb):  # noqa: F811
            try:
                _cb(stage)
            except Exception:
                pass
    for attempt in range(retries):
        # REVIEW-2026-10-20: the response HEADERS, captured on the way out.
        # They carry the provider's own Retry-After, which is the one delay
        # it knows to be right, and x-should-retry, which is the provider
        # saying whether another attempt could help.
        last = call_llm_api(
            provider=provider,
            system_prompt=system_prompt,
            image_b64=image_b64,
            media_type=media_type,
            user_text=user_text,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            capture_error_body=capture_error_body,
            progress_callback=progress_callback,
            temperature=temperature,
            seed=seed,
        )
        resp_headers = _last_response_headers()
        text, truncated, status, err_body, usage = last
        if text is not None:
            return last
        # REVIEW-2026-10-20 (borrowed from opencode's provider-error.ts):
        # the CLASSIFICATION decides, and it reproduces the old status
        # policy for every status it knows -- so this is not a widening
        # of what gets retried, it is the same set with the cases the
        # status alone lies about taken out:
        #
        # * a 429 whose body says the plan is exhausted is not a
        #   throttle. Retrying it cannot succeed and it spends the OTHER
        #   bucket (MiniMax's Token Plan has a 5-hour bucket and a weekly
        #   one, and it was the weekly one that throttled the end-to-end
        #   regression);
        # * a 200 whose STREAM ended early is a prefix of the answer, and
        #   a prefix of a table is a wrong table.
        failure = classify_provider_failure(
            status, err_body, resp_headers)
        if not failure.retryable:
            # The wording must not contain the word "retry": a message
            # that says "retry" reads as a PROMISE of another attempt,
            # and this failure is not going to get one.
            note = ("[%s - not attempting it again: %s]"
                    % (failure.category, failure.reason))
            if err_body:
                last = (text, truncated, status, err_body + "\n" + note, usage)
            else:
                last = (text, truncated, status, note, usage)
            return last
        # MEDIUM-2: network errors (status=None) can persist across multiple
        # retries and may indicate a persistent connectivity issue.
        # Cap network-error retries at 1 so we fail fast rather than burning
        # through all retries on a persistently unreachable endpoint.
        if status is None and attempt >= 1:
            # Even when we cap network-error retries at 1 attempt, the caller
            # still needs to know we did try — annotate err_body with the
            # retry summary so the surfaced message reads "1 attempt, gave up"
            # instead of "single connection failure with no indication of
            # retry behaviour".
            # REVIEW-2026-09-10: this branch RETURNS immediately (it does not
            # sleep), so the suffix used to advertise a delay that was never
            # waited and an attempt that never happens ("retry 2/3 after 1.6s"
            # with retries=3 on a give-up path). Report the attempts actually
            # made.
            attempts_made = attempt + 1
            net_suffix = (f"[{attempts_made} attempt"
                          f"{'' if attempts_made == 1 else 's'}"
                          f" — network error, giving up]")
            if err_body:
                last = (text, truncated, status, err_body + "\n" + net_suffix, usage)
            else:
                last = (text, truncated, status, net_suffix, usage)
            return last
        # Use error_utils.get_retry_delay for jitter (DSH pattern: 0.75x-1.0x of base delay)
        error_utils = _get_error_utils()
        delay = error_utils.get_retry_delay(
            status=status,
            # REVIEW-2026-10-20: the headers ARE available now -- they are
            # captured on the way out of the call above. A provider that
            # answers "429 Retry-After: 120" is asking for 120 seconds, and
            # the blind backoff this used to use was the opposite of what it
            # asked for.
            headers=resp_headers,
            attempt=attempt,
            initial_delay=initial_backoff_sec,
            backoff_factor=backoff_factor,
        )
        backoff = delay
        is_final = attempt == retries - 1
        if is_final:
            # AUDIT-2026-09-27 P2: no retry follows this attempt, so a
            # "[retry N/N after Xs]" line promises a delay that is never
            # waited and an attempt that never happens. It is also the LAST
            # line the user reads, because each attempt's fresh err_body
            # replaces the previous one -- so on a 429/503 that exhausts the
            # budget (the common case for a rate-limited API) every failed
            # extraction ends by claiming another retry is coming.
            # The network-error branch above already reports the attempts
            # actually made (REVIEW-2026-09-10); this is the same repair for
            # the status branch, which that review did not cover.
            made = attempt + 1
            tail = ("HTTP %s" % status) if status is not None else "network error"
            suffix = "[%d attempt%s - %s, giving up]" % (
                made, "" if made == 1 else "s", tail)
        else:
            suffix = f"[retry {attempt + 1}/{retries} after {backoff:.1f}s]"
        if err_body:
            err_body = err_body + "\n" + suffix
        elif status is not None:
            err_body = f"HTTP {status}\n" + suffix
        else:
            err_body = suffix
        last = (text, truncated, status, err_body, usage)
        if is_final:
            return last
        # UX-2026-10-18 (流畅性 A3): announce the wait BEFORE sleeping.
        #
        # The retry chain used to be completely invisible: each attempt ended
        # with a bare time.sleep() and the GUI's status line froze on whatever
        # stage the last attempt reported (typically "submitting") for the
        # whole backoff, then again for the whole next request. Measured on
        # the real path (headers=None, so Retry-After is unreachable and the
        # delay comes from the jittered exponential curve): worst case over
        # three attempts is ~4.3 s of silence, and the 120 s per-attempt
        # timeout in llm.call_llm_api sits in front of it -- so a rate-limited
        # extraction could sit for minutes between two visible status changes
        # and read as a hung UI.
        #
        # The seconds are the load-bearing part of the payload: "retrying" on
        # its own tells the operator nothing about how long they are in for,
        # which is the actual question during a wait. The format reuses the
        # "analyzing:{n}/{runs}:{elapsed}s" shape _run_multi already emits
        # (gui_fluent.py) so _on_progress parses it with the same split(":")
        # branch and no new placeholder scheme is introduced.
        #
        # Not emitted when is_final (above returns first): there is no next
        # attempt then, and promising one is exactly the AUDIT-2026-09-27 P2
        # defect this file already repaired once for err_body.
        #
        # Routed through the local `progress_callback` wrapper defined above,
        # NOT the caller's original: that wrapper exists so a UI callback that
        # raises (widget torn down mid-extraction) cannot abort the call, and
        # a new event type must inherit that protection rather than bypass it.
        #
        # The `is not None` guard is required, not defensive padding: the
        # wrapper is only DEFINED when the caller passed a callback, so with
        # progress_callback=None the name still resolves to the parameter
        # (None) and calling it unconditionally would raise TypeError on
        # every retry -- turning a recoverable 429 into a crash. Every other
        # call site in this module guards the same way (llm.py:1207, 1438).
        if progress_callback is not None:
            progress_callback(
                f"retrying:{attempt + 2}/{retries}:{backoff:.1f}s")
        time.sleep(backoff)
    return last


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


@dataclass
class ConnectionResult:
    ok: bool = False
    latency_ms: int = 0
    status: int | None = None
    error_key: str | None = None
    # AUDIT-2026-09-29: the upstream's own words, REDACTED, for the failure
    # case only. The probe already had this string -- `_do_probe` returns it
    # and the loop assigned it to `last_err_body` on every candidate -- and
    # the function then dropped it, so a failed connection test could only
    # say "err.http (HTTP 400)" while the server had actually said "the model
    # does not exist" or "your quota is exhausted". For a feature whose entire
    # job is diagnosing a CONFIGURATION, that is the wrong default.
    #
    # Redacted, not raw: this body is where a rejected key comes back echoed.
    # It shares rca_core/redact.py with the server, which is the whole reason
    # that module exists -- a second copy of that regex is how the AUDIT-
    # 2026-09-27 `|`-precedence narrowing would have come back. Empty on
    # success, and empty whenever there was no body to redact.
    error_body: str = ""
    models_sample: list[str] = field(default_factory=list)
    # Consecutive failures count — used to badge card health and for retry
    # decisions in the UI layer.
    consecutive_failures: int = 0


def _now_ms() -> int:
    # L3: monotonic clock so latency_ms never goes negative on systems
    # whose wall clock jumps (NTP sync, manual adjustment on Windows).
    return time.monotonic_ns() // 1_000_000


_MODEL_PROBE_LIMIT = 25  # M15: bounded so we don't drag hundreds of names into the picker.


def _extract_models(payload: dict[str, Any], fmt: ApiFormat) -> list[str]:
    """Best-effort pull of a few model names from a provider's /models list.

    M15: cap at ``_MODEL_PROBE_LIMIT`` so a provider with hundreds of
    models doesn't bloat the picker dropdown.
    """
    out: list[str] = []
    items: list[Any] = []
    if fmt == ApiFormat.GEMINI:
        items = payload.get("models") or []
    else:
        items = payload.get("data") or []
    for it in items[:_MODEL_PROBE_LIMIT]:
        if isinstance(it, dict):
            name = it.get("id") or it.get("name") or ""
            if name:
                out.append(str(name))
    return out


def list_models(provider: LlmProvider, timeout_sec: int = 15) -> list[str]:
    """The model IDs the endpoint actually serves, best effort.

    cc-switch's "自动获取模型": the user pastes a key and picks the model
    from what the endpoint serves, instead of copying a slug off a web page.
    A wrong model name is the commonest configuration mistake, and it fails
    LATE -- at the first extraction, with a 404 or a 400, long after the
    settings looked fine.

    Returns an empty list when the endpoint has no listing route (many
    Chinese relay services do not) or the key is wrong; the caller shows the
    user what to type instead. Never raises.
    """
    if provider is None:
        return []
    try:
        from .ssrf import validate_endpoint_local_ok
        ok, _why = validate_endpoint_local_ok(provider.endpoint)
        if not ok:
            return []
    except Exception:
        pass
    fmt = provider.api_format
    if getattr(provider, "full_url", False):
        # A FULL-URL endpoint has no listing route: it IS the request URL.
        # Looking for ``/v1/models`` under it would be a guess.
        return []
    base = _api_base(provider.endpoint)
    if fmt == ApiFormat.GEMINI:
        target = _gemini_path(base, "/v1beta/models")
        headers = {"x-goog-api-key": provider.api_key,
                   "x-api-key": provider.api_key}
    elif fmt == ApiFormat.ANTHROPIC:
        target = _endpoint_path(base, "/v1/models")
        headers = {"x-api-key": provider.api_key,
                   "anthropic-version": "2023-06-01"}
    else:
        target = _endpoint_path(base, "/v1/models")
        headers = {"Authorization": f"Bearer {provider.api_key}"}
    headers.update(provider.extra_headers or {})
    try:
        payload_bytes, _status, _err = _get_json(target, headers, timeout_sec)
    except Exception:
        return []
    if payload_bytes is None:
        return []
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    return _extract_models(payload, fmt)


def _probe_openai_models(provider: LlmProvider, timeout_sec: int) -> ConnectionResult:
    # /v1/models is a GET-only listing endpoint on the OpenAI-compatible API.
    # The previous implementation POSTed to it (via _post_json), which the
    # official gateways reject with 405 Method Not Allowed — the connection
    # test then falsely reports a working endpoint as broken. Use GET; on a
    # 405 / 404 / non-2xx response, fall back to a 1-token generation probe
    # so we still verify the key works end-to-end.
    target = _endpoint_path(_api_base(provider.endpoint), "/v1/models")
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
    }
    headers.update(provider.extra_headers or {})
    t0 = _now_ms()
    payload_bytes, status, err_body = _get_json(target, headers, timeout_sec)
    res = ConnectionResult(ok=False, latency_ms=_now_ms() - t0, status=status)
    # /models returned a usable listing — done.
    if payload_bytes is not None:
        try:
            payload = json.loads(payload_bytes.decode("utf-8-sig"))
        except Exception:
            res.error_key = "err.parse"
            return res
        res.ok = True
        res.models_sample = _extract_models(payload, ApiFormat.OPENAI)
        # If /models is empty or absent, fall back to a 1-token generate probe.
        if not res.models_sample:
            return _probe_minimal_generate(provider, timeout_sec, ApiFormat.OPENAI)
        return res
    # /models failed — could be 401 (bad key), 405/404 (gateway doesn't list
    # models), or network. Distinguish auth failures (return early) from
    # "no listing endpoint" (fall through to generate-probe).
    if status in (401, 403):
        res.error_key = "err.401" if status == 401 else "err.403"
        return res
    # Any other non-2xx: try the minimal generate probe so the user still
    # gets a green checkmark when the endpoint is actually reachable.
    fallback = _probe_minimal_generate(provider, timeout_sec, ApiFormat.OPENAI)
    # If fallback also failed, surface its status / error_key for context.
    if not fallback.ok:
        return fallback
    return fallback


def _probe_anthropic_models(provider: LlmProvider, timeout_sec: int) -> ConnectionResult:
    # Anthropic has no public /models endpoint on the standard API; do a
    # minimal generate probe instead.
    return _probe_minimal_generate(provider, timeout_sec, ApiFormat.ANTHROPIC)


def _probe_gemini_models(provider: LlmProvider, timeout_sec: int) -> ConnectionResult:
    # /v1beta/models is a GET-only listing endpoint. Same fix as the OpenAI
    # probe: the previous POST implementation 405'd against the official
    # Google Generative Language gateway.
    #
    # AUDIT-2026-09-29: `model = provider.model or "gemini-2.5-pro"` sat here
    # and was never read (ruff F841). A listing endpoint takes no model
    # parameter, so the binding was inert; it is removed rather than "used",
    # because there is nothing to pass it to.
    base = _api_base(provider.endpoint)
    # C4: put API key in x-api-key header (same as _call_gemini). Earlier
    # this function put it in the URL via ?key=... which leaked the key
    # into proxy/Referer logs. Users who need the URL form can override via
    # `extra_headers={"X-Use-Url-Key": "1"}` (gate detected via the body shape).
    target = _gemini_path(base, "/v1beta/models")
    headers = {
        "x-goog-api-key": provider.api_key,
        "x-api-key": provider.api_key,
    }
    headers.update(provider.extra_headers or {})
    t0 = _now_ms()
    payload_bytes, status, err_body = _get_json(target, headers, timeout_sec)
    res = ConnectionResult(ok=False, latency_ms=_now_ms() - t0, status=status)
    if payload_bytes is not None:
        try:
            payload = json.loads(payload_bytes.decode("utf-8-sig"))
        except Exception:
            res.error_key = "err.parse"
            return res
        res.ok = True
        res.models_sample = _extract_models(payload, ApiFormat.GEMINI)
        if not res.models_sample:
            return _probe_minimal_generate(provider, timeout_sec, ApiFormat.GEMINI)
        return res
    if status in (401, 403):
        res.error_key = "err.401" if status == 401 else "err.403"
        return res
    # /models endpoint not available (405/404/etc.) — fall back to generate
    # probe so a working key on a third-party Gemini proxy still passes.
    fallback = _probe_minimal_generate(provider, timeout_sec, ApiFormat.GEMINI)
    if not fallback.ok:
        return fallback
    return fallback


def _probe_minimal_generate(
    provider: LlmProvider, timeout_sec: int, fmt: ApiFormat
) -> ConnectionResult:
    """Send a minimal generate request to verify the model + key.

    If the configured model is rejected (401/403/400), retry with a
    well-known cheap fallback.  Many aggregator endpoints don't validate
    the model name strictly and accept the fallback even when the
    configured name is slightly wrong or unrecognized.
    """
    # Defense-in-depth: if this probe is ever reached with a bad endpoint
    # (callers normally validate first), surface err.badEndpoint instead of
    # letting urllib raise URLError and get masked as err.network. Mirrors the
    # guard in test_llm_connection / _call_* so every path agrees on the policy.
    if not _is_safe_endpoint(provider.endpoint):
        return ConnectionResult(
            ok=False, latency_ms=0, status=None, error_key="err.badEndpoint"
        )
    # Canonical fallback models + a slightly larger max_tokens to avoid
    # endpoints that reject max_tokens=1 as too small.
    ANTHROPIC_FALLBACKS = ["claude-3-haiku-20240307", "claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022"]
    OPENAI_FALLBACKS = ["gpt-4o-mini", "gpt-4o", "gpt-4o-2024-08-06"]
    GEMINI_FALLBACKS = ["gemini-2.0-flash", "gemini-1.5-flash"]
    PROBE_MAX_TOKENS = 8   # avoid "max_tokens too small" rejections

    def _do_probe(model: str) -> tuple[bytes | None, int | None, bytes, int]:
        """Returns (payload, status, err_body, latency_ms)."""
        t0 = _now_ms()
        if fmt == ApiFormat.OPENAI:
            target = _request_target(provider, "/v1/chat/completions")
            body = {"model": model, "max_tokens": PROBE_MAX_TOKENS,
                    "messages": [{"role": "user", "content": "hi"}]}
            headers = {"Authorization": f"Bearer {provider.api_key}",
                       "content-type": "application/json"}
        elif fmt == ApiFormat.GEMINI:
            base = _api_base(provider.endpoint)
            # REVIEW-2026-09-20 #22: same version-tail + quoting rules as
            # _call_gemini, otherwise the connection test probes a URL the
            # extraction path never uses (and a tuned model id broke it).
            target = _gemini_path(
                base,
                f"/v1beta/models/{urllib.parse.quote(model, safe='')}:generateContent")
            body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                    "generation_config": {"max_output_tokens": PROBE_MAX_TOKENS}}
            headers = {"content-type": "application/json",
                       "x-goog-api-key": provider.api_key,
                       "x-api-key": provider.api_key}
        else:  # ANTHROPIC
            target = _request_target(provider, "/v1/messages")
            body = {"model": model, "max_tokens": PROBE_MAX_TOKENS,
                    "messages": [{"role": "user", "content": "hi"}]}
            headers = {"x-api-key": provider.api_key,
                       "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        headers.update(provider.extra_headers or {})
        body.update(provider.extra_body or {})
        payload_bytes, status, err_body = _post_json(target, body, headers, timeout_sec)
        return payload_bytes, status, err_body, _now_ms() - t0

    # Build candidate model list: configured model first, then fallbacks.
    configured = provider.model.strip() if provider.model else ""
    if fmt == ApiFormat.OPENAI:
        fallbacks = OPENAI_FALLBACKS
    elif fmt == ApiFormat.GEMINI:
        fallbacks = GEMINI_FALLBACKS
    else:
        fallbacks = ANTHROPIC_FALLBACKS

    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for fb in fallbacks:
        if fb not in candidates:
            candidates.append(fb)

    last_status = None
    last_err_body = b""
    # REVIEW-2026-11-07 (low): the failure result used to report
    # ``best_latency`` which was never assigned — every failed test showed
    # "0 ms". Track the LAST probe's latency instead so the number the
    # user sees is a real network measurement.
    last_latency = 0

    for model in candidates:
        payload_bytes, status, err_body, latency = _do_probe(model)
        last_status = status
        last_err_body = err_body
        last_latency = latency
        if payload_bytes is not None:
            # HTTP 2xx — endpoint accepted the request.
            res = ConnectionResult(ok=True, latency_ms=latency, status=status)
            return res
        # Non-2xx or network error: check if we should keep trying.
        #
        # Bug-7 fix: distinguish "credential rejected" (401/403) from
        # "model not found" (400/404). On 401/403 the configured key is
        # bad — retrying with fallback models only burns quota and may
        # trigger upstream anti-abuse, so bail immediately. On 400/404
        # the configured model name is the problem — *that's* when the
        # fallback list is useful.
        # REVIEW-2026-11-07 (low): the early-stop now applies to ANY
        # candidate, not only the configured model — the same API key
        # authenticates every probe, so a 401/403 on a fallback model
        # means the key itself is rejected and the rest of the list is
        # guaranteed to fail the same way.
        if status in (401, 403):
            # Key is likely invalid; don't waste quota on fallbacks.
            break
        if status in (400, 404) and model != configured:
            # Configured model is rejected; we ARE trying fallbacks now,
            # and they all failed too. Stop early to avoid further burns.
            # (Falls through to the loop's natural end.)
            pass

    # All candidates exhausted.
    res = ConnectionResult(ok=False, latency_ms=last_latency,
                           status=last_status or None)
    # AUDIT-2026-09-29: `last_err_body` was tracked through every candidate
    # and then dropped on the floor. This is the same shape as the
    # `best_latency` bug REVIEW-2026-11-07 fixed one line above it -- a
    # measurement taken carefully and never reported -- and the fix is the
    # same: report the LAST probe's value. Redacted on the way in, because an
    # upstream error body is exactly where a rejected key comes back.
    #
    # _decode_err_body first, and that is not cosmetic: _post_json returns the
    # body as BYTES, and redact_error_body answers "" for anything that is not
    # a str (it is a shared helper and refuses to guess). Passing the bytes
    # straight in produced a redacted-to-nothing string -- the fix looked
    # present and was inert. Caught by the end-to-end probe, not by reading.
    res.error_body = redact_error_body(_decode_err_body(last_err_body))
    if last_status == 401:
        res.error_key = "err.401"
    elif last_status == 403:
        res.error_key = "err.403"
    elif last_status is None:
        res.error_key = "err.network"
    else:
        res.error_key = "err.http"
    return res


def test_llm_connection(provider: LlmProvider, timeout_sec: int = 10) -> ConnectionResult:
    """Verify a provider is reachable + its key works.

    Never raises. Returns a ConnectionResult with ``ok`` + ``error_key``.
    Fast path: probe the model-list endpoint. If that's empty/unsupported,
    fall back to a 1-token generate.
    """
    # NETERR fix (2026-07-27): validate the endpoint *before* any probe so an
    # empty / malformed / private / blocked endpoint surfaces as err.badEndpoint
    # rather than being handed to urllib (which would raise URLError and get
    # masked as err.network down in _post_json / _get_json). This is exactly the
    # "config was silently changed, now it fails" trap: a bad endpoint must read
    # as a config error, not a network error.
    # We deliberately use _is_safe_endpoint (NOT validate_endpoint_or_raise):
    # _is_safe_endpoint whitelists loopback (local Ollama / dev test servers)
    # while still rejecting private / link-local / non-https public hosts —
    # the SAME policy the live extract path uses (_call_anthropic/_call_openai/
    # _call_gemini). Using validate_endpoint_or_raise would wrongly reject
    # http://127.0.0.1 (a legitimate desktop LLM), breaking local models and
    # our own loopback integration tests. _is_safe_endpoint never raises, so
    # the "Never raises" contract below is preserved without a try/except.
    if not _is_safe_endpoint(provider.endpoint):
        return ConnectionResult(
            ok=False, latency_ms=0, status=None, error_key="err.badEndpoint"
        )
    dispatch = {
        ApiFormat.ANTHROPIC: _probe_anthropic_models,
        ApiFormat.OPENAI: _probe_openai_models,
        ApiFormat.GEMINI: _probe_gemini_models,
    }
    fn = dispatch.get(provider.api_format) or _probe_anthropic_models
    return fn(provider, timeout_sec)
