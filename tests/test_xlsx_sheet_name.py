r"""_xlsx_sheet_name had a second, byte-based truncation that contradicted its
own docstring, and the two disagreed exactly where it mattered.

The docstring said the 31-character OpenPyXL limit is "per code point, not per
UTF-8 byte" and that Python's code-point slicing is why no extra pass is
needed. The code then applied one anyway, cutting to a 93-UTF-8-byte budget
("31 * 3 bytes max per CJK") whenever the cleaned name exceeded it.

93 bytes is the width of 31 CJK characters, so it looks like a safe upper
bound. It is not: a code point can be up to 4 bytes in UTF-8, so a legal
31-emoji title (124 bytes) was silently cut to 23 characters. The function was
shortening exactly the names it was written to protect, on inputs its own
documentation says are fine.

Two independent measurements before removing it, because "it looks redundant"
is not a reason to delete a guard:

  * Every title the exporter can actually emit was enumerated through
    get_configs_for_result over one payload per mode: 12 distinct title_keys,
    all 2-23 code points and 6-31 bytes. The branch never fired in production,
    so removing it changes no shipped output.
  * openpyxl -- the arbiter, since the result goes straight into
    wb.create_sheet(title=...) -- gives an identical verdict with and without
    the budget for every adversarial shape, including 31 emoji. (openpyxl only
    *warns* past 31 characters; it does not raise, which is why the earlier
    adversarial sweep found zero gaps here.)

What is asserted below is the contract, not the absence of a particular line:
the result is always at most 31 code points, never contains a forbidden
character, is never empty, and is the FULL title whenever the title is already
within the limit -- including titles that are long in bytes but legal in
code points.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import exporter  # noqa: E402
from rca_core.exporter import _xlsx_sheet_name  # noqa: E402
from rca_core.i18n import TRANSLATIONS  # noqa: E402

FORBIDDEN = set("\\/*?[]:")
EMOJI = "\U0001f642"   # 1 code point, 4 UTF-8 bytes


# --- the two rules the docstring names ---------------------------------


@pytest.mark.parametrize("title", [
    "Sections",
    "A" * 31,
    "A" * 32,
    "A" * 500,
    "a/b\\c*d?e[f]g:h",
    "///",
    "",
    None,
    "'quoted'",
    "History",
    "   ",
    "Sec\ntions",
    "Sec\ttions",
    "a\x00b",
    EMOJI * 31,
    EMOJI * 40,
    "测" * 31,
    "测" * 40,
    "é" * 31,
    "a" * 20 + EMOJI * 20,
    "مقاطع" * 10,
])
def test_result_is_always_a_legal_sheet_title(title):
    out = _xlsx_sheet_name(title)
    assert isinstance(out, str)
    assert out, "sheet title must never be empty"
    assert len(out) <= 31, f"{len(out)} code points: {out!r}"
    assert not (set(out) & FORBIDDEN), f"forbidden character survived: {out!r}"


# --- the rule the removed branch broke ---------------------------------


@pytest.mark.parametrize("title,expected_len", [
    (EMOJI * 31, 31),   # 124 bytes, 31 code points -- legal, must be kept whole
    (EMOJI * 40, 31),   # truncated to exactly the code-point limit, no further
    ("测" * 31, 31),    # 93 bytes exactly: the old budget's boundary
    ("测" * 40, 31),
    (EMOJI * 23, 23),   # was the old budget's emoji ceiling
    ("测" * 30, 30),    # under both limits: untouched either way
])
def test_length_is_measured_in_code_points_not_bytes(title, expected_len):
    """The byte budget is gone: a title legal in code points keeps every
    character, however wide it is in UTF-8."""
    out = _xlsx_sheet_name(title)
    assert len(out) == expected_len, (
        f"expected {expected_len} code points, got {len(out)} "
        f"(title was {len(title)} cp / {len(title.encode('utf-8'))} bytes)"
    )


def test_a_legal_title_is_returned_unchanged():
    """The strongest statement of the same thing, and the one that would have
    caught the old branch immediately.

    "Legal" means both things at once -- already within 31 code points AND
    free of the forbidden characters. A title that trips either rule is
    supposed to change, so it does not belong in this list; the length rule is
    covered by test_length_is_measured_in_code_points_not_bytes and the
    character rule by test_result_is_always_a_legal_sheet_title.
    """
    for title in ("测" * 31, EMOJI * 31, "Abundant species ranges", "站点 (Sites)"):
        assert len(title) <= 31, f"test fixture is not within the limit: {title!r}"
        assert not (set(title) & FORBIDDEN), f"fixture has a forbidden char: {title!r}"
        assert _xlsx_sheet_name(title) == title, (
            f"a title already within the 31-code-point limit was altered: "
            f"{_xlsx_sheet_name(title)!r} != {title!r}"
        )


# --- the invariant that makes the removal safe: no shipped title changes --


def _resolve(key: str) -> str:
    """Same translation resolution to_xlsx uses when `translate` is not given."""
    for lang in ("zh", "en"):
        table = TRANSLATIONS.get(lang) or {}
        if key in table:
            return table[key]
    return key


EMITTED_PAYLOADS = [
    {"schema": "range_chart", "mode": "range_chart",
     "sections": [{"id": "s1", "name": "Sec", "age_range": "1-2 Ma",
                   "species": [{"taxon": "T", "range": "1-2 Ma"}]}]},
    {"schema": "columnar", "mode": "columnar",
     "sections": [{"id": "s1", "name": "Sec",
                   "lithology": [{"pattern": "sand", "meaning": "ss"}]}]},
    {"schema": "zonation", "mode": "zonation",
     "zones": [{"name": "Z", "section": "S", "age": "1 Ma",
                "thickness_m": 1.0}]},
    {"schema": "abundance", "mode": "abundance",
     "sites": [{"name": "Site", "location": "x", "age_range": "1 Ma"}],
     "abundances": [{"taxon": "T", "site": "Site", "abundance": 1}]},
    {"schema": "paleomap", "mode": "paleomap",
     "continents": [{"name": "C", "type": "continent",
                     "coordinates": "1,1"}]},
    {"schema": "phylogenetic_tree", "mode": "phylogenetic_tree",
     "nodes": [{"id": "n1", "parent": None, "name": "root"}]},
]


def _emitted_titles():
    titles = set()
    for payload in EMITTED_PAYLOADS:
        for cfg in exporter.get_configs_for_result(payload):
            key = cfg.get("title_key") or cfg.get("id")
            if key:
                titles.add(key)
    return titles


def _sanitise_31cp_only(title: str) -> str:
    """The function's rules without the removed byte budget.

    Comparing against this -- rather than against the raw title -- is the
    precise claim. `sec.sites` resolves to "站点 / 岩心 (Sites)", whose `/` is
    a forbidden character, so the shipped sheet name is NOT the raw title and
    asserting that it was would be asserting a bug.
    """
    cleaned = "".join("_" if c in "\\/*?[]:" else c for c in (title or ""))
    cleaned = cleaned or "Sheet"
    return cleaned[:31]


def test_the_removal_changes_no_shipped_sheet_title():
    """Pins the claim that made the removal safe.

    Every title the exporter can emit is far under 31 code points, so the byte
    budget was unreachable in production. This test is the standing proof: the
    shipped sheet name is byte-for-byte what the 31-code-point rule alone
    produces, and a title that would have tripped the budget fails loudly
    instead of being quietly shortened.
    """
    titles = _emitted_titles()
    assert titles, "no titles emitted -- the probe is not looking at anything"
    for key in sorted(titles):
        resolved = _resolve(key)
        out = _xlsx_sheet_name(resolved)
        assert out == _sanitise_31cp_only(resolved), (
            f"shipped sheet title for {key!r} no longer matches the "
            f"31-code-point rule: {out!r}"
        )
        assert len(out.encode("utf-8")) <= 93, (
            f"{key!r} is {len(out.encode('utf-8'))} bytes -- past the old "
            f"budget, so this test would have caught the branch firing"
        )
