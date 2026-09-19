"""Gold-standard evaluation metrics for extraction accuracy.

These metrics compute precision/recall against expert-annotated ground
truth. Used by tests/test_gold_*.py and the CI report.
"""
from __future__ import annotations

import re
from typing import Any

from .bed_parser import parse_bed as _parse_bed_info

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_taxon(name: str, *, preserve_qualifiers: bool = True) -> str:
    """Canonicalize a taxon name with an explicit strict/lenient policy.

    Strict normalization is the default: spelling, case and punctuation variants
    are normalized, but ICZN open-nomenclature qualifiers remain part of the
    scientific identity. Lenient normalization removes those qualifiers for an
    explicitly labelled secondary metric.
    """
    if not name:
        return ""
    s = re.sub(r"\s+", " ", str(name).strip().lower())
    canonical = (
        (r"\bex\s+gr(?:oup)?\.?(?=\s|$)", "ex gr."),
        (r"\bsensu\s+lato\b|\bs\.?\s*l\.?(?=\s|$)", "s.l."),
        (r"\bsensu\s+stricto\b|\bs\.?\s*str\.?(?=\s|$)", "s.str."),
        (r"\bcf\.?(?=\s|$)", "cf."),
        (r"\baff\.?(?=\s|$)", "aff."),
        (r"\bspp\.?(?=\s|$)", "spp."),
        (r"\bsp\.?(?=\s|$)", "sp."),
    )
    for pattern, replacement in canonical:
        s = re.sub(pattern, replacement, s)
    s = re.sub(r"\s*\?\s*", " ? ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if preserve_qualifiers:
        return s

    qualifier_patterns = (
        r"\bex\s+gr\.\s*",
        r"\bs\.l\.\s*",
        r"\bs\.str\.\s*",
        r"\bcf\.\s*",
        r"\baff\.\s*",
        r"\bspp\.\s*",
        r"\bsp\.\s*",
        r"\s*\?\s*",
    )
    for pattern in qualifier_patterns:
        s = re.sub(pattern, " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Species-level Precision/Recall
# ---------------------------------------------------------------------------


def _species_pr_for_policy(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    preserve_qualifiers: bool,
) -> dict[str, Any]:
    pred_species = {
        _normalize_taxon(row.get("species", ""), preserve_qualifiers=preserve_qualifiers)
        for row in predicted if row.get("species")
    }
    true_species = {
        _normalize_taxon(row.get("species", ""), preserve_qualifiers=preserve_qualifiers)
        for row in ground_truth if row.get("species")
    }
    pred_species.discard("")
    true_species.discard("")
    if not true_species:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "true_positives": 0, "false_positives": 0, "false_negatives": 0}

    tp = len(pred_species & true_species)
    fp = len(pred_species - true_species)
    fn = len(true_species - pred_species)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


def species_precision_recall(
    predicted: list[dict[str, Any]], ground_truth: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute strict species P/R and report an explicit lenient companion.

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

    REVIEW-2026-11-07 (low): with an EMPTY ground truth every predicted
    species is a false positive, but the deliberate convention here is to
    report 0/0/0 (not 0 precision) so callers can treat "no annotation
    available" as "not scored" rather than "perfectly wrong". If you need
    the strict set-theoretic reading, check ``false_positives`` directly.
    """
    strict = _species_pr_for_policy(
        predicted, ground_truth, preserve_qualifiers=True,
    )
    lenient = _species_pr_for_policy(
        predicted, ground_truth, preserve_qualifiers=False,
    )
    return {
        **strict,
        "matching_policy": "strict",
        "lenient": {**lenient, "matching_policy": "qualifier_insensitive"},
    }


# ---------------------------------------------------------------------------
# Range Top/Base Accuracy
# ---------------------------------------------------------------------------


def _parse_bed(value: Any) -> dict[str, Any] | None:
    """Parse a bed indicator into ``{bed_num, bed_sub, raw}`` or None.

    M-1 fix (REVIEW-2026-07-25): the previous inline implementation
    used ``re.search(r\"-?\\d+\")`` and dropped subscript qualifiers
    like \"Bed 23c\" → ``23``. This silently diverged from
    ``rca_core.exporter._parse_bed`` which kept both number and
    subscript. Both now route through ``rca_core.bed_parser``.

    REVIEW-2026-09-20: this wrapper itself still returned the bare integer
    (``parse_bed_int``), so the *scorer* dropped the subscript the shared
    parser had kept. Predicted "Bed 23c" against ground truth "Bed 23d"
    scored as an EXACT match and range-limit accuracy came back inflated —
    precisely the divergence the M-1 fix was supposed to remove. The dict
    form is what scoring compares now.
    """
    return _parse_bed_info(value)


def _bed_sub(value: Any) -> str:
    """Normalized subscript of a parsed bed ("" when the bed has none)."""
    return str((value or {}).get("bed_sub") or "")


def _score_bed_pair(pred: dict[str, Any], gt: dict[str, Any], tolerance: int) -> str:
    """Classify one parsed bed pair as 'exact' / 'within_tolerance' / 'wrong'.

    ``tolerance`` is a stratigraphic DISTANCE allowance and therefore applies
    to ``bed_num`` only: two sub-beds of the same bed (23c vs 23d) are
    different levels, and no tolerance can make them agree. A zero / negative
    tolerance disables the window, matching the historical behaviour.
    """
    try:
        diff = abs(int(pred["bed_num"]) - int(gt["bed_num"]))
    except (TypeError, ValueError, KeyError):  # pragma: no cover - parse_bed guarantees int
        return "wrong"
    if diff == 0:
        return "exact" if _bed_sub(pred) == _bed_sub(gt) else "wrong"
    try:
        window = float(tolerance or 0)
    except (TypeError, ValueError):
        window = 0.0
    if diff <= window:
        return "within_tolerance"
    return "wrong"


def _endpoint_accuracy(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    field: str,
    tolerance: int = 1,
) -> dict[str, Any]:
    """Shared scorer behind range_top_accuracy / range_base_accuracy.

    Only species present in both sides are scored; a row whose bed (either
    side) does not parse is skipped rather than guessed, exactly like before.
    """
    gt_lookup: dict[str, Any] = {}
    for row in ground_truth:
        sp_key = _normalize_taxon(row.get("species", ""))
        if sp_key:
            gt_lookup[sp_key] = _parse_bed(row.get(field))

    if not gt_lookup:
        return {"exact": 0, "within_tolerance": 0, "wrong": 0,
                "subscript_mismatch": 0, "acc_exact": 0.0, "acc_tolerance": 0.0}

    exact = 0
    within_tol = 0
    wrong = 0
    sub_mismatch = 0
    total = 0

    for row in predicted:
        sp_key = _normalize_taxon(row.get("species", ""))
        if sp_key not in gt_lookup:
            continue
        gt_bed, pred_bed = gt_lookup[sp_key], _parse_bed(row.get(field))
        if gt_bed is None or pred_bed is None:
            continue
        total += 1
        verdict = _score_bed_pair(pred_bed, gt_bed, tolerance)
        if verdict == "exact":
            exact += 1
            within_tol += 1
        elif verdict == "within_tolerance":
            within_tol += 1
        else:
            wrong += 1
            # Same bed, different subscript: the failure the integer-only
            # comparison used to hide. Reported separately so a report can
            # tell "wrong bed" from "wrong sub-bed of the right bed".
            if pred_bed["bed_num"] == gt_bed["bed_num"]:
                sub_mismatch += 1

    if total == 0:
        return {"exact": 0, "within_tolerance": 0, "wrong": 0,
                "subscript_mismatch": 0, "acc_exact": 0.0, "acc_tolerance": 0.0}

    return {
        "exact": exact,
        "within_tolerance": within_tol,
        "wrong": wrong,
        "subscript_mismatch": sub_mismatch,
        "acc_exact": round(exact / total, 4),
        "acc_tolerance": round(within_tol / total, 4),
    }


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
            "subscript_mismatch": N, # wrong = same bed_num, different bed_sub
            "acc_exact": 0.0-1.0,
            "acc_tolerance": 0.0-1.0,
        }
    """
    return _endpoint_accuracy(predicted, ground_truth, "range_top", tolerance)


def range_base_accuracy(
    predicted: list[dict[str, Any]], ground_truth: list[dict[str, Any]], tolerance: int = 1
) -> dict[str, Any]:
    """Symmetric counterpart to range_top_accuracy.

    M-1 fix (REVIEW-2026-07-25): the original ``range_top_accuracy``
    parsed both ``range_top`` and ``range_base`` from ground truth but
    only validated ``range_top``. A prediction that perfectly nailed
    the top boundary but completely missed the base scored 100%. For
    range charts the FAD (base, older) is often the more scientifically
    important datum. This function validates ``range_base`` and
    reports the same shape so the JSON side can render both badges.
    """
    return _endpoint_accuracy(predicted, ground_truth, "range_base", tolerance)


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
