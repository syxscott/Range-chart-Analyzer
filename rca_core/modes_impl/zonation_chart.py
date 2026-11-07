"""Mode normalizer: zonation_chart.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _KNOWN_CORRELATION_KEYS,
    _KNOWN_ZONATIONS_KEYS,
    _KNOWN_ZONATION_ROOT_KEYS,
    _KNOWN_ZONATION_ZONE_KEYS,
    _carry_extras,
    _confidence_clamped,
    _iter_rows,
    _pop_array_root_extras,
    _stringify_scalar,
)
from typing import Any

def normalize_zonation_chart_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed zonation-chart JSON into the strict result shape.

    H8: extra keys the model emits are preserved under ``_extras`` per row.
    All rows are string-typed, mirroring range-chart so the majority-vote
    merge machinery in aggregate.py works with no new code path.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to the
    appropriate keys (zonations, zones, correlations).
    """
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                parsed.setdefault("_unclassified", []).append(item)
                continue
            # Correlations carry from_zone / to_zone.
            if "from_zone" in item or "to_zone" in item:
                parsed.setdefault("correlations", []).append(item)
            # Zone rows carry a name plus rank / zonation / age_span markers.
            elif "name" in item and (
                "rank" in item or "zonation" in item or "age_span" in item
                or "defined_by" in item or "base_age" in item
            ):
                parsed.setdefault("zones", []).append(item)
            # Zonation columns carry name + region / framework / reference.
            elif "name" in item and (
                "region" in item or "framework" in item or "reference" in item
            ):
                parsed.setdefault("zonations", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    warnings: list[str] = []
    out: dict[str, Any] = {
        "zonations": [],
        "zones": [],
        "correlations": [],
        "confidence": 0.0,
    }
    if not isinstance(parsed, dict):
        return out
    # REVIEW-2026-09-10: _iter_rows also recovers dict-shaped fields
    # ({"zones": {"Z1": {...}}}) that previously collapsed to [].
    for z in _iter_rows(parsed.get("zonations"), "zonations", warnings):
        row = {
            "name": s(z.get("name")),
            "region": s(z.get("region")),
            "framework": s(z.get("framework")),
            "reference": s(z.get("reference")),
        }
        _carry_extras(z, _KNOWN_ZONATIONS_KEYS, row)
        out["zonations"].append(row)
    for z in _iter_rows(parsed.get("zones"), "zones", warnings):
        row = {
            "name": s(z.get("name")),
            "zonation": s(z.get("zonation")),
            "rank": s(z.get("rank")),
            "age_span": s(z.get("age_span")),
            "base_age": s(z.get("base_age")),
            "top_age": s(z.get("top_age")),
            "stage": s(z.get("stage")),
            "defined_by": s(z.get("defined_by")),
            "note": s(z.get("note")),
        }
        _carry_extras(z, _KNOWN_ZONATION_ZONE_KEYS, row)
        out["zones"].append(row)
    for c in _iter_rows(parsed.get("correlations"), "correlations", warnings):
        row = {
            "from_zone": s(c.get("from_zone")),
            "to_zone": s(c.get("to_zone")),
            "from_zonation": s(c.get("from_zonation")),
            "to_zonation": s(c.get("to_zonation")),
            "basis": s(c.get("basis")),
            "note": s(c.get("note")),
        }
        _carry_extras(c, _KNOWN_CORRELATION_KEYS, row)
        out["correlations"].append(row)
    try:
        out["confidence"] = _confidence_clamped(
            float(parsed.get("confidence", 0.0) or 0.0))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_ZONATION_ROOT_KEYS})
    if extras_src:
        out["_extras"] = extras_src
    if warnings:
        out["_warnings"] = warnings
    return out


