"""AUDIT-2026-10-01 [item 33]: the two transports must load the SAME ICS table.

HISTORY. This module originally pinned a DIVERGENCE: ``ics.py`` loaded
``ics_2024.json`` (98 entries) while ``js/ics_table.js`` shipped
``ics_current.json`` (109 entries), and the module asserted the gap so it
could not rot into silence.

AUDIT-2026-10-22: the promote has happened.  The gap was not a capability
difference -- it CORRUPTED DATA.  Because the 98-row table had no
``Late Pleistocene`` row, ``ics_resolve_age_bound("Late Pleistocene")`` fell
through to the bare ``Pleistocene`` EPOCH and published a FAD of **2.58 Ma
instead of 0.129 Ma** -- a factor of 20 too old -- straight into the
``max_ma`` column of the Darwin Core and PBDB exports.  The browser had the
109-row table and got it right, so the SAME figure published from the desktop
and from the web carried ages that differed by a factor of 20.  Six
stage-boundary ages also resolved to different stage NAMES per transport.

``rca_core/resources/ics_2024.json`` is now the promoted 109-row table
(byte-identical to ``ics_current.json``, every row carrying its
``ics_version`` / ``source`` / ``license`` / ``retrieved_at`` provenance).

This module is now the STANDING GUARD the original docstring promised: the two
transports must agree, row for row and age for age, so the split can never
reopen.  ``_EXPECTED_MISSING`` is empty because nothing is missing.
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
#: 2026-10-01.  EMPTY since the 2026-10-22 promote: the canonical table now
#: carries all 109 rows, and the assertions below are the permanent parity
#: guard that keeps it that way.
_EXPECTED_MISSING = []

#: The stages a micropalaeontologist's range chart is most likely to print.
_SILURIAN = ("Rhuddanian", "Telychian", "Aeronian",
             "Homerian", "Sheinwoodian", "Gorstian", "Ludfordian")


@pytest.fixture(scope="module")
def fetched() -> dict:
    return json.loads((RESOURCES / "ics_current.json").read_text(encoding="utf-8"))


def test_the_two_transports_load_the_same_table(fetched):
    missing = sorted(set(fetched) - set(ics.ICS_2024))
    assert missing == _EXPECTED_MISSING, (
        "the transports diverged again.  If a stage is NEW here it was added "
        "to the fetched table but not promoted -- run scripts/update_ics.py "
        "--write-canonical.  If one disappeared, a stage was dropped from the "
        "fetched table.  now=%r" % (missing,))
    extra = sorted(set(ics.ICS_2024) - set(fetched))
    assert extra == [], (
        "the canonical table carries rows the fetched table does not: %r -- "
        "the promote and the refresh are out of step" % (extra,))


def test_the_silurian_subseries_is_datable_on_both_transports(fetched):
    """The cost of the divergence, named in geological terms -- now repaired.

    Before the promote the desktop could not date a Ludlow conodont zone at
    all, and the Quaternary subseries were worse: "Late Pleistocene" resolved
    to the PLEISTOCENE EPOCH, publishing a FAD of 2.58 Ma instead of 0.129 Ma.
    """
    silent = sorted(s for s in _SILURIAN if s not in ics.ICS_2024)
    assert silent == [], (
        "the Silurian subseries fell out of the canonical table: %r" % (silent,))
    for stage in _SILURIAN:
        row = ics.ICS_2024.get(stage)
        assert row is not None, stage
        assert isinstance(row.get("base_ma"), (int, float)), stage
        # ... and it must now date on the desktop too.
        assert ics.ics_resolve_age_bound(stage) != (None, None), (
            "%s is in the table but does not resolve" % stage)
    # the Quaternary case that corrupted a published FAD
    assert ics.ics_resolve_age_bound("Late Pleistocene", prefer="older") == (
        "Late Pleistocene", 0.129), (
        "'Late Pleistocene' must resolve to its own subseries, not to the "
        "Pleistocene epoch (2.58 Ma) -- the epoch fallback published a FAD "
        "that was 20x too old")


def test_shared_entries_agree_exactly(fetched):
    """No value drift, and now nothing is missing either."""
    shared = set(ics.ICS_2024) & set(fetched)
    assert len(shared) == len(fetched) == 109, (
        "the shared stage count changed (%d of %d)" % (len(shared), len(fetched)))
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
    (443.1, "Rhuddanian"),   # Silurian base
    (438.6, "Llandovery"),
    (432.9, "Sheinwoodian"),
    (430.6, "Wenlock"),
    (426.7, "Gorstian"),
    (439.5, "Aeronian"),
    (442.0, "Rhuddanian"),
    (0.05, "Late Pleistocene"),
    (0.013, "Late Pleistocene"),
    (0.0, "Meghalayan"),
    (372.15, "Famennian"),   # control
]


def test_age_to_stage_name_agrees_across_transports():
    """The two transports must name the SAME stage for the same age.

    Before the promote they did not: at 426.7 Ma the desktop said "Ludlow"
    (a series) and the browser said "Gorstian" (a stage), and at 0.0 Ma the
    desktop said "Holocene" and the browser said "Meghalayan".  Those names
    are what a zonation or correlation workflow publishes, so the divergence
    reached the output VALUE, not just a lookup result.

    The JS side is evaluated with the SHIPPED resolver
    (``js/quality.js:_icsStageFromAge``) rather than a hand-written probe:
    the real function implements the interior/base/top boundary order and the
    "a stage beats a series" preference, and a naive probe cannot reproduce
    it.
    """
    import subprocess

    js = (
        "var fs=require('fs'),vm=require('vm');"
        "var ctx=vm.createContext({console:console});"
        "vm.runInContext(fs.readFileSync('js/ics_table.js','utf8'),ctx);"
        "vm.runInContext(fs.readFileSync('js/quality.js','utf8'),ctx);"
        "if(typeof ctx._icsStageFromAge!=='function')"
        "  throw new Error('js/quality.js exports no _icsStageFromAge');"
        "var ages=[%s];"
        "var out=[];for(var i=0;i<ages.length;i++){"
        "  out.push(ctx._icsStageFromAge(ctx.RCA_ICS_TABLE,ages[i]));}"
        "process.stdout.write(JSON.stringify(out));"
        % ",".join(repr(float(ma)) for ma, _n in _AGE_TO_STAGE)
    )
    proc = subprocess.run(["node", "-e", js], cwd=str(REPO),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, "could not evaluate js/quality.js: %s" % (
        (proc.stderr or proc.stdout)[:400],)
    js_stages = json.loads(proc.stdout)
    assert len(js_stages) == len(_AGE_TO_STAGE)

    for (ma, expected), js_got in zip(_AGE_TO_STAGE, js_stages):
        py_got = ics.ics_stage_from_age(ma)
        assert js_got == expected, (
            "js/quality.js resolved %.2f Ma to %r, expected %r -- the mirror "
            "moved, re-read the table before trusting this file"
            % (ma, js_got, expected))
        assert py_got == expected, (
            "python resolved %.2f Ma to %r, expected %r.  The two transports "
            "must name the same stage." % (ma, py_got, expected))

