"""REVIEW-2026-09-20 closing round: redraw positions, bed-subscript metrics,
names hardening tail, usage success-rate denominator.

Three independent defects that all share the same shape - a metric that
reports more confidence than the data supports:

1. ``rca_core.redraw`` fabricated the stratigraphic position of any row whose
   bed label did not parse (ordinal ``i * 2``), and ``min()/max()`` smoothed
   an inverted FAD/LAD pair into a plausible bar. A validation overlay that
   invents its own coordinates cannot validate anything.
2. ``rca_core.eval_metrics._parse_bed`` returned ``parse_bed_int``, so the
   scorer dropped the bed subscript the shared parser had kept: predicted
   "Bed 23c" vs ground truth "Bed 23d" scored as an EXACT match.
3. ``rca_core.names.verify_name_gbif`` still did its name cleaning and URL
   building outside the guard that carries the "never raises" contract, and
   ``rca_core.usage.UsageStore.summary`` needed ``status_code IS NULL`` rows
   excluded from the success-rate denominator (asserted here in pytest form;
   the repo's ``tests_usage.py`` is a standalone script pytest never
   collects).
"""
from __future__ import annotations

import json
import time

import pytest

from rca_core.eval_metrics import range_base_accuracy, range_top_accuracy
from rca_core.names import (
    DEFAULT_BATCH_BUDGET_SECONDS,
    name_issues,
    verify_name_gbif,
    verify_names,
)
from rca_core.redraw import (
    SOURCE_BED,
    SOURCE_INDEX,
    SOURCE_NONE,
    redraw_range_chart,
    resolve_species_range_rows,
    _caption,
)


def _row(species, base=None, top=None, base_idx=None, top_idx=None):
    row = {"species": species}
    if base is not None:
        row["range_base"] = base
    if top is not None:
        row["range_top"] = top
    if base_idx is not None:
        row["range_base_idx"] = base_idx
    if top_idx is not None:
        row["range_top_idx"] = top_idx
    return row


def _by_species(resolved):
    return {e["species"]: e for e in resolved}


# ---------------------------------------------------------------------------
# 1. redraw: positions must come from the extraction, never from the ordinal
# ---------------------------------------------------------------------------


class TestRedrawPositionResolution:
    def test_bed_labels_are_parsed(self):
        resolved, summary = resolve_species_range_rows([
            _row("Clarkina yini", base="8", top="13"),
            _row("Hindeodus parvus", base="Bed 27", top="Bed 27"),
        ])
        assert _by_species(resolved)["Clarkina yini"]["base"] == 8.0
        assert _by_species(resolved)["Clarkina yini"]["top"] == 13.0
        assert _by_species(resolved)["Hindeodus parvus"]["base_source"] == SOURCE_BED
        assert summary == {
            "rows_total": 2, "rows_plotted": 2, "located": 2, "unresolved": 0,
            "partially_located": 0, "inverted": 0, "truncated": 0,
        }

    def test_index_fields_win_over_labels(self):
        """range_base_idx / range_top_idx are the extractor's structured
        positions and take priority over the label strings."""
        resolved, _ = resolve_species_range_rows([
            _row("A", base="Bed 26", top="Bed 24", base_idx=4, top_idx=9),
        ])
        e = _by_species(resolved)["A"]
        assert (e["base"], e["top"]) == (4.0, 9.0)
        assert e["base_source"] == e["top_source"] == SOURCE_INDEX
        # Priority matters for the verdict too: the labels read inverted,
        # the indices do not, and the indices are what the row claims.
        assert e["inverted"] is False

    def test_unlocatable_row_has_no_fabricated_position(self):
        """The v1 bug: a row without a readable bed was plotted at ``i * 2``.

        It must now come back with BOTH endpoints None and be counted as
        unresolved - nothing invented.
        """
        resolved, summary = resolve_species_range_rows([
            _row("no bed at all"),
            _row("prose instead of a bed", base="top of the marl", top="last shale"),
            _row("absolute ages", base="253 Ma", top="250 Ma"),
            _row("bool is not bed 1", base_idx=True, top_idx=False),
        ])
        assert all(e["base"] is None and e["top"] is None for e in resolved)
        assert all(e["base_source"] == SOURCE_NONE for e in resolved)
        assert summary["unresolved"] == 4
        assert summary["located"] == 0
        assert summary["inverted"] == 0

    def test_partially_located_row_is_unresolved_not_smoothed(self):
        resolved, summary = resolve_species_range_rows([
            _row("only a base", base="Bed 12"),
        ])
        e = _by_species(resolved)["only a base"]
        assert e["base"] == 12.0 and e["top"] is None
        assert e["unresolved"] is True
        assert summary["unresolved"] == 1 and summary["partially_located"] == 1

    def test_non_dict_row_is_reported_not_dropped(self):
        resolved, summary = resolve_species_range_rows(["junk", 42, None])
        assert len(resolved) == 3
        assert all(e["unresolved"] for e in resolved)
        assert resolved[0]["species"].startswith("row 1")
        assert summary["unresolved"] == 3

    def test_inverted_pair_stays_inverted(self):
        """base > top is the anomaly (exporter's range_base_le_range_top
        convention); min()/max() used to erase it."""
        resolved, summary = resolve_species_range_rows([
            _row("inverted", base="26", top="24"),
            _row("normal", base="24", top="26"),
        ])
        inv = _by_species(resolved)["inverted"]
        assert inv["base"] == 26.0 and inv["top"] == 24.0  # not swapped
        assert inv["inverted"] is True
        assert _by_species(resolved)["normal"]["inverted"] is False
        assert summary["inverted"] == 1

    def test_unstringifiable_species_falls_back_to_the_row_number(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("nope")

        resolved, summary = resolve_species_range_rows([
            {"species": Boom(), "range_base": "3", "range_top": "5"},
        ])
        assert resolved[0]["species"] == "row 1"
        assert summary["located"] == 1

    def test_summary_counts_truncation(self):
        rows = [_row(f"s{i}", base="1", top="2") for i in range(10)]
        resolved, summary = resolve_species_range_rows(rows, max_rows=4)
        assert summary["rows_plotted"] == 4 and summary["truncated"] == 6
        assert summary["rows_total"] == 10

    def test_caption_reports_unresolved_rows(self):
        _, summary = resolve_species_range_rows([
            _row("a", base="1", top="3"),
            _row("b", base="9", top="7"),
            _row("c"),
        ])
        cap = _caption(summary)
        assert "unresolved bed position: 1" in cap
        assert "inverted base>top: 1" in cap
        assert "2/3 rows located" in cap

    def test_redraw_renders_png_with_unresolved_and_inverted_rows(self):
        pytest.importorskip("matplotlib")
        data = {"species_ranges": [
            _row("Clarkina yini", base="26", top="24"),   # inverted
            _row("Hindeodus parvus", base="8", top="13"),  # normal span
            _row("Novipelliella single bed", base="7", top="7"),  # zero width
            _row("Unreadable row"),                        # unresolved
        ]}
        png = redraw_range_chart(data)
        assert isinstance(png, (bytes, bytearray))
        assert bytes(png[:4]) == b"\x89PNG"

    def test_redraw_renders_png_of_only_point_ranges(self):
        """The single-bed branch draws ticks, and it must not depend on any
        bar existing (a zero-width barh is silently invisible otherwise)."""
        pytest.importorskip("matplotlib")
        png = redraw_range_chart({"species_ranges": [
            _row("A a", base_idx=3, top_idx=3),
            _row("B b", base_idx=5, top_idx=5),
        ]})
        assert png and bytes(png[:4]) == b"\x89PNG"

    def test_redraw_all_unresolved_still_draws(self):
        """Nothing locatable must not mean nothing drawn: the operator still
        needs to see which rows failed."""
        pytest.importorskip("matplotlib")
        png = redraw_range_chart({"species_ranges": [_row("ghost")]})
        assert png and bytes(png[:4]) == b"\x89PNG"

    def test_redraw_degrades_without_rows(self):
        assert redraw_range_chart({"species_ranges": []}) is None
        assert redraw_range_chart({}) is None
        assert redraw_range_chart({"species_ranges": "not a list"}) is None


# ---------------------------------------------------------------------------
# 2. eval_metrics: bed subscripts are part of the answer
# ---------------------------------------------------------------------------


class TestBedSubscriptMetrics:
    def test_subscript_mismatch_is_no_longer_an_exact_match(self):
        pred = [{"species": "A", "range_top": "Bed 23c", "range_base": "Bed 20a"}]
        true = [{"species": "A", "range_top": "Bed 23d", "range_base": "Bed 20a"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["exact"] == 0 and m["wrong"] == 1
        assert m["subscript_mismatch"] == 1
        assert m["acc_exact"] == 0.0 and m["acc_tolerance"] == 0.0
        # A wider window cannot rescue it either: tolerance is a bed_NUM
        # distance allowance.
        assert range_top_accuracy(pred, true, tolerance=5)["acc_exact"] == 0.0
        # The matching base of the same row still scores exact - the change
        # is per endpoint, not an all-or-nothing row verdict.
        assert range_base_accuracy(pred, true)["exact"] == 1

    def test_identical_subscript_is_exact(self):
        pred = [{"species": "A", "range_top": "Bed 23c"}]
        true = [{"species": "A", "range_top": "23c"}]
        assert range_top_accuracy(pred, true)["acc_exact"] == 1.0

    def test_missing_vs_present_subscript_counts_as_wrong(self):
        pred = [{"species": "A", "range_top": "Bed 23"}]
        true = [{"species": "A", "range_top": "Bed 23b"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["exact"] == 0 and m["subscript_mismatch"] == 1

    def test_tolerance_still_covers_bed_number_distance(self):
        pred = [{"species": "A", "range_top": "9c"}]
        true = [{"species": "A", "range_top": "8c"}]
        m = range_top_accuracy(pred, true, tolerance=1)
        assert m["within_tolerance"] == 1 and m["exact"] == 0
        assert m["acc_tolerance"] == 1.0 and m["acc_exact"] == 0.0
        assert range_top_accuracy(pred, true, tolerance=0)["acc_tolerance"] == 0.0

    def test_unparseable_beds_are_skipped_not_guessed(self):
        pred = [{"species": "A", "range_top": "253 Ma"}]
        true = [{"species": "A", "range_top": "8"}]
        m = range_top_accuracy(pred, true)
        assert m == {"exact": 0, "within_tolerance": 0, "wrong": 0,
                     "subscript_mismatch": 0, "acc_exact": 0.0,
                     "acc_tolerance": 0.0}

    def test_range_base_symmetry(self):
        pred = [{"species": "A", "range_base": "Bed 20a"}]
        true = [{"species": "A", "range_base": "Bed 20b"}]
        m = range_base_accuracy(pred, true, tolerance=2)
        assert m["wrong"] == 1 and m["subscript_mismatch"] == 1
        assert m["acc_exact"] == 0.0

    def test_empty_ground_truth_shape(self):
        m = range_top_accuracy([{"species": "A", "range_top": "8"}], [])
        assert m["acc_exact"] == 0.0 and m["subscript_mismatch"] == 0


# ---------------------------------------------------------------------------
# 3. names: the never-raises tail + the batch budget
# ---------------------------------------------------------------------------


def _gbif_body(match_type="EXACT", confidence=98, canonical="Clarkina yini"):
    return json.dumps({"matchType": match_type, "confidence": confidence,
                       "canonicalName": canonical, "scientificName": canonical,
                       "alternatives": []})


class TestNamesHardening:
    def test_list_payload_degrades_to_unavailable(self):
        v = verify_name_gbif("Clarkina yini", fetch=lambda url: "[1, 2, 3]")
        assert v["status"] == "unavailable"
        assert v["error"] == "unexpected_payload_list"

    def test_scalar_payload_degrades_to_unavailable(self):
        assert verify_name_gbif("Clarkina yini", fetch=lambda url: "42")["status"] == "unavailable"
        assert verify_name_gbif("Clarkina yini", fetch=lambda url: "not json")["status"] == "unavailable"

    def test_non_numeric_confidence_is_coerced(self):
        for bad in ("85", None, ["x"], True, {"a": 1}):
            body = json.dumps({"matchType": "FUZZY", "confidence": bad,
                               "canonicalName": "X y", "alternatives": []})
            v = verify_name_gbif("X y", fetch=lambda url, b=body: b)
            assert v["status"] == "ok"
            assert isinstance(v["confidence"], float)
            assert v["confidence"] == (85.0 if bad == "85" else 0.0)

    def test_malformed_alternatives_entries_are_skipped(self):
        body = json.dumps({"matchType": "EXACT", "confidence": 99,
                           "canonicalName": "X y",
                           "alternatives": ["a string", 5, {"canonicalName": "Z"}]})
        v = verify_name_gbif("X y", fetch=lambda url: body)
        assert v["alternatives"] == [
            {"canonical": "Z", "match_type": "", "confidence": 0.0, "status": ""}
        ]

    def test_unstringifiable_name_never_raises(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("nope")

        assert verify_name_gbif(Boom())["status"] == "unavailable"
        v = verify_names([Boom(), "Clarkina yini"],
                         fetch=lambda url: _gbif_body())
        assert set(v) == {"Clarkina yini"}  # the bad row cannot kill the batch

    def test_surrogate_name_never_raises(self):
        # A lone surrogate survives str() but cannot be URL-encoded.
        v = verify_name_gbif("\ud800Clarkina", fetch=lambda url: _gbif_body())
        assert v["status"] in {"unavailable", "unmatched", "ok"}

    def test_batch_survives_one_poisoned_entry(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if "second" in url:
                raise RuntimeError("upstream 500")
            return _gbif_body()

        v = verify_names(["Clarkina first", "Clarkina second", "Clarkina third"],
                         fetch=fetch)
        assert set(v) == {"Clarkina first", "Clarkina second", "Clarkina third"}
        assert v["Clarkina second"]["status"] == "unavailable"
        assert v["Clarkina third"]["status"] == "ok"
        # BORROW-2026-09-20: each unique cleaned name costs TWO round-trips
        # now (parser pre-resolution + backbone match); the poisoned parser
        # call is fail-open and the match call still runs.
        assert len(calls) == 6

    def test_batch_budget_stops_issuing_requests(self):
        calls = []

        def fetch(url):
            calls.append(url)
            time.sleep(0.02)
            return _gbif_body()

        # BORROW-2026-09-20: letter-only binomens - the new local malformed
        # gate (digits -> no network at all) would short-circuit "species 1"
        # style names and defeat the point of THIS test.
        names = [f"Genus {chr(97 + i // 26)}{chr(97 + i % 26)}"
                 for i in range(40)]
        v = verify_names(names, fetch=fetch, budget=0.05)
        assert len(v) == len(names)
        assert len(calls) < 2 * len(names), "budget must stop further round-trips"
        stopped = [n for n, r in v.items() if r.get("error") == "batch_budget_exceeded"]
        assert stopped and v[stopped[0]]["status"] == "unavailable"
        # Everything after the first budget breach is reported, none missing.
        # BORROW-2026-09-20: a processed name spends parser + match = 2 calls.
        processed = len(names) - len(stopped)
        assert len(calls) == 2 * processed

    def test_budget_can_be_disabled(self):
        assert DEFAULT_BATCH_BUDGET_SECONDS > 0
        calls = []
        v = verify_names(["A a", "B b"], fetch=lambda url: calls.append(url) or _gbif_body(),
                         budget=None)
        # 2 names x (parser + backbone match) — BORROW-2026-09-20.
        assert len(calls) == 4 and all(r["status"] == "ok" for r in v.values())

    def test_name_issues_tolerates_non_dict_entries(self):
        issues = name_issues({"A a": "not a dict", "B b": None,
                              "C c": {"status": "unavailable"},
                              "D d": {"match_type": "NONE"}})
        assert [i["msg_key"] for i in issues] == ["names.unmatched"]
        assert issues[0]["name"] == "D d"

    def test_name_issues_coerces_string_confidence(self):
        issues = name_issues({"A a": {"match_type": "FUZZY", "confidence": "88",
                                      "canonical": "B b"}})
        assert issues[0]["confidence"] == 88.0


# ---------------------------------------------------------------------------
# 4. usage: status_code IS NULL is "not reported", not "failed"
# ---------------------------------------------------------------------------


class TestUsageSuccessRateDenominator:
    @pytest.fixture()
    def store(self, tmp_path):
        from rca_core.db import Database
        from rca_core.usage import UsageStore

        db = Database(path=str(tmp_path / "usage.db"))
        try:
            yield UsageStore(db=db)
        finally:
            db.close()

    def test_unrated_rows_leave_the_denominator(self, store):
        from rca_core.usage import UsageRecord

        s = store
        s.record(UsageRecord(input_tokens=10, output_tokens=5, status_code=200))
        s.record(UsageRecord(input_tokens=10, output_tokens=5, status_code=200))
        s.record(UsageRecord(input_tokens=10, output_tokens=5))          # NULL
        s.record(UsageRecord(input_tokens=10, output_tokens=5))          # NULL
        sm = s.summary()
        assert sm.total_requests == 4          # the requests card keeps the true count
        assert sm.rated_requests == 2          # the rate only sees reported statuses
        assert sm.success_count == 2
        assert sm.success_rate == pytest.approx(1.0)

        s.record(UsageRecord(input_tokens=1, output_tokens=1, status_code=500))
        sm2 = s.summary()
        assert sm2.rated_requests == 3
        assert sm2.success_rate == pytest.approx(2 / 3)

    def test_nothing_rated_reports_zero(self, store):
        from rca_core.usage import UsageRecord

        s = store
        s.record(UsageRecord(input_tokens=10, output_tokens=5))
        sm = s.summary()
        assert sm.rated_requests == 0 and sm.total_requests == 1
        assert sm.success_rate == 0.0

    def test_time_window_keeps_the_same_rule(self, store):
        from rca_core.usage import UsageRecord

        s = store
        now = time.time()
        s.record(UsageRecord(timestamp=now - 7 * 86400, input_tokens=9,
                             output_tokens=9, status_code=200))
        s.record(UsageRecord(timestamp=now, input_tokens=1, output_tokens=1))
        sm = s.summary(start_ts=now - 3600)
        assert sm.total_requests == 1 and sm.rated_requests == 0
        assert sm.success_rate == 0.0
