"""BORROW-2026-09-20 — evaluation-domain tests for the new eval_metrics API.

Covers the four borrowed layers added to ``rca_core.eval_metrics``:

1. SCRM-style TIERED boundary scoring (strict / adjacent / coarse / wrong in
   bin space, plus the retained numeric Myr-tolerance mode).
2. CHOCOLATE-style ERROR TYPOLOGY (one deterministic label per row).
3. CharXiv-style SPLIT TRACKS (descriptive vs reasoning, never blended).
4. HONEST REFUSAL RATE built around the OPTIONAL extraction fields
   ``response_kind`` ("extracted" | "not_drawn" | "uncertain") and
   ``reason_codes`` — with explicit coverage of the degradation path when
   those fields are absent, because the extraction contract may not carry
   them yet (parallel work).

All inputs are synthetic predicted / ground-truth pairs; no LLM, no files
except the existing synthetic gold fixture used for a self-consistency check.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rca_core.eval_metrics import (
    BOUNDARY_TIERS,
    DEFAULT_MYR_TOLERANCES,
    ENDPOINT_FIELDS,
    ERROR_LABELS,
    TIER_ADJACENT,
    TIER_COARSE, TIER_STRICT, TIER_WRONG,
    boundary_tier_accuracy,
    classify_boundary_tier,
    error_typology,
    range_tier_accuracy,
    range_base_accuracy,
    range_top_accuracy,
    refusal_metrics,
    species_precision_recall,
    tiered_eval_report,
    track_scores,
)

GOLD_CASE = Path(__file__).parent / "fixtures" / "gold" / "range_chart" / "rc_synth_001"


def _row(species: str, top: Any = None, base: Any = None, **extra: Any) -> dict[str, Any]:
    """One species-range row in the shape extractors actually emit."""
    row: dict[str, Any] = {"species": species}
    if top is not None:
        row["range_top"] = top
    if base is not None:
        row["range_base"] = base
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 1. SCRM-style tiered boundary scoring
# ---------------------------------------------------------------------------


class TestClassifyBoundaryTier:
    """The four tiers, one pair at a time."""

    def test_strict_is_the_same_bin(self):
        verdict = classify_boundary_tier("Bed 23c", "Bed 23c")
        assert verdict == {
            "tier": TIER_STRICT, "distance": 0.0, "signed_distance": 0.0,
            "direction": "none", "mode": "bed",
        }

    def test_adjacent_is_the_neighbouring_bin(self):
        assert classify_boundary_tier("Bed 23c", "Bed 24c")["tier"] == TIER_ADJACENT

    def test_adjacent_covers_a_subscript_slip(self):
        # 23c vs 23d is ONE sub-bed apart: the legacy scorer called this
        # "wrong", the tiered scorer puts it in the lenient neighbouring bin.
        verdict = classify_boundary_tier("Bed 23c", "Bed 23d")
        assert verdict["tier"] == TIER_ADJACENT
        assert verdict["distance"] == 1.0
        # ... and the direction of the slip is preserved (c below d).
        assert verdict["direction"] == "down"

    def test_coarse_spans_one_extra_period(self):
        assert classify_boundary_tier("Bed 23", "Bed 25")["tier"] == TIER_COARSE

    def test_wrong_is_further_than_the_coarse_window(self):
        assert classify_boundary_tier("Bed 23", "Bed 30")["tier"] == TIER_WRONG

    def test_direction_follows_the_bin_axis(self):
        # Larger bed index = higher in the section.
        assert classify_boundary_tier("Bed 30", "Bed 23")["direction"] == "up"
        assert classify_boundary_tier("Bed 23", "Bed 30")["direction"] == "down"
        assert classify_boundary_tier("Bed 30", "Bed 23")["signed_distance"] == 7.0
        assert classify_boundary_tier("Bed 23", "Bed 30")["signed_distance"] == -7.0

    def test_custom_bin_widths_shift_the_tiers(self):
        assert classify_boundary_tier("Bed 23", "Bed 27")["tier"] == TIER_WRONG
        assert classify_boundary_tier(
            "Bed 23", "Bed 27", bin_widths={"adjacent": 1.0, "coarse": 5.0},
        )["tier"] == TIER_COARSE

    def test_uncomparable_pair_is_not_guessed(self):
        assert classify_boundary_tier("top of the cliff", "Bed 4") is None
        assert classify_boundary_tier("", "Bed 4") is None
        assert classify_boundary_tier(None, None) is None


class TestNumericAgeMode:
    """The existing numeric mode (absolute age in Myr) survives the tiers."""

    def test_myr_tolerances_define_the_tiers(self):
        assert classify_boundary_tier("253.1 Ma", "253.1 Ma", mode="numeric")["tier"] == TIER_STRICT
        assert classify_boundary_tier("253.1 Ma", "253.4 Ma", mode="numeric")["tier"] == TIER_ADJACENT
        assert classify_boundary_tier("253.1 Ma", "254.5 Ma", mode="numeric")["tier"] == TIER_COARSE
        assert classify_boundary_tier("253.1 Ma", "260.0 Ma", mode="numeric")["tier"] == TIER_WRONG

    def test_defaults_are_exposed_and_myr_based(self):
        assert DEFAULT_MYR_TOLERANCES == {"adjacent": 0.5, "coarse": 2.0}

    def test_age_direction_is_inverse_of_the_index_axis(self):
        # A SMALLER Ma is higher in the section, so "too young" reads as up.
        verdict = classify_boundary_tier("250 Ma", "253 Ma", mode="numeric")
        assert verdict["direction"] == "up"
        assert verdict["signed_distance"] == 3.0
        assert classify_boundary_tier("255 Ma", "253 Ma", mode="numeric")["direction"] == "down"

    def test_units_are_rescaled_before_comparison(self):
        assert classify_boundary_tier("253 Ma", "253000 ka", mode="numeric")["tier"] == TIER_STRICT
        assert classify_boundary_tier("0.253 Ma", "253 ka", mode="numeric")["tier"] == TIER_STRICT

    def test_auto_mode_never_reads_a_bare_bed_index_as_an_age(self):
        # A bare number is a bin, not an age: mode="auto" keeps the two spaces
        # apart instead of scoring "13" against "8" in Myr.
        assert classify_boundary_tier("13", "8")["mode"] == "bed"
        assert classify_boundary_tier("253 Ma", "8") is None

    def test_custom_myr_tolerances(self):
        assert classify_boundary_tier(
            "253.1 Ma", "256.0 Ma", mode="age",
            myr_tolerances={"adjacent": 0.5, "coarse": 4.0},
        )["tier"] == TIER_COARSE


class TestBoundaryTierAccuracy:
    GT = [
        _row("A", "8", "3"),
        _row("B", "13", "8"),
        _row("C", "20", "15"),
        _row("D", "30", "25"),
    ]

    def test_four_tier_proportions_and_weighted_score(self):
        pred = [
            _row("A", "8", "3"),    # strict
            _row("B", "14", "8"),   # adjacent
            _row("C", "18", "15"),  # coarse
            _row("D", "10", "25"),  # wrong
        ]
        m = boundary_tier_accuracy(pred, self.GT, field="range_top")
        assert m["scored"] == 4
        assert m["counts"] == {TIER_STRICT: 1, TIER_ADJACENT: 1, TIER_COARSE: 1, TIER_WRONG: 1}
        assert m["rates"] == {t: 0.25 for t in BOUNDARY_TIERS}
        assert m["weighted_score"] == pytest.approx((1.0 + 0.6 + 0.3 + 0.0) / 4)
        assert [row["tier"] for row in m["per_row"]] == [
            TIER_STRICT, TIER_ADJACENT, TIER_COARSE, TIER_WRONG,
        ]

    def test_weights_are_configurable(self):
        pred = [_row("A", "9", "3")]  # one adjacent bin
        m = boundary_tier_accuracy(
            pred, self.GT, field="range_top", weights={TIER_ADJACENT: 1.0},
        )
        assert m["weighted_score"] == 1.0
        assert m["weights"][TIER_WRONG] == 0.0  # wrong never earns credit

    def test_unmatched_species_and_unparseable_cells_are_skipped(self):
        pred = [_row("A", "8", "3"), _row("Z", "8", "3"), _row("B", "see text", "8")]
        m = boundary_tier_accuracy(pred, self.GT, field="range_top")
        assert m["scored"] == 1
        assert m["unscorable"] == 1

    def test_empty_ground_truth_yields_zeroed_structure(self):
        m = boundary_tier_accuracy([_row("A", "8", "3")], [], field="range_top")
        assert m["scored"] == 0
        assert m["rates"] == {t: 0.0 for t in BOUNDARY_TIERS}
        assert m["weighted_score"] == 0.0

    def test_numeric_field_scores_in_myr(self):
        gt = [_row("A", "253.1 Ma", "254.5 Ma")]
        pred = [_row("A", "253.4 Ma", "254.5 Ma")]
        m = boundary_tier_accuracy(pred, gt, field="range_top")
        assert m["modes"] == {"age": 1}
        assert m["counts"][TIER_ADJACENT] == 1


class TestRangeTierAccuracy:
    def test_row_tier_is_the_worst_endpoint(self):
        gt = [_row("A", "8", "3")]
        pred = [_row("A", "8", "20")]  # top nailed, base far away
        m = range_tier_accuracy(pred, gt)
        assert m["fields"]["range_top"]["counts"][TIER_STRICT] == 1
        assert m["fields"]["range_base"]["counts"][TIER_WRONG] == 1
        # The row may NOT be padded by the good endpoint.
        assert m["row"]["counts"][TIER_WRONG] == 1
        assert m["row"]["weighted_score"] == 0.0

    def test_both_fields_are_reported_per_species(self):
        gt = [_row("A", "8", "3"), _row("B", "13", "8")]
        pred = [_row("A", "9", "4"), _row("B", "13", "8")]
        m = range_tier_accuracy(pred, gt)
        assert set(ENDPOINT_FIELDS) == {"range_top", "range_base"}
        assert m["row"]["scored"] == 2
        assert m["row"]["counts"][TIER_ADJACENT] == 1  # A slid one bin on both
        assert m["row"]["counts"][TIER_STRICT] == 1    # B exact


# ---------------------------------------------------------------------------
# 2. CHOCOLATE-style error typology
# ---------------------------------------------------------------------------


def _labels_by_species(result: dict[str, Any]) -> dict[str, str]:
    return {row["species"]: row["label"] for row in result["rows"]}


class TestErrorTypology:
    def test_every_label_in_the_closed_set_is_reachable(self):
        gt = [
            _row("Gryphaea arcuata", "13", "8"),
            _row("Dactylioceras commune", "10", "3"),
            _row("Ammonites koslovensis", "6", "2"),
            _row("Inoceramus elongatus", "20", "15"),
            _row("Belemnitella mucronata", "30", "25"),
        ]
        pred = [
            _row("Gryphaea arcuata", "13", "8"),        # correct
            _row("Dactylioceras communis", "10", "3"),  # taxon_misid (fuzzy name)
            _row("Ammonites koslovensis", "60", "2"),   # value_scale_error (10x)
            _row("Belemnitella mucronata", "31", "26"), # range_shift_up (both +1)
            _row("Trilobites inventus", "4", "2"),      # hallucination
            # "Inoceramus elongatus" is neither answered nor refused ->
            # omission.
        ]
        result = error_typology(pred, gt)
        assert set(result["labels"]) == {
            "correct", "taxon_misid", "boundary_misread", "range_shift_up",
            "range_shift_down", "omission", "hallucination", "value_scale_error",
        }
        assert set(result["counts"]) == set(result["labels"])
        assert result["counts"] == {
            "correct": 1,
            "taxon_misid": 1,
            "boundary_misread": 0,
            "range_shift_up": 1,
            "range_shift_down": 0,
            "omission": 1,
            "hallucination": 1,
            "value_scale_error": 1,
        }
        assert result["n_rows"] == 6  # 5 ground-truth rows + 1 invented row

    def test_boundary_misread_is_a_single_endpoint_miss(self):
        gt = [_row("A", "8", "3")]
        pred = [_row("A", "14", "3")]  # top wrong, base exact
        result = error_typology(pred, gt)
        assert _labels_by_species(result) == {"A": "boundary_misread"}
        assert result["rows"][0]["detail"]["directions"]["range_top"] == "up"
        assert result["rows"][0]["detail"]["tiers"]["range_base"] == TIER_STRICT

    def test_stretched_range_is_not_reported_as_a_directional_shift(self):
        # Top pushed too high AND base pulled too low: the range was stretched,
        # not slid, so a "shifted up/down" label would describe it wrongly.
        gt = [_row("A", "8", "3")]
        pred = [_row("A", "12", "1")]
        result = error_typology(pred, gt)
        assert _labels_by_species(result) == {"A": "boundary_misread"}
        assert set(result["rows"][0]["detail"]["directions"].values()) == {"up", "down"}

    def test_range_shift_down_label(self):
        gt = [_row("A", "8", "3")]
        pred = [_row("A", "6", "1")]  # whole range slid down-section
        assert _labels_by_species(error_typology(pred, gt)) == {"A": "range_shift_down"}

    def test_abundance_unit_slip_is_a_scale_error(self):
        gt = [_row("A", "8", "3", level="1", abundance="60")]
        pred = [_row("A", "8", "3", level="1", abundance="0.6")]  # fraction vs %
        result = error_typology(pred, gt)
        assert _labels_by_species(result) == {"A": "value_scale_error"}
        assert result["rows"][0]["detail"]["scale"]["abundance"]["factor"] == 0.01

    def test_merely_far_numbers_are_not_reported_as_scale_slips(self):
        gt = [_row("A", "8", "3")]
        pred = [_row("A", "12", "3")]  # 1.5x is a misread, not a unit slip
        assert _labels_by_species(error_typology(pred, gt)) == {"A": "boundary_misread"}

    def test_qualifier_only_name_difference_stays_correct_but_is_flagged(self):
        gt = [_row("Ammonites koslovensis", "13", "8")]
        pred = [_row("Ammonites cf. koslovensis", "13", "8")]
        result = error_typology(pred, gt)
        row = result["rows"][0]
        assert row["label"] == "correct"
        assert row["aux"] == "name_qualifier"
        assert row["note"] == "qualifier_only_difference"
        assert result["aux_counts"] == {"name_qualifier": 1}
        # The strict legacy metric still refuses to call it a match: the two
        # views are complementary, not contradictory.
        assert species_precision_recall(pred, gt)["f1"] == 0.0
        assert species_precision_recall(pred, gt)["lenient"]["f1"] == 1.0

    def test_duplicate_prediction_of_one_species_is_a_hallucination(self):
        gt = [_row("A", "8", "3")]
        pred = [_row("A", "8", "3"), _row("A", "8", "3")]
        result = error_typology(pred, gt)
        assert result["counts"]["correct"] == 1
        assert result["counts"]["hallucination"] == 1

    def test_rates_are_over_labelled_rows_and_errors_are_counted(self):
        gt = [_row("A", "8", "3"), _row("B", "13", "8")]
        pred = [_row("A", "8", "3")]
        result = error_typology(pred, gt)
        assert result["n_rows"] == 2
        assert result["n_errors"] == 1
        assert result["error_rate"] == 0.5
        assert result["rates"]["correct"] == 0.5
        assert result["rates"]["omission"] == 0.5
        assert result["refusal_field_present"] is False

    def test_rows_can_be_omitted_for_compact_reports(self):
        result = error_typology([_row("A", "8", "3")], [_row("A", "8", "3")], include_rows=False)
        assert "rows" not in result
        assert result["counts"]["correct"] == 1


# ---------------------------------------------------------------------------
# 3. CharXiv-style split tracks
# ---------------------------------------------------------------------------


class TestTracks:
    GT = [
        _row("A", "13", "8", section="Section X", biozone="Koslovites Biozone"),
        _row("B", "10", "3", section="Section X", biozone="Aspidoceras Zone"),
    ]

    def test_tracks_are_reported_separately_and_never_blended(self):
        pred = [
            _row("A", "3", "2", section="Section X", biozone="Koslovites Biozone"),
            _row("B", "13", "8", section="Section X", biozone="Aspidoceras Zone"),
        ]
        tracks = track_scores(pred, self.GT)
        # Labels, zones and headers are all read perfectly -> descriptive 1.0
        assert tracks["descriptive"]["score"] == 1.0
        # ... while every comparison (intersection, ordering, extent) broke.
        assert tracks["reasoning"]["score"] == 0.0
        # No headline average exists to hide one behind the other.
        assert "score" not in tracks and "overall" not in tracks
        assert "no blended headline number" in tracks["policy"]

    def test_descriptive_components_break_the_label_reading_down(self):
        pred = [
            _row("A", "13", "8", section="Section X", biozone="Wrong Zone"),
            _row("B", "10", "3", section="Section Y", biozone="Aspidoceras Zone"),
        ]
        tracks = track_scores(pred, self.GT)
        components = tracks["descriptive"]["components"]
        assert components["taxon_label"]["score"] == 1.0
        assert components["biozone_label"]["score"] == 0.5
        # One of the two real headers is right and one is invented -> the
        # section component is symmetric, so inventing "Section Y" costs.
        assert components["section_header"]["score"] == 0.5
        # Boundaries are perfect, so the reasoning track stays untouched.
        assert tracks["reasoning"]["score"] == 1.0
        assert tracks["descriptive"]["score"] < 1.0

    def test_reasoning_intersection_and_ordering_components(self):
        tracks = track_scores([dict(r) for r in self.GT], self.GT)
        components = tracks["reasoning"]["components"]
        # A (8..13) and B (3..10) co-occur in beds 8..10 in ground truth.
        assert components["range_intersection"]["comparisons"] == 1
        assert components["range_intersection"]["score"] == 1.0
        assert components["top_order"]["score"] == 1.0
        assert components["base_order"]["score"] == 1.0
        assert components["range_extent"]["score"] == 1.0

    def test_intersection_breaks_when_ranges_stop_overlapping(self):
        pred = [
            _row("A", "13", "12", section="Section X", biozone="Koslovites Biozone"),
            _row("B", "10", "3", section="Section X", biozone="Aspidoceras Zone"),
        ]
        tracks = track_scores(pred, self.GT)
        assert tracks["reasoning"]["components"]["range_intersection"]["score"] == 0.0
        # Ordering of the tops survives, so the two components disagree by design.
        assert tracks["reasoning"]["components"]["top_order"]["score"] == 1.0

    def test_missing_annotations_drop_a_component_instead_of_scoring_zero(self):
        gt = [_row("A", "13", "8"), _row("B", "10", "3")]
        pred = [_row("A", "13", "8"), _row("B", "10", "3")]
        tracks = track_scores(pred, gt)
        assert "biozone_label" not in tracks["descriptive"]["components"]
        assert "section_header" not in tracks["descriptive"]["components"]
        assert tracks["descriptive"]["score"] == 1.0

    def test_explicit_biozone_block_overrides_the_species_row_fallback(self):
        # The species rows say "Wrong Zone"; the dedicated biozones block is
        # what the caller passed, so the descriptive track reads THAT.
        pred = [
            _row("A", "13", "8", section="Section X", biozone="Wrong Zone"),
            _row("B", "10", "3", section="Section X", biozone="Wrong Zone"),
        ]
        tracks = track_scores(
            pred, self.GT,
            biozone_predicted=[{"name": "Koslovites Biozone"}, {"name": "Aspidoceras Zone"}],
            biozone_ground_truth=[{"name": "Koslovites Biozone"}, {"name": "Aspidoceras Zone"}],
        )
        assert tracks["descriptive"]["components"]["biozone_label"]["score"] == 1.0


# ---------------------------------------------------------------------------
# 4. Honest refusal rate + graceful degradation without response_kind
# ---------------------------------------------------------------------------

GT_REFUSE = [
    _row("A", "13", "8"),
    _row("B", "10", "3"),
    _row("C", "6", "2"),
]


def _pred_with_refusals() -> list[dict[str, Any]]:
    """A real answer plus one "not drawn" and one "uncertain" declination.

    The refused rows carry the taxon name (that is what the model could read)
    but NO boundary values — which is exactly the case the optional
    ``response_kind`` field exists to separate from a wrong answer.
    """
    return [
        _row("A", "13", "8"),
        _row("B", response_kind="not_drawn", reason_codes=["range_not_drawn"]),
        _row("C", response_kind="uncertain", reason_codes=["illegible_label"]),
    ]


def _strip_response_kind(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in row.items() if k not in ("response_kind", "reason_codes")} for row in rows]


class TestRefusalMetrics:
    def test_field_present_counts_refusals_per_kind(self):
        m = refusal_metrics(_pred_with_refusals(), GT_REFUSE)
        assert m["refusal_field_present"] is True
        assert m["recall_as_omission"] is False
        assert m["n_not_drawn"] == 1 and m["n_uncertain"] == 1
        assert m["n_refused"] == 2 and m["n_answered"] == 1
        assert m["refusal_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert m["not_drawn_rate"] == pytest.approx(1 / 3, abs=1e-3)
        assert m["uncertain_rate"] == pytest.approx(1 / 3, abs=1e-3)
        assert m["answer_coverage"] == pytest.approx(1 / 3, abs=1e-3)

    def test_refusal_is_reported_beside_precision_not_inside_it(self):
        m = refusal_metrics(_pred_with_refusals(), GT_REFUSE)
        # All three predictions name real taxa, so the legacy precision stays
        # at 1.0; the cost of declining two of them shows up in refusal_rate
        # and in the answer-restricted recall, not in a laundered error count.
        assert m["precision"] == 1.0
        assert m["recall"] == 1.0
        assert m["precision_on_answered"] == 1.0
        assert m["recall_on_answered"] == pytest.approx(1 / 3, abs=1e-3)
        assert m["f1_on_answered"] < m["f1"]
        assert m["refusal_rate"] > 0.0

    def test_refused_rows_are_kept_out_of_the_boundary_denominator(self):
        m = boundary_tier_accuracy(_pred_with_refusals(), GT_REFUSE, field="range_top")
        assert m["scored"] == 1
        assert m["refused"] == 2
        assert m["refusal_field_present"] is True

    def test_refused_rows_are_not_charged_as_errors(self):
        result = error_typology(_pred_with_refusals(), GT_REFUSE)
        by_label = {row["label"] for row in result["rows"]}
        assert result["counts"]["omission"] == 0
        assert result["counts"]["hallucination"] == 0
        assert result["refused_rows"] == 2
        assert "refusal" not in result["labels"]  # auxiliary, not an error type
        assert by_label == {"correct", "refusal"}
        assert result["aux_counts"] == {"not_drawn": 1, "uncertain": 1}
        assert result["reason_code_counts"] == {"range_not_drawn": 1, "illegible_label": 1}

    def test_extracted_response_kind_is_not_a_refusal(self):
        pred = [
            _row("A", "13", "8", response_kind="extracted"),
            _row("B", "10", "3", response_kind="extracted"),
        ]
        m = refusal_metrics(pred, GT_REFUSE)
        assert m["n_refused"] == 0 and m["refusal_field_present"] is True
        assert m["refusal_rate"] == 0.0 and m["answer_coverage"] == 1.0
        assert m["n_answered"] == 2

    def test_refusal_spelling_and_singular_reason_code_variants_are_tolerated(self):
        # The contract field comes out of a model-facing schema, so "Not Drawn"
        # and a bare singular ``reason_code`` must still read as a refusal.
        pred = [_row("A", "13", "8", response_kind="Not Drawn", reason_code="range_not_drawn")]
        m = refusal_metrics(pred, GT_REFUSE)
        assert m["n_not_drawn"] == 1 and m["refusal_field_present"] is True
        assert m["reason_code_counts"] == {"range_not_drawn": 1}
        assert error_typology(pred, GT_REFUSE)["counts"]["omission"] == 2

    def test_unattributable_refusal_still_loses_the_cell_to_nothing(self):
        # A refusal without a species name cannot claim a ground-truth row, so
        # the annotation stays an omission — the scorer must not launder it.
        pred = [_row("A", "13", "8"), _row("", response_kind="not_drawn")]
        result = error_typology(pred, GT_REFUSE)
        assert result["counts"]["omission"] == 2  # B and C remain unclaimed
        assert result["refused_rows"] == 1
        assert refusal_metrics(pred, GT_REFUSE)["refusal_field_present"] is True


class TestRefusalDegradation:
    """Without the optional fields, behaviour must equal the pre-BORROW one."""

    def test_no_response_kind_means_no_visible_refusals(self):
        m = refusal_metrics(_strip_response_kind(_pred_with_refusals()), GT_REFUSE)
        assert m["refusal_field_present"] is False
        assert m["recall_as_omission"] is True
        assert m["n_refused"] == 0
        assert m["refusal_rate"] == 0.0 and m["uncertain_rate"] == 0.0
        assert m["n_answered"] == 3
        assert m["reason_code_counts"] == {}

    def test_unanswered_cells_fall_back_to_omission(self):
        result = error_typology(_strip_response_kind(_pred_with_refusals()), GT_REFUSE)
        assert result["refusal_field_present"] is False
        assert result["refused_rows"] == 0
        assert result["counts"]["omission"] == 2  # exactly today's behaviour
        assert result["counts"]["hallucination"] == 0
        assert result["aux_counts"] == {"no_boundary_delivered": 2}

    def test_tier_scorer_still_skips_cells_with_nothing_to_compare(self):
        m = boundary_tier_accuracy(_strip_response_kind(_pred_with_refusals()), GT_REFUSE)
        assert m["refused"] == 0
        assert m["refusal_field_present"] is False
        assert m["scored"] == 1 and m["unscorable"] == 2
        assert m["counts"][TIER_STRICT] == 1

    def test_name_only_match_is_flagged_instead_of_padded(self):
        # Nothing parseable on either side: the row is accepted on the name
        # alone, but the auxiliary counter keeps that visible in the report.
        gt = [_row("A", "see text", "unreadable")]
        pred = [_row("A", "see text", "unreadable")]
        result = error_typology(pred, gt)
        assert result["counts"]["correct"] == 1
        assert result["aux_counts"] == {"unscorable_endpoints": 1}

    def test_empty_answer_against_empty_annotation_is_not_an_omission(self):
        result = error_typology([_row("A")], [_row("A")])
        assert result["counts"]["omission"] == 0
        assert result["counts"]["correct"] == 1

    def test_empty_string_response_kind_is_not_a_refusal(self):
        pred = [_row("A", "13", "8", response_kind="")]
        m = refusal_metrics(pred, GT_REFUSE)
        assert m["refusal_field_present"] is False
        assert m["n_refused"] == 0


# ---------------------------------------------------------------------------
# 5. Report shape: legacy keys preserved, new layers additive
# ---------------------------------------------------------------------------


class TestTieredEvalReport:
    GT = [_row("A", "13", "8"), _row("B", "10", "3"), _row("C", "6", "2")]

    def test_legacy_keys_and_payloads_are_unchanged(self):
        pred = [_row("A", "13", "8"), _row("B", "11", "3")]
        report = tiered_eval_report(pred, self.GT)
        assert report["species_precision_recall"] == species_precision_recall(pred, self.GT)
        assert report["range_top_accuracy"] == range_top_accuracy(pred, self.GT, 1)
        assert report["range_base_accuracy"] == range_base_accuracy(pred, self.GT, 1)
        for key in ("exact", "within_tolerance", "wrong", "subscript_mismatch",
                    "acc_exact", "acc_tolerance"):
            assert key in report["range_top_accuracy"]

    def test_new_layers_are_additive_siblings(self):
        report = tiered_eval_report([_row("A", "13", "8")], self.GT)
        assert set(report["boundary_tiers"]["fields"]) == {"top", "base"}
        assert set(report["error_typology"]["counts"]) == set(ERROR_LABELS)
        assert set(report["tracks"]) == {"descriptive", "reasoning", "policy"}
        assert report["refusals"]["refusal_field_present"] is False
        assert report["boundary_tiers"]["row"]["rates"][TIER_STRICT] == 1.0

    def test_rows_block_is_opt_in(self):
        assert "rows" not in tiered_eval_report([_row("A", "13", "8")], self.GT)["error_typology"]
        assert "rows" in tiered_eval_report(
            [_row("A", "13", "8")], self.GT, include_rows=True,
        )["error_typology"]

    def test_numeric_mode_report_keeps_bed_mode_report_alive(self):
        gt = [_row("A", "253.1 Ma", "254.5 Ma")]
        pred = [_row("A", "253.4 Ma", "254.5 Ma")]
        report = tiered_eval_report(pred, gt, mode="age")
        assert report["boundary_tiers"]["fields"]["top"]["modes"] == {"age": 1}
        # The legacy bed-index scorer simply finds nothing to compare here and
        # reports zeros rather than crashing on the ages.
        assert report["range_top_accuracy"]["exact"] == 0


class TestGoldFixtureSelfConsistency:
    """The real fixture shape must score as a perfect read against itself."""

    def test_ground_truth_against_itself(self):
        gt = json.loads((GOLD_CASE / "ground_truth.json").read_text(encoding="utf-8"))
        rows = gt["species_ranges"]
        report = tiered_eval_report(rows, rows)
        assert report["boundary_tiers"]["row"]["counts"][TIER_STRICT] == len(rows)
        assert report["error_typology"]["counts"]["correct"] == len(rows)
        assert report["error_typology"]["n_errors"] == 0
        assert report["tracks"]["descriptive"]["score"] == 1.0
        assert report["tracks"]["reasoning"]["score"] == 1.0
        assert report["refusals"]["refusal_field_present"] is False


class TestGoldReportSync:
    """``scripts/gold_report.py`` renders the new blocks and ignores their absence."""

    @staticmethod
    def _load():
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "gold_report.py"
        spec = importlib.util.spec_from_file_location("gold_report", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module

    def test_legacy_metrics_render_no_extra_lines(self):
        module = self._load()
        legacy = {"species_precision_recall": {"precision": 1.0, "recall": 1.0, "f1": 1.0}}
        assert module._borrow_metric_lines(legacy) == []
        html = module.generate_html({
            "cases": [{"id": "c1", "type": "range_chart", "metrics": legacy, "passed": True}],
            "summary": {"total_cases": 1, "passed": 1, "failed": 0},
        })
        assert "refusal" not in html and "tiers" not in html

    def test_new_blocks_are_rendered_and_the_degraded_case_says_so(self):
        module = self._load()
        report = tiered_eval_report(
            [_row("A", "9", "3"), _row("B", "10", "3")],
            [_row("A", "8", "3"), _row("B", "10", "3"), _row("C", "6", "2")],
        )
        lines = module._borrow_metric_lines(report)
        assert any(line.startswith("tiers ") for line in lines)
        assert any("omission=1" in line for line in lines)
        assert any("descriptive=" in line and "reasoning=" in line for line in lines)
        # No response_kind in this run: the report must say "n/a", not "0.00".
        assert any("refusal=n/a" in line for line in lines)

        with_refusals = tiered_eval_report(_pred_with_refusals(), GT_REFUSE)
        refusal_lines = module._borrow_metric_lines(with_refusals)
        assert any("refusal=" in line and "n/a" not in line for line in refusal_lines)


class TestLegacyApiUntouched:
    """The pre-BORROW public surface must keep its exact signature contract."""

    def test_legacy_functions_still_return_their_old_keys(self):
        pred = [_row("A", "9", "3"), _row("Z", "1", "2")]
        gt = [_row("A", "8", "3")]
        assert set(species_precision_recall(pred, gt)) >= {
            "precision", "recall", "f1", "true_positives", "false_positives",
            "false_negatives", "matching_policy", "lenient",
        }
        assert set(range_top_accuracy(pred, gt, 1)) == {
            "exact", "within_tolerance", "wrong", "subscript_mismatch",
            "acc_exact", "acc_tolerance",
        }
        assert set(range_base_accuracy(pred, gt)) == set(range_top_accuracy(pred, gt))

    def test_positional_tolerance_still_works(self):
        # range_*_accuracy(pred, gt, tolerance) positional form is preserved.
        m = range_top_accuracy([_row("A", "9", "3")], [_row("A", "8", "3")], 2)
        assert m["within_tolerance"] == 1


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    raise SystemExit(pytest.main([__file__, "-q"]))
