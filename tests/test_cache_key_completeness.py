"""Regression tests for P1-4: rca_core.cache.ResultCache.make_key must
include every request-shape parameter so changing one produces a
different cache key. The previous bug was that the server.py caller
omitted extra_body, so two requests with the same image+provider but
different Anthropic prompt-caching settings returned the same cached
result.

REVIEW-2026-07-25 P1-4.

P1-7: PROMPT_VERSION is now a dict keyed by mode. Each mode has its own
version string so upgrading one mode's prompt doesn't invalidate the cache
for other modes.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.cache import ResultCache


class TestCacheKeyIncludesExtraBody:
    def test_different_extra_body_yields_different_keys(self):
        a = ResultCache.make_key(
            endpoint="https://api.example.com",
            model="m1", api_format="anthropic",
            extra_headers=[], extra_body=[("temperature", "0.7")],
            prompt_version="v1", max_tokens=4096,
            chart_lang="zh", mode="range_chart",
            image_b64="iVBORw0KGgo=", caption="", media_type="image/png",
            run_idx=0,
        )
        b = ResultCache.make_key(
            endpoint="https://api.example.com",
            model="m1", api_format="anthropic",
            extra_headers=[], extra_body=[("temperature", "1.0")],
            prompt_version="v1", max_tokens=4096,
            chart_lang="zh", mode="range_chart",
            image_b64="iVBORw0KGgo=", caption="", media_type="image/png",
            run_idx=0,
        )
        assert a != b, "different extra_body must yield different cache keys"

    def test_different_chart_lang_yields_different_keys(self):
        a = ResultCache.make_key(
            endpoint="e", model="m", api_format="openai",
            extra_headers=[], extra_body=[],
            prompt_version="v1", max_tokens=4096,
            chart_lang="zh", mode="range_chart",
            image_b64="abc", caption="", media_type="image/png",
            run_idx=0,
        )
        b = ResultCache.make_key(
            endpoint="e", model="m", api_format="openai",
            extra_headers=[], extra_body=[],
            prompt_version="v1", max_tokens=4096,
            chart_lang="ja", mode="range_chart",
            image_b64="abc", caption="", media_type="image/png",
            run_idx=0,
        )
        assert a != b, "different chart_lang must yield different keys"

    def test_same_inputs_yield_same_key(self):
        kw = dict(
            endpoint="e", model="m", api_format="openai",
            extra_headers=[], extra_body=[],
            prompt_version="v1", max_tokens=4096,
            chart_lang="zh", mode="range_chart",
            image_b64="abc", caption="x", media_type="image/png",
            run_idx=0,
        )
        a = ResultCache.make_key(**kw)
        b = ResultCache.make_key(**kw)
        assert a == b, "identical inputs must yield identical keys"


class TestPromptVersionPerMode:
    """P1-7: prompt_version is mode-specific; same version string for
    different modes must still use mode in the cache key."""

    def test_prompt_version_for_mode_returns_correct_values(self):
        """Each mode returns its own version string."""
        from rca_core.prompt import prompt_version_for_mode, PROMPT_VERSION
        assert prompt_version_for_mode("range_chart") == PROMPT_VERSION["range_chart"]
        assert prompt_version_for_mode("columnar_section") == PROMPT_VERSION["columnar_section"]
        assert prompt_version_for_mode("abundance") == PROMPT_VERSION["abundance"]
        assert prompt_version_for_mode("phylogenetic_tree") == PROMPT_VERSION["phylogenetic_tree"]

    def test_unknown_mode_defaults_to_v3(self):
        """Unknown mode falls back to 'v3'."""
        from rca_core.prompt import prompt_version_for_mode
        assert prompt_version_for_mode("unknown_mode") == "v3"

    def test_different_modes_yield_different_cache_keys(self):
        """Same image+params but different mode must produce different cache keys."""
        base = dict(
            endpoint="e", model="m", api_format="openai",
            extra_headers=[], extra_body=[],
            max_tokens=4096,
            chart_lang="zh",
            image_b64="abc", caption="", media_type="image/png",
            run_idx=0,
        )
        # Both use "v3" as prompt_version but different mode
        a = ResultCache.make_key(
            **base,
            prompt_version="v3",
            mode="range_chart",
        )
        b = ResultCache.make_key(
            **base,
            prompt_version="v3",
            mode="columnar_section",
        )
        assert a != b, "different mode must yield different cache keys even with same prompt_version"

    def test_upgrading_one_mode_does_not_invalidate_other_modes(self):
        """Upgrading range_chart version doesn't change cache keys for other modes."""
        from rca_core.prompt import PROMPT_VERSION
        # Simulate a future upgrade without assuming today's concrete version.
        old_version = PROMPT_VERSION["range_chart"]
        new_version = f"{old_version}-next"

        base = dict(
            endpoint="e", model="m", api_format="openai",
            extra_headers=[], extra_body=[],
            max_tokens=4096,
            chart_lang="zh",
            image_b64="abc", caption="", media_type="image/png",
            run_idx=0,
        )

        # Old key for range_chart
        old_rc_key = ResultCache.make_key(
            **base,
            prompt_version=old_version,
            mode="range_chart",
        )
        # Old key for columnar_section (unchanged version)
        old_cs_key = ResultCache.make_key(
            **base,
            prompt_version=PROMPT_VERSION["columnar_section"],
            mode="columnar_section",
        )

        # New key for range_chart (version bumped)
        new_rc_key = ResultCache.make_key(
            **base,
            prompt_version=new_version,
            mode="range_chart",
        )
        # New key for columnar_section (same version as before)
        new_cs_key = ResultCache.make_key(
            **base,
            prompt_version=PROMPT_VERSION["columnar_section"],
            mode="columnar_section",
        )

        # range_chart key changes when version bumps
        assert old_rc_key != new_rc_key, "bumped range_chart version must change its cache key"
        # columnar_section key stays same when range_chart version bumps
        assert old_cs_key == new_cs_key, (
            "upgrading range_chart version must NOT change columnar_section cache key"
        )