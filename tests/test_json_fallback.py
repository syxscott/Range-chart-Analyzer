"""Tests for the 6-level JSON parse fallback chain (json_utils.py / json-utils.js).

Verifies that the enhanced fallback chain (strip_markdown_fence,
extract_json_like, response_format) handles real-world VLM output formats.
"""

from __future__ import annotations

import json
import unittest

from rca_core.json_utils import (
    extract_all_balanced_json_objects,
    extract_json_like,
    safe_json_loads,
    strip_markdown_fence,
)


class TestStripMarkdownFence(unittest.TestCase):
    def test_clean_plain(self):
        s = '{"a": 1}'
        self.assertEqual(strip_markdown_fence(s), '{"a": 1}')

    def test_fence_with_lang(self):
        s = '```json\n{"a": 1}\n```'
        self.assertEqual(strip_markdown_fence(s), '{"a": 1}')

    def test_bare_fence(self):
        s = '```\n{"a": 1}\n```'
        self.assertEqual(strip_markdown_fence(s), '{"a": 1}')

    def test_fence_with_prose(self):
        s = 'Here is result:\n```json\n{"a": 1}\n```\nDone.'
        # Not a single block — defensive line-level strip.
        result = strip_markdown_fence(s)
        self.assertIn('{"a": 1}', result)

    def test_empty(self):
        self.assertEqual(strip_markdown_fence(''), '')
        self.assertEqual(strip_markdown_fence(None), None)


class TestExtractJsonLike(unittest.TestCase):
    def test_prose_wrapped(self):
        text = 'Here is the data:\n{"species": "Trex"}\nLet me know.'
        result = extract_json_like(text)
        self.assertIsNotNone(result)
        self.assertEqual(json.loads(result), {"species": "Trex"})

    def test_array_in_prose_returns_first_balanced(self):
        # Prose containing an array of objects: extract_json_like prefers
        # the first balanced substring. With '{' opener tried before '[', it
        # returns the first object — a safe partial fallback (still valid
        # JSON) rather than nothing. The full array is recovered earlier by
        # Level 5 (extract_balanced_json_array) when there's no leading '{'.
        text = 'The zones are:\n[{"name": "A"}, {"name": "B"}]\nDone.'
        result = extract_json_like(text)
        self.assertIsNotNone(result)
        # Must be valid JSON.
        parsed = json.loads(result)
        self.assertIsInstance(parsed, (dict, list))

    def test_no_json(self):
        text = "I'm sorry, I cannot help."
        self.assertIsNone(extract_json_like(text))

    def test_empty(self):
        self.assertIsNone(extract_json_like(''))


class TestSafeJsonLoads(unittest.TestCase):
    def test_clean_object(self):
        self.assertEqual(safe_json_loads('{"a": 1}'), {"a": 1})

    def test_fence_wrapped(self):
        s = '```json\n{"a": 1}\n```'
        self.assertEqual(safe_json_loads(s), {"a": 1})

    def test_prose_wrapped(self):
        s = 'Here is result:\n{"a": 1}\nDone.'
        self.assertEqual(safe_json_loads(s), {"a": 1})

    def test_top_level_array_wrapped(self):
        s = '[{"a": 1}, {"a": 2}]'
        result = safe_json_loads(s)
        self.assertIn("_array_root", result)
        self.assertEqual(result["_array_root"], [{"a": 1}, {"a": 2}])

    def test_control_chars_stripped(self):
        s = '{"a": "hel\x00lo"}'
        self.assertEqual(safe_json_loads(s), {"a": "hello"})

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            safe_json_loads('')

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            safe_json_loads('I cannot help.')

    def test_truncated_raises(self):
        with self.assertRaises(ValueError):
            safe_json_loads('{"a": 1')


class TestExtractAllBalancedJsonObjects(unittest.TestCase):
    """F-1 HIGH: helper used by safe_json_loads scoring fallback."""

    def test_returns_all_balanced_substrings(self):
        text = (
            'Schema example: {"format": "demo", "version": 1}. '
            'Actual data: {"species": "T. rex", "confidence": 0.9}'
        )
        results = extract_all_balanced_json_objects(text)
        self.assertEqual(len(results), 2)
        self.assertEqual(json.loads(results[0]), {"format": "demo", "version": 1})
        self.assertEqual(json.loads(results[1]),
                         {"species": "T. rex", "confidence": 0.9})

    def test_single_object(self):
        results = extract_all_balanced_json_objects('Here: {"a": 1}')
        self.assertEqual(results, ['{"a": 1}'])

    def test_no_objects(self):
        self.assertEqual(extract_all_balanced_json_objects('no json'), [])

    def test_empty(self):
        self.assertEqual(extract_all_balanced_json_objects(''), [])


class TestSafeJsonLoadsFirstBalScoring(unittest.TestCase):
    """F-1 HIGH: safe_json_loads must NOT return the FIRST balanced object when
    a later (larger / key-rich) object is the real payload."""

    def test_prefers_payload_over_leading_schema_example(self):
        # Model restates schema in prose, then returns the actual payload
        # later. Old behaviour: returns the schema (wrong). New behaviour:
        # the payload is bigger AND has a payload-style key (species_ranges,
        # biozones, other_fossils, sections, confidence).
        text = (
            'Here is the schema I followed: '
            '{"format": "demo", "required": ["species"], "version": 1}. '
            'Now the data:\n'
            '{"species_ranges": [{"species": "T. rex", "section": "Hell Creek"}], '
            '"confidence": 0.92}'
        )
        result = safe_json_loads(text)
        # The scorer must pick the payload, not the schema example.
        self.assertIn('species_ranges', result,
                      f"expected payload key, got schema-ish: {result}")
        self.assertEqual(result['confidence'], 0.92)

    def test_prefers_largest_when_keys_overlap(self):
        # Two plausible objects; scorer should prefer the larger one.
        text = (
            'small: {"a": 1, "b": 2} '
            'large: {"sections": [{"name": "S1", "age_range": "M", '
            '"formations": ["F1"], "formation_thickness_m": "10m", '
            '"coordinates": "31N"}], "species_ranges": [], '
            '"biozones": [], "other_fossils": [], "confidence": 0.8}'
        )
        result = safe_json_loads(text)
        self.assertIn('sections', result)
        self.assertNotIn('a', result)

    def test_existing_first_wins_for_legacy_short_inputs(self):
        # Single object: still works (no regression).
        text = 'just one: {"a": 1}'
        result = safe_json_loads(text)
        self.assertEqual(result, {"a": 1})


class TestSafeJsonLoadsNaNInfinityParity(unittest.TestCase):
    """M-2 MEDIUM: Python json.loads accepts NaN/Infinity (non-standard JSON);
    JS JSON.parse rejects them. After the fix, both sides must reject (or
    both accept). We pin the contract to STRICT REJECT to match browser
    behaviour and prevent downstream export weirdness."""

    def test_nan_in_number_field_rejected(self):
        bad = '{"species": "T. rex", "confidence": NaN}'
        with self.assertRaises((ValueError, Exception)):
            safe_json_loads(bad)

    def test_infinity_in_number_field_rejected(self):
        bad = '{"species": "T. rex", "confidence": Infinity}'
        with self.assertRaises((ValueError, Exception)):
            safe_json_loads(bad)

    def test_negative_infinity_rejected(self):
        bad = '{"species": "T. rex", "confidence": -Infinity}'
        with self.assertRaises((ValueError, Exception)):
            safe_json_loads(bad)

    def test_object_without_nan_still_parses(self):
        # Pure numeric value should still parse cleanly.
        ok = '{"species": "T. rex", "confidence": 0.5}'
        self.assertEqual(safe_json_loads(ok),
                         {"species": "T. rex", "confidence": 0.5})


class TestFixtureFile(unittest.TestCase):
    """Validate every case in tests/fixtures/json_parse.json."""

    def test_all_fixtures(self):
        import os
        path = os.path.join(os.path.dirname(__file__), 'fixtures', 'json_parse.json')
        with open(path, encoding='utf-8') as f:
            fixtures = json.load(f)
        for case in fixtures['cases']:
            name = case['name']
            if case['should_parse']:
                result = safe_json_loads(case['input'])
                self.assertIsInstance(result, dict,
                                      f"{name}: expected dict, got {type(result)}")
            else:
                with self.assertRaises(ValueError, msg=name):
                    safe_json_loads(case['input'])


if __name__ == '__main__':
    unittest.main()
