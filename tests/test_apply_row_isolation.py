r"""Edit ONE row; every other row must come back byte-identical.

apply_table_edits matches an edited row to a model row by identity, not by
position. The comment records why that matters (REVIEW-2026-09-20, "P0-7
follow-up -- the inheritance itself was wrong"): matching positionally meant any
row-count change -- a deletion, an insertion, a re-sort, the extra phantom row
a Qt widget reports -- shifted every later row onto its NEIGHBOUR's model row,
so a species silently inherited the previous row's _extras (plate figure, page,
per-row confidence). Scientifically wrong data that no later pass repairs.

The untouched-pass tests only show matching works when nothing was touched, which
is the trivial direction. This file tests the load-bearing one, and it is not
theoretical -- it is what found `support` being rewritten from int 84 to 84.0 on
every pass, because the comparison here is strict about types where the
untouched test normalises them.

Where the edited row actually lives is not always data[table_id]. The columnar
sub-tables (lithology_blocks / age_units / samples) are flattened for display,
so their rows live at data[parent][k][sub_key][j] and the flattened row records
where as `_src`. Reading data[table_id] for those returns None; the first draft
of the audit probe did exactly that and crashed.
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
    apply_table_edits, build_table_export, get_configs_for_result,
)

GOLD_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures", "gold")
T = lambda k: k  # noqa: E731
EDIT_SUFFIX = "\u270dEDITED"    # a marker real data will not contain


def _load_gold():
    out = []
    for dirpath, _d, files in os.walk(GOLD_DIR):
        for fn in files:
            if fn == "ground_truth.json":
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    out.append((os.path.basename(os.path.dirname(p)), json.load(f)))
    return sorted(out)


GOLD = _load_gold()


def changed_paths(before, after, path=None):
    """Every path whose value differs, as a tuple of keys/indexes.

    The root contributes NO element. An earlier version seeded the path with an
    empty string, so a real change came out as ('', 'abundances', 0, 'x') while
    the target was ('abundances', 0); the first elements never matched and all
    48 legitimate changes were reported as collateral damage. A broken
    comparator is as confident as a correct one.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for k in sorted(set(before) | set(after)):
            if k not in before:
                out.append(((path or ()) + (k,), "ADDED", after[k]))
            elif k not in after:
                out.append(((path or ()) + (k,), "REMOVED", before[k]))
            else:
                out += changed_paths(before[k], after[k], (path or ()) + (k,))
        return out
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return [((path or ()), "length", f"{len(before)}->{len(after)}")]
        out = []
        for i, (b, a) in enumerate(zip(before, after)):
            out += changed_paths(b, a, (path or ()) + (i,))
        return out
    if type(before) is not type(after) or before != after:
        return [((path or ()), "value", f"{before!r} -> {after!r}")]
    return []


def row_path(cfg, tid, index):
    """Where row `index` of this table actually lives in `data`."""
    nested = cfg.get("nested_in")
    if not nested:
        return (tid, index)
    rows = cfg.get("rows") or []
    if index >= len(rows):
        return None
    src = rows[index].get("_src")
    if not src:
        return None
    parent_idx, sub_key, sub_idx = src
    return (nested.get("parent", "sections"), parent_idx, sub_key, sub_idx)


def is_inside(changed, wanted):
    if changed == wanted:
        return True
    return all(changed[i] == wanted[i]
               for i in range(min(len(changed), len(wanted))))


def _multi_row_tables(case_id, data):
    """(table_id, config, rendered rows) for every table with 2+ rows."""
    out = []
    for cfg in get_configs_for_result(copy.deepcopy(data)):
        tid = cfg["id"]
        _h, rows = build_table_export(copy.deepcopy(data), tid, T)
        if len(rows) >= 2:
            out.append((tid, cfg, [list(r) for r in rows]))
    return out


def test_gold_fixtures_exist():
    assert len(GOLD) == 8, f"expected 8 gold cases, found {len(GOLD)}"


CASES = []
for _cid, _data in GOLD:
    for _tid, _cfg, _rows in _multi_row_tables(_cid, _data):
        CASES.append((_cid, _tid, _cfg, _rows, _data))


def test_there_are_multi_row_tables_to_test():
    """Otherwise every assertion below is vacuous and the file looks green."""
    assert len(CASES) > 5, f"only {len(CASES)} multi-row tables found"


@pytest.mark.parametrize(
    "case_id,tid,cfg,rows,data", CASES,
    ids=[f"{c}-{t}" for c, t, _c, _r, _d in CASES])
def test_editing_one_row_leaves_every_other_row_untouched(case_id, tid, cfg, rows, data):
    for target in range(len(rows)):
        cell = len(rows[target]) - 1
        if cell <= 1:
            continue
        wanted = row_path(cfg, tid, target)
        if wanted is None:
            continue
        work = copy.deepcopy(data)
        edited = [list(r) for r in rows]
        edited[target][cell] = f"{edited[target][cell]}{EDIT_SUFFIX}"
        apply_table_edits(work, tid, edited)
        stray = [ch for ch in changed_paths(data, work)
                 if not is_inside(ch[0], wanted)]
        assert stray == [], (
            f"{case_id}/{tid} editing row {target} also changed: "
            f"{[('.'.join(map(str, c[0])), c[2]) for c in stray]}"
        )


@pytest.mark.parametrize(
    "case_id,tid,cfg,rows,data", CASES,
    ids=[f"{c}-{t}" for c, t, _c, _r, _d in CASES])
def test_editing_one_row_changes_that_row(case_id, tid, cfg, rows, data):
    """The other half, so a pass that simply ignored every edit would pass the
    isolation test above. A no-op Apply must not be a way to lose data."""
    for target in range(len(rows)):
        cell = len(rows[target]) - 1
        if cell <= 1:
            continue
        wanted = row_path(cfg, tid, target)
        if wanted is None:
            continue
        work = copy.deepcopy(data)
        edited = [list(r) for r in rows]
        edited[target][cell] = f"{edited[target][cell]}{EDIT_SUFFIX}"
        apply_table_edits(work, tid, edited)
        inside = [ch for ch in changed_paths(data, work)
                  if is_inside(ch[0], wanted)]
        assert inside, f"{case_id}/{tid} editing row {target} changed NOTHING"


@pytest.mark.parametrize(
    "case_id,tid,cfg,rows,data", CASES,
    ids=[f"{c}-{t}" for c, t, _c, _r, _d in CASES])
def test_row_count_is_unchanged_by_a_single_cell_edit(case_id, tid, cfg, rows, data):
    """Identity matching is supposed to prevent the row-count shifts that made
    the positional implementation mis-attribute data."""
    before = copy.deepcopy(data)
    edited = [list(r) for r in rows]
    cell = len(edited[0]) - 1
    if cell <= 1:
        pytest.skip("single-column table")
    edited[0][cell] = f"{edited[0][cell]}{EDIT_SUFFIX}"
    work = copy.deepcopy(data)
    apply_table_edits(work, tid, edited)
    count_shifts = [ch for ch in changed_paths(before, work) if ch[1] == "length"]
    assert count_shifts == [], f"row count moved: {count_shifts}"
