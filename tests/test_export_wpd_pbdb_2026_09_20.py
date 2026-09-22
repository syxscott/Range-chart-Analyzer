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

FIX-2026-09-22 adds the audit's export-domain regressions, one class or test
per numbered bug in the report: bed subscripts that inverted the order (2),
an all-invalid abundance plate that raised IndexError (1), mode precedence and
the silently dropped tables (3), the strictly schema-valid upload sheet (4),
a name parser that fabricated determinations (5), mixed abundance units on the
VALUE axis (6), two exports overwriting each other on disk (7), the JS mirror
drift (8, with the case table in tests_export_parity.js), the missing OWASP
formula guard (9), corrupt abundance numbers (10) and the metadata
self-contradictions (11).
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
    _wpd_suffixed_name,
    get_configs_for_result,
    to_wpd,
)
from rca_core.standards.pbdb import (
    PBDB_COLLECTION_FIELDS,
    PBDB_OCCURRENCE_FIELDS,
    PBDB_UPLOAD_OCCURRENCE_FIELDS,
    _PBDB_SPECIES_RESO_VOCAB,
    _abundance_for,
    _abundance_lookup,
    _interval_reso,
    _pbdb_number,
    _pbdb_upload_projection,
    _split_taxon_name,
    _TIME_RESO_VOCAB,
    to_pbdb_collections,
    to_pbdb_csv,
    to_pbdb_occurrences,
)

# The eight name columns occurrence.schema.js declares, in its own order.
NAME_FIELDS = ("genus_name", "genus_reso", "subgenus_name", "subgenus_reso",
               "species_name", "species_reso", "subspecies_name",
               "subspecies_reso")


def _names(**kw):
    """The expected ``_split_taxon_name`` dict for one rank split."""
    out = {k: "" for k in NAME_FIELDS}
    out.update(kw)
    return out


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
        # FIX-2026-09-22 (item 2): the divisor is 27, not 26. With /26 the 26th
        # letter of the alphabet reached the next integer (23 + 26/26 == 24),
        # so "Bed 23z" scored ABOVE "Bed 24" and inverted the very order the
        # subscript is supposed to preserve.
        assert a == pytest.approx(23 + 1 / 27)

    def test_the_last_letter_still_sorts_below_the_next_bed(self):
        assert _wpd_bed_value("Bed 23z") < _wpd_bed_value("Bed 24")
        assert _wpd_bed_value("Bed 23z") == pytest.approx(23 + 26 / 27)

    def test_multi_letter_suffixes_keep_the_inversion_fixed(self):
        # The old mirror read only ONE character off the suffix, so "Bed 3ab"
        # became 3.0 + "a" == 3.b and "3ab" sorted above "3ac"-as-a-whole;
        # rca_core.bed_parser accepts a multi-letter suffix when the "Bed"
        # keyword is present, and a suffix that is not a single letter is not
        # scored at all rather than scored from its first character.
        assert _wpd_bed_value("Bed 3ab") == pytest.approx(3)
        assert _wpd_bed_value("Bed 12 top") == pytest.approx(12)
        assert _wpd_bed_value("Bed 12 base") == pytest.approx(12)

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
        # FIX-2026-09-22 (item 2): 23a = 23 + 1/27, and the fraction is what
        # keeps the two-point range 23a -> 24 instead of collapsing it.
        assert by_taxon["Palaeopascichnus sp."]["y"] == [23.037037, 24]

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
        # FIX-2026-09-22 (item 11): the note used to read "level level" and
        # advertise the /26 divisor; it now states the rule that is applied.
        assert "letter/27" in axes["y"]["note"]
        assert "/26" not in axes["y"]["note"]
        assert "level level" not in axes["y"]["note"]
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
    # FIX-2026-09-22 (items 4 + 5): the splitter answers in the columns
    # occurrence.schema.js declares - one *_name / *_reso pair per rank - and a
    # qualifier ("cf.", "gen. nov.", a trailing "?") is RESOLUTION information,
    # so it moves into the *_reso column instead of being glued onto, or
    # invented as, an epithet.
    @pytest.mark.parametrize("name,expected", [
        ("Pseudotirolites panigoniensis",
         _names(genus_name="Pseudotirolites", species_name="panigoniensis")),
        ("P. asiaticus (Zheng, 1979)", _names(genus_name="P.", species_name="asiaticus")),
        ("Palaeopascichnus sp.", _names(genus_name="Palaeopascichnus")),
        ("Costa cf. postwenti",
         _names(genus_name="Costa", species_name="postwenti", species_reso="cf.")),
        ("Clarkina? carli in Yang 1978",
         _names(genus_name="Clarkina", genus_reso="?", species_name="carli")),
        ("Paltomegus? aff. magnus Meeka, 1979",
         _names(genus_name="Paltomegus", genus_reso="?", species_name="magnus",
                species_reso="aff.")),
        ("Clarkina parvidentiformis subsp. triangularis (Mei, 1993)",
         _names(genus_name="Clarkina", species_name="parvidentiformis",
                subspecies_name="triangularis")),
        ("Acutaria zhugei Yang 1978 ex Smith 1982",
         _names(genus_name="Acutaria", species_name="zhugei")),
        ("Acutaria zhugei Yang, 1978",
         _names(genus_name="Acutaria", species_name="zhugei")),
        ("", _names()),
        (None, _names()),
        # --- the audit's five fabrications (item 5) -------------------------
        # the bug was the invented species, not the qualifier choice: an
        # occurrence-level doubt ("?") describes THIS determination, so it wins
        # over the name-level "gen. nov." - and taxon_name keeps both verbatim.
        ("Neospiniferites? gen. nov.",
         _names(genus_name="Neospiniferites", genus_reso="?")),
        ("P. asiaticus Zheng", _names(genus_name="P.", species_name="asiaticus")),
        ("cf. Pseudotirolites panigoniensis",
         _names(genus_name="Pseudotirolites", genus_reso="cf.",
                species_name="panigoniensis")),
        ("Costa sp. 1", _names(genus_name="Costa", species_reso="informal")),
        ("Clarkina (Parkinsonina) carli",
         _names(genus_name="Clarkina", subgenus_name="Parkinsonina",
                species_name="carli")),
        # --- the regressions those five fixes flirted with ------------------
        # a doubt mark glued to the SECOND word of a phrase must not strand
        # "gr." in the species column
        ("Trilobita gen. nov. ex gr.?",
         _names(genus_name="Trilobita", genus_reso="n. gen.")),
        # a specimen number after a nomenclatural phrase is not an epithet
        ("Fusulina sp. nov. 3",
         _names(genus_name="Fusulina", species_reso="n. sp.")),
        # an authority parenthesis sits AFTER the species-group name, a
        # subgenus parenthesis BEFORE it; content alone cannot tell them apart
        ("Neogloboboquadrina pachyderma (Ehrenberg) Cushman",
         _names(genus_name="Neogloboboquadrina", species_name="pachyderma")),
        # an elided authority ("d Orbigny") leaves no one-letter epithet
        ("Globigerina bulloides d Orbigny, 1826",
         _names(genus_name="Globigerina", species_name="bulloides")),
        ("Ausculia? ex gr. julia",
         _names(genus_name="Ausculia", genus_reso="?", species_name="julia",
                species_reso="ex gr.")),
        ("Clarkina (Parkinsonina) carli subsp. parva Koh, 1985",
         _names(genus_name="Clarkina", subgenus_name="Parkinsonina",
                species_name="carli", subspecies_name="parva")),
    ])
    def test_split(self, name, expected):
        assert _split_taxon_name(name) == expected

    def test_every_resolution_qualifier_is_in_the_rank_own_enum(self):
        # additionalProperties is false and each *_reso is a CLOSED enum; the
        # subgenus vocabulary in particular says "n. subgen.", not "n. gen.".
        enums = {
            "genus_reso": ("", "aff.", "cf.", "ex gr.", "n. gen.",
                           "sensu lato", "?", '"', "informal"),
            "subgenus_reso": ("", "aff.", "cf.", "ex gr.", "n. subgen.",
                              "sensu lato", "?", '"', "informal"),
            "species_reso": ("", "aff.", "cf.", "ex gr.", "n. sp.",
                             "sensu lato", "?", '"', "informal"),
            "subspecies_reso": ("", "aff.", "cf.", "ex gr.", "n. sp.",
                                "sensu lato", "?", '"', "informal"),
        }
        for name in ("Neospiniferites? gen. nov.", "Fusulina n. sp.",
                     "Clarkina (Xinshanicervella?) n. subgen. carli",
                     "Costa aff. postwenti", "cf. Trilobita"):
            parts = _split_taxon_name(name)
            for column, vocab in enums.items():
                assert parts[column] in vocab, (name, column, parts[column])

    def test_abundant_tokens_never_fabricate_a_determination(self):
        # "sp. indet." must NOT become a species called "indet."
        assert _split_taxon_name("Clarkina sp. indet.") == _names(
            genus_name="Clarkina")

    def test_split_lands_in_the_columns_without_touching_taxon_name(self):
        occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
            {"species": "P. asiaticus (Zheng, 1979)", "range_base": "Bed 1",
             "range_top": "Bed 2"}]})[0]
        assert occ["taxon_name"] == "P. asiaticus (Zheng, 1979)"
        assert (occ["genus_name"], occ["species_name"],
                occ["subspecies_name"]) == ("P.", "asiaticus", "")
        # the old columns the schema does not know are gone
        for gone in ("genus", "species", "subspecies"):
            assert gone not in occ


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
        value, unit, note = _abundance_for({"species": "Ambrosia"}, lookup)
        assert value == "common; rare" and unit == "" and note == ""

    def test_missing_abundance_is_an_empty_cell(self):
        occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
            {"species": "Unknown taxon"}]})[0]
        assert occ["abund_value"] == "" and occ["abund_unit"] == ""

    def test_nonnumeric_abundance_lookup_helper_is_robust(self):
        assert _abundance_lookup(None) == {}
        assert _abundance_lookup({"abundances": "no"}) == {}
        assert _abundance_lookup({"abundances": [{"taxon": " "}]}) == {}

    # --- FIX-2026-09-22 (item 10): the number is not corrupted on the way in
    def test_nan_and_infinity_never_reach_a_numeric_column(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "nan", "inf",
                    "-Infinity"):
            occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
                {"species": "A a", "abundance": bad,
                 "abundance_unit": "%"}]})[0]
            assert (occ["abund_value"], occ["abund_unit"]) == ("", ""), bad
        # ... and the reason is stated, not silently blanked
        occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
            {"species": "A a", "abundance": float("nan"),
             "abundance_unit": "%"}]})[0]
        assert "nan" in occ["comments"]

    def test_a_percent_scale_is_not_silently_renamed(self):
        # "35%" with no declared unit is 35 WITH unit %, exactly like the
        # 35 / unit-% row; both engines of the export say the same thing now.
        value, unit, note = _abundance_for(
            {"species": "A"}, _abundance_lookup({"abundances": [
                {"taxon": "A", "abundance": "35%"}]}))
        assert (value, unit, note) == ("35", "%", "")

    def test_peak_is_not_taken_across_incomparable_units(self):
        # 200 indiv/g is NOT more abundant than 35 % - a maximum across units
        # ranks the UNIT, not the organism. The dominant unit wins and the
        # mixing is reported instead of silently averaged away.
        result = {
            "sections": [{"name": "X"}],
            "species_ranges": [{"species": "Quedrus", "section": "X"}],
            "abundances": [
                {"taxon": "Quedrus", "site": "X", "abundance": 35,
                 "abundance_unit": "%"},
                {"taxon": "Quedrus", "site": "X", "abundance": 12,
                 "abundance_unit": "%"},
                {"taxon": "Quedrus", "site": "X", "abundance": 200,
                 "abundance_unit": "indiv/g"},
            ],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert (occ["abund_value"], occ["abund_unit"]) == ("35", "%")
        assert "units mixed" in occ["comments"]
        assert "indiv/g" in occ["comments"]

    def test_full_precision_survives_the_round_trip(self):
        # "%g" is SIX SIGNIFICANT DIGITS: 35.123456 used to leave the sheet as
        # "35.1235" while the abundance table still carried the original.
        result = {
            "sections": [{"name": "X"}],
            "species_ranges": [{"species": "Quedrus", "section": "X"}],
            "abundances": [{"taxon": "Quedrus", "site": "X", "abundance": 35.123456,
                             "abundance_unit": "%"}],
        }
        occ = to_pbdb_occurrences(result)[0]
        assert occ["abund_value"] == "35.123456"
        assert float(occ["abund_value"]) == 35.123456

    def test_the_number_grammar_is_the_exporter_s(self):
        # float() accepts these; a plate never wrote them. Same ASCII-only
        # rule _wpd_num applies (item 8), so the two sheets agree.
        for text in ("1_000", "２３", "١٢٣", "\u00a035", "35%", "nan", ""):
            number = _pbdb_number(text)
            if text == "35%":
                assert number == 35.0
            else:
                assert number is None, text
        assert _pbdb_number(float("nan")) is None
        assert _pbdb_number(float("inf")) is None
        assert _pbdb_number(12) == 12.0
        assert _pbdb_number("35.5") == 35.5


# ---------------------------------------------------------------------------
# PBDB — the upload sheet is schema-valid on its own (item 4)
# ---------------------------------------------------------------------------

class TestPbdbUploadSheet:
    def _occ(self, **kw):
        base = {"occurrence_id": "RC_1", "taxon_name": "Genus species",
                "genus_name": "Genus", "species_name": "species"}
        base.update(kw)
        return base

    def test_the_upload_header_is_exactly_the_declared_properties(self):
        assert PBDB_UPLOAD_OCCURRENCE_FIELDS == [
            "collection_no", "taxon_name",
            "genus_reso", "genus_name", "subgenus_reso", "subgenus_name",
            "species_reso", "species_name", "subspecies_reso", "subspecies_name",
            "abund_value", "abund_unit", "reference_no", "comments",
        ]

    def test_no_column_outside_the_schema_is_ever_emitted(self):
        row, _notes = _pbdb_upload_projection(self._occ(
            max_ma="252.4", max_ma_reso="measured", notes="author_year: 1993"))
        assert list(row) == PBDB_UPLOAD_OCCURRENCE_FIELDS
        assert "max_ma" not in row and "max_ma_reso" not in row

    def test_required_columns_are_present_even_when_unknown(self):
        row, notes = _pbdb_upload_projection(self._occ())
        assert "collection_no" in row and "reference_no" in row
        assert any("collection_no" in n for n in notes)

    def test_dependent_required_drops_the_orphan_and_says_so(self):
        # species_name requires genus_name; a chain break cascades, so an
        # orphaned subspecies never survives either.
        row, notes = _pbdb_upload_projection(self._occ(
            genus_name="", species_name="species", subgenus_name="X",
            subspecies_name="subspecies"))
        assert row["genus_name"] == ""
        assert (row["species_name"], row["subgenus_name"],
                row["subspecies_name"]) == ("", "", "")
        assert sum("dropped in the upload sheet" in n for n in notes) == 3
        # the extension sheet still carries everything
        assert self._occ(genus_name="")["species_name"] == "species"

    def test_abund_value_without_a_unit_is_not_uploaded(self):
        row, notes = _pbdb_upload_projection(self._occ(abund_value="35"))
        assert row["abund_value"] == "" and row["abund_unit"] == ""
        assert any(n.startswith("abund_value") for n in notes)
        row, notes = _pbdb_upload_projection(self._occ(abund_value="35",
                                                        abund_unit="%"))
        assert row["abund_value"] == "35" and row["abund_unit"] == "%"
        assert not any(n.startswith("abund_value") for n in notes)

    def test_a_resolution_value_the_enum_does_not_allow_is_clamped(self):
        # "n. gen." belongs to genus_reso only; species_reso's enum says
        # "n. sp.", so a hand-edited row cannot smuggle it across.
        row, _ = _pbdb_upload_projection(self._occ(
            genus_reso="n. gen.", species_reso="n. gen.",
            subgenus_reso="measured"))
        assert row["genus_reso"] == "n. gen."
        assert row["species_reso"] == "" and row["subgenus_reso"] == ""

    def test_notes_reach_the_only_free_text_column(self):
        row, _ = _pbdb_upload_projection(self._occ(notes="author_year: 1993"))
        assert row["comments"].startswith("author_year: 1993")

    def test_to_pbdb_csv_writes_three_sheets(self, tmp_path):
        result = {
            "sections": [{"name": "X", "age_range": "Lopingian",
                          "coordinates": "31N, 117E"}],
            "species_ranges": [{"species": "A b", "section": "X"}],
        }
        paths = to_pbdb_csv(result, tmp_path / "out")
        assert sorted(p.name for p in paths.values()) == [
            "pbdb_collections.csv", "pbdb_occurrence_extensions.csv",
            "pbdb_occurrences.csv",
        ]
        upload = list(csv.reader(io.StringIO(
            (tmp_path / "out" / "pbdb_occurrences.csv").read_text("utf-8"))))
        assert upload[0] == PBDB_UPLOAD_OCCURRENCE_FIELDS
        # nothing the schema would reject is in the upload sheet...
        assert "occurrence_id" not in upload[0]
        # ...and everything it cannot hold is in the extension sheet
        ext = list(csv.reader(io.StringIO(
            (tmp_path / "out" / "pbdb_occurrence_extensions.csv").read_text(
                "utf-8"))))
        assert ext[0] == PBDB_OCCURRENCE_FIELDS
        assert ext[0][:13] == HISTORICAL_OCCURRENCE_FIELDS
        row = dict(zip(ext[0], ext[1]))
        assert row["latitude"] == "31.0" and row["longitude"] == "117.0"
        assert row["genus_name"] == "A" and row["species_name"] == "b"

    def test_upload_sheet_never_ships_a_column_the_builder_did_not_name(self,
                                                                       tmp_path):
        result = {"sections": [{"name": "X"}], "species_ranges": []}
        paths = to_pbdb_csv(result, tmp_path / "empty")
        assert (paths["occurrences"].read_text("utf-8").strip()
                == ",".join(PBDB_UPLOAD_OCCURRENCE_FIELDS))
        assert (paths["extensions"].read_text("utf-8").strip()
                == ",".join(PBDB_OCCURRENCE_FIELDS))


# ---------------------------------------------------------------------------
# PBDB — OWASP formula injection reached the PBDB CSVs (item 9)
# ---------------------------------------------------------------------------

class TestPbdbFormulaInjection:
    def test_trigger_led_text_is_neutralised_in_every_sheet(self, tmp_path):
        payload = "=CMD('/c calc')"
        result = {
            "sections": [{"name": payload, "age_range": "Lopingian",
                          "formations": [payload],
                          "coordinates": "31N, 117E"}],
            "species_ranges": [{"species": "A b", "section": payload}],
        }
        paths = to_pbdb_csv(result, tmp_path / "out")
        for key, path in paths.items():
            text = path.read_text("utf-8")
            cells = [c for row in csv.reader(io.StringIO(text)) for c in row]
            assert payload not in cells, key
            assert "'" + payload in cells, (key, cells)
        # the collection_name join still works: the id column keeps the raw
        # string, only the SPREADSHEET cell is inert
        ext = list(csv.reader(io.StringIO(
            paths["extensions"].read_text("utf-8"))))
        row = dict(zip(ext[0], ext[1]))
        assert row["collection_name"] == "'" + payload

    def test_numeric_columns_keep_their_sign(self, tmp_path):
        # a southern-hemisphere section is not an attack; prefixing -31.0 would
        # turn a number into text in every spreadsheet that reads the sheet
        result = {
            "sections": [{"name": "X", "age_range": "Lopingian",
                          "coordinates": "31S, 117E"}],
            "species_ranges": [{"species": "A b", "section": "X"}],
        }
        paths = to_pbdb_csv(result, tmp_path / "out")
        ext = list(csv.reader(io.StringIO(
            paths["extensions"].read_text("utf-8"))))
        row = dict(zip(ext[0], ext[1]))
        assert row["latitude"] == "-31.0"

    def test_a_dropped_value_is_named_not_reproduced_in_comments(self):
        # the note is free text too: quoting a live formula there would move it
        # from a checked column into an unchecked one
        occ = to_pbdb_occurrences({"sections": [], "species_ranges": [
            {"species": "A b", "abundance": '=HYPERLINK("http://evil")'}]})[0]
        assert "=HYPERLINK" not in occ["comments"].replace("'", "")
        assert occ["comments"]  # the drop itself is still explained


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
        # new columns may only be appended - and they now live in the extension
        # sheet, because the upload sheet cannot carry them at all (item 4).
        assert PBDB_OCCURRENCE_FIELDS[:13] == HISTORICAL_OCCURRENCE_FIELDS
        assert PBDB_COLLECTION_FIELDS[:8] == HISTORICAL_COLLECTION_FIELDS
        assert set(PBDB_OCCURRENCE_FIELDS[13:]) == {
            "genus_name", "genus_reso", "subgenus_name", "subgenus_reso",
            "species_name", "species_reso", "subspecies_name",
            "subspecies_reso", "early_interval_reso", "late_interval_reso",
            "max_ma_reso", "min_ma_reso", "abund_value", "abund_unit",
            "comments"}

    def test_the_two_sheets_together_lose_nothing(self):
        # FIX-2026-09-22 (C2): the upload sheet is a strict SUBSET of the
        # extension sheet's vocabulary by construction - the two ranks of
        # columns it shares (taxon + name/*_reso + abundance + comments) live
        # in BOTH files, and the only upload columns the extension sheet does
        # NOT carry are the two PBDB-assigned join numbers: collection_no is
        # derived there from collection_name, reference_no has no local value
        # at all. A user can delete either file without losing data blind.
        only_upload = set(PBDB_UPLOAD_OCCURRENCE_FIELDS) - set(
            PBDB_OCCURRENCE_FIELDS)
        assert only_upload == {"collection_no", "reference_no"}

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
        paths = to_pbdb_csv(result, tmp_path / "out")
        sheets = {
            "pbdb_occurrences.csv": PBDB_UPLOAD_OCCURRENCE_FIELDS,
            "pbdb_occurrence_extensions.csv": PBDB_OCCURRENCE_FIELDS,
            "pbdb_collections.csv": PBDB_COLLECTION_FIELDS,
        }
        for name, fields in sheets.items():
            raw = (tmp_path / "out" / name).read_bytes()
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
            assert rows[0] == fields, name
            assert raw.endswith(b"\n") and b"\r\n" not in raw, name
            # every row has exactly as many cells as the header (DictWriter
            # raises on an undeclared key, but a MISSING key only writes "")
            assert all(len(r) == len(rows[0]) for r in rows), name
            assert len(rows) == 2, name

    def test_a_builder_key_missing_from_the_field_list_is_caught(self, tmp_path):
        # guards the "dict contains fields not in fieldnames" failure mode:
        # to_pbdb_csv must derive its header from the same list the builders
        # are checked against
        result = {"sections": [{"name": "X"}], "species_ranges": []}
        paths = to_pbdb_csv(result, tmp_path / "empty")
        text = paths["extensions"].read_text("utf-8")
        assert text.strip() == ",".join(PBDB_OCCURRENCE_FIELDS)


# ---------------------------------------------------------------------------
# FIX-2026-09-22 (C2): the residuals the first pass left open — the informal
# "sp. 1", the cf./aff. rank rule pinned AS a rule, the upload sheet join
# key, mixed units on the VALUE axis, mode precedence against the shared
# config path, same-directory overwrites and the CJK file names.
# ---------------------------------------------------------------------------

class TestC2NameRules:
    def test_informal_species_hint_survives_without_an_invented_epithet(self):
        parts = _split_taxon_name("Costa sp. 1")
        assert parts["genus_name"] == "Costa"
        assert parts["species_name"] == "" and parts["subspecies_name"] == ""
        # "informal" is a member of the upstream species_reso enum and the
        # ONLY honest slot: the plate cited an informal morphospecies, it did
        # not name one — "1" never becomes an epithet.
        assert parts["species_reso"] == "informal"
        assert parts["species_reso"] in _PBDB_SPECIES_RESO_VOCAB
        # a bare "sp." states no numbered morphospecies: genus-only, as
        # before (the number is what makes "sp. 1" a cited informal taxon)
        assert _split_taxon_name("Palaeopascichnus sp.") == _names(
            genus_name="Palaeopascichnus")

    def test_cf_and_aff_attach_to_the_rank_the_following_name_belongs_to(self):
        # ONE rule, both positions: the marker qualifies the name token that
        # FOLLOWS it, so "cf. Genus epithet" doubts the genus determination
        # while "Genus cf. epithet" doubts the species — the epithet is never
        # glued to the marker and never invented.
        head = _split_taxon_name("cf. Pseudotirolites panigoniensis")
        mid = _split_taxon_name("Costa cf. postwenti")
        aff = _split_taxon_name("Costa aff. postwenti")
        assert (head["genus_reso"], head["species_reso"]) == ("cf.", "")
        assert (mid["genus_reso"], mid["species_reso"]) == ("", "cf.")
        assert (mid["genus_name"], mid["species_name"]) == (
            "Costa", "postwenti")
        assert (aff["species_name"], aff["species_reso"]) == ("postwenti",
                                                              "aff.")


class TestC2UploadJoinKey:
    def test_collection_no_carries_the_local_name_the_sheets_join_on(self):
        # the upload sheet has no collection_name column (additionalProperties
        # is false), so collection_no IS the join onto pbdb_collections.csv;
        # an empty one made the sheet unlinkable and left the CSV formula
        # guard with nothing to guard in that file
        occ = to_pbdb_occurrences({"sections": [{"name": "X"}],
                                   "species_ranges": [
                                       {"species": "A b", "section": "X"}]})[0]
        row, _notes = _pbdb_upload_projection(occ)
        assert row["collection_no"] == "X"
        # without any collection information the placeholder + note stay
        row, notes = _pbdb_upload_projection({"taxon_name": "A b"})
        assert row["collection_no"] == ""
        assert any("collection_no" in n for n in notes)


class TestC2ValueAxisUnits:
    def test_abscissa_parses_a_cell_that_writes_its_own_unit(self):
        assert _wpd_abscissa("12 indiv/g") == (12, "indiv/g")
        assert _wpd_abscissa("2.5 specimens/kg") == (2.5, "specimens/kg")
        # a digit run after the number is a thousands-separator typo, not a
        # unit; letters-only cells stay unresolved; non-ASCII never becomes
        # a number (the _wpd_num rule)
        assert _wpd_abscissa("12 000") == (None, "")
        assert _wpd_abscissa("common") == (None, "")
        assert _wpd_abscissa("１２ indiv/g") == (None, "")

    def test_the_mixed_unit_probe_warns_instead_of_sharing_one_axis(self):
        # FIX-2026-09-22 (item 6) probe: the horizontal twin of
        # vertical_units_mixed — one warning, an honest "mixed" axis unit
        out = to_wpd({"abundances": [
            {"taxon": "A", "site": "S", "level": "1",
             "abundance": "12 indiv/g"},
            {"taxon": "B", "site": "S", "level": "1",
             "abundance": "35 %"}]})
        assert out["warnings"] == ["horizontal_units_mixed:%/indiv/g"]
        assert out["axes"]["x"]["unit"] == "mixed"
        assert "horizontal_units_mixed" in out["axes"]["x"]["note"]

    def test_inline_vs_declared_unit_conflict_names_its_precedence(self):
        # inline beats declared (the unit written next to the number is the
        # more specific evidence) and the disagreement is reported, never
        # resolved silently
        out = to_wpd({"abundances": [
            {"taxon": "A", "site": "S", "level": "1", "abundance": "35%",
             "abundance_unit": "indiv/g"}]})
        assert any(w.startswith("abundance_unit_conflict:indiv/g!=%")
                   for w in out["warnings"])
        assert out["datasets"][0]["abundance_unit"] == "%"


class TestC2ModePrecedence:
    def test_zonation_shape_goes_to_zonation_and_reports_the_drop(self):
        data = {"abundances": [{"taxon": "A", "site": "S", "level": "1",
                                "abundance": 5}],
                "zonations": [{"name": "R"}],
                "zones": [
                    {"name": "z1", "zonation": "R", "rank": "AZ",
                     "base_age": "251", "top_age": "247"},
                    {"name": "z2", "zonation": "R", "rank": "AZ",
                     "base_age": "247", "top_age": "242"}]}
        out = to_wpd(data)
        assert out["mode"] == "zonation_chart"
        assert any(w.startswith("mode_excludes_rows:abundances(1)")
                   for w in out["warnings"])
        # the two engines of one claim: the same detection the GUI uses
        assert [c["id"] for c in get_configs_for_result(data)][0] == \
            "zonations"

    def test_zone_named_bands_without_zone_markers_stay_abundance(self):
        # the OTHER shape of the audit probe: plain "zones" rows without any
        # zonation marker are not a zonation chart for EITHER engine — and
        # the abundance bundle still says the rows were excluded
        data = {"abundances": [{"taxon": "A", "site": "S", "level": "1",
                                "abundance": 5}],
                "zones": [{"name": "z1", "base": "1", "top": "2"},
                          {"name": "z2", "base": "3", "top": "4"}]}
        out = to_wpd(data)
        assert out["mode"] == "abundance_diagram"
        assert any(w.startswith("mode_excludes_rows:zones(2)")
                   for w in out["warnings"])
        assert [c["id"] for c in get_configs_for_result(data)][0] == "sites"


class TestC2DiskCollision:
    def test_second_bundle_into_the_same_dir_does_not_overwrite(self,
                                                                tmp_path):
        # FIX-2026-09-22 (item 7): same plate name, two exports, one folder —
        # deterministic _2 suffixes and a warning, never a silent rewrite
        a = {"abundances": [{"taxon": "A", "site": "S", "level": "1",
                             "abundance": 5}]}
        b = {"abundances": [{"taxon": "A", "site": "S", "level": "2",
                             "abundance": 6}]}
        r1 = to_wpd(a, source_file="fig.png", output_dir=str(tmp_path))
        r2 = to_wpd(b, source_file="fig.png", output_dir=str(tmp_path))
        assert not any(w.startswith("wpd_avoided_overwrite")
                       for w in r1["warnings"])
        assert any(w.startswith("wpd_avoided_overwrite:wpd_fig__S__A.csv")
                   for w in r2["warnings"])
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "wpd_axes.json", "wpd_axes_2.json",
            "wpd_fig__S__A.csv", "wpd_fig__S__A_2.csv"]
        # the manifest describes the file ACTUALLY on disk
        assert "wpd_fig__S__A_2.csv" in r2["files"]
        assert r2["datasets"][0]["file"] == "wpd_fig__S__A_2.csv"


class TestC2ReadableFileNames:
    def test_any_scripts_letters_survive_the_slug_and_stay_harmless(self):
        # FIX-2026-09-22 (item 11): CJK plate names used to collapse to the
        # useless fallback token; now letters of ANY script survive while
        # every separator, control character and dot-run still collapses
        assert _wpd_slug("图版3") == "图版3"
        assert _wpd_slug("Разрез 1") == "Разрез_1"
        assert _wpd_slug("../../etc/passwd") == "etc_passwd"
        assert _wpd_slug("a\\b:c") == "a_b_c"
        assert _wpd_slug("x\u202ey") == "x_y"
        assert _wpd_slug("...") == "dataset"
        out = to_wpd({"abundances": [{"taxon": "孢粉", "site": "S",
                                      "level": "1", "abundance": 5}]},
                     source_file="图版3.jpg")
        assert out["plate"] == "图版3"
        assert "wpd_图版3__S__孢粉.csv" in out["files"]
