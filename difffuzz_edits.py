"""Property fuzz: the user-edit round trip must be lossless.

THE INVARIANT
-------------
    apply_edits(before, capture_edits(before, after))  ==  after

A user opens a result, edits a table, presses Apply. The diff/replay pair is
the ONLY thing standing between their edit and the saved data, and the
invariant is trivially checkable -- which is exactly why nobody checked it.
The 2026-09-27 review found a scalar-row insert that produced an EMPTY edits
payload, is_dirty() False, and the row vanished on Save. It survived because
the tests pinned the payload SHAPE, not the round trip.

Single-engine: no JS needed, so this is cheap to run at high volume.

WHAT IS RANDOMISED
------------------
Row kinds (dicts and bare scalars mixed in one list, which is the shape that
exercises the dict/scalar branches against each other), list lengths, cell
values (str/int/float/None/bool/nested list), duplicate identifiers, blank
identifiers, insertions at the head / middle / tail, deletions, pure
reorderings, and no-op edits. The generator deliberately produces degenerate
identifiers because `_align_rows` prefers identity over position and its
fallback is exactly where a mis-alignment hides a lost edit.

Run:  python difffuzz_edits.py [cases] [seed] [--list]
Exit 0 when every round trip is lossless, 1 otherwise.
"""
import copy
import json
import os
import random
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.chdir(REPO)

from rca_core.editable import (  # noqa: E402
    apply_edits, capture_edits, is_dirty, new_row_template,
)

# editable.py:_SCALAR_LIST_KEYS -- the one list whose rows are plain strings.
_SCALAR_LIST_KEYS = ("other_fossils",)

LIST_KEYS = ["species_ranges", "sections", "biozones", "other_fossils",
             "abundances", "nodes", "zones", "sites"]

CELLS = [
    "", "Bed 7", "Bed 7 ", "bed 7", None, 0, 1, -3, 12.5, True, False,
    "9", "10", "NaN", "a,b", "ø", "晚二叠世", "sp.", "  ", "\t",
    ["x"], {"k": 1},
]
IDS = ["A", "B", "C", "D", "", None, "  ", "a", "A "]


def make_dict_row(rng, kind):
    if kind == "species_ranges":
        r = {"species": rng.choice(CELLS), "section": rng.choice(CELLS)}
        for c in ("range_base", "range_top", "biozone", "confidence", "note"):
            if rng.random() < 0.6:
                r[c] = rng.choice(CELLS)
        for c in ("range_base_idx", "range_top_idx"):
            if rng.random() < 0.25:
                r[c] = rng.choice([1, 2, "3", None])
    elif kind == "sections":
        r = {"id": rng.choice(IDS), "group": rng.choice(CELLS),
             "coordinates_text": rng.choice(CELLS),
             "thickness_m": rng.choice(CELLS)}
    elif kind == "nodes":
        r = {"id": rng.choice(IDS), "name": rng.choice(CELLS)}
    else:
        r = {"name": rng.choice(IDS), "note": rng.choice(CELLS)}
    if rng.random() < 0.08:
        r["_extras"] = {"secret": rng.choice(CELLS)}
    return r


def make_list(rng, kind, n):
    """Rows are HOMOGENEOUS per kind, because that is what a real result holds.

    An earlier version mixed bare strings into dict lists. That produced 106
    "failures" in 1500 cases and every one of them was the generator asking
    for a shape the product never creates -- the eleventh harness-vs-code
    confusion of this review. Keep the shapes real; the interesting alignment
    traps live in DUPLICATE and BLANK identifiers, which are still generated.
    """
    if kind in _SCALAR_LIST_KEYS:
        return [rng.choice(["fossil a", "fossil b", "", " ", "ø", "Bed 7"])
                for _ in range(n)]
    return [make_dict_row(rng, kind) for _ in range(n)]


def mutate(rng, lst, kind):
    """One of the operations the table editor can actually perform.

    DELIBERATELY NOT GENERATED: reordering rows, and replacing a row with one
    of a different shape. The editor's structural verbs are add / delete /
    edit-cell only (see js/table.js rcaTableEdits "Row delete / add are index
    operations"), so neither can reach capture_edits from the UI. An earlier
    version of this generator produced both, and 145 of 1500 "failures" were
    the generator asking for something the product does not do -- which is the
    eleventh time in this review that the harness, not the code, was wrong.
    """
    roll = rng.random()
    out = copy.deepcopy(lst)
    if roll < 0.22 or not out:
        return out
    if roll < 0.44:                      # edit a cell / retype a scalar row
        i = rng.randrange(len(out))
        if isinstance(out[i], dict) and out[i]:
            keys = [k for k in out[i] if k != "_extras"]
            if not keys:
                return out
            out[i][rng.choice(keys)] = rng.choice(CELLS)
        else:
            out[i] = rng.choice(["edited", "", "fossil c"])
    elif roll < 0.56:                    # insert at the head
        out.insert(0, make_row(rng, kind))
    elif roll < 0.70:                    # insert in the middle
        i = rng.randrange(len(out)) + 1
        out.insert(i, make_row(rng, kind))
    elif roll < 0.84:                    # insert at the tail
        out.append(make_row(rng, kind))
    else:                                # delete
        del out[rng.randrange(len(out))]
    return out


def make_row(rng, kind):
    """A new row of the shape THIS table actually holds."""
    if kind in _SCALAR_LIST_KEYS:
        return rng.choice(["new fossil", "", "ø", "fossil d"])
    return make_dict_row(rng, kind)


# The editor's structural verbs are add / delete / edit-cell, and addRow
# APPENDS. Head and mid insertion are therefore NOT reachable from the UI --
# reported separately so a count of 466 there is not mistaken for 466 bugs.
VERBS = ["none", "edit", "append", "delete", "insert_head", "insert_mid"]
REACHABLE = {"none", "edit", "append", "delete"}


def apply_verb(rng, lst, kind, verb):
    out = copy.deepcopy(lst)
    if verb == "none" or not out:
        return out
    if verb == "edit":
        i = rng.randrange(len(out))
        if isinstance(out[i], dict):
            keys = [k for k in out[i] if k != "_extras"]
            if not keys:
                return out
            out[i][rng.choice(keys)] = rng.choice(CELLS)
        else:
            out[i] = rng.choice(["edited", "", "fossil c"])
    elif verb == "append":
        out.append(make_row(rng, kind))
    elif verb == "delete":
        del out[rng.randrange(len(out))]
    elif verb == "insert_head":
        out.insert(0, make_row(rng, kind))
    else:
        out.insert(rng.randrange(len(out)) + 1, make_row(rng, kind))
    return out


def whitespace_only(a, b):
    """True when the two lists differ ONLY by leading/trailing cell whitespace.

    `_coerce` strips before comparing, so capture_edits cannot see such a
    diff and the payload comes out empty. That is deliberate -- it stops the
    normalizer's stray whitespace from lighting up every dirty dot on load --
    and it is invisible to the user, because both sides render blank. It is
    reported as its own bucket so it is never mistaken for data loss.
    """
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, dict) and isinstance(y, dict):
            if set(x) != set(y):
                return False
            for k in x:
                if k == "_extras":
                    continue
                if isinstance(x[k], str) and isinstance(y[k], str):
                    if x[k].strip() != y[k].strip():
                        return False
                elif x[k] != y[k]:
                    return False
        elif isinstance(x, str) and isinstance(y, str):
            if x.strip() != y.strip():
                return False
        elif x != y:
            return False
    return True


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    show_all = "--list" in argv
    n = int(args[0]) if args else 3000
    seed = int(args[1]) if len(args) > 1 else 20260927
    rng = random.Random(seed)

    real = {v: 0 for v in VERBS}
    wsonly = {v: 0 for v in VERBS}
    other_table_bad = 0
    dirty_bad = 0
    samples = {}

    for i in range(n):
        kind = rng.choice(LIST_KEYS)
        before = {kind: make_list(rng, kind, rng.randint(0, 5))}
        verb = rng.choice(VERBS)
        after = {kind: apply_verb(rng, before[kind], kind, verb)}
        # a second table must be untouched, to catch cross-table bleed
        other = rng.choice([k for k in LIST_KEYS if k != kind])
        before[other] = make_list(rng, other, rng.randint(0, 3))
        after[other] = copy.deepcopy(before[other])

        edits = capture_edits(copy.deepcopy(before), copy.deepcopy(after))
        replay = apply_edits(copy.deepcopy(before), copy.deepcopy(edits))

        if replay.get(other) != before[other]:
            other_table_bad += 1
            if show_all and other_table_bad <= 2:
                print("CROSS-TABLE BLEED on %s while editing %s" % (other, kind))
        if is_dirty(copy.deepcopy(before), copy.deepcopy(after)) is False and edits:
            dirty_bad += 1
        if replay.get(kind) == after[kind]:
            continue
        if whitespace_only(before[kind], after[kind]):
            wsonly[verb] += 1
            continue
        real[verb] += 1
        samples.setdefault(verb, (kind, before[kind], after[kind],
                                  replay.get(kind), edits))

    print("cases               : %d  (seed %d)" % (n, seed))
    print("cross-table bleed   : %d" % other_table_bad)
    print("is_dirty disagreed with a non-empty payload : %d" % dirty_bad)
    print()
    print("%-13s %8s %8s   %s" % ("verb", "LOST", "ws-only", "reachable"))
    for v in VERBS:
        print("%-13s %8d %8d   %s" % (v, real[v], wsonly[v],
              "yes" if v in REACHABLE else "NO (addRow appends)"))
    reach_bad = sum(real[v] for v in VERBS if v in REACHABLE)
    print()
    print("LOST ROUND TRIPS, UI-reachable verbs : %d" % reach_bad)
    print("LOST ROUND TRIPS, unreachable verbs  : %d"
          % sum(real[v] for v in VERBS if v not in REACHABLE))
    for v in VERBS:
        if real[v] and (show_all or v in REACHABLE):
            k, b, a, g, e = samples[v]
            print("=" * 74)
            print("#%s  [%s]" % (v, k))
            print("   before: %s" % json.dumps(b, ensure_ascii=False, default=str)[:240])
            print("   after : %s" % json.dumps(a, ensure_ascii=False, default=str)[:240])
            print("   replay: %s" % json.dumps(g, ensure_ascii=False, default=str)[:240])
            print("   edits : %s" % json.dumps(e, ensure_ascii=False, default=str)[:240])
    tmpl_bad = 0
    for k in LIST_KEYS:
        try:
            new_row_template(k, None)
        except Exception as exc:
            tmpl_bad += 1
            print("new_row_template(%r) raised %s: %s" % (k, type(exc).__name__, exc))
    return 1 if (reach_bad or other_table_bad or dirty_bad or tmpl_bad) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
