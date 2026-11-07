"""The ONE implementation of "collect N extraction futures under one budget".

WHY THIS EXISTS (ARCH-REVIEW-2026-11-02, P2-7 / A3)
===================================================
This decision used to be implemented THREE times: gui.py (Tkinter),
gui_fluent.py (PySide6) and server.py's multi-run branch. The two GUI copies
had already DRIFTED: the Fluent one gained `should_cancel` and `on_batch`,
so "the user clicks Cancel" did nothing on Tkinter, and only the Fluent UI
showed intermediate progress. Any fix to timeout / retry / failure-count
semantics had to be applied in both places, and nothing announced that the
copy existed.

Both GUI modules now keep their ORIGINAL public function names as thin
adapters (Tk returns a plain list; Fluent returns `(results, cancelled)`),
so every existing call site and every existing test keeps working while the
timeout / cancellation / progress decisions live here exactly once.

The server's multi-run branch still has its own slot mapping (it also does
cache probing, merge, scoring and usage aggregation); that is the phase-3
pipeline work, not a fan-out duplicate.

DESIGN CONSTRAINT: no GUI toolkit imports. This module is unit-testable
without a display, which tests/test_gui_sprint_b.py relies on.
"""
from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Callable, Iterable

from .extractor import ExtractResult

_log = logging.getLogger(__name__)

__all__ = ["collect_extraction_futures"]


def collect_extraction_futures(
    futures: Iterable[Any],
    budget: float,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_batch: Callable[[int], None] | None = None,
    log: logging.Logger | None = None,
) -> tuple[list, bool]:
    """Collect extraction futures under a REAL whole-batch time budget.

    All runs execute concurrently, so ONE budget equal to the per-request
    LLM timeout plus a small grace window covers the batch. Futures that
    overrun it are reported as ``err.timeout`` ExtractResults instead of
    being waited on indefinitely. Running threads cannot be killed safely,
    so the caller must NOT join the executor after this returns
    (``shutdown(wait=False)``).

    Returns ``(results, cancelled)``. ``cancelled`` is True when
    ``should_cancel`` fired between batches; every unfinished future was
    ``cancel()``ed and only the results collected so far are returned.
    ``on_batch(done_count)`` runs after each completed batch.

    The docstring of each old copy claimed the per-future ``fut.result(
    timeout=...)`` loop "was dead code because as_completed only yields
    finished futures". That history is preserved here; ``concurrent.futures
    .wait(..., ALL_COMPLETED)`` with a batch deadline is the fix.
    """
    logger = log or _log
    results: list = []
    pending = set(futures)
    deadline = time.monotonic() + float(budget)
    while pending:
        remaining = deadline - time.monotonic()
        done_set, pending = concurrent.futures.wait(
            pending, timeout=max(0.0, remaining),
            return_when=concurrent.futures.ALL_COMPLETED)
        for fut in done_set:
            try:
                results.append(fut.result())
            except Exception as exc:
                # extract() never raises, but defend against unforeseen
                # bugs in user code. Bug-12 fix: log the exception class +
                # traceback so "nothing happened" is diagnosable.
                logger.exception("extract future raised in worker thread")
                results.append(ExtractResult(
                    ok=False, error_key="err.http", raw=str(exc)))
        if on_batch is not None:
            try:
                on_batch(len(results))
            except Exception:
                logger.exception("on_batch callback raised")
        if pending and deadline - time.monotonic() <= 0:
            # Budget exhausted: cancel whatever has not started and report
            # the still-running runs as timeouts.
            n_timed_out = len(pending)
            for unf in pending:
                unf.cancel()
            pending.clear()
            for _ in range(n_timed_out):
                results.append(ExtractResult(
                    ok=False, error_key="err.timeout",
                    error_body=f"per-future timeout after {budget:.0f}s",
                ))
            break
        if pending and should_cancel is not None and should_cancel():
            for unf in pending:
                unf.cancel()
            return results, True
    return results, False
