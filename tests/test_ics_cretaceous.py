"""Regression tests pinning the Cretaceous stage boundaries to the official
ICS International Chronostratigraphic Chart v2024/12.

History:
  * REVIEW-2026-07-31 fixed the corrupted Late Cretaceous bases
    (Campanian 79.9, Santonian 84.9, Turonian 94.0) but locked the
    remaining stages to GTS2016-vintage numbers.
  * Sprint A (CODE_REVIEW_2026-09-01, 2026-09-01) rebuilt the whole table
    on ICS v2024/12 after the audit found ~20 wrong boundaries. The
    v2024/12 official values below were cross-verified against
    stratigraphy.org, Macrostrat timescale #1 and the Paleobiology
    Database: Campanian base 72.2 (was 72.1), Santonian/Coniacian 85.7
    (was 86.3), Albian base 113.2 (was 113.0), Valanginian 132.6-137.05
    (was 132.9-139.8), Berriasian base 143.1 (was 145.0).

Reference: International Commission on Stratigraphy v2024/12
https://stratigraphy.org/ICSchart/ChronostratChart2024-12.pdf
"""

import json
from pathlib import Path

import pytest


def _load_ics():
    p = Path(__file__).resolve().parent.parent / "rca_core" / "resources" / "ics_2024.json"
    return json.loads(p.read_text(encoding="utf-8"))


# stage          : (top_ma,  base_ma)  — official ICS v2024/12
REFERENCE_CRETACEOUS = {
    "Maastrichtian": (66.0, 72.2),
    "Campanian":     (72.2, 83.6),
    "Santonian":     (83.6, 85.7),
    "Coniacian":     (85.7, 89.8),
    "Turonian":      (89.8, 93.9),
    "Cenomanian":    (93.9, 100.5),
    "Albian":        (100.5, 113.2),
    "Aptian":        (113.2, 121.4),
    "Barremian":     (121.4, 125.77),
    "Hauterivian":   (125.77, 132.6),
    "Valanginian":   (132.6, 137.05),
    "Berriasian":    (137.05, 143.1),
}


@pytest.fixture(scope="module")
def ics():
    return _load_ics()


class TestCretaceousBoundaries:
    @pytest.mark.parametrize(
        "stage,expected",
        list(REFERENCE_CRETACEOUS.items()),
    )
    def test_boundaries(self, ics, stage, expected):
        top_ref, base_ref = expected
        info = ics[stage]
        assert abs(info["top_ma"] - top_ref) <= 0.01, (
            f"{stage}.top_ma: got {info['top_ma']}, expected {top_ref}"
        )
        assert abs(info["base_ma"] - base_ref) <= 0.01, (
            f"{stage}.base_ma: got {info['base_ma']}, expected {base_ref}"
        )


class TestCretaceousLookups:
    """Consumer-facing behaviour: numeric ages must map to the RIGHT stage."""

    @pytest.mark.parametrize(
        "ma,expected",
        [
            (80.0, "Campanian"),
            (82.0, "Campanian"),   # 72.2-83.6 (v2024/12)
            (85.0, "Santonian"),   # 83.6-85.7 (v2024/12; 85.7 is Coniacian base)
            (88.0, "Coniacian"),
            (92.0, "Turonian"),
            (95.0, "Cenomanian"),
            (105.0, "Albian"),
            (143.0, "Berriasian"),  # base Cretaceous is now 143.1
            (144.0, "Tithonian"),   # Jurassic side of the moved boundary
        ],
    )
    def test_stage_from_age(self, ma, expected):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(ma) == expected, (
            f"{ma} Ma should be {expected}"
        )

    def test_resolve_age_bound_campanian(self):
        from rca_core.standards.ics import ics_resolve_age_bound
        name, ma = ics_resolve_age_bound("82 Ma")
        assert name == "Campanian"
        assert abs(ma - 82.0) <= 1e-9

    def test_age_compare_santonian_younger_than_coniacian(self):
        from rca_core.standards.ics import ics_age_compare
        # Coniacian (89.8-85.7) is OLDER than Santonian (85.7-83.6).
        assert ics_age_compare("Coniacian", "Santonian") == -1
        assert ics_age_compare("Santonian", "Coniacian") == 1


class TestOrdovicianBoundaries:
    """Mid-Ordovician bases were locked to GTS2012-vintage values by the
    previous regression test (453.0/458.4/467.3/470.0/477.7).

    Official ICS v2024/12: Katian base 452.8, Sandbian 458.2,
    Darriwilian 469.4, Dapingian 471.3, Floian 477.1, base Ordovician
    486.85 (base Silurian 443.1).
    """

    @pytest.mark.parametrize(
        "stage,expected",
        [
            ("Katian", (445.2, 452.8)),
            ("Sandbian", (452.8, 458.2)),
            ("Darriwilian", (458.2, 469.4)),
            ("Dapingian", (469.4, 471.3)),
            ("Floian", (471.3, 477.1)),
            ("Tremadocian", (477.1, 486.85)),
        ],
    )
    def test_boundaries(self, ics, stage, expected):
        top_ref, base_ref = expected
        info = ics[stage]
        assert abs(info["top_ma"] - top_ref) <= 0.01
        assert abs(info["base_ma"] - base_ref) <= 0.01

    def test_stage_from_age(self):
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(448.0) == "Katian"
        assert ics_stage_from_age(455.0) == "Sandbian"
        assert ics_stage_from_age(462.0) == "Darriwilian"
        assert ics_stage_from_age(470.0) == "Dapingian"
        assert ics_stage_from_age(474.0) == "Floian"
        assert ics_stage_from_age(480.0) == "Tremadocian"
