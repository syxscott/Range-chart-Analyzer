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
# AUDIT-2026-09-29: the trailing ``a`` in the alternation is DEAD, and that is
# deliberate rather than an oversight. The only call site guards on
# ``len(sub) > 1`` before consulting this table (single letters are decided
# earlier, by the "m" check and then by _SUB_RE), so "a" can never match here
# and "27a" stays a bed label -- which is what the _sub_is_bed_label docstring
# says and what real range charts mean by it. It is kept in the list so the
# table reads as a complete inventory of the suffixes this domain uses, and so
# that lifting the len>1 guard does not silently change "a" from bed to unit.
# The alternative -- deleting the entry -- leaves the next reader wondering
# why annus is missing from a table of time units.
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


def _has_stray_residue(s: str, end: int) -> bool:
    """True when a match stopped before the end of the label.

    REVIEW-2026-09-10 allowed this for the BARE form only, so the two spellings
    of the same bed disagreed:
        "23 to 25"   -> None      "Bed 23 to 25" -> bed 23  (!)
        "12.5"       -> None      "Bed 12.5"     -> bed 12  (!)
    A range / a decimal age read as a single bed number is a wrong scientific
    value, and the "Bed"-prefixed form is the one the models emit MOST, so the
    check now covers both branches. A trailing parenthesis / bracket is still
    tolerated ("Bed 23 (Fig. 4)" is bed 23 with a citation).
    """
    rest = s[end:].strip()
    return bool(rest) and not rest.startswith(("(", "["))


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

    REVIEW-2026-09-20: the same rejection now applies to the ``Bed``-prefixed
    spelling — ``"Bed 23 to 25"`` and ``"Bed 12.5"`` are a range and a decimal,
    not beds 23 and 12.
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
        if _has_stray_residue(s, m.end()):
            return None
        return {"bed_num": int(m.group(1)), "bed_sub": sub.lower(), "raw": s}
    m = _BARE_NUM_RE.match(s)
    if m:
        sub = m.group(2)
        if not _sub_is_bed_label(sub, had_bed_word=False):
            return None
        # A bare number followed by more content ("23-25", "23 to 25") is a
        # range or a sentence, not a single bed.
        if _has_stray_residue(s, m.end()):
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


# ---------------------------------------------------------------------------
# Axis-label ordinals (AUDIT-2026-10-15)
# ---------------------------------------------------------------------------
#
# `parse_bed` above is deliberately strict: it returns None for anything that
# is not a bare or "Bed"-prefixed number, because a misread age or thickness
# silently becomes a wrong bed number. That strictness has a cost, measured on
# a real published figure (Diaz & Velez 2018, Fig. 5) whose samples are
# labelled `A1 (607 m)` .. `A21 (602 m)`:
#
#   parse_bed("A1 (607 m)") -> None     parse_bed("Sample 3") -> None
#   parse_bed("87 B1 4C")   -> None     parse_bed("607 m")     -> None
#
# The exporter's ONLY export-blocking invariant, `range_base_le_range_top`,
# parses through `parse_bed`, so on that figure it never once reached its own
# comparison: a result with 21 inverted range endpoints validated as
# ok=True / 0 blocking / 0 warnings, while the control "Bed 5" -> "Bed 1"
# blocked correctly. The guard was inert for the label style real charts use.
#
# `label_ordinal` is the deliberately NARROW companion: it never claims to be a
# bed parser and never guesses one. It answers a single question — "does this
# axis label carry its own position number?" — so two endpoints can be
# cross-checked against the index the model assigned them. None is the common,
# expected answer and callers MUST read it as "cannot check", never as
# "not inverted".

_LABEL_TRAILING_UNIT_RE = re.compile(
    r"(?:ma|myr|ka|kyr|m\b|cm|mm|ft|in)\s*$", re.IGNORECASE)
_ANY_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_BARE_DECIMAL_RE = re.compile(r"^\d+\.\d+$")
_LETTER_PREFIX_RE = re.compile(r"^[A-Za-z]{1,4}[-_. ]?\d")

# RCA-2026-10-17 (P2-2): an en-dash/em-dash between two NUMBERS is a range,
# not a position label. "23-25" used to fall through to the "last number" rule
# and answered 25 — the top of the interval — so a two-endpoint comparison asked
# "is 25 above 23?" and answered yes, manufacturing an inversion out of a
# single cell. Deliberately anchored on an ASCII HYPHEN only: a hyphen is
# ambiguous in this domain ("B1 4C", "zone-3" are labels; "23-25" is a range),
# but an en/em dash always separates the ends of an interval. The two-spelling
# ASCII case is handled separately by _is_range_label, which knows about the
# "Bed" / "A" markers a chart prints.
_DASHED_NUMERIC_RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[–—]\s*\d+(?:\.\d+)?")

# ...and the ASCII-hyphen range. AUDIT-2026-10-17 (adversarial pass): the
# first version of this pattern let a hyphen-separated LETTER run sit between
# the two numbers, which matched "87 B1-4C" — a composite well/interval
# identifier printed on radiolarian well logs, and one of the two label styles
# the AUDIT-2026-10-15 work was written for. It answered None for it, where
# the pre-fix code answered 4, so the fix silently disabled the exporter's
# `range_base_le_range_top` check for that whole figure class.
#
# A genuine ASCII range has its two numbers adjacent, optionally separated by
# ONE marker word that is the same on both sides or absent ("23-25",
# "Bed 7-11", "Bed 7-Bed 11"). "87 B1-4C" is not that: the "B1" is an
# identifier of its own between the numbers, so the hyphen is a SEPARATOR
# inside one label rather than the middle of an interval. The pattern below
# therefore forbids extra letters on the left side and allows only a marker
# word (not an arbitrary letter run) on the right.
#
# The second pattern is the lettered-sample form ("A1-A5"), which IS a range
# and is written the same way a well identifier is. The two are told apart by
# WHERE the hyphen falls: a range has the SAME prefix on both sides ("A1-A5"),
# while a composite identifier puts a different one in the middle ("87 B1-4C").
_ASCII_RANGE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*-\s*"                       # 23-  /  Bed 7-
    r"(?:bed|beds|sample|specimen)?\s*"             # optional marker
    r"\d+(?:\.\d+)?\b",                             # 25 / 11
    re.IGNORECASE,
)

#: The lettered-sample range ("A1-A5", "B3-B7"): same letter+number on BOTH
#: sides of the hyphen. The shared prefix is what separates it from a
#: composite identifier, where the middle piece differs from the outer one.
_LETTERED_RANGE_RE = re.compile(
    r"\b([A-Za-z])-?(\d+)\s*-\s*\1-?(\d+)\b",       # A1-A5, A1 - A5
)

# RCA-2026-10-17 (P2-2): words that legitimately introduce a sample/bed
# POSITION. Anything else that appears as a word in the label means the number
# belongs to something that is not the sample's place in the section — "zone
# Z1" numbers the ZONE, "Fig. 4" numbers a FIGURE. Reading those as ordinals
# compares two unrelated counters, so `label_ordinal`'s own docstring already
# promised None for "zone Z1" and the implementation simply never honoured it.
#
# Only words of two letters or more are inspected, so single-letter sample
# prefixes ("A1", "B1 4C") are untouched — they are the whole point of the
# letter-prefixed style this function exists to read.
#
# AUDIT-2026-10-17 (adversarial pass): the first version of this set was
# exactly the position words and nothing else, so any OTHER word killed the
# label — including the well and institute identifiers a published log prints
# in the same cell as the sample number:
#
#     "87 JED 8"        -> None   (JED is a well name)   was 8
#     "CAASP-84-20/15"  -> None   (CAASP is an institute) was 15
#
# Both are real radiolarian-figure label styles and both are on the critical
# path for the exporter's endpoint check, so the test is inverted: a label is
# only refused when it carries a word from the KNOWN-semantics list below.
# Anything unrecognised is treated as an identifier — the conservative
# direction, because a missed check reports nothing while a wrong ordinal
# invents an inversion.
_SAMPLE_PREFIX_WORDS = frozenset({
    "sample", "samples", "bed", "beds", "specimen", "specimens",
})

#: Words that assert the number belongs to something OTHER than the sample's
#: position. Kept deliberately short and closed: adding to it makes more real
#: labels unreadable, and an unlisted word is assumed to be an identifier.
_NON_POSITION_WORDS = frozenset({
    "zone", "zones", "zonation", "biozone", "biozones",
    "fig", "figs", "figure", "table", "plate", "plates",
    "section", "sections", "sect",
    "formation", "formations", "fm", "fmts",
    "stage", "stages",
})

_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _has_foreign_semantic_word(s: str) -> bool:
    """True when ``s`` names something other than the sample it numbers.

    Only the CLOSED list of known non-position words counts. An unrecognised
    word is an identifier — a well name, an institute code, an operator — and
    treating those as "the number means something else" made real labels
    unreadable (see ``_NON_POSITION_WORDS``).
    """
    return any(
        w.lower() in _NON_POSITION_WORDS for w in _WORD_RE.findall(s)
    )


def _is_range_label(s: str) -> bool:
    """True when the label prints an INTERVAL rather than one position.

    "23-25", "23 – 25", "Bed 7-Bed 11" and "A1-A5" all name a span; the far
    end is not where the sample sits, so reading it as the sample's ordinal
    turns one cell into a spurious inversion report.
    """
    return bool(
        _DASHED_NUMERIC_RANGE_RE.search(s)
        or _ASCII_RANGE_RE.search(s)
        or _LETTERED_RANGE_RE.search(s)
    )


def label_ordinal(value: Any) -> Optional[int]:
    """The position number a sample/bed label carries, or None.

    Accepts the label styles real range charts print, and only those it can
    read without guessing::

        "A1" / "A21"      -> 1 / 21   letter-prefixed sample
        "Sample 3"       -> 3        word-prefixed
        "87 B1 4C"       -> 4        LAST number: the bed, not the section
        "Bed 23" / "23c" -> 23       what parse_bed already handles
        "1.7"            -> 1        a Ma value: monotonic, usable
        "607 m"          -> None     a DEPTH, not an ordinal
        "1.7 Ma"         -> None     an AGE carries a unit
        "" / "zone Z1"   -> None     nothing to read
        "23-25"          -> None     a RANGE has no single position

    Depth-like values are refused on purpose. ``"607 m"`` is monotonic but its
    unit is spelled out, and a figure may print depth on one axis and a bed
    index on another; comparing across the two would manufacture inversions out
    of nothing. The caller needs "can I check this?", and for a bare
    ``NNN <unit>`` the honest answer is no.

    RCA-2026-10-17 (P2-2): two more shapes are refused for the same reason.
    A label naming a FOREIGN object — "zone Z1" counts zones, "Fig. 4" counts
    figures — carries a number that is not the sample's place in the section, so
    cross-checking it against a model-assigned index compares two unrelated
    counters. And a numeric RANGE ("23-25", "23–25", "23—25") has no single
    position at all: reading its far end answered 25 for the cell above and made
    every such row look inverted. The docstring already promised None for
    "zone Z1"; the implementation now honours it.

    Callers MUST read None as "cannot check", never as "not inverted" — that is
    the whole contract of this function and it is unchanged.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # float() carries NaN/inf, and int(NaN) raises — the same guard
        # parse_bed_int applies. A NaN cell reaches here through the exporter's
        # NaN-sanitisation tests, so this is a live path, not a theoretical one.
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return int(f)
    s = str(value).strip()
    if not s:
        return None

    # Already a bed by the strict parser — take its number, no guessing.
    info = parse_bed(s)
    if info is not None:
        return int(info["bed_num"])

    # A trailing unit means an age or a thickness, never a position label.
    if _LABEL_TRAILING_UNIT_RE.search(s):
        return None
    # RCA-2026-10-17 (P2-2): a foreign semantic word means the number belongs to
    # something other than this sample's position ("zone Z1" -> the zone,
    # "Fig. 4" -> the figure). Answering 1 / 4 there invented an ordinal.
    if _has_foreign_semantic_word(s):
        return None
    # RCA-2026-10-17 (P2-2): a numeric range is not a position. Checked before
    # the "last number" rule, which would otherwise answer the far endpoint and
    # turn every ranged cell into a reported inversion.
    if _is_range_label(s):
        return None
    if _BARE_DECIMAL_RE.match(s):
        return int(float(s))

    # A parenthesised tail is an ANNOTATION, not part of the label. This is the
    # form a measured section prints — "A1 (607 m)" is sample A1 annotated with
    # its depth — so the ordinal is whatever precedes the bracket. Reading the
    # last number instead returned 607 for "A1 (607 m)" and 100 for
    # "S1 (100 ft)", i.e. the depth, which orders the opposite way from the
    # sample index it is being compared against.
    if "(" in s:
        head = s.split("(", 1)[0]
        nums = _ANY_NUM_RE.findall(head)
        return int(float(nums[0])) if nums else None

    nums = _ANY_NUM_RE.findall(s)
    if not nums:
        return None
    # No bracket: take the LAST number, because "87 B1 4C" is section 87 / well
    # 1 / bed 4 and the bed is the position.
    return int(float(nums[-1]))


def ordinal_kind(value: Any) -> Optional[str]:
    """Coarse family of an axis label, so two endpoints are only compared when
    they could plausibly sit on the same axis.

    ``"letter"`` (A1, Sample 3), ``"number"`` (Bed 23, 23c, 4) or None. Lax on
    purpose — its job is to stop an age being compared against a sample index,
    not to police spelling.

    RCA-2026-10-17 (P2-2): a label the strict bed parser already understands
    is a NUMBER kind, full stop. "Bed 23" used to be classified "letter"
    because the ``\\b[A-Za-z]{3,}\\b`` word test matched the word "Bed" and ran
    before any numeric check — so a bed endpoint and a letter-prefixed sample
    endpoint were placed in different families and never cross-checked, and the
    docstring's own example was wrong. "23c" happened to be right only because
    it has no word to match. Running the parser first also keeps "A1" and
    "Sample 3" in the letter family, which is what they are.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if _LABEL_TRAILING_UNIT_RE.search(s):
        return None
    # Known bed spellings first: "Bed 23" / "23c" are numbers, whatever words
    # they happen to contain. "A1" is not accepted by parse_bed, so the
    # letter-prefix test below still owns it.
    if parse_bed(s) is not None:
        return "number"
    if _LETTER_PREFIX_RE.match(s) or re.search(r"\b[A-Za-z]{3,}\b", s):
        return "letter"
    if re.search(r"\d", s):
        return "number"
    return None


def bed_position(value: Any) -> float | None:
    """A bed's position on a continuous scale, subscript included.

    ``23`` -> 23.0, ``23a`` -> 23.019..., ``23z`` -> 23.981..., ``24`` -> 24.0.
    Monotonic in the letter, and strictly inside ``(bed_num, bed_num + 1)``:
    the half-step offset keeps every sub-bed above the bare bed and below the
    next one, so a position can never be confused with a different bed.

    This exists because returning the bare integer is lossy in a way that is
    invisible downstream. ``parse_bed_int("23a") == parse_bed_int("23b")`` is
    true, and code that sorts or compares on that number cannot tell 23a from
    23b -- so it either forgave a real inversion or invented one. Both
    happened: eval_metrics._bed_num returned a flat +0.001 for every letter
    (fixed 2026-10-01), and quality._score_biozone_order still used
    parse_bed_n, whose alphabetical tie-break then decided which of two
    same-bed species was treated as the younger one.

    Half-steps also keep the arithmetic well-conditioned: the old 0.001 was
    not exactly representable, and its binary error reached a reported metric
    (0.0010000000000012221).
    """
    info = parse_bed(value)
    if info is None:
        return None
    sub = (info.get("bed_sub") or "").lower()
    if not sub:
        return float(info["bed_num"])
    first = sub[0]
    if not ("a" <= first <= "z"):
        return float(info["bed_num"])
    return float(info["bed_num"]) + (ord(first) - ord("a") + 0.5) / 26.0


__all__ = ["parse_bed", "parse_bed_int", "bed_position"]
