"""Sprint B (REVIEW-2026-09-04) regression tests — rca_core pipeline fixes.

Mandatory coverage (per review):
  1. exporter: EXPORT_INVARIANTS no longer carries ``has_biozone_or_age``
     (species rows only ever had an optional ``biozone``), so biozone-less
     results pass validation and to_xlsx instead of raising ValueError.
  2. json_utils: multiple fenced blocks — the FIRST block that strictly
     parses to a dict with a known payload root key wins. A leading
     schema fence must no longer evict the real payload fence that
     follows it.
  3. llm: OpenAI reasoning models (o1/o3/o4-*) get the system prompt
     merged into the head of the user message instead of silently
     dropped; non-reasoning models keep a separate role:system message.

Also pins the remaining Sprint B fixes (items 4-14 of the review):
  4.  ssrf: pinning layer exempts loopback, matching validate_endpoint_local_ok.
  5.  extractor: root_ids int/str type confusion in phylo normalization.
  6.  extractor: is_leaf corrections are recorded in the node's metadata.
  7.  extractor: failed normalize / unusable-payload results carry image_sha256.
  8.  extractor: float-truncated bed indices carry a row-level _warning.
  9.  extractor: to_newick warns (RuntimeWarning) when it drops unreachable nodes.
  10. error_utils: retry_with_backoff retryable predicate semantics.
  11. cache: put() swallows the cross-process INSERT UNIQUE race.
  12. extractor: paleomap / scatter sub-rows preserve unknown keys via _extras.
  13. aggregate: B-1 species-mode restoration compares _norm to _norm.
  14. i18n: zh text for quality.abundance_sum_violation carries {sample}.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.request
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import rca_core.extractor as E  # noqa: E402
from rca_core import exporter  # noqa: E402
from rca_core.aggregate import merge_results  # noqa: E402
from rca_core.cache import ResultCache  # noqa: E402
from rca_core.error_utils import retry_with_backoff  # noqa: E402
from rca_core.i18n import TRANSLATIONS  # noqa: E402
from rca_core.json_utils import safe_json_loads, strip_markdown_fence  # noqa: E402
from rca_core.llm import ApiFormat, LlmProvider, _call_openai  # noqa: E402
from rca_core.ssrf import pinned_endpoint_ip, validate_endpoint  # noqa: E402


# ---------------------------------------------------------------------------
# 1. exporter: has_biozone_or_age removed
# ---------------------------------------------------------------------------

class TestExporterBiozoneInvariantRemoved:
    def test_constraint_removed_from_registry(self):
        constraints = exporter.EXPORT_INVARIANTS["species_ranges"]["constraints"]
        assert "has_biozone_or_age" not in constraints
        # The genuinely scientific constraint survives.
        assert "range_base_le_range_top" in constraints

    def test_biozone_less_row_passes_validation(self):
        """normalize_result produces species rows with only optional
        biozone and NEVER an ``age`` key — such rows must validate OK."""
        data = {
            "species_ranges": [
                # A realistic normalize_result row: no biozone, no age.
                {"species": "Genus species", "section": "S1",
                 "range_base": "1", "range_top": "3"},
            ],
            "biozones": [],
            "sections": [{"name": "S1"}],
        }
        ok, issues = exporter.validate_export_invariants(data)
        assert ok, f"biozone-less rows must pass, got issues: {issues}"
        assert issues == []

    def test_biozone_less_row_exports_to_xlsx(self):
        """The original symptom: to_xlsx raised ValueError and XLSX export
        failed wholesale for results without biozones."""
        pytest.importorskip("openpyxl")
        data = {
            "sections": [{"name": "S1", "age_range": "Permian",
                          "formations": [], "formation_thickness_m": "",
                          "coordinates": ""}],
            "species_ranges": [
                {"species": "Sp. x", "section": "S1",
                 "range_base": "Bed 1", "range_top": "Bed 3",
                 "biozone": ""},
            ],
            "biozones": [],
            "other_fossils": [],
            "confidence": 0.9,
        }
        blob = exporter.to_xlsx(data)
        assert isinstance(blob, bytes) and len(blob) > 0

    def test_inverted_ranges_still_rejected(self):
        """Removing the biozone constraint must not weaken FAD<=LAD."""
        data = {
            "species_ranges": [
                {"species": "G", "section": "X",
                 "range_base": "9", "range_top": "5"},
            ],
            "biozones": [],
            "sections": [{"name": "X"}],
        }
        ok, issues = exporter.validate_export_invariants(data)
        assert not ok
        assert any(i.get("constraint") == "range_base_le_range_top"
                   for i in issues)


# ---------------------------------------------------------------------------
# 2. json_utils: schema fence must not evict the payload fence
# ---------------------------------------------------------------------------

_SCHEMA_THEN_PAYLOAD = (
    "Sure! Here is the JSON schema I will follow:\n"
    "```json\n"
    + json.dumps({
        "type": "object",
        "required": ["species_ranges"],
        "properties": {"species_ranges": {"type": "array"}},
    })
    + "\n```\n"
    "And here is the actual extraction result:\n"
    "```json\n"
    + json.dumps({
        "sections": [{"name": "S1", "age_range": "Permian",
                      "formations": [], "formation_thickness_m": "",
                      "coordinates": ""}],
        "species_ranges": [{"species": "Genus species", "section": "S1",
                            "range_top": "3", "range_base": "1",
                            "biozone": "Zone A"}],
        "biozones": [],
        "other_fossils": [],
        "confidence": 0.9,
    })
    + "\n```\n"
    "Let me know if you need anything else."
)


class TestFencePayloadSelection:
    def test_schema_fence_first_payload_fence_second(self):
        """Regression: the non-greedy fence regex returned only the FIRST
        block, so the schema evicted the real payload."""
        parsed = safe_json_loads(_SCHEMA_THEN_PAYLOAD)
        assert "species_ranges" in parsed, (
            f"real payload lost to schema fence: {list(parsed.keys())}"
        )
        assert parsed["species_ranges"][0]["species"] == "Genus species"
        assert parsed["confidence"] == 0.9
        # No schema-only keys leaked through.
        assert "type" not in parsed
        assert "properties" not in parsed

    def test_strip_markdown_fence_returns_payload_block(self):
        assert strip_markdown_fence(_SCHEMA_THEN_PAYLOAD).startswith(
            '{"sections"')

    def test_payload_fence_first_still_wins(self):
        """First qualifying block is returned (payload happens to be first)."""
        text = (
            "```json\n"
            '{"sections": [{"name": "S1"}], "confidence": 0.5}\n'
            "```\n"
            "```json\n"
            '{"type": "object", "properties": {}}\n'
            "```\n"
        )
        parsed = safe_json_loads(text)
        assert parsed.get("sections") == [{"name": "S1"}]
        assert parsed.get("confidence") == 0.5

    def test_no_known_root_key_falls_back_to_first_block(self):
        """Backward compat: if no block carries a known root key, keep the
        historical first-block behaviour (later fallback chain still runs)."""
        text = 'prose ```json\n{"foo": 1}\n``` middle ```json\n{"bar": 2}\n``` tail'
        assert strip_markdown_fence(text) == '{"foo": 1}'

    def test_single_fence_unchanged(self):
        assert strip_markdown_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
        assert strip_markdown_fence('Here:\n```json\n{"a": 1}\n```\nThanks'
                                    ) == '{"a": 1}'

    def test_unparseable_blocks_fall_back_to_first(self):
        # Prose prefix so the whole-text single-fence regex cannot swallow
        # both blocks (that pre-existing path is unchanged by the fix).
        text = 'Here: ```json\n{not json\n```\n```json\n{"also bad": }\n```'
        assert strip_markdown_fence(text) == "{not json"


# ---------------------------------------------------------------------------
# 3. llm: reasoning models must keep the system prompt
# ---------------------------------------------------------------------------

class _FakeResponse:
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


def _capture_openai_call(model, system_prompt):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        resp = {"choices": [{"message": {"content": "{}"},
                             "finish_reason": "stop"}]}
        return _FakeResponse(json.dumps(resp).encode(), 200)

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        prov = LlmProvider(
            api_format=ApiFormat.OPENAI,
            endpoint="https://api.openai.com/v1",
            api_key="sk-test", model=model,
        )
        _call_openai(
            provider=prov, system_prompt=system_prompt,
            image_b64="QUFB", media_type="image/png",
            user_text="hi", max_tokens=100, timeout_sec=5,
        )
    finally:
        urllib.request.urlopen = orig
    return captured["body"]


def _user_text_parts(body):
    user = next(m for m in body["messages"] if m["role"] == "user")
    return [p.get("text", "") for p in user["content"]
            if isinstance(p, dict) and p.get("type") == "text"]


class TestReasoningSystemPromptMerged:
    def test_reasoning_model_merges_system_prompt_into_user_text(self):
        """Regression: o1/o3/o4 models reject role:system; the old code
        responded by silently dropping the system prompt entirely."""
        body = _capture_openai_call("o1-mini", "system-prompt-X")
        roles = [m["role"] for m in body["messages"]]
        assert "system" not in roles
        assert "developer" not in roles  # relay/aggregator compatibility
        texts = _user_text_parts(body)
        assert texts and texts[0] == "system-prompt-X\n\nhi"
        # Reasoning-model request-shape fixes are unaffected.
        assert "max_completion_tokens" in body
        assert "response_format" not in body

    def test_non_reasoning_model_keeps_system_role(self):
        body = _capture_openai_call("gpt-4o", "system-prompt-X")
        sys_msgs = [m for m in body["messages"] if m["role"] == "system"]
        assert len(sys_msgs) == 1 and sys_msgs[0]["content"] == "system-prompt-X"
        # The user text must NOT be doubled.
        assert _user_text_parts(body) == ["hi"]
        assert "response_format" in body


# ---------------------------------------------------------------------------
# 4. ssrf: pinning exempts loopback only
# ---------------------------------------------------------------------------

class TestPinningLoopbackExemption:
    def test_loopback_literal_ip_pins_without_public_check(self):
        assert pinned_endpoint_ip("https://127.0.0.1:8443/v1") == "127.0.0.1"
        assert pinned_endpoint_ip("http://[::1]:11434") == "::1"

    def test_localhost_name_pins_to_loopback(self):
        ip = ipaddress.ip_address(pinned_endpoint_ip("https://localhost:8443/v1"))
        assert ip.is_loopback

    def test_nonloopback_private_ip_still_rejected(self):
        with pytest.raises(ValueError):
            pinned_endpoint_ip("https://192.168.1.10/v1")

    def test_strict_validator_still_rejects_loopback(self):
        """The fix must not weaken validate_endpoint (public-https policy)."""
        ok, _why = validate_endpoint("https://127.0.0.1:8443")
        assert not ok


# ---------------------------------------------------------------------------
# 5./6. extractor: root_ids type confusion + is_leaf correction trail
# ---------------------------------------------------------------------------

class TestPhyloNormalizationSprintB:
    def test_integer_root_ids_do_not_misclassify_root(self):
        """Regression: {"root_ids":[1]} with node id "1" raised
        "Non-root node 1 must have a parent"."""
        raw = {
            "root_ids": [1],  # ints straight from the model
            "nodes": [
                {"id": 1, "parent": None, "name": "root", "is_leaf": False},
                {"id": 2, "parent": 1, "name": "kid", "is_leaf": True},
            ],
            "confidence": 0.8,
        }
        res = E.normalize_phylogenetic_tree_result(raw)
        assert res["root_ids"] == ["1"]
        assert res["nodes"][0]["parent"] is None
        assert res["nodes"][0]["is_leaf"] is False

    def test_is_leaf_correction_recorded_in_metadata(self):
        raw = {
            "root_ids": ["r"],
            "nodes": [
                # Model claims the root is a leaf; the reverse check
                # proves it has a child. The correction must leave a trace.
                {"id": "r", "parent": None, "name": "root", "is_leaf": True},
                {"id": "c", "parent": "r", "name": "kid", "is_leaf": True},
            ],
        }
        res = E.normalize_phylogenetic_tree_result(raw)
        root = res["nodes"][0]
        assert root["is_leaf"] is False
        assert (root.get("metadata") or {}).get("_is_leaf_corrected") is True
        child = res["nodes"][1]
        assert child["is_leaf"] is True
        assert "_is_leaf_corrected" not in (child.get("metadata") or {})


# ---------------------------------------------------------------------------
# 7. extractor: failed paths carry image_sha256
# ---------------------------------------------------------------------------

class TestFailedPathsCarryImageSha:
    def test_normalize_failed_carries_image_sha256(self, monkeypatch):
        monkeypatch.setattr(
            E, "call_llm_api",
            lambda **kw: ('{"sections": []}', False, 200, b"", None))

        def _boom(_parsed):
            raise RuntimeError("boom")

        monkeypatch.setattr(E, "normalize_result", _boom)
        res = E.extract_range_chart(api_key="k", image_b64="QUFB",
                                    media_type="image/png")
        assert not res.ok and res.error_key == "err.extract"
        assert res.image_sha256 and len(res.image_sha256) == 64

    def test_unrecognized_payload_carries_image_sha256(self, monkeypatch):
        monkeypatch.setattr(
            E, "call_llm_api",
            lambda **kw: ('{"foo": 1}', False, 200, b"", None))
        res = E.extract_range_chart(api_key="k", image_b64="QUFB",
                                    media_type="image/png")
        assert not res.ok and res.error_key == "err.parse"
        assert res.image_sha256 and len(res.image_sha256) == 64


# ---------------------------------------------------------------------------
# 8. extractor: float-truncated bed indices carry a row-level _warning
# ---------------------------------------------------------------------------

class TestBedIndexTruncationWarning:
    def test_numeric_string_truncation_flagged(self):
        res = E.normalize_columnar_result({
            "sections": [{"id": "S1", "lithology_blocks": [
                {"pattern": "packstone", "range_top_idx": "8.5",
                 "range_base_idx": 2},
            ]}],
        })
        blk = res["sections"][0]["lithology_blocks"][0]
        assert blk["range_top_idx"] == 8  # truncated, not lost
        assert blk["_warning"] == "range_top_idx_truncated"

    def test_float_input_truncation_flagged(self):
        res = E.normalize_columnar_result({
            "sections": [{"id": "S1", "age_units": [
                {"label": "U1", "range_top_idx": 9.5, "range_base_idx": 3},
            ]}],
        })
        unit = res["sections"][0]["age_units"][0]
        assert unit["range_top_idx"] == 9
        assert "_warning" in unit

    def test_clean_ints_are_not_flagged(self):
        res = E.normalize_columnar_result({
            "sections": [{"id": "S1", "lithology_blocks": [
                {"pattern": "mudstone", "range_top_idx": 9,
                 "range_base_idx": "3"},
            ]}],
        })
        blk = res["sections"][0]["lithology_blocks"][0]
        assert "_warning" not in blk


# ---------------------------------------------------------------------------
# 9. extractor: to_newick warns when dropping unreachable nodes
# ---------------------------------------------------------------------------

class TestToNewickUnreachableWarning:
    def test_unreachable_node_warns_and_is_dropped(self):
        tree = {
            "root_ids": ["A"],
            "nodes": [
                {"id": "A", "parent": None, "name": "A",
                 "is_leaf": True, "branch_length": 0.3},
                # Dangling subtree: parent "lost" is not rooted anywhere.
                {"id": "X", "parent": "lost", "name": "X",
                 "is_leaf": True, "branch_length": 0.1},
            ],
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = E.to_newick(tree)
        assert out == "(A:0.3);"
        assert caught, "silent drop of unreachable nodes"
        assert any("unreachable" in str(w.message) and "X" in str(w.message)
                   for w in caught)

    def test_fully_reachable_tree_does_not_warn(self):
        tree = {
            "root_ids": ["A"],
            "nodes": [
                {"id": "A", "parent": None, "name": "A", "is_leaf": False},
                {"id": "B", "parent": "A", "name": "B", "is_leaf": True},
            ],
        }
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            out = E.to_newick(tree)
        assert "B" in out
        assert not caught


# ---------------------------------------------------------------------------
# 10. error_utils: retryable predicate semantics
# ---------------------------------------------------------------------------

class TestRetryWithBackoffPredicate:
    def test_retryable_result_keeps_retrying_until_exhausted(self):
        calls = {"n": 0}

        def f():
            calls["n"] += 1
            return "retry-me"

        res = retry_with_backoff(
            f, max_retries=2, initial_delay=0,
            retryable=lambda r: r == "retry-me")
        # Regression: the old dead logic returned on the first attempt.
        assert calls["n"] == 3
        assert res == "retry-me"

    def test_non_retryable_result_returns_immediately(self):
        calls = {"n": 0}

        def f():
            calls["n"] += 1
            return "done"

        res = retry_with_backoff(
            f, max_retries=5, initial_delay=0, retryable=lambda r: False)
        assert calls["n"] == 1
        assert res == "done"


# ---------------------------------------------------------------------------
# 11. cache: put() survives the cross-process INSERT UNIQUE race
# ---------------------------------------------------------------------------

class TestCachePutIntegrityRace:
    def test_put_treats_integrity_error_as_already_exists(self, tmp_path):
        import rca_core.cache as cache_mod

        cache = ResultCache(db_path=str(tmp_path / "race.sqlite"))
        key = ResultCache.make_key(x="race")
        cache.put(key, {"v": 1})

        # Simulate the race: another process inserted the same key right
        # after our SELECT said "absent", so our INSERT hits UNIQUE(k).
        orig = cache_mod._rowid_for
        cache_mod._rowid_for = lambda conn, k: None
        try:
            cache.put(key, {"v": 2})  # must NOT raise IntegrityError
        finally:
            cache_mod._rowid_for = orig
        assert cache.get(key) == {"v": 2}


# ---------------------------------------------------------------------------
# 12. extractor: paleomap / scatter sub-rows honor the H8 _extras contract
# ---------------------------------------------------------------------------

class TestPaleomapScatterSubRowExtras:
    def test_paleomap_sub_row_keeps_unknown_keys(self):
        res = E.normalize_paleomap_result({
            "continents": [{"name": "Laurasia", "type": "continent",
                            "custom_field": "kept"}],
            "fossil_sites": [{"name": "Site 1", "lat_lon": "1,2",
                              "age": "100 Ma", "fossils": "A",
                              "marker_type": "fossil",
                              "collector": "someone"}],
        })
        assert res["continents"][0]["_extras"]["custom_field"] == "kept"
        assert res["fossil_sites"][0]["_extras"]["collector"] == "someone"

    def test_scatter_sub_row_keeps_unknown_keys(self):
        res = E.normalize_scatter_plot_result({
            "groups": [{"name": "G1", "color": "#fff", "marker": "o",
                        "custom_field": "kept"}],
            "points": [{"x": "1", "y": "2", "depth_hint": "3m"}],
            "outliers": [{"x": "9", "y": "9", "reason": "manual",
                          "flagged_by": "reviewer"}],
        })
        assert res["groups"][0]["_extras"]["custom_field"] == "kept"
        assert res["points"][0]["_extras"]["depth_hint"] == "3m"
        assert res["outliers"][0]["_extras"]["flagged_by"] == "reviewer"


# ---------------------------------------------------------------------------
# 13. aggregate: B-1 species-mode restoration is a _norm-to-_norm compare
# ---------------------------------------------------------------------------

class TestAggregateSpeciesModeRestoration:
    def test_restoration_runs_when_mode_is_not_normalized(self):
        """Regression: `_norm(sp_val) == species_mode` compared a normalized
        value against the RAW mode, so any mode string that wasn't already
        lowercase + single-spaced made the whole restoration block no-op.
        Two qualifier-bearing spellings ("Genus sp." / "Genus  sp.") tie on
        count; with the fix the restoration kicks in and keeps the
        first-seen original spelling instead of the sorted tiebreak
        artifact with the double space."""
        def run(sp):
            return {"sections": [], "species_ranges": [
                {"species": sp, "section": "X", "range_top": "Bed 9",
                 "range_base": "Bed 7", "biozone": "B Zone"}],
                "biozones": [], "other_fossils": [], "confidence": 0.9}

        merged = merge_results(
            [run("Genus sp."), run("Genus  sp.")], total_runs=2)
        rows = merged.get("species_ranges", [])
        assert len(rows) == 1
        # Before the fix the block no-op'd and the double-spaced sorted
        # tiebreak artifact ("Genus  sp.") survived untouched.
        assert rows[0]["species"] == "Genus sp."


# ---------------------------------------------------------------------------
# 14. i18n: zh message carries the {sample} placeholder
# ---------------------------------------------------------------------------

class TestI18nAbundanceSumViolationPlaceholder:
    def test_all_languages_render_sample_and_sum(self):
        params = {"sample": "S1 / Level 3", "sum": "97.5"}
        for lang in ("zh", "en", "ja"):
            tpl = TRANSLATIONS[lang]["quality.abundance_sum_violation"]
            rendered = tpl.format(**params)
            assert "S1 / Level 3" in rendered, \
                f"{lang} message missing {{sample}} placeholder"
            assert "97.5" in rendered
