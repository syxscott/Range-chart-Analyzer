"""A provider must be able to say how hard the model should think.

MiniMax's M3.1 family ALWAYS thinks and its effort level DEFAULTS TO ``max``.
Thinking tokens consume ``max_tokens``, and on a dense figure the thinking
alone outgrew the budget: measured on the reference figure's label call,

    MiniMax-M3           (thinking off by default)   6.5-13.1 s, answered
    MiniMax-M3.1-Flash   (effort=max, the default)   read TIMEOUT at 90 s, 2/2
    MiniMax-M3.1-Flash   (effort=low)                28.3 s, answered 22/18/3
    MiniMax-M3.1-Flash   (effort=high)               48.3 s, answered 22/18/3

Raising max_tokens does not fix that (the thinking is unbounded); lowering the
effort does. The knob is therefore a first-class provider field, not a
hand-written ``extra_body`` blob: the GUI's provider wizard REBUILDS the
provider from a fixed field list, so anything smuggled into extra_body is
silently dropped the moment the user edits that provider in the settings
dialog -- and the product would revert to the timing-out configuration with
nothing on screen to explain why.

The wire shape is ``output_config: {"effort": ...}`` on the Anthropic-
compatible endpoint. ``thinking: {budget_tokens: N}`` is NOT in M3.1's
documented vocabulary, and a provider that cannot think at all (M3, and every
other vendor here) must be left alone.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.llm as L                                  # noqa: E402


def _captured_body(provider, **overrides):
    """Run one Anthropic call and return the JSON body that went on the wire."""
    seen = {}

    def fake_read(target, body, headers, timeout_sec, progress_callback=None):
        seen.update(body)
        # _read_response returns (raw_text, truncated, status, err_str, payload)
        return ("OK", False, 200, "", {"usage": {"input_tokens": 1,
                                                 "output_tokens": 1}})

    kw = dict(system_prompt="s", image_b64="QUFB", media_type="image/png",
              user_text="hi", max_tokens=64)
    kw.update(overrides)
    with patch.object(L, "_read_response", side_effect=fake_read):
        L.call_llm_api(provider=provider, **kw)
    return seen


def _anthropic(**kw):
    base = dict(api_format=L.ApiFormat.ANTHROPIC,
                endpoint="https://api.minimaxi.com/anthropic",
                api_key="sk-test", model="MiniMax-M3.1-Flash")
    base.update(kw)
    return L.LlmProvider(**base)


# --- the field itself -------------------------------------------------------

def test_the_effort_travels_with_the_provider_record():
    p = L.LlmProvider(reasoning_effort="low")
    d = p.to_dict()
    assert d.get("reasoning_effort") == "low", \
        "the field must be persisted or the setting is lost on restart"
    assert L.LlmProvider.from_dict(d).reasoning_effort == "low"


def test_an_old_record_without_the_field_still_loads():
    """providers.json written before this field existed must not break."""
    d = L.LlmProvider().to_dict()
    d.pop("reasoning_effort", None)
    assert L.LlmProvider.from_dict(d).reasoning_effort == ""


def test_the_effort_survives_the_temperature_copy():
    """call_llm_api REBUILDS the provider when temperature/seed are given.

    The rebuild used a fixed field list, so the effort was dropped and M3.1
    silently reverted to effort=max -- the configuration that times out.
    """
    seen = {}

    def fake_read(target, body, headers, timeout_sec, progress_callback=None):
        seen.update(body)
        return ("OK", False, 200, "", {"usage": {"input_tokens": 1,
                                                 "output_tokens": 1}})

    with patch.object(L, "_read_response", side_effect=fake_read):
        L.call_llm_api(provider=_anthropic(reasoning_effort="low"),
                       system_prompt="s", image_b64="QUFB",
                       media_type="image/png", user_text="hi", max_tokens=64,
                       temperature=0.2)
    assert seen.get("output_config") == {"effort": "low"}, \
        "the rebuilt provider dropped the effort, so M3.1 fell back to max"


# --- the wire shape ---------------------------------------------------------

def test_the_effort_is_sent_as_output_config():
    body = _captured_body(_anthropic(reasoning_effort="low"))
    assert body.get("output_config") == {"effort": "low"}, \
        "M3.1's documented shape is output_config.effort"


def test_no_effort_means_no_output_config():
    body = _captured_body(_anthropic())
    assert "output_config" not in body, \
        "a provider that did not ask for an effort must not get one"


def test_budget_tokens_is_never_sent():
    """`thinking: {budget_tokens: N}` is not in M3.1's vocabulary.

    The Anthropic-compatible shape IS `thinking: {type: adaptive}` plus
    `output_config: {effort: ...}` -- OpenRouter's Messages API rejects
    `thinking.type=enabled` without an integer budget, and its Claude 4.6
    migration note says adaptive and the budget mode are different
    mechanisms. So `thinking` may be present; what must never be is a
    `budget_tokens`.
    """
    body = _captured_body(_anthropic(reasoning_effort="low"))
    thinking = body.get("thinking") or {}
    assert "budget_tokens" not in thinking, \
        "a fixed token budget is not M3.1's vocabulary"
    assert thinking.get("type") == "adaptive"
    assert body.get("output_config") == {"effort": "low"}


def test_extra_body_still_wins():
    """extra_body is the documented escape hatch and keeps the last word."""
    p = _anthropic(reasoning_effort="low",
                   extra_body={"output_config": {"effort": "high"}})
    body = _captured_body(p)
    assert body.get("output_config") == {"effort": "high"}


def test_a_non_anthropic_provider_is_left_alone():
    """The OpenAI endpoint of the same vendor takes a different spelling.

    That shape was NOT measured, so sending it would risk a 400 on a request
    that used to work. The effort is ignored, not guessed at.
    """
    p = L.LlmProvider(api_format=L.ApiFormat.OPENAI,
                      endpoint="https://api.minimaxi.com/anthropic",
                      api_key="sk-test", model="m", reasoning_effort="low")
    seen = {}

    def fake_read(target, body, headers, timeout_sec, progress_callback=None):
        seen.update(body)
        return ("OK", False, 200, "", {"usage": {"input_tokens": 1,
                                                 "output_tokens": 1}})

    with patch.object(L, "_read_response", side_effect=fake_read):
        L.call_llm_api(provider=p, system_prompt="s", image_b64="QUFB",
                       media_type="image/png", user_text="hi", max_tokens=64)
    assert "output_config" not in seen


# --- the normaliser ---------------------------------------------------------

def test_an_unknown_level_is_dropped_not_forwarded():
    assert L.normalize_reasoning_effort("turbo") == "", \
        "a typo must not become a request that 400s the whole extraction"
    assert L.normalize_reasoning_effort(None) == ""
    assert L.normalize_reasoning_effort(7) == ""


def test_the_documented_levels_are_accepted():
    for lvl in ("low", "medium", "high", "xhigh", "max"):
        assert L.normalize_reasoning_effort(lvl) == lvl
        assert L.normalize_reasoning_effort(lvl.upper()) == lvl
        assert L.normalize_reasoning_effort("  %s  " % lvl) == lvl


def test_an_unknown_level_does_not_reach_the_wire():
    body = _captured_body(_anthropic(reasoning_effort="turbo"))
    assert "output_config" not in body


# --- the cache key ----------------------------------------------------------

def test_two_efforts_do_not_share_a_cache_entry():
    """A cached result is a result for ONE request, not for the image.

    The effort changes the request shape exactly the way ``extra_body`` does,
    and the cache module's own rule says two requests with different
    ``extra_body`` MUST NOT share an entry. The effort was left out of the
    key, so a user who switched it got the previous effort's answer back
    with no way to tell.
    """
    from rca_core.cache import ResultCache, stable_reasoning_effort

    def key_for(effort):
        return ResultCache.make_key(
            endpoint="https://api.minimaxi.com/anthropic",
            model="MiniMax-M3.1-Flash",
            api_format="anthropic",
            extra_headers=[],
            extra_body=[],
            reasoning_effort=stable_reasoning_effort(
                _anthropic(reasoning_effort=effort)),
            prompt_version="v1", mode="occurrence_matrix",
            image_b64="QUFB")

    assert key_for("low") != key_for("high"), \
        "two efforts must not share a cache entry"
    assert key_for("low") != key_for(""), \
        "an unset effort and a set one must not share a cache entry"
    assert key_for("low") == key_for("low")


def test_the_helper_reads_the_field():
    from rca_core.cache import stable_reasoning_effort
    assert stable_reasoning_effort(_anthropic(reasoning_effort="low")) == "low"
    assert stable_reasoning_effort(_anthropic()) == ""
    assert stable_reasoning_effort(None) == ""
