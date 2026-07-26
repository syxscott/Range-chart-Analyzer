"""Generate all synthetic gold-standard fixtures.

Run once to populate tests/fixtures/gold/ with 8 synthetic cases.
"""
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.fixtures.synthetic_data import (
    gen_range_chart,
    gen_columnar_section,
    gen_abundance_diagram,
    gen_phylogenetic_tree,
)

GOLD_ROOT = Path(__file__).parent.parent / "tests" / "fixtures" / "gold"

CASES = [
    ("range_chart", "rc_synth_001", gen_range_chart, 0),
    ("range_chart", "rc_synth_002", gen_range_chart, 1),
    ("range_chart", "rc_synth_003", gen_range_chart, 2),
    ("columnar_section", "cs_synth_001", gen_columnar_section, 10),
    ("columnar_section", "cs_synth_002", gen_columnar_section, 11),
    ("abundance", "ab_synth_001", gen_abundance_diagram, 20),
    ("abundance", "ab_synth_002", gen_abundance_diagram, 21),
    ("phylogenetic_tree", "pt_synth_001", gen_phylogenetic_tree, 30),
]


def main():
    print("Generating synthetic gold-standard fixtures...")
    generated = 0
    skipped = 0

    for chart_type, case_id, gen_fn, seed in CASES:
        case_dir = GOLD_ROOT / chart_type / case_id
        case_dir.mkdir(parents=True, exist_ok=True)

        img_bytes, gt = gen_fn(seed=seed)

        if gt.get("_skip"):
            print(f"  ~ {chart_type}/{case_id} — SKIPPED ({gt['reason']})")
            skipped += 1
            continue

        (case_dir / "image.png").write_bytes(img_bytes)
        (case_dir / "ground_truth.json").write_text(
            json.dumps(gt, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"  [OK] {chart_type}/{case_id}")
        generated += 1

    # Update _index.json
    index_path = GOLD_ROOT / "_index.json"
    index_data = {
        "version": "1.0.0",
        "description": "Gold-standard test set index",
        "cases": [
            {
                "type": ct,
                "id": cid,
                "synthetic": True,
                "seed": seed,
            }
            for ct, cid, _, seed in CASES
        ],
    }
    index_path.write_text(json.dumps(index_data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone: {generated} generated, {skipped} skipped (matplotlib unavailable).")
    if generated > 0:
        print(f"Fixtures written to: {GOLD_ROOT}")
    return 0 if generated > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
