"""PBDB mapping for range chart data."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Optional


def _parse_coords(text):
    if not text or not isinstance(text, str):
        return None, None
    text = text.strip()
    if re.search(r"not\s*visible|unknown|missing", text, re.IGNORECASE):
        return None, None
    m = re.search(r"([+-]?\d+\.?\d*)\s*[NSns],?\s*([+-]?\d+\.?\d*)\s*[EWew]", text)
    if m:
        lat_val = float(m.group(1))
        lon_val = float(m.group(2))
        if re.search(r"[Ss]", text): lat_val = -abs(lat_val)
        if re.search(r"[Ww]", text): lon_val = -abs(lon_val)
        return lat_val, lon_val
    return None, None


def _parse_age_range_ma(text):
    if not text: return None, None
    numbers = re.findall(r"\d+\.?\d*", text)
    if len(numbers) >= 2:
        try:
            min_ma = float(numbers[0])
            max_ma = float(numbers[1])
            if min_ma > max_ma: min_ma, max_ma = max_ma, min_ma
            return min_ma, max_ma
        except ValueError: pass
    return None, None


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
            }
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
        author_year = row.get("author_year", row.get("authority", ""))
        occurrence = {
            "occurrence_id": f"RC_{species.replace(' ', '_')}_{section}_{idx}",
            "taxon_name": species,
            "identified_by": "",
            "collection_name": sec_data.get("collection_name", section),
            "formation": sec_data.get("formation", ""),
            "early_interval": biozone,
            "late_interval": biozone,
            "max_ma": "", "min_ma": "",
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
        collection = {
            "collection_name": name,
            "latitude": str(lat) if lat is not None else "",
            "longitude": str(lon) if lon is not None else "",
            "formation": formation,
            "early_interval": "", "late_interval": "",
            "max_ma": "", "min_ma": "",
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
