"""The reasoning level: per-model options, the wire shape, and the budgets.

Three things came out of measuring MiniMax's M3.1 family, and they are
pinned here because each one is a place where a plausible-looking change
silently breaks the product:

1. The LEVELS belong to the model. M3.1 takes five named levels; M3 does not
   take an effort at all (its thinking is an on/off switch, and minimax-code
   puts it plainly: "Responses effort values do not change thinking depth").

2. The WIRE SHAPE is MiniMax's, not Anthropic's. On the Anthropic-compatible
   endpoint it is `thinking: {type: adaptive}` PLUS
   `output_config: {effort: ...}`. `thinking.type=enabled` without an integer
   `budget_tokens` is REJECTED by OpenRouter's Messages API, and its Claude
   4.6 migration note says adaptive and the budget mode are different
   mechanisms. The OpenAI-compatible endpoint takes `reasoning_effort` at the
   top level.

3. The BUDGETS scale with the level, because the level decides how long the
   model thinks. Measured on the reference figure's label call: 28.3 s on
   "low", 48.3 s on "high", 69-102 s on "max". A single 90 s ceiling was
   right for "low" and too small for the others -- at "max" the path gave up
   before it could answer and the end-to-end regression failed 3 of 3.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.extractor as E                                # noqa: E402
import rca_core.llm as L                                     # noqa: E402
import rca_core.matrix as M                                  # noqa: E402


# --- the catalogue ----------------------------------------------------------

def test_m31_offers_the_five_named_levels_plus_default():
    assert L.reasoning_options_for("MiniMax-M3.1-Flash") == [
        "default", "low", "medium", "high", "xhigh", "max"]


def test_m3_offers_a_switch_not_a_depth():
    """M3's thinking is on/off; an effort value does not change its depth."""
    assert L.reasoning_options_for("MiniMax-M3") == ["default", "off", "on"]


def test_a_model_with_no_reasoning_control_offers_nothing():
    assert L.reasoning_options_for("gpt-4o") == []
    assert L.reasoning_options_for("") == []
    assert L.reasoning_options_for(None) == []


def test_the_lookup_tolerates_case_and_the_preview_suffix():
    assert L.reasoning_options_for("minimax-m3.1-flash") == \
        L.reasoning_options_for("MiniMax-M3.1-Flash")
    assert L.reasoning_options_for("MiniMax-M3.1-Flash-Preview") == \
        L.reasoning_options_for("MiniMax-M3.1-Flash")


# --- the resolution rule ----------------------------------------------------

def test_a_valid_choice_wins():
    assert L.resolve_reasoning_choice("MiniMax-M3.1-Flash", "low") == "low"
    assert L.resolve_reasoning_choice("MiniMax-M3.1-Flash", "xhigh") == "xhigh"


def test_the_catalog_default_wins_over_the_midpoint():
    """minimax-code's rule: a valid choice, else the default, else the midpoint."""
    assert L.resolve_reasoning_choice("MiniMax-M3.1-Flash", "") == "default"
    assert L.resolve_reasoning_choice("MiniMax-M3.1-Flash", None) == "default"
    assert L.resolve_reasoning_choice("MiniMax-M3.1-Flash", "turbo") == "default"


def test_a_model_with_no_profile_resolves_to_nothing():
    assert L.resolve_reasoning_choice("gpt-4o", "low") == ""


# --- the wire shape ---------------------------------------------------------

def _patch(model, effort, fmt=L.ApiFormat.ANTHROPIC):
    p = L.LlmProvider(model=model, reasoning_effort=effort)
    return L._reasoning_request_patch(p, fmt)


def test_the_anthropic_shape_is_adaptive_plus_output_config():
    assert _patch("MiniMax-M3.1-Flash", "low") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"}}


def test_the_adaptive_marker_is_not_optional():
    """`thinking.type=enabled` without a budget is a 400; `adaptive` is not."""
    patch = _patch("MiniMax-M3.1-Flash", "max")
    assert patch.get("thinking") == {"type": "adaptive"}
    assert "budget_tokens" not in str(patch)


def test_the_openai_shape_is_a_top_level_reasoning_effort():
    assert _patch("MiniMax-M3.1-Flash", "high",
                  L.ApiFormat.OPENAI) == {"reasoning_effort": "high"}


def test_m3_sends_a_thinking_switch():
    assert _patch("MiniMax-M3", "off") == {"thinking": {"type": "disabled"}}
    assert _patch("MiniMax-M3", "on") == {"thinking": {"type": "adaptive"}}


def test_default_sends_nothing():
    """`default` means "let the provider decide"."""
    assert _patch("MiniMax-M3.1-Flash", "default") == {}


def test_a_model_with_no_profile_sends_nothing():
    assert _patch("gpt-4o", "max") == {}


# --- the MAP, not just a list of options (cc-switch's shape) ---------------

def test_a_level_the_provider_does_not_support_is_not_offered():
    """A level mapped to None is not in the picker.

    cc-switch's rule is explicit about why: "思考等级对部分供应商无效 ...
    硬传可能导致请求被拒" -- offering a level the provider would reject is
    worse than not offering it.
    """
    profile = L.reasoning_profile_for("MiniMax-M3.1-Flash")
    assert profile.wire_value("off") is None
    assert "off" not in profile.levels()
    # ... and sending it anyway is refused rather than guessed at.
    p = L.LlmProvider(model="MiniMax-M3.1-Flash", reasoning_effort="off")
    assert L._reasoning_request_patch(p, L.ApiFormat.ANTHROPIC) == {}


def test_the_wire_value_may_differ_from_the_level_name():
    """cc-switch maps Gemini's ``low`` to ``"LOW"`` and ``high`` to ``"HIGH"``.

    A per-model list of options has no way to say that, which is why the
    catalogue is a MAP and not a list.
    """
    profile = L.REASONING_PROFILES["MiniMax-M3.1-Flash"]
    assert profile.wire_value("low") == "low"
    assert profile.wire_value("max") == "max"
    # The "default" row is offered but sends nothing.
    assert profile.wire_value(L.REASONING_DEFAULT) == L.REASONING_OMIT
    assert L.REASONING_DEFAULT in profile.levels()


def test_a_missing_level_is_not_the_same_as_an_unsupported_one():
    """cc-switch's distinction, kept.

    cc-switch spells "this provider does not take that level" by putting the
    key in the map with a ``null`` VALUE (so the UI can still show the level
    as unavailable), while a level that is simply ABSENT was never part of
    the profile. Both are refused on the wire here, and the difference is
    in what the catalogue MEANS -- so a catalogue that wants to be explicit
    can use either spelling.
    """
    profile = L.reasoning_profile_for("MiniMax-M3.1-Flash")
    # M3.1 omits the levels it does not take.
    assert "off" not in profile.map
    assert profile.wire_value("off") is None
    assert "off" not in profile.levels()
    # ... and a level that IS taken is mapped to its wire value.
    assert profile.wire_value("high") == "high"

    # The other spelling: the key is present and mapped to None.
    explicit = L.ReasoningProfile(
        map={"low": "low", "high": None}, default="low",
        style=L.REASONING_STYLE_EFFORT)
    assert explicit.levels() == ["low"]
    assert explicit.wire_value("high") is None


def test_the_wire_value_is_what_goes_out():
    p = L.LlmProvider(model="MiniMax-M3.1-Flash", reasoning_effort="xhigh")
    assert L._reasoning_request_patch(p, L.ApiFormat.ANTHROPIC) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"}}
    p2 = L.LlmProvider(model="MiniMax-M3.1-Flash", reasoning_effort="default")
    assert L._reasoning_request_patch(p2, L.ApiFormat.ANTHROPIC) == {}


def test_extra_body_still_wins_over_the_built_in_patch():
    """The documented escape hatch keeps the last word."""
    p = L.LlmProvider(model="MiniMax-M3.1-Flash", reasoning_effort="low",
                      extra_body={"output_config": {"effort": "high"}})
    body = {"model": p.model}
    body.update(L._reasoning_request_patch(p, L.ApiFormat.ANTHROPIC))
    body.update(p.extra_body or {})
    assert body["output_config"] == {"effort": "high"}


# --- the budgets ------------------------------------------------------------

def test_the_budget_scales_with_the_level():
    """A deeper level needs a longer call and a longer path.

    Read at a timeout ABOVE the levels' own needs, because the caller's
    timeout is a hard cap on one call: at the default 120 s the "high" and
    "max" levels are both capped to 120 s (which is enough -- the measured
    "max" label call took 69-102 s), and that cap is asserted separately.
    """
    low = M.matrix_budgets_for("MiniMax-M3.1-Flash", "low", 300)
    high = M.matrix_budgets_for("MiniMax-M3.1-Flash", "high", 300)
    mx = M.matrix_budgets_for("MiniMax-M3.1-Flash", "max", 300)
    assert low[0] < high[0] < mx[0], "a deeper level needs a longer call"
    assert low[1] < high[1] < mx[1], "and a longer path"


def test_the_low_budget_is_exactly_the_measured_good_one():
    """The "low" row is the measured 3-of-3 configuration."""
    assert M.matrix_budgets_for("MiniMax-M3.1-Flash", "low", 120) == (90, 360)


def test_the_caller_timeout_bounds_the_run_not_one_call_of_it():
    """A path that makes FOUR calls is one request with a budget.

    The caller's timeout bounds the whole run -- through the server's
    single-run budget -- and the per-call ceiling is the level's own need.
    Capping a single call by it instead left the deeper levels no room at
    all: measured, the "max" label call took 69.1 s, 94 s, 102.4 s and 121 s
    on four tries, so a 120 s per-call ceiling failed it about half the time.
    """
    per_call, path = M.matrix_budgets_for("MiniMax-M3.1-Flash", "max", 120)
    assert per_call >= 121, \
        "the max level must have room for its slowest measured call"
    # ... and the RUN still fits the server's budget at that timeout.
    assert path + 2 * 120 <= E.extraction_wall_clock_budget_sec(120)


def test_the_default_timeout_gives_the_max_level_room_to_answer():
    """The measured "max" label call took 69.1 s and 102.4 s."""
    per_call, _path = M.matrix_budgets_for("MiniMax-M3.1-Flash", "max", 120)
    assert per_call >= 120, "the max level needs more than the old 90 s ceiling"


def test_the_path_budget_fits_the_server_budget_with_the_fallback_left_over():
    """The single-pass fallback has to fit AFTER the two-stage path."""
    for timeout_sec in (30, 60, 120, 300):
        _per_call, path = M.matrix_budgets_for(
            "MiniMax-M3.1-Flash", "max", timeout_sec)
        fallback = 2 * timeout_sec
        assert path + fallback <= E.extraction_wall_clock_budget_sec(timeout_sec), (
            "the two-stage path and its fallback must fit the server's budget "
            "at timeout_sec=%s" % timeout_sec)


def test_the_server_budget_counts_eight_attempts():
    """Four calls of the two-stage path, each of which may retry."""
    assert E.SINGLE_RUN_MAX_ATTEMPTS == 8
    assert E.extraction_wall_clock_budget_sec(120) == 120 * 8 + 25


def test_the_max_tokens_ceiling_is_the_models_real_limit():
    """MiniMax answers a larger value with "does not support max tokens >
    524288"; 1000000 is a 400."""
    assert E.MAX_MAX_TOKENS == 524288
    assert E.clamp_max_tokens(1_000_000) == 524288
    assert E.clamp_max_tokens(524_288) == 524_288
