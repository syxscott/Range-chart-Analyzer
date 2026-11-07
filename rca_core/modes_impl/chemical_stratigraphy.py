"""Mode normalizer: chemical_stratigraphy.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _CONTRACT_SOURCE_KEYS,
    _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS,
    _KNOWN_CHEMICAL_STRAT_EVENT_KEYS,
    _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS,
    _KNOWN_CHEMICAL_STRAT_ROOT_KEYS,
    _carry_extras,
    _confidence_clamped,
    _dict_rows,
    _fold_axis_calibration,
    _pop_array_root_extras,
    _stringify_scalar,
    _unwrap_array_root_into,
    attach_row_contract,
    axis_domains_from,
)
from typing import Any

def normalize_chemical_stratigraphy_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed chemical stratigraphy JSON into the strict result shape.

    FIX-NEW: new chart type for isotopic curves, elemental data.
    H8: extra keys the model emits are preserved under ``_extras``.
    """
    if not isinstance(parsed, dict):
        return {
            "metadata": {},
            "data_points": [],
            "events": [],
            "intervals": [],
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }

    def s(v):
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    # REVIEW-2026-09-10: repairs to surface on the result.
    _chem_warnings: list[str] = []
    out = {
        "metadata": {},
        "data_points": [],
        "events": [],
        "intervals": [],
        "confidence": 0.0,
    }

    # REVIEW-2026-09-20 #6: this was the only normalizer without the
    # ``_array_root`` unwrap every other mode has - json_utils wraps a bare
    # array emission (the model replying ``[{...}, {...}]`` to an object
    # contract) into {"_array_root": [...]}, and the whole payload was thrown
    # away below because parsed.get("data_points") was empty.
    # BORROW-2026-09-20 (B): depth/age are a continuous axis here, so read the
    # optional calibration once for all rows.
    _chem_axes = axis_domains_from(parsed)
    _unwrap_array_root_into(parsed, out, (
        ("data_points", ("sample_id", "depth_m"), out["data_points"]),
        ("events", ("type", "depth_m"), out["events"]),
        ("intervals", ("name", "top_depth_m"), out["intervals"]),
    ), _chem_warnings)

    # Normalize metadata
    meta = parsed.get("metadata") or {}
    if isinstance(meta, dict):
        out["metadata"] = {
            "section_name": s(meta.get("section_name", "")),
            "location": s(meta.get("location", "")),
            "latitude": s(meta.get("latitude", "")),
            "longitude": s(meta.get("longitude", "")),
            "age_range": s(meta.get("age_range", "")),
            "curve_types": meta.get("curve_types") if isinstance(meta.get("curve_types"), list) else [],
        }

    # Normalize data_points
    for pt in _dict_rows(parsed.get("data_points"), "data_points", _chem_warnings):
        values_raw = pt.get("values") or {}
        values = {}
        if isinstance(values_raw, dict):
            for k, v in values_raw.items():
                values[str(k)] = s(v)
        elif isinstance(values_raw, list):
            # REVIEW-2026-09-10: a non-dict `values` (e.g. the model emitting
            # [{"name": "d13C", "value": "-1.24"}]) used to leave values == {}
            # with the raw list unrecoverable - `values` is a known key, so
            # _carry_extras would not preserve it either. That produced a
            # sample row that looks valid but carries no geochemistry.
            # Accept the common list-of-{name,value} shape, and keep anything
            # unrecognised under a companion key rather than losing it.
            for entry in values_raw:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("name", entry.get("element", entry.get("isotope")))
                if label is None:
                    continue
                values[str(label)] = s(entry.get("value", entry.get("val", "")))
        row = {
            "sample_id": s(pt.get("sample_id")),
            "depth_m": s(pt.get("depth_m")),
            "age_ma": s(pt.get("age_ma")),
            "stage": s(pt.get("stage")),
            "values": values,
            "lithology": s(pt.get("lithology")),
            "fossil_horizon": s(pt.get("fossil_horizon")),
            "note": s(pt.get("note")),
        }
        _carry_extras(pt,
                      _KNOWN_CHEMICAL_STRAT_DATA_POINT_KEYS
                      + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(pt, row, axes=_chem_axes), row)
        if values_raw and not values:
            # Nothing could be lifted out of a non-dict `values`. Keep the raw
            # payload on the row so the measurements are not silently lost.
            extras = row.get("_extras")
            if not isinstance(extras, dict):
                extras = {}
                row["_extras"] = extras
            extras["values_raw"] = values_raw
            if "values_unparseable" not in _chem_warnings:
                _chem_warnings.append("values_unparseable")
        out["data_points"].append(row)

    # Normalize events
    # REVIEW-2026-09-20 #4: a dict-shaped ``events`` ({"AEZZ": {...}}) failed
    # the isinstance check for every value and the whole emission vanished
    # silently. Same tolerant path as data_points above (and norm_cross in
    # the columnar mode).
    for ev in _dict_rows(parsed.get("events"), "events", _chem_warnings):
        row = {
            "type": s(ev.get("type")),
            "depth_m": s(ev.get("depth_m")),
            "age_ma": s(ev.get("age_ma")),
            "name": s(ev.get("name")),
            "magnitude": s(ev.get("magnitude")),
            "description": s(ev.get("description")),
        }
        _carry_extras(ev,
                      _KNOWN_CHEMICAL_STRAT_EVENT_KEYS
                      + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(ev, row, axes=_chem_axes), row)
        out["events"].append(row)

    # Normalize intervals
    # REVIEW-2026-09-20 #4: same dict-shaped recovery as events.
    for iv in _dict_rows(parsed.get("intervals"), "intervals", _chem_warnings):
        _iv_top_depth = s(iv.get("top_depth_m"))
        _iv_base_depth = s(iv.get("base_depth_m"))
        _iv_top_age = s(iv.get("top_age_ma"))
        _iv_base_age = s(iv.get("base_age_ma"))
        # FIX-2026-10-01: chemical-stratigraphy intervals had NO order check at
        # all, while columnar's lithology_blocks / age_units swap an inverted
        # pair and flag it and range_chart's species indices do the same. Two
        # independent impossibilities were therefore accepted silently and
        # exported:
        #   * depth pair inverted  (top DEEPER than base, "older above younger")
        #   * age pair inverted    (base YOUNGER than top -- a layer whose top
        #     is older than its base, which cannot exist)
        # A duration computed downstream from an inverted age pair comes out
        # negative. Swap and flag, exactly like the columnar path, so the
        # operator sees the repair instead of inheriting a reversed interval.
        _iv_warnings: list[str] = []

        def _num_or_none(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        _td = _num_or_none(_iv_top_depth)
        _bd = _num_or_none(_iv_base_depth)
        if _td is not None and _bd is not None and _td > _bd:
            _iv_top_depth, _iv_base_depth = _iv_base_depth, _iv_top_depth
            _iv_warnings.append("interval_depth_order_swap")
        _ta = _num_or_none(_iv_top_age)
        _ba = _num_or_none(_iv_base_age)
        # Geologic convention: base is the OLDER end, so its Ma is the LARGER
        # one. base < top means the base is younger -> inverted.
        if _ta is not None and _ba is not None and _ba < _ta:
            _iv_top_age, _iv_base_age = _iv_base_age, _iv_top_age
            _iv_warnings.append("interval_age_order_swap")
        row = {
            "name": s(iv.get("name")),
            "top_depth_m": _iv_top_depth,
            "base_depth_m": _iv_base_depth,
            "top_age_ma": _iv_top_age,
            "base_age_ma": _iv_base_age,
            "characteristic_values": s(iv.get("characteristic_values")),
            "lithology": s(iv.get("lithology")),
        }
        if _iv_warnings:
            row["_warning"] = (_iv_warnings[0] if len(_iv_warnings) == 1
                               else _iv_warnings)
            for _w in _iv_warnings:
                if _w not in _chem_warnings:
                    _chem_warnings.append(_w)
        _carry_extras(iv,
                      _KNOWN_CHEMICAL_STRAT_INTERVAL_KEYS
                      + _CONTRACT_SOURCE_KEYS
                      + attach_row_contract(iv, row, axes=_chem_axes), row)
        out["intervals"].append(row)

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)
    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items()
         if k not in _KNOWN_CHEMICAL_STRAT_ROOT_KEYS})
    # BORROW-2026-09-20 (B): hoist the calibration; absent -> no new key.
    # FIX-2026-09-22 (audit item 7): unusable -> warn + keep the raw block.
    _fold_axis_calibration(parsed, _chem_axes, out, extras_src, _chem_warnings)
    if extras_src:
        out["_extras"] = extras_src
    if _chem_warnings:
        out["_warnings"] = _chem_warnings
    return out


