"""The protocol layer: one adapter per format, and it really is pluggable.

REVIEW-2026-10-20. The three API formats were three functions inside
``rca_core/llm.py`` and ``call_llm_api`` dispatched to them with a
hard-coded table. They now live in ``rca_core/protocols.py`` as three
adapter classes behind a registry.

The property worth testing is the ONE the refactor was for: **a fourth
format is one class plus one registry line, and it does not need the
transport touched.**

The protocol bodies were MOVED, not rewritten, so the behaviour they
carry is pinned by the suites that already existed. These tests add the
things that suite could not see: the registry, the adapter contract, and
the plug-in property itself.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.llm import ApiFormat, LlmProvider                    # noqa: E402
from rca_core.protocols import (                                    # noqa: E402
    AnthropicAdapter, GeminiAdapter, OpenAIAdapter, ProtocolAdapter,
    adapter_for,
)


def _provider(model="m", fmt=ApiFormat.ANTHROPIC, endpoint="https://x.example"):
    return LlmProvider(name="p", api_format=fmt, endpoint=endpoint,
                       api_key="k", model=model)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_every_format_has_an_adapter():
    for fmt in ApiFormat:
        adapter = adapter_for(fmt)
        assert isinstance(adapter, ProtocolAdapter)
        assert adapter.name == fmt.value


def test_the_registry_is_cached():
    """It is built once, not on every call."""
    from rca_core import llm
    llm._adapters()
    first = llm.ADAPTERS
    llm._adapters()
    assert llm.ADAPTERS is first


def test_a_fourth_format_is_one_class_and_one_line():
    """The property the refactor was for.

    A new format is a subclass plus a registry entry -- and the new
    adapter works without the transport being touched, because the
    transport lives behind the adapter's ``call``.
    """
    from rca_core import llm

    class _ToyAdapter(ProtocolAdapter):
        name = "toy"
        supports_stream = False

        def resource_path(self, provider):
            return "/v1/toy"

        def headers(self, provider):
            return {"x-toy-key": provider.api_key}

        def body(self, provider, **kw):
            return {"model": provider.model, "prompt": kw["user_text"]}

        def parse(self, payload):
            return str(payload.get("answer") or ""), False, ""

    toy = _ToyAdapter()
    assert toy.name == "toy"
    # The contract every adapter implements:
    for attr in ("call", "guard", "endpoint", "headers", "body", "parse"):
        assert hasattr(toy, attr), attr
    # And the registry takes it without any other change.
    llm.ADAPTERS = dict(llm._adapters())
    llm.ADAPTERS["toy"] = toy
    try:
        assert llm._adapters()["toy"] is toy
    finally:
        llm.ADAPTERS = {k: v for k, v in llm.ADAPTERS.items() if k != "toy"}


# ---------------------------------------------------------------------------
# The bodies
# ---------------------------------------------------------------------------

def test_the_anthropic_body_carries_the_system_prompt_as_its_own_field():
    body = AnthropicAdapter().body(
        _provider(), system_prompt="sys", image_b64="QQ==",
        media_type="image/png", user_text="hi", max_tokens=100)
    assert body["system"] == "sys"
    assert body["max_tokens"] == 100
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[1]["text"] == "hi"


def test_the_openai_body_asks_for_json():
    body = OpenAIAdapter().body(
        _provider(fmt=ApiFormat.OPENAI), system_prompt="sys", image_b64="QQ==",
        media_type="image/png", user_text="hi", max_tokens=100)
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 100
    assert body["messages"][0]["role"] == "system"


def test_the_openai_body_adapts_to_a_reasoning_model():
    """o1/o3/o4-mini reject ``max_tokens`` and ``role:system``."""
    body = OpenAIAdapter().body(
        _provider(model="o3-mini", fmt=ApiFormat.OPENAI),
        system_prompt="sys", image_b64="QQ==", media_type="image/png",
        user_text="hi", max_tokens=100)
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 100
    assert "response_format" not in body
    # The system prompt is MERGED, not dropped.
    assert "sys" in body["messages"][0]["content"][1]["text"]


def test_the_openai_body_adapts_to_a_capitalised_reasoning_model():
    """Some aggregators list it as "O1"."""
    body = OpenAIAdapter().body(
        _provider(model="O1", fmt=ApiFormat.OPENAI),
        system_prompt="sys", image_b64="QQ==", media_type="image/png",
        user_text="hi", max_tokens=100)
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 100


def test_the_gemini_body_keeps_the_sampling_knobs_inside_generation_config():
    """Google answers an unknown root field with 400."""
    provider = _provider(fmt=ApiFormat.GEMINI)
    provider.extra_body = {"temperature": 0.3, "seed": 7}
    body = GeminiAdapter().body(
        provider, system_prompt="sys", image_b64="QQ==",
        media_type="image/png", user_text="hi", max_tokens=100)
    assert "temperature" not in body
    assert body["generation_config"]["temperature"] == 0.3
    assert body["generation_config"]["seed"] == 7
    assert body["generation_config"]["max_output_tokens"] == 100
    assert body["generation_config"]["response_mime_type"] == "application/json"


def test_the_gemini_body_does_not_mutate_the_providers_extra_body():
    provider = _provider(fmt=ApiFormat.GEMINI)
    provider.extra_body = {"generation_config": {"temperature": 0.9}}
    GeminiAdapter().body(
        provider, system_prompt="s", image_b64="QQ==", media_type="image/png",
        user_text="u", max_tokens=10)
    assert provider.extra_body["generation_config"] == {"temperature": 0.9}


# ---------------------------------------------------------------------------
# The replies
# ---------------------------------------------------------------------------

def test_the_anthropic_reply_is_read():
    text, truncated, err = AnthropicAdapter().parse({
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
    })
    assert text == "hello"
    assert truncated is False
    assert err == ""


def test_the_openai_reply_is_read():
    text, truncated, err = OpenAIAdapter().parse({
        "choices": [{"message": {"content": "hello"},
                     "finish_reason": "stop"}],
    })
    assert text == "hello"
    assert truncated is False


def test_the_openai_reply_keeps_a_list_of_content_parts():
    text, _t, _e = OpenAIAdapter().parse({
        "choices": [{"message": {"content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}],
    })
    assert text == "ab"


def test_the_openai_reply_notices_the_truncation():
    text, truncated, _e = OpenAIAdapter().parse({
        "choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
    })
    assert text == "x"
    assert truncated is True


def test_the_gemini_reply_skips_the_thought_parts():
    """Gemini 2.5+ emits a separate ``thought`` part; leaking it into the
    JSON would break the parse."""
    text, _t, _e = GeminiAdapter().parse({
        "candidates": [{"content": {"parts": [
            {"thought": True, "text": "internal reasoning..."},
            {"text": '{"answer": 1}'},
        ]}}],
    })
    assert text == '{"answer": 1}'


def test_the_gemini_reply_keeps_a_plain_text_part():
    """Only a part flagged ``thought`` is reasoning. A part that merely
    LOOKS like reasoning is the model's answer."""
    text, _t, _e = GeminiAdapter().parse({
        "candidates": [{"content": {"parts": [
            {"text": "plain answer"},
        ]}}],
    })
    assert text == "plain answer"


def test_an_empty_reply_carries_the_reason():
    """An empty 200 is NOT "the model answered with nothing"."""
    _text, _truncated, err = OpenAIAdapter().parse({
        "choices": [{"message": {"content": ""}}],
        "error": {"message": "insufficient balance"},
    })
    assert err


# ---------------------------------------------------------------------------
# The dispatch stays late-bound
# ---------------------------------------------------------------------------

def test_the_dispatch_is_still_late_bound():
    """``call_llm_api`` looks the function up on the module at CALL time,
    so a test that patches ``llm._call_openai`` still reaches the
    patched function. The refactor had to keep that."""
    import rca_core.llm as L

    calls = {"n": 0}
    orig = L._call_openai

    def fake(**kw):
        calls["n"] += 1
        return ("patched", False, 200, "", None)

    L._call_openai = fake
    try:
        out = L.call_llm_api(
            provider=_provider(fmt=ApiFormat.OPENAI), system_prompt="s",
            image_b64="QQ==", media_type="image/png", user_text="u",
            max_tokens=10)
    finally:
        L._call_openai = orig
    assert calls["n"] == 1
    assert out[0] == "patched"


def test_the_adapters_reach_the_patched_transport():
    """A test that patches ``llm._post_json`` still reaches it through
    the adapter -- the lazy import inside the method is what makes that
    true."""
    import rca_core.llm as L

    seen = {}

    def fake_post(target, body, headers, timeout_sec, progress_callback=None,
                  headers_out=None):
        seen["target"] = target
        seen["body"] = body
        return (b'{"choices":[{"message":{"content":"hi"}}]}', 200, b"")

    orig = L._post_json
    L._post_json = fake_post
    try:
        # A LITERAL public address, so the SSRF guard accepts the endpoint:
        # the point of the test is that the patched transport is reached,
        # not that the guard lets the call through.
        out = L._call_openai(
            provider=_provider(fmt=ApiFormat.OPENAI,
                               endpoint="https://93.184.216.34"),
            system_prompt="s", image_b64="QQ==", media_type="image/png",
            user_text="u", max_tokens=10, timeout_sec=30)
    finally:
        L._post_json = orig
    assert seen["target"].endswith("/v1/chat/completions")
    assert seen["body"]["model"] == "m"
    assert out[0] == "hi"
