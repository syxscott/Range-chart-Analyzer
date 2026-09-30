"""AUDIT-2026-10-01 [item 33]: the two transports load DIFFERENT ICS tables.

This test does not fix the divergence -- the age table is a research data file
and promoting it is the maintainer's call.  It pins the gap so the gap is a
named, asserted fact instead of a year-old silence.

What is true right now (measured, not inferred):

* ``rca_core/standards/ics.py:53`` loads ``resources/ics_2024.json`` --
  98 entries, and NOT ONE row carries an ``ics_version`` stamp or Macrostrat
  provenance.
* ``js/ics_table.js`` loads ``resources/ics_current.json`` -- 109 entries,
  every row stamped ``ICS v2024/12`` with ``source``/``license``/
  ``retrieved_at``/``source_id``.
* ``scripts/update_ics.py`` writes the fetched table to ``ics_current.json``
  and promotes it to ``ics_2024.json`` ONLY with an explicit
  ``--write-canonical``.  The script's own version-coherence contract refuses
  to promote a table whose rows lack the stamp -- which is exactly
  ``ics_2024.json``.  Git dates the split: ``ics_current.json`` was refreshed
  2026-09-20, ``ics_2024.json`` last changed 2026-09-07.  The promote never
  happened.
* For the 98 entries the two share, EVERY ``base_ma``/``top_ma`` is
  identical.  So there is no value drift; the loss is the 11 stages only in
  the fetched file, and they are the whole Silurian subseries.

User-visible effect: a chart printing ``Gorstian`` resolves to 426.7 Ma in the
browser and to ``None`` in the desktop app.  This project digitises
radiolarian / conodont range charts, where Silurian conodont zonation
(Llandovery -> Rhuddanian / Telychian / Aeronian, Wenlock -> Homerian /
Sheinwoodian, Ludlow -> Gorstian / Ludfordian) is routine.

Why no existing test caught it: ``tests/test_ics_invariants.py::TestJsMirror``
pins ``js/ics_table.js`` against ``ics_current.json`` and its comment declares
"both ends now ship ics_current.json" -- but it never asserts WHICH file the
Python import path reads, so it passes while the desktop transport ships the
stale table.  This module is the missing half.

REMEDY (maintainer decision, not taken here): promote the already-validated
2026-09-20 snapshot, i.e. make ``ics_2024.json`` a copy of
``ics_current.json``.  Verified safe before it was proposed: both files have
the same two "orphan" boundaries and they are the legitimate bottom of the
chart (538.8 Ma = base of the Cambrian, 0.0 Ma = the present -- the second one
just sits under a different stage name, Meghalayan having replaced Holocene in
the 2018 chart), identical load-bearing anchors (PTB 251.902, KPG 66.0, base
Cretaceous 143.1, base Quaternary 2.58), and identical field/era/period
consistency.  Running ``scripts/update_ics.py --write-canonical`` instead would
re-fetch from the network and could land a different vintage.

When the promote happens, update ``_EXPECTED_MISSING`` below to empty and this
module becomes the standing guard that the two transports stay in step.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rca_core.standards import ics

REPO = Path(__file__).resolve().parent.parent
RESOURCES = REPO / "rca_core" / "resources"

#: Exactly the stages the browser can date and the desktop cannot, as measured
#: 2026-10-01.  If this list is empty the promote has happened and the
#: assertions below become the permanent parity guard.
_EXPECTED_MISSING = [
    "Aeronian", "Gorstian", "Greenlandian", "Homerian", "Late Pleistocene",
    "Ludfordian", "Meghalayan", "Northgrippian", "Rhuddanian", "Sheinwoodian",
    "Telychian",
]

#: The stages a micropalaeontologist's range chart is most likely to print.
_SILURIAN = ("Rhuddanian", "Telychian", "Aeronian",
             "Homerian", "Sheinwoodian", "Gorstian", "Ludfordian")


@pytest.fixture(scope="module")
def fetched() -> dict:
    return json.loads((RESOURCES / "ics_current.json").read_text(encoding="utf-8"))


def test_the_two_transports_load_different_tables(fetched):
    missing = sorted(set(fetched) - set(ics.ICS_2024))
    assert missing == _EXPECTED_MISSING, (
        "the divergence set changed.  If a stage is NEW here it was added to "
        "ics_2024.json -- good, shrink _EXPECTED_MISSING.  If one disappeared, "
        "a stage was dropped from the fetched table, which is worth "
        "investigating before anything else.  now=%r" % (missing,))


def test_the_silurian_subseries_is_undatable_on_the_desktop(fetched):
    """The cost of the divergence, named in geological terms."""
    # _SILURIAN is written in ICS order (oldest first) because that is how a
    # range chart's axis reads; the comparison is sorted so the failure message
    # is stable and diffable.
    silent = sorted(s for s in _SILURIAN if s not in ics.ICS_2024)
    assert silent == sorted(set(_EXPECTED_MISSING) & set(_SILURIAN)), (
        "expected the Silurian subseries to be missing from the Python table; "
        "now missing=%r" % (silent,))
    for stage in silent:
        assert ics.ICS_2024.get(stage) is None
        row = fetched[stage]
        assert isinstance(row.get("base_ma"), (int, float)), (
            "%s has no base_ma in the fetched table either -- the gap is not "
            "a missing promote" % stage)


def test_shared_entries_agree_exactly(fetched):
    """No value drift: the loss is the 11 stages, nothing else."""
    shared = set(ics.ICS_2024) & set(fetched)
    assert len(shared) == 98, "the shared stage count changed (%d)" % len(shared)
    for name in sorted(shared):
        a, b = ics.ICS_2024[name], fetched[name]
        for field in ("base_ma", "top_ma"):
            assert abs(float(a[field]) - float(b[field])) < 1e-9, (
                "%s.%s drifted: python=%r fetched=%r" % (name, field,
                                                          a[field], b[field]))


def test_the_loaded_table_cannot_attest_the_stamped_version():
    """``ICS_VERSION`` is printed into every evidence-chain report.

    It is a constant in ics.py, not something read off the table, so right now
    no loaded row can corroborate it.  After the promote this becomes true and
    the assertion inverts -- which is the point of writing it down.
    """
    stamps = {v.get("ics_version") for v in ics.ICS_2024.values()
              if isinstance(v, dict)} - {None}
    if _EXPECTED_MISSING:
        assert stamps == set(), (
            "expected the canonical table to carry no version stamp (that is "
            "why the promote was refused); found %r -- if this table is now "
            "stamped, update _EXPECTED_MISSING and the promote has happened"
            % (sorted(stamps),))
    else:
        assert ics.ICS_VERSION in stamps, (
            "the canonical table is promoted but its rows do not carry the "
            "version this module stamps into reports (%r vs %r)"
            % (sorted(stamps), ics.ICS_VERSION))
