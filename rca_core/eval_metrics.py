"""Gold-standard evaluation metrics for extraction accuracy.

These metrics compute precision/recall against expert-annotated ground
truth. Used by tests/test_gold_*.py and the CI report.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_taxon(name: str) -> str:
    """Lowercase, strip whitespace, drop ICZN qualifiers for fuzzy matching.

    Example: "Ammonites cf. koslovensis" → "ammonites koslovensis"
    """
    if not name:
        return ""
    s = re.sub(r"\s+", " ", str(name).strip().lower())
    # Token-based ICZN qualifier removal.
    # Handles: cf., aff., ex gr., sensu lato, and ? (doubt marker, possibly attached).
    _ICZN = frozenset({"cf.", "cf", "aff.", "aff", "ex", "gr.", "sensu", "lato"})
    parts = s.split()
    filtered = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        tok_stripped = tok.rstrip(".")
        if tok in _ICZN or tok_stripped in _ICZN:
            # Skip compound qualifiers like "ex gr." and "sensu lato"
            if tok in ("ex", "sensu") and i + 1 < len(parts):
                i += 2
                continue
            if tok == "gr." and i > 0 and parts[i - 1] == "ex":
                i += 1
                continue
            if tok == "lato" and i > 0 and parts[i - 1] == "sensu":
                i += 1
                continue
            i += 1
            continue
        # Handle ? suffix (ICZN doubt marker possibly attached to the name)
        if tok.endswith("?"):
            tok = tok[:-1]
            if tok:
                filtered.append(tok)
            i += 1
            continue
        filtered.append(tok)
        i += 1
    return " ".join(filtered)


# ---------------------------------------------------------------------------
# Species-level Precision/Recall
# ---------------------------------------------------------------------------


def species_precision_recall(
    predicted: list[dict[str, Any]], ground_truth: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute P/R for species identification (range_chart / abundance).

    Args:
        predicted: list of species row dicts (must have "species" key)
        ground_truth: list of expert-annotated species rows

    Returns:
        {
            "precision": 0.0-1.0,
            "recall": 0.0-1.0,
            "f1": 0.0-1.0,
            "true_positives": int,
            "false_positives": int,
            "false_negatives": int,
        }
    """
    pred_species = {_normalize_taxon(r.get("species", "")) for r in predicted if r.get("species")}
    true_species = {_normalize_taxon(r.get("species", "")) for r in ground_truth if r.get("species")}

    if not true_species:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "true_positives": 0, "false_positives": 0, "false_negatives": 0}

    tp = len(pred_species & true_species)
    fp = len(pred_species - true_species)
    fn = len(true_species - pred_species)

    p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0

    return {
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1, 4),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


# ---------------------------------------------------------------------------
# Range Top/Base Accuracy
# ---------------------------------------------------------------------------


def _parse_bed(value: Any) -> int | None:
    """Parse a bed indicator into an integer, or return None if unparseable."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    return int(m.group()) if m else None


def range_top_accuracy(
    predicted: list[dict[str, Any]], ground_truth: list[dict[str, Any]], tolerance: int = 1
) -> dict[str, Any]:
    """Check if range_top is within ±N bed index of GT.

    Only matches species present in both predicted and ground truth.

    Returns:
        {
            "exact": N,              # exact match count
            "within_tolerance": N,  # within ±tolerance
            "wrong": N,
            "acc_exact": 0.0-1.0,
            "acc_tolerance": 0.0-1.0,
        }
    """
    # Build GT lookup: normalized species → (top, base)
    gt_lookup: dict[str, tuple[int | None, int | None]] = {}
    for row in ground_truth:
        sp_key = _normalize_taxon(row.get("species", ""))
        if sp_key:
            gt_lookup[sp_key] = (_parse_bed(row.get("range_top")), _parse_bed(row.get("range_base")))

    if not gt_lookup:
        return {"exact": 0, "within_tolerance": 0, "wrong": 0, "acc_exact": 0.0, "acc_tolerance": 0.0}

    exact = 0
    within_tol = 0
    wrong = 0
    total = 0

    for row in predicted:
        sp_key = _normalize_taxon(row.get("species", ""))
        if sp_key not in gt_lookup:
            continue
        gt_top, gt_base = gt_lookup[sp_key]
        pred_top = _parse_bed(row.get("range_top"))
        if gt_top is None or pred_top is None:
            continue
        total += 1
        if pred_top == gt_top:
            exact += 1
            within_tol += 1
        elif abs(pred_top - gt_top) <= tolerance:
            within_tol += 1
        else:
            wrong += 1

    if total == 0:
        return {"exact": 0, "within_tolerance": 0, "wrong": 0, "acc_exact": 0.0, "acc_tolerance": 0.0}

    return {
        "exact": exact,
        "within_tolerance": within_tol,
        "wrong": wrong,
        "acc_exact": round(exact / total, 4),
        "acc_tolerance": round(within_tol / total, 4),
    }


# ---------------------------------------------------------------------------
# Biozone Accuracy
# ---------------------------------------------------------------------------


def biozone_accuracy(
    predicted: list[dict[str, Any]], ground_truth: list[dict[str, Any]]
) -> dict[str, Any]:
    """Check biozone name matching (exact + fuzzy).

    Returns per-biozone results plus aggregate precision.
    """
    pred_zones = {str(z.get("name", "")).strip().lower() for z in predicted if z.get("name")}
    true_zones = {str(z.get("name", "")).strip().lower() for z in ground_truth if z.get("name")}

    if not true_zones:
        return {"exact": 0, "fuzzy": 0, "missed": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0}

    exact = len(pred_zones & true_zones)

    # Fuzzy: normalized match (drop Zone/zone suffix, case-insensitive).
    # A fuzzy-matched pred zone is one whose stripped name appears in stripped true zones.
    _ZONE_RE = re.compile(r"\s+zone\b", re.IGNORECASE)
    fuzzy_stripped_true = {_ZONE_RE.sub("", z).strip() for z in true_zones}
    fuzzy_matched: set[str] = set()
    for z in pred_zones:
        stripped = _ZONE_RE.sub("", z).strip()
        if stripped in fuzzy_stripped_true:
            fuzzy_matched.add(z)
    fuzzy = len(fuzzy_matched) - exact  # don't double-count exact matches

    # True positives: exact matches + fuzzy matches.
    # False positives: pred zones not in true_zones AND not fuzzy-matched.
    tp = exact + fuzzy
    fp = len(pred_zones - true_zones - fuzzy_matched)
    fn = len(true_zones - pred_zones)

    p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0

    return {
        "exact": exact,
        "fuzzy": max(0, fuzzy),
        "missed": fn,
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1, 4),
    }


# ---------------------------------------------------------------------------
# Abundance Sum Error
# ---------------------------------------------------------------------------


def abundance_sum_error(
    predicted: list[dict[str, Any]], ground_truth: list[dict[str, Any]]
) -> dict[str, Any]:
    """Per-sample percentage sum error.

    Returns the maximum absolute deviation from 100% across all levels.
    """
    def build_level_map(rows: list[dict[str, Any]]) -> dict[str, float]:
        """Aggregate percentages per level (key="level")."""
        sums: dict[str, float] = {}
        for row in rows:
            unit = str(row.get("abundance_unit", "")).strip().lower()
            if unit != "%":
                continue
            try:
                pct = float(row.get("abundance", 0))
            except (TypeError, ValueError):
                continue
            level = str(row.get("level", ""))
            if level:
                sums[level] = sums.get(level, 0.0) + pct
        return sums

    pred_sums = build_level_map(predicted)
    true_sums = build_level_map(ground_truth)

    if not true_sums:
        return {"max_error": 0.0, "levels_checked": 0, "per_level": {}}

    per_level: dict[str, dict[str, float]] = {}
    errors: list[float] = []

    for level, true_val in true_sums.items():
        pred_val = pred_sums.get(level, 0.0)
        err = abs(pred_val - true_val)
        errors.append(err)
        per_level[level] = {"predicted_sum": round(pred_val, 2), "true_sum": round(true_val, 2), "error": round(err, 2)}

    max_error = max(errors) if errors else 0.0
    return {
        "max_error": round(max_error, 2),
        "levels_checked": len(true_sums),
        "per_level": per_level,
    }


# ---------------------------------------------------------------------------
# Phylogenetic Tree — Robinson-Foulds Distance
# ---------------------------------------------------------------------------


def phylogenetic_topology_distance(predicted: dict[str, Any], ground_truth: dict[str, Any]) -> int:
    """Robinson-Foulds distance between predicted and GT trees.

    Requires ete3 (optional). Falls back to a manual set-based distance
    when ete3 is unavailable.
    """
    try:
        from ete3 import Tree
    except ImportError:
        return _manual_rf(predicted, ground_truth)

    try:
        def tree_to_newick(tree_dict: dict[str, Any]) -> str:
            """Convert our node format to a minimal Newick string."""
            nodes = tree_dict.get("nodes", [])
            root_ids = tree_dict.get("root_ids", [])
            if not nodes or not root_ids:
                return "();"

            nodes_dict = {n["id"]: n for n in nodes if isinstance(n, dict)}
            id_to_children: dict[str, list[str]] = {rid: [] for rid in root_ids}
            for n in nodes:
                if isinstance(n, dict):
                    pid = n.get("parent")
                    if pid is not None:
                        pid_str = str(pid)
                        if pid_str not in id_to_children:
                            id_to_children[pid_str] = []
                        id_to_children[pid_str].append(str(n["id"]))

            def build(nid: str) -> str:
                children = id_to_children.get(nid, [])
                if not children:
                    nd = nodes_dict.get(nid, {})
                    name = str(nd.get("name", "")).replace(" ", "_")
                    bl = nd.get("branch_length")
                    return f"{name}:{bl}" if bl is not None else f"{name}:0.1"
                child_parts = [build(cid) for cid in children]
                nd = nodes_dict.get(nid, {})
                support = nd.get("support")
                support_str = f"{support}" if support is not None else ""
                bl = nd.get("branch_length")
                bl_str = f":{bl}" if bl is not None else ""
                return f"({','.join(child_parts)}){support_str}{bl_str}"

            parts = [build(rid) for rid in root_ids]
            return f"({','.join(parts)});"

        t1 = Tree(tree_to_newick(predicted), format=1)
        t2 = Tree(tree_to_newick(ground_truth), format=1)
        rf, _, _, _ = t1.robinson_foulds(t2)
        return rf
    except Exception:
        return _manual_rf(predicted, ground_truth)


def _manual_rf(predicted: dict[str, Any], ground_truth: dict[str, Any]) -> int:
    """Fallback Robinson-Foulds using only set operations on clades.

    Counts the symmetric difference of the partition sets induced by each tree.
    This is O(n^2) but works without ete3.
    """
    def get_clades(nodes: list[dict[str, Any]], root_ids: list[str]) -> frozenset[frozenset[str]]:
        """Return the set of all clades (non-empty proper subsets of leaves)."""
        nodes_dict = {n["id"]: n for n in nodes if isinstance(n, dict)}
        id_to_children: dict[str, list[str]] = {rid: [] for rid in root_ids}
        for n in nodes:
            if isinstance(n, dict):
                pid = n.get("parent")
                if pid is not None:
                    pid_str = str(pid)
                    if pid_str not in id_to_children:
                        id_to_children[pid_str] = []
                    id_to_children[pid_str].append(str(n["id"]))

        # Get leaves
        leaves: set[str] = set()
        for n in nodes:
            if isinstance(n, dict) and n.get("is_leaf"):
                leaves.add(str(n["id"]))

        def get_leaf_set(node_id: str) -> frozenset[str]:
            """Get all leaves descended from node_id."""
            children = id_to_children.get(node_id, [])
            if not children:
                return frozenset({node_id})
            result: set[str] = set()
            for cid in children:
                result |= get_leaf_set(cid)
            return frozenset(result)

        clades: set[frozenset[str]] = set()
        for nid in nodes_dict:
            ls = get_leaf_set(nid)
            if ls and ls != frozenset(leaves):  # proper non-empty subset
                clades.add(ls)
        return frozenset(clades)

    pred_nodes = predicted.get("nodes", [])
    gt_nodes = ground_truth.get("nodes", [])
    pred_roots = predicted.get("root_ids", ["root"])
    gt_roots = ground_truth.get("root_ids", ["root"])

    pred_clades = get_clades(pred_nodes, pred_roots)
    gt_clades = get_clades(gt_nodes, gt_roots)

    return len(pred_clades ^ gt_clades)
