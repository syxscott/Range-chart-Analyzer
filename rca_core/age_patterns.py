"""The ONE definition of the explicit-age regexes, in source form.

AUDIT-2026-10-02 [item 10.2]. These two patterns were copy-pasted, byte for
byte, into four places:

    rca_core/standards/ics.py     _EXPLICIT_MA_PATTERN / _EXPLICIT_MA_RANGE_PATTERN
    rca_core/quality.py           _AGE_UNIT_PATTERN
    rca_core/standards/pbdb.py    _AGE_VALUE_WITH_UNIT / _AGE_RANGE_WITH_UNIT
    js/quality.js                 _AGE_UNIT_RE / _AGE_RANGE_RE   (the browser mirror)

That is four copies of a decision, which is four chances to fix one of them.
It already happened: the first bug this round was fixed in ``ics.py`` only, and
the same wrong guard was still live in ``quality.py`` (which the desktop's
quality score reads through) and in ``pbdb.py`` (which is the PBDB writer) --
so after that fix the desktop answered 260 Ma for ``深度260 Ma - 250 Ma`` from
one module and 250 Ma from another, on the same label, in the same run.

WHY THE CHARACTER CLASSES ARE WRITTEN OUT
------------------------------------------
The guard exists to stop ``"Madison 3"`` being read as an age -- that is, to
reject digits that continue an ASCII identifier (``a260``, ``_260``).

The JavaScript mirror cannot use a lookbehind: it is a parse-time SyntaxError
on the Safari / WebView versions UI-REVIEW-2026-09-22 had to survive, so it
spells the same guard as ``(?:^|[^\\w.])``. Those two spellings are equivalent
ONLY given the same notion of "word character", and they were not the same:
Python's ``\\w`` and ``\\d`` are Unicode-aware on str patterns, while
ECMAScript's are ``[A-Za-z0-9_]`` and ``[0-9]`` without the ``u`` flag. So
``\\w`` is written out as ``[A-Za-z0-9_]`` and ``\\d`` as ``[0-9]`` here.

The direction is not a coin toss. A CJK glyph does not continue an ASCII
identifier, so ASCII is the correct notion of "word character" for this guard
and Python was the side that was wrong. Measured on the ``age_bound`` parity
group, with the Unicode-aware spelling in place:

    "图260 Ma" / "深度260 Ma" / "図260 Ma"
        python: no match at all  ->  (None, None)
        js:     match            ->  {"name": "Capitanian", "ma": 260}
    "深度260 Ma - 250 Ma", prefer="older"
        python: ma=250   <-- the 260 endpoint was LOST
        js:     ma=260

The second shape is the serious one: the guard dropped the OLDER endpoint, so
``prefer="older"`` answered with the YOUNGER age, inverting the sense of a
column that feeds FAD/LAD in the DwC and PBDB exports.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No ``re.ASCII`` on the patterns. That flag would also narrow ``\\s`` to
``[ \\t\\n\\r\\f\\v]``, and an ideographic or non-breaking space between the
number and the unit is ordinary in CJK typesetting ("260　Ma"), so it would
trade this divergence for its mirror image. ``\\s`` stays Unicode here; the
residual is that Python's Unicode ``\\s`` also contains \\x1c-\\x1f and \\x85,
which ECMAScript's does not, and neither is reachable in a figure caption.

Callers compile these with their own ``re.IGNORECASE``; the flags are not
duplicated here on purpose, so a caller that needs different flags can have
them without a second source of truth for the pattern text.
"""

from __future__ import annotations

#: One explicit age value carrying its unit: "260 Ma", "255.5Ma", "5 m.y.".
AGE_VALUE_WITH_UNIT_PATTERN = (
    r"(?<![A-Za-z0-9_.])([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)(?![A-Za-z0-9_])"
)

#: The same, written as an interval with a single unit: "259.51-254.14 Ma",
#: "255 to 250 Ma". `\bto\b` keeps the shorthand because both engines agree on
#: it there -- a space, ASCII or CJK, is a non-word character on both sides, so
#: the boundary exists in both.
AGE_RANGE_WITH_UNIT_PATTERN = (
    r"(?<![A-Za-z0-9_.])"
    r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*"
    r"(?:[-–—]|\bto\b)\s*"
    r"([+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)(?![A-Za-z0-9_])"
)
