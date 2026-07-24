"""Tests for rca_core.quality.score_range_chart."""

from __future__ import annotations

import unittest

from rca_core.quality import score_range_chart


def _good_result():
    return {
        "sections": [
            {"name": "Pingdingshan", "age_range": "Late Permian",
             "formations": ["Talung Fm"], "formation_thickness_m": "~9m",
             "coordinates": "31N, 117E"},
        ],
        "species_ranges": [
            {"species": "Neoalbaillella optima", "section": "Pingdingshan",
             "range_top": "Bed 9", "range_base": "Bed 7",
             "biozone": "N. optima Zone", "confidence": 0.9},
        ],
        "biozones": [
            {"name": "N. optima Zone", "section": "Pingdingshan",
             "age": "Latest Changhsingian", "thickness_m": "~3m"},
        ],
        "other_fossils": ["Ammonite sp."],
        "confidence": 0.88,
    }


class TestScoreRangeChart(unittest.TestCase):
    def test_good_result_high_score(self):
        result = score_range_chart(_good_result())
        self.assertGreaterEqual(result["score"], 0.75)
        self.assertIn(result["grade"], ("A", "B"))
        self.assertIsInstance(result["issues"], list)

    def test_empty_result_low_score(self):
        result = score_range_chart({})
        self.assertLess(result["score"], 0.4)
        self.assertEqual(result["grade"], "F")

    def test_none_result(self):
        result = score_range_chart(None)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["grade"], "F")

    def test_missing_primary_rows(self):
        data = _good_result()
        data["species_ranges"] = []
        result = score_range_chart(data)
        # Should flag empty primary rows.
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertIn("quality.empty_primary_rows", keys)

    def test_unmatched_section_ref(self):
        data = _good_result()
        data["species_ranges"][0]["section"] = "NonExistentSection"
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertTrue(
            "quality.unmatched_section_ref" in keys
            or "quality.all_section_refs_unmatched" in keys,
            f"expected section ref issue, got {keys}")

    def test_many_extras(self):
        data = _good_result()
        data["_extras"] = {f"extra_{i}": i for i in range(10)}
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertIn("quality.many_extras", keys)

    def test_agreement_exceeds_runs(self):
        data = _good_result()
        data["species_ranges"][0]["agreement_count"] = 99
        data["runs"] = 3
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertIn("quality.agreement_exceeds_runs", keys)

    def test_score_bounded(self):
        result = score_range_chart(_good_result())
        self.assertGreaterEqual(result["score"], 0.0)
        self.assertLessEqual(result["score"], 1.0)

    def test_grade_thresholds(self):
        # A minimal-but-valid result should score reasonably (B range),
        # not F — F is reserved for empty/invalid results.
        result = score_range_chart({"confidence": 1.0, "species_ranges": [
            {"section": "S1"}], "sections": [{"name": "S1"}]})
        self.assertIn(result["grade"], ("A", "B", "C"))

    # ------------------------------------------------------------------
    # Regression tests for code-review audit 2026-07-19 / 2026-07-20
    # ------------------------------------------------------------------

    # HIGH: empty-but-formal results (all top-level arrays present but
    # empty) previously scored 0.94 / A. Must now score very low (F).

    def test_all_empty_arrays_present_grade_F(self):
        """Schema-present-but-no-data must NOT grade A."""
        data = {
            "species_ranges": [],
            "sections": [],
            "biozones": [],
            "other_fossils": [],
            "confidence": 0.9,
        }
        result = score_range_chart(data)
        self.assertLess(result["score"], 0.4,
                        f"all-empty result must score < 0.4, got {result['score']}")
        self.assertEqual(result["grade"], "F",
                         f"all-empty result must be F, got {result['grade']}")
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertTrue(
            "quality.empty_result" in keys
            or "quality.empty_primary_rows" in keys,
            f"expected empty/empty_primary issue, got {keys}")

    def test_pure_extraction_miss_only_species_empty(self):
        """When species_ranges is empty but other fields are absent too, F."""
        data = {"species_ranges": [], "confidence": 0.5}
        result = score_range_chart(data)
        self.assertEqual(result["grade"], "F")
        self.assertLess(result["score"], 0.4)

    def test_abundances_empty_only(self):
        """When abundances is present but empty (abundance diagram miss), F."""
        data = {"abundances": [], "sections": [], "confidence": 0.5}
        result = score_range_chart(data)
        self.assertEqual(result["grade"], "F")

    def test_nonempty_with_one_mode_present_passes(self):
        """Sanity: when one mode has actual data, no grade F."""
        data = {
            "species_ranges": [{"species": "X", "section": "S1"}],
            "sections": [{"name": "S1"}],
            "confidence": 0.5,
        }
        result = score_range_chart(data)
        self.assertNotEqual(result["grade"], "F",
                            "non-empty data must not be F")

    # HIGH: FAD<LAD / bed-order checks now implemented.

    def test_range_top_lt_base_emits_warning(self):
        """species with range_top older than range_base is implausible."""
        data = _good_result()
        # Good test data has range_top='Bed 9', range_base='Bed 7' (correct).
        # Invert them to simulate an extraction error.
        data["species_ranges"][0]["range_top"] = "Bed 5"
        data["species_ranges"][0]["range_base"] = "Bed 9"
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertIn("quality.range_top_lt_base", keys,
                      f"expected range_top_lt_base warning, got {keys}")

    def test_range_top_eq_base_ok(self):
        """range_top == range_base is valid (single-bed occurrence)."""
        data = _good_result()
        data["species_ranges"][0]["range_top"] = "Bed 7"
        data["species_ranges"][0]["range_base"] = "Bed 7"
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertNotIn("quality.range_top_lt_base", keys,
                         f"equal top=base should be ok, got {keys}")

    def test_columnar_bed_order_inverted_emits_warning(self):
        """Columnar beds with range_top_idx < range_base_idx -> warning."""
        data = {
            "sections": [{"id": "A", "lithology_blocks": [
                {"pattern": "ss", "range_top_idx": 5, "range_base_idx": 9}
            ]}],
            "cross_beds": [],
            "confidence": 0.85,
        }
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertIn("quality.bed_index_order_invalid", keys,
                      f"expected bed-order invalid warning, got {keys}")

    def test_columnar_bed_order_valid_no_warning(self):
        """Columnar beds with valid top >= base should not warn."""
        data = {
            "sections": [{"id": "A", "lithology_blocks": [
                {"pattern": "ss", "range_top_idx": 9, "range_base_idx": 5}
            ]}],
            "cross_beds": [],
            "confidence": 0.85,
        }
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertNotIn("quality.bed_index_order_invalid", keys,
                         f"valid order must not warn, got {keys}")

    # MEDIUM: columnar-mode results must not be falsely penalised.

    def test_columnar_mode_not_penalised_for_rangechart_fields(self):
        """Columnar result (has cross_beds) must not lose points for
        missing species_ranges / biozones / other_fossils — those are
        irrelevant in columnar mode and are correctly absent."""
        data = {
            "sections": [{"id": "A", "lithology_blocks": [
                {"pattern": "ss", "range_top_idx": 5, "range_base_idx": 1}
            ]}],
            "cross_beds": [
                {"from_section": "A", "from_bed_idx": 1,
                 "to_section": "A", "to_bed_idx": 3}
            ],
            "confidence": 0.85,
        }
        result = score_range_chart(data)
        keys = [i["msg_key"] for i in result["issues"]]
        self.assertNotIn("quality.missing_top_level", keys,
                         f"columnar mode falsely penalised: {keys}")
        self.assertIn(result["grade"], ("A", "B", "C"),
                      f"valid columnar mode scored {result['grade']}")


if __name__ == '__main__':
    unittest.main()
