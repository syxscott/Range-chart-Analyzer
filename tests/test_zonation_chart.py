"""Regression tests for the zonation_chart extraction mode
(UI-REVIEW-2026-09-05, radiolarian biozonation / correlation charts).

Figure type: columns of named biozones correlated across regions/authors
and calibrated against ammonoid/conodont zones and stages — the canonical
radiolarian biochronology figure (e.g. Gorican et al. 2018, Fig. 1-2).
"""

import os
import tempfile

import pytest

from rca_core.aggregate import SCHEMA_BY_MODE, merge_results
from rca_core.chart_mode import auto_detect_chart_mode
from rca_core.exporter import (
    build_table_export,
    get_configs_for_result,
    to_xlsx,
    validate_export_invariants,
)
from rca_core.extractor import (
    _MODE_DISPATCH,
    extract_zonation_chart,
    normalize_zonation_chart_result,
)
from rca_core.quality import _detect_mode, score_range_chart


SAMPLE = {
    "zonations": [
        {
            "name": "Bragin (2018), Koryak Highlands",
            "region": "Koryak Highlands, NE Russia",
            "framework": "radiolarian",
            "reference": "Bragin, 2018",
        },
        {
            "name": "Carter (1993), Queen Charlotte Islands",
            "region": "British Columbia, Canada",
            "framework": "radiolarian",
            "reference": "Carter, 1993",
        },
    ],
    "zones": [
        {
            "name": "Proparvicingula moniliformis Zone",
            "zonation": "Bragin (2018), Koryak Highlands",
            "rank": "zone",
            "age_span": "lower Rhaetian",
            "base_age": "",
            "top_age": "",
            "stage": "Rhaetian",
            "defined_by": "FAD of Proparvicingula moniliformis",
            "note": "",
        },
        {
            "name": "Globolaxtorum tozeri Zone",
            "zonation": "Bragin (2018), Koryak Highlands",
            "rank": "zone",
            "age_span": "upper Rhaetian",
            "base_age": "",
            "top_age": "",
            "stage": "Rhaetian",
            "defined_by": "",
            "note": "",
        },
    ],
    "correlations": [
        {
            "from_zone": "Proparvicingula moniliformis Zone",
            "to_zone": "Crassistephanus thuyensis Zone",
            "from_zonation": "Bragin (2018), Koryak Highlands",
            "to_zonation": "Carter (1993), Queen Charlotte Islands",
            "basis": "shared stage",
            "note": "",
        },
    ],
    "confidence": 0.9,
}


# ---------------------------------------------------------------------------
# normalizer
# ---------------------------------------------------------------------------

def test_normalize_shapes_three_tables():
    data = normalize_zonation_chart_result(SAMPLE)
    assert len(data["zonations"]) == 2
    assert len(data["zones"]) == 2
    assert len(data["correlations"]) == 1
    assert data["confidence"] == pytest.approx(0.9)


def test_normalize_rows_are_string_typed():
    data = normalize_zonation_chart_result(
        {"zones": [{"name": "Z", "rank": "zone", "base_age": 251.902}]}
    )
    row = data["zones"][0]
    assert row["base_age"] == "251.902"  # numeric coerced to string
    assert isinstance(row["name"], str)


def test_normalize_h8_row_extras_carried():
    data = normalize_zonation_chart_result(
        {"zones": [{"name": "Z", "isotopic_age": "250.3 Ma"}]}
    )
    assert data["zones"][0]["_extras"]["isotopic_age"] == "250.3 Ma"


def test_normalize_array_root_rescue():
    data = normalize_zonation_chart_result({
        "_array_root": [
            {"from_zone": "A Zone", "to_zone": "B Zone"},
            {"name": "C Zone", "zonation": "Z1", "rank": "zone"},
            {"name": "Z2", "framework": "ammonoid"},
            {"something": "else"},
        ],
        "confidence": 0.5,
    })
    assert len(data["correlations"]) == 1
    assert len(data["zones"]) == 1
    assert len(data["zonations"]) == 1
    assert data["_extras"]["_unclassified"][0] == {"something": "else"}


def test_normalize_non_dict_returns_empty_shape():
    data = normalize_zonation_chart_result("garbage")
    assert data["zones"] == [] and data["confidence"] == 0.0


def test_mode_dispatch_registered():
    assert _MODE_DISPATCH["zonation_chart"] is extract_zonation_chart


# ---------------------------------------------------------------------------
# chart-mode auto detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caption", [
    "Fig. 1. Correlation of Triassic radiolarian zones and subzones",
    "Radiolarian zonation tied to ammonoid and conodont zones",
    "图1 三叠纪放射虫生物带对比",
    "Kozur zonation chart, Middle Anisian to Ladinian",
])
def test_autodetect_zonation(caption):
    assert auto_detect_chart_mode(caption) == "zonation_chart"


@pytest.mark.parametrize("caption,expected", [
    ("Fig.3 Stratigraphic range chart of radiolarians", "range_chart"),
    ("Pollen percentage diagram of core XT47", "abundance_diagram"),
    ("Columnar section of the Dalong Formation", "columnar_section"),
    ("Molecular phylogeny of polycystine radiolaria", "phylogenetic_tree"),
])
def test_autodetect_regressions(caption, expected):
    assert auto_detect_chart_mode(caption) == expected


# ---------------------------------------------------------------------------
# quality scoring
# ---------------------------------------------------------------------------

def test_quality_detects_zonation_mode():
    assert _detect_mode(SAMPLE) == "zonation"


def test_quality_zonation_does_not_flag_abundance_zones_as_zonation():
    assert _detect_mode(
        {"abundances": [{"taxon": "Pinus"}], "zones": [{"name": "PAZ-1"}]}
    ) == "abundance"


def test_quality_scores_populated_zonation_chart():
    r = score_range_chart(SAMPLE)
    assert r["grade"] in ("A", "B")
    assert r["score"] >= 0.5


# ---------------------------------------------------------------------------
# aggregation (multi-run merge)
# ---------------------------------------------------------------------------

def test_merge_zonation_with_schema():
    merged = merge_results([SAMPLE, SAMPLE], total_runs=2,
                           schema=SCHEMA_BY_MODE["zonation_chart"])
    assert len(merged["zones"]) == 2
    assert merged["zones"][0]["agreement"] == "2/2"
    assert len(merged["correlations"]) == 1
    assert merged["zonations"][0]["name"].startswith("Bragin")


def test_schema_by_mode_contains_zonation_chart():
    assert "zonation_chart" in SCHEMA_BY_MODE
    assert SCHEMA_BY_MODE["zonation_chart"].primary_list_key == "zones"


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def test_get_configs_for_result_routes_zonation():
    cfgs = get_configs_for_result(SAMPLE)
    assert [c["id"] for c in cfgs] == ["zonations", "zones", "correlations"]


def test_build_table_export_zones():
    headers, rows = build_table_export(SAMPLE, "zones", lambda k: k)
    assert "col.name" in headers
    assert rows[0][1] == "Proparvicingula moniliformis Zone"


def test_export_invariants_pass():
    ok, issues = validate_export_invariants(SAMPLE)
    assert ok, issues


def test_to_xlsx_smoke():
    tmp = os.path.join(tempfile.mkdtemp(), "zonation.xlsx")
    to_xlsx(SAMPLE, tmp)
    assert os.path.getsize(tmp) > 0


# ---------------------------------------------------------------------------
# server whitelist
# ---------------------------------------------------------------------------

def test_server_source_whitelists_zonation_chart():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "server.py"), encoding="utf-8") as f:
        src = f.read()
    assert "'zonation_chart'" in src
