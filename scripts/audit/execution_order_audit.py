"""Run the suite in REVERSE collection order, to measure order-dependence.

NOT IN CI, deliberately -- this is an audit, not a gate. Like the other
root-level audit tools it is opt-in:

    python -m pytest tests/ tests_core.py -q -p execution_order_audit

WHY
---
CI collects in one fixed order, so a suite that only passes in that order looks
perfectly healthy there and breaks the moment a developer runs a subset, an IDE
reorders, a file is renamed, or a test is isolated with `pytest path::test`.
Shared module state is the usual cause, and it is invisible by construction:
the build only ever runs the one order that hides it.

"I did not find any order dependence" is a much weaker statement than a
measurement, so this reorders and lets the suite report for itself.

THE ANSWER TODAY
----------------
Green. The suite passes in reverse collection order, so no test depends on
another having run first.

The answer is worth keeping as a tool because it is the one property of a test
suite that silently stops being true the moment someone adds a module-level
cache -- and the first person to find out would be a developer running one file,
not CI.

If it ever goes red, the fix is to remove the shared state (module-level
mutable globals, a singleton left configured, an environment variable set
without restoring it). Do NOT pin the order: that converts a real coupling into
an invisible one.
"""
from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def pytest_collection_modifyitems(session, config, items):
    items.reverse()


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    terminalreporter.write_line(
        "execution_order_audit: collection order was REVERSED for this run. "
        "Failures here mean shared state between tests, not a bad test.")
