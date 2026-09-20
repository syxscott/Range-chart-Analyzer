"""PBDB mapping for range chart data."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Optional

# C-4 (REVIEW-2026-07-25): wire the ICS 2024 table into the PBDB export path
# so stage-name -> numeric Ma conversion happens here (previously ICS was
# only imported by quality.py, so PBDB emitted empty max_ma/min_ma for text
# age ranges and duplicated the biozone into both early/late_interval).
# Degrade gracefully to the previous behaviour when ICS is unavailable.
try:
    from .ics import (
        ICS_2024,
        ics_age_range_bounds,
        ics_parse_age_range,
        ics_resolve_age_bound,
    )
    _HAS_ICS = bool(ICS_2024)
except Exception:  # pragma: no cover - import fallback
    _HAS_ICS = False
    ICS_2024 = {}
    ics_age_range_bounds = None
    ics_parse_age_range = None
    ics_resolve_age_bound = None

# BORROW-2026-09-20 (PBDB pbdbUpload-api schema gaps): the ICS series/epoch
# label table is needed to tell "Lopingian" (a SERIES) from "Permian" (a
# SYSTEM) when a *_reso column is derived from an interval name. Guarded like
# the import above — a missing/renamed private table degrades the qualifier to
# ``informal``, it never breaks the export.
try:  # pragma: no cover - optional private detail
    from .ics import _SERIES_STAGE_LISTS as _ICS_SERIES_LABELS
except Exception:  # pragma: no cover - import fallback
    _ICS_SERIES_LABELS = {}


# ---------------------------------------------------------------------------
# BORROW-2026-09-20: chronostratigraphic resolution qualifiers (``*_reso``)
# ---------------------------------------------------------------------------
# The pbdbUpload-api upload template pairs every chronostratigraphic VALUE
# with a resolution qualifier column (``early_interval`` ↔ ``early_*_reso``,
# ``max_ma``/``min_ma`` ↔ ``max_ma_reso``/``min_ma_reso``, and the same idea on
# the taxon-side ``first_tma``/``last_tma`` fields). The qualifier says HOW
# well constrained the value is — "the plate printed 252.4 Ma" and "the plate
# said Wuchiapingian, so the number is a table lookup" are NOT equally strong
# ages, and a validator (and the next human) needs to know which one landed in
# the cell. Our export used to collapse both into the same bare number.
#
# Vocabulary (documented in README, section "导出格式"):
#   "measured"  an absolute age literally printed on the plate ("252.4 Ma")
#   "stage"     an ICS stage name (the Ma is that stage's boundary)
#   "series"    an ICS series / epoch ("Lopingian", "Late Permian")
#   "system"    a period / system ("Permian")
#   "era"       an era ("Paleozoic")
#   "zone"      a local biozone label — weakest chronostratigraphic constraint
#   "informal"  a name the ICS table does not know
#   ""          no value at all (the paired column is empty too)
_TIME_RESO_VOCAB = (
    "measured", "stage", "series", "system", "era", "zone", "informal", "",
)


def _ics_name_sets() -> tuple[frozenset, frozenset, frozenset]:
    """``(system_names, series_names, era_names)`` derivable from the table."""
    rows = list((ICS_2024 or {}).values())
    systems = frozenset(str(i.get("period")) for i in rows if i.get("period"))
    eras = frozenset(str(i.get("era")) for i in rows if i.get("era"))
    series = frozenset(
        str(v[0]) for v in _ICS_SERIES_LABELS.values()
        if isinstance(v, (tuple, list)) and v and v[0]
    )
    return systems, series, eras


def _interval_reso(name: Any, measured: bool = False, source: str = "") -> str:
    """Resolution qualifier for one chronostratigraphic interval NAME.

    ``measured`` short-circuits to ``"measured"`` (an absolute age read off the
    plate). ``source`` overrides the classification when the CALLER knows
    better than the string does — currently only ``"zone"`` (a biozone label).
    """
    if measured:
        return "measured"
    if source:
        return source
    text = str(name or "").strip()
    if not text:
        return ""
    info = (ICS_2024 or {}).get(text)
    if info:
        # The bundled table spells the rank "Stage" / "Series".
        return str(info.get("rank") or "Stage").lower()
    systems, series, eras = _ics_name_sets()
    if text in systems:
        return "system"
    if text in series:
        return "series"
    if text in eras:
        return "era"
    return "informal"


def _text_age_reso(text: Any, prefer: str = "older") -> str:
    """How well the endpoint label *text* constrains an absolute age.

    A single printed ``"260 Ma"`` is as MEASURED as a printed ``"260-252 Ma"``
    span, so the range parser (which deliberately needs two numbers) is not
    enough here — the lone-age regex decides on its own.
    """
    if not text:
        return ""
    value = str(text).strip()
    if _AGE_RANGE_WITH_UNIT.search(value) or _AGE_VALUE_WITH_UNIT.search(value):
        return "measured"
    if ics_resolve_age_bound:
        try:
            name, _ma = ics_resolve_age_bound(text, prefer=prefer)
        except Exception:  # pragma: no cover - defensive
            return ""
        if name:
            return _interval_reso(name)
    return ""


# ---------------------------------------------------------------------------
# BORROW-2026-09-20: three-part (trinomial) name split
# ---------------------------------------------------------------------------
# The upload template wants ``genus`` / ``species`` / ``subspecies`` as three
# separate columns; we only ever shipped the concatenated ``taxon_name`` and
# buried the authority in ``notes``. ``rca_core/names.py`` has a
# ``clean_name_for_lookup`` (it strips the authorship and the open-nomenclature
# markers for a GBIF query) but it deliberately DISCARDS an abbreviated genus
# ("P. asiaticus" -> "asiaticus"), which is exactly what a taxonomic column
# must not do — so the split below keeps every token it can attribute to a
# rank and leaves the row's own text in ``taxon_name`` untouched.
_AUTHOR_PAREN_RE = re.compile(r"\([^()]*\)")
# A trailing authorship: an optional "ex"/"in" citation, the surname(s)
# (accented and initial-form included, "et al." allowed), then a 4-digit
# year. The year is the ANCHOR — without it a legitimate trinomial
# ("Genus species subspecies") could be eaten, so a name with no year is left
# exactly as the plate wrote it.
_AUTHOR_TAIL_RE = re.compile(
    r"\s+(?:(?:ex|in)\s+)?[A-Z][A-Za-z.\u00c0-\u2fff]*"
    r"(?:[,\s\-]+(?:et\s+al\.?|&\s*[A-Z][A-Za-z.]*|[A-Z][A-Za-z.]*))*"
    r"[,\s]+\d{4}[a-z]?\s*$"
)
_YEAR_TAIL_RE = re.compile(r"[,\s]*\d{4}[a-z]?\s*$")
_RANK_MARKERS = {
    "subsp.": "subspecies", "subsp": "subspecies", "ssp.": "subspecies",
    "subspecies": "subspecies", "var.": "subspecies", "var": "subspecies",
    "variety": "subspecies", "forma": "subspecies", "f.": "subspecies",
}
# Markers that mean "no lower rank is determined at all" — the species column
# stays empty rather than being filled with a word.
_INDETERMINATE = {"sp.", "sp", "spp.", "spp", "gen.", "gen", "indet."}
# Open-nomenclature markers that DO attach to the following epithet. The
# qualifier is kept IN the cell: stripping it would turn "cf. magnus"
# ("compare with magnus", not identified) into a positive determination of
# magnus, which is a scientific misstatement rather than a tidy column.
_COMPARE_MARKERS = {"cf.", "cf", "aff.", "aff", "?cf.", "?aff."}


def _split_taxon_name(name: Any) -> tuple[str, str, str]:
    """``(genus, species, subspecies)`` for a binomial or trinomial string.

    The forms that actually occur on range charts::

        "Pseudotirolites panigoniensis"      -> ("Pseudotirolites", "panigoniensis", "")
        "P. asiaticus (Zheng, 1979)"         -> ("P.", "asiaticus", "")
        "Palaeopascichnus sp."               -> ("Palaeopascichnus", "", "")
        "Costa cf. postwenti"                -> ("Costa", "cf. postwenti", "")
        "Genus species subsp. subspecies"    -> ("Genus", "species", "subspecies")
        "Clarkina? carli in Yang 1978"       -> ("Clarkina?", "carli", "")

    Every retained token is written VERBATIM (including an abbreviated genus
    like ``"P."`` and a doubt ``"?"``): a taxonomic column that quietly
    expanded or cleaned a name would state something the plate did not, and
    the un-split source string stays available in ``taxon_name`` regardless.
    """
    text = str(name or "").strip()
    if not text:
        return "", "", ""
    # Drop the authorship first: "(Smith, 1979)" and a trailing "Smith 1979"
    # are not part of the name.
    text = _AUTHOR_PAREN_RE.sub(" ", text)
    # Compound citations ("... Yang 1978 ex Smith 1982") strip one authorship
    # at a time: the regex anchors on the LAST year, so the outer "ex Smith
    # 1982" goes first and the exposed "Yang 1978" on the next pass. Without
    # the loop a bare surname would be left over and land in ``subspecies``.
    for _ in range(3):
        stripped = _AUTHOR_TAIL_RE.sub(" ", text).strip()
        if stripped == text.strip():
            break
        text = stripped
    text = _YEAR_TAIL_RE.sub("", text).strip()
    tokens = [t for t in re.split(r"\s+", text) if t]
    if not tokens:
        return "", "", ""
    genus = tokens[0]
    rest = tokens[1:]
    species = ""
    subspecies = ""
    i = 0
    while i < len(rest):
        tok = rest[i]
        key = tok.lower().strip()
        if key in _RANK_MARKERS:
            # The NEXT token is the sub-specific epithet.
            if i + 1 < len(rest):
                subspecies = rest[i + 1]
                i += 2
                continue
            i += 1
            continue
        if key in _INDETERMINATE:
            # "Genus sp." / "Genus spp." — nothing determined below genus.
            i += 1
            continue
        if key in _COMPARE_MARKERS and i + 1 < len(rest):
            qualified = "%s %s" % (
                key if key.endswith(".") else key + ".", rest[i + 1])
            if not species:
                species = qualified
            elif not subspecies:
                subspecies = qualified
            i += 2
            continue
        if not species:
            species = tok
        elif not subspecies:
            subspecies = tok
        i += 1
    return genus, species, subspecies


# ---------------------------------------------------------------------------
# BORROW-2026-09-20: abundance columns
# ---------------------------------------------------------------------------

def _norm_taxon_key(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _abundance_lookup(result: Any) -> dict[str, list[dict[str, Any]]]:
    """``{normalised taxon: [abundance row, ...]}`` from ``result['abundances']``.

    An abundance-diagram result (and a multi-run range chart that read the
    abundance annotations) is the only place abundance lives, so the occurrence
    rows join onto it by taxon — and, when the row names a section, by site.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(result, dict):
        return out
    rows = result.get("abundances")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        taxon = _norm_taxon_key(row.get("taxon"))
        if not taxon:
            continue
        out.setdefault(taxon, []).append(row)
    return out


def _abundance_for(row: dict[str, Any], lookup: dict[str, list[dict[str, Any]]]) -> tuple[str, str]:
    """``(abund_value, abund_unit)`` for one occurrence row.

    Priority: a value ON the species row (the model read it as a per-range
    annotation, possibly under ``_extras``) beats a join onto the abundance
    table. From several levels of the same taxon the PEAK is exported — the
    occurrence is a range, not a sample, so a single level would be arbitrary
    while a maximum is a defensible summary of "how abundant in this range".
    Values are written verbatim: a relative scale ("common") is data, and
    silently dropping it would be worse than a non-numeric cell.
    """
    direct_value = row.get("abundance")
    direct_unit = row.get("abundance_unit")
    extras = row.get("_extras") if isinstance(row.get("_extras"), dict) else {}
    if direct_value in (None, ""):
        direct_value = extras.get("abundance")
    if direct_unit in (None, ""):
        direct_unit = extras.get("abundance_unit")

    candidates: list[dict[str, Any]] = []
    if lookup:
        taxon = _norm_taxon_key(row.get("species"))
        candidates = list(lookup.get(taxon) or [])
        # A taxon sampled in several sites only joins to ITS OWN section —
        # otherwise a site's abundance leaks onto another section's occurrence.
        section = _norm_taxon_key(row.get("section"))
        if candidates and section:
            on_site = [
                c for c in candidates
                if _norm_taxon_key(c.get("site")) == section
            ]
            if on_site:
                candidates = on_site
    if direct_value not in (None, ""):
        return str(direct_value), str(direct_unit or "")
    if not candidates:
        return "", ""
    numeric: list[float] = []
    texts: list[str] = []
    units: list[str] = []
    for c in candidates:
        v = c.get("abundance")
        if v in (None, ""):
            continue
        unit = str(c.get("abundance_unit") or "").strip()
        if unit:
            units.append(unit)
        try:
            # "35%" is the same measurement as 35 with unit "%" — keep the
            # number in the value column and the unit in the unit column.
            numeric.append(float(str(v).strip().rstrip("%")))
        except (TypeError, ValueError):
            texts.append(str(v).strip())
    top_unit = max(set(units), key=units.count) if units else ""
    if numeric:
        peak = max(numeric)
        return (("%d" % peak) if float(peak).is_integer() else ("%g" % peak)), top_unit
    if texts:
        # A relative / categorical scale ("rare", "common"): the distinct
        # labels are joined rather than ranked, because we have no ordered
        # scale to pick a "peak" from and dropping them would lose the data.
        seen: list[str] = []
        for t in texts:
            if t not in seen:
                seen.append(t)
        return "; ".join(seen), top_unit
    return "", ""



def _resolve_pbdb_bounds(row):
    """Resolve a species row's FAD/LAD to (early_stage, late_stage, early_ma,
    late_ma).

    ``early`` = older bound (range_base / FAD); ``late`` = younger bound
    (range_top / LAD). Falls back to (None, None, None, None) when ICS is
    unavailable or the bounds can't be resolved so the caller can use its
    section-level numeric fallback.
    """
    if not _HAS_ICS or not isinstance(row, dict):
        return None, None, None, None
    base = row.get("range_base")
    top = row.get("range_top")
    # REVIEW-2026-07-31: interval literals resolve to the correct end —
    # FAD/base is the OLDER (larger Ma) end, LAD/top the YOUNGER end.
    e_stage, e_ma = ics_resolve_age_bound(base, prefer="older") if ics_resolve_age_bound else (None, None)
    l_stage, l_ma = ics_resolve_age_bound(top, prefer="younger") if ics_resolve_age_bound else (None, None)
    return e_stage, l_stage, e_ma, l_ma


def _stage_endpoint_names(stages):
    """Return ``(older_name, younger_name)`` for *stages* — by their ICS bounds.

    REVIEW-2026-09-20: both PBDB builders took ``stages[0]`` / ``stages[-1]``,
    i.e. the TEXT order of the age_range label. Labels do not have to be
    written bottom-to-top ("Changhsingian - Wuchiapingian" occurs on real
    charts, and the module's own numeric path is deliberately
    order-independent: max_ma/min_ma use max()/min()). Pairing the first word
    with early_interval then exported the YOUNGER name beside the OLDER max_ma
    — a self-contradicting interval row that PBDB validators reject.
    """
    known = [s for s in stages if s in ICS_2024]
    if not known:
        return "", ""
    # Geological convention inside the table: base_ma is the older (larger)
    # number and top_ma the younger (smaller) one.
    older = max(known, key=lambda s: ICS_2024[s].get("base_ma", 0) or 0)
    younger = min(known, key=lambda s: ICS_2024[s].get("top_ma", 0) or 0)
    return older, younger


def _parse_coords(text):
    if not text or not isinstance(text, str):
        return None, None
    text = text.strip()
    if re.search(r"not\s*visible|unknown|missing", text, re.IGNORECASE):
        return None, None
    m = re.search(r"([+-]?\d+\.?\d*)\s*([NSns]),?\s*([+-]?\d+\.?\d*)\s*([EWew])", text)
    if m:
        lat_val = float(m.group(1))
        lon_val = float(m.group(3))
        # REVIEW-2026-07-31: hemisphere comes from the matched letter
        # group, not a whole-text scan ("31N, 117E (south bank)" used to
        # flip the latitude to -31).
        if m.group(2).upper() == "S": lat_val = -abs(lat_val)
        if m.group(4).upper() == "W": lon_val = -abs(lon_val)
        return lat_val, lon_val
    return None, None


_AGE_RANGE_WITH_UNIT = re.compile(
    r"(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:[-–—]|\bto\b)\s*"
    r"([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)
_AGE_VALUE_WITH_UNIT = re.compile(
    r"(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b",
    re.IGNORECASE,
)


def _parse_age_range_ma(text):
    """Return ``(min_ma, max_ma)`` only for explicit absolute-age ranges.

    Bed/sample identifiers and other bare numbers are rejected. Both common
    forms, ``"260-250 Ma"`` and ``"260 Ma to 250 Ma"``, are supported.
    """
    if not text:
        return None, None
    value = str(text).strip()
    match = _AGE_RANGE_WITH_UNIT.search(value)
    if match:
        ages = [float(match.group(1)), float(match.group(2))]
    else:
        ages = [float(item) for item in _AGE_VALUE_WITH_UNIT.findall(value)]
        if len(ages) < 2:
            return None, None
        ages = ages[:2]
    return min(ages), max(ages)


def _validated_pbdb_ages(
    older_ma: Any,
    younger_ma: Any,
    fallback_max_ma: Any,
    fallback_min_ma: Any,
    row_reso: str = "",
    section_reso: str = "",
) -> tuple[Any, Any, str, str]:
    """Return PBDB ``(max_ma, min_ma, max_ma_reso, min_ma_reso)``.

    Never mixes incompatible bounds. BORROW-2026-09-20: the two ``*_reso``
    qualifiers say WHICH pair won — a per-row age and a section-level age are
    not equally well constrained, and the values alone used to hide that.
    ``row_reso`` may be one qualifier or an ``(older, younger)`` pair (the two
    endpoints of a real chart commonly resolve differently: a printed
    ``252.4 Ma`` at the FAD and a stage name at the LAD). ``section_reso`` is
    single because both ends come from the same age_range label.
    A caller that only cares about the numbers still gets the historical pair
    (both resos default to ``""``).
    """
    if isinstance(row_reso, (tuple, list)):
        row_reso_older = str(row_reso[0] or "")
        row_reso_younger = str(row_reso[1] or "")
    else:
        row_reso_older = row_reso_younger = str(row_reso or "")
    if older_ma is not None and younger_ma is not None:
        if older_ma >= younger_ma:
            return older_ma, younger_ma, row_reso_older, row_reso_younger
        return "", "", "", ""
    if fallback_max_ma not in (None, "") and fallback_min_ma not in (None, ""):
        try:
            max_ma = float(fallback_max_ma)
            min_ma = float(fallback_min_ma)
        except (TypeError, ValueError):
            return "", "", "", ""
        if max_ma >= min_ma:
            return max_ma, min_ma, section_reso, section_reso
    return "", "", "", ""


def to_pbdb_occurrences(result):
    occurrences = []
    species_ranges = result.get("species_ranges", []) or []
    sections = result.get("sections", []) or []
    section_info = {}
    for sec in sections:
        if isinstance(sec, dict):
            name = sec.get("name", "")
            section_info[name] = {
                "collection_name": name,
                "formation": "; ".join(sec.get("formations", []) or []),
                "coordinates": sec.get("coordinates", ""),
                # M-1 fix: persist the section's age_range so we can
                # populate max_ma (older boundary) and min_ma (younger
                # boundary) for each species row from this section.
                # Before the fix these were hard-coded empty.
                "age_range": sec.get("age_range", "") or "",
                # Pre-parse Ma at write-time so PBDB temporal queries
                # actually work.
                "min_ma": "",  # younger
                "max_ma": "",  # older
                # REVIEW-2026-07-31: interval-name fallbacks resolved from
                # the section age_range (stage/series/period labels) used
                # for early_interval / late_interval when per-row bounds
                # don't resolve.
                "older_name": "",
                "younger_name": "",
                # BORROW-2026-09-20: how the section-level numbers below were
                # obtained, for the max_ma_reso / min_ma_reso columns.
                "age_reso": "",
            }
            age_range = section_info[name]["age_range"]
            min_ma, max_ma = _parse_age_range_ma(age_range)
            # BORROW-2026-09-20: an explicit "252.4-250.1 Ma" on the plate and
            # a table lookup from "Wuchiapingian" produce the SAME number and
            # used to export the SAME (empty) qualifier. Remember which one it
            # was before the ICS path overwrites the pair.
            explicit_ma = min_ma is not None
            if min_ma is None and _HAS_ICS and ics_age_range_bounds:
                # C-3 fix (REVIEW-2026-07-31): stage/series-only age ranges
                # ("Late Permian (Wuchiapingian - Changhsingian)") never
                # resolved to numeric Ma before — PBDB temporal queries
                # stayed empty for the most common chart labels. Resolve
                # them through the ICS table now.
                older_ma, younger_ma = ics_age_range_bounds(age_range)
                if older_ma is not None:
                    min_ma, max_ma = younger_ma, older_ma
            if min_ma is not None:
                section_info[name]["min_ma"] = str(min_ma)
            if max_ma is not None:
                section_info[name]["max_ma"] = str(max_ma)
            if _HAS_ICS and ics_parse_age_range:
                _stages = ics_parse_age_range(age_range)
                _older_name, _younger_name = (
                    _stage_endpoint_names(_stages) if _stages else ("", ""))
                if _older_name and _younger_name:
                    section_info[name]["older_name"] = _older_name
                    section_info[name]["younger_name"] = _younger_name
                elif ics_resolve_age_bound:
                    _n_old, _ = ics_resolve_age_bound(age_range, prefer="older")
                    if _n_old:
                        section_info[name]["older_name"] = _n_old
                        section_info[name]["younger_name"] = _n_old
            if max_ma is not None or min_ma is not None:
                # BORROW-2026-09-20: "measured" wins — the plate printed the
                # number. Otherwise the number is a lookup from the interval
                # name the section resolved to.
                section_info[name]["age_reso"] = (
                    "measured" if explicit_ma
                    else _interval_reso(section_info[name]["older_name"])
                )
    # BORROW-2026-09-20: abundance rows (an abundance diagram, or a range
    # chart whose bars the model read) join onto the occurrences by taxon.
    abundance_lookup = _abundance_lookup(result)
    for idx, row in enumerate(species_ranges):
        if not isinstance(row, dict): continue
        species = row.get("species", "")
        if not species: continue
        section = row.get("section", "")
        sec_data = section_info.get(section, {})
        biozone = row.get("biozone", "")
        lat, lon = None, None
        coords = sec_data.get("coordinates", "")
        if coords: lat, lon = _parse_coords(coords)
        # REVIEW-2026-09-20: ``dict.get(key, default)`` only uses the default
        # when the key is ABSENT, and the normalizer always writes
        # ``author_year`` (empty string when nothing was read) — so the
        # ``row.get("authority", "")`` second argument was dead code and an
        # empty author_year never fell through to the authority alias the
        # older payloads / hand-edited rows carry. ``or``-chaining is what was
        # meant here (same fix in darwin_core.py).
        author_year = (
            row.get("author_year") or row.get("authority")
            or row.get("author") or ""
        )
        # C-2 / C-4 fix (REVIEW-2026-07-25): resolve the per-species FAD/LAD
        # bounds via ICS. early = older bound (range_base), late = younger
        # bound (range_top). When ICS is unavailable or a bound can't be
        # resolved we fall back to the biozone label and the section-level
        # numeric age_range.
        e_stage, l_stage, e_ma, l_ma = _resolve_pbdb_bounds(row)
        # REVIEW-2026-07-31: when per-row bounds don't resolve (bed labels
        # are the common case), fall back to the section's resolved
        # interval names instead of duplicating the biozone into both
        # interval fields (old C-2 semantic error).
        early_interval = e_stage or sec_data.get("older_name", "") or biozone
        late_interval = l_stage or sec_data.get("younger_name", "") or biozone
        # BORROW-2026-09-20: which rung of the chronostratigraphic ladder the
        # interval name actually sits on (stage / series / system / zone).
        # The biozone fallback is its own weakest case.
        early_interval_reso = _interval_reso(
            early_interval,
            source="zone" if (early_interval and early_interval == biozone
                              and not e_stage
                              and not sec_data.get("older_name", "")) else "",
        )
        late_interval_reso = _interval_reso(
            late_interval,
            source="zone" if (late_interval and late_interval == biozone
                              and not l_stage
                              and not sec_data.get("younger_name", "")) else "",
        )
        # PBDB requires max_ma (older) >= min_ma (younger). Never combine a
        # per-row bound with a section fallback because that can create a
        # scientifically invalid hybrid interval. Use a complete, ordered
        # per-row pair or a complete, ordered section-level pair; else blank.
        max_ma, min_ma, max_ma_reso, min_ma_reso = _validated_pbdb_ages(
            e_ma,
            l_ma,
            sec_data.get("max_ma", ""),
            sec_data.get("min_ma", ""),
            row_reso=(
                _text_age_reso(row.get("range_base"), "older"),
                _text_age_reso(row.get("range_top"), "younger"),
            ),
            section_reso=sec_data.get("age_reso", ""),
        )
        # BORROW-2026-09-20: three-part name split + abundance columns (see
        # the module header). taxon_name stays the verbatim source string.
        genus, specific_epithet, subspecies = _split_taxon_name(species)
        abund_value, abund_unit = _abundance_for(row, abundance_lookup)
        occurrence = {
            "occurrence_id": f"RC_{species.replace(' ', '_')}_{section}_{idx}",
            "taxon_name": species,
            "identified_by": "",
            "collection_name": sec_data.get("collection_name", section),
            "formation": sec_data.get("formation", ""),
            # C-2 fix: early_interval = older bound, late_interval = younger
            # bound. These were both set to the biozone, which is a semantic
            # type error (the biozone label is the biozone, not the interval).
            "early_interval": early_interval,
            "late_interval": late_interval,
            # C-4 fix: max_ma = older boundary, min_ma = younger boundary,
            # sourced from the ICS-resolved per-row bounds (or the section's
            # numeric age_range). PBDB importers key temporal queries on these.
            "max_ma": str(max_ma) if max_ma not in (None, "") else "",
            "min_ma": str(min_ma) if min_ma not in (None, "") else "",
            "latitude": str(lat) if lat is not None else "",
            "longitude": str(lon) if lon is not None else "",
            "biostratigraphic_zone": biozone,
            "notes": f"author_year: {author_year}",
            # ----------------------------------------------------------------
            # BORROW-2026-09-20 (PBDB pbdbUpload-api upload-schema gaps). These
            # are APPENDED after the historical columns: the first 13 columns
            # keep their exact order so an existing download, the tests that
            # pin them and the js/export.js mirror all stay valid.
            # ----------------------------------------------------------------
            "genus": genus,
            "species": specific_epithet,
            "subspecies": subspecies,
            "early_interval_reso": early_interval_reso,
            "late_interval_reso": late_interval_reso,
            "max_ma_reso": max_ma_reso,
            "min_ma_reso": min_ma_reso,
            "abund_value": abund_value,
            "abund_unit": abund_unit,
        }
        occurrences.append(occurrence)
    return occurrences


def to_pbdb_collections(result):
    collections = []
    sections = result.get("sections", []) or []
    for sec in sections:
        if not isinstance(sec, dict): continue
        name = sec.get("name", "")
        if not name: continue
        lat, lon = None, None
        coords = sec.get("coordinates", "")
        if coords: lat, lon = _parse_coords(coords)
        formation = "; ".join(sec.get("formations", []) or [])
        # M-1 fix: populate min_ma / max_ma on collections as well.
        # Previously always empty — PBDB joins on these fields.
        # REVIEW-2026-07-31: stage/series-only age ranges now fall back to
        # ICS resolution (same as occurrences), and the resolved interval
        # names fill early_interval / late_interval.
        age_range = sec.get("age_range", "") or ""
        min_ma, max_ma = _parse_age_range_ma(age_range)
        # BORROW-2026-09-20: "the plate printed it" vs "we looked it up".
        explicit_ma = min_ma is not None
        early_interval, late_interval = "", ""
        if min_ma is None and _HAS_ICS and ics_age_range_bounds:
            older_ma, younger_ma = ics_age_range_bounds(age_range)
            if older_ma is not None:
                min_ma, max_ma = younger_ma, older_ma
        if _HAS_ICS and ics_parse_age_range:
            _stages = ics_parse_age_range(age_range)
            _older_name, _younger_name = (
                _stage_endpoint_names(_stages) if _stages else ("", ""))
            if _older_name and _younger_name:
                early_interval, late_interval = _older_name, _younger_name
            elif ics_resolve_age_bound:
                _n_old, _ = ics_resolve_age_bound(age_range, prefer="older")
                if _n_old:
                    early_interval = late_interval = _n_old
        # BORROW-2026-09-20: the numeric pair came from the OLDER name's base
        # and the YOUNGER name's top, so each end carries its own qualifier.
        age_reso_older = ("measured" if explicit_ma
                          else _interval_reso(early_interval))
        age_reso_younger = ("measured" if explicit_ma
                            else _interval_reso(late_interval or early_interval))
        collection = {
            "collection_name": name,
            "latitude": str(lat) if lat is not None else "",
            "longitude": str(lon) if lon is not None else "",
            "formation": formation,
            "early_interval": early_interval, "late_interval": late_interval,
            # BORROW-2026-09-20: max_ma BEFORE min_ma — the order the historical
            # CSV header (and ``PBDB_COLLECTION_FIELDS``) uses. ``DictWriter``
            # reorders anyway, but keeping the two in step means a caller that
            # serialises the dict directly writes the same columns.
            "max_ma": str(max_ma) if max_ma is not None else "",
            "min_ma": str(min_ma) if min_ma is not None else "",
            # BORROW-2026-09-20: appended after the historical columns (see
            # to_pbdb_occurrences for why the order is frozen).
            "early_interval_reso": _interval_reso(early_interval),
            "late_interval_reso": _interval_reso(late_interval),
            "max_ma_reso": age_reso_older,
            "min_ma_reso": age_reso_younger,
        }
        collections.append(collection)
    return collections


# BORROW-2026-09-20: the PBDB upload column lists. The historical columns keep
# their original ORDER (a download users already have, tests/test_pbdb.py and
# tests/test_review_2026_07_31_domain.py, and the js mirror all read them by
# position as well as by name); the upload-schema additions are appended.
PBDB_OCCURRENCE_FIELDS = [
    "occurrence_id", "taxon_name", "identified_by", "collection_name",
    "formation", "early_interval", "late_interval", "max_ma", "min_ma",
    "latitude", "longitude", "biostratigraphic_zone", "notes",
    # three-part name split / chronostratigraphic resolution / abundance
    "genus", "species", "subspecies",
    "early_interval_reso", "late_interval_reso", "max_ma_reso", "min_ma_reso",
    "abund_value", "abund_unit",
]
PBDB_COLLECTION_FIELDS = [
    "collection_name", "latitude", "longitude", "formation",
    "early_interval", "late_interval", "max_ma", "min_ma",
    "early_interval_reso", "late_interval_reso", "max_ma_reso", "min_ma_reso",
]


def _write_lf(path, text):
    """Write *text* with the LF terminators the writer above chose.

    ``Path.write_text`` opens in text mode, which on Windows rewrites every
    ``\\n`` to ``\\r\\n`` — so the ``lineterminator=chr(10)`` the
    ``DictWriter`` asks for was silently undone by the platform. ``newline=""``
    passes the string through untouched (same rule ``to_wpd`` applies to its
    CSVs, whose byte-for-byte text the browser mirror has to reproduce).
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def to_pbdb_csv(result, output_path):
    output_path = Path(output_path)
    if output_path.is_file(): output_path = output_path.parent
    occ_path = output_path / "pbdb_occurrences.csv"
    col_path = output_path / "pbdb_collections.csv"
    # BORROW-2026-09-20: exporting to a fresh "outputs/<run>/" died with
    # FileNotFoundError because the two writes below assumed the directory
    # already existed; ``to_wpd`` (exporter.py) has always made it.
    output_path.mkdir(parents=True, exist_ok=True)
    occurrences = to_pbdb_occurrences(result)
    collections = to_pbdb_collections(result)
    # ``PBDB_*_FIELDS`` are the single source of truth: a key a builder adds
    # without being listed here would make DictWriter raise
    # "dict contains fields not in fieldnames" and the whole export die.
    occ_fields = PBDB_OCCURRENCE_FIELDS
    col_fields = PBDB_COLLECTION_FIELDS
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=occ_fields, lineterminator=chr(10))
    writer.writeheader()
    for occ in occurrences: writer.writerow(occ)
    _write_lf(occ_path, output.getvalue())
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=col_fields, lineterminator=chr(10))
    writer.writeheader()
    for col in collections: writer.writerow(col)
    _write_lf(col_path, output.getvalue())
