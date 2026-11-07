"""Mode normalizer: generic_table.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _carry_extras,
    _confidence_clamped,
    _stringify_scalar,
)
from typing import Any
import re

def normalize_generic_table_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Last-resort structural extraction for an unrecognised chart genre.

    This is the DEFAULT for a figure the heuristic and the vision classifier
    could not name. What it deliberately does NOT do is the thing every prior
    unknown-chart path did: fall through to ``normalize_result`` and hand back a
    range chart with invented ``pos_0_999`` values. A fabricated range chart
    survives a skim-read of the output - it has the right keys, plausible
    numbers, and an ``ok=True`` result - so it can be cited without anyone
    noticing it was invented. Refusing is the correct behaviour, and it is what
    the prompt asks the model to do when it cannot read the structure.

    Note the polarity is the OPPOSITE of occurrence_matrix: there a blank cell
    means "absent" and the row is dropped, because absence is recoverable from
    the rows that were NOT emitted. Here the row set is already unknown, so a
    blank cell is only knowable by keeping it - ``cells`` is positional and
    must carry an empty string in that slot rather than shortening the list.
    """
    if not isinstance(parsed, dict):
        return {"columns": [], "rows": [], "legend": [], "notes": "",
                "confidence": 0.0}

    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if isinstance(item, dict) and "cells" in item:
                parsed.setdefault("rows", []).append(item)
            elif isinstance(item, dict):
                parsed.setdefault("rows", []).append(item)

    def s(v: Any) -> str:
        return _stringify_scalar(v)

    warnings: list[str] = []      # FIX-2026-10-17 (P1-4)
    out: dict[str, Any] = {
        "columns": [],
        "rows": [],
        "legend": [],
        "notes": s(parsed.get("notes") or "").strip(),
        "confidence": 0.0,
    }

    # FIX-2026-10-17 (P1-4): a column whose HEADER could not be read is kept
    # as a position, not dropped. The loop used to `continue` on it, which
    # compacted ``out["columns"]`` — and every row's ``cells`` is positional,
    # so ``width = len(out["columns"])`` then shrank with it. The docstring
    # below this block spells out the rule the code broke:
    # "``cells`` is positional and must carry an empty string in that slot
    # rather than shortening the list". Measured on
    # ``columns=[A, "", C]`` / ``cells=["a1","b1","c1"]``:
    #
    #     before: columns [A, C]           cells ["a1", "b1"]
    #             -> b1 filed under column C, and c1 gone entirely
    #     after:  columns [A, "", C]        cells ["a1", "b1", "c1"]
    #
    # That is not a missing column, it is a MISLABELLED one plus silent data
    # loss, and it is invisible downstream because nothing warns about it.
    # The blank header is announced instead, so the reader knows which column
    # they are looking at.
    raw_columns = [c for c in (parsed.get("columns") or [])
                   if isinstance(c, dict)]
    for i, col in enumerate(raw_columns):
        label = s(col.get("label") or col.get("name") or "").strip()
        try:
            index = int(col.get("index"))
        except (TypeError, ValueError):
            index = i
        if not label:
            warnings.append(
                f"column {index} has no readable header; kept at its position "
                f"so the row cells stay aligned with the figure")
        out["columns"].append({"index": index, "label": label})

    width = len(out["columns"])
    for i, row in enumerate(parsed.get("rows") or []):
        if not isinstance(row, dict):
            continue
        cells_in = row.get("cells")
        if not isinstance(cells_in, list):
            cells_in = []
        # Positional contract: pad to the column count so the row's cells[i]
        # always addresses column i. A short list would silently shift every
        # later cell one place left.
        cells = [s(c) for c in cells_in][:width]
        cells.extend([""] * (width - len(cells)))
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            index = i
        out["rows"].append({
            "index": index,
            "label": s(row.get("label") or "").strip(),
            "cells": cells,
        })

    for entry in parsed.get("legend") or []:
        if not isinstance(entry, dict):
            continue
        code = s(entry.get("code") or "").strip()
        if not code:
            continue
        out["legend"].append({
            "code": code,
            "class": s(entry.get("class") or entry.get("meaning") or "").strip(),
        })

    conf = parsed.get("confidence")
    try:
        out["confidence"] = _confidence_clamped(float(conf))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    _carry_extras(parsed, ("columns", "rows", "legend", "notes",
                           "confidence", "_warnings", "warnings"), out)
    if warnings:
        # FIX-2026-10-17 (P1-4 + P3-4): ``_warnings`` like every other mode.
        out["_warnings"] = warnings
    return out


