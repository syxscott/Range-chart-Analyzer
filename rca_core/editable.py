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

REVIEW-2026-09-20 adds a second edit form: ``{"_deleted_keys": ["biozone"]}``
inside a row edit removes fields the user cleared (the previous per-cell diff
only ever walked the AFTER row, so a cleared field survived the replay and the
resulting row combined values from both versions).
"""

from __future__ import annotations

import copy
from typing import Any

from .exporter import COL_TYPES, _find_cfg, get_config

_LIST_KEYS = (
    "sections", "species_ranges", "biozones",
    "fossil_legend", "lithology_legend", "cross_beds",
    "other_fossils",
    # abundance-diagram (pollen / percentage-diagram) tables. Without these
    # keys, capture_edits() returns {} and apply_edits() no-ops on abundance
    # results — every edit a user makes on an abundance chart is silently
    # dropped on "Apply edits".
    "sites", "abundances", "zones",
    # REVIEW-2026-09-10: phylogenetic-tree and zonation tables were missing,
    # so capture_edits() returned {} for any edit confined to them and
    # apply_edits() silently no-op'd — the same class of bug the abundance
    # addition above fixed. report.py already tracks all three keys.
    "nodes", "zonations", "correlations",
    # REVIEW-2026-09-20: the columnar sub-tables. In a columnar result these
    # lists are NESTED (``sections[i]["lithology_blocks"]``) and their edits
    # are already captured through the ``sections`` row diff above — but a
    # payload produced by the old exporter Apply-edits path carried them as
    # bogus TOP-LEVEL keys (rca_core/exporter.py used to write
    # ``data["lithology_blocks"]``, which nothing in the model reads). Listing
    # the ids here means such a legacy payload still round-trips instead of
    # the diff quietly ignoring a list it does not recognise.
    "lithology_blocks", "age_units", "samples",
)

# Rows that are plain scalars, not dicts (normalize_result emits
# ``other_fossils`` as a list of fossil names). The "new row" template has to
# match, or the GUI inserts a dict into a string list.
_SCALAR_LIST_KEYS = ("other_fossils",)

# Row-edit key that lists the columns capture_edits saw DISAPPEAR between
# before/after. ``apply_edits`` deletes them on replay.
_DELETED_KEYS = "_deleted_keys"


def _default_for(table_id: str, data_key: str) -> Any:
    """A model-native empty value for one column, from COL_TYPES."""
    t = COL_TYPES.get(table_id, {}).get(data_key, "str")
    if t == "list":
        return []
    if t in ("int", "float", "number", "bool_yn", "nullable_str"):
        # None means "not filled in" for every one of these; the renderers
        # already map None -> "" and None -> "N".
        return None
    return ""


def _template_from_cfg(cfg: dict[str, Any] | None, list_key: str) -> dict[str, Any]:
    """Build a row template from a table config's column metadata.

    REVIEW-2026-09-20: the templates used to be a hand-maintained dict here,
    duplicating (and drifting from) the exporter's TABLE_CONFIGS:
    ``other_fossils`` got ``{"text": ""}`` while the model row is a plain
    string / ``fossil`` key, and the columnar ``sections`` template carried
    the RANGE-CHART fields (name / age_range / formations), so "Add row" on a
    columnar section created a row with none of the columns that table shows
    (id / group / thickness_m / coordinates_text). Driving the template off
    ``data_keys`` + ``COL_TYPES`` removes the second source of truth.
    """
    if not cfg:
        return {}
    keys = list(cfg.get("data_keys") or cfg.get("cols") or [])
    table_id = cfg.get("id") or list_key
    out: dict[str, Any] = {}
    for k in keys:
        if k == "agreement":
            continue  # computed at merge time, never typed
        out[k] = _default_for(table_id, k)
    return out


def new_row_template(list_key: str, data: dict[str, Any] | None = None) -> Any:
    """Return an empty row for ``list_key`` — the columns the UI shows.

    Used when the user clicks 'Add row' on a table.

    ``data`` (REVIEW-2026-09-20) is the result being edited: the table id
    ``sections`` means two different schemas (range-chart vs columnar), so
    only the result's shape can pick the right one. Without it the
    range-chart preset is used, as before.

    Returns a plain ``""`` for a scalar list key (``other_fossils``).
    """
    if list_key in _SCALAR_LIST_KEYS:
        return ""
    cfg = _find_cfg(list_key, data) if data else get_config(list_key)
    if cfg is None:
        return {}
    tpl = _template_from_cfg(cfg, list_key)
    return tpl



def _coerce(value: Any) -> Any:
    """Trim whitespace; pass through everything else."""
    if isinstance(value, str):
        return value.strip()
    return value


#: The business key of a row, per list — a PRIORITY LIST of candidate key
#: tuples, strongest first. Mirrors the primary-id idea in
#: ``rca_core.extractor._PRIMARY_ID_KEYS`` and the identity keys in
#: ``rca_core.exporter``. Used to ALIGN rows across a diff so an insertion
#: cannot shift a neighbouring row's edit onto it.
#:
#: AUDIT-2026-09-27: the first version demanded that EVERY key of a single
#: tuple be present, which broke plain ``[{"name": "a"}, ...]`` section lists
#: (no ``id``): nothing was identifiable, every row looked new, and appending
#: three rows produced four ``new_*`` entries on top of an existing row. The
#: key sets are therefore tried strongest-first, and a weaker single-field key
#: is accepted only when its value is UNIQUE among the before-rows.
_ROW_ID_KEYS = {
    "species_ranges": [("species", "section"), ("species",)],
    "sections": [("id",), ("name",)],
    "biozones": [("name", "section"), ("name",)],
    "abundances": [("taxon", "site", "level"), ("taxon", "level"), ("taxon",)],
    "sites": [("name",)],
    "zones": [("name",)],
    "zonations": [("name",)],
    "correlations": [("from_zone", "to_zone"), ("from_zone",)],
    "nodes": [("id",)],
}


def _row_key(row: Any, id_keys: list) -> tuple:
    """The strongest usable identity key for *row*, or () if it has none.

    Key sets are tried in priority order. A single-field key is accepted only
    when its value is unique among the rows being aligned, so two taxa that
    share a species name in different sections are never paired by the weak
    fallback when the strong key was available.
    """
    if not isinstance(row, dict):
        return ("\x00scalar", str(row))
    for keys in id_keys or [()]:
        if not keys:
            continue
        parts = []
        for k in keys:
            v = row.get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                parts = None
                break
            parts.append(str(v).strip().lower())
        if parts:
            return tuple(parts)
    return ()


def _align_rows(b: list, a: list, id_keys: list) -> list:
    """Pair up ``b``/``a`` rows, in ``a``'s order, as ``(b_idx | None, a_idx)``.

    AUDIT-2026-09-27 P1: the diff was purely POSITIONAL, so a mid-table insert
    could not be expressed. Executed: ``[Alpha, Beta, Gamma]`` with ``Delta``
    inserted at index 2 came back as "row 2 changed" plus "append a copy of the
    old last row", and applying it produced
    ``[Alpha, Beta, Delta(_extras='Fig Gamma'), Gamma(_extras='Fig Gamma')]`` —
    a FABRICATED plate figure on a new taxon and a DUPLICATED one. A pure
    re-sort was worse still: ``Alpha`` kept ``Beta``'s ``_extras``.

    The root cause is that ``_extras`` is deliberately excluded from the cell
    diff (``continue`` below), so it stays welded to the POSITIONAL slot and
    follows whatever row slides into it. Aligning by identity first fixes both
    the insert and the re-sort.

    Two passes, and the second one is what keeps this compatible with the JS
    mirror (``js/table.js``) and with every pre-existing caller:

    1. pair what identity can decide (a weak single-field key is used only
       when its value is unambiguous across the before-rows);
    2. pair everything still unpaired POSITIONALLY, in order — i.e. exactly the
       old behaviour for whatever the identity pass could not decide.

    Only an ``a``-row left with no ``b``-row at all is genuinely new. An
    earlier version returned ``(None, j)`` for every unmatched row and the
    caller then emitted ``new_<j>`` for each, which turned a plain two-row
    cell edit into "two brand new rows" and broke parity with the JS engine.
    """
    pairs: list = [None] * len(a)
    claimed_b: set = set()

    b_index: dict = {}
    weak_counts: dict = {}
    for i, row in enumerate(b):
        k = _row_key(row, id_keys)
        if not k:
            continue
        b_index.setdefault(k, []).append(i)
        if len(k) == 1:
            weak_counts[k] = weak_counts.get(k, 0) + 1

    # Pass 1 - identity.
    for j, row in enumerate(a):
        k = _row_key(row, id_keys)
        if not k or not b_index.get(k):
            continue
        if len(k) == 1 and weak_counts.get(k, 0) > 1:
            continue          # ambiguous single-field key: leave it to pass 2
        for i in b_index[k]:
            if i not in claimed_b:
                claimed_b.add(i)
                pairs[j] = (i, j)
                break

    # Pass 2 - positional, for whatever pass 1 left undecided.
    free_b = [i for i in range(len(b)) if i not in claimed_b]
    cursor = 0
    for j in range(len(a)):
        if pairs[j] is not None:
            continue
        if cursor < len(free_b):
            i = free_b[cursor]
            cursor += 1
            claimed_b.add(i)
            pairs[j] = (i, j)
        else:
            pairs[j] = (None, j)      # genuinely beyond the end -> a new row

    # Before-rows no after-row claimed are deletions. The caller already
    # replaces the whole list when it shrank, so these are only reported.
    for i in range(len(b)):
        if i not in claimed_b:
            pairs.append((i, None))
    return pairs


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
        # AUDIT-2026-09-27 P1: align by row identity first, so an insertion or
        # a re-sort cannot slide one taxon's edit onto its neighbour. Falls
        # back to the positional pairing when the rows carry no usable key
        # (scalar rows, or objects with an empty identifier).
        pairs = _align_rows(b, a, _ROW_ID_KEYS.get(key, ()))
        aligned = any(bi is not None and bi != ai for bi, ai in pairs)
        # Capture scalar-row edits via _replaced so lists like
        # other_fossils (a list of plain strings) are not silently
        # dropped. Per-cell diffing is impossible for non-dict rows
        # (bi/ai both default to {} and the diff loop never fires),
        # so a list-level replacement is the correct edit encoding.
        scalar_changed = False
        for bi_i, ai_i in pairs:
            if bi_i is None:
                # A row that exists only in `after`: a genuine insertion.
                # Recorded at its own position so apply_edits puts it there.
                #
                # AUDIT-2026-09-27 P1: a SCALAR row cannot be encoded as
                # `new_<i>` -- the payload is a dict, and apply_edits drops a
                # non-dict insertion on the floor -- so it was not recorded at
                # all. Adding one fossil to `other_fossils` produced an EMPTY
                # edits payload, is_dirty() returned False, and the row was
                # silently discarded on Save. The repair for a scalar row is
                # the list-level replacement, which is exactly what the
                # "both rows are scalars" branch below already uses.
                if ai_i is not None:
                    if isinstance(a[ai_i], dict):
                        edits[f"new_{ai_i}"] = copy.deepcopy(a[ai_i])
                    else:
                        scalar_changed = True
                continue
            if ai_i is None:
                continue   # deletion; the shrink case above replaced the list
            i = bi_i
            bi = b[i] if isinstance(b[i], dict) else {}
            ai = a[ai_i] if isinstance(a[ai_i], dict) else {}
            # Row-type mismatch (dict vs scalar): cannot merge cell
            # edits; fall through to list-replacement semantics.
            if isinstance(b[i], dict) != isinstance(a[ai_i], dict):
                scalar_changed = True
                continue
            if not isinstance(a[ai_i], dict):
                # Both rows are scalars — compare values directly.
                if _coerce(a[ai_i]) != _coerce(b[i]):
                    scalar_changed = True
                continue
            cell_edits: dict[str, Any] = {}
            # REVIEW-2026-09-20: the diff runs over the UNION of both rows'
            # keys. It used to walk only ``ai`` (the after row), so a field
            # the user CLEARED — normalize_result drops empty keys, and
            # "delete this cell" in the GUI removes them — was never recorded:
            # replaying the edit left the old value in place and produced a
            # CHIMERIC row (before's deleted field + after's other fields),
            # i.e. a row that exists in neither version and may carry a stale
            # range_top against a new range_base. Deletions now travel as a
            # ``_deleted_keys`` list which apply_edits replays with pop().
            for col in list(bi) + [k for k in ai if k not in bi]:
                if col == "_extras":
                    continue   # never edit internal extras via UI
                if col in ai and col in bi:
                    if _coerce(ai[col]) != _coerce(bi[col]):
                        cell_edits[col] = ai[col]
                elif col in ai:
                    bv = None
                    av = ai[col]
                    if _coerce(av) != _coerce(bv):
                        cell_edits[col] = av
                else:  # present before, gone after -> a deletion
                    deleted = cell_edits.setdefault(_DELETED_KEYS, [])
                    deleted.append(col)
            if cell_edits:
                edits[i] = cell_edits
        if scalar_changed:
            # Copy the entire 'after' list so apply_edits can replay it.
            edits["_replaced"] = copy.deepcopy(a)
        # AUDIT-2026-09-27 P1: the old loop here emitted `new_<i>` only for
        # i >= len(b), i.e. rows appended at the END. A mid-table insert is
        # now emitted by the alignment loop above at its real position, so
        # this fallback is kept only for the case where alignment could not
        # pair anything at all (unidentifiable rows) — and it is deep-copied
        # like the other branch, so a later in-place edit cannot alias.
        if not aligned:
            for i in range(n, len(a)):
                if isinstance(a[i], dict):
                    edits[f"new_{i}"] = copy.deepcopy(a[i])
                elif not scalar_changed:
                    # AUDIT-2026-09-27 P1: same reason as the alignment loop --
                    # a trailing SCALAR row is not encodable as `new_<i>`, so
                    # appending one fossil to `other_fossils` fell out of the
                    # fallback and was dropped. Route it to the replacement.
                    # `scalar_changed` is consumed ABOVE this block, so the
                    # replacement is written here rather than by setting it.
                    scalar_changed = True
                    edits["_replaced"] = copy.deepcopy(a)
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
                if col == _DELETED_KEYS:
                    # REVIEW-2026-09-20: replay a field removal (see
                    # capture_edits). Without this branch the list would be
                    # written onto the row as a bogus ``_deleted_keys`` field.
                    if isinstance(val, (list, tuple)):
                        for drop in val:
                            if isinstance(drop, str):
                                item.pop(drop, None)
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

