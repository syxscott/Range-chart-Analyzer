"""Table configuration + CSV / TSV / JSON / XLSX export helpers.

The table configs are the single source of truth for both the GUI tables
and the export columns, so exported columns always match what is shown.

Two modes: range-chart (default) and columnar-section. The shape of the
result dict selects which table set is used.

XLSX support requires ``openpyxl``; if it's missing the GUI falls back
to a clear error message so the user knows which dep to install.
"""

from __future__ import annotations

import copy
import csv
import io
import math
import re
import sys
from decimal import Decimal as _Decimal
from typing import Any, Callable

# M-1 fix (REVIEW-2026-07-25): shared bed parser. Previously
# eval_metrics.py and exporter.py carried two independent
# implementations that disagreed on subscript handling — a
# predicted Bed 23c and a ground-truth Bed 23d would both be reduced
# to the integer 23, inflating accuracy. Both now route through
# rca_core.bed_parser.parse_bed so the exporter and the quality
# scorer always agree.
from .bed_parser import parse_bed as _parse_bed_impl  # noqa: E402  (post-import hook)


def _parse_bed(value):
    """Module-level alias preserved for any tests that monkey-patch here."""
    return _parse_bed_impl(value)


# Backward-compat alias kept for tests that import ``_BED_PATTERN``.
_BED_PATTERN = re.compile(r"^Bed\s*(\d+)\s*([a-zA-Z]*)$|^(?:Bed\s*)?(\d+)\s*([a-zA-Z]*)$")

# P2-5 (REVIEW-2026-07-25): scientific invariants that all exported
# range-chart data MUST satisfy before being written to CSV / xlsx /
# JSON. Set as module-level constants so they can be referenced from
# validators and quality.py alike.
EXPORT_INVARIANTS = {
    "species_ranges": {
        # REVIEW-2026-09-20: ONLY ``species`` is export-blocking. The
        # extractor legitimately writes ``""`` for a legal single-section
        # row (no section header to attribute it to) and for a single-
        # endpoint range (an open-ended FAD or LAD — "range extends past
        # the top of the section"), so treating an empty string as a
        # missing REQUIRED field made ``validate_export_invariants`` fail
        # on perfectly valid results and ``to_xlsx`` raised ValueError —
        # i.e. whole-workbook export died on valid data. A blank section
        # or endpoint is a DATA-QUALITY concern (rca_core/quality.py
        # reports it); for export it is a non-blocking warning.
        "required": ["species"],
        # Reported as warnings (see ``validate_export_invariants``): they
        # belong in the report the caller gets back, never in a raise.
        "warn_if_missing": ["section", "range_base", "range_top"],
        # Numeric coherence: FAD <= LAD. Strings that fail to parse
        # are ignored (exporter may legitimately leave them as labels).
        #
        # Sprint B (REVIEW-2026-09-04): removed "has_biozone_or_age" — the
        # constraint referenced an ``age`` key that species rows NEVER carry
        # (extractor.normalize_result only emits optional ``biozone``), so
        # every biozone-less result — perfectly legal per the extraction
        # schema — failed validate_export_invariants and to_xlsx raised,
        # breaking XLSX export wholesale. A missing biozone is a quality
        # concern (see rca_core/quality.py), not an export-blocking one.
        "constraints": ["range_base_le_range_top"],
    },
    "biozones": {
        "required": ["name"],
    },
    "sections": {
        "required": ["name"],
    },
}


def validate_export_invariants(
    data: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(ok, issues, warnings)``.

    ``issues`` are BLOCKING defects: a row that cannot be exported without
    stating something scientifically wrong (a species row with no species,
    an inverted FAD/LAD pair). Each entry is a dict with ``table``,
    ``row_index`` plus ``missing`` / ``constraint``.

    ``warnings`` (REVIEW-2026-09-20) are NON-BLOCKING gaps: a missing
    ``section`` / ``range_base`` / ``range_top`` on an otherwise valid row.
    The extractor emits ``""`` there for legal single-section and single-
    endpoint ranges, so refusing to export would destroy the user's file
    for a value that is simply absent.

    P2-5: this is an ENTRY validator — calling it before to_csv/to_tsv/
    to_xlsx surfaces data-integrity violations before they reach the
    user's downloaded file.

    Sprint B (REVIEW-2026-09-04): clarified the contract — THIS function
    never raises and never blocks an export; it only reports. The only
    hard consumer is ``to_xlsx``, which raises ValueError when ``ok`` is
    False (so a broken workbook is never written). ``to_csv`` / ``to_tsv``
    still run to completion so the user can see what's wrong, and the GUI
    can badge the result with a 'had invariants failures' flag.

    REVIEW-2026-09-20: the return gained the third element (warnings).
    Call sites unpack three values now — ``ok, issues, warnings = ...``.
    """
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    # REVIEW-2026-09-10: the `sections` table id is shared by two schemas.
    # Range-chart sections carry `name`; columnar-section sections carry `id`
    # (plus group/thickness_m/coordinates_text) and NEVER a `name`, so the
    # fixed `required: ["name"]` failed every columnar result and to_xlsx
    # raised ValueError - XLSX export was dead for the whole columnar mode
    # while the same data exported to CSV/JSON fine.
    columnar = _looks_columnar(data)
    for table_id, spec in EXPORT_INVARIANTS.items():
        rows = data.get(table_id) or []
        if not isinstance(rows, list):
            continue
        for ridx, row in enumerate(rows):
            if not isinstance(row, dict):
                # other_fossils plain-string row is allowed.
                if table_id == "species_ranges" or "required" in spec:
                    if not isinstance(row, dict):
                        issues.append({
                            "table": table_id,
                            "row_index": ridx,
                            "constraint": "not_a_dict",
                        })
                continue
            required = spec.get("required", [])
            if table_id == "sections" and columnar:
                required = ["id"]
            for key in required:
                v = row.get(key)
                if v is None or (isinstance(v, str) and not v.strip()):
                    issues.append({
                        "table": table_id, "row_index": ridx,
                        "missing": key,
                    })
            # REVIEW-2026-09-20: non-blocking gaps — reported so the GUI /
            # server can badge the export, but never raised about (an empty
            # section / open range endpoint is legal extractor output).
            for key in spec.get("warn_if_missing", []):
                v = row.get(key)
                if v is None or (isinstance(v, str) and not v.strip()):
                    warnings.append({
                        "table": table_id, "row_index": ridx,
                        "missing": key, "severity": "warning",
                    })
            for constraint in spec.get("constraints", []):
                if constraint == "range_base_le_range_top":
                    # P0-7 fix: Bed strings ("Bed 23c", "23c") must not be forced
                    # through float() — they carry a subscript that float cannot
                    # represent. We parse them structurally so numeric beds (23)
                    # and sub-beds (c) are compared correctly.
                    base_val = row.get("range_base")
                    top_val = row.get("range_top")
                    base_parsed = _parse_bed(base_val)
                    top_parsed = _parse_bed(top_val)
                    try:
                        if base_parsed is not None and top_parsed is not None:
                            # Both are Bed strings: compare bed_num first, then
                            # alphabetic subscript as tiebreaker (bed 23a < bed 23b).
                            if top_parsed["bed_num"] < base_parsed["bed_num"]:
                                issues.append({
                                    "table": table_id, "row_index": ridx,
                                    "constraint": constraint,
                                    "base": base_val, "top": top_val,
                                })
                            elif (top_parsed["bed_num"] == base_parsed["bed_num"]
                                  and top_parsed["bed_sub"] < base_parsed["bed_sub"]):
                                issues.append({
                                    "table": table_id, "row_index": ridx,
                                    "constraint": constraint,
                                    "base": base_val, "top": top_val,
                                })
                        elif base_parsed is not None and top_parsed is None:
                            # base is Bed, top is plain numeric: compare bed_num.
                            top_num = float(top_val)
                            if top_num < base_parsed["bed_num"]:
                                issues.append({
                                    "table": table_id, "row_index": ridx,
                                    "constraint": constraint,
                                    "base": base_val, "top": top_val,
                                })
                        elif base_parsed is None and top_parsed is not None:
                            # base is plain numeric, top is Bed: compare with bed_num.
                            base_num = float(base_val)
                            if top_parsed["bed_num"] < base_num:
                                issues.append({
                                    "table": table_id, "row_index": ridx,
                                    "constraint": constraint,
                                    "base": base_val, "top": top_val,
                                })
                        else:
                            # Both are plain numeric.
                            base = float(base_val)
                            top = float(top_val)
                            if top < base:
                                issues.append({
                                    "table": table_id, "row_index": ridx,
                                    "constraint": constraint,
                                    "base": base, "top": top,
                                })
                    except (TypeError, ValueError):
                        pass
                # Sprint B (REVIEW-2026-09-04): the "has_biozone_or_age"
                # branch was removed — it tested an ``age`` key that species
                # rows never carry (see EXPORT_INVARIANTS note above).
    return len(issues) == 0, issues, warnings


# Each config: id, i18n title key, list of column i18n keys, and a row
# extractor producing cell values in column order.
def _looks_columnar(data: dict[str, Any] | None) -> bool:
    """Heuristic: if data.sections is present and its first item has an 'id'
    field (column-label) rather than a 'name' (measured-section), it's a
    columnar-section result. Mirrors js/table.js.
    """
    if not data:
        return False
    sects = data.get("sections")
    if not isinstance(sects, list):
        return False
    if not sects:
        return False
    first = sects[0]
    # "id" is the definitive columnar marker (it holds the column-label
    # value). Even if "name" is also present (LLM being verbose), the
    # presence of "id" is sufficient to classify as columnar. Range-chart
    # sections use "name" (measured-section name) and never have "id".
    return isinstance(first, dict) and ("id" in first)


def _looks_abundance(data: dict[str, Any] | None) -> bool:
    """Heuristic: an abundance-diagram result carries an ``abundances`` list
    (the per-(taxon, level) rows). Mirrors js/table.js.

    An empty list (`abundances: []`) is NOT enough to call this an
    abundance result — every freshly-extracted object schema includes an
    empty `abundances` placeholder before the model fills it in, so
    accepting empty lists misroutes pure range-chart results to the
    abundance renderer. Require at least one entry plus a non-empty
    `abundances` list to disambiguate.
    """
    if not data:
        return False
    ab = data.get("abundances")
    return isinstance(ab, list) and len(ab) > 0


def _looks_phylogenetic_tree(data: dict[str, Any] | None) -> bool:
    """Heuristic: a phylogenetic-tree result carries a ``nodes`` list
    with parent/id structure and optionally ``root_ids``."""
    if not data:
        return False
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    first = nodes[0]
    return isinstance(first, dict) and "id" in first and "parent" in first


def _looks_zonation_chart(data: dict[str, Any] | None) -> bool:
    """Heuristic: a zonation / correlation chart result carries a
    non-empty ``correlations`` list, or a non-empty ``zones`` list of
    zone-rank rows (``rank`` / ``zonation`` markers) that no other mode
    emits. UI-REVIEW-2026-09-05 (radiolarian biochronology charts)."""
    if not data:
        return False
    corr = data.get("correlations")
    if isinstance(corr, list) and len(corr) > 0:
        return True
    zones = data.get("zones")
    if isinstance(zones, list) and len(zones) > 0:
        first = zones[0]
        if isinstance(first, dict) and ("rank" in first or "zonation" in first):
            return True
    # A payload whose zonation column descriptors are populated is a
    # zonation chart even when every zone row was unreadable.
    zns = data.get("zonations")
    if isinstance(zns, list) and len(zns) > 0:
        return True
    return False


def _range_chart_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Range-chart table configs. Multi-run results gain an agreement column
    on the species table — mirrors js/table.js.
    """
    multi = bool(data) and int(data.get("runs", 1) or 1) > 1
    species_cols = ["col.species", "col.section", "col.rangeBase", "col.rangeTop", "col.biozone"]
    species_data = ["species", "section", "range_base", "range_top", "biozone"]
    species_row_base = lambda r: [
        r.get("species", ""),
        r.get("section", ""),
        r.get("range_base", ""),
        r.get("range_top", ""),
        r.get("biozone", ""),
    ]

    # REVIEW-2026-09-10: the optional per-species columns the browser has
    # always added (js/table.js H3) — emitted only when at least one row
    # populates the field, so a CSV carries `author_year` / `note` /
    # `confidence` when the model emitted them instead of dropping those
    # values on the GUI/server export path. The js/table.js comment claimed
    # this mirrored rca_core/exporter.py; it did not.
    rows_for_pred = data.get("species_ranges") if isinstance(data, dict) else None
    rows_for_pred = rows_for_pred if isinstance(rows_for_pred, list) else []
    # REVIEW-2026-09-20: ``v or ""`` also swallows a legitimate 0 / False
    # (an author_year of "0"? an empty-but-present note?), and the getter
    # doubles as the "does any row populate this column?" predicate — so a
    # falsy value hid the whole column. Test for None instead and stringify
    # everything else.
    _opt_text = lambda v: "" if v is None else str(v)
    species_opt = [
        ("col.authorYear", "author_year", lambda r: _opt_text(r.get("author_year"))),
        ("col.rangeTopBed", "range_top_bed", lambda r: _opt_text(r.get("range_top_bed"))),
        ("col.rangeTopIdx", "range_top_idx", lambda r: _opt_text(r.get("range_top_idx"))),
        ("col.endpointKind", "endpoint_kind",
         lambda r: "" if r.get("endpoint_kind") in (None, "unknown") else str(r.get("endpoint_kind"))),
        ("col.occurrenceMode", "occurrence_mode",
         lambda r: "" if r.get("occurrence_mode") in (None, "unknown") else str(r.get("occurrence_mode"))),
        ("col.colConfidence", "confidence", lambda r: _opt_text(r.get("confidence"))),
        ("col.note", "note", lambda r: _opt_text(r.get("note"))),
    ]
    species_extra_cols: list[str] = []
    species_extra_data: list[str] = []
    species_extra_getters: list = []
    for label, data_key, getter in species_opt:
        if any(getter(r) for r in rows_for_pred if isinstance(r, dict)):
            species_extra_cols.append(label)
            species_extra_data.append(data_key)
            species_extra_getters.append(getter)
    species_cols = species_cols + species_extra_cols
    species_data = species_data + species_extra_data
    if species_extra_getters:
        # Capture the ORIGINAL base row builder before rebinding, or the
        # lambda would call itself (late binding).
        _base = species_row_base
        _getters = list(species_extra_getters)
        species_row_base = lambda r: _base(r) + [g(r) for g in _getters]

    return [
        {
            "id": "sections",
            "title_key": "sec.sections",
            "cols": ["col.name", "col.ageRange", "col.formation", "col.thickness", "col.coordinates"],
            "data_keys": ["name", "age_range", "formations", "formation_thickness_m", "coordinates"],
            # REVIEW-2026-09-20: stable row identity — see ``identity_keys``
            # on apply_table_edits (an edited row only inherits the untouched
            # extra fields of the model row it can be matched to).
            "identity_keys": ["name"],
            "row": lambda s: [
                s.get("name", ""),
                s.get("age_range", ""),
                "; ".join(s.get("formations", []) or []),
                s.get("formation_thickness_m", ""),
                s.get("coordinates", ""),
            ],
        },
        {
            "id": "species_ranges",
            "title_key": "sec.species",
            "cols": species_cols + (["col.agreement"] if multi else []),
            "data_keys": species_data + (["agreement"] if multi else []),
            "identity_keys": ["species", "section"],
            "row": (lambda r: species_row_base(r) + [r.get("agreement", "")]) if multi else species_row_base,
        },
        {
            "id": "biozones",
            "title_key": "sec.biozones",
            "cols": ["col.name", "col.section", "col.age", "col.thickness"],
            "data_keys": ["name", "section", "age", "thickness_m"],
            "identity_keys": ["name", "section"],
            "row": lambda b: [b.get("name", ""), b.get("section", ""), b.get("age", ""), b.get("thickness_m", "")],
        },
        {
            "id": "other_fossils",
            "title_key": "sec.fossils",
            "cols": ["col.fossil"],
            "data_keys": ["fossil"],
            # REVIEW-2026-09-20: plain-string rows — there is no dict to
            # inherit untouched fields from, so this table has no
            # ``identity_keys`` (apply_table_edits writes strings).
            # Defensive: items may be plain strings (from rca_core
            # normalize_result) or dicts (older or third-party producers).
            "row": lambda f: [f.get("fossil", f.get("text", ""))] if isinstance(f, dict) else [f],
        },
    ]


def _columnar_section_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Columnar-section table configs. Multi-run results gain an agreement
    column on the sections table — mirrors js/table.js.
    """
    multi = bool(data) and int(data.get("runs", 1) or 1) > 1
    sec_cols = ["col.sectionId", "col.sectionGroup", "col.thickness", "col.coordinates"]
    sec_data = ["id", "group", "thickness_m", "coordinates_text"]
    cols_final = sec_cols + (["col.agreement"] if multi else [])
    data_final = sec_data + (["agreement"] if multi else [])
    row_base = lambda s: [
        s.get("id", ""),
        s.get("group", ""),
        s.get("thickness_m", ""),
        s.get("coordinates_text", ""),
    ]
    row = (lambda s: row_base(s) + [s.get("agreement", "")]) if multi else row_base

    # REVIEW-2026-09-10: flatten the per-section sub-tables so the exporter
    # can treat them like any top-level table (see the `rows` handling in
    # build_table_export). Same field order as js/table.js
    # rcaColumnarSubTableRows: section_id first, then the row's own fields.
    #
    # REVIEW-2026-09-20: each flattened row also records where it came from
    # (``_src`` = parent section index + index inside that section's sub
    # list) so apply_table_edits can write edits BACK to the nested model
    # row instead of inventing a top-level ``data["lithology_blocks"]`` key
    # that nothing reads. ``cfg["row"]`` selects the exported keys
    # explicitly, so ``_src`` never reaches a CSV / TSV / XLSX cell.
    blocks_rows: list[dict[str, Any]] = []
    units_rows: list[dict[str, Any]] = []
    samples_rows: list[dict[str, Any]] = []
    for si, s in enumerate((data or {}).get("sections") or []):
        if not isinstance(s, dict):
            continue
        sid = s.get("id", "")
        for bi, b in enumerate(s.get("lithology_blocks") or []):
            if isinstance(b, dict):
                blocks_rows.append({
                    "section_id": sid, "pattern": b.get("pattern", ""),
                    "top_idx": b.get("range_top_idx"),
                    "base_idx": b.get("range_base_idx"),
                    "_src": (si, "lithology_blocks", bi),
                })
        for ui, u in enumerate(s.get("age_units") or []):
            if isinstance(u, dict):
                units_rows.append({
                    "section_id": sid, "label": u.get("label", ""),
                    "top_idx": u.get("range_top_idx"),
                    "base_idx": u.get("range_base_idx"),
                    "_src": (si, "age_units", ui),
                })
        for mi, smp in enumerate(s.get("samples") or []):
            if isinstance(smp, dict):
                samples_rows.append({
                    "section_id": sid, "bed_idx": smp.get("bed_idx"),
                    "fossil_marker": smp.get("fossil_marker", ""),
                    "ref": smp.get("ref", ""),
                    "_src": (si, "samples", mi),
                })

    return [
        {
            "id": "sections",
            "title_key": "sec.sections",
            "cols": cols_final,
            "data_keys": data_final,
            "identity_keys": ["id"],
            "row": row,
        },
        {
            "id": "fossil_legend",
            "title_key": "sec.fossils",
            "cols": ["col.fossilMarker", "col.fossilMeaning"],
            "data_keys": ["marker", "meaning"],
            "identity_keys": ["marker"],
            "row": lambda it: [it.get("marker", ""), it.get("meaning", "")],
        },
        {
            "id": "lithology_legend",
            "title_key": "sec.columnarLithology",
            "cols": ["col.lithologyPattern", "col.lithologyMeaning"],
            "data_keys": ["pattern", "meaning"],
            "identity_keys": ["pattern"],
            "row": lambda it: [it.get("pattern", it.get("marker", "")), it.get("meaning", "")],
        },
        {
            "id": "cross_beds",
            "title_key": "sec.crossBeds",
            "cols": ["col.crossFrom", "col.crossFromBed", "col.crossTo", "col.crossToBed"],
            "data_keys": ["from_section", "from_bed_idx", "to_section", "to_bed_idx"],
            # The section pair identifies the row; the bed indices are the
            # values an operator edits, so they stay out of the identity.
            "identity_keys": ["from_section", "to_section"],
            "row": lambda it: [
                it.get("from_section", ""),
                "" if it.get("from_bed_idx") is None else str(it.get("from_bed_idx")),
                it.get("to_section", ""),
                "" if it.get("to_bed_idx") is None else str(it.get("to_bed_idx")),
            ],
        },
        # REVIEW-2026-09-10: the three per-section sub-tables the browser
        # export has always carried (js/table.js rcaColumnarSubTableRows).
        # They live nested inside sections[i], so they are flattened here —
        # section_id first, exactly like the JS rows — otherwise the GUI /
        # server CSV + XLSX lost every lithology block, age unit and sample
        # that the browser kept.
        #
        # REVIEW-2026-09-20: ``nested_in`` tells apply_table_edits that the
        # model has NO top-level key with this id — the real rows live in
        # ``data["sections"][i][sub_key]`` — and ``key_map`` translates the
        # flattened export keys back to the nested model keys.
        {
            "id": "lithology_blocks",
            "title_key": "sec.lithologyBlocks",
            "cols": ["col.secId", "col.pattern", "col.topIdx", "col.baseIdx"],
            "data_keys": ["section_id", "pattern", "top_idx", "base_idx"],
            "identity_keys": ["section_id", "pattern", "top_idx", "base_idx"],
            "rows": blocks_rows,
            "nested_in": {
                "parent": "sections",
                "parent_key": "id",
                "sub_key": "lithology_blocks",
                # ``section_id`` is a DERIVED export column (the parent's own
                # id, prepended so a flattened row stays attributable); it is
                # not a field of the nested model row.
                "derived": ["section_id"],
                "key_map": {"top_idx": "range_top_idx", "base_idx": "range_base_idx"},
            },
            "row": lambda it: [
                it.get("section_id", ""),
                it.get("pattern", ""),
                "" if it.get("top_idx") is None else str(it.get("top_idx")),
                "" if it.get("base_idx") is None else str(it.get("base_idx")),
            ],
        },
        {
            "id": "age_units",
            "title_key": "sec.ageUnits",
            "cols": ["col.secId", "col.label", "col.topIdx", "col.baseIdx"],
            "data_keys": ["section_id", "label", "top_idx", "base_idx"],
            "identity_keys": ["section_id", "label", "top_idx", "base_idx"],
            "rows": units_rows,
            "nested_in": {
                "parent": "sections",
                "parent_key": "id",
                "sub_key": "age_units",
                "derived": ["section_id"],
                "key_map": {"top_idx": "range_top_idx", "base_idx": "range_base_idx"},
            },
            "row": lambda it: [
                it.get("section_id", ""),
                it.get("label", ""),
                "" if it.get("top_idx") is None else str(it.get("top_idx")),
                "" if it.get("base_idx") is None else str(it.get("base_idx")),
            ],
        },
        {
            "id": "samples",
            "title_key": "sec.samples",
            "cols": ["col.secId", "col.bedIdx", "col.fossilMarker", "col.ref"],
            "data_keys": ["section_id", "bed_idx", "fossil_marker", "ref"],
            "identity_keys": ["section_id", "bed_idx", "fossil_marker"],
            "rows": samples_rows,
            "nested_in": {
                "parent": "sections",
                "parent_key": "id",
                "sub_key": "samples",
                "derived": ["section_id"],
                # samples store their fields under the export names already.
                "key_map": {},
            },
            "row": lambda it: [
                it.get("section_id", ""),
                "" if it.get("bed_idx") is None else str(it.get("bed_idx")),
                it.get("fossil_marker", ""),
                it.get("ref", ""),
            ],
        },
    ]


# REVIEW-2026-09-20: the three ASSISTANT modes (UI-REVIEW checklist) have no
# table schema at all — no ``TABLE_CONFIGS`` branch, nothing in js/table.js,
# no editable grid. Their root keys mirror ``rca_core/extractor.py``'s
# ``_KNOWN_CHEMICAL_STRAT_ROOT_KEYS`` / ``_KNOWN_PALEOMAP_ROOT_KEYS`` /
# ``_KNOWN_SCATTER_PLOT_ROOT_KEYS`` (kept as literals here for the same reason
# js/json-utils.js duplicates them: importing extractor from exporter would
# create an import cycle). Kept in sync by ``tests/test_exporter_modes.py``.
_TABLELESS_MODES: dict[str, tuple[str, ...]] = {
    "chemical_stratigraphy": ("data_points", "events", "intervals"),
    "paleomap": (
        "continents", "oceans_seas", "tectonic_features",
        "biogeographic_realms", "fossil_sites", "paleolatitude_indicators",
    ),
    "scatter_plot": ("groups", "points", "outliers", "statistics"),
}
# Root keys that belong to a tableless mode ONLY — a result carrying one of
# these is that mode even if a generic key (``metadata`` / ``confidence``)
# overlaps with another shape.
_MODE_EXCLUSIVE_KEYS = {
    "chemical_stratigraphy": ("data_points",),
    "paleomap": (
        "continents", "oceans_seas", "tectonic_features",
        "biogeographic_realms", "paleolatitude_indicators",
    ),
    "scatter_plot": ("outliers", "statistics"),
}


def detect_tableless_mode(data: dict[str, Any] | None) -> str | None:
    """Return the mode id when ``data`` is an assistant result with NO tables.

    ``get_configs_for_result`` used to fall through to
    ``_range_chart_tables(data)`` for these results, so a scatter plot /
    geochemical curve / palaeomap "exported" as a workbook of four EMPTY
    range-chart sheets — the user got a file that looked like a successful
    export of data they never had. The GUI now hides the table grid for these
    modes; the JSON export is the real path.
    """
    if not isinstance(data, dict) or not data:
        return None
    # A result that DOES carry tables is never tableless, whatever else it
    # holds (a merged or hand-edited payload can contain stray keys).
    for table_key in ("species_ranges", "sections", "abundances", "nodes",
                      "zonations", "zones", "correlations", "biozones"):
        v = data.get(table_key)
        if isinstance(v, list) and v:
            return None
    for mode, exclusive in _MODE_EXCLUSIVE_KEYS.items():
        if any(_nonempty(data.get(k)) for k in exclusive):
            return mode
    # A fully-unreadable assistant result still identifies itself through the
    # mode's own (present-but-empty) root keys, when ALL of them are there.
    for mode, roots in _TABLELESS_MODES.items():
        if all(k in data for k in roots):
            return mode
    return None


def _nonempty(v: Any) -> bool:
    return bool(v) if isinstance(v, (list, dict, str)) else v is not None


def get_configs_for_result(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the table config list appropriate for ``data``.

    Empty list = "this result has no tabular representation" (the assistant
    modes; see ``detect_tableless_mode``). Callers must handle it: the GUI
    hides the table tab, ``to_xlsx`` raises a clear ValueError instead of
    writing a 4-sheet empty workbook.
    """
    if detect_tableless_mode(data):
        return []
    if _looks_zonation_chart(data):
        return _zonation_chart_tables(data)
    if _looks_abundance(data):
        return _abundance_diagram_tables(data)
    # I7 fix: also detect columnar shape even when sections is empty
    # (VLM failed to extract — schema shape still tells us the mode).
    if _looks_columnar(data):
        return _columnar_section_tables(data)
    # I7 fix: also detect columnar shape when sections is empty but the data
    # came from a columnar extraction (identified by the presence of columnar-only
    # fields like fossil_legend / lithology_legend / cross_beds).
    sects = data.get("sections") if data else None
    if isinstance(sects, list):
        # Columnar data may have sections=[] when the VLM failed to parse any columns.
        # Detect by the presence of any columnar-named key.
        if any(k in data for k in ("fossil_legend", "lithology_legend", "cross_beds")):
            return _columnar_section_tables(data)
    # Phylogenetic-tree detection (nodes with parent/id structure).
    if _looks_phylogenetic_tree(data):
        return _phylogenetic_tree_tables(data)
    return _range_chart_tables(data)


def _abundance_diagram_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Abundance-diagram (pollen / percentage-diagram) table configs.

    Multi-run results gain an agreement column on the abundances table —
    mirrors js/table.js.
    """
    multi = bool(data) and int(data.get("runs", 1) or 1) > 1
    ab_cols = ["col.taxon", "col.site", "col.level", "col.depth", "col.abundance", "col.abundanceUnit"]
    ab_data = ["taxon", "site", "level", "depth", "abundance", "abundance_unit"]
    ab_row_base = lambda r: [
        r.get("taxon", ""),
        r.get("site", ""),
        r.get("level", ""),
        r.get("depth", ""),
        r.get("abundance", ""),
        r.get("abundance_unit", ""),
    ]

    return [
        {
            "id": "sites",
            "title_key": "sec.sites",
            "cols": ["col.name", "col.location", "col.ageRange", "col.depthUnit"],
            "data_keys": ["name", "location", "age_range", "depth_unit"],
            "identity_keys": ["name"],
            "row": lambda s: [
                s.get("name", ""),
                s.get("location", ""),
                s.get("age_range", ""),
                s.get("depth_unit", ""),
            ],
        },
        {
            "id": "abundances",
            "title_key": "sec.abundances",
            "cols": ab_cols + (["col.agreement"] if multi else []),
            "data_keys": ab_data + (["agreement"] if multi else []),
            "identity_keys": ["taxon", "site", "level"],
            "row": (lambda r: ab_row_base(r) + [r.get("agreement", "")]) if multi else ab_row_base,
        },
        {
            "id": "zones",
            "title_key": "sec.zones",
            "cols": ["col.name", "col.age", "col.levelRange"],
            "data_keys": ["name", "age", "level_range"],
            "identity_keys": ["name"],
            "row": lambda z: [z.get("name", ""), z.get("age", ""), z.get("level_range", "")],
        },
    ]


def _phylogenetic_tree_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Phylogenetic-tree table configs. Nodes are exported as a flat table."""
    return [
        {
            "id": "nodes",
            "title_key": "sec.nodes",
            "cols": ["col.nodeId", "col.parent", "col.name", "col.isLeaf",
                     "col.branchLength", "col.nodeAgeMa", "col.support"],
            "data_keys": ["id", "parent", "name", "is_leaf",
                          "branch_length", "node_age_ma", "support"],
            "identity_keys": ["id"],
            "row": lambda n: [
                n.get("id", ""),
                n.get("parent", ""),
                n.get("name", ""),
                "Y" if n.get("is_leaf") else "N",
                "" if n.get("branch_length") is None else str(n.get("branch_length")),
                "" if n.get("node_age_ma") is None else str(n.get("node_age_ma")),
                "" if n.get("support") is None else str(n.get("support")),
            ],
        },
    ]


def _zonation_chart_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Zonation / correlation chart table configs
    (UI-REVIEW-2026-09-05, radiolarian biochronology). Three tables:
    zonation columns, zone rows (primary), correlation edges. Multi-run
    results gain an agreement column on the zones table — mirrors
    js/table.js."""
    multi = bool(data) and int(data.get("runs", 1) or 1) > 1
    zone_cols = ["col.name", "col.zonation", "col.rank", "col.ageSpan",
                 "col.baseAge", "col.topAge", "col.stage", "col.definedBy", "col.note"]
    zone_data = ["name", "zonation", "rank", "age_span", "base_age", "top_age",
                 "stage", "defined_by", "note"]
    zone_row_base = lambda r: [
        r.get("name", ""),
        r.get("zonation", ""),
        r.get("rank", ""),
        r.get("age_span", ""),
        r.get("base_age", ""),
        r.get("top_age", ""),
        r.get("stage", ""),
        r.get("defined_by", ""),
        r.get("note", ""),
    ]

    return [
        {
            "id": "zonations",
            "title_key": "sec.zonations",
            "cols": ["col.name", "col.region", "col.framework", "col.reference"],
            "data_keys": ["name", "region", "framework", "reference"],
            "identity_keys": ["name"],
            "row": lambda z: [
                z.get("name", ""),
                z.get("region", ""),
                z.get("framework", ""),
                z.get("reference", ""),
            ],
        },
        {
            "id": "zones",
            "title_key": "sec.zonesTable",
            "cols": zone_cols + (["col.agreement"] if multi else []),
            "data_keys": zone_data + (["agreement"] if multi else []),
            # A zone name is only unique inside its own zonation scheme.
            "identity_keys": ["name", "zonation"],
            "row": (lambda r: zone_row_base(r) + [r.get("agreement", "")]) if multi else zone_row_base,
        },
        {
            "id": "correlations",
            "title_key": "sec.correlations",
            "cols": ["col.fromZone", "col.fromZonation", "col.toZone",
                     "col.toZonation", "col.basis", "col.note"],
            "data_keys": ["from_zone", "from_zonation", "to_zone",
                          "to_zonation", "basis", "note"],
            # REVIEW-2026-09-20: an edge is identified by its two endpoints;
            # basis/note are the editable payloads (see ``identity_keys``).
            "identity_keys": ["from_zone", "to_zone"],
            "row": lambda c: [
                c.get("from_zone", ""),
                c.get("from_zonation", ""),
                c.get("to_zone", ""),
                c.get("to_zonation", ""),
                c.get("basis", ""),
                c.get("note", ""),
            ],
        },
    ]


def get_config(table_id: str) -> dict[str, Any] | None:
    # Search through all presets — used by the fallback path in
    # build_table_export / apply_table_edits when the table isn't in the
    # data-shape-matched configs (e.g. editing a cross_beds row while the
    # result has no sections to detect columnar shape).
    for fn in (_range_chart_tables, _columnar_section_tables, _abundance_diagram_tables,
               _phylogenetic_tree_tables, _zonation_chart_tables):
        for c in fn(None):
            if c["id"] == table_id:
                return c
    return None


def _find_cfg(table_id: str, data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resolve a table config: first the shape-matched configs (so the
    multi-run agreement column and columnar/range/abundance routing apply),
    then a cross-shape fallback so tables whose shape can't be inferred
    from the data (e.g. editing cross_beds with an empty sections list) are
    still found.
    """
    for c in get_configs_for_result(data):
        if c["id"] == table_id:
            return c
    return get_config(table_id)


# Legacy alias kept for callers that imported TABLE_CONFIGS directly.
TABLE_CONFIGS = _range_chart_tables(None)


_NONFINITE_TEXT = {"nan", "inf", "-inf", "+inf", "-nan", "infinity", "-infinity"}


def _export_cell_text(value: Any) -> str:
    """Cell text for an export row, with non-finite numbers blanked.

    REVIEW-2026-09-10: _sanitize_number_cell only sees float/Decimal
    instances, but by the time a value reaches this table builder it has
    usually already been str()-ed by a normalizer or a row extractor — so the
    literal text "nan" / "inf" reached the CSV and the XLSX as a value that
    looks like data. Blank those spellings here as defence in depth
    (mirrors the M5 intent and json_utils._strip_nonfinite).
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
    text = str(value)
    if text.strip().lower() in _NONFINITE_TEXT:
        return ""
    return text


def _table_items(data: dict[str, Any] | None, cfg: dict[str, Any]) -> list[Any]:
    """The model rows a config exports — the ONE source of truth.

    REVIEW-2026-09-20: a config may carry pre-flattened ``rows`` (the columnar
    sub-tables live nested inside ``sections[i]`` and are flattened when the
    config is built); otherwise the top-level ``data[table_id]`` list is used.
    ``build_table_export`` already preferred ``cfg["rows"]``, but ``to_xlsx``
    read only ``data[cfg["id"]]`` — so those sheets came out with a header row
    and no data at all (every lithology block, age unit and sample the browser
    kept vanished from the workbook). Both paths call this helper now.
    """
    items = cfg.get("rows")
    if items is None:
        items = (data or {}).get(cfg.get("id")) or []
    return items if isinstance(items, list) else []


def _row_values(cfg: dict[str, Any], item: Any) -> list[Any]:
    """``cfg["row"](item)``, padded / truncated to ``len(cfg["cols"])``.

    M11: pad / truncate to ``n_cols`` so a future contributor who adds a
    custom row extractor can't silently misalign columns between headers and
    rows on a CSV / Excel paste.

    P0-6: a non-dict item (``other_fossils`` rows are plain strings after
    ``normalize_result``) renders as a single cell instead of raising
    ``AttributeError`` inside a dict-expecting extractor.
    """
    if isinstance(item, dict) or cfg.get("id") == "other_fossils":
        vals = list(cfg["row"](item))
    else:
        vals = [item]
    n_cols = len(cfg.get("cols") or [])
    return (vals + [""] * n_cols)[:n_cols]


def _export_grid(
    data: dict[str, Any],
    cfg: dict[str, Any],
    translate: Callable[[str], str],
    *,
    include_index: bool = True,
    index_label: str | None = None,
):
    """``(headers, text_rows, raw_rows)`` for one table config.

    REVIEW-2026-09-20: this is the single row builder behind BOTH the
    CSV/TSV/GUI path (``build_table_export`` → ``text_rows``) and the XLSX
    path (``to_xlsx`` → ``raw_rows``). Keeping two implementations in sync
    by hand was the root cause of the headers-only columnar sheets and of
    the CSV/Excel disagreement on which columns exist. ``raw_rows`` carries
    the cells BEFORE stringification so the workbook can still write real
    ints/floats in numeric columns; ``text_rows`` is what a CSV needs.
    """
    label = "#" if index_label is None else index_label
    headers = ([label] if include_index else []) + [translate(c) for c in cfg["cols"]]
    text_rows: list[list[str]] = []
    raw_rows: list[list[Any]] = []
    for idx, item in enumerate(_table_items(data, cfg)):
        cells = _row_values(cfg, item)
        # REVIEW-2026-09-10: this is where values become strings, so it is the
        # LAST point at which a non-finite float can be caught — the
        # _sanitize_number_cell guard runs on float INSTANCES only, and by the
        # time a producer has already str()-ed its value the literal text
        # "nan"/"inf" sailed past it into the CSV / workbook. Normalise the
        # spellings here as defence in depth.
        text_rows.append(
            ([str(idx + 1)] if include_index else []) + [_export_cell_text(v) for v in cells]
        )
        raw_rows.append(([idx + 1] if include_index else []) + cells)
    return headers, text_rows, raw_rows


def build_table_export(data: dict[str, Any], table_id: str, translate: Callable[[str], str]):
    """Return (headers, rows) for a table, using translated column labels."""
    cfg = _find_cfg(table_id, data)
    if not cfg:
        return [], []
    headers, text_rows, _raw = _export_grid(
        data, cfg, translate, include_index=True,
        # The GUI table header shows the translated "#" label; to_xlsx keeps
        # its historical literal "#" (an untranslated i18n key would be a
        # worse sheet header than the symbol).
        index_label=translate("col.index"),
    )
    return headers, text_rows



# ---------------------------------------------------------------------------
# Column type metadata
# ---------------------------------------------------------------------------
# Each table id maps to a per-column type tag so the GUI's "Apply Edits"
# path can parse the strings the user types back into the right Python
# type ("formations" → list[str], "from_bed_idx" → int, ...).
#
# Keys here are the same as ``data_keys`` on the table cfg (i.e. the
# actual dict key inside the result, not the i18n label).
#
# Types:
#   "str"     — leave as string (default; omit from the map)
#   "list"    — split by ";" and strip
#   "int"     — coerce to int, empty → None
#   "float"   — coerce to float, empty → None
#   "number"  — int when the cell is whole, float otherwise ("3" → 3,
#               "3.5" → 3.5), empty → None
#   "bool_yn" — the nodes table's "Y"/"N" display -> bool
#   "nullable_str" — "" -> None (keeps the phylo root marker)
#
# REVIEW-2026-09-20: an unparseable "int"/"float"/"number" cell keeps the
# operator's TEXT instead of becoming None — see ``_coerce_cell``.
COL_TYPES: dict[str, dict[str, str]] = {
    "sections": {
        "formations": "list",
        # REVIEW-2026-09-20: the columnar `sections` table renders
        # ``thickness_m`` via ``str()`` and the GUI hands the cell back as a
        # string, so an untouched Apply-edits pass turned the model's
        # ``12.5`` into ``"12.5"`` — which then failed the numeric range /
        # quality checks and re-exported differently from the browser.
        # ``number`` (not ``float``) keeps a whole thickness as an int and
        # blanks — never zeros — a cell the operator cleared.
        "thickness_m": "number",
        # Range-chart sections carry no thickness key at all, so the tag
        # above simply never fires for that shape.
    },
    "species_ranges": {
        # REVIEW-2026-09-20: the optional per-species columns the exporter
        # added in REVIEW-2026-09-10 had no type tag, so an Apply-edits pass
        # wrote the DISPLAY strings back into the model: bed indices became
        # ``"3"`` instead of ``3`` and ``confidence`` became ``"0.9"``.
        # Downstream (aggregate row voting, quality scoring, js/table.js
        # sorting) compares those fields numerically.
        "range_top_idx": "number",
        "range_base_idx": "number",
        "confidence": "float",
    },
    "biozones": {
        "thickness_m": "number",
    },
    "fossil_legend": {},
    "lithology_legend": {},
    "cross_beds": {
        "from_bed_idx": "int",
        "to_bed_idx": "int",
    },
    "other_fossils": {},
    # Columnar sub-tables flattened out of ``sections[i]`` — the bed indices
    # are ints in the model (see nested key_map range_top_idx/range_base_idx).
    "lithology_blocks": {
        "top_idx": "int",
        "base_idx": "int",
    },
    "age_units": {
        "top_idx": "int",
        "base_idx": "int",
    },
    "samples": {
        "bed_idx": "int",
    },
    # Abundance-diagram tables: all free-text (depth/abundance kept as
    # strings so units like "35%" or categorical "common" survive edits).
    "sites": {},
    "abundances": {},
    "zones": {},
    # Phylogenetic-tree tables: branch_length / node_age_ma / support are floats.
    "nodes": {
        "branch_length": "float",
        "node_age_ma": "float",
        "support": "float",
        # REVIEW-2026-09-10: the nodes table renders is_leaf as the display
        # strings "Y"/"N" and parent None as "", but without a COL_TYPES entry
        # _coerce_cell's default ("str") wrote those DISPLAY strings back into
        # the model. One no-op Apply-edits pass therefore turned
        # is_leaf into the string "N" for every node — which is TRUTHY, so the
        # re-export reported "Y" (leaf) for all of them and eval_metrics'
        # Random-Forest metric changed on an unchanged tree — and it blanked
        # the root marker `parent: None` that the "root must have parent None"
        # invariant relies on.
        "is_leaf": "bool_yn",
        "parent": "nullable_str",
    },
}


def _coerce_cell(value: Any, data_key: str, table_id: str) -> Any:
    """Convert a string from a table cell into the right Python type.

    The default ("str") leaves the value unchanged. ``list`` splits on
    ``;`` and trims; ``int``/``float`` parse and return None on empty input;
    ``number`` returns an int for "3", a float for "3.5".

    REVIEW-2026-09-20 (two bugs on the numeric branches):

    * An UNPARSEABLE numeric cell used to return ``None``, so a value the
      operator typed on purpose — ``"5m"``, ``"Bed 3"``, ``"≈ 12"`` — was
      silently DELETED from the model on Apply-edits. The text is kept
      instead: a wrong-but-visible string beats a lost field, and the
      quality report flags it.
    * ``int("3.0")`` raises, so a cell the spreadsheet reformatted to
      ``"3.0"`` (or ``"3e2"``) read back as None/"" even though it is a
      perfectly good number. Fall through to ``int(float(s))``.
    * ``str(value)`` is applied here rather than by the caller, so a caller
      that still holds model-native values (int/float/None) doesn't corrupt
      the cell.
    """
    type_map = COL_TYPES.get(table_id, {})
    t = type_map.get(data_key, "str")
    if value is None:
        value = ""
    s = value if isinstance(value, str) else str(value)
    if t == "list":
        return [x.strip() for x in (s or "").split(";") if x.strip()]
    if t in ("int", "float", "number"):
        txt = (s or "").strip()
        if not txt:
            return None
        try:
            if t == "int":
                return int(txt)
            if t == "float":
                return float(txt)
            num = float(txt)
            return int(num) if num.is_integer() else num
        except (TypeError, ValueError, OverflowError):
            pass
        if t == "int":
            # "3.0" / "3e2" written by a spreadsheet are still integers.
            try:
                f = float(txt)
                if f == f and f not in (float("inf"), float("-inf")):
                    return int(f)
            except (TypeError, ValueError, OverflowError):
                pass
        return txt  # keep what the operator typed rather than dropping it
    if t == "bool_yn":
        # Inverse of the renderer's "Y" / "N". Accept the obvious human
        # spellings; an empty cell reads as False (an internal node).
        v = (s or "").strip().lower()
        return v in ("y", "yes", "true", "1", "t")
    if t == "nullable_str":
        # Inverse of the renderer's None -> "" mapping, so the root marker
        # `parent: None` survives an edit round-trip.
        v = (s or "").strip()
        return v or None
    return s or ""



def _identity_text(value: Any) -> str:
    """Normalised cell text used as one component of a row's stable identity.

    REVIEW-2026-09-20: the model side holds native values (``3``, ``3.0``,
    ``None``, ``["a", "b"]``) while the table side holds strings ("3", "3.0",
    "", "a; b"), and both must hash to the SAME identity or matching fails.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ";".join(_identity_text(v) for v in value)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip()


def _row_identity(item: Any, keys: list[str]) -> tuple[str, ...]:
    """The identity tuple of a model row or an edited cell row."""
    if not isinstance(item, dict):
        return (_identity_text(item),)
    return tuple(_identity_text(item.get(k)) for k in keys)


def _identity_pool(items: list[Any], keys: list[str]) -> dict[tuple, list[int]]:
    """identity -> FIFO of matching indexes.

    Duplicate identities (the model can legitimately emit two "Genus A sp."
    rows in one section) inherit in their original order rather than both
    latching onto the first one.
    """
    pool: dict[tuple, list[int]] = {}
    for i, it in enumerate(items):
        pool.setdefault(_row_identity(it, keys), []).append(i)
    return pool


def _pop_match(pool: dict[tuple, list[int]], identity: tuple) -> int | None:
    slots = pool.get(identity)
    if not slots:
        return None
    idx = slots.pop(0)
    if not slots:
        pool.pop(identity, None)
    return idx


def apply_table_edits(
    data: dict[str, Any],
    table_id: str,
    rows: list[list[str]],
) -> dict[str, Any]:
    """Mutate ``data[table_id]`` from a list of cell rows.

    ``rows`` is the table widget's current state, one entry per row
    (the first cell is the auto-index column and is ignored). The shape
    of the produced items is inferred from the cfg: dict for everything
    except ``other_fossils`` (plain strings).

    Non-editable per-row fields (``_extras``, per-row ``confidence``,
    plate references, …) are preserved: the rebuilt dict starts from a copy
    of the model row the edited row was MATCHED TO, so unknown keys survive
    the edit. Without this, every Apply-edits permanently deletes
    model-emitted auxiliary fields.

    REVIEW-2026-09-20 (P0-7 follow-up — the inheritance itself was wrong):
    matching used to be POSITIONAL (``existing_items[data_row_idx]``). Any
    GUI edit that changes the row COUNT — deleting a row, inserting one at
    the top, re-sorting the table, or simply the extra phantom row a Qt
    widget reports — shifts every later row onto its NEIGHBOUR's model row,
    so a species silently inherits the previous row's ``_extras`` (plate
    figure, page, per-row confidence): scientifically wrong data that no
    later pass repairs. Rows are now matched by ``cfg["identity_keys"]`` —
    the fields that identify a row scientifically (species + section, zone
    name, node id, …) — and a row that matches NOTHING is treated as new
    (``d = {}``, no inheritance at all). Position can no longer move data
    between rows.

    REVIEW-2026-09-20 (columnar sub-tables): ``lithology_blocks`` /
    ``age_units`` / ``samples`` have no top-level model key — their rows are
    flattened out of ``sections[i]`` at config-build time (``nested_in``).
    Writing ``data["lithology_blocks"]`` therefore created a key nothing
    reads, so those three tables were effectively read-only AND left a bogus
    key behind that quality/report could misinterpret. Edits are now written
    back to the nested rows via the per-row ``(section_index, sub_key,
    row_index)`` provenance recorded in ``cfg["rows"]`` (``_src``).
    """
    if not isinstance(data, dict):
        return data
    cfg = _find_cfg(table_id, data)
    if not cfg:
        return data
    data_keys = cfg.get("data_keys") or cfg["cols"]
    identity_keys = list(cfg.get("identity_keys") or [])
    if cfg.get("nested_in"):
        return _apply_nested_table_edits(data, table_id, cfg, rows, data_keys, identity_keys)
    out: list[Any] = []
    existing_items = data.get(table_id)
    existing_items = existing_items if isinstance(existing_items, list) else []
    # REVIEW-2026-09-20: identity index instead of the old positional
    # ``data_row_idx`` cursor.
    pool = _identity_pool(existing_items, identity_keys) if identity_keys else {}
    for row in rows:
        # Skip empty placeholder rows (a Qt quirk: rowCount is 1 even
        # when the model is empty, so the last row is a phantom). A skipped
        # row consumes nothing: with identity matching there is no cursor to
        # keep in step (that coupling WAS the P0-7 bug).
        if not row or all((c is None or str(c).strip() == "") for c in row[1:]):
            continue
        if table_id == "other_fossils":
            # Plain string list.
            txt = (row[1] if len(row) > 1 else "") or ""
            txt = str(txt).strip()
            if txt:
                out.append(txt)
            continue
        # Coerce the cells first: the identity is computed from the EDITED
        # row (the operator may have corrected the name itself).
        edited: dict[str, Any] = {}
        for ci, dk in enumerate(data_keys, start=1):
            if dk == "agreement":
                # agreement is computed at merge time — ignore on edit.
                continue
            v = row[ci] if ci < len(row) else ""
            edited[dk] = _coerce_cell(v, dk, table_id)
        src_idx: int | None = None
        if identity_keys:
            # Matched by the SCIENTIFIC identity of the row, never by its
            # position: a row the operator renamed / retyped legitimately
            # loses its _extras (a new row), which is the safe direction —
            # the old positional cursor silently attached the NEIGHBOUR's
            # plate references instead, i.e. fabricated provenance.
            src_idx = _pop_match(pool, _row_identity(edited, identity_keys))
        if src_idx is not None and isinstance(existing_items[src_idx], dict):
            d: dict[str, Any] = dict(existing_items[src_idx])
        else:
            # Genuinely new (or unmatchable) row: no inheritance. The old code
            # reused whatever happened to sit at this index — a deleted row's
            # _extras landed on its successor.
            d = {}
        d.update(edited)
        out.append(d)
    data[table_id] = out
    return data


def _apply_nested_table_edits(
    data: dict[str, Any],
    table_id: str,
    cfg: dict[str, Any],
    rows: list[list[str]],
    data_keys: list[str],
    identity_keys: list[str],
) -> dict[str, Any]:
    """Write a flattened sub-table's edits back into ``sections[i][sub_key]``.

    The exported table is the UNION of every parent's sub-rows, so the edited
    table is authoritative for all of them: a parent the operator emptied
    loses its rows. Rows whose parent cannot be resolved are skipped with a
    warning — and if NOTHING resolves the write is aborted entirely, because
    silently writing an empty list for every section would delete the model's
    lithology / age / sample data.
    """
    nested = cfg["nested_in"] or {}
    sub_key = nested.get("sub_key") or table_id
    parent_key = nested.get("parent_key") or "id"
    key_map: dict[str, str] = dict(nested.get("key_map") or {})
    derived = set(nested.get("derived") or ())
    parents = data.get(nested.get("parent") or "sections")
    parents = parents if isinstance(parents, list) else []
    flat_rows = [r for r in (cfg.get("rows") or []) if isinstance(r, dict)]
    pool = _identity_pool(flat_rows, identity_keys) if identity_keys else {}

    # Parent id -> index, so a row whose section_id the operator retyped (or
    # a brand-new row) still lands in the right section.
    by_id: dict[str, int] = {}
    for pi, p in enumerate(parents):
        if isinstance(p, dict):
            pid = _identity_text(p.get(parent_key))
            if pid and pid not in by_id:
                by_id[pid] = pi

    # Every parent starts empty (delete semantics) and is refilled below.
    rebuilt: dict[int, list[dict[str, Any]]] = {pi: [] for pi in range(len(parents))}
    resolved = 0
    unresolved = 0
    for row in rows:
        if not row or all((c is None or str(c).strip() == "") for c in row[1:]):
            continue
        edited: dict[str, Any] = {}
        for ci, dk in enumerate(data_keys, start=1):
            if dk == "agreement":
                continue
            v = row[ci] if ci < len(row) else ""
            edited[dk] = _coerce_cell(v, dk, table_id)
        pi: int | None = None
        base: dict[str, Any] | None = None
        if identity_keys:
            fi = _pop_match(pool, _row_identity(edited, identity_keys))
            if fi is not None:
                src = flat_rows[fi].get("_src")
                if isinstance(src, (list, tuple)) and len(src) == 3:
                    pi = src[0] if 0 <= int(src[0]) < len(parents) else None
                # Inherit the UNTOUCHED nested model row (not the flattened
                # export row, which only carries the exported columns).
                if pi is not None:
                    sibling = (parents[pi] or {}).get(sub_key) if isinstance(parents[pi], dict) else None
                    if isinstance(sibling, list) and 0 <= int(src[2]) < len(sibling):
                        cand = sibling[int(src[2])]
                        if isinstance(cand, dict):
                            base = cand
        if pi is None:
            pi = by_id.get(_identity_text(edited.get("section_id")))
        if pi is None:
            unresolved += 1
            continue
        resolved += 1
        d: dict[str, Any] = dict(base) if base is not None else {}
        for k, v in edited.items():
            if k in derived:
                continue
            d[key_map.get(k, k)] = v
        rebuilt[pi].append(d)

    if unresolved and not resolved:
        # Nothing matched — e.g. the result was extracted with a different
        # section id scheme. Aborting is the only safe option.
        print(
            f"warning: apply_table_edits({table_id!r}): {unresolved} row(s) "
            "could not be attributed to a section; edits NOT written back "
            "(the flattened sub-table is read from sections[*]"
            f".{sub_key}).",
            file=sys.stderr,
        )
        return data
    if unresolved:
        print(
            f"warning: apply_table_edits({table_id!r}): {unresolved} row(s) "
            f"had no resolvable section and were dropped; check the "
            f"section id column against sections[].{parent_key}.",
            file=sys.stderr,
        )
    for pi, new_rows in rebuilt.items():
        p = parents[pi]
        if isinstance(p, dict):
            p[sub_key] = new_rows
    return data



def _sanitize_formula_cell(v: Any) -> Any:
    """Mitigate CSV/TSV formula injection (OWASP): prefix a cell whose first
    character is a formula trigger (= + - @) or a tab/CR/LF with a single
    quote so Excel/LibreOffice treats it as text, not an executable formula.
    An LLM-extracted (or attacker-crafted) value like =CMD(...) or
    =HYPERLINK(...) would otherwise execute on open.
    """
    if v is None:
        return v
    s = str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r", "\n"):
        return "'" + s
    return v


def _sanitize_number_cell(v: Any) -> Any:
    """Replace NaN / +/-Infinity floats with empty string so they don't
    flow into exports as the literal text ``nan`` / ``inf`` (which is
    invalid JSON on re-serialization and breaks Excel number coercion).

    Finite floats pass through unchanged. Strings / ints / None are
    returned as-is — formula-injection mitigation is handled separately
    by ``_sanitize_formula_cell``.

    M5 fix (REVIEW-2026-11-07): ``decimal.Decimal`` is NOT a subclass of
    ``float``, so ``Decimal('NaN')`` / ``Decimal('Infinity')`` used to
    bypass the check and reach XLSX as invalid numbers. Check it too.
    """
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return ""
    if isinstance(v, _Decimal):
        if v.is_nan() or v.is_infinite():
            return ""
    return v


def _cell_to_export(v: Any) -> Any:
    """Pipeline: NaN/Inf → empty; then formula-injection prefix."""
    return _sanitize_formula_cell(_sanitize_number_cell(v))


def to_csv(headers: list[str], rows: list[list[str]]) -> str:
    """CSV text with a UTF-8 BOM so Excel reads CJK/Cyrillic correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([_cell_to_export(h) for h in headers])
    writer.writerows([[_cell_to_export(c) for c in row] for row in rows])
    return "﻿" + buf.getvalue()


def to_tsv(headers: list[str], rows: list[list[str]]) -> str:
    """TSV text; tabs/newlines inside cells collapsed to spaces.

    TSV is pasted into Excel just like CSV, so the OWASP formula-injection
    mitigation applies here too — the clipboard path was in fact the MORE
    exposed one, because Excel parses a pasted ``=...`` cell as a formula.
    """
    def clean(v: Any) -> str:
        # Pipeline: NaN/Inf → empty, formula-injection prefix.
        s = "" if v is None else str(_cell_to_export(v))
        # Collapse whitespace so a cell cannot smuggle a tab/newline into the
        # row (that shifts every following column) — and so the guard below
        # sees the text the importer actually parses.
        s = " ".join(s.split())
        # REVIEW-2026-09-20 (formula injection re-opened by the collapse):
        # the apostrophe is a CSV/Excel-text artifact, so TSV used to strip it
        # back off — but it stripped it *after* the collapse without re-running
        # the guard, and worse, ``_sanitize_formula_cell`` had already missed
        # values whose trigger character was hidden by whitespace:
        #     "   =HYPERLINK(..)"  -> no prefix (first char is a space)
        #     " ".join(split())     -> "=HYPERLINK(..)"   ← live formula in the
        # TSV / clipboard. Sanitising the FINAL string closes both holes; the
        # apostrophe now stays for a genuine trigger (it is inert text, and a
        # stray one is far cheaper than an executed command).
        if s[:1] == "'" and s[1:2] in ("=", "+", "-", "@"):
            s = s[1:]
        return str(_sanitize_formula_cell(s))

    lines = ["\t".join(clean(h) for h in headers)]
    for row in rows:
        lines.append("\t".join(clean(c) for c in row))
    return "\n".join(lines)


def _strip_nonfinite(obj: Any) -> Any:
    """M5 fix (REVIEW-2026-11-07): recursively replace non-finite floats
    (NaN / +/-Inf — invalid in strict JSON) with ``None`` so that
    ``json.dumps(..., allow_nan=False)`` cannot raise when the LLM emits
    them (e.g. a ``confidence`` of ``Infinity``). Only the JSON shapes the
    result carries (dict / list / float) are walked; everything else —
    including Decimal, which json.dumps already rejects loudly — passes
    through untouched.
    """
    if isinstance(obj, dict):
        return {k: _strip_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_nonfinite(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def result_to_json(data: dict[str, Any], source_file: str | None, timestamp: str | None) -> str:
    payload = {
        "extracted_at": timestamp,
        "source_file": source_file,
        "result": data,
    }
    import json

    # M5 fix (REVIEW-2026-11-07): allow_nan=False used to raise ValueError
    # on NaN/Inf floats anywhere in the result; sanitize first so the
    # export degrades to null instead of crashing.
    return json.dumps(_strip_nonfinite(payload), ensure_ascii=False, indent=2, allow_nan=False)


# ---------------------------------------------------------------------------
# XLSX export
# ---------------------------------------------------------------------------

def _xlsx_sheet_name(title: str) -> str:
    """OpenPyXL sheet titles are limited to 31 characters (Unicode code points)
    and must not contain any of ``\\ / * ? [ ] :``. Invalid characters are
    replaced with underscores.

    The limit is per code point, not per UTF-8 byte. CJK characters (each
    1 code point, 3 bytes in UTF-8) are safe — Python 3 str indexing is by
    Unicode code point, so ``s[:31]`` never splits inside a character.
    We still validate by encoding to UTF-8 to confirm the result is <= 31 cp.
    """
    cleaned = "".join("_" if c in "\\/*?[]:" else c for c in (title or ""))
    cleaned = cleaned or "Sheet"
    # Python 3 str slicing is by Unicode code point, not byte. Enforce the
    # 31-code-point OpenPyXL limit safely for all scripts.
    if len(cleaned) > 31:
        cleaned = cleaned[:31]
    # Guard: if we somehow exceeded 31 code points after re-encoding
    # (should never happen with valid Unicode), truncate by code point count.
    if len(cleaned.encode("utf-8")) > 93:  # 31 * 3 bytes max per CJK
        chars = []
        byte_count = 0
        for c in cleaned:
            char_bytes = len(c.encode("utf-8"))
            if byte_count + char_bytes > 93:
                break
            chars.append(c)
            byte_count += char_bytes
        cleaned = "".join(chars)
    return cleaned


# openpyxl refuses to SAVE a worksheet whose cell text contains a control
# character other than tab/newline (``IllegalCharacterError``). This mirrors
# ``openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE`` — one stray ``\\x07`` in a model
# response used to abort the WHOLE workbook, so the characters are stripped
# before the cell is written.
_XLSX_ILLEGAL_CHARS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")


def _xlsx_cell(v: Any) -> tuple[Any, bool]:
    """``(value_to_write, needs_text_guard)`` for one workbook cell.

    REVIEW-2026-09-20: the workbook now runs EVERY cell through the same
    ``_export_cell_text(_cell_to_export(v))`` pipeline the CSV/TSV path uses —
    previously to_xlsx applied ``_cell_to_export`` but not the non-finite TEXT
    spellings ("nan"/"inf" a producer had already stringified), so the same
    result exported as an empty CSV cell and a literal "nan" in Excel.
    openpyxl-illegal control characters are stripped here too.

    The second return value is the OWASP formula guard. ``_sanitize_formula_cell``
    encodes it as a leading apostrophe, which is a CSV artifact: in a workbook
    the apostrophe becomes PART OF THE VALUE, so the species name reads
    ``'=cmd...`` in the sheet, the GUI's re-import and any downstream diff see
    it, and the scientific text is corrupted. XLSX has the proper mechanism —
    store the cell as a string and set the ``quotePrefix`` style flag, which
    makes Excel DISPLAY the apostrophe without it being part of the value.
    """
    sanitized = _cell_to_export(v)  # non-finite -> "", formula trigger -> "'..."
    # Real numbers keep their type so Excel can sort/average them; a
    # numeric-looking STRING ("3m", "Bed 3") stays text — auto-converting
    # those loses the unit (documented behaviour of this function).
    if isinstance(sanitized, (int, float, _Decimal)):
        return sanitized, False
    text = _export_cell_text(sanitized)
    if _XLSX_ILLEGAL_CHARS_RE.search(text):
        text = _XLSX_ILLEGAL_CHARS_RE.sub("", text)
    guard = text.startswith("'")
    if guard:
        text = text[1:]
    return text, guard


def to_xlsx(
    data: dict[str, Any],
    file_or_path: str | None = None,
    *,
    include_index: bool = True,
    translate: Callable[[str], str] | None = None,
    warnings_out: list[dict[str, Any]] | None = None,
) -> bytes | None:
    """XLSX with one sheet per table.

    Species name is italicized. Numeric-looking strings (e.g. ``"3m"``)
    are left as text — auto-converting them to numbers loses units.

    Parameters
    ----------
    warnings_out:
        optional list that receives the NON-BLOCKING invariant warnings
        (blank section / open range endpoint). REVIEW-2026-09-20: a UI that
        wants to badge the export passes a list and reads it after the call —
        no second validation pass needed.

    Returns
    -------
    bytes when ``file_or_path`` is None (caller writes to disk).
    None   when ``file_or_path`` is given and the file is written.

    Raises
    ------
    ValueError: when ``data`` violates a BLOCKING export invariant (a species
        row without a species, an inverted FAD/LAD pair) or when the result
        has no tabular shape at all (assistant modes — JSON only).
    RuntimeError: when openpyxl is not installed.
    """
    # F-1 (REVIEW-2026-07-25 P2-5): wire the entry validator so data-integrity
    # violations are surfaced before any table is written to the workbook.
    # REVIEW-2026-09-20: only BLOCKING issues raise. A blank ``section`` or a
    # single range endpoint is what the extractor legitimately emits for a
    # single-column row / an open-ended FAD, and raising there destroyed the
    # user's whole workbook for data that is simply absent (quality.py is the
    # place that reports it).
    ok, issues, warnings = validate_export_invariants(data)
    if not ok:
        raise ValueError(f"export invariants violated: {issues}")
    if warnings_out is not None:
        warnings_out.extend(warnings)

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError as exc:
        raise RuntimeError(
            "XLSX export requires openpyxl. Install with: pip install openpyxl"
        ) from exc

    cfgs = get_configs_for_result(data)
    if not cfgs:
        # REVIEW-2026-09-20: get_configs_for_result no longer invents four
        # empty range-chart sheets for a scatter / geochemical / palaeomap
        # result. openpyxl cannot save a workbook with zero sheets either, so
        # the honest answer is an error naming the real export path.
        raise ValueError(
            "to_xlsx: this result has no tabular structure "
            f"(mode: {detect_tableless_mode(data) or 'unknown'}); "
            "use the JSON export instead")

    wb = Workbook()
    wb.remove(wb.active)

    def _t(key: str) -> str:
        # Translate i18n keys (e.g. "sec.speciesRanges", "col.species") so the
        # sheet names and headers match the user's language instead of showing
        # the raw keys. Falls back to the raw key when no translator is given.
        return translate(key) if translate else key

    bold = Font(bold=True)
    italic = Font(italic=True)
    header_fill = PatternFill(start_color="E0E7FF", end_color="E0E7FF", fill_type="solid")
    thin = Side(border_style="thin", color="CBD5E1")
    cell_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center")
    wrap = Alignment(horizontal="left", vertical="top", wrap_text=True)

    italic_col_idx: dict[str, int] = {
        "species_ranges": 2,   # 1=# index column, 2=species
        "abundances": 2,       # 1=# index column, 2=taxon (genus → italic)
    }

    def _write_row(ws, row_no: int, cells: list[Any], *, header: bool = False,
                   italic_col: int = -1) -> int:
        """Write + style one sheet row; return the next free row number.

        REVIEW-2026-09-20 (item 8): rows used to be addressed as
        ``enumerate(items, start=1) + 1`` while ``ws.append`` advanced at its
        own pace — the other_fossils branch ``continue``d on a blank string
        without any bookkeeping, so from that point on every row got the
        styling (and the italic species column) of a DIFFERENT row, and the
        sheet grew unstyled ghost rows. The counter is now the writer's own.
        """
        for ci, v in enumerate(cells, start=1):
            value, guard = _xlsx_cell(v)
            cell = ws.cell(row=row_no, column=ci, value=value)
            cell.border = cell_border
            if header:
                cell.font = bold
                cell.fill = header_fill
                cell.alignment = center
            else:
                cell.alignment = wrap
                if ci == italic_col:
                    cell.font = italic
            if guard:
                # ORDER MATTERS: assigning `.value` re-derives data_type ('f'
                # for a leading '='), and openpyxl only creates `_style` once
                # a style attribute has been set — hence the border/alignment
                # above. quotePrefix then persists as <xf quotePrefix="1"/>.
                cell.data_type = "s"
                try:
                    st = copy.copy(cell._style)
                    st.quotePrefix = 1
                    cell._style = st
                except (AttributeError, TypeError):  # pragma: no cover - old openpyxl
                    pass
        return row_no + 1

    for cfg in cfgs:
        title_key = cfg.get("title_key") or cfg["id"]
        sheet_name = _xlsx_sheet_name(_t(title_key))
        ws = wb.create_sheet(title=sheet_name)

        # REVIEW-2026-09-20: the SAME row builder as build_table_export /
        # to_csv, so a column that exists in the CSV cannot be missing from
        # the workbook (the columnar sub-tables and the optional per-species
        # columns used to be CSV-only).
        headers, _texts, raw_rows = _export_grid(
            data, cfg, _t, include_index=include_index, index_label="#",
        )
        sheet_row = _write_row(ws, 1, headers, header=True)
        ws.row_dimensions[1].height = 22
        ws.freeze_panes = "A2"

        italic_col = italic_col_idx.get(cfg["id"], -1)
        if italic_col > 0 and not include_index:
            # REVIEW-2026-09-20: the map counts the leading index column, so
            # with include_index=False the italic column drifts one to the
            # right — section names were italicised and species names were
            # not.
            italic_col -= 1

        for raw in raw_rows:
            data_cells = raw[1:] if include_index else raw
            if not any(_export_cell_text(v).strip() for v in data_cells):
                # Blank placeholder row (an empty other_fossils entry, a Qt
                # phantom): not written, and NOT counted.
                continue
            sheet_row = _write_row(ws, sheet_row, raw, italic_col=italic_col)

        # Auto-size columns: min 10, max 60 (capped so a single long cell
        # doesn't blow the column out across multiple screen widths).
        for col_idx, col_cells in enumerate(ws.columns, start=1):
            try:
                length = max(
                    (len(str(c.value)) for c in col_cells if c.value is not None),
                    default=10,
                )
            except Exception:
                length = 10
            letter = ws.cell(row=1, column=col_idx).column_letter
            ws.column_dimensions[letter].width = min(60, max(10, length + 2))

    if file_or_path:
        wb.save(file_or_path)
        return None
    from io import BytesIO
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_newick_file(tree: dict[str, Any], path: str) -> None:
    """Write a normalized phylogenetic tree as a Newick-formatted file.

    Raises
    ------
    OSError: on file write failure.
    """
    # Import lazily here to avoid circular import with extractor.py.
    from .extractor import to_newick
    text = to_newick(tree)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
