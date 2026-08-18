"""Shared bed-label parser for evaluation metrics and exporters.

The codebase previously had TWO independent ``_parse_bed`` functions:

* ``rca_core.exporter._parse_bed`` — returns ``{bed_num, bed_sub, raw}``
  or None, accepting both ``"Bed 23c"`` and bare ``"23c"`` forms.
* ``rca_core.eval_metrics._parse_bed`` — returns an ``int`` (first
  integer via ``re.search(r\"-?\\d+\")``), DROPPING subscripts like
  ``\"23c\"``.

This split caused the quality/accuracy scorer and the exporter to
disagree on what \"Bed 23c\" means — REVIEW-2026-07-25 flagged this as
a HIGH-severity data-integrity bug: a predicted Bed 23c and ground
truth Bed 23d would both score as integer 23 → false-positive
accuracy.

M-1 fix: route both call sites through this single module so the
suite-of-evaluators and the file-export pipeline always agree on bed
semantics. ``parse_bed`` returns a dict (the exporter's richer
shape); ``parse_bed_int`` is a thin convenience wrapper that returns
just the integer (matches the old ``eval_metrics._parse_bed``
contract).
"""

from __future__ import annotations

import re
from typing import Any, Optional


_BED_FULL_RE = re.compile(r"^Bed\s*(\d+)\s*([a-zA-Z]*)", re.IGNORECASE)
_BARE_NUM_RE = re.compile(r"^(\d+)\s*([a-zA-Z]*)")


def parse_bed(value: Any) -> Optional[dict[str, Any]]:
    """Parse a Bed identifier string.

    Returns a dict ``{bed_num: int, bed_sub: str, raw: str}`` on
    success, or ``None`` for empty / unrecognisable input.

    Accepts both forms:
      * ``"Bed 23c"``   → ``{bed_num: 23, bed_sub: "c", raw: "Bed 23c"}``
      * ``"23c"``       → ``{bed_num: 23, bed_sub: "c", raw: "23c"}``
      * ``"Bed 23"``    → ``{bed_num: 23, bed_sub: "", raw: "Bed 23"}``
      * ``"Bed"``       → ``None``
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _BED_FULL_RE.match(s)
    if m:
        return {"bed_num": int(m.group(1)), "bed_sub": m.group(2).lower(), "raw": s}
    m = _BARE_NUM_RE.match(s)
    if m:
        return {"bed_num": int(m.group(1)), "bed_sub": m.group(2).lower(), "raw": s}
    return None


def parse_bed_int(value: Any) -> Optional[int]:
    """Convenience wrapper that returns just the bed number as int.

    Mirrors the old ``eval_metrics._parse_bed`` contract. Bed
    subscripts (e.g. ``\"23c\"``) are NOT preserved — callers that need
    them should call ``parse_bed`` instead.
    """
    info = parse_bed(value)
    if info is None:
        return None
    return int(info["bed_num"])


__all__ = ["parse_bed", "parse_bed_int"]
