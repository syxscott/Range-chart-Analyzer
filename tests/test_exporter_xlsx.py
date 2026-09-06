"""Tests for rca_core.exporter to_xlsx, apply_table_edits, and shape routing.

Covers:
- HIGH: to_xlsx formula-injection sanitization (= + - @ triggers)
- MEDIUM: apply_table_edits preserves per-row _extras + non-data_keys
- MEDIUM: get_configs_for_result parity (empty abundances, columnar detection)
- HIGH: biozones config has 4 cols (incl. section) — mirrors JS table.js
- MEDIUM: NaN / Infinity values are sanitized in CSV / TSV / XLSX exports
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core import exporter


def _make_workbook(data):
    """Build the workbook via to_xlsx and return the openpyxl Workbook."""
    from openpyxl import load_workbook
    blob = exporter.to_xlsx(data)
    assert blob is not None, "to_xlsx returned None when given no file_or_path"
    return load_workbook(io.BytesIO(blob))


# Map table id -> sheet title (default identity translator is used so the
# title_key is the sheet name verbatim).
TABLE_ID_TO_SHEET = {
    "sections": "sec.sections",
    "species_ranges": "sec.species",
    "biozones": "sec.biozones",
    "other_fossils": "sec.fossils",
    "sites": "sec.sites",
    "abundances": "sec.abundances",
    "zones": "sec.zones",
    "fossil_legend": "sec.fossils",
    "lithology_legend": "sec.columnarLithology",
    "cross_beds": "sec.crossBeds",
}


def _sheet_for(wb, table_id):
    return wb[TABLE_ID_TO_SHEET[table_id]]


# ---------------------------------------------------------------------------
# HIGH: formula injection in to_xlsx
# ---------------------------------------------------------------------------

class TestToXlsxFormulaInjection(unittest.TestCase):
    """to_xlsx must mirror to_csv's OWASP formula-injection mitigation."""

    def _species_sheet_cells(self, data):
        wb = _make_workbook(data)
        # species_ranges sheet — pick the sheet whose first cell is the
        # "#" header and whose first column has integer index values.
        for ws in wb.worksheets:
            v = ws.cell(row=2, column=2).value if ws.max_row >= 2 else None
            if isinstance(v, str) and v.startswith("=cmd"):
                return ws
        return None

    def test_formula_trigger_is_text_not_formula(self):
        """A leading '=' cell must NOT be written as an openpyxl formula."""
        data = {
            "sections": [
                {"name": "S1", "age_range": "Permian", "formations": [],
                 "formation_thickness_m": "10m", "coordinates": ""},
            ],
            "species_ranges": [
                # Sprint B (REVIEW-2026-09-04): has_biozone_or_age was removed
                # from EXPORT_INVARIANTS, so biozone-less rows export fine;
                # this row keeps a biozone anyway (realistic payload).
                {"species": "=cmd|'/C calc'!A0", "section": "S1",
                 "range_base": "B1", "range_top": "B2", "biozone": "B Zone"},
            ],
            "biozones": [],
            "other_fossils": [],
        }
        wb = _make_workbook(data)
        ws = _sheet_for(wb, "species_ranges")
        # Column 2 = species (1=# index column)
        cell = ws.cell(row=2, column=2)
        # openpyxl records data_type 'f' for live formulas, 's' for text
        self.assertNotEqual(cell.data_type, "f",
                            f"formula-injection: cell kept as formula: value={cell.value!r}")
        # Mitigation: prefix with single quote so Excel treats as text
        self.assertEqual(cell.value, "'=cmd|'/C calc'!A0",
                         "expected leading single-quote sanitizer prefix")

    def test_hyperlink_trigger_is_text_not_formula(self):
        """=HYPERLINK(...) must not become a live formula in XLSX."""
        data = {
            "sections": [{"name": "S1", "age_range": "", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [
                # Sprint B (REVIEW-2026-09-04): biozone/age no longer required
                # by EXPORT_INVARIANTS (has_biozone_or_age removed); only the
                # required fields + range order constraints remain.
                {"species": "=HYPERLINK(\"http://evil/\")",
                 "section": "S1", "range_base": "B1", "range_top": "B2",
                 "biozone": "B Zone"},
            ],
            "biozones": [],
            "other_fossils": [],
        }
        wb = _make_workbook(data)
        cell = _sheet_for(wb, "species_ranges").cell(row=2, column=2)
        self.assertNotEqual(cell.data_type, "f")
        self.assertTrue(str(cell.value).startswith("'"))

    def test_negative_and_at_triggers_are_sanitized(self):
        """'-foo' and '@foo' must also be sanitized (OWASP coverage)."""
        data = {
            "sections": [{"name": "-bad", "age_range": "@bad", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [],
            "biozones": [],
            "other_fossils": [],
        }
        wb = _make_workbook(data)
        # sections sheet: col1=# index, col2=name, col3=age_range,
        # col4=formations, col5=thickness, col6=coordinates
        ws = _sheet_for(wb, "sections")
        name_cell = ws.cell(row=2, column=2)
        age_cell = ws.cell(row=2, column=3)
        self.assertNotEqual(name_cell.data_type, "f")
        self.assertNotEqual(age_cell.data_type, "f")
        self.assertTrue(str(name_cell.value).startswith("'"))
        self.assertTrue(str(age_cell.value).startswith("'"))

    def test_plain_text_is_unchanged(self):
        """Cells without formula triggers must not get a spurious quote prefix."""
        data = {
            "sections": [{"name": "Pingdingshan", "age_range": "Late Permian",
                          "formations": [], "formation_thickness_m": "9m",
                          "coordinates": "31N"}],
            "species_ranges": [],
            "biozones": [],
            "other_fossils": [],
        }
        wb = _make_workbook(data)
        ws = _sheet_for(wb, "sections")
        # Layout: col1=# index, col2=name, col3=age_range, ...
        self.assertEqual(ws.cell(row=2, column=2).value, "Pingdingshan")
        self.assertEqual(ws.cell(row=2, column=2).data_type, "s")


# ---------------------------------------------------------------------------
# HIGH: biozones config has 4 columns incl. section (parity with browser)
# ---------------------------------------------------------------------------

class TestBiozonesSectionColumn(unittest.TestCase):
    """The Python biozones table config must include the 'section' column,
    matching the schema that extractor.normalize_result preserves."""

    def test_biozones_has_section_in_cols_and_data_keys(self):
        cfgs = exporter.get_configs_for_result({
            "sections": [], "species_ranges": [], "other_fossils": [],
        })
        bz = next(c for c in cfgs if c["id"] == "biozones")
        self.assertIn("col.section", bz["cols"],
                      "biozones cols missing 'col.section'")
        self.assertIn("section", bz["data_keys"],
                      "biozones data_keys missing 'section'")

    def test_biozones_row_extracts_section(self):
        bz_row = {"name": "N. optima Zone", "section": "Pingdingshan",
                  "age": "Latest Changhsingian", "thickness_m": "3m"}
        cfgs = exporter.get_configs_for_result({
            "sections": [], "species_ranges": [], "other_fossils": [],
        })
        bz_cfg = next(c for c in cfgs if c["id"] == "biozones")
        row = bz_cfg["row"](bz_row)
        self.assertEqual(row[0], "N. optima Zone")
        self.assertEqual(row[1], "Pingdingshan",
                         "section must be column 2 of biozones row")

    def test_biozones_xlsx_export_has_section_column(self):
        data = {
            "sections": [{"name": "S1", "age_range": "", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [],
            "biozones": [{"name": "Zone A", "section": "S1", "age": "Permian",
                          "thickness_m": "5m"}],
            "other_fossils": [],
        }
        wb = _make_workbook(data)
        ws = _sheet_for(wb, "biozones")
        # Header row should include the section column header
        headers = [c.value for c in ws[1]]
        # We translated nothing → header text matches i18n key
        self.assertIn("col.section", headers)
        # Row 2 should have section value in column 3 (1=#, 2=name, 3=section)
        self.assertEqual(ws.cell(row=2, column=3).value, "S1")


# ---------------------------------------------------------------------------
# MEDIUM: apply_table_edits preserves _extras and per-row metadata
# ---------------------------------------------------------------------------

class TestApplyTableEditsPreservesExtras(unittest.TestCase):
    """apply_table_edits must not silently drop per-row _extras or other
    non-data_keys fields (e.g. per-row confidence) when rebuilding rows."""

    def test_species_row_preserves_extras(self):
        data = {
            "sections": [{"name": "S1", "age_range": "", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [
                {"species": "Neoalbaillella optima", "section": "S1",
                 "range_base": "B7", "range_top": "B9", "biozone": "Zone A",
                 "_extras": {"note": "rare", "confidence": 0.91}},
            ],
            "biozones": [],
            "other_fossils": [],
        }
        # Two columns per data_key + 1 index column = 6 cells
        rows = [
            ["1", "Neoalbaillella optima", "S1", "B7", "B9", "Zone A"],
        ]
        exporter.apply_table_edits(data, "species_ranges", rows)
        out = data["species_ranges"][0]
        self.assertIn("_extras", out,
                      "_extras dropped by apply_table_edits")
        self.assertEqual(out["_extras"].get("note"), "rare")
        self.assertEqual(out["_extras"].get("confidence"), 0.91)

    def test_biozones_row_preserves_extras(self):
        data = {
            "sections": [{"name": "S1", "age_range": "", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [],
            "biozones": [
                {"name": "Zone A", "section": "S1", "age": "Permian",
                 "thickness_m": "5m",
                 "_extras": {"extra_field": "preserved"}},
            ],
            "other_fossils": [],
        }
        rows = [["1", "Zone A", "S1", "Permian", "5m"]]
        exporter.apply_table_edits(data, "biozones", rows)
        out = data["biozones"][0]
        self.assertIn("_extras", out)
        self.assertEqual(out["_extras"].get("extra_field"), "preserved")

    def test_apply_edits_does_not_drop_unrelated_top_level_keys(self):
        """Editing one table must not wipe other tables' state."""
        data = {
            "sections": [{"name": "S1", "age_range": "Permian", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [
                {"species": "A", "section": "S1", "range_base": "B1",
                 "range_top": "B2", "biozone": "Zone A",
                 "_extras": {"k": 1}},
            ],
            "biozones": [{"name": "Z", "section": "S1", "age": "", "thickness_m": ""}],
            "other_fossils": ["Ammonoid: X"],
        }
        rows = [["1", "A-edited", "S1", "B1", "B2", ""]]
        exporter.apply_table_edits(data, "species_ranges", rows)
        # biozones / other_fossils / sections must be untouched
        self.assertEqual(len(data["biozones"]), 1)
        self.assertEqual(data["other_fossils"], ["Ammonoid: X"])


# ---------------------------------------------------------------------------
# MEDIUM: mode routing parity (empty abundances, columnar detection)
# ---------------------------------------------------------------------------

class TestModeRoutingParity(unittest.TestCase):
    """get_configs_for_result must mirror js/table.js detection order:
    non-empty abundances → abundance; sections[0].id (or legend-key
    fallback) → columnar; else range. Empty abundances list must NOT
    route to abundance tables (parity with js/table.js if also fixed)."""

    def test_empty_abundances_routes_to_range(self):
        """An empty abundances list must NOT promote a range-chart result
        to abundance tables. (Python's existing rule.)"""
        data = {
            "sections": [{"name": "S1", "age_range": "", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [{"species": "X", "section": "S1",
                                 "range_base": "B1", "range_top": "B2",
                                 "biozone": ""}],
            "abundances": [],
        }
        cfgs = exporter.get_configs_for_result(data)
        ids = [c["id"] for c in cfgs]
        # Must NOT have sites/zones/abundances (abundance tables)
        self.assertNotIn("sites", ids,
                          "empty abundances wrongly routed to abundance tables")
        self.assertNotIn("zones", ids)
        # Should have range-chart tables
        self.assertIn("species_ranges", ids)
        self.assertIn("sections", ids)

    def test_nonempty_abundances_routes_to_abundance(self):
        data = {
            "sections": [],
            "species_ranges": [],
            "sites": [{"name": "Site1"}],
            "abundances": [{"taxon": "Pinus", "site": "Site1", "level": "L1",
                            "depth": "0m", "abundance": "10",
                            "abundance_unit": "%"}],
            "zones": [{"name": "Zone A", "age": "Holocene", "level_range": "0-10"}],
        }
        cfgs = exporter.get_configs_for_result(data)
        ids = [c["id"] for c in cfgs]
        self.assertIn("sites", ids)
        self.assertIn("abundances", ids)
        self.assertIn("zones", ids)


# ---------------------------------------------------------------------------
# MEDIUM: NaN / Infinity sanitization in exported cells
# ---------------------------------------------------------------------------

class TestNaNSanitization(unittest.TestCase):
    """Model JSON may contain NaN/Infinity literals. They must NOT
    propagate into exported cells as the literal text 'NaN' or 'Inf'
    (which is invalid JSON if re-serialized and which breaks Excel
    number coercion)."""

    def test_csv_sanitizes_nan_cell(self):
        headers = ["col.species"]
        rows = [["=Foo"], [float("nan")], [float("inf")], [float("-inf")]]
        text = exporter.to_csv(headers, rows)
        # Strip BOM
        if text.startswith("﻿"):
            text = text[1:]
        lines = text.split("\r\n")
        # NaN/Inf cells must be rendered as empty (not the literal "nan")
        self.assertNotIn("nan", lines[2].lower())
        self.assertNotIn("inf", lines[3].lower())
        self.assertNotIn("inf", lines[4].lower())

    def test_tsv_sanitizes_nan_cell(self):
        headers = ["c"]
        rows = [[float("nan")], [float("inf")]]
        text = exporter.to_tsv(headers, rows)
        lines = text.split("\n")
        for line in lines[1:]:
            self.assertNotIn("nan", line.lower())
            self.assertNotIn("inf", line.lower())

    def test_xlsx_sanitizes_nan_cell(self):
        data = {
            "sections": [{"name": "S1", "age_range": "", "formations": [],
                          "formation_thickness_m": "", "coordinates": ""}],
            "species_ranges": [
                # NaN slips in via a float field; verify it doesn't
                # end up in the XLSX as the literal text "nan".
                {"species": "A", "section": "S1", "range_base": float("nan"),
                 "range_top": float("inf"), "biozone": "Zone A"},
            ],
            "biozones": [],
            "other_fossils": [],
        }
        wb = _make_workbook(data)
        ws = _sheet_for(wb, "species_ranges")
        # Find the species row (row 2)
        for col_idx in range(1, ws.max_column + 1):
            v = ws.cell(row=2, column=col_idx).value
            if v is None:
                continue
            sv = str(v).lower()
            self.assertNotEqual(sv, "nan", f"NaN leaked into XLSX col {col_idx}")
            self.assertNotEqual(sv, "inf", f"Inf leaked into XLSX col {col_idx}")


if __name__ == "__main__":
    unittest.main()