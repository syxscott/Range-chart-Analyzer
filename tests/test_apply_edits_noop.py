r"""An untouched Apply-Edits pass must not add keys the model never had.

apply_table_edits ended with ``d.update(edited)``, which writes EVERY column of
the table onto EVERY row -- including columns that row never carried. The
operator clicks Apply on an unchanged table and the model comes back with new
keys in it. Measured on the gold fixtures:

    rc_synth_001/2/3  sections   + formation_thickness_m: ""   (per section)
    rc_synth_001/2/3  biozones   + thickness_m: None           (per zone)
    pt_synth_001      nodes      + node_age_ma: None           (per node)

No value was corrupted and none was lost, so this is not data loss. It is a
shape change from an operation with zero edits, and shape is load-bearing: the
JSON export grows keys it did not have before, and any consumer testing
``"k" in row`` rather than ``row.get("k")`` starts matching rows that never
carried the field. The fix is two-sided and both halves matter -- write the key
if the row already had it (so CLEARING still works) or if the new value is
non-empty (so editing an absent field still works). Only "absent AND empty" is
skipped, which carries no information: the cell was blank because the field was
never there.

The tests below drive the real entry points -- build_table_export then
apply_table_edits -- on the gold canned responses, because two earlier versions
of the audit probe hand-wrote payloads and invented two bugs the product cannot
produce (a formations list rendered as its Python repr, and age_units read
under the wrong key names). Gold data is real normalizer output, committed and
checked in CI, so the payload cannot drift from what the code reads.

One movement is EXPECTED and is not a defect: a columnar section's thickness
goes from the string the normalizer stored ('11') to the number the "number"
type produces (11). That is the documented purpose of the tag -- see the
COL_TYPES comment about a thickness that "failed the numeric range / quality
checks and re-exported differently from the browser".
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.exporter import (  # noqa: E402
    COL_TYPES, apply_table_edits, build_table_export, get_configs_for_result,
)

GOLD_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures", "gold")
T = lambda k: k  # noqa: E731 - identity translate keeps labels readable

# The one normalisation the "number" tag exists to perform.
EXPECTED_NORMALISATION = {("cs_synth_001", "sections"), ("cs_synth_002", "sections")}


def _load_gold():
    cases = []
    for dirpath, _dirs, files in os.walk(GOLD_DIR):
        for fn in files:
            if fn == "ground_truth.json":
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    cases.append((os.path.basename(os.path.dirname(p)), json.load(f)))
    return sorted(cases)


GOLD = _load_gold()


def _norm(v):
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return ("num", int(v))
    if isinstance(v, (int, float)):
        return ("num", v)
    if isinstance(v, list):
        return ("list", [_norm(x) for x in v])
    if isinstance(v, str):
        return ("str", v.strip())
    if v is None:
        return ("none", None)
    return ("other", repr(v))


def _leaves_that_moved(before, after, path=""):
    """Keys that appeared, vanished, or changed value."""
    out = []
    if isinstance(before, dict) and isinstance(after, dict):
        for k in sorted(set(before) | set(after)):
            if k not in before:
                out.append(f"{path}.{k} ADDED")
            elif k not in after:
                out.append(f"{path}.{k} REMOVED")
            else:
                out += _leaves_that_moved(before[k], after[k], f"{path}.{k}")
        return out
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return [f"{path} length {len(before)} -> {len(after)}"]
        out = []
        for i, (b, a) in enumerate(zip(before, after)):
            out += _leaves_that_moved(b, a, f"{path}[{i}]")
        return out
    if _norm(before) != _norm(after):
        out.append(f"{path} {before!r} -> {after!r}")
    return out


def _tables(case_id, data):
    return [c["id"] for c in get_configs_for_result(copy.deepcopy(data))]


# --- the property, over every table of every gold case ------------------


def test_gold_fixtures_exist():
    """If the fixture walk found nothing, every assertion below is vacuous."""
    assert len(GOLD) == 8, f"expected 8 gold cases, found {len(GOLD)}"
    assert GOLD, "no gold fixtures -- the rest of this file would pass trivially"


@pytest.mark.parametrize("case_id,data", GOLD, ids=[c for c, _ in GOLD])
def test_untouched_pass_adds_no_keys(case_id, data):
    """The defect, stated once: nothing may APPEAR that was not there."""
    work = copy.deepcopy(data)
    before = copy.deepcopy(work)
    for tid in _tables(case_id, work):
        _headers, rows = build_table_export(work, tid, T)
        if not rows:
            continue
        apply_table_edits(work, tid, [list(r) for r in rows])
    added = [m for m in _leaves_that_moved(before, work) if " ADDED" in m]
    assert added == [], f"untouched pass added {len(added)} key(s): {added}"


@pytest.mark.parametrize("case_id,data", GOLD, ids=[c for c, _ in GOLD])
def test_untouched_pass_preserves_values_and_removes_nothing(case_id, data):
    work = copy.deepcopy(data)
    before = copy.deepcopy(work)
    for tid in _tables(case_id, work):
        _headers, rows = build_table_export(work, tid, T)
        if not rows:
            continue
        apply_table_edits(work, tid, [list(r) for r in rows])
    lost = [m for m in _leaves_that_moved(before, work) if " REMOVED" in m]
    assert lost == [], f"untouched pass removed {len(lost)} key(s): {lost}"


@pytest.mark.parametrize("case_id,data", GOLD, ids=[c for c, _ in GOLD])
def test_the_only_movement_left_is_the_documented_normalisation(case_id, data):
    """Everything that still moves must be accounted for by name.

    A blanket "the model is unchanged" assertion would fail on the columnar
    thickness, which is SUPPOSED to change: the normalizer stored the string
    '11' and the "number" tag exists to turn it into 11. Pinning the exception
    by name means a NEW movement fails the test instead of being absorbed.
    """
    work = copy.deepcopy(data)
    before = copy.deepcopy(work)
    for tid in _tables(case_id, work):
        _headers, rows = build_table_export(work, tid, T)
        if not rows:
            continue
        apply_table_edits(work, tid, [list(r) for r in rows])
    moved = _leaves_that_moved(before, work)
    if (case_id, "sections") in EXPECTED_NORMALISATION:
        # Only ever the thickness field, only ever string -> number.
        bad = [m for m in moved
               if "thickness_m" not in m or " ADDED" in m or " REMOVED" in m]
        assert bad == [], f"unexpected movement: {bad}"
    else:
        assert moved == [], f"unexpected movement: {moved}"


# --- the two halves of the fix, which must both keep working ------------


def test_clearing_an_existing_value_still_clears_it():
    """Half one: a key the row already had is still written, even when the new
    value is empty. Without this the fix would silently make it impossible to
    clear a field -- the one thing Apply Edits exists to do."""
    data = {
        "schema": "range_chart", "mode": "range_chart",
        "sections": [{"name": "Sec A", "age_range": "1-2 Ma",
                      "formations": ["Sandstone"],
                      "coordinates": "31N, 117E"}],
    }
    _headers, rows = build_table_export(copy.deepcopy(data), "sections", T)
    # coordinates is column index 5 (0 is the auto index)
    assert rows[0][5] == "31N, 117E", rows[0]
    rows[0][5] = ""
    apply_table_edits(data, "sections", rows)
    assert "coordinates" in data["sections"][0], "the key was dropped instead of cleared"
    assert data["sections"][0]["coordinates"] == "", data["sections"][0]


def test_clearing_a_typed_numeric_field_still_yields_none():
    data = {
        "schema": "columnar", "mode": "columnar",
        "sections": [{"id": "s1", "name": "Col",
                      "thickness_m": "11", "coordinates_text": "x"}],
    }
    _headers, rows = build_table_export(copy.deepcopy(data), "sections", T)
    assert rows[0][3] == "11", rows[0]
    rows[0][3] = ""
    apply_table_edits(data, "sections", rows)
    assert "thickness_m" in data["sections"][0], "the key was dropped instead of cleared"
    assert data["sections"][0]["thickness_m"] is None, data["sections"][0]


def test_typing_a_value_into_an_absent_field_still_writes_it():
    """Half two: a non-empty value is always written, whether or not the row
    had the key. This is what stops the fix from swallowing real edits."""
    data = {
        "schema": "range_chart", "mode": "range_chart",
        "sections": [{"name": "Sec A", "age_range": "1-2 Ma",
                      "formations": ["Sandstone"]}],
    }
    assert "coordinates" not in data["sections"][0]
    _headers, rows = build_table_export(copy.deepcopy(data), "sections", T)
    rows[0][5] = "31N, 117E"
    apply_table_edits(data, "sections", rows)
    assert data["sections"][0]["coordinates"] == "31N, 117E", data["sections"][0]


def test_a_zero_is_written_not_treated_as_empty():
    """0 and False are falsy but are DATA. The emptiness test must not use
    truthiness, or a real 0 would be silently dropped."""
    data = {
        "schema": "range_chart", "mode": "range_chart",
        "sections": [{"name": "Sec A", "age_range": "1-2 Ma",
                      "formations": ["Sandstone"]}],
    }
    _headers, rows = build_table_export(copy.deepcopy(data), "sections", T)
    rows[0][4] = "0"          # formation_thickness_m, typed "str"
    apply_table_edits(data, "sections", rows)
    assert data["sections"][0]["formation_thickness_m"] == "0", data["sections"][0]


def test_applying_twice_is_idempotent():
    """The first pass used to change the model, so the second differed. With
    the fix the second pass has nothing left to do."""
    data = json.loads(json.dumps(dict(GOLD[0][1])))
    first = copy.deepcopy(data)
    for tid in _tables("", first):
        _h, rows = build_table_export(first, tid, T)
        if rows:
            apply_table_edits(first, tid, [list(r) for r in rows])
    second = copy.deepcopy(first)
    for tid in _tables("", second):
        _h, rows = build_table_export(second, tid, T)
        if rows:
            apply_table_edits(second, tid, [list(r) for r in rows])
    assert _leaves_that_moved(first, second) == []
