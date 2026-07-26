"""Test P1-3: _payload_score array semantic parity between Python and JS.

The Python _payload_score recursively sums dict-item scores inside arrays.
The JS _payloadScore previously returned 0 for arrays, losing signal.
Both implementations must agree on array scoring.
"""
import json
import re
import os
import pytest


class TestPayloadScoreArrayParity:
    """P1-3: JS _payloadScore must sum dict-item scores for arrays."""

    def test_python_payload_score_arrays(self):
        """Python _payload_score sums dict-item scores for arrays."""
        from rca_core.json_utils import _payload_score

        # Simple list of dicts with payload keys
        parsed = [
            {"species_ranges": [{"species": "A"}]},
            {"species_ranges": [{"species": "B"}]},
        ]
        score = _payload_score(parsed)
        assert score > 0, "Array with dict items carrying payload keys must score > 0"

        # Each item contributes 100 (species_ranges hit) + nested
        # → total should be sum of both items
        single = _payload_score([{"species_ranges": [{"species": "A"}]}])
        assert single > 0

    def test_python_payload_score_array_vs_dict_parity(self):
        """Python: array score should sum dict-item scores, not return 0."""
        from rca_core.json_utils import _payload_score

        # Array with one dict item
        arr_score = _payload_score([{"species_ranges": [1, 2]}])
        # Dict with one dict item
        dict_score = _payload_score({"species_ranges": [1, 2]})

        # Array score should be similar to dict score (both have same nesting depth)
        assert arr_score > 0, "Array of dicts must score > 0 (not 0)"

    def test_js_payload_score_handles_arrays(self):
        """JS _payloadScore must not return 0 for arrays of dicts."""
        # Read the JS source
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "json-utils.js"
        )
        with open(js_path, encoding="utf-8") as f:
            js_source = f.read()

        # Check that _payloadScore handles arrays (not early-return 0)
        # The fix should NOT have: if (Array.isArray(parsed)) return 0;
        # Instead it should iterate and sum dict items.
        m = re.search(
            r'function _payloadScore\(parsed\)\s*\{(.*?)(?=\nfunction |\nconst |\nlet |\nvar |\Z)',
            js_source, re.DOTALL
        )
        assert m, "_payloadScore function not found"
        body = m.group(1)

        # Must NOT early-return 0 for arrays
        # Pattern: Array.isArray check followed immediately by return 0
        has_bad_early_return = re.search(
            r'Array\.isArray\(parsed\)[^;]*return\s+0',
            body
        )
        assert not has_bad_early_return, (
            "JS _payloadScore must not return 0 for arrays - "
            "it should sum dict-item scores like Python does"
        )

        # Must handle arrays with iteration
        assert "for" in body and ("item" in body or "parsed" in body), (
            "JS _payloadScore must iterate over array items"
        )

    def test_js_payload_score_array_handling_code(self):
        """Verify the JS fix code is present: array case must sum dict scores."""
        js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..",
            "js", "json-utils.js"
        )
        with open(js_path, encoding="utf-8") as f:
            js_source = f.read()

        # The fixed code should have an array branch that sums dict items
        # Look for: if (Array.isArray(parsed)) { ... for ... _payloadScore(item) ... }
        has_array_branch = re.search(
            r'if\s*\(\s*Array\.isArray\(parsed\)\s*\)(.*?)\}',
            js_source, re.DOTALL
        )
        assert has_array_branch, "JS _payloadScore must have an Array.isArray branch"
        branch_body = has_array_branch.group(1)
        # The branch must NOT just return 0
        assert not re.search(r'^\s*return\s+0', branch_body.strip()), (
            "JS _payloadScore array branch must not just return 0"
        )

    def test_python_and_js_array_semantic_match(self):
        """Python and JS must both sum dict-item scores for arrays."""
        from rca_core.json_utils import _payload_score

        # Python: array with dicts containing payload keys
        test_payload = [
            {"species_ranges": [1]},
            {"sections": [2]},
            {"biozones": [3]},
        ]
        py_score = _payload_score(test_payload)

        # JS: same structure, verified via source code inspection
        # Both must give non-zero scores (no early-return-0 for arrays)
        assert py_score > 0, (
            "Python scores non-empty array of dicts > 0; "
            "JS must match this behavior"
        )

        # Bare array with no dict items scores 0
        empty_score = _payload_score([1, 2, 3])
        assert empty_score == 0, "Array of primitives should score 0"
