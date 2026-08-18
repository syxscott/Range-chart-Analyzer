"""Regression test for ICS Cretaceous boundary corruption (REVIEW-2026-07-31).

The shipped ics_2024.json had three wrong Late Cretaceous stage bases:

    Campanian  79.9  (official ICS v2024: 83.6)
    Santonian  84.9  (official ICS v2024: 86.3)
    Turonian   94.0  (official ICS v2024: 93.9)

Consequences verified in the audit:
  * ics_stage_from_age(82) returned "Santonian" (must be Campanian)
  * ics_stage_from_age(85) returned "Coniacian" (must be Santonian)
  * every numeric age between 79.9 and 86.3 Ma was exported to the wrong
    stage in Darwin Core / PBDB output
  * js/ics_table.js mirrored the same wrong values

Reference: International Commission on Stratigraphy v2024/12
https://stratigraphy.org/ICSchart/ChronostratChart2024-12.pdf
"""

import json
from pathlib import Path

import pytest


def _load_ics():
    p = Path(__file__).resolve().parent.parent / "rca_core" / "resources" / "ics_2024.json"
    return json.loads(p.read_text(encoding="utf-8"))


# stage          : (top_ma,  base_ma)  — official ICS v2024
REFERENCE_CRETACEOUS = {
    "Maastrichtian": (66.0, 72.1),
    "Campanian":     (72.1, 83.6),
    "Santonian":     (83.6, 86.3),
    "Coniacian":     (86.3, 89.8),
    "Turonian":      (89.8, 93.9),
    "Cenomanian":    (93.9, 100.5),
    "Albian":        (100.5, 113.0),
    "Aptian":        (113.0, 121.4),
    "Barremian":     (121.4, 125.77),
    "Hauterivian":   (125.77, 132.9),
    "Valanginian":   (132.9, 139.8),
    "Berriasian":    (139.8, 145.0),
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
            (80.0, "Campanian"),   # 79.9-83.6 used to mis-assign to Santonian
            (82.0, "Campanian"),
            (85.0, "Santonian"),   # used to resolve to Coniacian
            (88.0, "Coniacian"),
            (92.0, "Turonian"),
            (95.0, "Cenomanian"),
            (105.0, "Albian"),
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
        # Coniacian (89.8-86.3) is OLDER than Santonian (86.3-83.6).
        assert ics_age_compare("Coniacian", "Santonian") == -1
        assert ics_age_compare("Santonian", "Coniacian") == 1


class TestOrdovicianBoundaries:
    """Mid-Ordovician bases were also off (468.1/464.8/456.7/451.8).

    Official GTS2020 / ICS v2024: Dapingian 470.0, Darriwilian 467.3,
    Sandbian 458.4, Katian 453.0.
    """

    @pytest.mark.parametrize(
        "stage,expected",
        [
            ("Katian", (445.2, 453.0)),
            ("Sandbian", (453.0, 458.4)),
            ("Darriwilian", (458.4, 467.3)),
            ("Dapingian", (467.3, 470.0)),
            ("Floian", (470.0, 477.7)),
        ],
    )
    def test_boundaries(self, ics, stage, expected):
        top_ref, base_ref = expected
        info = ics[stage]
        assert abs(info["top_ma"] - top_ref) <= 0.01
        assert abs(info["base_ma"] - base_ref) <= 0.01

    def test_stage_from_age(self):
        from rca_core.standards.ics import ics_stage_from_age
        # Katian 445.2-453.0, Sandbian 453.0-458.4, Darriwilian 458.4-467.3,
        # Dapingian 467.3-470.0, Floian 470.0-477.7.
        assert ics_stage_from_age(448.0) == "Katian"
        assert ics_stage_from_age(455.0) == "Sandbian"
        assert ics_stage_from_age(462.0) == "Darriwilian"
        assert ics_stage_from_age(468.5) == "Dapingian"
        assert ics_stage_from_age(474.0) == "Floian"
