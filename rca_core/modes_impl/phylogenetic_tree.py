"""Mode normalizer: phylogenetic_tree.

Phase 3 / P1-4: bodies MOVED VERBATIM out of ``rca_core/extractor.py``.
Nothing was rewritten. Shared helpers come from ``modes_impl._shared`` (a
leaf module); ``extractor.py`` re-exports these names at the end so every
existing import path and monkeypatch target is unchanged.
"""
from __future__ import annotations
from ._shared import (
    _coerce_bool_flag,
    _confidence_clamped,
    _dict_rows,
    _pop_array_root_extras,
    _stringify_scalar,
)
from typing import Any
import re

def _normalize_phylogenetic_tree_into(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a phylogenetic tree response.

    Schema:
      {
        "metadata": {
          "title": str,
          "extraction_timestamp": str,  # ISO 8601
          "tree_type": str,  # "cladogram" | "phylogram" | "dendrogram"
          "scale": str,  # e.g. "Ma" / "substitutions/site"
          "rooted": bool,
          "source": str
        },
        "root_ids": list[str],
        "nodes": [{
          "id": str,
          "parent": str | None,  # null for roots
          "name": str,           # taxon or internal node label
          "is_leaf": bool,
          "branch_length": float | None,
          "node_age_ma": float | None,  # optional, for time-calibrated trees
          "support": float | None,      # bootstrap 0..100
          "metadata": dict  # any extra fields (preserved)
        }],
        "confidence": float
      }

    Invariants enforced:
      - root_ids is non-empty
      - Every non-root node's parent is in node ids
      - Each root's parent is None (not just the first)
      - is_leaf == (children_count == 0) by reverse check
      - support in [0, 100] or None
    """
    # H3-fix pattern: unwrap _array_root wrapper (top-level array from
    # safe_json_loads wrapped as {_array_root:[...]}).
    #
    # REVIEW-2026-09-10: the old form REPLACED `raw` with the inner item, so
    # every outer field the model emitted alongside the array was discarded -
    # e.g. {"_array_root":[{nodes...}], "confidence": 0.9} normalized to
    # confidence 0.0. The wrapper's own keys now fill only the gaps the inner
    # object does not define (setdefault), matching the in-place style the
    # abundance/columnar unwraps already use.
    if "_array_root" in raw and isinstance(raw["_array_root"], list):
        for item in raw["_array_root"]:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("nodes"), list):
                merged = dict(item)
                for key, value in raw.items():
                    if key in ("_array_root", "_note"):
                        continue
                    merged.setdefault(key, value)
                raw = merged
                break

    # REVIEW-2026-09-10: collected repairs to surface on the result.
    phylo_warnings: list[str] = []

    def s(v):
        # Container-safe: a dict/list in a scalar field collapses to "" rather
        # than leaking a Python repr into a scientific field.
        return _stringify_scalar(v)

    def fv(v):
        """Coerce to float or None."""
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # REVIEW-2026-09-10: `_dict_rows` accepts a list OR a dict-shaped
    # ``{"nodes": {"n0": {...}}}`` emission, which previously produced [].
    nodes_in = list(_dict_rows(raw.get("nodes")))

    # REVIEW-2026-09-10: a node with a missing/empty id used to be skipped
    # silently ("continue"), which collapsed the topology - a 2-node tree
    # became a single-node tree and to_newick returned "(Root);". Assign a
    # stable synthetic id instead and flag the repair, so the clade is kept
    # and the operator can see that the id was not in the figure.
    _existing_ids = {str(n.get("id") or "").strip()
                     for n in nodes_in if isinstance(n, dict)}
    _anon = 0
    for n in nodes_in:
        if str(n.get("id") or "").strip():
            continue
        while True:
            _anon += 1
            candidate = f"_anon{_anon}"
            if candidate not in _existing_ids:
                break
        _existing_ids.add(candidate)
        n["id"] = candidate
        phylo_warnings.append("node_missing_id_synthesised")

    # FIX-2026-10-01: a DUPLICATE id. The model reuses one id for two
    # different leaves quite often (it invents "n1"/"1" when the figure prints
    # no node numbers), and every id-keyed structure in this module keeps ONE
    # entry per id. to_newick therefore emitted the second node's name for both
    # and dropped the first entirely: three nodes in, "((Beta,Beta));" out --
    # one taxon silently gone, one duplicated. The table still held all three
    # rows, so nothing looked wrong upstream, and to_newick feeds BOTH the
    # Newick export (rca_core/exporter.py to_newick_file) and the RF-distance
    # metric (rca_core/eval_metrics.py), so the published tree and the score
    # were both wrong. Same treatment as the missing-id case above: keep every
    # taxon, give the later one a unique id, and flag the repair.
    _seen_ids: set[str] = set()
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        if not nid:
            continue
        if nid not in _seen_ids:
            _seen_ids.add(nid)
            continue
        while True:
            _anon += 1
            candidate = f"_dup{_anon}"
            if candidate not in _seen_ids:
                break
        _seen_ids.add(candidate)
        n["id"] = candidate
        phylo_warnings.append("node_duplicate_id_disambiguated")

    # Build id→node lookup and compute children counts (reverse check for is_leaf).
    id_to_node: dict[str, dict[str, Any]] = {}
    children_count: dict[str, int] = {}
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if nid:
            id_to_node[nid] = n
            children_count[nid] = 0

    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        parent = n.get("parent")
        if parent is not None:
            pid = str(parent)
            if pid in children_count:
                children_count[pid] = children_count.get(pid, 0) + 1

    # Sprint B (REVIEW-2026-09-04): normalise root_ids to strings ONCE, up
    # front. Previously one check used str(rid) while the membership test
    # below compared a str node id against the RAW list — so
    # {"root_ids": [1], "nodes": [{"id": "1", "parent": null, ...}]}
    # mis-classified its only root as a non-root node and raised
    # "Non-root node 1 must have a parent".
    #
    # AUDIT-2026-09-29: the comprehension was the ONE rejection path in this
    # function that could raise TypeError instead of ValueError. A model that
    # writes a scalar there —
    #     {"root_ids": 42, "nodes": [...]}
    # — made `raw.get("root_ids") or []` yield 42, and iterating an int raises
    # TypeError: 'int' object is not iterable. That escaped every
    # `pytest.raises(ValueError)` contract in tests/test_phylo_parent_null.py
    # and surfaced to the user as `normalize failed: 'int' object is not
    # iterable` (extractor.py catches Exception at the call site), which names
    # an implementation detail instead of anything about their figure.
    #
    # The JS mirror already had this right and handled four shapes explicitly
    # (js/minimax.js:2507-2516): array, string, object (its keys), and
    # anything else -> empty. A scalar is in the last group there, so the
    # browser reported "root_ids is empty" while the desktop raised TypeError
    # on the same payload. Mirroring the branch list keeps the two engines
    # refusing the same payloads for the same stated reason, and restores the
    # ValueError contract this function otherwise keeps everywhere.
    _raw_root_ids = raw.get("root_ids")
    if isinstance(_raw_root_ids, (list, tuple, str, dict)):
        root_ids = [str(r) for r in (_raw_root_ids or [])]
    else:
        # None / int / float / bool: the JS mirror's "anything else" branch.
        root_ids = []
    if not root_ids:
        raise ValueError("root_ids is empty")

    # Validate all root_ids reference actual nodes.
    for rid in root_ids:
        if rid not in id_to_node:
            raise ValueError(f"root_ids contains unknown node id: {rid}")

    nodes_out = []
    for n in nodes_in:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "")
        if not nid:
            continue

        parent_val = n.get("parent")

        # Invariant: non-root nodes must have a parent in node ids.
        if nid not in root_ids:
            if parent_val is None:
                raise ValueError(f"Non-root node {nid} must have a parent")
            if str(parent_val) not in id_to_node:
                raise ValueError(f"Node {nid} references parent {parent_val} not in node ids")
        else:
            # Root nodes must have parent == None.
            if parent_val is not None:
                raise ValueError(f"Root node {nid} must have parent == None, got {parent_val}")

        # Reverse-check is_leaf: a node is_leaf iff it has no children.
        # Sprint B (REVIEW-2026-09-04): the correction used to be written to
        # a dead local (``extra_flag``) that was never read, so the flag
        # vanished and the audit trail was lost. The corrected value is now
        # marked on the row via its ``metadata`` dict — the same channel the
        # unknown-key extras flow through — so downstream consumers can see
        # that the model's is_leaf claim was overridden.
        is_leaf_input = bool(n.get("is_leaf"))
        actual_is_leaf = children_count.get(nid, 0) == 0
        leaf_corrected = is_leaf_input != actual_is_leaf
        is_leaf = actual_is_leaf

        support_raw = n.get("support")
        support = fv(support_raw)
        if support is not None and not (0.0 <= support <= 100.0):
            raise ValueError(f"support must be in [0, 100] or None, got {support}")

        row = {
            "id": nid,
            "parent": str(parent_val) if parent_val is not None else None,
            "name": s(n.get("name")),
            "is_leaf": is_leaf,
            "branch_length": fv(n.get("branch_length")),
            "node_age_ma": fv(n.get("node_age_ma")),
            "support": support,
        }
        # Carry extras for unknown keys (preserve depth_range_m, sequence_count,
        # support_confidence, depth_confidence, etc.)
        extras = {k: v for k, v in n.items()
                   if k not in ("id", "parent", "name", "is_leaf",
                                "branch_length", "node_age_ma", "support")}
        if leaf_corrected:
            extras["_is_leaf_corrected"] = True
        if extras:
            row["metadata"] = extras
        nodes_out.append(row)

    # AUDIT-2026-09-30: `raw.get("metadata") or {}` only rejects FALSY
    # non-mappings, so a truthy string / list / number went straight through
    # and `metadata_raw.get("title", "")` raised
    # AttributeError: 'str' object has no attribute 'get'. That escapes the
    # ValueError contract this function keeps everywhere else, and the user
    # saw "normalize failed: 'str' object has no attribute 'get'" -- an
    # implementation detail instead of anything about their figure. Found by
    # difffuzz_normalize.py once its phylogenetic_tree generator stopped
    # raising on every single case (see that file: node ids were random
    # scalars and the parent key was spelled "parent_id", which this function
    # does not read, so 400/400 cases died before reaching one line of
    # normalisation and "both engines refused" was scored as agreement).
    #
    # The browser mirror failed the same payload differently, and no better:
    # `rcaPyOr(raw.metadata, {})` then `Object.keys("str")` yields ["0","1","2"],
    # so it emitted {"0": "s", "1": "t", "2": "r"} as if a string's character
    # positions were metadata keys. A non-mapping is not a mapping, so both
    # sides now take the SAME shape as the `legend` line three lines below,
    # which was already guarded on both engines: a non-dict is not metadata.
    metadata_raw = raw.get("metadata")
    if not isinstance(metadata_raw, dict):
        metadata_raw = {}
    # Preserve raw fields not in the new schema (taxon_group, root_name,
    # total_nodes) so existing tests and downstream consumers that read
    # those fields continue to work.
    NEW_META_KEYS = {"title", "extraction_timestamp", "tree_type", "scale",
                     "rooted", "source"}
    metadata: dict[str, Any] = {
        "title": s(metadata_raw.get("title", "")),
        "extraction_timestamp": s(metadata_raw.get("extraction_timestamp", "")),
        "tree_type": s(metadata_raw.get("tree_type", "")),
        "scale": s(metadata_raw.get("scale", "")),
        # REVIEW-2026-09-10: `bool(...)` inverted the meaning of the string
        # "false" (non-empty string -> True) and turned an explicit null into
        # False, diverging from the browser mirror (js/minimax.js treats
        # anything that is not exactly false as rooted). _coerce_bool_flag
        # parses the textual forms and keeps the mirror's absent/null default.
        "rooted": _coerce_bool_flag(metadata_raw.get("rooted"), True),
        "source": s(metadata_raw.get("source", metadata_raw.get("image_source", ""))),
    }
    for k, v in metadata_raw.items():
        if k not in NEW_META_KEYS:
            metadata[k] = v

    legend_raw = raw.get("legend")
    legend = dict(legend_raw) if isinstance(legend_raw, dict) else {}

    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = _confidence_clamped(conf)

    root_extras = _pop_array_root_extras(
        {k: v for k, v in raw.items()
         if k not in ("metadata", "nodes", "root_ids", "legend", "confidence")})
    out: dict[str, Any] = {
        "metadata": metadata,
        # root_ids is already str-normalised up front (Sprint B REVIEW-2026-09-04).
        "root_ids": root_ids,
        "nodes": nodes_out,
        "legend": legend,
        "confidence": conf,
    }
    if root_extras:
        out["_extras"] = root_extras
    if phylo_warnings:
        out["_warnings"] = phylo_warnings
    return out


# Public alias.  (Moved verbatim from extractor.py L4037.)
normalize_phylogenetic_tree_result = _normalize_phylogenetic_tree_into
