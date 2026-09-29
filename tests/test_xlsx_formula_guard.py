r"""_xlsx_cell re-derived the formula guard from the text instead of asking.

The two export paths detect the OWASP formula guard differently:

  CSV  : ``_sanitize_formula_cell`` prefixes ``"'"`` when the value starts with
         a trigger (= + - @ TAB CR LF). The prefixed text is the output.
  XLSX : ``_xlsx_cell`` needs a *boolean* as well as a value, and it cannot see
         the sanitiser's decision, so it re-derived it:
             guard = text.startswith("'")

Those agree only if "starts with an apostrophe" implies "was guarded right
here". It does not. A value that legitimately begins with an apostrophe was
never guarded, and the re-derivation cannot tell the two apart -- so the
apostrophe was stripped from the value AND the cell was styled quotePrefix:

    input      CSV (correct)   XLSX (before)   XLSX (now)
    'Acacia    'Acacia         Acacia          'Acacia
    'K         'K              K               'K
    'Orbitolina''Orbitolina'   Orbitolina'     'Orbitolina'
    ''         ''              '               ''
    '          '               None            '

The last row is the severity: a one-character value became an EMPTY CELL. The
others are silent corruption of exactly the kind of text this tool exists to
carry -- a leading apostrophe is a real convention (botanical 'Acacia means
"leafy", the stratigraphic 'K apperture), and quoted terms appear in
section remarks.

The fix asks the sanitiser's own question about the value it was given
(``_formula_triggered(str(v))`` -- the exact expression the sanitiser tests) so
the two paths agree by construction rather than by coincidence.

What must NOT change: a genuine trigger is still guarded on both sides, with
the apostrophe kept in the CSV text and shown-but-not-stored in the workbook.
"""


from __future__ import annotations

import copy
import io
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.exporter import (  # noqa: E402
    _cell_to_export, _formula_triggered, _sanitize_formula_cell, _xlsx_cell,
    to_csv,
)

openpyxl = pytest.importorskip("openpyxl")
from openpyxl import Workbook, load_workbook  # noqa: E402
from openpyxl.styles import Border  # noqa: E402

TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n")

# Values that legitimately begin with an apostrophe: DATA, not attacks.
LEADING_APOSTROPHE = [
    "'Acacia",
    "'K",
    "'Orbitolina'",
    "''",
    "'",
    "'=1+1",
    "'   ",
    "'''",
    "'\t",
]

# Values that must still be guarded.
REAL_TRIGGERS = [
    "=CMD(calc)",
    "=HYPERLINK(\"http://x\",\"click\")",
    "+1+1",
    "-31.2",
    "@SUM(A1)",
    "\t=1+1",
    "\r=1+1",
    "\n=1+1",
]


def _round_trip(v):
    """Write one cell exactly the way to_xlsx does, then read it back."""
    value, guard = _xlsx_cell(v)
    wb = Workbook()
    ws = wb.active
    cell = ws.cell(row=1, column=1, value=value)
    cell.border = Border()          # _style must exist before it is copied
    if guard:
        cell.data_type = "s"
        st = copy.copy(cell._style)
        st.quotePrefix = 1
        cell._style = st
    buf = io.BytesIO()
    wb.save(buf)
    back = load_workbook(buf).active.cell(row=1, column=1)
    return back.value, bool(back._style.quotePrefix)


def _csv_cell(v):
    """Parse the CSV back rather than comparing raw text.

    Two things make string surgery wrong here: csv.writer quotes any cell
    containing a comma, CR or LF, so the raw text carries quoting artefacts;
    and `splitlines()` cuts INSIDE a cell that contains a newline, so a trigger
    like "\\r=1+1" reads back as two rows. csv.reader over a StringIO handles
    both, because that is the reader the file is meant to be read with.
    """
    import csv as _csv
    import io as _io
    body = to_csv(["h"], [[v]]).lstrip("\ufeff")
    rows = list(_csv.reader(_io.StringIO(body)))
    assert len(rows) == 2, f"expected a header and one data row, got {rows!r}"
    return rows[1][0]


# --- the defect ---------------------------------------------------------


@pytest.mark.parametrize("v", LEADING_APOSTROPHE)
def test_leading_apostrophe_survives_the_workbook(v):
    """The apostrophe is the data. It must come out the other side intact and
    the cell must NOT be marked as guarded."""
    value, guard = _round_trip(v)
    assert value == v, f"value mangled: {value!r} != {v!r}"
    assert guard is False, "a value that was never a trigger was marked guarded"


@pytest.mark.parametrize("v", ["'", "''", "'''"])
def test_a_short_apostrophe_value_does_not_vanish(v):
    """The empty-cell regression, isolated.

    "'" used to become None: one character of real data, dropped silently. A
    reader of the sheet cannot tell an empty cell from a missing one.
    """
    value, _ = _round_trip(v)
    assert value is not None, f"{v!r} became an empty cell"
    assert value == v


@pytest.mark.parametrize("v", LEADING_APOSTROPHE)
def test_csv_and_xlsx_agree_on_a_leading_apostrophe(v):
    """The cross-format statement of the same fact: what the user downloads
    and what the user opens in Excel must contain the same text."""
    assert _csv_cell(v) == v, "CSV mangled it"
    value, _ = _round_trip(v)
    assert value == v, "XLSX mangled it"


# --- the direction that must NOT change ---------------------------------


@pytest.mark.parametrize("v", REAL_TRIGGERS)
def test_real_triggers_are_still_guarded_in_the_workbook(v):
    value, guard = _round_trip(v)
    assert guard is True, f"{v!r} was not guarded -- that is the security fix"
    assert value == v, f"guard must hide the apostrophe, not alter the text: {value!r}"


@pytest.mark.parametrize("v", REAL_TRIGGERS)
def test_real_triggers_are_still_guarded_in_the_csv(v):
    assert _csv_cell(v) == "'" + v, _csv_cell(v)


def test_a_guarded_workbook_cell_is_stored_as_text_not_a_formula():
    """quotePrefix is only a display hint; the value must still be a string
    cell, or openpyxl would re-derive data_type 'f' on a leading '='."""
    value, guard = _xlsx_cell("=CMD(calc)")
    assert guard is True
    wb = Workbook()
    ws = wb.active
    cell = ws.cell(row=1, column=1, value=value)
    cell.border = Border()
    cell.data_type = "s"
    st = copy.copy(cell._style)
    st.quotePrefix = 1
    cell._style = st
    buf = io.BytesIO()
    wb.save(buf)
    back = load_workbook(buf).active.cell(row=1, column=1)
    assert back.data_type == "s", back.data_type


@pytest.mark.parametrize("v", [
    "Brachiopod", "sand", "1-2 Ma", "  ", "0", "测", "a'b",
])
def test_ordinary_values_are_untouched(v):
    value, guard = _round_trip(v)
    assert guard is False
    assert value == v


def test_an_empty_string_becomes_an_empty_cell_and_that_is_fine():
    """Not a defect, pinned so it is not mistaken for one later.

    openpyxl stores "" as an empty cell, so the round trip returns None. That
    is indistinguishable from "" on screen and in any reader, it is not the
    leading-apostrophe bug, and the only thing that went wrong the first time
    was asserting "untouched" about it. So it gets its own named test.
    """
    value, guard = _round_trip("")
    assert guard is False
    assert value in ("", None)
    assert _csv_cell("") == ""


@pytest.mark.parametrize("v,expected", [
    ("=x", True), ("+x", True), ("-x", True), ("@x", True),
    ("\tx", True), ("\rx", True), ("\nx", True),
    ("'x", False), ("", False), ("x", False), (" x", False),
    ("\x0bx", False),
])
def test_the_predicate_is_the_trigger_list_and_nothing_else(v, expected):
    """The shared predicate is the whole fix; pin it directly.

    "'" and " " are the two characters that made the old inference wrong, and
    both must be False here -- that is what makes the XLSX path agree with the
    sanitiser instead of coinciding with it.

    The second assertion is "did the sanitiser actually change the value",
    which is the right way to ask. Asserting `str(result).startswith("'")`
    would be re-introducing the exact heuristic under test as a test, and it
    fails for precisely the inputs this whole change is about.
    """
    assert _formula_triggered(v) is expected
    assert (_cell_to_export(v) != v) is expected, (
        f"the sanitiser's decision disagrees with the predicate for {v!r}"
    )


def test_numbers_are_never_guarded():
    for n in (-31.2, 0, 1, 10 ** 20, -0.0):
        value, guard = _round_trip(n)
        assert guard is False
        assert value == n, f"{n!r} became {value!r}"
