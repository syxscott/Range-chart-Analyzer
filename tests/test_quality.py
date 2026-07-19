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


if __name__ == '__main__':
    unittest.main()
