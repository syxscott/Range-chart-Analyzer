"""The cache key factory must produce EXACTLY the keys it replaced.

ARCH-REVIEW A2 / P0-2. Three call sites used to hand-write
`make_key(**fields)` with different field sets; all three now call
rca_core.cache.build_extraction_key. A cache key change is harmless in
isolation (old entries just miss), but it would be a silent, wasteful
invalidation of every user's cache -- several hundred LLM calls' worth.
These tests prove byte-level equivalence for all three shapes.
"""
from __future__ import annotations

import pytest

from rca_core.cache import (CACHE_KEY_FIELDS, CACHE_KEY_REQUIRED,
                            ResultCache, build_extraction_key)

BASE = dict(
    endpoint="https://api.example.com/v1",
    model="model-x",
    api_format="anthropic",
    extra_headers=[["Authorization", "Bearer X"]],
    extra_body=[["routing", "eu"]],
    reasoning_effort="high",
    prompt_version="v5",
    mode="range_chart",
    image_b64="A" * 400,
    caption="Fig. 1",
    chart_lang="auto",
)


def _legacy(extra):
    """What the old call sites did: make_key with exactly the fields given."""
    return ResultCache.make_key(**{**BASE, **extra})


def test_single_extraction_key_is_unchanged():
    extra = {"max_tokens": 4096, "media_type": "image/png"}
    got = build_extraction_key(**BASE, **extra)
    assert got == _legacy(extra)


def test_multi_run_slot_key_is_unchanged():
    extra = {"max_tokens": 4096, "media_type": "image/png", "run_idx": 3}
    got = build_extraction_key(**BASE, **extra)
    assert got == _legacy(extra)


def test_classification_key_is_unchanged():
    classify = {**BASE, "mode": "chart_classify", "prompt_version": "v3"}
    got = build_extraction_key(**classify)
    assert got == ResultCache.make_key(**classify)


def test_optional_none_fields_are_dropped_not_hashed():
    assert build_extraction_key(**BASE) == ResultCache.make_key(**BASE)
    assert build_extraction_key(**BASE, run_idx=None) == ResultCache.make_key(**BASE)


def test_every_field_that_changes_the_request_changes_the_key():
    base = build_extraction_key(**BASE)
    for field, value in (
        ("prompt_version", "v6"),
        ("reasoning_effort", "low"),
        ("extra_body", [["routing", "us"]]),
        ("caption", "Fig. 2"),
        ("chart_lang", "zh"),
        ("mode", "columnar_section"),
        ("run_idx", 1),
    ):
        changed = build_extraction_key(**{**BASE, field: value})
        assert changed != base, f"changing {field} did not change the cache key"


def test_factory_rejects_a_missing_required_field():
    with pytest.raises(TypeError):
        build_extraction_key(**{k: v for k, v in BASE.items()
                                if k != "reasoning_effort"})


def test_factory_rejects_a_none_required_field():
    with pytest.raises(AssertionError, match="required cache key fields"):
        build_extraction_key(**{**BASE, "caption": None})


def test_required_is_a_subset_of_declared():
    assert CACHE_KEY_REQUIRED <= CACHE_KEY_FIELDS
    assert len(CACHE_KEY_FIELDS) == 14
