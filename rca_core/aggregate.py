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
    top_vals = [v for v in counts if counts[v] == top]
    if len(top_vals) == 1:
        return top_vals[0]
    # H4: tie at the top count - covers BOTH all-unique (top==1) and partial
    # ties (2v2/3v3). Break deterministically by sorted order so input order
    # never silently decides the merged string across sessions. (Previously
    # only all-unique was sorted; partial ties fell to first-seen input order.)
    return sorted(top_vals, key=str)[0]


# Sentinel returned by _merge_field_across_runs to signal "no consensus"
# values are non-string scalars or all None — caller should drop the key.
_NO_MERGE = object()


def _merge_scalar_field(values):
    """_mode for plain str / int / float / bool values.

    Non-empty values are coerced to str so Counter works uniformly. None,
    empty strings, and structurally non-primitive values are skipped.
    Booleans are kept as-is (not stringified) so that mode of [F,F,F]
    returns the bool False, not the string 'False'.
    """
    strs: list[str] = []
    bools: list[bool] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            bools.append(v)
        elif isinstance(v, (str, int, float)):
            s = str(v).strip()
            if s:
                strs.append(s)
    # Prefer the mode of strings; fall back to bool mode if no strings.
    if strs:
        return _mode(strs)
    if bools:
        # Mode of bools, converted back to the original bool type.
        # Counter on bools gives the most-common bool; converting that
        # bool to str gives 'True' or 'False', but callers expect the
        # original type. We return the bool directly.
        from collections import Counter
        mode_bool = Counter(bools).most_common(1)[0][0]
        return mode_bool
    return _NO_MERGE


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

# Schema for abundance-diagram (pollen / percentage-diagram) results.
# Rows are all-string like range-chart, so the majority-vote machinery
# needs no new branch. The parallel "sites" and "zones" lists are merged
# as named lists (deduped by their "name" field).
ABUNDANCE_DIAGRAM_SCHEMA = MergeSchema(
    primary_list_key="abundances",
    primary_id_keys=["site", "taxon", "level"],
    primary_str_mode_fields=[
        "taxon", "site", "level", "depth", "abundance", "abundance_unit",
    ],
    sort_keys=[("agreement_count", "desc"), ("taxon", "asc")],
    list_keys=["sites", "zones"],
    confidence_field="confidence",
)


SCHEMA_BY_MODE = {
    "range_chart": RANGE_CHART_SCHEMA,
    "columnar_section": COLUMNAR_SECTION_SCHEMA,
    "abundance_diagram": ABUNDANCE_DIAGRAM_SCHEMA,
}


def _looks_abundance(data: Optional[dict]) -> bool:
    """Mirror of rca_core.exporter._looks_abundance. An abundance-diagram
    result carries a non-empty ``abundances`` list (unique to that mode).
    Empty list is ignored because it can be an uninitialized placeholder."""
    if not data:
        return False
    ab = data.get("abundances")
    return isinstance(ab, list) and len(ab) > 0


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
    return isinstance(first, dict) and ("id" in first)


def _auto_detect_schema(results):
    """Pick the schema that matches the majority of inputs.

    Tie-breaking: when two shape-detectors both meet the threshold
    (or both fall short by the same margin), we apply a deterministic
    preference order — columnar > abundance > range-chart. Columnar is
    more specific than abundance (its `sections[].id` field is unique
    to that mode), so misdetecting it as range-chart is a worse failure
    than misdetecting abundance as range-chart. The old version fell
    through to RANGE_CHART for any tie, which silently lost columnar
    data when 2 runs split evenly.
    """
    if not results:
        return RANGE_CHART_SCHEMA
    n = len(results)
    ab = sum(1 for r in results if _looks_abundance(r))
    col = sum(1 for r in results if _looks_columnar(r))
    half = (n + 1) // 2
    ab_passes = ab >= half
    col_passes = col >= half
    if col_passes and ab_passes:
        # Both detectors agree there's a majority — prefer the more
        # specific one. Counts break the tie if they disagree.
        if col > ab:
            return COLUMNAR_SECTION_SCHEMA
        if ab > col:
            return ABUNDANCE_DIAGRAM_SCHEMA
        return COLUMNAR_SECTION_SCHEMA  # tie → columnar (more specific)
    if col_passes:
        return COLUMNAR_SECTION_SCHEMA
    if ab_passes:
        return ABUNDANCE_DIAGRAM_SCHEMA
    # Neither detector hit majority. If they tie on counts, still apply
    # the same preference so the result is deterministic across runs.
    if col > ab:
        return COLUMNAR_SECTION_SCHEMA
    if ab > col:
        return ABUNDANCE_DIAGRAM_SCHEMA
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
        # Per-run dedup: if a single run emits the same primary row twice
        # (model hiccup), count it once so agreement_count can't exceed n
        # (e.g. "3/2"), which would break consensus filters expecting
        # agreement_count <= total runs.
        seen_in_run = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            key = tuple(_norm(it.get(k)) for k in schema.primary_id_keys)
            if not any(key):
                continue
            if key in seen_in_run:
                continue
            seen_in_run.add(key)
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
        #
        # BUGFIX: collect (key, values) pairs across the group, then merge
        # each key once. The previous loop called setdefault on every
        # iteration, so whichever group item ran first "won" — a key that
        # was None in the first run but present in later runs was silently
        # dropped. Iterating outside-in guarantees every value across the
        # whole group participates in the merge.
        fields_to_merge: dict[str, list] = {}
        for g in group:
            if not isinstance(g, dict):
                continue
            for k, v in g.items():
                if k in aggr or k in ("agreement_count", "agreement"):
                    continue
                if v is None:
                    continue
                fields_to_merge.setdefault(k, []).append(v)
        for k, vals in fields_to_merge.items():
            per_run_values = [gi.get(k) for gi in group]
            merged_v = _merge_field_across_runs(per_run_values)
            if merged_v is _NO_MERGE:
                continue
            aggr[k] = merged_v
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
                    # No name/marker/meaning field (e.g. abundance-diagram
                    # single-site with empty name, or columnar cross_beds
                    # which key on from/to bed indices). Fall back to a
                    # content signature so identical items across runs still
                    # collapse to one and distinct items are preserved,
                    # instead of being silently dropped.
                    try:
                        label = "__nolabel__:" + repr(sorted(
                            (k, "" if v is None else str(v)) for k, v in it.items()
                        ))
                    except TypeError:
                        label = "__nolabel__:" + repr(sorted(it.keys()))
                # If the item carries a `section` field (biozones, after the
                # prompt/extractor schema fix), fold it into the dedup label
                # so the same biozone measured in different sections isn't
                # collapsed into one row - which would silently lose the
                # second section's thickness. Items without a section field
                # (other_fossils, legends, cross_beds) are unaffected.
                section = _norm(it.get("section"))
                if section and label and not label.startswith("__nolabel__"):
                    label = f"{label}@@{section}"
                if label not in groups:
                    groups[label] = []
                    order.append(label)
                groups[label].append(it)
        merged = []
        for label in order:
            group = groups[label]
            # Collect (key, per-run values) across the whole group, then merge
            # each key ONCE via the safe dispatcher _merge_field_across_runs.
            # The previous code called raw _mode([x.get(k) for x in group]) on
            # every field, which crashes (TypeError: unhashable type 'dict')
            # when a field holds a dict/list - notably the `_extras` bucket the
            # extractor deliberately preserves on every named-list item. The
            # primary-list path was already fixed this way; this mirrors it.
            rep = {}
            fields_to_merge: dict[str, list] = {}
            for g in group:
                if not isinstance(g, dict):
                    continue
                for k, v in g.items():
                    if v is None:
                        continue
                    fields_to_merge.setdefault(k, []).append(v)
            for k in fields_to_merge:
                per_run_values = [gi.get(k) for gi in group]
                merged_v = _merge_field_across_runs(per_run_values)
                if merged_v is _NO_MERGE:
                    continue
                rep[k] = merged_v
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

    # Merge "sections" for both range-chart and columnar-section schemas.
    # For range-chart: group by section name and mode-merge scalar fields.
    # For columnar-section: group by (id, group) and mode-merge all fields.
    if sch.primary_list_key == "species_ranges":
        # Range-chart sections: keyed by name, formations list-merged.
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
            # Mirror the primary-list merge: include agreement_count +
            # agreement on every section so the JS / Python renderers can
            # flag low-agreement rows. Without these fields, sections
            # filtering by cross-run consensus isn't possible — and the
            # asymmetry with species (which DOES expose agreement) is
            # confusing for users running multi-run extraction.
            merged_sections.append({
                "name": _mode([g.get("name", "") for g in group]),
                "age_range": _mode([g.get("age_range", "") for g in group]),
                "formations": forms,
                "formation_thickness_m": _mode(
                    [g.get("formation_thickness_m", "") for g in group]
                ),
                "coordinates": _mode([g.get("coordinates", "") for g in group]),
                "agreement_count": len(group),
                "agreement": f"{len(group)}/{n}",
            })
        out["sections"] = merged_sections
        if "runs" not in out:
            out["runs"] = n
    elif sch.primary_list_key == "sections":
        # Columnar-section sections: keyed by (id, group), mode-merge all fields.
        sec_groups: dict[tuple, list] = {}
        sec_order = []
        for r in runs:
            for sec in r.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                key = (_norm(sec.get("id", "")), _norm(sec.get("group", "")))
                if not any(key):
                    continue
                if key not in sec_groups:
                    sec_groups[key] = []
                    sec_order.append(key)
                sec_groups[key].append(sec)
        merged_sections = []
        for key in sec_order:
            group = sec_groups[key]
            aggr: dict[str, Any] = {"agreement_count": len(group), "agreement": f"{len(group)}/{n}"}
            # BUGFIX: same setdefault-first-iteration-wins issue as in
            # _merge_primary_list. Collect (k, values) across the group,
            # then merge each key once.
            fields_to_merge: dict[str, list] = {}
            for g in group:
                if not isinstance(g, dict):
                    continue
                for k, v in g.items():
                    if k in aggr or k in ("agreement_count", "agreement"):
                        continue
                    if v is None:
                        continue
                    fields_to_merge.setdefault(k, []).append(v)
            for k, _vals in fields_to_merge.items():
                # MEDIUM-3: renamed from per_run_values — "group" here already
                # contains one entry per run, so this is the per-group-item value list.
                group_item_values = [gi.get(k) for gi in group]
                merged_v = _merge_field_across_runs(group_item_values)
                if merged_v is _NO_MERGE:
                    continue
                aggr[k] = merged_v
            aggr.update(_mode_keys(group, sch.primary_str_mode_fields))
            merged_sections.append(aggr)
        out["sections"] = merged_sections

    return out


def merge_columnar_results(results, total_runs=None):
    return merge_results(
        results,
        total_runs=total_runs,
        schema=COLUMNAR_SECTION_SCHEMA,
    )
