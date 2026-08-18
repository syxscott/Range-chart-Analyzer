"""Generate canned model responses for the gold fixtures (REVIEW-2026-07-31).

The gold gate in tests/test_gold_smoke.py runs the REAL extraction pipeline
offline: each case directory gets a `model_response.json` — a deterministic,
seeded DEGRADATION of `ground_truth.json` — which stands in for an imperfect
VLM reply. The pipeline (safe_json_loads -> normalize_* -> eval_metrics)
must extract it back to at least the gated precision/recall.

Degradations are small and deterministic:
  * range_chart: drop the LAST species row (recall < 1) and typo the FIRST
    species name (precision < 1).
  * columnar_section / abundance / phylogenetic_tree: pass the ground truth
    through verbatim (gates = pipeline correctness + metric wiring).

Re-run with:  python tests/fixtures/gold/_gen_canned.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

GOLD_ROOT = Path(__file__).parent

CASES = [
    ("range_chart", "rc_synth_001"),
    ("range_chart", "rc_synth_002"),
    ("range_chart", "rc_synth_003"),
    ("columnar_section", "cs_synth_001"),
    ("columnar_section", "cs_synth_002"),
    ("abundance", "ab_synth_001"),
    ("abundance", "ab_synth_002"),
    ("phylogenetic_tree", "pt_synth_001"),
]


def _typo_epithet(epithet: str) -> str:
    """Deterministic single-letter substitution guaranteed to change the name."""
    for old, new in (("a", "e"), ("e", "a"), ("i", "o"), ("o", "u")):
        if old in epithet:
            return epithet.replace(old, new, 1)
    return epithet + "x"


def _degrade_range_chart(gt: dict) -> dict:
    out = json.loads(json.dumps(gt))  # deep copy
    rows = out.get("species_ranges") or []
    if rows:
        # Drop the LAST row (recall loss).
        rows.pop()
    if rows:
        # Typo the FIRST remaining species name (precision loss).
        first = rows[0]
        name = first.get("species", "")
        if name:
            parts = name.split(" ", 1)
            if len(parts) == 2 and parts[1]:
                first["species"] = parts[0] + " " + _typo_epithet(parts[1])
            else:
                first["species"] = _typo_epithet(name)
    return out


def _degrade_verbatim(gt: dict) -> dict:
    return json.loads(json.dumps(gt))


def main() -> int:
    n = 0
    for chart_type, case_id in CASES:
        case_dir = GOLD_ROOT / chart_type / case_id
        gt_path = case_dir / "ground_truth.json"
        if not gt_path.exists():
            print(f"skip (no ground_truth): {chart_type}/{case_id}")
            continue
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        if chart_type == "range_chart":
            canned = _degrade_range_chart(gt)
        else:
            canned = _degrade_verbatim(gt)
        out_path = case_dir / "model_response.json"
        out_path.write_text(
            json.dumps(canned, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        n += 1
        print(f"wrote {out_path.relative_to(GOLD_ROOT.parent.parent.parent)}")
    print(f"{n} canned responses generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
