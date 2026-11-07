#!/usr/bin/env python3
"""Generate / verify the JS<->Python shape-detector parity fixture.

ARCH-REVIEW-2026-11-02 A11. rca_core/shape.py is now the ONE Python
implementation of six shape decisions; js/table.js holds six named mirrors.
Cross-language code cannot be shared, so it must be LOCKED. This generator
computes the Python oracle from the real module (not hand-written booleans)
and stores both the payloads and the answers in a JSON fixture that
tests/js/tests_detector_parity.js reads.

    python scripts/gen_detector_parity.py            # write the fixture
    python scripts/gen_detector_parity.py --check   # CI: fail if stale
    node tests/js/tests_detector_parity.js          # compare the JS mirrors

The CSIs are deliberate: empty `abundances` (initialised placeholder),
generic_table's `columns` (the collision that once deleted a whole export),
gated occurrence matrices (empty occurrences BY DESIGN), and non-dict first
rows. Any single one of those has previously produced a real divergence.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "detector_parity.json"
sys.path.insert(0, str(ROOT))

from rca_core import shape  # noqa: E402

#: JS function name -> Python function name. rcaLooksColumnarEmptySections
#: is JS-only (I7: columnar schema declared with no readable columns) and
#: is deliberately excluded from the cross-language comparison.
PAIRS = (
    ("rcaLooksColumnar", "looks_columnar"),
    ("rcaLooksAbundance", "looks_abundance"),
    ("rcaLooksPhylogeneticTree", "looks_phylogenetic_tree"),
    ("rcaLooksZonationChart", "looks_zonation_chart"),
    ("rcaLooksPaleomap", "looks_paleomap"),
    ("rcaLooksOccurrenceMatrix", "looks_occurrence_matrix"),
)

CASES: list[tuple[str, object]] = [
    ("null", None),
    ("empty object", {}),
    ("plain range chart", {"species_ranges": [{"species": "A"}]}),
    ("columnar id sections", {"sections": [{"id": "C1", "top": 10}]}),
    ("range-chart name sections", {"sections": [{"name": "Measured A"}]}),
    ("columnar empty sections", {"sections": []}),
    ("columnar first row not dict", {"sections": ["oops"]}),
    ("abundance empty placeholder", {"abundances": []}),
    ("abundance populated", {"abundances": [{"taxon": "A", "pct": 20}]}),
    ("abundances not a list", {"abundances": "nonsense"}),
    ("phylo nodes id+parent", {"nodes": [{"id": "n1", "parent": None}]}),
    ("phylo nodes missing parent", {"nodes": [{"id": "n1"}]}),
    ("phylo nodes empty", {"nodes": []}),
    ("zonation correlations", {"correlations": [{"from_zone": "A"}]}),
    ("zonation zone rank rows", {"zones": [{"rank": 1, "name": "Z1"}]}),
    ("zonation zonation descriptor", {"zones": [{"zonation": "A"}]}),
    ("zonation descriptors only", {"zonations": [{"name": "A"}], "zones": []}),
    ("paleomap continents", {"continents": [{"name": "Pangea"}]}),
    ("paleomap empty fossil_sites", {"fossil_sites": []}),
    ("paleomap first row not dict", {"continents": ["oops"]}),
    ("occurrence matrix occurrences",
     {"occurrences": [{"taxon": "A", "locality": "L1"}], "columns": []}),
    ("occurrence matrix gated taxon_rows",
     {"occurrences": [], "taxon_rows": [{"taxon": "A"}], "columns": []}),
    ("generic table columns only",
     {"columns": [{"name": "a"}], "rows": [["1"]]}),
    ("mixed abundance and zonation",
     {"abundances": [{"taxon": "A"}], "zones": [{"rank": 1}]}),
]


def build() -> dict:
    rows = []
    for label, payload in CASES:
        expected = {
            js_name: bool(getattr(shape, py_name)(payload))
            for js_name, py_name in PAIRS
        }
        rows.append({"label": label, "payload": payload, "expected": expected})
    return {"schema_version": 1, "pairs": [list(p) for p in PAIRS],
            "cases": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="do not write; fail if the committed fixture is stale")
    args = ap.parse_args()
    data = build()
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not FIXTURE.is_file():
            print(f"MISSING fixture: {FIXTURE}", file=sys.stderr)
            return 1
        if FIXTURE.read_text(encoding="utf-8") != encoded:
            print("STALE detector parity fixture: run "
                  "python scripts/gen_detector_parity.py", file=sys.stderr)
            return 1
        print(f"detector parity fixture up to date ({len(CASES)} cases)")
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(encoded, encoding="utf-8")
    print(f"wrote {FIXTURE} ({len(CASES)} cases, {len(PAIRS)} detectors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
