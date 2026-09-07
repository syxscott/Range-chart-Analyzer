"""Tests for the survey-borrowed modules (UI-REVIEW-2026-09-07):

- rca_core.names        - GBIF species-match verification (fail-open)
- rca_core.report       - evidence-chain extraction report
- rca_core.capabilities - mode maturity registry
"""

import pytest

from rca_core.capabilities import CAPABILITIES, maturity_for
from rca_core.names import (
    clean_name_for_lookup,
    name_issues,
    verify_names,
)
from rca_core.report import REPORT_SCHEMA_VERSION, build_extraction_report


# ---------------------------------------------------------------------------
# names.clean_name_for_lookup
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Clarkina yini", "Clarkina yini"),
    ("Pseudotirolites cf. P. asiaticus (Zheng, 1979)",
     "Pseudotirolites asiaticus"),
    ("Genus sp.", "Genus"),
    ("Nankinella? aff. discoides", "Nankinella discoides"),
    ("Albaillella ex gr. A. levis", "Albaillella levis"),
    ("", ""),
])
def test_clean_name_for_lookup(raw, expected):
    assert clean_name_for_lookup(raw) == expected


# ---------------------------------------------------------------------------
# names.verify_names / name_issues (mocked transport)
# ---------------------------------------------------------------------------

def _gbif(match_type, confidence, canonical=""):
    return json_dumps({"matchType": match_type, "confidence": confidence,
                       "canonicalName": canonical,
                       "scientificName": canonical, "alternatives": []})


def json_dumps(obj):
    import json
    return json.dumps(obj)


def test_verify_names_fuzzy_suggestion():
    def fetch(url):
        return _gbif("FUZZY", 85, "Pseudotirolites asiaticus")

    v = verify_names(["Psendotirolites asiaticus"], fetch=fetch)
    assert v["Psendotirolites asiaticus"]["match_type"] == "FUZZY"
    issues = name_issues(v)
    assert issues and issues[0]["msg_key"] == "names.fuzzy"
    assert issues[0]["suggestion"] == "Pseudotirolites asiaticus"


def test_verify_names_exact_no_issue():
    v = verify_names(["Clarkina yini"],
                     fetch=lambda url: _gbif("EXACT", 98, "Clarkina yini"))
    assert name_issues(v) == []


def test_verify_names_none_becomes_info_issue():
    v = verify_names(["Someodd taxon"],
                     fetch=lambda url: _gbif("NONE", 0))
    issues = name_issues(v)
    assert len(issues) == 1 and issues[0]["msg_key"] == "names.unmatched"


def test_verify_names_fail_open_on_network_error():
    def boom(url):
        raise IOError("dns down")

    v = verify_names(["Clarkina yini"], fetch=boom)
    assert v["Clarkina yini"]["status"] == "unavailable"
    assert name_issues(v) == []  # silent when the backend is unreachable


def test_verify_names_dedupes_and_keys_by_original():
    calls = []

    def fetch(url):
        calls.append(url)
        return _gbif("EXACT", 90, "Clarkina yini")

    v = verify_names(["Clarkina yini", "Clarkina  yini."], fetch=fetch)
    assert set(v) == {"Clarkina yini", "Clarkina  yini."}
    assert len(calls) == 1  # same cleaned name queried once


# ---------------------------------------------------------------------------
# report.build_extraction_report
# ---------------------------------------------------------------------------

def test_report_rows_and_empty_reasons():
    data = {
        "species_ranges": [{"species": "A"}, {"species": "B"}],
        "sections": [],
        "_extras": {"note": "pie charts with obscured labels"},
    }
    rep = build_extraction_report(data=data, mode="auto",
                                  mode_used="abundance_diagram",
                                  mode_source="vision")
    assert rep["rows"]["species_ranges"] == 2
    assert rep["rows"]["sections"] == 0
    assert rep["empty_tables"][0]["key"] == "sections"
    assert "pie charts" in rep["empty_tables"][0]["reason"]
    assert rep["mode"]["used"] == "abundance_diagram"
    assert rep["mode"]["source"] == "vision"


def test_report_truncation_and_timescale_stamp():
    rep = build_extraction_report(data={}, mode="range_chart",
                                  truncated=True, warning="cut")
    assert rep["truncation"]["truncated"] is True
    assert rep["warnings"] == ["cut"]
    assert rep["timescale"]["ics_version"].startswith("ICS v")


def test_report_schema_version_and_degenerate_input():
    rep = build_extraction_report(data="not a dict", mode="range_chart")
    assert rep["schema_version"] == REPORT_SCHEMA_VERSION
    assert rep["rows"] == {}


# ---------------------------------------------------------------------------
# capabilities registry
# ---------------------------------------------------------------------------

def test_capabilities_cover_all_wired_modes():
    expected = {"range_chart", "columnar_section", "abundance_diagram",
                "phylogenetic_tree", "zonation_chart",
                "chemical_stratigraphy", "paleomap", "scatter_plot"}
    assert expected == set(CAPABILITIES)


def test_maturity_levels_are_from_the_registry_vocabulary():
    for mode, entry in CAPABILITIES.items():
        assert entry["maturity"] in {"stable", "candidate", "assistant"}, mode
        assert entry["notes"].strip(), mode


def test_maturity_for_unknown_mode_returns_stub():
    entry = maturity_for("nope")
    assert entry["maturity"] == "assistant"


# ---------------------------------------------------------------------------
# redraw.redraw_range_chart (optional matplotlib)
# ---------------------------------------------------------------------------

def _mpl_available():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:
        return False


def test_redraw_renders_png_bytes():
    pytest.importorskip("matplotlib")
    from rca_core.redraw import redraw_range_chart
    data = {"species_ranges": [
        {"species": "Clarkina yini", "range_base": "26", "range_top": "24"},
        {"species": "Hindeodus parvus", "range_base": "27", "range_top": "27"},
    ]}
    png = redraw_range_chart(data)
    assert isinstance(png, (bytes, bytearray))
    PNG_MAGIC = bytes([0x89]) + b"PNG"  #  P N G
    assert bytes(png[:4]) == PNG_MAGIC


def test_redraw_empty_payload_returns_none():
    from rca_core.redraw import redraw_range_chart
    assert redraw_range_chart({"species_ranges": []}) is None
    assert redraw_range_chart({}) is None
