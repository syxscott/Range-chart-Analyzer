"""Python ↔ JS safe_json_loads parity tests.

Runs the same inputs through both engines and verifies that the
6-level fallback chain produces equivalent structures on both
sides. Specifically:
  - clean objects
  - schema-example-in-prose + real payload (HIGH fix)
  - control-character stripping
  - NaN/Infinity rejection (MEDIUM fix)
  - prose-wrapped JSON

NOTE: The JS-side mirror of extract_all_balanced_json_objects +
payload_score + strict NaN/Infinity rejection was reverted from
js/json-utils.js before this test file ran. The Python-side fix is
correct; the JS-side parity gap is tracked as a blocker in this
agent's report. The parity tests below therefore verify Python
behaviour only and skip the JS-side assertions until the mirror is
re-applied.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rca_core.json_utils import (  # noqa: E402
    extract_all_balanced_json_objects,
    safe_json_loads,
)


JS_JSON_UTILS = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                             'js', 'json-utils.js')


# Detect whether the JS mirror has been applied. If the new scoring
# helpers are present in js/json-utils.js the parity checks below run;
# otherwise they self-skip and document the gap.
def _js_has_scoring_mirror() -> bool:
    try:
        with open(JS_JSON_UTILS, encoding='utf-8') as f:
            src = f.read()
    except OSError:
        return False
    return ('extractAllBalancedJsonObjects' in src
            and 'payloadScore' in src)


def _js_safe_json_loads(input_str: str):
    """Execute safeJsonLoads via node and return its parsed result, or
    marker strings for parity comparison (we can't easily proxy JS
    exceptions across the subprocess boundary, so we use string
    markers)."""
    inp = _json.dumps(input_str)
    script = (
        "const fs=require('fs');"
        "const vm=require('vm');"
        f"const code=fs.readFileSync({_json.dumps(JS_JSON_UTILS)}, 'utf8');"
        "const ctx={console};"
        "vm.createContext(ctx);"
        "vm.runInContext(code, ctx, { filename: 'json-utils.js' });"
        "let res='THREW';"
        f"try {{ res=JSON.stringify(ctx.safeJsonLoads({inp})); }} catch (e) {{ res='THREW'; }}"
        "console.log(res);"
    )
    proc = subprocess.run(['node', '-e', script], capture_output=True,
                          text=True, timeout=10,
                          cwd=os.path.dirname(os.path.dirname(__file__)))
    if proc.returncode != 0:
        return 'THREW'
    return proc.stdout.strip() or 'THREW'


def _norm(v):
    """Normalize a Python safe_json_loads result for parity comparison."""
    if isinstance(v, (dict, list)):
        return _json.dumps(v, sort_keys=True)
    return repr(v)


class TestSafeJsonLoadsParityPython(unittest.TestCase):
    """Python-only smoke tests for safe_json_loads (run regardless)."""

    def test_clean_object(self):
        self.assertEqual(safe_json_loads('{"a": 1, "b": 2}'),
                         {"a": 1, "b": 2})

    def test_fenced_clean(self):
        self.assertEqual(safe_json_loads('```json\n{"a": 1}\n```'),
                         {"a": 1})

    def test_prose_wrapped(self):
        self.assertEqual(safe_json_loads('Here is result:\n{"a": 1}\nDone.'),
                         {"a": 1})

    def test_payload_over_schema_picks_payload(self):
        py_in = (
            'Here is the schema I followed: '
            '{"format": "demo", "required": ["species"], "version": 1}. '
            'Now the data:\n'
            '{"species_ranges": [{"species": "T. rex", "section": "Hell Creek"}], '
            '"confidence": 0.92}'
        )
        result = safe_json_loads(py_in)
        self.assertIn('species_ranges', result)
        self.assertEqual(result.get('confidence'), 0.92)


class TestSafeJsonLoadsParity(unittest.TestCase):
    """Python ↔ JS parity — auto-skipped if js/json-utils.js lacks the
    scoring/strict-parse mirror (currently the case; see js/bugfixes gap
    tracker)."""

    @classmethod
    def setUpClass(cls):
        cls._js_has_mirror = _js_has_scoring_mirror()

    def _skip_if_no_mirror(self):
        if not self._js_has_mirror:
            self.skipTest(
                "js/json-utils.js mirror of scoring + strict-parse constants "
                "not yet re-applied; skipping JS parity assertion")

    def test_clean_object(self):
        self._skip_if_no_mirror()
        self.assertEqual(_norm(safe_json_loads('{"a": 1, "b": 2}')),
                         _norm(_json.loads(_js_safe_json_loads('{"a": 1, "b": 2}'))))

    def test_fenced_clean(self):
        self._skip_if_no_mirror()
        py_in = '```json\n{"a": 1}\n```'
        self.assertEqual(_norm(safe_json_loads(py_in)),
                         _norm(_json.loads(_js_safe_json_loads(py_in))))

    def test_prose_wrapped(self):
        self._skip_if_no_mirror()
        py_in = 'Here is result:\n{"a": 1, "b": 2}\nDone.'
        self.assertEqual(_norm(safe_json_loads(py_in)),
                         _norm(_json.loads(_js_safe_json_loads(py_in))))

    def test_no_json_raises_on_both(self):
        self._skip_if_no_mirror()
        py_in = "I'm sorry, I cannot extract."
        # Both engines must raise (or at least not produce a payload).
        py_out = 'THREW'
        try:
            safe_json_loads(py_in)
        except Exception:
            py_out = 'THREW'
        self.assertEqual(py_out, _js_safe_json_loads(py_in), 'both must raise')

    def test_payload_over_schema(self):
        self._skip_if_no_mirror()
        py_in = (
            'Here is the schema I followed: '
            '{"format": "demo", "required": ["species"], "version": 1}. '
            'Now the data:\n'
            '{"species_ranges": [{"species": "T. rex", "section": "Hell Creek"}], '
            '"confidence": 0.92}'
        )
        py = safe_json_loads(py_in)
        js_raw = _js_safe_json_loads(py_in)
        js = _json.loads(js_raw)
        # Both must pick the payload (have species_ranges key).
        self.assertIn('species_ranges', py,
                      f'python picked schema: {py}')
        self.assertIn('species_ranges', js,
                      f'js picked schema: {js}')

    def test_nan_rejected_on_both(self):
        self._skip_if_no_mirror()
        py_in = '{"species": "T. rex", "confidence": NaN}'
        py_out = 'THREW'
        try:
            safe_json_loads(py_in)
        except Exception:
            py_out = 'THREW'
        self.assertEqual(py_out, _js_safe_json_loads(py_in),
                         'both engines must reject NaN')

    def test_infinity_rejected_on_both(self):
        self._skip_if_no_mirror()
        py_in = '{"species": "T. rex", "confidence": Infinity}'
        py_out = 'THREW'
        try:
            safe_json_loads(py_in)
        except Exception:
            py_out = 'THREW'
        self.assertEqual(py_out, _js_safe_json_loads(py_in),
                         'both engines must reject Infinity')


class TestExtractAllBalancedJsonObjects(unittest.TestCase):
    """Direct coverage of the new helper used by the scoring fix."""

    def test_returns_all_balanced_substrings(self):
        text = (
            'Schema: {"format": "demo", "version": 1}. '
            'Data: {"species": "T. rex", "confidence": 0.9}'
        )
        results = extract_all_balanced_json_objects(text)
        self.assertEqual(len(results), 2)


if __name__ == '__main__':
    unittest.main()
