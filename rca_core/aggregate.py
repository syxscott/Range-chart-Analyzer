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
from functools import cmp_to_key
from typing import Any, Optional

# BORROW-2026-09-20 (A): the coverage contract has its own merge semantics —
# codes union, response kind resolves by precedence — so the merge layer reads
# them from the single source of truth instead of reimplementing them.
from .reason_codes import merge_reason_codes, merge_response_kinds


# ICZN open-nomenclature markers (P0-6).
# Long-pattern forms must appear before shorter sub-patterns (e.g. ex gr.
# before gr., s.str. before s. — the ordering only matters for the labels
# this list produces, not for correctness of the match).
_QUALIFIER_PATTERNS = [
    (re.compile(r"\bex\s+gr(?:oup)?\.?\b", re.IGNORECASE), "ex gr."),
    (re.compile(r"\bs\.?\s*l\.?\b", re.IGNORECASE), "s.l."),
    (re.compile(r"\bs\.?\s*str\.?\b", re.IGNORECASE), "s.str."),
    (re.compile(r"\bsp\.?\b", re.IGNORECASE), "sp."),
    (re.compile(r"\bspp\.?\b", re.IGNORECASE), "spp."),
    # REVIEW-2026-09-20: cf./aff. required TRAILING WHITESPACE (`\s+`), so the
    # very common trailing-suffix forms "Genus cf." / "Genus aff." — nothing
    # after the marker — never matched and the specimen was deduped together
    # with the identified "Genus". `\b` after the optional dot matches at an
    # end of string as well, and still rejects look-alikes ("coffee",
    # "affinis") because the dot is optional on both sides of the boundary.
    (re.compile(r"\bcf\.?\b", re.IGNORECASE), "cf."),
    (re.compile(r"\baff\.?\b", re.IGNORECASE), "aff."),
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
    # Open-nomenclature markers (sp./cf./aff./…) are deliberately NOT stripped
    # here: _extract_qualifiers carries them as a separate component of the
    # dedup key so an indeterminate "Genus sp." never collapses into the
    # identified "Genus".
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


def _str_for_merge(v: Any) -> str:
    """Stringify a scalar for the merge string pool.

    REVIEW-2026-09-10: JSON has no int/float distinction, so JavaScript's
    ``String(1.0)`` is "1" while Python's ``str(1.0)`` is "1.0" - the two
    engines merged the SAME numeric value to different strings, and the
    merged value lands in CSV/XLSX/JSON as text. Integral floats are
    therefore rendered without the trailing ".0" so both sides agree.
    Common values (0/1 confidence, 0-length branches, full support) are
    affected, which is why this is worth normalising.
    """
    if isinstance(v, bool):  # bool is an int subclass - keep "True"/"False"
        return "True" if v else "False"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _mode(values):
    # REVIEW-2026-09-10: skip structurally non-scalar values (dict / list /
    # set / tuple). They are unhashable, so Counter raised
    # `TypeError: unhashable type` and aborted the ENTIRE merge for any
    # caller whose payload put a container in a field the schema declares as a
    # string-mode field. The JS mirror keys a Map by reference and never
    # throws, and a single unrepresentable field must not take the whole
    # merged result down.
    non_empty = []
    for v in values:
        if isinstance(v, (dict, list, set, tuple)):
            continue
        # REVIEW-2026-09-20: the filter used to be ``if v and str(v).strip()``,
        # which dropped every FALSY-BUT-REAL observation: 0, 0.0 and False are
        # legitimate values (0 % abundance, an empty part of the diagram,
        # ``reworked: False``). With votes [0, 0, 12] the two zeros were
        # discarded and the merge answered 12; with [0, 0] it answered '' —
        # i.e. real data turned into "nothing recorded". Only ``None`` and
        # blank/whitespace strings count as missing.
        if v is None or not str(v).strip():
            continue
        non_empty.append(v)
    if not non_empty:
        return ""
    # REVIEW-2026-09-20: vote on the SAME normalized string pool
    # ``_merge_scalar_field`` uses (``_str_for_merge``), so 1 and 1.0 are one
    # and the same candidate on every code path instead of two votes that
    # Counter happens to fold together in run order. The representative value
    # keeps the field's original type (an int index stays an int).
    counts: Counter = Counter()
    representative: dict[str, Any] = {}
    for v in non_empty:
        key = _str_for_merge(v)
        counts[key] += 1
        representative.setdefault(key, v)
    top = max(counts.values())
    top_keys = [k for k, c in counts.items() if c == top]
    if len(top_keys) == 1:
        return representative[top_keys[0]]
    # H4: tie at the top count - covers BOTH all-unique (top==1) and partial
    # ties (2v2/3v3). Break deterministically by sorted order so input order
    # never silently decides the merged string across sessions. (Previously
    # only all-unique was sorted; partial ties fell to first-seen input order.)
    return representative[sorted(top_keys)[0]]


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
            s = _str_for_merge(v).strip()
            if s:
                strs.append(s)
    # Prefer the mode of strings; fall back to bool mode if no strings.
    if strs:
        return _mode(strs)
    if bools:
        # Mode of bools, converted back to the original bool type: the mode of
        # [F,F,F] must be the bool False, not the string 'False'.
        #
        # REVIEW-2026-09-10: this used Counter.most_common(1)[0][0], which
        # breaks ties by INSERTION ORDER - so [True, False] merged to True and
        # [False, True] to False, i.e. the same set of runs produced a
        # different merged flag depending on which run came first. That is the
        # exact non-determinism the H4 fix removed from the string path
        # ("Break deterministically by sorted order so input order never
        # silently decides the merged string across sessions"). Sort the tied
        # values instead (False < True), which is also what the JS mirror
        # does and what its test asserts.
        counts = Counter(bools)
        top = max(counts.values())
        return sorted(v for v in counts if counts[v] == top)[0]
    return _NO_MERGE


def _stable_typed_mode(values, expected_type):
    """Return a deterministic mode while preserving the requested type.

    REVIEW-2026-09-20: the filter used to be ``type(v) is expected_type``, so
    an integral float vote — which is exactly what JSON hands back for "3.0"
    after ``json.loads`` — was silently discarded for ``range_top_idx`` /
    ``range_base_idx``. With runs [3, 3.0, 3] only two votes survived, and
    with [3.0, 3.0] the field disappeared from the merged row entirely while
    the string endpoint kept its value. The JS mirror
    (``js/aggregate.mergeTypedInteger``) accepts any ``Number.isInteger``
    value, so integral floats are folded into the int pool here too and
    re-cast to int so the merged row keeps a stable type. ``bool`` is an int
    subclass in Python but is NOT a number in JS, so it stays excluded.
    """
    valid: list[Any] = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, expected_type):
            valid.append(v)
        elif (expected_type is int and isinstance(v, float)
              and v.is_integer()):
            valid.append(int(v))
    if not valid:
        return _NO_MERGE
    counts = Counter(valid)
    top = max(counts.values())
    tied = [value for value, count in counts.items() if count == top]
    return sorted(tied)[0]


def _add_row_warning(row: dict, flag: str) -> None:
    """Append a merge-stage warning to a row using the extractor's convention.

    ``rca_core/extractor.py`` writes ``row["_warning"]`` as a single string
    when there is one flag and as a list when there are several; downstream
    consumers (server, GUI, js/table.js) accept both shapes. Merged rows must
    not invent a third shape, so the existing value is normalised to a list
    before the new flag is appended.
    """
    existing = row.get("_warning")
    if existing in (None, ""):
        row["_warning"] = flag
        return
    flags = list(existing) if isinstance(existing, list) else [existing]
    if flag not in flags:
        flags.append(flag)
    row["_warning"] = flags[0] if len(flags) == 1 else flags


def _merge_row_warnings(target: dict, values: list) -> None:
    """Union every run's ``_warning`` into ``target``.

    FIX-2026-09-22 (audit item 2). Warnings are independent facts, not
    alternative measurements of one field, so the generic scalar merger is the
    wrong tool for ``_warning`` in two ways:

    * it kept exactly ONE run's flag and deleted the others' — and because the
      contract merge writes ``response_kind_divergent`` into the row while the
      loop runs, whether the source row's own flag or the merge-stage flag
      survived depended on key order;
    * a row that already carried several flags arrives as a list, which the
      mode merger cannot hash, so the whole warning was dropped.

    Every flag from the existing target value and from every run therefore
    survives, in first-seen order, de-duplicated, re-written with the
    single-string / list convention of :func:`_add_row_warning`.
    ``js/aggregate.js#rcaMergeRowWarnings`` mirrors this.
    """
    flags: list[str] = []

    def _push(value):
        if value in (None, ""):
            return
        parts = value if isinstance(value, list) else [value]
        for part in parts:
            if isinstance(part, str) and part and part not in flags:
                flags.append(part)

    _push(target.get("_warning"))
    for value in values or []:
        _push(value)
    if not flags:
        return
    target["_warning"] = flags[0] if len(flags) == 1 else flags


def _merge_contract_field(key: str, values: list, target: dict) -> bool:
    """Merge one coverage/geometry field across runs into ``target``.

    Returns True when ``key`` belongs to the BORROW-2026-09-20 contract and was
    handled here — the callers then skip their generic field merger, which
    would otherwise drop a list of strings (``_NO_MERGE``) or stringify the
    integers inside ``geometry``.

    Contract semantics, shared by every merge path so the two can't drift:

    * ``reason_codes`` — UNION, in first-seen order. Two runs each spotting a
      different defect means both defects are true.
    * ``response_kind`` — precedence vote, extracted > uncertain > not_drawn:
      one run that saw the range drawn outranks a run that called it a dash.
      Divergence is recorded as a warning plus the raw ballot, never hidden.
    * ``geometry`` — first well-formed block wins, deep-copied. Averaging two
      runs' pixel reads would invent a position neither run observed.
    """
    if key == "reason_codes":
        union = merge_reason_codes(values)
        if union:
            target[key] = union
        return True
    if key == "response_kind":
        kind, divergent = merge_response_kinds(values)
        if kind:
            target[key] = kind
        if divergent:
            _add_row_warning(target, "response_kind_divergent")
            target["response_kind_votes"] = [v for v in values if v]
        return True
    if key == "geometry":
        block = next((v for v in values
                      if isinstance(v, dict) and v.get("points")), None)
        if block is not None:
            target[key] = copy.deepcopy(block)
        return True
    return False


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

# REVIEW-2026-09-10: modes that are wired through the FULL stack (prompt +
# normalizer + merge schema + quality + exporter tables + entry points). The
# extractor implements three more (chemical_stratigraphy / paleomap /
# scatter_plot) but they have no MergeSchema and no exporter tables, so they
# MUST NOT reach merge_results - SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)
# would silently substitute the range-chart schema and drop every
# mode-specific row (continents, fossil_sites, data_points) while still
# returning ok=True. server.py already rejects them when explicitly
# requested; this constant exists so the "auto" path can apply the same rule
# after vision classification resolves a mode the caller never named.
WIRED_MODES = frozenset(SCHEMA_BY_MODE)


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

    REVIEW-2026-09-20: the threshold and the preference order used to be
    inconsistent in two ways:

    * ``half = (n + 1) // 2`` labelled a bare 1 vote out of 2 a "majority", so
      every branch below the phylo check was reachable by a minority.
    * The fallback compared detectors pairwise (``phy > col and phy > ab``,
      then ``col > ab``), which is NOT a total order: ``[phylo, columnar]``
      returned phylo, but adding one more run of any other shape
      (``[phylo, columnar, abundance]``) made all three counts equal, the
      strict ``>`` comparisons all failed, and the answer became RANGE_CHART —
      the phylo preference vanished as more data arrived.

    Both problems are gone by using one rule twice: a STRICT majority
    (``count * 2 > n``) wins, and whenever no shape holds one — or several do,
    since the detectors are independent — the declared preference order
    phylogenetic > columnar > abundance breaks the tie. That order is now the
    only thing deciding equal counts, so the choice is monotonic and cannot
    depend on the argument order of ``results``. Phylogenetic stays first
    because it is the most specific shape (``nodes[].id`` plus a single
    ``metadata`` dict); misdetecting it as range-chart is the worst failure
    because it destroys the primary row key.
    """
    if not results:
        return RANGE_CHART_SCHEMA
    n = len(results)
    counts = [
        (PHYLOGENETIC_TREE_SCHEMA, sum(1 for r in results if _looks_phylogenetic(r))),
        (COLUMNAR_SECTION_SCHEMA, sum(1 for r in results if _looks_columnar(r))),
        (ABUNDANCE_DIAGRAM_SCHEMA, sum(1 for r in results if _looks_abundance(r))),
    ]
    # Declared preference order, filtered to shapes that were seen at all.
    ranked = [(sch, c) for sch, c in counts if c > 0]
    if not ranked:
        return RANGE_CHART_SCHEMA
    # A strict majority decides; among several majorities the preference order
    # does. Without any majority, fall back to the highest count, again with
    # the same order breaking ties.
    majority = [(sch, c) for sch, c in ranked if c * 2 > n]
    pool = majority or ranked
    best = max(c for _s, c in pool)
    for sch, c in pool:  # `ranked` is already in preference order
        if c == best:
            return sch
    return RANGE_CHART_SCHEMA  # unreachable, kept as a defensive default


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
    """Majority-vote the schema-declared fields of a merged row.

    REVIEW-2026-09-20: this path now shares ``_merge_scalar_field``'s caliber
    by construction: both vote through ``_mode``, which (a) only treats
    ``None``/blank strings as missing — 0, 0.0 and False are real observations
    again — and (b) buckets votes by ``_str_for_merge`` so 1 and 1.0 are one
    candidate instead of two order-dependent ones. What is deliberately kept
    is the RAW representative value (int 0, not "0"): the JS mirror
    (``rcaAggMode`` over ``strModeFields``) returns the original value too,
    and these fields land in CSV/XLSX/JSON, so stringifying here would be a
    fresh Python↔JS divergence rather than a fix.

    Like JS, the declared fields win over the generic per-key merge that ran
    before them (JS skips those keys with ``if (aggr[k] !== undefined)
    continue``), so a schema-declared field is resolved in exactly one place.
    """
    out = {}
    for k in keys:
        out[k] = _mode([d.get(k) for d in d_items if isinstance(d, dict)])
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
            # Mirror JS: skip a row only when ALL id fields are empty
            # (parts.some(p => p) — skip if no part is truthy).
            #
            # REVIEW-2026-09-10: this test must run BEFORE the author is
            # folded into the key. It used to run after, so a row with an
            # empty species AND empty section but a non-empty author_year
            # ("Smith 1950") counted as identified and survived - the server/
            # GUI/CSV emitted a species row with no species name, while the
            # browser (which appends the author after its own guard) dropped
            # it. Same runs, different row sets per endpoint.
            if not any(id_norm):
                continue
            # P1-2 (REVIEW-2026-07-25): for range-chart schema, also
            # include ICZN-normalized author_year in the dedup key so
            # "Smith, 1950", "(Smith, 1950)", "Smith 1950" merge together
            # but stay distinct from "Smith, 1960".
            if schema.primary_list_key == "species_ranges":
                id_norm = id_norm + (_norm_iczn_author(it.get("author_year", "")),)
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
            # BORROW-2026-09-20 (A+B): coverage contract + geometry sidecar are
            # merged by their own rules, never by the generic field merger.
            if _merge_contract_field(k, per_run_values, aggr):
                continue
            if k == "_warning":
                # FIX-2026-09-22 (audit item 2): union, never overwrite.
                _merge_row_warnings(aggr, per_run_values)
                continue
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

        # REVIEW-2026-09-20: the two bed indices are voted independently, so a
        # run-A base and a run-B top can recombine into an inverted pair even
        # though every source row was sane (the extractor already swaps
        # inverted pairs per row). An inverted pair exports as a species that
        # dies before it appears, and quality.py reads the *string* endpoints,
        # so nothing else catches it. Repair the order and flag it with the
        # same ``index_order_swap`` marker the extractor uses.
        if schema.primary_list_key == "species_ranges":
            top_idx = aggr.get("range_top_idx")
            base_idx = aggr.get("range_base_idx")
            if (isinstance(top_idx, (int, float)) and not isinstance(top_idx, bool)
                    and isinstance(base_idx, (int, float))
                    and not isinstance(base_idx, bool)
                    and base_idx > top_idx):
                aggr["range_top_idx"], aggr["range_base_idx"] = base_idx, top_idx
                _add_row_warning(aggr, "index_order_swap")

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
        # REVIEW-2026-09-20 (1): the old key function negated only the NUMERIC
        # branch, so a declared string sort with direction "desc" silently
        # sorted ascending. The JS mirror compares with a single `cmp` and
        # applies `-cmp` for "desc" regardless of type, so both types now
        # honour the direction.
        # REVIEW-2026-09-20 (2): rows tying on every declared key kept their
        # merge order, which is first-seen-run order — the same set of runs
        # therefore produced different row orders across sessions. The
        # schema's primary id fields are appended as ascending tiebreakers so
        # the order is a pure function of the merged data.
        tiebreakers = [
            (k, "asc") for k in schema.primary_id_keys
            if k not in {f for f, _d in schema.sort_keys}
        ]
        effective_keys = list(schema.sort_keys) + tiebreakers

        def _sort_tuple(row, field):
            """(rank, number, lowered, raw) — rank keeps numbers before str."""
            v = row.get(field)
            try:
                num_v = float(v) if v is not None else None
            except (TypeError, ValueError):
                num_v = None
            if num_v is not None and not isinstance(v, (dict, list)):
                return (0, num_v, "", "")
            s = "" if v is None else str(v)
            return (1, 0.0, s.lower(), s)

        def _compare(a, b):
            for fld, direction in effective_keys:
                ta, tb = _sort_tuple(a, fld), _sort_tuple(b, fld)
                if ta != tb:
                    c = -1 if ta < tb else 1
                    return -c if direction == "desc" else c
            return 0

        merged.sort(key=cmp_to_key(_compare))
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
                # BORROW-2026-09-20 (A+B): same contract rules as the primary
                # path, so a biozone / site / point row merges identically.
                if _merge_contract_field(k, per_run_values, rep):
                    continue
                if k == "_warning":
                    # FIX-2026-09-22 (audit item 2): union, never overwrite.
                    _merge_row_warnings(rep, per_run_values)
                    continue
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

    # REVIEW-2026-09-10: root-level _extras / _warnings carry figure-level
    # data the extractor deliberately preserves (captions, notes, degradation
    # warnings). The single-run path keeps them, but the N-run path built
    # `out` only from the primary list + list_keys + confidence, so raising
    # the run count silently dropped them. Preserve from the first run that
    # has them, deep-copied so later mutations cannot rewrite the source run.
    for key in ("_extras", "_warnings"):
        for run in runs:
            if isinstance(run, dict) and run.get(key):
                out[key] = copy.deepcopy(run[key])
                break

    # For phylogenetic tree, metadata and legend are single dicts (not
    # lists). They are usually identical across runs for the same image;
    # preserve them so the merged output keeps the figure-level context.
    if sch.primary_list_key == "nodes" and sch is not RANGE_CHART_SCHEMA:
        for key in ("metadata", "legend"):
            # REVIEW-2026-09-20: this used to read ``runs[0]`` only, so when
            # the first run answered without a metadata/legend block (a common
            # degradation) the blocks observed by the OTHER runs were dropped
            # even though the single-run path keeps them — raising the run
            # count lost data. Scan every run, same rule as the `_extras` /
            # `_warnings` loop above: first run that has the key wins.
            for run in runs:
                if isinstance(run, dict) and key in run:
                    # REVIEW-2026-09-10: deep-copy, not alias. The JS mirror
                    # does (deepClone) and the single-run path above already
                    # does; the bare alias let a caller's in-place edit of the
                    # merged result rewrite the source run, breaking audit
                    # integrity.
                    out[key] = copy.deepcopy(run[key])
                    break

    # Weighted-mean confidence (M-1 / REVIEW-2026-09-20 comment correction).
    # The claim that this is a "simple average" was wrong: each run is
    # weighted by its OWN ``runs`` field, so a run that itself already
    # aggregated k sub-attempts contributes k times. When every run reports
    # ``runs`` = 1 (the normal case for freshly extracted runs, which is why
    # the old comment read as true) the formula reduces to a plain mean;
    # with differing ``runs`` values it does NOT, and both engines are pinned
    # to the weighted result by tests/test_confidence_weighted_parity.py
    # (0.9 with runs=3 plus 0.5 with runs=1 → 0.8, not 0.7). A missing /
    # blank / unparsable ``runs`` falls back to weight 1. Consensus-rate
    # weighting is still NOT computed — that would need row-level agreement
    # fractions which are not available on the run objects.
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
