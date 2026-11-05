"""The one wall-clock budget for an extraction run.

WHY THIS IS A SEPARATE MODULE
=============================
``extraction_wall_clock_budget_sec`` used to live in ``extractor.py`` and
``matrix.py`` imported it from there, while ``extractor.py`` imports
``extract_matrix_two_stage`` from ``matrix.py`` (``extractor.py:5706``).
That is a real cycle, held together only by function-local imports --
invisible to a column-0 grep and to import-time failure, but it welds the
two modules together and makes either one impossible to move.

Moving a pure function with no dependencies into its own module turns both
edges into one-way ``-> budget`` edges. ARCH-REVIEW-2026-11-02, P3-11.

The server has its OWN copy (``server.py:151``) that reads module globals so
tests can shrink them; that split is deliberate and is NOT consolidated here.
"""
from __future__ import annotations

#: Worst-case outbound attempts in one extraction slot.
SINGLE_RUN_MAX_ATTEMPTS = 8
#: Slack added on top of the attempt budget.
SINGLE_RUN_DEADLINE_SLACK_SEC = 15
#: Extra slack for a multi-run batch (slots run concurrently, so this is
#: ONE slot's budget, not N of them added up).
MULTI_RUN_TIMEOUT_SLACK_SEC = 10


def extraction_wall_clock_budget_sec(timeout_sec: int) -> int:
    """Total wall-clock seconds ONE extraction may spend, for every caller.

    ``timeout_sec`` per outbound attempt, times the worst-case number of
    attempts, plus slack. The server's single-run and multi-run branches
    both use this value, so a batch's slots -- which run concurrently -- are
    bounded by ONE slot's budget and not by N of them added up.
    """
    return (timeout_sec * SINGLE_RUN_MAX_ATTEMPTS
            + SINGLE_RUN_DEADLINE_SLACK_SEC
            + MULTI_RUN_TIMEOUT_SLACK_SEC)


__all__ = ["SINGLE_RUN_MAX_ATTEMPTS", "SINGLE_RUN_DEADLINE_SLACK_SEC",
           "MULTI_RUN_TIMEOUT_SLACK_SEC", "extraction_wall_clock_budget_sec"]
