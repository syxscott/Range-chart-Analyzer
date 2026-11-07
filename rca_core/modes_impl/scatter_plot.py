"""Mode normalizer: scatter_plot.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _CONTRACT_SOURCE_KEYS,
    _KNOWN_SCATTER_GROUP_KEYS,
    _KNOWN_SCATTER_OUTLIER_KEYS,
    _KNOWN_SCATTER_PLOT_ROOT_KEYS,
    _KNOWN_SCATTER_POINT_KEYS,
    _SCATTER_ARRAY_SPEC_KEYS,
    _carry_extras,
    _confidence_clamped,
    _dict_rows,
    _fold_axis_calibration,
    _pop_array_root_extras,
    _rehydrate_array_root,
    _stringify_scalar,
    attach_row_contract,
    axis_domains_from,
)
from typing import Any

def normalize_scatter_plot_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed scatter plot JSON into the strict result shape.

    FIX-NEW: new chart type for scatter plots and biplots.
    """
    if not isinstance(parsed, dict):
        return {
            "metadata": {},
            "groups": [],
            "points": [],
            "outliers": [],
            "statistics": {},
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }

    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    out = {
        "metadata": {},
        "groups": [],
        "points": [],
        "outliers": [],
        "statistics": {},
        "confidence": 0.0,
    }

    # Normalize metadata
    # REVIEW-2026-09-10: repairs to surface on the result.
    scatter_warnings: list[str] = []
    # BORROW-2026-09-20 (B): a scatter plot is two continuous axes (x, y).
    _scatter_axes = axis_domains_from(parsed)

    # AUDIT-2026-09-27 P0-1: same missing ``_array_root`` unwrap the paleomap
    # normalizer had - a bare-array reply emptied groups/points/outliers and
    # was still reported as a successful extraction.
    _rehydrate_array_root(parsed, _SCATTER_ARRAY_SPEC_KEYS, scatter_warnings)

    meta = parsed.get("metadata") or {}
    if isinstance(meta, dict):
        out["metadata"] = {
            "title": s(meta.get("title", "")),
            "x_axis_label": s(meta.get("x_axis_label", "")),
            "y_axis_label": s(meta.get("y_axis_label", "")),
            "z_axis_label": s(meta.get("z_axis_label", "")),
            "x_unit": s(meta.get("x_unit", "")),
            "y_unit": s(meta.get("y_unit", "")),
            "n_points": meta.get("n_points") if isinstance(meta.get("n_points"), int) else None,
            "grouping_variable": s(meta.get("grouping_variable", "")),
        }

    # Normalize groups
    for grp in _dict_rows(parsed.get("groups"), "groups", scatter_warnings):
        row = {
            "name": s(grp.get("name")),
            "color": s(grp.get("color")),
            "marker": s(grp.get("marker")),
            "n_points_visible": grp.get("n_points_visible") if isinstance(grp.get("n_points_visible"), int) else None,
            "description": s(grp.get("description")),
        }
        # H8 (Sprint B REVIEW-2026-09-04): preserve unknown row keys.
        _carry_extras(grp, _KNOWN_SCATTER_GROUP_KEYS, row)
        out["groups"].append(row)

    # Normalize points (limit to first 500 for very large outputs)
    # REVIEW-2026-09-10: the cap used to be applied in silence, so the
    # result contradicted its own metadata.n_points (which keeps the model's
    # full count) and the JSON/table/CSV silently reported 500 of N
    # observations. The cap now surfaces as a warning.
    # AUDIT-2026-09-27 P1: `points` / `outliers` take no `kind` - a bare string
    # is not a measurement, so it stays dropped (see _row_from_string).
    _points_all = list(_dict_rows(parsed.get("points")))
    if len(_points_all) > 500:
        scatter_warnings.append("points_truncated_to_500")
    for pt in _points_all[:500]:
        row = {
            "x": s(pt.get("x")),
            "y": s(pt.get("y")),
            "z": s(pt.get("z")),
            "group": s(pt.get("group")),
            "label": s(pt.get("label")),
            "note": s(pt.get("note")),
        }
        _carry_extras(pt,
                      _KNOWN_SCATTER_POINT_KEYS + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(pt, row, axes=_scatter_axes), row)
        out["points"].append(row)

    # Normalize outliers
    for ot in _dict_rows(parsed.get("outliers")):
        row = {
            "x": s(ot.get("x")),
            "y": s(ot.get("y")),
            "group": s(ot.get("group")),
            "reason": s(ot.get("reason")),
        }
        _carry_extras(ot,
                      _KNOWN_SCATTER_OUTLIER_KEYS + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(ot, row, axes=_scatter_axes), row)
        out["outliers"].append(row)

    # Normalize statistics
    stats = parsed.get("statistics") or {}
    if isinstance(stats, dict):
        out["statistics"] = {
            "correlation": s(stats.get("correlation")),
            "regression_line": s(stats.get("regression_line")),
            "r_squared": s(stats.get("r_squared")),
            "p_value": s(stats.get("p_value")),
        }

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)
    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_SCATTER_PLOT_ROOT_KEYS})
    # BORROW-2026-09-20 (B): hoist the calibration; absent -> no new key.
    # FIX-2026-09-22 (audit item 7): unusable -> warn + keep the raw block.
    _fold_axis_calibration(parsed, _scatter_axes, out, extras_src, scatter_warnings)
    if extras_src:
        out["_extras"] = extras_src
    if scatter_warnings:
        out["_warnings"] = scatter_warnings
    return out


