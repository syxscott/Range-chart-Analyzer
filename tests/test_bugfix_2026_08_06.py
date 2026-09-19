"""Regression tests for the 2026-08-06 bug-hunt fixes.

Covers:
  * P1 — multi-run extraction merged results dropped the image fingerprint:
    server.py / gui_fluent.py / gui.py built the merged ExtractResult with
    image_sha256="" and server._write_history_record's fallback read the
    never-assigned ``_image_b64_for_audit`` attribute, so every multi-run
    history record was stamped with the empty-bytes SHA-256 (e3b0c44...) and
    get_by_sha256 grouped unrelated multi-run records as one image.
  * P2 — merge_results single-run branch shallow-copied the input, aliasing
    every nested structure (formations, _extras, lithology_blocks, ...) with
    the caller's run so a downstream in-place mutation rewrote the source.
  * P3 — PROMPT_VERSION's abundance key ("abundance") never matched the mode
    string ("abundance_diagram") that prompt_version_for_mode() is called
    with, so the version always fell back to "v3" and an abundance prompt
    upgrade never invalidated the result cache.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rca_core.aggregate import merge_results  # noqa: E402
from rca_core.cache import ResultCache  # noqa: E402
from rca_core.extractor import ExtractResult  # noqa: E402
from rca_core.image_hash import compute_image_sha256_from_b64  # noqa: E402
from rca_core.prompt import PROMPT_VERSION, prompt_version_for_mode  # noqa: E402


# ---------------------------------------------------------------------------
# P1 — multi-run audit fingerprint
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_history_store(monkeypatch, tmp_path):
    """Point server._history_store_singleton at a throwaway DB."""
    from rca_core import Database, HistoryStore
    db = Database(path=str(tmp_path / "rca_bugfix.db"))
    store = HistoryStore(db=db)
    import server
    monkeypatch.setattr(server, "_history_store_singleton", lambda: store)
    yield store
    db.close()


def test_write_history_uses_real_fingerprint(tmp_history_store):
    """A merged multi-run ExtractResult carrying the source fingerprint must
    persist THAT fingerprint (and it must appear in request_meta too), not
    the empty-bytes sentinel."""
    import server

    sha = "a" * 64
    res = ExtractResult(
        ok=True,
        data={"confidence": 0.5, "runs": 2},
        raw='{"confidence": 0.5}',
        image_sha256=sha,
        request_meta={"model": "MiniMax-M3", "mode": "range_chart"},
    )
    server._write_history_record(
        res, mode="range_chart", runs=2, provider=None,
        max_tokens=4000, chart_lang="auto",
    )
    recs = tmp_history_store.list(limit=10)
    assert recs, "history record should be written"
    assert recs[0].image_sha256 == sha
    assert recs[0].request_meta.get("image_sha256") == sha


def test_write_history_empty_fingerprint_is_not_sentinel(tmp_history_store):
    """REVIEW-2026-09-20 (item 5) — this test previously pinned the OPPOSITE
    contract ("the fallback remains the deterministic empty-bytes hash").

    That sentinel was the bug: ``e3b0c442...`` is the SHA-256 of zero bytes, so
    HistoryStore.get_by_sha256() answered "every record for this image" with
    every unrelated fingerprint-less record, and the audit trail implied a
    digest for an image that was never hashed. A missing fingerprint is now
    stored as missing (NULL column + request_meta flag) instead of as a bogus
    shared digest.
    """
    import server

    res = ExtractResult(ok=True, data={"confidence": 0.5, "runs": 2}, raw="{}")
    server._write_history_record(
        res, mode="range_chart", runs=2, provider=None,
        max_tokens=4000, chart_lang="auto",
    )
    recs = tmp_history_store.list(limit=10)
    assert recs
    assert recs[0].image_sha256 == ""
    assert recs[0].image_sha256 != compute_image_sha256_from_b64("")
    assert recs[0].request_meta.get("image_sha256_missing") is True


def test_extract_result_default_fingerprint_is_empty():
    """Baseline: an unadorned ExtractResult still defaults to "" — the
    merged paths must explicitly propagate the real fingerprint (this is the
    field P1 restores)."""
    assert ExtractResult().image_sha256 == ""


# ---------------------------------------------------------------------------
# P2 — merge_results single-run must not alias the input
# ---------------------------------------------------------------------------

def test_merge_single_run_does_not_alias_nested_structures():
    run = {
        "sections": [{"name": "S1", "formations": ["F1", "F2"]}],
        "species_ranges": [{
            "species": "A", "section": "S1",
            "range_base": "Bed 1", "range_top": "Bed 5",
            "biozone": "Z", "_extras": {"k": [1, 2]},
        }],
        "confidence": 0.9,
    }
    merged = merge_results([run])
    # In-place mutations on the merged result must not leak into the input.
    merged["sections"][0]["formations"].append("F9")
    merged["species_ranges"][0]["_extras"]["k"].append(3)
    merged["species_ranges"][0]["_extras"]["other"] = "x"
    assert run["sections"][0]["formations"] == ["F1", "F2"]
    assert run["species_ranges"][0]["_extras"]["k"] == [1, 2]
    assert "_extras" not in run["species_ranges"][0] or "other" not in run["species_ranges"][0]["_extras"]


def test_merge_single_run_agreement_fields_still_present():
    run = {
        "species_ranges": [{"species": "A", "section": "S1"}],
        "confidence": 0.9,
    }
    merged = merge_results([run])
    assert merged["species_ranges"][0]["agreement_count"] == 1
    assert merged["species_ranges"][0]["agreement"] == "1/1"
    assert merged["runs"] == 1


# ---------------------------------------------------------------------------
# P3 — abundance prompt version canonical key
# ---------------------------------------------------------------------------

def test_prompt_version_abundance_canonical_key():
    """prompt_version_for_mode('abundance_diagram') must resolve the
    canonical per-mode version (previously it fell through to the 'v3'
    default because the dict was keyed 'abundance')."""
    assert "abundance_diagram" in PROMPT_VERSION
    assert PROMPT_VERSION["abundance_diagram"] == PROMPT_VERSION["abundance"]
    assert prompt_version_for_mode("abundance_diagram") == PROMPT_VERSION["abundance_diagram"]
    # Legacy lookup still works (compat alias).
    assert prompt_version_for_mode("abundance") == PROMPT_VERSION["abundance"]


def test_prompt_version_abundance_upgrade_invalidates_cache_key():
    """The whole point of the per-mode version: bumping the abundance
    prompt version must produce a DIFFERENT cache key so stale cached
    results are not served after a prompt upgrade."""
    base = dict(
        endpoint="e", model="m", api_format="anthropic",
        extra_headers=[], extra_body=[],
        max_tokens=4000, chart_lang="zh",
        image_b64="abc", caption="", media_type="image/png",
        mode="abundance_diagram",
    )
    k_v3 = ResultCache.make_key(**base, prompt_version="v3")
    k_v4 = ResultCache.make_key(**base, prompt_version="v4")
    assert k_v3 != k_v4
    # And the actual current version is what the cache layer would use.
    assert prompt_version_for_mode("abundance_diagram") == PROMPT_VERSION["abundance_diagram"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
