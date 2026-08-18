"""Domain-correctness regression tests for REVIEW-2026-07-31 fixes.

Covers:
  * range-literal bounds resolve to the OLDER end for FAD / the YOUNGER
    end for LAD (previously both resolved to the younger end, re-creating
    the C-1 earliest==latest collapse and PBDB zero-length intervals);
  * Darwin Core exports numeric fad_ma / lad_ma columns and does NOT
    collapse earliest/latest for series-level labels;
  * coordinate hemisphere is read from the matched letter group, not a
    whole-text scan;
  * PBDB min_ma/max_ma resolves stage/series-only section age_ranges via
    ICS (C-3 gap);
  * quality.py FAD<LAD does not flag valid bed labels that merely contain
    the letters "ma" ("Madison 3" / "Madison 6");
  * _BIOZONE_STAGE_MAP is species-level — Wuchiapingian Clarkina zones
    resolve to Wuchiapingian, not Changhsingian.
"""
from __future__ import annotations

import pytest


def _make_row(species="Neoalbaillella optima", section="Section A",
              range_base=None, range_top=None, biozone=""):
    return {
        "species": species,
        "section": section,
        "range_base": range_base or "",
        "range_top": range_top or "",
        "biozone": biozone,
    }


class TestRangeLiteralBounds:
    def test_range_literal_base_resolves_older_end(self):
        from rca_core.standards.ics import ics_resolve_age_bound
        name, ma = ics_resolve_age_bound("259.51-254.14 Ma", prefer="older")
        assert name == "Wuchiapingian"
        assert abs(ma - 259.51) < 1e-6

    def test_range_literal_top_resolves_younger_end(self):
        from rca_core.standards.ics import ics_resolve_age_bound
        name, ma = ics_resolve_age_bound("259.51-254.14 Ma", prefer="younger")
        assert name == "Changhsingian"
        assert abs(ma - 254.14) < 1e-6

    def test_reversed_notation_range_literal(self):
        """'254.14-259.51 Ma' (young-first) must still pick 259.51 as older."""
        from rca_core.standards.ics import ics_resolve_age_bound
        name, ma = ics_resolve_age_bound("254.14-259.51 Ma", prefer="older")
        assert abs(ma - 259.51) < 1e-6

    def test_dwc_no_collapse_with_range_literal(self):
        """The old bug: earliest==latest=='Changhsingian' and a PBDB
        zero-length interval. Now earliest=Wuchiapingian / latest=
        Changhsingian with fad_ma=259.51 / lad_ma=254.14."""
        from rca_core.standards.darwin_core import to_darwin_core_occurrences
        result = {
            "sections": [{"name": "Section A", "age_range": "Wuchiapingian - Changhsingian",
                          "formations": [], "coordinates": ""}],
            "species_ranges": [
                _make_row(range_base="259.51-254.14 Ma", range_top="254.14 Ma"),
            ],
        }
        occs = to_darwin_core_occurrences(result)
        assert len(occs) == 1
        occ = occs[0]
        assert occ["earliestAgeOrLowestStage"] == "Wuchiapingian"
        assert occ["latestAgeOrHighestStage"] == "Changhsingian"
        assert occ["fad_ma"] == "259.51"
        assert occ["lad_ma"] == "254.14"

    def test_pbdb_no_zero_length_interval_with_range_literal(self):
        from rca_core.standards.pbdb import to_pbdb_occurrences
        result = {
            "sections": [{"name": "Section A", "age_range": "Wuchiapingian - Changhsingian",
                          "formations": [], "coordinates": ""}],
            "species_ranges": [
                _make_row(range_base="259.51-254.14 Ma", range_top="254.14 Ma"),
            ],
        }
        occs = to_pbdb_occurrences(result)
        assert len(occs) == 1
        occ = occs[0]
        assert occ["early_interval"] == "Wuchiapingian"
        assert occ["late_interval"] == "Changhsingian"
        assert float(occ["max_ma"]) > float(occ["min_ma"])


class TestSeriesLabelResolution:
    def test_dwc_series_labels_do_not_collapse(self):
        """'Late Permian' / 'Early Triassic' must not collapse into one
        string (they now resolve to Lopingian / Lower Triassic)."""
        from rca_core.standards.darwin_core import to_darwin_core_occurrences
        result = {
            "sections": [{"name": "Section A", "age_range": "Late Permian - Early Triassic",
                          "formations": [], "coordinates": ""}],
            "species_ranges": [
                _make_row(range_base="Late Permian", range_top="Early Triassic"),
            ],
        }
        occs = to_darwin_core_occurrences(result)
        occ = occs[0]
        assert occ["earliestAgeOrLowestStage"] == "Lopingian"
        assert occ["latestAgeOrHighestStage"] == "Lower Triassic"
        assert occ["earliestAgeOrLowestStage"] != occ["latestAgeOrHighestStage"]

    def test_dwc_bed_labels_keep_raw_text_fallback(self):
        """Unresolvable bed labels fall back to the RAW bound labels
        (distinct), not the same biozone string on both sides."""
        from rca_core.standards.darwin_core import to_darwin_core_occurrences
        result = {
            "sections": [{"name": "Section A", "age_range": "Wuchiapingian",
                          "formations": [], "coordinates": ""}],
            "species_ranges": [
                _make_row(range_base="Bed 3", range_top="Bed 9",
                          biozone="N. optima Zone"),
            ],
        }
        occs = to_darwin_core_occurrences(result)
        occ = occs[0]
        assert occ["earliestAgeOrLowestStage"] == "Bed 3"
        assert occ["latestAgeOrHighestStage"] == "Bed 9"
        assert occ["fad_ma"] == "" and occ["lad_ma"] == ""

    def test_pbdb_section_stage_name_fallback(self):
        """C-3 gap: a section with a stage/series-only age_range now
        produces numeric max_ma/min_ma via ICS resolution."""
        from rca_core.standards.pbdb import to_pbdb_occurrences, to_pbdb_collections
        result = {
            "sections": [{"name": "Section A",
                          "age_range": "Late Permian (Wuchiapingian - Changhsingian)",
                          "formations": [], "coordinates": ""}],
            "species_ranges": [
                _make_row(range_base="Bed 3", range_top="Bed 9"),
            ],
        }
        occs = to_pbdb_occurrences(result)
        occ = occs[0]
        assert float(occ["max_ma"]) == pytest.approx(259.51, abs=0.01)
        assert float(occ["min_ma"]) == pytest.approx(251.9, abs=0.01)
        assert occ["early_interval"] == "Wuchiapingian"
        assert occ["late_interval"] == "Changhsingian"
        cols = to_pbdb_collections(result)
        assert float(cols[0]["max_ma"]) == pytest.approx(259.51, abs=0.01)
        assert float(cols[0]["min_ma"]) == pytest.approx(251.9, abs=0.01)
        assert cols[0]["early_interval"] == "Wuchiapingian"
        assert cols[0]["late_interval"] == "Changhsingian"

    def test_pbdb_epoch_label_resolution(self):
        """'Pleistocene' section age_range resolves to 2.588-0.0117."""
        from rca_core.standards.pbdb import to_pbdb_collections
        result = {
            "sections": [{"name": "Section A", "age_range": "Pleistocene",
                          "formations": [], "coordinates": ""}],
            "species_ranges": [],
        }
        cols = to_pbdb_collections(result)
        assert float(cols[0]["max_ma"]) == pytest.approx(2.588, abs=0.01)
        assert float(cols[0]["min_ma"]) == pytest.approx(0.0117, abs=0.01)


class TestCoordinateHemisphere:
    @pytest.mark.parametrize("text,expected", [
        ("31N, 117E", (31.0, 117.0)),
        ("31S, 117W", (-31.0, -117.0)),
        ("31.5 S 117.5 W", (-31.5, -117.5)),
        # Words containing s/w must NOT flip the hemisphere.
        ("31N, 117E (south bank)", (31.0, 117.0)),
        ("31N, 117E west of village", (31.0, 117.0)),
        ("31N, 117E (south-west exposure)", (31.0, 117.0)),
        ("S31N, 117E", (31.0, 117.0)),  # permissive: matches "31N, 117E"
    ])
    def test_darwin_core_coords(self, text, expected):
        from rca_core.standards.darwin_core import _parse_coordinates
        assert _parse_coordinates(text) == expected

    def test_pbdb_coords(self):
        from rca_core.standards.pbdb import _parse_coords
        assert _parse_coords("31N, 117E (south bank)") == (31.0, 117.0)
        assert _parse_coords("31.5 S 117.5 W") == (-31.5, -117.5)


class TestQualityFadLadFalsePositive:
    def _quality(self, result):
        from rca_core.quality import score_range_chart
        out = score_range_chart(result)
        return out["score"], out["issues"]

    def test_bed_labels_with_ma_substring_not_ages(self):
        """'Madison 3' / 'Madison 6' is a valid bed pair (top > base) —
        previously the 'ma' substring routed it to the age branch and
        flagged an FAD<LAD violation."""
        result = {
            "sections": [{"name": "A", "age_range": "Permian"}],
            "species_ranges": [
                _make_row(range_base="Madison 3", range_top="Madison 6"),
            ],
            "confidence": 0.9,
        }
        score, issues = self._quality(result)
        assert not any(i.get("msg_key") == "quality.range_top_lt_base"
                       for i in issues), issues

    def test_numeric_ma_range_valid(self):
        result = {
            "sections": [{"name": "A", "age_range": "Permian"}],
            "species_ranges": [
                _make_row(range_base="260 Ma", range_top="250 Ma"),
            ],
            "confidence": 0.9,
        }
        score, issues = self._quality(result)
        assert not any(i.get("msg_key") == "quality.range_top_lt_base"
                       for i in issues), issues

    def test_inverted_numeric_ma_range_flagged(self):
        result = {
            "sections": [{"name": "A", "age_range": "Permian"}],
            "species_ranges": [
                _make_row(range_base="250 Ma", range_top="260 Ma"),
            ],
            "confidence": 0.9,
        }
        score, issues = self._quality(result)
        assert any(i.get("msg_key") == "quality.range_top_lt_base"
                   for i in issues), issues

    def test_range_literal_inverted_pair_flagged(self):
        """'259.51-254.14 Ma' as base with top '254.14 Ma' is VALID; a
        genuine inversion (base younger than top) is still caught."""
        result = {
            "sections": [{"name": "A", "age_range": "Permian"}],
            "species_ranges": [
                _make_row(range_base="259.51-254.14 Ma", range_top="254.14 Ma"),
            ],
            "confidence": 0.9,
        }
        score, issues = self._quality(result)
        assert not any(i.get("msg_key") == "quality.range_top_lt_base"
                       for i in issues), issues


class TestBiozoneStageMap:
    def _resolve(self, label):
        from rca_core.quality import _resolve_biozone_stage
        return _resolve_biozone_stage(label)

    def test_wuchiapingian_clarkina_zones(self):
        assert self._resolve("Clarkina orientalis Zone") == "Wuchiapingian"
        assert self._resolve("Clarkina leveni Zone") == "Wuchiapingian"
        assert self._resolve("Clarkina subcarinata Zone") == "Wuchiapingian"

    def test_changhsingian_clarkina_zones(self):
        assert self._resolve("Clarkina changxingensis Zone") == "Changhsingian"
        assert self._resolve("Clarkina yini Zone") == "Changhsingian"
        assert self._resolve("Clarkina meishanensis Zone") == "Changhsingian"

    def test_unknown_biozone_still_skipped(self):
        assert self._resolve("Neogondolella postserrata Zone") is None
        assert self._resolve("Clarkina sp.") is None
