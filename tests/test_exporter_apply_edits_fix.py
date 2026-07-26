"""Regression tests for P0-7: rca_core.exporter.apply_table_edits must
increment data_row_idx when skipping empty placeholder rows, otherwise
deleting a middle row causes _extras / per-row confidence fields to
shift onto the wrong subsequent row.

REVIEW-2026-07-25 P0-7.
"""
from __future__ import annotations

import copy
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.exporter import apply_table_edits


def _fresh_data():
    return {
        "species_ranges": [
            {"species": "Genus A sp.", "section": "Sec 1", "range_top": "Bed 3", "range_base": "Bed 1", "biozone": "Z1", "_extras": {"note": "A_note"}},
            {"species": "Genus B sp.", "section": "Sec 1", "range_top": "Bed 5", "range_base": "Bed 2", "biozone": "Z1", "_extras": {"note": "B_note"}},
            {"species": "Genus C sp.", "section": "Sec 1", "range_top": "Bed 7", "range_base": "Bed 4", "biozone": "Z2", "_extras": {"note": "C_note"}},
        ],
    }


class TestApplyTableEditsRowAlignment:
    def test_delete_middle_row_preserves_extras_alignment(self):
        """User deletes row 2 (Genus B). The remaining rows must still
        carry their OWN _extras, not each other's."""
        data = copy.deepcopy(_fresh_data())
        rows = [
            [1, "Genus A sp.", "Sec 1", "Bed 3", "Bed 1", "Z1", "A_note"],
            [],  # Row 2 deleted → empty placeholder
            [3, "Genus C sp.", "Sec 1", "Bed 7", "Bed 4", "Z2", "C_note"],
        ]
        out = apply_table_edits(data, "species_ranges", rows)
        rows_out = out["species_ranges"]
        assert len(rows_out) == 2, f"expected 2 rows after delete, got {len(rows_out)}: {rows_out}"
        assert rows_out[0]["_extras"].get("note") == "A_note", (
            f"Genus A's _extras misaligned: {rows_out[0].get('_extras')}"
        )
        assert rows_out[1]["_extras"].get("note") == "C_note", (
            f"Genus C's _extras misaligned: {rows_out[1].get('_extras')}"
        )

    def test_delete_first_row_preserves_extras_alignment(self):
        """User deletes row 1 (Genus A). Row 2 becomes the new first row
        but must still carry its OWN _extras."""
        data = copy.deepcopy(_fresh_data())
        rows = [
            [],  # placeholder
            [2, "Genus B sp.", "Sec 1", "Bed 5", "Bed 2", "Z1", "B_note"],
            [3, "Genus C sp.", "Sec 1", "Bed 7", "Bed 4", "Z2", "C_note"],
        ]
        out = apply_table_edits(data, "species_ranges", rows)
        rows_out = out["species_ranges"]
        assert len(rows_out) == 2
        assert rows_out[0]["_extras"].get("note") == "B_note"
        assert rows_out[1]["_extras"].get("note") == "C_note"