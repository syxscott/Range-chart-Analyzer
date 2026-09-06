"""Regression tests pinning the Cenozoic stages to the official ICS
International Chronostratigraphic Chart v2024/12.

History:
  * REVIEW-2026-07-25/P1-2 repaired the wholesale Cenozoic corruption
    (Danian mapped to mid-Cretaceous, missing Holocene/Thanetian) and
    pinned 21 stages — but to GTS2016-vintage numbers.
  * Sprint A (CODE_REVIEW_2026-09-01, 2026-09-01) rebuilt the table on
    ICS v2024/12, cross-verified against stratigraphy.org, Macrostrat
    timescale #1 and the Paleobiology Database. Boundary updates:
    Danian base 61.66 (was 61.6), Thanetian base 59.24 (was 59.2),
    Ypresian base 48.07 (was 47.8), Lutetian 41.03-48.07 (was
    41.2-47.8), Bartonian base 41.03 (was 41.2), Chattian base 27.30
    (was 27.82), Aquitanian 20.45-23.04 (was 20.44-23.03), Burdigalian
    15.98-20.45, Langhian top 15.98 (was 15.97), base Quaternary 2.58
    (was 2.588). Gelasian now correctly belongs to the QUATERNARY (ICS
    2009 redefinition; previously mislabelled Neogene).

Reference: International Commission on Stratigraphy v2024/12
https://stratigraphy.org/ICSchart/ChronostratChart2024-12.pdf
"""

import json
import re
from pathlib import Path

import pytest


def _load_ics():
    p = Path(__file__).resolve().parent.parent / "rca_core" / "resources" / "ics_2024.json"
    return json.loads(p.read_text(encoding="utf-8"))


# Reference values (Ma) — top_ma is younger boundary, base_ma is older.
# NOTE: Pleistocene is an EPOCH (2.58-0.0117 Ma), not a stage; it is NOT
# a table entry — it resolves through the series map (see
# TestIcsPleistoceneEpoch below). REVIEW-2026-07-31 removed the bogus
# stage entry that covered only 0.129-0.0117 Ma (it duplicated Chibanian).
REFERENCE_CENOZOIC = {
    # stage          : (top_ma,  base_ma, period)
    "Holocene":       (0.0,    0.0117,  "Quaternary"),
    "Chibanian":      (0.129,  0.774,   "Quaternary"),
    "Calabrian":      (0.774,  1.80,    "Quaternary"),
    "Gelasian":       (1.80,   2.58,    "Quaternary"),
    "Piacenzian":     (2.58,   3.60,    "Neogene"),
    "Zanclean":       (3.60,   5.333,   "Neogene"),
    "Messinian":      (5.333,  7.246,   "Neogene"),
    "Tortonian":      (7.246,  11.63,   "Neogene"),
    "Serravallian":   (11.63,  13.82,   "Neogene"),
    "Langhian":       (13.82,  15.98,   "Neogene"),
    "Burdigalian":    (15.98,  20.45,   "Neogene"),
    "Aquitanian":     (20.45,  23.04,   "Neogene"),
    "Chattian":       (23.04,  27.30,   "Paleogene"),
    "Rupelian":       (27.30,  33.9,    "Paleogene"),
    "Priabonian":     (33.9,   37.71,   "Paleogene"),
    "Bartonian":      (37.71,  41.03,   "Paleogene"),
    "Lutetian":       (41.03,  48.07,   "Paleogene"),
    "Ypresian":       (48.07,  56.00,   "Paleogene"),
    "Thanetian":      (56.00,  59.24,   "Paleogene"),
    "Selandian":      (59.24,  61.66,   "Paleogene"),
    "Danian":         (61.66,  66.00,   "Paleogene"),
}


@pytest.fixture(scope="module")
def ics():
    return _load_ics()


class TestIcsCenozoicPresence:
    """Every stage in REFERENCE_CENOZOIC must exist in the table."""

    @pytest.mark.parametrize("stage", REFERENCE_CENOZOIC.keys())
    def test_stage_present(self, ics, stage):
        assert stage in ics, f"{stage} missing from ics_2024.json"


class TestIcsCenozoicBoundaries:
    """Each Cenozoic stage must have the correct top_ma / base_ma / period.

    Tolerances are tight (≤ 0.01 Ma) — the ICS chart is the source of truth.
    """

    @pytest.mark.parametrize(
        "stage,expected",
        list(REFERENCE_CENOZOIC.items()),
    )
    def test_boundaries(self, ics, stage, expected):
        top_ref, base_ref, period_ref = expected
        info = ics[stage]
        assert abs(info["top_ma"] - top_ref) <= 0.01, (
            f"{stage}.top_ma: got {info['top_ma']}, expected {top_ref}"
        )
        assert abs(info["base_ma"] - base_ref) <= 0.01, (
            f"{stage}.base_ma: got {info['base_ma']}, expected {base_ref}"
        )
        assert info["period"] == period_ref, (
            f"{stage}.period: got {info['period']!r}, expected {period_ref!r}"
        )
        assert info.get("era") == "Cenozoic", (
            f"{stage}.era: got {info.get('era')!r}, expected 'Cenozoic'"
        )


class TestIcsCenozoicOrdering:
    """Within Cenozoic, stage ages must form a non-overlapping staircase."""

    def test_bartonian_top_equals_priabonian_base(self, ics):
        bart = ics["Bartonian"]
        priab = ics["Priabonian"]
        assert abs(bart["top_ma"] - priab["base_ma"]) <= 0.02

    def test_danian_base_equals_maastrichtian_top(self, ics):
        # K-Pg boundary: Danian base = Maastrichtian top = 66.0.
        danian = ics["Danian"]
        maas = ics["Maastrichtian"]
        assert abs(danian["base_ma"] - maas["top_ma"]) <= 0.05, (
            f"K-Pg boundary mismatch: Danian.base={danian['base_ma']} "
            f"Maastrichtian.top={maas['top_ma']}"
        )

    def test_gelasian_belongs_to_quaternary(self, ics):
        # Sprint A (2026-09-01): the Gelasian was moved into the
        # Quaternary by ICS in 2009; the old table mislabelled it Neogene.
        assert ics["Gelasian"]["period"] == "Quaternary"


class TestIcsCenozoicLookups:
    """The downstream lookups must return Cenozoic stages correctly."""

    def test_stage_from_age_danian(self, ics):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(64.0) == "Danian"

    def test_stage_from_age_thanetian(self, ics):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(58.0) == "Thanetian"

    def test_stage_from_age_holocene(self, ics):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(0.0) == "Holocene"

    def test_age_compare_danian_older_than_thanetian(self, ics):
        from rca_core.standards.ics import ics_age_compare
        assert ics_age_compare("Danian", "Thanetian") < 0

    def test_age_compare_holocene_younger_than_danian(self, ics):
        from rca_core.standards.ics import ics_age_compare
        assert ics_age_compare("Holocene", "Danian") > 0

    def test_parse_age_range_thanetian_present(self, ics):
        from rca_core.standards.ics import ics_parse_age_range
        assert "Thanetian" in ics_parse_age_range("Thanetian (Paleocene)")


class TestIcsPleistoceneEpoch:
    """Pleistocene is an Epoch (2.58-0.0117 Ma), not a stage."""

    def test_not_a_stage_entry(self, ics):
        assert "Pleistocene" not in ics, (
            "Pleistocene is an epoch, not a stage — a table entry would "
            "collide with the real stages (Gelasian/Calabrian/Chibanian)."
        )

    def test_resolves_to_epoch_bounds(self):
        from rca_core.standards.ics import ics_age_range_bounds, ics_resolve_age_bound
        name, ma = ics_resolve_age_bound("Pleistocene", prefer="older")
        assert name == "Pleistocene"
        assert abs(ma - 2.58) <= 0.01
        older, younger = ics_age_range_bounds("Pleistocene")
        assert abs(older - 2.58) <= 0.01
        assert abs(younger - 0.0117) <= 0.01

    def test_midpleistocene_age_resolves_to_chibanian(self):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(0.5) == "Chibanian"
