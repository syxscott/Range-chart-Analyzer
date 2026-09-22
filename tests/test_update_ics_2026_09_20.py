"""BORROW-2026-09-20: offline tests for scripts/update_ics.py.

The network layer is never touched: the pipeline is driven either through
``--offline-fixture`` (tests/fixtures/ics_macrostrat_mini.json, a truncated
Macrostrat defs payload, and tests/fixtures/ics_chart_mini.ttl, a handful of
ICS chart Turtle concepts) or through an injected ``fetch`` callable, so the
mapping / validation / diff / write logic is what actually runs here.
"""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("update_ics", ROOT / "scripts" / "update_ics.py")
update_ics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_ics)

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "ics_macrostrat_mini.json"
FIXTURE_TTL = ROOT / "tests" / "fixtures" / "ics_chart_mini.ttl"
BASELINE = ROOT / "rca_core" / "resources" / "ics_2024.json"

BASELINE_FIELDS = {"rank", "abbrev", "top_ma", "base_ma", "period",
                   "period_top_ma", "period_base_ma", "era"}
PROVENANCE_FIELDS = {"source", "license", "retrieved_at", "ics_version"}


def _row(name, top, base, period, bounds, rank="Stage", era=None):
    return {"rank": rank, "abbrev": name[:2], "top_ma": top, "base_ma": base,
            "period": period, "period_top_ma": bounds[0], "period_base_ma": bounds[1],
            "era": era or {"Permian": "Paleozoic", "Triassic": "Mesozoic",
                           "Silurian": "Paleozoic", "Cambrian": "Paleozoic",
                           "Quaternary": "Cenozoic", "Paleogene": "Cenozoic",
                           "Cretaceous": "Mesozoic"}[period]}


@pytest.fixture
def mini_baseline(tmp_path):
    """A stand-in canonical table covering every period the JSON fixture uses.

    Series-rank rows carry the period bounds a brand-new stage (Rhuddanian)
    needs to be placed, and stay out of the stage ladder - which is exactly
    how rca_core/standards/ics.py consumes the real table.
    """
    permian = (251.902, 298.9)
    triassic = (201.4, 251.902)
    silurian = (419.62, 443.1)
    cambrian = (486.85, 538.8)
    paleogene = (23.04, 66.0)
    cretaceous = (66.0, 143.1)
    quaternary = (0.0, 2.58)
    table = {
        "Wuchiapingian": _row("Wuchiapingian", 254.14, 259.857, "Permian", permian),
        "Changhsingian": _row("Changhsingian", 251.902, 254.14, "Permian", permian),
        "Lopingian": _row("Lopingian", 251.902, 259.857, "Permian", permian, "Series"),
        "Olenekian": _row("Olenekian", 246.7, 250.8, "Triassic", triassic),
        "Induan": _row("Induan", 250.8, 251.902, "Triassic", triassic),
        "Llandovery": _row("Llandovery", 432.9, 443.1, "Silurian", silurian, "Series"),
        "Series 2": _row("Series 2", 506.5, 521.0, "Cambrian", cambrian, "Series"),
        "Danian": _row("Danian", 61.66, 66.0, "Paleogene", paleogene),
        "Maastrichtian": _row("Maastrichtian", 66.0, 72.1, "Cretaceous", cretaceous),
        "Gelasian": _row("Gelasian", 1.8, 2.58, "Quaternary", quaternary),
    }
    path = tmp_path / "ics_2024.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    return path


def run_fixture(tmp_path, fixture, canonical, **kwargs):
    out = tmp_path / "ics_current.json"
    summary = update_ics.run(
        out=out, canonical=canonical, fixture=fixture,
        retrieved_at="2026-09-19T21:00:00Z", reporter=lambda _line: None,
        **kwargs)
    return summary, out


class TestOfflinePipeline:
    def test_output_is_a_flat_superset_of_the_bundled_schema(self, tmp_path, mini_baseline):
        summary, out = run_fixture(tmp_path, FIXTURE_JSON, mini_baseline)
        table = json.loads(out.read_text(encoding="utf-8"))
        assert set(summary["errors"]) == set()
        # the shape rca_core/standards/ics.py assumes: name -> row, nothing else
        assert all(isinstance(row, dict) for row in table.values())
        baseline = json.loads(mini_baseline.read_text(encoding="utf-8"))
        assert set(baseline) <= set(table)
        for name, row in table.items():
            assert BASELINE_FIELDS <= set(row), f"{name} lost a baseline field"
            assert PROVENANCE_FIELDS <= set(row), f"{name} has no provenance"
        assert table["Wuchiapingian"]["source"].startswith("offline-fixture:")
        assert table["Wuchiapingian"]["license"] == "CC-BY 4.0"
        assert table["Wuchiapingian"]["retrieved_at"] == "2026-09-19T21:00:00Z"
        assert table["Wuchiapingian"]["ics_version"] == "ICS v2024/12"

    def test_diff_reports_added_names_and_moved_ages(self, tmp_path):
        summary, _ = run_fixture(tmp_path, FIXTURE_JSON, BASELINE)
        diff = summary["diff"]
        moved = {row["name"]: row for row in diff["age_changed"]}
        # the three boundaries v2026-06 moves, exactly as rca_core/standards
        # /ics.py documents them
        assert moved["Wuchiapingian"]["base_delta"] == pytest.approx(0.347)
        assert moved["Olenekian"]["base_delta"] == pytest.approx(0.9)
        assert moved["Induan"]["top_delta"] == pytest.approx(0.9)
        assert "Rhuddanian" in diff["added"] and "Meghalayan" in diff["added"]
        # nothing is deleted: a baseline name the upstream dropped is carried
        # forward instead, so stored range charts keep resolving it
        assert diff["removed"] == []
        assert "Stage 5" in diff["carried_forward"]

    def test_carried_forward_rows_are_marked_as_such(self, tmp_path):
        summary, out = run_fixture(tmp_path, FIXTURE_JSON, BASELINE)
        table = json.loads(out.read_text(encoding="utf-8"))
        assert table["Stage 5"]["carried_forward"] is True
        assert "carried forward" in table["Stage 5"]["source"]
        assert table["Wuchiapingian"].get("carried_forward") is None

    def test_no_carry_forward_leaves_the_keys_out(self, tmp_path):
        summary, out = run_fixture(tmp_path, FIXTURE_JSON, BASELINE,
                                   carry_forward=False)
        table = json.loads(out.read_text(encoding="utf-8"))
        assert "Stage 5" not in table
        assert summary["diff"]["removed"]

    def test_output_is_reproducible(self, tmp_path, mini_baseline):
        first = tmp_path / "a.json"
        second = tmp_path / "b.json"
        for target in (first, second):
            update_ics.run(out=target, canonical=mini_baseline, fixture=FIXTURE_JSON,
                           retrieved_at="2026-09-19T21:00:00Z",
                           reporter=lambda _line: None)
        assert first.read_bytes() == second.read_bytes()

    def test_series_row_survives_only_where_it_is_the_only_name(self, tmp_path,
                                                                mini_baseline):
        summary, out = run_fixture(tmp_path, FIXTURE_JSON, mini_baseline)
        table = json.loads(out.read_text(encoding="utf-8"))
        # Late Pleistocene has no formal stage -> it stays; it is emitted at
        # series granularity, which is what the bundled table does for Holocene
        assert table["Late Pleistocene"]["rank"] == "Series"
        assert table["Meghalayan"]["rank"] == "Stage"


class TestIcsChartChannel:
    def test_turtle_fixture_maps_labels_hierarchy_and_ratification(self, tmp_path):
        # no canonical table at all: every assertion below is about what the
        # RDF itself provides, not about what the bundled table carries
        summary, _ = run_fixture(tmp_path, FIXTURE_TTL, tmp_path / "absent.json")
        table = summary["table"]
        # "Stage 2" comes from the @en prefLabel, not the gtsd:CambrianStage2 IRI
        assert "Stage 2" in table
        assert table["Stage 2"]["period"] == "Cambrian"
        assert table["Stage 2"]["era"] == "Paleozoic"
        assert table["Stage 2"]["gssp_ratified"] is False
        assert table["Wuchiapingian"]["gssp_ratified"] is True
        assert table["Wuchiapingian"]["series"] == "Lopingian"
        assert table["Wuchiapingian"]["period_base_ma"] == 298.9
        assert summary["version"] == "2026-06"
        # Precambrian: outside the Phanerozoic ladder every consumer supports
        assert "Siderian" not in table
        # an epoch fully tiled by formal stages must not become a row, or
        # ics_stage_from_age would answer "Lopingian" where it says "Wuchiapingian"
        assert "Lopingian" not in table
        assert "Holocene" in table and table["Holocene"]["rank"] == "Series"

    def test_stale_carried_neighbour_is_reported_and_blocks_the_write(self, tmp_path):
        # The fixture moves Wuchiapingian's base to 259.857 while the real
        # 2024 table still has Capitanian ending at 259.51: refreshing one
        # boundary without its neighbour IS an inconsistency and must be loud.
        summary, out = run_fixture(tmp_path, FIXTURE_TTL, BASELINE)
        assert any("Capitanian" in error and "overlap" in error
                   for error in summary["errors"])
        assert not out.exists()


class TestValidation:
    def _row(self, **kwargs):
        # BORROW-2026-09-20: the ladder tests below invent their own ages, so
        # the default period envelope stays "unknown" (0.0) - validate_table
        # skips containment for it and reports only real contradictions.
        row = {"rank": "Stage", "abbrev": "Xy", "top_ma": 100.0, "base_ma": 110.0,
               "period": "Permian", "period_top_ma": 0.0,
               "period_base_ma": 0.0, "era": "Paleozoic", "source": "s",
               "license": "CC-BY-4.0", "retrieved_at": "t", "ics_version": "v"}
        row.update(kwargs)
        return row

    def test_reversed_bounds_are_an_error(self):
        errors, _ = update_ics.validate_table(
            {"Bad": self._row(top_ma=120.0, base_ma=110.0)})
        assert any("not older than" in e for e in errors)

    def test_ladder_must_not_double_count_time(self):
        table = {"Young": self._row(top_ma=100.0, base_ma=105.0),
                 "Old": self._row(top_ma=102.0, base_ma=108.0)}
        errors, _ = update_ics.validate_table(table)
        assert any("overlaps" in e for e in errors)

    def test_gap_and_duplicate_alias_are_warnings_not_errors(self):
        gapped = {"Young": self._row(top_ma=100.0, base_ma=105.0),
                  "Old": self._row(top_ma=108.0, base_ma=110.0)}
        errors, warnings = update_ics.validate_table(gapped)
        assert errors == []
        assert any("gap" in w for w in warnings)
        aliased = {"Wuliuan": self._row(top_ma=504.5, base_ma=506.5),
                   "Stage 5": self._row(top_ma=504.5, base_ma=506.5)}
        errors, warnings = update_ics.validate_table(aliased)
        assert errors == []
        assert any("duplicate alias" in w for w in warnings)

    def test_informal_name_may_not_claim_a_ratified_gssa(self):
        table = {"Stage 9": self._row(gssp_ratified=True)}
        errors, _ = update_ics.validate_table(table)
        assert any("GSSA/GSSP" in e for e in errors)

    def test_missing_hierarchy_is_an_error(self):
        table = {"Orphan": self._row(period="", era="", period_base_ma=0.0,
                                     period_top_ma=0.0)}
        errors, _ = update_ics.validate_table(table)
        assert any("no period assigned" in e for e in errors)

    def test_stage_outside_its_period_envelope_is_an_error(self):
        # Wushiuan at 514 Ma inside a Cambrian that stops at 506.5 would let
        # ics_stage_from_age answer a name for an age it never covered.
        table = {"Odd": self._row(top_ma=100.0, base_ma=110.0,
                                  period_top_ma=105.0, period_base_ma=115.0)}
        errors, _ = update_ics.validate_table(table)
        assert any("not contained" in e for e in errors)

    def test_period_bounds_must_be_shared_by_every_row_of_a_period(self):
        table = {"A": self._row(top_ma=260.0, base_ma=265.0,
                                period_top_ma=251.902, period_base_ma=298.9),
                 "B": self._row(top_ma=210.0, base_ma=250.0,
                                period_top_ma=201.967, period_base_ma=298.9)}
        errors, _ = update_ics.validate_table(table)
        assert any("disagree" in e for e in errors)


def _row_for(top, base, period):
    """One minimal, already-provenanced row for the pure-function tests."""
    return {"rank": "Stage", "abbrev": "x", "top_ma": top, "base_ma": base,
            "period": period, "period_top_ma": 486.85, "period_base_ma": 538.8,
            "era": "Paleozoic", "source": "s", "license": "l",
            "retrieved_at": "t", "ics_version": "v"}


class TestRenameAndDiff:
    def test_same_span_under_a_new_name_reads_as_a_rename(self):
        old = {"Stage 5": _row_for(504.5, 506.5, "Cambrian")}
        new = {"Wuliuan": _row_for(504.5, 506.5, "Cambrian")}
        diff = update_ics.diff_tables(old, new)
        assert diff["renamed"] == [("Stage 5", "Wuliuan", 506.5, 504.5)]
        assert diff["added"] == [] and diff["removed"] == []

    def test_a_moved_boundary_is_not_reported_as_a_rename(self):
        old = {"Stage 5": _row_for(504.5, 506.5, "Cambrian")}
        new = {"Wuliuan": _row_for(504.5, 507.0, "Cambrian")}
        diff = update_ics.diff_tables(old, new)
        assert diff["renamed"] == []
        assert diff["added"] == ["Wuliuan"] and diff["removed"] == ["Stage 5"]

    def test_render_diff_names_the_tables_and_the_moves(self):
        diff = update_ics.diff_tables({"A": _row_for(1.0, 2.0, "Cambrian")},
                                      {"A": _row_for(1.0, 3.0, "Cambrian")})
        text = "\n".join(update_ics.render_diff(diff, old_name="ics_2024.json",
                                                new_name="ics_current.json"))
        assert "ics_2024.json -> ics_current.json" in text
        assert "age moved (1)" in text and "A [Cambrian]" in text


class TestNetworkLayerIsMocked:
    # FIX-2026-09-22 (audit item 5): the working intervals endpoint leads;
    # the documented-but-404ing ages alias is the fallback. These offline
    # tests pin the ORDER so the refresh never pays a guaranteed 404 again.

    def test_working_intervals_endpoint_is_tried_first(self):
        tried = []

        def fetch(url):
            tried.append(url)
            return FIXTURE_JSON.read_text(encoding="utf-8")

        result = update_ics.acquire("macrostrat", fetch=fetch)
        assert "timescale_id=1" in tried[0]
        assert not any("/defs/ages?" in url for url in tried), \
            "the 404-ing ages endpoint must not be hit when intervals works"
        assert result["channel"] == "macrostrat"
        assert len(result["rows"]) >= 14

    def test_ages_endpoint_is_kept_as_fallback(self):
        tried = []

        def fetch(url):
            tried.append(url)
            if "timescale_id=1" in url:
                raise update_ics.IcsUpdateError("HTTP 410 gone")
            return FIXTURE_JSON.read_text(encoding="utf-8")

        result = update_ics.acquire("macrostrat", fetch=fetch)
        assert "timescale_id=1" in tried[0]
        assert any("/defs/ages?" in url for url in tried[1:])
        assert len(result["rows"]) >= 14

    def test_auto_falls_through_to_the_ics_chart(self):
        def fetch(url):
            if "macrostrat.org" in url:
                raise update_ics.IcsUpdateError("403 rate limited")
            return FIXTURE_TTL.read_text(encoding="utf-8")

        result = update_ics.acquire("auto", fetch=fetch)
        assert result["channel"] == "ics"
        assert result["ics_version"] == "2026-06"
        assert result["license"].startswith("CC-BY-4.0")

    def test_every_channel_down_is_actionable(self):
        def fetch(url):
            raise update_ics.IcsUpdateError("cannot reach " + url)

        with pytest.raises(update_ics.IcsUpdateError) as excinfo:
            update_ics.acquire("auto", fetch=fetch)
        message = str(excinfo.value)
        assert "[macrostrat]" in message and "[ics]" in message
        assert "--offline-fixture" in message

    def test_plain_http_and_missing_fixture_are_refused(self, tmp_path):
        with pytest.raises(update_ics.IcsUpdateError):
            update_ics.fetch_url("http://macrostrat.org/api/v2/defs/ages")
        with pytest.raises(update_ics.IcsUpdateError) as excinfo:
            update_ics.load_fixture(tmp_path / "nope.json")
        assert "does not exist" in str(excinfo.value)


class TestCanonicalPromotion:
    def test_write_canonical_backs_up_first(self, tmp_path, mini_baseline):
        # FIX-2026-09-22 (audit item 2): --offline-fixture + --write-canonical
        # is now REFUSED unless --force is passed (fixture data is toy data
        # and must not silently become the authority source). The test keeps
        # its intent - the pre-write backup survives - but opts in with
        # --force, which additionally trips the loud fixture warning.
        lines = []
        summary = update_ics.run(out=tmp_path / "ics_current.json",
                                 canonical=mini_baseline, fixture=FIXTURE_JSON,
                                 retrieved_at="2026-09-19T21:00:00Z",
                                 write_canonical=True, force=True,
                                 reporter=lines.append)
        backup = mini_baseline.with_name(mini_baseline.name + ".bak")
        assert summary["promoted"] and backup.exists()
        assert any("LOUD WARNING" in ln and "offline-fixture" in ln.lower()
                   for ln in lines)
        assert json.loads(backup.read_text(encoding="utf-8"))["Gelasian"]["base_ma"] == 2.58
        promoted = json.loads(mini_baseline.read_text(encoding="utf-8"))
        assert BASELINE_FIELDS <= set(promoted["Rhuddanian"])

    def test_promotion_is_refused_while_the_table_is_inconsistent(self, tmp_path):
        canonical = tmp_path / "ics_2024.json"
        canonical.write_bytes(BASELINE.read_bytes())
        summary = update_ics.run(out=tmp_path / "ics_current.json",
                                 canonical=canonical, fixture=FIXTURE_TTL,
                                 retrieved_at="2026-09-19T21:00:00Z",
                                 write_canonical=True, reporter=lambda _line: None)
        assert not summary["promoted"]
        assert summary["errors"]
        assert not canonical.with_name(canonical.name + ".bak").exists()
        assert json.loads(canonical.read_text(encoding="utf-8")) == json.loads(
            BASELINE.read_text(encoding="utf-8"))

    def test_dry_run_never_touches_the_bundled_file(self, tmp_path):
        before = BASELINE.read_bytes()
        update_ics.run(out=tmp_path / "ics_current.json", canonical=BASELINE,
                       fixture=FIXTURE_JSON, retrieved_at="2026-09-19T21:00:00Z",
                       reporter=lambda _line: None)
        assert BASELINE.read_bytes() == before
        assert not (BASELINE.parent / "ics_2024.json.bak").exists()


class TestAlignmentCsvAndCli:
    def test_alignment_csv_lists_source_and_baseline_names(self, tmp_path):
        csv_path = tmp_path / "alignment.csv"
        update_ics.run(out=tmp_path / "ics_current.json", canonical=BASELINE,
                       fixture=FIXTURE_JSON, retrieved_at="2026-09-19T21:00:00Z",
                       export_csv=csv_path, reporter=lambda _line: None)
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        header = rows[0]
        assert header[:6] == ["name", "baseline_name", "status",
                              "base_delta_ma", "top_delta_ma", "changed_fields"]
        assert "ics_version" in header and "retrieved_at" in header
        names = {row[0]: row for row in rows[1:]}
        status = {row[0]: row[2] for row in rows[1:]}
        assert status["Rhuddanian"] == "new"
        assert status["Wuchiapingian"] == "age-changed"
        assert status["Changhsingian"] == "same"
        assert status["Stage 5"] == "carried-forward"
        # the moved row also carries the signed deltas and the field list
        wu = names["Wuchiapingian"]
        assert float(wu[3]) == pytest.approx(0.347)
        assert wu[5] == "base_ma"

    def test_cli_exit_codes(self, tmp_path, mini_baseline):
        out = tmp_path / "ics_current.json"
        assert update_ics.main(["--offline-fixture", str(FIXTURE_JSON),
                                "--canonical", str(mini_baseline),
                                "--out", str(out),
                                "--retrieved-at", "2026-09-19T21:00:00Z"]) == 0
        assert update_ics.main(["--offline-fixture", str(FIXTURE_TTL),
                                "--canonical", str(BASELINE),
                                "--out", str(tmp_path / "blocked.json"),
                                "--retrieved-at", "2026-09-19T21:00:00Z"]) == 2
        assert update_ics.main(["--offline-fixture",
                                str(tmp_path / "missing.json")]) == 3
