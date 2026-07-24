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

import re
from typing import Any, Iterable

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


# Top-level arrays that count as primary "content" for the extraction result.
# When ALL of these are present-but-empty the model claims to have run an
# extraction but produced no rows — that's a "pure miss" and should grade F.
_CONTENT_KEYS: tuple[str, ...] = (
    "species_ranges", "abundances", "sections", "biozones",
    "other_fossils", "cross_beds", "lithology_legend",
    "fossil_legend", "age_units",
)

# Columnar-mode marker keys — presence of any of these shifts the active
# mode from range-chart to columnar-section, and completeness checks for
# range-chart-only fields (species_ranges, abundances, biozones,
# other_fossils) are skipped.
_COLUMNAR_MARKERS: tuple[str, ...] = (
    "cross_beds", "lithology_legend", "fossil_legend",
)


def _detect_mode(data: dict[str, Any]) -> str:
    """Identify which extraction mode the data belongs to.

    Returns one of: ``"columnar"``, ``"abundance"``, ``"range_chart"``.
    Columnar wins ties because cross_beds / lithology_legend are unique
    markers that can't appear in range-chart output."""
    if not isinstance(data, dict):
        return "range_chart"
    for key in _COLUMNAR_MARKERS:
        v = data.get(key)
        if v is None:
            continue
        # Only treat a columnar marker as authoritative when non-empty —
        # an explicitly-empty marker shouldn't force the mode.
        if isinstance(v, list) and len(v) > 0:
            return "columnar"
        if isinstance(v, dict) and len(v) > 0:
            return "columnar"
        if isinstance(v, str) and v.strip():
            return "columnar"
    # Abundance mode: abundances present, species_ranges absent.
    if "abundances" in data and "species_ranges" not in data:
        return "abundance"
    return "range_chart"


def _is_pure_extraction_miss(data: dict[str, Any]) -> bool:
    """True when the model ran an extraction but produced NO content rows.

    Specifically: at least one of the primary content keys is present
    (``species_ranges`` / ``abundances`` / ``sections`` / …), but every
    such key that IS present is empty / null.  An absent key doesn't count
    as a miss — the chart simply didn't have that information — but a
    present-and-empty key is a strong signal that extraction failed
    silently."""
    seen_any = False
    for key in _CONTENT_KEYS:
        v = data.get(key)
        if v is None:
            continue
        seen_any = True
        if isinstance(v, list) and len(v) > 0:
            return False
        if isinstance(v, dict) and len(v) > 0:
            return False
        if isinstance(v, str) and v.strip():
            return False
    return seen_any


_BED_RE = re.compile(r"-?\d+")


def _parse_bed_n(value: Any) -> int | None:
    """Parse a bed indicator like ``"Bed 9"``, ``"bed-7"``, ``"5"``, or ``5``
    into an integer.  Returns ``None`` for empty / unparsable values."""
    if value is None:
        return None
    # bool is a subclass of int — exclude explicitly.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    m = _BED_RE.search(s)
    return int(m.group()) if m else None


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
    """How many expected fields are populated vs. empty.

    Mode-aware: columnar / abundance / range-chart each have their own set
    of expected fields.  Irrelevant keys (e.g. ``species_ranges`` when the
    chart is a columnar section) are NOT counted as missing, and the
    non-existence is not surfaced as an issue."""
    issues: list[dict[str, str]] = []
    checks = 0
    passed = 0

    mode = _detect_mode(data)

    # Mode-specific primary field selection.
    if mode == "abundance":
        primary = "abundances"
    elif mode == "columnar":
        primary = None  # columnar has no single "primary" — no single penalty
    else:  # range_chart
        primary = "species_ranges" if "species_ranges" in data else None

    if primary:
        checks += 1
        rows = data.get(primary) or []
        if isinstance(rows, list) and len(rows) > 0:
            passed += 1
        else:
            issues.append({"severity": "warning",
                           "msg_key": "quality.empty_primary_rows"})

    # Mode-appropriate non-primary top-level arrays.  Only count as a
    # check those that are actually meaningful in this mode — and only
    # emit ``missing_top_level`` when the chart genuinely has NO signal
    # in any of the relevant keys.  Sparse columnar / abundance results
    # are still valid (e.g. only ``sections`` + ``cross_beds``).
    if mode == "columnar":
        relevant_keys = ("sections", "cross_beds", "lithology_legend",
                         "fossil_legend")
    elif mode == "abundance":
        relevant_keys = ("abundances", "sections")
    else:
        relevant_keys = ("sections", "biozones", "other_fossils")

    # The chart has mode-signal if at least one relevant key is non-empty.
    has_mode_signal = any(
        isinstance(data.get(k), list) and len(data.get(k) or []) > 0
        for k in relevant_keys
    )

    # Only emit ``missing_top_level`` when the chart is essentially empty
    # for this mode.  Pure-extraction-miss already short-circuits the
    # score, so this only fires when SOME content exists but ALL of the
    # other "expected" keys are absent.
    for key in relevant_keys:
        checks += 1
        val = data.get(key)
        if val is not None:
            passed += 1  # present, even if empty — already flagged empty_primary_rows
        else:
            # Key absent — only penalise when truly no signal at all.
            if not has_mode_signal:
                issues.append({"severity": "info",
                               "msg_key": "quality.missing_top_level"})
            passed += 1  # don't drop the score for absent optional fields

    # Confidence should be present in every mode.
    checks += 1
    conf = data.get("confidence")
    if conf is not None and isinstance(conf, (int, float)):
        passed += 1
    else:
        issues.append({"severity": "info", "msg_key": "quality.missing_confidence"})

    score = passed / checks if checks else 0.0
    return min(1.0, max(0.0, score)), issues


def _score_accuracy(data: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """Geological plausibility: section references resolve, FAD<LAD is
    respected (range-chart species), and bed indices are ordered
    top >= base (columnar-section blocks / age_units)."""
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

    # FAD<LAD check: each species_ranges row's range_top must be at least
    # as young (>=) as its range_base.  Convention from the prompt: beds are
    # 1-indexed from the bottom, so a younger bed has the LARGER index.
    # Inverting top<base means the model misread the chart.
    fad_lad_violations = 0
    fad_lad_total = 0
    for row in species_rows:
        if not isinstance(row, dict):
            continue
        top = _parse_bed_n(row.get("range_top"))
        base = _parse_bed_n(row.get("range_base"))
        if top is None or base is None:
            continue  # can't validate — skip, don't penalise
        fad_lad_total += 1
        if top < base:
            fad_lad_violations += 1
            issues.append({"severity": "warning",
                           "msg_key": "quality.range_top_lt_base"})
    if fad_lad_total > 0:
        checks += 1
        if fad_lad_violations == 0:
            passed += 1
        else:
            # Each violation reduces proportionally — severe when ALL inverted.
            passed += max(0.0, 1.0 - fad_lad_violations / fad_lad_total)

    # Columnar-section bed-order check: every lithology_block / age_unit
    # row with both range_top_idx and range_base_idx must satisfy top>=base.
    # The extractor.py B-3 fix already swaps inverted pairs and emits
    # ``_warning="index_order_swap"`` — we surface that AND re-check (defense
    # in depth, in case the swap pipeline is bypassed).
    bed_violations = 0
    bed_total = 0
    bed_swapped = 0
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        blocks_list: Iterable[Any] = (
            list(sec.get("lithology_blocks") or [])
            + list(sec.get("age_units") or [])
        )
        for block in blocks_list:
            if not isinstance(block, dict):
                continue
            top = _parse_bed_n(block.get("range_top_idx"))
            base = _parse_bed_n(block.get("range_base_idx"))
            if top is None or base is None:
                continue
            bed_total += 1
            if top < base:
                bed_violations += 1
                issues.append({"severity": "warning",
                               "msg_key": "quality.bed_index_order_invalid"})
            if block.get("_warning") == "index_order_swap":
                bed_swapped += 1
                issues.append({"severity": "info",
                               "msg_key": "quality.bed_index_order_swapped"})
    if bed_total > 0:
        checks += 1
        if bed_violations == 0:
            passed += 1
        else:
            # Severe inversion penalty when the model consistently inverts.
            passed += max(0.0, 1.0 - bed_violations / bed_total)

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
    """Shape: _extras ratio, array lengths, no unexpected nulls.

    Mode-aware: in columnar mode the relevant top-level keys are different
    (``cross_beds`` / ``lithology_legend`` / ``fossil_legend`` vs the
    range-chart trio of ``sections`` / ``biozones`` / ``other_fossils``)."""
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

    # Mode-appropriate "no top-level null" check.
    mode = _detect_mode(data)
    if mode == "columnar":
        null_check_keys = ("sections", "cross_beds", "lithology_legend",
                           "fossil_legend", "confidence")
    elif mode == "abundance":
        null_check_keys = ("sections", "abundances", "confidence")
    else:
        null_check_keys = ("sections", "biozones", "other_fossils",
                           "confidence")

    checks += 1
    nulls = 0
    for key in null_check_keys:
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

    # C5 fix: detect "the model ran an extraction but produced nothing".
    # Previously this case scored 0.94 / A because all-empty arrays didn't
    # trigger any check — a serious false-positive for the user.
    if _is_pure_extraction_miss(data):
        return {"score": 0.0, "grade": "F",
                "issues": [{"severity": "warning",
                            "msg_key": "quality.empty_result"}]}

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
