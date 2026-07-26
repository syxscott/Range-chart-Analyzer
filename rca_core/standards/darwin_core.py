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
    m = re.search(r"([+-]?\d+\.?\d*)\s*[NSns],?\s*([+-]?\d+\.?\d*)\s*[EWew]", text)
    if m:
        lat_val = float(m.group(1))
        lon_val = float(m.group(2))
        if re.search(r"[Ss]", text):
            lat_val = -abs(lat_val)
        if re.search(r"[Ww]", text):
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
    for sec in sections:
        if isinstance(sec, dict):
            name = sec.get("name", "")
            coords = sec.get("coordinates", "")
            lat, lon = _parse_coordinates(coords)
            section_coords[name] = (lat, lon)
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
        occurrence = {
            "occurrenceID": f"{species.replace(' ', '_')}_{section}_{idx}",
            "scientificName": species,
            "scientificNameAuthorship": author_year or "",
            "locality": section,
            "decimalLatitude": str(lat) if lat is not None else "",
            "decimalLongitude": str(lon) if lon is not None else "",
            "chronostratigraphicAge": biozone or "",
            "earliestAgeOrLowestStage": biozone,
            "latestAgeOrHighestStage": biozone,
            "biostratigraphicZone": biozone,
            "lithostratigraphicTerms": "",
            "occurrenceRemarks": f"Range chart extraction from {section}",
            "occurrenceStatus": "present",
            "basisOfRecord": "MachineExtractedFromImage",
        }
        occurrences.append(occurrence)
    return occurrences


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
        # Write occurrence.txt
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(fields)
        for occ in occurrences:
            row = [occ.get(f, "") for f in fields]
            writer.writerow(row)
        zf.writestr("occurrence.txt", output.getvalue())

        # Write meta.xml
        zf.writestr("meta.xml", meta_xml)

    return archive_path
