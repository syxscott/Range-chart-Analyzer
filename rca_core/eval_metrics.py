"""Gold-standard evaluation metrics for extraction accuracy.

These metrics compute precision/recall against expert-annotated ground
truth. Used by tests/test_gold_*.py and the CI report.

BORROW-2026-09-20: this module gained a second, layered scoring vocabulary
on top of the legacy single-number metrics (nothing below the marker changes
an existing signature or drops an existing key):

* SCRM-style TIERED boundary scoring — a boundary miss is graded into four
  tiers (``strict`` / ``adjacent`` / ``coarse`` / ``wrong``) instead of the
  old binary exact-or-wrong verdict, with a weighted score per field. Both
  the ordinal (bed bin) mode and the existing numeric mode (absolute age in
  Myr with a Myr tolerance) are supported.
* CHOCOLATE-style ERROR TYPOLOGY — every scored row gets one deterministic
  error label so the *kind* of failure is visible, not just its count.
* CharXiv-style SPLIT TRACKS — "descriptive" (label/axis/header reading) and
  "reasoning" (cross-column comparison, range intersection) score
  separately and are never blended into one headline number.
* HONEST REFUSAL RATE — a model that answers "not drawn"/"uncertain" is
  reported as a refusal next to precision, not silently as an error.
"""
from __future__ import annotations

import difflib
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


# ===========================================================================
# BORROW-2026-09-20 — tiered boundary scoring, error typology, split tracks,
# honest refusal rate. Everything below is ADDITIVE: the legacy functions
# above keep their signatures and their output keys.
#
# TODO(BORROW-2026-09-20, discipline item): a per-gate ABLATION switch (run
# the report with one metric/gate disabled to see what each contributes)
# does not exist in this repo — there is no gate registry, only the hard-coded
# gates in tests/test_gold_smoke.py. Wire it once a gate registry exists.
# ===========================================================================

# --- Tier vocabulary (SCRM-style graded tolerance) -------------------------

TIER_STRICT = "strict"          # same bin (or |Δage| within the strict floor)
TIER_ADJACENT = "adjacent"      # neighbouring bin / within the adjacent window
TIER_COARSE = "coarse"          # off by more, but still inside one coarse step
TIER_WRONG = "wrong"            # further away (wrong direction / wrong unit)

#: Ordered strict -> wrong. ``rates`` in every tier result uses these keys.
BOUNDARY_TIERS: tuple[str, ...] = (TIER_STRICT, TIER_ADJACENT, TIER_COARSE, TIER_WRONG)

#: Credit per tier for ``weighted_score``. Wrong must never carry credit.
DEFAULT_TIER_WEIGHTS: dict[str, float] = {
    TIER_STRICT: 1.0,
    TIER_ADJACENT: 0.6,
    TIER_COARSE: 0.3,
    TIER_WRONG: 0.0,
}

#: Ordinal mode: distance measured in BINS (bed indices).
DEFAULT_BIN_WIDTHS: dict[str, float] = {"adjacent": 1.0, "coarse": 2.0}

#: Numeric mode (the existing absolute-age use case): distance in Myr.
DEFAULT_MYR_TOLERANCES: dict[str, float] = {"adjacent": 0.5, "coarse": 2.0}

#: Endpoints scored per species row.
ENDPOINT_FIELDS: tuple[str, ...] = ("range_top", "range_base")

# --- Error typology labels (CHOCOLATE-style) -------------------------------

ERROR_CORRECT = "correct"
ERROR_TAXON_MISID = "taxon_misid"
ERROR_BOUNDARY_MISREAD = "boundary_misread"
ERROR_RANGE_SHIFT_UP = "range_shift_up"
ERROR_RANGE_SHIFT_DOWN = "range_shift_down"
ERROR_OMISSION = "omission"
ERROR_HALLUCINATION = "hallucination"
ERROR_VALUE_SCALE = "value_scale_error"

#: The dominant label per row is drawn from this closed set (one label/row).
ERROR_LABELS: tuple[str, ...] = (
    ERROR_CORRECT,
    ERROR_TAXON_MISID,
    ERROR_BOUNDARY_MISREAD,
    ERROR_RANGE_SHIFT_UP,
    ERROR_RANGE_SHIFT_DOWN,
    ERROR_OMISSION,
    ERROR_HALLUCINATION,
    ERROR_VALUE_SCALE,
)

#: Rows the model explicitly declined. Kept OUT of ``ERROR_LABELS`` so the
#: error distribution is not diluted by honest refusals; reported per kind.
REFUSAL_KINDS: tuple[str, ...] = ("not_drawn", "uncertain")
REFUSAL_LABEL = "refusal"

#: Name similarity levels produced by the row pairing.
NAME_EXACT = "exact"        # identical after strict normalization
NAME_QUALIFIER = "qualifier"  # only ICZN open-nomenclature qualifiers differ
NAME_FUZZY = "fuzzy"        # near-miss spelling of a different-looking name

#: Ratios treated as a unit/scale slip rather than a random boundary miss.
SCALE_RATIOS: tuple[float, ...] = (10.0, 100.0, 1000.0)

_MODE_ALIASES: dict[str, str] = {
    "auto": "auto", "bed": "bed", "ordinal": "bed", "index": "bed",
    "age": "age", "numeric": "age", "myr": "age", "ma": "age",
}

_AGE_UNIT_FACTORS: dict[str, float] = {
    "ma": 1.0, "myr": 1.0, "mya": 1.0, "my": 1.0, "m.y.": 1.0, "m.y": 1.0,
    "ka": 1e-3, "kyr": 1e-3, "ky": 1e-3,
    "ga": 1e3, "gyr": 1e3, "gy": 1e3,
}
_AGE_VALUE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*"
    r"(m\.?\s*y\.?|myr|mya|ma|ka|kyr|ga|gyr)\b",
    re.IGNORECASE,
)


def _plain_float(value: Any) -> float | None:
    """Bare numeric value of a field (``"12.5"`` -> 12.5), else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _age_in_myr(value: Any, *, require_unit: bool = True) -> float | None:
    """Absolute age of a boundary label in Ma, or None when it is not one.

    Same policy as ``rca_core.standards.ics``: a BARE number is not an age
    (charts are full of bed / sample / column indices), so it is rejected
    unless the caller pins ``mode="age"`` (``require_unit=False``), where the
    column really is numeric by construction. ka/kyr and Ga/gyr are rescaled
    so a unit slip still lands on the right magnitude comparison.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if not require_unit else None
    text = str(value).strip()
    if not text:
        return None
    if not require_unit:
        direct = _plain_float(text)
        if direct is not None:
            return direct
    match = _AGE_VALUE_RE.search(text)
    if not match:
        return None
    unit = re.sub(r"[.\s]+", "", match.group(2).lower())
    factor = _AGE_UNIT_FACTORS.get(unit) or _AGE_UNIT_FACTORS.get(unit + ".")
    if factor is None:
        return None
    try:
        return float(match.group(1)) * factor
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None


def _sub_token(sub: Any) -> str:
    """Comparable subscript token ("" when the bed has none).

    Compared lexicographically for the direction of a same-bed slip: the
    letters of a bed subscript run a, b, c … up-section in practice.
    """
    return str(sub or "").strip().lower()


def _bin_distance(pred: dict[str, Any], gt: dict[str, Any]) -> float:
    """Distance between two parsed beds in BINS.

    ``bed_num`` carries the stratigraphic distance; a subscript difference
    adds exactly one bin. One bin — not the alphabetic gap — because the
    sampling interval between sub-beds (23c vs 23a) is not recoverable from
    the label alone, and "no subscript" vs "some subscript" must not score as
    a two-bin miss. Same bed, same subscript is distance 0 (``strict``).
    """
    try:
        num_gap = abs(int(pred["bed_num"]) - int(gt["bed_num"]))
    except (TypeError, ValueError, KeyError):  # pragma: no cover - parse_bed guards
        return float("inf")
    sub_gap = 0 if _sub_token(_bed_sub(pred)) == _sub_token(_bed_sub(gt)) else 1
    return float(num_gap + sub_gap)


def _bin_direction(pred: dict[str, Any], gt: dict[str, Any]) -> str:
    """Up-section ("up") / down-section ("down") / "none" for a bed pair.

    Bed indices increase up-section, so a LARGER predicted bed sits too HIGH.
    Subscripts break the tie alphabetically within one bed (23d above 23c).
    """
    try:
        diff = int(pred["bed_num"]) - int(gt["bed_num"])
    except (TypeError, ValueError, KeyError):  # pragma: no cover
        return "none"
    if diff > 0:
        return "up"
    if diff < 0:
        return "down"
    p_sub, g_sub = _sub_token(_bed_sub(pred)), _sub_token(_bed_sub(gt))
    if p_sub > g_sub:
        return "up"
    if p_sub < g_sub:
        return "down"
    return "none"


def _tier_for_distance(
    distance: float,
    mode: str,
    bin_widths: dict[str, float] | None,
    myr_tolerances: dict[str, float] | None,
) -> str:
    """Map a scalar distance onto one of the four tiers."""
    if mode == "bed":
        widths = {**DEFAULT_BIN_WIDTHS, **(bin_widths or {})}
        adjacent, coarse = widths.get("adjacent", 1.0), widths.get("coarse", 2.0)
    else:
        widths = {**DEFAULT_MYR_TOLERANCES, **(myr_tolerances or {})}
        adjacent, coarse = widths.get("adjacent", 0.5), widths.get("coarse", 2.0)
    try:
        adjacent, coarse = float(adjacent), float(coarse)
    except (TypeError, ValueError):
        adjacent, coarse = 1.0, 2.0
    coarse = max(coarse, adjacent)
    if distance <= 1e-9:
        return TIER_STRICT
    if distance <= adjacent:
        return TIER_ADJACENT
    if distance <= coarse:
        return TIER_COARSE
    return TIER_WRONG


def classify_boundary_tier(
    predicted: Any,
    ground_truth: Any,
    *,
    mode: str = "auto",
    bin_widths: dict[str, float] | None = None,
    myr_tolerances: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """Grade ONE boundary reading into the four SCRM-style tiers.

    Args:
        predicted: predicted boundary value (bed label, index or age).
        ground_truth: expert boundary value in the same space.
        mode: ``"auto"`` (default: bin space when both sides parse as beds,
            else unit-bearing ages), ``"bed"`` to force bin space,
            ``"age"``/``"numeric"`` to force absolute age in Myr where bare
            numbers are allowed — this is the existing numeric mode, kept.
        bin_widths: ``{"adjacent": .., "coarse": ..}`` in BINS.
        myr_tolerances: the same, in Myr.

    Returns:
        ``{"tier", "distance", "signed_distance", "direction", "mode"}`` or
        None when the pair is not comparable (unparseable on either side) —
        mirrors the legacy scorer, which SKIPS such rows rather than guessing.
    """
    kind = _MODE_ALIASES.get(str(mode or "auto").strip().lower(), "auto")

    distance: float | None = None
    direction = "none"
    resolved = kind

    if kind in ("auto", "bed"):
        pred_bed, gt_bed = _parse_bed(predicted), _parse_bed(ground_truth)
        if pred_bed is not None and gt_bed is not None:
            distance = _bin_distance(pred_bed, gt_bed)
            direction = _bin_direction(pred_bed, gt_bed)
            resolved = "bed"

    if distance is None:
        pred_age = _age_in_myr(predicted, require_unit=(kind != "age"))
        gt_age = _age_in_myr(ground_truth, require_unit=(kind != "age"))
        if pred_age is None or gt_age is None:
            return None
        distance = abs(pred_age - gt_age)
        # Ages run the other way: a SMALLER Ma is higher in the section.
        direction = "none" if pred_age == gt_age else ("up" if pred_age < gt_age else "down")
        resolved = "age"

    tier = _tier_for_distance(distance, resolved, bin_widths, myr_tolerances)
    return {
        "tier": tier,
        "distance": None if distance == float("inf") else round(float(distance), 6),
        "signed_distance": (
            None if distance == float("inf")
            else (0.0 if direction == "none" else (distance if direction == "up" else -distance))
        ),
        "direction": direction,
        "mode": resolved,
    }


def _row_is_refusal(row: Any) -> str | None:
    """``not_drawn``/``uncertain`` when the row is an explicit refusal.

    Graceful degradation: the ``response_kind`` field is OPTIONAL (the
    extraction contract may not carry it yet) and non-dict rows are ignored,
    so callers without the field keep the pre-BORROW behaviour of counting
    every unanswered cell as a miss. Spellings are normalized ("not drawn",
    "Not-Drawn" both read as ``not_drawn``) because the marker comes from a
    model-facing contract, not from a database enum.
    """
    if not isinstance(row, dict):
        return None
    kind = re.sub(r"[\s-]+", "_", str(row.get("response_kind") or "").strip().lower())
    return kind if kind in REFUSAL_KINDS else None


def _reason_codes(row: dict[str, Any]) -> list[str]:
    """Reason codes of a row, tolerating the singular spelling and a bare string.

    The extraction contract may emit ``reason_codes: list`` or the legacy
    singular ``reason_code``; either way the typology aggregates them.
    """
    raw = row.get("reason_codes")
    if raw is None:
        raw = row.get("reason_code")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    codes: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            codes.append(text)
    return codes


def _refusal_field_present(rows: list[dict[str, Any]]) -> bool:
    """True when at least one row carries the optional ``response_kind`` field."""
    return any(
        isinstance(row, dict) and str(row.get("response_kind") or "").strip() != ""
        for row in rows
    )


def _tier_summary(
    verdicts: list[dict[str, Any]],
    weights: dict[str, float] | None,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Counts / four proportions / weighted score for a list of tier verdicts."""
    merged_weights = {**DEFAULT_TIER_WEIGHTS, **(weights or {})}
    counts = {tier: 0 for tier in BOUNDARY_TIERS}
    directions = {"up": 0, "down": 0, "none": 0}
    modes: dict[str, int] = {}
    for verdict in verdicts:
        counts[verdict["tier"]] = counts.get(verdict["tier"], 0) + 1
        direction = verdict.get("direction") or "none"
        if direction in directions:
            directions[direction] += 1
        modes[verdict.get("mode", "?")] = modes.get(verdict.get("mode", "?"), 0) + 1
    scored = len(verdicts)
    rates = {tier: (round(counts[tier] / scored, 4) if scored else 0.0) for tier in BOUNDARY_TIERS}
    credit = sum(counts[tier] * float(merged_weights.get(tier, 0.0)) for tier in BOUNDARY_TIERS)
    result: dict[str, Any] = {
        "scored": scored,
        "counts": counts,
        "rates": rates,
        "weighted_score": round(credit / scored, 4) if scored else 0.0,
        "directions": directions,
        "modes": modes,
        "weights": {tier: float(merged_weights.get(tier, 0.0)) for tier in BOUNDARY_TIERS},
    }
    if extra:
        result.update(extra)
    return result


def boundary_tier_accuracy(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    field: str = "range_top",
    mode: str = "auto",
    bin_widths: dict[str, float] | None = None,
    myr_tolerances: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """BORROW-2026-09-20 (SCRM): tiered accuracy for ONE boundary field.

    Only species present on both sides are scored, exactly like the legacy
    ``range_top_accuracy``; a refused cell (``response_kind`` =
    "not_drawn"/"uncertain") is excluded from the denominator and reported
    under ``refused`` instead of being charged as an error.

    Returns:
        ``{scored, unscorable, refused, counts{4}, rates{4}, weighted_score,
        directions, modes, weights, per_row[]}`` — proportions of all four
        tiers plus the weighted score; 0.0 rates when nothing was scorable.
    """
    gt_lookup: dict[str, Any] = {}
    for row in ground_truth:
        if not isinstance(row, dict):
            continue
        key = _normalize_taxon(row.get("species", ""))
        if key:
            gt_lookup[key] = row

    verdicts: list[dict[str, Any]] = []
    skipped = 0
    refused = 0
    for row in predicted:
        if not isinstance(row, dict):
            continue
        key = _normalize_taxon(row.get("species", ""))
        if not key or key not in gt_lookup:
            continue
        if _row_is_refusal(row):
            refused += 1
            continue
        verdict = classify_boundary_tier(
            row.get(field), gt_lookup[key].get(field),
            mode=mode, bin_widths=bin_widths, myr_tolerances=myr_tolerances,
        )
        if verdict is None:
            skipped += 1  # unparseable either side: never guessed
            continue
        verdicts.append({"species": row.get("species"), "field": field, **verdict})

    return _tier_summary(
        verdicts, weights,
        extra={
            "field": field,
            "unscorable": skipped,
            "refused": refused,
            "refusal_field_present": _refusal_field_present(predicted),
            "per_row": verdicts,
        },
    )


def range_tier_accuracy(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    fields: tuple[str, ...] = ENDPOINT_FIELDS,
    mode: str = "auto",
    bin_widths: dict[str, float] | None = None,
    myr_tolerances: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Tiered accuracy for every endpoint PLUS a per-row range verdict.

    A row's own tier is its WORST endpoint (a range is only as good as its
    sloppiest boundary), so ``row.weighted_score`` cannot be padded by nailing
    one boundary while missing the other.
    """
    per_field = {
        field: boundary_tier_accuracy(
            predicted, ground_truth, field=field, mode=mode,
            bin_widths=bin_widths, myr_tolerances=myr_tolerances, weights=weights,
        )
        for field in fields
    }

    by_species: dict[str, dict[str, Any]] = {}
    for field, summary in per_field.items():
        for verdict in summary["per_row"]:
            key = _normalize_taxon(verdict.get("species", ""))
            entry = by_species.setdefault(key, {"species": verdict.get("species"), "tiers": {}})
            entry["tiers"][field] = verdict["tier"]
            entry["tiers"][f"{field}_direction"] = verdict.get("direction", "none")

    row_verdicts: list[dict[str, Any]] = []
    for key, entry in by_species.items():
        tiers = [entry["tiers"][f] for f in fields if f in entry["tiers"]]
        if not tiers:
            continue
        worst = max(tiers, key=lambda t: BOUNDARY_TIERS.index(t))
        row_verdicts.append({"species": entry["species"], "tier": worst, "mode": "row"})

    return {
        "fields": {
            field: {k: v for k, v in summary.items() if k != "per_row"}
            for field, summary in per_field.items()
        },
        "per_field": {f: per_field[f]["per_row"] for f in fields},
        "row": _tier_summary(row_verdicts, weights, extra={"unit": "species_row"}),
    }


# ---------------------------------------------------------------------------
# BORROW-2026-09-20 (CHOCOLATE): error typology
# ---------------------------------------------------------------------------


def _name_similarity(pred_key: str, gt_key: str) -> str | None:
    """How close two normalized taxon names are: exact / qualifier / fuzzy."""
    if not pred_key or not gt_key:
        return None
    if pred_key == gt_key:
        return NAME_EXACT
    lenient_p = _normalize_taxon(pred_key, preserve_qualifiers=False)
    lenient_g = _normalize_taxon(gt_key, preserve_qualifiers=False)
    if lenient_p and lenient_p == lenient_g:
        return NAME_QUALIFIER
    if lenient_p.split(" ")[0] != lenient_g.split(" ")[0]:
        return None  # different genus: not a mis-reading, an invented taxon
    if lenient_p == lenient_g:
        return NAME_QUALIFIER
    ratio = difflib.SequenceMatcher(None, lenient_p, lenient_g).ratio()
    return NAME_FUZZY if ratio >= 0.75 else None


def _scale_factor(pred: Any, gt: Any) -> float | None:
    """Exact 10/100/1000x slip between two numeric readings, else None.

    Only called on readings already graded ``wrong`` / on abundance values, so
    a merely-far bed number cannot masquerade as a unit slip (the ratio has to
    land on a decade within 1%, in either direction: 0.6 vs 60 is a fraction
    written where a percentage belongs, 253000 vs 253 is ka written for Ma).
    """
    p, g = _plain_float(pred), _plain_float(gt)
    if p is None or g is None or p == 0 or g == 0:
        return None
    ratio = abs(p) / abs(g)
    for scale in SCALE_RATIOS:
        for factor in (scale, 1.0 / scale):
            if abs(ratio - factor) / factor <= 0.01:
                return factor
    return None


def _present(value: Any) -> bool:
    """True when a cell carries anything at all (not None / not blank)."""
    return value is not None and str(value).strip() != ""


def _classify_matched_row(
    pred_row: dict[str, Any],
    gt_row: dict[str, Any],
    *,
    name_similarity: str,
    fields: tuple[str, ...],
    mode: str,
    bin_widths: dict[str, float] | None,
    myr_tolerances: dict[str, float] | None,
) -> tuple[str, dict[str, Any]]:
    """Deterministic label for a predicted row paired with a GT row.

    Rule priority (documented so the distribution is reproducible):
    ``value_scale_error`` (a decade slip on a wrong boundary or on abundance)
    > ``omission`` (the column was named but NO boundary was delivered where
    ground truth has one — the pre-BORROW reading of an unanswered cell) >
    ``taxon_misid`` (name only fuzzy-matched) > coherent whole-range slide
    (both endpoints off in the SAME direction) > ``boundary_misread`` (one
    endpoint off, or endpoints off in inconsistent directions) > ``correct``.
    """
    tiers: dict[str, str] = {}
    directions: dict[str, str] = {}
    distances: dict[str, Any] = {}
    scale_detail: dict[str, Any] = {}
    for field in fields:
        verdict = classify_boundary_tier(
            pred_row.get(field), gt_row.get(field),
            mode=mode, bin_widths=bin_widths, myr_tolerances=myr_tolerances,
        )
        if verdict is None:
            continue
        tiers[field] = verdict["tier"]
        directions[field] = verdict.get("direction", "none")
        distances[field] = verdict.get("distance")
        if verdict["tier"] == TIER_WRONG:
            slip = _scale_factor(pred_row.get(field), gt_row.get(field))
            if slip is not None:
                scale_detail[field] = {"factor": slip, "predicted": pred_row.get(field), "ground_truth": gt_row.get(field)}

    detail: dict[str, Any] = {
        "tiers": tiers,
        "directions": directions,
        "distances": distances,
        "name_similarity": name_similarity,
    }

    if scale_detail:
        detail["scale"] = scale_detail
        return ERROR_VALUE_SCALE, detail

    named_only = not any(_present(pred_row.get(f)) for f in fields)
    gt_has_data = any(_present(gt_row.get(f)) for f in fields)
    if named_only and gt_has_data:
        # Named the taxon, delivered no boundary: that is a missing answer, not
        # a wrong one. Without ``response_kind`` this is exactly what the
        # scorer has always done, so the degradation path stays honest.
        detail["note"] = "no_boundary_delivered"
        return ERROR_OMISSION, detail

    if name_similarity == NAME_FUZZY:
        return ERROR_TAXON_MISID, detail

    off = [f for f, tier in tiers.items() if tier != TIER_STRICT]
    if not off:
        # Boundaries nailed (or unscorable); abundance can still expose a unit
        # slip — % written as a fraction — so it is checked before the verdict.
        slip = _scale_factor(pred_row.get("abundance"), gt_row.get("abundance"))
        if slip is not None and str(pred_row.get("level", "")) == str(gt_row.get("level", "")):
            detail["scale"] = {"abundance": {"factor": slip}}
            return ERROR_VALUE_SCALE, detail
        if not tiers:
            # Nothing verifiable on either side: accepted on the name alone,
            # surfaced as an auxiliary counter so the rate cannot be inflated.
            detail["note"] = "unscorable_endpoints"
        return ERROR_CORRECT, detail

    active = {directions[f] for f in off if directions.get(f, "none") != "none"}
    both_endpoints_off = len(off) == len(tiers) and len(tiers) > 1
    if both_endpoints_off and active == {"up"}:
        return ERROR_RANGE_SHIFT_UP, detail
    if both_endpoints_off and active == {"down"}:
        return ERROR_RANGE_SHIFT_DOWN, detail
    return ERROR_BOUNDARY_MISREAD, detail


def error_typology(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    fields: tuple[str, ...] = ENDPOINT_FIELDS,
    mode: str = "auto",
    bin_widths: dict[str, float] | None = None,
    myr_tolerances: dict[str, float] | None = None,
    include_rows: bool = True,
) -> dict[str, Any]:
    """BORROW-2026-09-20 (CHOCOLATE): label every row with ONE error type.

    Pairing is deterministic and single-pass: strict-normalized name first,
    then qualifier-insensitive, then a same-genus fuzzy near-miss; each GT row
    is claimed by at most one predicted row (later duplicates read as
    hallucinations). GT rows nobody claimed become ``omission`` — unless the
    model had explicitly refused them via ``response_kind``, in which case the
    refusal claims the cell so it is not double-counted as an omission; the
    coverage cost of that declination is reported by ``refusal_metrics``
    (``refusal_rate`` up, ``recall_on_answered`` down) instead.

    Returns:
        ``{labels, counts, rates, n_rows, n_predicted, n_ground_truth,
        n_errors, error_rate, refused_rows, aux_counts, reason_code_counts,
        refusal_field_present, rows[]}`` with ``rates`` over labelled rows.
    """
    pred_rows = [row for row in predicted if isinstance(row, dict)]
    gt_rows = [row for row in ground_truth if isinstance(row, dict)]

    gt_by_key: dict[str, int] = {}
    gt_by_lenient: dict[str, list[int]] = {}
    for idx, row in enumerate(gt_rows):
        key = _normalize_taxon(row.get("species", ""))
        if not key:
            continue
        gt_by_key.setdefault(key, idx)
        lenient = _normalize_taxon(row.get("species", ""), preserve_qualifiers=False)
        if lenient:
            gt_by_lenient.setdefault(lenient, []).append(idx)

    claimed: set[int] = set()
    rows: list[dict[str, Any]] = []
    aux_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}

    def _label_row(entry: dict[str, Any]) -> None:
        """Collect one labelled row and tally its auxiliary tag."""
        aux = entry.get("aux")
        if aux:
            aux_counts[aux] = aux_counts.get(aux, 0) + 1
        rows.append(entry)

    for pred_row in pred_rows:
        species = pred_row.get("species", "")
        key = _normalize_taxon(species)
        refusal = _row_is_refusal(pred_row)
        codes = _reason_codes(pred_row)
        for code in codes:
            reason_code_counts[code] = reason_code_counts.get(code, 0) + 1

        match_idx: int | None = None
        similarity = NAME_EXACT
        if key and key in gt_by_key and gt_by_key[key] not in claimed:
            match_idx = gt_by_key[key]
        else:
            lenient_key = _normalize_taxon(species, preserve_qualifiers=False)
            candidates = [i for i in gt_by_lenient.get(lenient_key or "", []) if i not in claimed]
            if candidates:
                match_idx, similarity = candidates[0], NAME_QUALIFIER
            else:
                # Same-genus fuzzy near-miss against the still-unclaimed rows.
                best: tuple[float, int] | None = None
                for idx, gt_row in enumerate(gt_rows):
                    if idx in claimed:
                        continue
                    similarity_name = _name_similarity(key, _normalize_taxon(gt_row.get("species", "")))
                    if similarity_name != NAME_FUZZY:
                        continue
                    ratio = difflib.SequenceMatcher(
                        None, key, _normalize_taxon(gt_row.get("species", "")),
                    ).ratio()
                    if best is None or ratio > best[0]:
                        best = (ratio, idx)
                if best is not None:
                    match_idx, similarity = best[1], NAME_FUZZY

        if refusal:
            # Honest declination: claim the cell so it is not an omission.
            if match_idx is not None:
                claimed.add(match_idx)
            _label_row({
                "species": species, "label": REFUSAL_LABEL, "aux": refusal,
                "response_kind": refusal, "reason_codes": codes,
            })
            continue

        if match_idx is None:
            _label_row({
                "species": species, "label": ERROR_HALLUCINATION,
                "reason_codes": codes,
                "detail": {"note": "no ground-truth taxon within the matching policy"},
            })
            continue

        claimed.add(match_idx)
        label, detail = _classify_matched_row(
            pred_row, gt_rows[match_idx],
            name_similarity=similarity, fields=fields, mode=mode,
            bin_widths=bin_widths, myr_tolerances=myr_tolerances,
        )
        entry: dict[str, Any] = {
            "species": gt_rows[match_idx].get("species", species),
            "predicted_species": species,
            "label": label,
            "reason_codes": codes,
            "detail": detail,
        }
        if similarity != NAME_EXACT:
            entry["aux"] = f"name_{similarity}"
            if label == ERROR_CORRECT and similarity == NAME_QUALIFIER:
                # Right identification, hedged spelling: never silently perfect.
                entry["note"] = "qualifier_only_difference"
        elif detail.get("note"):
            # "no_boundary_delivered" / "unscorable_endpoints" stay visible in
            # aux_counts so a name-only answer cannot inflate the correct rate.
            entry["aux"] = str(detail["note"])
        _label_row(entry)

    for idx, gt_row in enumerate(gt_rows):
        if idx in claimed:
            continue
        _label_row({
            "species": gt_row.get("species", ""),
            "label": ERROR_OMISSION,
            "detail": {"ground_truth": {f: gt_row.get(f) for f in fields}},
        })

    counts = {label: 0 for label in ERROR_LABELS}
    refused_rows = 0
    for entry in rows:
        if entry["label"] == REFUSAL_LABEL:
            refused_rows += 1
            continue
        counts[entry["label"]] = counts.get(entry["label"], 0) + 1
    labelled = len(rows) - refused_rows
    rates = {
        label: (round(counts[label] / labelled, 4) if labelled else 0.0)
        for label in ERROR_LABELS
    }
    error_total = labelled - counts[ERROR_CORRECT]
    result: dict[str, Any] = {
        "labels": list(ERROR_LABELS),
        "counts": counts,
        "rates": rates,
        "n_rows": labelled,
        "n_predicted": len(pred_rows),
        "n_ground_truth": len(gt_rows),
        "n_errors": error_total,
        "error_rate": round(error_total / labelled, 4) if labelled else 0.0,
        "refused_rows": refused_rows,
        "aux_counts": aux_counts,
        "reason_code_counts": reason_code_counts,
        # Degradation flag: no response_kind anywhere -> refusals are invisible
        # and every unanswered cell stayed an omission (pre-BORROW behaviour).
        "refusal_field_present": _refusal_field_present(pred_rows),
    }
    if include_rows:
        result["rows"] = rows
    return result


# ---------------------------------------------------------------------------
# BORROW-2026-09-20 (CharXiv): split tracks + honest refusal rate
# ---------------------------------------------------------------------------


def refusal_metrics(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    species_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report refusal / uncertainty rate SIDE BY SIDE with precision.

    A declinated cell ("this taxon range is not drawn on the chart", "I am not
    sure") is coverage loss, not a wrong answer, so it must be visible next to
    the precision it protects. The legacy trio ``precision`` / ``recall`` /
    ``f1`` is kept verbatim over ALL predicted rows (backward compatible), and
    the ``*_on_answered`` companions recompute it over the rows the model
    actually committed to — that is where refusing half the chart shows up as
    a recall cost instead of a flattering precision.

    When no row carries the optional ``response_kind`` field, the scorer
    degrades to the current behaviour: zero refusal counts with
    ``refusal_field_present: False`` and ``recall_as_omission: True`` (every
    unanswered cell is still charged as an omission).
    """
    pred_rows = [row for row in predicted if isinstance(row, dict)]
    not_drawn = sum(1 for row in pred_rows if _row_is_refusal(row) == "not_drawn")
    uncertain = sum(1 for row in pred_rows if _row_is_refusal(row) == "uncertain")
    n_predicted = len(pred_rows)
    answered = sum(1 for row in pred_rows if not _row_is_refusal(row))
    present = _refusal_field_present(pred_rows)

    metrics = species_metrics or species_precision_recall(pred_rows, [
        row for row in ground_truth if isinstance(row, dict)
    ])
    answered_metrics = species_precision_recall(
        [row for row in pred_rows if not _row_is_refusal(row)],
        [row for row in ground_truth if isinstance(row, dict)],
    ) if answered else {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    n_gt = len([row for row in ground_truth if isinstance(row, dict)])
    reason_code_counts: dict[str, int] = {}
    for row in pred_rows:
        for code in _reason_codes(row):
            reason_code_counts[code] = reason_code_counts.get(code, 0) + 1

    def _rate(count: int) -> float:
        return round(count / n_predicted, 4) if n_predicted else 0.0

    return {
        "refusal_field_present": present,
        "n_predicted": n_predicted,
        "n_ground_truth": n_gt,
        "n_answered": answered,
        "n_not_drawn": not_drawn,
        "n_uncertain": uncertain,
        "n_refused": not_drawn + uncertain,
        "not_drawn_rate": _rate(not_drawn),
        "uncertain_rate": _rate(uncertain),
        "refusal_rate": _rate(not_drawn + uncertain),
        "answer_coverage": _rate(answered),
        "precision": metrics.get("precision", 0.0),
        "recall": metrics.get("recall", 0.0),
        "f1": metrics.get("f1", 0.0),
        # BORROW-2026-09-20: the same trio computed over committed answers only,
        # so declination costs coverage (recall) instead of buying precision.
        "precision_on_answered": answered_metrics.get("precision", 0.0),
        "recall_on_answered": answered_metrics.get("recall", 0.0),
        "f1_on_answered": answered_metrics.get("f1", 0.0),
        "reason_code_counts": reason_code_counts,
        # Honest degradation note (BORROW-2026-09-20): without response_kind a
        # refusal cannot be distinguished from a miss.
        "recall_as_omission": not present,
    }


def _matched_pair_indices(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Strictly name-matched (predicted row, GT row) pairs, refusals excluded.

    Strict matching on purpose: the reasoning track measures what the model
    read at a place it named identically to the annotation, so a mis-identified
    column cannot leak into the pairwise comparisons (typology owns that case).
    """
    gt_by_key: dict[str, dict[str, Any]] = {}
    for row in ground_truth:
        if not isinstance(row, dict):
            continue
        key = _normalize_taxon(row.get("species", ""))
        if key:
            gt_by_key.setdefault(key, row)
    pairs = []
    for row in predicted:
        if not isinstance(row, dict) or _row_is_refusal(row):
            continue
        key = _normalize_taxon(row.get("species", ""))
        gt_row = gt_by_key.get(key)
        if gt_row is not None:
            pairs.append((row, gt_row))
    return pairs


def _bed_num(row: dict[str, Any], field: str) -> float | None:
    """Comparable up-section position of one endpoint (bed index, else age)."""
    bed = _parse_bed(row.get(field))
    if bed is not None:
        try:
            return float(bed["bed_num"]) + (0.001 if _bed_sub(bed) else 0.0)
        except (TypeError, ValueError):  # pragma: no cover
            return None
    return _age_in_myr(row.get(field), require_unit=False)


def _track_component(score: float | None, agreement: int, comparisons: int, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Uniform component shape: ``{score, agreements, comparisons, ...}``."""
    payload: dict[str, Any] = {
        "score": None if score is None else round(score, 4),
        "agreements": agreement,
        "comparisons": comparisons,
    }
    if extra:
        payload.update(extra)
    return payload


def _zone_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Biozone labels lifted out of species rows so ``biozone_accuracy`` reads them.

    ``species_ranges`` carries the zone in ``biozone`` while the dedicated
    ``biozones`` block carries it in ``name``; the descriptive track wants the
    label either way, and a chart that annotates no zones at all still yields
    an empty list (component dropped, never scored as 0).
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and row.get("biozone"):
            out.append({"name": row.get("biozone")})
    return out


def descriptive_track(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    biozone_predicted: list[dict[str, Any]] | None = None,
    biozone_ground_truth: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """CharXiv "descriptive" track: reading axes / headers / labels.

    Components are label-level reads: taxon name identity, biozone label,
    section header identity. A missing component (nothing annotated) is
    dropped from the mean instead of scoring 0.
    """
    pred_rows = [row for row in predicted if isinstance(row, dict)]
    gt_rows = [row for row in ground_truth if isinstance(row, dict)]

    true_species = {_normalize_taxon(row.get("species", "")) for row in gt_rows}
    true_species.discard("")
    answered_pred = [row for row in pred_rows if not _row_is_refusal(row)]
    pred_answered_names = {_normalize_taxon(row.get("species", "")) for row in answered_pred}
    pred_answered_names.discard("")
    name_hits = len(pred_answered_names & true_species)
    name_total = len(pred_answered_names | true_species)
    name_component = _track_component(
        (name_hits / name_total) if name_total else None,
        name_hits, name_total,
        {"note": "exact normalized taxon labels, union over both sides"},
    )

    components: dict[str, Any] = {"taxon_label": name_component}

    bz_pred = biozone_predicted if biozone_predicted is not None else _zone_rows(answered_pred)
    bz_true = biozone_ground_truth if biozone_ground_truth is not None else _zone_rows(gt_rows)
    zones = biozone_accuracy(bz_pred, bz_true)
    if zones.get("precision") or zones.get("recall") or zones.get("exact") or zones.get("missed"):
        components["biozone_label"] = _track_component(
            zones["f1"] if (zones["precision"] + zones["recall"]) else None,
            zones["exact"] + zones["fuzzy"],
            zones["exact"] + zones["fuzzy"] + zones["missed"],
        )

    gt_sections = {
        _normalize_taxon(row.get("section", "")) for row in gt_rows if row.get("section")
    }
    pred_sections = {
        _normalize_taxon(row.get("section", "")) for row in answered_pred if row.get("section")
    }
    gt_sections.discard("")
    pred_sections.discard("")
    if gt_sections:
        # Symmetric like the taxon-label component: inventing a section header
        # the chart does not have is a descriptive error, not free coverage.
        hits = len(pred_sections & gt_sections)
        total = len(pred_sections | gt_sections)
        components["section_header"] = _track_component(
            (hits / total) if total else None, hits, total,
        )

    usable = [c["score"] for c in components.values() if c["score"] is not None]
    return {
        "track": "descriptive",
        "score": round(sum(usable) / len(usable), 4) if usable else 0.0,
        "components": components,
    }


def reasoning_track(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    bin_widths: dict[str, float] | None = None,
) -> dict[str, Any]:
    """CharXiv "reasoning" track: cross-column comparison, range intersection.

    Every component here compares TWO things at once, which is what reading a
    range chart actually requires beyond transcribing a label:
      * ``range_intersection`` — is the co-occurrence relation between two
        taxa preserved, and to within one coarse step of the annotated overlap?
      * ``top_order`` / ``base_order`` — does the ordering between taxa (who
        ranges higher in the section) survive?
      * ``range_extent`` — is each range as long as annotated, which needs BOTH
        endpoints of one column and the section context to be right?

    Rows the model declined (``response_kind``) are excluded, so the track
    measures reasoning quality, not coverage (``refusal_metrics`` owns that).
    """
    widths = {**DEFAULT_BIN_WIDTHS, **(bin_widths or {})}
    pairs = _matched_pair_indices(predicted, ground_truth)
    # One comparison per distinct species column, in input order.
    seen: set[str] = set()
    named: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row, gt_row in pairs:
        key = _normalize_taxon(row.get("species", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        named.append((row, gt_row))

    # -- range intersection / co-occurrence --------------------------------
    overlap_agree = overlap_total = 0
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            pa, ga = named[i]
            pb, gb = named[j]
            pred_ext = _overlap_extent(pa, pb)
            true_ext = _overlap_extent(ga, gb)
            if pred_ext is None or true_ext is None:
                continue
            overlap_total += 1
            agrees = (pred_ext > 0.0) == (true_ext > 0.0)
            if agrees and pred_ext > 0.0:
                # Co-occurring: the overlap itself must stay inside the coarse
                # window, or the "intersection" is a different palaeontological
                # statement (shared interval vs merely shared bed).
                agrees = abs(pred_ext - true_ext) <= max(1.0, float(widths.get("coarse", 2.0)))
            overlap_agree += 1 if agrees else 0

    # -- cross-taxon ordering (first / last appearance) --------------------
    def _order_component(field: str) -> dict[str, Any]:
        agree = total = 0
        for i in range(len(named)):
            for j in range(i + 1, len(named)):
                pa, ga = named[i]
                pb, gb = named[j]
                x_p, y_p = _bed_num(pa, field), _bed_num(pb, field)
                x_g, y_g = _bed_num(ga, field), _bed_num(gb, field)
                if None in (x_p, y_p, x_g, y_g):
                    continue
                total += 1
                agrees = (x_p > y_p) == (x_g > y_g) and (x_p < y_p) == (x_g < y_g)
                agree += 1 if agrees else 0
        return _track_component((agree / total) if total else None, agree, total)

    # -- range length in context ------------------------------------------
    extent_agree = extent_total = 0
    for row, gt_row in named:
        pred_len, true_len = _span(row), _span(gt_row)
        if pred_len is None or true_len is None:
            continue
        extent_total += 1
        extent_agree += 1 if abs(pred_len - true_len) <= max(1.0, float(widths.get("adjacent", 1.0))) else 0

    components = {
        "range_intersection": _track_component(
            (overlap_agree / overlap_total) if overlap_total else None,
            overlap_agree, overlap_total,
            {"note": "pairwise co-occurrence plus coarse-step extent tolerance"},
        ),
        "top_order": _order_component("range_top"),
        "base_order": _order_component("range_base"),
        "range_extent": _track_component(
            (extent_agree / extent_total) if extent_total else None,
            extent_agree, extent_total,
        ),
    }

    usable = [c["score"] for c in components.values() if c["score"] is not None]
    return {
        "track": "reasoning",
        "score": round(sum(usable) / len(usable), 4) if usable else 0.0,
        "components": components,
        "n_columns_compared": len(named),
    }


def _span(row: dict[str, Any]) -> float | None:
    """Stratigraphic thickness of a range in bins (base -> top)."""
    lo, hi = _bed_num(row, "range_base"), _bed_num(row, "range_top")
    if lo is None or hi is None:
        return None
    return abs(hi - lo)


def _overlap_extent(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """Bins of co-occurrence between two ranges (0.0 when disjoint)."""
    a_lo, a_hi = _bed_num(a, "range_base"), _bed_num(a, "range_top")
    b_lo, b_hi = _bed_num(b, "range_base"), _bed_num(b, "range_top")
    if None in (a_lo, a_hi, b_lo, b_hi):
        return None
    a_lo, a_hi = min(a_lo, a_hi), max(a_lo, a_hi)
    b_lo, b_hi = min(b_lo, b_hi), max(b_lo, b_hi)
    return max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))


def track_scores(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    bin_widths: dict[str, float] | None = None,
    biozone_predicted: list[dict[str, Any]] | None = None,
    biozone_ground_truth: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """BORROW-2026-09-20 (CharXiv): score descriptive vs reasoning SEPARATELY.

    Deliberately returns no blended number: a model that transcribes labels
    perfectly and reasons about ranges badly must not be able to hide it in a
    single average (and vice versa).
    """
    return {
        "descriptive": descriptive_track(
            predicted, ground_truth,
            biozone_predicted=biozone_predicted, biozone_ground_truth=biozone_ground_truth,
        ),
        "reasoning": reasoning_track(predicted, ground_truth, bin_widths=bin_widths),
        "policy": "per-track scores only; no blended headline number by design",
    }


# ---------------------------------------------------------------------------
# BORROW-2026-09-20: one-call report builder (legacy keys preserved)
# ---------------------------------------------------------------------------


def tiered_eval_report(
    predicted: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    mode: str = "auto",
    fields: tuple[str, ...] = ENDPOINT_FIELDS,
    bin_widths: dict[str, float] | None = None,
    myr_tolerances: dict[str, float] | None = None,
    weights: dict[str, float] | None = None,
    tolerance: int = 1,
    biozone_predicted: list[dict[str, Any]] | None = None,
    biozone_ground_truth: list[dict[str, Any]] | None = None,
    include_rows: bool = False,
) -> dict[str, Any]:
    """Full evaluation dict: legacy metrics PLUS the BORROW-2026-09-20 layers.

    Backward compatibility: the keys ``species_precision_recall``,
    ``range_top_accuracy``, ``range_base_accuracy`` and their payloads keep
    the exact shape the legacy CI report (``scripts/gold_report.py``) and
    ``tests/test_gold_*.py`` read. The new blocks — ``boundary_tiers``,
    ``error_typology``, ``tracks``, ``refusals`` — are additive siblings.

    ``biozone_predicted`` / ``biozone_ground_truth`` feed the descriptive
    track's zone-label component; omitted, it falls back to the ``biozone``
    carried on the species rows.
    """
    pred_rows = [row for row in predicted if isinstance(row, dict)]
    gt_rows = [row for row in ground_truth if isinstance(row, dict)]

    species = species_precision_recall(pred_rows, gt_rows)
    tiers = {
        field.split("range_", 1)[-1]: boundary_tier_accuracy(
            pred_rows, gt_rows, field=field, mode=mode,
            bin_widths=bin_widths, myr_tolerances=myr_tolerances, weights=weights,
        )
        for field in fields
    }
    typology = error_typology(
        pred_rows, gt_rows, fields=fields, mode=mode,
        bin_widths=bin_widths, myr_tolerances=myr_tolerances,
        include_rows=include_rows,
    )
    return {
        # ---- legacy block (keys and shapes unchanged) -------------------
        "species_precision_recall": species,
        "range_top_accuracy": range_top_accuracy(pred_rows, gt_rows, tolerance),
        "range_base_accuracy": range_base_accuracy(pred_rows, gt_rows, tolerance),
        # ---- BORROW-2026-09-20 block ------------------------------------
        "boundary_tiers": {
            "fields": tiers,
            "row": range_tier_accuracy(
                pred_rows, gt_rows, fields=fields, mode=mode,
                bin_widths=bin_widths, myr_tolerances=myr_tolerances, weights=weights,
            )["row"],
        },
        "error_typology": typology,
        "tracks": track_scores(
            pred_rows, gt_rows, bin_widths=bin_widths,
            biozone_predicted=biozone_predicted,
            biozone_ground_truth=biozone_ground_truth,
        ),
        "refusals": refusal_metrics(pred_rows, gt_rows, species_metrics=species),
        # TODO(BORROW-2026-09-20): no per-gate ablation switches exist yet, so
        # this report cannot be re-run with a single gate disabled.
    }
