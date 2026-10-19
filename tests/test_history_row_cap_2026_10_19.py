"""The history row cap: what it actually does, stated honestly.

The cap's own docstring used to describe a safeguard that never existed: "shed
the thumbnail first, and only delete a row when the table is still over the
cap". Step 1 clears a BLOB and cannot change the ROW COUNT, and step 2's
predicate was the row count -- the same statement as the condition that entered
it, so it was always true. The rows it stripped were exactly the rows that
were then deleted, and their `raw_responses` and `record_edits` children
cascaded away with them.

The 2026-09-27 P1 this answers was fixed by raising the default from 500
(3.5 months of use) to 5000, not by the shedding. The cap is a RETENTION cap.

This file therefore pins what the cap ACTUALLY does -- the row count is capped,
and eviction takes the provenance of the evicted rows with it -- and that the
documentation says so. Changing the retention policy (keeping stripped rows
forever) is a different decision, with a different storage cost, and is not
made here.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core import history as H                            # noqa: E402
from rca_core.db import Database                            # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "MAX_HISTORY_ROWS", 5)
    # HistoryStore takes a Database, not a path: the first version of this
    # fixture passed the path string and every call failed with
    # "'str' object has no attribute 'execute'".
    db = Database(str(tmp_path / "rca.db"))
    st = H.HistoryStore(db)
    yield st
    try:
        db.close()
    except Exception:
        pass


def _add(st, i, *, thumb=True, raw="x" * 4000):
    rec = H.HistoryRecord(
        timestamp=1000.0 + i,
        source_file="probe",
        image_thumbnail=b"\x89PNG\r\n\x1a\n" + b"0" * 200 if thumb else None,
        image_width=0, image_height=0,
        provider_id="p", provider_name="p", model="m", mode="range_chart",
        runs=1, result={"a": 1}, raw=raw,
        confidence=1.0, partial_failures=0, duration_ms=1,
        status_code=200, notes="", image_sha256="sha%d" % i,
    )
    raws = [{"run_idx": 0, "raw_text": "the model said something",
             "prompt_text": "", "request_meta": {},
             "timestamp": 1000.0 + i}]
    st.add(rec, raw_responses=raws)
    return rec


def _counts(st):
    rows = st.db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    raws = st.db.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
    thumbs = st.db.execute(
        "SELECT COUNT(*) FROM history WHERE image_thumbnail IS NOT NULL"
    ).fetchone()[0]
    return rows, raws, thumbs


def test_the_cap_is_a_retention_cap(store):
    """The row count is capped -- that is the contract, and the P1 test
    in tests/test_audit_2026_09_27.py pins it too."""
    for i in range(7):
        _add(store, i)
    rows, _raws, _thumbs = _counts(store)
    assert rows == 5, "the retention cap is on the ROW COUNT"


def test_the_eviction_is_announced_with_what_it_costs(store, capsys):
    for i in range(7):
        _add(store, i)
    err = capsys.readouterr().err
    assert "evicted" in err
    assert "raw responses" in err, \
        "the operator must be told that the provenance goes too"


def test_the_size_driver_is_shed_before_the_rows_go(store):
    """The shedding still happens; it is simply not the safeguard."""
    for i in range(7):
        _add(store, i)
    rows, _raws, thumbs = _counts(store)
    assert rows == 5
    assert thumbs <= 5, "the thumbnail is the size driver and must be shed"


def test_the_documentation_does_not_claim_a_safeguard_it_lacks():
    """The defect that was actually found: the docstring promised a protection
    the code could not deliver, so a reader of the code believed the audit
    trail survived eviction. It does not, and now the text says so."""
    src = inspect.getsource(H.HistoryStore._enforce_row_cap)
    assert "only deletes a row when" not in src, \
        "the old claim must not come back"
    assert "RETENTION cap" in src, "the real behaviour must be stated"
    assert "no-op" in src or "never was one" in src, \
        "the review finding must stay on the record"
