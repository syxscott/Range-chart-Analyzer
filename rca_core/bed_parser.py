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

# REVIEW-2026-09-10: the trailing ``[a-zA-Z]*`` above swallowed ANY letters,
# so an absolute age or a thickness was indistinguishable from a bed
# subscript and both regexes happily returned a "bed number" for them:
#   "253 Ma"  -> bed 253 (sub "ma")      "0.5 Ma" -> bed 0
#   "23 m"    -> bed 23 (a thickness)    "23-25"  -> bed 23 (range cut)
# Reachable from eval_metrics (range_top / range_base feed parse_bed_int) and
# from the exporter's range_base_le_range_top constraint. A trailing unit is
# therefore an explicit rejection, and a bare-number form only accepts a
# SINGLE-letter subscript (what bed labels use: 23c, 27a).
_UNIT_WORDS_RE = re.compile(
    r"^(ma|myr|mya|m\.y\.|m\.y\.?|ka|kyr|ga|gyr|yr|cm|mm|km|ft|a)$",
    re.IGNORECASE,
)
_SUB_RE = re.compile(r"^[a-ln-z]$", re.IGNORECASE)  # single letters except "m"


def _sub_is_bed_label(sub: str, had_bed_word: bool) -> bool:
    """True when the trailing letters read as a bed subscript, not a unit.

    Single letters are bed subscripts (23a … 23z) EXCEPT "m", which is the
    metre unit ("23 m" is a thickness). Multi-letter suffixes are checked
    against the unit list ("253 Ma", "12 ka"). The lone "a" (annus) is
    deliberately NOT treated as a unit: bed subscripts run a…z and "27a" is
    overwhelmingly a bed label in this domain.
    """
    if not sub:
        return True  # bare number after an explicit "Bed" keyword is a bed
    if sub.lower() == "m":
        return False  # metres
    if len(sub) > 1 and _UNIT_WORDS_RE.match(sub):
        return False
    # A multi-letter suffix is only a label when the "Bed" keyword is present
    # ("Bed 12 top" is unusual; "12 ab" is not a bed), and even then it must
    # look like a continuation of the label rather than a unit.
    if had_bed_word:
        return True
    return bool(_SUB_RE.match(sub))


def parse_bed(value: Any) -> Optional[dict[str, Any]]:
    """Parse a Bed identifier string.

    Returns a dict ``{bed_num: int, bed_sub: str, raw: str}`` on
    success, or ``None`` for empty / unrecognisable input.

    Accepts both forms:
      * ``"Bed 23c"``   → ``{bed_num: 23, bed_sub: "c", raw: "Bed 23c"}``
      * ``"23c"``       → ``{bed_num: 23, bed_sub: "c", raw: "23c"}``
      * ``"Bed 23"``    → ``{bed_num: 23, bed_sub: "", raw: "Bed 23"}``
      * ``"Bed"``       → ``None``

    REVIEW-2026-09-10: returns ``None`` for ages / thicknesses / ranges that
    merely START with a number — ``"253 Ma"``, ``"0.5 Ma"``, ``"23 m"``,
    ``"23-25"`` — instead of silently reporting a wrong bed number.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _BED_FULL_RE.match(s)
    if m:
        sub = m.group(2)
        if not _sub_is_bed_label(sub, had_bed_word=True):
            return None
        return {"bed_num": int(m.group(1)), "bed_sub": sub.lower(), "raw": s}
    m = _BARE_NUM_RE.match(s)
    if m:
        sub = m.group(2)
        if not _sub_is_bed_label(sub, had_bed_word=False):
            return None
        # A bare number followed by more content ("23-25", "23 to 25") is a
        # range or a sentence, not a single bed.
        rest = s[m.end():].strip()
        if rest and not rest.startswith(("(", "[")):
            return None
        return {"bed_num": int(m.group(1)), "bed_sub": sub.lower(), "raw": s}
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
