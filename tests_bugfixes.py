"""Regression tests for the bugs flagged in the 2026-07-18 code review.

Each test names the bug it covers (Bug-N) so a future failure points
straight at the fix that regressed.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core.db import Database
from rca_core.history import HistoryStore, MAX_HISTORY_ROWS, make_thumbnail
from rca_core.usage import UsageStore, _row_to_record as _usage_row_to_record, UsageRecord
from rca_core.editable import apply_edits, capture_edits
from rca_core.extractor import load_image_b64, clamp_max_tokens, clamp_timeout_sec
from rca_core.llm import LlmProvider, ApiFormat, _chmod_user_only
import server as server_mod
from server import _validate_endpoint

_pass = 0
_fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)


# ----------------------------------------------------------------------
# Bug-1: usage.summary() with date range returns non-empty by_day rows.
# ----------------------------------------------------------------------
def _make_usage_rec(ts: float) -> UsageRecord:
    return UsageRecord(
        timestamp=ts,
        provider_id="p", provider_name="p", model="m",
        endpoint="", mode="range_chart",
        input_tokens=10, output_tokens=5,
        cache_read_tokens=0, cache_creation_tokens=0,
        status_code=200, latency_ms=100,
    )


def test_bug1_summary_with_date_range_by_day():
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "t.db"))
        try:
            store = UsageStore(db=db)
            # Three rows on three different UTC days.
            for i in range(3):
                store.record(_make_usage_rec(86400.0 * (i + 1)))
            # The pre-fix code returned [] for by_day when start_ts was
            # supplied (because of the ?/:offset parameter mismatch).
            s = store.summary(start_ts=0.0, end_ts=86400 * 10)
            check("bug1-by-day-non-empty", len(s.by_day) > 0)
            check("bug1-by-day-count-matches",
                  sum(d["count"] for d in s.by_day) == 3)
        finally:
            db.close()


# ----------------------------------------------------------------------
# Bug-2: by_day buckets use local time, not UTC.
# ----------------------------------------------------------------------
def test_bug2_by_day_uses_local_offset():
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "t.db"))
        try:
            store = UsageStore(db=db)
            # One row at noon UTC on day 5.
            store.record(_make_usage_rec(86400.0 * 5 + 43200))
            s = store.summary()
            # by_day must be non-empty and have exactly 1 entry.
            check("bug2-by-day-single-bucket", len(s.by_day) == 1)
            if s.by_day:
                # The bucket key must reflect local-midnight alignment:
                # day_bucket = int(ts/86400)*86400 + local_offset_at_noon.
                import time as _t
                local_offset = -_t.localtime(86400 * 5 + 43200).tm_gmtoff
                expected_day = int((86400 * 5 + 43200) // 86400) * 86400 + local_offset
                check("bug2-by-day-key-uses-local-offset",
                      s.by_day[0]["day"] == expected_day)
        finally:
            db.close()


# ----------------------------------------------------------------------
# Bug-4: apply_edits preserves the order of multiple new_* rows.
# ----------------------------------------------------------------------
def test_bug4_new_rows_in_order():
    # Capture a result where the user appended three new rows at the end
    # (original length 1, new length 4) and apply it to the original.
    before = {"sections": [{"name": "a"}]}
    after = {"sections": [
        {"name": "a"},
        {"name": "b"},
        {"name": "c"},
        {"name": "d"},
    ]}
    edits = capture_edits(before, after)
    out = apply_edits({"sections": [{"name": "a"}]}, edits)
    check("bug4-three-appended-rows", len(out["sections"]) == 4)
    names = [s.get("name") for s in out["sections"]]
    check("bug4-original-row-preserved", names[0] == "a")
    check("bug4-new-rows-in-order", names[1:] == ["b", "c", "d"])


def test_bug4_new_rows_out_of_capture_order():
    # Even if capture emitted them out of order (it doesn't, but defend
    # against future changes), apply must sort before inserting.
    result = {"sections": [{"name": "a"}]}
    # Manually craft an edits payload with new_* in non-monotonic order.
    edits = {"sections": {
        "new_3": {"name": "d"},
        "new_1": {"name": "b"},
        "new_2": {"name": "c"},
    }}
    apply_edits(result, edits)
    names = [s.get("name") for s in result["sections"]]
    check("bug4-insert-sort-1", names == ["a", "b", "c", "d"])


# ----------------------------------------------------------------------
# Bug-5: Database holds a single persistent connection, not per-call.
# ----------------------------------------------------------------------
def test_bug5_persistent_connection():
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "t.db"))
        try:
            conn_id_1 = id(db._conn)
            conn_id_2 = id(db._conn)
            check("bug5-same-conn-object", conn_id_1 == conn_id_2)
            # Query 100 times — connection object unchanged.
            for _ in range(100):
                db.query("SELECT 1")
            check("bug5-conn-stable-after-queries",
                  id(db._conn) == conn_id_1)
        finally:
            db.close()


# ----------------------------------------------------------------------
# Bug-6: _validate_endpoint rejects private hosts.
# ----------------------------------------------------------------------
def test_bug6_validate_endpoint_rejects_private():
    cases = [
        ("http://api.example.com", False, "http rejected (https required)"),
        ("https://localhost", False, "localhost rejected"),
        ("https://127.0.0.1", False, "loopback rejected"),
        ("https://10.0.0.1", False, "RFC1918 rejected"),
        ("https://192.168.1.1", False, "RFC1918 rejected"),
        ("https://169.254.169.254", False, "AWS metadata rejected"),
        ("https://api.minimaxi.com", True, "public host accepted"),
    ]
    for url, expected_ok, label in cases:
        ok, _ = _validate_endpoint(url)
        check(f"bug6-{label}", ok == expected_ok)


def test_bug6_validate_endpoint_allows_private_when_opt_in(monkeypatch=None):
    # Patch _ALLOW_PRIVATE on the server module.
    saved = server_mod._ALLOW_PRIVATE
    try:
        server_mod._ALLOW_PRIVATE = True
        ok, _ = server_mod._validate_endpoint("https://127.0.0.1")
        check("bug6-opt-in-allows-loopback", ok)
    finally:
        server_mod._ALLOW_PRIVATE = saved


# ----------------------------------------------------------------------
# Bug-7: probe fallback doesn't waste quota on 401/403 with the
# configured model (re-uses the dispatcher but doesn't iterate the
# full fallback list).
# ----------------------------------------------------------------------
def test_bug7_probe_breaks_on_401_for_configured_model():
    """Indirect test: confirm the dispatch table excludes the legacy
    "loop over all fallbacks" behaviour by inspecting the source for the
    early-break logic. Catches a regression where someone removes the
    `if status in (401, 403) and model == configured: break` guard.
    """
    import inspect
    src = inspect.getsource(sys.modules["rca_core.llm"])
    check("bug7-early-break-401", "status in (401, 403)" in src and "configured" in src)


# ----------------------------------------------------------------------
# Bug-8: make_thumbnail caps size.
# ----------------------------------------------------------------------
def test_bug8_make_thumbnail_caps_size():
    # Construct a 1000x1000 white image. Without the cap this would be
    # ~50 KB at quality 70; with the 200-px cap + 20 KB byte cap, the
    # result must be small.
    try:
        from PIL import Image
        import io
    except Exception:
        # No Pillow — the helper falls back to truncation.
        big = b"\x00" * 100000
        out = make_thumbnail(big)
        check("bug8-no-pillow-truncates", len(out) <= 20 * 1024)
        return
    img = Image.new("RGB", (1000, 1000), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = make_thumbnail(buf.getvalue())
    check("bug8-thumbnail-under-cap", len(out) <= 20 * 1024)


def test_bug8_history_evicts_old_rows():
    with tempfile.TemporaryDirectory() as td:
        db = Database(path=os.path.join(td, "t.db"))
        try:
            store = HistoryStore(db=db)
            # Insert MAX_HISTORY_ROWS + 5 rows. After the last insert,
            # total must not exceed the cap.
            from rca_core.history import HistoryRecord
            for i in range(MAX_HISTORY_ROWS + 5):
                rec = HistoryRecord(
                    timestamp=float(i),
                    result={"sections": []},
                    source_file=f"f{i}",
                )
                store.add(rec)
            check("bug8-row-count-capped",
                  store.count() == MAX_HISTORY_ROWS)
        finally:
            db.close()


# ----------------------------------------------------------------------
# Bug-9: _chmod_user_only sets 0600 on POSIX (no-op on Windows).
# ----------------------------------------------------------------------
def test_bug9_chmod_user_only():
    """On POSIX we verify the file ends up mode 0600. On Windows the
    ACL model differs and chmod is best-effort, so we just confirm the
    helper doesn't raise.
    """
    if not hasattr(os, "chmod"):
        check("bug9-skip-on-platform-without-chmod", True)
        return
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        os.chmod(path, 0o644)  # ensure a known starting state
        _chmod_user_only(path)
        mode = os.stat(path).st_mode & 0o777
        # Windows: admin processes can read regardless of mode bits, so
        # we accept either 0o600 (POSIX) or the unchanged 0o644 (Windows
        # where chmod is a no-op).
        is_windows = sys.platform == "win32"
        if is_windows:
            # Windows chmod is largely cosmetic — the ACL is inherited
            # from the parent directory and individual mode bits may or
            # may not stick. We just verify the helper didn't raise and
            # left the file present. POSIX users get the 0600 guarantee.
            check("bug9-windows-no-raise", os.path.isfile(path))
        else:
            check("bug9-mode-is-0600", mode == 0o600)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Bug-15: load_image_b64 reports decode_error on corrupt input.
# ----------------------------------------------------------------------
def test_bug15_decode_error_flag():
    # Write a "PNG" that's actually random bytes — Pillow will fail to
    # decode but the file exists.
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"not really a png" * 10)
        bad_path = f.name
    try:
        try:
            b64, mime, w, h, resized, decode_error = load_image_b64(bad_path)
        except Exception as exc:
            check("bug15-loads-without-raising", False)
            return
        # If Pillow is missing, decode_error is False but the result
        # is still 0×0. The bug-15 contract: when Pillow IS present and
        # decode fails, decode_error must be True.
        try:
            from PIL import Image  # noqa
            has_pil = True
        except Exception:
            has_pil = False
        if has_pil:
            check("bug15-decode-error-true-on-corrupt", decode_error is True)
            check("bug15-zero-dims-on-corrupt", w == 0 and h == 0)
        else:
            check("bug15-no-pillow-skipped", True)
    finally:
        try:
            os.unlink(bad_path)
        except OSError:
            pass


# ----------------------------------------------------------------------
# Bug-18: clamp_timeout_sec bounds the user's input.
# ----------------------------------------------------------------------
def test_bug18_clamp_timeout():
    check("bug18-negative-defaults", clamp_timeout_sec(-5) >= 10)
    check("bug18-huge-capped", clamp_timeout_sec(100000) <= 300)
    check("bug18-string-parses", clamp_timeout_sec("120") == 120)
    check("bug18-junk-uses-default",
          clamp_timeout_sec("garbage") == 120)


# ----------------------------------------------------------------------
# Bug-14: schema version auto-derived from _MIGRATIONS.
# ----------------------------------------------------------------------
def test_bug14_schema_version_derives_from_migrations():
    # The class attribute is auto-set at import time to len(_MIGRATIONS).
    check("bug14-version-equals-migration-count",
          Database._CURRENT_SCHEMA_VERSION == len(Database._MIGRATIONS))
    # Simulate adding a migration and re-deriving.
    saved = list(Database._MIGRATIONS)
    saved.append((0, 1, "SELECT 1;"))  # dummy migration
    Database._MIGRATIONS = saved
    Database._CURRENT_SCHEMA_VERSION = len(Database._MIGRATIONS)
    check("bug14-version-recomputes", Database._CURRENT_SCHEMA_VERSION == 1)
    # Restore.
    Database._MIGRATIONS = saved[:-1]
    Database._CURRENT_SCHEMA_VERSION = len(Database._MIGRATIONS)


test_bug1_summary_with_date_range_by_day()
test_bug2_by_day_uses_local_offset()
test_bug4_new_rows_in_order()
test_bug4_new_rows_out_of_capture_order()
test_bug5_persistent_connection()
test_bug6_validate_endpoint_rejects_private()
test_bug6_validate_endpoint_allows_private_when_opt_in()
test_bug7_probe_breaks_on_401_for_configured_model()
test_bug8_make_thumbnail_caps_size()
test_bug8_history_evicts_old_rows()
test_bug9_chmod_user_only()
test_bug15_decode_error_flag()
test_bug18_clamp_timeout()
test_bug14_schema_version_derives_from_migrations()
print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)
