"""The detector parity fixture must be generated from the REAL shape module.

ARCH-REVIEW A11: a hand-maintained oracle rots silently. This test proves
tests/fixtures/detector_parity.json still matches rca_core.shape, so
tests/js/tests_detector_parity.js is comparing the browser against a real
Python answer rather than a stale file.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "detector_parity.json"


def test_fixture_is_current():
    result = subprocess.run(
        [sys.executable, "scripts/gen_detector_parity.py", "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fixture_covers_every_detector_and_key_edge_cases():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(data["pairs"]) == 6
    labels = {c["label"] for c in data["cases"]}
    for must in ("empty object", "abundance empty placeholder",
                 "generic table columns only",
                 "occurrence matrix gated taxon_rows",
                 "paleomap continents", "zonation zone rank rows"):
        assert must in labels, f"missing CSI: {must}"
    for case in data["cases"]:
        assert set(case["expected"]) == {js for js, _ in data["pairs"]}
