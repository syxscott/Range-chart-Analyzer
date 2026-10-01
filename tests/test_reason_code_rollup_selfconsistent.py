"""reason_code_rollup's own numbers must agree with each other.

AUDIT-2026-10-02. ``rca_core/report.py`` consumes ``by_code``, ``by_kind``,
``contracted_rows`` and ``entries`` from ONE call and renders them together as
the evidence chain -- the panel a paper cites when it says how many rows
answered and how they broke down. Nothing tied those four to each other: the
existing tests assert ``contracted_rows`` and ``len(entries)`` separately on
one corpus, so a change that double-counted a row in ``by_kind`` or listed a
non-contracted row in ``entries`` would have kept every assertion green while
the report printed mutually inconsistent counts.

Measured over eight corpora x three limits before writing this: all four
relations already hold, so this is coverage, NOT a fix, and it is labelled as
such so nobody goes looking for a bug that was not there.

    sum(by_kind.values())  <= contracted_rows      equality when every
                                                     contracted row is kinded
    sum(by_code.values())  <= contracted_rows      a row can carry several codes
    len(entries)           == min(limit, contracted_rows)
    every entry            carries a response_kind or a reason_codes value

The first two are ``<=`` rather than ``==`` by design: a row that carries only
``reason_codes`` is contracted (it is part of the evidence chain) but appears
in no ``by_kind`` bucket, so the breakdowns are each a partition of a SUBSET of
the contracted rows.

A TRAP WORTH NAMING, since it looks like a bug and is not:
``coverage_ledger``'s ``cells`` and ``reason_code_rollup``'s ``contracted_rows``
are DIFFERENT QUANTITIES and must never be reconciled with each other. ``cells``
is the sum of the four response buckets over the (column x stratum) grid -- with
``cross_product=True`` a (column, stratum) pair that no row fills is not a cell
at all, so ``cells`` counts pairs, not rows, and several rows can share one
cell. The docstrings of both say so; this file is where a reader looking for the
discrepancy lands.
"""

from __future__ import annotations

import pytest

from rca_core import reason_codes as RC

CORPORA = {
    "kinds only": [
        {"response_kind": "extracted"},
        {"response_kind": "not_drawn"},
        {"response_kind": "uncertain"},
        {"response_kind": "extracted"},
    ],
    "codes only, no kind": [
        {"reason_codes": ["dash"]},
        {"reason_codes": ["low_confidence"]},
    ],
    "kind and codes": [
        {"response_kind": "extracted", "reason_codes": ["inferred"]},
        {"response_kind": "not_drawn", "reason_codes": ["not_drawn"]},
    ],
    "mixed with uncontracted rows": [
        {"response_kind": "extracted", "reason_codes": ["inferred"]},
        {"reason_codes": ["dash"]},
        {"response_kind": "uncertain"},
        {},
        None,
        "not-a-row",
    ],
    "aliases and case": [
        {"response_kind": "EXTRACTED"},
        {"reason_code": "dash"},
        {"response_kind": "", "reason_codes": []},
    ],
    "unknown kind plus codes": [
        {"response_kind": "bogus", "reason_codes": ["dash"]},
    ],
    "empty": [],
    "no contract fields at all": [{}, {}, {}],
}
LIMITS = [0, 2, 200]


def _ids():
    return [(name, limit)
            for name in CORPORA for limit in LIMITS]


@pytest.mark.parametrize("name,limit", _ids())
def test_the_breakdowns_never_exceed_the_row_count(name, limit):
    r = RC.reason_code_rollup(CORPORA[name], limit=limit)
    by_kind = r.get("by_kind") or {}
    by_code = r.get("by_code") or {}
    assert sum(by_kind.values()) <= r["contracted_rows"], (name, limit, r)
    assert sum(by_code.values()) <= r["contracted_rows"], (name, limit, r)


@pytest.mark.parametrize("name,limit", _ids())
def test_entries_are_the_first_min_limit_contracted_rows(name, limit):
    r = RC.reason_code_rollup(CORPORA[name], limit=limit)
    entries = r.get("entries") or []
    assert len(entries) == min(max(0, int(limit)), r["contracted_rows"]), \
        (name, limit, len(entries), r["contracted_rows"])


@pytest.mark.parametrize("name,limit", _ids())
def test_every_listed_entry_is_actually_a_contracted_row(name, limit):
    """The docstring's promise: entries list ONLY rows that carry a
    response_kind or at least one valid reason_codes value, "the evidence
    chain should show the decisions, not re-transcribe the table"."""
    r = RC.reason_code_rollup(CORPORA[name], limit=limit)
    for entry in r.get("entries") or []:
        assert entry.get("response_kind") or entry.get("reason_codes"), (name, entry)


def test_the_counts_partition_when_every_contracted_row_is_kinded():
    """The strict direction, so the two `<=` assertions above are not the only
    thing keeping the panel honest: a reader summing by_kind must land on the
    stated row count whenever the corpus allows it."""
    rows = CORPORA["kinds only"]
    r = RC.reason_code_rollup(rows)
    assert r["contracted_rows"] == 4
    assert sum((r.get("by_kind") or {}).values()) == r["contracted_rows"]


def test_by_kind_keys_are_known_response_kinds():
    """A row carrying a spelling the contract does not know contributes to
    contracted_rows and to by_code but must not invent a by_kind bucket --
    otherwise the report prints a kind nothing downstream recognises."""
    known = {RC.RESPONSE_EXTRACTED, RC.RESPONSE_NOT_DRAWN, RC.RESPONSE_UNCERTAIN}
    r = RC.reason_code_rollup(CORPORA["unknown kind plus codes"])
    assert set(r.get("by_kind") or {}) <= known, r
    assert r["contracted_rows"] == 1


# Rows the LEDGER can attribute. The corpora above deliberately omit
# species/section, because the rollup does not need them -- but the ledger
# counts a reason code only for a row it can place in the grid, so the
# cross-check below needs rows that carry both. Measured over these eight
# before being written down.
LEDGER_CORPORA = {
    "distinct species": [
        {"species": "A", "section": "S1", "response_kind": "extracted"},
        {"species": "B", "section": "S1", "response_kind": "extracted"},
    ],
    "same species twice": [
        {"species": "A", "section": "S1", "response_kind": "extracted"},
        {"species": "A", "section": "S1", "response_kind": "not_drawn"},
    ],
    "two strata one column": [
        {"species": "A", "section": "S1", "response_kind": "extracted"},
        {"species": "A", "section": "S2", "response_kind": "uncertain"},
    ],
    "codes only": [
        {"species": "A", "section": "S1", "reason_codes": ["dash"]},
    ],
    "no contract fields": [{"species": "A", "section": "S1"}],
    "missing species key": [
        {"section": "S1", "response_kind": "extracted"},
    ],
    "unknown kind plus codes": [
        {"species": "A", "section": "S1", "response_kind": "bogus",
         "reason_codes": ["dash"]},
    ],
    "empty": [],
}


@pytest.mark.parametrize("name", list(LEDGER_CORPORA))
def test_the_ledger_and_the_rollup_agree_on_the_code_counts(name):
    """Two independently computed tallies of the same reason_codes, one in the
    ledger and one in the rollup, and the report prints both. Only meaningful
    for rows the ledger can place in the grid, which is why this uses
    LEDGER_CORPORA rather than the rollup corpora above."""
    rows = LEDGER_CORPORA[name]
    led = RC.coverage_ledger(rows)
    roll = RC.reason_code_rollup(rows)
    assert (led.get("reason_code_counts") or {}) == (roll.get("by_code") or {}), name


def test_the_ledger_totals_are_self_accounting():
    """Every relation here was measured over LEDGER_CORPORA before being
    written down; the ones that are NOT asserted are named below."""
    corpora = (LEDGER_CORPORA["distinct species"]
               + LEDGER_CORPORA["same species twice"])
    led = RC.coverage_ledger(corpora)
    t = led["totals"]
    assert (t["extracted"] + t["not_drawn"] + t["uncertain"]
            + t["silent_missing"]) == t["cells"]
    assert t["answered"] == t["extracted"] + t["not_drawn"] + t["uncertain"]
    assert sum(c["cells"] for c in led["columns"]) == t["cells"]
    assert sum(s["cells"] for s in led["strata"]) == t["cells"]
    assert t["honest_coverage"] >= t["strict_coverage"]
    # A (column, stratum) pair is a cell, so rows sharing a species leave
    # cells BELOW row_count -- which is why the two must never be reconciled
    # with each other.
    assert t["cells"] <= t["row_count"]
    assert t["row_count"] == 4, t


# NOT invariants, measured false on at least one corpus, recorded so nobody
# adds them as assertions later:
#   totals["explicit_responses"] == extracted + not_drawn + uncertain
#       -- a row whose response_kind the contract does not recognise lands in
#          no bucket and so is not an "explicit" response, while it still
#          counts as contracted.
#   totals["row_count"] == cells + unattributed_rows
#       -- rows sharing a (column, stratum) pair share one cell, so the row
#          count is not the cell count plus the unattributed ones.
#   totals["explicit_responses"] == rollup["contracted_rows"]
#       -- the two count DIFFERENT things: explicit_responses needs a
#          recognised kind, contracted_rows also accepts a codes-only row.
#   totals["cells"] != rollup["contracted_rows"]
#       -- do not assert this either. cells counts (column, stratum) pairs;
#          contracted_rows counts rows. They coincide on a corpus where every
#          row has its own species and diverge as soon as two share one, which
#          is why neither can be used to check the other.
