"""Tests for P0-completion (REVIEW-2026-07-27) Track C: raw evidence integrity.

Covers:
- image_sha256 computation (image_hash module)
- HistoryRecord.image_sha256 + request_meta fields
- raw_responses table write / read
- record_edits table write / read on update_result
- get_by_sha256 for audit lookup
"""
import json
import os
import tempfile
import base64

import pytest


def _tmp_db():
    """Return a fresh Database backed by a temp file."""
    from rca_core.db import Database
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.unlink(path)
    return Database(path=path)


def test_compute_image_sha256_basic():
    from rca_core.image_hash import compute_image_sha256
    h = compute_image_sha256(b"hello world")
    assert len(h) == 64
    # Same input → same hash (stability)
    assert compute_image_sha256(b"hello world") == h
    # Different input → different hash
    assert compute_image_sha256(b"hello WORLD") != h


def test_compute_image_sha256_empty():
    from rca_core.image_hash import compute_image_sha256
    # SHA-256 of empty bytes (deterministic sentinel, never raises)
    h = compute_image_sha256(b"")
    assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_compute_image_sha256_from_b64_plain():
    from rca_core.image_hash import compute_image_sha256_from_b64, compute_image_sha256
    raw = b"\x89PNG\r\n\x1a\n test bytes"
    b64 = base64.b64encode(raw).decode()
    h = compute_image_sha256_from_b64(b64)
    assert h == compute_image_sha256(raw)


def test_compute_image_sha256_from_b64_data_url():
    from rca_core.image_hash import compute_image_sha256_from_b64, compute_image_sha256
    raw = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    b64 = base64.b64encode(raw).decode()
    data_url = f"data:image/jpeg;base64,{b64}"
    h = compute_image_sha256_from_b64(data_url)
    assert h == compute_image_sha256(raw)


def test_compute_image_sha256_from_b64_empty():
    from rca_core.image_hash import compute_image_sha256_from_b64
    h = compute_image_sha256_from_b64("")
    assert len(h) == 64


def test_compute_image_sha256_from_b64_corrupt_does_not_raise():
    from rca_core.image_hash import compute_image_sha256_from_b64
    # Malformed base64 — must not raise; we fall back to hashing the text
    h = compute_image_sha256_from_b64("not valid base64 !@#$")
    assert len(h) == 64


def test_history_record_has_new_fields():
    from rca_core.history import HistoryRecord
    rec = HistoryRecord()
    assert rec.image_sha256 == ""
    assert rec.request_meta == {}
    # Default values don't break serialization
    rec.image_sha256 = "abc123"
    rec.request_meta = {"model": "test"}
    assert rec.image_sha256 == "abc123"
    assert rec.request_meta["model"] == "test"


def test_history_add_with_image_sha256_and_request_meta():
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    store = HistoryStore(db=db)
    rec = HistoryRecord(
        source_file="/tmp/x.png",
        image_sha256="deadbeef" * 8,
        request_meta={"model": "M", "max_tokens": 4096},
        result={"sections": [], "species_ranges": []},
    )
    rid = store.add(rec)
    got = store.get(rid)
    assert got.image_sha256 == "deadbeef" * 8
    assert got.request_meta == {"model": "M", "max_tokens": 4096}


def test_history_raw_responses_roundtrip():
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    store = HistoryStore(db=db)
    rec = HistoryRecord(source_file="/tmp/x.png", result={"ok": True})
    rid = store.add(rec, raw_responses=[
        {"run_idx": 0, "raw_text": "RAW TEXT RUN 1", "request_meta": {"k": 1}, "timestamp": 1700000000},
        {"run_idx": 1, "raw_text": "RAW TEXT RUN 2", "request_meta": {"k": 2}, "timestamp": 1700000001},
    ])
    all_raw = store.get_raw_responses(rid)
    assert len(all_raw) == 2
    assert all_raw[0]["raw_text"] == "RAW TEXT RUN 1"
    assert all_raw[1]["raw_text"] == "RAW TEXT RUN 2"
    # Filter by run_idx
    only_first = store.get_raw_responses(rid, run_idx=0)
    assert len(only_first) == 1
    assert only_first[0]["raw_text"] == "RAW TEXT RUN 1"


def test_history_raw_response_no_truncation():
    """P2: raw_text in raw_responses must NOT be truncated to 8KB."""
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    store = HistoryStore(db=db)
    rec = HistoryRecord(source_file="/tmp/x.png", result={})
    rid = store.add(rec)
    big = "x" * (50 * 1024)  # 50 KB
    store._insert_raw_responses(rid, [
        {"run_idx": 0, "raw_text": big, "request_meta": {}, "timestamp": 0},
    ])
    all_raw = store.get_raw_responses(rid)
    assert len(all_raw[0]["raw_text"]) == 50 * 1024


def test_history_get_by_sha256():
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    store = HistoryStore(db=db)
    shared = "a" * 64
    store.add(HistoryRecord(source_file="/tmp/1.png", image_sha256=shared, result={}))
    store.add(HistoryRecord(source_file="/tmp/2.png", image_sha256=shared, result={}))
    store.add(HistoryRecord(source_file="/tmp/3.png", image_sha256="b" * 64, result={}))
    found = store.get_by_sha256(shared)
    assert len(found) == 2


def test_history_update_result_writes_record_edits():
    """P3: update_result must append immutable record_edits row."""
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    store = HistoryStore(db=db)
    rec = HistoryRecord(source_file="/tmp/x.png", result={"v": 1})
    rid = store.add(rec)
    # First edit
    ok = store.update_result(rid, {"v": 2}, editor="alice")
    assert ok
    edits = store.get_edits(rid)
    assert len(edits) == 1
    assert edits[0]["editor"] == "alice"
    assert edits[0]["edit_type"] == "result_update"
    assert edits[0]["before"] == {"v": 1}
    assert edits[0]["after"] == {"v": 2}
    # Second edit
    store.update_result(rid, {"v": 3}, editor="bob")
    edits = store.get_edits(rid)
    assert len(edits) == 2
    assert edits[1]["after"] == {"v": 3}
    assert edits[1]["before"] == {"v": 2}


def test_history_record_edit_direct_helper():
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    store = HistoryStore(db=db)
    rec = HistoryRecord(source_file="/tmp/x.png", result={})
    rid = store.add(rec)
    eid = store.record_edit(rid, editor="manual", edit_type="cell_update",
                            row_idx=3, col_name="species",
                            before="Genus a", after="Genus b")
    assert eid > 0
    edits = store.get_edits(rid)
    assert len(edits) == 1
    assert edits[0]["edit_type"] == "cell_update"
    assert edits[0]["row_idx"] == 3
    assert edits[0]["col_name"] == "species"


def test_record_edit_keeps_numeric_audit_values():
    """REVIEW-2026-09-20 (finding 8): non-string audit values must survive.

    The reader used to call json.loads() on the column value unconditionally.
    A JSON-affinity column hands SQLite's native scalar back (int 12, not
    "12"), json.loads raised TypeError, and the except-branch silently
    replaced the value with None — the audit trail lost exactly the numeric
    edits (bed index, confidence, age) that reviewers look for.
    """
    from rca_core.history import HistoryStore, HistoryRecord
    db = _tmp_db()
    try:
        store = HistoryStore(db=db)
        rid = store.add(HistoryRecord(source_file="/tmp/x.png", result={}))
        cases = [
            ("int", 12, 13),
            ("zero", 0, 1),
            ("float", 0.5, 1.5),
            ("bool", True, False),
            ("dict", {"bed": "23a"}, {"bed": "23b"}),
            ("str", "Bed 23a", "Bed 23b"),
            ("null", None, 7),
        ]
        for name, before, after in cases:
            store.record_edit(rid, editor="manual", edit_type="cell_update",
                              row_idx=1, col_name=name, before=before, after=after)
        edits = store.get_edits(rid)
        assert len(edits) == len(cases)
        got = {e["col_name"]: (e["before"], e["after"]) for e in edits}
        for name, before, after in cases:
            assert got[name] == (before, after), f"audit value lost for {name}"
        # None stays None on the "before" side (no value existed).
        assert got["null"][0] is None
    finally:
        db.close()


def test_decode_audit_value_adopts_native_scalars():
    """REVIEW-2026-09-20 (finding 8): reader contract, incl. legacy rows.

    Databases created before the TEXT-affinity fix still have a JSON column,
    so SQLite returns native scalars; those must be adopted verbatim instead
    of being re-parsed.
    """
    from rca_core.history import _decode_audit_value
    assert _decode_audit_value(None) is None
    assert _decode_audit_value(12) == 12
    assert _decode_audit_value(0.5) == 0.5
    assert _decode_audit_value(True) is True
    assert _decode_audit_value(b'{"a": 1}') == {"a": 1}
    assert _decode_audit_value('{"a": 1}') == {"a": 1}
    assert _decode_audit_value("12") == 12
    # Non-JSON text (written by a foreign tool) is kept, not dropped.
    assert _decode_audit_value("Bed 23c") == "Bed 23c"


def test_extract_result_has_image_sha256_default():
    from rca_core.extractor import ExtractResult
    r = ExtractResult()
    assert r.image_sha256 == ""
    assert r.request_meta == {}


def test_image_sha256_computed_for_empty_input_is_empty_sha256():
    """If image_b64 is empty, function returns ok=False with image_sha256 of empty bytes."""
    from rca_core.image_hash import compute_image_sha256
    from rca_core.extractor import extract_range_chart
    result = extract_range_chart(
        api_key="dummy", image_b64="", media_type="image/png"
    )
    assert result.ok is False
    assert result.error_key == "err.imageRead"
    # The sentinel hash for empty bytes
    expected = compute_image_sha256(b"")
    assert expected == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_end_to_end_extract_to_history_with_provenance():
    """End-to-end: extract → image_sha256 → history.add with raw_responses."""
    from rca_core.history import HistoryStore, HistoryRecord
    from rca_core.image_hash import compute_image_sha256_from_b64

    fake_image = b"\x89PNG_FAKE_BYTES_FOR_TEST"
    image_b64 = base64.b64encode(fake_image).decode()
    expected_sha = compute_image_sha256_from_b64(image_b64)

    db = _tmp_db()
    store = HistoryStore(db=db)

    # Simulate the client side persisting an extraction result
    rec = HistoryRecord(
        source_file="user_paste.png",
        image_sha256=expected_sha,
        request_meta={
            "model": "MiniMax-M3",
            "max_tokens": 4096,
            "prompt_version": "v3",
            "chart_lang": "en",
            "temperature": 0.0,
        },
        result={"sections": [], "species_ranges": [], "confidence": 0.85},
        mode="range_chart",
        provider_name="MiniMax",
        runs=2,
    )
    rid = store.add(rec, raw_responses=[
        {
            "run_idx": 0, "raw_text": json.dumps({"sections": [], "species_ranges": []}),
            "request_meta": {"model": "MiniMax-M3"}, "timestamp": 1700000000,
        },
        {
            "run_idx": 1, "raw_text": json.dumps({"sections": [], "species_ranges": [{"species": "A"}]}),
            "request_meta": {"model": "MiniMax-M3"}, "timestamp": 1700000001,
        },
    ])

    # Audit lookup by image hash — finds the record
    same_image = store.get_by_sha256(expected_sha)
    assert len(same_image) == 1
    assert same_image[0].id == rid

    # Full audit: image hash + raw responses + request meta
    got = store.get(rid)
    assert got.image_sha256 == expected_sha
    assert got.request_meta["model"] == "MiniMax-M3"
    assert got.runs == 2

    raws = store.get_raw_responses(rid)
    assert len(raws) == 2
    assert raws[0]["run_idx"] == 0
    assert raws[1]["run_idx"] == 1
    # 5-year audit: can prove the record came from a specific image AND
    # recover both raw model responses verbatim.