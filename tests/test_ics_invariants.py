"""Data-integrity invariants for the bundled ICS table.

Sprint A of CODE_REVIEW_2026-09-01: the audit found ~20 wrong boundaries
and 4 internal overlaps/gaps in ics_2024.json while all 819 logic tests
stayed green — the suite tested the CODE, never the DATA. These
invariants close that gap for good:

  1. every entry has base_ma > top_ma (older > younger);
  2. period bounds tile the Phanerozoic with NO gaps and NO overlaps;
  3. every entry's period_base_ma / period_top_ma match its period;
  4. no orphan boundaries — every top/base mates with a neighbouring
     entry or a period boundary (duplicate alias entries such as
     Wuliuan/"Stage 5" and Jiangshanian/"Stage 9" are tolerated);
  5. scientific anchors: P-T boundary 251.902, K-Pg 66.0, base
     Quaternary 2.58, base Cretaceous 143.1 (ICS v2024/12);
  6. numeric-age lookups land in the correct stage;
  7. js/ics_table.js mirrors the JSON exactly (double-ended parity).

Reference: https://stratigraphy.org/ICSchart/ChronostratChart2024-12.pdf
"""

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ICS_PATH = _ROOT / "rca_core" / "resources" / "ics_2024.json"
_JS_PATH = _ROOT / "js" / "ics_table.js"

_EPS = 1e-6


@pytest.fixture(scope="module")
def ics():
    return json.loads(_ICS_PATH.read_text(encoding="utf-8"))


def _period_bounds(ics):
    bounds = {}
    for info in ics.values():
        period = info["period"]
        pair = (info["period_base_ma"], info["period_top_ma"])
        bounds.setdefault(period, pair)
        assert bounds[period] == pair, (
            f"period {period} has inconsistent bounds: {bounds[period]} vs {pair}"
        )
    return bounds


class TestFieldSanity:
    def test_base_older_than_top(self, ics):
        for name, info in ics.items():
            assert info["base_ma"] > info["top_ma"], (
                f"{name}: base_ma {info['base_ma']} must be > top_ma "
                f"{info['top_ma']} (geological convention: larger Ma = older)"
            )

    def test_required_fields(self, ics):
        required = {"rank", "abbrev", "top_ma", "base_ma", "period",
                    "period_top_ma", "period_base_ma", "era"}
        for name, info in ics.items():
            assert required.issubset(info.keys()), f"{name} missing fields"
            assert info["era"] in {"Paleozoic", "Mesozoic", "Cenozoic"}, (
                f"{name}: unexpected era {info['era']!r}"
            )


class TestPeriodChain:
    """The period bounds must tile the Phanerozoic gaplessly (Cambrian ->
    Quaternary, oldest to youngest)."""

    _EXPECTED_ORDER = [
        ("Cambrian", 538.8, 486.85),
        ("Ordovician", 486.85, 443.1),
        ("Silurian", 443.1, 419.62),
        ("Devonian", 419.62, 358.86),
        ("Carboniferous", 358.86, 298.9),
        ("Permian", 298.9, 251.902),
        ("Triassic", 251.902, 201.4),
        ("Jurassic", 201.4, 143.1),
        ("Cretaceous", 143.1, 66.0),
        ("Paleogene", 66.0, 23.04),
        ("Neogene", 23.04, 2.58),
        ("Quaternary", 2.58, 0.0),
    ]

    def test_periods_tile_phanerozoic(self, ics):
        bounds = _period_bounds(ics)
        assert set(bounds) == {p for p, _, _ in self._EXPECTED_ORDER}
        for period, base, top in self._EXPECTED_ORDER:
            got_base, got_top = bounds[period]
            assert abs(got_base - base) <= _EPS, (
                f"{period}.period_base_ma: got {got_base}, expected {base}"
            )
            assert abs(got_top - top) <= _EPS, (
                f"{period}.period_top_ma: got {got_top}, expected {top}"
            )

    def test_period_fields_match_period_bounds(self, ics):
        bounds = _period_bounds(ics)
        for name, info in ics.items():
            pair = (info["period_base_ma"], info["period_top_ma"])
            assert pair == bounds[info["period"]], (
                f"{name}: period fields {pair} disagree with the "
                f"{info['period']} bounds {bounds[info['period']]}"
            )


class TestNoOrphanBoundaries:
    """Every stage boundary must mate with a neighbour or a period bound.

    This is the invariant whose absence let the original corruption
    through: Induan top 249.7 vs Olenekian base 251.2, Norian top 208.5
    vs Rhaetian base 205.7, Frasnian top 367.1 vs Famennian base 372.15,
    Pragian top 401.8 vs Emsian base 410.62 — all orphan boundaries.
    Duplicate alias entries (Wuliuan/"Stage 5", Jiangshanian/"Stage 9",
    series-level extras like "Series 2") are tolerated: their boundaries
    still mate with the chain.

    One formal gap is allowed by VALUE: the upper Pleistocene
    (0.129-0.0117 Ma, informal Tarantian / Upper Pleistocene sub-series)
    has NO formal ICS stage, so Chibanian's TOP (0.129) and the
    Holocene's BASE (0.0117) mate only with the deliberate absence
    documented in REVIEW-2026-07-31 (the bogus "Pleistocene"
    pseudo-stage that covered this interval was removed on purpose).
    Allow-listing the two boundary ages keeps the check honest for
    every other time interval.
    """

    # Boundary ages (Ma) belonging to the unstaged upper Pleistocene.
    _FORMAL_GAP_BOUNDARIES = (0.129, 0.0117)

    def _is_formal_gap(self, value: float) -> bool:
        return any(abs(value - gap) <= _EPS for gap in self._FORMAL_GAP_BOUNDARIES)

    def test_every_boundary_mates(self, ics):
        bounds = _period_bounds(ics)
        bases = {round(info["base_ma"], 6) for info in ics.values()}
        tops = {round(info["top_ma"], 6) for info in ics.values()}
        for name, info in ics.items():
            p_base, p_top = bounds[info["period"]]
            top_mates = (
                abs(info["top_ma"]) <= _EPS  # present day
                or self._is_formal_gap(info["top_ma"])
                or any(abs(info["top_ma"] - b) <= _EPS for b in bases)
                or abs(p_top - info["top_ma"]) <= _EPS
            )
            assert top_mates, (
                f"{name}.top_ma={info['top_ma']} is an orphan boundary "
                f"(no neighbour base or period top equals it)"
            )
            base_mates = (
                self._is_formal_gap(info["base_ma"])
                or any(abs(info["base_ma"] - t) <= _EPS for t in tops)
                or abs(p_base - info["base_ma"]) <= _EPS
            )
            assert base_mates, (
                f"{name}.base_ma={info['base_ma']} is an orphan boundary "
                f"(no neighbour top or period base equals it)"
            )


class TestScientificAnchors:
    """Load-bearing boundaries, pinned to ICS v2024/12."""

    def test_ptb(self, ics):
        # Permian-Triassic boundary: Changhsingian top == Induan base.
        assert abs(ics["Changhsingian"]["top_ma"] - 251.902) <= _EPS
        assert abs(ics["Induan"]["base_ma"] - 251.902) <= _EPS

    def test_kpg(self, ics):
        assert abs(ics["Maastrichtian"]["top_ma"] - 66.0) <= _EPS
        assert abs(ics["Danian"]["base_ma"] - 66.0) <= _EPS

    def test_base_cretaceous(self, ics):
        # Moved 145.0 -> 143.1 in the modern chart.
        assert abs(ics["Berriasian"]["base_ma"] - 143.1) <= _EPS
        assert abs(ics["Tithonian"]["top_ma"] - 143.1) <= _EPS

    def test_base_quaternary(self, ics):
        assert abs(ics["Gelasian"]["base_ma"] - 2.58) <= _EPS
        assert abs(ics["Piacenzian"]["top_ma"] - 2.58) <= _EPS

    def test_no_internal_overlap_or_gap_samples(self, ics):
        # The four contradictions found by the 2026-09-01 audit — now
        # impossible by construction, but pinned so a regression is
        # diagnosed in geological language, not just "orphan boundary".
        assert ics["Induan"]["top_ma"] == ics["Olenekian"]["base_ma"]
        assert ics["Norian"]["top_ma"] == ics["Rhaetian"]["base_ma"]
        assert ics["Frasnian"]["top_ma"] == ics["Famennian"]["base_ma"]
        assert ics["Pragian"]["top_ma"] == ics["Emsian"]["base_ma"]


class TestNumericLookups:
    """ages must resolve to the stratigraphically correct stage."""

    @pytest.mark.parametrize(
        "ma,expected",
        [
            (252.0, "Changhsingian"),
            (251.0, "Induan"),
            (250.0, "Induan"),
            (249.5, "Olenekian"),
            (144.0, "Tithonian"),
            (85.0, "Santonian"),
            (72.5, "Campanian"),
            (470.0, "Dapingian"),
            (445.0, "Hirnantian"),
            (300.0, "Gzhelian"),
            (0.5, "Chibanian"),
        ],
    )
    def test_stage_from_age(self, ma, expected):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(ma) == expected

    def test_age_compare_direction(self):
        from rca_core.standards.ics import ics_age_compare
        # Norian (~205.7-227.3) is OLDER than Rhaetian (201.4-205.7).
        assert ics_age_compare("Norian", "Rhaetian") == -1
        assert ics_age_compare("Rhaetian", "Norian") == 1


class TestJsMirror:
    """js/ics_table.js must mirror the PRODUCTION table ics_current.json
    exactly (dual-end parity).

    FE-FIX-2026-09-21: this pinned ics_2024.json (98 rows) while both ends
    now ship ics_current.json (109 rows - the 11 added stages are the
    authorized fix); mirroring the stale baseline would re-introduce drift
    against the data the app actually uses."""

    def test_js_table_matches_json(self):
        ics = json.loads(
            (_ROOT / "rca_core" / "resources" / "ics_current.json")
            .read_text(encoding="utf-8")
        )
        text = _JS_PATH.read_text(encoding="utf-8")
        pattern = re.compile(
            r'"([^"]+)":\s*\{top_ma:\s*([0-9.]+),\s*base_ma:\s*([0-9.]+),'
            r'\s*era:\s*"([^"]+)"\}'
        )
        js_table = {}
        for name, top, base, era in pattern.findall(text):
            js_table[name] = (float(top), float(base), era)
        assert set(js_table) == set(ics), (
            "js/ics_table.js and ics_current.json key sets diverged: "
            f"json-only={sorted(set(ics) - set(js_table))} "
            f"js-only={sorted(set(js_table) - set(ics))}"
        )
        for name, info in ics.items():
            j_top, j_base, j_era = js_table[name]
            assert abs(j_top - info["top_ma"]) <= _EPS, (
                f"{name}.top_ma drift: json={info['top_ma']} js={j_top}"
            )
            assert abs(j_base - info["base_ma"]) <= _EPS, (
                f"{name}.base_ma drift: json={info['base_ma']} js={j_base}"
            )
            assert j_era == info["era"], f"{name}.era drift"
