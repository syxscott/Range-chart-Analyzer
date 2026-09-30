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
import re
import zipfile
from pathlib import Path

import pytest

from rca_core.standards import ics
from rca_core.standards.darwin_core import to_darwin_core_archive

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


#: A Ludlow conodont zone: the case a micropalaeontologist's range chart
#: actually prints, and the one this module says the desktop cannot date.
_SILURIAN_EXPORT_CASE = ("Gorstian", "Ludfordian")
#: Famennian is present in BOTH tables, so it is the control that proves the
#: empty result below is about the missing stages and not about the pipeline.
_CONTROL_EXPORT_CASE = ("Famennian", "Famennian")


@pytest.mark.parametrize("label,stages,expect_numbers", [
    ("silurian (undatable on the python transport)",
     _SILURIAN_EXPORT_CASE, not _EXPECTED_MISSING),
    ("control: a stage in both tables", _CONTROL_EXPORT_CASE, True),
])
def test_the_shipped_archive_carries_no_age_for_an_undatable_stage(
        tmp_path, label, stages, expect_numbers):
    """The consequence, asserted on the ARTIFACT rather than on the table.

    A missing key in a lookup table is easy to under-rate.  What a researcher
    receives is the DwC-A archive, so this reads it: the stage NAME survives
    (``earliestAgeOrLowestStage`` keeps ``'Gorstian'``) while the numeric Ma
    columns come out EMPTY, and a record with a name and no age has no
    temporal resolution once it lands in GBIF / iDigBio.

    Flip ``expect_numbers`` with ``_EXPECTED_MISSING`` when the promote lands.
    """
    base, top = stages
    out = tmp_path / "dwca.zip"
    to_darwin_core_archive({
        "sections": [{"name": "SecA", "coordinates": "31N, 117E"}],
        "species_ranges": [{
            "species": "Ozarkodina munda", "section": "SecA",
            "biozone": "Ozarkodina Zone",
            "range_base": base, "range_top": top,
        }],
    }, str(out))

    with zipfile.ZipFile(str(out)) as zf:
        raw = zf.read("occurrence.txt").decode("utf-8")
        meta = zf.read("meta.xml").decode("utf-8")
    delim = re.search(r'fieldsTerminatedBy="([^"]*)"', meta).group(1)
    lines = [ln for ln in raw.split("\n") if ln]
    row = dict(zip(lines[0].split(delim), lines[1].split(delim)))

    # The stage NAME is echoed either way -- that is the point of the case.
    assert row["earliestAgeOrLowestStage"], (
        "the stage name should survive regardless of the age lookup; got %r"
        % row["earliestAgeOrLowestStage"])

    has_numbers = bool(row.get("fad_ma") and row.get("lad_ma"))
    assert has_numbers is expect_numbers, (
        "%s: fad_ma/lad_ma = %r/%r, expected numeric ages=%s"
        % (label, row.get("fad_ma"), row.get("lad_ma"), expect_numbers))


#: Ages sitting on a Silurian substage base.  The reverse direction (age ->
#: stage name) diverges too, and not by returning nothing: Python cannot name a
#: stage the canonical table lacks, so it falls back to the SERIES
#: ("Ludlow"), while the browser names the stage ("Gorstian").  In a
#: zonation / correlation workflow the output VALUE is that label, so the two
#: transports emit different ranks for the same number.
#: Famennian is the control: it is in both tables, so both must agree.
_AGE_TO_STAGE = [
    (443.1, "Llandovery", "Hirnantian"),   # Silurian base / Hirnantian top
    (438.6, "Llandovery", "Aeronian"),
    (432.9, "Wenlock", "Llandovery"),
    (430.6, "Wenlock", "Homerian"),
    (426.7, "Ludlow", "Gorstian"),
    (372.15, "Famennian", "Famennian"),   # control
]


def test_age_to_stage_name_agrees_across_transports():
    """The reverse lookup is a second, independent symptom of the same gap.

    Read the JS table out of js/ics_table.js (globalThis.RCA_ICS_TABLE) rather
    than trusting a name here -- an earlier probe guessed the export wrong and
    reported 8/8 divergence including the control, which is how a broken probe
    announces itself.
    """
    import subprocess

    js = (
        "var fs=require('fs'),vm=require('vm');"
        "var src=fs.readFileSync('js/ics_table.js','utf8');"
        "var ctx=vm.createContext({console:console});"
        "vm.runInContext(src,ctx);"
        "var t=ctx.RCA_ICS_TABLE;"
        "if(!t)throw new Error('js/ics_table.js exports no RCA_ICS_TABLE');"
        "var ages=[%s];"
        "var out=[];for(var i=0;i<ages.length;i++){var m=ages[i],best=null;"
        "  for(var k in t){var r=t[k];"
        "    if(r.top_ma<=m&&m<=r.base_ma){best=k;break;}}"
        "  out.push(best);}"
        "process.stdout.write(JSON.stringify(out));"
        % ",".join(repr(float(ma)) for ma, _p, _j in _AGE_TO_STAGE)
    )
    proc = subprocess.run(["node", "-e", js], cwd=str(REPO),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "could not evaluate js/ics_table.js: %s" % (
        (proc.stderr or proc.stdout)[:400],)
    js_stages = json.loads(proc.stdout)
    assert len(js_stages) == len(_AGE_TO_STAGE)

    for (ma, py_expected, js_expected), js_got in zip(_AGE_TO_STAGE, js_stages):
        # The control must agree on BOTH sides whatever the gap is.
        py_got = ics.ics_stage_from_age(ma)
        assert js_got == js_expected, (
            "js/ics_table.js stopped resolving %.2f Ma to %r (got %r) -- the "
            "mirror moved, re-read the table before trusting this file"
            % (ma, js_expected, js_got))
        assert py_got == py_expected, (
            "python resolved %.2f Ma to %r, expected %r.  If the promote has "
            "landed this becomes a REAL fix requirement: the two transports "
            "must name the same stage." % (ma, py_got, py_expected))

