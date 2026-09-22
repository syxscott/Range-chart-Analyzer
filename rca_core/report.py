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


# FIX-2026-09-22 (audit item 6): the ledger input used to be resolved here by a
# private copy of quality.py's table list plus a looser "is this row under the
# contract" test — three tables and two predicates that had quietly drifted
# apart, so the evidence report graded a DIFFERENT table than the quality score
# printed two sections above it (a chemical payload reported the 2-row
# ``events`` rollup while quality reported the 6-row ``data_points`` grid).
# Everything now comes from rca_core/quality.py, the single source of truth.
from .quality import (  # noqa: E402
    coverage_column_keys,
    select_coverage_table,
)


def _coverage_source(data: dict[str, Any]
                     ) -> tuple[str, list[dict[str, Any]], dict[str, Any],
                                tuple[str, ...]]:
    """Locate the table to audit: the LARGEST one carrying contract fields.

    Returns ``(table_key, rows, reason_code_rollup, ledger_column_keys)``;
    ``("", [], empty_rollup, ())`` when nothing answered under the contract, so
    the caller skips the block instead of reporting a ledger full of invented
    gaps for a pre-contract result.

    FIX-2026-09-22 (audit item 6): the pick and the column ladder come from
    :mod:`rca_core.quality` now, so the report audits the SAME table with the
    SAME column naming as the quality score it prints next to. It used to keep
    a private table list (three extra entries) and a looser "contracted" test
    that also accepted a bare ``geometry`` block, and it then called
    ``coverage_ledger`` with the DEFAULT column keys — so a payload whose
    primary rows are keyed by ``sample_id`` / ``label`` lost every stratum and
    reported ``cells: 0``: a grid of one column per sample, no taxon columns at
    all.
    """
    empty = reason_code_rollup([])
    picked = select_coverage_table(data)
    if picked is None:
        return "", [], empty, ()
    key, column_key = picked
    rows = [r for r in (data.get(key) or []) if isinstance(r, dict)]
    return (key, rows,
            reason_code_rollup(rows, column_keys=(column_key,)),
            coverage_column_keys(column_key))


def _ics_version() -> str:
    """Best-effort ICS version stamp (2026-09-01 review recommendation:
    stage-alignment outputs should say WHICH timescale anchored them)."""
    try:
        from .standards.ics import ICS_VERSION
        return ICS_VERSION
    except Exception:
        return "unknown"


def _localize_decisions(entries: list[dict[str, Any]], lang: str) -> None:
    """Translate each decision entry's ``code_summaries`` in place.

    FIX-2026-09-22 (A2, audit carry-over): the rollup emitted by
    :func:`rca_core.reason_codes.reason_code_rollup` carries the English
    :func:`code_summary` gloss, so a zh/ja audit report printed English text
    inside an otherwise-localized document. The ``reason_code.<slug>`` keys
    exist in all three catalogs of rca_core/i18n.py; the machine-readable
    slug list (``reason_codes``) is untouched, and an unknown slug falls back
    to English then to the slug itself (``Translator.t`` semantics). Called
    only when the caller passed a non-empty ``lang``; server.py callers keep
    the historical English shape.
    """
    if not lang or lang == "en":
        return
    try:
        from .i18n import Translator
        tr = Translator(lang)
    except Exception:  # i18n unavailable: stay English (the report is total)
        return
    for entry in entries:
        codes = entry.get("reason_codes")
        if not codes:
            continue
        glosses: list[str] = []
        for c in codes:
            key = "reason_code." + str(c)
            text = tr.t(key)
            if text == key:  # unknown slug: Translator hands the key back
                text = str(c)  # ... while code_summary() glosses it verbatim
            glosses.append(text)
        entry["code_summaries"] = glosses


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
    lang: str = "",
) -> dict[str, Any]:
    """Build the evidence-chain report dict for one extraction result.

    Pure and total: never raises, always returns at least the schema
    version and mode identity so the report itself is diagnostic even for
    degenerate payloads.

    ``lang`` (FIX-2026-09-22 A2): optional BCP-47 tag ("zh" / "en" / "ja").
    When a non-English value is given, the human-readable glosses of the
    coverage decisions are routed through rca_core/i18n.py so the audit
    report localizes; the machine-readable slugs never change, and callers
    that omit ``lang`` keep the historical English shape verbatim.
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
        # FIX-2026-09-22 (A2): _coverage_source grew a 4th return value
        # (ledger_column_keys) in the quality-shared refactor, but this call
        # site still unpacked three — the ValueError was swallowed by the
        # blanket `except Exception` below, so the audit report emitted an
        # empty coverage block ('{}') instead of the evidence chain. Unpack
        # all four and feed the SAME column ladder to the ledger that
        # quality.coverage_for uses (the old bug: cells:0 vs cells:2).
        primary_key, primary_rows, rollup, ledger_columns = _coverage_source(data)
        if primary_key:
            coverage = {
                "table": primary_key,
                "row_count": len(primary_rows),
                "contracted_rows": rollup["contracted_rows"],
                "by_response_kind": rollup["by_kind"],
                "by_reason_code": rollup["by_code"],
                "decisions": rollup["entries"],
                "ledger": coverage_ledger(primary_rows,
                                         column_keys=ledger_columns),
            }
        # FIX-2026-09-22 (A2): localize the decision glosses when the caller
        # declared a report language (no-op for "" / "en", see helper).
        if coverage.get("decisions"):
            _localize_decisions(coverage["decisions"], lang)
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
