"""Mode normalizer: range_chart.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    VALID_ENDPOINT_KINDS,
    VALID_OCCURRENCE_MODES,
    _CONTRACT_SOURCE_KEYS,
    _IRON_RULE_ZONE_RE,
    _KNOWN_BIOZONE_KEYS,
    _KNOWN_RANGE_CHART_KEYS,
    _KNOWN_SECTION_KEYS,
    _KNOWN_SPECIES_KEYS,
    _add_row_flag,
    _carry_extras,
    _classify_array_item,
    _confidence_clamped,
    _fold_axis_calibration,
    _guard_wrapped_header_rows,
    _merge_other_fossils,
    _row_from_string,
    _single_record_from_dict,
    _stringify_scalar,
    attach_row_contract,
    axis_domains_from,
)
from typing import Any
import re

def _normalize_section_into(sec: dict[str, Any],
                             target: list[dict[str, Any]]) -> None:
    """Build a section row from a raw dict and append it to ``target``.

    MEDIUM fix: the _array_root handler previously appended the raw dict
    without coercion, so string-where-list-was-expected fields and missing
    keys slipped through unchanged. Routing every item through this helper
    (and the sibling species/biozone helpers) guarantees a uniform shape.
    """
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    formations = sec.get("formations")
    if isinstance(formations, list):
        formations_out = [str(x).strip() for x in formations
                          if isinstance(x, str) and str(x).strip()]
    elif isinstance(formations, str) and formations.strip():
        formations_out = [formations.strip()]
    else:
        formations_out = []
    row = {
        "name": s(sec.get("name")),
        "age_range": s(sec.get("age_range")),
        "formations": formations_out,
        "formation_thickness_m": s(sec.get("formation_thickness_m")),
        "coordinates": s(sec.get("coordinates")),
    }
    _carry_extras(sec, _KNOWN_SECTION_KEYS, row)
    target.append(row)


def _normalize_occurrence_mode(sp: dict[str, Any]) -> str:
    """Return a scientifically explicit occurrence classification.

    Missing or invalid modern enum values remain ``unknown`` rather than being
    promoted to the positive assertion ``in_situ``. The legacy
    ``reworked: bool`` field is still mapped for backward compatibility because
    both boolean values carry an explicit assertion from older saved results.
    """
    raw = sp.get("occurrence_mode")
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in VALID_OCCURRENCE_MODES:
            return normalized
    reworked = sp.get("reworked")
    if isinstance(reworked, bool):
        return "reworked" if reworked else "in_situ"
    return "unknown"


def _normalize_endpoint_kind(value: Any) -> str:
    """Normalize an endpoint classification without inventing observation."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_ENDPOINT_KINDS:
            return normalized
    return "unknown"


def _normalize_optional_int(value: Any) -> int | None:
    """Return an exact integer index, or ``None`` when it is not integral."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else None
    return None


def _normalize_confidence(value: Any) -> float | None:
    """Normalize an optional per-row confidence to the closed interval [0, 1].

    AUDIT-2026-09-30: NaN returns None, not 0.0 -- the ROW variant of the
    contract. js/minimax.js rcaOptionalConfidence (the mirror of this
    function, used for ``species_ranges``/``confidence``) ends with
    ``if (n === null || Number.isNaN(n)) return null``, so an unstateable
    per-row confidence makes the browser OMIT the key while Python used to
    write a confident 1.0. Same root cause as _confidence_clamped, different
    fallback because this one is nullable.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:  # NaN
        return None
    return _confidence_clamped(parsed)


def _normalize_species_into(sp: dict[str, Any],
                            target: list[dict[str, Any]],
                            *,
                            axes: dict[str, dict[str, Any]] | None = None,
                            ) -> None:
    """Build a species_ranges row from a raw dict and append it."""
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    # REVIEW-2026-09-10: species rows carried their bed indices through
    # _normalize_optional_int with no order check, unlike the columnar path
    # (norm_blocks) which swaps an inverted pair and flags it. An inverted
    # (range_top_idx < range_base_idx) pair was therefore exported as a valid
    # range and nothing downstream caught it - quality.py's FAD/LAD check
    # reads the *string* fields, not the index fields.
    _base_idx = _normalize_optional_int(sp.get("range_base_idx"))
    _top_idx = _normalize_optional_int(sp.get("range_top_idx"))
    _idx_warnings: list[str] = []
    if _top_idx is not None and _base_idx is not None and _top_idx < _base_idx:
        _top_idx, _base_idx = _base_idx, _top_idx
        _idx_warnings.append("index_order_swap")

    # AUDIT-2026-10-15: the indices above are ordered, but the TEXT endpoints
    # were taken verbatim from the model, so the two could disagree — and the
    # text is what the export writes.
    #
    # Measured on Diaz & Velez 2018 Fig. 5 (33 taxa, real extraction):
    # 21 of 28 rows carried
    #     range_base="A5 (606 m)"  range_base_idx=1
    #     range_top ="A1 (607 m)"  range_top_idx =5
    # A1 is the DEEPEST sample (607 m), so it is the OLD end and belongs in
    # range_base; the numeric pair was right and the text pair was transposed.
    # The workbook therefore shipped the older end labelled as the younger one,
    # and quality.py's FAD/LAD check read the text, so it raised 21
    # `range_top_lt_base` warnings while `to_xlsx` still wrote the wrong values
    # — `validate_export_invariants` reported ok=True because
    # `range_base_le_range_top` parses endpoints through the strict
    # `parse_bed`, which returns None for "A1 (607 m)" and so never reached
    # its own comparison.
    #
    # Repair when the two representations CONTRADICT each other: the labels
    # carry their own ordinal (A1 -> 1, A5 -> 5), and when that order disagrees
    # with the index order the text is the transposed half. Swap text + bed
    # labels together and keep the indices, so all three agree afterwards.
    # When the labels cannot be read, or the two orders merely agree, nothing
    # is touched — a wrong "repair" would be worse than the original defect.
    _range_top = s(sp.get("range_top"))
    _range_base = s(sp.get("range_base"))
    _top_bed = s(sp.get("range_top_bed", ""))
    _base_bed = s(sp.get("range_base_bed", ""))
    if _base_idx is not None and _top_idx is not None and _base_idx != _top_idx:
        from ..bed_parser import label_ordinal, ordinal_kind
        _ord_base = label_ordinal(_range_base or _base_bed)
        _ord_top = label_ordinal(_range_top or _top_bed)
        _kinds = {ordinal_kind(_range_base or _base_bed),
                  ordinal_kind(_range_top or _top_bed)}
        if (_ord_base is not None and _ord_top is not None
                and _ord_base != _ord_top
                and len(_kinds) == 1 and None not in _kinds
                # opposite signs: labels say one order, indices the other
                and (_ord_base > _ord_top) != (_base_idx > _top_idx)):
            _range_top, _range_base = _range_base, _range_top
            _top_bed, _base_bed = _base_bed, _top_bed
            _idx_warnings.append("endpoint_label_index_swap")

    row = {
        "species": s(sp.get("species")),
        "section": s(sp.get("section")),
        "range_top": _range_top,
        "range_base": _range_base,
        "biozone": s(sp.get("biozone")),
        "author": s(sp.get("author", "")),
        "year": s(sp.get("year", "")),
        # HIGH fix: author_year is the combined string the prompt requests.
        "author_year": s(sp.get("author_year") or ""),
        # P0-3 (REVIEW-2026-07-25): prompt.py asks for these four scientific
        # metadata fields per species row. They were previously listed in
        # _KNOWN_SPECIES_KEYS so _carry_extras would skip them — silently
        # dropping observed/projected/truncated endpoint classification,
        # reworked flag, and exact bed labels. Promote them to first-class
        # row keys and ALSO drop them from _KNOWN_SPECIES_KEYS so _carry_extras
        # does not double-write them.
        "range_top_bed": _top_bed,
        "range_base_bed": _base_bed,
        "range_top_idx": _top_idx,
        "range_base_idx": _base_idx,
        "endpoint_kind": _normalize_endpoint_kind(sp.get("endpoint_kind")),
        # Missing modern classifications remain unknown. Explicit legacy
        # reworked booleans retain their historical compatibility mapping.
        "occurrence_mode": _normalize_occurrence_mode(sp),
        "confidence": _normalize_confidence(sp.get("confidence")),
        # H-8 fix: prompt explicitly asks for a note field for degraded
        # determinations; it was never written to the output row.
        "note": s(sp.get("note", "")),
    }
    # BORROW-2026-09-20 (A+B): geometry + coverage contract, additively.
    consumed = attach_row_contract(sp, row, axes=axes)
    _carry_extras(sp, _KNOWN_SPECIES_KEYS + _CONTRACT_SOURCE_KEYS + consumed, row)
    # P0-4: defensive — if species name looks like a zone, flag & strip.
    sp_name = row["species"]
    if sp_name and _IRON_RULE_ZONE_RE.search(sp_name):
        row["note"] = (row["note"] + " [zone-mislabel-warning]").strip()
    if _idx_warnings:
        # Same single-string / list convention the block rows use, so
        # rcaWarningFlags / _warning_flags consumers read it uniformly.
        # FIX-2026-09-22 (audit item 2 family): additive, because
        # attach_row_contract above may already have flagged this very row
        # (response_kind_conflict) and a plain assignment destroyed it.
        for _w in _idx_warnings:
            _add_row_flag(row, _w)
    target.append(row)


def _normalize_biozone_into(bz: dict[str, Any],
                            target: list[dict[str, Any]]) -> None:
    """Build a biozones row from a raw dict and append it.

    P0-4: enforces zone_type field. Infers zone_type from name keywords
    when not explicitly provided: assemblage, acme, lineage, interval, zonule,
    subzone, oppel, range zone.
    """
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    name = s(bz.get("name")).strip()
    # P0-4: infer zone_type from name keywords when not explicitly provided.
    inferred_zt = "biozone"
    nl = name.lower()
    if "assemblage" in nl or "ass." in nl:
        inferred_zt = "assemblage_zone"
    elif "acme" in nl:
        inferred_zt = "acme_zone"
    elif "lineage" in nl:
        inferred_zt = "lineage_zone"
    elif "interval" in nl:
        inferred_zt = "interval_zone"
    # AUDIT-2026-10-02: the only two boundary-guarded branches of this ladder
    # (the other seven are plain substring tests and cannot diverge). The guard
    # is written as explicit ASCII lookarounds because Python's `\b` is
    # Unicode-aware on str patterns and ECMAScript's is not: a CJK / Greek /
    # Cyrillic letter touching the keyword is a word character here and not
    # there, so js/minimax.js#rcaNormalizeBiozoneInto inferred "zonule" for
    # "图zonule" and this side fell through to the "biozone" default -- the
    # same figure taking a different zone_type, and so a different biozone
    # row, on the two endpoints. Measured on the `range_chart` parity group.
    #
    # Direction, as with the explicit-age guard: a CJK glyph is not a
    # continuation of an ASCII identifier, so ASCII is the right notion of
    # "adjacent identifier character" here. "zozonule" / "zonules" are still
    # rejected on both engines, and a space or hyphen is still a boundary both
    # engines agree on.
    elif re.search(r"(?<![A-Za-z0-9_])zonule(?![A-Za-z0-9_])", nl):
        inferred_zt = "zonule"
    elif re.search(r"(?<![A-Za-z0-9_])subzone(?![A-Za-z0-9_])", nl):
        inferred_zt = "subzone"
    elif "oppel" in nl:
        inferred_zt = "oppel_zone"
    elif "range zone" in nl or "taxon-range" in nl:
        inferred_zt = "range_zone"
    row = {
        "name": name,
        "section": s(bz.get("section", "")),
        "age": s(bz.get("age", "")),
        "thickness_m": s(bz.get("thickness_m", "")),
        "zone_type": s(bz.get("zone_type") or inferred_zt),
    }
    _carry_extras(bz, _KNOWN_BIOZONE_KEYS, row)
    target.append(row)


def normalize_result(parsed):
    """Coerce the parsed JSON into the strict result shape.

    H8: extra top-level / row-level keys the model emits are not silently
    discarded - they are attached under ``_extras`` so the operator sees
    what was extracted. This avoids losing data the caller assumes is
    captured by the schema.

    H3-fix: when safe_json_loads wraps a top-level array as
    ``{"_array_root": [...]}``, we unwrap it and distribute items to the
    appropriate keys (sections, species_ranges, biozones, other_fossils).
    """
    # MEDIUM fix: normalize_result crashes on non-dict input. Guard so callers don't get AttributeError.
    if not isinstance(parsed, dict):
        return {
            "sections": [],
            "species_ranges": [],
            "biozones": [],
            "other_fossils": [],
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }
    def s(v):
        # Container-safe (see _stringify_scalar): a dict/list in a
        # scalar field degrades to "" rather than leaking a Python
        # repr into a scientific field.
        return _stringify_scalar(v)

    out = {
        "sections": [],
        "species_ranges": [],
        "biozones": [],
        "other_fossils": [],
        "confidence": 0.0,
    }
    root_warnings = []

    # BORROW-2026-09-20 (B): one figure-level axis calibration per result. The
    # rows' *pos_0_999 reads are converted against it; absent it they stay raw
    # evidence. Read once here so every normalizer shares the same domains.
    axes = axis_domains_from(parsed)

    # MEDIUM fix (truncated rescue): if the JSON parser rescued a partial
    # / inner object that does not match any of the documented range-chart
    # root keys, surface a warning so the operator is not silently given
    # an empty ok=True result.
    RANGE_CHART_ROOTS = {"sections", "species_ranges", "biozones",
                         "other_fossils", "confidence"}
    if (isinstance(parsed, dict) and parsed
            and not RANGE_CHART_ROOTS.intersection(parsed.keys())
            and "_array_root" not in parsed):
        root_warnings.append("truncated_or_unrecognized_payload")

    # H3-fix: unwrap _array_root wrapper and distribute items to known keys.
    if "_array_root" in parsed and isinstance(parsed["_array_root"], list):
        for item in parsed["_array_root"]:
            if not isinstance(item, dict):
                # Non-dict items (e.g. bare strings) go to other_fossils.
                if isinstance(item, str) and item.strip():
                    out["other_fossils"].append(item.strip())
                continue
            key = _classify_array_item(item)
            if key is None:
                # Unclassifiable dicts: attach as top-level extras under
                # a generated key so nothing is silently dropped.
                out.setdefault("_unclassified", []).append(item)
            else:
                # MEDIUM fix: normalize the unwrapped item using the same
                # shape as the regular list, so string-instead-of-list
                # fields (formations, etc.) and missing keys are handled
                # uniformly.
                if key == "sections":
                    _normalize_section_into(item, out["sections"])
                elif key == "species_ranges":
                    _normalize_species_into(item, out["species_ranges"], axes=axes)
                elif key == "biozones":
                    _normalize_biozone_into(item, out["biozones"])
                else:
                    out.setdefault(key, []).append(item)

    # MEDIUM fix (dict-shaped arrays): if a named array is actually a dict
    # (single-object model emission), iterate the values instead of string
    # keys so the record is not silently lost. The wrapper key is preserved
    # under _extras.wrapper_key for traceability.
    def _coerce_list_or_dict(raw, kind):
        if isinstance(raw, dict):
            # FIX-2026-10-01: flag the shape change here too. This is the only
            # mode that does not route through _iter_rows, so it was the only
            # one that silently repaired a dict-shaped array: the rows came
            # back (name recovered from the wrapper key) and _warnings stayed
            # None, while abundance / zonation / columnar all reported
            # `dict_shaped_array` for the identical shape. The operator could
            # not tell the result had been shape-repaired -- the only trace was
            # _extras.wrapper_key, which CSV/XLSX export does not read.
            if "dict_shaped_array" not in root_warnings:
                root_warnings.append("dict_shaped_array")
            # FIX-2026-10-01: a single record returned where an array was asked
            # for used to empty the whole table silently (ok=True, confidence
            # preserved, no warning, one billed call). See
            # _single_record_from_dict.
            single = _single_record_from_dict(raw, kind)
            if single is not None:
                inner2 = single
                inner2.pop("_extras", None)
                if kind == "sections":
                    _normalize_section_into(inner2, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(inner2, out["species_ranges"], axes=axes)
                elif kind == "biozones":
                    _normalize_biozone_into(inner2, out["biozones"])
                return
            for wrapper_key, inner in raw.items():
                if not isinstance(inner, dict):
                    continue
                inner2 = dict(inner)
                # Fall back: when the inner record has NO primary identifier
                # key at all (no ``name`` for sections/biozones, no
                # ``species`` for species_ranges), use the wrapper key as the
                # primary identifier. This recovers the most common
                # dict-shaped payload (``{'Pingdingshan': {...}}``) without
                # losing the record.
                #
                # REVIEW-2026-09-10: the test must be "key absent", not
                # "value falsy". A record that carries ``species: ""`` is the
                # model following the prompt's instruction to leave an
                # unreadable name empty - overwriting it with the wrapper key
                # fabricated a taxon name (e.g. ``{"unclear": {"species": ""}}``
                # became species "unclear", ``{"0": {...}}`` became "0").
                if kind in ("sections", "biozones") and "name" not in inner2:
                    if not str(wrapper_key).strip().isdigit():
                        inner2["name"] = wrapper_key
                    else:
                        # FIX-2026-10-01: a bare index is not a section or
                        # biozone name. Injecting "0" fabricates an identifier
                        # that exports as if it were printed on the plate.
                        root_warnings.append("identifier_not_recovered_from_index")
                elif kind == "species_ranges" and "species" not in inner2:
                    if not str(wrapper_key).strip().isdigit():
                        inner2["species"] = wrapper_key
                    else:
                        root_warnings.append("identifier_not_recovered_from_index")
                # Drop any pre-existing _extras before re-running the
                # normalizer so we don't end up with a nested ``_extras``
                # field. The normalizer will rebuild it cleanly with
                # ``wrapper_key`` preserved via the _carry_extras merge.
                inner2.pop("_extras", None)
                if kind == "sections":
                    _normalize_section_into(inner2, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(inner2, out["species_ranges"], axes=axes)
                elif kind == "biozones":
                    _normalize_biozone_into(inner2, out["biozones"])
                # After normalization, attach wrapper_key to the LAST
                # appended row. We can't put it on the source dict (it's a
                # 'known' marker the normalizer would treat as unknown-key
                # extras and re-attach), so we add it to the row directly.
                if out[kind]:
                    row = out[kind][-1]
                    extras = row.get("_extras")
                    if not isinstance(extras, dict):
                        extras = {}
                        row["_extras"] = extras
                    extras["wrapper_key"] = wrapper_key
        elif isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    # REVIEW-2026-09-10: a bare string entry
                    # (``{"biozones": ["Clarkina postbitteri Zone"]}``) used
                    # to be skipped in silence (both engines). Coerce it to a
                    # one-field row so the record survives, and flag it.
                    if isinstance(item, str):
                        row = _row_from_string(item, kind)
                        if row is not None:
                            if kind == "sections":
                                _normalize_section_into(row, out["sections"])
                            elif kind == "species_ranges":
                                _normalize_species_into(row, out["species_ranges"], axes=axes)
                            elif kind == "biozones":
                                _normalize_biozone_into(row, out["biozones"])
                            if "string_row_coerced" not in root_warnings:
                                root_warnings.append("string_row_coerced")
                    continue
                if kind == "sections":
                    _normalize_section_into(item, out["sections"])
                elif kind == "species_ranges":
                    _normalize_species_into(item, out["species_ranges"], axes=axes)
                elif kind == "biozones":
                    _normalize_biozone_into(item, out["biozones"])

    _coerce_list_or_dict(parsed.get("sections"), "sections")
    _coerce_list_or_dict(parsed.get("species_ranges"), "species_ranges")
    _coerce_list_or_dict(parsed.get("biozones"), "biozones")

    # MEDIUM fix (iron rule): post-normalize pass that flags any species
    # whose name reads like a zone label. We do not MOVE the row (which
    # would be silently destructive) - we flag it so the operator sees
    # the slip-through and decides.
    for sp in out["species_ranges"]:
        name = (sp.get("species") or "").strip()
        if name and _IRON_RULE_ZONE_RE.search(name):
            # FIX-2026-09-22 (audit item 2 family): additive — the row can
            # already carry index_order_swap / response_kind_conflict and a
            # plain assignment wiped both on the server.
            _add_row_flag(sp, "iron_rule_zone_label")
            if "iron_rule_zone_label" not in root_warnings:
                root_warnings.append("iron_rule_zone_label")

    # AUDIT-2026-10-15 (D1): repair the rows a two-line column header was
    # split into, and drop the ones no evidence can place. See
    # _guard_wrapped_header_rows for the measured figure.
    dropped_fragments = _guard_wrapped_header_rows(
        out["species_ranges"], root_warnings)

    # AUDIT-2026-10-15 (D4): one biozone stamped on every row.
    #
    # Measured on Diaz & Velez 2018 Fig. 5 (`_figtest/diaz_fig5_result.json`):
    # all 28 rows carry the identical
    #     biozone = "Trochammina sp. Zone, Marsonella oxycona Subzone"
    # including the radiolarians drawn OUTSIDE the pink foraminiferal block,
    # while the figure's zonation column is a band that covers only part of the
    # section. Per-row biozone is copied verbatim from the model
    # (_normalize_species_into), so nothing here can correct it: there is no
    # positional evidence in the payload to say which zone a given row sits in,
    # and guessing one would stamp a different fabrication. Two root-level
    # warnings are raised instead, and nothing is rewritten.
    #
    # Root-level, not per-row, on purpose: aggregate.py:1168 records an earlier
    # attempt at a per-row flag for a whole-result pattern that turned four
    # contract tests red, and _warnings is the established vocabulary for a
    # result-level shape problem (dict_shaped_array / string_row_coerced /
    # iron_rule_zone_label all live there). aggregate.py:1454 carries it
    # through a multi-run merge, so the operator still sees it.
    _zones = [s(_bz.get("name")).strip() for _bz in out["biozones"]]
    _zones = [z for z in _zones if z]
    _row_zones = [s(sp.get("biozone")).strip() for sp in out["species_ranges"]]
    _used = [z for z in _row_zones if z]
    if len(_used) >= 3 and len(set(_used)) == 1:
        if "biozone_uniform_across_figure" not in root_warnings:
            root_warnings.append("biozone_uniform_across_figure")
        # The sharper form: the constant is not one of the declared zones at
        # all, it is the figure's ENTIRE zone list joined into one cell -- so
        # the stamp asserts every row belongs to every zone on the plate.
        if (len(_zones) >= 2
                and _used[0] == ", ".join(_zones)
                and "biozone_spans_whole_figure" not in root_warnings):
            root_warnings.append("biozone_spans_whole_figure")

    of = parsed.get("other_fossils") or []
    # Fix B-3: handle case where model returns a string instead of a list.
    # REVIEW-2026-09-10: dict-shaped entries used to be dropped here while
    # the browser (js/minimax.js M1 fix) already lifted their label/species/
    # taxon/name - so a labelled fossil record appeared in the browser and
    # vanished from the server, CSV and XLSX. Both sides now accept both
    # shapes (see _other_fossils_from).
    # REVIEW-2026-09-20 #1: merge, do not replace - the ``_array_root``
    # unwrap above may already have salvaged bare-string labels into
    # out["other_fossils"], and a plain assignment dropped them silently.
    out["other_fossils"] = _merge_other_fossils(out["other_fossils"], of)
    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)
    # LOW fix: drop _array_root (and its _note companion) from top-level
    # extras - the per-item rows are already distributed, so the raw
    # payload would be a duplicate.
    extras_src = {k: v for k, v in parsed.items()
                  if k not in _KNOWN_RANGE_CHART_KEYS}
    extras_src.pop("_array_root", None)
    extras_src.pop("_note", None)
    # AUDIT-2026-10-15 (D1): a wrapped-header fragment that no evidence could
    # place is removed from species_ranges, so its text is preserved here
    # rather than lost -- the same "warn, and keep the raw text" contract
    # _fold_axis_calibration below follows for an unusable calibration block.
    if dropped_fragments:
        extras_src["dropped_species_fragments"] = dropped_fragments
    # BORROW-2026-09-20 (B): hoist the optional calibration to a root key so
    # the geometry sidecars' numbers are re-derivable downstream. Absent when
    # the model emitted none — legacy results keep their exact old key set.
    # FIX-2026-09-22 (audit item 7): an unusable block now warns instead of
    # disappearing, and its raw text is kept under ``_extras``.
    _fold_axis_calibration(parsed, axes, out, extras_src, root_warnings)
    if extras_src:
        out["_extras"] = extras_src
    if root_warnings:
        out["_warnings"] = root_warnings
    return out


