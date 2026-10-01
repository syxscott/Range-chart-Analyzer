"""One definition of the explicit-age regex, and no consumer invents its own.

AUDIT-2026-10-02 [item 10.2]

The two explicit-age patterns were copy-pasted byte for byte into four places
-- standards/ics.py, quality.py, standards/pbdb.py, and the browser mirror
js/quality.js. That is four copies of one decision. It already cost a bug:
the CJK-prefix defect (a CJK glyph is a ``\\w`` for Python, so ``(?<![\\w.])``
refused to match "图260 Ma" and, in a range, silently dropped the OLDER
endpoint so ``prefer="older"`` answered with the YOUNGER age) was fixed in
ics.py alone, leaving the same wrong guard live in

  * quality.py -- which the desktop's quality score reads through, and
  * standards/pbdb.py -- the Paleobiology Database writer.

So immediately after that "fix" the desktop answered 260 Ma for
"深度260 Ma - 250 Ma" from ics.py and 250 Ma from pbdb.py, on the same label,
in the same run. Four copies, one fix applied to one of them, and the tree was
left internally inconsistent.

ics.py IS DELIBERATELY STILL A COPY, and cannot be otherwise.
scripts/update_ics.py::validate_loads_in_ics_module validates a candidate ICS
payload by copying THIS FILE's text alone into a throwaway tree and executing
it there (tests/test_update_ics_2026_09_22.py::_load_ics_copy reproduces it).
That tree has no parent package, so a relative import out of the package fails
with "attempted relative import with no known parent package" and --write
refuses the promotion for entirely the wrong reason. That gate is a PRODUCTION
path, not a test quirk, and the refactor that broke it broke 10 tests.

So the sharing is asymmetric by necessity: quality.py and pbdb.py import the
shared text from rca_core.age_patterns, ics.py carries the same text, and
``test_ics_has_no_cross_package_import`` turns the constraint into an
assertion so the next refactor fails here first, with a readable message,
instead of ten failures in an unrelated suite.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from rca_core import age_patterns
from rca_core.quality import _AGE_UNIT_PATTERN
from rca_core.standards import ics, pbdb

_ICS_PATH = Path(ics.__file__)


def test_ics_has_no_cross_package_import():
    """Enforce the constraint that makes ics.py a copy rather than a consumer.

    update_ics.py executes this file's TEXT in a throwaway tree with no parent
    package, so any `from ..x import y` / `from rca_core.x import y` here
    breaks the promotion gate. Asserted rather than only documented, because
    the alternative -- finding out from ten unrelated test failures -- is how
    it was found the first time.
    """
    tree = ast.parse(_ICS_PATH.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 1:
                offenders.append("." * node.level + (node.module or ""))
            elif node.level == 0 and (node.module or "").startswith("rca_core"):
                offenders.append(node.module or "")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("rca_core"):
                    offenders.append(alias.name)
    assert not offenders, (
        "standards/ics.py must stay loadable with no package context; remove "
        "these imports and inline the value instead: %r" % (offenders,)
    )


def test_ics_compiles_the_shared_text():
    """ics.py holds a copy -- see the module docstring -- so this is the
    assertion that keeps the copy and the shared text from drifting apart."""
    assert ics._EXPLICIT_MA_PATTERN.pattern == (
        age_patterns.AGE_VALUE_WITH_UNIT_PATTERN)
    assert ics._EXPLICIT_MA_RANGE_PATTERN.pattern == (
        age_patterns.AGE_RANGE_WITH_UNIT_PATTERN)


def test_quality_compiles_the_shared_text():
    assert _AGE_UNIT_PATTERN.pattern == age_patterns.AGE_VALUE_WITH_UNIT_PATTERN


def test_pbdb_compiles_the_shared_text():
    assert pbdb._AGE_VALUE_WITH_UNIT.pattern == (
        age_patterns.AGE_VALUE_WITH_UNIT_PATTERN)
    assert pbdb._AGE_RANGE_WITH_UNIT.pattern == (
        age_patterns.AGE_RANGE_WITH_UNIT_PATTERN)


@pytest.mark.parametrize("compiled", [
    ics._EXPLICIT_MA_PATTERN,
    ics._EXPLICIT_MA_RANGE_PATTERN,
    _AGE_UNIT_PATTERN,
    pbdb._AGE_VALUE_WITH_UNIT,
    pbdb._AGE_RANGE_WITH_UNIT,
])
def test_every_copy_carries_the_ignorecase_flag(compiled):
    """The flags are deliberately NOT shared -- each caller compiles the shared
    text with its own -- but all five copies need re.IGNORECASE, and losing it
    would make "260 ma" and "260 Ma" behave differently."""
    assert compiled.flags & re.IGNORECASE


@pytest.mark.parametrize("compiled", [
    ics._EXPLICIT_MA_PATTERN,
    _AGE_UNIT_PATTERN,
    pbdb._AGE_VALUE_WITH_UNIT,
])
def test_no_consumer_kept_a_unicode_aware_word_class(compiled):
    """`\\w` and `\\d` on a Python str pattern are Unicode-aware; the browser's
    are not. That is the whole defect, so its fingerprint must be absent --
    a grep-level guard, because the bug was invisible to every behavioural
    test until a CJK case was added to the corpus."""
    assert "\\w" not in compiled.pattern
    assert "\\d" not in compiled.pattern
    assert "\\b" not in compiled.pattern
    # ...and the ASCII spelling must be the one that is actually there.
    assert "(?<![A-Za-z0-9_.])" in compiled.pattern
    assert "(?![A-Za-z0-9_])" in compiled.pattern
    assert "[0-9]+" in compiled.pattern


# ---------------------------------------------------------------------------
# Behaviour, so the shared text cannot be "simplified" back into the bug.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [
    ics._EXPLICIT_MA_PATTERN, _AGE_UNIT_PATTERN, pbdb._AGE_VALUE_WITH_UNIT,
])
def test_a_cjk_character_before_the_age_does_not_suppress_it(pattern):
    """The regression, on all three consumers at once. A CJK glyph is a word
    character for Python, so the old `(?<![\\w.])` guard refused to match here
    -- and for a Chinese/Japanese product "图260 Ma" is what a figure label
    looks like, not a contrived input."""
    for text in ("图260 Ma", "深度260 Ma", "図260 Ma", "年龄260 Ma"):
        m = pattern.search(text)
        assert m is not None, (pattern.pattern, text)
        assert m.group(1) == "260", (pattern.pattern, text)


@pytest.mark.parametrize("pattern", [
    ics._EXPLICIT_MA_PATTERN, _AGE_UNIT_PATTERN, pbdb._AGE_VALUE_WITH_UNIT,
])
def test_the_guard_still_rejects_an_ascii_identifier(pattern):
    """The reverse control. The guard exists so "Madison 3" stays a bed label;
    widening it past ASCII would have traded this divergence for its mirror
    image."""
    for text in ("a260 Ma", "A260 Ma", "_260 Ma", "K3 Ma", "Stage260 Ma"):
        assert pattern.search(text) is None, (pattern.pattern, text)


@pytest.mark.parametrize("pattern", [
    ics._EXPLICIT_MA_PATTERN, _AGE_UNIT_PATTERN, pbdb._AGE_VALUE_WITH_UNIT,
])
def test_ideographic_space_between_number_and_unit_still_matches(pattern):
    """Why the shared text does NOT use `re.ASCII`: that flag would narrow
    `\\s` to [ \\t\\n\\r\\f\\v] and an ideographic or non-breaking space is
    ordinary in CJK typesetting, so it would have moved the divergence rather
    than closed it."""
    for text in ("260　Ma", "260 Ma", "260 Ma"):
        m = pattern.search(text)
        assert m is not None, (pattern.pattern, repr(text))


@pytest.mark.parametrize("pattern", [
    ics._EXPLICIT_MA_PATTERN, _AGE_UNIT_PATTERN, pbdb._AGE_VALUE_WITH_UNIT,
])
def test_both_endpoints_of_a_split_label_survive_the_cjk_prefix(pattern):
    """The shape that inverts meaning, on the SINGLE-value pattern.

    "深度260 Ma - 250 Ma" is not a range-pattern shape -- the unit sits between
    the two numbers, so it resolves as two single values, and that is the path
    the parity harness measured. With the Unicode-aware guard the 260 was
    dropped and the one surviving value was the YOUNGER one, so a caller asking
    for the older end got the younger age. Two matches, not one.
    """
    found = [m.group(1) for m in pattern.finditer("深度260 Ma - 250 Ma")]
    assert found == ["260", "250"], (pattern.pattern, found)


@pytest.mark.parametrize("pattern", [
    ics._EXPLICIT_MA_RANGE_PATTERN, pbdb._AGE_RANGE_WITH_UNIT,
])
def test_the_range_keeps_both_endpoints_across_a_cjk_prefix(pattern):
    """Same regression on the range pattern's own shape: the unit at the end,
    a CJK glyph in front of the first number."""
    m = pattern.search("深度260-250 Ma")
    assert m is not None
    assert (m.group(1), m.group(2)) == ("260", "250")


@pytest.mark.parametrize("pattern", [
    ics._EXPLICIT_MA_RANGE_PATTERN, pbdb._AGE_RANGE_WITH_UNIT,
])
def test_range_forms_still_match(pattern):
    """The forms the RANGE pattern owns. "260 Ma - 250 Ma" is deliberately
    absent: the unit sits between the numbers, so it is two single-value
    matches, not one range match -- see the test above."""
    for text in ("259.51-254.14 Ma", "260-250 Ma", "255 to 250 Ma",
                 "255.5 to 250 Ma", "260–250 Ma", "260—250 Ma"):
        m = pattern.search(text)
        assert m is not None, (pattern.pattern, text)
        assert m.group(1) and m.group(2), (pattern.pattern, text)
