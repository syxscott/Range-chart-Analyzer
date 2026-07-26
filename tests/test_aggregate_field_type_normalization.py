"""Test P1-11: aggregate field type normalization.

The signature used for deduplication must coerce values to str so that
{"a": 8} and {"a": "8"} produce the same signature and merge together.
"""
import pytest


class TestAggregateFieldTypeNormalization:
    """P1-11: int vs str values in structured fields must merge together."""

    def test_int_and_str_same_value_merge(self):
        """A dict with int 8 and a dict with str '8' must merge (same signature)."""
        from rca_core.aggregate import _merge_structured_field
        # One run produces {"id": 1, "name": "A"}
        # Another run produces {"id": "1", "name": "A"}
        # Both should be recognized as the same item (same key+value signature)
        values = [
            [{"id": 1, "name": "A"}],
            [{"id": "1", "name": "A"}],
        ]
        result = _merge_structured_field(values)
        # Should produce ONE merged entry (not two)
        assert len(result) == 1, f"Expected 1 merged entry, got {len(result)}: {result}"

    def test_different_values_do_not_merge(self):
        """Same key but different values must NOT merge."""
        from rca_core.aggregate import _merge_structured_field
        values = [
            [{"id": 1, "name": "A"}],
            [{"id": 1, "name": "B"}],  # different name
        ]
        result = _merge_structured_field(values)
        # Two different entries
        assert len(result) == 2, f"Expected 2 entries, got {len(result)}: {result}"

    def test_all_values_str_coerced_before_repr(self):
        """Signature computation should str-coerce all values before repr."""
        from rca_core.aggregate import _merge_structured_field
        # Mixed int/str in same dict
        values = [
            [{"count": 5, "label": "X"}],
            [{"count": "5", "label": "X"}],
        ]
        result = _merge_structured_field(values)
        assert len(result) == 1, "int 5 and str '5' should produce same signature and merge"
