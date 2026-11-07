"""The one convention for attaching a ``_warning`` flag to a result row.

ARCH-REVIEW-2026-11-02 A9 / P3-13. This helper used to be a PRIVATE symbol
of the 7,000-line rca_core/extractor.py, imported by rca_core/grounding.py
via a function-local ``from .extractor import _add_row_flag``. That created
a cross-module dependency on a private name (a name that by convention does
not promise stability), and forced ``grounding`` -- a small name-checking
module -- to pull in the whole extractor (PIL / llm / prompt) just to append
a string to a dict.

It now lives in its own zero-dependency module. ``extractor.py`` re-exports
it under the old private name so all of its internal call sites and any test
that pinned the shape keep working; ``grounding.py`` imports the public name
from here and no longer depends on ``extractor`` at all.

Shape contract (do not "simplify"): ONE flag is a string, SEVERAL flags are
a list. aggregate._add_row_warning and every JS consumer read both shapes.
"""
from __future__ import annotations

from typing import Any

__all__ = ["add_row_flag"]


def add_row_flag(row: dict[str, Any], tag: str) -> None:
    """Attach a ``_warning`` to a row, preserving the two-shape convention."""
    existing = row.get("_warning")
    if existing in (None, ""):
        row["_warning"] = tag
        return
    flags = list(existing) if isinstance(existing, list) else [existing]
    if tag not in flags:
        flags.append(tag)
    row["_warning"] = flags[0] if len(flags) == 1 else flags
