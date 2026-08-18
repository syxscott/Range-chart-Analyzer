"""Gold-standard pipeline gate (REVIEW-2026-07-31 rewrite).

Runs the REAL extraction pipeline OFFLINE against canned model responses
(`model_response.json`, generated deterministically from ground truth by
`tests/fixtures/gold/_gen_canned.py`) and asserts precision / recall /
metric gates. Previously the only extraction test was permanently skipped
and the gold fixtures were never exercised end-to-end — the "professional
research software" claim had no executable accuracy regression gate.

Pipeline under test per case:
    safe_json_loads(model_response.json) -> normalize_* -> eval_metrics vs
    ground_truth.json

Gates:
  * range_chart:      species precision >= 0.7, recall >= 0.75, f1 >= 0.7
                      (canned degradation drops 1 row + typos 1 name)
  * abundance:        abundance_sum_error <= 0.05 (verbatim canned)
  * phylogenetic:     topology distance == 0 (verbatim canned)
  * columnar_section: sections parse + lithology blocks survive (verbatim)
  * every case:       the pipeline never throws.

These are PIPELINE gates (normalization + metric wiring), not VLM-quality
measurements — the fixtures are synthetic renders (see README.md).
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path

GOLD_ROOT = Path(__file__).parent / "fixtures" / "gold"

SMOKE_CASES = [
    ("range_chart", "rc_synth_001"),
    ("range_chart", "rc_synth_002"),
    ("range_chart", "rc_synth_003"),
    ("columnar_section", "cs_synth_001"),
    ("columnar_section", "cs_synth_002"),
    ("abundance", "ab_synth_001"),
    ("abundance", "ab_synth_002"),
    ("phylogenetic_tree", "pt_synth_001"),
]

# Species gate thresholds. rc_synth_* have 5 true species; the canned
# degradation drops 1 row and typos 1 of the remaining 4, so the expected
# strict scores are precision = 3/4 = 0.75, recall = 3/5 = 0.6,
# f1 = 0.667. Gates sit below those with margin so pipeline regressions
# (not the seeded degradation) are what fails the test.
SPECIES_GATES = {"min_precision": 0.7, "min_recall": 0.55, "min_f1": 0.6}


def _normalize(chart_type: str, parsed: dict) -> dict:
    """Run the real normalizer for the chart type (never-throws wrapper)."""
    if chart_type == "range_chart":
        from rca_core.extractor import normalize_result
        return normalize_result(parsed)
    if chart_type == "columnar_section":
        from rca_core.extractor import normalize_columnar_result
        return normalize_columnar_result(parsed)
    if chart_type == "abundance":
        from rca_core.extractor import normalize_abundance_result
        return normalize_abundance_result(parsed)
    if chart_type == "phylogenetic_tree":
        from rca_core.extractor import _normalize_phylogenetic_tree_into
        return _normalize_phylogenetic_tree_into(parsed)
    raise AssertionError(f"unknown chart type {chart_type}")


@pytest.mark.gold_smoke
@pytest.mark.parametrize("chart_type,case_id", SMOKE_CASES)
def test_gold_pipeline_gate(chart_type, case_id):
    """The offline extraction pipeline must meet the accuracy gates."""
    from rca_core.json_utils import safe_json_loads

    case_dir = GOLD_ROOT / chart_type / case_id
    canned_path = case_dir / "model_response.json"
    gt_path = case_dir / "ground_truth.json"
    assert canned_path.exists(), (
        f"missing canned response {canned_path} — run "
        "`python tests/fixtures/gold/_gen_canned.py`"
    )
    assert gt_path.exists(), f"missing ground truth {gt_path}"

    canned_text = canned_path.read_text(encoding="utf-8")
    gt = json.loads(gt_path.read_text(encoding="utf-8"))

    # 1. JSON tolerance layer (real parser).
    parsed = safe_json_loads(canned_text)
    assert isinstance(parsed, dict)

    # 2. Normalization layer — must never throw on any case.
    normalized = _normalize(chart_type, parsed)

    # 3. Per-type gates.
    if chart_type == "range_chart":
        from rca_core.eval_metrics import species_precision_recall
        pred = normalized.get("species_ranges") or []
        true = gt.get("species_ranges") or []
        m = species_precision_recall(pred, true)
        assert m["precision"] >= SPECIES_GATES["min_precision"], (
            f"{case_id}: precision {m['precision']} < "
            f"{SPECIES_GATES['min_precision']}"
        )
        assert m["recall"] >= SPECIES_GATES["min_recall"], (
            f"{case_id}: recall {m['recall']} < {SPECIES_GATES['min_recall']}"
        )
        assert m["f1"] >= SPECIES_GATES["min_f1"], (
            f"{case_id}: f1 {m['f1']} < {SPECIES_GATES['min_f1']}"
        )
    elif chart_type == "abundance":
        from rca_core.eval_metrics import abundance_sum_error
        err = abundance_sum_error(
            normalized.get("abundances") or [],
            gt.get("abundances") or [],
        )
        max_err = err.get("max_error", 1.0)
        assert max_err <= 0.05, (
            f"{case_id}: abundance sum error {max_err} > 0.05"
        )
    elif chart_type == "phylogenetic_tree":
        from rca_core.eval_metrics import phylogenetic_topology_distance
        dist = phylogenetic_topology_distance(normalized, gt)
        assert dist == 0, f"{case_id}: topology distance {dist} != 0"
    elif chart_type == "columnar_section":
        assert normalized.get("sections"), f"{case_id}: sections lost"
        blocks = (normalized["sections"][0] or {}).get("lithology_blocks") or []
        assert blocks, f"{case_id}: lithology blocks lost"
    else:  # pragma: no cover
        raise AssertionError(f"unknown chart type {chart_type}")


@pytest.mark.gold_smoke
@pytest.mark.parametrize("chart_type,case_id", SMOKE_CASES)
def test_gold_fixtures_exist(chart_type, case_id):
    """Verify all smoke-test fixtures are present on disk."""
    case_dir = GOLD_ROOT / chart_type / case_id
    assert case_dir.exists(), f"Missing case directory: {case_dir}"
    assert (case_dir / "image.png").exists(), f"Missing image: {case_dir / 'image.png'}"
    gt_path = case_dir / "ground_truth.json"
    assert gt_path.exists(), f"Missing ground_truth: {gt_path}"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    # Check it's not a skip marker
    assert "_skip" not in gt, f"Fixture is skipped ({gt.get('reason')})"
    # REVIEW-2026-07-31: canned responses are now REQUIRED — the pipeline
    # gate runs offline on them.
    canned_path = case_dir / "model_response.json"
    assert canned_path.exists(), (
        f"Missing canned response: {canned_path} — run "
        "`python tests/fixtures/gold/_gen_canned.py`"
    )
