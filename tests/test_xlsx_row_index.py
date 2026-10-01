r"""to_xlsx skipped blank rows but kept numbering over the unfiltered sequence.

The skip is deliberate and documented:

    # Blank placeholder row (an empty other_fossils entry, a Qt
    # phantom): not written, and NOT counted.

"NOT counted" was not true. The index cell is produced by ``_export_grid``,
which numbers rows as it walks the unfiltered item list, so dropping a row
here left a hole in the numbers that were already baked in:

    other_fossils = ["Brachiopod", "", "   ", "Trilobite"]

    before:  # | fossil          after:  # | fossil
             1 | Brachiopod              1 | Brachiopod
             4 | Trilobite               2 | Trilobite

A workbook whose index column reads 1, 4 says "two rows were deleted", which
is a different and wrong story from "two blank rows were omitted" -- and the
operator reading the sheet has no way to tell which happened.

This is only about the INDEX. Whether the CSV should also drop blank rows is a
separate question and deliberately not answered here: build_table_export feeds
the on-screen table, the clipboard TSV and the CSV alike, so changing it
changes what the user sees as well as what they download. That is a product
decision; the measurement behind it is in the PR description.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import exporter  # noqa: E402
from rca_core.exporter import build_table_export, to_xlsx  # noqa: E402

openpyxl = pytest.importorskip("openpyxl", reason="to_xlsx needs openpyxl")


def _range_chart(**extra):
    payload = {
        "schema": "range_chart", "mode": "range_chart",
        "sections": [{"name": "Sec", "age_range": "1-2 Ma",
                      "species": [{"taxon": "T", "range": "1-2 Ma"}]}],
    }
    payload.update(extra)
    return payload


def _sheet(data, table_id, *, include_index=True):
    """Read one table's sheet back out of a real workbook."""
    cfg = exporter._find_cfg(table_id, data)
    assert cfg is not None, f"no config for {table_id!r}"
    want = exporter._xlsx_sheet_name(cfg.get("title_key") or cfg["id"])
    buf = io.BytesIO()
    to_xlsx(data, buf, include_index=include_index)
    wb = openpyxl.load_workbook(buf)
    assert want in wb.sheetnames, f"{want!r} not in {wb.sheetnames}"
    return [[c.value for c in row] for row in wb[want].iter_rows()]


# --- the defect ---------------------------------------------------------


@pytest.mark.parametrize("items,expected_index,expected_data", [
    (["Brachiopod", "", "   ", "Trilobite"], [1, 2], ["Brachiopod", "Trilobite"]),
    (["A", "", "B"], [1, 2], ["A", "B"]),
    (["A", "  ", "  ", "B"], [1, 2], ["A", "B"]),
    (["A", "", "B", "", "C"], [1, 2, 3], ["A", "B", "C"]),
    (["A", "", "B", "", "C", "", "D"], [1, 2, 3, 4], ["A", "B", "C", "D"]),
    (["", "A"], [1], ["A"]),
    (["A", ""], [1], ["A"]),
    (["A", "", ""], [1], ["A"]),
])
def test_index_is_renumbered_over_the_rows_actually_written(
        items, expected_index, expected_data):
    data = _range_chart(other_fossils=items)
    grid = _sheet(data, "other_fossils")
    header, rows = grid[0], grid[1:]
    assert header[0] == "#", header
    assert [r[0] for r in rows] == expected_index, (
        f"index column is { [r[0] for r in rows] }, expected {expected_index} "
        f"for other_fossils={items!r}"
    )
    assert [r[1] for r in rows] == expected_data, rows


def test_index_is_contiguous_with_no_holes():
    """The general property, stated once: 1..N with nothing skipped."""
    data = _range_chart(other_fossils=["A", "", "B", "", "", "C", "D", ""])
    rows = _sheet(data, "other_fossils")[1:]
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1)), rows


def test_no_blank_row_itself_reaches_the_sheet():
    data = _range_chart(other_fossils=["A", "", "   ", "B"])
    for row in _sheet(data, "other_fossils")[1:]:
        cells = [c for c in row[1:] if c is not None and str(c).strip()]
        assert cells, f"a blank row reached the sheet: {row}"


# --- the direction that must NOT change ---------------------------------


def test_a_table_with_no_blanks_is_untouched():
    """The renumbering is a no-op when nothing is filtered, which is the common
    case -- so this is what says the fix did not quietly renumber everything."""
    data = _range_chart(other_fossils=["A", "B", "C"])
    rows = _sheet(data, "other_fossils")[1:]
    assert [r[0] for r in rows] == [1, 2, 3]
    assert [r[1] for r in rows] == ["A", "B", "C"]


def test_include_index_false_still_writes_every_nonblank_row():
    """Without an index column there is nothing to renumber, and the rows
    themselves must be unaffected."""
    data = _range_chart(other_fossils=["A", "", "B"])
    grid = _sheet(data, "other_fossils", include_index=False)
    assert grid[0][0] != "#", grid[0]
    assert [r[0] for r in grid[1:]] == ["A", "B"], grid


def test_the_index_matches_what_the_shared_row_builder_numbers():
    """Cross-check against the other side of the exporter.

    Where nothing is filtered, the XLSX index and the CSV index must agree
    exactly -- they come from the same _export_grid enumerate(). After the fix
    they agree on the non-blank subset too, which is the whole point.
    """
    items = ["A", "B", "C"]
    data = _range_chart(other_fossils=items)
    headers, text_rows = build_table_export(data, "other_fossils", lambda k: k)
    xlsx_rows = _sheet(data, "other_fossils")[1:]
    assert [r[0] for r in xlsx_rows] == [int(r[0]) for r in text_rows]
    assert [r[0] for r in xlsx_rows] == list(range(1, len(items) + 1))
