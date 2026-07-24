"""TDD tests for the llm.py findings assigned to this agent.

These cover:
  - HIGH: connection test uses GET (not POST) on /models endpoints.
  - HIGH: _api_base must only strip /v1 (or whitelist per api_format),
          not every trailing /vN segment.
  - MEDIUM: OpenAI reasoning models: omit role:system and response_format.
  - LOW: ProviderStore.save() concurrent-safe (per-instance RLock + process lock).
  - LOW: Gemini call forces response_mime_type=application/json.
  - LOW: Gemini response parser ignores thought parts.
  - LOW: Fallback usage estimation includes image tokens.
  - LOW: Anthropic reader concatenates all text blocks.
  - LOW: Gemini model name URL-encoded in the URL path.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.llm as L  # noqa: E402
from rca_core.llm import (  # noqa: E402
    ApiFormat,
    LlmProvider,
    ProviderStore,
    _api_base,
    _call_anthropic,
    _call_gemini,
    _call_openai,
    _extract_models,
    _probe_openai_models,
    _probe_gemini_models,
    _read_response,
)


_pass = 0
_fail = 0


def _check(name, cond, detail=""):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name, detail)


# ----------------------------------------------------------------------------
# HIGH: _api_base only strips /v1 (or whitelists per api_format).
# ----------------------------------------------------------------------------
def test_api_base_strips_v1():
    """https://api.openai.com/v1 -> https://api.openai.com (no double-v1)."""
    _check("api-base-strips-v1",
           _api_base("https://api.openai.com/v1") == "https://api.openai.com")


def test_api_base_preserves_v1beta_for_gemini():
    """Gemini endpoint with /v1beta must NOT be stripped — Gemini path needs it."""
    base = _api_base("https://generativelanguage.googleapis.com/v1beta")
    _check("api-base-preserves-v1beta",
           base == "https://generativelanguage.googleapis.com/v1beta",
           f"got {base!r}")


def test_api_base_preserves_anthropic_compat_path():
    """Anthropic-compat endpoint with /anthropic suffix must be preserved."""
    base = _api_base("https://api.minimaxi.com/anthropic")
    _check("api-base-preserves-anthropic-suffix",
           base == "https://api.minimaxi.com/anthropic",
           f"got {base!r}")


def test_api_base_v2_preserved():
    """Non-/v1 trailing segments (like /v2) must NOT be stripped unless whitelisted."""
    base = _api_base("https://example.com/v2")
    _check("api-base-preserves-v2",
           base == "https://example.com/v2",
           f"got {base!r}")


# ----------------------------------------------------------------------------
# HIGH: connection test should use GET, not POST, on /models endpoints.
# ----------------------------------------------------------------------------
class _MethodCapturingResponse:
    def __init__(self, data: bytes, status=200):
        self._data = data
        self.status = status
        self.reason = "OK"

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _make_get_only_server(handler):
    """Builds a fake urlopen that delegates to ``handler`` for all methods.

    The handler decides what to return — pass-through, HTTPError, etc. — so
    each test can model the exact upstream behavior it cares about (e.g.
    GET succeeds, POST succeeds; or GET 404s and POST succeeds).
    """
    captured = {"method": None, "url": None, "headers": {}, "body": None,
                "get_calls": 0, "post_calls": 0}

    def fake(req, timeout=None):
        method = req.get_method()
        captured["method"] = method
        captured["url"] = req.full_url
        captured["headers"] = {k: v for k, v in req.headers.items()}
        captured["body"] = req.data
        if method == "POST":
            captured["post_calls"] += 1
        else:
            captured["get_calls"] += 1
        return handler(req)

    return captured, fake


def test_probe_openai_models_uses_get():
    """The OpenAI /models probe must use GET (the endpoint is read-only)."""
    payload = json.dumps({"data": [{"id": "gpt-4o-mini"}]}).encode()

    def handler(req):
        # Real /v1/models refuses POST with 405.
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(
                req.full_url, 405, "Method Not Allowed",
                hdrs=None, fp=None,
            )
        return _MethodCapturingResponse(payload, 200)

    captured, fake = _make_get_only_server(handler)
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o",
        )
        result = _probe_openai_models(prov, timeout_sec=5)
    finally:
        urllib.request.urlopen = orig

    _check("probe-openai-models-method-is-get",
           captured["method"] == "GET",
           f"got method={captured['method']!r}")
    _check("probe-openai-models-no-post-attempt",
           captured["post_calls"] == 0,
           f"post_calls={captured['post_calls']}")
    _check("probe-openai-models-result-ok",
           result.ok,
           f"result={result}")


def test_probe_gemini_models_uses_get():
    """The Gemini /v1beta/models probe must use GET."""
    payload = json.dumps({"models": [{"name": "models/gemini-2.5-pro"}]}).encode()

    def handler(req):
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(
                req.full_url, 405, "Method Not Allowed",
                hdrs=None, fp=None,
            )
        return _MethodCapturingResponse(payload, 200)

    captured, fake = _make_get_only_server(handler)
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.GEMINI,
            endpoint="https://generativelanguage.googleapis.com/v1beta",
            api_key="k", model="gemini-2.5-pro",
        )
        result = _probe_gemini_models(prov, timeout_sec=5)
    finally:
        urllib.request.urlopen = orig

    _check("probe-gemini-models-method-is-get",
           captured["method"] == "GET",
           f"got method={captured['method']!r}")
    _check("probe-gemini-models-no-post-attempt",
           captured["post_calls"] == 0,
           f"post_calls={captured['post_calls']}")
    _check("probe-gemini-models-result-ok",
           result.ok,
           f"result={result}")


def test_probe_openai_models_falls_back_on_404():
    """When /models 404s, fall back to minimal-generate (which still POSTs)."""
    def handler(req):
        if req.get_method() == "GET":
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", hdrs=None, fp=None,
            )
        # POST minimal-generate success
        resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
        return _MethodCapturingResponse(json.dumps(resp).encode(), 200)

    captured, fake = _make_get_only_server(handler)
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o",
        )
        result = _probe_openai_models(prov, timeout_sec=5)
    finally:
        urllib.request.urlopen = orig
    _check("probe-openai-models-fallback-ok",
           result.ok,
           f"result={result}")
    _check("probe-openai-models-fallback-tried-post",
           captured["post_calls"] >= 1,
           f"post_calls={captured['post_calls']}")


# ----------------------------------------------------------------------------
# MEDIUM: OpenAI reasoning models: omit role:system + response_format.
# ----------------------------------------------------------------------------
def test_openai_reasoning_omits_system_role():
    """o1/o3/o4-mini models reject role:system — omit it for reasoning models."""
    captured = {}

    def fake(req, timeout=None):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["headers"] = {k: v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data)
        resp = {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        }
        return _MethodCapturingResponse(json.dumps(resp).encode(), 200)

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="o1-mini",
        )
        _call_openai(
            provider=prov, system_prompt="system-prompt-X",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
        )
    finally:
        urllib.request.urlopen = orig

    msgs = captured["body"]["messages"]
    roles = [m["role"] for m in msgs]
    _check("openai-reasoning-no-system-role",
           "system" not in roles,
           f"roles={roles}")
    _check("openai-reasoning-no-response-format",
           "response_format" not in captured["body"],
           f"body keys={list(captured['body'].keys())}")
    _check("openai-reasoning-uses-max-completion-tokens",
           "max_completion_tokens" in captured["body"],
           f"body keys={list(captured['body'].keys())}")


def test_openai_non_reasoning_keeps_system_role():
    """Non-reasoning models keep role:system + response_format."""
    captured = {}

    def fake(req, timeout=None):
        captured["body"] = json.loads(req.data)
        resp = {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        }
        return _MethodCapturingResponse(json.dumps(resp).encode(), 200)

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o",
        )
        _call_openai(
            provider=prov, system_prompt="system-prompt-X",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
        )
    finally:
        urllib.request.urlopen = orig

    msgs = captured["body"]["messages"]
    roles = [m["role"] for m in msgs]
    _check("openai-non-reasoning-keeps-system-role",
           "system" in roles,
           f"roles={roles}")
    _check("openai-non-reasoning-keeps-response-format",
           "response_format" in captured["body"],
           f"body keys={list(captured['body'].keys())}")


# ----------------------------------------------------------------------------
# LOW: Gemini call forces response_mime_type=application/json.
# ----------------------------------------------------------------------------
def test_gemini_force_json_mime_type():
    """Gemini call must set generation_config.response_mime_type=application/json."""
    captured = {}

    def fake(req, timeout=None):
        captured["body"] = json.loads(req.data)
        resp = {
            "candidates": [
                {"content": {"parts": [{"text": "{}"}]},
                 "finishReason": "STOP"},
            ],
        }
        return _MethodCapturingResponse(json.dumps(resp).encode(), 200)

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.GEMINI,
            endpoint="https://generativelanguage.googleapis.com/v1beta",
            api_key="k", model="gemini-2.5-pro",
        )
        _call_gemini(
            provider=prov, system_prompt="s",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
        )
    finally:
        urllib.request.urlopen = orig

    gc = captured["body"].get("generation_config", {})
    _check("gemini-response-mime-type-set",
           gc.get("response_mime_type") == "application/json",
           f"gen config={gc}")


# ----------------------------------------------------------------------------
# LOW: Gemini model name URL-encoded in the URL path.
# ----------------------------------------------------------------------------
def test_gemini_model_name_url_encoded():
    """Model names with slashes (e.g. tuned models) must be percent-encoded."""
    captured = {}

    def fake(req, timeout=None):
        captured["url"] = req.full_url
        resp = {
            "candidates": [
                {"content": {"parts": [{"text": "{}"}]},
                 "finishReason": "STOP"},
            ],
        }
        return _MethodCapturingResponse(json.dumps(resp).encode(), 200)

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        # Model with a slash that needs encoding.
        prov = LlmProvider(
            api_format=ApiFormat.GEMINI,
            endpoint="https://generativelanguage.googleapis.com/v1beta",
            api_key="k", model="tunedModels/my-tuned-model",
        )
        _call_gemini(
            provider=prov, system_prompt="s",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
        )
    finally:
        urllib.request.urlopen = orig

    # The raw slash in the model name must be percent-encoded in the URL.
    _check("gemini-model-name-encoded",
           "tunedModels%2Fmy-tuned-model" in captured["url"],
           f"url={captured['url']!r}")
    # Decoded path segment must round-trip to the original (strip the
    # trailing ``:generateContent`` action before unquoting).
    parsed = urllib.parse.urlparse(captured["url"])
    seg = parsed.path.rsplit(":", 1)[0].rsplit("/", 1)[-1]
    _check("gemini-model-name-decodes-back",
           urllib.parse.unquote(seg) == "tunedModels/my-tuned-model",
           f"path segment={seg!r}")


# ----------------------------------------------------------------------------
# LOW: Gemini response parser ignores thought parts.
# ----------------------------------------------------------------------------
def test_gemini_response_skips_thought_parts():
    """Gemini 2.5+ emits 'thought' parts (chain-of-thought) — skip them."""
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "internal reasoning..."},
                        {"text": '{"answer":42}'},
                    ],
                },
                "finishReason": "STOP",
            },
        ],
    }

    class _R:
        def __init__(self, b):
            self._b = b
            self.status = 200
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake(req, timeout=None):
        return _R(json.dumps(payload).encode())

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        prov = LlmProvider(
            api_format=ApiFormat.GEMINI,
            endpoint="https://generativelanguage.googleapis.com/v1beta",
            api_key="k", model="gemini-2.5-pro",
        )
        raw, truncated, status, err_body, usage = _call_gemini(
            provider=prov, system_prompt="s",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
        )
    finally:
        urllib.request.urlopen = orig

    _check("gemini-skips-thought-parts",
           raw == '{"answer":42}',
           f"raw={raw!r}")


# ----------------------------------------------------------------------------
# LOW: Anthropic reader concatenates all text blocks.
# ----------------------------------------------------------------------------
def test_anthropic_reader_concatenates_text_blocks():
    """Anthropic can return multiple text blocks; concatenate them all."""
    payload = {
        "content": [
            {"type": "text", "text": '{"a"'},
            {"type": "text", "text": ":1}"},
        ],
        "stop_reason": "end_turn",
    }

    class _R:
        def __init__(self, b):
            self._b = b
            self.status = 200
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake(req, timeout=None):
        return _R(json.dumps(payload).encode())

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake
    try:
        raw, truncated, status, err_body, payload_parsed = _read_response(
            "https://example.com/v1/messages",
            {"model": "m", "max_tokens": 100, "messages": []},
            {"x-api-key": "k", "anthropic-version": "2023-06-01",
             "content-type": "application/json"},
            5,
        )
    finally:
        urllib.request.urlopen = orig
    _check("anthropic-concatenates-text-blocks",
           raw == '{"a":1}',
           f"raw={raw!r}")


# ----------------------------------------------------------------------------
# LOW: Fallback usage estimation includes image tokens.
# ----------------------------------------------------------------------------
def test_fallback_usage_estimates_image_tokens():
    """call_llm_api's fallback usage estimation should add image token cost."""
    import rca_core.llm as _L
    # Reach into the fallback path: patch _call_openai to return text but no usage.
    def fake_call(**kw):
        return ('{"a":1}', False, 200, "", None)
    orig = _L._call_openai
    _L._call_openai = fake_call
    try:
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o",
        )
        raw, truncated, status, err_body, usage = _L.call_llm_api(
            provider=prov, system_prompt="s",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100,
        )
    finally:
        _L._call_openai = orig

    _check("fallback-usage-estimated",
           usage is not None and usage.get("estimated") is True,
           f"usage={usage}")
    plain = _L.estimate_tokens("hi s")
    _check("fallback-usage-image-tokens-added",
           usage["input_tokens"] > plain,
           f"image-aware={usage['input_tokens']} vs plain={plain}")


# ----------------------------------------------------------------------------
# LOW: ProviderStore.save() is process-lock + per-instance safe.
# ----------------------------------------------------------------------------
def test_providerstore_save_concurrent_safe():
    """Hammering save() from N threads must not corrupt or race the JSON file."""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "providers.json")
        store = ProviderStore(path=path)
        store.load()
        errors = []

        def worker(i):
            try:
                p = LlmProvider(
                    name=f"P{i}", api_format=ApiFormat.OPENAI,
                    endpoint="https://example.com/v1",
                    api_key="k", model="m",
                )
                store.add(p)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        _check("providerstore-concurrent-no-errors",
               not errors,
               f"errors={errors[:3]}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _check("providerstore-concurrent-disk-valid",
               isinstance(data, dict) and isinstance(data.get("providers"), list),
               f"data keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}")


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    test_api_base_strips_v1()
    test_api_base_preserves_v1beta_for_gemini()
    test_api_base_preserves_anthropic_compat_path()
    test_api_base_v2_preserved()
    test_probe_openai_models_uses_get()
    test_probe_gemini_models_uses_get()
    test_probe_openai_models_falls_back_on_404()
    test_openai_reasoning_omits_system_role()
    test_openai_non_reasoning_keeps_system_role()
    test_gemini_force_json_mime_type()
    test_gemini_model_name_url_encoded()
    test_gemini_response_skips_thought_parts()
    test_anthropic_reader_concatenates_text_blocks()
    test_fallback_usage_estimates_image_tokens()
    test_providerstore_save_concurrent_safe()
    print("--- %d passed, %d failed ---" % (_pass, _fail))
    sys.exit(1 if _fail else 0)


def _run_all():
    """Helper so the suite is importable by unittest / pytest."""
    test_api_base_strips_v1()
    test_api_base_preserves_v1beta_for_gemini()
    test_api_base_preserves_anthropic_compat_path()
    test_api_base_v2_preserved()
    test_probe_openai_models_uses_get()
    test_probe_gemini_models_uses_get()
    test_probe_openai_models_falls_back_on_404()
    test_openai_reasoning_omits_system_role()
    test_openai_non_reasoning_keeps_system_role()
    test_gemini_force_json_mime_type()
    test_gemini_model_name_url_encoded()
    test_gemini_response_skips_thought_parts()
    test_anthropic_reader_concatenates_text_blocks()
    test_fallback_usage_estimates_image_tokens()
    test_providerstore_save_concurrent_safe()
    test_retry_on_429_then_success()
    test_no_retry_on_401_or_403()
    test_provider_from_dict_round_trip()
    test_provider_from_dict_unknown_api_format()


# ----------------------------------------------------------------------------
# Tests for call_llm_api_with_retry
# ----------------------------------------------------------------------------
def test_retry_on_429_then_success():
    """Verify retry logic on 429 (rate limited)."""
    from rca_core.llm import call_llm_api_with_retry
    from unittest.mock import patch

    call_count = [0]
    def fake_call(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return (None, False, 429, "Rate limited", None)
        return ('{"a":1}', False, 200, "", {"input_tokens": 10, "output_tokens": 5})

    with patch('rca_core.llm.call_llm_api', side_effect=fake_call):
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o",
        )
        result = call_llm_api_with_retry(
            provider=prov, system_prompt="s",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
            retries=3,
        )

    _check("retry-on-429-then-success-result",
           result[0] == '{"a":1}',
           f"got {result[0]!r}")
    _check("retry-on-429-then-success-count",
           call_count[0] == 2,
           f"called {call_count[0]} times")


def test_no_retry_on_401_or_403():
    """Non-retryable errors (401/403) should not retry."""
    from rca_core.llm import call_llm_api_with_retry
    from unittest.mock import patch

    call_count = [0]
    def fake_call(**kwargs):
        call_count[0] += 1
        return (None, False, 401, "Unauthorized", None)

    with patch('rca_core.llm.call_llm_api', side_effect=fake_call):
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model="gpt-4o",
        )
        result = call_llm_api_with_retry(
            provider=prov, system_prompt="s",
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
            retries=3,
        )

    _check("no-retry-on-401-count",
           call_count[0] == 1,
           f"called {call_count[0]} times (should be 1)")


# ----------------------------------------------------------------------------
# Provider from_dict tests
# ----------------------------------------------------------------------------
def test_provider_from_dict_round_trip():
    """LlmProvider to_dict and from_dict round-trip correctly."""
    prov = LlmProvider(
        name="Test Provider",
        api_format=ApiFormat.OPENAI,
        endpoint="https://api.example.com/v1",
        api_key="sk-test-key",
        model="gpt-4o",
        extra_headers={"X-Custom": "value"},
        extra_body={"custom_field": True},
    )
    d = prov.to_dict()
    prov2 = LlmProvider.from_dict(d)
    _check("provider-round-trip-name",
           prov2.name == prov.name,
           f"got {prov2.name!r}")
    _check("provider-round-trip-endpoint",
           prov2.endpoint == prov.endpoint,
           f"got {prov2.endpoint!r}")
    _check("provider-round-trip-api-key",
           prov2.api_key == prov.api_key,
           f"got {prov2.api_key!r}")
    _check("provider-round-trip-extra-headers",
           prov2.extra_headers == prov.extra_headers,
           f"got {prov2.extra_headers!r}")


def test_provider_from_dict_unknown_api_format():
    """Unknown api_format string falls back to ANTHROPIC."""
    d = {
        "name": "Test",
        "api_format": "unknown-format",
        "endpoint": "https://example.com",
        "api_key": "k",
        "model": "m",
    }
    prov = LlmProvider.from_dict(d)
    _check("provider-unknown-format-defaults-to-anthropic",
           prov.api_format == ApiFormat.ANTHROPIC,
           f"got {prov.api_format!r}")
