# Gold-Standard Test Set

This directory contains expert-annotated paleontology range charts used to
measure extraction accuracy. Each case has:
- `image.png` — the chart image
- `ground_truth.json` — expert-verified extraction
- `source.json` — citation, license, annotator

## Current Coverage

| Type | Real | Synthetic | Total |
|---|---|---|---|
| range_chart | 0 | 3 | 3 |
| columnar_section | 0 | 2 | 2 |
| abundance | 0 | 2 | 2 |
| phylogenetic_tree | 0 | 1 | 1 |
| **Total** | **0** | **8** | **8** |

## CI Usage

- `pytest -m gold_smoke` — 5 quick cases, every PR
- `pytest -m gold` — all 8 cases, weekly cron + release tag
- `RCA_OFFLINE_GOLD=1` — use cached responses, zero API cost

## Adding New Cases

See ANNOTATION_GUIDE.md for instructions.
