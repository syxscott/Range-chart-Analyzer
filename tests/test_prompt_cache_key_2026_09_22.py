"""FIX-2026-09-22 audit item 1 [中·实测]: auto-mode prompt cache key stuck
on the fallback version.

The defect: PROMPT_VERSION has (deliberately) no "auto" entry, so
``prompt_version_for_mode("auto")`` silently returns the "v3" fallback. If
a result cache key is ever built from the REQUESTED mode instead of the
RESOLVED one, the v4->v5 contract bump never invalidates cached results on
the default UI path (auto), while the audit line in
``extractor._ok_result`` records the RESOLVED mode's version - key says
v3, audit says v5.

The fix keeps two properties:
  * "auto" stays OUT of PROMPT_VERSION - no fake version that hides which
    prompt actually ran; the key must carry the RESOLVED mode;
  * a STRICT helper (``prompt_version_for_cache_key``) raises instead of
    falling back, and the server's two cache-key sites use it.

All tests are function-level; the server is never booted (the /api/extract
site is pinned by source inspection instead).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from rca_core.cache import ResultCache
from rca_core.prompt import (
    PROMPT_VERSION,
    UNRESOLVED_MODES,
    prompt_version_for_cache_key,
    prompt_version_for_mode,
    resolve_prompt_mode,
)


# ---------------------------------------------------------------------------
# the version table itself
# ---------------------------------------------------------------------------

def test_auto_has_no_fake_version_entry():
    """No fake "auto" stamp may ever be added: the key must carry the mode
    that actually ran, not a placeholder hiding it."""
    assert "auto" not in PROMPT_VERSION
    assert "auto" in UNRESOLVED_MODES


def test_lenient_fallback_is_unchanged_for_display_consumers():
    # Extraction/history stamping keeps the documented lenient behaviour;
    # only cache keys are held to the strict helper.
    assert prompt_version_for_mode("auto") == "v3"
    assert prompt_version_for_mode("range_chart") == "v5"


def test_strict_helper_returns_the_per_mode_version():
    for mode, version in PROMPT_VERSION.items():
        assert prompt_version_for_cache_key(mode) == version
    assert resolve_prompt_mode("range_chart") == "range_chart"


def test_strict_helper_refuses_unresolved_and_unknown_modes():
    with pytest.raises(ValueError, match="unresolved"):
        prompt_version_for_cache_key("auto")
    with pytest.raises(ValueError, match="unresolved"):
        prompt_version_for_cache_key(" AUTO ")
    with pytest.raises(ValueError):
        prompt_version_for_cache_key("nonsense_mode")
    with pytest.raises(ValueError):
        prompt_version_for_cache_key("")


# ---------------------------------------------------------------------------
# the cache key behaviour
# ---------------------------------------------------------------------------

BASE_FIELDS = dict(
    endpoint="https://api.example.invalid/v1/chat",
    model="MiniMax-M3",
    api_format="openai",
    extra_headers=[],
    extra_body=[],
    max_tokens=8000,
    chart_lang="auto",
    image_b64="iVBORw0KGgo=",
    caption="",
    media_type="image/png",
)


def _key(mode: str) -> str:
    return ResultCache.make_key(
        **BASE_FIELDS, mode=mode,
        prompt_version=prompt_version_for_cache_key(mode))


def test_two_resolved_modes_with_different_versions_get_different_keys():
    """Regression: an "auto" request resolved to range_chart (v5) and one
    resolved to abundance_diagram (v4) must never share a cache entry."""
    assert _key("range_chart") != _key("abundance_diagram")
    assert prompt_version_for_cache_key("range_chart") == "v5"
    assert prompt_version_for_cache_key("abundance_diagram") == "v4"


def test_same_resolved_mode_always_gets_the_same_key():
    assert _key("range_chart") == _key("range_chart")


def test_contract_bump_invalidates_the_auto_path_key():
    """The measured symptom: with the OLD lenient lookup the auto path keyed
    the range_chart result under the "v3" fallback, so the v4->v5 contract
    bump could not invalidate it. With the resolved-mode key, a pre-bump
    ("v3") entry and the post-bump key are DIFFERENT entries."""
    resolved_key = _key("range_chart")
    stale_v3_key = ResultCache.make_key(
        **BASE_FIELDS, mode="range_chart",
        prompt_version=prompt_version_for_mode("auto"))  # the old bug: "v3"
    assert stale_v3_key != resolved_key
    # and the resolved key visibly carries the bumped version
    assert prompt_version_for_cache_key("range_chart") == "v5"


# ---------------------------------------------------------------------------
# the server call sites (source-level; no server boot)
# ---------------------------------------------------------------------------

def _server_source() -> str:
    return (ROOT / "server.py").read_text(encoding="utf-8")


def test_server_cache_key_sites_use_the_strict_helper():
    src = _server_source()
    sites = [m.start() for m in re.finditer(r"cache\.make_key\(", src)]
    assert len(sites) == 2, (
        "expected exactly the single-run and multi-run /api/extract key "
        "sites; a new cache-key site must use prompt_version_for_cache_key")
    for start in sites:
        block = src[start:start + 900]
        assert "prompt_version=prompt_version_for_cache_key(mode)" in block, (
            "a result cache key is being built with the LENIENT version "
            "lookup - an unresolved 'auto' mode would silently key results "
            "under the v3 fallback (audit item 1)")
        assert "prompt_version=prompt_version_for_mode(" not in block


def test_server_resolves_auto_before_building_the_key():
    """Ordering pin: every cache.make_key( site must appear AFTER the
    resolve_auto_mode() call inside the /api/extract handler, so `mode` is
    the RESOLVED mode when the key is built."""
    src = _server_source()
    resolve_at = src.index("mode, classify_result = resolve_auto_mode(")
    for m in re.finditer(r"cache\.make_key\(", src):
        assert m.start() > resolve_at


def test_prompt_py_carries_no_stale_js_parity_claim():
    """Audit item 1: the 'js/prompt.js still carries the old versions' note
    is false since 5e724aa (both sides v5/v4/v2 - pinned by
    tests/test_prompt_fixes.py)."""
    src = (ROOT / "rca_core" / "prompt.py").read_text(encoding="utf-8")
    assert "still carries the old versions and the old prompt text" not in src
    assert "js/prompt.js is NOT touched here" not in src
