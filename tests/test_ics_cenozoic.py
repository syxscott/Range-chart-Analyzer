"""Regression test for ICS Cenozoic data corruption (REVIEW-2026-07-25/P1-2).

The shipped `ics_2024.json` originally had every Cenozoic stage mapped to
wrong Ma boundary ages (Danian mapped to 100.5–83.6 Ma / mid-Cretaceous
instead of 66.0–61.6 Ma / immediately above K-Pg). The 3 stages
immediately above the K-Pg boundary (Thanetian, Selandian, Danian) had
the worst corruption, and Pleistocene/Holocene were missing entirely.

This test pins all 22 Cenozoic stages to their correct ICS 2023/2024
values so a future data corruption cannot silently regress.

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
# (top_ma ≤ base_ma because top_ma < base_ma for all real stages.)
# NOTE: Pleistocene is an EPOCH (2.588-0.0117 Ma), not a stage; it is NOT
# a table entry — it resolves through the series map (see
# TestIcsPleistoceneEpoch below). REVIEW-2026-07-31 removed the bogus
# stage entry that covered only 0.129-0.0117 Ma (it duplicated Chibanian).
REFERENCE_CENOZOIC = {
    # stage          : (top_ma,  base_ma, period)
    "Holocene":       (0.0,    0.0117,  "Quaternary"),
    "Chibanian":      (0.129,  0.774,   "Quaternary"),
    "Calabrian":      (0.774,  1.80,    "Quaternary"),
    "Gelasian":       (1.80,   2.588,   "Neogene"),
    "Piacenzian":     (2.588,  3.60,    "Neogene"),
    "Zanclean":       (3.60,   5.333,   "Neogene"),
    "Messinian":      (5.333,  7.246,   "Neogene"),
    "Tortonian":      (7.246,  11.63,   "Neogene"),
    "Serravallian":   (11.63,  13.82,   "Neogene"),
    "Langhian":       (13.82,  15.97,   "Neogene"),
    "Burdigalian":    (15.97,  20.44,   "Neogene"),
    "Aquitanian":     (20.44,  23.03,   "Neogene"),
    "Chattian":       (23.03,  27.82,   "Paleogene"),
    "Rupelian":       (27.82,  33.9,    "Paleogene"),
    "Priabonian":     (33.9,   37.71,   "Paleogene"),
    "Bartonian":      (37.71,  41.2,    "Paleogene"),
    "Lutetian":       (41.2,   47.8,    "Paleogene"),
    "Ypresian":       (47.8,   56.0,    "Paleogene"),
    "Thanetian":      (56.0,   59.2,    "Paleogene"),
    "Selandian":      (59.2,   61.6,    "Paleogene"),
    "Danian":         (61.6,   66.0,    "Paleogene"),
}


@pytest.fixture(scope="module")
def ics():
    return _load_ics()


class TestIcsCenozoicPresence:
    """Every stage in REFERENCE_CENOZOIC must exist in the table.

    Catches the original bug where Thanetian, Pleistocene, and Holocene
    were MISSING from the table — `ics_parse_age_range` would silently
    fail to find them.
    """

    @pytest.mark.parametrize("stage", REFERENCE_CENOZOIC.keys())
    def test_stage_present(self, ics, stage):
        assert stage in ics, f"{stage} missing from ics_2024.json"


class TestIcsCenozoicBoundaries:
    """Each Cenozoic stage must have the correct top_ma / base_ma / period.

    Tolerances are tight (≤ 0.01 Ma) — the ICS chart is the source of truth,
    and boundaries do not drift between revisions at the sub-0.01 Ma level.
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
    """Within Cenozoic, stage ages must form a non-overlapping staircase.

    A stage's top_ma must equal (within tolerance) the next-younger stage's base_ma.
    """

    def test_bartonian_top_equals_priabonian_base(self, ics):
        # The boundary Bartonian|Priabonian is 37.71 Ma. Verify both sides agree.
        bart = ics["Bartonian"]
        priab = ics["Priabonian"]
        assert abs(bart["top_ma"] - priab["base_ma"]) <= 0.02

    def test_danian_base_lt_maastrichtian_top(self, ics):
        # K-Pg boundary: Danian base = 66.0, Maastrichtian top = 66.0.
        # They must match within tolerance — this is the boundary that
        # the original data corruption broke (Danian was mapped to 100.5).
        danian = ics["Danian"]
        maas = ics["Maastrichtian"]
        assert abs(danian["base_ma"] - maas["top_ma"]) <= 0.05, (
            f"K-Pg boundary mismatch: Danian.base={danian['base_ma']} "
            f"Maastrichtian.top={maas['top_ma']}"
        )


class TestIcsCenozoicLookups:
    """The downstream lookups must return Cenozoic stages correctly.

    These are the actual consumer-facing behaviour tests — even if the
    underlying numbers changed, the function must still produce the
    correct stage for canonical Ma values.
    """

    def test_stage_from_age_danian(self, ics):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(64.0) == "Danian", (
            "64 Ma should be Danian (Paleocene, 66.0–61.6 Ma); the original "
            "data corruption returned 'Bartonian' for this input."
        )

    def test_stage_from_age_thanetian(self, ics):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(58.0) == "Thanetian"

    def test_stage_from_age_holocene(self, ics):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(0.0) == "Holocene", (
            "0 Ma should be Holocene. Original data missing Holocene "
            "returned None."
        )

    def test_age_compare_danian_older_than_thanetian(self, ics):
        from rca_core.standards.ics import ics_age_compare
        # In stratigraphic time order (oldest → youngest):
        #   Danian (Paleocene base, 61.6–66.0) → Selandian (59.2–61.6) →
        #   Thanetian (56.0–59.2) → Ypresian (47.8–56.0)
        # So Danian is OLDER than Thanetian.
        # ics_age_compare: returns -1 if stage1 is older, 1 if younger.
        assert ics_age_compare("Danian", "Thanetian") < 0, (
            "Danian (61.6–66.0 Ma, oldest Paleocene) must be reported as "
            "older than Thanetian (56.0–59.2 Ma, youngest Paleocene)."
        )

    def test_age_compare_holocene_younger_than_danian(self, ics):
        from rca_core.standards.ics import ics_age_compare
        assert ics_age_compare("Holocene", "Danian") > 0

    def test_parse_age_range_thanetian_present(self, ics):
        from rca_core.standards.ics import ics_parse_age_range
        assert "Thanetian" in ics_parse_age_range("Thanetian (Paleocene)")


class TestIcsPleistoceneEpoch:
    """Pleistocene is an Epoch (2.588–0.0117 Ma), not a stage.

    REVIEW-2026-07-31: the old table carried a bogus "Pleistocene" stage
    entry covering only 0.129–0.0117 Ma (a duplicate of Chibanian). It was
    removed; the label now resolves through the series map to the real
    Pleistocene range. A chart labeled "Pleistocene" previously resolved
    to a ~0.07 Ma midpoint (~1.3 Ma off); it now resolves to 2.588 Ma.
    """

    def test_not_a_stage_entry(self, ics):
        assert "Pleistocene" not in ics, (
            "Pleistocene is an epoch, not a stage — a table entry would "
            "collide with the real stages (Gelasian/Calabrian/Chibanian)."
        )

    def test_resolves_to_epoch_bounds(self):
        from rca_core.standards.ics import ics_age_range_bounds, ics_resolve_age_bound
        name, ma = ics_resolve_age_bound("Pleistocene", prefer="older")
        assert name == "Pleistocene"
        assert abs(ma - 2.588) <= 0.01
        older, younger = ics_age_range_bounds("Pleistocene")
        assert abs(older - 2.588) <= 0.01
        assert abs(younger - 0.0117) <= 0.01

    def test_midpleistocene_age_resolves_to_chibanian(self):
        from rca_core.standards.ics import ics_stage_from_age
        # 0.5 Ma is mid-Pleistocene — must be Chibanian, NOT the bogus
        # "Pleistocene" pseudo-stage.
        assert ics_stage_from_age(0.5) == "Chibanian"
