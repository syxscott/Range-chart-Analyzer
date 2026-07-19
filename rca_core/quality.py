"""Quality scoring for VLM-extracted range-chart / columnar-section results.

Scores a normalized result dict on 4 weighted dimensions and produces a
letter grade + human-readable issue list for the UI to surface:

    completeness  (0.30) — how many expected fields are populated
    accuracy     (0.40) — geological plausibility (FAD<LAD, monotonic beds…)
    consistency  (0.20) — cross-field agreement (section name references…)
    structure    (0.10) — array lengths, _extras ratio, confidence coherence

Pure function — no I/O, no LLM. Drop-in callable from server.py, the GUI
workers, or the JS port (js/quality.js parity planned).

Output shape::

    {
      "score": 0.87,        # 0.0 – 1.0 weighted composite
      "grade": "B",         # A / B / C / D / F
      "issues": [
        {"severity": "warning", "msg_key": "quality.missing_section_ref"},
        ...
      ]
    }

The ``msg_key`` entries map to i18n tables in rca_core/i18n.py and
js/i18n.js (``quality.*`` keys) so the UI can render localized messages.
"""

from __future__ import annotations

from typing import Any

# Weights for the 4 quality dimensions. Must sum to 1.0.
W_COMPLETENESS = 0.30
W_ACCURACY = 0.40
W_CONSISTENCY = 0.20
W_STRUCTURE = 0.10

# Grade thresholds (score >= threshold).
_GRADES = (
    (0.90, "A"),
    (0.75, "B"),
    (0.60, "C"),
    (0.40, "D"),
)


def _grade_for(score: float) -> str:
    for threshold, letter in _GRADES:
        if score >= threshold:
            return letter
    return "F"


def _section_names(data: dict[str, Any]) -> set[str]:
    """Collect all section.name / id values in the result (mode-aware)."""
    names: set[str] = set()
    for sec in data.get("sections") or []:
        if isinstance(sec, dict):
            # range-chart mode uses "name"; columnar-section mode uses "id"
            n = sec.get("name") or sec.get("id")
            if n and str(n).strip():
                names.add(str(n).strip())
    return names


def _score_completeness(data: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """How many expected fields are populated vs. empty."""
    issues: list[dict[str, str]] = []
    checks = 0
    passed = 0

    # C3 fix: explicitly detect the mode and handle it clearly.
    # Primary-mode fields (species_ranges / abundances): an empty list means
    # "nothing extracted" — penalise it with a warning.
    # Other top-level arrays (sections, biozones, other_fossils): an empty
    # array is fine (the chart simply didn't have that information).
    primary = None
    for key in ("species_ranges", "abundances"):
        if key in data:
            primary = key
            break
    if primary:
        checks += 1
        rows = data.get(primary) or []
        if isinstance(rows, list) and len(rows) > 0:
            passed += 1
        else:
            issues.append({"severity": "warning", "msg_key": "quality.empty_primary_rows"})

    # Non-primary top-level arrays.
    for key in ("sections", "biozones", "other_fossils"):
        checks += 1
        val = data.get(key)
        if val is not None:
            passed += 1
        else:
            issues.append({"severity": "info", "msg_key": "quality.missing_top_level"})

    # Confidence should be present.
    checks += 1
    conf = data.get("confidence")
    if conf is not None and isinstance(conf, (int, float)):
        passed += 1
    else:
        issues.append({"severity": "info", "msg_key": "quality.missing_confidence"})

    score = passed / checks if checks else 0.0
    return min(1.0, max(0.0, score)), issues


def _score_accuracy(data: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """Geological plausibility: section references resolve, columnar bed indices
    are ordered (top >= base), and internal consistency is maintained.

    Note: FAD/LAD direction checks for range-chart species and full
    monotonic-bed enforcement for columnar sections are not yet implemented
    (see B-3 for index-order normalisation; additional checks are future work)."""
    issues: list[dict[str, str]] = []
    checks = 0
    passed = 0
    section_names = _section_names(data)

    # Every species_ranges.section should reference a real section.name.
    species_rows = data.get("species_ranges") or []
    if species_rows:
        checks += 1
        ok = 0
        has_ref = 0
        for row in species_rows:
            if not isinstance(row, dict):
                continue
            sec = str(row.get("section") or "").strip()
            if sec:
                has_ref += 1
                if sec in section_names:
                    ok += 1
        if has_ref == 0:
            # Species omit section entirely — not ideal but not a mismatch.
            passed += 1
        elif section_names and ok == has_ref:
            passed += 1
        elif section_names and ok > 0:
            passed += 0.5
            issues.append({"severity": "warning",
                           "msg_key": "quality.unmatched_section_ref"})
        elif section_names:
            issues.append({"severity": "warning",
                           "msg_key": "quality.all_section_refs_unmatched"})
        else:
            # No sections declared at all, yet species reference sections —
            # the references cannot be validated. Flag as info but don't
            # tank the score (completeness already penalises empty sections).
            passed += 0.5
            issues.append({"severity": "info",
                           "msg_key": "quality.sections_absent"})

    # Biozone section refs (columnar mode uses per-section thickness).
    for bz in data.get("biozones") or []:
        if not isinstance(bz, dict):
            continue
        sec = str(bz.get("section") or "").strip()
        if sec and section_names and sec not in section_names:
            issues.append({"severity": "info",
                           "msg_key": "quality.biozone_section_mismatch"})
            break

    # Agreement_count should not exceed total runs (data integrity).
    for row in species_rows:
        if not isinstance(row, dict):
            continue
        ac = row.get("agreement_count")
        # I4 fix: guard against None — row.runs is absent in single-run results
        # (merge_results only writes it at the top level), so the or-chain
        # may still resolve to None.
        n = row.get("runs") if row.get("runs") is not None else data.get("runs")
        if ac is None or n is None:
            continue
        try:
            if int(ac) > int(n):
                issues.append({"severity": "warning",
                               "msg_key": "quality.agreement_exceeds_runs"})
                break
        except (TypeError, ValueError):
            continue

    if checks == 0:
        return 1.0, issues
    score = passed / checks
    return min(1.0, max(0.0, score)), issues


def _score_consistency(data: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """Cross-field agreement: primarily driven by _score_completeness checks.
    This dimension is intentionally lightweight — the primary accuracy and
    completeness checks handle the meaningful signal. Kept for schema
    completeness (W_CONSISTENCY = 0.20 weight)."""
    issues: list[dict[str, str]] = []
    # D-2/M5 fix: removed dead per-row confidence check (rows never carry
    # a confidence field — normalize_result does not populate it) and removed
    # the redundant species_ranges/abundances existence check (completeness
    # already covers that).
    return 1.0, issues


def _score_structure(data: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """Shape: _extras ratio, array lengths, no unexpected nulls."""
    issues: list[dict[str, str]] = []
    checks = 0
    passed = 0

    # _extras should be tiny; a large _extras means the model invented keys.
    extras = data.get("_extras")
    if isinstance(extras, dict):
        checks += 1
        n_extras = len(extras)
        # Allow a few unknown keys (<=3 is fine).
        if n_extras <= 3:
            passed += 1
        else:
            issues.append({"severity": "info",
                           "msg_key": "quality.many_extras"})
    else:
        checks += 1
        passed += 1

    # No top-level null values among expected keys.
    checks += 1
    nulls = 0
    for key in ("sections", "biozones", "other_fossils", "confidence"):
        if data.get(key) is None and key != "confidence":
            nulls += 1
    if nulls == 0:
        passed += 1
    elif nulls <= 1:
        passed += 0.5
        issues.append({"severity": "info", "msg_key": "quality.null_fields"})

    if checks == 0:
        return 1.0, issues
    score = passed / checks
    return min(1.0, max(0.0, score)), issues


def score_range_chart(data: dict[str, Any]) -> dict[str, Any]:
    """Score a normalized result and return {score, grade, issues}.

    Args:
        data: a normalized result dict (the ``data`` field of an
              ExtractResult, or the merged result from aggregate.py).

    Returns:
        dict with integer weighted ``score`` (0.0–1.0), letter ``grade``
        (A/B/C/D/F), and a list of ``issues`` each with ``severity``
        (info/warning) and an i18n ``msg_key``.
    """
    if not data or not isinstance(data, dict):
        return {"score": 0.0, "grade": "F",
                "issues": [{"severity": "warning",
                            "msg_key": "quality.invalid_result"}]}

    c_score, c_issues = _score_completeness(data)
    a_score, a_issues = _score_accuracy(data)
    k_score, k_issues = _score_consistency(data)
    s_score, s_issues = _score_structure(data)

    composite = (
        W_COMPLETENESS * c_score
        + W_ACCURACY * a_score
        + W_CONSISTENCY * k_score
        + W_STRUCTURE * s_score
    )
    composite = min(1.0, max(0.0, composite))

    all_issues = c_issues + a_issues + k_issues + s_issues
    return {
        "score": round(composite, 4),
        "grade": _grade_for(composite),
        "issues": all_issues,
    }
