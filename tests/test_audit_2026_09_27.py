"""Regression tests for the AUDIT-2026-09-27 full-repository review wave.

Every test here corresponds to a defect that was CONFIRMED by executing the
reported trigger before the fix. They are the dated artefact of that wave in
the repository's established convention: one review round -> one new test
module -> no editing of the earlier round's files.

The wave's theme was a single blind spot: **"unmeasurable / unrecognised" was
treated as "fine"**. Silently-dropped rows, a deleted taxon, a lost axis
calibration, a rejected cell that trapped the keyboard, a negative number
demoted to text, an API key overwritten with "" — none of them raised, so none
of them was visible. Where an OLD test pinned the defective behaviour, that
test was revised with the reason recorded in place (see
test_chimera_section.py, test_aggregate_row_voting.py,
test_review_2026_09_20_extraction_core_tail.py and tests_edit_history.js);
this module covers the rest and locks the fixes in.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from rca_core import exporter as E                                   # noqa: E402
from rca_core import extractor as X                                  # noqa: E402
from rca_core import json_utils as J                                  # noqa: E402
from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results     # noqa: E402
from rca_core.editable import (apply_edits, capture_edits,    # noqa: E402
                                   is_dirty)
from rca_core.quality import score_range_chart                       # noqa: E402


# ---------------------------------------------------------------------------
# P0-1  paleomap / scatter_plot never consumed the _array_root rescue
# ---------------------------------------------------------------------------
class TestP0ArrayRootUnwrap:
    """6 of 8 normalizers unwrapped ``{"_array_root": [...]}``; these two did
    not, so a bare-array reply produced empty tables AND ``ok=True``. The
    shared classifier is a RANGE-CHART heuristic that maps any dict with a
    ``name`` onto "sections", which is not a paleomap table at all."""

    def test_paleomap_keeps_a_bare_array_payload(self):
        parsed = J.safe_json_loads('[{"name": "Laurasia"}, {"name": "Pangaea"}]')
        out = X.normalize_paleomap_result(parsed)
        assert len(out["continents"]) == 2, out
        # Rows must go through the normalizer, not land raw in _unclassified.
        assert not out.get("_unclassified")
        assert out["continents"][0]["name"] == "Laurasia"

    def test_scatter_keeps_a_bare_array_payload(self):
        parsed = J.safe_json_loads('[{"name": "Cluster A"}, {"name": "Cluster B"}]')
        out = X.normalize_scatter_plot_result(parsed)
        assert len(out["groups"]) == 2, out

    def test_scatter_routes_a_coordinate_pair_to_points(self):
        parsed = J.safe_json_loads('[{"x": 1, "y": 2}, {"x": 3, "y": 4}]')
        out = X.normalize_scatter_plot_result(parsed)
        assert len(out["points"]) == 2, out
        assert out["points"][0]["x"] == "1"

    def test_scatter_routes_a_reason_to_outliers(self):
        parsed = J.safe_json_loads('[{"x": 1, "y": 2, "reason": "extreme"}]')
        out = X.normalize_scatter_plot_result(parsed)
        assert len(out["outliers"]) == 1, out

    def test_paleomap_routes_a_lat_lon_to_fossil_sites(self):
        parsed = J.safe_json_loads('[{"name": "S1", "lat_lon": "31N,117E"}]')
        out = X.normalize_paleomap_result(parsed)
        assert len(out["fossil_sites"]) == 1, out

    def test_a_well_formed_paleomap_payload_is_untouched(self):
        data = {"continents": [{"name": "Laurasia", "type": "continent"}],
                "confidence": 0.9}
        out = X.normalize_paleomap_result(copy.deepcopy(data))
        assert len(out["continents"]) == 1
        assert out["continents"][0]["name"] == "Laurasia"
        assert not out.get("_warnings")


# ---------------------------------------------------------------------------
# P1  bare-string rows were silently dropped in four modes
# ---------------------------------------------------------------------------
class TestP1BareStringRows:
    """``_dict_rows`` skipped non-dict items, so ``{"continents": ["Laurasia",
    "Pangaea"]}`` came back as six empty tables with ``ok=True`` and NO
    warning. ``_iter_rows`` already coerced and flagged for range_chart /
    abundance / zonation; the gap was the four modes that used ``_dict_rows``."""

    @pytest.mark.parametrize("fn,key,payload", [
        (X.normalize_result, "sections", {"sections": ["Pingdingshan"]}),
        (X.normalize_columnar_result, "sections", {"sections": ["Section A"]}),
        (X.normalize_paleomap_result, "continents",
         {"continents": ["Laurasia", "Pangaea"]}),
        (X.normalize_paleomap_result, "paleolatitude_indicators",
         {"paleolatitude_indicators": ["Equator"]}),
        (X.normalize_scatter_plot_result, "groups", {"groups": ["Cluster A"]}),
        (X.normalize_chemical_stratigraphy_result, "events",
         {"events": ["CIE C2-C1"]}),
    ])
    def test_bare_string_rows_are_coerced_and_flagged(self, fn, key, payload):
        out = fn(dict(payload, confidence=0.9))
        assert len(out.get(key) or []) >= 1, (key, out)
        assert "string_row_coerced" in (out.get("_warnings") or []), (key, out)

    @pytest.mark.parametrize("fn,key,payload", [
        (X.normalize_abundance_result, "abundances", {"abundances": ["Pinus"]}),
        (X.normalize_scatter_plot_result, "points", {"points": ["1,2"]}),
    ])
    def test_tables_without_a_single_field_shape_still_drop(self, fn, key, payload):
        """A bare string is not a measurement. Coercing it would invent a taxon
        with no abundance and a point with no coordinates — tests_core already
        pins that for abundances, and the same reasoning covers points."""
        out = fn(dict(payload, confidence=0.9))
        assert out.get(key) == [], (key, out)

    def test_range_chart_behaviour_is_unchanged(self):
        out = X.normalize_result({"sections": ["Pingdingshan"], "confidence": 0.9})
        assert len(out["sections"]) == 1
        assert "string_row_coerced" in out["_warnings"]


# ---------------------------------------------------------------------------
# P1  the chimera rule deleted taxa every run agreed were present
# ---------------------------------------------------------------------------
def _range_run(base, top, zone):
    return {"sections": [{"name": "S1"}],
            "species_ranges": [{"species": "Neoalbaillella optima", "section": "S1",
                                "range_base": base, "range_top": top,
                                "biozone": zone}],
            "biozones": [], "confidence": 0.8, "runs": 1}


class TestP1RecombinationIsFlaggedNotDeleted:
    """``_mode`` breaks a 1-1 tie with sorted-first-wins, so the merged tuple is
    only ever observed when EVERY field's winner came from the same run. Two
    runs disagreeing on >= 2 of the four fields therefore ALWAYS produced an
    "unobserved" tuple — ordinary OCR disagreement — and the row was deleted.
    Raising `runs`, the documented way to be MORE reliable, removed taxa, and
    ``chimera_warnings`` was read by gui_fluent.py alone so the HTTP/browser
    path never learned a row had gone."""

    def test_two_field_disagreement_keeps_the_row(self):
        runs = [_range_run("Bed 7", "Bed 9", "Zone B"),
                _range_run("Bed 7", "Bed 11", "Zone C")]
        merged = merge_results(runs, total_runs=2, schema=RANGE_CHART_SCHEMA)
        rows = merged["species_ranges"]
        assert len(rows) == 1, rows
        assert rows[0]["_warning"] == "recombined_consensus"

    def test_the_ballots_are_recorded(self):
        runs = [_range_run("Bed 7", "Bed 9", "Zone B"),
                _range_run("Bed 7", "Bed 11", "Zone C")]
        merged = merge_results(runs, total_runs=2, schema=RANGE_CHART_SCHEMA)
        ballots = merged["species_ranges"][0]["_recombination_ballots"]
        assert len(ballots) == 2, ballots
        assert {b["votes"] for b in ballots} == {1}
        # ...and the warning carries them too, so every consumer sees it.
        assert len(merged["chimera_warnings"][0]["ballots"]) == 2
        assert "not dropped" in merged["chimera_warnings"][0]["reason"]

    def test_ballot_ORDER_is_the_python_tuple_order(self):
        """P2: ballots are ORDERED (votes desc, then the tuple itself).

        Every other ballot assertion in the repo checked only `len()` and the
        set of vote counts, so an engine could reorder the ballots freely and
        stay green. js/aggregate.js did: it tie-broke on `JSON.stringify(tup)`
        instead of on the tuple, which orders PREFIXES BACKWARDS because the
        separator ',' (0x2C) sorts after the space (0x20) inside a value.
        "bed 1" vs "bed 1 (rp13)" therefore landed in the OPPOSITE order from
        Python -- same disagreement, opposite presentation, and the first
        ballot listed is the one an operator reads first.

        This pins the order, and tests_ui_fixes_2026_09_27.js pins the same
        literal on the JS side, so neither engine can drift again.
        """
        runs = [_range_run("bed 1", "bed 9", "zone c"),
                _range_run("bed 1 (rp13)", "bed 9", "zone b")]
        merged = merge_results(runs, total_runs=2, schema=RANGE_CHART_SCHEMA)
        row = merged["species_ranges"][0]
        # Two runs, 1-1 tie on every field; the per-field winners come from
        # different runs, so the merged tuple is observed by neither.
        assert row["_warning"] == "recombined_consensus", row
        got = [b["range_base"] for b in row["_recombination_ballots"]]
        assert got == ["bed 1", "bed 1 (rp13)"], got
        # The warning payload the GUI renders must carry the same order.
        assert [b["range_base"]
                for b in merged["chimera_warnings"][0]["ballots"]] == got

    def test_agreeing_runs_are_not_flagged(self):
        runs = [_range_run("Bed 7", "Bed 9", "Zone B"),
                _range_run("Bed 7", "Bed 9", "Zone B")]
        merged = merge_results(runs, total_runs=2, schema=RANGE_CHART_SCHEMA)
        assert len(merged["species_ranges"]) == 1
        assert merged["species_ranges"][0].get("_warning") is None
        assert not merged.get("chimera_warnings")


class TestP1RootKeysSurviveMultiRun:
    """The N-run path built `out` from the primary list + list_keys +
    confidence, so any figure-level key the single-run path keeps was dropped
    by raising the run count. `axis_calibration` is a FIRST-CLASS hoisted root
    key and js/viz.js reads it — so the more carefully you extracted, the more
    axis evidence you lost."""

    def _run(self):
        return {"sections": [{"name": "S1"}],
                "species_ranges": [{"species": "A optima", "section": "S1",
                                    "range_base": "Bed 7", "range_top": "Bed 9"}],
                "axis_calibration": {"vertical": {"at_0": 1, "at_999": 24,
                                                  "unit": "bed"}},
                "_extras": {"note": "caption"},
                "confidence": 0.8, "runs": 1}

    def test_axis_calibration_survives_two_runs(self):
        two = merge_results([self._run(), self._run()], total_runs=2,
                            schema=RANGE_CHART_SCHEMA)
        assert two.get("axis_calibration") == self._run()["axis_calibration"]

    def test_nothing_is_lost_relative_to_the_single_run_path(self):
        one = merge_results([self._run()], total_runs=1, schema=RANGE_CHART_SCHEMA)
        two = merge_results([self._run(), self._run()], total_runs=2,
                            schema=RANGE_CHART_SCHEMA)
        assert set(one) - set(two) == set(), sorted(set(one) - set(two))

    def test_extras_and_rows_survive(self):
        two = merge_results([self._run(), self._run()], total_runs=2,
                            schema=RANGE_CHART_SCHEMA)
        assert two["_extras"] == {"note": "caption"}
        assert len(two["species_ranges"]) == 1


class TestP2MixedStringDictList:
    """A list holding one object steered the whole list into the object branch,
    whose loop `continue`d on every non-dict item: two taxa vanished."""

    def test_strings_survive_alongside_objects(self):
        a = {"sections": [{"name": "S1"}], "species_ranges": [],
             "other_fossils": ["Brachiopoda", {"name": "Crustacea"}],
             "confidence": 0.8, "runs": 1}
        b = {"sections": [{"name": "S1"}], "species_ranges": [],
             "other_fossils": ["Mollusca"], "confidence": 0.8, "runs": 1}
        merged = merge_results([dict(a), dict(b)], total_runs=2,
                               schema=RANGE_CHART_SCHEMA)
        got = merged["other_fossils"]
        assert "Brachiopoda" in got, got
        assert "Mollusca" in got, got
        assert any(isinstance(x, dict) and x.get("name") == "Crustacea"
                   for x in got), got

    def test_pure_string_list_is_unchanged(self):
        a = {"sections": [{"name": "S1"}], "species_ranges": [],
             "other_fossils": ["Brachiopoda", "Brachiopoda", "Mollusca"],
             "confidence": 0.8, "runs": 1}
        b = {"sections": [{"name": "S1"}], "species_ranges": [],
             "other_fossils": ["Mollusca"], "confidence": 0.8, "runs": 1}
        merged = merge_results([dict(a), dict(b)], total_runs=2,
                               schema=RANGE_CHART_SCHEMA)
        assert merged["other_fossils"] == ["Brachiopoda", "Mollusca"]


# ---------------------------------------------------------------------------
# P1  the formula guard demoted every negative number to text
# ---------------------------------------------------------------------------
class TestP1FormulaGuardNumericExemption:
    """``str(-31.2)`` starts with "-", so every negative number was exported as
    the STRING ``'-31.2`` with a literal apostrophe. South/west hemisphere
    coordinates stopped parsing, and negative thickness/level cells became text
    that Excel's sorting, pivots and charts silently drop. pbdb.py already had
    this exemption and documented why; the generic exporter never got it."""

    @pytest.mark.parametrize("value", [-31.2, -1, -0.5, 0, 3.5, 42])
    def test_real_numbers_pass_through(self, value):
        out = E._cell_to_export(value)
        assert out == value and isinstance(out, (int, float))

    @pytest.mark.parametrize("value", [
        "=HYPERLINK(x)", "-31.2, 120.5", "@SUM(A1)", "+1", "\t=cmd",
    ])
    def test_text_triggers_are_still_guarded(self, value):
        out = E._cell_to_export(value)
        assert isinstance(out, str) and out.startswith("'"), out

    def test_a_negative_number_carried_as_text_is_still_guarded(self):
        assert E._cell_to_export("-31") == "'-31"

    def test_csv_has_no_apostrophe_for_a_numeric_cell(self):
        csv = E.to_csv(["thickness_m"], [[-31.2]])
        assert "-31.2" in csv
        assert "'-31.2" not in csv


# ---------------------------------------------------------------------------
# P1  editing one columnar cell destroyed that section's sub-tables
# ---------------------------------------------------------------------------
class TestP1ColumnarSubTablesSurviveARename:
    """The columnar `sections` config keys on `id`, so retyping the very field
    the identity is built from missed the match and inherited nothing — taking
    `lithology_blocks`, `age_units` and `samples` with it. The sections tab
    never showed those sub-tables, so the loss was invisible."""

    def _data(self):
        return {"sections": [
            {"id": "A", "name": "Sec 1",
             "lithology_blocks": [{"top_idx": 1, "base_idx": 0, "lith": "shale"}],
             "age_units": [{"top_idx": 1, "base_idx": 0}],
             "samples": [{"bed_idx": 1}],
             "_extras": {"plate": "Fig 3"}},
            {"id": "B", "name": "Sec 2", "lithology_blocks": [],
             "age_units": [], "samples": []},
        ]}

    def test_sub_tables_survive_a_retype(self):
        data = self._data()
        before = copy.deepcopy(data)
        out = E.apply_table_edits(data, "sections",
                                  {"rows": {0: {1: "Column 1 (fixed OCR)"}}})
        row = out["sections"][0]
        assert row["lithology_blocks"] == before["sections"][0]["lithology_blocks"]
        assert row["age_units"] == before["sections"][0]["age_units"]
        assert row["samples"] == before["sections"][0]["samples"]

    def test_extras_are_still_never_fabricated(self):
        """The distinction that matters: sub-tables are measurement data and may
        be recovered, but plate/page references are PROVENANCE the model
        attached to a row — carrying the neighbour's would invent it. This is
        exactly what the identity match was introduced to prevent."""
        data = self._data()
        out = E.apply_table_edits(data, "sections",
                                  {"rows": {0: {1: "Column 1 (fixed OCR)"}}})
        assert out["sections"][0].get("_extras") is None

    def test_an_appended_row_still_inherits_nothing(self):
        data = {"sections": [{"id": "A",
                              "lithology_blocks": [{"lith": "shale"}],
                              "_extras": {"plate": "F1"}}]}
        out = E.apply_table_edits(data, "sections", [[0, 0, "A"], [1, 1, "Z"]])
        assert len(out["sections"]) == 2, out
        assert out["sections"][1].get("lithology_blocks") is None
        assert out["sections"][1].get("_extras") is None


# ---------------------------------------------------------------------------
# P1  capture_edits fabricated provenance on a mid-table insert
# ---------------------------------------------------------------------------
class TestP1CaptureEditsInsert:
    """The diff was purely POSITIONAL and ``_extras`` is deliberately excluded
    from the cell diff, so it stayed welded to the SLOT. Inserting a row in the
    middle therefore fabricated a plate figure on the new taxon and duplicated
    the last one; a pure re-sort swapped two taxons' provenance."""

    def _rows(self, *spec):
        return {"species_ranges": [
            {"species": n, "section": "S1", "range_base": b, "range_top": t,
             "_extras": {"plate": p}} for (n, b, t, p) in spec]}

    def test_mid_table_insert_lands_in_place(self):
        before = self._rows(("Alpha", "1", "3", "Fig Alpha"),
                            ("Beta", "2", "4", "Fig Beta"),
                            ("Gamma", "3", "5", "Fig Gamma"))
        after = copy.deepcopy(before)
        after["species_ranges"].insert(2, {"species": "Delta", "section": "S1",
                                           "range_base": "9", "range_top": "9"})
        merged = apply_edits(copy.deepcopy(before),
                             capture_edits(before, after))
        got = [r["species"] for r in merged["species_ranges"]]
        assert got == ["Alpha", "Beta", "Delta", "Gamma"], got
        new = merged["species_ranges"][2]
        assert new.get("_extras") is None, "a new taxon must not inherit a plate"
        assert merged["species_ranges"][3]["_extras"] == {"plate": "Fig Gamma"}

    def test_re_sort_keeps_each_taxons_own_extras(self):
        before = self._rows(("Alpha", "1", "3", "Fig Alpha"),
                            ("Beta", "2", "4", "Fig Beta"))
        after = copy.deepcopy(before)
        after["species_ranges"].reverse()
        merged = apply_edits(copy.deepcopy(before),
                             capture_edits(before, after))
        for row in merged["species_ranges"]:
            assert row["_extras"] == {"plate": "Fig " + row["species"]}, row

    def test_a_plain_cell_edit_still_round_trips(self):
        before = self._rows(("Alpha", "1", "3", "Fig Alpha"))
        after = copy.deepcopy(before)
        after["species_ranges"][0]["range_base"] = "7"
        merged = apply_edits(copy.deepcopy(before), capture_edits(before, after))
        assert merged["species_ranges"][0]["range_base"] == "7"
        assert merged["species_ranges"][0]["_extras"] == {"plate": "Fig Alpha"}


# ---------------------------------------------------------------------------
# P1  an unreadable API key was overwritten with ""
# ---------------------------------------------------------------------------
class TestP1UndecryptableKeyIsNotDestroyed:
    """A keyring available at save time and unavailable at the next launch
    leaves a ``fer:v1:`` envelope no other key can open. ``from_dict`` blanked
    it, and the very next ``ProviderStore`` mutation called ``save()``, whose
    ``to_dict()`` only re-encrypts a NON-EMPTY key — so the ciphertext was
    replaced with "" and every paid credential was gone."""

    def test_decrypt_or_none_reports_failure_distinctly(self):
        from rca_core.secrets_store import _FER_TAG, decrypt_or_none
        assert decrypt_or_none("legacy-plaintext") == "legacy-plaintext"
        assert decrypt_or_none("") == ""
        assert decrypt_or_none(_FER_TAG + "not-a-real-token") is None

    def test_the_envelope_survives_two_store_saves(self):
        import rca_core.llm as L
        from rca_core.secrets_store import _FER_TAG
        tmp = tempfile.mkdtemp(prefix="rca_audit_key_")
        try:
            path = os.path.join(tmp, "providers.json")
            planted = _FER_TAG + "ciphertext-from-a-vanished-keyring"
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "current_id": "p1", "providers": [
                    {"id": "p1", "name": "Paid", "api_format": "anthropic",
                     "endpoint": "https://api.example.com/anthropic",
                     "model": "M3", "api_key": planted, "extra_headers": {},
                     "extra_body": {}, "is_current": True, "created_at": 1.0,
                     "sort_index": 0, "consecutive_failures": 0}]}, f)

            store = L.ProviderStore(path=path).load()
            provider = store.providers[0]
            assert provider.api_key == ""
            assert provider.needs_key_reentry is True

            # The old code lost the key on ANY of these.
            store.set_current("p1")
            store.save()
            store.add(L.LlmProvider(name="second", endpoint="https://x", model="m"))
            store.save()

            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            kept = [p for p in raw["providers"] if p["id"] == "p1"][0]
            assert kept["api_key"] == planted, "the ciphertext must round-trip"
            assert "_unreadable_envelope" not in kept, \
                "the runtime marker is not stored state"
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_readable_key_still_round_trips(self):
        import rca_core.llm as L
        tmp = tempfile.mkdtemp(prefix="rca_audit_ok_")
        try:
            path = os.path.join(tmp, "ok.json")
            store = L.ProviderStore(path=path)
            store.add(L.LlmProvider(name="ok", endpoint="https://y", model="m",
                                    api_key="sk-live-123"))
            store.save()
            reloaded = L.ProviderStore(path=path).load().providers[0]
            assert reloaded.api_key == "sk-live-123"
            assert reloaded.needs_key_reentry is False
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# P1  the history row cap cascaded away the audit trail
# ---------------------------------------------------------------------------
class TestP1HistoryCapKeepsProvenance:
    """A flat 500-row cap deleted history rows, and because both child tables
    declare ON DELETE CASCADE under a permanently-on ``PRAGMA foreign_keys``
    that also destroyed the per-run raw LLM text and the immutable edit log —
    irreversibly, with no warning, at ~3.5 months of ordinary use."""

    def test_the_default_cap_is_years_not_months(self):
        from rca_core.history import MAX_HISTORY_ROWS
        assert MAX_HISTORY_ROWS >= 4000, MAX_HISTORY_ROWS

    def test_the_cap_is_env_tunable(self):
        import importlib
        import rca_core.history as H
        old = os.environ.get("RCA_MAX_HISTORY_ROWS")
        default = H.MAX_HISTORY_ROWS
        try:
            os.environ["RCA_MAX_HISTORY_ROWS"] = "77"
            assert importlib.reload(H).MAX_HISTORY_ROWS == 77
            # A typo must not silently change the retention window either way:
            # an unparsable or non-positive value falls back to the DEFAULT,
            # never to a small number that would start deleting rows.
            os.environ["RCA_MAX_HISTORY_ROWS"] = "not-a-number"
            assert importlib.reload(H).MAX_HISTORY_ROWS == default
            os.environ["RCA_MAX_HISTORY_ROWS"] = "-5"
            assert importlib.reload(H).MAX_HISTORY_ROWS == default
            os.environ["RCA_MAX_HISTORY_ROWS"] = "0"
            assert importlib.reload(H).MAX_HISTORY_ROWS == default
        finally:
            if old is None:
                os.environ.pop("RCA_MAX_HISTORY_ROWS", None)
            else:
                os.environ["RCA_MAX_HISTORY_ROWS"] = old
            importlib.reload(H)

    def test_going_over_the_cap_sheds_the_thumbnail_before_the_row(self):
        """The cap exists to bound SIZE and the thumbnail is the size driver, so
        the thumbnail goes first and the row survives."""
        import rca_core.history as H
        from rca_core.db import Database
        tmp = tempfile.mkdtemp(prefix="rca_audit_hist_")
        try:
            store = H.HistoryStore(db=Database(os.path.join(tmp, "h.db")))
            H.MAX_HISTORY_ROWS = 2
            ids = []
            for i in range(4):
                rec = H.HistoryRecord(
                    timestamp=time.time() + i,
                    source_file="f%d.png" % i,
                    image_thumbnail=b"\xff\xd8\xff" + bytes(32),
                    mode="range_chart", runs=1, result={"confidence": 0.9},
                )
                ids.append(store.add(rec, raw_responses=[
                    {"run_idx": 0, "raw_text": "reply %d" % i}]))
            assert store.count() <= 2, store.count()
            # The newest records keep both their raw text and their row.
            newest = store.get(ids[-1])
            assert newest is not None, "the newest row must survive"
            # The oldest had its thumbnail shed rather than vanishing.
            oldest = store.get(ids[0])
            if oldest is not None:
                assert oldest.image_thumbnail in (None, b"")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
            H.MAX_HISTORY_ROWS = 5000


# ---------------------------------------------------------------------------
# P2  PBDB coordinates had drifted two revisions behind Darwin Core
# ---------------------------------------------------------------------------
class TestP2PbdbCoordinateParity:
    """The same section text georeferenced occurrences in the DwC archive and
    left them UNLOCATED in the PBDB sheets, and an out-of-range pair was
    published verbatim."""

    @pytest.mark.parametrize("text,expected", [
        ("31N, 117E", (31.0, 117.0)),
        ("31.5 S 117.5 W", (-31.5, -117.5)),
        ("31.2°N, 120.5°E", (31.2, 120.5)),
        ("95N, 200E", (None, None)),          # impossible locality
        ("Not visible in chart", (None, None)),
    ])
    def test_pbdb_matches_darwin_core(self, text, expected):
        from rca_core.standards.darwin_core import _parse_coordinates
        from rca_core.standards.pbdb import _parse_coords
        assert _parse_coords(text) == expected, text
        assert _parse_coords(text) == _parse_coordinates(text), \
            "the two standards must never disagree on the same section"


# ---------------------------------------------------------------------------
# P2  a block-abbreviated genus was queried as a naked epithet
# ---------------------------------------------------------------------------
class TestP2AbbreviatedGenusIsNotQueried:
    """``P. asiaticus`` -> ``asiaticus``, which GBIF answers with a confident
    match for an "asiaticus" in a DIFFERENT genus. Block abbreviations are the
    norm in range charts, so this could attach a wrong canonical name and a
    green "verified" badge to a species range, which then travelled into the
    DwC / PBDB export."""

    @pytest.mark.parametrize("raw", [
        "P. asiaticus", "B. attenuatus", "P. hoenesi Hoenes, 1891",
    ])
    def test_naked_epithet_is_refused(self, raw):
        from rca_core.names import clean_name_for_lookup
        assert clean_name_for_lookup(raw) == "", raw

    @pytest.mark.parametrize("raw,expected", [
        ("Pseudotirolites hoenesi", "Pseudotirolites hoenesi"),
        ("Neoalbaillella optima", "Neoalbaillella optima"),
        # The abbreviation is stripped, the rest is kept verbatim (case is not
        # the parser's business) — and two tokens survive, so it IS queryable.
        ("P. hoenesi Pseudotirolites", "hoenesi Pseudotirolites"),
    ])
    def test_full_binomina_are_unaffected(self, raw, expected):
        from rca_core.names import clean_name_for_lookup
        assert clean_name_for_lookup(raw) == expected, raw


# ---------------------------------------------------------------------------
# P2  the usage by_day key had the timezone offset applied twice
# ---------------------------------------------------------------------------
class TestP2UsageByDayLocalMidnight:
    """The key was ``utc_day + offset`` (a shifted timestamp) while the only
    consumer re-applies the zone with ``localtime()``, so the offset counted
    twice and every bar west of UTC was labelled a day early. The author's own
    UTC+8 zone landed on the right date anyway, which is why it survived.

    AUDIT-2026-10-02 [item 10.1]: the P2 fix only MOVED the error. The key
    then became the epoch of the local day holding the MAJORITY of that UTC
    day's rows, which is a different wrong answer rather than the right one --
    at UTC+8 it misfiled 8 of the 24 hours of a day, and the error moved to
    the east end where the P2 note had claimed it was fine. This test could
    see none of it because it exercised the formula in pure arithmetic rather
    than the store that produces the key, so it kept passing against code it
    was supposed to be checking. It now goes through ``UsageStore``.
    """

    def test_the_key_is_local_midnight_for_every_offset(self, tmp_path,
                                                       monkeypatch):
        import time as _time
        from rca_core.db import Database
        from rca_core.usage import UsageRecord, UsageStore
        day = 1735689600          # 2025-01-01 00:00 UTC
        for off in (50400, 28800, 0, -18000, -32400):   # UTC+14 .. UTC-9
            class _Fake:
                tm_gmtoff = off

                def __call__(self, ts):
                    return self

            monkeypatch.setattr(_time, "localtime", _Fake())
            store = UsageStore(db=Database(path=str(tmp_path / f"u{off}.db")))
            # 23:00 UTC is already the NEXT local day east of UTC and already
            # the PREVIOUS local day west of it -- the hours the majority rule
            # cannot represent.
            ts = day + 23 * 3600
            store.record(UsageRecord(
                timestamp=ts, provider_id="p", provider_name="p", model="m",
                input_tokens=1, output_tokens=1,
            ))
            s = store.summary()
            assert len(s.by_day) == 1, (off, s.by_day)
            key = s.by_day[0]["day"]
            # What the consumer sees: time.localtime(key) then strftime.
            wall = _time.gmtime(key + off)     # local wall clock of the instant
            assert time.strftime("%H:%M", wall) == "00:00", (off, wall)
            # ...on the date the request was ACTUALLY made, rather than on
            # another date derived from the same formula under test.
            assert time.strftime("%Y-%m-%d", wall) == \
                time.strftime("%Y-%m-%d", _time.gmtime(ts + off)), off


# ---------------------------------------------------------------------------
# P2  quality scored unparseable endpoints as near-perfect
# ---------------------------------------------------------------------------
class TestP2QualityAccuracyHonest:
    """With nothing measurable present, ``checks`` stayed empty and the
    accuracy dimension returned 1.0 — a payload of junk endpoints scored 0.975/A,
    statistically identical to a perfect one. "Unmeasurable" was being scored as
    "fine"."""

    def test_junk_endpoints_are_not_scored_as_a_clean_payload(self):
        junk = {
            "sections": [{"name": "S1", "age_range": "Late Permian",
                          "formations": ["Yinkeng"]}],
            "species_ranges": [{"species": "Taxon %d" % i, "section": "S1",
                                "range_base": "zz0", "range_top": "aa0",
                                "biozone": "Z1", "confidence": 0.9}
                               for i in range(10)],
            "biozones": [{"name": "Z1", "section": "S1", "age": "Changhsingian"}],
            "confidence": 0.9,
        }
        wellformed = copy.deepcopy(junk)
        for row in wellformed["species_ranges"]:
            row["range_base"], row["range_top"] = "Bed 7", "Bed 9"
        assert score_range_chart(junk)["score"] < \
            score_range_chart(wellformed)["score"] + 0.30, \
            "unreadable endpoints must not score like readable ones"


class TestP1LeadingBomIsStripped:
    """A leading UTF-8 BOM (U+FEFF) is a byte-order mark, not content.

    ECMAScript's String.prototype.trim() removes U+FEFF -- the spec keeps it in
    the WhiteSpace production as the historical ZWNBSP -- while Python's
    str.strip() does not, because it goes by str.isspace() and U+FEFF is not
    whitespace there. A model reply or a file saved with a BOM therefore
    parsed in the browser and FAILED on the desktop/backend: the whole
    extraction came back empty, or the top-level array took a different
    recovery path and landed in _array_root.

    Found by difffuzz_json.py, which reached it by prefixing a top-level array
    with U+FEFF; the 20 hand-written safe_json_loads fixtures never had one.
    The three parity fixtures sj_bom_* pin the same thing on the JS side.
    """

    def test_bom_before_an_object_is_stripped(self):
        got = J.safe_json_loads(chr(0xFEFF) + '{"sections": [{"name": "A"}]}')
        assert got.get("sections") == [{"name": "A"}], got

    def test_bom_before_a_top_level_array_is_stripped(self):
        body = '[{"species": "A", "section": "S"}]'
        # The invariant is PARITY WITH THE UNPREFIXED SPELLING, not a shape
        # guessed here: whatever the chain does with a top-level array, a BOM
        # must not push it onto a different recovery path.
        assert J.safe_json_loads(chr(0xFEFF) + body) == J.safe_json_loads(body)

    def test_bom_with_surrounding_prose_is_stripped(self):
        got = J.safe_json_loads(chr(0xFEFF)
                              + 'Sure! Here it is:\n{"sections": [{"name": "A"}]}')
        assert got.get("sections") == [{"name": "A"}], got

    def test_bom_after_whitespace_is_still_stripped(self):
        got = J.safe_json_loads("  \n" + chr(0xFEFF)
                              + ' {"sections": [{"name": "A"}]}')
        assert got.get("sections") == [{"name": "A"}], got

    def test_AN_INTERIOR_bom_is_content_and_must_survive(self):
        """The fix removes a LEADING marker only. A U+FEFF inside a caption is
        real text, and dropping it would be a second, quieter data loss."""
        got = J.safe_json_loads('{"sections": [{"name": "A' + chr(0xFEFF) + 'B"}]}')
        assert got.get("sections") == [{"name": "A" + chr(0xFEFF) + "B"}], got


class TestP1ScalarRowInsertionIsEncoded:
    """A scalar row (a plain string, e.g. an entry in `other_fossils`) that
    exists only in the AFTER list was not encodable.

    The edits payload carries an inserted row as `new_<i>`, and that payload
    is a DICT -- `apply_edits` drops a non-dict insertion on the floor. So a
    scalar insertion set neither `new_<i>` nor the list-level `_replaced`:
    the edits payload came out EMPTY, `is_dirty()` returned False, and the
    row was silently discarded on Save. The user typed a fossil name, pressed
    Apply, and the tool reported no changes at all.

    Dict rows were already correct -- that is the path the earlier
    mid-table-insert fix (AUDIT-2026-09-27 P1) covered -- so only the scalar
    list was broken, and no existing test touched it. The repair routes the
    case to the replacement encoding, which is the only representation a
    scalar row has. Mirrored in js/table.js rcaCaptureListEdits, where the
    APPEND path had the same hole in BOTH engines.
    """

    @staticmethod
    def _round_trip(before_list, after_list, list_key):
        before = {list_key: copy.deepcopy(before_list)}
        after = {list_key: copy.deepcopy(after_list)}
        edits = capture_edits(copy.deepcopy(before), copy.deepcopy(after))
        replayed = apply_edits(copy.deepcopy(before), copy.deepcopy(edits))
        return edits, replayed

    def test_mid_list_scalar_insert_survives(self):
        edits, replayed = self._round_trip(
            ["a", "b"], ["a", "NEW", "b"], "other_fossils")
        assert replayed["other_fossils"] == ["a", "NEW", "b"], replayed
        assert edits, "an insert that survives must produce a non-empty payload"

    def test_trailing_scalar_insert_survives(self):
        edits, replayed = self._round_trip(
            ["a", "b"], ["a", "b", "c"], "other_fossils")
        assert replayed["other_fossils"] == ["a", "b", "c"], replayed
        assert edits, "an append that survives must produce a non-empty payload"

    def test_leading_scalar_insert_survives(self):
        _edits, replayed = self._round_trip(
            ["a", "b"], ["NEW", "a", "b"], "other_fossils")
        assert replayed["other_fossils"] == ["NEW", "a", "b"], replayed

    def test_the_edit_marks_the_result_dirty(self):
        # The user-visible half of the bug: is_dirty() is what makes the GUI
        # offer to save at all. A payload that is {} reads as "nothing changed".
        before = {"other_fossils": ["a", "b"]}
        after = {"other_fossils": ["a", "NEW", "b"]}
        assert is_dirty(before, after) is True

    def test_dict_row_insert_is_unaffected(self):
        # The already-correct path must keep working: a dict row still travels
        # as new_<i> rather than collapsing the whole list to a replacement.
        edits, replayed = self._round_trip(
            [{"species": "A", "section": "S"}, {"species": "B", "section": "S"}],
            [{"species": "A", "section": "S"},
             {"species": "NEW", "section": "S"},
             {"species": "B", "section": "S"}],
            "species_ranges")
        assert replayed["species_ranges"][1]["species"] == "NEW", replayed
        assert "new_1" in edits["species_ranges"], edits
        assert "_replaced" not in edits["species_ranges"], edits

    def test_scalar_edit_and_delete_still_travel_as_before(self):
        _e, replayed = self._round_trip(["a", "b"], ["a", "B2"], "other_fossils")
        assert replayed["other_fossils"] == ["a", "B2"], replayed
        _e, replayed = self._round_trip(["a", "b", "c"], ["a", "c"], "other_fossils")
        assert replayed["other_fossils"] == ["a", "c"], replayed


class TestReachableEditVerbsAreLossless:
    """`apply_edits(before, capture_edits(before, after)) == after` for every
    operation the table editor can perform.

    difffuzz_edits.py ran ~10500 randomised cases over the four reachable verbs
    and found zero losses, zero cross-table bleed and zero cases where
    is_dirty() disagreed with a non-empty payload. These are the deterministic
    samples of that sweep, kept in CI so a future change to the diff or the
    replay cannot regress the property silently.

    Head and mid insertion are NOT here: `addRow` appends, so they are
    unreachable from the UI -- and the same sweep shows they are genuinely
    fragile (an inserted row lands at the wrong index when identifiers are
    duplicated or blank). That is a trap for whoever adds an "insert row
    above" verb, not a live bug, and it is recorded in the fuzzer's output
    rather than fixed here.
    """

    A = {"species": "Taxus", "section": "S1", "range_base": "Bed 7",
         "range_top": "Bed 9", "confidence": 0.9}
    B = {"species": "Pinus", "section": "S1", "range_base": "Bed 2",
         "range_top": "Bed 4", "confidence": 0.8}
    C = {"species": "Ginkgo", "section": "S2", "range_base": "Bed 1",
         "range_top": "Bed 3", "confidence": 0.7}

    @staticmethod
    def _rt(before, after, key="species_ranges"):
        b = {key: copy.deepcopy(before)}
        a = {key: copy.deepcopy(after)}
        edits = capture_edits(copy.deepcopy(b), copy.deepcopy(a))
        return edits, apply_edits(copy.deepcopy(b), copy.deepcopy(edits))

    def test_append_dict_row(self):
        _e, got = self._rt([self.A, self.B], [self.A, self.B, self.C])
        assert got["species_ranges"] == [self.A, self.B, self.C], got

    def test_delete_middle_row(self):
        _e, got = self._rt([self.A, self.B, self.C], [self.A, self.C])
        assert got["species_ranges"] == [self.A, self.C], got

    def test_edit_a_plain_cell(self):
        _e, got = self._rt([self.A, self.B],
                           [dict(self.A, range_base="Bed 8"), self.B])
        assert got["species_ranges"] == [dict(self.A, range_base="Bed 8"), self.B], got

    def test_edit_an_identity_cell(self):
        # section is part of the alignment key, so changing it makes the row
        # unmatchable by identity. The positional fallback must still put the
        # edit on the right row.
        _e, got = self._rt([self.A, self.B], [dict(self.A, section="S9"), self.B])
        assert got["species_ranges"] == [dict(self.A, section="S9"), self.B], got

    def test_append_scalar_row_to_a_scalar_list(self):
        _e, got = self._rt(["a", "b"], ["a", "b", "c"], key="other_fossils")
        assert got["other_fossils"] == ["a", "b", "c"], got

    def test_editing_one_table_leaves_another_alone(self):
        before = {"species_ranges": [self.A, self.B], "biozones": [{"name": "Z1"}]}
        after = {"species_ranges": [self.A, self.B, self.C],
                 "biozones": [{"name": "Z1"}]}
        edits = capture_edits(copy.deepcopy(before), copy.deepcopy(after))
        got = apply_edits(copy.deepcopy(before), copy.deepcopy(edits))
        assert got["biozones"] == [{"name": "Z1"}], got
        assert got["species_ranges"] == [self.A, self.B, self.C], got

    def test_edit_reports_dirty(self):
        before = {"species_ranges": [self.A, self.B]}
        after = {"species_ranges": [dict(self.A, range_base="Bed 8"), self.B]}
        assert is_dirty(before, after) is True


class TestP2RetryMessageDoesNotPromiseARetryThatNeverHappens:
    """The final failing attempt announced a retry that was never taken.

    `call_llm_api_with_retry` built its `[retry N/N after Xs]` suffix
    BEFORE the `attempt == retries - 1` early return, and because each
    attempt's fresh `err_body` replaces the previous one, that was the LAST
    line the user read. So every extraction that exhausted its budget against
    a rate-limited API (429/503 -- the common case) ended by telling the
    operator a delay was applied that was never waited and a retry was
    pending that never came.

    The retry LOGIC was always right -- `retries` attempts and `retries - 1`
    sleeps -- so this was purely a false statement in the dialog, and no test
    looked at it: the existing retry tests assert the RESULT, never the
    message. REVIEW-2026-09-10 fixed exactly this for the network-error
    branch and left the status branch untouched.
    """

    @staticmethod
    def _run(retries, statuses):
        from unittest.mock import patch
        import rca_core.llm as L
        from rca_core.llm import LlmProvider, ApiFormat, call_llm_api_with_retry
        seen = []
        slept = []

        def fake(**kw):
            st = statuses[min(len(seen), len(statuses) - 1)]
            seen.append(st)
            return (None, False, st, "upstream said %s" % st, {})

        prov = LlmProvider(api_format=ApiFormat.OPENAI,
                           endpoint="https://example.invalid/v1",
                           api_key="sk-test", model="m")
        with patch.object(L, "call_llm_api", side_effect=fake), \
             patch.object(L.time, "sleep", side_effect=lambda s: slept.append(s)):
            text, _trunc, status, err, _usage = call_llm_api_with_retry(
                provider=prov, system_prompt="s", image_b64="QUFB",
                media_type="image/png", user_text="hi", max_tokens=16,
                retries=retries, initial_backoff_sec=0.0)
        return len(seen), len(slept), err, status

    def test_exhausted_budget_reports_the_attempts_actually_made(self):
        for retries in (1, 2, 3, 4):
            made, slept, err, status = self._run(retries, [429])
            assert made == retries, (retries, made)
            assert slept == retries - 1, (retries, slept)
            assert status == 429
            assert "giving up" in err, (retries, err)
            assert "%d attempt" % retries in err, (retries, err)

    def test_the_final_message_never_promises_another_retry(self):
        for retries in (1, 2, 3, 4):
            _m, _s, err, _st = self._run(retries, [429])
            assert "retry " not in err.lower(), (retries, err)
            assert " after " not in err, (retries, err)

    def test_only_the_final_line_is_ever_visible(self):
        # Each attempt builds a fresh err_body, so the intermediate
        # "[retry N/M after Xs]" lines are overwritten and the caller sees
        # exactly ONE status line -- the last one. That is why the lying
        # final line was the whole problem: before the fix it was the only
        # suffix any operator ever saw, on every exhausted-budget failure.
        _m, _s, err, _st = self._run(3, [429, 503, 500])
        lines = [ln for ln in err.splitlines() if ln.strip()]
        assert len(lines) == 2, lines          # upstream body + one status line
        assert lines[-1].endswith("giving up]"), lines

    def test_non_retryable_status_is_untouched(self):
        from unittest.mock import patch
        import rca_core.llm as L
        from rca_core.llm import LlmProvider, ApiFormat, call_llm_api_with_retry
        calls = []

        def fake(**kw):
            calls.append(1)
            return (None, False, 400, "bad request", {})

        prov = LlmProvider(api_format=ApiFormat.OPENAI,
                           endpoint="https://example.invalid/v1",
                           api_key="sk-test", model="m")
        with patch.object(L, "call_llm_api", side_effect=fake):
            _t, _tr, status, err, _u = call_llm_api_with_retry(
                provider=prov, system_prompt="s", image_b64="QUFB",
                media_type="image/png", user_text="hi", max_tokens=16,
                retries=3, initial_backoff_sec=0.0)
        assert len(calls) == 1, calls
        assert status == 400
        assert "retry" not in err.lower(), err


class TestP2RedactionCoversJsonAndNamedParameterForms:
    """A key echoed back in an error body reaches the browser, the browser
    history and every proxy log in between.

    Measured 2026-09-27: the field-name alternative required the separator to
    follow the name DIRECTLY (``api[_-]?key\\s*[:=]\\s*``), so ``?api_key=X``
    was redacted while ``{"api_key": "X"}`` was not -- the same field, two
    serializations, opposite outcomes, because JSON puts a closing quote
    first. ``?key=``, ``secret=``, ``token=`` and a bare ``Authorization:``
    with no "Bearer" were missing outright.

    Severity is deliberately stated honestly: every key format this app's own
    providers use (sk-, sk-ant-, sk-cp-, AIza, AKIA, Bearer JWT, and the
    repo's ccs-/pk- presets) was ALREADY covered, so no known provider key
    leaked. This closes the custom-provider / proxy / gateway shapes.

    The other direction matters just as much. A random 24-character string in
    free text is indistinguishable from an ordinary word, so it is NOT chased
    -- redacting it would destroy every error message, and a redacted
    diagnosis is worth less than a visible one. The fix is therefore scoped to
    forms carrying an unambiguous marker: a known field name, or the
    Authorization header. test_review_2026_09_10.py pins the prose side and
    must keep passing.
    """

    SECRET = "Zq7Xr4PbVn2Kd8Lm3Qw9Rt6Yc1"

    @staticmethod
    def _redact():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "srv_audit_2026_09_27",
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "server.py"))
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        return srv._redact_error_body

    def test_json_echoed_opaque_key_is_redacted(self):
        R = self._redact()
        S = self.SECRET
        for body in ('{"x-api-key": "%s"}' % S,
                     '{"api_key": "%s"}' % S,
                     '{"apiKey": "%s"}' % S,
                     '{"secret": "%s"}' % S,
                     '{"token": "%s"}' % S):
            out = R(body)
            assert S not in out, (body, out)
            assert out != body, body

    def test_named_parameter_forms_are_redacted(self):
        R = self._redact()
        S = self.SECRET
        for body in ("/v1/x?key=%s" % S, "/v1/x?api_key=%s" % S,
                     "secret=%s" % S, "token=%s" % S,
                     "Authorization: %s" % S):
            out = R(body)
            assert S not in out, (body, out)

    def test_the_app_s_own_provider_key_formats_still_redact(self):
        R = self._redact()
        for body in ("sk-proj-abc123DEF456ghi789jkl",
                     "x-api-key=sk-ant-api03-AbCdEf0123456789XyZ",
                     "sk-cp-0123456789abcdefABCDEF",
                     "AIzaSyA0123456789abcdefghijklmnopqrstu",
                     "AKIAIOSFODNN7EXAMPLE",
                     "Bearer eyJhbGciOi.eyJzdWIiOiIxIn0.sig",
                     "ccs-0123456789abcdef0123",
                     "pk-0123456789abcdef0123"):
            out = R(body)
            assert out != body, (body, out)

    def test_ordinary_prose_is_not_redacted(self):
        # The over-redaction guard. A diagnosis the operator cannot read is
        # worth less than a visible key-shaped string, and the existing suite
        # already pins `R("plain error message") == "plain error message"`.
        R = self._redact()
        for body in ("plain error message",
                     "the key is wrong",
                     "monkey=5",                      # \bkey must not match inside it
                     "token limit exceeded, try again",
                     "API returned 429 rate limited",
                     "connection reset by peer",
                     "model gpt-4o not found for this account",
                     "your API call exceeded the context window",
                     "authentication failed: check the secret you configured",
                     "request took too long; the server did not respond"):
            assert R(body) == body, (body, R(body))


class TestP2ProviderReprHidesTheApiKey:
    """``LlmProvider.api_key`` rendered in CLEARTEXT by ``repr()``/``str()``.

    Measured before the fix: ``repr(prov)`` contained the key verbatim, while
    the neighbouring ``_unreadable_envelope`` field already carried
    ``repr=False`` for exactly the same reason -- so this was an omission in
    the repr-safety treatment, not a decision.

    Severity, stated honestly: an AST scan of every logging/print call in
    llm.py, server.py, both GUIs, extractor, exporter and history found NONE
    that formats a provider, so there is no live leak today. It is one debug
    line or one traceback away from writing a real credential into a log
    file, and a provider is the kind of object that ends up in an exception
    message.

    The change is ``repr=False`` and nothing else, so persistence and
    equality must be untouched -- and they are, which is what the lower half
    of this class pins:
      * ``to_dict()`` calls ``dataclasses.asdict`` (field VALUES, not repr)
        and then ENCRYPTS the key via secrets_store.encrypt, so the stored
        form is an envelope;
      * ``from_dict()`` decrypts it back;
      * ``__eq__`` still compares the key, so a provider whose key changed is
        still detected as a different provider.
    """

    KEY = "sk-SUPERSECRETVALUE123456"

    @staticmethod
    def _prov(key=None, cid="x", ts=1000.0):
        from rca_core.llm import LlmProvider, ApiFormat
        return LlmProvider(
            id=cid, name="n", endpoint="https://e/v1",
            api_key=TestP2ProviderReprHidesTheApiKey.KEY if key is None else key,
            model="m", api_format=ApiFormat.OPENAI, created_at=ts)

    def test_repr_and_str_do_not_render_the_key(self):
        prov = self._prov()
        assert self.KEY not in repr(prov), repr(prov)
        assert self.KEY not in str(prov), str(prov)
        # The diagnostic is still useful: the non-secret fields survive.
        assert "Ki-1" or "https://e/v1" in repr(prov)

    def test_repr_stays_clean_after_a_serialization_round_trip(self):
        from rca_core.llm import LlmProvider
        prov = self._prov()
        back = LlmProvider.from_dict(prov.to_dict())
        assert back.api_key == self.KEY          # the value survives...
        assert self.KEY not in repr(back)        # ...but is never rendered

    def test_the_stored_form_is_encrypted_not_plaintext(self):
        d = self._prov().to_dict()
        assert d["api_key"] != self.KEY
        assert str(d["api_key"])[:3] in ("fer", "obf"), d["api_key"]

    def test_equality_still_compares_the_key(self):
        assert self._prov() == self._prov()
        assert self._prov() != self._prov("a-different-key")


class TestP2ProviderReprMasksExtraHeadersAndBody:
    """The class docstring invites the leak that ``repr=False`` did not cover.

    ``LlmProvider``'s own docstring says ``extra_headers`` "let advanced users
    accommodate proxies or non-standard gateways" -- which is precisely where
    a user puts ``Authorization: Bearer ...`` -- and ``extra_body`` is
    free-form and routinely carries a second ``api_key``. Masking only the
    named ``api_key`` field left those two rendering in cleartext: measured,
    all three secrets appeared in ``repr(prov)``.

    The header and body NAMES stay visible, because the useful part of a
    diagnostic is knowing WHICH proxy headers are configured.

    NOT fixed here, and deliberately: ``to_dict()`` still writes
    ``extra_headers`` / ``extra_body`` to providers.json in cleartext while
    encrypting ``api_key``. Encrypting those two dicts is a STORAGE FORMAT
    change -- it needs a decrypt-on-load path and a migration for every
    existing providers.json -- so it is a decision for the owner rather than
    a defect to fix unilaterally. The test below pins the CURRENT state of
    that asymmetry so it cannot drift unnoticed in either direction.
    """

    API_KEY = "sk-FIRSTKEY0000000000000000"
    HDR_KEY = "Bearer ghp_SECONDKEY0000000000000000000000000000"
    BODY_KEY = "sk-BODYKEY00000000000000000"

    @staticmethod
    def _prov():
        from rca_core.llm import LlmProvider, ApiFormat
        return LlmProvider(
            id="p1", name="gateway", endpoint="https://gw.example/v1",
            api_key=TestP2ProviderReprMasksExtraHeadersAndBody.API_KEY,
            model="m", api_format=ApiFormat.OPENAI, created_at=1000.0,
            extra_headers={
                "Authorization": TestP2ProviderReprMasksExtraHeadersAndBody.HDR_KEY,
                "X-Tenant": "lab-7"},
            extra_body={"api_key": TestP2ProviderReprMasksExtraHeadersAndBody.BODY_KEY})

    def test_repr_renders_no_credential(self):
        r = repr(self._prov())
        for secret in (self.API_KEY, self.HDR_KEY, self.BODY_KEY):
            assert secret not in r, (secret[:12], r)

    def test_repr_still_shows_the_header_and_body_names(self):
        # Names survive, values do not. Masking selectively -- e.g. only the
        # headers whose NAME looks secret-ish -- would hand the safety of a
        # credential to a header name the user chose, and "X-Whatever" is a
        # perfectly good place to put a token. The name is what a diagnostic
        # needs; the value is where a secret lives.
        r = repr(self._prov())
        assert "Authorization" in r, r      # which proxy headers are set
        assert "X-Tenant" in r, r
        assert "lab-7" not in r, r          # every VALUE is masked
        assert "gateway" in r               # the rest of the record is intact

    def test_the_at_rest_asymmetry_is_pinned_not_hidden(self):
        # api_key IS encrypted; extra_headers / extra_body are NOT yet. If a
        # future change closes that, this assertion is the one to update --
        # and it makes the decision visible instead of accidental.
        d = self._prov().to_dict()
        assert str(d["api_key"])[:3] in ("fer", "obf"), d["api_key"]
        assert self.API_KEY not in json.dumps(d, default=str)
        assert d["extra_headers"]["Authorization"] == self.HDR_KEY


class TestP2AbbreviatedSubgenusSurvivesTheSplit:
    """``Cephalodiscus (L.) amygdala`` lost its subgenus on the way to PBDB.

    ``_split_taxon_name`` has a ``subgenus_name`` column and its own docstring
    promises "genus + subgenus + species" for "Clarkina (Parkinsonina) carli".
    But the parenthesis handler only accepted a FULLY SPELLED word
    (``^[A-Z][a-z]{2,}$``), so the abbreviated form fell through to the
    discard branch -- and the abbreviation is the NORM in radiolarian
    taxonomy, which is this app's own domain. Five of the recorded payloads
    use exactly that shape.

    The failure is invisible by construction: the occurrence row still exists,
    the row count is still right, and ``taxon_name`` still carries the whole
    original string. Only the structured columns -- the ones PBDB matches
    taxonomy on -- were wrong.

    Widening the SHAPE is safe because the disambiguation was always
    POSITIONAL: the consumer takes the group as a rank only when
    ``genus and not species and not subgenus``, i.e. between the genus and
    the specific epithet, which is where ICZN puts a subgenus. That is what
    FIX-2026-09-22 (item 5) relied on, and the authority cases it was
    protecting are pinned below to prove they still do not produce a rank.
    """

    @staticmethod
    def _split(name):
        from rca_core.standards.pbdb import _split_taxon_name
        return _split_taxon_name(name)

    def test_abbreviated_subgenus_is_captured(self):
        for name, want in (
                ("Cephalodiscus (L.) amygdala", "L."),
                ("Podocyrtis (P.) phyxis", "P."),
                ("Lophocyrtis (L.) ampla", "L."),
                ("Podocyrtis (L.?) cf. phyxis", "L."),
                ("Lophocyrtis (L. ?) barbodense", "L.")):
            d = self._split(name)
            assert d["subgenus_name"] == want, (name, d)
            assert d["species_name"], (name, d)

    def test_the_spelled_out_subgenus_still_works(self):
        d = self._split("Clarkina (Parkinsonina) carli")
        assert d["genus_name"] == "Clarkina", d
        assert d["subgenus_name"] == "Parkinsonina", d
        assert d["species_name"] == "carli", d

    def test_an_authority_parenthesis_is_still_not_a_rank(self):
        # FIX-2026-09-22 (item 5) exists to stop this: reading "(Ehrenberg)"
        # at the end of a binomial as a subgenus invented a rank nobody
        # stated. Position decides, and these sit AFTER the species group.
        for name in ("Neogloboboquadrina pachyderma (Ehrenberg) Cushman",
                     "P. asiaticus (Zheng, 1979)"):
            d = self._split(name)
            assert d["subgenus_name"] == "", (name, d)

    def test_plain_binomials_still_have_no_subgenus(self):
        for name in ("Pseudotirolites panigoniensis", "Costa cf. postwenti",
                     "Palaeopascichnus sp.", "Genus species"):
            d = self._split(name)
            assert d["subgenus_name"] == "", (name, d)

    def test_a_doubted_subgenus_keeps_its_rank_AND_records_the_doubt(self):
        # The module docstring states the rule for EVERY rank: "a doubt /
        # nomenclatural marker MOVES from the name into its resolution
        # column". It was broken for exactly one rank -- the subgenus -- where
        # the full-word shape test rejected the "?" and the RANK ITSELF
        # vanished, so "Clarkina (Parkinsonina?) carli" published a genus and
        # a species with the intervening subgenus silently dropped.
        for name, want in (("Clarkina (Parkinsonina?) carli", "Parkinsonina"),
                           ("Cephalodiscus (L.?) amygdala", "L."),
                           ("Cephalodiscus (L. ?) amygdala", "L."),
                           ("Podocyrtis (L.?) cf. phyxis", "L.")):
            d = self._split(name)
            assert d["subgenus_name"] == want, (name, d)
            assert d["subgenus_reso"] == "?", (name, d)

    def test_the_doubt_goes_to_the_SUBGENUS_reso_not_the_species(self):
        # An earlier iteration emitted the "?" as a separate token, which the
        # pending-qualifier machinery then handed to the NEXT name -- the
        # species. The species rank has its own resolution slot and the wrong
        # rank carrying a doubt is a wrong published record.
        # A name with NO species qualifier, so species_reso has only one
        # possible occupant: the doubt that belongs to the subgenus. (An
        # earlier version of this test used "... cf. phyxis" and wrongly
        # expected species_reso to be empty -- "cf." is a legitimate species
        # qualifier and correctly lands there.)
        d = self._split("Cephalodiscus (L.?) amygdala")
        assert d["subgenus_reso"] == "?", d
        assert d["species_reso"] == "", d

    def test_a_clean_subgenus_still_has_no_resolution(self):
        for name in ("Clarkina (Parkinsonina) carli",
                     "Cephalodiscus (L.) amygdala"):
            d = self._split(name)
            assert d["subgenus_name"], (name, d)
            assert d["subgenus_reso"] == "", (name, d)

    def test_the_doubt_is_never_lost_from_taxon_name(self):
        # The abbreviated-with-doubt case does not populate subgenus_reso --
        # a fidelity gap, recorded rather than chased. What must hold is that
        # the marker survives in the un-split column, which is the docstring's
        # stated guarantee and the audit trail a reader can check.
        from rca_core.standards.pbdb import to_pbdb_occurrences
        src = "Podocyrtis (L.?) cf. phyxis"
        occ = to_pbdb_occurrences({
            "sections": [{"name": "S1"}], "confidence": 0.9,
            "species_ranges": [{"species": src, "section": "S1",
                                "range_base": "7", "range_top": "9"}]})[0]
        assert occ["subgenus_name"] == "L.", occ
        assert occ["taxon_name"] == src, occ
        assert "?" in occ["taxon_name"], occ


class TestP2EveryRankMovesItsDoubtIntoItsResolutionSlot:
    """``_split_taxon_name``'s docstring claims the rule for EVERY rank.

        "Each rank has ONE resolution slot, so the first marker met wins
         and the others are not lost: they remain in taxon_name."
        ... "a doubt / nomenclatural marker MOVES from the name into its
         resolution column -- which is where PBDB says it belongs."

    Three of the four ranks honoured that. The subgenus did not: the
    parenthesis shape test rejected a trailing "?", so "(Parkinsonina?)" was
    discarded whole and the RANK vanished -- not just the marker. So the
    docstring was true of genus, species and subspecies, and false of
    subgenus, with nothing to say so.

    This pins all four together. The property is one line -- strip the doubt
    off a name, and the doubt has to appear in THAT rank's resolution slot
    and no other -- so a future rank added without it fails here.
    """

    RANKS = (
        # (name, rank, name_field, reso_field)
        ("Genus? species", "genus", "genus_name", "genus_reso"),
        ("Genus speciaes?", "species", "species_name", "species_reso"),
        ("Genus (Sub?) species", "subgenus", "subgenus_name", "subgenus_reso"),
        ("Genus (L.?) species", "subgenus", "subgenus_name", "subgenus_reso"),
        ("Genus species subspecies?", "subspecies",
         "subspecies_name", "subspecies_reso"),
    )
    ALL_RESO = ("genus_reso", "subgenus_reso", "species_reso",
                "subspecies_reso")

    @staticmethod
    def _split(name):
        from rca_core.standards.pbdb import _split_taxon_name
        return _split_taxon_name(name)

    def test_the_doubt_lands_in_its_own_rank_and_nowhere_else(self):
        for name, rank, name_field, reso_field in self.RANKS:
            d = self._split(name)
            assert d[reso_field] == "?", (rank, name, d)
            assert "?" not in d[name_field], (rank, name, d)
            for other in self.ALL_RESO:
                if other != reso_field:
                    assert d[other] == "", (rank, name, other, d)

    def test_the_rank_survives_the_doubt(self):
        # The defect was not "the marker was lost" but "the RANK was lost".
        for name, rank, name_field, reso_field in self.RANKS:
            d = self._split(name)
            assert d[name_field], (rank, name, d)


class TestP2GeometryRejectsAnAbsurdStoredTolerance:
    """A calibration's ``is_trusted`` gate guards every point georeferenced
    through it, and the module comment claimed a clamp that was not there.

    FIX-2026-09-22 (audit bug 5-3) recorded the defect and the repair: "a
    stored ``residual_tolerance`` wider than half the axis' data span is not
    a judgement, it laundered any misfit into 'trusted' (measured: tolerance
    1e9 -> is_trusted True with residual 33). Such payloads fall back to the
    auto fraction instead of being taken verbatim."

    Measured against the code as written, the clamp did not exist:
    ``AxisCalibration.fit(..., residual_tolerance=1e9)`` on an axis spanning
    205 produced residuals of ~1.67 and reported ``is_trusted True`` -- the
    defect the audit had already found, straight back, with a comment
    claiming it was handled. An operator-supplied or stale-stored tolerance
    wide enough to accept any line at all would make a visibly wrong axis
    confidently usable, and every occurrence georeferenced from it would
    inherit the error.
    """

    ANCHORS = [(0, 0), (100, 100), (200, 205)]
    SPAN = 205.0

    @staticmethod
    def _fit(tol=None):
        from rca_core.geometry import AxisCalibration
        kw = {} if tol is None else {"residual_tolerance": tol}
        return AxisCalibration.fit(
            [tuple(a) for a in TestP2GeometryRejectsAnAbsurdStoredTolerance.ANCHORS],
            axis="x", name="X", unit="u", **kw)

    def test_a_tolerance_wider_than_half_the_span_falls_back_to_auto(self):
        for absurd in (1e6, 1e9, 1e30):
            c = self._fit(absurd)
            assert c.residual_tolerance < 0.5 * self.SPAN, (absurd, c.residual_tolerance)

    def test_a_reasonable_tolerance_is_still_honoured_verbatim(self):
        # Accepting a tolerance is the point of the parameter; only the
        # nonsensical ones are clamped.
        c = self._fit(0.5)
        assert c.residual_tolerance == 0.5, c.residual_tolerance

    def test_no_tolerance_still_yields_the_auto_fraction(self):
        c = self._fit(None)
        assert 0 < c.residual_tolerance < 0.5 * self.SPAN, c.residual_tolerance

    def test_direct_construction_is_clamped_too(self):
        # The THIRD route. audit 5-7 names __post_init__ as "one invariant
        # gate for every route into the dataclass (fit, from_json, direct
        # construction)", and 5-3's clamp was on from_json only, so direct
        # construction still took a tolerance verbatim: an object built with
        # anchors spanning 205, residuals (0.83, -1.67, 0.83) and
        # residual_tolerance=1e30 came back is_trusted=True. The gate is where
        # the guard belongs; the per-caller clamps stay as defence in depth.
        from rca_core.geometry import Anchor, AxisCalibration
        anchors = (Anchor(pixel=0, data_value=0.0),
                   Anchor(pixel=100, data_value=100.0),
                   Anchor(pixel=200, data_value=205.0))

        def build(tol):
            return AxisCalibration(
                axis="x", name="X", unit="u", direction="direct",
                slope=5.0, intercept=0.0, anchors=anchors,
                residuals=(0.83, -1.67, 0.83), residual_tolerance=tol)

        for absurd in (1e6, 1e9, 1e30):
            c = build(absurd)
            assert c.residual_tolerance < 0.5 * self.SPAN, (absurd, c)
        assert build(0.5).residual_tolerance == 0.5
        # ...and a sane tolerance makes the verdict true for a real reason:
        # the residuals are inside it.
        assert build(0.5).is_trusted is False
        assert build(0.0).is_trusted is False

    def test_an_anchor_less_object_stays_conservative(self):
        # No span means the half-span bound is undefined, so the tolerance is
        # left alone -- but from_json's anchor-less branch already refuses to
        # call such a calibration trusted, and that must hold here too.
        from rca_core.geometry import AxisCalibration
        z = AxisCalibration(axis="x", name="X", unit="u", direction="direct",
                            slope=1.0, intercept=0.0, residuals=(0.0, 33.0),
                            residual_tolerance=1e9)
        assert z.is_trusted is False, z

    def test_the_trust_verdict_is_justified_by_the_residuals(self):
        # The property that matters: is_trusted must follow from the numbers,
        # never from a tolerance loose enough to accept anything.
        for tol in (None, 0.5, 1e6, 1e9):
            c = self._fit(tol)
            expected = c.max_abs_residual <= c.residual_tolerance
            assert c.is_trusted is expected, (tol, c.is_trusted,
                                             c.max_abs_residual,
                                             c.residual_tolerance)


class TestP2ProviderPayloadWithABomStillParses:
    """A gateway/proxy that prepends a UTF-8 BOM must not look like an error.

    ``rca_core/json_utils.safe_json_loads`` strips a leading U+FEFF (fixed
    earlier the same day) -- but ``rca_core/llm.py`` never routes through
    that chain. All FIVE provider payload decodes did
    ``json.loads(payload_bytes.decode("utf-8"))``, and the strict loader
    rejects a BOM outright:

        json.decoder.JSONDecodeError: Unexpected UTF-8 BOM
        (decode using utf-8-sig): line 1 column 1 (char 0)

    The ``except`` around each site then treated a perfectly good 2xx body as
    an error body, so the extraction reported a transport failure for a
    response that was valid JSON with three extra bytes in front of it.

    This is the "guard on one path but not its sibling" shape for the second
    time in this review: the fix existed, in the wrong layer, and the layer
    that actually decodes provider bytes never got it. ``utf-8-sig`` is a
    no-op when there is no BOM, so nothing else changes.
    """

    BODY = (b'\xef\xbb\xbf'
            b'{"choices": [{"message": {"content": "{\\"a\\":1}"}}]}')

    def test_the_strict_loader_rejects_what_we_now_strip(self):
        import json
        with self.assert_raises_json():
            json.loads(self.BODY.decode("utf-8"))
        # ...and accepts it with the codec that knows about a BOM.
        assert json.loads(self.BODY.decode("utf-8-sig"))["choices"]

    def test_a_body_without_a_bom_is_unchanged(self):
        import json
        plain = self.BODY[3:]
        assert json.loads(plain.decode("utf-8-sig")) == json.loads(
            plain.decode("utf-8"))

    def test_every_provider_decode_uses_a_bom_tolerant_codec(self):
        """A static check, because the five sites are identical in shape and
        a future sixth one would silently miss the fix."""
        import re
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "rca_core", "llm.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        sites = re.findall(r'json\.loads\(payload_bytes\.decode\("([^"]+)"\)\)', src)
        assert len(sites) == 5, ("the set of provider payload decodes changed; "
                                 "re-check them", sites)
        assert set(sites) == {"utf-8-sig"}, sites

    class assert_raises_json:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            assert exc_type is not None and "BOM" in str(exc), (
                "expected a JSONDecodeError naming the BOM, got %r" % (exc,))
            return True


class TestP2ReadmeMaturityTableMatchesTheRegistry:
    """rca_core/capabilities.py and README.md state the SAME thing, twice.

    The module exists "so UIs and docs can tell users honestly what is
    production-grade versus exploratory". The docs half is realised -- but by
    HAND, as a maturity table in README.md. Nothing checks the two against
    each other, so adding a mode to CAPABILITIES and forgetting the README
    publishes the wrong maturity: precisely the failure the module was
    written to prevent.

    Measured 2026-09-28: all eight modes AGREE today (range_chart /
    columnar_section stable; abundance_diagram / phylogenetic_tree /
    zonation_chart candidate; chemical_stratigraphy / paleomap /
    scatter_plot assistant). This test does not catch a live bug; it closes
    the gap that let a future one through. It also settles the "is
    capabilities.py dead?" question: it is not dead, it is an unverified
    duplicate, and the repair is a guard rather than a deletion.
    """

    LEVEL_WORD = {"stable": "稳定", "candidate": "候选", "assistant": "辅助"}

    @staticmethod
    def _readme_maturity_rows():
        """mode -> the maturity cell, from the README maturity table ONLY.

        Two ways a naive parse goes wrong here, both hit during this review:
          * a backtick inside a shell heredoc is consumed by PowerShell, so a
            regex written that way silently matches NOTHING -- hence chr(96);
          * the README has several ``| `name` | ... | ... |`` tables, and the
            dependency list (cryptography / matplotlib / openpyxl / ...) parses
            as one. So the table is anchored on its header row and scanning
            stops at the first non-table line.
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "README.md")
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

        tick = chr(96)
        start = None
        for i, line in enumerate(lines):
            if line.startswith("|") and "成熟度" in line and "说明" in line:
                start = i + 2                 # skip the header + separator
                break
        assert start is not None, "README maturity table header not found"

        rows = {}
        for line in lines[start:]:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 4:
                break
            first = cells[1]
            if not first.startswith(tick) or len(first) < 3:
                break
            mode = first[1:].split(tick)[0]
            if not mode:
                break
            rows[mode] = cells[2]
        return rows

    def test_every_registered_mode_has_a_readme_row_of_the_same_maturity(self):
        from rca_core.capabilities import CAPABILITIES
        rows = self._readme_maturity_rows()
        assert rows, "the README maturity table was not found -- has it moved?"
        missing, drifting = [], []
        for mode, cap in sorted(CAPABILITIES.items()):
            level = cap.get("maturity") if isinstance(cap, dict) else cap
            if mode not in rows:
                missing.append(mode)
                continue
            word = self.LEVEL_WORD.get(level)
            assert word, ("unknown maturity %r for %s -- add it to LEVEL_WORD "
                          "so a new level cannot slip past this check"
                          % (level, mode))
            if word not in rows[mode]:
                drifting.append((mode, level, rows[mode]))
        assert not missing, ("modes in CAPABILITIES with no README row: %s"
                            % missing)
        assert not drifting, ("README maturity disagrees with the registry: %s"
                             % drifting)

    def test_the_readme_has_no_mode_the_registry_does_not_know(self):
        from rca_core.capabilities import CAPABILITIES
        extra = sorted(set(self._readme_maturity_rows()) - set(CAPABILITIES))
        assert not extra, ("README documents modes the registry does not: %s"
                           % extra)


class TestP2ImageHashDoesNotCollideWithTheEmptySentinel:
    """image_sha256 exists so "a 5-year-from-now audit can verify this record
    really came from that image" (rca_core/image_hash.py).

    ``base64.b64decode(s, validate=False)`` DISCARDS every character outside
    the base64 alphabet rather than raising, so the "on decode failure we hash
    the raw text" fallback the docstring promises almost never ran. Measured:

      * "!!!!####$$$$%%%%"  -> decoded to ZERO bytes -> returned
        e3b0c442...b855, the documented sentinel for "no image". A corrupt
        upload was therefore indistinguishable from no upload, in the one
        column whose job is provenance.
      * "   " (whitespace) -> the same collision.
      * a TRUNCATED payload, a `data:` URL with no comma, and a run of plain
        letters -> confident-looking hashes of garbage, with nothing marking
        them as failed.

    The repair keeps the contract: decoded-but-empty from a NON-empty input is
    the no-base64-content case, so it hashes the raw text instead. Genuinely
    empty input still returns the sentinel (that is what it means), and every
    valid encoding still hashes to the image's real digest.

    Known limit, recorded rather than fixed: a TRUNCATED payload still decodes
    to garbage bytes and still yields a plausible non-matching hash. Catching
    that needs a re-encode comparison, which also has to tolerate the
    whitespace `validate=False` just discarded -- more risk than the collision
    this closes.
    """

    EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    @staticmethod
    def _png():
        import base64
        raw = b"\x89PNG\r\n\x1a\n" + b"payload" * 5
        return raw, base64.b64encode(raw).decode()

    def test_valid_encodings_all_hash_to_the_real_image(self):
        from rca_core.image_hash import compute_image_sha256, \
            compute_image_sha256_from_b64
        raw, b64 = self._png()
        ref = compute_image_sha256(raw)
        assert compute_image_sha256_from_b64(b64) == ref
        assert compute_image_sha256_from_b64("data:image/png;base64," + b64) == ref
        # real base64 is often line-wrapped or space-separated
        wrapped = "\n".join(b64[i:i + 40] for i in range(0, len(b64), 40))
        assert compute_image_sha256_from_b64(wrapped) == ref

    def test_genuinely_absent_input_still_returns_the_sentinel(self):
        from rca_core.image_hash import compute_image_sha256_from_b64
        for val in ("", None):
            assert compute_image_sha256_from_b64(val) == self.EMPTY, val

    def test_input_with_no_base64_content_does_not_collide_with_the_sentinel(self):
        from rca_core.image_hash import compute_image_sha256_from_b64
        for val in ("!!!!####$$$$%%%%", "   ", "----", "++++"):
            got = compute_image_sha256_from_b64(val)
            assert got != self.EMPTY, (
                "a corrupt payload is fingerprinted identically to 'no image', "
                "so the audit cannot tell the two apart: %r" % val)
            # ...and it is deterministic, so a retry of the same bad upload
            # still matches its own record.
            assert compute_image_sha256_from_b64(val) == got, val

    def test_the_hash_is_still_deterministic_for_garbage(self):
        from rca_core.image_hash import compute_image_sha256_from_b64
        a = compute_image_sha256_from_b64("this is not base64 at all")
        b = compute_image_sha256_from_b64("this is not base64 at all")
        assert a == b


class TestP2ChartModeBoundaryMeansTheSameThingInBothEngines:
    """rca_core/chart_mode.py exists so the web frontend and both desktop GUIs
    "classify the same caption identically" (its own module docstring).

    Both sides spell the word boundary ``\\b``. They do not mean the same thing
    by it. Python's ``\\w`` is UNICODE-aware for str patterns, so a CJK or
    Cyrillic character adjacent to an ASCII keyword is a WORD character and
    the trailing boundary does not exist; a JavaScript RegExp without the ``u``
    flag treats ``\\w`` as ASCII-only, so the same boundary DOES exist. The
    ASCII branch is then a hit in the browser and a miss here, and since the
    branch order is what decides the mode, the two engines run DIFFERENT
    EXTRACTION PIPELINES on the same figure.

        "biplot图"        python: no match -> range_chart (the default)
                          js:     match     -> scatter_plot

    Measured scope, 2026-09-28, against the shipped js/app.js detector over
    22 captions: 3 diverged, all of them the same shape. The exposure is
    NARROWER than "mixed script" suggests, because the CJK tables are checked
    on plain substring and usually fire first -- "pollen丰度图" contains 丰度,
    "column柱状图" contains 柱状, "zonation生物带对比" contains 生物带, so those
    agree regardless. What is left is an ASCII keyword that touches CJK or
    Cyrillic AND has no CJK-table word in the caption to carry it:
    "biplot图", "scatter plot图", "pollenГрафик".

    ``re.ASCII`` is the whole repair: it makes Python's ``\\b`` mean what
    JavaScript's means. Pure-ASCII captions are untouched, because both
    notions of word character agree there.
    """

    def test_ascii_keyword_flush_against_cjk_is_a_hit(self):
        # Before the fix these returned (range_chart, False) -- the caption
        # matched NOTHING, so the caller also spent a vision-classifier call
        # on a caption that had already been answered.
        from rca_core.chart_mode import auto_detect_chart_mode_ex
        for text, want in (("biplot图", "scatter_plot"),
                           ("scatter plot图", "scatter_plot"),
                           ("pollenГрафик", "abundance_diagram")):
            mode, matched = auto_detect_chart_mode_ex(text)
            assert mode == want, (text, mode, matched)
            assert matched is True, (text, mode, matched)

    def test_pure_ascii_captions_are_unaffected(self):
        from rca_core.chart_mode import auto_detect_chart_mode_ex
        for text, want in (("Pollen abundance diagram", "abundance_diagram"),
                           ("lithologic column section", "columnar_section"),
                           ("phylogenetic tree of radiolarians", "phylogenetic_tree"),
                           ("Zonation of the Lopingian", "zonation_chart"),
                           ("radiolarian range chart", "range_chart")):
            assert auto_detect_chart_mode_ex(text)[0] == want, text

    def test_the_substring_guards_still_hold(self):
        # re.ASCII must not weaken what the guards were written for: an ASCII
        # keyword inside a longer word is still not a hit.
        from rca_core.chart_mode import auto_detect_chart_mode_ex
        for text in ("pollenate count", "colour chart",
                     "range-charts of radiolarians"):
            mode, matched = auto_detect_chart_mode_ex(text)
            assert matched is False, (text, mode)
            assert mode == "range_chart", (text, mode)
# ---------------------------------------------------------------------------
# AUDIT-2026-09-29 [P2] estimate_skew_angle refused every search window at or
# below the module's own MIN_USEFUL_ANGLE.
# ---------------------------------------------------------------------------

def _deskew_plate(width=260, height=260, period=9):
    from PIL import Image, ImageDraw
    im = Image.new("L", (width, height), 255)
    d = ImageDraw.Draw(im)
    for y in range(0, height, period):
        d.line([(0, y), (width - 1, y)], fill=0, width=1)
    return im


class TestP2SmallSearchWindowIsUsable:
    """``estimate_skew_angle`` is the function whose docstring promises a
    caller can see a sub-threshold skew. It could not be called that way.

    It passed ``MIN_USEFUL_ANGLE`` as the ``min_angle`` argument purely to
    satisfy ``_check_params``' signature, and that function requires
    ``min_angle < max_angle``. So every ``max_angle`` at or below 0.15 was
    rejected with

        DeskewError: min_angle must satisfy 0 <= min_angle < max_angle (0.1)

    although ``max_angle=0.1`` is a perfectly legal search window under
    ``_check_params``' own rule (0, 45], and although this function does no
    min_angle filtering at all -- ``_min_angle`` was discarded on the same
    line. The sub-threshold caller the docstring advertises was the one
    caller that could not call it.
    """

    def test_windows_at_or_below_the_constant_are_accepted(self):
        import pytest
        from rca_core.deskew import DeskewError, estimate_skew_angle
        plate = _deskew_plate()
        for max_angle in (0.15, 0.1, 0.05, 0.01):
            try:
                angle = estimate_skew_angle(
                    plate, max_angle=max_angle, method="projection")
            except DeskewError as exc:  # pragma: no cover - the regression
                raise AssertionError(
                    "estimate_skew_angle rejected the legal search window "
                    f"max_angle={max_angle}: {exc}") from None
            assert -max_angle <= angle <= max_angle, (max_angle, angle)

    def test_the_constant_itself_is_still_usable(self):
        # MIN_USEFUL_ANGLE is the documented "below this, do not bother
        # rotating" cut. Being able to ASK about that regime is the point.
        import rca_core.deskew as ds
        assert ds.MIN_USEFUL_ANGLE == 0.15
        angle = ds.estimate_skew_angle(_deskew_plate(),
                                      max_angle=ds.MIN_USEFUL_ANGLE,
                                      method="projection")
        assert abs(angle) <= ds.MIN_USEFUL_ANGLE

    def test_default_window_is_unchanged(self):
        # The fix must not move the default path: same plate, same answer.
        from rca_core.deskew import estimate_skew_angle
        plate = _deskew_plate()
        assert estimate_skew_angle(plate, method="projection") == \
            estimate_skew_angle(plate, 5.0, method="projection")

    def test_illegal_windows_are_still_rejected(self):
        # Relaxing the internal sentinel must not relax the real rules.
        from rca_core.deskew import DeskewError, estimate_skew_angle
        plate = _deskew_plate()
        for bad in (0.0, -1.0, 45.1, 90.0):
            try:
                estimate_skew_angle(plate, max_angle=bad, method="projection")
            except DeskewError:
                continue
            raise AssertionError(f"max_angle={bad} should have been rejected")

    def test_other_parameters_are_still_validated(self):
        from rca_core.deskew import DeskewError, estimate_skew_angle
        plate = _deskew_plate()
        for kwargs in ({"downsample": 0}, {"axis": "sideways"},
                       {"method": "magic"}):
            try:
                estimate_skew_angle(plate, max_angle=1.0, **kwargs)
            except DeskewError:
                continue
            raise AssertionError(f"{kwargs} should have been rejected")

    def test_deskew_image_still_reports_a_contradictory_min_angle(self):
        # deskew_image has no such excuse: there a min_angle >= max_angle IS
        # a caller contradiction, and the guard must stay in place. This is
        # the sibling path -- it is the reason the fix was made in
        # estimate_skew_angle alone rather than in _check_params.
        from rca_core.deskew import DeskewError, deskew_image
        plate = _deskew_plate()
        try:
            deskew_image(plate, max_angle=2.0, min_angle=2.0,
                         method="projection")
        except DeskewError:
            return
        raise AssertionError(
            "deskew_image accepted min_angle == max_angle; the real "
            "contradiction check was lost")# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: the prompt's JSON-schema blocks hand-write the three
# response kinds, so "generated from the single source of truth" only held for
# half of the paths.
# ---------------------------------------------------------------------------

_SCHEMA_LINE = re.compile(
    r'"response_kind":\s*"(\w+)"\s*\(string, REQUIRED: one of '
    r'"(\w+)", "(\w+)", "(\w+)"'
)

#: The prompts that carry the three-state answer, i.e. the ones whose
#: ``species_ranges`` / ``abundances`` / ... rows are digested by the contract
#: source keys. Each of these ALSO prints a JSON-schema example of a row, and
#: in that example the three kinds are written out as string literals.
_CONTRACT_SCHEMA_PROMPTS = (
    "RANGE_CHART_SYSTEM_PROMPT",
    "ABUNDANCE_DIAGRAM_SYSTEM_PROMPT",
    "CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT",
    "SCATTER_PLOT_SYSTEM_PROMPT",
)


class TestPromptSchemaKindsAreTheModulesKinds:
    """``prompt.py``'s header says the contract clauses are GENERATED from
    ``rca_core.reason_codes.py`` "never transcribed". That is true of
    ``_coverage_contract_clause``, which interpolates the module constants --
    and it is NOT true of the JSON-schema example block printed above it,
    where the three kinds are typed out by hand.

    So a rename in ``reason_codes.py`` would move the rule and leave the
    example behind: the shipped prompt would tell the model, in one place,
    that the allowed values are A | B | C and, a few lines earlier, that they
    are x | y | z. Nothing caught that --
    ``test_kinds_come_from_the_module_not_a_literal`` asserts
    ``RESPONSE_KIND_LIST`` equals the expression that DEFINES it, which is
    true no matter what the constants become, and ``RESPONSE_KIND_LIST`` is
    referenced by no production code at all (the prompt writes the kinds
    quoted, so the unquoted list is not even a substring of it).

    The values agree today. This pins the agreement so it cannot stop being
    true quietly.
    """

    def test_every_contract_prompt_carries_a_schema_line(self):
        import rca_core.prompt as P
        for name in _CONTRACT_SCHEMA_PROMPTS:
            text = getattr(P, name)
            assert _SCHEMA_LINE.search(text), (
                f"{name} no longer matches the expected schema line shape; if "
                "the wording changed, update _SCHEMA_LINE in this test "
                "rather than let it go unchecked")

    def test_schema_literals_are_the_module_values(self):
        import rca_core.prompt as P
        from rca_core.reason_codes import (
            RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN,
        )
        want = (RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN)
        for name in _CONTRACT_SCHEMA_PROMPTS:
            hit = _SCHEMA_LINE.search(getattr(P, name))
            assert hit, name
            default, *listed = hit.groups()
            assert tuple(listed) == want, (
                f"{name} advertises kinds {tuple(listed)} but "
                f"reason_codes.py defines {want}")
            assert default == RESPONSE_EXTRACTED, (
                f"{name} shows the example response_kind as {default!r}, "
                f"expected {RESPONSE_EXTRACTED!r}")

    def test_the_generated_clause_agrees_with_the_schema_blocks(self):
        # The two paths -- interpolated clause vs hand-written example -- are
        # checked against each other here, so neither can drift alone.
        import rca_core.prompt as P
        from rca_core.reason_codes import (
            RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN,
        )
        clause = P._coverage_contract_clause("species_ranges", "unit", "eg")
        for kind in (RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN):
            assert f'"{kind}"' in clause, kind
            assert f'"{kind}"' in P.RANGE_CHART_SYSTEM_PROMPT, kind

    def test_response_kind_list_is_not_a_second_source_of_truth(self):
        """Pins the current (harmless) state instead of pretending the
        variable does work: it is unreferenced by production code, and the
        existing test only restates its own definition. If a future change
        makes the clause interpolate it, this test is the place to notice."""
        import rca_core.prompt as P
        from rca_core.reason_codes import (
            RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN,
        )
        assert P.RESPONSE_KIND_LIST == " | ".join(
            (RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN))
        # The prompt writes the kinds quoted, so the bare list is NOT a
        # substring of the shipped text -- which is why interpolating it
        # would be a real (if small) behaviour change, not a refactor.
        assert P.RESPONSE_KIND_LIST not in P.RANGE_CHART_SYSTEM_PROMPT# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: the phylo normalizer had exactly one rejection path that
# raised TypeError instead of ValueError, and the JS mirror had no such path
# at all.
# ---------------------------------------------------------------------------

class TestScalarRootIdsRefuseLikeTheMirrorDoes:
    """``_normalize_phylogenetic_tree_into`` raises ValueError for every
    unusable payload it documents -- empty root_ids, an unknown root id, a
    non-root with a null parent, a root with a parent. ``tests/
    test_phylo_parent_null.py`` pins all four with ``pytest.raises
    (ValueError)``.

    There was one more, and it raised the wrong class. ``[str(r) for r in
    (raw.get("root_ids") or [])]`` iterates whatever it is handed, so a model
    that wrote a scalar there::

        {"root_ids": 42, "nodes": [...]}

    raised ``TypeError: 'int' object is not iterable``. The call site catches
    ``Exception`` (extractor.py:3226), so nothing crashed -- the user saw
    ``normalize failed: 'int' object is not iterable``, which names an
    implementation detail instead of anything about their figure.

    It was also a cross-engine split. ``js/minimax.js:2507-2516`` enumerates
    four shapes explicitly -- array, string, object (its keys), everything
    else -> empty -- so the browser refused the same payload with
    "root_ids is empty" while the desktop raised TypeError. The differential
    fuzzer caught it as 79 "refusal text differs" per run, and its own
    comment says that class is a REAL difference in failure mode rather than
    two spellings of one refusal.
    """

    BASE = {
        "metadata": {"taxon_group": "Radiolaria"},
        "nodes": [{"id": "n0", "parent": None, "name": "Root", "is_leaf": False},
                  {"id": "n1", "parent": "n0", "name": "Leaf", "is_leaf": True}],
    }

    def _payload(self, root_ids):
        p = dict(self.BASE)
        p["root_ids"] = root_ids
        return p

    def test_scalar_root_ids_raise_value_error_not_type_error(self):
        from rca_core.extractor import _normalize_phylogenetic_tree_into as N
        for bad in (42, 0, 1.5, True, 3 + 0j):
            with pytest.raises(ValueError) as ei:
                N(self._payload(bad))
            # The mirror's wording, so both engines refuse for the same stated
            # reason rather than one of them leaking a TypeError.
            assert "root_ids is empty" in str(ei.value), (bad, str(ei.value))

    def test_the_empty_forms_still_read_the_same(self):
        # None / [] / "" were already empty before the fix and must stay so;
        # the guard is not allowed to change the existing contract.
        from rca_core.extractor import _normalize_phylogenetic_tree_into as N
        for empty in (None, [], "", {}):
            with pytest.raises(ValueError, match="root_ids is empty"):
                N(self._payload(empty))

    def test_the_accepted_shapes_are_untouched(self):
        # array / string / dict are the three shapes the JS mirror accepts, and
        # a dict contributes its KEYS on both sides. Refusing a scalar must not
        # have narrowed any of these.
        from rca_core.extractor import _normalize_phylogenetic_tree_into as N
        listed = N(self._payload(["n0"]))
        assert [n["id"] for n in listed["nodes"]] == ["n0", "n1"]
        by_key = N(self._payload({"n0": True}))
        assert [n["id"] for n in by_key["nodes"]] == ["n0", "n1"]
        # a string iterates per character, exactly as the mirror's
        # Array.from(rootIdsRaw) does -- "n" and "0" are both unknown ids, so
        # the refusal is about the ids, not about the shape.
        with pytest.raises(ValueError, match="unknown node id"):
            N(self._payload("n0"))

    def test_numeric_ids_inside_a_list_still_work(self):
        # The Sprint B fix this line was written for must survive: root_ids
        # are stringified once, so [1] matches a node whose id is "1".
        from rca_core.extractor import _normalize_phylogenetic_tree_into as N
        out = N({
            "root_ids": [1],
            "nodes": [{"id": "1", "parent": None, "name": "A", "is_leaf": True}],
        })
        assert out["nodes"][0]["id"] == "1"

    def test_unknown_root_id_still_names_the_id(self):
        from rca_core.extractor import _normalize_phylogenetic_tree_into as N
        with pytest.raises(ValueError, match="unknown node id: n9"):
            N(self._payload(["n9"]))# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: two NameErrors in server.py, both on live request paths.
# Found by `ruff check --select F821`, not by reading: three rounds of
# line-by-line review had gone through this file without seeing either, and
# neither had a test.
# ---------------------------------------------------------------------------

def _handler_class():
    import server
    return server, server.Handler


class _RecordingHandler:
    """Minimal stand-in for BaseHTTPRequestHandler's output surface.

    ``wfile`` is an ATTRIBUTE on BaseHTTPRequestHandler (socket.makefile()),
    not a method -- an earlier version of this probe defined it as a method
    and failed with "'function' object has no attribute 'write'", which
    looked like a product failure and was not one.
    """

    @staticmethod
    def build(handler_cls):
        import io

        class Probe(handler_cls):
            def send_response(self, code, msg=None):
                self.captured_code = code

            def send_header(self, k, v):
                self.captured_headers = getattr(self, "captured_headers", [])
                self.captured_headers.append((k, v))

            def end_headers(self):
                self.captured_ended = True

        probe = Probe.__new__(Probe)
        probe.wfile = io.BytesIO()
        return probe


class TestP1NonFiniteResponsePayloadIsSanitisedNotFatal:
    """Handler._send_json's fallback for a payload carrying NaN/Infinity
    called ``_strip_nonfinite_shallow`` unqualified, but that helper is a
    @staticmethod ON THE CLASS -- not a module-level name.

    The name therefore never resolved, and the fallback introduced by
    FIX-2026-09-22 for precisely these payloads had never run once.
    _send_json has 50 call sites, so every response whose payload happened to
    carry a non-finite float became a 500 rather than a sanitised 200 --
    which inverts the intent of the comment three lines above it: the
    handler is meant to survive the very JSON it cannot serialise.
    """

    PAYLOAD = {"ok": True,
               "usage": {"input_tokens": 1, "junk": float("nan")},
               "data": {"confidence": float("inf")}}

    def _send(self):
        _server, handler = _handler_class()
        probe = _RecordingHandler.build(handler)
        probe._send_json(200, dict(self.PAYLOAD))
        return probe

    def test_it_does_not_raise(self):
        # Before the fix this was NameError: name '_strip_nonfinite_shallow'
        # is not defined.
        probe = self._send()
        assert probe.captured_code == 200

    def test_the_client_receives_parsable_json(self):
        import json
        probe = self._send()
        text = probe.wfile.getvalue().decode("utf-8")
        parsed = json.loads(text)          # raises if invalid JSON was served
        assert parsed["ok"] is True
        assert "NaN" not in text and "Infinity" not in text, text

    def test_the_non_finite_values_became_null_not_a_crash(self):
        probe = self._send()
        import json
        parsed = json.loads(probe.wfile.getvalue().decode("utf-8"))
        assert parsed["usage"]["junk"] is None
        assert parsed["data"]["confidence"] is None

    def test_a_clean_payload_still_takes_the_fast_path(self):
        # The guard must not have changed the ordinary case: no sanitising,
        # exact bytes out.
        _server, handler = _handler_class()
        probe = _RecordingHandler.build(handler)
        payload = {"ok": True, "data": {"confidence": 0.5}}
        probe._send_json(200, payload)
        import json
        assert json.loads(probe.wfile.getvalue().decode("utf-8")) == payload

    def test_the_helper_is_reachable_as_a_method(self):
        # The staticmethod may be called unbound too; both spellings must
        # work, because the fix chose the bound one.
        _server, handler = _handler_class()
        out = handler._strip_nonfinite_shallow(
            {"a": float("nan"), "b": [float("inf"), 1.0]})
        assert out["a"] is None
        assert out["b"][0] is None and out["b"][1] == 1.0


class TestP1MultiRunRequestMetaResolvesItsPromptVersion:
    """``_handle_extract_body`` builds ``request_meta`` for a multi-run batch
    using ``prompt_version_for_mode``, which server.py never bound at module
    level. The file's only import of it lives inside
    ``_write_history_record`` -- a function-local import -- so the name simply
    was not there.

    The batch had already been extracted by then: the runs were paid for, the
    merged result was assembled, and the request died while stamping its own
    provenance. Every batched extraction (runs > 1) hit it.
    """

    def test_the_method_binds_the_name_it_uses(self):
        import ast
        from pathlib import Path
        import server
        tree = ast.parse(Path(server.__file__).read_text(encoding="utf-8"))
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and \
                    node.name == "_handle_extract_body":
                fn = node
                break
        assert fn is not None, "_handle_extract_body disappeared"
        imports = [n.lineno for n in ast.walk(fn)
                   if isinstance(n, ast.ImportFrom) and any(
                       a.name == "prompt_version_for_mode" for a in n.names)]
        loads = [n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Name) and n.id == "prompt_version_for_mode"
                 and isinstance(n.ctx, ast.Load)]
        assert loads, "the multi-run branch no longer reads the prompt version"
        assert imports, ("prompt_version_for_mode is used in "
                         "_handle_extract_body with no binding in scope")
        assert imports[0] < loads[0], (
            "the import must precede the use; got import at %s, use at %s"
            % (imports, loads))

    def test_the_module_namespace_does_not_rely_on_a_sibling_import(self):
        # Guards the shape of the bug: a function-local import in ONE function
        # is not a module binding, which is what made this invisible.
        import server
        assert "prompt_version_for_mode" not in vars(server), (
            "if this is now a module-level import, drop the local one and "
            "update this test -- two bindings is how the confusion started")

    def test_the_imported_function_is_the_one_the_parser_uses(self):
        from rca_core.prompt import prompt_version_for_mode
        assert prompt_version_for_mode("range_chart") == "v5"
        assert prompt_version_for_mode("abundance_diagram") == "v4"
        assert prompt_version_for_mode("no_such_mode") == "v3"# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: the provider connection test discarded the upstream's own
# error message, and the fix had to survive a bytes/str mismatch.
# ---------------------------------------------------------------------------

_SECRET = "sk-proj-AAAABBBBCCCCDDDDEEEEFFFF"
_UPSTREAM = (
    '{"error": {"type": "invalid_request_error", "message": '
    '"The model `claude-x` does not exist. key=' + _SECRET + '"}}'
)


def _install_failing_probe(monkeypatch, body=_UPSTREAM, status=404):
    """Make every urlopen raise HTTPError carrying ``body``.

    A real urlopen RAISES on 4xx. A fake that RETURNS a response object makes
    _post_json take its success branch instead, which looks exactly like the
    bug being hunted (ok=True, empty error_body) and sends you hunting in the
    wrong file.
    """
    import io
    import urllib.error
    import urllib.request

    def fake(req, timeout=None):
        raise urllib.error.HTTPError(
            getattr(req, "full_url", str(req)), status, "probe failed",
            {}, io.BytesIO(body.encode("utf-8")))

    monkeypatch.setattr(urllib.request, "urlopen", fake)


class TestP2ConnectionTestSurfacesTheUpstreamReason:
    """`_probe_minimal_generate` tracked the upstream error body through every
    candidate model into `last_err_body` and then returned a ConnectionResult
    that could not carry it. The GUI rendered exactly two facts --
    ``✗ err.http (HTTP 400)`` -- while the server had said "the model does not
    exist" or "your quota is exhausted".

    For a feature whose entire job is diagnosing a CONFIGURATION, that is the
    wrong default, and it is the same shape as the `best_latency` bug that
    REVIEW-2026-11-07 fixed two lines above: a measurement taken carefully and
    never reported.
    """

    def _probe(self, monkeypatch, body=_UPSTREAM, status=404):
        import rca_core.llm as L
        _install_failing_probe(monkeypatch, body, status)
        prov = L.LlmProvider(
            name="X", api_format=L.ApiFormat.ANTHROPIC,
            # loopback is whitelisted by _is_safe_endpoint, and the probe is
            # reached at all; a public name would fail DNS in a sandbox and
            # come back as err.badEndpoint before anything is probed.
            endpoint="http://127.0.0.1:9/",
            api_key="sk-test-key-0000000000", model="claude-x")
        return L.test_llm_connection(prov, timeout_sec=3)

    def test_the_failure_still_reports_its_status_and_key(self, monkeypatch):
        res = self._probe(monkeypatch)
        assert res.ok is False
        assert res.status == 404
        assert res.error_key == "err.http"

    def test_the_upstream_message_reaches_the_result(self, monkeypatch):
        res = self._probe(monkeypatch)
        assert res.error_body, "the upstream body was dropped again"
        assert "does not exist" in res.error_body, res.error_body

    def test_a_rejected_key_is_redacted_out_of_it(self, monkeypatch):
        # This is the whole reason the field is not just `err_body`: an
        # upstream error is where a rejected key comes back echoed.
        res = self._probe(monkeypatch)
        assert _SECRET not in res.error_body, res.error_body
        assert "[REDACTED]" in res.error_body, res.error_body

    def test_the_bytes_slash_str_mismatch_is_pinned(self, monkeypatch):
        """_post_json returns the body as BYTES and redact_error_body answers
        "" for anything that is not a str, so the first version of the fix
        decoded to nothing and looked correct. This asserts on a body that is
        NOT valid utf-8-decodable in the strict sense only -- i.e. it exercises
        _decode_err_body, which is what keeps the value from being empty."""
        import rca_core.llm as L
        # latin-1 bytes: _decode_err_body falls back for these
        body = '{"msg": "café denied"}'.encode("latin-1")
        res = self._probe(monkeypatch, body=body.decode("latin-1"), status=500)
        assert res.ok is False
        # whether the decode lands on utf-8-with-replace or latin-1, the point
        # is that it is NON-EMPTY -- a bytes passthrough would be "".
        assert res.error_body, "the body did not survive decode+redact"
        assert L._decode_err_body(body), "the decoder itself returned nothing"

    def test_success_carries_no_error_body(self):
        import rca_core.llm as L
        assert L.ConnectionResult(ok=True, latency_ms=5).error_body == ""

    def test_the_new_field_is_optional_for_every_existing_caller(self):
        import rca_core.llm as L
        # constructed positionally elsewhere in the codebase / by callers
        p = L.ConnectionResult(False, 0, None, "err.http")
        assert p.error_key == "err.http"
        assert p.error_body == ""
        assert L.ConnectionResult().error_body == ""

    def test_a_bodyless_failure_is_not_an_error(self, monkeypatch):
        # 401 with no body must still work -- the field is optional input.
        res = self._probe(monkeypatch, body="", status=401)
        assert res.ok is False
        assert res.error_body == ""


class TestP2RedactionIsSharedNotCopied:
    """`rca_core/redact.py` exists because a guard that lives on one path reads
    as "handled" while the path that actually echoes a key has none. The
    server had the pattern; the connection probe now needs the same one, and a
    second copy is how the AUDIT-2026-09-27 `|`-precedence narrowing would have
    come back.
    """

    def test_server_uses_the_shared_function_not_a_local_copy(self):
        import server
        from rca_core.redact import redact_error_body
        assert server._redact_error_body is redact_error_body

    def test_llm_uses_the_shared_function(self):
        import rca_core.llm as L
        from rca_core.redact import redact_error_body
        assert L.redact_error_body is redact_error_body

    def test_the_value_class_is_inside_the_alternation_group(self):
        # `|` binds loosest, so a value class written as a SIBLING branch means
        # every branch but the last silently lost its value matcher. Asserted
        # on behaviour instead of on the pattern text, because the text is
        # what people get wrong.
        from rca_core.redact import redact_error_body
        for body, leaked in (
            ("x-api-key=sk-abc.defghijklmnop", "sk-abc.defghijklmnop"),
            ("Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM", "SflKxwRJSM"),
            ('{"api_key": "Zq7Xw9Kp2LmN4Qr"}', "Zq7Xw9Kp2LmN4Qr"),
            ("?key=Zq7Xw9Kp2LmN4Qr&x=1", "Zq7Xw9Kp2LmN4Qr"),
        ):
            out = redact_error_body(body)
            assert leaked not in out, (body, out)
            assert "[REDACTED]" in out, (body, out)

    def test_ordinary_prose_survives(self):
        # The over-redaction half: a redacted diagnosis is worth less than a
        # visible one, and test_review_2026_09_10.py pins this for the server.
        from rca_core.redact import redact_error_body
        for text in ("plain error message",
                     "The model `claude-x` does not exist",
                     "the monkey sat on a key"):
            assert redact_error_body(text) == text, text

    def test_non_strings_yield_empty_rather_than_raising(self):
        from rca_core.redact import redact_error_body
        for v in (None, 123, b"bytes", ["a"]):
            assert redact_error_body(v) == ""# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: ResultCache created the WRONG directory.
# ---------------------------------------------------------------------------

class TestP2ResultCacheCreatesTheDirectoryItWasGiven:
    """``_ensure_dir()`` took no argument and created the module-level
    ``_CACHE_DIR`` -- the parent of the DEFAULT path, which has nothing to do
    with the path the instance was handed::

        ResultCache(db_path=".../does/not/exist/cache.sqlite")
          -> sqlite3.OperationalError: unable to open database file

    and the missing directory was still missing afterwards, because the one
    that got created was the unrelated module-level one.

    The default path never exposed it (~/.range_chart_analyzer always
    exists), and no test reached it either, because they all pass a flat
    filename into an already-created tempdir.

    rca_core/db.py does this correctly, forty lines away in the same package,
    so the project contained two answers to the same question and the cache
    held the wrong one.
    """

    @staticmethod
    def _nested(db_root):
        import os
        return os.path.join(db_root, "does", "not", "exist", "cache.sqlite")

    def test_a_nested_missing_directory_is_created(self, tmp_path):
        import os
        from rca_core.cache import ResultCache
        path = self._nested(str(tmp_path))
        assert not os.path.isdir(os.path.dirname(path))
        cache = ResultCache(db_path=path)
        try:
            assert os.path.isdir(os.path.dirname(path))
            # and it is a working cache, not just a directory
            cache.put("k", {"a": 1})
            assert cache.get("k") == {"a": 1}
        finally:
            cache.close()

    def test_it_creates_the_GIVEN_parent_not_the_module_one(self, tmp_path,
                                                            monkeypatch):
        import rca_core.cache as C
        made = []
        real_makedirs = C.os.makedirs

        def spy(path, *a, **kw):
            made.append(path)
            return real_makedirs(path, *a, **kw)

        monkeypatch.setattr(C.os, "makedirs", spy)
        cache = C.ResultCache(db_path=self._nested(str(tmp_path)))
        cache.close()
        assert made, "no directory was created at all"
        for p in made:
            assert p.startswith(str(tmp_path)), (
                "created %r, which is not under the requested root" % p)

    def test_a_bare_filename_still_works(self, tmp_path):
        # dirname("") is "" and makedirs("") raises -- the shape db.py guards
        # against. The relative-path contract must survive the fix.
        import os
        from rca_core.cache import ResultCache
        cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            cache = ResultCache(db_path="bare.sqlite")
            try:
                cache.put("k", {"b": 2})
                assert cache.get("k") == {"b": 2}
            finally:
                cache.close()
        finally:
            os.chdir(cwd)

    def test_the_default_path_is_unchanged(self):
        import rca_core.cache as C
        assert C._CACHE_DIR.endswith(".range_chart_analyzer")
        # the module constant is still exported for anything that reads it
        assert isinstance(C._DB_PATH, str) and C._DB_PATH.endswith(".sqlite")# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: the taxon-name parser, pinned on names that actually occur
# on radiolarian range charts.
# ---------------------------------------------------------------------------

#: (name, genus, species, subgenus, subspecies) -- None where the rank is absent.
#: Every entry is a real shape from this domain. The first two fixed bugs
#: (the abbreviated subgenus "(L.)" and a doubt marker taking the subgenus
#: with it) were found by READING the function; these are the cases that show
#: whether the reading was right.
_TAXON_NAME_CASES = (
    # plain binomials, the overwhelmingly common case
    ("Neoalbaillella optima", "Neoalbaillella", "optima", "", ""),
    ("Palaeoscolecidia bispinosa", "Palaeoscolecidia", "bispinosa", "", ""),
    # the abbreviated subgenus: kept, with the dot, BEFORE the species
    ("Fusulina (L.) fusulina", "Fusulina", "fusulina", "L.", ""),
    ("Fusulina (L.) elongata", "Fusulina", "elongata", "L.", ""),
    # a spelled-out subgenus
    ("Genus (Subgenus) species", "Genus", "species", "Subgenus", ""),
    # doubt markers: the "?" belongs in the reso slot, not glued to the name,
    # and it must NOT take the subgenus down with it
    ("Genus (Subgenus?) species", "Genus", "species", "Subgenus", ""),
    ("Barbarousia ?", "Barbarousia", "", "", ""),
    ("Barbarousia cf. regularis", "Barbarousia", "regularis", "", ""),
    # open nomenclature must not be mistaken for a real epithet
    ("Genus sp.", "Genus", "", "", ""),
    ("Genus sp", "Genus", "", "", ""),
    ("Genus cf. species", "Genus", "species", "", ""),
    ("Genus aff. species", "Genus", "species", "", ""),
    # infraspecific: three tokens, and the middle one is the rank marker
    ("Genus species ssp. trilobata", "Genus", "species", "", "trilobata"),
    # a BARE rank word with no epithet is not a subspecies name
    ("Genus species subspecies", "Genus", "species", "", ""),
    # authorities are not epithets
    ("Neoalbaillella optima De Wever & Dumitrica, 2002",
     "Neoalbaillella", "optima", "", ""),
    ("Fusulina (L.) fusulina Schwager, 1881",
     "Fusulina", "fusulina", "L.", ""),
    # whitespace
    ("  Neoalbaillella   optima  ", "Neoalbaillella", "optima", "", ""),
    # a single word is a group name and is kept as the genus; a chart labelled
    # "Radiolaria" must not vanish
    ("Radiolaria", "Radiolaria", "", "", ""),
    # nothing at all
    ("", "", "", "", ""),
    (None, "", "", "", ""),
)


class TestPbdbTaxonNameParsing:
    def test_every_real_name_splits_as_documented(self):
        from rca_core.standards.pbdb import _split_taxon_name
        bad = []
        for name, genus, species, subgenus, subspecies in _TAXON_NAME_CASES:
            got = _split_taxon_name(name)
            actual = (got["genus_name"], got["species_name"],
                      got["subgenus_name"], got["subspecies_name"])
            want = (genus, species, subgenus, subspecies)
            if actual != want:
                bad.append((name, want, actual))
        assert not bad, "\n".join(
            f"  {n!r}\n    want {w}\n    got  {a}" for n, w, a in bad)

    def test_the_doubt_marker_travels_in_reso_not_in_the_name(self):
        # The AUDIT-2026-09-27 fix had two halves: stop the "?" from deleting
        # the subgenus, and stop it from being glued to the name. Both are
        # asserted separately here, because either alone would look right.
        from rca_core.standards.pbdb import _split_taxon_name
        r = _split_taxon_name("Genus (Subgenus?) species")
        assert r["subgenus_name"] == "Subgenus", r
        assert r["subgenus_reso"] == "?", r

    def test_the_authoritative_string_is_not_split_into_names(self):
        # "Schwager, 1881" must land in neither name field; a taxon that
        # shipped "Fusulina / fusulina / Schwager" would break the join back.
        from rca_core.standards.pbdb import _split_taxon_name
        r = _split_taxon_name("Fusulina (L.) fusulina Schwager, 1881")
        for field in ("genus_name", "species_name", "subgenus_name",
                      "subspecies_name"):
            assert "Schwager" not in r[field], (field, r)
            assert "1881" not in r[field], (field, r)

    def test_never_raises_on_junk(self):
        from rca_core.standards.pbdb import _split_taxon_name
        for junk in (123, 4.5, [], {}, object(), b"bytes", "()", "((("):
            out = _split_taxon_name(junk)
            assert isinstance(out, dict), (junk, out)
            for field in ("genus_name", "species_name", "subgenus_name",
                          "subspecies_name"):
                assert isinstance(out[field], str), (junk, field, out[field])# ---------------------------------------------------------------------------
# AUDIT-2026-09-29: an error_key with no translation renders as the raw dotted
# key on BOTH ends, and one message told the user its warning was normal.
# ---------------------------------------------------------------------------

#: Read from the source at run time, deliberately NOT restated here. Two
#: earlier drafts hard-coded a copy of RCA_KNOWN_ERROR_KEYS into this module
#: and both went stale within the same hour -- a list duplicated next to its
#: origin is a second place to forget to update. Same for the emitted keys
#: below: they are collected from the sources rather than typed.
def _js_error_gate():
    import re
    minimax = (REPO / "js" / "minimax.js").read_text(encoding="utf-8")
    m = re.search(r"RCA_KNOWN_ERROR_KEYS\s*=\s*new Set\(\[(.*?)\]\)",
                  minimax, re.S)
    assert m, "RCA_KNOWN_ERROR_KEYS not found -- was the gate renamed?"
    return set(re.findall(r"'([A-Za-z0-9_.\-]+)'", m.group(1)))


def _error_keys_the_backend_emits():
    """Every error_key literal in server.py / rca_core, collected."""
    import re
    pat = re.compile(
        r"""["']error_key["']\s*[":]\s*["']([A-Za-z0-9_.\-]+)["']""")
    found = set()
    files = [REPO / "server.py"] + [
        f for f in (REPO / "rca_core").rglob("*.py") if f.name != "i18n.py"]
    for f in files:
        text = f.read_text(encoding="utf-8", errors="ignore")
        found |= set(pat.findall(text))
        found |= set(re.findall(
            r"""\.?error_key\s*=\s*["']([A-Za-z0-9_.\-]+)["']""", text))
    return found


def _js_i18n_blocks():
    """(zh, en, ja) text blocks, sliced by the file's OWN markers.

    The order is zh, en, ja -- NOT en, zh, ja (RCA_I18N = {zh: ...},
    RCA_I18N.en = ..., RCA_I18N.ja = ...). Reading them as file order
    silently swaps two locales, which is how an audit that "confirmed" a
    wording fix mistook the Chinese string for the English one.
    """
    import re
    from pathlib import Path
    lines = Path(REPO / "js" / "i18n.js").read_text(encoding="utf-8").splitlines()
    zh = next(i for i, l in enumerate(lines) if l.strip() == "zh: {")
    en = next(i for i, l in enumerate(lines) if "RCA_I18N.en = {" in l)
    ja = next(i for i, l in enumerate(lines) if "RCA_I18N.ja = {" in l)
    end = next(i for i, l in enumerate(lines) if "i18n runtime" in l and i > ja)
    return ("\n".join(lines[zh:en]), "\n".join(lines[en:ja]),
            "\n".join(lines[ja:end]))


class TestEveryErrorKeyTheServerEmitsIsRenderable:
    """server.py answers with ``{"ok": false, "error_key": "err.X"}``. The
    browser then renders t(errorKey) and the desktop renders the same key
    through its own catalogue. With no entry, the user sees the literal
    "err.badRequest" -- the failure docs/FRONTEND-REVIEW-2026-08-19.json
    records for quality.fad_lt_lad.

    Three keys were missing from EVERY locale on both sides
    (err.badRequest / err.methodNotAllowed / err.serverBusy) and three more
    existed only in js/i18n.js (err.badContentType / err.forbidden /
    err.rateLimit), so the desktop showed a raw key for those too.
    """

    def test_python_catalogue_has_all_three_locales(self):
        from rca_core.i18n import TRANSLATIONS
        missing = []
        for key in _error_keys_the_backend_emits():
            for loc in ("en", "zh", "ja"):
                value = TRANSLATIONS.get(loc, {}).get(key)
                if not isinstance(value, str) or not value.strip():
                    missing.append(f"{key}[{loc}]")
        assert not missing, "unrenderable in the desktop GUI: " + ", ".join(missing)

    def test_every_emitted_key_is_in_the_browser_catalogue(self):
        # The real invariant, and it is about EMITTED keys, not whitelisted
        # ones: a key the backend can send must have text in js/i18n.js, or
        # t() hands back the raw dotted key. The whitelist being WIDER than the
        # catalogue is safe -- those keys fall back -- so it is deliberately
        # not asserted to be a subset.
        import re
        zh, en, ja = _js_i18n_blocks()
        for key in _error_keys_the_backend_emits():
            for loc, block in (("zh", zh), ("en", en), ("ja", ja)):
                m = re.search(r"'%s':\s*'((?:[^'\\]|\\.)*)'" % re.escape(key),
                              block)
                assert m, f"{key} is emitted by the backend but has no {loc} text"
                assert m.group(1).strip(), f"{key}[{loc}] is empty"

    def test_the_gate_is_not_narrower_than_the_emitter(self):
        # err.methodNotAllowed is emitted by the server but is NOT in
        # RCA_KNOWN_ERROR_KEYS. That is safe today (the gate substitutes a
        # fallback), and it is asserted as a fact rather than left to be
        # rediscovered -- if it ever lands in the gate, the string is already
        # in the catalogue (the test above), so nothing breaks.
        import re
        minimax = (REPO / "js" / "minimax.js").read_text(encoding="utf-8")
        m = re.search(r"RCA_KNOWN_ERROR_KEYS\s*=\s*new Set\(\[(.*?)\]\)",
                      minimax, re.S)
        assert m, "RCA_KNOWN_ERROR_KEYS not found -- the gate was renamed?"
        gate = set(re.findall(r"'([A-Za-z0-9_.\-]+)'", m.group(1)))
        assert "err.methodNotAllowed" not in gate, (
            "it is now whitelisted; the string exists, so this is fine -- but "
            "the gate comment in minimax.js should say so")
        assert set(_js_error_gate()) == gate, (
            "RCA_KNOWN_ERROR_KEYS changed; update _js_error_gate() from the "
            f"source: gate-only={sorted(gate - set(_js_error_gate()))} "
            f"test-only={sorted(set(_js_error_gate()) - gate)}")


class TestTheFadLadWarningDescribesTheViolation:
    """The check fires on an INVERSION -- the bed branch on `top < base`
    (beds count up from the bottom, so a smaller top index is an OLDER top) and
    the age branch on `base_ma < top_ma` (FAD younger than LAD). The English
    string used to say "range top younger than their range base", which is the
    NORMAL ordering, with a contradictory "(LAD before FAD)" appended.

    docs/FRONTEND-REVIEW-2026-08-19.json records this as CONFIRMED; the
    browser half was fixed and the Python half was not, so the desktop GUI
    still told an English researcher that a real inversion was routine.
    """

    KEY = "quality.range_top_lt_base"

    def test_english_text_names_the_earlier_lad(self):
        from rca_core.i18n import TRANSLATIONS
        en = TRANSLATIONS["en"][self.KEY]
        assert "LAD" in en and "FAD" in en, en
        low = en.lower()
        for wrong in ("younger than their range base",
                      "younger than the range base"):
            assert wrong not in low, (
                f"the string still describes the NORMAL ordering: {en!r}")

    def test_the_browser_says_the_same_thing(self):
        import re
        zh, en, ja = _js_i18n_blocks()
        m = re.search(r"'%s':\s*'((?:[^'\\]|\\.)*)'" % re.escape(self.KEY), en)
        assert m, "the key is gone from the en block"
        en_js = m.group(1)
        from rca_core.i18n import TRANSLATIONS
        assert "LAD" in en_js and "FAD" in en_js, en_js
        assert en_js.split("(")[0].strip() != \
            TRANSLATIONS["en"][self.KEY].split("(")[0].strip(), (
            "the two catalogues now disagree on this message's subject")# ---------------------------------------------------------------------------
# AUDIT-2026-09-30: the two engines ship DIFFERENT ICS tables, on purpose, and
# the comment in js/ics_table.js said the opposite.
# ---------------------------------------------------------------------------

#: The eleven stages update_ics.py added. They are in ics_current.json and in
#: the JS mirror; they are NOT in ics_2024.json, which is what the Python core
#: actually loads.
_ELEVEN_UNPROMOTED_STAGES = (
    "Aeronian", "Rhuddanian", "Telychian", "Homerian", "Gorstian",
    "Sheinwoodian", "Ludfordian", "Greenlandian", "Meghalayan",
    "Northgrippian", "Late Pleistocene",
)


class TestTheIcsTableSplitIsDocumentedNotAccidental:
    """js/ics_table.js claimed its eleven added stages were needed "while
    Python placed them". Python does not place them: ics.py:53 loads
    rca_core/resources/ics_2024.json (98 stages) and ics_current.json (109) is
    update_ics.py's un-promoted refresh output with zero runtime consumers.

    The gap is deliberate -- tests/test_ics_current_json_2026_09_22.py records
    the switch as "a research backlog item, NOT an accident to be fixed here"
    -- but the comment stated the opposite, and a comment that says the two
    ends agree is exactly what would prompt someone to "fix" the Python side
    by pointing it at ics_current.json, silently promoting research data past
    the gate update_ics.py puts behind --write-canonical.

    Measured, because the failure is worse than a miss:

        ics_resolve_age_bound("Aeronian") -> (None, None)
        ics_era("Aeronian")               -> None
        ics_stage_from_age(439.5)         -> 'Llandovery'   # a WRONG stage,
                                                          # not None
    """

    def test_python_loads_the_promoted_canonical_not_the_refresh_output(self):
        import ast
        from pathlib import Path
        src = (REPO / "rca_core" / "standards" / "ics.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        loaded = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.endswith(".json"):
                loaded = node.value
                break
        assert loaded == "ics_2024.json", (
            f"rca_core/standards/ics.py now loads {loaded!r}. Promoting "
            "ics_current.json is a deliberate research decision gated behind "
            "scripts/update_ics.py --write-canonical; if that happened, "
            "ICS_VERSION and this test both have to change together.")

    def test_the_eleven_are_in_the_js_mirror_and_not_in_the_python_table(self):
        from rca_core.standards.ics import ICS_2024
        import json
        js = (REPO / "js" / "ics_table.js").read_text(encoding="utf-8")
        for stage in _ELEVEN_UNPROMOTED_STAGES:
            assert f'"{stage}"' in js, f"{stage} vanished from the JS mirror"
            assert stage not in ICS_2024, (
                f"{stage} is now in the Python table too -- the split has "
                "closed, so the comment in js/ics_table.js and this test are "
                "both stale")

    def test_the_split_has_a_visible_cost_not_just_a_miss(self):
        # Recorded because it is the part that would make someone force the
        # change: the desktop does not merely fail to resolve these, it
        # resolves them to the WRONG neighbouring series.
        from rca_core.standards.ics import ics_stage_from_age
        assert ics_stage_from_age(439.5) == "Llandovery", (
            "ics_stage_from_age no longer falls back to the neighbour; if the "
            "Aeronian is now in the table this should be 'Aeronian' and the "
            "comment above needs revising")

    def test_era_and_resolve_report_the_gap_rather_than_guessing(self):
        from rca_core.standards.ics import ics_era, ics_resolve_age_bound
        assert ics_era("Aeronian") is None
        assert ics_resolve_age_bound("Aeronian") == (None, None)

    def test_the_refresh_output_still_exists_and_stays_unpromoted(self):
        # If this file is ever deleted the record of what the next promotion
        # is reviewing disappears with it.
        path = REPO / "rca_core" / "resources" / "ics_current.json"
        assert path.exists(), "ics_current.json was removed"
        data = json.loads(path.read_text(encoding="utf-8"))
        for stage in _ELEVEN_UNPROMOTED_STAGES:
            assert stage in data, f"{stage} missing from the refresh output"# ---------------------------------------------------------------------------
# AUDIT-2026-09-30: eight hand-synced lookup tables, and nothing tested the
# two engines against EACH OTHER.
# ---------------------------------------------------------------------------

_JS_ICS = REPO / "js" / "ics_table.js"


def _js_block(name):
    """The body of a `globalThis.<name> = { ... };` literal."""
    import re
    text = _JS_ICS.read_text(encoding="utf-8")
    m = re.search(r"globalThis\.%s = \{(.*?)\n\};" % re.escape(name), text, re.S)
    assert m, f"{name} not found in js/ics_table.js"
    return m.group(1)


def _js_flat(body, quoted):
    import re
    pat = (r"'([^']+)':\s*'([^']+)'" if quoted else r"([A-Za-z_]+):\s*'([^']+)'")
    return {m.group(1): m.group(2) for m in re.finditer(pat, body)}


def _js_series(body):
    import re
    out = {}
    for m in re.finditer(
            r"'([^']+)':\s*\{name:\s*'([^']+)',\s*stages:\s*\[([^\]]*)\]", body):
        out[m.group(1)] = (m.group(2), re.findall(r"'([^']+)'", m.group(3)))
    return out


def _js_bounds(body):
    import re
    return {m.group(1): (float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r"'?([A-Za-z_一-鿿]+)'?:\s*"
                                 r"\[([-\d.]+),\s*([-\d.]+)\]", body)}


class TestTheSevenLookupTablesMatchAcrossEngines:
    """js/ics_table.js holds eight globals. Exactly one -- the AGE table --
    is deliberately split (ics_2024.json on the Python side, ics_current.json
    on the browser side, gated behind update_ics.py --write-canonical; see
    TestTheIcsTableSplitIsDocumentedNotAccidental).

    The other seven are hand-synced copies of tables in
    rca_core/standards/ics.py, and NOTHING compared the two engines. The
    existing checks all compare the JS table against a JSON FILE, not against
    the Python module:

      tests/test_ics_invariants.py::TestJsMirror  -> ics_current.json
      tests_frontend.js::test_ics_table_vs_ics_current_json -> same file

    so a one-sided edit to any of the seven -- a stage appended to
    _CN_STAGE_ALIASES, a label retargeted, a stage dropped from a series list --
    would have been invisible to CI while the desktop and the browser
    resolved the same Chinese label to different intervals.

    All seven are identical today. Verified 2026-09-30 by extraction, not by
    reading.
    """

    def test_chinese_stage_aliases(self):
        import rca_core.standards.ics as IC
        js = _js_flat(_js_block("RCA_ICS_CN_STAGES"), True)
        assert js == IC._CN_STAGE_ALIASES, (
            "Chinese stage aliases drifted: "
            f"py-only={sorted(set(IC._CN_STAGE_ALIASES) - set(js))} "
            f"js-only={sorted(set(js) - set(IC._CN_STAGE_ALIASES))}")

    def test_chinese_series_aliases(self):
        import rca_core.standards.ics as IC
        js = _js_flat(_js_block("RCA_ICS_CN_SERIES"), True)
        assert js == IC._CN_SERIES_ALIASES, (
            "Chinese series aliases drifted: "
            f"py-only={sorted(set(IC._CN_SERIES_ALIASES) - set(js))} "
            f"js-only={sorted(set(js) - set(IC._CN_SERIES_ALIASES))}")

    def test_series_stage_lists(self):
        import rca_core.standards.ics as IC
        py = {k: (v[0], list(v[1])) for k, v in IC._SERIES_STAGE_LISTS.items()}
        js = _js_series(_js_block("RCA_ICS_SERIES"))
        assert js == py, (
            "series -> (canonical name, stage list) drifted: "
            f"py-only={sorted(set(py) - set(js))} "
            f"js-only={sorted(set(js) - set(py))} "
            f"changed={sorted(k for k in set(py) & set(js) if py[k] != js[k])}")

    def test_period_bounds(self):
        # Python derives these from the table at import; JS hard-codes them.
        # A divergence here means one side was promoted and the other was not.
        import rca_core.standards.ics as IC
        py = {k.lower(): v for k, v in IC._PERIOD_BOUNDS.items()}
        js = _js_bounds(_js_block("RCA_ICS_PERIODS"))
        assert js == py, (
            f"period bounds drifted: py={py} js={js}")

    def test_english_period_names(self):
        import rca_core.standards.ics as IC
        js = _js_flat(_js_block("RCA_ICS_PERIOD_NAMES"), False)
        assert js == IC._EN_PERIOD_ALIASES

    def test_chinese_period_bounds(self):
        # Caught by the coverage guard below on the first run: the Chinese
        # BOUNDS table is a separate global from the English one, and it was
        # the one table this file had not yet compared.
        import rca_core.standards.ics as IC
        py = {label: IC._PERIOD_BOUNDS.get(period)
              for label, period in IC._CN_PERIOD_ALIASES.items()}
        js = _js_bounds(_js_block("RCA_ICS_CN_PERIODS"))
        assert js == py, (
            "Chinese period bounds drifted (label -> the bounds of the "
            f"canonical period): py={py} js={js}")

    def test_chinese_period_names(self):
        import rca_core.standards.ics as IC
        js = _js_flat(_js_block("RCA_ICS_CN_PERIOD_NAMES"), True)
        assert js == IC._CN_PERIOD_ALIASES

    def test_every_alias_target_resolves_on_both_engines(self):
        # A target that is absent from the table is a silent dead mapping: the
        # label "resolves" to an interval the scorer cannot then place.
        import rca_core.standards.ics as IC
        from rca_core.standards.ics import ICS_2024
        dead = sorted({v for v in IC._CN_STAGE_ALIASES.values()
                       if v not in ICS_2024})
        assert not dead, f"aliases point at stages absent from the table: {dead}"

    def test_the_guard_covers_every_table_the_file_assigns(self):
        # If someone adds a ninth lookup table, this list must grow with it,
        # otherwise the new table is silently unguarded. It earned its keep on
        # the first run: it caught RCA_ICS_CN_PERIODS, the one table the
        # comparisons above had missed.
        import re
        text = _JS_ICS.read_text(encoding="utf-8")
        assigned = set(re.findall(r"globalThis\.(RCA_ICS[A-Z_]*) =", text))
        guarded = {
            "RCA_ICS_TABLE",          # deliberately split, guarded elsewhere
            "RCA_ICS_CN_STAGES",
            "RCA_ICS_SERIES",
            "RCA_ICS_CN_SERIES",
            "RCA_ICS_PERIODS",
            "RCA_ICS_CN_PERIODS",
            "RCA_ICS_PERIOD_NAMES",
            "RCA_ICS_CN_PERIOD_NAMES",
        }
        assert assigned == guarded, (
            "js/ics_table.js gained/lost a table: "
            f"unguarded={sorted(assigned - guarded)} "
            f"stale={sorted(guarded - assigned)}")