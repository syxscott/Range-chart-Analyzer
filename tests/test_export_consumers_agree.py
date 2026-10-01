"""The DwC and PBDB writers must agree about the age they publish.

AUDIT-2026-10-02.

``standards/darwin_core.py`` and ``standards/pbdb.py`` are two consumers of
the same merged result, and they resolve a species row's FAD/LAD through the
SAME call (``ics_resolve_age_bound(base, "older")`` /
``ics_resolve_age_bound(top, "younger")``), so they agreed by construction
until darwin_core grew a guard that pbdb never received.

REVIEW-2026-07-31 gave darwin_core a rule: a complete numeric pair must run
in geological direction, so a reversed pair has its NUMBERS suppressed while
its stage labels survive. pbdb had no equivalent, and the two then answered
differently about the same row:

    range_base="Induan",  range_top="Wuchiapingian"
        DwC   ma=(None, None)        suppressed
        PBDB  ma=(251.902, 254.14)  published

    range_base="250 Ma",  range_top="260 Ma"
        DwC   ma=(None, None)        suppressed
        PBDB  ma=(250.0, 260.0)     published

A reversed FAD/LAD is what a model emits when it swaps the two endpoints --
the same shape aggregate.py records as ``bed_index_order_swapped`` and
quality.py as ``quality.bed_index_order_swapped`` -- so it is an ordinary OCR
slip. The consequence is not cosmetic: one chart told one database nothing and
the other a range that runs backwards in time, and ``_stage_endpoint_names``'s
own docstring records that a self-contradicting interval row is one "PBDB
validators reject", so the upload fails with nothing identifying the row.

This is the executable form of "the two products must corroborate each other":
rather than pinning each writer's answer separately, it asserts they AGREE,
which keeps holding if either one's resolution strategy changes.
"""

from __future__ import annotations

import pytest

from rca_core.standards import darwin_core as DC
from rca_core.standards import pbdb as PB

# (label, range_base, range_top) -- the four directions that matter.
ROWS = [
    ("stage order, base older", "Wuchiapingian", "Induan"),
    ("stage order, base younger", "Induan", "Wuchiapingian"),
    ("numeric, base older", "260 Ma", "250 Ma"),
    ("numeric, base younger", "250 Ma", "260 Ma"),
    ("same stage both ends", "Induan", "Induan"),
    ("adjacent stages, shared bound", "Induan", "Changhsingian"),
    ("unreadable base", "zzz", "Induan"),
    ("unreadable top", "Induan", "zzz"),
    ("both unreadable", "zzz", "yyy"),
    ("bed labels, not ages", "Bed 9", "Bed 7"),
    ("empty", "", ""),
]


def _row(base, top):
    return {"species": "A", "section": "S1", "range_base": base, "range_top": top}


@pytest.mark.parametrize("label,base,top", ROWS, ids=[r[0] for r in ROWS])
def test_the_two_exporters_publish_the_same_numbers(label, base, top):
    row = _row(base, top)
    dwc_ma = DC._resolve_age_bounds(row)[2:]
    pbdb_ma = PB._resolve_pbdb_bounds(row)[2:]
    assert dwc_ma == pbdb_ma, (label, base, top, dwc_ma, pbdb_ma)


@pytest.mark.parametrize("label,base,top", ROWS, ids=[r[0] for r in ROWS])
def test_the_two_exporters_publish_the_same_stage_labels(label, base, top):
    """Same row, same stage names. "" vs None is normalised because the two
    writers spell an unresolvable bound differently and the downstream CSV
    serialisation is what decides how each is rendered."""
    row = _row(base, top)
    dwc = tuple(x or None for x in DC._resolve_age_bounds(row)[:2])
    pbdb = tuple(x or None for x in PB._resolve_pbdb_bounds(row)[:2])
    assert dwc == pbdb, (label, base, top, dwc, pbdb)


@pytest.mark.parametrize("label,base,top", ROWS, ids=[r[0] for r in ROWS])
def test_no_exporter_ever_publishes_a_reversed_interval(label, base, top):
    """The property behind the agreement: a published pair must not run
    backwards in time. Asserted on both writers so neither can regress alone
    and quietly diverge again."""
    row = _row(base, top)
    for name, got in (("dwc", DC._resolve_age_bounds(row)),
                      ("pbdb", PB._resolve_pbdb_bounds(row))):
        early_ma, late_ma = got[2], got[3]
        if early_ma is None or late_ma is None:
            continue
        assert early_ma >= late_ma, (
            "%s published a reversed interval for %r: early=%r late=%r"
            % (name, row, early_ma, late_ma))


def test_the_guard_is_not_over_eager():
    """A pair that is equal, or a single unresolvable end, must still publish.
    The guard fires on `early < late`, never on `early == late`."""
    row = _row("Induan", "Changhsingian")
    early_ma, late_ma = PB._resolve_pbdb_bounds(row)[2:]
    assert early_ma is not None and late_ma is not None
    assert early_ma == late_ma

    row = _row("zzz", "Induan")
    assert PB._resolve_pbdb_bounds(row)[3] is not None, \
        "one unresolvable end must not suppress the other"
