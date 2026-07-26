"""Smoke test for gold-standard fixtures.

Runs a subset of synthetic cases to verify the extraction pipeline
doesn't crash. Use:

    pytest tests/test_gold_smoke.py -m gold_smoke
    pytest tests/test_gold_smoke.py -m gold_smoke --rca-offline

Note: These tests require a real LLM API key and will fail without one.
They are marked with @pytest.mark.skip by default — remove the skip
marker when you have API credentials configured.
"""
from __future__ import annotations

import base64
import json
import pytest
from pathlib import Path

GOLD_ROOT = Path(__file__).parent / "fixtures" / "gold"

SMOKE_CASES = [
    ("range_chart", "rc_synth_001"),
    ("columnar_section", "cs_synth_001"),
    ("abundance", "ab_synth_001"),
    ("phylogenetic_tree", "pt_synth_001"),
]


def _get_extractor(chart_type):
    """Lazily import extractor to avoid heavy import when not needed."""
    from rca_core.extractor import (
        extract_range_chart,
        extract_columnar_section,
        extract_abundance_diagram,
        extract_phylogenetic_tree,
    )
    return {
        "range_chart": extract_range_chart,
        "columnar_section": extract_columnar_section,
        "abundance": extract_abundance_diagram,
        "phylogenetic_tree": extract_phylogenetic_tree,
    }[chart_type]


@pytest.mark.gold_smoke
@pytest.mark.skip(reason="Requires live LLM API — enable when API key is configured")
@pytest.mark.parametrize("chart_type,case_id", SMOKE_CASES)
def test_synthetic_gold_smoke(chart_type, case_id, rca_offline_gold):
    """Smoke test: each case should extract without crashing.

    This is a smoke test only — it checks the pipeline doesn't error out,
    not that the result is correct (that requires the metric tests).
    """
    case_dir = GOLD_ROOT / chart_type / case_id
    if not case_dir.exists():
        pytest.skip(f"Case not found: {case_dir}")

    img_bytes = (case_dir / "image.png").read_bytes()
    image_b64 = base64.b64encode(img_bytes).decode()

    # Check ground truth exists
    gt_path = case_dir / "ground_truth.json"
    if not gt_path.exists():
        pytest.skip(f"Ground truth not found: {gt_path}")
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    if gt.get("_skip"):
        pytest.skip(f"Synthetic data unavailable: {gt.get('reason')}")

    extract_fn = _get_extractor(chart_type)

    # When offline mode is active, skip if no API key
    if rca_offline_gold:
        pytest.skip("RCA_OFFLINE_GOLD=1 — skipping live API call")

    result = extract_fn(
        api_key="test-key",
        image_b64=image_b64,
        media_type="image/png",
        runs=1,
    )

    # The extraction should complete (ok or not ok — we just check it didn't crash)
    assert hasattr(result, "ok"), f"Unexpected result type: {type(result)}"


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


@pytest.mark.gold_smoke
def test_gold_smoke_count():
    """Verify the expected number of smoke cases are registered."""
    assert len(SMOKE_CASES) == 4, f"Expected 4 smoke cases, got {len(SMOKE_CASES)}"
