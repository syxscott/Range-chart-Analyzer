"""Mode normalizer: abundance_diagram.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _CONTRACT_SOURCE_KEYS,
    _KNOWN_ABUNDANCE_KEYS,
    _KNOWN_ABUNDANCE_ROOT_KEYS,
    _KNOWN_SITE_KEYS,
    _KNOWN_ZONE_KEYS,
    _carry_extras,
    _confidence_clamped,
    _fold_axis_calibration,
    _iter_rows,
    _pop_array_root_extras,
    _stringify_scalar,
    attach_row_contract,
    axis_domains_from,
)
from typing import Any

def normalize_abundance_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed abundance-diagram JSON into the strict result shape.

    H8: extra keys the model emits are preserved under ``_extras``. The shape
    mirrors range-chart (all-string rows) so the majority-vote merge machinery
    in aggregate.py works with no new code path.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to
    the appropriate keys (sites, abundances, zones).
    """
    # H3-fix: unwrap _array_root wrapper and distribute items to known keys.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                continue
            # Sites have "name" and typically "location" or "depth_unit".
            if "name" in item and ("location" in item or "depth_unit" in item or "age_range" in item):
                parsed.setdefault("sites", []).append(item)
            # Abundances have "taxon", "site", "level", "depth", "abundance".
            elif "taxon" in item or ("abundance" in item and "level" in item):
                parsed.setdefault("abundances", []).append(item)
            # Zones have "name" and "age" (and typically "level_range").
            elif "age" in item and "name" in item:
                parsed.setdefault("zones", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        # REVIEW-2026-09-10: _stringify_scalar, not str(), so a container in a
        # scalar field degrades to "" instead of a Python repr.
        return _stringify_scalar(v)

    warnings: list[str] = []
    out: dict[str, Any] = {
        "sites": [],
        "abundances": [],
        "zones": [],
        "confidence": 0.0,
    }
    # BORROW-2026-09-20 (B): optional figure-level calibration for the
    # continuous axis (depth / abundance value) of the diagram.
    axes = axis_domains_from(parsed)
    # REVIEW-2026-09-10: _iter_rows tolerates a dict-shaped field
    # ({"sites": {"Suigetsu": {...}}}), which previously produced [] and
    # silently discarded every record for this mode.
    for site in _iter_rows(parsed.get("sites"), "sites", warnings):
        row = {
            "name": s(site.get("name")),
            "location": s(site.get("location")),
            "age_range": s(site.get("age_range")),
            "depth_unit": s(site.get("depth_unit")),
        }
        _carry_extras(site, _KNOWN_SITE_KEYS, row)
        out["sites"].append(row)
    for ab in _iter_rows(parsed.get("abundances"), "abundances", warnings):
        row = {
            "taxon": s(ab.get("taxon")),
            "site": s(ab.get("site")),
            "level": s(ab.get("level")),
            "depth": s(ab.get("depth")),
            "abundance": s(ab.get("abundance")),
            "abundance_unit": s(ab.get("abundance_unit")),
        }
        # BORROW-2026-09-20 (A+B): coverage contract + geometry sidecar.
        consumed = attach_row_contract(ab, row, axes=axes)
        _carry_extras(ab, _KNOWN_ABUNDANCE_KEYS + _CONTRACT_SOURCE_KEYS + consumed, row)
        out["abundances"].append(row)
    for z in _iter_rows(parsed.get("zones"), "zones", warnings):
        row = {
            "name": s(z.get("name")),
            "age": s(z.get("age")),
            "level_range": s(z.get("level_range")),
        }
        _carry_extras(z, _KNOWN_ZONE_KEYS, row)
        out["zones"].append(row)
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)
    top_extras = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_ABUNDANCE_ROOT_KEYS})
    # BORROW-2026-09-20 (B): hoist the calibration; absent -> no new key.
    # FIX-2026-09-22 (audit item 7): unusable -> warn + keep the raw block.
    _fold_axis_calibration(parsed, axes, out, top_extras, warnings)
    if top_extras:
        out["_extras"] = top_extras
    if warnings:
        out["_warnings"] = warnings
    return out


