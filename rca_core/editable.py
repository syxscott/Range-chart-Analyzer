"""Editable extraction results.

The TableWidget in the GUI is editable in-place. When the user clicks
"Apply edits", we diff the current ``result`` dict against the original
to produce a normalized edits payload, then save it back. The edits
format mirrors the table structure (one entry per primary list, with
row indices and column names).

Example
-------
    edits = {
        "species_ranges": {
            0: {"species": "Neoalbaillella optima", "range_base": "Bed 7"},
            3: {"biozone": "Zone 2"},
        },
        "sections": {0: {"name": "Updated Section A"}},
    }
    apply_edits(result_dict, edits)
"""

from __future__ import annotations

import copy
from typing import Any

_LIST_KEYS = (
    "sections", "species_ranges", "biozones",
    "fossil_legend", "lithology_legend", "cross_beds",
    "other_fossils",
    # abundance-diagram (pollen / percentage-diagram) tables. Without these
    # keys, capture_edits() returns {} and apply_edits() no-ops on abundance
    # results — every edit a user makes on an abundance chart is silently
    # dropped on "Apply edits".
    "sites", "abundances", "zones",
)


def _coerce(value: Any) -> Any:
    """Trim whitespace; pass through everything else."""
    if isinstance(value, str):
        return value.strip()
    return value


def capture_edits(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Diff two result dicts and return a normalized edits payload.

    Only differences are recorded. New rows are emitted with the special
    key ``"_new"`` so callers can distinguish "modified" from "added".
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _LIST_KEYS:
        b = before.get(key) or []
        a = after.get(key) or []
        if not isinstance(b, list) or not isinstance(a, list):
            continue
        # Deletions (len(a) < len(b)): index-based diffing cannot represent a
        # removed row - it would silently no-op, leaving is_dirty False and
        # losing the delete on Apply (middle deletes also misalign every later
        # row). Replace the whole list so apply_edits can replay it exactly.
        # Appends (len(a) > len(b)) stay on the new_<i> path below.
        if len(a) < len(b):
            out[key] = {"_replaced": copy.deepcopy(a)}
            continue
        edits: dict[str, Any] = {}
        n = min(len(b), len(a))
        for i in range(n):
            bi = b[i] if isinstance(b[i], dict) else {}
            ai = a[i] if isinstance(a[i], dict) else {}
            cell_edits: dict[str, Any] = {}
            for col, av in ai.items():
                if col == "_extras":
                    continue   # never edit internal extras via UI
                bv = bi.get(col)
                if _coerce(av) != _coerce(bv):
                    cell_edits[col] = av
            if cell_edits:
                edits[i] = cell_edits
        # Newly appended rows: encode the whole dict so they can be
        # reconstructed verbatim.
        for i in range(n, len(a)):
            if isinstance(a[i], dict):
                edits[f"new_{i}"] = a[i]
        if edits:
            out[key] = edits
    return out


def apply_edits(result: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    """Mutate ``result`` in-place per the edits payload. Unknown rows /
    columns are silently ignored. Adds new rows at the indicated index
    for keys starting with ``"new_"``.

    Bug-4 fix: ``new_<i>`` rows are inserted in *ascending* index order.
    Inserting them in dict-iteration order shifts later insertion points
    and scrambles the final row order (e.g. capturing rows at indices
    3,4,5 then applying them out of order produced [r4,r3,r5] instead of
    the intended [r3,r4,r5]). We collect all ``new_*`` indices, sort
    numerically, then insert.
    """
    if not isinstance(result, dict) or not isinstance(edits, dict):
        return result
    for key, row_edits in edits.items():
        if key not in _LIST_KEYS:
            continue
        items = result.get(key)
        if not isinstance(items, list):
            continue
        if not isinstance(row_edits, dict):
            continue
        # Full-list replacement (deletions captured by capture_edits). Deep
        # copy so the result list doesn't alias the edits payload's nested
        # mutables (formations lists, _extras, ...).
        if "_replaced" in row_edits:
            result[key] = copy.deepcopy(row_edits["_replaced"])
            continue
        # Partition into modifications vs insertions so we can sort the
        # insertions by target index before mutating the list.
        modifications: list[tuple[int, dict[str, Any]]] = []
        insertions: list[tuple[int, dict[str, Any]]] = []
        for idx, cell_edits in row_edits.items():
            idx_str = str(idx)
            if idx_str.startswith("new_"):
                try:
                    insert_at = int(idx_str[4:])
                except ValueError:
                    continue
                if isinstance(cell_edits, dict):
                    # Deep copy so the inserted row doesn't alias the edits
                    # payload's nested mutables (e.g. formations list).
                    insertions.append((insert_at, copy.deepcopy(cell_edits)))
                continue
            try:
                i = int(idx)
            except (TypeError, ValueError):
                continue
            if isinstance(cell_edits, dict):
                modifications.append((i, cell_edits))
        # Apply modifications first (in-place field updates don't shift
        # other indices). Skip indices that have fallen off the end
        # (e.g. row was deleted by another field's edit).
        for i, cell_edits in modifications:
            if i < 0 or i >= len(items):
                continue
            item = items[i]
            if not isinstance(item, dict):
                # Promote scalar row to dict so we can store cell edits.
                items[i] = {"value": item}
                item = items[i]
            for col, val in cell_edits.items():
                if col == "_extras":
                    continue
                # Deep copy so a list-typed value (e.g. formations) written
                # onto the result isn't shared with the edits payload.
                item[col] = copy.deepcopy(val)
        # Then apply insertions in ascending index order. After each
        # insert, indices >insert_at shift by +1, but since we sort, each
        # subsequent insertion targets the post-shift index that matches
        # its original intent.
        insertions.sort(key=lambda x: x[0])
        for insert_at, payload in insertions:
            # list.insert clamps out-of-range indices to the ends, which
            # matches the existing behavior for new_* rows.
            items.insert(insert_at, payload)
    return result


def is_dirty(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """True when ``after`` differs from ``before`` on any editable field."""
    return bool(capture_edits(before, after))


def new_row_template(list_key: str) -> dict[str, Any]:
    """Return an empty row with the columns the UI expects for ``list_key``.
    Used when the user clicks 'Add row' on a table."""
    templates = {
        "sections": {
            "name": "", "age_range": "", "formations": [],
            "formation_thickness_m": "", "coordinates": "",
        },
        "species_ranges": {
            "species": "", "section": "", "range_top": "",
            "range_base": "", "biozone": "",
        },
        "biozones": {"name": "", "section": "", "age": "", "thickness_m": ""},
        "fossil_legend": {"marker": "", "meaning": ""},
        "lithology_legend": {"pattern": "", "meaning": ""},
        "cross_beds": {
            "from_section": "", "from_bed_idx": None,
            "to_section": "", "to_bed_idx": None,
        },
        "other_fossils": {"text": ""},
        # Abundance-diagram tables (pollen / percentage-diagram).
        "sites": {
            "name": "", "location": "", "age_range": "", "depth_unit": "",
        },
        "abundances": {
            "taxon": "", "site": "", "level": "", "depth": "",
            "abundance": "", "abundance_unit": "",
        },
        "zones": {
            "name": "", "age": "", "level_range": "",
        },
    }
    return copy.deepcopy(templates.get(list_key, {}))
