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

try:
    from .standards.ics import (
        ics_age_compare,
        ics_parse_age_range,
        ics_resolve_age_bound,
        ics_era,
        ICS_2024 as _ICS_2024,
    )
    _HAS_ICS = True
except ImportError:
    _HAS_ICS = False

# BORROW-2026-09-20 (A): the coverage ledger turns "the chart drew a dash" into
# an ANSWERED cell and a silent omission into a measurable gap. Only imported
# for the ledger view — the four scored dimensions keep their old behaviour.
from .reason_codes import (  # noqa: E402
    RESPONSE_NOT_DRAWN,
    coverage_ledger,
    coverage_state,
)


def is_not_drawn(row: Any) -> bool:
    """True when a row is an explicit "the chart did not draw this range"."""
    return isinstance(row, dict) and coverage_state(row) == RESPONSE_NOT_DRAWN

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
    # UI-REVIEW-2026-09-07: zonation / correlation chart content keys so
    # _is_pure_extraction_miss also covers that mode.
    "zones", "correlations", "zonations",
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
    # Zonation mode (UI-REVIEW-2026-09-05): correlations is the unique
    # marker no other mode emits; zones alone is ambiguous (abundance
    # results also carry a zones list) so require the pair or a non-empty
    # correlations list.
    if "correlations" in data or ("zones" in data and "zonations" in data):
        if "species_ranges" not in data and "abundances" not in data:
            return "zonation"
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


def _warning_flags(value: Any) -> set:
    """Normalise a row's ``_warning`` field to a set of flag names.

    REVIEW-2026-09-10: the extractor writes a bare string when a single
    warning fires and a list when several do (extractor.py row builders), so
    consumers must accept both. Mirrors ``rcaWarningFlags`` in js/quality.js.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set)):
        return {str(v) for v in value if v}
    return {str(value)}


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


# REVIEW-2026-07-31: an age unit is only recognised when a NUMBER is
# directly followed by the unit ("260 Ma", "255.5Ma", "5 m.y."). The
# previous substring search `re.search(r"ma|myr", text, re.I)` matched ANY
# word containing "ma" (Madison, marine, Maokou, samples...) and then took
# the first number in the text as the age — so a perfectly valid bed pair
# like range_base="Madison 3" / range_top="Madison 6" was misread as an
# inverted age range (3 Ma < 6 Ma) and flagged as an FAD<LAD violation.
_AGE_UNIT_PATTERN = re.compile(
    r"(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)


def _looks_like_age(value: Any) -> bool:
    """True when the value carries an explicit numeric age unit.

    Used to route a range bound to the bed-index branch vs the Ma/stage
    branch of the FAD<LAD check.
    """
    return bool(_AGE_UNIT_PATTERN.search(str(value or "")))


def _resolve_age_ma(value: Any, prefer: str = "older") -> Optional[float]:
    """Resolve a free-text age/stage label (or numeric Ma) to a numeric Ma.

    Used by the FAD<LAD check to validate ranges expressed as ages or stage
    names rather than bed numbers. Delegates to
    :func:`rca_core.standards.ics.ics_resolve_age_bound`, which handles:
      1. explicit numeric Ma literals — range-aware, ``prefer`` selects the
         older ("259.51-254.14 Ma" -> 259.51) or younger end;
      2. Chinese stage aliases, series/epoch labels ("Late Permian"), and
         period names;
      3. ICS stage names — ``prefer`` selects that stage's base (older) or
         top (younger) bound, so a range whose two ends are labelled with the
         SAME stage still spans the stage instead of collapsing to a point.

    Returns ``None`` when the ICS table is unavailable or the value can't be
    resolved, so callers can skip the age/stage branch gracefully.
    """
    if value is None or not _HAS_ICS or ics_resolve_age_bound is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    _stage, ma = ics_resolve_age_bound(text, prefer=prefer)
    return ma


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
    elif mode == "zonation":
        primary = "zones" if "zones" in data else None
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
    elif mode == "zonation":
        relevant_keys = ("zones", "correlations", "zonations")
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
    #
    # REVIEW-2026-09-20: this loop used to do ``passed += 1`` on BOTH
    # branches, so these checks were unfailable — completeness called an
    # all-empty set of expected arrays "populated" (the docstring's "how many
    # expected fields are populated" never held), and every extra unfailable
    # check inflated ``checks`` and diluted the deductions of the checks that
    # could actually fail. Present-but-empty is now a real failure; an ABSENT
    # key stays forgiven, because a sparse result (a columnar section with
    # only ``sections`` + ``cross_beds``) is scientifically valid and the
    # extractor only writes keys it observed.
    empty_relevant: list[str] = []
    for key in relevant_keys:
        checks += 1
        val = data.get(key)
        if val is None:
            # Key absent — only penalise when truly no signal at all.
            if not has_mode_signal:
                issues.append({"severity": "info",
                               "msg_key": "quality.missing_top_level"})
            passed += 1  # don't drop the score for absent optional fields
        elif isinstance(val, list):
            if val:
                passed += 1
            elif key != primary:
                empty_relevant.append(key)
            # key == primary: the failed check above already reported
            # quality.empty_primary_rows, so no second message here.
        elif val:
            passed += 1  # non-list payload with content (dict / str / number)
        elif key != primary:
            empty_relevant.append(key)
    if empty_relevant:
        issues.append({"severity": "info", "msg_key": "quality.missing_top_level"})

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
        top_raw = row.get("range_top")
        base_raw = row.get("range_base")
        top = _parse_bed_n(top_raw)
        base = _parse_bed_n(base_raw)
        # A value carrying an age unit ("Ma"/"myr") is an age, not a bed
        # index; send it to the age/stage branch below instead of letting
        # _parse_bed_n misread "253 Ma" as bed 253. REVIEW-2026-07-31: the
        # unit must be a number+unit pair — substrings like "Madison" no
        # longer route bed labels into the age branch.
        looks_like_age = _looks_like_age(top_raw) or _looks_like_age(base_raw)
        if top is not None and base is not None and not looks_like_age:
            # Bed-number branch (existing): beds are 1-indexed from the
            # bottom, so a younger bed has the LARGER index; FAD (base)
            # must have the smaller index than LAD (top).
            fad_lad_total += 1
            if top < base:
                fad_lad_violations += 1
                issues.append({"severity": "warning",
                               "msg_key": "quality.range_top_lt_base"})
        elif _HAS_ICS:
            # M-1 fix (REVIEW-2026-07-25): when the model emits ages
            # ("260–255 Ma") or stage names instead of bed numbers, the
            # bed-number parse returns None and the original check silently
            # skipped — making it ineffective for most real range-chart
            # output. Resolve both bounds via ICS and compare numerically.
            # Convention: range_base = FAD = OLDER = LARGER Ma;
            # range_top = LAD = YOUNGER = SMALLER Ma. A valid range has
            # base_ma >= top_ma; an inversion (base_ma < top_ma) is a
            # violation.
            top_ma = _resolve_age_ma(top_raw, prefer="younger")
            base_ma = _resolve_age_ma(base_raw, prefer="older")
            if top_ma is not None and base_ma is not None:
                fad_lad_total += 1
                if base_ma < top_ma:
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
            # REVIEW-2026-09-10: the extractor emits `_warning` as a bare
            # string for one flag but as a LIST when two fire together (e.g.
            # ["range_top_idx_truncated", "index_order_swap"] - the normal
            # order when a float index was also floored). The old `== "..."`
            # test missed the swap in exactly that combination, so the
            # "indices were swapped" info never reached the operator.
            if "index_order_swap" in _warning_flags(block.get("_warning")):
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

    # H-5 fix: detect impossible age sequences (e.g., Jurassic above Cambrian).
    # Use a simple heuristic: flag when beds from radically different eons
    # (Paleozoic + Mesozoic or older + younger eras) appear in the same section.
    # A full ICS timescale lookup requires an external table; this catches the
    # most egregious impossible-orderings without one.
    _PALEOZOIC_RE = re.compile(
        r"\b(cambrian|ordovician|silurian|devonian|carboniferous|pennsylvanian|mississippian|permian)\b",
        re.IGNORECASE,
    )
    _MESOZOIC_RE = re.compile(
        r"\b(triassic|jurassic|cretaceous)\b",
        re.IGNORECASE,
    )
    _CENOZOIC_RE = re.compile(
        # "paleocene" is listed explicitly alongside "paleogene": a chart may
        # label an age at epoch level ("Paleocene") without naming the period,
        # and the JS mirror (js/quality.js cross-era detector) already matches
        # it — keeping both sides on the same term set prevents the frontend
        # and the server from disagreeing on the same chart.
        r"\b(paleocene|paleogene|neogene|quaternary|pleistocene|holocene|eocene|oligocene|miocene|pliocene)\b",
        re.IGNORECASE,
    )
    section_ages: dict[str, set[str]] = {}
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        sec_name = str(sec.get("name") or sec.get("id") or "").strip()
        if not sec_name:
            continue
        eras: set[str] = set()
        for block in list(sec.get("lithology_blocks") or []) + list(sec.get("age_units") or []):
            if not isinstance(block, dict):
                continue
            age_str = str(block.get("age") or "")
            if _PALEOZOIC_RE.search(age_str):
                eras.add("Paleozoic")
            if _MESOZOIC_RE.search(age_str):
                eras.add("Mesozoic")
            if _CENOZOIC_RE.search(age_str):
                eras.add("Cenozoic")
        if eras:
            section_ages[sec_name] = eras
    cross_era_violations = sum(
        1 for eras in section_ages.values()
        if len(eras) > 1
    )
    if cross_era_violations > 0:
        # M-1 fix (REVIEW-2026-07-25): boundary sections (e.g. K-Pg or
        # P-Tr) legitimately span more than one era, so a section
        # containing both Paleozoic and Mesozoic keywords is NOT a
        # hard error. Downgrade to a warning and only penalise
        # proportionally so single-boundary extraction isn't graded F.
        checks += 1
        passed += max(0.0, 1.0 - 0.5 * cross_era_violations)
        issues.append({
            "severity": "warning",
            "msg_key": "quality.ages_inconsistent",
            "params": {"count": str(cross_era_violations)},
        })

    # ICS-based cross-era accuracy check: detect Stage order reversals
    # using the actual ICS timescale lookup.
    if _HAS_ICS:
        ics_violations = _score_cross_era_accuracy(data.get("sections") or [])
        for v in ics_violations:
            checks += 1
            passed += 0
            issues.append({
                "severity": v.get("severity", "warning"),
                "msg_key": "quality.stage_order_reversed",
                "params": {"section": v.get("section", ""), "detail": v.get("issue", "")},
            })

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
        score = 1.0
    else:
        score = passed / checks

    # P1-8: abundance sum-to-100 check.
    # Deduct 0.05 per violating level, capped at 0.3 total.
    level_sums, sum_skipped = _abundance_percentage_buckets(data)
    sum_violations = _abundance_sum_violations(level_sums)
    if sum_violations:
        deduction = min(0.3, 0.05 * len(sum_violations))
        score = max(0.0, score - deduction)
        for v in sum_violations[:5]:
            issues.append({
                "severity": "warning",
                "msg_key": "quality.abundance_sum_violation",
                "params": {"sample": str(v.get("sample", "")), "sum": str(round(v.get("sum", 0), 1))},
            })
        issues.append({
            "severity": "info",
            "msg_key": "quality.abundance_sum_violation_count",
            "params": {"count": str(len(sum_violations))},
        })
    elif sum_skipped["no_level"] or sum_skipped["unparsable"]:
        # REVIEW-2026-09-20: "no violations" is not the same as "checked".
        # Percentage rows without a level, or whose value was not a number,
        # were dropped silently and the diagram scored as if the rule held.
        # Surface them (info, no deduction: the sparse-diagram penalty itself
        # is a prompt-level concern, see the ABUNDANCE prompt's sum-to-100
        # instruction) so the operator knows the rule never ran on those rows.
        issues.append({
            "severity": "info",
            "msg_key": "quality.null_fields",
        })

    return min(1.0, max(0.0, score)), issues


def _score_consistency(data: dict[str, Any]) -> tuple[float, list[dict[str, str]]]:
    """P1-3 (REVIEW-2026-07-25): actually compute consistency, instead of
    the previous hard-coded 1.0 which made the 0.20 weight a free 0.20
    bonus and prevented D/F grades from ever being reachable.

    What we check here (cross-field / per-row invariants that the other
    three dimensions do NOT cover):
      * FAD <= LAD on every species_ranges row (range_top >= range_base)
      * per-row agreement_count <= total_runs when present (catches
        over-merged rows where agreement denominator doesn't match runs)
      * chimera_warnings presence (only the aggregator sets this)
      * biozone plausibility: biozone string is non-empty for
        every species_ranges row
    """
    issues: list[dict[str, str]] = []
    score = 1.0

    # (1) Chimera warnings from the merge.
    chimera_warnings = data.get("chimera_warnings") or []
    if chimera_warnings:
        # Each dropped row degrades consistency by 0.1, floor 0.
        score -= min(0.5, 0.1 * len(chimera_warnings))
        for w in chimera_warnings[:5]:
            issues.append({
                "severity": "warning",
                "msg_key": "quality.chimera_dropped",
                "params": {"row": str(w.get("row", {}))[:200]},
            })

    species = data.get("species_ranges") or []
    if isinstance(species, list) and species:
        # (2) FAD <= LAD per row.
        # REVIEW-2026-07-31: age/stage bounds ("300 Ma", "Wuchiapingian")
        # are validated numerically in _score_accuracy; the bed-index
        # comparison here must skip them, or a valid age range like
        # base="300 Ma" / top="250 Ma" was flagged as inverted
        # (300 < 250 after _parse_bed_n read the leading integers).
        fad_violations = 0
        for sp in species:
            if not isinstance(sp, dict):
                continue
            top = sp.get("range_top")
            base = sp.get("range_base")
            if top is None or base is None:
                continue
            if _looks_like_age(top) or _looks_like_age(base):
                continue
            top_n = _parse_bed_n(top)
            base_n = _parse_bed_n(base)
            if top_n is not None and base_n is not None and top_n < base_n:
                fad_violations += 1
        if fad_violations:
            score -= min(0.3, 0.1 * fad_violations)
            issues.append({
                "severity": "warning",
                "msg_key": "quality.fad_lt_lad",
                "params": {"count": str(fad_violations)},
            })

        # (3) agreement_count <= total_runs (catches the bug pattern from
        # columnar secondary merge, even after P0-2 fixed it).
        total_runs = data.get("runs")
        over_agreed = 0
        if isinstance(total_runs, int) and total_runs > 0:
            for sp in species:
                ac = sp.get("agreement_count")
                if isinstance(ac, int) and ac > total_runs:
                    over_agreed += 1
        if over_agreed:
            score -= min(0.3, 0.2 * over_agreed)
            issues.append({
                "severity": "warning",
                "msg_key": "quality.agreement_overflow",
                "params": {"count": str(over_agreed)},
            })

        # (4) Every species row has a non-empty biozone label.
        missing_bz = sum(
            1 for sp in species
            # REVIEW-2026-09-20: ``or ""`` only guards None/falsy — a numeric
            # biozone cell (0, or a bare int from a hand-crafted payload) made
            # ``.strip()`` raise AttributeError. score_range_chart catches it
            # per dimension, so one bad cell zeroed the WHOLE consistency
            # dimension (0.20 of the composite) and replaced every real
            # message with a generic quality.scoring_failed.
            if isinstance(sp, dict)
            # BORROW-2026-09-20 (A): a row the chart explicitly did NOT draw
            # has no biozone to label — the dash is the answer. Penalising it
            # would punish exactly the honest reporting the new contract asks
            # for, so those rows are exempt from this check.
            and not is_not_drawn(sp)
            and not str(sp.get("biozone") or "").strip()
        )
        if missing_bz:
            score -= min(0.2, 0.05 * missing_bz)
            issues.append({
                "severity": "warning",
                "msg_key": "quality.missing_biozone",
                "params": {"count": str(missing_bz)},
            })

    # P1-12: Steno's Law biozone order check.
    biozone_violations, biozone_issues = _score_biozone_order(species, data.get("sections") or [])
    if biozone_violations:
        score -= min(0.3, 0.1 * biozone_violations)
        issues.extend(biozone_issues[:5])

    score = max(0.0, min(1.0, score))
    return score, issues


def _score_cross_era_accuracy(sections: list) -> list[dict[str, Any]]:
    """ICS-based Stage order check using the actual timescale lookup.

    For each section, parses the age_range string to extract Stage names,
    then checks that stages are in correct stratigraphic order (older stages
    should appear below/younger stages in the section).

    Returns a list of violation dicts with keys: section, issue, severity.
    """
    violations: list[dict[str, Any]] = []

    for sec in sections:
        if not isinstance(sec, dict):
            continue
        sec_name = str(sec.get("name") or sec.get("id") or "").strip()
        if not sec_name:
            continue

        age_range = str(sec.get("age_range") or "")
        stages = ics_parse_age_range(age_range)

        if len(stages) < 2:
            continue

        # Check consecutive stage pairs
        for i in range(len(stages) - 1):
            cmp_result = ics_age_compare(stages[i], stages[i + 1])
            if cmp_result is None:
                continue  # unknown stages - skip

            # In stratigraphic order, oldest (highest Ma) should be first in list
            # List order in section: oldest -> youngest (bottom -> top)
            # If stages[i] is younger than stages[i+1], that's a reversal
            # cmp_result > 0 means stage1 is younger (lower Ma) than stage2
            if cmp_result > 0:
                violations.append({
                    "section": sec_name,
                    "issue": f"Stage order reversed: {stages[i]} above {stages[i + 1]}",
                    # REVIEW-2026-09-20: "high" broke the module contract —
                    # this file's docstring and every other emitter only use
                    # "info" / "warning", and js/quality.js reports the same
                    # detector with severity 'warning'. The value reaches the
                    # UI as `issues[].severity`, so an unhandled level made the
                    # server/GUI/i18n renderers fall through to their default
                    # (or nothing) for the one violation class that matters.
                    "severity": "warning",
                })

    return violations


def _score_biozone_order(species: list, sections: list) -> tuple[int, list[dict[str, Any]]]:
    """Steno's Law biozone order check (P1-12 / M-1).

    For each section, if species A is in biozone X and species B is in
    biozone Y where X is YOUNGER than Y in real stratigraphic time,
    but A's range is BELOW B's in the section (i.e. older-looking FAD),
    this is a Steno's Law violation.

    M-1 fix (REVIEW-2026-07-25): the previous version used
    *lexicographic* string comparison (`younger_bz > older_bz`) as a
    proxy for stratigraphic order. Lexicographic order has NO
    relationship to time — "Zone 10" sorts before "Zone 9" in
    strings, and "N. optima Zone" vs "T. pseudotruncarum Zone" is
    arbitrary. We now use ``ics_age_compare`` (real stratigraphic
    ordering by ICS base_ma). When neither biozone label matches a
    known stage in the bundled ICS table, the pair is skipped
    rather than penalised (ancient or rarely-referenced biozone
    names should not generate false positives).
    """
    if not species or not sections:
        return 0, []

    # Build section name → age string mapping.
    section_ages: dict[str, str] = {}
    for sec in sections:
        if isinstance(sec, dict):
            name = sec.get("name", "")
            age_range = sec.get("age_range", "")
            if name and age_range:
                section_ages[str(name)] = str(age_range)

    if not section_ages:
        return 0, []

    violations = 0
    issues = []

    by_section: dict[str, list] = {}
    for sp in species:
        if not isinstance(sp, dict):
            continue
        sec_name = str(sp.get("section") or "")
        if sec_name in section_ages:
            by_section.setdefault(sec_name, []).append(sp)

    for sec_name, sp_list in by_section.items():
        if len(sp_list) < 2:
            continue
        # REVIEW-2026-09-20: position kind matters as much as position value.
        # The rest of this function assumes "sorted first = youngest", which is
        # only true for BED indices (larger index = higher in the section =
        # younger). Ages run the other way — a larger Ma is OLDER — so a chart
        # whose endpoints are "253 Ma" / "251 Ma" used to be sorted by the
        # leading integer through _parse_bed_n and every correctly ordered
        # pair was then reported as a Steno violation. Route through
        # _looks_like_age exactly like _score_accuracy (and the FAD/LAD branch
        # in _score_consistency) already do: ages sort by -Ma, beds by +index,
        # and a section mixing both is skipped because the two scales cannot
        # be compared at all.
        positioned: list[tuple[float, dict]] = []
        kinds: set[str] = set()
        for sp in sp_list:
            raw_top = sp.get("range_top")
            if _looks_like_age(raw_top):
                ma = _resolve_age_ma(raw_top, prefer="younger")
                if ma is None:
                    continue
                kinds.add("age")
                positioned.append((-ma, sp))
                continue
            bed_n = _parse_bed_n(raw_top)
            if bed_n is None:
                continue
            kinds.add("bed")
            positioned.append((float(bed_n), sp))
        if len(positioned) < 2 or len(kinds) > 1:
            # Unpositioned rows are excluded rather than invented; a mixed
            # bed/age section has no single ordering scale.
            continue
        # Stable tie-break by species label so equal positions do not depend
        # on the input row order (the pair loop below compares neighbours).
        positioned.sort(key=lambda item: (item[0], str(item[1].get("species") or "")))
        sorted_sp = [sp for _, sp in positioned]
        for i in range(len(sorted_sp) - 1):
            younger = sorted_sp[i]
            older = sorted_sp[i + 1]
            younger_bz = str(younger.get("biozone") or "").strip()
            older_bz = str(older.get("biozone") or "").strip()
            if not younger_bz or not older_bz:
                continue
            # Resolve each biozone label to an ICS stage (if known).
            y_stage = _resolve_biozone_stage(younger_bz)
            o_stage = _resolve_biozone_stage(older_bz)
            if y_stage is None or o_stage is None:
                # Unknown biozone names — skip the pair instead of
                # using the meaningless lexicographic fallback. This
                # prevents the false positives flagged in the audit.
                continue
            cmp = _HAS_ICS and ics_age_compare(y_stage, o_stage)
            if cmp is None:
                continue
            # sorted_sp is sorted by range_top ASC — so sorted_sp[i]
            # (smaller range_top, sits LOWER in section) has the
            # "younger" biozone label and sorted_sp[i+1] has the
            # "older" label. Stratigraphic convention says younger
            # stages sit ABOVE older stages, i.e. have LARGER
            # range_top. If the species labeled "younger" really is
            # younger in time (cmp > 0), the LOWER position is a
            # Steno's-Law violation.
            if cmp > 0:
                violations += 1
                issues.append({
                    "severity": "warning",
                    "msg_key": "quality.biozone_order_violation",
                    "params": {
                        "species": str(younger.get("species", "")),
                        "younger_biozone": younger_bz,
                        "older_biozone": older_bz,
                    },
                })
            # cmp == 0 means the two stages are the same age — no
            # violation. cmp < 0 means the "younger" label actually
            # names an older stage, which is itself a labelling
            # inconsistency but not a strat-order violation here.
    return violations, issues


# M-1 / P1-12 (REVIEW-2026-07-25): best-effort map from common biozone index
# fossils to their ICS stage. Real ammonite / radiolarian / conodont zone
# names (e.g. "N. optima Zone") do NOT contain a literal ICS stage name, so
# the whole-word stage match below never fired and Steno's Law check was a
# near-no-op. This modest dictionary lets the check actually run for a few
# well-known zones. It is intentionally small and incomplete; unknown zones
# still fall through to None and are skipped (no false positives).
_BIOZONE_STAGE_MAP: dict[str, str] = {
    # REVIEW-2026-07-31: species-level conodont zone keys only. The genus
    # Clarkina spans the Wuchiapingian-Changhsingian, so a bare "clarkina"
    # key mis-assigned Wuchiapingian zones (C. orientalis, C. leveni,
    # C. subcarinata...) to the Changhsingian, which made the Steno check
    # silently miss real violations.
    # Wuchiapingian conodont zones.
    "clarkina orientalis": "Wuchiapingian",
    "clarkina leveni": "Wuchiapingian",
    "clarkina subcarinata": "Wuchiapingian",
    "clarkina guangyuanensis": "Wuchiapingian",
    "clarkina transcaucasica": "Wuchiapingian",
    # Changhsingian conodont zones.
    "clarkina changxingensis": "Changhsingian",
    "clarkina yini": "Changhsingian",
    "clarkina meishanensis": "Changhsingian",
    "clarkina optima": "Changhsingian",
    "neogondolella changxingensis": "Changhsingian",
    "neogondolella optima": "Changhsingian",
    "n. optima": "Changhsingian",
    # Ammonite zones (basal Triassic Induan).
    "otoceras": "Induan",
    "ophiceras": "Induan",
    "griesbachian": "Induan",
    # Ammonite zones (Olenekian / early Middle Triassic).
    "anasirabites": "Olenekian",
    "subcolumbites": "Olenekian",
}


def _resolve_biozone_stage(label: str) -> Optional[str]:
    """Map a free-text biozone label to an ICS stage name if possible.

    Resolution order:
      1. an explicit biozone -> stage entry in ``_BIOZONE_STAGE_MAP``
         (substring match on the label, e.g. "N. optima Zone" ->
         "Changhsingian");
      2. a whole-word ICS stage name appearing literally in the label.

    Returns the matched stage name from the bundled ICS_2024 table, or None
    if no match. Unknown biozone names return None so the Steno's Law check
    skips them (no false positives).
    """
    if not label:
        return None
    if not _HAS_ICS:
        return None
    label_lower = label.lower()
    for key, stage in _BIOZONE_STAGE_MAP.items():
        if key in label_lower and stage in _ICS_2024:
            return stage
    for stage_name in _ICS_2024.keys():
        # Match by whole-word presence of the stage name.
        if re.search(r"\b" + re.escape(stage_name.lower()) + r"\b", label_lower):
            return stage_name
    return None


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
    elif mode == "zonation":
        null_check_keys = ("zones", "correlations", "zonations", "confidence")
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


def _abundance_percentage_buckets(data: dict[str, Any]) -> tuple[dict[str, float], dict[str, int]]:
    """Group percentage abundances per level.

    Returns ``(level_sums, skipped)`` where ``skipped`` counts the rows the
    check could not use, split into ``"no_level"`` (no level/sample to
    attribute the value to) and ``"unparsable"`` (unit is ``%`` but the value
    is not a number).

    REVIEW-2026-09-20: two silent-degradation paths fixed.

    * A row without ``level`` used to be bucketed under the key ``""``, which
      merged EVERY unlabelled row of the whole diagram into one imaginary
      level and then reported a violation pointing at nothing
      (``sample=""`` → the badge read "the abundance percentages of '' sum to
      100%"). Such rows are now skipped and counted, because their share of a
      level that is unknown to us cannot be summed meaningfully.
    * An unparsable ``abundance`` used to ``continue`` silently, so a diagram
      whose cells are all "30 %" (unit baked into the value, comma decimal
      separators, ellipsis) degenerated into "no buckets → no violations →
      full marks". Counting them lets the caller say the check was not
      evaluable instead of pretending it passed.
    """
    violations: dict[str, float] = {}
    skipped = {"no_level": 0, "unparsable": 0}
    for entry in data.get("samples") or data.get("abundances") or []:
        if not isinstance(entry, dict):
            continue
        unit = str(entry.get("abundance_unit", "")).strip().lower()
        # Only sum percentage values (unit == '%')
        if unit != "%":
            continue
        level = str(entry.get("level") or "").strip()
        if not level:
            skipped["no_level"] += 1
            continue
        try:
            pct = float(entry.get("abundance", ""))
        except (TypeError, ValueError):
            skipped["unparsable"] += 1
            continue
        violations[level] = violations.get(level, 0.0) + pct
    return violations, skipped


def _abundance_sum_violations(level_sums: dict[str, float]) -> list[dict[str, Any]]:
    """Levels whose percentages miss 100 ± 5."""
    return [
        {"sample": level, "sum": total}
        for level, total in level_sums.items()
        if not (95 <= total <= 105)
    ]


def _score_abundance_sum(data: dict[str, Any]) -> list[dict[str, Any]]:
    """P1-8: For abundance diagrams, check that each level's percentages sum to 100±5.

    Returns a list of violation dicts, each with keys ``sample`` (level id) and ``sum``
    (the computed total). An empty list means no violations — which, after
    REVIEW-2026-09-20, no longer includes rows the check could not evaluate;
    see :func:`_abundance_percentage_buckets` and the caller's
    not-evaluable note.
    """
    samples = data.get("samples") or data.get("abundances") or []
    if not isinstance(samples, list):
        return []
    level_sums, _skipped = _abundance_percentage_buckets(data)
    return _abundance_sum_violations(level_sums)


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

    dimensions = (
        ("completeness", W_COMPLETENESS, _score_completeness),
        ("accuracy", W_ACCURACY, _score_accuracy),
        ("consistency", W_CONSISTENCY, _score_consistency),
        ("structure", W_STRUCTURE, _score_structure),
    )
    weighted_scores: list[float] = []
    all_issues: list[dict[str, Any]] = []
    for dimension, weight, scorer in dimensions:
        try:
            dimension_score, dimension_issues = scorer(data)
        except Exception as exc:  # fail closed: scoring must never block extraction
            dimension_score = 0.0
            dimension_issues = [{
                "severity": "warning",
                "msg_key": "quality.scoring_failed",
                "params": {
                    "dimension": dimension,
                    "error_type": type(exc).__name__,
                },
            }]
        weighted_scores.append(weight * min(1.0, max(0.0, dimension_score)))
        all_issues.extend(dimension_issues)

    composite = sum(weighted_scores)
    composite = min(1.0, max(0.0, composite))

    out: dict[str, Any] = {
        "score": round(composite, 4),
        "grade": _grade_for(composite),
        "issues": all_issues,
    }

    # BORROW-2026-09-20 (A): the honest-coverage ledger. ADDITIVE and purely
    # informational — it never moves the composite score, because the four
    # weighted dimensions already carry the penalties and a model that has not
    # adopted the tri-state contract yet must not score worse than one that
    # has. Only when runs actually answered with response_kind / reason_codes
    # does it also raise an ``info`` issue, so legacy results keep their exact
    # old issue list.
    try:
        ledger = coverage_for(data)
    except Exception:  # the ledger must never break scoring
        ledger = None
    if ledger is not None:
        out["coverage"] = ledger
        totals = ledger.get("totals") or {}
        if totals.get("explicit_responses"):
            all_issues.append({
                "severity": "info",
                "msg_key": "quality.coverage_ledger",
                "params": {
                    "answered": str(totals.get("answered", 0)),
                    "cells": str(totals.get("cells", 0)),
                    "not_drawn": str(totals.get("not_drawn", 0)),
                    "gaps": str(totals.get("silent_missing", 0)),
                },
            })
    return out


#: Primary per-mode row tables the coverage ledger can be computed over.
_COVERAGE_TABLES: tuple[tuple[str, str], ...] = (
    ("species_ranges", "species"),
    ("abundances", "taxon"),
    ("data_points", "sample_id"),
    ("points", "label"),
)


def coverage_for(data: dict[str, Any]) -> dict[str, Any] | None:
    """Ledger for the largest contracted row table of a result, or None.

    Returns None when no row anywhere carries a ``response_kind`` /
    ``reason_codes`` field: a pre-contract result has nothing to account for,
    and inventing a grid of "silent gaps" for it would be noise.
    """
    if not isinstance(data, dict):
        return None
    best: tuple[int, str] | None = None
    for key, column_key in _COVERAGE_TABLES:
        rows = data.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        contracted = sum(1 for r in rows
                         if isinstance(r, dict)
                         and (r.get("response_kind") or r.get("reason_codes")))
        if not contracted:
            continue
        if best is None or len(rows) > best[0]:
            best = (len(rows), key)
    if best is None:
        return None
    row_count, table_key = best
    rows = data.get(table_key)
    primary_column = dict(_COVERAGE_TABLES)[table_key]
    return coverage_ledger(
        [r for r in rows if isinstance(r, dict)],
        column_keys=(primary_column, "taxon", "species", "sample_id",
                     "label", "name"),
    )
