"""AUDIT-2026-10-22: the standards export writes both standards, and says what
it did not publish.

The README advertised Darwin Core and PBDB deliverables; the writers existed
but were reachable only from Python, so the desktop GUI could not produce the
files the documentation told users to publish. This module pins the Qt-free
core of that export.
"""
from __future__ import annotations

import csv
import json
import os
import zipfile
from pathlib import Path

import pytest

from rca_core.standards_export import export_standards, standards_available


def _result():
    return {
        "sections": [{"name": "Section A",
                      "age_range": "Late Permian (Wuchiapingian - Changhsingian)"}],
        "species_ranges": [
            {"species": "Neoalbaillella optima", "section": "Section A",
             "range_base": "Bed 7", "range_top": "Bed 9",
             "range_top_idx": 9, "range_base_idx": 7,
             "response_kind": "extracted", "confidence": 0.9},
            # examined and not found -> not an occurrence
            {"species": "Pseudofusulina sp.", "section": "Section A",
             "response_kind": "not_drawn", "confidence": 0.4},
            # the name could not be read -> not a publishable occurrence
            {"species": "", "section": "Section A",
             "response_kind": "uncertain", "note": "unclear", "confidence": 0.2},
        ],
        "biozones": [{"name": "N. optima Zone", "section": "Section A",
                      "age": "Latest Changhsingian"}],
        "other_fossils": [],
        "confidence": 0.8,
    }


def test_the_writers_are_available():
    ok, why = standards_available()
    assert ok, why


def test_export_standards_writes_both_standards(tmp_path):
    out = export_standards(_result(), str(tmp_path), prefix="fig8-")
    assert out["ok"], out["errors"]
    names = set(out["files"])
    assert "darwin_core" in names, names
    for key in ("pbdb_occurrences", "pbdb_extensions", "pbdb_collections"):
        assert key in names, (key, names)
    for path in out["files"].values():
        assert os.path.exists(path), path


def test_the_archive_is_a_readable_darwin_core_archive(tmp_path):
    out = export_standards(_result(), str(tmp_path))
    with zipfile.ZipFile(out["files"]["darwin_core"]) as zf:
        names = zf.namelist()
        assert "occurrence.txt" in names
        assert "meta.xml" in names
        assert "eml.xml" in names
        raw = zf.read("occurrence.txt").decode("utf-8")
    rows = list(csv.reader(raw.splitlines(), delimiter="\t"))
    header = rows[0]
    data = [r for r in rows[1:] if r]
    # only the row that claims to be an occurrence is published
    assert len(data) == 1, data
    rec = dict(zip(header, data[0]))
    assert rec["scientificName"] == "Neoalbaillella optima"


def test_the_rows_that_were_not_published_are_reported(tmp_path):
    out = export_standards(_result(), str(tmp_path))
    reasons = sorted(w.get("reason") for w in out["warnings"])
    assert reasons == ["not_drawn", "uncertain"], out["warnings"]
    for w in out["warnings"]:
        assert w.get("detail"), w


def test_the_pbdb_sheets_have_the_documented_headers(tmp_path):
    out = export_standards(_result(), str(tmp_path))
    with open(out["files"]["pbdb_occurrences"], encoding="utf-8-sig",
              newline="") as fh:
        header = next(csv.reader(fh))
    assert "taxon_name" in header
    assert "collection_no" in header
    with open(out["files"]["pbdb_extensions"], encoding="utf-8-sig",
              newline="") as fh:
        header = next(csv.reader(fh))
    for col in ("early_interval", "late_interval", "max_ma", "min_ma",
                "max_ma_reso", "notes"):
        assert col in header, col


def test_an_unusable_result_is_reported_not_raised(tmp_path):
    out = export_standards({}, str(tmp_path))
    assert out["ok"] is False
    assert out["errors"]
