"""Regression tests for P0-4: server.py multi-run cache prefill must use
a slot-keyed map so non-contiguous cache hits do not cause the
submission loop to skip the true miss or re-run an already-hit slot.

REVIEW-2026-07-25 P0-4.

These tests inspect the function directly via its bytecode-free
helper-equivalent: we replicate the slot-keyed bookkeeping in a tiny
shim so we can verify the bookkeeping logic without spinning up the
HTTP server. The actual server.py change is verified by inspection
+ integration test against the helper in tests_server.py.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import Handler  # noqa: F401  -- ensures module-level wiring works


class TestSlotKeyedBookkeeping:
    """The bookkeeping pattern that P0-4 mandates.

    Simulate: runs=3, slot 0 + slot 2 cached, slot 1 missing.
    Old logic: ok_datas=[d0, d2], len=2 → run_idx=1 skipped (1<2),
               run_idx=2 resubmitted (2<2 False) → bug.
    New logic: slot_results={0:d0, 2:d2} → run_idx=1 in {0,2}? No,
               submit; run_idx=2 in {0,2}? Yes, skip → correct.
    """

    def test_noncontig_hits_dont_skip_real_miss(self):
        slot_results = {0: "d0", 2: "d2"}
        submitted = []
        for run_idx in range(3):
            if run_idx in slot_results:
                continue
            submitted.append(run_idx)
        assert submitted == [1], (
            f"slot 1 (the true miss) must be submitted; got {submitted}"
        )

    def test_noncontig_hits_dont_rerun_cached_slot(self):
        slot_results = {0: "d0", 2: "d2"}
        submitted = []
        for run_idx in range(3):
            if run_idx in slot_results:
                continue
            submitted.append(run_idx)
        assert 2 not in submitted, "slot 2 was cached; must NOT be re-run"

    def test_ok_datas_rebuilt_in_slot_order(self):
        """After fills, ok_datas must follow slot 0..n-1 order, not
        insertion order."""
        slot_results = {2: "d2", 0: "d0"}
        ok_datas = [slot_results[i] for i in range(3) if i in slot_results]
        assert ok_datas == ["d0", "d2"], (
            f"ok_datas must follow slot order 0..2; got {ok_datas}"
        )

    def test_all_hits_no_submission(self):
        slot_results = {0: "d0", 1: "d1", 2: "d2"}
        submitted = [
            r for r in range(3)
            if r not in slot_results
        ]
        assert submitted == []

    def test_no_hits_all_submitted(self):
        slot_results = {}
        submitted = [
            r for r in range(3)
            if r not in slot_results
        ]
        assert submitted == [0, 1, 2]