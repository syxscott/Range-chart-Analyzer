"""Merge results from multiple extraction runs of the same chart.

Running the same image N times and taking the union with an agreement
count reduces random single-pass OCR misreads: a name read consistently
across runs is trustworthy; a name seen only once is flagged for review.

Schema-aware: pass a `MergeSchema` describing how to dedup/group rows
for modes other than range-chart. When ``schema`` is omitted we
auto-detect between range-chart and columnar-section using the row shape
(H5).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional


def _norm(s):
    if not s:
        return ""
    t = str(s).strip()
    t = re.sub(r"\s+sp\.?$", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+cf\.?\s+", " ", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"\s+", " ", t)
    return t.lower()


def _mode(values):
    non_empty = [v for v in values if v and str(v).strip()]
    if not non_empty:
        return ""
    counts = Counter(non_empty)
    top = max(counts.values())
    if top == 1 and len(counts) == len(non_empty):
        # H4: all values unique → tie-break deterministically by sorted
        # order so input order doesn't silently decide the merged string.
        return sorted(non_empty)[0]
    for v in non_empty:
        if counts[v] == top:
            return v
    return non_empty[0]


# Sentinel returned by _merge_field_across_runs to signal "no consensus"
# values are non-string scalars or all None — caller should drop the key.
_NO_MERGE = object()


def _merge_scalar_field(values):
    """_mode for plain str / int / float / bool values.

    Non-empty values are coerced to str so Counter works uniformly. None,
    empty strings, and structurally non-primitive values are skipped.
    """
    coerced = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            s = str(v).strip()
            if s:
                coerced.append(s)
    if not coerced:
        return _NO_MERGE
    return _mode(coerced)


def _merge_structured_field(values):
    """Merge a structured field (list of dicts) across runs.

    Union all items by signature; duplicate signatures collapse to a single
    entry. Returns [] when no item has any dict entries.
    """
    seen = set()
    out = []
    for v in values:
        if v is None:
            continue
        if not isinstance(v, list):
            continue
        for item in v:
            if not isinstance(item, dict):
                continue
            # Signature is a sorted frozenset of values. Stable across dict
            # iteration but only catches values that compare equal; that
            # is the best we can do without a domain-specific schema.
            try:
                sig = repr(sorted(item.items()))
            except TypeError:
                # Mixed-type dict value; fall back to a coerced signature.
                sig = repr(sorted(((k, str(val)) for k, val in item.items())))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(item)
    return out


def _merge_field_across_runs(values):
    """Dispatch a list of run-time values to the right merge strategy.

    - scalar (str / int / float / bool, possibly with a few Nones mixed) → _mode
    - list of dicts (columnar-section sub-arrays) → structured union
    - any other shape (None-only, plain strings, dicts at top level) →
      return first non-None string via _mode's fallback (preserves prior
      behaviour for "other_fossils" strings and similar plain-string
      fields), or the structured union if it's a dict sequence.
    """
    # If every non-None value is a list-of-dicts → structured merge.
    non_none = [v for v in values if v is not None]
    if non_none and all(isinstance(v, list) and all(isinstance(x, dict) for x in v) for v in non_none):
        merged = _merge_structured_field(values)
        return merged  # always a list (possibly empty)
    # If every non-None value is a primitive → scalar merge.
    if non_none and all(isinstance(v, (str, int, float, bool)) for v in non_none):
        return _merge_scalar_field(values)
    # Mixed: fall back to plain string _mode (handles "other_fossils"-style
    # lists of strings, etc.).
    return _mode([str(v) for v in values if v is not None])


@dataclass(slots=True)
class MergeSchema:
    """Describes how merge_results aggregates a result dict.

    Attributes
    ----------
    primary_list_key : str
        Top-level key whose items are the primary rows
        (e.g. "species_ranges" for range-chart, "sections" for
        columnar_section).
    primary_id_keys : list[str]
        Field names on each primary row whose normalized combination
        forms the dedup key. Must contain at least one field.
    primary_str_mode_fields : list[str]
        String fields collapsed by majority across runs (`_mode`).
    sort_keys : list[tuple[str, Any]]
        Each tuple `(field_name, direction)` controlling post-merge order.
    list_keys : list[str]
        Top-level keys treated as de-duplicated lists
        (e.g. "biozones" and "other_fossils").
    confidence_field : str
        Top-level key whose float is averaged into "confidence".
    """

    primary_list_key: str
    primary_id_keys: list[str]
    primary_str_mode_fields: list[str] = field(default_factory=list)
    sort_keys: list[tuple[str, Any]] = field(default_factory=list)
    list_keys: list[str] = field(default_factory=list)
    confidence_field: str = "confidence"


# Backward-compatible default for range-chart results.
RANGE_CHART_SCHEMA = MergeSchema(
    primary_list_key="species_ranges",
    primary_id_keys=["section", "species"],
    primary_str_mode_fields=[
        "species", "section", "range_base", "range_top", "biozone",
    ],
    sort_keys=[("agreement_count", "desc"), ("species", "asc")],
    list_keys=["biozones", "other_fossils"],
    confidence_field="confidence",
)

# Schema for columnar-section results.
COLUMNAR_SECTION_SCHEMA = MergeSchema(
    primary_list_key="sections",
    primary_id_keys=["id", "group"],
    primary_str_mode_fields=[
        "id", "group", "coordinates_text", "thickness_m",
    ],
    sort_keys=[("id", "asc")],
    list_keys=["fossil_legend", "lithology_legend", "cross_beds"],
    confidence_field="confidence",
)


SCHEMA_BY_MODE = {
    "range_chart": RANGE_CHART_SCHEMA,
    "columnar_section": COLUMNAR_SECTION_SCHEMA,
}


def _looks_columnar(data: Optional[dict]) -> bool:
    """Mirror of rca_core.exporter._looks_columnar kept private to this
    module so the auto-detect stays schema-only and avoids any circular
    import back through exporter / extractor."""
    if not data:
        return False
    sects = data.get("sections")
    if not isinstance(sects, list) or not sects:
        return False
    first = sects[0]
    return isinstance(first, dict) and ("id" in first) and ("name" not in first)


def _auto_detect_schema(results):
    """H5: pick the schema that matches the majority of inputs. Falls back
    to the range-chart schema if shapes are inconsistent."""
    if not results:
        return RANGE_CHART_SCHEMA
    col = sum(1 for r in results if _looks_columnar(r))
    if col >= (len(results) + 1) // 2:
        return COLUMNAR_SECTION_SCHEMA
    return RANGE_CHART_SCHEMA


def _empty_for(schema: MergeSchema, runs_n: int) -> dict[str, Any]:
    out: dict[str, Any] = {schema.primary_list_key: [], "runs": runs_n}
    for k in schema.list_keys:
        out[k] = []
    out[schema.confidence_field] = 0.0
    return out


def _mode_keys(d_items, keys):
    out = {}
    for k in keys:
        out[k] = _mode([d.get(k, "") for d in d_items])
    return out


def _merge_primary_list(runs, schema, n):
    groups = {}
    order = []
    for r in runs:
        items = r.get(schema.primary_list_key) or []
        for it in items:
            if not isinstance(it, dict):
                continue
            key = tuple(_norm(it.get(k)) for k in schema.primary_id_keys)
            if not any(key):
                continue
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(it)

    merged = []
    for key in order:
        group = groups[key]
        aggr = {"agreement_count": len(group), "agreement": f"{len(group)}/{n}"}
        # Mode-merge every scalar string/number field across runs. Structured
        # fields (list of dicts, e.g. columnar-section `lithology_blocks` /
        # `age_units` / `samples`) are merged via _merge_structured_field —
        # calling _mode() on them would either crash (lists are unhashable)
        # or silently drop data (`Counter` keys objects by identity).
        seen_keys = set()
        for g in group:
            for k, v in g.items():
                if k in aggr or k in ("agreement_count", "agreement"):
                    continue
                if v is None:
                    continue
                merged_v = _merge_field_across_runs([x.get(k) for x in group])
                if merged_v is _NO_MERGE:
                    continue
                aggr.setdefault(k, merged_v)
                seen_keys.add(k)
        # Schema-declared mode fields also get cleanly populated
        # (overrides setdefault above if needed).
        aggr.update(_mode_keys(group, schema.primary_str_mode_fields))
        merged.append(aggr)

    # Apply schema sort_keys (e.g. agreement_count desc, species asc).
    if schema.sort_keys:
        def sortkey(row):
            keys = []
            for field, direction in schema.sort_keys:
                v = row.get(field)
                # numeric field if possible, else str fallback
                num_v = None
                try:
                    num_v = float(v) if v is not None else None
                except (TypeError, ValueError):
                    pass
                if num_v is not None:
                    keys.append((0, num_v if direction == "asc" else -num_v))
                else:
                    s = (str(v) if v is not None else "").lower()
                    keys.append((1, s))
            return keys
        merged.sort(key=sortkey)
    else:
        merged.sort(key=lambda row: str(row.get("id") or row.get("species") or ""))
    return merged


def _merge_named_lists(runs, schema):
    out = {}
    for key in schema.list_keys:
        # Special case: list of plain strings (e.g. "other_fossils").
        # Union by lowercase-string dedup; preserve first-seen order.
        all_items = []
        for r in runs:
            all_items.extend(r.get(key) or [])
        if all_items and not any(isinstance(x, dict) for x in all_items):
            seen = set()
            merged_strs = []
            for s in all_items:
                t = str(s).strip()
                if t and t.lower() not in seen:
                    seen.add(t.lower())
                    merged_strs.append(t)
            out[key] = merged_strs
            continue

        groups = {}
        order = []
        for r in runs:
            items = r.get(key) or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                label = (
                    _norm(it.get("name"))
                    or _norm(it.get("marker"))
                    or _norm(it.get("meaning"))
                )
                if not label:
                    continue
                if label not in groups:
                    groups[label] = []
                    order.append(label)
                groups[label].append(it)
        merged = []
        for label in order:
            group = groups[label]
            # Mode-merge every string-valued field across runs to keep
            # all custom fields (e.g. "to_bed_idx") and not just a fixed set.
            rep = {}
            for g in group:
                for k, v in g.items():
                    if v is None:
                        continue
                    if k not in rep:
                        rep[k] = _mode([x.get(k) for x in group])
            if not rep and group:
                rep = dict(group[0])
            merged.append(rep)
        out[key] = merged
    return out


def merge_results(
    results,
    total_runs=None,
    schema=None,
):
    """Merge a list of normalized result dicts into one.

    Parameters
    ----------
    results : list[dict]
    total_runs : int | None
        Defaults to ``len(results)``. Pass to make the denominator reflect
        attempts when some runs failed.
    schema : MergeSchema | None
        Defaults to ``RANGE_CHART_SCHEMA`` when omitted for backward compat
        **but** when ``schema`` is omitted AND the inputs look columnar (a
        ``sections[0].id``-shaped dict in the majority of runs), we
        auto-detect ``COLUMNAR_SECTION_SCHEMA``. Pass an explicit
        ``schema=`` to override. (H5)
    """
    runs = [r for r in results if isinstance(r, dict)]
    n = total_runs if total_runs is not None else len(runs)
    if n <= 0:
        n = 1
    if schema is not None:
        sch = schema
    else:
        sch = _auto_detect_schema(runs)

    if not runs:
        return _empty_for(sch, n)

    if len(runs) == 1 and total_runs in (None, 1):
        single = dict(runs[0])
        items = single.get(sch.primary_list_key) or []
        new_items = []
        for it in items:
            it2 = dict(it)
            it2["agreement_count"] = 1
            it2["agreement"] = "1/1"
            new_items.append(it2)
        single[sch.primary_list_key] = new_items
        single.setdefault(sch.confidence_field, single.get(sch.confidence_field, 0.0))
        single["runs"] = 1
        return single

    out = {"runs": n}
    out[sch.primary_list_key] = _merge_primary_list(runs, sch, n)
    out.update(_merge_named_lists(runs, sch))

    confs = []
    for r in runs:
        try:
            confs.append(float(r.get(sch.confidence_field, 0.0)))
        except (TypeError, ValueError):
            pass
    out[sch.confidence_field] = round(sum(confs) / len(confs), 4) if confs else 0.0

    # Range-chart-only legacy: also merge "sections" so the original
    # four-table result shape (sections, species_ranges, biozones,
    # other_fossils) is preserved untouched.
    if sch.primary_list_key == "species_ranges":
        sec_groups = {}
        sec_order = []
        for r in runs:
            for sec in r.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                key = _norm(sec.get("name")) or "section"
                if key not in sec_groups:
                    sec_groups[key] = []
                    sec_order.append(key)
                sec_groups[key].append(sec)
        merged_sections = []
        for key in sec_order:
            group = sec_groups[key]
            forms = []
            for g in group:
                for f in g.get("formations") or []:
                    if f and f not in forms:
                        forms.append(f)
            merged_sections.append({
                "name": _mode([g.get("name", "") for g in group]),
                "age_range": _mode([g.get("age_range", "") for g in group]),
                "formations": forms,
                "formation_thickness_m": _mode(
                    [g.get("formation_thickness_m", "") for g in group]
                ),
                "coordinates": _mode([g.get("coordinates", "") for g in group]),
            })
        out["sections"] = merged_sections
        if "runs" not in out:
            out["runs"] = n

    return out


def merge_columnar_results(results, total_runs=None):
    return merge_results(
        results,
        total_runs=total_runs,
        schema=COLUMNAR_SECTION_SCHEMA,
    )
