"""FIX-2026-09-22 regression tests — extraction-contract domain (agent A2).

Covers, on the Python side (the JS mirrors are covered by
``tests_contract_mirror_2026_09_22.js``):

1. extractor: zero-span / half-broken root ``axis_calibration`` is
   UNUSABLE — never hoisted as ``calibrated`` truth, warned, raw block kept
   under ``_extras`` (audit items 4 + 7);
2. extractor: the chemical ``top_/base_pos_0_999`` semantic pairing feeds the
   15% contradiction check (audit item 5);
3. extractor: ``bool`` rejection in the position parsers (audit item 8) and
   ``normalize_codes`` dict semantics — an explicitly-false value DENIES the
   code, other values keep the legacy claim behaviour;
4. extractor: the deskew identity check — a no-op deskew keeps the
   original-bytes fast path (audit item 9);
5. extractor: additive row warnings — ``response_kind_conflict`` and
   ``index_order_swap`` (and the iron-rule flag) coexist (audit item 2);
6. aggregate: a divergent ``response_kind`` merge PRESERVES a pre-existing
   zone/index warning — the py==js shape guarantee;
7. report vs quality: the ledger parity fix — the audit report and the
   quality score grade the SAME table with the SAME column ladder, so a
   scatter/chemical payload keyed by ``sample_id`` / ``label`` no longer
   reports ``cells: 0`` beside ``cells: 2`` (audit item 6);
8. report: ``lang="zh"`` localizes the decision glosses through
   rca_core/i18n.py while the slugs and the no-lang shape stay untouched.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rca_core import extractor as E  # noqa: E402
from rca_core.aggregate import merge_results  # noqa: E402
from rca_core.quality import coverage_for, select_coverage_table  # noqa: E402
from rca_core.reason_codes import (  # noqa: E402
    LOW_CONFIDENCE_CODE,
    normalize_codes,
)
from rca_core.report import build_extraction_report  # noqa: E402


# ---------------------------------------------------------------------------
# 1 + 4. unusable root axis_calibration (zero span / half-broken)
# ---------------------------------------------------------------------------

def _range_chart_with_pos(axis_calibration):
    return {
        "sections": [{"name": "S1"}],
        "species_ranges": [
            {"species": "A", "section": "S1", "range_top": "9",
             "range_base": "7",
             "range_top_pos_0_999": 347, "range_base_pos_0_999": 261},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8,
        "axis_calibration": axis_calibration,
    }


class TestAxisCalibrationUnusable:
    def test_zero_span_is_unusable_never_calibrated(self):
        # audit item 4: {at_0: 0, at_999: 0} used to convert EVERY position
        # to 0.0 with calibrated: true and skip both contradiction guards.
        out = E.normalize_result(_range_chart_with_pos(
            {"vertical": {"at_0": 0, "at_999": 0}}))
        assert "axis_calibration" not in out, "an unusable block is not hoisted"
        assert "axis_calibration_unusable" in out.get("_warnings", [])
        # audit item 7: the raw claim survives for the operator under _extras.
        assert out["_extras"]["axis_calibration"] == \
            {"vertical": {"at_0": 0, "at_999": 0}}
        row = out["species_ranges"][0]
        geo = row["geometry"]
        assert geo["calibrated"] is False, "a zero span carries no information"
        for point in geo["points"].values():
            assert "value" not in point, "no engineered value from a dead span"
            assert point["pos"] in (347, 261), "the raw 0-999 evidence stays"
        assert LOW_CONFIDENCE_CODE in row["reason_codes"]

    def test_half_broken_axis_is_unusable(self):
        # only one numeric end -> nothing survives _axis_domain -> unusable.
        out = E.normalize_result(_range_chart_with_pos(
            {"vertical": {"at_0": 1}}))
        assert "axis_calibration" not in out
        assert "axis_calibration_unusable" in out.get("_warnings", [])
        assert "axis_calibration" in out.get("_extras", {})

    def test_usable_calibration_is_hoisted(self):
        out = E.normalize_result(_range_chart_with_pos(
            {"vertical": {"at_0": 0.0, "at_999": 25.0, "unit": "m"}}))
        assert out["axis_calibration"]["vertical"]["at_999"] == 25.0
        assert "axis_calibration_unusable" not in out.get("_warnings", [])
        assert "axis_calibration" not in out.get("_extras", {})
        row = out["species_ranges"][0]
        assert row["geometry"]["calibrated"] is True
        # pos 347 on [0, 25] -> 347/999*25 = 8.6837; range_top "9":
        # |8.68-9| < 15% of 25, so the point keeps its converted value.
        assert row["geometry"]["points"]["range_top_pos_0_999"]["value"] == \
            pytest.approx(347 * 25.0 / 999.0, abs=1e-6)

    def test_absent_calibration_changes_nothing(self):
        parsed = _range_chart_with_pos(None)
        del parsed["axis_calibration"]
        out = E.normalize_result(parsed)
        assert "axis_calibration" not in out
        assert "axis_calibration_unusable" not in out.get("_warnings", [])
        assert "_extras" not in out  # legacy key set, byte for byte


# ---------------------------------------------------------------------------
# 2. chemical top/base semantic pairing (audit item 5)
# ---------------------------------------------------------------------------

class TestChemicalSemanticPairing:
    def _axes(self):
        return {"vertical": {"at_0": 0.0, "at_999": 10.0, "unit": ""}}

    def test_top_pos_contradicting_transcribed_depth_is_rejected(self):
        # pos 500 on [0, 10] -> 5.0; top_depth_m 1.0: |5.0 - 1.0| = 4.0 >
        # 15% of 10 = 1.5 -> rejected. Before the pairing existed the point
        # was accepted silently (the guard never ran for this mode).
        entry, bad, _axis = E._geometry_point(
            "top_pos_0_999", 500, {"top_depth_m": 1.0}, self._axes())
        assert bad is True
        assert entry is None or "value" not in entry

    def test_base_pos_contradiction_check_also_runs(self):
        entry, bad, _axis = E._geometry_point(
            "base_pos_0_999", 100, {"base_age_ma": 5.0}, self._axes())
        assert bad is True
        assert entry is None or "value" not in entry

    def test_agreeing_pair_survives_with_value(self):
        # pos 400 on [0, 10] -> 4.004; top_depth_m 4.0 is inside the 15% tol.
        entry, bad, _axis = E._geometry_point(
            "top_pos_0_999", 400, {"top_depth_m": 4.0}, self._axes())
        assert bad is False
        assert entry["value"] == pytest.approx(400 * 10.0 / 999.0, abs=1e-6)

    def test_semantic_table_lists_both_pairs(self):
        assert "top_depth_m" in E._GEOMETRY_SEMANTIC_KEYS["top_pos_0_999"]
        assert "base_age_ma" in E._GEOMETRY_SEMANTIC_KEYS["base_pos_0_999"]


# ---------------------------------------------------------------------------
# 3. bool rejection + normalize_codes dict semantics (audit item 8)
# ---------------------------------------------------------------------------

class TestValueSemantics:
    def test_bool_is_never_a_position(self):
        assert E.normalize_pos_0_999(True) is None
        assert E.normalize_pos_0_999(False) is None

    def test_bool_never_maps_to_an_axis_value(self):
        domain = {"at_0": 0.0, "at_999": 10.0, "unit": ""}
        assert E.pos_to_axis_value(True, domain) is None
        assert E.pos_to_axis_value(False, domain) is None

    def test_row_bool_pos_is_rejected_not_mapped_as_one(self):
        out = E.normalize_result({
            "sections": [{"name": "S1"}],
            "species_ranges": [{"species": "A", "section": "S1",
                                "range_top": "9",
                                "range_top_pos_0_999": True}],
            "biozones": [], "other_fossils": [], "confidence": 0.8,
        })
        row = out["species_ranges"][0]
        assert "range_top_pos_0_999" not in row.get("geometry", {}).get(
            "points", {}), "true must not become position 1"
        assert LOW_CONFIDENCE_CODE in row["reason_codes"]

    def test_false_denies_the_code_other_values_do_not(self):
        assert normalize_codes({"obscured": False}) == []
        assert normalize_codes({"obscured": True}) == ["obscured"]
        # 0 / "" / None are not claims either way -> legacy pass-through kept.
        assert normalize_codes({"obscured": 0}) == ["obscured"]
        assert normalize_codes({"obscured": ""}) == ["obscured"]
        assert normalize_codes({"obscured": None}) == ["obscured"]


# ---------------------------------------------------------------------------
# 4. deskew identity (audit item 9)
# ---------------------------------------------------------------------------

class TestDeskewIdentity:
    def test_noop_deskew_keeps_the_original_bytes(self, tmp_path, monkeypatch):
        from PIL import Image

        import rca_core.deskew as deskew_mod

        img_path = tmp_path / "chart.png"
        Image.new("RGB", (20, 30), "white").save(img_path)
        raw = img_path.read_bytes()

        def fake_deskew(im, *a, **kw):
            return im, 0.0  # SAME object, exactly like the real min_angle path

        monkeypatch.setattr(deskew_mod, "deskew_image", fake_deskew)
        b64, mime, w, h, resized, err = E.load_image_b64(
            str(img_path), max_edge=0, enhance=False, deskew=True)
        assert err is False
        assert resized is False, "a no-op deskew must not flag `modified`"
        assert base64.b64decode(b64) == raw, "original-bytes fast path kept"

    def test_real_rotation_marks_modified(self, tmp_path, monkeypatch):
        from PIL import Image

        import rca_core.deskew as deskew_mod

        img_path = tmp_path / "chart.png"
        Image.new("RGB", (20, 30), "white").save(img_path)
        raw = img_path.read_bytes()

        def rotating_deskew(im, *a, **kw):
            return im.rotate(1.0, expand=True), 1.0

        monkeypatch.setattr(deskew_mod, "deskew_image", rotating_deskew)
        b64, mime, w, h, resized, err = E.load_image_b64(
            str(img_path), max_edge=0, enhance=False, deskew=True)
        assert err is False
        # A real rotation returns a NEW object -> `modified` -> re-encoded,
        # i.e. the uploaded bytes are NOT the (untouched) original file.
        assert base64.b64decode(b64) != raw
        Image.open(io.BytesIO(base64.b64decode(b64))).verify()


# ---------------------------------------------------------------------------
# 5 + 6. warning preservation, py == js shape
# ---------------------------------------------------------------------------

def _run(row):
    return {"sections": [{"name": "S1"}], "species_ranges": [row],
            "biozones": [], "other_fossils": [], "confidence": 0.8}


class TestWarningMerge:
    def test_conflict_and_order_swap_coexist_on_one_row(self):
        out = E.normalize_result({
            "sections": [{"name": "S1"}],
            "species_ranges": [{"species": "A", "section": "S1",
                                "range_top": "9", "range_base": "7",
                                "range_top_idx": 3, "range_base_idx": 8,
                                "response_kind": "not_drawn",
                                "reason_codes": ["not_drawn"]}],
            "biozones": [], "other_fossils": [], "confidence": 0.8,
        })
        row = out["species_ranges"][0]
        # not_drawn + a readable value -> extracted + response_kind_conflict,
        # AND the index repair still flags (audit item 2 family: additive).
        assert row["response_kind"] == "extracted"
        assert set(row["_warning"]) == {"response_kind_conflict",
                                        "index_order_swap"}

    def test_divergent_merge_preserves_zone_warning(self):
        # The merge layer's job: a row that already carries a warning keeps it
        # beside response_kind_divergent — exactly what js/aggregate.js
        # rcaAddRowWarning writes (same single-string/list convention).
        merged = merge_results([
            _run({"species": "A", "section": "S1", "range_top": "9",
                  "response_kind": "extracted", "reason_codes": ["crosses_top"],
                  "_warning": "iron_rule_zone_label"}),
            _run({"species": "A", "section": "S1",
                  "response_kind": "not_drawn",
                  "reason_codes": ["not_drawn"]}),
        ], total_runs=2)
        row = merged["species_ranges"][0]
        assert row["response_kind"] == "extracted"  # priority vote
        warnings = row["_warning"]
        assert isinstance(warnings, list) and len(warnings) == 2
        assert "iron_rule_zone_label" in warnings
        assert "response_kind_divergent" in warnings
        assert set(row["reason_codes"]) == {"crosses_top", "not_drawn"}

    def test_single_warning_stays_a_bare_string_in_both_engines(self):
        merged = merge_results([
            _run({"species": "A", "section": "S1", "range_top": "9",
                  "response_kind": "extracted"}),
            _run({"species": "A", "section": "S1",
                  "response_kind": "not_drawn"}),
        ], total_runs=2)
        # exactly ONE added flag on a warning-free row -> string, not [string]
        # (the js mirror rcaAddRowWarning follows the same rule).
        assert merged["species_ranges"][0]["_warning"] == \
            "response_kind_divergent"


# ---------------------------------------------------------------------------
# 7. report vs quality ledger parity (audit item 6) + 8. localization
# ---------------------------------------------------------------------------

def _chemical_payload():
    # Two samples keyed ONLY by sample_id: the pre-fix report called
    # coverage_ledger with the DEFAULT (species/taxon/name) ladder -> cells: 0
    # while quality graded cells: 2 beside it in the same document.
    return {
        "metadata": {},
        "data_points": [
            {"sample_id": "S-1", "depth_m": "1.0", "values": {"d13c": 1.2},
             "response_kind": "extracted"},
            {"sample_id": "S-2", "depth_m": "2.0", "values": {"d13c": 0.4},
             "response_kind": "not_drawn", "reason_codes": ["not_drawn"]},
        ],
        "events": [], "intervals": [], "confidence": 0.7,
    }


def _scatter_payload():
    return {
        "metadata": {},
        "groups": [], "statistics": {},
        "points": [
            {"label": "P1", "x": "1", "y": "2", "response_kind": "extracted"},
            {"label": "P2", "x": "3", "y": "4",
             "response_kind": "uncertain", "reason_codes": ["no_label"]},
        ],
        "outliers": [], "confidence": 0.6,
    }


@pytest.mark.parametrize("factory", [_chemical_payload, _scatter_payload])
class TestReportQualityLedgerParity:
    def _report(self, data):
        return build_extraction_report(
            data=data, mode="chemical_stratigraphy",
            mode_used="chemical_stratigraphy", image_sha256="ab", runs=1)

    def test_table_pick_agrees(self, factory):
        data = factory()
        picked = select_coverage_table(data)
        report = self._report(data)
        assert picked is not None
        assert report["coverage"]["table"] == picked[0]

    def test_ledgers_are_identical(self, factory):
        data = factory()
        report = self._report(data)
        q = coverage_for(data)
        assert q is not None and report["coverage"], "ledger missing entirely"
        assert report["coverage"]["ledger"] == q, \
            "the audit and the score must grade the same grid"
        assert q["totals"]["cells"] == 2, \
            "two sample_id/label columns — the old report said 0"

    def test_rollup_denominators_agree(self, factory):
        data = factory()
        report = self._report(data)
        cov = report["coverage"]
        assert cov["contracted_rows"] == 2
        # by_response_kind must mirror the quality ledger's per-state counts.
        ledger = cov["ledger"]
        states = ("extracted", "not_drawn", "uncertain")
        assert sum(ledger["totals"][s] for s in states) == 2
        assert sum(cov["by_response_kind"].values()) == 2

    def test_evidence_chain_is_not_empty_dict(self, factory):
        # The known regression this round FOUND: the tuple-arity slip made
        # EVERY coverage block collapse to '{}' through the blanket guard.
        data = factory()
        report = self._report(data)
        text = json.dumps(report["coverage"], ensure_ascii=False)
        assert text != "{}", "the coverage refactor regressed the evidence chain"


class TestReportLocalization:
    def _row(self):
        return {
            "sections": [{"name": "S1"}],
            "species_ranges": [{"species": "A", "section": "S1",
                                "response_kind": "not_drawn",
                                "reason_codes": ["not_drawn"]}],
            "biozones": [], "other_fossils": [], "confidence": 0.9,
        }

    def _report(self, **kw):
        return build_extraction_report(
            data=self._row(), mode="range_chart", mode_used="range_chart",
            image_sha256="ab", runs=1, **kw)

    def test_no_lang_keeps_the_english_gloss(self):
        entry = self._report()["coverage"]["decisions"][0]
        assert entry["reason_codes"] == ["not_drawn"]
        assert "not drawn" in entry["code_summaries"][0].lower()

    def test_zh_localizes_the_gloss_slugs_stay(self):
        entry = self._report(lang="zh")["coverage"]["decisions"][0]
        assert entry["reason_codes"] == ["not_drawn"]  # machine field untouched
        assert entry["code_summaries"][0] != "not drawn"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in entry["code_summaries"][0])

    def test_en_lang_is_a_no_op(self):
        assert self._report(lang="en")["coverage"]["decisions"] == \
            self._report()["coverage"]["decisions"]
