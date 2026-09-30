r"""Six user-facing strings claimed the tool drops rows it keeps.

merge_results used to REMOVE a recombined (chimera) row. That policy was
reversed on purpose -- AUDIT-2026-09-27 P1 in aggregate.py:

    "this used to REMOVE the row. That was wrong, and the vote tie-break is
     why: ... Raising `runs`, the documented way to make an extraction MORE
     reliable, was deleting taxa. The row is now kept, flagged
     `recombined_consensus`, and carries the ballots."

The wording was not updated with it. All three languages said the rows were
dropped, for both keys -- six strings in rca_core/i18n.py and six more in
js/i18n.js, which keeps its own catalogue:

    quality.chimera_dropped   "Chimera row dropped (...)"
    results.chimera_warning   "... — automatically dropped"

Measured on a construction that produces a real chimera: the merged result
keeps the row, flags it, attaches the ballots, and reports one
chimera_warnings entry -- and then tells the operator it was dropped. In
research use that is worse than silence: someone stops looking for a
disagreement the tool is still holding, or assumes the tool shielded them from
fabrication when the only protection is a flag in the JSON.

The key NAMES still say "dropped", and renaming them would touch the GUI and
the parity fixtures, so the names stay and the discrepancy is noted here
instead. The test asserts the TEXT, which is what a user reads.

Deliberately a plain substring scan: it caught a first draft of this fix that
worded the Japanese as 除外せず ("does NOT exclude"), which is accurate but
puts a removal word in the string and reads ambiguously to anyone skimming.
The wording now uses 保持 so no removal word appears at all.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.aggregate import merge_results  # noqa: E402
from rca_core.i18n import TRANSLATIONS  # noqa: E402

CATALOGUES = {
    "rca_core/i18n.py": pathlib.Path(PROJECT_ROOT) / "rca_core" / "i18n.py",
    "js/i18n.js": pathlib.Path(PROJECT_ROOT) / "js" / "i18n.js",
}

KEYS = ["quality.chimera_dropped", "results.chimera_warning"]

# Words that assert a removal, in every language the catalogue ships.
REMOVAL_WORDS = (
    "drop", "discard", "delet", "remov", "exclud",
    "丢弃", "删除", "丢弃", "排除", "自动丢弃",
    "除外", "削除", "破棄", "除去",
)


def _values(path, key):
    """The three language strings for one key, in file order.

    Both quote styles have to be accepted: the Python catalogue spells its
    entries with DOUBLE quotes and js/i18n.js with single ones, and a pattern
    that only knew one of them reported ZERO strings for the Python side --
    which reads as "the key is missing" rather than "the pattern was wrong".
    """
    text = path.read_text(encoding="utf-8")
    pattern = (rf"['\"]{re.escape(key)}['\"]\s*:\s*"
               r"['\"]([^'\"]*)['\"]")
    return re.findall(pattern, text)


@pytest.mark.parametrize("label,path", CATALOGUES.items())
@pytest.mark.parametrize("key", KEYS)
def test_no_language_claims_a_row_was_dropped(label, path, key):
    values = _values(path, key)
    assert len(values) == 3, (
        f"{label}: {key} appears {len(values)} times, expected one per language"
    )
    for value in values:
        hits = [w for w in REMOVAL_WORDS if w in value.lower() or w in value]
        assert hits == [], (
            f"{label}: {key} = {value!r} claims a removal {hits}, but the rows "
            f"are KEPT (flagged, with ballots) since AUDIT-2026-09-27 P1"
        )


@pytest.mark.parametrize("label,path", CATALOGUES.items())
@pytest.mark.parametrize("key", KEYS)
def test_each_language_says_the_row_is_kept(label, path, key):
    """So the correction is not merely the absence of a wrong word."""
    keep_words = ("kept", "flagged", "review",
                 "保留", "标记", "复核",
                 "保持", "フラグ", "投票")
    for value in _values(path, key):
        assert any(w in value for w in keep_words), (
            f"{label}: {key} = {value!r} does not say the row is kept"
        )


def test_the_two_catalogues_agree_on_what_each_string_says():
    """js/i18n.js keeps its own catalogue, so a fix on one side is not a fix.

    Measured: both files carried the same six false claims until this commit.
    Compared on the KIND of statement (kept / flagged), not on the literal
    wording, which differs legitimately between the two files today.
    """
    for key in KEYS:
        py_vals = _values(CATALOGUES["rca_core/i18n.py"], key)
        js_vals = _values(CATALOGUES["js/i18n.js"], key)
        assert len(py_vals) == len(js_vals) == 3
        for pv, jv in zip(py_vals, js_vals):
            py_kept = any(w in pv for w in ("kept", "保留", "保持"))
            js_kept = any(w in jv for w in ("kept", "保留", "保持"))
            assert py_kept == js_kept, (
                f"{key}: python says kept={py_kept}, js says kept={js_kept}"
            )


# --- and the policy the wording describes is the policy in force ---------


def test_a_recombined_row_is_really_kept_not_removed():
    """The control: the strings would be right if this were false."""
    def _run(p):
        p = dict(p)
        p.setdefault("confidence", 0.8)
        p.setdefault("runs", 1)
        return p

    runs = [
        _run({"species_ranges": [{"species": "Genus A", "section": "S",
                                 "range_base": "Bed 10",
                                 "range_top": "Bed 20", "biozone": "Z2"}]}),
        _run({"species_ranges": [{"species": "Genus A", "section": "S",
                                 "range_base": "Bed 10",
                                 "range_top": "Bed 30", "biozone": "Z1"}]}),
        _run({"species_ranges": [{"species": "Genus A", "section": "S",
                                 "range_base": "Bed 20",
                                 "range_top": "Bed 20", "biozone": "Z1"}]}),
    ]
    merged = merge_results(runs)
    rows = merged.get("species_ranges") or []
    assert len(rows) == 1, f"expected the three runs to collapse to one row, got {len(rows)}"
    assert rows[0].get("_warning") == "recombined_consensus"
    assert rows[0].get("_recombination_ballots"), "a kept row must carry its ballots"
    assert merged.get("chimera_warnings"), "and the caller must be told"
