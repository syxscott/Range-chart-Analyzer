"""One adapter per API format: build the request, read the reply.

REVIEW-2026-10-20. The three API formats were three FUNCTIONS in
``rca_core/llm.py`` -- ``_call_anthropic``, ``_call_openai``,
``_call_gemini`` -- and ``call_llm_api`` dispatched to them with a
hard-coded table. Adding a fourth format meant editing the module, and
the protocol knowledge (how a body is shaped, where the key goes, what
the reply looks like) was mixed into the transport.

This module is the other shape: a class per format, each owning its own
call, and a registry. A fourth format is now one class in this file plus
one line in the registry.

Two things are deliberate:

* The bodies were MOVED, not rewritten. Every quirk they carry is
  load-bearing, and most of them are load-bearing because of a bug that
  was measured: the reasoning-model field names, the ``role:system``
  merge, Gemini's ``thought`` parts, the ``utf-8-sig`` decode, the
  "empty 200 is not an empty answer" rule. Re-typing them is how those
  get lost.

* The transport lives in ``llm.py`` and is imported LAZILY, inside the
  methods. ``llm.py`` imports this module at module level, so the
  dependency has to be one-way at import time -- and a lazy import
  inside the method also keeps the test hooks alive: a test that
  patches ``llm._post_json`` or ``llm._read_response`` still reaches
  the patched function, because the lookup happens at call time.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .llm import ApiFormat, LlmProvider


def adapter_for(api_format):
    """The adapter that speaks this API format.

    The registry itself lives in ``llm.py`` (next to ``ApiFormat``, which
    it is keyed by), and the import is resolved at CALL time -- the two
    modules need each other, and a module-level import in both
    directions would be a cycle.
    """
    from .llm import _adapters
    return _adapters()[api_format]


class ProtocolAdapter:
    """One API format.

    A subclass owns the WHOLE call for its format: the endpoint, the
    request body, the headers, and the reading of the reply. The
    transport (POST, SSE, the error body) stays in ``llm.py``, because
    it is the same for every format.
    """
    #: The format's name, for logs and for the registry key.
    name: str = ""
    #: Whether this format can answer as a stream.
    supports_stream: bool = False

    def call(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int,
             timeout_sec: int, progress_callback=None,
             stream: bool = False, thinking_callback=None
             ) -> tuple[str | None, bool, int | None, str, dict | None]:
        raise NotImplementedError

    # -- the pieces the shared transport needs ----------------------------

    def guard(self, provider) -> tuple[bool, str]:
        """Is this endpoint allowed to be called at all?

        The policy lives in ``rca_core.ssrf`` and is shared with the
        connection test; the adapter only decides which failure WORD to
        use.
        """
        from .ssrf import validate_endpoint_local_ok
        ok, why = validate_endpoint_local_ok(provider.endpoint)
        return ok, ("endpoint rejected by SSRF guard: %s" % why if not ok else "")

    def endpoint(self, provider) -> str:
        from .llm import _request_target
        return _request_target(provider, self.resource_path(provider))

    def resource_path(self, provider) -> str:
        raise NotImplementedError

    def headers(self, provider) -> dict[str, str]:
        raise NotImplementedError

    def body(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int) -> dict[str, Any]:
        raise NotImplementedError

    def reasoning_patch(self, provider) -> dict[str, Any]:
        """The reasoning/effort fields this format understands."""
        return {}

    def parse(self, payload: Any) -> tuple[str | None, bool, str]:
        """The reply's text, whether it was cut short, and the reason.

        Returns ``(text, truncated, err)``. ``text`` is None when the
        reply carried no usable text -- and ``err`` then carries WHY,
        because "the model answered with nothing" and "the model
        answered with an error object" are different problems.
        """
        raise NotImplementedError


class AnthropicAdapter(ProtocolAdapter):
    """The Anthropic Messages API (and everything that speaks it)."""

    name = "anthropic"
    supports_stream = True

    def resource_path(self, provider) -> str:
        return "/v1/messages"

    def headers(self, provider) -> dict[str, str]:
        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        headers.update(provider.extra_headers or {})
        return headers

    def body(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": provider.model,
            "max_tokens": max_tokens or 4000,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64",
                                                    "media_type": media_type or "image/png",
                                                    "data": image_b64}},
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        }
        # REVIEW-2026-10-19: reasoning effort, BEFORE ``extra_body`` so a user
        # who put ``output_config`` in extra_body by hand still wins --
        # extra_body is the documented escape hatch and must keep the last
        # word.
        body.update(self.reasoning_patch(provider))
        body.update(provider.extra_body or {})
        return body

    def reasoning_patch(self, provider) -> dict[str, Any]:
        from .llm import ApiFormat, _reasoning_request_patch
        return _reasoning_request_patch(provider, ApiFormat.ANTHROPIC)

    def parse(self, payload: Any) -> tuple[str | None, bool, str]:
        from .llm import _assemble_anthropic_payload, _summarise_empty_payload
        text, truncated, _status, err, _usage = _assemble_anthropic_payload(
            payload)
        if text is None or not str(text).strip():
            return None, truncated, _summarise_empty_payload(payload)
        return text, truncated, ""

    def call(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int,
             timeout_sec: int, progress_callback=None,
             stream: bool = False, thinking_callback=None
             ) -> tuple[str | None, bool, int | None, str, dict | None]:
        from .llm import (_read_response, _read_response_stream, parse_usage)
        # P0-2 (REVIEW-2026-07-25): validate endpoint before any network call.
        # The server.py path validated before dispatching, but the GUI path loads
        # providers from disk without re-validation. This unifies both paths.
        # REVIEW-2026-07-31: validate_endpoint_local_ok — same policy as the
        # connection test and the Gemini path (loopback + plain-http allowed
        # for local Ollama-style endpoints; everything else stays
        # SSRF-rejected).
        ok_ep, why_ep = self.guard(provider)
        if not ok_ep:
            return None, False, None, why_ep, None
        if not image_b64:
            return None, False, None, "", None
        target = self.endpoint(provider)
        body = self.body(provider, system_prompt=system_prompt,
                          image_b64=image_b64, media_type=media_type,
                          user_text=user_text, max_tokens=max_tokens)
        headers = self.headers(provider)
        if progress_callback:
            progress_callback("uploading")
        if stream:
            # REVIEW-2026-10-19: the stream carries the model's THINKING as it
            # is produced, which is the only way a progress indicator can
            # show what the model is actually doing instead of a spinner that
            # says "working" for a hundred seconds. The reply is assembled
            # into the same 5-tuple, so nothing above this line changes.
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


class OpenAIAdapter(ProtocolAdapter):
    """The OpenAI Chat Completions API (and everything that speaks it)."""

    name = "openai"

    def resource_path(self, provider) -> str:
        return "/v1/chat/completions"

    def headers(self, provider) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "content-type": "application/json",
        }
        headers.update(provider.extra_headers or {})
        return headers

    def body(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int) -> dict[str, Any]:
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
        return body

    def parse(self, payload: Any) -> tuple[str | None, bool, str]:
        from .llm import _coerce_text, _summarise_empty_payload
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
            return None, truncated, _summarise_empty_payload(payload)
        return raw_text, truncated, ""

    def call(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int,
             timeout_sec: int, progress_callback=None,
             stream: bool = False, thinking_callback=None
             ) -> tuple[str | None, bool, int | None, str, dict | None]:
        from .llm import _decode_err_body, _post_json, parse_usage
        ok_ep, why_ep = self.guard(provider)
        if not ok_ep:
            return None, False, None, why_ep, None
        if not image_b64:
            return None, False, None, "", None
        target = self.endpoint(provider)
        body = self.body(provider, system_prompt=system_prompt,
                          image_b64=image_b64, media_type=media_type,
                          user_text=user_text, max_tokens=max_tokens)
        headers = self.headers(provider)
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
            # payload decodes. A gateway or proxy that prepends a UTF-8 BOM made
            # json.loads raise "Unexpected UTF-8 BOM" on a perfectly good 2xx
            # body, and the except below then reported the whole response as an
            # error body. utf-8-sig is a no-op when there is no BOM.
            payload = json.loads(payload_bytes.decode("utf-8-sig"))
        except Exception:
            # REVIEW-2026-09-10: a 2xx that is not JSON (a WAF page, an SSE
            # stream) must hand its bytes back as the error body, not vanish.
            return None, False, status, _decode_err_body(payload_bytes), None
        text, truncated, parse_err = self.parse(payload)
        if text is None:
            # The fifth element is the PAYLOAD, not a usage dict. That is
            # what the original code returned, and it is preserved
            # deliberately: the shape of this tuple is consumed in several
            # places and changing it is not this refactor's business.
            return (None, truncated, status, (parse_err or err_str),
                    payload if isinstance(payload, dict) else None)
        usage = None
        u = parse_usage(payload, "openai")
        if u:
            usage = dict(u); usage["estimated"] = False
        return text, truncated, status, err_str, usage


class GeminiAdapter(ProtocolAdapter):
    """Google's Generative Language API."""

    name = "gemini"

    def guard(self, provider) -> tuple[bool, str]:
        from .llm import _is_safe_endpoint
        if _is_safe_endpoint(provider.endpoint):
            return True, ""
        return False, "bad endpoint"

    def resource_path(self, provider) -> str:
        # Gemini puts the MODEL in the path, and a tuned model id may
        # contain a reserved character, so the path is built here rather
        # than by the shared ``_request_target``.
        from .llm import _api_base, _gemini_path
        model = provider.model or "gemini-2.5-pro"
        base = _api_base(provider.endpoint)
        # URL-encode the model name so tuned models like ``tunedModels/foo``
        # (and any future model id that contains a reserved char) don't break
        # the URL parser or accidentally introduce an extra path segment.
        safe_model = urllib.parse.quote(model, safe="")
        if getattr(provider, "full_url", False):
            # A FULL-URL endpoint is the request URL, verbatim.
            return (provider.endpoint or "").strip()
        return _gemini_path(base, f"/v1beta/models/{safe_model}:generateContent")

    def headers(self, provider) -> dict[str, str]:
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
        return headers

    def body(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int) -> dict[str, Any]:
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
        # ``generation_config``. Official Google answers an unknown root field
        # with 400 ``Unknown name`` (or silently ignores it), so the
        # reproducibility knobs were a no-op on this format. Move them in; a
        # value the user set directly inside ``generation_config`` still wins.
        # Copy before mutating: ``body["generation_config"]`` may be the very
        # dict owned by ``provider.extra_body``, and editing it in place would
        # leak the per-call sampling into the persisted provider.
        gen_cfg = body.get("generation_config")
        gen_cfg = dict(gen_cfg) if isinstance(gen_cfg, dict) else {}
        for _key in ("temperature", "seed"):
            if _key in body:
                gen_cfg.setdefault(_key, body.pop(_key))
        body["generation_config"] = gen_cfg
        return body

    def parse(self, payload: Any) -> tuple[str | None, bool, str]:
        from .llm import _coerce_text, _summarise_empty_payload
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
                # ``part["text"]`` may legitimately be null/numeric from a
                # broken proxy; ``raw_text += None`` used to raise TypeError.
                raw_text += _coerce_text(part.get("text"))
        finish = first.get("finishReason")
        truncated = finish in ("MAX_TOKENS", "LENGTH")
        if not raw_text.strip():
            # REVIEW-2026-09-20 #21: same contract as the Anthropic / OpenAI
            # paths - an empty 200 is NOT "the model answered with nothing";
            # hand the upstream reason (blockReason, an in-body error) back as
            # err_body.
            return None, truncated, _summarise_empty_payload(payload_obj)
        return raw_text, truncated, ""

    def call(self, provider, *, system_prompt: str, image_b64: str,
             media_type: str, user_text: str, max_tokens: int,
             timeout_sec: int, progress_callback=None,
             stream: bool = False, thinking_callback=None
             ) -> tuple[str | None, bool, int | None, str, dict | None]:
        from .llm import _decode_err_body, _post_json, parse_usage
        if not image_b64:
            return None, False, None, "", None
        # F-26 fix: validate the endpoint before using it. The server.py caller
        # already validated, but this guards against the GUI path (providers
        # loaded from disk) and any future caller that bypasses server.py.
        ok_ep, why_ep = self.guard(provider)
        if not ok_ep:
            return None, False, None, why_ep, None
        target = self.resource_path(provider)
        body = self.body(provider, system_prompt=system_prompt,
                          image_b64=image_b64, media_type=media_type,
                          user_text=user_text, max_tokens=max_tokens)
        headers = self.headers(provider)
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
        text, truncated, parse_err = self.parse(payload)
        usage = None
        u = parse_usage(payload if isinstance(payload, dict) else {}, "gemini")
        if u:
            usage = dict(u); usage["estimated"] = False
        if text is None:
            return None, truncated, status, (parse_err or err_str), None
        return text, truncated, status, err_str, usage
