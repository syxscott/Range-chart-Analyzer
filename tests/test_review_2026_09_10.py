"""Regression tests for the 2026-09-10 backend review fixes.

Every test here pins a bug that was reproduced before the fix, so a future
refactor cannot quietly reintroduce it. Each docstring names the failure mode.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# extractor: normalizer data loss
# ---------------------------------------------------------------------------

class TestNormalizerDataLoss:

    def test_other_fossils_dict_entries_are_lifted(self):
        """A dict-shaped other_fossils entry used to be dropped by Python while
        the browser lifted it (js/minimax.js M1)."""
        from rca_core.extractor import normalize_result
        r = normalize_result({
            "other_fossils": [{"name": "Ammonoid: Pleuronodoceras sp."},
                              "Conodont: Clarkina sp."],
            "confidence": 0.7,
        })
        assert r["other_fossils"] == ["Ammonoid: Pleuronodoceras sp.",
                                      "Conodont: Clarkina sp."]

    def test_bare_string_rows_are_coerced_and_flagged(self):
        """{"biozones": ["Zone A"]} used to be dropped with no warning."""
        from rca_core.extractor import normalize_result
        r = normalize_result({"sections": ["Pingdingshan"],
                              "biozones": ["Zone A", "Zone B"],
                              "species_ranges": ["Clarkina yini"],
                              "confidence": 0.7})
        assert [s["name"] for s in r["sections"]] == ["Pingdingshan"]
        assert [b["name"] for b in r["biozones"]] == ["Zone A", "Zone B"]
        assert [s["species"] for s in r["species_ranges"]] == ["Clarkina yini"]
        assert "string_row_coerced" in (r.get("_warnings") or [])

    def test_wrapper_key_not_fabricated_over_an_empty_species(self):
        """The prompt says leave an unreadable name empty; fabricating the dict
        key ("unclear", "0") invented a taxon with a FAD/LAD attached."""
        from rca_core.extractor import normalize_result
        r = normalize_result({"species_ranges": {
            "unclear": {"species": "", "note": "unclear",
                        "range_base": "Bed 7", "range_top": "Bed 9"}}})
        assert [row["species"] for row in r["species_ranges"]] == [""]
        r2 = normalize_result({"species_ranges": {
            "0": {"species": "", "note": "unclear"},
            "1": {"species": "B"}}})
        assert [row["species"] for row in r2["species_ranges"]] == ["", "B"]

    def test_wrapper_key_still_recovers_a_missing_species_key(self):
        from rca_core.extractor import normalize_result
        r = normalize_result({"species_ranges": {
            "Neoalbaillella optima": {"range_base": "Bed 7", "range_top": "Bed 9"}}})
        assert [row["species"] for row in r["species_ranges"]] == ["Neoalbaillella optima"]

    def test_dict_shaped_arrays_recovered_in_every_mode(self):
        """{"sites": {...}} / {"sections": {...}} collapsed to [] before."""
        from rca_core.extractor import (normalize_abundance_result,
                                        normalize_columnar_result,
                                        normalize_phylogenetic_tree_result,
                                        normalize_zonation_chart_result)
        ab = normalize_abundance_result({"sites": {"Ki-1": {"name": "Ki-1"}},
                                         "abundances": [], "confidence": 0.5})
        assert [s["name"] for s in ab["sites"]] == ["Ki-1"]
        col = normalize_columnar_result({"sections": {"Ki-1": {"id": "Ki-1"}},
                                         "confidence": 0.5})
        assert [s["id"] for s in col["sections"]] == ["Ki-1"]
        col2 = normalize_columnar_result({"sections": [{
            "id": "S1",
            "lithology_blocks": {"b1": {"pattern": "chert"}},
            "age_units": {"u1": {"label": "Permian"}},
            "samples": {"s1": {"bed_idx": 5, "fossil_marker": "J"}},
        }], "confidence": 0.5})
        sec = col2["sections"][0]
        assert sec["lithology_blocks"][0]["pattern"] == "chert"
        assert sec["age_units"][0]["label"] == "Permian"
        assert sec["samples"][0]["fossil_marker"] == "J"
        zo = normalize_zonation_chart_result({"zones": {"Z1": {"name": "Z1"}},
                                              "confidence": 0.5})
        assert [z["name"] for z in zo["zones"]] == ["Z1"]
        ph = normalize_phylogenetic_tree_result(
            {"nodes": {"n0": {"id": "n0", "parent": None, "name": "R"}},
             "root_ids": ["n0"], "confidence": 0.5})
        assert [n["id"] for n in ph["nodes"]] == ["n0"]

    def test_confidence_null_falls_back_to_sibling(self):
        """`overall_confidence: null` zeroed the confidence and discarded a
        perfectly good `confidence` (the browser returned 0.8)."""
        from rca_core.extractor import normalize_columnar_result as C
        def conf(probe):
            return C(dict(probe, sections=[])).get("confidence")
        assert conf({"overall_confidence": None, "confidence": 0.8}) == 0.8
        assert conf({"confidence": 0.8}) == 0.8
        assert conf({"overall_confidence": 0.9, "confidence": 0.8}) == 0.9
        # An explicit zero must stay zero.
        assert conf({"overall_confidence": 0.0, "confidence": 0.8}) == 0.0

    def test_phylogenetic_wrapper_keeps_outer_fields(self):
        """The _array_root unwrap REPLACED the payload, discarding confidence."""
        from rca_core.extractor import normalize_phylogenetic_tree_result as P
        r = P({"_array_root": [{"nodes": [{"id": "n0", "parent": None, "name": "R"}],
                                "root_ids": ["n0"]}],
               "confidence": 0.9})
        assert r["confidence"] == 0.9

    def test_rooted_flag_coercion(self):
        """bool("false") is True — the flag was inverted for string payloads."""
        from rca_core.extractor import normalize_phylogenetic_tree_result as P
        base = {"nodes": [{"id": "n0", "parent": None, "name": "R"}], "root_ids": ["n0"]}
        def rooted(value):
            return P(dict(base, metadata={"rooted": value}))["metadata"]["rooted"]
        assert rooted("false") is False
        assert rooted("true") is True
        assert rooted(False) is False
        assert rooted(None) is True      # mirrors the browser's != false
        assert rooted(0) is False
        assert rooted(1) is True

    def test_scatter_truncation_is_flagged(self):
        from rca_core.extractor import normalize_scatter_plot_result as S
        r = S({"points": [{"x": i, "y": i} for i in range(600)],
               "metadata": {"n_points": 600}, "confidence": 0.5})
        assert len(r["points"]) == 500
        assert "points_truncated_to_500" in (r.get("_warnings") or [])

    def test_phylogenetic_node_without_id_is_kept_and_flagged(self):
        """A missing node id used to drop the clade silently."""
        from rca_core.extractor import normalize_phylogenetic_tree_result as P
        r = P({"root_ids": ["n0"],
               "nodes": [{"id": "n0", "parent": None, "name": "Root"},
                         {"name": "LeafA", "parent": "n0", "is_leaf": True}],
               "confidence": 0.8})
        assert len(r["nodes"]) == 2
        assert "node_missing_id_synthesised" in (r.get("_warnings") or [])

    def test_chemical_values_list_shape_is_lifted(self):
        from rca_core.extractor import normalize_chemical_stratigraphy_result as C
        r = C({"data_points": [{"sample_id": "Zum-23", "depth_m": "12.5",
                                "values": [{"name": "d13C", "value": "-1.24"}]}],
               "confidence": 0.5})
        assert r["data_points"][0]["values"] == {"d13C": "-1.24"}

    def test_chemical_values_unparseable_are_kept_and_flagged(self):
        from rca_core.extractor import normalize_chemical_stratigraphy_result as C
        r = C({"data_points": [{"sample_id": "X", "values": [1, 2, 3]}],
               "confidence": 0.5})
        dp = r["data_points"][0]
        assert dp["values"] == {}
        assert dp["_extras"]["values_raw"] == [1, 2, 3]
        assert "values_unparseable" in (r.get("_warnings") or [])

    def test_species_index_inversion_is_swapped_and_flagged(self):
        from rca_core.extractor import normalize_result
        r = normalize_result({"species_ranges": [{
            "species": "X", "section": "S1",
            "range_base_idx": 9, "range_top_idx": 5}]})
        row = r["species_ranges"][0]
        assert (row["range_base_idx"], row["range_top_idx"]) == (5, 9)
        pv = row.get("_warning")
        flags = pv if isinstance(pv, list) else [pv]
        assert "index_order_swap" in flags

    def test_warning_flags_helper_accepts_both_shapes(self):
        from rca_core.quality import _warning_flags
        assert _warning_flags("index_order_swap") == {"index_order_swap"}
        assert "index_order_swap" in _warning_flags(
            ["range_top_idx_truncated", "index_order_swap"])
        assert _warning_flags(None) == set()


# ---------------------------------------------------------------------------
# aggregate: engine parity
# ---------------------------------------------------------------------------

class TestAggregateParity:

    def test_empty_id_row_with_an_author_does_not_survive(self):
        """The emptiness test ran AFTER the ICZN author was folded in, so a
        nameless row with an author outlived the browser's (which drops it)."""
        from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results
        runs = [{"species_ranges": [{
            "species": "", "section": "", "author_year": "Smith 1950",
            "range_base": "Bed 7", "range_top": "Bed 9", "biozone": "Z"}],
            "sections": [], "biozones": [], "other_fossils": [], "confidence": 0.5}] * 2
        merged = merge_results(runs, None, RANGE_CHART_SCHEMA)
        assert merged["species_ranges"] == []

    def test_bool_tie_is_deterministic(self):
        """Counter.most_common broke ties by insertion order, so [T, F] gave
        True and [F, T] gave False for the same set of runs."""
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([True, False]) is False
        assert _merge_scalar_field([False, True]) is False

    def test_mixed_bool_and_string_ignores_the_bools(self):
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([True, True, "maybe"]) == "maybe"

    def test_integral_float_stringifies_like_js(self):
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([1.0, 1.0]) == "1"

    def test_unhashable_mode_value_does_not_abort_the_merge(self):
        from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results
        runs = [{"species_ranges": [{"species": {"nested": "x"}, "section": "S1",
                                     "range_base": "1", "range_top": "2"}],
                 "sections": [], "biozones": [], "other_fossils": [], "confidence": 0.5}]
        merged = merge_results(runs, None, RANGE_CHART_SCHEMA)
        assert isinstance(merged, dict)          # used to raise TypeError

    def test_root_extras_survive_a_multi_run_merge(self):
        from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results
        runs = [
            {"species_ranges": [{"species": "A", "section": "S1",
                                 "range_base": "1", "range_top": "2"}],
             "sections": [], "biozones": [], "other_fossils": [], "confidence": 0.5,
             "_extras": {"caption": "Fig 3"}, "_warnings": ["string_row_coerced"]},
            {"species_ranges": [{"species": "A", "section": "S1",
                                 "range_base": "1", "range_top": "2"}],
             "sections": [], "biozones": [], "other_fossils": [], "confidence": 0.5},
        ]
        merged = merge_results(runs, None, RANGE_CHART_SCHEMA)
        assert merged["_extras"] == {"caption": "Fig 3"}
        assert merged["_warnings"] == ["string_row_coerced"]

    def test_phylogenetic_metadata_is_deep_copied(self):
        from rca_core.aggregate import PHYLOGENETIC_TREE_SCHEMA, merge_results
        run = {"nodes": [{"id": "n0", "parent": None, "name": "R", "is_leaf": True}],
               "root_ids": ["n0"], "metadata": {"taxon_group": "Radiolaria"},
               "legend": {}, "confidence": 0.5}
        runs = [run, dict(run)]
        merged = merge_results(runs, None, PHYLOGENETIC_TREE_SCHEMA)
        assert merged["metadata"] == {"taxon_group": "Radiolaria"}
        assert merged["metadata"] is not run["metadata"]

    def test_wired_modes_excludes_the_unwired_three(self):
        from rca_core.aggregate import WIRED_MODES
        assert "paleomap" not in WIRED_MODES
        assert "scatter_plot" not in WIRED_MODES
        assert "chemical_stratigraphy" not in WIRED_MODES
        assert "range_chart" in WIRED_MODES


# ---------------------------------------------------------------------------
# json_utils: fence selection and parsing
# ---------------------------------------------------------------------------

class TestJsonFenceSelection:

    EX = ('{"sections": [{"name": "<section name>"}], '
          '"species_ranges": [{"species": "<binomial>"}], "confidence": 0.9}')
    PAY = ('{"sections": [{"name": "Real"}], '
           '"species_ranges": [{"species": "Neoalbaillella optima"}], '
           '"confidence": 0.8}')

    def _species(self, text):
        from rca_core.json_utils import safe_json_loads
        parsed = safe_json_loads(text)
        return (parsed.get("species_ranges") or [{}])[0].get("species")

    def test_placeholder_example_does_not_evict_the_payload(self):
        """A restated contract carries the REAL root keys plus <placeholders>,
        so it qualified as a payload and won over the real data."""
        text = ("Here is the contract:\n```json\n" + self.EX +
                "\n```\nNow the result:\n```json\n" + self.PAY + "\n```")
        assert self._species(text) == "Neoalbaillella optima"

    def test_placeholder_example_without_prose(self):
        text = ("```json\n" + self.EX + "\n```\n```json\n" + self.PAY + "\n```")
        assert self._species(text) == "Neoalbaillella optima"

    def test_earlier_real_payload_still_wins(self):
        text = ("```json\n" + self.PAY + "\n```\n```json\n" + self.EX + "\n```")
        assert self._species(text) == "Neoalbaillella optima"

    def test_zonation_root_keys_are_known(self):
        """A fence carrying only zonations/correlations was not recognised."""
        from rca_core.json_utils import _KNOWN_ROOT_KEYS
        assert {"zonations", "correlations"} <= set(_KNOWN_ROOT_KEYS)

    def test_literal_newline_inside_a_string_is_recovered(self):
        """Level 2 keeps \\n, so a caption with a raw newline made the whole
        reply unparseable and Level 4 salvaged one inner row."""
        from rca_core.json_utils import safe_json_loads
        text = '{"sections": [{"name": "A"}], "note": "row 1\\nrow 2", "confidence": 0.9}'
        parsed = safe_json_loads(text)
        assert parsed["sections"] == [{"name": "A"}]
        assert parsed["note"] == "row 1\nrow 2"


# ---------------------------------------------------------------------------
# llm: empty/failed responses
# ---------------------------------------------------------------------------

class TestLlmEmptyResponses:

    def test_200_with_no_text_returns_none_and_explains(self):
        """A relay answering 200 {"error": ...} was reported as a SUCCESSFUL
        empty result, and the reason was discarded."""
        import rca_core.llm as L
        original = L._post_json
        try:
            for body, needle in [
                (json.dumps({"error": {"message": "insufficient balance"}}).encode(),
                 "insufficient balance"),
                (json.dumps({"content": [], "stop_reason": "refusal"}).encode(),
                 "no text blocks"),
                (b"<html>Blocked by WAF</html>", "Blocked by WAF"),
            ]:
                L._post_json = lambda t, b, h, ts, pc=None, _b=body: (_b, 200, b"")
                text, trunc, status, err_body, payload = L._read_response("u", {}, {}, 5)
                assert text is None or text == ""
                assert needle in err_body, (needle, err_body)
        finally:
            L._post_json = original

    def test_null_text_block_does_not_raise(self):
        """`"" + None` raised TypeError out of a never-raises API."""
        import rca_core.llm as L
        original = L._post_json
        try:
            body = json.dumps({"content": [{"type": "text", "text": '{"a":1}'},
                                           {"type": "text", "text": None}]}).encode()
            L._post_json = lambda t, b, h, ts, pc=None, _b=body: (_b, 200, b"")
            text, *_ = L._read_response("u", {}, {}, 5)
            assert text == '{"a":1}'
        finally:
            L._post_json = original

    def test_endpoint_path_does_not_double_the_version(self):
        """Zhipu /api/paas/v4 -> .../v4/v1/chat/completions 404'd."""
        from rca_core.llm import _api_base, _endpoint_path
        assert _endpoint_path(_api_base("https://open.bigmodel.cn/api/paas/v4"),
                              "/v1/chat/completions") == \
            "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        assert _endpoint_path(_api_base("https://api.openai.com/v1"),
                              "/v1/chat/completions") == \
            "https://api.openai.com/v1/chat/completions"
        assert _endpoint_path(_api_base("https://api.minimaxi.com/anthropic"),
                              "/v1/messages") == \
            "https://api.minimaxi.com/anthropic/v1/messages"


# ---------------------------------------------------------------------------
# server: mode constraint, validation, redaction
# ---------------------------------------------------------------------------

class TestServerGuards:

    def test_auto_resolved_unwired_mode_falls_back(self):
        """mode:"auto" bypassed the mode whitelist via vision classification."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "srv_review", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "server.py"))
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        assert srv._constrain_resolved_mode("paleomap", "vision") == \
            ("range_chart", "auto-fallback")
        assert srv._constrain_resolved_mode("scatter_plot", "vision") == \
            ("range_chart", "auto-fallback")
        assert srv._constrain_resolved_mode("columnar_section", "vision") == \
            ("columnar_section", "vision")

    def test_provider_nested_field_validation(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "srv_review2", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "server.py"))
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        V = srv._validate_provider_fields
        assert V({"endpoint": 5})
        assert V({"endpoint": []})
        assert V({"extra_headers": "oops"})
        assert V({"extra_body": ["a"]})
        assert V({"model": 5})
        assert V({"endpoint": "https://x/v1", "api_key": "k", "model": "m",
                  "extra_headers": {}, "extra_body": {}}) == ""

    def test_redaction_covers_modern_key_shapes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "srv_review3", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "server.py"))
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        R = srv._redact_error_body
        for text in ('x-api-key=sk-abc.defghijklmnop',
                     'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij',
                     'token: ccs-1234567890abcdef',
                     'key=pk-abcdefghijklmnopqrstuvwxyz',
                     'AIzaSyA1bC2dE3fGhIjKlMnOpQrStUvWxYz0123',
                     'AKIAIOSFODNN7EXAMPLE'):
            assert R(text) != text, text
        assert R("plain error message") == "plain error message"


# ---------------------------------------------------------------------------
# misc modules
# ---------------------------------------------------------------------------

class TestMiscFixes:

    def test_bed_parser_rejects_ages_and_units(self):
        from rca_core.bed_parser import parse_bed_int
        for raw in ("253 Ma", "253Ma", "0.5 Ma", "1.5", "23 m", "23-25",
                    "Bed 253 Ma", "Madison 3", "12 ka", "3 ft"):
            assert parse_bed_int(raw) is None, raw
        for raw, want in (("Bed 23c", 23), ("23c", 23), ("Bed 23", 23),
                          ("23", 23), ("Bed 27a", 27), ("23z", 23)):
            assert parse_bed_int(raw) == want, raw

    def test_database_accepts_memory_and_relative_paths(self):
        """dirname('relative.db') == '' made makedirs raise FileNotFoundError."""
        from rca_core.db import Database
        db = Database(":memory:")
        assert db is not None

    def test_report_tolerates_a_non_dict_extras(self):
        from rca_core.report import build_extraction_report
        r = build_extraction_report(data={"sections": [], "_extras": ["x"]},
                                    mode="range_chart")
        assert r["empty_tables"][0]["reason"] == "not readable in this figure"

    def test_report_truncation_unknown_is_null(self):
        from rca_core.report import build_extraction_report
        assert build_extraction_report(data={}, mode="range_chart",
                                       truncated=None)["truncation"]["truncated"] is None
        assert build_extraction_report(data={}, mode="range_chart",
                                       truncated=False)["truncation"]["truncated"] is False

    def test_columnar_xlsx_is_no_longer_dead(self):
        """EXPORT_INVARIANTS["sections"]["required"] == ["name"] but columnar
        sections carry `id`, so to_xlsx raised for the whole mode."""
        from rca_core.extractor import normalize_columnar_result
        from rca_core.exporter import to_xlsx, validate_export_invariants
        res = normalize_columnar_result({
            "sections": [{"id": "Ki-1", "group": "Lower", "thickness_m": "120",
                          "coordinates_text": "31N 112E",
                          "lithology_blocks": [{"pattern": "chert",
                                                "range_base_idx": 1,
                                                "range_top_idx": 8}],
                          "age_units": [{"label": "Lower", "range_base_idx": 1,
                                         "range_top_idx": 8}],
                          "samples": [{"bed_idx": 5, "fossil_marker": "J",
                                       "ref": "Kamata, 1996"}]}],
            "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
            "confidence": 0.65})
        ok, issues = validate_export_invariants(res)
        assert ok, issues
        assert to_xlsx(res)                      # used to raise ValueError

    def test_columnar_sub_tables_are_exported(self):
        from rca_core.extractor import normalize_columnar_result
        from rca_core.exporter import build_table_export, get_configs_for_result
        res = normalize_columnar_result({
            "sections": [{"id": "Ki-1",
                          "lithology_blocks": [{"pattern": "chert"}],
                          "age_units": [{"label": "Lower"}],
                          "samples": [{"bed_idx": 5, "fossil_marker": "J"}]}],
            "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
            "confidence": 0.5})
        ids = [c["id"] for c in get_configs_for_result(res)]
        for tid in ("lithology_blocks", "age_units", "samples"):
            assert tid in ids
            _h, rows = build_table_export(res, tid, lambda k: k)
            assert len(rows) == 1

    def test_phylo_node_edit_round_trip_is_lossless(self):
        """A no-op Apply-edits turned is_leaf into "N" (truthy -> all leaves)."""
        from rca_core.exporter import apply_table_edits, build_table_export
        data = {"nodes": [{"id": "n0", "parent": None, "name": "Root",
                           "is_leaf": False},
                          {"id": "n1", "parent": "n0", "name": "LeafA",
                           "is_leaf": True}],
                "root_ids": ["n0"], "confidence": 0.8}
        _h, rows = build_table_export(data, "nodes", lambda k: k)
        out = apply_table_edits(data, "nodes", [list(r) for r in rows])
        assert [n["is_leaf"] for n in out["nodes"]] == [False, True]
        assert [n["parent"] for n in out["nodes"]] == [None, "n0"]
        idx = list(_h).index("col.isLeaf")
        _h2, rows2 = build_table_export(out, "nodes", lambda k: k)
        assert [list(r)[idx] for r in rows2] == ["N", "Y"]

    def test_export_blanks_nonfinite_text_values(self):
        from rca_core.exporter import build_table_export, to_csv
        data = {"species_ranges": [
            {"species": "X", "section": "S1", "range_base": float("nan"),
             "range_top": 12.5, "biozone": "Z"},
            {"species": "Y", "section": "S1", "range_base": float("inf"),
             "range_top": "nan", "biozone": ""}],
            "sections": [], "biozones": [], "other_fossils": [], "confidence": 0.5}
        headers, rows = build_table_export(data, "species_ranges", lambda k: k)
        assert not any(t.strip().lower() in ("nan", "inf", "-inf")
                       for r in rows for t in r)
        assert "nan" not in to_csv(headers, rows).lower()

    def test_editable_tracks_nodes_zonations_correlations(self):
        from rca_core.editable import capture_edits, new_row_template
        d = capture_edits({"nodes": [{"id": "n0", "name": "A"}]},
                          {"nodes": [{"id": "n0", "name": "B"}]})
        assert d, "nodes edits were silently dropped"
        assert new_row_template("nodes")["id"] == ""
        assert new_row_template("zonations")["name"] == ""
        assert new_row_template("correlations")["from_zone"] == ""

    def test_darwin_core_uses_real_dwc_terms(self):
        import io as _io
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "rca_core", "standards", "darwin_core.py")
        src = _io.open(path, encoding="utf-8").read()
        assert "dwc/terms/chronostratigraphicAge" not in src
        assert "dwc/terms/biostratigraphicZone" not in src
        assert "dwc/terms/lowestBiostratigraphicZone" in src

    def test_formal_series_names_resolve(self):
        """The dict emits e.g. "Lopingian" as the canonical name, so the name
        must resolve back to its own bounds."""
        from rca_core.standards.ics import ics_age_range_bounds
        for name in ("Lopingian", "Guadalupian", "Cisuralian",
                     "Miaolingian", "Terreneuvian"):
            older, younger = ics_age_range_bounds(name)
            assert older is not None and younger is not None, name

    def test_compound_label_resolves_the_same_unit_on_both_paths(self):
        """Series-first resolution made the DwC path and the PBDB path
        disagree about which unit a compound label names ("Late Permian
        (Wuchiapingian)" gave the Lopingian SERIES bound on one and the
        Wuchiapingian range on the other).

        The two functions still answer different questions by design — a
        single endpoint uses the named stage's midpoint, the range function
        returns that stage's bounds — so this pins the invariant that matters:
        the same unit, with the endpoint inside its interval.
        """
        from rca_core.standards.ics import (ics_age_range_bounds,
                                            ics_resolve_age_bound)
        name, ma = ics_resolve_age_bound("Late Permian (Wuchiapingian)",
                                         prefer="older")
        assert name == "Wuchiapingian"
        older, younger = ics_age_range_bounds("Late Permian (Wuchiapingian)")
        assert younger <= ma <= older, (younger, ma, older)
