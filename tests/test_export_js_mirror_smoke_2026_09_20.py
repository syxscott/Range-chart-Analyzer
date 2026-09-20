"""JS-mirror drift guard + README accuracy lock for the export domain.

BORROW-2026-09-20 (export domain continuation). The previous pass landed the
Python side of the WebPlotDigitizer exchange (``to_wpd``, format
``rca-wpd/1``) and the PBDB upload columns, and stated in exporter.py that
"the browser mirror ``rcaToWpd`` in js/export.js must byte-for-byte reproduce
``files``" — but no JS existed and no test pinned it.

Boundary established by reading the actual mechanism:

  * the differential fixture (tests/gen_frontend_parity_fixtures.py, replayed
    by tests_diff_frontend_parity.js) covers the EXTRACTION groups only — it
    never loads js/export.js, so to_wpd / the PBDB columns are outside it;
  * index.html / js/app.js have no WPD or PBDB export entry point (the export
    buttons only use rcaToCsv/rcaToTsv/rcaDownload).

So js/export.js mirrors ONLY the pure scalar helpers (rcaWpdNum,
rcaWpdNumText, rcaWpdSlug) and tests_export_parity.js is the minimal Node
smoke. This module closes the loop the way the repo closes every JS/Python
pair: the case table embedded in tests_export_parity.js is recomputed here
against rca_core, so neither side can drift silently, plus the README claims
this section rewrote are asserted to describe the shipped behaviour.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rca_core.exporter import _wpd_num, _wpd_num_text, _wpd_slug

ROOT = Path(__file__).resolve().parents[1]
NODE_SMOKE = ROOT / "tests_export_parity.js"
EXPORT_JS = ROOT / "js" / "export.js"
README = ROOT / "README.md"


def _node_cases() -> dict:
    """The same marker-extracted table the Node smoke replays."""
    src = NODE_SMOKE.read_text(encoding="utf-8")
    m = re.search(
        r"__RCA_WPD_CASES_BEGIN__\s*\nconst CASES_SOURCE = `(.*?)`",
        src, re.S)
    assert m, "tests_export_parity.js lost the case-table markers"
    return json.loads(m.group(1))


class TestWpdJsMirrorCaseTable:
    """Every row of the Node case table must equal the Python answer."""

    def test_num_and_text_rows(self):
        cases = _node_cases()["num"]
        assert cases, "empty case table pins nothing"
        for case in cases:
            value = case["in"]
            got = _wpd_num(value)
            want = case["num"]
            assert (got is None) == (want is None), case
            if want is not None:
                assert got == want and float(got) == float(want), case
            assert _wpd_num_text(value) == case["text"], case

    def test_slug_rows(self):
        cases = _node_cases()["slug"]
        for case in cases:
            fallback = case.get("fallback")
            got = (_wpd_slug(case["in"], fallback)
                   if fallback is not None else _wpd_slug(case["in"]))
            assert got == case["slug"], case

    def test_integral_collapse_is_the_documented_rule(self):
        # The int branch of _wpd_num (JSON has no "2.0" spelling) — pinned
        # here rather than in the JSON table, which cannot carry the type.
        assert isinstance(_wpd_num(2.0), int)
        assert _wpd_num_text(2.0) == "2"


class TestExportJsMirrorExists:
    def test_pure_helpers_are_mirrored(self):
        src = EXPORT_JS.read_text(encoding="utf-8")
        for name in ("rcaWpdNum", "rcaWpdNumText", "rcaWpdSlug"):
            assert re.search(r"function %s\(" % name, src), name

    def test_no_full_builder_claimed_before_it_exists(self):
        # Boundary lock: rcaToWpd (the bundle builder) is deliberately NOT
        # mirrored while the frontend has no export entry; if someone wires
        # one up they must extend the differential fixture too, and this
        # guard's message says so.
        src = EXPORT_JS.read_text(encoding="utf-8")
        assert "function rcaToWpd(" not in src, (
            "rcaToWpd landed in js/export.js — add a to_wpd group to "
            "tests/gen_frontend_parity_fixtures.py and "
            "tests_diff_frontend_parity.js instead of a smoke table")


class TestReadmeExportClaims:
    """README must describe the SHIPPED behaviour, not the old overclaim."""

    def test_overclaim_is_gone(self):
        text = README.read_text(encoding="utf-8")
        assert "WebPlotDigitizer 友好的纯表格形态" not in text

    def test_wpd_section_names_the_real_artifacts(self):
        text = README.read_text(encoding="utf-8")
        for token in ("to_wpd", "rca-wpd/1", "wpd_axes.json"):
            assert token in text, token

    def test_pbdb_upload_columns_are_listed(self):
        text = README.read_text(encoding="utf-8")
        for token in ("genus", "subspecies", "max_ma_reso", "abund_value"):
            assert token in text, token
