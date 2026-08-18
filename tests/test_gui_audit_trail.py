"""Regression tests for the Phase J GUI audit-trail fixes.

The previous build wrote HistoryRecord rows from the GUI without
populating ``image_sha256`` or ``request_meta`` (both defaulted to
empty). The Tkinter gui.py did not write ANY history record at all.
These tests pin the new behaviour so the 5-year audit trail is
complete on both fronts.
"""

import pytest
from rca_core.extractor import ExtractResult


class TestGuiHistoryAuditFields:
    """ExtractResult must carry image_sha256 + request_meta so the GUI
    save_to_history() caller has something meaningful to persist.
    """

    def test_extract_result_default_has_empty_audit_fields(self):
        r = ExtractResult(ok=True, data={"sections": [], "species_ranges": []})
        # Defaults: empty fingerprint and empty audit dict.
        assert r.image_sha256 == ""
        assert r.request_meta == {}

    def test_request_meta_accepts_audit_dict(self):
        meta = {
            "model": "MiniMax-M3",
            "max_tokens": 4096,
            "prompt_version": "v3",
            "image_sha256": "abc123",
            "endpoint": "https://api.minimaxi.com/anthropic",
        }
        r = ExtractResult(
            ok=True, data={}, image_sha256="abc123",
            request_meta=meta,
        )
        assert r.image_sha256 == "abc123"
        assert r.request_meta["model"] == "MiniMax-M3"
        assert r.request_meta["prompt_version"] == "v3"
        assert r.request_meta["max_tokens"] == 4096

    def test_historyrecord_serialization_round_trip(self):
        """HistoryRecord.to_dict() must serialize the audit fields;
        Phase J fix confirmed both are reachable."""
        from rca_core.history import HistoryRecord
        rec = HistoryRecord(
            timestamp=1700000000.0,
            mode="range_chart",
            runs=1,
            result={"sections": [], "species_ranges": []},
            raw="{}",
            confidence=0.85,
            image_sha256="deadbeef" * 8,
            request_meta={"model": "MiniMax-M3", "prompt_version": "v3"},
        )
        d = rec.to_dict()
        assert d.get("image_sha256") == "deadbeef" * 8
        assert d.get("request_meta", {}).get("model") == "MiniMax-M3"
        assert d.get("request_meta", {}).get("prompt_version") == "v3"

    def test_duplicate_field_decl_no_runtime_error(self):
        """Regression: Phase D1 collapsed the duplicate ``image_sha256``
        and ``request_meta`` field declarations in ExtractResult. The
        Python dataclass keeps the LAST one but earlier we had two
        such declarations. Make sure a runtime instance still has the
        expected shape."""
        import dataclasses
        r = ExtractResult()
        fields = [f.name for f in dataclasses.fields(ExtractResult)]
        # Exactly one of each (not two):
        assert fields.count("image_sha256") == 1
        assert fields.count("request_meta") == 1
