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

import copy
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional


# P0-6: 10 common ICZN open-nomenclature markers.
# Long-pattern forms must appear before shorter sub-patterns (e.g. ex gr.
# before gr., s.str. before s., comb. nov. before nov.).
_QUALIFIER_PATTERNS = [
    (re.compile(r"\bex\s+gr(?:oup)?\.?\b", re.IGNORECASE), "ex gr."),
    (re.compile(r"\bs\.?\s*l\.?\b", re.IGNORECASE), "s.l."),
    (re.compile(r"\bs\.?\s*str\.?\b", re.IGNORECASE), "s.str."),
    (re.compile(r"\bsp\.?\b", re.IGNORECASE), "sp."),
    (re.compile(r"\bspp\.?\b", re.IGNORECASE), "spp."),
    (re.compile(r"\bcf\.?\s+", re.IGNORECASE), "cf."),
    (re.compile(r"\baff\.?\s+", re.IGNORECASE), "aff."),
    (re.compile(r"\?\s*$"), "?"),
    (re.compile(r"\bnom\.?\s+(dub|nud|nov|cons|obl|rej|van)\b", re.IGNORECASE), "nom. \\1"),
    (re.compile(r"\bcomb\.?\s+nov\.?\b", re.IGNORECASE), "comb. nov."),
    (re.compile(r"\bstat\.?\s+nov\.?\b", re.IGNORECASE), "stat. nov."),
    (re.compile(r"\bsubsp\.?\b", re.IGNORECASE), "subsp."),
    (re.compile(r"\bvar\.?\b", re.IGNORECASE), "var."),
]


def _extract_qualifiers(s: str) -> frozenset:
    """Return the set of open nomenclature qualifiers present in ``s``."""
    if not s:
        return frozenset()
    quals = set()
    for pattern, name in _QUALIFIER_PATTERNS:
        m = pattern.search(s)
        if not m:
            continue
        if '\\1' in name:
            # Pattern has a backreference placeholder: resolve it from m.group(1)
            quals.add(name.replace('\\1', m.group(1)))
        else:
            quals.add(name)
    return frozenset(quals)


def _norm(s):
    if not s:
        return ""
    t = str(s).strip()
    # B-1 fix: do NOT strip sp./cf./aff. here — that info is preserved by
    # _extract_qualifiers and carried into the dedup key as a separate
    # component so "Genus sp." and "Genus" are NOT merged together.
    # The original code stripped these suffixes, causing indeterminate
    # (sp.) / cf. specimens to be silently collapsed into the identified
    # species — a serious taxonomic data-integrity bug.
    t = re.sub(r"\s+", " ", t)
    return t.lower()


def _norm_iczn_author(s):
    """P1-2 (REVIEW-2026-07-25): ICZN-style author normalization for
    the species dedup key.

    Goal: "Smith, 1950", "(Smith, 1950)", "Smith 1950", and "Smith,1950"
    all refer to the same authorship and must dedup together. Year is
    preserved as a separate suffix so "Smith, 1950" and "Smith, 1960"
    remain distinct.

    We deliberately do NOT merge authors with the same surname but
    different initials ("J. Smith" vs "K. Smith") — that is the only safe
    behavior without a real ICZN authority database, and it preserves
    the existing "Smith, 1950" / "Smith, 1950" dedup signal.

    Returns the empty string for empty input.
    """
    if not s:
        return ""
    t = str(s).strip().lower()
    # Strip the year part first so we can normalize the author separately.
    year_match = re.search(r"(\d{4})", t)
    year = year_match.group(1) if year_match else ""
    # Remove the year from the working string.
    author = re.sub(r"\d{4}", "", t)
    # H-7 fix: handle em-dash (— or --) as author separator per ICZN Art. 51.2.
    # "Smith—Jones" should dedup with "Smith, Jones".
    author = re.sub(r"—+", " ", author)  # em-dash
    author = re.sub(r"--+", " ", author)  # double-dash
    # H-7 fix: handle "ex" / "in" references per ICZN Art. 51.2.
    # "Smith ex Jones" and "Smith in Jones" cite Jones as the original;
    # only the primary author(s) should be retained for dedup.
    # Strip "ex <person>" and "in <person>" constructs.
    author = re.sub(r"\s+ex\s+\S+(\s+\S+)*", "", author)
    author = re.sub(r"\s+in\s+\S+(\s+\S+)*", "", author)
    # Strip ICZN-style punctuation: commas, parentheses, ampersands,
    # multiple spaces, "et", "al.", "&".
    author = author.replace("&", " ").replace(" and ", " ")
    author = re.sub(r"[(),.;:'`\"]", " ", author)
    author = re.sub(r"\bet\.?\s+al\.?\b", "", author)  # "et al."
    author = re.sub(r"\s+", " ", author).strip()
    return f"{author}|{year}" if year else author


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


def _stable_typed_mode(values, expected_type):
    """Return a deterministic mode while preserving the requested type."""
    valid = [v for v in values if type(v) is expected_type]
    if not valid:
        return _NO_MERGE
    counts = Counter(valid)
    top = max(counts.values())
    tied = [value for value, count in counts.items() if count == top]
    return sorted(tied)[0]


def _merge_confidence(values):
    """Average valid per-row confidence values without treating missing as zero."""
    valid = []
    for value in values:
        if value is None or value == "" or isinstance(value, bool):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        valid.append(max(0.0, min(1.0, parsed)))
    if not valid:
        return _NO_MERGE
    return round(sum(valid) / len(valid), 4)


def _merge_mapping_field(values):
    """Merge dictionaries recursively while keeping structured data structured."""
    mappings = [value for value in values if isinstance(value, dict)]
    if not mappings:
        return _NO_MERGE
    keys = sorted({key for mapping in mappings for key in mapping})
    merged = {}
    for key in keys:
        merged_value = _merge_field_across_runs([mapping.get(key) for mapping in mappings])
        if merged_value is not _NO_MERGE:
            merged[key] = merged_value
    return merged


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
            # P1-11 fix: coerce all values to str before repr so that
            # {"a": 8} and {"a": "8"} produce the same signature and merge.
            try:
                sig = repr(sorted(((k, str(val)) for k, val in item.items())))
            except TypeError:
                # Fall back to bare repr for truly unhashable values.
                sig = repr(sorted(item.items()))
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
    # Dictionaries (notably _extras) are recursively merged and never
    # stringified. If a malformed run mixes a dict with another shape, keep
    # the structured observations and ignore the incompatible value.
    if any(isinstance(v, dict) for v in non_none):
        return _merge_mapping_field(values)
    # If every non-None value is a primitive → scalar merge.
    if non_none and all(isinstance(v, (str, int, float, bool)) for v in non_none):
        return _merge_scalar_field(values)
    # Unsupported mixed structures are omitted instead of leaking repr()/str()
    # artifacts into scientific data.
    return _NO_MERGE


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
    list_key_id_fields : dict[str, tuple[str, ...]]
        UI-REVIEW-2026-09-07: optional per-key business identity fields for
        named lists whose items have NO natural "name" field (e.g.
        zonation correlations keyed by both endpoints and their columns).
        When present, _merge_named_lists groups the key's items by these
        fields (majority-vote per field) instead of the content-signature
        fallback, so the same edge reported across runs with a differing
        free-text note merges into ONE row instead of duplicating.
    """

    primary_list_key: str
    primary_id_keys: list[str]
    primary_str_mode_fields: list[str] = field(default_factory=list)
    sort_keys: list[tuple[str, Any]] = field(default_factory=list)
    list_keys: list[str] = field(default_factory=list)
    confidence_field: str = "confidence"
    list_key_id_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


# Backward-compatible default for range-chart results.
RANGE_CHART_SCHEMA = MergeSchema(
    primary_list_key="species_ranges",
    primary_id_keys=["section", "species"],
    primary_str_mode_fields=[
        "species", "section", "range_base", "range_top", "biozone",
    ],
    sort_keys=[("agreement_count", "desc"), ("species", "asc")],
    list_keys=["sections", "biozones", "other_fossils"],
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

# Schema for phylogenetic-tree results. Nodes are the primary rows
# (deduped by ``id``); species names collapse by majority across runs.
# metadata and legend are single dicts (not lists) and must NOT be in
# list_keys — _merge_named_lists iterates dict keys via .extend() which
# would destroy their values. root_ids is a genuine list and is safe.
PHYLOGENETIC_TREE_SCHEMA = MergeSchema(
    primary_list_key="nodes",
    primary_id_keys=["id"],
    primary_str_mode_fields=["name"],
    sort_keys=[("agreement_count", "desc"), ("name", "asc")],
    list_keys=["root_ids"],
    confidence_field="confidence",
)


# Schema for zonation / biostratigraphic correlation chart results
# (radiolarian biochronology). Zone rows are the primary rows (deduped by
# their column (zonation) + name); correlations are a second deduped list
# keyed by both endpoints and their columns; zonation column descriptors
# dedupe by name. All string rows — the majority-vote machinery applies.
ZONATION_CHART_SCHEMA = MergeSchema(
    primary_list_key="zones",
    primary_id_keys=["zonation", "name"],
    primary_str_mode_fields=[
        "name", "zonation", "rank", "age_span", "base_age", "top_age",
        "stage", "defined_by", "note",
    ],
    sort_keys=[("agreement_count", "desc"), ("name", "asc")],
    list_keys=["zonations", "correlations"],
    confidence_field="confidence",
    # UI-REVIEW-2026-09-07: correlations have no "name" — without business
    # identity fields the content-signature fallback treated the same edge
    # with a differing free-text note across runs as two separate rows.
    list_key_id_fields={
        "correlations": ("from_zonation", "from_zone", "to_zonation", "to_zone"),
    },
)


SCHEMA_BY_MODE = {
    "range_chart": RANGE_CHART_SCHEMA,
    "columnar_section": COLUMNAR_SECTION_SCHEMA,
    "abundance_diagram": ABUNDANCE_DIAGRAM_SCHEMA,
    "phylogenetic_tree": PHYLOGENETIC_TREE_SCHEMA,
    "zonation_chart": ZONATION_CHART_SCHEMA,
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


def _looks_phylogenetic(data: Optional[dict]) -> bool:
    """REVIEW-2026-08-17 (P2): phylo detection.

    The phylo extractor emits ``nodes`` (primary rows deduped by id)
    plus a single ``metadata`` dict and a ``root_ids`` list. The shape
    is unique to phylo — without this detector, phylo data was silently
    merged as range_chart when no explicit ``schema=`` was passed."""
    if not data:
        return False
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    first = nodes[0]
    # M2 fix (REVIEW-2026-11-07): require BOTH id and parent so this
    # detector agrees with exporter._looks_phylogenetic_tree and the JS
    # mirror. The normalizer always writes both keys (parent=None for
    # roots), so normalized data is unaffected; only hand-crafted payloads
    # lacking ``parent`` change classification (range-chart fallback, the
    # same result the exporter would have given).
    return isinstance(first, dict) and "id" in first and "parent" in first


def _auto_detect_schema(results):
    """Pick the schema that matches the majority of inputs.

    Tie-breaking: when two shape-detectors both meet the threshold
    (or both fall short by the same margin), we apply a deterministic
    preference order — phylogenetic > columnar > abundance > range-chart.
    Phylogenetic is the most specific shape (nodes[].id plus a single
    metadata dict); misdetecting it as range-chart is the worst failure
    because it destroys the primary row key. The previous version had no
    phylo branch at all and silently demoted phylo to range-chart.
    """
    if not results:
        return RANGE_CHART_SCHEMA
    n = len(results)
    ab = sum(1 for r in results if _looks_abundance(r))
    col = sum(1 for r in results if _looks_columnar(r))
    phy = sum(1 for r in results if _looks_phylogenetic(r))
    half = (n + 1) // 2
    ab_passes = ab >= half
    col_passes = col >= half
    phy_passes = phy >= half
    if phy_passes:
        return PHYLOGENETIC_TREE_SCHEMA
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
    # Neither detector hit majority. Apply deterministic preference so
    # the result is the same across runs. Phylo beats both when at least
    # one run has it (handles the 1-run / 2-run edge case where no shape
    # meets majority but phylo is clearly present).
    if phy > col and phy > ab:
        return PHYLOGENETIC_TREE_SCHEMA
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


def _is_chimeric_row(group: list, merged: dict) -> bool:
    """P1-1 (REVIEW-2026-07-25): return True if the merged row's
    (range_base, range_top, biozone, section) tuple never appears in any
    single source run — i.e. the per-field mode vote produced a
    recombination that no individual run ever observed.

    P0-5 fix: include section in the chimera detection key. Same species
    in DIFFERENT sections is NOT a chimera — it's a legitimate multi-section
    observation. Without section in the key, two runs of the same species
    in two different sections would falsely look like a chimeric consensus.

    Scientific meaning: emitting such a row as if it were a real
    consensus is misleading. The merged tuple may still be internally
    consistent (base ≤ top, biozone plausible for that range), but it
    is not what the chart showed. Mark it so the caller can drop or
    surface it explicitly.
    """
    if len(group) < 2:
        return False
    # P0-5 fix: include section so same species across different sections
    # are NOT flagged as chimeras (they are legitimate multi-section obs).
    keys = ("range_base", "range_top", "biozone", "section")
    merged_tuple = tuple(_norm(merged.get(k, "")) for k in keys)
    if not any(merged_tuple):
        return False  # no scientific content to compare
    for g in group:
        if not isinstance(g, dict):
            continue
        item_tuple = tuple(_norm(g.get(k, "")) for k in keys)
        if item_tuple == merged_tuple:
            return False  # at least one source run observed this tuple
    return True


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
            # B-1 fix: include open-nomenclature qualifiers (sp./cf./aff./?)
            # in the dedup key so "Genus sp." and "Genus" stay separate.
            species_val = it.get("species", "") or ""
            id_norm = tuple(_norm(it.get(k)) for k in schema.primary_id_keys)
            # P1-2 (REVIEW-2026-07-25): for range-chart schema, also
            # include ICZN-normalized author_year in the dedup key so
            # "Smith, 1950", "(Smith, 1950)", "Smith 1950" merge together
            # but stay distinct from "Smith, 1960".
            if schema.primary_list_key == "species_ranges":
                id_norm = id_norm + (_norm_iczn_author(it.get("author_year", "")),)
            # Mirror JS: skip row only when ALL id fields are empty
            # (parts.some(p => p) — skip if no part is truthy)
            if not any(id_norm):
                continue
            key = (id_norm, _extract_qualifiers(species_val))
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
            if schema.primary_list_key == "species_ranges" and k in {
                "range_top_idx", "range_base_idx",
            }:
                merged_v = _stable_typed_mode(per_run_values, int)
            elif schema.primary_list_key == "species_ranges" and k == "confidence":
                merged_v = _merge_confidence(per_run_values)
            else:
                merged_v = _merge_field_across_runs(per_run_values)
            if merged_v is _NO_MERGE:
                continue
            aggr[k] = merged_v
        # Schema-declared mode fields also get cleanly populated
        # (overrides setdefault above if needed).
        aggr.update(_mode_keys(group, schema.primary_str_mode_fields))
        # B-1 fix: species mode was computed on _norm()-stripped values
        # (qualifiers lost). Restore the most-common original species string
        # that produced the mode, so "sp." / "cf." / "aff." are preserved.
        if "species" in schema.primary_str_mode_fields:
            species_mode = aggr.get("species") or ""
            if species_mode:
                # Count original species strings that normalised to the mode value
                # Sprint B (REVIEW-2026-09-04): compare _norm-to-_norm. The
                # old `_norm(sp_val) == species_mode` matched a normalised
                # value against the RAW mode, so any mode string that wasn't
                # already lowercase/single-spaced ("Genus  species" etc.)
                # matched nothing and this whole restoration block no-op'd.
                species_mode_norm = _norm(species_mode)
                species_counter: dict[str, int] = {}
                for g in group:
                    sp_val = (g.get("species") or "").strip()
                    if not sp_val:
                        continue
                    if _norm(sp_val) == species_mode_norm:
                        species_counter[sp_val] = species_counter.get(sp_val, 0) + 1
                if species_counter:
                    most_common_original = max(
                        species_counter,
                        key=species_counter.get,
                    )
                    quals = _extract_qualifiers(most_common_original)
                    if quals:
                        aggr["species"] = most_common_original

        # P1-1 (REVIEW-2026-07-25): bio-geological consistency gate.
        # If every contributing run produced a DIFFERENT (range_base,
        # range_top, biozone) tuple for this species — i.e. no run ever
        # observed the merged tuple — DROP this row instead of emitting a
        # chimeric "consensus" the data never supported.
        if _is_chimeric_row(group, aggr):
            # Still append, but mark it so downstream consumers can flag
            # it. The merge caller checks this flag and excludes from
            # final output via the `_drop_chimeras` toggle.
            aggr["_chimera_dropped"] = True
            merged.append(aggr)
            continue

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
                # UI-REVIEW-2026-09-07: business-identity fields for keys
                # whose items have no natural name (zonation correlations).
                # When configured, group by those fields (normalized, joined)
                # so the same edge across runs merges into one row with
                # majority-voted free-text fields. Items missing any id
                # field fall through to the content-signature path below.
                id_fields = schema.list_key_id_fields.get(key)
                if id_fields:
                    parts = [_norm(it.get(f)) for f in id_fields]
                    if all(parts):
                        label = "".join(parts)
                        if label not in groups:
                            groups[label] = []
                            order.append(label)
                        groups[label].append(it)
                        continue

                # B-1 fix: include qualifiers (sp./cf./aff./?) in the dedup
                # label so "N. optima Zone (cf.)" and "N. optima Zone" are not
                # silently collapsed.  Extract from whichever field is non-empty.
                _raw_label_src = (
                    it.get("name") or it.get("marker") or it.get("meaning") or ""
                )
                label = _norm(_raw_label_src)
                quals = _extract_qualifiers(_raw_label_src)
                if quals:
                    label = label + "\x1f" + "|".join(sorted(quals))
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
        # P2 fix (2026-08-06): deep-copy the single run instead of a
        # shallow ``dict(runs[0])`` — the shallow copy aliased every nested
        # structure (sections[].formations, _extras, lithology_blocks, ...)
        # with the caller's input run, so a downstream in-place mutation of
        # the merged result silently rewrote the source data.
        single = copy.deepcopy(runs[0])
        items = single.get(sch.primary_list_key) or []
        new_items = []
        for it in items:
            # P2 (2026-08-06) + REVIEW-2026-11-07 (low): `it` is already a
            # deep copy of the caller's row (root deep-copied above), so it
            # is NOT aliased with the input. Add the agreement fields in
            # place instead of a shallow ``dict(it)`` — the shallow copy's
            # nested values still shared objects with the (now orphaned)
            # deep copy, which was correct but easy to misread. Guard
            # non-dict rows so a malformed entry can't crash the merge.
            if isinstance(it, dict):
                it["agreement_count"] = 1
                it["agreement"] = "1/1"
            new_items.append(it)
        single[sch.primary_list_key] = new_items
        single.setdefault(sch.confidence_field, single.get(sch.confidence_field, 0.0))
        single["runs"] = 1
        return single

    out = {"runs": n}
    out[sch.primary_list_key] = _merge_primary_list(runs, sch, n)
    out.update(_merge_named_lists(runs, sch))

    # For phylogenetic tree, metadata and legend are single dicts (not
    # lists). They are identical across runs for the same image; preserve
    # from the first run to keep them in the merged output.
    if sch.primary_list_key == "nodes" and sch is not RANGE_CHART_SCHEMA:
        for key in ("metadata", "legend"):
            if runs and isinstance(runs[0], dict) and key in runs[0]:
                out[key] = runs[0][key]

    confs = []
    # M-1 fix (REVIEW-2026-07-25): merged confidence is a SIMPLE AVERAGE of
    # per-run confidences. The previous comment claimed consensus-rate
    # weighting (weight each run by the fraction of its primary rows reaching
    # row-level consensus), but the implementation used `w = int(r.get("runs")
    # or 1)`, which is always 1 for single-run results, so the math reduces to
    # a plain mean. Consensus-rate weighting would require row-level agreement
    # fractions that are not available on the merged `runs` objects, so we
    # keep the simple average and document it honestly rather than imply a
    # weighting that isn't computed. This is defensible: in multi-run mode each
    # run contributes one confidence, and averaging them is the unbiased merge
    # when no per-row consensus signal is present.
    weight_n = 0
    weight_sum = 0.0
    for r in runs:
        raw = r.get(sch.confidence_field)
        if raw is None:
            continue
        try:
            c = float(raw)
        except (TypeError, ValueError):
            continue
        # Each run is weighted uniformly (its own `runs` count is ~1 here, so
        # this is effectively a simple average across runs).
        try:
            w = int(r.get("runs") or 1)
        except (TypeError, ValueError):
            w = 1
        if w < 1:
            w = 1
        weight_sum += c * w
        weight_n += w
    if weight_n > 0:
        out[sch.confidence_field] = round(weight_sum / weight_n, 4)
    else:
        out[sch.confidence_field] = 0.0

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
    # P0-2 (REVIEW-2026-07-25) regression fix:
    # REMOVED the `elif sch.primary_list_key == "sections":` second-pass
    # columnar merge. Line 574 already calls _merge_primary_list (with
    # seen_in_run dedup + sort_keys applied) for ALL schemas including
    # columnar-section. The old elif block unconditionally OVERWROTE that
    # correct result with a second pass that:
    #   - lacked seen_in_run → agreement_count could exceed n (3/2 violation)
    #   - lacked sort_keys → result order diverged from the JS frontend
    #   - produced a divergent agreement string in the merged output
    # Columnar now relies solely on _merge_primary_list.

    # P1-1 (REVIEW-2026-07-25): filter chimeric rows from the final
    # output. Dropped rows are surfaced via ``chimera_warnings`` so the
    # UI can tell the operator "we dropped X rows because no run ever
    # observed the merged (FAD/LAD/biozone) tuple together".
    primary_key = sch.primary_list_key
    chimeras = [
        r for r in out.get(primary_key, [])
        if r.get("_chimera_dropped")
    ]
    if chimeras:
        out[primary_key] = [
            r for r in out.get(primary_key, [])
            if not r.get("_chimera_dropped")
        ]
        # Strip the internal marker from any rows that remain.
        for r in out.get(primary_key, []):
            r.pop("_chimera_dropped", None)
        if "chimera_warnings" not in out:
            out["chimera_warnings"] = []
        for c in chimeras:
            w = {
                "table": primary_key,
                "row": {k: c.get(k) for k in ("species", "section", "biozone",
                                              "range_top", "range_base")},
                "reason": "no single run observed the merged (FAD/LAD/biozone) tuple",
            }
            out["chimera_warnings"].append(w)
            c.pop("_chimera_dropped", None)

    return out


def merge_columnar_results(results, total_runs=None):
    return merge_results(
        results,
        total_runs=total_runs,
        schema=COLUMNAR_SECTION_SCHEMA,
    )
