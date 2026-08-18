"""Targeted tests for the Darwin Core Archive export fixes.

Covers H4 (TAB-delimited occurrence.txt) and the C-1 age-bound derivation
(earliestAgeOrLowestStage != latestAgeOrHighestStage when range_base !=
range_top, via the ICS table).
"""

import csv
import io
import zipfile

import pytest

from rca_core.standards.darwin_core import (
    to_darwin_core_archive,
    to_darwin_core_occurrences,
)


def _read_occurrence_txt(archive_path):
    with zipfile.ZipFile(archive_path) as zf:
        text = zf.read("occurrence.txt").decode("utf-8")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = list(reader)
    assert rows, "occurrence.txt should not be empty"
    header = rows[0]
    return header, rows[1:]


def test_occurrence_txt_is_tab_delimited(tmp_path):
    """H4: DwC-A occurrence.txt must be TAB-separated, matching meta.xml."""
    result = {
        "sections": [
            {"name": "SecA", "coordinates": "31N, 117E", "formations": ["Formation X"]},
        ],
        "species_ranges": [
            {
                "species": "Ammonite a",
                "section": "SecA",
                "biozone": "Zone 1",
                "author_year": "Smith 1900",
                "range_base": "Wuchiapingian",
                "range_top": "Changhsingian",
                "endpoint_kind": "observed",
                "occurrence_mode": "in_situ",
            },
        ],
    }
    out = tmp_path / "dwc.zip"
    to_darwin_core_archive(result, str(out))
    header, data = _read_occurrence_txt(str(out))
    assert "\t" in (header and "\t".join(header)) or len(header) > 1
    # The header+row must contain more than one column when split on TAB.
    assert len(header) > 1
    assert len(data) == 1
    # Re-read strictly with TAB: every row (header + data) should expose the
    # expected number of fields when parsed as TAB-separated.
    with zipfile.ZipFile(str(out)) as zf:
        raw = zf.read("occurrence.txt").decode("utf-8")
    for line in raw.splitlines():
        # If it were comma-delimited this would be a single field; with TAB
        # we expect multiple columns.
        assert line.count("\t") >= 1, "occurrence.txt line is not TAB-delimited"


def test_distinct_stage_bounds_when_range_base_ne_range_top(tmp_path):
    """C-1: earliestAgeOrLowestStage must differ from latestAgeOrHighestStage
    when the species' range_base (older) and range_top (younger) resolve to
    different ICS stages."""
    result = {
        "sections": [
            {"name": "SecA", "coordinates": "31N, 117E"},
        ],
        "species_ranges": [
            {
                "species": "Ammonite a",
                "section": "SecA",
                "biozone": "Zone 1",
                "range_base": "Wuchiapingian",  # older
                "range_top": "Changhsingian",   # younger
            },
        ],
    }
    out = tmp_path / "dwc.zip"
    to_darwin_core_archive(result, str(out))
    header, data = _read_occurrence_txt(str(out))
    assert len(data) == 1
    row = dict(zip(header, data[0]))
    earliest = row["earliestAgeOrLowestStage"]
    latest = row["latestAgeOrHighestStage"]
    assert earliest and latest
    assert earliest != latest, (
        f"earliest ({earliest}) must differ from latest ({latest}) when "
        f"range_base != range_top"
    )

    # The same contract must hold for the non-archive occurrence conversion.
    occs = to_darwin_core_occurrences(result)
    assert occs[0]["earliestAgeOrLowestStage"] != occs[0]["latestAgeOrHighestStage"]


def test_signals_survive_export(tmp_path):
    """H4: endpoint_kind / occurrence_mode must be exported as columns and
    embedded in occurrenceRemarks."""
    result = {
        "sections": [{"name": "SecA", "coordinates": "31N, 117E"}],
        "species_ranges": [
            {
                "species": "Ammonite a",
                "section": "SecA",
                "biozone": "Zone 1",
                "range_base": "Wuchiapingian",
                "range_top": "Changhsingian",
                "endpoint_kind": "projected",
                "occurrence_mode": "reworked",
            },
        ],
    }
    out = tmp_path / "dwc.zip"
    to_darwin_core_archive(result, str(out))
    header, data = _read_occurrence_txt(str(out))
    row = dict(zip(header, data[0]))
    assert row.get("endpointKind") == "projected"
    assert row.get("occurrenceMode") == "reworked"
    assert "endpoint_kind=projected" in row["occurrenceRemarks"]
    assert "occurrence_mode=reworked" in row["occurrenceRemarks"]
