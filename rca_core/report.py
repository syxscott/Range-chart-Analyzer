"""Structured extraction report - the "evidence chain" for one result.

Borrowed from the GitHub survey (2026-09-07, thu-digitizer's
``report.json``): every result should be auditable without re-running the
model - what was requested, what the auto-resolver decided, whether the
payload was truncated, how many rows each table carries (``rows.other`` plus
``other_keys`` for tables this module does not know), WHY known-empty tables
are empty when the payload actually says why (``empty_tables[].reason`` stays
``""`` when nothing establishes a cause), and which ICS version anchored the
chronostratigraphic fields (a 2026-09-01 review recommendation).

Pure function over data the caller already has; no I/O, no network. The
server attaches ``"report"`` to its JSON responses; GUIs can call it the
same way.
"""

from __future__ import annotations

from typing import Any, Optional

# BORROW-2026-09-20 (A): the coverage contract belongs in the evidence chain —
# an audit of one extraction should say which cells the chart answered, which
# it explicitly left blank, and which nobody ever asked about.
from .reason_codes import coverage_ledger, reason_code_rollup

__all__ = ["build_extraction_report", "REPORT_SCHEMA_VERSION"]

# v2 adds the ``coverage`` block (ledger + reason-code rollup + the rows that
# carry a contract field). Consumers must read it as an optional key.
REPORT_SCHEMA_VERSION = 2

# Top-level list keys we track row counts for, by mode family. Every other
# public (non-underscore) list/dict key is folded into the single aggregate
# ``rows["other"]`` item count, and its name is listed in ``other_keys`` so
# the number can be traced back to the tables it summed.
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


#: Tables the coverage ledger can be built over, with the key that names the
#: "column" (the taxon / sample the range belongs to) in that table.
_COVERAGE_TABLES: tuple[tuple[str, str], ...] = (
    ("species_ranges", "species"),
    ("abundances", "taxon"),
    ("data_points", "sample_id"),
    ("points", "label"),
    ("cross_beds", "name"),
    ("samples", "name"),
)


def _is_contracted(row: Any) -> bool:
    """True when a row carries any part of the coverage contract."""
    return bool(isinstance(row, dict)
                and (row.get("response_kind") or row.get("reason_codes")
                     or row.get("geometry")))


def _coverage_source(data: dict[str, Any]
                     ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Locate the table to audit: the LARGEST one carrying contract fields.

    Returns ``("", [], empty_rollup)`` when nothing answered under the
    contract, so the caller can skip the block instead of reporting a ledger
    full of invented gaps for a pre-contract result.
    """
    empty = reason_code_rollup([])
    best: tuple[int, str] | None = None
    for key, _column in _COVERAGE_TABLES:
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        contracted = sum(1 for r in rows if _is_contracted(r))
        if not contracted:
            continue
        if best is None or len(rows) > best[0]:
            best = (len(rows), key)
    if best is None:
        return "", [], empty
    _count, key = best
    rows = [r for r in data.get(key) if isinstance(r, dict)]
    column_key = dict(_COVERAGE_TABLES)[key]
    return key, rows, reason_code_rollup(rows, column_keys=(column_key,))


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

    # REVIEW-2026-09-20: `_extras` is whatever the model emitted, so it is
    # not guaranteed to be a dict (a list/str leaked through before) —
    # `(x or {}).get()` then raised AttributeError out of a function
    # documented "Pure and total: never raises", and the server attaches this
    # report to its response, so a malformed payload took the request down.
    extras_raw = data.get("_extras")
    extras: dict[str, Any] = extras_raw if isinstance(extras_raw, dict) else {}
    figure_note = extras.get("note")
    figure_note = figure_note.strip() if isinstance(figure_note, str) else ""

    def _table_note(key: str) -> str:
        """Per-table explanation, when the payload carries one for THIS key."""
        for candidate in (f"{key}_note", f"{key}_reason"):
            value = extras.get(candidate)
            if isinstance(value, str) and value.strip():
                return value.strip()
        notes = extras.get("empty_table_notes")
        if isinstance(notes, dict):
            value = notes.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    rows: dict[str, int] = {}
    empty_keys: list[str] = []
    tracked = set(_TRACKED_LIST_KEYS)
    for key in _TRACKED_LIST_KEYS:
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, list):
            rows[key] = len(value)
            if not value:
                empty_keys.append(key)
        elif isinstance(value, dict):
            rows[key] = len(value)

    # REVIEW-2026-09-20 (1): the module docstring and the _TRACKED_LIST_KEYS
    # comment both promised "unknown keys simply add to ``rows.other``", but
    # nothing ever wrote that entry — a chart carrying tables the list never
    # learned about (a new mode's rows key, or model-invented keys) reported a
    # row count of nothing at all. It is now the item count of every untracked
    # public list/dict key, so the audit record can never silently understate
    # the payload. The key names themselves are listed beside it in
    # ``other_keys`` so the number stays attributable.
    other_keys: list[str] = []
    other_rows = 0
    for key, value in data.items():
        if key in tracked or key.startswith("_"):
            continue
        if isinstance(value, (list, dict)):
            other_keys.append(key)
            other_rows += len(value)
    if other_keys:
        rows["other"] = other_rows

    empty_tables: list[dict[str, Any]] = []
    for key in empty_keys:
        # REVIEW-2026-09-20 (2): the old code stamped the invented string
        # "not readable in this figure" on EVERY empty table, and when
        # `_extras.note` existed it reused that single figure-level note as
        # the reason of all of them. Both asserted a cause nothing
        # established, for tables that may simply not exist in the source.
        # Prefer a note written for THIS table; fall back to the figure note
        # only when it is the only empty table (then the note can reasonably
        # be about it); otherwise report no reason. js/table.js already treats
        # a falsy reason as "no reason given", so "" is safe for the UI.
        reason = _table_note(key)
        if not reason and figure_note and len(empty_keys) == 1:
            reason = figure_note
        empty_tables.append({"key": key, "reason": reason})

    warnings: list[str] = []
    if warning:
        warnings.append(warning)
    inner = data.get("_warnings")
    if isinstance(inner, list):
        warnings.extend(str(w) for w in inner)
    for w in extra_warnings or []:
        warnings.append(str(w))

    auto_mode = data.get("_auto_mode") if isinstance(data.get("_auto_mode"), dict) else None

    # BORROW-2026-09-20 (A+B): the coverage evidence chain. Picked over the
    # largest table that actually answered under the contract, so a
    # pre-contract result reports `contracted_rows: 0` and an empty ledger
    # instead of pretending every omission was a gap.
    coverage: dict[str, Any] = {}
    try:
        primary_key, primary_rows, rollup = _coverage_source(data)
        if primary_key:
            coverage = {
                "table": primary_key,
                "row_count": len(primary_rows),
                "contracted_rows": rollup["contracted_rows"],
                "by_response_kind": rollup["by_kind"],
                "by_reason_code": rollup["by_code"],
                "decisions": rollup["entries"],
                "ledger": coverage_ledger(primary_rows),
            }
    except Exception:  # the report is total: never fail on a new field shape
        coverage = {}

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
            # REVIEW-2026-09-10: `truncated=None` means UNKNOWN (server.py
            # uses it when serving a result from the cache, whose originating
            # call's flags may be absent for legacy entries). Collapsing that
            # to False made the audit record assert completeness it could not
            # know about; null says so honestly. Callers that only test
            # truthiness (`!!payload.truncated`) are unaffected.
            "truncated": None if truncated is None else bool(truncated),
            "warning": warning or "",
        },
        "rows": rows,
        # Untracked key names folded into ``rows.other`` (empty when there
        # were none), so the aggregate count stays auditable.
        "other_keys": other_keys,
        "empty_tables": empty_tables,
        # BORROW-2026-09-20 (A): {} when the result never answered under the
        # coverage contract — an absent audit, honestly reported, beats a grid
        # of gaps nothing asked about.
        "coverage": coverage,
        "warnings": warnings,
        "timescale": {
            "ics_version": _ics_version(),
        },
        "provenance": request_meta or {},
    }
