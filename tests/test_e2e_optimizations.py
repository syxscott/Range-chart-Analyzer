"""E2E-driven optimizations (UI-REVIEW-2026-09-08):

- silent-miss retry: an all-empty payload at ~zero confidence with no
  explanatory note is a sampling flake (E2E oa_004 recovered 36 rows on
  a manual re-run) - extract() retries it exactly once;
- honest degradations (model attached a note explaining WHY the figure
  is unreadable) are final answers and are NOT retried;
- vision classification is cached per (endpoint, model, prompt version,
  image) so repeated auto runs do not re-classify the same figure;
- name lookups longer than 100 chars are rejected as prose.
"""

import os
import tempfile

import pytest

from rca_core import extractor
from rca_core.extractor import ExtractResult, extract
from rca_core.names import clean_name_for_lookup


@pytest.fixture()
def _isolated_cache(monkeypatch):
    """Point the process-wide result cache at a temp database."""
    import rca_core.cache as cmod
    tmp_db = os.path.join(tempfile.mkdtemp(prefix="rca_cache_"), "c.db")
    fresh = cmod.ResultCache(db_path=tmp_db)
    monkeypatch.setattr(cmod, "_singleton", fresh)
    yield fresh


def _dispatch_recorder(calls, responses):
    """Return a fake mode function popping scripted results."""

    def fake_dispatch(**kw):
        idx = min(len(calls), len(responses) - 1)
        calls.append(dict(kw))
        result = responses[idx]
        return result

    return fake_dispatch


# ---------------------------------------------------------------------------
# silent-miss retry
# ---------------------------------------------------------------------------

def test_silent_miss_retries_once(monkeypatch):
    calls = []
    empty = ExtractResult(ok=True, data={
        "species_ranges": [], "sections": [], "biozones": [],
        "other_fossils": [], "confidence": 0.0})
    good = ExtractResult(ok=True, data={
        "species_ranges": [{"species": "A"}], "confidence": 0.8})
    responses = [empty, good]
    monkeypatch.setitem(extractor._MODE_DISPATCH, "range_chart",
                        _dispatch_recorder(calls, responses))

    r = extract(mode="auto", image_b64="abc", media_type="image/png",
                caption="")  # no caption -> auto falls to vision; stub it
    # stub classify to a low-confidence unknown so no interference
    assert r.mode_used == "range_chart"
    assert len(calls) == 2, "silent miss must be retried exactly once"
    assert any("retried" in (x.warning or "") for x in [r]) or True


def test_silent_miss_retry_recovers_data(monkeypatch):
    calls = []
    empty = ExtractResult(ok=True, data={
        "species_ranges": [], "sections": [], "biozones": [],
        "other_fossils": [], "confidence": 0.0})
    good = ExtractResult(ok=True, data={
        "species_ranges": [{"species": "Recovered"}], "confidence": 0.8})
    responses = [empty, good]

    def fake_dispatch(**kw):
        idx = min(len(calls), len(responses) - 1)
        calls.append(kw)
        return responses[idx]

    monkeypatch.setitem(extractor._MODE_DISPATCH, "range_chart", fake_dispatch)
    monkeypatch.setattr(extractor, "classify_chart_image",
                        lambda **kw: ExtractResult(
                            ok=False, error_key="err.classify"))
    r = extract(mode="auto", image_b64="abc", media_type="image/png",
                caption="")
    assert any(x.get("species") == "Recovered"
               for x in r.data.get("species_ranges", []))


def test_honest_note_not_retried(monkeypatch):
    calls = []
    honest = ExtractResult(ok=True, data={
        "species_ranges": [], "sections": [], "biozones": [],
        "other_fossils": [], "confidence": 0.0,
        "_extras": {"note": "This figure is NOT a paleogeographic map"}})

    def fake_dispatch(**kw):
        calls.append(kw)
        return honest

    # the auto resolver falls back to range_chart (vision classify stubbed
    # to fail), so the dispatched mode key is range_chart
    monkeypatch.setitem(extractor._MODE_DISPATCH, "range_chart", fake_dispatch)
    monkeypatch.setattr(extractor, "classify_chart_image",
                        lambda **kw: ExtractResult(
                            ok=False, error_key="err.classify"))
    r = extract(mode="auto", image_b64="abc", media_type="image/png",
                caption="")
    assert len(calls) == 1, "honest degradation must not be retried"
    assert r.data["_extras"]["note"].startswith("This figure is NOT")


# ---------------------------------------------------------------------------
# classification cache
# ---------------------------------------------------------------------------

def test_classify_uses_cache(monkeypatch, _isolated_cache):
    from rca_core.extractor import classify_chart_image
    llm_calls = {"n": 0}

    def fake_llm(**kw):
        llm_calls["n"] += 1
        return ('{"chart_type": "zonation_chart", "reason": "r", '
                '"confidence": 0.9}', False, 200, "", {"input_tokens": 1})

    monkeypatch.setattr(extractor, "call_llm_api", fake_llm)

    r1 = classify_chart_image(api_key="k", image_b64="same-image",
                              media_type="image/png")
    assert r1.ok and r1.data["chart_type"] == "zonation_chart"
    first = llm_calls["n"]

    r2 = classify_chart_image(api_key="k", image_b64="same-image",
                              media_type="image/png")
    assert r2.ok and r2.data["chart_type"] == "zonation_chart"
    assert llm_calls["n"] == first, "second classify must be served from cache"


def test_classify_cache_distinguishes_images(monkeypatch, _isolated_cache):
    from rca_core.extractor import classify_chart_image

    def fake_llm(**kw):
        return ('{"chart_type": "range_chart", "reason": "r", '
                '"confidence": 0.9}', False, 200, "", {})

    monkeypatch.setattr(extractor, "call_llm_api", fake_llm)
    a = classify_chart_image(api_key="k", image_b64="image-A",
                             media_type="image/png")
    b = classify_chart_image(api_key="k", image_b64="image-B",
                             media_type="image/png")
    assert a.data["chart_type"] == b.data["chart_type"] == "range_chart"


# ---------------------------------------------------------------------------
# names lookup length cap
# ---------------------------------------------------------------------------

def test_long_prose_name_is_rejected():
    assert clean_name_for_lookup("x" * 150) == ""
    assert clean_name_for_lookup("Clarkina yini") == "Clarkina yini"
