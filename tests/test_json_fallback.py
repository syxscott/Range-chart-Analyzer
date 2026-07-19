"""Tests for the 6-level JSON parse fallback chain (json_utils.py / json-utils.js).

Verifies that the enhanced fallback chain (strip_markdown_fence,
extract_json_like, response_format) handles real-world VLM output formats.
"""

from __future__ import annotations

import json
import unittest

from rca_core.json_utils import (
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
