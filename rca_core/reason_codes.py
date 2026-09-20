"""Coverage reason codes and the "answered / not drawn / silently missing" ledger.

BORROW-2026-09-20 (A) — borrowed from ``thu-digitizer``'s reason-code coverage
ledger.  Its core insight is a bookkeeping discipline, not an algorithm: a
digitised table cell must record *why* it holds what it holds, and three
totally different situations must never collapse into the same empty string:

1. something is drawn and it was read            -> ``response_kind: "extracted"``
2. the column/level exists but the author drew a
   dash (or left it blank ON PURPOSE: "not drawn") -> ``response_kind: "not_drawn"``
3. the model simply did not answer that cell      -> silently missing

Case 2 is a POSITIVE observation ("the chart says this taxon is absent here")
and counts as an answered cell; case 3 is a gap.  Before this module both
looked like ``""`` and a run that honestly reported 40 dashes scored *worse*
on coverage than one that quietly omitted those rows — which trains the
operator to prefer the dishonest run.

The fields below are strictly OPTIONAL and additive: every helper tolerates
their absence (legacy results, other modes, hand-edited rows) and every
existing code path keeps working unchanged.  A row without ``response_kind``
is classified by :func:`coverage_state` from the values it does carry.

Contract (mirrored 1:1 by ``js/reason-codes.js``):

* ``response_kind``: ``"extracted"`` | ``"not_drawn"`` | ``"uncertain"``
* ``reason_codes``: list of the 12 slugs in :data:`REASON_CODE_SLUGS`
* merge: ``reason_codes`` union; ``response_kind`` by
  :data:`RESPONSE_KIND_PRECEDENCE` (extracted > uncertain > not_drawn —
  "if any run saw it drawn, it is not 'not drawn'"), divergence flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "ReasonCode", "REASON_CODES", "REASON_CODE_SLUGS", "REASON_CODES_BY_SLUG",
    "RESPONSE_KINDS", "RESPONSE_EXTRACTED", "RESPONSE_NOT_DRAWN",
    "RESPONSE_UNCERTAIN", "SILENT_MISSING", "RESPONSE_KIND_PRECEDENCE",
    "LOW_CONFIDENCE_CODE",
    "is_valid_code", "normalize_code", "normalize_codes", "code_summary",
    "normalize_response_kind", "merge_response_kinds", "merge_reason_codes",
    "coverage_state", "has_value", "is_answered", "row_column", "row_stratum",
    "coverage_ledger", "render_codes", "row_label", "reason_code_rollup",
]


# ---------------------------------------------------------------------------
# The code catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReasonCode:
    """One reason code: a stable machine slug plus a one-line English gloss.

    ``family`` groups the codes so a UI can colour them without hard-coding
    the 12 slugs (coverage = "did it answer", geometry = "where the mark
    ends", source = "where the claim comes from", quality = "how unsure").
    """

    slug: str
    summary: str
    family: str


REASON_CODES: tuple[ReasonCode, ...] = (
    ReasonCode(
        "not_drawn",
        "The column/level exists in the chart but this taxon is deliberately "
        "not drawn there (blank cell, dash or em-dash in place of a range).",
        "coverage",
    ),
    ReasonCode(
        "uncertain",
        "Something is drawn for this cell but its boundary or value could not "
        "be read confidently.",
        "coverage",
    ),
    ReasonCode(
        "obscured",
        "The mark or its label is covered by another element, a caption, a "
        "fold or print bleed.",
        "source",
    ),
    ReasonCode(
        "inferred",
        "The value was interpolated from adjacent evidence instead of read "
        "directly off the chart.",
        "source",
    ),
    ReasonCode(
        "legend_only",
        "The name appears only in the legend, caption or a species list; it "
        "is not plotted in the figure body.",
        "source",
    ),
    ReasonCode(
        "crosses_top",
        "The range or curve runs off the top (youngest) edge of the frame, so "
        "the true upper limit is unknown.",
        "geometry",
    ),
    ReasonCode(
        "crosses_base",
        "The range or curve runs off the bottom (oldest) edge of the frame, "
        "so the true lower limit is unknown.",
        "geometry",
    ),
    ReasonCode(
        "truncated",
        "The mark, its label or the column itself is cut off by the figure "
        "edge, a panel split or a page break.",
        "geometry",
    ),
    ReasonCode(
        "no_label",
        "A mark is drawn but no bed/level/tick label exists to attach a value "
        "to it.",
        "quality",
    ),
    ReasonCode(
        "abbreviated",
        "The name or value is abbreviated on the chart as printed ('Gen. sp.', "
        "'cf.', 'sp.'), so the full form is not recoverable.",
        "quality",
    ),
    ReasonCode(
        "low_confidence",
        "Best-effort value emitted at reduced confidence; see the row note.",
        "quality",
    ),
    ReasonCode(
        "out_of_scope",
        "The cell was not requested (taxon/level outside the requested list) "
        "or belongs to another chart mode's table.",
        "coverage",
    ),
)

REASON_CODES_BY_SLUG: dict[str, ReasonCode] = {c.slug: c for c in REASON_CODES}
REASON_CODE_SLUGS: frozenset[str] = frozenset(REASON_CODES_BY_SLUG)

#: The code the parser adds by itself when it throws away derived evidence
#: (an out-of-range 0-999 position, or one that contradicts the semantic
#: value).  Never a model-facing claim — see extractor.py's geometry guard.
LOW_CONFIDENCE_CODE = "low_confidence"

# Slugs the model tends to spell differently.  Normalisation is deliberately
# conservative: only these known variants are folded, unknown codes are
# DROPPED (a typo must not silently become a new category).
_CODE_ALIASES: dict[str, str] = {
    "notdrawn": "not_drawn",
    "not-drawn": "not_drawn",
    "not drawn": "not_drawn",
    "undrawn": "not_drawn",
    "blank": "not_drawn",
    "dash": "not_drawn",
    "no_range": "not_drawn",
    "unclear": "uncertain",
    "ambiguous": "uncertain",
    "illegible": "uncertain",
    "hidden": "obscured",
    "covered": "obscured",
    "interpolated": "inferred",
    "legend": "legend_only",
    "off_top": "crosses_top",
    "exceeds_top": "crosses_top",
    "off_base": "crosses_base",
    "off_bottom": "crosses_base",
    "exceeds_base": "crosses_base",
    "cut_off": "truncated",
    "cutoff": "truncated",
    "unlabeled": "no_label",
    "unlabelled": "no_label",
    "abbreviation": "abbreviated",
    "abbr": "abbreviated",
    "low-confidence": "low_confidence",
    "lowconf": "low_confidence",
    "out-of-scope": "out_of_scope",
    "outside_scope": "out_of_scope",
}


def is_valid_code(code: Any) -> bool:
    """True for exactly the slugs in :data:`REASON_CODE_SLUGS` (case-insensitive)."""
    if not isinstance(code, str):
        return False
    return code.strip().lower() in REASON_CODE_SLUGS


def normalize_code(value: Any) -> str | None:
    """Return the canonical slug for ``value``, or ``None`` when unknown.

    Accepts a slug, an alias, or a :class:`ReasonCode`.  Unknown text is
    dropped rather than passed through: an unrecognised code is a model
    hallucination and must not inflate the ledger.
    """
    if isinstance(value, ReasonCode):
        return value.slug
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text in REASON_CODE_SLUGS:
        return text
    return _CODE_ALIASES.get(text)


def normalize_codes(values: Any) -> list[str]:
    """Coerce anything the model emitted into a de-duplicated list of slugs.

    Accepts a list, a comma/space separated string, a single slug, a dict
    (``{"not_drawn": true}``) or ``None``.  Order of first appearance is
    preserved so the UI shows the codes in the order they were claimed.
    """
    if values is None:
        return []
    items: Iterable[Any]
    if isinstance(values, dict):
        items = list(values.keys())
    elif isinstance(values, (list, tuple, set, frozenset)):
        items = list(values)
    elif isinstance(values, str):
        text = values.strip()
        if not text:
            return []
        items = [p for p in text.replace(";", ",").split(",") if p.strip()]
    else:
        items = [values]
    out: list[str] = []
    for item in items:
        slug = normalize_code(item)
        if slug and slug not in out:
            out.append(slug)
    return out


def code_summary(code: Any) -> str:
    """One-line English gloss for a slug (the slug itself when unknown)."""
    entry = REASON_CODES_BY_SLUG.get(str(code or "").strip().lower())
    return entry.summary if entry else str(code or "")


def render_codes(codes: Any) -> str:
    """Render a code list for a report / evidence chain: ``"not_drawn, obscured"``.

    Empty / malformed input renders as ``""`` so callers can test truthiness.
    """
    return ", ".join(normalize_codes(codes))


# ---------------------------------------------------------------------------
# The three-state answer
# ---------------------------------------------------------------------------

RESPONSE_EXTRACTED = "extracted"
RESPONSE_NOT_DRAWN = "not_drawn"
RESPONSE_UNCERTAIN = "uncertain"

RESPONSE_KINDS: frozenset[str] = frozenset(
    {RESPONSE_EXTRACTED, RESPONSE_NOT_DRAWN, RESPONSE_UNCERTAIN})

#: Cells "answered" by the model, ranked.  Higher rank wins a merge conflict:
#: a single run that saw a drawn range outweighs any number of runs that
#: reported a dash ("有画就不算未画"), and "I saw something but cannot read it"
#: outranks "nothing is drawn" for the same reason.
RESPONSE_KIND_PRECEDENCE: dict[str, int] = {
    RESPONSE_NOT_DRAWN: 1,
    RESPONSE_UNCERTAIN: 2,
    RESPONSE_EXTRACTED: 3,
}

#: Synthetic state for a cell nobody answered.  Never written to a row.
SILENT_MISSING = "silent_missing"

# Row fields that carry an actual reading, used to classify legacy rows that
# predate ``response_kind`` (and modes that do not emit it).
_VALUE_BEARING_KEYS: tuple[str, ...] = (
    "range_top", "range_base", "range_top_idx", "range_base_idx",
    "abundance", "depth", "depth_m", "age_ma", "top_age", "base_age",
    "thickness_m", "value", "values", "x", "y", "level_range",
)

_KIND_ALIASES: dict[str, str] = {
    "extracted": RESPONSE_EXTRACTED,
    "extract": RESPONSE_EXTRACTED,
    "read": RESPONSE_EXTRACTED,
    "value": RESPONSE_EXTRACTED,
    "drawn": RESPONSE_EXTRACTED,
    "observed": RESPONSE_EXTRACTED,
    "not_drawn": RESPONSE_NOT_DRAWN,
    "notdrawn": RESPONSE_NOT_DRAWN,
    "not-drawn": RESPONSE_NOT_DRAWN,
    "not drawn": RESPONSE_NOT_DRAWN,
    "undrawn": RESPONSE_NOT_DRAWN,
    "blank": RESPONSE_NOT_DRAWN,
    "dash": RESPONSE_NOT_DRAWN,
    "uncertain": RESPONSE_UNCERTAIN,
    "unsure": RESPONSE_UNCERTAIN,
    "unclear": RESPONSE_UNCERTAIN,
    "ambiguous": RESPONSE_UNCERTAIN,
}


def normalize_response_kind(value: Any) -> str | None:
    """Return one of :data:`RESPONSE_KINDS`, or ``None`` when absent/unknown.

    An unknown word is NOT guessed: a silent omission has to stay detectable as
    one, which is the whole point of the ledger.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    return _KIND_ALIASES.get(text)


def _has_value(row: dict[str, Any]) -> bool:
    for key in _VALUE_BEARING_KEYS:
        val = row.get(key)
        if val is None:
            continue
        if isinstance(val, (str, int, float, bool)):
            if str(val).strip():
                return True
        elif isinstance(val, (dict, list, tuple, set)):
            if len(val) > 0:
                return True
        else:  # pragma: no cover - defensive
            return True
    return False


def has_value(row: Any) -> bool:
    """True when a row carries at least one readable value (any mode's field)."""
    return isinstance(row, dict) and _has_value(row)


def coverage_state(row: Any) -> str:
    """Classify one row as ``extracted`` / ``not_drawn`` / ``uncertain`` / ``silent_missing``.

    Explicit beats inferred.  A row without ``response_kind`` that carries a
    reading counts as extracted (legacy results stay usable); a row without
    either is a genuine gap — exactly the case the ledger exists to expose.
    """
    if not isinstance(row, dict):
        return SILENT_MISSING
    kind = normalize_response_kind(row.get("response_kind"))
    if kind:
        return kind
    return RESPONSE_EXTRACTED if _has_value(row) else SILENT_MISSING


def is_answered(row: Any) -> bool:
    """True when the row is an answer — including the honest ``not_drawn``."""
    return coverage_state(row) in RESPONSE_KINDS


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

# "Column" = the horizontal identity of a cell (which taxon), "stratum" = the
# vertical identity (which section / level / site).  Both are read from the
# first key that carries text, so one helper serves every mode.
DEFAULT_COLUMN_KEYS: tuple[str, ...] = ("species", "taxon", "name")
DEFAULT_STRATUM_KEYS: tuple[str, ...] = ("section", "site", "zonation", "level")

# Above this many grid cells the cross-product is skipped (a 500-taxon x
# 200-level diagram would allocate 100k entries to answer a question nobody
# can act on).  The emitted-cell statistics are always computed.
_MAX_LEDGER_CELLS = 20000


def _label(row: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return str(val)
    return ""


def row_column(row: Any, keys: Iterable[str] = DEFAULT_COLUMN_KEYS) -> str:
    """The ledger column label of a row (taxon / species / table row name)."""
    if not isinstance(row, dict):
        return ""
    return _label(row, keys)


def row_stratum(row: Any, keys: Iterable[str] = DEFAULT_STRATUM_KEYS) -> str:
    """The ledger stratum label of a row (section / site / level)."""
    if not isinstance(row, dict):
        return ""
    return _label(row, keys)


def _blank_counts() -> dict[str, int]:
    return {RESPONSE_EXTRACTED: 0, RESPONSE_NOT_DRAWN: 0,
            RESPONSE_UNCERTAIN: 0, SILENT_MISSING: 0}


def _finalize(counts: dict[str, int]) -> dict[str, Any]:
    # Every cell lands in exactly one of the four buckets, so the cell count
    # is the bucket sum - correct for the grid view and for the emitted-only
    # fallback below the :data:`_MAX_LEDGER_CELLS` guard.
    cells = sum(counts.values())
    answered = (counts[RESPONSE_EXTRACTED] + counts[RESPONSE_NOT_DRAWN]
                + counts[RESPONSE_UNCERTAIN])
    return {
        "cells": cells,
        "extracted": counts[RESPONSE_EXTRACTED],
        "not_drawn": counts[RESPONSE_NOT_DRAWN],
        "uncertain": counts[RESPONSE_UNCERTAIN],
        "silent_missing": counts[SILENT_MISSING],
        # honest_coverage counts an explicit "the chart draws nothing here" as
        # the answer it is; strict_coverage still demands a readable value.
        "answered": answered,
        "honest_coverage": round(answered / cells, 4) if cells else 0.0,
        "strict_coverage": round(counts[RESPONSE_EXTRACTED] / cells, 4) if cells else 0.0,
    }


def coverage_ledger(rows: Any,
                    *,
                    column_keys: Iterable[str] = DEFAULT_COLUMN_KEYS,
                    stratum_keys: Iterable[str] = DEFAULT_STRATUM_KEYS,
                    cross_product: bool = True) -> dict[str, Any]:
    """Summarise which cells were answered, which were declared not drawn,
    and which were silently skipped.

    ``rows`` is any iterable of row dicts (``data["species_ranges"]``,
    ``data["abundances"]``, ...).  The return value is JSON-friendly and
    total — it never raises on malformed input:

    ``{"columns": [{"column", "cells", "extracted", "not_drawn",
    "uncertain", "silent_missing", "answered", "honest_coverage",
    "strict_coverage"}], "strata": [same keyed by "stratum"], "totals":
    {same fields}, "reason_code_counts": {slug: n}, "explicit_responses": n,
    "row_count": n, "unattributed_rows": n, "grid_used": bool}``

    The grid is the cross product of the observed columns x the observed
    strata; a grid cell with no row at all is ``silent_missing``.  When the
    model never emits a stratum (single-section charts) the stratum set is
    ``{""}`` and the grid degenerates to the emitted rows, so no phantom gaps
    appear.
    """
    column_keys = tuple(column_keys)
    stratum_keys = tuple(stratum_keys)
    cell_state: dict[tuple[str, str], str] = {}
    cell_order: list[tuple[str, str]] = []
    columns: list[str] = []
    strata: list[str] = []
    code_counts: dict[str, int] = {}
    row_count = 0
    unattributed = 0
    explicit = 0

    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        row_count += 1
        if normalize_response_kind(row.get("response_kind")):
            explicit += 1
        for code in normalize_codes(row.get("reason_codes")):
            code_counts[code] = code_counts.get(code, 0) + 1
        column = row_column(row, column_keys)
        if not column:
            unattributed += 1
            continue
        stratum = row_stratum(row, stratum_keys)
        state = coverage_state(row)
        if column not in columns:
            columns.append(column)
        if stratum not in strata:
            strata.append(stratum)
        key = (column, stratum)
        prev = cell_state.get(key)
        if prev is None:
            cell_state[key] = state
            cell_order.append(key)
        elif (RESPONSE_KIND_PRECEDENCE.get(state, 0)
                > RESPONSE_KIND_PRECEDENCE.get(prev, 0)):
            # Several rows for the same cell: the strongest answer wins, so a
            # duplicated "not drawn" can never mask a drawn range.
            cell_state[key] = state

    emitted_counts = _blank_counts()
    per_column: dict[str, dict[str, int]] = {}
    per_stratum: dict[str, dict[str, int]] = {}

    def _bump(bucket: dict[str, dict[str, int]], label: str, state: str) -> None:
        counts = bucket.setdefault(label, _blank_counts())
        counts[state] += 1

    grid_used = False
    grid_cells: list[tuple[str, str]] = cell_order
    if (cross_product and columns and strata
            and len(columns) * len(strata) <= _MAX_LEDGER_CELLS):
        grid_used = True
        grid_cells = [(c, s) for c in columns for s in strata]

    for key in grid_cells:
        state = cell_state.get(key) or SILENT_MISSING
        emitted_counts[state] += 1
        _bump(per_column, key[0], state)
        _bump(per_stratum, key[1], state)

    totals = _finalize(emitted_counts)
    totals["explicit_responses"] = explicit
    totals["row_count"] = row_count
    totals["unattributed_rows"] = unattributed

    return {
        "columns": [dict({"column": c}, **_finalize(per_column.get(c) or _blank_counts()))
                    for c in columns],
        "strata": [dict({"stratum": s}, **_finalize(per_stratum.get(s) or _blank_counts()))
                   for s in strata],
        "totals": totals,
        "reason_code_counts": dict(sorted(code_counts.items())),
        "explicit_responses": explicit,
        "row_count": row_count,
        "unattributed_rows": unattributed,
        "grid_used": grid_used,
    }


# ---------------------------------------------------------------------------
# Merge rules (used by rca_core/aggregate.py)
# ---------------------------------------------------------------------------

def merge_response_kinds(values: Iterable[Any]) -> tuple[str | None, bool]:
    """Resolve per-run ``response_kind`` votes into one value.

    Returns ``(kind, divergent)``.  Precedence is
    :data:`RESPONSE_KIND_PRECEDENCE` — ``extracted`` beats ``not_drawn``
    because a single run that saw a drawn range disproves "nothing is drawn",
    and the divergence flag lets the merge record the disagreement as a row
    warning instead of hiding it.  ``None`` when no run answered.
    """
    seen = [k for k in (normalize_response_kind(v) for v in (values or [])) if k]
    if not seen:
        return None, False
    best = max(seen, key=lambda k: RESPONSE_KIND_PRECEDENCE[k])
    return best, len(set(seen)) > 1


def merge_reason_codes(values: Iterable[Any]) -> list[str]:
    """Union of the per-run ``reason_codes`` lists (first-seen order)."""
    out: list[str] = []
    for group in (values or []):
        for slug in normalize_codes(group):
            if slug not in out:
                out.append(slug)
    return out


# ---------------------------------------------------------------------------
# Reporting helpers (used by rca_core/report.py)
# ---------------------------------------------------------------------------

def row_label(row: Any,
              keys: Iterable[str] = DEFAULT_COLUMN_KEYS) -> str:
    """Human-readable identity of a row for an evidence listing."""
    if not isinstance(row, dict):
        return ""
    return row_column(row, keys) or row_stratum(row, DEFAULT_STRATUM_KEYS)


def reason_code_rollup(rows: Any,
                       *,
                       column_keys: Iterable[str] = DEFAULT_COLUMN_KEYS,
                       limit: int = 200,
                       ) -> dict[str, Any]:
    """Summarise the contract fields of ``rows`` for the audit report.

    Returns ``{"contracted_rows", "by_code": {slug: count},
    "by_kind": {kind: count}, "entries": [{row, response_kind, reason_codes,
    code_summaries}]}`` where ``entries`` lists ONLY rows that carry a
    ``response_kind`` or at least one valid ``reason_codes`` value — the
    evidence chain should show the decisions, not re-transcribe the table.
    At most ``limit`` entries are listed; the counts cover every row.
    """
    by_code: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    contracted = 0
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        kind = normalize_response_kind(row.get("response_kind"))
        codes = normalize_codes(row.get("reason_codes")
                                or row.get("reason_code"))
        if not kind and not codes:
            continue
        contracted += 1
        if kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        for slug in codes:
            by_code[slug] = by_code.get(slug, 0) + 1
        if len(entries) < max(0, int(limit)):
            entry: dict[str, Any] = {"row": row_label(row, column_keys)}
            if kind:
                entry["response_kind"] = kind
            if codes:
                entry["reason_codes"] = codes
                entry["code_summaries"] = [code_summary(c) for c in codes]
            geometry = row.get("geometry")
            if isinstance(geometry, dict) and geometry.get("points"):
                # (B) says whether the position reads were usable evidence and
                # whether anything converted them — "4 of 4 points, calibrated"
                # is the auditable statement, the numbers themselves stay on
                # the row.
                points = geometry.get("points")
                entry["geometry"] = {
                    "points": len(points) if isinstance(points, dict) else 0,
                    "calibrated": bool(geometry.get("calibrated")),
                }
            entries.append(entry)
    return {
        "contracted_rows": contracted,
        "by_code": dict(sorted(by_code.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "entries": entries,
    }
