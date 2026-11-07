"""Mode normalizer: columnar_section.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _KNOWN_BLOCK_KEYS,
    _KNOWN_COLUMNAR_ROOT_KEYS,
    _KNOWN_COLUMNAR_SECTION_KEYS,
    _KNOWN_CROSS_KEYS,
    _KNOWN_LEGEND_KEYS,
    _KNOWN_SAMPLE_KEYS,
    _KNOWN_UNIT_KEYS,
    _carry_extras,
    _confidence_clamped,
    _dict_rows,
    _stringify_scalar,
)
from typing import Any

def normalize_columnar_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed columnar-section JSON into the strict result shape.

    H8: extra keys the model emits are preserved under ``_extras``.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to
    the appropriate keys (sections, fossil_legend, lithology_legend, cross_beds).
    """
    # H3-fix: unwrap _array_root wrapper and distribute items to known keys.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                continue
            # Columnar sections have "lithology_blocks" or "age_units" as
            # distinguishing features.
            if "lithology_blocks" in item or "age_units" in item or "id" in item:
                parsed.setdefault("sections", []).append(item)
            # Legend items have "marker" or "pattern" + "meaning".
            elif "meaning" in item and ("marker" in item or "pattern" in item):
                if "marker" in item:
                    parsed.setdefault("fossil_legend", []).append(item)
                else:
                    parsed.setdefault("lithology_legend", []).append(item)
            # Cross-bed entries have from_section/to_section.
            elif "from_section" in item or "from_bed_idx" in item:
                parsed.setdefault("cross_beds", []).append(item)
            else:
                parsed.setdefault("_unclassified", []).append(item)

    def s(v: Any) -> str:
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    def fi(v: Any) -> tuple[int | None, bool]:
        """Convert a bed-index field to ``(int | None, lossy)``.

        LOW fix: previously a numeric string like ``"8.0"`` or a float
        ``8.5`` was silently nulled (resp. floored to 8) without warning.

        Sprint B (REVIEW-2026-09-04): the docstring promised a row-level
        ``_warning`` for the float→int truncation path, but the flag was
        never surfaced. ``fi`` now returns a second element: ``True`` when
        the value had to go through float coercion (``"8.5"`` -> 8,
        ``8.5`` -> 8, and any numeric string ``int()`` rejects), and the
        callers below attach a row-level ``_warning`` so the operator sees
        a flagged value instead of a silent data loss.

        Return contract:
          ``(None, False)``   unparseable / empty / bool input
          ``(v, False)``      clean int conversion (int or int-string)
          ``(int(v), True)``  lossy truncation path taken
        """
        if v is None or v == "":
            return None, False
        if isinstance(v, bool):
            # bool is an int subclass — treat True/False as 1/0 is
            # surprising; return None instead so the caller can flag it.
            return None, False
        if isinstance(v, int):
            return v, False
        if isinstance(v, float):
            # Sprint B (REVIEW-2026-09-04): int() on a float TRUNCATES
            # silently (8.5 -> 8, and 9.5 -> 9) without ever raising, so
            # the old code's float fallback below never saw floats. Flag
            # every float as the lossy path it is.
            try:
                return int(v), True
            except (TypeError, ValueError, OverflowError):
                return None, False
        try:
            return int(v), False
        except (TypeError, ValueError):
            pass
        # Fall back to float coercion (handles "8.5" -> 8, "8.0" -> 8).
        # This is the lossy path — signal it to the caller.
        try:
            return int(float(v)), True
        except (TypeError, ValueError):
            return None, False

    def norm_blocks(items):
        out = []
        for b in _dict_rows(items):
            raw_top = b.get("range_top_idx")
            raw_base = b.get("range_base_idx")
            top_idx, top_lossy = fi(raw_top)
            base_idx, base_lossy = fi(raw_base)
            # B-3 fix: enforce top (younger/higher) >= base (older/lower).
            # The prompt says "1-indexed from bottom (oldest=1), top >= base".
            # If the model emitted them reversed, swap and flag so the UI
            # can surface a warning without discarding the data.
            swapped = False
            if top_idx is not None and base_idx is not None and top_idx < base_idx:
                top_idx, base_idx = base_idx, top_idx
                swapped = True
            row = {
                "pattern": s(b.get("pattern")),
                "range_top_idx": top_idx,
                "range_base_idx": base_idx,
            }
            warnings: list[str] = []
            # LOW fix: flag when fi() silently coerced (numeric-string or
            # float) so the operator can audit the conversion instead of
            # seeing a clean None or floored integer.
            # Sprint B (REVIEW-2026-09-04): fi() also reports the lossy
            # float-truncation path ("8.5" -> 8) — surface it here as the
            # row-level ``_warning`` the fi() docstring always promised.
            if top_idx is None and raw_top not in (None, ""):
                warnings.append("range_top_idx_unparseable")
            elif top_lossy:
                warnings.append("range_top_idx_truncated")
            if base_idx is None and raw_base not in (None, ""):
                warnings.append("range_base_idx_unparseable")
            elif base_lossy:
                warnings.append("range_base_idx_truncated")
            if swapped:
                warnings.append("index_order_swap")
            if warnings:
                row["_warning"] = warnings[0] if len(warnings) == 1 else warnings
            _carry_extras(b, _KNOWN_BLOCK_KEYS, row)
            out.append(row)
        return out

    def norm_units(items):
        out = []
        for u in _dict_rows(items):
            top_idx, top_lossy = fi(u.get("range_top_idx"))
            base_idx, base_lossy = fi(u.get("range_base_idx"))
            # B-3 fix: same swap-logic for age_units.
            swapped = False
            if top_idx is not None and base_idx is not None and top_idx < base_idx:
                top_idx, base_idx = base_idx, top_idx
                swapped = True
            row = {
                "label": s(u.get("label")),
                "range_top_idx": top_idx,
                "range_base_idx": base_idx,
            }
            # Sprint B (REVIEW-2026-09-04): mirror norm_blocks — report the
            # lossy float-truncation path via row-level ``_warning``.
            warnings: list[str] = []
            if swapped:
                warnings.append("index_order_swap")
            if top_lossy:
                warnings.append("range_top_idx_truncated")
            if base_lossy:
                warnings.append("range_base_idx_truncated")
            if warnings:
                row["_warning"] = warnings[0] if len(warnings) == 1 else warnings
            _carry_extras(u, _KNOWN_UNIT_KEYS, row)
            out.append(row)
        return out

    def norm_samples(items):
        out = []
        for s_item in _dict_rows(items):
            bed_idx, bed_lossy = fi(s_item.get("bed_idx"))
            row = {
                "bed_idx": bed_idx,
                "fossil_marker": s(s_item.get("fossil_marker")),
                "ref": s(s_item.get("ref")),
            }
            # Sprint B (REVIEW-2026-09-04): lossy float truncation must be
            # visible here too (row-level ``_warning`` per the fi() contract).
            if bed_lossy:
                row["_warning"] = "bed_idx_truncated"
            _carry_extras(s_item, _KNOWN_SAMPLE_KEYS, row)
            out.append(row)
        return out

    def norm_legend(items):
        out = []
        warning = None
        # Fix B-5: if items is a string (not a list), don't iterate over chars.
        # Issue-1 fix: add _warning flag instead of silently discarding.
        if isinstance(items, str):
            warning = "legend_input_is_string"
            items = []
        # REVIEW-2026-09-20 #3: a dict-shaped legend - which is what the model
        # emits when it keys the entries by marker ("legend": {"ammonite":
        # {"meaning": ...}}) - used to fail ``isinstance(x, dict)`` for every
        # value and vanish without a trace. Same tolerant path as norm_cross.
        for x in _dict_rows(items):
            # fossil_legend uses marker+meaning; lithology_legend uses
            # pattern+meaning. Carry both so neither legend's primary
            # column is silently dropped into _extras (which the exporter
            # never reads) and rendered blank.
            row = {
                "marker": s(x.get("marker")),
                "pattern": s(x.get("pattern")),
                "meaning": s(x.get("meaning")),
            }
            _carry_extras(x, _KNOWN_LEGEND_KEYS, row)
            out.append(row)
        return out, warning

    def norm_cross(items):
        out = []
        for x in _dict_rows(items):
            from_bed_idx, from_lossy = fi(x.get("from_bed_idx"))
            to_bed_idx, to_lossy = fi(x.get("to_bed_idx"))
            row = {
                "from_section": s(x.get("from_section")),
                "from_bed_idx": from_bed_idx,
                "to_section": s(x.get("to_section")),
                "to_bed_idx": to_bed_idx,
            }
            # Sprint B (REVIEW-2026-09-04): lossy float truncation warnings.
            trunc = [name for name, lossy in (
                ("from_bed_idx_truncated", from_lossy),
                ("to_bed_idx_truncated", to_lossy),
            ) if lossy]
            if trunc:
                row["_warning"] = trunc[0] if len(trunc) == 1 else trunc
            _carry_extras(x, _KNOWN_CROSS_KEYS, row)
            out.append(row)
        return out

    sections = []
    # AUDIT-2026-09-27 P1: a bare-string section row used to be skipped with
    # no warning at all. `sections` is a name-bearing table, so the string is
    # the identifier - coerce it and say so, exactly as range_chart does.
    _columnar_warnings: list[str] = []
    for sec in _dict_rows(parsed.get("sections"), "sections", _columnar_warnings):
        # FIX-2026-10-01: an ABSENT confidence_by_section used to be written as
        # 0.0, byte-identical to a model that explicitly reported 0.0 -- so
        # "the model never said" and "the model said zero" were
        # indistinguishable in every export, and the operator would judge the
        # whole batch as a failed recognition. This module is careful about
        # that distinction elsewhere (the root confidence has a None-aware
        # fallback; row-level _normalize_confidence returns None), so this one
        # coercion was the odd one out. Absent / null / unparsable now stays
        # None ("not stated"); a real number is still clamped to 0..1.
        _raw_cbs = sec.get("confidence_by_section")
        conf_v: float | None
        if _raw_cbs is None or _raw_cbs == "":
            conf_v = None
        else:
            try:
                conf_v = float(_raw_cbs)
            except (TypeError, ValueError):
                conf_v = None
        row = {
            "id": s(sec.get("id")),
            "group": s(sec.get("group")),
            "lithology_blocks": norm_blocks(sec.get("lithology_blocks")),
            "age_units": norm_units(sec.get("age_units")),
            "samples": norm_samples(sec.get("samples")),
            "coordinates_text": s(sec.get("coordinates_text")),
            "thickness_m": s(sec.get("thickness_m")),
            "confidence_by_section": (_confidence_clamped(conf_v)
                                      if conf_v is not None else None),
        }
        _carry_extras(sec, _KNOWN_COLUMNAR_SECTION_KEYS, row)
        sections.append(row)

    try:
        # Models sometimes emit `confidence` at the root instead of the
        # documented `overall_confidence`; fall back so the value isn't
        # silently zeroed (which would also distort aggregate's mean).
        #
        # REVIEW-2026-09-10: `dict.get(key, default)` only falls back when the
        # key is ABSENT, so an explicit ``overall_confidence: null`` (a common
        # model emission) still produced 0.0 and discarded a perfectly good
        # sibling ``confidence``. The browser's mirror uses `!= null`
        # (js/minimax.js) and returned 0.8 for the same payload - the server
        # and the browser disagreed on the same extraction.
        _overall_raw = parsed.get("overall_confidence")
        if _overall_raw is None:
            _overall_raw = parsed.get("confidence", 0.0)
        overall = float(_overall_raw)
    except (TypeError, ValueError):
        overall = 0.0
    overall = _confidence_clamped(overall)

    # Issue-1 fix: unpack tuple return from norm_legend (returns (list, warning))
    fossil_legend, fossil_legend_warn = norm_legend(parsed.get("fossil_legend"))
    lithology_legend, lithology_legend_warn = norm_legend(parsed.get("lithology_legend"))
    # Collect warnings to surface at root level
    legend_warnings = [w for w in (fossil_legend_warn, lithology_legend_warn) if w]
    for w in _columnar_warnings:            # AUDIT-2026-09-27 P1
        if w not in legend_warnings:
            legend_warnings.append(w)

    out: dict[str, Any] = {
        "sections": sections,
        "fossil_legend": fossil_legend,
        "lithology_legend": lithology_legend,
        "cross_beds": norm_cross(parsed.get("cross_beds")),
        "confidence": overall,
    }
    if legend_warnings:
        out["_warnings"] = legend_warnings
    root_extras = {k: v for k, v in parsed.items() if k not in _KNOWN_COLUMNAR_ROOT_KEYS}
    if root_extras:
        out["_extras"] = root_extras
    return out


