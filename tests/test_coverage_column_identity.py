"""AUDIT-2026-10-01 [item 26]: the coverage column ladder never learned ``group``.

The defect, measured on a committed gold fixture
(``tests/fixtures/real_payloads/Clarke_LL_et_al_2020_
DNA-based_diet_analysis_o_p006_ve3.json``, scatter_plot, ``points``):

* all 44 rows carry ``group`` -- five real taxon names (``Bathylagus sp.``,
  ``E. antarctica``, ``G. braueri``, ``G. nicholsi``, ``G. opisthopterus``)
* all 44 rows carry an EMPTY ``label`` (the table's own primary column key)
* ``_COVERAGE_EXTRA_COLUMN_KEYS`` = (taxon, species, sample_id, label, name)
  names none of them, so ``coverage_ledger`` built an EMPTY grid:
  ``cells: 0``, ``honest_coverage: 0.0``, ``columns: []``,
  ``unattributed_rows: 44`` -- while the same report's rollup said
  ``contracted_rows: 44`` and listed 44 coverage decisions, every one of them
  with ``row: ""``.

So one audit report asserted, in the same ``coverage`` block, both "44 cells
were answered" and "0 cells, 0.0 coverage".  The module's own doctrine
(``reason_codes.py``: "a silent omission has to stay detectable as one, which
is the whole point of the ledger") is about exactly this: a tool that cannot
index its own rows must not be allowed to speak in the same voice as a tool
that checked them and found nothing.

``group`` is not a fixture-local key -- it is a first-class product field:
``extractor.py`` puts it in ``_KNOWN_SCATTER_POINT_KEYS`` (line 4066) and the
scatter normaliser writes it (line 4162), and ``aggregate.py`` names it in
``COLUMNAR_SECTION_SCHEMA.primary_id_keys``.  For a scatter plot the group IS
the taxon, which is exactly what a ledger "column" is.

Deliberately NOT fixed here: the chem-profile fixture's ``stage`` key.  A
stratigraphic stage is a stratigraphic index, not a taxon; promoting it to a
"column" would turn ``cells: 0`` into ``cells: 12`` / ``coverage: 1.0`` and
make the number look finished without the semantics behind it.  That table has
no column identity, and the honest signal for it is ``unattributed_rows``, which
``test_chem_profile_reports_its_gap_instead_of_inventing_a_column`` pins.

The ladder is duplicated as a literal in ``js/quality.js`` (inside
``rcaCoverageFor``), so these tests also pin the two copies to the same key set:
a one-sided edit is the drift this project has already paid for once
(FIX-2026-09-22 audit item 6).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from rca_core.quality import (
    _COVERAGE_EXTRA_COLUMN_KEYS,
    coverage_column_keys,
    coverage_for,
    select_coverage_table,
)
from rca_core.report import build_extraction_report

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "real_payloads"

CLARKE = ("Clarke_LL_et_al_2020_DNA-based_diet_analysis_o_p006_ve3.json")
BOLE = ("Bole_et_al_2020_SIMS_analysis_of_Si_isotope_fo_p008_ra6.json")

#: The five taxon names that fixture's ``group`` column actually carries, and
#: the exact ledger size they must produce (44 rows collapse onto 5 taxa).
CLARKE_TAXA = {
    "Bathylagus sp.",
    "E. antarctica",
    "G. braueri",
    "G. nicholsi",
    "G. opisthopterus",
}


def _payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The ladder itself
# ---------------------------------------------------------------------------

def test_group_is_in_the_python_ladder():
    assert "group" in _COVERAGE_EXTRA_COLUMN_KEYS, (
        "the coverage ladder still omits `group`, a first-class product "
        "column key (extractor.py:4066 / 4162, aggregate.py:550)")


def test_js_ladder_carries_the_same_keys():
    """js/quality.js hardcodes the ladder; it must not drift from Python."""
    text = (REPO / "js" / "quality.js").read_text(encoding="utf-8")
    match = re.search(
        r"columnKeys:\s*\[primaryColumn,\s*([^\]]*?)\]", text, re.S)
    assert match, "js/quality.js no longer builds a coverage columnKeys ladder"
    js_keys = re.findall(r"'([^']+)'", match.group(1))
    assert js_keys == list(_COVERAGE_EXTRA_COLUMN_KEYS), (
        "js/quality.js ladder %r != rca_core.quality %r"
        % (js_keys, list(_COVERAGE_EXTRA_COLUMN_KEYS)))


def test_group_is_lowest_priority():
    """Appended, not inserted: a row that HAS its own label must still use it."""
    ladder = coverage_column_keys("label")
    assert ladder == ("label", "taxon", "species", "sample_id", "name", "group")
    assert ladder.index("group") == len(ladder) - 1


# ---------------------------------------------------------------------------
# The measured consequence, on a real committed payload
# ---------------------------------------------------------------------------

def test_scatter_points_are_attributed_by_group():
    data = _payload(CLARKE)
    assert select_coverage_table(data) == ("points", "label")

    ledger = coverage_for(data)
    assert ledger is not None
    assert {c["column"] for c in ledger["columns"]} == CLARKE_TAXA
    assert len(ledger["columns"]) == 5

    totals = ledger["totals"]
    assert totals["cells"] == 5
    assert totals["extracted"] == 5
    assert totals["honest_coverage"] == 1.0
    assert ledger["unattributed_rows"] == 0, (
        "all 44 rows carry `group`; none may be left unattributed")


def test_report_decisions_name_the_row_they_describe():
    """The evidence chain must be auditable: find the row from the decision."""
    cov = build_extraction_report(data=_payload(CLARKE),
                                  mode="scatter_plot")["coverage"]
    decisions = cov["decisions"]
    assert cov["contracted_rows"] == 44
    assert len(decisions) == 44
    assert [d["row"] for d in decisions].count("") == 0, (
        "a decision with an empty `row` cannot be audited against the result")
    assert {d["row"] for d in decisions} == CLARKE_TAXA
    assert cov["ledger"]["totals"]["cells"] == 5


def test_chem_profile_reports_its_gap_instead_of_inventing_a_column():
    """A chem profile has no taxon column. Say so; do not fake coverage 1.0."""
    data = _payload(BOLE)
    assert select_coverage_table(data) == ("data_points", "sample_id")

    ledger = coverage_for(data)
    assert ledger["totals"]["cells"] == 0, (
        "`stage` is a stratigraphic index, not a taxon: promoting it to a "
        "ledger column would report coverage this table does not have")
    assert ledger["unattributed_rows"] == 29
    # ...and the gap must be VISIBLE, not just implied by cells == 0.
    cov = build_extraction_report(data=data, mode="chemical_strat")["coverage"]
    assert cov["contracted_rows"] == 29
    assert cov["ledger"]["unattributed_rows"] == 29
