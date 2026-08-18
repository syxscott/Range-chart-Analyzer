"""Regression tests for the Phase E shared bed parser.

Pre-Phase E, two independent ``_parse_bed`` implementations existed
and disagreed on subscript handling — both Bed 23c and Bed 23d were
both reduced to the integer 23 in eval_metrics, inflating accuracy.
Phase E unifies them through rca_core.bed_parser.
"""

import pytest


class TestSharedBedParser:
    def test_bed_with_prefix_and_subscript(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("Bed 23c")
        assert info == {"bed_num": 23, "bed_sub": "c", "raw": "Bed 23c"}

    def test_bed_with_prefix_no_subscript(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("Bed 23")
        assert info == {"bed_num": 23, "bed_sub": "", "raw": "Bed 23"}

    def test_bare_with_subscript(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("23c")
        assert info == {"bed_num": 23, "bed_sub": "c", "raw": "23c"}

    def test_bare_digit(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("23")
        assert info == {"bed_num": 23, "bed_sub": "", "raw": "23"}

    def test_empty_returns_none(self):
        from rca_core.bed_parser import parse_bed
        assert parse_bed("") is None
        assert parse_bed(None) is None

    def test_unparseable_returns_none(self):
        from rca_core.bed_parser import parse_bed
        # '' without digit should not match
        assert parse_bed("Bed") is None
        assert parse_bed("abc") is None

    def test_parse_bed_int_strips_subscript(self):
        """The legacy eval_metrics signature returned an int;
        parse_bed_int preserves that contract while keeping subscript
        available via parse_bed."""
        from rca_core.bed_parser import parse_bed_int, parse_bed
        assert parse_bed_int("Bed 23c") == 23
        assert parse_bed_int("Bed 23d") == 23
        # But parse_bed preserves the disagreement so the operator
        # can see Bed 23c != Bed 23d downstream.
        assert parse_bed("Bed 23c")["bed_sub"] != parse_bed("Bed 23d")["bed_sub"]

    def test_exporter_and_eval_metrics_now_agree_on_subscript(self):
        """The original divergence — exporter preserved Bed 23c's
        subscript while eval_metrics dropped it — is gone. Both
        call sites should produce the same bed_num for identical
        inputs."""
        from rca_core.exporter import _parse_bed as exporter_parse_bed
        from rca_core.bed_parser import parse_bed as shared_parse_bed
        for s in ["Bed 23c", "Bed 23d", "Bed 23", "23c", "Bed"]:
            a = exporter_parse_bed(s)
            b = shared_parse_bed(s)
            assert a == b, f"divergence on {s!r}: {a!r} vs {b!r}"
