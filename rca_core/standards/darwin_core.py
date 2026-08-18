"""Darwin Core mapping for range chart data.

Provides functions to convert range chart extraction results to Darwin Core
Occurrence records and Darwin Core Archive (DwC-A) format.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Optional

# M-1 / C-1 (REVIEW-2026-07-25): wire the ICS 2024 chronostratigraphic table
# into the export path so stage-name -> numeric Ma conversion actually
# happens here (previously ICS was only imported by quality.py, so the
# Darwin Core / PBDB exporters emitted raw labels with no numeric ages).
# Degrade gracefully to the previous behaviour if ICS is unavailable.
try:
    from .ics import (
        ICS_2024,
        ics_parse_age_range,
        ics_resolve_age_bound,
        ics_stage_from_age,
    )
    _HAS_ICS = bool(ICS_2024)
except Exception:  # pragma: no cover - import fallback
    _HAS_ICS = False
    ICS_2024 = {}
    ics_parse_age_range = None
    ics_resolve_age_bound = None
    ics_stage_from_age = None


def _parse_coordinates(text: str) -> tuple[Optional[float], Optional[float]]:
    """Parse coordinate text like "31N, 117E" or "31.5 S 117.5 W" to (lat, lon).

    Handles:
    - "31N, 117E" -> (31.0, 117.0)
    - "31.5 S 117.5 W" -> (-31.5, -117.5)
    - "31.5 S, 117.5 E" -> (-31.5, 117.5)
    - "Not visible in chart" -> (None, None)
    Returns (lat, lon) tuple.
    """
    if not text or not isinstance(text, str):
        return None, None
    text = text.strip()
    if re.search(r"not\s*visible|unknown|missing", text, re.IGNORECASE):
        return None, None
    lat, lon = None, None
    # Try pattern like "31N, 117E" or "31N 117E"
    m = re.search(r"([+-]?\d+\.?\d*)\s*([NSns]),?\s*([+-]?\d+\.?\d*)\s*([EWew])", text)
    if m:
        lat_val = float(m.group(1))
        lon_val = float(m.group(3))
        # REVIEW-2026-07-31: hemisphere must come from the matched letter
        # group, NOT a scan of the whole text — "31N, 117E (south bank)"
        # used to flip the latitude to -31.
        if m.group(2).upper() == "S":
            lat_val = -abs(lat_val)
        if m.group(4).upper() == "W":
            lon_val = -abs(lon_val)
        return lat_val, lon_val
    # Try pattern like "31.5 S 117.5 W" (space separated)
    m = re.search(r"([+-]?\d+\.?\d*)\s*([NSns])\s+([+-]?\d+\.?\d*)\s*([EWew])", text)
    if m:
        lat_val = float(m.group(1))
        lon_val = float(m.group(3))
        if m.group(2).upper() == "S":
            lat_val = -abs(lat_val)
        if m.group(4).upper() == "W":
            lon_val = -abs(lon_val)
        return lat_val, lon_val
    return None, None


def to_darwin_core_occurrences(result: dict) -> list[dict]:
    """Convert range_chart result to Darwin Core occurrence records.

    Returns list of dicts with DwC terms as keys:
    - occurrenceID, scientificName, scientificNameAuthorship
    - locality, decimalLatitude, decimalLongitude
    - chronostratigraphicAge, earliestAgeOrLowestStage, latestAgeOrHighestStage
    - biostratigraphicZone
    - lithostratigraphicTerms
    - occurrenceRemarks, occurrenceStatus
    - basisOfRecord ('MachineExtractedFromImage' for our use case)
    """
    occurrences = []
    species_ranges = result.get("species_ranges", []) or []
    sections = result.get("sections", []) or []
    section_coords = {}
    section_ages = {}
    section_formations: dict[str, Any] = {}
    for sec in sections:
        if isinstance(sec, dict):
            name = sec.get("name", "")
            coords = sec.get("coordinates", "")
            lat, lon = _parse_coordinates(coords)
            section_coords[name] = (lat, lon)
            # Carry each section's age_range for FAD/LAD derivation
            section_ages[name] = sec.get("age_range", "") or ""
            # H3: carry lithostratigraphic context so occurrence-level
            # lithostratigraphicTerms can be populated below.
            section_formations[name] = sec.get("formations", []) or []
    for idx, row in enumerate(species_ranges):
        if not isinstance(row, dict):
            continue
        species = row.get("species", "")
        if not species:
            continue
        section = row.get("section", "")
        lat, lon = section_coords.get(section, (None, None))
        biozone = row.get("biozone", "")
        author_year = row.get("author_year", row.get("authority", ""))
        age_range = section_ages.get(section, "")
        # M-1 / C-1 fix: derive FAD/LAD from the species' range_base
        # (older) and range_top (younger) via ICS, instead of collapsing
        # both stage fields to the same biozone string.
        # REVIEW-2026-07-31: (a) the numeric Ma values are no longer
        # discarded — they are emitted as fad_ma / lad_ma columns; (b) when
        # a bound cannot be resolved the RAW range_base / range_top labels
        # are kept (instead of falling back to the same biozone/age_range
        # string on both sides, which re-collapsed earliest == latest).
        earliest_stage, latest_stage, e_ma, l_ma = _resolve_age_bounds(row)
        base_raw = str(row.get("range_base") or "").strip()
        top_raw = str(row.get("range_top") or "").strip()
        earliest = earliest_stage or base_raw or biozone or age_range or ""
        latest = latest_stage or top_raw or biozone or age_range or ""
        # H3: lithostratigraphicTerms from the row's own formation/group/
        # member fields if present, else from the parent section's
        # formations list.
        litho = _resolve_lithostratigraphy(row, section_formations.get(section))
        # H4: carry per-row endpoint_kind / occurrence_mode signals so
        # Lazarus / range-extension signals survive export.
        endpoint_kind = str(row.get("endpoint_kind") or "").strip()
        occurrence_mode = str(row.get("occurrence_mode") or "").strip()
        remarks = f"Range chart extraction from {section}"
        signals = []
        if endpoint_kind:
            signals.append(f"endpoint_kind={endpoint_kind}")
        if occurrence_mode:
            signals.append(f"occurrence_mode={occurrence_mode}")
        if signals:
            remarks = f"{remarks}; {'; '.join(signals)}"
        # REVIEW-2026-07-31: emit the ICS-resolved numeric FAD/LAD ages as
        # first-class columns (project-local namespace, like endpointKind)
        # so downstream analyses get numeric ages, not just stage text.
        # A reversed pair (e_ma < l_ma) is suppressed by _resolve_age_bounds.
        fad_ma = str(e_ma) if e_ma is not None else ""
        lad_ma = str(l_ma) if l_ma is not None else ""
        occurrence = {
            "occurrenceID": f"{species.replace(' ', '_')}_{section}_{idx}",
            "scientificName": species,
            "scientificNameAuthorship": author_year or "",
            "locality": section,
            "decimalLatitude": str(lat) if lat is not None else "",
            "decimalLongitude": str(lon) if lon is not None else "",
            "chronostratigraphicAge": age_range or biozone or "",
            # M-1 fix: DO NOT collapse earliest and latest. Use the
            # species' range_base (FAD, older) and range_top (LAD,
            # younger) so downstream diversity analyses can compute
            # true ranges.
            "earliestAgeOrLowestStage": earliest,
            "latestAgeOrHighestStage": latest,
            "biostratigraphicZone": biozone or "",
            "lithostratigraphicTerms": litho,
            "occurrenceRemarks": remarks,
            "occurrenceStatus": "present",
            # H1 fix (REVIEW-2026-07-25): basisOfRecord must be a valid
            # Darwin Core vocabulary term. "MachineGenerated" is NOT in
            # the TDWG basisOfRecord vocabulary; "MachineObservation" is
            # the correct term for records produced automatically by a
            # machine process (e.g. VLM extraction). Strict consumers
            # (GBIF, iDigBio) reject unknown terms.
            "basisOfRecord": "MachineObservation",
            # H4 fix: surface the per-row signals as first-class columns
            # so downstream tooling can read them programmatically
            # (not only embedded in occurrenceRemarks).
            "endpointKind": endpoint_kind,
            "occurrenceMode": occurrence_mode,
            # REVIEW-2026-07-31: numeric FAD (older) / LAD (younger) Ma.
            "fad_ma": fad_ma,
            "lad_ma": lad_ma,
        }
        occurrences.append(occurrence)
    return occurrences


def _resolve_age_bounds(row: dict) -> tuple[str, str, Optional[float], Optional[float]]:
    """Resolve a species row's FAD/LAD bounds to ICS stages + numeric Ma.

    Returns ``(earliest_stage, latest_stage, earliest_ma, latest_ma)`` where
    ``earliest`` is the OLDER bound (range_base / FAD) and ``latest`` is the
    YOUNGER bound (range_top / LAD). When ICS is unavailable or a bound
    can't be resolved the corresponding value is ``""`` / ``None`` so
    callers can fall back to their previous behaviour.
    """
    if not _HAS_ICS or not isinstance(row, dict):
        return "", "", None, None
    base = row.get("range_base")
    top = row.get("range_top")
    # REVIEW-2026-07-31: prefer="older"/"younger" resolves interval
    # literals ("259.51-254.14 Ma") to the correct end — FAD/base is the
    # OLDER (larger Ma) end, LAD/top the YOUNGER end.
    e_stage, e_ma = ics_resolve_age_bound(base, prefer="older") if ics_resolve_age_bound else (None, None)
    l_stage, l_ma = ics_resolve_age_bound(top, prefer="younger") if ics_resolve_age_bound else (None, None)
    # A complete numeric pair must respect geological direction: the FAD/base
    # is older (larger Ma) than the LAD/top. Keep independently resolved stage
    # labels, but suppress invalid numeric ages so exporters cannot publish a
    # reversed temporal range.
    if e_ma is not None and l_ma is not None and e_ma < l_ma:
        e_ma = None
        l_ma = None
    return (e_stage or "", l_stage or "", e_ma, l_ma)


def _resolve_lithostratigraphy(row: dict, section_formations: Any) -> str:
    """Build a lithostratigraphicTerms string from row/section fields.

    H3 fix (REVIEW-2026-07-25): ``lithostratigraphicTerms`` was hardcoded
    to ``""``. Populate it from the row's own formation/group/member fields
    when present, otherwise from the parent section's formations list.
    Returns an empty string when nothing is available.
    """
    def _join(val: Any) -> str:
        if not val:
            return ""
        if isinstance(val, (list, tuple)):
            return "; ".join(str(v).strip() for v in val if str(v).strip())
        return str(val).strip()

    litho = _join(row.get("formation") or row.get("group") or row.get("member"))
    if not litho:
        litho = _join(section_formations)
    return litho


def _first_stage_before(age_range, bed_label):
    """Kept for backward compatibility; superseded by ``_resolve_age_bounds``.

    The real FAD/LAD derivation now lives in ``_resolve_age_bounds`` which
    uses the ICS table. This helper is retained only so any external caller
    that imported it still works; it remains a passthrough of ``age_range``.
    """
    if not age_range:
        return ""
    return age_range


def _ics_stage_to_pbdb_name(stage: str) -> Optional[str]:
    """Map ICS stage name to PBDB early_interval/late_interval format."""
    return stage


def to_darwin_core_archive(result: dict, output_path: str) -> str:
    """Create DwC-A ZIP file.

    Creates archive.zip with occurrence.txt and meta.xml.
    Returns the path to the created archive.
    """
    occurrences = to_darwin_core_occurrences(result)
    output_path = Path(output_path)
    if output_path.is_dir():
        output_path = output_path / "DwC-A.zip"
    archive_path = str(output_path)

    fields = [
        "occurrenceID", "scientificName", "scientificNameAuthorship",
        "locality", "decimalLatitude", "decimalLongitude",
        "chronostratigraphicAge", "earliestAgeOrLowestStage",
        "latestAgeOrHighestStage", "biostratigraphicZone",
        "lithostratigraphicTerms", "occurrenceRemarks",
        "occurrenceStatus", "basisOfRecord",
        # H4: dynamic property columns so endpoint_kind / occurrence_mode
        # signals survive export as machine-readable fields (not only
        # embedded in occurrenceRemarks).
        "endpointKind", "occurrenceMode",
        # REVIEW-2026-07-31: numeric FAD/LAD ages (Ma) from ICS resolution.
        "fad_ma", "lad_ma",
    ]

    field_terms = [
        "http://rs.tdwg.org/dwc/terms/occurrenceID",
        "http://rs.tdwg.org/dwc/terms/scientificName",
        "http://rs.tdwg.org/dwc/terms/scientificNameAuthorship",
        "http://rs.tdwg.org/dwc/terms/locality",
        "http://rs.tdwg.org/dwc/terms/decimalLatitude",
        "http://rs.tdwg.org/dwc/terms/decimalLongitude",
        "http://rs.tdwg.org/dwc/terms/chronostratigraphicAge",
        "http://rs.tdwg.org/dwc/terms/earliestAgeOrLowestStage",
        "http://rs.tdwg.org/dwc/terms/latestAgeOrHighestStage",
        "http://rs.tdwg.org/dwc/terms/biostratigraphicZone",
        "http://rs.tdwg.org/dwc/terms/lithostratigraphicTerms",
        "http://rs.tdwg.org/dwc/terms/occurrenceRemarks",
        "http://rs.tdwg.org/dwc/terms/occurrenceStatus",
        "http://rs.tdwg.org/dwc/terms/basisOfRecord",
        # Custom extension terms for the per-row signals. These are not in
        # the core DwC vocabulary; they are emitted as additional columns
        # keyed by a project-local namespace so strict consumers ignore
        # them rather than rejecting the whole record.
        "https://range-chart-analyzer.local/terms/endpointKind",
        "https://range-chart-analyzer.local/terms/occurrenceMode",
        # REVIEW-2026-07-31: project-namespace numeric ages (no standard
        # core DwC term for numeric Ma in the core file; consumers use
        # these columns for time filtering / diversity analyses).
        "https://range-chart-analyzer.local/terms/fadMa",
        "https://range-chart-analyzer.local/terms/ladMa",
    ]

    tab = "\t"
    newline = "\n"

    meta_xml_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<archive xmlns="http://rs.tdwg.org/dwc/text/" metadata="eml.xml">',
        '  <core encoding="UTF-8" fieldsTerminatedBy="\t" linesTerminatedBy="\n"',
        '        fieldsEnclosedBy="" ignoreHeaderLines="1"',
        '        rowType="http://rs.tdwg.org/dwc/terms/Occurrence">',
        '    <files><location>occurrence.txt</location></files>',
        '    <id column="0"/>',
    ]
    for i, term in enumerate(field_terms):
        meta_xml_lines.append(f'    <field index="{i}" term="{term}"/>')
    meta_xml_lines.append('  </core>')
    meta_xml_lines.append('</archive>')
    meta_xml = "\n".join(meta_xml_lines)

    with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Write occurrence.txt. H4 fix (REVIEW-2026-07-25): Darwin Core
        # Archive MUST be TAB-separated. The previous csv.writer used the
        # default comma delimiter while meta.xml declared
        # fieldsTerminatedBy="\t", producing a file strict consumers
        # (GBIF, iDigBio) couldn't parse.
        output = io.StringIO()
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(fields)
        for occ in occurrences:
            row = [occ.get(f, "") for f in fields]
            writer.writerow(row)
        zf.writestr("occurrence.txt", output.getvalue())

        # Write meta.xml
        zf.writestr("meta.xml", meta_xml)

    return archive_path
