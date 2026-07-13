"""Table configuration + CSV / TSV / JSON export helpers.

The table configs are the single source of truth for both the GUI tables
and the export columns, so exported columns always match what is shown.

Two modes: range-chart (default) and columnar-section. The shape of the
result dict selects which table set is used.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Callable

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
    return isinstance(first, dict) and ("id" in first) and ("name" not in first)


def _range_chart_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Range-chart table configs. Multi-run results gain an agreement column
    on the species table — mirrors js/table.js.
    """
    multi = bool(data) and int(data.get("runs", 1) or 1) > 1
    species_cols = ["col.species", "col.section", "col.rangeBase", "col.rangeTop", "col.biozone"]
    species_row_base = lambda r: [
        r.get("species", ""),
        r.get("section", ""),
        r.get("range_base", ""),
        r.get("range_top", ""),
        r.get("biozone", ""),
    ]

    return [
        {
            "id": "sections",
            "title_key": "sec.sections",
            "cols": ["col.name", "col.ageRange", "col.formations", "col.thickness", "col.coordinates"],
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
            "row": (lambda r: species_row_base(r) + [r.get("agreement", "")]) if multi else species_row_base,
        },
        {
            "id": "biozones",
            "title_key": "sec.biozones",
            "cols": ["col.name", "col.age", "col.thickness"],
            "row": lambda b: [b.get("name", ""), b.get("age", ""), b.get("thickness_m", "")],
        },
        {
            "id": "other_fossils",
            "title_key": "sec.fossils",
            "cols": ["col.fossil"],
            "row": lambda f: [f],
        },
    ]


def _columnar_section_tables(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Columnar-section table configs. Multi-run results gain an agreement
    column on the sections table — mirrors js/table.js.
    """
    multi = bool(data) and int(data.get("runs", 1) or 1) > 1
    sec_cols = ["col.sectionId", "col.sectionGroup", "col.thickness", "col.coordinates"]
    cols_final = sec_cols + (["col.agreement"] if multi else [])
    row_base = lambda s: [
        s.get("id", ""),
        s.get("group", ""),
        s.get("thickness_m", ""),
        s.get("coordinates_text", ""),
    ]
    row = (lambda s: row_base(s) + [s.get("agreement", "")]) if multi else row_base

    return [
        {
            "id": "sections",
            "title_key": "sec.sections",
            "cols": cols_final,
            "row": row,
        },
        {
            "id": "fossil_legend",
            "title_key": "sec.fossils",
            "cols": ["col.fossilMarker", "col.fossilMeaning"],
            "row": lambda it: [it.get("marker", ""), it.get("meaning", "")],
        },
        {
            "id": "lithology_legend",
            "title_key": "sec.columnarLithology",
            "cols": ["col.lithologyPattern", "col.lithologyMeaning"],
            "row": lambda it: [it.get("pattern", it.get("marker", "")), it.get("meaning", "")],
        },
        {
            "id": "cross_beds",
            "title_key": "sec.crossBeds",
            "cols": ["col.crossFrom", "col.crossFromBed", "col.crossTo", "col.crossToBed"],
            "row": lambda it: [
                it.get("from_section", ""),
                "" if it.get("from_bed_idx") is None else str(it.get("from_bed_idx")),
                it.get("to_section", ""),
                "" if it.get("to_bed_idx") is None else str(it.get("to_bed_idx")),
            ],
        },
    ]


def get_configs_for_result(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the table config list appropriate for ``data``."""
    return _columnar_section_tables(data) if _looks_columnar(data) else _range_chart_tables(data)


def get_config(table_id: str) -> dict[str, Any] | None:
    # Search through both presets — used by export path.
    for fn in (_range_chart_tables, _columnar_section_tables):
        for c in fn(None):
            if c["id"] == table_id:
                return c
    return None


# Legacy alias kept for callers that imported TABLE_CONFIGS directly.
TABLE_CONFIGS = _range_chart_tables(None)


def build_table_export(data: dict[str, Any], table_id: str, translate: Callable[[str], str]):
    """Return (headers, rows) for a table, using translated column labels."""
    cfg = get_config(table_id)
    if not cfg:
        return [], []
    headers = [translate("col.index")] + [translate(c) for c in cfg["cols"]]
    n_cols = len(cfg["cols"])
    items = data.get(table_id) or []
    rows = []
    for idx, item in enumerate(items):
        cells = cfg["row"](item)
        # M11: pad / truncate to `n_cols` so a future contributor who adds
        # a custom row extractor can't silently misalign columns between
        # headers and rows on a CSV / Excel paste.
        cells = (cells + [""] * n_cols)[:n_cols]
        rows.append([str(idx + 1)] + ["" if v is None else str(v) for v in cells])
    return headers, rows


def to_csv(headers: list[str], rows: list[list[str]]) -> str:
    """CSV text with a UTF-8 BOM so Excel reads CJK/Cyrillic correctly."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return "﻿" + buf.getvalue()


def to_tsv(headers: list[str], rows: list[list[str]]) -> str:
    """TSV text; tabs/newlines inside cells collapsed to spaces."""
    def clean(v: Any) -> str:
        s = "" if v is None else str(v)
        return " ".join(s.split())

    lines = ["\t".join(clean(h) for h in headers)]
    for row in rows:
        lines.append("\t".join(clean(c) for c in row))
    return "\n".join(lines)


def result_to_json(data: dict[str, Any], source_file: str | None, timestamp: str | None) -> str:
    payload = {
        "extracted_at": timestamp,
        "source_file": source_file,
        "result": data,
    }
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)
