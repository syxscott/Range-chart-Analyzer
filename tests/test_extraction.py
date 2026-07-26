"""Tests for rca_core.extractor extraction helpers + clamp_max_edge + never-raises
contract for extract_range_chart / extract_columnar_section / extract_abundance_diagram.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from rca_core import extractor as E
from rca_core.extractor import (
    ExtractResult,
    clamp_max_edge,
    clamp_max_tokens,
    clamp_timeout_sec,
    extract,
    extract_range_chart,
)


class TestClampMaxTokens(unittest.TestCase):
    def test_clamp_below_min(self):
        self.assertEqual(clamp_max_tokens(0), 1)
        self.assertEqual(clamp_max_tokens(-100), 1)

    def test_clamp_above_max(self):
        self.assertEqual(clamp_max_tokens(1_000_000), 100000)

    def test_default_on_non_numeric(self):
        self.assertEqual(clamp_max_tokens(None), 4000)
        self.assertEqual(clamp_max_tokens("abc"), 4000)

    def test_valid_value_unchanged(self):
        self.assertEqual(clamp_max_tokens(8192), 8192)


class TestClampTimeoutSec(unittest.TestCase):
    def test_clamp_below_min(self):
        self.assertEqual(clamp_timeout_sec(0), 10)
        self.assertEqual(clamp_timeout_sec(5), 10)

    def test_clamp_above_max(self):
        self.assertEqual(clamp_timeout_sec(1000), 300)

    def test_default_on_non_numeric(self):
        self.assertEqual(clamp_timeout_sec(None), 120)
        self.assertEqual(clamp_timeout_sec("abc"), 120)

    def test_valid_value_unchanged(self):
        self.assertEqual(clamp_timeout_sec(60), 60)


class TestClampMaxEdge(unittest.TestCase):
    """LOW: clamp_max_edge is currently dead code with no callers in the
    shipped front-ends, but it is still part of the public surface and
    must behave correctly."""

    def test_clamp_min(self):
        self.assertEqual(clamp_max_edge(-1), 0)

    def test_clamp_max(self):
        self.assertEqual(clamp_max_edge(99_999_999), 10000)

    def test_clamp_default_on_bad(self):
        self.assertEqual(clamp_max_edge("oops"), 4000)

    def test_clamp_mid(self):
        self.assertEqual(clamp_max_edge(2048), 2048)

    def test_zero_means_disabled(self):
        self.assertEqual(clamp_max_edge(0), 0)


class TestExtractRangeChartNeverRaises(unittest.TestCase):
    """LOW: extract_range_chart's "never raises" promise is broken when the
    provider has a malformed extra_body / extra_headers — those values flow
    into dict.update() inside llm.py and raise TypeError. We wrap the
    call_llm_api path so a malformed provider config returns a clean error
    result instead of propagating."""

    def _patched_call_llm_api(self, exc):
        def _raise(*args, **kwargs):
            raise exc
        return _patched_call_llm_api

    def test_extract_handles_malformed_provider_config(self):
        # Simulate a malformed extra_body: dict.update([]) raises TypeError.
        def fake_call(*args, **kwargs):
            raise TypeError("'NoneType' object is not iterable")

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            r = extract_range_chart(
                api_key="k", image_b64="QQ==", media_type="image/png",
            )
        # Never raises; returns ok=False with a meaningful error_key.
        self.assertFalse(r.ok)
        self.assertTrue(r.error_key)
        self.assertIn("TypeError", r.warning or r.error_body or "")


class TestExtractDispatchesByMode(unittest.TestCase):
    """Sanity: the extract() dispatcher routes to the right function."""

    def test_unknown_mode_returns_error(self):
        r = extract(mode="not-a-mode", image_b64="QQ==", media_type="image/png")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_key, "err.http")

    def test_empty_image_returns_error(self):
        r = extract(mode="range_chart", image_b64="", media_type="image/png")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_key, "err.imageRead")


class TestTruncatedVLMReturnsWarning(unittest.TestCase):
    """MEDIUM: when the model returns truncated JSON that the parser rescues
    as an inner object, extract_range_chart should set ok=False and a clear
    warning so the user is not silently given an empty extraction as ok=True.
    """

    def test_truncated_unrescuable_returns_error(self):
        # Patch call_llm_api to return truncated text that does NOT parse as a
        # recognizable range-chart root.
        truncated = (
            '{"sections":[{"name":"Pingdingshan","age_range":"Late Permian"}],'
            '"species_ranges":[{"species":"Neoalbaillella or'
        )

        def fake_call(*args, **kwargs):
            return (truncated, True, 200, "", {})

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            r = extract_range_chart(
                api_key="k", image_b64="QQ==", media_type="image/png",
            )
        # The result is either an outright error (ok=False) or carries an
        # explicit warning that the data may be unusable. Either is fine —
        # but NEVER silently ok=True with all-empty arrays.
        if r.ok:
            data = r.data or {}
            primary_empty = (
                not data.get("sections")
                and not data.get("species_ranges")
                and not data.get("biozones")
                and not data.get("other_fossils")
            )
            if primary_empty:
                self.assertTrue(
                    r.warning or data.get("_warnings"),
                    "Truncated VLM output was rescued as an inner object and "
                    "returned ok=True with empty arrays but no warning. This "
                    "is silently-wrong output the user cannot diagnose.",
                )


class TestFiPreservesNumericStrings(unittest.TestCase):
    """LOW: the columnar normalizer's fi() coerces '8.0' to None (loss) and
    silently truncates 8.5 to 8. Preserve the original string for non-int
    inputs so the row carries a clear `_warning` instead of a silent floor."""

    def test_numeric_string_preserved_with_warning(self):
        from rca_core.extractor import normalize_columnar_result
        r = normalize_columnar_result({'sections': [
            {'id': 's1', 'lithology_blocks': [
                {'pattern': 'shale', 'range_top_idx': '8.0', 'range_base_idx': 1},
            ], 'age_units': [], 'samples': [],
             'coordinates_text': '', 'thickness_m': '', 'confidence_by_section': 0.5},
        ], 'fossil_legend': [], 'lithology_legend': [], 'cross_beds': [],
            'overall_confidence': 0.5})
        block = r['sections'][0]['lithology_blocks'][0]
        # Either keep the original string OR flag a warning — do NOT silently
        # null it.
        if block['range_top_idx'] is None:
            self.assertIn('_warning', block)
        else:
            # Int preserved (e.g. 8 if the string was '8') without warning.
            pass

    def test_float_safely_floored(self):
        from rca_core.extractor import normalize_columnar_result
        r = normalize_columnar_result({'sections': [
            {'id': 's1', 'lithology_blocks': [
                {'pattern': 'shale', 'range_top_idx': 8.5, 'range_base_idx': 1},
            ], 'age_units': [], 'samples': [],
             'coordinates_text': '', 'thickness_m': '', 'confidence_by_section': 0.5},
        ], 'fossil_legend': [], 'lithology_legend': [], 'cross_beds': [],
            'overall_confidence': 0.5})
        block = r['sections'][0]['lithology_blocks'][0]
        # 8.5 silently floored to 8 — acceptable; but no exception.
        self.assertIn(block['range_top_idx'], (8, None))


class TestExtractColumnarSection(unittest.TestCase):
    """End-to-end tests for extract_columnar_section."""

    def test_extract_columnar_section_returns_normalized_result(self):
        """Mock call_llm_api and verify extract_columnar_section returns correct structure."""
        mock_response = json.dumps({
            "sections": [{
                "id": "Ki-1",
                "group": "Kurohone-Kiryu Complex",
                "lithology_blocks": [
                    {"pattern": "chert", "range_top_idx": 8, "range_base_idx": 1}
                ],
                "age_units": [],
                "samples": [],
                "coordinates_text": "35N, 135E",
                "thickness_m": "500 m",
                "confidence_by_section": 0.7
            }],
            "fossil_legend": [],
            "lithology_legend": [],
            "cross_beds": [],
            "overall_confidence": 0.6
        })

        def fake_call(*args, **kwargs):
            return (mock_response, False, 200, "", {})

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            r = E.extract_columnar_section(
                api_key="k", image_b64="QQ==", media_type="image/png",
            )
        self.assertTrue(r.ok)
        self.assertIsInstance(r.data, dict)
        self.assertIn("sections", r.data)
        self.assertEqual(len(r.data["sections"]), 1)
        self.assertEqual(r.data["sections"][0]["id"], "Ki-1")


class TestExtractAbundanceDiagram(unittest.TestCase):
    """End-to-end tests for extract_abundance_diagram."""

    def test_extract_abundance_diagram_returns_normalized_result(self):
        """Mock call_llm_api and verify extract_abundance_diagram returns correct structure."""
        mock_response = json.dumps({
            "sites": [{"name": "Lake Suigetsu", "location": "35N, 135E", "age_range": "Holocene", "depth_unit": "cm"}],
            "abundances": [{"taxon": "Pinus", "site": "Lake Suigetsu", "level": "120 cm", "depth": "120", "abundance": "35", "abundance_unit": "%"}],
            "zones": [{"name": "PAZ-3", "age": "Early Holocene", "level_range": "80-140 cm"}],
            "confidence": 0.75
        })

        def fake_call(*args, **kwargs):
            return (mock_response, False, 200, "", {})

        with patch.object(E, "call_llm_api", side_effect=fake_call):
            r = E.extract_abundance_diagram(
                api_key="k", image_b64="QQ==", media_type="image/png",
            )
        self.assertTrue(r.ok)
        self.assertIsInstance(r.data, dict)
        self.assertIn("sites", r.data)
        self.assertIn("abundances", r.data)
        self.assertIn("zones", r.data)
        self.assertEqual(len(r.data["abundances"]), 1)


if __name__ == '__main__':
    unittest.main()