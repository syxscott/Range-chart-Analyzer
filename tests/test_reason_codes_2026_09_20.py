"""BORROW-2026-09-20 — the extraction CONTRACT tests.

Companion to ``rca_core/reason_codes.py`` (new module), the extractor merge /
quality / report wiring the previous pass landed, and the prompt clauses that
make the contract visible to the model.  Nothing here touches the network: the
end-to-end cases drive ``extract_*`` through a patched ``call_llm_api`` exactly
like ``test_review_2026_09_20_extraction_core_tail.py`` drives the cache, so the
whole file runs offline.

What is locked, and why:

1. ``reason_codes`` public surface — the 12-code closed vocabulary, the
   three-state answer, the merge precedence (``extracted`` > ``uncertain`` >
   ``not_drawn``), and the coverage ledger that separates an honest
   ``not_drawn`` from a silent omission (the whole scientific point of the
   borrow: a run that reports 40 dashes must not score worse than one that
   drops those rows).
2. The PARSE side: ``response_kind`` / ``reason_codes`` digestion
   (:func:`extractor.apply_coverage_contract`), the strict 0-999 integer
   (:func:`extractor.normalize_pos_0_999`), the optional figure-level
   calibration, and the geometry guard rails — an out-of-scale or
   contradictory position is DROPPED and the row is degraded with
   ``low_confidence``, it never rewrites the transcribed value (the anti
   self-echo rule).
3. The PROMPT side: the two clauses are present per mode, the reason-code list
   is GENERATED from ``reason_codes.py`` (no second truth), and every
   ``*pos_0_999`` name the prompts ask for is a name the parser digests — in
   both directions.
4. Merge / quality / report smoke: the contract survives a multi-run merge and
   reaches the audit output.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import rca_core.reason_codes as RC  # noqa: E402
from rca_core import extractor as E  # noqa: E402
from rca_core.aggregate import merge_results  # noqa: E402
from rca_core.prompt import (  # noqa: E402
    ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
    CHART_CLASSIFY_SYSTEM_PROMPT,
    CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT,
    COLUMNAR_SECTION_SYSTEM_PROMPT,
    PALEOMAP_SYSTEM_PROMPT,
    PHYLOGENETIC_TREE_SYSTEM_PROMPT,
    POS_FIELDS,
    PROMPT_VERSION,
    RANGE_CHART_SYSTEM_PROMPT,
    REASON_CODE_SLUG_LIST,
    RESPONSE_KIND_LIST,
    SCATTER_PLOT_SYSTEM_PROMPT,
    ZONATION_CHART_SYSTEM_PROMPT,
)
from rca_core.reason_codes import (  # noqa: E402
    LOW_CONFIDENCE_CODE,
    REASON_CODES,
    REASON_CODE_SLUGS,
    RESPONSE_EXTRACTED,
    RESPONSE_KINDS,
    RESPONSE_NOT_DRAWN,
    RESPONSE_UNCERTAIN,
    SILENT_MISSING,
    code_summary,
    coverage_ledger,
    coverage_state,
    has_value,
    is_answered,
    is_valid_code,
    merge_reason_codes,
    merge_response_kinds,
    normalize_code,
    normalize_codes,
    normalize_response_kind,
    render_codes,
    row_column,
    row_label,
    row_stratum,
)
from rca_core.quality import coverage_for, score_range_chart  # noqa: E402
from rca_core.report import build_extraction_report  # noqa: E402


# ---------------------------------------------------------------------------
# 1. reason_codes: the catalogue and the normalisers
# ---------------------------------------------------------------------------

class TestCodeCatalogue:
    def test_twelve_codes_and_unique_slugs(self):
        assert len(REASON_CODES) == 12
        slugs = [c.slug for c in REASON_CODES]
        assert len(set(slugs)) == len(slugs)
        assert set(slugs) == set(REASON_CODE_SLUGS)

    def test_families_group_the_catalogue(self):
        # The UI colours by family, so a code may not silently change family.
        families = {c.slug: c.family for c in REASON_CODES}
        assert families["not_drawn"] == "coverage"
        assert families["out_of_scope"] == "coverage"
        assert families["crosses_top"] == "geometry"
        assert families["legend_only"] == "source"
        assert families[LOW_CONFIDENCE_CODE] == "quality"

    @pytest.mark.parametrize("slug", sorted(REASON_CODE_SLUGS))
    def test_every_code_has_a_gloss(self, slug):
        assert code_summary(slug) != slug
        assert code_summary(slug).strip()

    def test_low_confidence_is_in_the_catalogue(self):
        # Documented as "never a model-facing claim", but it IS a legal slug:
        # the parser writes it itself. Locking that so a future cleanup does
        # not make the parser's own code unnormalisable.
        assert LOW_CONFIDENCE_CODE in REASON_CODE_SLUGS
        assert normalize_code(LOW_CONFIDENCE_CODE) == LOW_CONFIDENCE_CODE


class TestNormalizeCode:
    @pytest.mark.parametrize("value", ["not_drawn", " NOT_DRAWN ", "low_confidence"])
    def test_slugs_are_case_and_space_tolerant(self, value):
        assert normalize_code(value) == value.strip().lower()

    @pytest.mark.parametrize("value,expected", [
        ("dash", "not_drawn"),
        ("blank", "not_drawn"),
        ("Not-Drawn", "not_drawn"),
        ("off_top", "crosses_top"),
        ("unlabelled", "no_label"),
        ("illegible", "uncertain"),
        ("interpolated", "inferred"),
        ("outside_scope", "out_of_scope"),
    ])
    def test_known_aliases_fold(self, value, expected):
        assert normalize_code(value) == expected

    @pytest.mark.parametrize("value", [
        "definitely_not_a_real_code", "", "   ", "not_drawn_but_wrong",
        None, 7, ["not_drawn"],
    ])
    def test_unknown_input_is_dropped_not_guessed(self, value):
        assert normalize_code(value) is None

    def test_is_valid_code_matches_the_catalogue_only(self):
        assert is_valid_code("truncated")
        assert is_valid_code("Truncated")
        assert not is_valid_code("dash")  # an alias is not a canonical slug
        assert not is_valid_code(None)

    def test_reason_code_object_normalizes_to_its_slug(self):
        assert normalize_code(RC.REASON_CODES[0]) == RC.REASON_CODES[0].slug

    def test_code_summary_of_unknown_is_the_input_text(self):
        assert code_summary("nonsense") == "nonsense"
        assert code_summary(None) == ""


class TestNormalizeCodes:
    @pytest.mark.parametrize("value,expected", [
        (None, []),
        ("", []),
        ("not_drawn", ["not_drawn"]),
        ("not_drawn, obscured", ["not_drawn", "obscured"]),
        ("not_drawn;obscured", ["not_drawn", "obscured"]),
        (["dash", "not_drawn", "unclear"], ["not_drawn", "uncertain"]),
        ({"crosses_top": True, "junk": True}, ["crosses_top"]),
        ("truncated", ["truncated"]),
    ])
    def test_every_shape_the_model_may_emit(self, value, expected):
        assert normalize_codes(value) == expected

    def test_first_seen_order_survives_and_duplicates_collapse(self):
        assert normalize_codes(["uncertain", "not_drawn", "uncertain", "dash"]) \
            == ["uncertain", "not_drawn"]

    def test_scalar_garbage_yields_empty(self):
        assert normalize_codes(42) == []


class TestResponseKind:
    @pytest.mark.parametrize("value,expected", [
        ("extracted", RESPONSE_EXTRACTED),
        ("Drawn", RESPONSE_EXTRACTED),
        ("not_drawn", RESPONSE_NOT_DRAWN),
        ("not drawn", RESPONSE_NOT_DRAWN),
        ("dash", RESPONSE_NOT_DRAWN),
        ("unclear", RESPONSE_UNCERTAIN),
        ("nonsense", None),
        ("", None),
        (None, None),
        (5, None),
    ])
    def test_normalize(self, value, expected):
        assert normalize_response_kind(value) == expected

    def test_precedence_is_extracted_over_uncertain_over_not_drawn(self):
        assert RC.RESPONSE_KIND_PRECEDENCE[RESPONSE_EXTRACTED] \
            > RC.RESPONSE_KIND_PRECEDENCE[RESPONSE_UNCERTAIN] \
            > RC.RESPONSE_KIND_PRECEDENCE[RESPONSE_NOT_DRAWN]
        assert set(RC.RESPONSE_KIND_PRECEDENCE) == set(RESPONSE_KINDS)


class TestCoverageStateAndAnswered:
    def test_explicit_kind_beats_the_values(self):
        row = {"species": "A", "range_top": "9", "response_kind": "uncertain"}
        assert coverage_state(row) == RESPONSE_UNCERTAIN
        assert is_answered(row)

    def test_legacy_row_with_a_value_counts_as_extracted(self):
        row = {"species": "A", "range_top": "9", "range_base": "7"}
        assert coverage_state(row) == RESPONSE_EXTRACTED
        assert is_answered(row)
        assert has_value(row)

    def test_row_with_nothing_is_a_silent_gap(self):
        row = {"species": "A", "section": "S1"}
        assert coverage_state(row) == SILENT_MISSING
        assert not is_answered(row)
        assert not has_value(row)

    def test_not_drawn_row_is_an_answer_without_values(self):
        row = {"species": "A", "section": "S1", "response_kind": "not_drawn",
               "reason_codes": ["not_drawn"]}
        assert coverage_state(row) == RESPONSE_NOT_DRAWN
        assert is_answered(row)

    def test_a_real_zero_is_a_value_not_an_absence(self):
        assert has_value({"abundance": "0"})
        assert has_value({"values": ["x"]})
        assert not has_value({"values": []})
        assert not has_value({"note": "only prose"})
        assert not has_value("not a dict")

    def test_non_dict_row_is_silent_missing(self):
        assert coverage_state(None) == SILENT_MISSING
        assert coverage_state(["x"]) == SILENT_MISSING


class TestMergeRules:
    def test_extracted_outranks_any_number_of_not_drawn(self):
        kind, divergent = merge_response_kinds(
            ["not_drawn", "not_drawn", "extracted"])
        assert kind == RESPONSE_EXTRACTED
        assert divergent is True

    def test_uncertain_beats_not_drawn(self):
        assert merge_response_kinds(["not_drawn", "uncertain"])[0] \
            == RESPONSE_UNCERTAIN

    def test_unanimous_vote_is_not_divergent(self):
        assert merge_response_kinds(["not_drawn", "dash"]) == ("not_drawn", False)

    def test_silence_merges_to_none(self):
        assert merge_response_kinds([]) == (None, False)
        assert merge_response_kinds([None, "junk"]) == (None, False)

    def test_reason_codes_union_in_first_seen_order(self):
        assert merge_reason_codes([["obscured"], "dash,bogus", ["obscured"]]) \
            == ["obscured", "not_drawn"]


# ---------------------------------------------------------------------------
# 2. reason_codes: the ledger and the rollup
# ---------------------------------------------------------------------------

class TestCoverageLedger:
    ROWS = [
        # A is drawn in S1, and honestly declared absent in S2.
        {"species": "A", "section": "S1", "range_top": "9", "range_base": "7",
         "response_kind": "extracted"},
        {"species": "A", "section": "S2", "response_kind": "not_drawn",
         "reason_codes": ["not_drawn"]},
        # B is only answered in S1 ...
        {"species": "B", "section": "S1", "range_top": "4",
         "response_kind": "uncertain", "reason_codes": ["unclear", "obscured"]},
        # ... and B/S2 has no row at all: the grid must expose it as a gap.
    ]

    def test_grid_counts_the_answered_and_the_missing(self):
        led = coverage_ledger(self.ROWS)
        t = led["totals"]
        assert t["cells"] == 4, t
        assert t["extracted"] == 1
        assert t["not_drawn"] == 1
        assert t["uncertain"] == 1
        assert t["silent_missing"] == 1
        assert t["answered"] == 3
        assert t["honest_coverage"] == 0.75
        assert t["strict_coverage"] == 0.25

    def test_dashes_do_not_depress_coverage(self):
        # The regression the borrow exists for: 40 honest "not drawn" rows must
        # score as answered, not as missing.
        rows = [{"species": f"S{i}", "section": "X", "range_top": "1",
                 "range_base": "2", "response_kind": "extracted"}
                for i in range(10)] + [
                {"species": f"N{i}", "section": "X",
                 "response_kind": "not_drawn"} for i in range(40)]
        honest = coverage_ledger(rows)["totals"]["honest_coverage"]
        assert honest == 1.0

    def test_omitting_rows_scores_worse_than_admitting_them(self):
        honest = coverage_ledger(
            [{"species": "A", "section": "X", "response_kind": "not_drawn"}]
        )["totals"]["honest_coverage"]
        silent = coverage_ledger([])["totals"]["honest_coverage"]
        assert honest == 1.0 and silent == 0.0

    def test_per_column_and_per_stratum_breakdowns(self):
        led = coverage_ledger(self.ROWS)
        by_col = {c["column"]: c for c in led["columns"]}
        assert by_col["A"]["silent_missing"] == 0
        assert by_col["B"]["silent_missing"] == 1
        assert by_col["B"]["cells"] == 2
        assert {s["stratum"] for s in led["strata"]} == {"S1", "S2"}

    def test_reason_code_counts_and_explicit_responses(self):
        led = coverage_ledger(self.ROWS)
        assert led["reason_code_counts"] == {"not_drawn": 1, "obscured": 1,
                                             "uncertain": 1}
        assert led["explicit_responses"] == 3
        assert led["row_count"] == 3
        assert led["unattributed_rows"] == 0
        assert led["grid_used"] is True

    def test_strongest_answer_wins_a_duplicated_cell(self):
        rows = [{"species": "A", "section": "S1", "response_kind": "not_drawn"},
                {"species": "A", "section": "S1", "range_top": "9",
                 "response_kind": "extracted"}]
        led = coverage_ledger(rows)
        assert led["totals"]["extracted"] == 1
        assert led["totals"]["not_drawn"] == 0

    def test_single_section_chart_does_not_invent_gaps(self):
        # No stratum field anywhere -> the stratum set is {""} and the grid
        # degenerates to the emitted rows.
        rows = [{"species": "A", "range_top": "9"},
                {"species": "B", "response_kind": "not_drawn"}]
        led = coverage_ledger(rows)
        assert led["totals"]["cells"] == 2
        assert led["totals"]["silent_missing"] == 0
        assert led["strata"][0]["stratum"] == ""

    def test_unattributable_rows_are_counted_not_placed(self):
        rows = [{"section": "S1", "response_kind": "not_drawn"}]
        led = coverage_ledger(rows)
        assert led["unattributed_rows"] == 1
        assert led["totals"]["cells"] == 0
        assert led["totals"]["honest_coverage"] == 0.0

    def test_custom_column_and_stratum_keys(self):
        rows = [{"taxon": "Pinus", "level": "120 cm", "abundance": "35"},
                {"taxon": "Pinus", "level": "200 cm",
                 "response_kind": "not_drawn"}]
        led = coverage_ledger(rows, column_keys=("taxon",),
                             stratum_keys=("level",))
        assert led["totals"]["cells"] == 2
        assert led["totals"]["strict_coverage"] == 0.5

    def test_cross_product_can_be_switched_off(self):
        led = coverage_ledger(self.ROWS, cross_product=False)
        assert led["grid_used"] is False
        assert led["totals"]["cells"] == 3  # emitted rows only
        assert led["totals"]["silent_missing"] == 0

    def test_oversized_grid_is_skipped(self, monkeypatch):
        monkeypatch.setattr(RC, "_MAX_LEDGER_CELLS", 2)
        led = coverage_ledger(self.ROWS)
        assert led["grid_used"] is False
        # The emitted-cell statistics stay correct on the fallback path too.
        assert led["totals"]["answered"] == 3

    def test_malformed_input_never_raises(self):
        assert coverage_ledger(None)["totals"]["cells"] == 0
        assert coverage_ledger(["x", 5, {"species": "A"}])["row_count"] == 1

    def test_row_identity_helpers(self):
        row = {"species": "A", "section": "S1"}
        assert row_column(row) == "A"
        assert row_stratum(row) == "S1"
        assert row_column({"name": "n"}) == "n"
        # `level` is a STRATUM key, not a column key, and numbers stringify.
        assert row_stratum({"level": 3}) == "3"
        assert row_column({"level": 3}) == ""
        assert row_column("nope") == ""
        assert row_label({"section": "S1"}) == "S1"
        assert row_label({"species": "A"}, ("species",)) == "A"


class TestReasonCodeRollup:
    ROWS = [
        {"species": "A", "range_top": "9", "response_kind": "extracted",
         "geometry": {"points": {"range_top_pos_0_999": {"pos": 1}},
                      "calibrated": True}},
        {"species": "B", "response_kind": "not_drawn",
         "reason_codes": "not_drawn, bogus"},
        {"species": "C"},  # never answered under the contract: not listed
    ]

    def test_only_contracted_rows_are_listed_but_all_are_counted(self):
        roll = RC.reason_code_rollup(self.ROWS)
        assert roll["contracted_rows"] == 2
        assert roll["by_kind"] == {"extracted": 1, "not_drawn": 1}
        # "bogus" is dropped, not guessed at: the rollup only ever counts the
        # closed vocabulary.
        assert roll["by_code"] == {"not_drawn": 1}
        assert [e["row"] for e in roll["entries"]] == ["A", "B"]
        assert roll["entries"][0]["geometry"] == {"points": 1, "calibrated": True}
        assert roll["entries"][1]["code_summaries"][0] == \
            RC.code_summary("not_drawn")

    def test_bogus_codes_are_dropped_from_the_evidence_chain(self):
        roll = RC.reason_code_rollup(
            [{"species": "B", "reason_codes": ["bogus"]}])
        assert roll["contracted_rows"] == 0
        assert roll["entries"] == []

    def test_legacy_single_reason_code_key_is_still_read(self):
        roll = RC.reason_code_rollup([{"species": "B", "reason_code": "dash"}])
        assert roll["by_code"] == {"not_drawn": 1}

    def test_entry_limit(self):
        rows = [{"species": f"S{i}", "response_kind": "not_drawn"}
                for i in range(10)]
        assert len(RC.reason_code_rollup(rows, limit=3)["entries"]) == 3
        assert RC.reason_code_rollup(rows, limit=3)["contracted_rows"] == 10

    def test_render_codes_is_empty_for_nothing_to_say(self):
        assert render_codes(["not_drawn", "obscured"]) == "not_drawn, obscured"
        assert render_codes(None) == ""
        assert render_codes("junk") == ""


# ---------------------------------------------------------------------------
# 3. extractor: the three-state answer
# ---------------------------------------------------------------------------

class TestApplyCoverageContract:
    def test_writes_only_what_the_model_answered(self):
        row: dict[str, Any] = {"species": "A", "range_top": "9"}
        E.apply_coverage_contract({"confidence": 0.9}, row)
        assert "response_kind" not in row and "reason_codes" not in row

    def test_happy_path(self):
        row = {"species": "A", "range_top": "9"}
        E.apply_coverage_contract(
            {"response_kind": "extracted", "reason_codes": ["crosses_top"]}, row)
        assert row["response_kind"] == "extracted"
        assert row["reason_codes"] == ["crosses_top"]

    def test_not_drawn_claim_with_a_value_is_read_back_as_extracted(self):
        # The contradiction rule, parser side: "not drawn" plus a readable
        # boundary means it WAS drawn; keep the value, flag the lie.
        row = {"species": "A", "range_top": "9"}
        E.apply_coverage_contract({"response_kind": "not_drawn"}, row)
        assert row["response_kind"] == RESPONSE_EXTRACTED
        assert row["_warning"] == "response_kind_conflict"

    def test_empty_row_may_stay_not_drawn(self):
        row = {"species": "A", "section": "S1", "range_top": "", "note": "x"}
        E.apply_coverage_contract({"response_kind": "not_drawn"}, row)
        assert row["response_kind"] == RESPONSE_NOT_DRAWN
        assert "_warning" not in row

    def test_unknown_kind_is_never_written(self):
        row = {"species": "A"}
        E.apply_coverage_contract({"response_kind": "probably_fine"}, row)
        assert "response_kind" not in row

    def test_extra_codes_append_without_duplicates(self):
        row = {"species": "A", "range_top": "9"}
        E.apply_coverage_contract(
            {"reason_codes": ["low_confidence"]}, row,
            extra_codes=["low_confidence", "obscured"])
        assert row["reason_codes"] == ["low_confidence", "obscured"]

    def test_legacy_singular_reason_code_key_is_accepted(self):
        row = {"species": "A", "range_top": "9"}
        E.apply_coverage_contract({"reason_code": "off_top"}, row)
        assert row["reason_codes"] == ["crosses_top"]

    def test_contract_source_keys_cover_exactly_the_three_inputs(self):
        # The prompt may say it in any of these three shapes; adding a fourth
        # without adding it here duplicates it into _extras.
        assert set(E._CONTRACT_SOURCE_KEYS) == {
            "response_kind", "reason_codes", "reason_code"}


# ---------------------------------------------------------------------------
# 4. extractor: the 0-999 positions and their guard rails
# ---------------------------------------------------------------------------

class TestNormalizePos:
    @pytest.mark.parametrize("value,expected", [
        (0, 0), (999, 999), (712, 712), ("712", 712), (500.0, 500),
        (" 640 ", 640),
    ])
    def test_integers_in_scale_survive(self, value, expected):
        assert E.normalize_pos_0_999(value) == expected

    @pytest.mark.parametrize("value", [
        None, True, False, 712.5, 1000, -1, "1000", "-3", "", "abc", {}, [],
        1e400,
    ])
    def test_anything_else_is_refused_not_clamped(self, value):
        assert E.normalize_pos_0_999(value) is None

    def test_scale_constants_match_the_prompt_wording(self):
        assert (E.POS_MIN, E.POS_MAX) == (0, 999)
        assert E.POS_SCALE_KEY == "pos_0_999"
        assert E.POS_SUFFIX == "_pos_0_999"
        assert E.GEOMETRY_SCALE == E.POS_SCALE_KEY


class TestAxisCalibration:
    def test_shapes_accepted(self):
        assert E._axis_domain({"at_0": 1, "at_999": 24, "unit": "bed"}) == \
            {"at_0": 1.0, "at_999": 24.0, "unit": "bed"}
        assert E._axis_domain({"bottom": 0, "top": 10})["at_999"] == 10.0
        assert E._axis_domain([1, 24]) == {"at_0": 1.0, "at_999": 24.0,
                                           "unit": ""}
        assert E._axis_domain({"oldest": 5, "youngest": 1})["at_0"] == 5.0

    @pytest.mark.parametrize("raw", [
        None, "1-24", {"at_0": 1}, {"at_999": "abc"}, {}, [1, 2, 3], 5,
    ])
    def test_incomplete_domains_are_dropped(self, raw):
        assert E._axis_domain(raw) is None

    def test_axis_domains_collected_from_every_hiding_place(self):
        for payload in (
            {"axis_calibration": {"vertical": {"at_0": 1, "at_999": 24}}},
            {"metadata": {"axis_calibration": {"vertical": [1, 24]}}},
            {"_extras": {"axis_calibration": {"vertical": [1, 24]}}},
        ):
            assert E.axis_domains_from(payload) == {
                "vertical": {"at_0": 1.0, "at_999": 24.0, "unit": ""}}

    def test_first_winning_container_and_unknown_axis_names_kept(self):
        # Root wins over metadata/_extras, and an axis name the parser has
        # never heard of is kept verbatim (a future "thickness" scale needs
        # no code change here).
        got = E.axis_domains_from({
            "axis_calibration": {"vertical": {"at_0": 1, "at_999": 24},
                                 "thickness": {"at_0": 0, "at_999": 100,
                                               "unit": "m"}},
            "metadata": {"axis_calibration": {"vertical": [0, 100]}},
        })
        assert got["vertical"] == {"at_0": 1.0, "at_999": 24.0, "unit": ""}
        assert got["thickness"] == {"at_0": 0.0, "at_999": 100.0, "unit": "m"}
        # Text ends ("Bed 1") are not numbers: the axis is dropped entirely
        # rather than half-calibrated.
        assert E.axis_domains_from({"axis_calibration": {
            "vertical": {"at_0": "Bed 1", "at_999": "Bed 24"}}}) == {}
        assert E.axis_domains_from("nope") == {}

    def test_pos_to_axis_value_is_linear_and_direction_agnostic(self):
        dom = {"at_0": 1.0, "at_999": 24.0}
        assert E.pos_to_axis_value(0, dom) == 1.0
        assert round(E.pos_to_axis_value(999, dom), 6) == 24.0
        deep = {"at_0": 520.0, "at_999": 0.0}  # depth below the surface
        assert E.pos_to_axis_value(0, deep) == 520.0
        assert E.pos_to_axis_value(768, deep) == pytest.approx(120.7, abs=0.6)
        assert E.pos_to_axis_value(5, None) is None


class TestGeometryAxisMapping:
    def test_explicit_and_vertical_field_names(self):
        assert E._geometry_axis_for("x_pos_0_999", {}) == "x"
        assert E._geometry_axis_for("y_pos_0_999", {}) == "y"
        for field in E._GEOMETRY_VERTICAL_FIELDS:
            assert E._geometry_axis_for(field, None) == "vertical"

    def test_unknown_prefix_needs_a_matching_calibration(self):
        # This is the deliberate non-catch-all: a bar-length read must not
        # borrow the stratigraphic axis and look like a real number.
        assert E._geometry_axis_for("abundance_pos_0_999", None) is None
        assert E._geometry_axis_for("abundance_pos_0_999",
                                    {"abundance": {"at_0": 0, "at_999": 100}}) \
            == "abundance"
        assert E._geometry_axis_for("totally_unknown", {}) is None


class TestGeometryFromRow:
    SRC_ROW = {"species": "A", "range_top": "Bed 9", "range_base": "Bed 7",
               "range_top_idx": 9, "range_base_idx": 7}
    AXES = {"vertical": {"at_0": 1.0, "at_999": 24.0, "unit": "bed"}}

    def test_no_positions_no_geometry(self):
        geom, rejected, consumed = E.geometry_from_row(dict(self.SRC_ROW),
                                                       self.AXES)
        assert geom is None and rejected is False and consumed == []

    def test_uncalibrated_position_is_kept_as_evidence_only(self):
        src = dict(self.SRC_ROW, range_top_pos_0_999=347)
        geom, rejected, consumed = E.geometry_from_row(src, None,
                                                       row=self.SRC_ROW)
        assert rejected is False and consumed == ["range_top_pos_0_999"]
        assert geom["calibrated"] is False
        assert "axes" not in geom, "nothing was calibrated, so nothing to echo"
        point = geom["points"]["range_top_pos_0_999"]
        # The field name itself already says which axis it belongs to.
        assert point == {"pos": 347, "axis": "vertical"}
        assert "value" not in point, "no calibration must invent a number"

    def test_position_on_an_uncalibrated_unknown_axis_is_tagged_as_such(self):
        # A prefix the parser cannot resolve has no axis to claim; the raw
        # integer is still kept, but plainly labelled.
        geom, rejected, _ = E.geometry_from_row(
            {"bar_length_pos_0_999": 400}, None, row={})
        assert rejected is False
        assert geom["points"]["bar_length_pos_0_999"] == {
            "pos": 400, "axis": "uncalibrated"}
        assert geom["calibrated"] is False

    def test_calibrated_position_becomes_an_axis_value(self):
        src = dict(self.SRC_ROW, range_top_pos_0_999=347,
                   range_base_pos_0_999=261)
        geom, rejected, consumed = E.geometry_from_row(src, self.AXES,
                                                       row=self.SRC_ROW)
        assert rejected is False and geom["calibrated"] is True
        assert geom["scale"] == "pos_0_999" and geom["version"] == 1
        assert geom["points"]["range_top_pos_0_999"]["value"] == \
            pytest.approx(9.0, abs=0.05)
        assert geom["points"]["range_top_pos_0_999"]["unit"] == "bed"
        assert geom["axes"]["vertical"] == self.AXES["vertical"]
        assert sorted(consumed) == ["range_base_pos_0_999",
                                    "range_top_pos_0_999"]

    def test_guard_out_of_scale_integer_drops_the_point_and_degrades_row(self):
        src = dict(self.SRC_ROW, range_top_pos_0_999=1000,
                   range_base_pos_0_999=261)
        geom, rejected, _consumed = E.geometry_from_row(src, self.AXES,
                                                        row=self.SRC_ROW)
        assert rejected is True
        assert "range_top_pos_0_999" not in geom["points"]
        assert "range_base_pos_0_999" in geom["points"], \
            "one bad point must not take the good one down with it"

    def test_guard_non_integer_is_rejected_not_rounded(self):
        geom, rejected, _ = E.geometry_from_row(
            {"range_top_pos_0_999": 347.5}, self.AXES, row={})
        assert geom is None and rejected is True

    def test_guard_contradiction_beyond_15_percent_of_the_span(self):
        # pos 900 on a 1..24 bed axis converts to ~21.7 bed while the row says
        # bed 9: 12.7 apart on a span of 23 = 55% > GEOMETRY_TOLERANCE.
        assert E.GEOMETRY_TOLERANCE == 0.15
        src = dict(self.SRC_ROW, range_top_pos_0_999=900)
        geom, rejected, _ = E.geometry_from_row(src, self.AXES, row=self.SRC_ROW)
        assert rejected is True and geom is None

    def test_contradiction_within_tolerance_is_kept(self):
        # pos 480 -> 12.05 bed against a transcribed bed 9: 3.05 apart on a
        # span of 23 = 13.3% of the span, i.e. inside GEOMETRY_TOLERANCE, so
        # the read is kept as corroborating evidence.
        src = dict(self.SRC_ROW, range_top_pos_0_999=480)
        geom, rejected, _ = E.geometry_from_row(src, self.AXES, row=self.SRC_ROW)
        assert rejected is False
        assert geom["points"]["range_top_pos_0_999"]["value"] == \
            pytest.approx(12.05, abs=0.05)

    def test_tolerance_is_a_hard_edge_not_a_nudge(self):
        # The very next integer past 15% of the span is discarded instead:
        # 500 -> 12.51 bed, 3.51 from bed 9 = 15.3% of the 23-bed span.
        src = dict(self.SRC_ROW, range_top_pos_0_999=500)
        geom, rejected, _ = E.geometry_from_row(src, self.AXES, row=self.SRC_ROW)
        assert rejected is True and geom is None

    def test_unit_mismatch_between_label_and_axis_cannot_false_trigger(self):
        # Bed indices transcribed as "9" against a METRE axis running 100-500:
        # the semantic value lies OUTSIDE the calibrated domain, so the
        # contradiction test is skipped rather than throwing away a perfectly
        # good pixel read.
        axes = {"vertical": {"at_0": 100.0, "at_999": 500.0, "unit": "m"}}
        src = {"depth_m": "9", "depth_pos_0_999": 250}
        geom, rejected, _ = E.geometry_from_row(src, axes, row=src)
        assert rejected is False
        assert geom["points"]["depth_pos_0_999"]["value"] == \
            pytest.approx(200.1, abs=0.2)
        # Same field, same scale, but now the label IS on the axis domain
        # (4.5 m inside 0-10 m) and the pixel read disagrees by 45% of the
        # span: here the contradiction check does fire.
        axes_m = {"vertical": {"at_0": 0.0, "at_999": 10.0, "unit": "m"}}
        src_m = {"depth_m": "4.5", "depth_pos_0_999": 900}
        geom_m, rejected_m, _ = E.geometry_from_row(src_m, axes_m, row=src_m)
        assert rejected_m is True and geom_m is None

    def test_geometry_can_never_rewrite_the_semantic_value(self):
        # The anti self-echo rule, stated as an assertion: attach_row_contract
        # adds keys, it does not touch the transcribed fields.
        src = dict(self.SRC_ROW, range_top_pos_0_999=347, response_kind="x")
        row = dict(self.SRC_ROW)
        before = dict(row)
        E.attach_row_contract(src, row, axes=self.AXES)
        assert {k: row[k] for k in before} == before


class TestAttachRowContractEndToEnd:
    def test_species_row_carries_geometry_and_codes_without_extras(self):
        data = E.normalize_result({
            "sections": [{"name": "S1"}],
            "species_ranges": [{
                "species": "A. alpha", "section": "S1",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "range_top_idx": 9, "range_base_idx": 7,
                "range_top_pos_0_999": 347, "range_base_pos_0_999": 1000,
                "response_kind": "extracted",
                "reason_codes": ["crosses_top"],
            }],
            "confidence": 0.8,
            "axis_calibration": {"vertical": {"at_0": 1, "at_999": 24,
                                              "unit": "bed"}},
        })
        row = data["species_ranges"][0]
        assert row["geometry"]["points"]["range_top_pos_0_999"]["value"] == \
            pytest.approx(9.0, abs=0.05)
        assert "range_base_pos_0_999" not in row["geometry"]["points"]
        assert row["reason_codes"] == ["crosses_top", LOW_CONFIDENCE_CODE]
        assert row["response_kind"] == "extracted"
        # Consumed keys must not reappear under _extras (double-write).
        assert "_extras" not in row
        assert data["axis_calibration"]["vertical"]["at_999"] == 24.0

    def test_a_row_that_answered_nothing_keeps_the_legacy_key_set(self):
        data = E.normalize_result({
            "species_ranges": [{"species": "A", "section": "S1",
                                "range_top": "9", "range_base": "7"}],
        })
        row = data["species_ranges"][0]
        for key in ("geometry", "response_kind", "reason_codes", "_extras"):
            assert key not in row, key
        assert "axis_calibration" not in data

    def test_abundance_mode_digests_the_bare_pos_field(self):
        data = E.normalize_abundance_result({
            "abundances": [{"taxon": "Pinus", "site": "SG06", "level": "120 cm",
                            "depth": "120", "abundance": "35",
                            "pos_0_999": 768, "response_kind": "extracted"}],
            "axis_calibration": {"vertical": {"at_0": 520, "at_999": 0,
                                              "unit": "cm"}},
        })
        row = data["abundances"][0]
        assert row["response_kind"] == "extracted"
        point = row["geometry"]["points"]["pos_0_999"]
        assert point["axis"] == "vertical"
        assert point["value"] == pytest.approx(120.7, abs=0.6)
        assert "_extras" not in row

    def test_chemical_mode_positions_interval_boundaries(self):
        data = E.normalize_chemical_stratigraphy_result({
            "data_points": [{"sample_id": "Z-1", "depth_m": "12.5",
                             "values": {"d13C": "-1.2"}, "pos_0_999": 583}],
            "intervals": [{"name": "Lower", "top_depth_m": "20.0",
                           "base_depth_m": "30.0", "top_pos_0_999": 333,
                           "base_pos_0_999": 0}],
            "axis_calibration": {"vertical": {"at_0": 30.0, "at_999": 0.0,
                                              "unit": "m"}},
        })
        dp = data["data_points"][0]["geometry"]["points"]["pos_0_999"]
        assert dp["value"] == pytest.approx(12.5, abs=0.3)
        iv = data["intervals"][0]["geometry"]["points"]
        # The axis is inverted on purpose (at_0 = 30 m at the BOTTOM of the
        # frame, at_999 = 0 at the top), so pos 0 is the 30 m base.
        assert iv["top_pos_0_999"]["value"] == pytest.approx(20.0, abs=0.2)
        assert iv["base_pos_0_999"]["value"] == pytest.approx(30.0, abs=0.05)

    def test_scatter_mode_uses_its_two_axes(self):
        data = E.normalize_scatter_plot_result({
            "points": [{"x": "-2.34", "y": "1.56", "label": "A-12",
                        "x_pos_0_999": 207, "y_pos_0_999": 759}],
            "axis_calibration": {
                "x": {"at_0": -4.0, "at_999": 4.0, "unit": "PC1"},
                "y": {"at_0": -3.0, "at_999": 3.0, "unit": "PC2"}},
        })
        pts = data["points"][0]["geometry"]["points"]
        assert pts["x_pos_0_999"]["axis"] == "x"
        assert pts["x_pos_0_999"]["value"] == pytest.approx(-2.34, abs=0.02)
        assert pts["y_pos_0_999"]["value"] == pytest.approx(1.56, abs=0.02)

    def test_scatter_position_without_its_axis_stays_uncalibrated(self):
        # Only X is calibrated: a y read must not borrow the x domain.
        data = E.normalize_scatter_plot_result({
            "points": [{"x": "-2.34", "y": "1.56", "x_pos_0_999": 207,
                        "y_pos_0_999": 759}],
            "axis_calibration": {"x": {"at_0": -4.0, "at_999": 4.0}},
        })
        pts = data["points"][0]["geometry"]["points"]
        assert "value" in pts["x_pos_0_999"]
        assert pts["y_pos_0_999"] == {"pos": 759, "axis": "y"}


# ---------------------------------------------------------------------------
# 5. merge / quality / report smoke (the wiring the previous pass landed)
# ---------------------------------------------------------------------------

class TestContractSurvivesMergeAndReporting:
    def _run(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"sections": [{"name": "S1"}], "species_ranges": [row],
                "biozones": [], "other_fossils": [], "confidence": 0.8}

    def test_two_runs_disagreeing_on_the_answer(self):
        merged = merge_results([
            self._run({"species": "A", "section": "S1", "range_top": "9",
                       "range_base": "7", "response_kind": "extracted",
                       "reason_codes": ["crosses_top"]}),
            self._run({"species": "A", "section": "S1",
                       "response_kind": "not_drawn",
                       "reason_codes": ["not_drawn"]}),
        ], total_runs=2)
        row = merged["species_ranges"][0]
        assert row["response_kind"] == RESPONSE_EXTRACTED
        assert "response_kind_divergent" in (
            [row["_warning"]] if isinstance(row["_warning"], str)
            else row["_warning"])
        assert set(row["reason_codes"]) == {"crosses_top", "not_drawn"}
        assert sorted(row["response_kind_votes"]) == sorted(
            ["extracted", "not_drawn"])

    def test_geometry_is_not_averaged(self):
        block = {"version": 1, "scale": "pos_0_999", "calibrated": True,
                 "points": {"range_top_pos_0_999": {"pos": 347,
                                                    "axis": "vertical",
                                                    "value": 9.0}}}
        other = {"version": 1, "scale": "pos_0_999", "calibrated": True,
                 "points": {"range_top_pos_0_999": {"pos": 400,
                                                    "axis": "vertical",
                                                    "value": 10.2}}}
        merged = merge_results([
            self._run({"species": "A", "section": "S1", "range_top": "9",
                       "geometry": dict(block)}),
            self._run({"species": "A", "section": "S1", "range_top": "9",
                       "geometry": dict(other)}),
        ], total_runs=2)
        row = merged["species_ranges"][0]
        assert row["geometry"]["points"] == \
            block["points"], "the first well-formed read wins, never a mean"
        assert row["geometry"] is not block

    def test_quality_reports_the_ledger_only_when_something_answered(self):
        result = self._run({"species": "A", "section": "S1",
                            "response_kind": "not_drawn",
                            "reason_codes": ["not_drawn"]})
        out = score_range_chart(result)
        assert out["coverage"]["totals"]["not_drawn"] == 1
        assert any(i.get("msg_key") == "quality.coverage_ledger"
                   for i in out["issues"])
        # A pre-contract result stays exactly as quiet as before.
        legacy = self._run({"species": "A", "section": "S1", "range_top": "9"})
        assert "coverage" not in score_range_chart(legacy)
        assert coverage_for(legacy) is None

    def test_audit_report_carries_the_evidence_chain(self):
        result = self._run({"species": "A", "section": "S1",
                            "response_kind": "not_drawn",
                            "reason_codes": ["not_drawn"]})
        report = build_extraction_report(
            data=result, mode="range_chart", mode_used="range_chart",
            image_sha256="ab", runs=1)
        cov = report["coverage"] if "coverage" in report else report
        text = json.dumps(cov, ensure_ascii=False)
        assert "not_drawn" in text


# ---------------------------------------------------------------------------
# 6. prompt layer
# ---------------------------------------------------------------------------

#: The four modes whose ROW normalizer digests the contract (extractor calls
#: ``attach_row_contract``), i.e. the only modes allowed to ask for it.
CONTRACT_MODES = {
    "range_chart": RANGE_CHART_SYSTEM_PROMPT,
    "abundance_diagram": ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
    "chemical_stratigraphy": CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT,
    "scatter_plot": SCATTER_PLOT_SYSTEM_PROMPT,
}

#: Everything else: no consumer means no clause, because the fields would only
#: land under ``_extras`` and mislead the operator into trusting them.
NON_CONTRACT_MODES = {
    "columnar_section": COLUMNAR_SECTION_SYSTEM_PROMPT,
    "phylogenetic_tree": PHYLOGENETIC_TREE_SYSTEM_PROMPT,
    "paleomap": PALEOMAP_SYSTEM_PROMPT,
    "zonation_chart": ZONATION_CHART_SYSTEM_PROMPT,
    "chart_classify": CHART_CLASSIFY_SYSTEM_PROMPT,
}


class TestPromptCoverageContract:
    @pytest.mark.parametrize("mode", sorted(CONTRACT_MODES))
    def test_three_state_clause_present(self, mode):
        prompt = CONTRACT_MODES[mode]
        assert "`response_kind`" in prompt
        # The three states, spelled exactly as reason_codes.py names them.
        assert ('"%s" | "%s" | "%s"' % (RESPONSE_EXTRACTED,
                                        RESPONSE_NOT_DRAWN,
                                        RESPONSE_UNCERTAIN) in prompt), \
            "the three states must be listed"
        assert "ANSWER EVERY REQUESTED CELL" in prompt
        # not_drawn must be framed as a positive observation that still emits
        # a row — the one behaviour the ledger depends on.
        assert "POSITIVE" in prompt
        assert "NEVER delete the row" in prompt

    @pytest.mark.parametrize("mode", sorted(CONTRACT_MODES))
    def test_reason_code_vocabulary_is_the_generated_one(self, mode):
        prompt = CONTRACT_MODES[mode]
        assert REASON_CODE_SLUG_LIST in prompt, \
            "the prompt must ship the catalogue verbatim from reason_codes.py"
        assert "REASON CODES" in prompt
        for slug in REASON_CODE_SLUGS:
            assert slug in prompt

    def test_prompt_advertises_exactly_the_twelve_codes(self):
        # No second source of truth: the closed list in the prompt is the
        # catalogue, with nothing appended and nothing dropped.
        listed = REASON_CODE_SLUG_LIST.split(", ")
        assert set(listed) == set(REASON_CODE_SLUGS)
        assert len(listed) == 12
        for prompt in CONTRACT_MODES.values():
            m = re.search(r"this closed list: (.+?)\. Spelling counts", prompt)
            assert m and m.group(1) == REASON_CODE_SLUG_LIST

    def test_kinds_come_from_the_module_not_a_literal(self):
        # Mirroring the constants means a rename in reason_codes.py cannot
        # leave a prompt asking for a kind the parser would drop.
        assert {"extracted", "not_drawn", "uncertain"} == set(RESPONSE_KINDS)
        assert RESPONSE_KIND_LIST == " | ".join(
            (RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN))

    @pytest.mark.parametrize("mode", sorted(NON_CONTRACT_MODES))
    def test_no_contract_asked_where_nothing_digests_it(self, mode):
        prompt = NON_CONTRACT_MODES[mode]
        assert "response_kind" not in prompt
        assert "reason_codes" not in prompt
        assert "pos_0_999" not in prompt

    def test_conflict_rule_quoted_back_to_the_parser(self):
        # apply_coverage_contract flips not_drawn+value to extracted; the
        # prompt says the same thing, so the model is not surprised by it.
        assert "read back as `extracted`" in \
            CONTRACT_MODES["range_chart"]


class TestPromptAxisPositions:
    @pytest.mark.parametrize("mode", sorted(CONTRACT_MODES))
    def test_pos_scale_worded_the_same_way_everywhere(self, mode):
        prompt = CONTRACT_MODES[mode]
        assert "NORMALISED AXIS POSITIONS" in prompt
        assert "AXIS CALIBRATION" in prompt
        assert "0-999" in prompt

    @pytest.mark.parametrize("mode", sorted(CONTRACT_MODES))
    def test_every_prompted_field_is_digestible_by_the_parser(self, mode):
        parser_known = (set(E._GEOMETRY_VERTICAL_FIELDS)
                        | set(E._GEOMETRY_AXIS_BY_FIELD)
                        | {E.POS_SCALE_KEY})
        for name, _what in POS_FIELDS[mode]:
            assert name in parser_known, (mode, name)
            assert f'"{name}"' in CONTRACT_MODES[mode], \
                f"{mode}: {name} is registered but never asked for in the schema"

    @pytest.mark.parametrize("mode", sorted(CONTRACT_MODES))
    def test_no_invented_position_field_reaches_the_prompt_text(self, mode):
        parser_known = (set(E._GEOMETRY_VERTICAL_FIELDS)
                        | set(E._GEOMETRY_AXIS_BY_FIELD)
                        | {E.POS_SCALE_KEY})
        found = set(re.findall(r"\w*pos_0_999", CONTRACT_MODES[mode]))
        assert found, mode
        assert not found - parser_known, \
            f"{mode} asks for positions the parser cannot digest: " \
            f"{sorted(found - parser_known)}"

    def test_modes_with_a_continuous_axis_are_the_contract_modes(self):
        # A columnar section indexes beds (discrete) and a zonation chart's
        # rows are zones; the parser folds positions only for the four modes
        # above, so POS_FIELDS must not grow past its consumers.
        assert set(POS_FIELDS) == set(CONTRACT_MODES)

    def test_vertical_axis_modes_ask_for_vertical_fields_only(self):
        for mode in ("range_chart", "abundance_diagram",
                     "chemical_stratigraphy"):
            for name, _ in POS_FIELDS[mode]:
                assert E._geometry_axis_for(name, None) == "vertical", (mode, name)

    def test_scatter_asks_for_its_two_own_axes(self):
        names = [n for n, _ in POS_FIELDS["scatter_plot"]]
        assert sorted(names) == ["x_pos_0_999", "y_pos_0_999"]
        assert {E._geometry_axis_for(n, {}) for n in names} == {"x", "y"}

    def test_calibration_examples_use_axis_names_the_parser_accepts(self):
        # Every `"NAME": {"at_0": ...}` pair in a prompt must be a name
        # _geometry_axis_for / axis_domains_from can resolve, or the operator
        # is shown a calibration that silently converts nothing.
        for mode in sorted(CONTRACT_MODES):
            prompt = CONTRACT_MODES[mode]
            names = re.findall(r'"(\w+)":\s*\{\s*"at_0"', prompt)
            assert names, mode
            for name in names:
                assert name in E.GEOMETRY_AXIS_NAMES, (mode, name)
        # The scatter example calibrates both axes by name.
        assert '"x": {"at_0"' in SCATTER_PLOT_SYSTEM_PROMPT
        assert '"y": {"at_0"' in SCATTER_PLOT_SYSTEM_PROMPT

    def test_at_0_at_999_keys_match_the_domain_reader(self):
        # _axis_domain must understand the exact key names the prompt shows.
        for mode in sorted(CONTRACT_MODES):
            assert '"at_0"' in CONTRACT_MODES[mode]
            assert '"at_999"' in CONTRACT_MODES[mode]

    def test_examples_are_self_consistent_with_their_own_calibration(self):
        # range_chart: bed axis 1..24 -> bed 9 sits at 347 (checked by the
        # parser's own conversion, which is the harshest possible review).
        axes = {"vertical": {"at_0": 1.0, "at_999": 24.0, "unit": "bed"}}
        src = {"range_top": "Bed 9", "range_base": "Bed 7", "range_top_idx": 9,
               "range_base_idx": 7, "range_top_pos_0_999": 347,
               "range_base_pos_0_999": 261}
        geom, rejected, _ = E.geometry_from_row(src, axes, row=src)
        assert rejected is False, geom
        assert geom["points"]["range_top_pos_0_999"]["value"] == \
            pytest.approx(9.0, abs=0.1)


class TestPromptVersioning:
    def test_contract_modes_bumped_and_others_untouched(self):
        # A cached pre-contract answer must never be served as if the model had
        # accounted for its negative answers.
        assert PROMPT_VERSION["range_chart"] == "v5"
        assert PROMPT_VERSION["abundance_diagram"] == "v4"
        assert PROMPT_VERSION["chemical_stratigraphy"] == "v2"
        assert PROMPT_VERSION["scatter_plot"] == "v2"
        assert PROMPT_VERSION["columnar_section"] == "v3"
        assert PROMPT_VERSION["zonation_chart"] == "v1"
        assert PROMPT_VERSION["phylogenetic_tree"] == "v1"

    def test_abundance_alias_still_tracks_the_canonical_key(self):
        assert PROMPT_VERSION["abundance"] == PROMPT_VERSION["abundance_diagram"]

    def test_every_mode_with_a_prompt_clause_has_a_version(self):
        for mode in sorted(CONTRACT_MODES):
            assert PROMPT_VERSION.get(mode)


# ---------------------------------------------------------------------------
# 7. end-to-end with a mocked LLM (no network)
# ---------------------------------------------------------------------------

def _mock_llm(payload: Any, calls: list[dict] | None = None):
    def fake_call(*args, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        return (json.dumps(payload), False, 200, "", {})
    return fake_call


class TestContractThroughTheRealPipeline:
    def test_range_chart_run_lands_the_contract_on_the_row(self):
        calls: list[dict] = []
        payload = {
            "sections": [{"name": "S1"}],
            "species_ranges": [
                {"species": "A. alpha", "section": "S1", "range_top": "Bed 9",
                 "range_base": "Bed 7", "range_top_idx": 9, "range_base_idx": 7,
                 "range_top_pos_0_999": 347, "range_base_pos_0_999": 261,
                 "response_kind": "extracted", "reason_codes": ["crosses_top"]},
                {"species": "B. beta", "section": "S1", "range_top": "",
                 "range_base": "", "response_kind": "not_drawn",
                 "reason_codes": "dash"},
            ],
            "biozones": [], "other_fossils": [], "confidence": 0.8,
            "axis_calibration": {"vertical": {"at_0": 1, "at_999": 24,
                                              "unit": "bed"}},
        }
        with patch.object(E, "call_llm_api", side_effect=_mock_llm(payload, calls)):
            r = E.extract_range_chart(api_key="k", image_b64="QQ==",
                                      media_type="image/png")
        assert r.ok, r.error_key
        assert calls and RANGE_CHART_SYSTEM_PROMPT in calls[0]["system_prompt"]
        rows = r.data["species_ranges"]
        assert rows[0]["geometry"]["calibrated"] is True
        assert rows[0]["reason_codes"] == ["crosses_top"]
        # The honest negative answer survives the whole path as an answer.
        assert rows[1]["response_kind"] == RESPONSE_NOT_DRAWN
        assert rows[1]["reason_codes"] == ["not_drawn"]
        assert "geometry" not in rows[1]
        led = coverage_ledger(rows)
        assert led["totals"]["answered"] == 2
        assert led["totals"]["silent_missing"] == 0
        assert r.data["axis_calibration"]["vertical"]["at_0"] == 1.0

    def test_abundance_run_drops_a_contradictory_position(self):
        payload = {
            "sites": [{"name": "SG06", "depth_unit": "cm"}],
            "abundances": [
                {"taxon": "Pinus", "site": "SG06", "level": "120 cm",
                 "depth": "120", "abundance": "35", "pos_0_999": 768},
                {"taxon": "Quercus", "site": "SG06", "level": "120 cm",
                 "depth": "120", "abundance": "5", "pos_0_999": 12},
            ],
            "zones": [], "confidence": 0.7,
            "axis_calibration": {"vertical": {"at_0": 520, "at_999": 0,
                                              "unit": "cm"}},
        }
        calls: list[dict] = []
        with patch.object(E, "call_llm_api",
                          side_effect=_mock_llm(payload, calls)):
            r = E.extract_abundance_diagram(api_key="k", image_b64="QQ==",
                                            media_type="image/png")
        assert r.ok, r.error_key
        assert ABUNDANCE_DIAGRAM_SYSTEM_PROMPT in calls[0]["system_prompt"]
        good, bad = r.data["abundances"]
        assert good["geometry"]["points"]["pos_0_999"]["value"] == \
            pytest.approx(120.7, abs=0.6)
        assert "geometry" not in bad, "12 is ~500 cm, not 120 cm"
        assert bad["reason_codes"] == [LOW_CONFIDENCE_CODE]

    def test_scatter_run_calibrates_both_axes(self):
        payload = {
            "metadata": {"x_axis_label": "PC1"},
            "groups": [],
            "points": [{"x": "-2.34", "y": "1.56", "label": "A-12",
                        "x_pos_0_999": 207, "y_pos_0_999": 99999,
                        "response_kind": "extracted"}],
            "outliers": [], "statistics": {}, "confidence": 0.6,
            "axis_calibration": {
                "x": {"at_0": -4.0, "at_999": 4.0, "unit": "PC1"},
                "y": {"at_0": -3.0, "at_999": 3.0, "unit": "PC2"}},
        }
        calls: list[dict] = []
        with patch.object(E, "call_llm_api", side_effect=_mock_llm(payload, calls)):
            r = E.extract_scatter_plot(api_key="k", image_b64="QQ==",
                                       media_type="image/png")
        assert r.ok, r.error_key
        assert SCATTER_PLOT_SYSTEM_PROMPT in calls[0]["system_prompt"]
        row = r.data["points"][0]
        assert row["geometry"]["points"]["x_pos_0_999"]["value"] == \
            pytest.approx(-2.34, abs=0.02)
        assert row["reason_codes"] == [LOW_CONFIDENCE_CODE]
        assert "y_pos_0_999" not in row.get("_extras", {}), \
            "a rejected position must not be duplicated into _extras"

    def test_columnar_run_never_sees_the_contract(self):
        # Negative control on the dispatch side: this mode's prompt does not
        # ask for the contract, so a row that (hallucinatorily) carries one
        # stays in _extras rather than being reported as an answer.
        payload = {"sections": [{"id": "Ki-1", "samples": [
            {"bed_idx": 5, "fossil_marker": "J", "response_kind": "not_drawn"}]}],
            "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
            "overall_confidence": 0.5}
        calls: list[dict] = []
        with patch.object(E, "call_llm_api", side_effect=_mock_llm(payload, calls)):
            r = E.extract_columnar_section(api_key="k", image_b64="QQ==",
                                           media_type="image/png")
        assert r.ok, r.error_key
        assert COLUMNAR_SECTION_SYSTEM_PROMPT in calls[0]["system_prompt"]
        assert "response_kind" not in calls[0]["system_prompt"]
        sample = r.data["sections"][0]["samples"][0]
        assert sample.get("_extras", {}).get("response_kind") == "not_drawn"
        assert "response_kind" not in sample
