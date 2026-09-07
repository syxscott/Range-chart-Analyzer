"""Structured extraction report - the "evidence chain" for one result.

Borrowed from the GitHub survey (2026-09-07, thu-digitizer's
``report.json``): every result should be auditable without re-running the
model - what was requested, what the auto-resolver decided, whether the
payload was truncated, how many rows each table carries, WHY known-empty
tables are empty, and which ICS version anchored the chronostratigraphic
fields (a 2026-09-01 review recommendation).

Pure function over data the caller already has; no I/O, no network. The
server attaches ``"report"`` to its JSON responses; GUIs can call it the
same way.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["build_extraction_report", "REPORT_SCHEMA_VERSION"]

REPORT_SCHEMA_VERSION = 1

# Top-level list keys we track row counts for, by mode family. Unknown
# keys simply add to ``rows.other``.
_TRACKED_LIST_KEYS = (
    "sections", "species_ranges", "biozones", "other_fossils",
    "fossil_legend", "lithology_legend", "cross_beds", "age_units",
    "samples", "sites", "abundances", "zones",
    "zonations", "correlations",
    "data_points", "events", "intervals",
    "groups", "points", "outliers",
    "continents", "oceans_seas", "tectonic_features",
    "biogeographic_realms", "fossil_sites", "paleolatitude_indicators",
    "nodes", "root_ids", "legend",
)


def _ics_version() -> str:
    """Best-effort ICS version stamp (2026-09-01 review recommendation:
    stage-alignment outputs should say WHICH timescale anchored them)."""
    try:
        from .standards.ics import ICS_VERSION
        return ICS_VERSION
    except Exception:
        return "unknown"


def build_extraction_report(
    *,
    data: dict[str, Any],
    mode: str,
    mode_used: Optional[str] = None,
    mode_source: Optional[str] = None,
    truncated: bool = False,
    warning: str = "",
    image_sha256: str = "",
    request_meta: Optional[dict[str, Any]] = None,
    runs: int = 1,
    extra_warnings: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build the evidence-chain report dict for one extraction result.

    Pure and total: never raises, always returns at least the schema
    version and mode identity so the report itself is diagnostic even for
    degenerate payloads.
    """
    data = data if isinstance(data, dict) else {}

    rows: dict[str, int] = {}
    empty_tables: list[dict[str, Any]] = []
    for key in _TRACKED_LIST_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, list):
            rows[key] = len(value)
            if not value:
                reason = "not readable in this figure"
                note = (data.get("_extras") or {}).get("note")
                if isinstance(note, str) and note.strip():
                    reason = note.strip()
                empty_tables.append({"key": key, "reason": reason})
        elif isinstance(value, dict):
            rows[key] = len(value)

    warnings: list[str] = []
    if warning:
        warnings.append(warning)
    inner = data.get("_warnings")
    if isinstance(inner, list):
        warnings.extend(str(w) for w in inner)
    for w in extra_warnings or []:
        warnings.append(str(w))

    auto_mode = data.get("_auto_mode") if isinstance(data.get("_auto_mode"), dict) else None

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": {
            "requested": mode,
            "used": mode_used or mode,
            "source": mode_source or "",
            "auto": auto_mode,
        },
        "input": {
            "image_sha256": image_sha256 or "",
            "runs": runs,
        },
        "truncation": {
            "truncated": bool(truncated),
            "warning": warning or "",
        },
        "rows": rows,
        "empty_tables": empty_tables,
        "warnings": warnings,
        "timescale": {
            "ics_version": _ics_version(),
        },
        "provenance": request_meta or {},
    }
