"""occurrence_matrix / generic_table - the two modes added 2026-10-15.

Structured after tests/test_zonation_chart.py, the smallest complete
per-mode test file in this suite.

WHY THESE TESTS EXIST, in terms of a real run
=============================================
The step-0 baseline (a real taxon x locality matrix, real model, asked to answer
as an abundance_diagram) came back ok=True with 63 rows. Twelve taxa and
sixteen localities were read CORRECTLY. But:

  * rows were emitted for BLANK cells too, each one asserting "this taxon
    occurs at this locality" - the assertion a systematic section gets cited for;
  * the rotated locality headers landed in the taxon slot and the row labels in
    the level slot, producing a complete, well-formed, entirely wrong table;
  * rotated headers lost characters ("87 B1-4C" -> "87 BI 4C",
    "CAASP-84-20/15" -> "CAA-SP-84-").

None of that raised anything. That is the whole argument for this file: the
failure modes of this mode are silent, so each one gets a named test that fails
the moment it comes back.
"""

import json
import os
import subprocess

import pytest

from rca_core import chart_mode as cm
from rca_core import exporter, extractor, modes, prompt
from rca_core.aggregate import SCHEMA_BY_MODE, merge_results


SAMPLE = {
    "columns": [
        {"index": 0, "label": "87 B1-4C", "kind": "locality"},
        {"index": 1, "label": "87 JED 8", "kind": "locality"},
        {"index": 2, "label": "CAASP-84-20/15", "kind": "locality"},
    ],
    "legend": [
        {"code": "A", "class": "abundant"},
        {"code": "C", "class": "common"},
        {"code": "R", "class": "rare"},
    ],
    "occurrences": [
        {"taxon": "Caprinodoe angipes", "authority": "De Wever",
         "row_kind": "taxon", "site": "87 B1-4C", "col_index": 0,
         "code": "R", "response_kind": "extracted", "reason_codes": []},
        # BLANK CELL: no code and no class. Must not survive as a row.
        {"taxon": "Caprinodoe angipes", "row_kind": "taxon",
         "site": "87 JED 8", "col_index": 1, "code": "", "class": ""},
        {"taxon": "Capmusphaera crassa", "authority": "Yeh", "row_kind": "taxon",
         "site": "CAASP-84-20/15", "col_index": 2, "code": "A",
         "response_kind": "extracted", "reason_codes": []},
    ],
    "confidence": 0.82,
}


def _norm(payload=None):
    return extractor.normalize_occurrence_matrix_result(
        SAMPLE if payload is None else payload)


# ---------------------------------------------------------------------------
# The sparsity contract - the defect the baseline actually exhibited.
# ---------------------------------------------------------------------------

class TestSparseByContract:
    def test_blank_cells_do_not_produce_rows(self):
        out = _norm()
        assert len(out["occurrences"]) == 2, (
            "a cell with neither code nor class is a BLANK cell; it must not "
            "become a row, because absence is recoverable as the row that was "
            "not emitted while a blank row asserts the opposite")

    def test_the_drop_is_reported_not_silent(self):
        out = _norm()
        assert any("blank" in w for w in out.get("warnings", [])), (
            "the number of rows dropped as padding is the single most useful "
            "quality signal this answer carries; it must be visible")

    def test_a_zero_is_not_a_blank(self):
        """A printed 0 is a reading. Only an EMPTY cell is absent."""
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                    "occurrences": [{"taxon": "Pinus", "site": "L1",
                                     "code": "0", "class": ""}]})
        assert len(out["occurrences"]) == 1
        assert out["occurrences"][0]["code"] == "0"

    def test_class_alone_is_enough_to_keep_a_row(self):
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                    "occurrences": [{"taxon": "Pinus", "site": "L1",
                                     "code": "", "class": "abundant"}]})
        assert len(out["occurrences"]) == 1


# ---------------------------------------------------------------------------
# The transposition failure mode - the other silent one.
# ---------------------------------------------------------------------------

class TestColumnIdentity:
    def test_site_not_matching_any_column_is_flagged_not_accepted(self):
        out = _norm({"columns": [{"index": 0, "label": "87 B1-4C"}],
                    "occurrences": [{"taxon": "Pinus", "site": "87 JED 8",
                                     "code": "A"}]})
        row = out["occurrences"][0]
        assert row["response_kind"] == "uncertain"
        assert "unknown_column" in row["reason_codes"]

    def test_the_taxon_row_survives_a_flagged_site(self):
        """The taxon may still be readable even when the column is not; the
        row is kept so the name is not thrown away with the bad site."""
        out = _norm({"columns": [{"index": 0, "label": "87 B1-4C"}],
                    "occurrences": [{"taxon": "Pinus", "site": "GHOST",
                                     "code": "A"}]})
        assert len(out["occurrences"]) == 1
        assert out["occurrences"][0]["taxon"] == "Pinus"

    def test_spacing_variant_resolves_to_the_columns_own_spelling(self):
        out = _norm({"columns": [{"index": 0, "label": "87 B1-4C"}],
                    "occurrences": [{"taxon": "Pinus", "site": "87  B1-4C",
                                     "code": "A"}]})
        row = out["occurrences"][0]
        assert row["site"] == "87 B1-4C"
        # `.get`, not `[...]`: attach_row_contract only writes the coverage
        # contract when the source carried it, so a clean row legitimately has
        # no response_kind key at all.
        assert row.get("response_kind") != "uncertain"

    def test_column_order_is_preserved(self):
        out = _norm()
        assert [c["label"] for c in out["columns"]] == [
            "87 B1-4C", "87 JED 8", "CAASP-84-20/15"]
        assert [c["index"] for c in out["columns"]] == [0, 1, 2]


# ---------------------------------------------------------------------------
# The legend must be read from the figure, never assumed.
# ---------------------------------------------------------------------------

class TestLegendIsData:
    def test_class_is_looked_up_from_the_emitted_legend(self):
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                    "legend": [{"code": "X", "class": "very rare"}],
                    "occurrences": [{"taxon": "Pinus", "site": "L1",
                                     "code": "X"}]})
        assert out["occurrences"][0]["class"] == "very rare"

    def test_an_unconventional_letter_meaning_survives(self):
        """The A/R/C alphabet's alphabetical order coincides with its abundance
        order, which is what makes a hardcoded or sorted table silently right
        on the first paper and wrong on the next. So a convention where the
        letters do NOT sort that way has to come through untouched."""
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                    "legend": [{"code": "A", "class": "absent"},
                               {"code": "C", "class": "common"}],
                    "occurrences": [{"taxon": "Pinus", "site": "L1",
                                     "code": "A"}]})
        assert out["occurrences"][0]["class"] == "absent"

    def test_unknown_code_leaves_class_empty_rather_than_guessing(self):
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                    "occurrences": [{"taxon": "Pinus", "site": "L1",
                                     "code": "?"}]})
        assert out["occurrences"][0]["code"] == "?"
        assert out["occurrences"][0]["class"] == ""


# ---------------------------------------------------------------------------
# Shape / robustness.
# ---------------------------------------------------------------------------

class TestNormalizerRobustness:
    def test_non_dict_input(self):
        out = extractor.normalize_occurrence_matrix_result(None)
        assert out["occurrences"] == [] and out["columns"] == []

    def test_missing_occurrences_key(self):
        out = extractor.normalize_occurrence_matrix_result({"columns": []})
        assert out["occurrences"] == []

    def test_array_root_is_unwrapped(self):
        out = extractor.normalize_occurrence_matrix_result(
            {"_array_root": [{"index": 0, "label": "L1"},
                             {"taxon": "Pinus", "site": "L1", "code": "A"}]})
        assert out["columns"][0]["label"] == "L1"
        assert len(out["occurrences"]) == 1

    def test_a_column_with_no_header_is_dropped_with_a_warning(self):
        out = extractor.normalize_occurrence_matrix_result(
            {"columns": [{"index": 0, "label": ""}, {"index": 1, "label": "L1"}],
             "occurrences": []})
        assert len(out["columns"]) == 1
        assert any("header" in w for w in out.get("warnings", []))

    def test_row_with_no_taxon_is_dropped(self):
        out = extractor.normalize_occurrence_matrix_result(
            {"columns": [{"index": 0, "label": "L1"}],
             "occurrences": [{"taxon": "  ", "site": "L1", "code": "A"}]})
        assert out["occurrences"] == []

    def test_strat_row_kind_is_kept_and_distinguishable(self):
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                    "occurrences": [{"taxon": "Conodont level (see text)",
                                     "row_kind": "strat_correlation",
                                     "site": "L1", "code": "1"}]})
        assert out["occurrences"][0]["row_kind"] == "strat_correlation"

    def test_confidence_is_clamped(self):
        assert _norm({"columns": [], "occurrences": [], "confidence": 7})[
            "confidence"] == 1.0
        assert _norm({"columns": [], "occurrences": [], "confidence": -3})[
            "confidence"] == 0.0


# ---------------------------------------------------------------------------
# generic_table - the honest fallback. Its polarity is the OPPOSITE of
# occurrence_matrix, which is the easy thing to get wrong.
# ---------------------------------------------------------------------------

class TestGenericTableKeepsBlanks:
    def test_blank_cell_is_preserved(self):
        out = extractor.normalize_generic_table_result(
            {"columns": [{"label": "A"}, {"label": "B"}],
             "rows": [{"label": "r1", "cells": ["x", ""]}]})
        assert out["rows"][0]["cells"] == ["x", ""]

    def test_a_short_row_is_padded_not_truncated(self):
        """cells[i] addresses column i. Dropping the tail would shift every
        later value one place left, which is silent and wrong."""
        out = extractor.normalize_generic_table_result(
            {"columns": [{"label": "A"}, {"label": "B"}, {"label": "C"}],
             "rows": [{"label": "r1", "cells": ["x"]}]})
        assert out["rows"][0]["cells"] == ["x", "", ""]

    def test_refusal_survives(self):
        out = extractor.normalize_generic_table_result(
            {"columns": [], "rows": [], "notes": "a specimen photograph",
             "confidence": 0.1})
        assert out["rows"] == []
        assert out["notes"] == "a specimen photograph"

    def test_it_is_never_auto_detected(self):
        assert modes.MODES["generic_table"].keywords_ascii == ()
        assert modes.MODES["generic_table"].keywords_cjk == ()

    def test_it_is_not_classifier_nameable(self):
        assert "generic_table" not in extractor.KNOWN_CHART_TYPES


# ---------------------------------------------------------------------------
# Registration. A mode that exists in one layer and not another is the defect
# modes.py was written to remove, so each layer is asserted explicitly.
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_dispatch_entry_exists(self):
        assert "occurrence_matrix" in extractor._MODE_DISPATCH
        assert "generic_table" in extractor._MODE_DISPATCH

    def test_prompt_version_declared(self):
        assert prompt.PROMPT_VERSION["occurrence_matrix"] == "v1"
        assert prompt.PROMPT_VERSION["generic_table"] == "v1"

    def test_prompt_version_is_derived_not_hand_written(self):
        assert prompt.PROMPT_VERSION == modes.PROMPT_VERSIONS

    def test_registry_declares_the_mode(self):
        spec = modes.MODES["occurrence_matrix"]
        assert spec.maturity == "assistant"
        assert spec.export_supported is True
        assert spec.server_supported is True
        # Phase 1 is backend-only on purpose.
        assert spec.browser_supported is False

    def test_occurrence_matrix_is_known_to_the_classifier(self):
        assert "occurrence_matrix" in extractor.KNOWN_CHART_TYPES

    def test_browser_refuses_rather_than_falling_through_to_range_chart(self):
        assert "occurrence_matrix" in modes.BROWSER_UNSUPPORTED_MODES

    def test_root_keys_declared(self):
        # 2026-10-16: `cell_status` and `taxon_rows` joined the list.
        # `cell_status` says how the cells were obtained and the exporter reads
        # it to choose between a plain and a caveated sheet title. `taxon_rows`
        # carries the labels-only path's taxon names, which cannot ride in
        # `occurrences` because that list is sparse by contract and drops a row
        # with no code. Keys the normalizer emits but modes.py does not declare
        # are invisible to the "unrelated root key" guard in _ok_result.
        assert extractor._mode_root_keys("occurrence_matrix") == (
            "columns", "legend", "occurrences", "strat_rows", "taxon_rows",
            "confidence", "cell_status")

    def test_merge_schema_exists_and_is_wired(self):
        assert "occurrence_matrix" in SCHEMA_BY_MODE
        assert "occurrence_matrix" in SCHEMA_BY_MODE.keys() and \
            "occurrence_matrix" in __import__(
                "rca_core.aggregate", fromlist=["WIRED_MODES"]).WIRED_MODES

    def test_generic_table_has_no_merge_schema_on_purpose(self):
        """It carries no row table, so merging it would apply the range-chart
        schema's identity fields to a structure that has none."""
        assert "generic_table" not in SCHEMA_BY_MODE


# ---------------------------------------------------------------------------
# Detection: positives for the new mode, and - more importantly - that the
# eight pre-existing modes still win exactly where they used to.
# ---------------------------------------------------------------------------

class TestAutoDetect:
    @pytest.mark.parametrize("caption", [
        "Miscellaneous localities",
        "range of occurrences across localities",
        "conodont occurrence matrix from boreholes",
        "A/R/C frequency matrix",
    ])
    def test_positive(self, caption):
        assert cm.auto_detect_chart_mode_ex(caption) == (
            "occurrence_matrix", True)

    def test_localit_is_a_stem_not_a_whole_word(self):
        """Without the stem registration `localit` matches the word 'localit'
        and never 'localities' - which is the word the caption actually
        prints. This failure would be silent."""
        assert "localit" in cm._STEMS
        assert cm.auto_detect_chart_mode_ex("fossil localities")[0] \
            == "occurrence_matrix"

    @pytest.mark.parametrize("caption,expected", [
        ("range chart", "range_chart"),
        ("pollen diagram", "abundance_diagram"),
        ("Columnar section", "columnar_section"),
        ("phylogenetic tree", "phylogenetic_tree"),
        ("Biozonation", "zonation_chart"),
        ("isotope curve", "chemical_stratigraphy"),
        ("palaeogeographic map", "paleomap"),
        ("scatter plot", "scatter_plot"),
    ])
    def test_no_pre_existing_mode_is_stolen(self, caption, expected):
        assert cm.auto_detect_chart_mode_ex(caption)[0] == expected

    def test_a_map_caption_mentioning_localities_stays_a_map(self):
        """occurrence_matrix's keywords name a COLUMN KIND, not a genre. A
        palaeomap caption that says 'fossil localities' must keep reaching
        paleomap, which is why the new branch is last in the cascade."""
        assert cm.auto_detect_chart_mode_ex(
            "palaeogeographic map with fossil localities")[0] == "paleomap"

    def test_an_explicit_range_chart_caption_still_wins(self):
        assert cm.auto_detect_chart_mode_ex(
            "range chart of the localities")[0] == "range_chart"

    def test_unmatched_text_still_falls_back_to_range_chart(self):
        assert cm.auto_detect_chart_mode_ex("figure 7")[0] == "range_chart"


# ---------------------------------------------------------------------------
# Merge + export, end to end.
# ---------------------------------------------------------------------------

class TestMerge:
    def _one(self, code, klass="rare"):
        return extractor.normalize_occurrence_matrix_result({
            "columns": [{"index": 0, "label": "87 B1-4C"}],
            "occurrences": [{"taxon": "Caprinodoe", "site": "87 B1-4C",
                             "code": code, "class": klass}],
            "confidence": 0.8})

    def test_same_taxon_same_site_merges_to_one_row(self):
        merged = merge_results([self._one("R"), self._one("R"),
                                self._one("", "")])
        rows = [r for r in merged["occurrences"]
                if r.get("taxon") == "Caprinodoe"]
        assert len(rows) == 1, "the same taxon at the same locality is ONE record"

    def test_the_same_taxon_at_two_localities_stays_two_rows(self):
        merged = merge_results([
            self._one("R"),
            extractor.normalize_occurrence_matrix_result({
                "columns": [{"index": 0, "label": "87 JED 8"}],
                "occurrences": [{"taxon": "Caprinodoe", "site": "87 JED 8",
                                 "code": "A", "class": "abundant"}],
                "confidence": 0.8}),
        ])
        assert len(merged["occurrences"]) == 2


class TestExport:
    def test_tables_are_produced(self):
        cfgs = exporter.get_configs_for_result(_norm())
        assert [c["id"] for c in cfgs] == ["columns", "occurrences", "legend"]

    def test_the_occurrence_row_is_the_one_a_section_cites(self):
        cfg = next(c for c in exporter.get_configs_for_result(_norm())
                   if c["id"] == "occurrences")
        row = cfg["row"](_norm()["occurrences"][0])
        assert row[0] == "Caprinodoe angipes"
        assert row[1] == "De Wever"
        assert row[2] == "87 B1-4C"
        assert row[3] == "R"

    def test_blank_rows_never_reach_the_export(self):
        cfgs = exporter.get_configs_for_result(_norm())
        occ = next(c for c in cfgs if c["id"] == "occurrences")
        rows = [occ["row"](r) for r in _norm()["occurrences"]]
        assert all(r[3] for r in rows), "an exported row with an empty code is padding"

    def test_the_matrix_is_not_mistaken_for_a_down_core_abundance_diagram(self):
        """Both carry rows with a taxon and a value; only the column header
        list distinguishes them."""
        assert exporter._looks_occurrence_matrix(_norm()) is True
        assert exporter._looks_abundance(_norm()) is False


# ---------------------------------------------------------------------------
# Registry hygiene.
# ---------------------------------------------------------------------------

class TestRegression_2026_10_15_Review:
    """Seven defects found by review after the first green run.

    Every test here corresponds to a real defect that passed the whole suite
    at the time it was written. They are collected together because the shared
    lesson is the point: the original 55 tests covered each mode's HAPPY PATH
    in isolation, and all seven defects lived in the gaps between modes, in
    type boundaries, and in field self-consistency — none of which a
    single-mode happy path can reach.
    """

    # --- D1: exporter classified generic_table as an occurrence matrix -----
    # `_looks_occurrence_matrix` keyed on `columns`, which generic_table ALSO
    # emits. The export then returned the wrong three configs and the generic
    # table's `rows` vanished from CSV/XLSX while staying visible in the
    # browser.
    def test_d1_generic_table_data_survives_the_export(self):
        payload = extractor.normalize_generic_table_result({
            "columns": [{"index": 0, "label": "Alpha"}, {"index": 1, "label": "Beta"}],
            "rows": [{"index": 0, "label": "r1", "cells": ["1", ""]}],
            "legend": [{"code": "1", "class": "one"}],
        })
        ids = [c["id"] for c in exporter.get_configs_for_result(payload)]
        assert "rows" in ids, (
            "generic_table output was being classified as an occurrence matrix, "
            "so its rows never reached the export")
        assert exporter._looks_occurrence_matrix(payload) is False

    def test_d1_an_empty_occurrence_matrix_still_exports_its_columns(self):
        """The fix must not degrade a matrix whose cells were not read: its
        columns and legend are still real data and must still render."""
        payload = extractor.normalize_occurrence_matrix_result(
            {"columns": [{"index": 0, "label": "L1"}], "occurrences": []})
        ids = [c["id"] for c in exporter.get_configs_for_result(payload)]
        assert "columns" in ids

    # --- D2: js/quality.js branch order diverged from Python ---------------
    def test_d2_python_and_js_detectors_agree_on_the_ordering_case(self):
        """The step-0 baseline shape carries BOTH. Python checks occurrences
        before abundances; the JS mirror checked abundances first and graded
        the same payload on fields (level/depth/abundance_unit) the matrix
        does not have.

        This EXECUTES the JS detector through node and compares verdicts. An
        earlier version of this test only inspected the ORDER of `return`
        statements in the source, which does not catch a changed condition --
        mutating the branch's predicate to `if (false)` left that test green
        while the detector was broken. Running the code is the only probe
        that cannot be satisfied by re-arranging prose.
        """
        payload = {"columns": [{"index": 0, "label": "L1"}],
                   "occurrences": [{"taxon": "P", "site": "L1", "code": "A"}],
                   "abundances": [{"taxon": "P", "level": "10", "abundance": "5"}]}

        from rca_core.quality import _detect_mode
        py_verdict = _detect_mode(payload)
        assert py_verdict == "occurrence_matrix"

        js_path = os.path.join(os.path.dirname(__file__), "..", "js", "quality.js")
        src = open(js_path, encoding="utf-8").read()
        body = src[src.index("function _detectMode("):]
        body = body[:body.index("\n}\n") + 3]
        # _CONTENT_KEYS / _COLUMNAR_MARKERS are the detector's other inputs.
        for name in ("_CONTENT_KEYS", "_COLUMNAR_MARKERS"):
            chunk = src[src.index("const %s = [" % name):]
            body = chunk[:chunk.index("];") + 2] + "\n" + body

        script = body + "\nconsole.log(_detectMode(%s));" % json.dumps(payload)
        # encoding is load-bearing, not decoration: on this Windows box the
        # default locale codec is GBK, so text=True would raise
        # UnicodeDecodeError on js/quality.js's non-ASCII comments and the test
        # would go RED for a decoding failure while the detector was fine.
        # A regression test that can fail for an unrelated reason is a test
        # that will be trusted when it should not be.
        r = subprocess.run(["node", "-e", script], capture_output=True,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, ("node could not evaluate _detectMode: %s"
                                   % (r.stderr or "")[-400:])
        js_verdict = (r.stdout or "").strip()
        assert js_verdict == py_verdict, (
            "the two quality engines disagree on the step-0 baseline shape: "
            "python=%s js=%s — the browser would grade a locality matrix "
            "against abundance-diagram fields it does not have"
            % (py_verdict, js_verdict))

    def test_d2_js_detector_source_still_carries_the_branch(self):
        """Belt-and-braces: the JS file must physically contain the branch,
        so deleting it is caught even if node is unavailable in some run."""
        js_path = os.path.join(os.path.dirname(__file__), "..", "js", "quality.js")
        src = open(js_path, encoding="utf-8").read()
        assert "return 'occurrence_matrix'" in src

    # --- D3: integer 0 treated as an absent cell --------------------------
    @pytest.mark.parametrize("raw_code,rendered", [
        (0, "0"), (0.0, "0.0"), (False, "false"),
    ])
    def test_d3_a_falsy_value_is_a_reading_not_a_blank_cell(
            self, raw_code, rendered):
        """A bare `or` chain short-circuits on every falsy value, so a printed
        0 fell through to the next field, read as an empty cell, and was
        DROPPED with a false "blank cells" warning.

        The exact spelling after stringification is _stringify_scalar's
        business and deliberately not asserted here; the contract is only
        that the row survives and no blank-cell warning is raised.
        """
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                     "occurrences": [{"taxon": "Pinus", "site": "L1",
                                      "code": raw_code}]})
        assert len(out["occurrences"]) == 1, (
            "a falsy but present value must survive as a reading")
        assert out["occurrences"][0]["code"] == rendered
        assert not any("blank" in w for w in out.get("warnings", [])), (
            "a zero must not produce a 'dropped blank cells' warning")

    def test_d3_a_genus_named_zero_is_not_reported_as_a_missing_label(self):
        """The same short-circuit hit `taxon`. Under the fix `0` stringifies
        to "0", so the row is kept — which is the point: the regression was
        the row vanishing into the 'no taxon label' path, not whether 0 is a
        sensible genus."""
        out = _norm({"columns": [{"index": 0, "label": "L1"}],
                     "occurrences": [{"taxon": 0, "site": "L1", "code": "A"}]})
        assert len(out["occurrences"]) == 1
        assert out["occurrences"][0]["taxon"] == "0"
        assert not any("no taxon" in w for w in out.get("warnings", []))

    def test_d3_a_genuinely_absent_code_is_still_a_blank_cell(self):
        """The fix must not turn "missing" into "present": None and "" remain
        blank."""
        for absent in (None, "", "   "):
            out = _norm({"columns": [{"index": 0, "label": "L1"}],
                         "occurrences": [{"taxon": "Pinus", "site": "L1",
                                          "code": absent}]})
            assert out["occurrences"] == [], (
                "None / empty / whitespace is absence, not a reading: %r"
                % (absent,))

    # --- D4: col_index contradicting site was passed through silently -----
    def test_d4_a_col_index_that_contradicts_its_site_is_flagged(self):
        """site is the authoritative identity. A row with site="B" and
        col_index=0 — a valid index, just the WRONG one — used to reach the
        export with two contradictory identity fields, no warning, no code."""
        out = _norm({"columns": [{"index": 0, "label": "A"},
                                 {"index": 1, "label": "B"}],
                     "occurrences": [{"taxon": "Pinus", "site": "B",
                                      "col_index": 0, "code": "A"}]})
        row = out["occurrences"][0]
        assert row["response_kind"] == "uncertain"
        assert "column_index_conflict" in row["reason_codes"]
        assert any("col_index" in w for w in out.get("warnings", []))

    def test_d4_a_consistent_pair_is_left_alone(self):
        out = _norm({"columns": [{"index": 0, "label": "A"},
                                 {"index": 1, "label": "B"}],
                     "occurrences": [{"taxon": "Pinus", "site": "B",
                                      "col_index": 1, "code": "A"}]})
        row = out["occurrences"][0]
        assert row["col_index"] == 1
        assert row.get("response_kind") != "uncertain"

    # --- D5: a registry reference that pointed at nothing -----------------
    def test_d5_every_registry_reference_resolves(self):
        """`normalizer_ref` for chemical_stratigraphy named a function that
        does not exist, and NOTHING in the repo parsed these strings — which
        is the exact class of "missed it and nothing complained" this registry
        was built to remove."""
        import importlib
        for name, spec in modes.MODES.items():
            for attr in ("normalizer_ref", "prompt_ref"):
                ref = getattr(spec, attr)
                if not ref:
                    continue
                mod, _, fn = ref.partition(":")
                obj = getattr(importlib.import_module(mod), fn)
                if attr == "prompt_ref":
                    assert isinstance(obj, str), f"{name}.{attr} is not a string"
                else:
                    assert callable(obj), f"{name}.{attr} is not callable"

    # --- D6: an unreachable duplicate detector branch ----------------------
    def test_d6_the_detector_has_exactly_one_occurrence_branch(self):
        import inspect

        from rca_core import quality
        src = inspect.getsource(quality._detect_mode)
        hits = src.count('return "occurrence_matrix"')
        assert hits == 1, (
            "a second, unreachable copy of the branch existed; dead code in a "
            "detector is what let js/quality.js drift to the wrong position")
    def test_prompt_version_matches_the_registry_for_every_mode(self):
        for name, spec in modes.MODES.items():
            assert prompt.PROMPT_VERSION[name] == spec.prompt_version

    def test_browser_unsupported_is_derived_from_the_flags(self):
        expected = {n for n, s in modes.MODES.items()
                    if not s.browser_supported}
        assert set(modes.BROWSER_UNSUPPORTED_MODES) == expected

    def test_every_registered_mode_declares_notes(self):
        for name, spec in modes.MODES.items():
            assert spec.notes, f"{name} has no notes; the capability UI shows them"
