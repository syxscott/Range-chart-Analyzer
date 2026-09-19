"""PBDB mapping for range chart data."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Optional

# C-4 (REVIEW-2026-07-25): wire the ICS 2024 table into the PBDB export path
# so stage-name -> numeric Ma conversion happens here (previously ICS was
# only imported by quality.py, so PBDB emitted empty max_ma/min_ma for text
# age ranges and duplicated the biozone into both early/late_interval).
# Degrade gracefully to the previous behaviour when ICS is unavailable.
try:
    from .ics import (
        ICS_2024,
        ics_age_range_bounds,
        ics_parse_age_range,
        ics_resolve_age_bound,
    )
    _HAS_ICS = bool(ICS_2024)
except Exception:  # pragma: no cover - import fallback
    _HAS_ICS = False
    ICS_2024 = {}
    ics_age_range_bounds = None
    ics_parse_age_range = None
    ics_resolve_age_bound = None


def _resolve_pbdb_bounds(row):
    """Resolve a species row's FAD/LAD to (early_stage, late_stage, early_ma,
    late_ma).

    ``early`` = older bound (range_base / FAD); ``late`` = younger bound
    (range_top / LAD). Falls back to (None, None, None, None) when ICS is
    unavailable or the bounds can't be resolved so the caller can use its
    section-level numeric fallback.
    """
    if not _HAS_ICS or not isinstance(row, dict):
        return None, None, None, None
    base = row.get("range_base")
    top = row.get("range_top")
    # REVIEW-2026-07-31: interval literals resolve to the correct end —
    # FAD/base is the OLDER (larger Ma) end, LAD/top the YOUNGER end.
    e_stage, e_ma = ics_resolve_age_bound(base, prefer="older") if ics_resolve_age_bound else (None, None)
    l_stage, l_ma = ics_resolve_age_bound(top, prefer="younger") if ics_resolve_age_bound else (None, None)
    return e_stage, l_stage, e_ma, l_ma


def _stage_endpoint_names(stages):
    """Return ``(older_name, younger_name)`` for *stages* — by their ICS bounds.

    REVIEW-2026-09-20: both PBDB builders took ``stages[0]`` / ``stages[-1]``,
    i.e. the TEXT order of the age_range label. Labels do not have to be
    written bottom-to-top ("Changhsingian - Wuchiapingian" occurs on real
    charts, and the module's own numeric path is deliberately
    order-independent: max_ma/min_ma use max()/min()). Pairing the first word
    with early_interval then exported the YOUNGER name beside the OLDER max_ma
    — a self-contradicting interval row that PBDB validators reject.
    """
    known = [s for s in stages if s in ICS_2024]
    if not known:
        return "", ""
    # Geological convention inside the table: base_ma is the older (larger)
    # number and top_ma the younger (smaller) one.
    older = max(known, key=lambda s: ICS_2024[s].get("base_ma", 0) or 0)
    younger = min(known, key=lambda s: ICS_2024[s].get("top_ma", 0) or 0)
    return older, younger


def _parse_coords(text):
    if not text or not isinstance(text, str):
        return None, None
    text = text.strip()
    if re.search(r"not\s*visible|unknown|missing", text, re.IGNORECASE):
        return None, None
    m = re.search(r"([+-]?\d+\.?\d*)\s*([NSns]),?\s*([+-]?\d+\.?\d*)\s*([EWew])", text)
    if m:
        lat_val = float(m.group(1))
        lon_val = float(m.group(3))
        # REVIEW-2026-07-31: hemisphere comes from the matched letter
        # group, not a whole-text scan ("31N, 117E (south bank)" used to
        # flip the latitude to -31).
        if m.group(2).upper() == "S": lat_val = -abs(lat_val)
        if m.group(4).upper() == "W": lon_val = -abs(lon_val)
        return lat_val, lon_val
    return None, None


_AGE_RANGE_WITH_UNIT = re.compile(
    r"(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:[-–—]|\bto\b)\s*"
    r"([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)
_AGE_VALUE_WITH_UNIT = re.compile(
    r"(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)


def _parse_age_range_ma(text):
    """Return ``(min_ma, max_ma)`` only for explicit absolute-age ranges.

    Bed/sample identifiers and other bare numbers are rejected. Both common
    forms, ``"260-250 Ma"`` and ``"260 Ma to 250 Ma"``, are supported.
    """
    if not text:
        return None, None
    value = str(text).strip()
    match = _AGE_RANGE_WITH_UNIT.search(value)
    if match:
        ages = [float(match.group(1)), float(match.group(2))]
    else:
        ages = [float(item) for item in _AGE_VALUE_WITH_UNIT.findall(value)]
        if len(ages) < 2:
            return None, None
        ages = ages[:2]
    return min(ages), max(ages)


def _validated_pbdb_ages(
    older_ma: Any,
    younger_ma: Any,
    fallback_max_ma: Any,
    fallback_min_ma: Any,
) -> tuple[Any, Any]:
    """Return PBDB ``(max_ma, min_ma)`` without mixing incompatible bounds."""
    if older_ma is not None and younger_ma is not None:
        if older_ma >= younger_ma:
            return older_ma, younger_ma
        return "", ""
    if fallback_max_ma not in (None, "") and fallback_min_ma not in (None, ""):
        try:
            max_ma = float(fallback_max_ma)
            min_ma = float(fallback_min_ma)
        except (TypeError, ValueError):
            return "", ""
        if max_ma >= min_ma:
            return max_ma, min_ma
    return "", ""


def to_pbdb_occurrences(result):
    occurrences = []
    species_ranges = result.get("species_ranges", []) or []
    sections = result.get("sections", []) or []
    section_info = {}
    for sec in sections:
        if isinstance(sec, dict):
            name = sec.get("name", "")
            section_info[name] = {
                "collection_name": name,
                "formation": "; ".join(sec.get("formations", []) or []),
                "coordinates": sec.get("coordinates", ""),
                # M-1 fix: persist the section's age_range so we can
                # populate max_ma (older boundary) and min_ma (younger
                # boundary) for each species row from this section.
                # Before the fix these were hard-coded empty.
                "age_range": sec.get("age_range", "") or "",
                # Pre-parse Ma at write-time so PBDB temporal queries
                # actually work.
                "min_ma": "",  # younger
                "max_ma": "",  # older
                # REVIEW-2026-07-31: interval-name fallbacks resolved from
                # the section age_range (stage/series/period labels) used
                # for early_interval / late_interval when per-row bounds
                # don't resolve.
                "older_name": "",
                "younger_name": "",
            }
            age_range = section_info[name]["age_range"]
            min_ma, max_ma = _parse_age_range_ma(age_range)
            if min_ma is None and _HAS_ICS and ics_age_range_bounds:
                # C-3 fix (REVIEW-2026-07-31): stage/series-only age ranges
                # ("Late Permian (Wuchiapingian - Changhsingian)") never
                # resolved to numeric Ma before — PBDB temporal queries
                # stayed empty for the most common chart labels. Resolve
                # them through the ICS table now.
                older_ma, younger_ma = ics_age_range_bounds(age_range)
                if older_ma is not None:
                    min_ma, max_ma = younger_ma, older_ma
            if min_ma is not None:
                section_info[name]["min_ma"] = str(min_ma)
            if max_ma is not None:
                section_info[name]["max_ma"] = str(max_ma)
            if _HAS_ICS and ics_parse_age_range:
                _stages = ics_parse_age_range(age_range)
                _older_name, _younger_name = (
                    _stage_endpoint_names(_stages) if _stages else ("", ""))
                if _older_name and _younger_name:
                    section_info[name]["older_name"] = _older_name
                    section_info[name]["younger_name"] = _younger_name
                elif ics_resolve_age_bound:
                    _n_old, _ = ics_resolve_age_bound(age_range, prefer="older")
                    if _n_old:
                        section_info[name]["older_name"] = _n_old
                        section_info[name]["younger_name"] = _n_old
    for idx, row in enumerate(species_ranges):
        if not isinstance(row, dict): continue
        species = row.get("species", "")
        if not species: continue
        section = row.get("section", "")
        sec_data = section_info.get(section, {})
        biozone = row.get("biozone", "")
        lat, lon = None, None
        coords = sec_data.get("coordinates", "")
        if coords: lat, lon = _parse_coords(coords)
        # REVIEW-2026-09-20: ``dict.get(key, default)`` only uses the default
        # when the key is ABSENT, and the normalizer always writes
        # ``author_year`` (empty string when nothing was read) — so the
        # ``row.get("authority", "")`` second argument was dead code and an
        # empty author_year never fell through to the authority alias the
        # older payloads / hand-edited rows carry. ``or``-chaining is what was
        # meant here (same fix in darwin_core.py).
        author_year = (
            row.get("author_year") or row.get("authority")
            or row.get("author") or ""
        )
        # C-2 / C-4 fix (REVIEW-2026-07-25): resolve the per-species FAD/LAD
        # bounds via ICS. early = older bound (range_base), late = younger
        # bound (range_top). When ICS is unavailable or a bound can't be
        # resolved we fall back to the biozone label and the section-level
        # numeric age_range.
        e_stage, l_stage, e_ma, l_ma = _resolve_pbdb_bounds(row)
        # REVIEW-2026-07-31: when per-row bounds don't resolve (bed labels
        # are the common case), fall back to the section's resolved
        # interval names instead of duplicating the biozone into both
        # interval fields (old C-2 semantic error).
        early_interval = e_stage or sec_data.get("older_name", "") or biozone
        late_interval = l_stage or sec_data.get("younger_name", "") or biozone
        # PBDB requires max_ma (older) >= min_ma (younger). Never combine a
        # per-row bound with a section fallback because that can create a
        # scientifically invalid hybrid interval. Use a complete, ordered
        # per-row pair or a complete, ordered section-level pair; else blank.
        max_ma, min_ma = _validated_pbdb_ages(
            e_ma,
            l_ma,
            sec_data.get("max_ma", ""),
            sec_data.get("min_ma", ""),
        )
        occurrence = {
            "occurrence_id": f"RC_{species.replace(' ', '_')}_{section}_{idx}",
            "taxon_name": species,
            "identified_by": "",
            "collection_name": sec_data.get("collection_name", section),
            "formation": sec_data.get("formation", ""),
            # C-2 fix: early_interval = older bound, late_interval = younger
            # bound. These were both set to the biozone, which is a semantic
            # type error (the biozone label is the biozone, not the interval).
            "early_interval": early_interval,
            "late_interval": late_interval,
            # C-4 fix: max_ma = older boundary, min_ma = younger boundary,
            # sourced from the ICS-resolved per-row bounds (or the section's
            # numeric age_range). PBDB importers key temporal queries on these.
            "max_ma": str(max_ma) if max_ma not in (None, "") else "",
            "min_ma": str(min_ma) if min_ma not in (None, "") else "",
            "latitude": str(lat) if lat is not None else "",
            "longitude": str(lon) if lon is not None else "",
            "biostratigraphic_zone": biozone,
            "notes": f"author_year: {author_year}",
        }
        occurrences.append(occurrence)
    return occurrences


def to_pbdb_collections(result):
    collections = []
    sections = result.get("sections", []) or []
    for sec in sections:
        if not isinstance(sec, dict): continue
        name = sec.get("name", "")
        if not name: continue
        lat, lon = None, None
        coords = sec.get("coordinates", "")
        if coords: lat, lon = _parse_coords(coords)
        formation = "; ".join(sec.get("formations", []) or [])
        # M-1 fix: populate min_ma / max_ma on collections as well.
        # Previously always empty — PBDB joins on these fields.
        # REVIEW-2026-07-31: stage/series-only age ranges now fall back to
        # ICS resolution (same as occurrences), and the resolved interval
        # names fill early_interval / late_interval.
        age_range = sec.get("age_range", "") or ""
        min_ma, max_ma = _parse_age_range_ma(age_range)
        early_interval, late_interval = "", ""
        if min_ma is None and _HAS_ICS and ics_age_range_bounds:
            older_ma, younger_ma = ics_age_range_bounds(age_range)
            if older_ma is not None:
                min_ma, max_ma = younger_ma, older_ma
        if _HAS_ICS and ics_parse_age_range:
            _stages = ics_parse_age_range(age_range)
            _older_name, _younger_name = (
                _stage_endpoint_names(_stages) if _stages else ("", ""))
            if _older_name and _younger_name:
                early_interval, late_interval = _older_name, _younger_name
            elif ics_resolve_age_bound:
                _n_old, _ = ics_resolve_age_bound(age_range, prefer="older")
                if _n_old:
                    early_interval = late_interval = _n_old
        collection = {
            "collection_name": name,
            "latitude": str(lat) if lat is not None else "",
            "longitude": str(lon) if lon is not None else "",
            "formation": formation,
            "early_interval": early_interval, "late_interval": late_interval,
            "min_ma": str(min_ma) if min_ma is not None else "",
            "max_ma": str(max_ma) if max_ma is not None else "",
        }
        collections.append(collection)
    return collections


def to_pbdb_csv(result, output_path):
    output_path = Path(output_path)
    if output_path.is_file(): output_path = output_path.parent
    occ_path = output_path / "pbdb_occurrences.csv"
    col_path = output_path / "pbdb_collections.csv"
    occurrences = to_pbdb_occurrences(result)
    collections = to_pbdb_collections(result)
    occ_fields = ["occurrence_id", "taxon_name", "identified_by", "collection_name", "formation", "early_interval", "late_interval", "max_ma", "min_ma", "latitude", "longitude", "biostratigraphic_zone", "notes"]
    col_fields = ["collection_name", "latitude", "longitude", "formation", "early_interval", "late_interval", "max_ma", "min_ma"]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=occ_fields, lineterminator=chr(10))
    writer.writeheader()
    for occ in occurrences: writer.writerow(occ)
    occ_path.write_text(output.getvalue(), encoding="utf-8")
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=col_fields, lineterminator=chr(10))
    writer.writeheader()
    for col in collections: writer.writerow(col)
    col_path.write_text(output.getvalue(), encoding="utf-8")
