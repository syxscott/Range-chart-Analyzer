"""FIX-2026-09-22 — audit regressions for the evaluation domain.

One test class per confirmed audit bug in ``rca_core.eval_metrics`` /
``scripts/gold_report.py`` (baseline 8f9ed23, re-verified at HEAD):

1. [HIGH] Species-only, last-write-wins ground-truth lookup in
   ``boundary_tier_accuracy`` vs first-wins in the typology / pair indices —
   one two-section dataset with PERFECTLY correct predictions produced four
   mutually contradictory headline numbers. Fix: one compound
   ``(section, species)`` key for every BORROW-layer consumer, first row
   wins, duplicates listed deterministically under
   ``duplicate_ground_truth`` instead of being silently overwritten.
2. [MED] ``mode="auto"`` read numeric ages as bed numbers (``_parse_bed``
   accepts "30"), inverting the direction semantics against ``mode="age"``.
   Fix: bed space only for bed-SHAPED values ("Bed 12", "12a", "23z"); a
   bare number (optionally with Ma) is an age.
3. [MED] Zero-denominator ratios reported 0.0 ("all wrong") instead of None
   ("not measured"), and ``scripts/gold_report.py`` printed those fake
   0.00s. Fix: None + "n/a" rendering.
4. [LOW-MED] Row summary carried no refused/unscorable counts; eval had a
   private response-kind spelling table that missed blank/dash/undrawn/
   unclear. Fix: row counts added; ``reason_codes.normalize_response_kind``
   is now the single normalization authority.
5. [LOW] ``datetime.utcnow()`` in gold_report; plus the A-6 all-refusal
   f1=1.0 optic, annotated via ``all_rows_refused`` + an "n/a"-aware
   renderer (the ``*_on_answered`` trio moved to None semantics with #3).

All offline: synthetic rows only, no LLM, no network.
"""
from __future__ import annotations

import importlib.util
import warnings
from pathlib import Path
from typing import Any

import pytest

from rca_core.eval_metrics import (
    BOUNDARY_TIERS,
    TIER_ADJACENT,
    TIER_COARSE,
    TIER_STRICT,
    TIER_WRONG,
    _matched_pair_indices,
    _row_is_refusal,
    boundary_tier_accuracy,
    classify_boundary_tier,
    error_typology,
    range_tier_accuracy,
    refusal_metrics,
    tiered_eval_report,
    track_scores,
)

GOLD_REPORT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gold_report.py"


def _load_gold_report():
    spec = importlib.util.spec_from_file_location("gold_report_audit", GOLD_REPORT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _row(species: str, top: Any = None, base: Any = None, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"species": species}
    if top is not None:
        row["range_top"] = top
    if base is not None:
        row["range_base"] = base
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 1. Compound (section, species) key — coherent metrics on two-section data
# ---------------------------------------------------------------------------


class TestTwoSectionCoherence:
    GT = [
        _row("A", "Bed 5", "Bed 2", section="Section S1"),
        _row("A", "Bed 9", "Bed 7", section="Section S2"),
    ]

    def test_perfect_predictions_are_perfect_everywhere(self):
        pred = [dict(r) for r in self.GT]
        report = tiered_eval_report(pred, self.GT)
        top = report["boundary_tiers"]["fields"]["top"]
        # Before the fix: gt_lookup keyed on species alone and LAST WRITE won,
        # so the S1 prediction was graded against the S2 annotation ->
        # "scored 2 / strict 1 / wrong 1 / weighted 0.5" while the typology
        # (first-wins) said 0 errors. Coherent now:
        assert top["scored"] == 2
        assert top["counts"] == {TIER_STRICT: 2, TIER_ADJACENT: 0, TIER_COARSE: 0, TIER_WRONG: 0}
        assert top["weighted_score"] == 1.0
        row = report["boundary_tiers"]["row"]
        assert row["scored"] == 2 and row["weighted_score"] == 1.0
        assert report["error_typology"]["counts"]["correct"] == 2
        assert report["error_typology"]["error_rate"] == 0.0
        assert report["tracks"]["descriptive"]["score"] == 1.0
        assert report["tracks"]["reasoning"]["score"] == 1.0

    def test_row_aggregation_keeps_sections_apart(self):
        # S1 nailed, S2 slid one bin up: two ROW verdicts, worst-of-endpoints
        # per row — not one species-collapsed row.
        pred = [
            _row("A", "Bed 5", "Bed 2", section="Section S1"),
            _row("A", "Bed 10", "Bed 8", section="Section S2"),
        ]
        m = range_tier_accuracy(pred, self.GT)
        assert m["row"]["scored"] == 2
        assert m["row"]["counts"][TIER_STRICT] == 1
        assert m["row"]["counts"][TIER_ADJACENT] == 1
        assert m["row"]["weighted_score"] == pytest.approx((1.0 + 0.6) / 2)

    def test_matching_keys_are_section_scoped(self):
        # A prediction for a section nobody annotated is NOT scored against
        # the same species' row from another section.
        pred = [_row("A", "Bed 5", "Bed 2", section="Section S3")]
        m = boundary_tier_accuracy(pred, self.GT, field="range_top")
        assert m["scored"] == 0 and m["counts"][TIER_WRONG] == 0
        pairs = _matched_pair_indices(pred, self.GT)
        assert pairs == []

    def test_duplicate_gt_keys_first_wins_and_are_listed(self):
        gt = [
            _row("A", "Bed 5", "Bed 2", section="S1"),
            _row("A", "Bed 90", "Bed 9", section="S1"),  # same compound key
        ]
        pred = [_row("A", "Bed 5", "Bed 2", section="S1")]
        m = boundary_tier_accuracy(pred, gt, field="range_top")
        # FIRST row kept (the old lookup last-write-won -> "wrong").
        assert m["counts"][TIER_STRICT] == 1 and m["counts"][TIER_WRONG] == 0
        assert m["duplicate_ground_truth"] == [
            {"section": "S1", "species": "A", "rows": [0, 1]},
        ]
        typ = error_typology(pred, gt)
        assert typ["duplicate_ground_truth"] == [
            {"section": "S1", "species": "A", "rows": [0, 1]},
        ]
        # The unclaimed twin is visible as an omission, never silently eaten.
        assert typ["counts"]["omission"] == 1

    def test_normalization_of_the_key_is_case_and_whitespace_insensitive(self):
        gt = [_row("A", "Bed 5", "Bed 2", section=" Section  X ")]
        pred = [_row("a", "Bed 5", "Bed 2", section="section  x")]
        m = boundary_tier_accuracy(pred, gt, field="range_top")
        assert m["scored"] == 1 and m["counts"][TIER_STRICT] == 1

    def test_legacy_scorers_keep_their_documented_species_only_contract(self):
        # The fix deliberately did not touch the legacy keys; only the four
        # BORROW consumers had to converge. Pinned so the boundary is visible.
        pred = [dict(r) for r in self.GT]
        report = tiered_eval_report(pred, self.GT)
        # species-only lookup, last-write-wins -> S1 pred scored against S2.
        assert report["range_top_accuracy"]["acc_exact"] == 0.5


# ---------------------------------------------------------------------------
# 2. Auto-mode heuristic: bare numbers are AGES, bed shapes are BINS
# ---------------------------------------------------------------------------


class TestAutoBedShapeHeuristic:
    def test_auto_matches_explicit_age_mode_on_bare_numbers(self):
        for pred, gt in ((34, 36), ("34", "35"), (34.5, 33.0), ("34 Ma", "36")):
            assert classify_boundary_tier(pred, gt) == classify_boundary_tier(
                pred, gt, mode="age",
            ), (pred, gt)

    def test_measured_audit_probe_direction_coherent(self):
        # range_top 34 vs gt 36: younger reading sits HIGHER up-section.
        # Old auto resolved to bed space and graded the same pair the other
        # way round against mode="age".
        auto = classify_boundary_tier(34, 36)
        age = classify_boundary_tier(34, 36, mode="age")
        assert auto["mode"] == "age" and auto == age
        assert auto["direction"] == age["direction"] == "up"
        # (34, 35): one Myr is COARSE in age space (adjacent window 0.5),
        # not the adjacent bin the bed reinterpretation produced.
        assert classify_boundary_tier(34, 35)["tier"] == TIER_COARSE
        assert classify_boundary_tier("34", "35")["tier"] == TIER_COARSE

    def test_bed_shaped_patterns_still_resolve_to_bins(self):
        assert classify_boundary_tier("Bed 12", "Bed 13")["mode"] == "bed"
        assert classify_boundary_tier("12a", "13a")["mode"] == "bed"
        assert classify_boundary_tier("23z", "23z")["mode"] == "bed"
        assert classify_boundary_tier("23z", "23z")["tier"] == TIER_STRICT
        # Mixed spellings of one bed column stay in bin space.
        assert classify_boundary_tier("Bed 12", "12")["mode"] == "bed"
        assert classify_boundary_tier("12", "Bed 12")["tier"] == TIER_STRICT

    def test_explicit_modes_keep_their_contracts(self):
        # mode="bed" documents bare indices as beds; mode="age" bare as ages.
        assert classify_boundary_tier("34", "36", mode="bed")["mode"] == "bed"
        assert classify_boundary_tier("34", "36", mode="bed")["direction"] == "down"
        assert classify_boundary_tier("34", "36", mode="age")["mode"] == "age"

    def test_tiered_report_default_mode_no_longer_inverts_numeric_data(self):
        gt = [_row("A", 34, 30)]
        pred = [_row("A", 36, 32)]  # whole range read OLDER = too low
        auto = tiered_eval_report(pred, gt, include_rows=True)  # default mode="auto"
        forced = tiered_eval_report(pred, gt, mode="age", include_rows=True)
        assert auto["error_typology"]["rows"][0]["label"] == "range_shift_down"
        assert forced["error_typology"]["rows"][0]["label"] == "range_shift_down"
        top = auto["boundary_tiers"]["fields"]["top"]
        assert top["directions"] == forced["boundary_tiers"]["fields"]["top"]["directions"]
        assert top["counts"] == forced["boundary_tiers"]["fields"]["top"]["counts"]
        assert top["directions"]["down"] == 1  # the top endpoint sits too LOW


# ---------------------------------------------------------------------------
# 3. Zero denominators are None ("n/a"), never a fake 0.0
# ---------------------------------------------------------------------------


class TestNotMeasuredSemantics:
    def test_empty_run_reports_none_not_zero(self):
        report = tiered_eval_report([], [])
        top = report["boundary_tiers"]["fields"]["top"]
        assert top["scored"] == 0
        assert top["rates"] == {t: None for t in BOUNDARY_TIERS}
        assert top["weighted_score"] is None
        assert report["boundary_tiers"]["row"]["weighted_score"] is None
        typ = report["error_typology"]
        assert typ["error_rate"] is None
        assert typ["rates"] == {label: None for label in typ["labels"]}
        tracks = report["tracks"]
        assert tracks["descriptive"]["score"] is None
        assert tracks["reasoning"]["score"] is None
        assert tracks["descriptive"]["components"]["taxon_label"]["score"] is None
        assert report["refusals"]["refusal_rate"] is None
        assert report["refusals"]["answer_coverage"] is None

    def test_measured_zero_count_ratios_stay_numeric(self):
        # 0 denominators -> None, but a MEASURED 0/1 stays 0.0: only the
        # undefined case changes meaning.
        gt = [_row("A", "Bed 5", "Bed 2")]
        m = boundary_tier_accuracy([_row("A", "Bed 6", "Bed 2")], gt, field="range_top")
        assert m["rates"][TIER_WRONG] == 0.0 and m["rates"][TIER_STRICT] == 0.0
        assert m["rates"][TIER_ADJACENT] == 1.0
        assert m["weighted_score"] == pytest.approx(0.6)

    def test_gold_report_renders_n_a_for_undefined_ratios(self):
        module = _load_gold_report()
        lines = module._borrow_metric_lines({
            "boundary_tiers": {"row": {
                "rates": {t: None for t in BOUNDARY_TIERS},
                "weighted_score": None,
            }},
            "tracks": {"descriptive": {"score": None}, "reasoning": {"score": None}},
            "refusals": {
                "refusal_field_present": True, "refusal_rate": None,
                "not_drawn_rate": None, "uncertain_rate": None,
                "precision_on_answered": None, "recall_on_answered": None,
            },
        })
        assert all("0.00" not in line for line in lines)
        assert any("weighted=n/a" in line and "strict=n/a" in line for line in lines)
        assert any("descriptive=n/a reasoning=n/a" in line for line in lines)
        assert any("refusal=n/a (not_drawn=n/a" in line for line in lines)

    def test_all_refused_dataset_is_annotated_not_padded(self):
        # A-6 optic: every row declined -> the legacy label-based trio can
        # still read f1=1.0; the *_on_answered trio moves to None and the
        # report says what happened.
        gt = [_row("A", "Bed 5", "Bed 2")]
        pred = [_row("A", response_kind="not_drawn")]
        m = refusal_metrics(pred, gt)
        assert m["all_rows_refused"] is True
        assert m["precision_on_answered"] is None
        assert m["recall_on_answered"] is None
        assert m["f1_on_answered"] is None
        assert "note" in m
        module = _load_gold_report()
        lines = module._borrow_metric_lines({
            "refusals": m, "tracks": {}, "boundary_tiers": {}, "error_typology": {},
        })
        assert any("label-only" in line and "P_answered=n/a" in line for line in lines)


# ---------------------------------------------------------------------------
# 4. Row-level refused/unscorable counts + canonical refusal spellings
# ---------------------------------------------------------------------------


class TestRowSummaryAndRefusalNormalization:
    def test_row_summary_counts_refused_and_unscorable(self):
        gt = [
            _row("A", "Bed 5", "Bed 2"),
            _row("B", "Bed 9", "Bed 7"),
            _row("C", "Bed 4", "Bed 1"),
        ]
        pred = [
            _row("A", "Bed 5", "Bed 2"),
            _row("B", response_kind="not_drawn"),
            _row("C", "see text", "unreadable prose"),
        ]
        m = range_tier_accuracy(pred, gt)
        assert m["row"]["refused"] == 1
        assert m["row"]["unscorable"] == 1
        assert m["row"]["scored"] == 1  # only A produced tier verdicts

    def test_refusal_row_counted_once_not_per_field(self):
        gt = [_row("A", "Bed 5", "Bed 2")]
        pred = [_row("A", response_kind="uncertain")]
        m = range_tier_accuracy(pred, gt)
        assert m["row"]["refused"] == 1
        # (per-field summaries count the same declination once per field —
        # that is their per-field viewpoint; the row unit is per row.)
        assert m["fields"]["range_top"]["refused"] == 1
        assert m["fields"]["range_base"]["refused"] == 1

    @pytest.mark.parametrize("spelling,expected", [
        ("not_drawn", "not_drawn"),
        ("Not Drawn", "not_drawn"),      # historical whitespace fold kept
        ("Not-Drawn", "not_drawn"),
        ("undrawn", "not_drawn"),        # canonical reason_codes aliases:
        ("blank", "not_drawn"),          # the private table used to miss
        ("dash", "not_drawn"),           # every one of these
        ("uncertain", "uncertain"),
        ("unclear", "uncertain"),
        ("unsure", "uncertain"),
        ("ambiguous", "uncertain"),
        ("extracted", None),
        ("made-up-token", None),         # unknowns are NOT guessed
        ("", None),
        (None, None),
    ])
    def test_response_kind_delegates_to_reason_codes_table(self, spelling, expected):
        assert _row_is_refusal({"response_kind": spelling}) == expected

    def test_typology_sees_the_blank_spelling_as_a_refusal(self):
        gt = [_row("A", "Bed 5", "Bed 2")]
        result = error_typology([_row("A", response_kind="blank")], gt)
        assert result["counts"]["omission"] == 0
        assert result["refused_rows"] == 1


# ---------------------------------------------------------------------------
# 5. gold_report timezone stamp (no deprecated utcnow)
# ---------------------------------------------------------------------------


class TestGoldReportTimestamp:
    def test_generated_timestamp_is_timezone_aware_utc(self):
        module = _load_gold_report()
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            html = module.generate_html({"cases": [], "summary": {}})
        assert "Generated:" in html
        assert ("+00:00" in html) or html.split("Generated:")[1].split("<")[0].rstrip().endswith("Z")

    def test_explicit_timestamp_still_wins(self):
        module = _load_gold_report()
        html = module.generate_html({"cases": [], "summary": {}, "timestamp": "T-STATIC"})
        assert "T-STATIC" in html


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    raise SystemExit(pytest.main([__file__, "-q"]))
