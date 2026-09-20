"""BORROW-2026-09-20 (导出域): tests for the WebPlotDigitizer exchange and the
PBDB upload-schema alignment.

Borrowed from:
  * automeris-io/WebPlotDigitizer — the "one two-column ``x,y`` file per
    dataset" + axis-definition JSON exchange that ``to_wpd`` now emits, so a
    user can refine our extraction in WPD instead of re-digitising the plate.
  * PBDB pbdbUpload-api — the upload template's resolution qualifiers
    (``*_reso``), the three-part name columns and the abundance columns.

The tests are offline and deterministic: no clock is read inside the export
(``exported_at`` is a caller-supplied string), and the numbers the axis
descriptor advertises are the values the user anchors WPD's scale points on.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from rca_core.exporter import (
    WPD_AXIS_JSON_FILENAME,
    WPD_EXPORT_FORMAT,
    _wpd_abscissa,
    _wpd_age_value,
    _wpd_bed_value,
    _wpd_num,
    _wpd_num_text,
    _wpd_slug,
    to_wpd,
)
from rca_core.standards.pbdb import (
    PBDB_COLLECTION_FIELDS,
    PBDB_OCCURRENCE_FIELDS,
    _abundance_for,
    _abundance_lookup,
    _interval_reso,
    _split_taxon_name,
    _TIME_RESO_VOCAB,
    to_pbdb_collections,
    to_pbdb_csv,
    to_pbdb_occurrences,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _range_result(bed=True):
    """A two-section range chart, the shape ``extractor`` normalizes to."""
    if bed:
        rows = [
            {"species": "Pseudotirolites panigoniensis", "section": "Section A",
             "range_base": "Bed 3", "range_top": "Bed 9",
             "range_base_idx": 3, "range_top_idx": 9, "biozone": "Z1"},
            {"species": "Clarkina carli", "section": "Section A",
             "range_base": "Bed 5", "range_top": "Bed 11",
             "range_base_idx": 5, "range_top_idx": 11},
            # second panel: the taxon slot restarts, the file name differs
            {"species": "Palaeopascichnus sp.", "section": "Section B",
             "range_base": "Bed 23a", "range_top": "Bed 24",
             "range_base_idx": None, "range_top_idx": None},
        ]
    else:
        rows = [
            {"species": "Pseudotirolites panigoniensis", "section": "Section A",
             "range_base": "260 Ma", "range_top": "252 Ma"},
            {"species": "Clarkina carli", "section": "Section A",
             "range_base": "Wuchiapingian", "range_top": "Changhsingian"},
        ]
    return {
        "sections": [
            {"name": "Section A", "age_range": "Bed 1 to Bed 12",
             "formations": ["Formation A"], "coordinates": "31N, 117E"},
            {"name": "Section B", "age_range": "", "formations": [], "coordinates": ""},
        ],
        "species_ranges": rows,
    }


def _abundance_result():
    return {
        "sites": [
            {"name": "Site 1", "location": "", "age_range": "", "depth_unit": "m"},
            {"name": "Site 2", "location": "", "age_range": "", "depth_unit": "ft"},
        ],
        "abundances": [
            {"taxon": "Quedrus", "site": "Site 1", "level": "", "depth": 2.5,
             "abundance": 12, "abundance_unit": "%"},
            {"taxon": "Quedrus", "site": "Site 1", "level": "", "depth": 5.0,
             "abundance": "35%", "abundance_unit": "%"},
            {"taxon": "Quedrus", "site": "Site 2", "level": "", "depth": 7.5,
             "abundance": 4, "abundance_unit": "%"},
            {"taxon": "Ambrosia", "site": "Site 1", "level": "", "depth": 2.5,
             "abundance": "common", "abundance_unit": "relative"},
            {"taxon": "Ambrosia", "site": "Site 1", "level": "Bed 4", "depth": "",
             "abundance": 3, "abundance_unit": "%"},
        ],
    }


def _zonation_result():
    return {
        "zonations": [{"name": "Radiolarian", "region": "Tibet",
                       "framework": "", "reference": ""}],
        "zones": [
            {"name": "Zone 1", "zonation": "Radiolarian", "rank": "AZ",
             "age_span": "", "base_age": "254.14", "top_age": "251.902"},
            {"name": "Zone 2", "zonation": "Radiolarian", "rank": "AZ",
             "age_span": "", "base_age": "251.902", "top_age": "250.1"},
            {"name": "Zone 3", "zonation": "Conodont", "rank": "CZ",
             "base_age": "VARIABLE", "top_age": ""},
        ],
        "correlations": [],
    }


# ---------------------------------------------------------------------------
# numeric / text helpers — the parity contract with js/export.js
# ---------------------------------------------------------------------------

class TestWpdNumberContract:
    """``_wpd_num`` / ``_wpd_num_text`` are what the two engines agree on."""

    def test_integral_floats_collapse_to_int(self):
        # JSON.stringify has no "2.0" spelling; Python's json writes one, so
        # the exchange normalises to int before either engine serialises.
        assert _wpd_num(2.0) == 2 and isinstance(_wpd_num(2.0), int)
        assert _wpd_num("9") == 9
        assert _wpd_num_text(2.0) == "2"

    def test_non_numeric_and_nonfinite_are_dropped(self):
        for value in (None, True, False, "", "common", "251.902 Ma", float("nan"),
                      float("inf")):
            assert _wpd_num(value) is None, value

    def test_rounding_matches_tofixed6(self):
        assert _wpd_num(23 + 1 / 26) == 23.038462
        assert _wpd_num_text(23 + 1 / 26) == "23.038462"
        assert _wpd_num_text(2.5) == "2.5"

    def test_slug_keeps_filename_safe_tokens(self):
        assert _wpd_slug("Fig. 3b (a)") == "Fig._3b_a"
        assert _wpd_slug("") == "dataset"
        assert _wpd_slug("  ", "plate") == "plate"


class TestWpdValueReading:
    def test_bed_subscripts_stay_ordered_without_becoming_depths(self):
        a, b, c = (_wpd_bed_value("Bed 23a"), _wpd_bed_value("Bed 23b"),
                   _wpd_bed_value("Bed 24"))
        assert a < b < c
        assert a == pytest.approx(23 + 1 / 26)

    def test_shared_bed_parser_rejects_ages_and_thicknesses(self):
        # The M-1/C-3 lesson: "253 Ma" is not bed 253. This must go through
        # rca_core/bed_parser, so a change there is reflected here.
        assert _wpd_bed_value("253 Ma") is None
        assert _wpd_bed_value("23 m") is None
        assert _wpd_bed_value("Bed 23 to 25") is None

    def test_ages_resolve_only_through_ics(self):
        assert _wpd_age_value("Wuchiapingian", "older") == 259.51
        assert _wpd_age_value("Bed 7", "older") is None
        # A bare number is a bed / sample id on a range chart...
        assert _wpd_age_value("251.902", "older") is None
        # ...but a printed axis value in a zonation table.
        assert _wpd_age_value("251.902", "older", bare_numeric=True) == 251.902


# ---------------------------------------------------------------------------
# to_wpd — range charts
# ---------------------------------------------------------------------------

class TestWpdRangeChart:
    def test_one_dataset_per_taxon_and_plate_in_file_names(self):
        out = to_wpd(_range_result(), source_file="Fig3_plate.png")
        assert out["format"] == WPD_EXPORT_FORMAT == "rca-wpd/1"
        assert out["mode"] == "range_chart"
        assert out["plate"] == "Fig3_plate"
        assert out["n_datasets"] == 3
        names = sorted(k for k in out["files"] if k.endswith(".csv"))
        assert names == [
            "wpd_Fig3_plate__Section_A__Clarkina_carli.csv",
            "wpd_Fig3_plate__Section_A__Pseudotirolites_panigoniensis.csv",
            "wpd_Fig3_plate__Section_B__Palaeopascichnus_sp.csv",
        ]
        # the plate + taxon are readable in a file listing without opening
        # anything, which is the whole point of per-dataset files
        doc = json.loads(out["json"])
        # datasets keep the result's row order; the manifest sorts by name
        assert [d["file"] for d in doc["datasets"]] == [
            "wpd_Fig3_plate__Section_A__Pseudotirolites_panigoniensis.csv",
            "wpd_Fig3_plate__Section_A__Clarkina_carli.csv",
            "wpd_Fig3_plate__Section_B__Palaeopascichnus_sp.csv",
        ]
        assert doc["files"][0] == WPD_AXIS_JSON_FILENAME
        assert doc["files"][1:] == names

    def test_csv_body_is_plain_two_column_xy(self):
        out = to_wpd(_range_result(), source_file="fig.png")
        text = out["files"]["wpd_fig__Section_A__Pseudotirolites_panigoniensis.csv"]
        assert text == "x,y\n1,3\n1,9\n"
        assert not text.startswith("\ufeff")  # a BOM breaks WPD's header parse
        assert "\r" not in text

    def test_taxon_slot_restarts_per_section(self):
        out = to_wpd(_range_result(), source_file="fig.png")
        by_taxon = {d["taxon"]: d for d in out["datasets"]}
        assert by_taxon["Pseudotirolites panigoniensis"]["x"] == [1, 1]
        assert by_taxon["Clarkina carli"]["x"] == [2, 2]
        # Section B is a different panel of the same plate: slot 1 again, and
        # the bed subscript survives as an ordered fraction.
        assert by_taxon["Palaeopascichnus sp."]["x"] == [1, 1]
        assert by_taxon["Palaeopascichnus sp."]["y"] == [23.038462, 24]

    def test_level_axis_descriptor_documents_the_inversion(self):
        out = to_wpd(_range_result(), source_file="fig.png")
        assert out["y_axis"] == "level"
        axes = out["axes"]
        assert axes["y"]["name"] == "stratigraphic level"
        assert axes["y"]["unit"] == "bed/sample index"
        assert axes["y"]["min"] == 3 and axes["y"]["max"] == 24
        assert axes["y"]["orientation"] == "inverted"
        assert [sp["value"] for sp in axes["y"]["scale_points"]] == [3, 24]
        assert axes["y"]["ticks"] == [3, 8.25, 13.5, 18.75, 24]
        assert "26" in axes["y"]["note"]  # the subscript rule is stated
        assert axes["x"]["min"] == 1 and axes["x"]["max"] == 2

    def test_age_axis_is_used_when_the_plate_prints_ma(self):
        out = to_wpd(_range_result(bed=False), source_file="fig.png")
        assert out["y_axis"] == "age"
        assert out["axes"]["y"]["unit"] == "Ma"
        assert out["axes"]["y"]["min"] == 251.902   # Changhsingian top
        assert out["axes"]["y"]["max"] == 260
        text = out["files"]["wpd_fig__Section_A__Clarkina_carli.csv"]
        assert text == "x,y\n2,251.902\n2,259.51\n"
        # JSON.stringify has no "260.0" spelling, so the manifest must say 260
        # or the two engines would write different bytes for one result
        assert '"max": 260,' in out["json"]
        assert "260.0" not in out["json"]

    def test_y_axis_can_be_forced(self):
        forced = to_wpd(_range_result(bed=False), source_file="fig.png", y_axis="level")
        assert forced["y_axis"] == "level"
        assert forced["datasets"] == []
        assert forced["warnings"][0].startswith("range_endpoints_unresolved:2")
        # an unknown spelling degrades to "auto" instead of raising
        assert to_wpd(_range_result(), y_axis="pixel")["y_axis"] == "level"

    def test_duplicate_taxon_and_section_get_a_collision_suffix(self):
        data = _range_result()
        data["species_ranges"].append(dict(data["species_ranges"][0]))
        out = to_wpd(data, source_file="fig.png")
        names = sorted(out["files"])
        assert "wpd_fig__Section_A__Pseudotirolites_panigoniensis_2.csv" in names
        assert out["n_datasets"] == 4

    def test_open_ended_range_exports_one_point(self):
        data = {"sections": [], "species_ranges": [
            {"species": "A", "section": "", "range_base": "Bed 4",
             "range_base_idx": 4, "range_top": ""}]}
        out = to_wpd(data, source_file="fig.png")
        assert out["datasets"][0]["panel"] == ""
        assert out["datasets"][0]["name"] == "A"
        assert out["files"]["wpd_fig__nopanel__A.csv"] == "x,y\n1,4\n"


# ---------------------------------------------------------------------------
# to_wpd — abundance / zonation / unsupported
# ---------------------------------------------------------------------------

class TestWpdAbundance:
    def test_one_curve_per_taxon_and_site(self):
        out = to_wpd(_abundance_result(), source_file="pollen.png")
        assert out["mode"] == "abundance_diagram"
        assert sorted(d["taxon"] for d in out["datasets"]) == ["Ambrosia", "Quedrus", "Quedrus"]
        files = sorted(k for k in out["files"] if k.endswith(".csv"))
        assert files == [
            "wpd_pollen__Site_1__Ambrosia.csv",
            "wpd_pollen__Site_1__Quedrus.csv",
            "wpd_pollen__Site_2__Quedrus.csv",
        ]
        # x = abundance, y = depth; the "35%" row is the same measurement as
        # 35 with unit "%"
        assert out["files"][files[1]] == "x,y\n12,2.5\n35,5\n"

    def test_axes_follow_the_csv_columns_not_the_picture(self):
        out = to_wpd(_abundance_result(), source_file="pollen.png")
        axes = out["axes"]
        # points are written [abundance, level], so x = abundance even though
        # the diagram draws it horizontally
        assert axes["x"]["name"] == "abundance" and axes["x"]["unit"] == "%"
        assert axes["x"]["min"] == 3 and axes["x"]["max"] == 35
        assert axes["y"]["name"] == "sampled level"
        assert axes["y"]["orientation"] == "inverted"
        assert axes["y"]["min"] == 2.5 and axes["y"]["max"] == 7.5
        # metres, feet AND bed indices share this plate: the axis refuses to
        # pick one of them and the bundle warns about it
        assert axes["y"]["unit"] == "mixed"
        assert any(w.startswith("vertical_units_mixed:ft/index/m")
                   for w in out["warnings"])
        assert "subset" in axes["y"]["note"]

    def test_categorical_abundance_warns_instead_of_inventing_a_number(self):
        out = to_wpd(_abundance_result(), source_file="pollen.png")
        assert any(w.startswith("abundance_points_unresolved:1")
                   for w in out["warnings"])
        ambrosia = [d for d in out["datasets"] if d["taxon"] == "Ambrosia"][0]
        assert ambrosia["y"] == [4] and ambrosia["x"] == [3]
        # the curve's unit comes from the row that IS point data; the
        # "common" row contributed nothing but a skipped-point warning
        assert ambrosia["abundance_unit"] == "%"

    def test_single_unit_plate_keeps_the_site_depth_unit(self):
        data = {
            "sites": [{"name": "S", "depth_unit": "cm"}],
            "abundances": [
                {"taxon": "T", "site": "S", "depth": 1, "abundance": 2},
                {"taxon": "T", "site": "S", "depth": 4, "abundance": 9},
            ],
        }
        out = to_wpd(data, source_file="p.png")
        assert out["axes"]["y"]["unit"] == "cm"
        assert out["warnings"] == []
        assert out["files"]["wpd_p__S__T.csv"] == "x,y\n2,1\n9,4\n"

    def test_percent_signed_cell_is_a_number_plus_a_unit(self):
        assert _wpd_abscissa("35%") == (35, "%")
        assert _wpd_abscissa(35) == (35, "")
        assert _wpd_abscissa("common") == (None, "")
        assert _wpd_abscissa(None) == (None, "")


class TestWpdZonation:
    def test_zone_spans_become_segments(self):
        out = to_wpd(_zonation_result(), source_file="biochron.png")
        assert out["mode"] == "zonation_chart"
        assert out["n_datasets"] == 2
        assert out["y_axis"] == "age"
        assert out["axes"]["y"]["min"] == 250.1
        assert out["axes"]["y"]["max"] == 254.14
        # slot is per zonation scheme, and the unresolvable zone is reported
        assert [d["x"] for d in out["datasets"]] == [[1, 1], [2, 2]]
        assert out["datasets"][1]["panel"] == "Radiolarian"
        assert any(w.startswith("zone_ages_unresolved:1") for w in out["warnings"])


class TestWpdUnsupportedModes:
    def test_columnar_section_says_so_and_still_ships_a_valid_manifest(self):
        data = {"sections": [{"id": "col-1", "name": "Column 1"}],
                "units": [{"section_id": "col-1", "name": "Shale"}]}
        out = to_wpd(data, source_file="col.png")
        assert out["mode"] == "columnar_section"
        assert out["n_datasets"] == 0
        assert out["warnings"][0].startswith("mode_unsupported:columnar_section")
        doc = json.loads(out["json"])
        assert doc["datasets"] == [] and doc["files"] == [WPD_AXIS_JSON_FILENAME]

    def test_empty_and_tree_results(self):
        assert to_wpd({})["mode"] == "unsupported"
        assert to_wpd(None)["mode"] == "unsupported"
        tree = {"nodes": [{"id": "1", "parent": ""}], "species_ranges": []}
        out = to_wpd(tree)
        assert out["mode"] == "unsupported"
        assert out["warnings"] == ["mode_unsupported (no point geometry in this result)"]

    def test_no_species_ranges_at_all(self):
        out = to_wpd({"sections": [{"name": "X"}], "species_ranges": [
            {"species": "", "range_base": "Bed 1"}]})
        assert out["mode"] == "range_chart"
        assert out["warnings"] == ["no_species_ranges"]


# ---------------------------------------------------------------------------
# to_wpd — the manifest itself
# ---------------------------------------------------------------------------

class TestWpdManifest:
    def test_no_clock_inside_the_export(self):
        data = _range_result()
        a = to_wpd(data, source_file="fig.png", exported_at="2026-09-20T00:00:00Z")
        b = to_wpd(data, source_file="fig.png", exported_at="2026-09-20T00:00:00Z")
        assert a["json"] == b["json"]
        assert a["files"] == b["files"]
        assert json.loads(a["json"])["exported_at"] == "2026-09-20T00:00:00Z"
        # no caller timestamp at all -> still byte-identical across calls: an
        # internal datetime.now() here would break the browser parity test
        c = to_wpd(data, source_file="fig.png")
        d = to_wpd(data, source_file="fig.png")
        assert c["json"] == d["json"]
        assert json.loads(c["json"])["exported_at"] is None

    def test_document_shape_and_wpd_compat_block(self):
        out = to_wpd(_range_result(bed=False), source_file="fig.png")
        doc = json.loads(out["json"])
        assert list(doc) == [
            "format", "tool", "tool_version", "exported_at", "source_image",
            "plate", "mode", "y_axis", "axes", "usage", "datasets",
            "n_datasets", "files", "warnings", "wpd",
        ]
        assert doc["tool"] == "Range Chart Analyzer"
        assert doc["source_image"] == "fig.png"
        assert len(doc["usage"]) == 3
        assert "taxon range" in doc["usage"][1]
        assert doc["usage"][2].startswith("The pixel calibration")
        d0 = doc["datasets"][0]
        assert list(d0) == ["name", "taxon", "panel", "plate", "file", "x", "y",
                            "n_points", "base_label", "top_label", "abundance_unit"]
        assert d0["n_points"] == len(d0["x"]) == len(d0["y"])
        axes = doc["axes"]
        assert list(axes) == ["x", "y"]
        # WPD's own key spelling, so the file is at least readable by tools
        # that already speak the WebPlotDigitizer JSON dialect.
        compat = doc["wpd"]["axes"]["yaxis"]
        assert compat["name"] == axes["y"]["name"]
        assert compat["type"] == "int"
        assert [sp["value"] for sp in compat["scalePoints"]] == [
            axes["y"]["min"], axes["y"]["max"]]
        assert compat["ticks"] == [{"value": t} for t in axes["y"]["ticks"]]
        assert doc["wpd"]["datasets"][0]["data"]["1"]["x"] == d0["x"]
        assert doc["wpd"]["datasets"][0]["color"].startswith("#")

    def test_json_has_no_nonfinite_literals(self):
        data = {"sections": [], "species_ranges": [
            {"species": "A", "range_base": "Bed 2", "range_top": "Bed 2",
             "range_base_idx": float("nan"), "range_top_idx": 2}]}
        text = to_wpd(data, source_file="fig.png")["json"]
        assert "NaN" not in text and "Infinity" not in text

    def test_output_dir_writes_every_listed_file(self, tmp_path):
        out = to_wpd(_range_result(), source_file="fig.png",
                     output_dir=str(tmp_path / "wpd"))
        assert len(out["written"]) == len(out["files"]) == 4
        for name, body in out["files"].items():
            path = tmp_path / "wpd" / name
            assert path.is_file()
            raw = path.read_bytes()
            # Windows text mode would have rewritten the LFs to CRLF and the
            # file on disk would no longer match the manifest.
            assert b"\r\n" not in raw
            assert raw.decode("utf-8") == body
            assert raw.startswith(b"x,y\n" if name.endswith(".csv") else b"{")


# ---------------------------------------------------------------------------
# PBDB — three-part name split
# ---------------------------------------------------------------------------

class TestPbdbNameSplit:
    @pytest.mark.parametrize("name,expected", [
        ("Pseudotirolites panigoniensis", ("Pseudotirolites", "panigoniensis", "")),
        ("P. asiaticus (Zheng, 1979)", ("P.", "asiaticus", "")),
        ("Palaeopascichnus sp.", ("Palaeopascichnus", "", "")),
        ("Costa cf. postwenti", ("Costa", "cf. postwenti", "")),
        ("Clarkina? carli in Yang 1978", ("Clarkina?", "carli", "")),
        ("Paltomegus? aff. magnus Meeka, 1979",
         ("Paltomegus?", "aff. magnus", "")),
        ("Clarkina parvidentiformis subsp. triangularis (Mei, 1993)",
         ("Clarkina", "parvidentiformis", "triangularis")),
        ("Acutaria zhugei Yang 1978 ex Smith 1982", ("Acutaria", "zhugei", "")),
        ("Acutaria zhugei Yang, 1978", ("Acutaria", "zhugei", "")),
        ("", ("", "", "")),
        (None, ("", "", "")),
    ])
    def test_split(self, name, expected):
        assert _split_taxon_name(name) == expected

    def test_abundant_tokens_never_fabricate_a_determination(self):
        # "sp. indet." must NOT become a species called "indet."
        assert _split_taxon_name("Clarkina sp. indet.") == ("Clarkina", "", "")

    def test_split_lands_in_the_columns_without_touching_taxon_name(self):
        occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
            {"species": "P. asiaticus (Zheng, 1979)", "range_base": "Bed 1",
             "range_top": "Bed 2"}]})[0]
        assert occ["taxon_name"] == "P. asiaticus (Zheng, 1979)"
        assert (occ["genus"], occ["species"], occ["subspecies"]) == ("P.", "asiaticus", "")


# ---------------------------------------------------------------------------
# PBDB — chronostratigraphic resolution qualifiers
# ---------------------------------------------------------------------------

class TestPbdbReso:
    def test_interval_reso_vocabulary(self):
        assert _interval_reso("Wuchiapingian") == "stage"
        assert _interval_reso("Lopingian") == "series"
        assert _interval_reso("Permian") == "system"
        assert _interval_reso("Paleozoic") == "era"
        assert _interval_reso("Qingshan Formation") == "informal"
        assert _interval_reso("") == ""
        assert _interval_reso("Anything", source="zone") == "zone"
        assert _interval_reso("Anything", measured=True) == "measured"
        # every qualifier the builders can emit is in the documented set
        for name in ("Wuchiapingian", "Lopingian", "Permian", "Paleozoic", "Fm", ""):
            assert _interval_reso(name) in _TIME_RESO_VOCAB

    def test_measured_section_age_is_flagged_as_measured(self):
        result = {
            "sections": [{"name": "X", "age_range": "254.5-252.0 Ma"}],
            "species_ranges": [{"species": "A", "section": "X",
                                "range_base": "Bed 3", "range_top": "Bed 9"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert (occ["max_ma"], occ["min_ma"]) == ("254.5", "252.0")
        assert occ["max_ma_reso"] == "measured" == occ["min_ma_reso"]
        assert occ["early_interval_reso"] == "stage"  # 254.5 -> Wuchiapingian

    def test_lookup_ages_are_flagged_as_a_table_resolution(self):
        result = {
            "sections": [{"name": "X", "age_range": "Lopingian"}],
            "species_ranges": [{"species": "A", "section": "X",
                                "range_base": "Wuchiapingian",
                                "range_top": "Changhsingian"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert occ["max_ma_reso"] == "stage" and occ["min_ma_reso"] == "stage"
        assert occ["early_interval_reso"] == "stage"
        col = to_pbdb_collections(result)[0]
        assert col["early_interval_reso"] == "series"  # Lopingian itself
        assert col["max_ma_reso"] in _TIME_RESO_VOCAB

    def test_bed_only_rows_carry_no_qualifier_lies(self):
        result = {
            "sections": [{"name": "X", "age_range": "Bed 1 to Bed 12"}],
            "species_ranges": [{"species": "A", "section": "X",
                                "range_base": "Bed 7", "range_top": "Bed 9"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert occ["max_ma"] == "" and occ["max_ma_reso"] == ""
        assert occ["early_interval"] == "" and occ["early_interval_reso"] == ""

    def test_biozone_fallback_is_the_weakest_resolution(self):
        result = {
            "sections": [{"name": "X"}],
            "species_ranges": [{"species": "A", "section": "X",
                                "biozone": "Cellomorgana Zone"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert occ["early_interval"] == "Cellomorgana Zone"
        assert occ["early_interval_reso"] == "zone" == occ["late_interval_reso"]

    def test_row_reso_pair_survives_the_validator(self):
        # an incompatible (reversed) pair must blank BOTH the values and the
        # qualifiers - a "measured" tag on an empty cell is worse than none
        from rca_core.standards.pbdb import _validated_pbdb_ages
        assert _validated_pbdb_ages(250.0, 260.0, "", "",
                                    row_reso=("measured", "measured")) == ("", "", "", "")
        assert _validated_pbdb_ages(260.0, 250.0, "", "",
                                    row_reso=("measured", "stage")) == (
            260.0, 250.0, "measured", "stage")
        assert _validated_pbdb_ages(None, None, "260", "250",
                                    section_reso="series") == (260.0, 250.0, "series", "series")


# ---------------------------------------------------------------------------
# PBDB — abundance columns
# ---------------------------------------------------------------------------

class TestPbdbAbundanceColumns:
    def test_peak_over_the_range_wins_and_the_unit_is_carried(self):
        result = {
            "sections": [{"name": "X"}],
            "species_ranges": [{"species": "Quedrus", "section": "X"}],
            "abundances": [
                {"taxon": "Quedrus", "site": "X", "abundance": 12, "abundance_unit": "%"},
                {"taxon": "Quedrus", "site": "X", "abundance": "35%", "abundance_unit": "%"},
                {"taxon": "Quedrus", "site": "X", "abundance": 4, "abundance_unit": "%"},
            ],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert occ["abund_value"] == "35" and occ["abund_unit"] == "%"

    def test_site_scoped_join_does_not_leak_across_sections(self):
        result = {
            "sections": [{"name": "X"}, {"name": "Y"}],
            "species_ranges": [{"species": "Quedrus", "section": "Y"}],
            "abundances": [
                {"taxon": "Quedrus", "site": "X", "abundance": 80, "abundance_unit": "%"},
                {"taxon": "Quedrus", "site": "Y", "abundance": 3, "abundance_unit": "%"},
            ],
        }
        assert to_pbdb_occurrences(result)[0]["abund_value"] == "3"

    def test_row_level_annotation_beats_the_join(self):
        result = {
            "sections": [],
            "species_ranges": [{"species": "Quedrus", "abundance": 7,
                                "abundance_unit": "individuals"}],
            "abundances": [{"taxon": "Quedrus", "abundance": 80, "abundance_unit": "%"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert (occ["abund_value"], occ["abund_unit"]) == ("7", "individuals")

    def test_relative_scale_is_exported_verbatim_not_dropped(self):
        lookup = _abundance_lookup({"abundances": [
            {"taxon": "Ambrosia", "abundance": "common"},
            {"taxon": "Ambrosia", "abundance": "rare"}]})
        value, unit = _abundance_for({"species": "Ambrosia"}, lookup)
        assert value == "common; rare" and unit == ""

    def test_missing_abundance_is_an_empty_cell(self):
        occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
            {"species": "Unknown taxon"}]})[0]
        assert occ["abund_value"] == "" and occ["abund_unit"] == ""

    def test_nonnumeric_abundance_lookup_helper_is_robust(self):
        assert _abundance_lookup(None) == {}
        assert _abundance_lookup({"abundances": "no"}) == {}
        assert _abundance_lookup({"abundances": [{"taxon": " "}]}) == {}


# ---------------------------------------------------------------------------
# PBDB — column order & the CSV writer
# ---------------------------------------------------------------------------

HISTORICAL_OCCURRENCE_FIELDS = [
    "occurrence_id", "taxon_name", "identified_by", "collection_name",
    "formation", "early_interval", "late_interval", "max_ma", "min_ma",
    "latitude", "longitude", "biostratigraphic_zone", "notes",
]
HISTORICAL_COLLECTION_FIELDS = [
    "collection_name", "latitude", "longitude", "formation",
    "early_interval", "late_interval", "max_ma", "min_ma",
]


class TestPbdbColumnOrder:
    def test_historical_columns_keep_their_order(self):
        # an existing download + the js mirror read these BY POSITION, so the
        # new columns may only be appended
        assert PBDB_OCCURRENCE_FIELDS[:13] == HISTORICAL_OCCURRENCE_FIELDS
        assert PBDB_COLLECTION_FIELDS[:8] == HISTORICAL_COLLECTION_FIELDS
        assert set(PBDB_OCCURRENCE_FIELDS[13:]) == {
            "genus", "species", "subspecies", "early_interval_reso",
            "late_interval_reso", "max_ma_reso", "min_ma_reso",
            "abund_value", "abund_unit"}

    def test_builders_emit_exactly_the_declared_fields(self):
        result = {
            "sections": [{"name": "X", "age_range": "254.5-252.0 Ma",
                          "coordinates": "31N, 117E"}],
            "species_ranges": [{"species": "A b", "section": "X", "biozone": "Z"}],
            "abundances": [{"taxon": "A b", "site": "X", "abundance": 2,
                            "abundance_unit": "%"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        col = to_pbdb_collections(result)[0]
        assert list(occ) == PBDB_OCCURRENCE_FIELDS
        assert list(col) == PBDB_COLLECTION_FIELDS

    def test_to_pbdb_csv_headers_match_the_field_lists(self, tmp_path):
        result = {
            "sections": [{"name": "X", "age_range": "Lopingian",
                          "coordinates": "31N, 117E"}],
            "species_ranges": [{"species": "A b", "section": "X"}],
        }
        to_pbdb_csv(result, tmp_path / "out")
        occ_raw = (tmp_path / "out" / "pbdb_occurrences.csv").read_bytes()
        col_raw = (tmp_path / "out" / "pbdb_collections.csv").read_bytes()
        occ_rows = list(csv.reader(io.StringIO(occ_raw.decode("utf-8"))))
        col_rows = list(csv.reader(io.StringIO(col_raw.decode("utf-8"))))
        assert occ_rows[0] == PBDB_OCCURRENCE_FIELDS
        assert col_rows[0] == PBDB_COLLECTION_FIELDS
        assert occ_raw.endswith(b"\n") and col_raw.endswith(b"\n")
        # the LF the DictWriter asked for survives Windows text mode; a silent
        # CRLF rewrite would be platform-dependent bytes
        assert b"\r\n" not in occ_raw
        # every row has exactly as many cells as the header (DictWriter raises
        # on an undeclared key, but a MISSING key only writes a silent "")
        assert all(len(r) == len(occ_rows[0]) for r in occ_rows)
        assert len(occ_rows) == 2 and len(col_rows) == 2
        occ = dict(zip(occ_rows[0], occ_rows[1]))
        assert occ["latitude"] == "31.0" and occ["longitude"] == "117.0"
        assert occ["genus"] == "A" and occ["species"] == "b"

    def test_a_builder_key_missing_from_the_field_list_is_caught(self, tmp_path):
        # guards the "dict contains fields not in fieldnames" failure mode:
        # to_pbdb_csv must derive its header from the same list the builders
        # are checked against
        result = {"sections": [{"name": "X"}], "species_ranges": []}
        to_pbdb_csv(result, tmp_path / "empty")
        text = (tmp_path / "empty" / "pbdb_occurrences.csv").read_text("utf-8")
        assert text.strip() == ",".join(PBDB_OCCURRENCE_FIELDS)
