"""Mode normalizer: paleomap.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _KNOWN_PALEOMAP_CONTINENT_KEYS,
    _KNOWN_PALEOMAP_INDICATOR_KEYS,
    _KNOWN_PALEOMAP_REALM_KEYS,
    _KNOWN_PALEOMAP_ROOT_KEYS,
    _KNOWN_PALEOMAP_SEA_KEYS,
    _KNOWN_PALEOMAP_SITE_KEYS,
    _KNOWN_PALEOMAP_TECTONIC_KEYS,
    _PALEOMAP_ARRAY_SPEC_KEYS,
    _carry_extras,
    _confidence_clamped,
    _dict_rows,
    _pop_array_root_extras,
    _rehydrate_array_root,
    _stringify_scalar,
    first_non_empty,
)
from typing import Any

def normalize_paleomap_result(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the parsed paleogeographic map JSON into the strict result shape.

    FIX-NEW: new chart type for paleogeographic maps.
    """
    if not isinstance(parsed, dict):
        return {
            "metadata": {},
            "continents": [],
            "oceans_seas": [],
            "tectonic_features": [],
            "biogeographic_realms": [],
            "fossil_sites": [],
            "paleolatitude_indicators": [],
            "confidence": 0.0,
            "_warnings": ["normalize_non_dict_input"],
        }

    def s(v):
        # REVIEW-2026-09-10: container-safe (see _stringify_scalar).
        return _stringify_scalar(v)

    # REVIEW-2026-09-10: repairs to surface on the result.
    _paleomap_warnings: list[str] = []
    out = {
        "metadata": {},
        "continents": [],
        "oceans_seas": [],
        "tectonic_features": [],
        "biogeographic_realms": [],
        "fossil_sites": [],
        "paleolatitude_indicators": [],
        "confidence": 0.0,
    }

    # AUDIT-2026-09-27 P0-1: this was the only remaining normalizer without
    # the ``_array_root`` unwrap. json_utils wraps a bare-array emission (the
    # model replying ``[{...}, {...}]`` to an object contract) into
    # ``{"_array_root": [...]}``; ``parsed.get("continents")`` was then empty,
    # ``_pop_array_root_extras`` DELETED the key, and the whole map came back
    # as ``ok=True`` with six empty tables and no warning.
    _rehydrate_array_root(parsed, _PALEOMAP_ARRAY_SPEC_KEYS, _paleomap_warnings)

    # Normalize metadata
    meta = parsed.get("metadata") or {}
    if isinstance(meta, dict):
        out["metadata"] = {
            "time_slice": s(meta.get("time_slice", "")),
            "approximate_age_ma": s(meta.get("approximate_age_ma", "")),
            "map_title": s(meta.get("map_title", "")),
            "projection": s(meta.get("projection", "")),
            "scale": s(meta.get("scale", "")),
            "source": s(meta.get("source", "")),
        }

    def _one_point(item):
        """A single coordinate -> [lat, lon], or None when it is not one."""
        if isinstance(item, dict):
            lat = first_non_empty(
                (item.get("lat"), item.get("latitude"), item.get("y")))
            lon = first_non_empty(
                (item.get("lon"), item.get("lng"), item.get("longitude"),
                 item.get("x")))
            if lat is None and lon is None:
                return None
            item = [lat if lat is not None else "",
                    lon if lon is not None else ""]
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            if any(isinstance(p, (list, tuple, dict)) for p in item[:2]):
                return None          # a ring / nested collection, not a point
            try:
                return [float(item[0]), float(item[1])]
            except (TypeError, ValueError):
                return [s(item[0]), s(item[1])]
        return None

    def norm_coords(val, label="coordinates"):
        """Normalize coordinates to a list of [lat, lon] pairs.

        REVIEW-2026-09-20 #16: models emit at least four shapes for this one
        field - ``[[lat, lon], ...]``, a bare ``[lat, lon]`` single point,
        ``{"lat": .., "lon": ..}`` / ``{"latitude": .., "longitude": ..}``
        objects, and dicts of named points. Only the first was accepted, so a
        perfectly readable feature came back as "no coordinates at all"
        without a word about it. Unparseable payloads now warn.
        """
        pts: list[Any] = []
        if isinstance(val, dict):
            direct = _one_point(val)
            if direct is not None:
                return [direct]
            pts = [p for p in (_one_point(v) for v in val.values()) if p]
        elif isinstance(val, (list, tuple)):
            direct = _one_point(val)
            if direct is not None and len(val) == 2:
                return [direct]      # a single [lat, lon] point
            for item in val:
                p = _one_point(item)
                if p is not None:
                    pts.append(p)
                elif isinstance(item, (list, tuple, dict)):
                    pts.extend(norm_coords(item, label))
        if pts or val in (None, "", [], {}):
            return pts
        _paleomap_warnings.append(f"coordinates_unparsed:{label}")
        # AUDIT-2026-09-27 [item 7.4]: a coordinate the model wrote as TEXT is
        # not junk — "48.1N 12.0E" is exactly what a geologist wants to see,
        # and every paleomap table routes through this function, so returning
        # [] here discarded the value in all six of them. The warning stays
        # (the string was not machine-parsed), but the value is kept rather
        # than thrown away; the GUI renders this column with `col.coordinates`
        # and displays a string there without complaint.
        #
        # The test is "contains a digit", not merely "is a non-empty string":
        # the models also emit placeholders ("unknown", "not visible in
        # chart", "-") for a coordinate they could not read, and those must
        # NOT be promoted into the coordinates column as if they were a
        # reading. Those keep the pre-existing behaviour: dropped, warned.
        if isinstance(val, str) and any(ch.isdigit() for ch in val):
            return [val.strip()]
        return pts

    # Normalize continents
    for cont in _dict_rows(parsed.get("continents"), "continents", _paleomap_warnings):
        row = {
            "name": s(cont.get("name")),
            "type": s(cont.get("type")),
            "coordinates": norm_coords(cont.get("coordinates"), "continents"),
            "paleolatitude": s(cont.get("paleolatitude")),
            "note": s(cont.get("note")),
        }
        # H8 (Sprint B REVIEW-2026-09-04): preserve unknown row keys.
        _carry_extras(cont, _KNOWN_PALEOMAP_CONTINENT_KEYS, row)
        out["continents"].append(row)

    # Normalize oceans/seas
    for sea in _dict_rows(parsed.get("oceans_seas"), "oceans_seas", _paleomap_warnings):
        row = {
            "name": s(sea.get("name")),
            "type": s(sea.get("type")),
            "coordinates": norm_coords(sea.get("coordinates"), "oceans_seas"),
            "note": s(sea.get("note")),
        }
        _carry_extras(sea, _KNOWN_PALEOMAP_SEA_KEYS, row)
        out["oceans_seas"].append(row)

    # Normalize tectonic features
    for feat in _dict_rows(parsed.get("tectonic_features"), "tectonic_features", _paleomap_warnings):
        row = {
            "name": s(feat.get("name")),
            "type": s(feat.get("type")),
            "coordinates": norm_coords(feat.get("coordinates"), "tectonic_features"),
            "direction": s(feat.get("direction")),
            "description": s(feat.get("description")),
        }
        _carry_extras(feat, _KNOWN_PALEOMAP_TECTONIC_KEYS, row)
        out["tectonic_features"].append(row)

    # Normalize biogeographic realms
    for realm in _dict_rows(parsed.get("biogeographic_realms"), "biogeographic_realms", _paleomap_warnings):
        row = {
            "name": s(realm.get("name")),
            "type": s(realm.get("type")),
            "coordinates": norm_coords(realm.get("coordinates"), "biogeographic_realms"),
            "characteristic_fauna": s(realm.get("characteristic_fauna")),
        }
        _carry_extras(realm, _KNOWN_PALEOMAP_REALM_KEYS, row)
        out["biogeographic_realms"].append(row)

    # Normalize fossil sites
    for site in _dict_rows(parsed.get("fossil_sites"), "fossil_sites", _paleomap_warnings):
        row = {
            "name": s(site.get("name")),
            # AUDIT-2026-09-27 [item 7.3]: this was the ONE paleomap table
            # that did not route its coordinate through norm_coords — every
            # sibling (continents / oceans_seas / tectonic_features /
            # biogeographic_realms / paleolatitude_indicators) does. `s(...)`
            # stringifies, and a dict/list lat_lon stringifies to "", so a
            # model returning structured coordinates for a site lost them
            # outright. And because "lat_lon" is in _KNOWN_PALEOMAP_SITE_KEYS
            # the value was considered "known", so _carry_extras did not
            # rescue it either. Observed in real runs as lat_lon: "" next to
            # a fossil_sites row that plainly had coordinates.
            # The GUI already renders this column with `col.coordinates`, so
            # normalising it is also what the table was displaying all along.
            "lat_lon": norm_coords(site.get("lat_lon"), "fossil_sites"),
            "age": s(site.get("age")),
            "fossils": s(site.get("fossils")),
            "marker_type": s(site.get("marker_type")),
        }
        _carry_extras(site, _KNOWN_PALEOMAP_SITE_KEYS, row)
        out["fossil_sites"].append(row)

    # Normalize paleolatitude indicators
    for ind in _dict_rows(parsed.get("paleolatitude_indicators"), "paleolatitude_indicators", _paleomap_warnings):
        row = {
            "type": s(ind.get("type")),
            "coordinates": norm_coords(ind.get("coordinates"), "paleolatitude_indicators"),
        }
        _carry_extras(ind, _KNOWN_PALEOMAP_INDICATOR_KEYS, row)
        out["paleolatitude_indicators"].append(row)

    try:
        conf = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    out["confidence"] = _confidence_clamped(conf)

    extras_src = _pop_array_root_extras(
        {k: v for k, v in parsed.items() if k not in _KNOWN_PALEOMAP_ROOT_KEYS})
    if extras_src:
        out["_extras"] = extras_src
    if _paleomap_warnings:
        out["_warnings"] = _paleomap_warnings
    return out


