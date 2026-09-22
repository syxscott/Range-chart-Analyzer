"""PBDB mapping for range chart data."""

from __future__ import annotations

import csv
import io
import math
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
# FIX-2026-09-22 (item 4), correcting what this block used to claim: the
# pbdbUpload-api template does NOT pair every chronostratigraphic value with a
# resolution column. ``occurrence.schema.js`` knows resolution ONLY for the
# four name ranks (``genus_reso`` … ``subspecies_reso``, closed enums), and the
# collection schema has no ``max_ma`` / ``min_ma`` / ``*_reso`` properties at
# all. The qualifier below is therefore OURS — an extension vocabulary that
# says HOW well constrained a value is ("the plate printed 252.4 Ma" and "the
# plate said Wuchiapingian, so the number is a table lookup" are NOT equally
# strong ages), which is exactly why it is written to
# ``pbdb_occurrence_extensions.csv`` and never into an upload sheet, where
# ``additionalProperties: false`` would reject it.
#
# Vocabulary (documented in README, section "提取字段" → PBDB 导出行):
#   "measured"  an absolute age literally printed on the plate ("252.4 Ma")
#   "stage"     an ICS stage name (the Ma is that stage's boundary)
#   "series"    an ICS series / epoch ("Lopingian", "Late Permian")
#   "system"    a period / system ("Permian")
#   "era"       an era ("Paleozoic")
#   "zone"      a local biozone label - weakest chronostratigraphic constraint
#   "informal"  a name the ICS table does not know
#   ""          no value at all (the paired column is empty too)
#
# Of these, only "informal" is also a member of an upstream rank enum.
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
# FIX-2026-09-22 (items 4 + 5), verified against the upstream schema:
#
#   pbdbUpload-api routes/api/v1/occurrence/occurrence.schema.js declares
#       collection_no, taxon_name, genus_reso, genus_name, subgenus_reso,
#       subgenus_name, species_reso, species_name, subspecies_reso,
#       subspecies_name, abund_value, abund_unit, reference_no, comments,
#       upload, plant_organ, plant_organ2
#   with ``additionalProperties: false``, ``required: [collection_no,
#   reference_no]`` and ``dependentRequired: {subgenus_name: [genus_name],
#   species_name: [genus_name], subspecies_name: [species_name, genus_name],
#   abund_value: [abund_unit]}``. Each rank has its OWN resolution column, and
#   the resolution vocabulary is the closed enum below — so the qualifier is a
#   FIELD, never glue inside the epithet (the old
#   ``species="cf. postwenti"`` would have been rejected, and reading it back
#   as "cf. postwenti" invents a species that the plate never named).
#
#   The parse below therefore never FABRICATES a determination
#   (module header: "never say what the chart didn't"). The forms it used to
#   get wrong, now pinned by tests/test_export_wpd_pbdb_2026_09_20.py:
#       "Neospiniferites? gen. nov."  -> genus + genus_reso, NO species
#       "Fusulina sp. nov. 3"         -> genus + species_reso "n. sp."; the
#                                        specimen number is not an epithet
#       "P. asiaticus Zheng"          -> genus + species; "Zheng" is an
#                                        authority WITHOUT a year, not a subspecies
#       "cf. Pseudotirolites panigoniensis"
#                                     -> genus "Pseudotirolites" + genus_reso
#                                        "cf.", species "panigoniensis"
#       "Costa sp. 1"                 -> genus "Costa" + species_reso
#                                        "informal"; "1" is not an epithet,
#                                        but "sp. 1" IS the informal taxon
#                                        the plate cited (FIX-2026-09-22 C2)
# ---------------------------------------------------------------------------
_PBDB_GENUS_RESO_VOCAB = (
    "", "aff.", "cf.", "ex gr.", "n. gen.", "sensu lato", "?", '"', "informal",
)
_PBDB_SUBGENUS_RESO_VOCAB = (
    "", "aff.", "cf.", "ex gr.", "n. subgen.", "sensu lato", "?", '"', "informal",
)
_PBDB_SPECIES_RESO_VOCAB = (
    "", "aff.", "cf.", "ex gr.", "n. sp.", "sensu lato", "?", '"', "informal",
)
_PBDB_SUBSPECIES_RESO_VOCAB = _PBDB_SPECIES_RESO_VOCAB
_PBDB_RANK_RESO_VOCAB = {
    "genus": _PBDB_GENUS_RESO_VOCAB,
    "subgenus": _PBDB_SUBGENUS_RESO_VOCAB,
    "species": _PBDB_SPECIES_RESO_VOCAB,
    "subspecies": _PBDB_SUBSPECIES_RESO_VOCAB,
}

# Nomenclatural abbreviations that NAME A RANK ("this is a new genus"), i.e.
# they are resolution information and never an epithet. Two tokens each, so
# they must be matched before the single-token tables below.
_NOMENCLATURAL_PHRASES = {
    ("gen.", "nov."): ("genus", "n. gen."),
    ("gener.", "nov."): ("genus", "n. gen."),
    ("gen. nov"): ("genus", "n. gen."),
    ("subgen.", "nov."): ("subgenus", "n. subgen."),
    ("sp.", "nov."): ("species", "n. sp."),
    ("sp. nov"): ("species", "n. sp."),
    ("spp.", "nov."): ("species", "n. sp."),
    ("subsp.", "nov."): ("subspecies", "n. sp."),
    ("ssp.", "nov."): ("subspecies", "n. sp."),
    ("var.", "nov."): ("subspecies", "n. sp."),
    ("forma", "nov."): ("subspecies", "n. sp."),
    ("f.", "nov."): ("subspecies", "n. sp."),
    ("n.", "gen."): ("genus", "n. gen."),
    ("n.", "subgen."): ("subgenus", "n. subgen."),
    ("n.", "sp."): ("species", "n. sp."),
    ("n.", "subsp."): ("subspecies", "n. sp."),
}
# Qualifiers that attach to the NEXT name token ("cf. magnus" = compare with
# magnus). They are reported through the resolution column of that rank, which
# is exactly what PBDB's schema carries them in — the epithet itself stays the
# epithet, so neither the doubt nor the name is invented.
_QUALIFIER_PHRASES = {
    ("ex", "gr."): ("next", "ex gr."),
    ("sensu", "lato"): ("next", "sensu lato"),
    ("sec", "lato"): ("next", "sensu lato"),
}
_QUALIFIER_TOKENS = {
    "cf.": ("next", "cf."), "cf": ("next", "cf."),
    "aff.": ("next", "aff."), "aff": ("next", "aff."),
    "s.l.": ("next", "sensu lato"),
    "?": ("next", "?"),
    '"': ("next", '"'),
    "informal": ("next", "informal"),
}
# Rank words that DO introduce a lower-rank epithet.
_RANK_MARKERS = {
    "subsp.": "subspecies", "subsp": "subspecies", "ssp.": "subspecies",
    "subspecies": "subspecies", "var.": "subspecies", "var": "subspecies",
    "variety": "subspecies", "forma": "subspecies", "f.": "subspecies",
}
# Markers that mean "no lower rank is determined at all" — the species column
# stays empty rather than being filled with a word.
_INDETERMINATE = {"sp.", "sp", "spp.", "spp", "gen.", "gen", "indet.", "indet"}
# Words that belong to an authority citation, never to an epithet.
_AUTHOR_WORDS = {"in", "ex", "pro", "non", "emend.", "emend", "sensu", "sec"}
# FIX-2026-09-22 (item 5): the nobiliary particles inside those citations
# ("d Orbigny", "de Lamarck", "von Buch", "van der Brixl"). A one-letter token
# is not a species-group name either, but the particle list is what lets
# "Genus b" keep its (abbreviated) epithet while "bulloides d Orbigny" loses
# the stray "d" the year-anchored strip left behind.
_AUTHOR_PARTICLES = {
    "d", "l", "de", "du", "des", "di", "da", "del", "della", "der", "den",
    "el", "la", "le", "les", "van", "von", "ten", "ter", "bin", "ibn",
}
_EMPTY_NAME_PARTS = {
    "genus_name": "", "genus_reso": "",
    "subgenus_name": "", "subgenus_reso": "",
    "species_name": "", "species_reso": "",
    "subspecies_name": "", "subspecies_reso": "",
}
_YEAR_IN_TEXT_RE = re.compile(r"\b\d{4}\b")
# A trailing authorship: an optional "ex"/"in" citation, the surname(s)
# (accented and initial-form included, "et al." allowed), then a 4-digit
# year. The year is the ANCHOR — without it a legitimate trinomial
# ("Genus species subspecies") could be eaten, so a name with no year is left
# exactly as the plate wrote it (and the capitalised surname is dropped by
# ``_split_taxon_name`` rather than promoted to a subspecies).
_AUTHOR_TAIL_RE = re.compile(
    r"\s+(?:(?:ex|in)\s+)?[A-Z][A-Za-z.\u00c0-\u2fff]*"
    r"(?:[,\s\-]+(?:et\s+al\.?|&\s*[A-Z][A-Za-z.]*|[A-Z][A-Za-z.]*))*"
    r"[,\s]+\d{4}[a-z]?\s*$"
)
_YEAR_TAIL_RE = re.compile(r"[,\s]*\d{4}[a-z]?\s*$")


def _clamp_reso(rank: str, value: str) -> str:
    """``value`` only when the RANK's own enum allows it (schema-valid by
    construction: 'n. gen.' can never leak into ``species_reso``)."""
    vocab = _PBDB_RANK_RESO_VOCAB.get(rank, ("",))
    text = str(value or "")
    return text if text in vocab else ""


def _split_taxon_name(name: Any) -> dict[str, str]:
    """Upstream name columns for a binomial / trinomial string.

    Returns the eight ``*_name`` / ``*_reso`` fields of the PBDB occurrence
    schema (all ``""`` when nothing could be attributed to that rank)::

        "Pseudotirolites panigoniensis"   genus "Pseudotirolites" + species
        "P. asiaticus (Zheng, 1979)"      genus "P.", species "asiaticus"
        "Palaeopascichnus sp."            genus only — nothing determined below
        "Costa cf. postwenti"             genus "Costa", species "postwenti",
                                          species_reso "cf."
        "Clarkina (Parkinsonina) carli"   genus + subgenus + species
        "Genus species subsp. subspecies" three ranks
        "Neospiniferites? gen. nov."      genus + genus_reso "?" (an
                                          occurrence-level doubt outranks the
                                          name-level "new genus"; both stay in
                                          ``taxon_name``)

    Every retained token is written VERBATIM (including an abbreviated genus
    like ``"P."``): a taxonomic column that quietly expanded or cleaned a name
    would state something the plate did not, and the un-split source string
    stays available in ``taxon_name`` regardless. The only transformation is
    that a doubt / nomenclatural marker MOVES from the name into its
    resolution column — which is where PBDB says it belongs. Each rank has ONE
    resolution slot, so the first marker met wins and the others are not lost:
    they remain in ``taxon_name``.
    """
    text = str(name or "").strip()
    if not text:
        return dict(_EMPTY_NAME_PARTS)

    genus = subgenus = species = subspecies = ""
    resos = {"genus": "", "subgenus": "", "species": "", "subspecies": ""}

    # Parenthesised groups are AMBIGUOUS by content — "(Parkinsonina)" is a
    # subgenus, "(Zheng, 1979)" and the year-less zoological "(Ehrenberg)" are
    # authorships — so they are marked here and resolved POSITIONALLY while the
    # tokens are walked (see the sentinel branch below): ICZN places a subgenus
    # in parentheses BETWEEN the genus and the specific epithet, and an
    # original-authority parenthesis AFTER the species-group name. Content
    # alone cannot tell those apart; order can.
    def _paren(m: "re.Match[str]") -> str:
        inner = m.group(1).strip()
        if _YEAR_IN_TEXT_RE.search(inner) or "," in inner or " " in inner:
            return " "                      # authority citation
        if re.match(r"^[A-Z][a-z]{2,}$", inner):
            # FIX-2026-09-22 (item 5): keep the group in place as a marker
            # instead of consuming it immediately. Reading "(Ehrenberg)" at the
            # end of a binomial as a subgenus invented a rank nobody stated.
            return " \x00%s\x00 " % inner
        return " "

    text = re.sub(r"\(([^()]*)\)", _paren, text)
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
    pending = ""          # qualifier waiting for the next NAME token
    expect_subspecies = False
    i = 0

    def _phrase_key(tok: str) -> str:
        # FIX-2026-09-22 (item 5): a doubt mark glued to the SECOND word of a
        # two-token phrase ("ex gr.?", "sp. nov.?") must not hide the phrase —
        # otherwise "gr." is left over and lands in ``species_name``, naming a
        # species the plate never wrote.
        return tok[:-1] if tok.endswith("?") and len(tok) > 1 else tok

    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        # 0) a parenthesised single capitalised word (see ``_paren`` above).
        # ICZN puts a subgenus in parentheses BETWEEN the genus and the
        # specific epithet; the SAME shape after the species-group name
        # ("Neogloboboquadrina pachyderma (Ehrenberg) Cushman") is the
        # original-authority parenthesis. Only the first is a rank, so the
        # positional test decides it and the second is dropped.
        if tok.startswith("\x00") and tok.endswith("\x00") and len(tok) > 2:
            if genus and not species and not subgenus:
                subgenus = tok.strip("\x00")
                resos["subgenus"] = resos["subgenus"] or _clamp_reso(
                    "subgenus", pending)
            pending, expect_subspecies = "", False
            i += 1
            continue
        nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        pair = (low, _phrase_key(nxt))
        # 1) two-token nomenclatural abbreviations: rank information only.
        if pair in _NOMENCLATURAL_PHRASES:
            rank, reso = _NOMENCLATURAL_PHRASES[pair]
            resos[rank] = resos[rank] or _clamp_reso(rank, reso)
            pending, expect_subspecies = "", False
            i += 2
            continue
        # 2) two-token qualifiers ("ex gr.", "sensu lato").
        if pair in _QUALIFIER_PHRASES:
            _, reso = _QUALIFIER_PHRASES[pair]
            pending = reso
            i += 2
            continue
        # 3) single-token qualifiers and the indeterminate / rank words.
        if low in _QUALIFIER_TOKENS:
            _, reso = _QUALIFIER_TOKENS[low]
            pending = reso
            i += 1
            continue
        if low == "?" and tok == "?":
            pending = "?"
            i += 1
            continue
        if low in _INDETERMINATE:
            # "Genus sp." / "Genus spp." — nothing determined below genus.
            # FIX-2026-09-22 (item 5): the tokens AFTER such a marker are a
            # specimen / figure number ("Costa sp. 1"), not an epithet.
            # FIX-2026-09-22 (C2, item 1): but "sp. 1" as a WHOLE is the
            # cited informal morphospecies the plate wrote — dropping it
            # entirely made the split answer LESS than the plate said. The
            # number is still not an epithet (no species_name is invented);
            # the hint survives in the one slot that admits it honestly,
            # species_reso = "informal" (upstream closed enum member).
            numbered = low in ("sp.", "sp", "spp.", "spp") and genus != ""
            pending, expect_subspecies = "", False
            i += 1
            if i < len(tokens) and re.fullmatch(r"\d+[a-z]?", tokens[i]):
                if numbered:
                    resos["species"] = resos["species"] or _clamp_reso(
                        "species", "informal")
                i += 1
            continue
        if low in _RANK_MARKERS:
            expect_subspecies = True
            i += 1
            continue
        if low in _AUTHOR_WORDS:
            pending, expect_subspecies = "", False
            i += 1
            continue
        # 4) a name token. A trailing "?" ("Clarkina?") is the doubt marker
        # the schema carries as genus_reso "?", so it is not part of the name.
        doubt = ""
        if tok.endswith("?") and len(tok) > 1:
            tok, doubt = tok[:-1], "?"
        # Which rank does this token fill?
        if not genus:
            rank = "genus"
        elif expect_subspecies:
            rank = "subspecies"
        elif not species:
            rank = "species"
        elif not subspecies:
            rank = "subspecies"
        else:
            rank = ""
        # FIX-2026-09-22 (item 5): a bare number ("Fusulina sp. nov. 3",
        # "Costa cf. 1") is a specimen / figure label, not an epithet — the
        # ICZN requires a word for a species-group name. It must not be
        # promoted to a determination; ``taxon_name`` still carries it. The
        # same holds for an authority particle left behind by a stripped
        # citation ("bulloides d Orbigny" -> the stray "d").
        if rank and rank != "genus" and (
                not re.search(r"[^\W\d_]", tok) or low in _AUTHOR_PARTICLES):
            rank = ""
        if rank == "subspecies" and not expect_subspecies \
                and tok[:1].isupper() and tok.isascii():
            # "P. asiaticus Zheng": a CAPITALISED leftover after a full
            # binomial is an authority whose year the plate did not print.
            # Calling it a subspecies fabricates a trinomial nobody stated,
            # so it is dropped (``taxon_name`` still carries it).
            rank = ""
        if rank:
            if rank == "genus":
                genus = tok
            elif rank == "subgenus":
                subgenus = tok
            elif rank == "species":
                species = tok
            else:
                subspecies = tok
            reso = pending or doubt
            if reso:
                resos[rank] = resos[rank] or _clamp_reso(rank, reso)
            expect_subspecies = False
        pending = ""
        i += 1

    return {
        "genus_name": genus,
        "genus_reso": resos["genus"],
        "subgenus_name": subgenus,
        "subgenus_reso": resos["subgenus"] if subgenus else "",
        "species_name": species,
        "species_reso": resos["species"],
        "subspecies_name": subspecies,
        "subspecies_reso": resos["subspecies"],
    }

# ---------------------------------------------------------------------------
# BORROW-2026-09-20: abundance columns
# ---------------------------------------------------------------------------
# FIX-2026-09-22 (item 10): what may be called a NUMBER here.
#
# ``float()`` is far too generous for text that came off a plate through a
# vision model:
#   float("nan") / float("inf")  -> NaN / Infinity   (poison a numeric column,
#                                                     invalid in strict JSON)
#   float("1_000")               -> 1000.0           (PEP 515 underscores are a
#                                                     literal syntax, not data)
#   float("２３")                 -> 23.0             (Unicode digits)
#   float("\u00a035")            -> 35.0             (NBSP, not a space)
# The WPD side of the export already refuses all of these
# (``exporter._wpd_num``); the PBDB side now uses the same ASCII-only literal
# grammar, so "1_0" reaches a spreadsheet as the TEXT it is instead of a
# number the plate never printed.
_PBDB_NUM_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _pbdb_number(value: Any) -> Optional[float]:
    """Finite ASCII float for *value*, else ``None`` (see above)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    raw = str(value)
    # ASCII is checked on the WHOLE cell, before stripping: str.strip() also
    # removes NBSP and the Unicode spaces, which is exactly the smuggling
    # rule this check exists to refuse.
    if not raw.isascii():
        return None
    text = raw.strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    if not text or not _PBDB_NUM_RE.match(text):
        return None
    try:
        number = float(text)
    except ValueError:  # pragma: no cover - the regex admits only parseables
        return None
    return number if math.isfinite(number) else None


def _pbdb_number_text(value: float) -> str:
    """Shortest round-trip text for a peak value.

    FIX-2026-09-22 (item 10): ``"%g" % 35.123456`` is ``"35.1235"`` — the
    export used to silently round every abundance to six significant digits
    while the abundance table itself kept full precision, so the two files
    disagreed about the same number. ``repr`` of a Python float is the
    shortest string that reads back as the SAME value.
    """
    if float(value).is_integer() and abs(value) < 1e16:
        return "%d" % value
    return repr(float(value))


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


_NONFINITE_TEXT_RE = re.compile(r"^[-+]?(?:nan|inf(?:inity)?|na|n/?a)$",
                               re.IGNORECASE)


def _abundance_for(row: dict[str, Any],
                   lookup: dict[str, list[dict[str, Any]]]) -> tuple[str, str, str]:
    """``(abund_value, abund_unit, note)`` for one occurrence row.

    Priority: a value ON the species row (the model read it as a per-range
    annotation, possibly under ``_extras``) beats a join onto the abundance
    table. From several levels of the same taxon the PEAK is exported — the
    occurrence is a range, not a sample, so a single level would be arbitrary
    while a maximum is a defensible summary of "how abundant in this range".
    Values are written verbatim: a relative scale ("common") is data, and
    silently dropping it would be worse than a non-numeric cell.

    FIX-2026-09-22 (item 10), three ways this used to corrupt the number:

      * ``nan`` / ``inf`` (or the words) are dropped and reported through
        ``note`` instead of landing in a numeric column;
      * the peak is taken per UNIT — ``max(5 % , 200 indiv/g)`` is not an
        abundance, it is a category error, so the dominant unit wins and the
        foreign-unit rows are reported in ``note`` rather than folded in;
      * the text is the shortest round-trip form (``_pbdb_number_text``), not
        ``%g``'s six significant digits.
    """
    direct_value = row.get("abundance")
    direct_unit = row.get("abundance_unit")
    extras = row.get("_extras") if isinstance(row.get("_extras"), dict) else {}
    if direct_value in (None, ""):
        direct_value = extras.get("abundance")
    if direct_unit in (None, ""):
        direct_unit = extras.get("abundance_unit")
    note = ""
    if direct_value not in (None, ""):
        number = _pbdb_number(direct_value)
        text = str(direct_value).strip()
        if number is None and (not isinstance(direct_value, str)
                               or _NONFINITE_TEXT_RE.match(text)):
            # float("nan") / float("inf") / the words spelled out — a numeric
            # column must not receive them, and neither may the TEXT "nan",
            # which reads back as a name rather than as a missing value.
            return "", "", "row abundance %r is not a finite number - dropped" % (
                direct_value,)
        if number is not None:
            # normalise "35%" on the row itself the same way the join below
            # does: number in the value column, unit in the unit column.
            unit = str(direct_unit or "").strip()
            if not unit and str(direct_value).strip().endswith("%"):
                unit = "%"
            return _pbdb_number_text(number), unit, ""
        return text, str(direct_unit or "").strip(), ""

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
    if not candidates:
        return "", "", ""

    by_unit: dict[str, list[float]] = {}
    texts: list[str] = []
    seen_units: list[str] = []
    skipped = 0
    for c in candidates:
        v = c.get("abundance")
        if v in (None, ""):
            continue
        unit = str(c.get("abundance_unit") or "").strip()
        # FIX-2026-09-22 (C2, item 10 follow-up): the join path must normalise
        # "35%" exactly like the direct path above — number in the value
        # column, unit in the unit column — or the same cell says one thing in
        # ``abund_value`` and another in ``abund_unit`` depending on which
        # table it was read from. A DECLARED unit still wins: the column is
        # the curator's statement, the suffix is the plate's, and the two
        # engines of this export agree on that precedence.
        if not unit and str(v).strip().endswith("%"):
            unit = "%"
        number = _pbdb_number(v)
        if number is None:
            text = str(v).strip()
            if _NONFINITE_TEXT_RE.match(text):
                skipped += 1
                continue
            texts.append(text)
            if unit:
                by_unit.setdefault(unit, [])
                seen_units.append(unit)
            continue
        if unit:
            seen_units.append(unit)
        by_unit.setdefault(unit, []).append(number)

    # Dominant unit = the one carrying the most VALUES (ties resolve to the
    # first seen, i.e. the plate's own bottom-to-top order).
    top_unit = ""
    if seen_units:
        top_unit = max(dict.fromkeys(seen_units),
                       key=lambda u: seen_units.count(u))
    values = by_unit.get(top_unit) or []
    foreign = sorted({u for u, nums in by_unit.items() if u and u != top_unit
                      and nums})
    if foreign:
        note = ("abundance units mixed (%s): peak taken within %s only, the "
                "%s values are not comparable"
                % ("/".join([top_unit or "(none)", *foreign]),
                   top_unit or "(no unit)", ", ".join(foreign)))
    if values:
        peak = max(values)
        return _pbdb_number_text(peak), top_unit, note
    if skipped:
        note = note or "abundance is nan/inf on %d row(s) - no value exported" % skipped
    if texts:
        # A relative / categorical scale ("rare", "common"): the distinct
        # labels are joined rather than ranked, because we have no ordered
        # scale to pick a "peak" from and dropping them would lose the data.
        seen: list[str] = []
        for t in texts:
            if t not in seen:
                seen.append(t)
        return "; ".join(seen), top_unit, note
    return "", "", note



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
        # FIX-2026-09-22 (item 4): the split now lands in the columns PBDB
        # actually declares (genus_name / genus_reso / subgenus_* / species_* /
        # subspecies_*), not in "genus" / "species" / "subspecies", which the
        # occurrence schema does not know.
        name_parts = _split_taxon_name(species)
        abund_value, abund_unit, abund_note = _abundance_for(row, abundance_lookup)
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
            "notes": f"author_year: {author_year}" if author_year else "",
            # ----------------------------------------------------------------
            # BORROW-2026-09-20 (PBDB pbdbUpload-api upload-schema gaps). These
            # are APPENDED after the historical columns: the first 13 columns
            # keep their exact order so an existing download, the tests that
            # pin them and the js/export.js mirror all stay valid.
            # ----------------------------------------------------------------
            "genus_name": name_parts["genus_name"],
            "genus_reso": name_parts["genus_reso"],
            "subgenus_name": name_parts["subgenus_name"],
            "subgenus_reso": name_parts["subgenus_reso"],
            "species_name": name_parts["species_name"],
            "species_reso": name_parts["species_reso"],
            "subspecies_name": name_parts["subspecies_name"],
            "subspecies_reso": name_parts["subspecies_reso"],
            "early_interval_reso": early_interval_reso,
            "late_interval_reso": late_interval_reso,
            "max_ma_reso": max_ma_reso,
            "min_ma_reso": min_ma_reso,
            "abund_value": abund_value,
            "abund_unit": abund_unit,
            "comments": "",
        }
        # FIX-2026-09-22 (item 4): ``comments`` is the ONLY free-text slot the
        # occurrence schema has, so it is where every curation decision the
        # upload sheet is forced to make is recorded — a cell that disappears
        # silently is worse than a cell that says why.
        _row, upload_notes = _pbdb_upload_projection(occurrence)
        if abund_note:
            upload_notes.insert(0, abund_note)
        occurrence["comments"] = "; ".join(
            [s for s in [occurrence["notes"]] + upload_notes if s])
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


# ---------------------------------------------------------------------------
# The PBDB column lists — TWO sheets, because one file cannot be both
# ---------------------------------------------------------------------------
# FIX-2026-09-22 (item 4). ``occurrence.schema.js`` is declared with
# ``additionalProperties: false``, so the historical 13-column sheet
# (occurrence_id / collection_name / latitude / notes / ...) could never be a
# VALID upload: the validator rejects the very columns that made it readable.
# Rather than choose between "uploads" and "documents the chart", the export
# now writes both, each under the columns it is allowed to carry:
#
#   pbdb_occurrences.csv          ONLY the properties below, in the upstream
#                                 order, every ``*_reso`` from the rank's own
#                                 enum, and the ``dependentRequired`` rules
#                                 enforced by dropping the orphan (the note
#                                 goes to ``comments``, never silently)
#   pbdb_occurrence_extensions.csv  the historical sheet, unchanged order, plus
#                                 the chronostratigraphic ``*_reso`` columns
#                                 that have no upstream slot
#
# The historical columns keep their original ORDER (a download users already
# have, tests/test_pbdb.py and tests/test_review_2026_07_31_domain.py all read
# them by position as well as by name); the additions are appended.
PBDB_OCCURRENCE_FIELDS = [
    "occurrence_id", "taxon_name", "identified_by", "collection_name",
    "formation", "early_interval", "late_interval", "max_ma", "min_ma",
    "latitude", "longitude", "biostratigraphic_zone", "notes",
    # three-part name split (upstream column names) / chronostratigraphic
    # resolution / abundance
    "genus_name", "genus_reso", "subgenus_name", "subgenus_reso",
    "species_name", "species_reso", "subspecies_name", "subspecies_reso",
    "early_interval_reso", "late_interval_reso", "max_ma_reso", "min_ma_reso",
    "abund_value", "abund_unit", "comments",
]
# The upstream property list, verbatim order, restricted to what this project
# can fill: ``upload`` / ``plant_organ`` / ``plant_organ2`` are OMITTED because
# a chart never states an upload flag or a plant part, and an absent optional
# property is schema-valid while a guessed one is not.
PBDB_UPLOAD_OCCURRENCE_FIELDS = [
    "collection_no", "taxon_name",
    "genus_reso", "genus_name",
    "subgenus_reso", "subgenus_name",
    "species_reso", "species_name",
    "subspecies_reso", "subspecies_name",
    "abund_value", "abund_unit",
    "reference_no", "comments",
]
# ``dependentRequired`` of occurrence.schema.js, as "rank -> ranks that must
# also carry a name for this rank's name to be uploadable".
_PBDB_RANK_DEPENDENCIES = {
    "genus": (),
    "subgenus": ("genus",),
    "species": ("genus",),
    "subspecies": ("genus", "species"),
}
_PBDB_RANKS = ("genus", "subgenus", "species", "subspecies")
PBDB_COLLECTION_FIELDS = [
    "collection_name", "latitude", "longitude", "formation",
    "early_interval", "late_interval", "max_ma", "min_ma",
    "early_interval_reso", "late_interval_reso", "max_ma_reso", "min_ma_reso",
]


def _pbdb_safe_quote(value: Any, limit: int = 60) -> str:
    """A cell value quoted INSIDE a note: whitespace collapsed, leading
    formula triggers removed, length bounded.

    FIX-2026-09-22 (item 9): a note is itself free text, so pasting
    ``abund_value '=HYPERLINK(...)' dropped`` would move the payload from a
    column the validator checks into one it does not. The note names the
    problem; it does not reproduce the attack.
    """
    text = " ".join(str(value if value is not None else "").split())
    text = text.lstrip("=+-@\t\r\n'\"")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def _pbdb_upload_projection(occ: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """One occurrence dict -> (schema-valid upload row, curation notes).

    The row carries EXACTLY ``PBDB_UPLOAD_OCCURRENCE_FIELDS`` (so
    ``additionalProperties: false`` is satisfied), both ``required`` properties
    (``collection_no`` / ``reference_no`` — PBDB assigns the real numbers, so
    until then ``collection_no`` carries the LOCAL collection name, which is
    the only key that joins this sheet to ``pbdb_collections.csv`` /
    ``pbdb_occurrence_extensions.csv`` (FIX-2026-09-22, C2: an empty id column
    made the upload sheet unlinkable AND let an unguarded free-text value
    bypass the CSV formula guard on its way out), and ``reference_no`` stays
    the empty placeholder), and honours every ``dependentRequired`` pair by
    dropping the orphan rather than shipping a row a validator bounces.
    """
    row: dict[str, str] = {key: "" for key in PBDB_UPLOAD_OCCURRENCE_FIELDS}
    notes: list[str] = []
    row["collection_no"] = str(occ.get("collection_no")
                               or occ.get("collection_name") or "")
    row["reference_no"] = str(occ.get("reference_no") or "")
    row["taxon_name"] = str(occ.get("taxon_name") or "")
    for rank in _PBDB_RANKS:
        # The rank enums are closed; re-clamping here means a hand-edited
        # dict cannot smuggle e.g. "n. gen." into species_reso.
        row["%s_reso" % rank] = _clamp_reso(rank, str(
            occ.get("%s_reso" % rank) or ""))
        name = str(occ.get("%s_name" % rank) or "").strip()
        if name:
            # Dependencies are checked against the PROJECTED row, in rank
            # order, so a missing genus cascades to species and subspecies.
            missing = [dep for dep in _PBDB_RANK_DEPENDENCIES[rank]
                       if not row["%s_name" % dep]]
            if missing:
                notes.append(
                    "%s_name %r dropped in the upload sheet: the schema "
                    "requires %s" % (rank, _pbdb_safe_quote(name),
                                      " and ".join(
                                          "%s_name" % m for m in missing)))
                name = ""
        row["%s_name" % rank] = name
    value = str(occ.get("abund_value") or "")
    unit = str(occ.get("abund_unit") or "")
    if value and not unit:
        notes.append(
            "abund_value %r dropped: the schema pairs it with abund_unit "
            "(dependentRequired) and the plate states no unit"
            % _pbdb_safe_quote(value),)
        value = ""
    row["abund_value"] = value
    row["abund_unit"] = unit
    if not row["collection_no"] or not row["reference_no"]:
        notes.append(
            "collection_no/reference_no are assigned by PBDB and are the "
            "schema's only required properties - fill them before uploading")
    comments = str(occ.get("comments") or "")
    if not comments:
        comments = "; ".join(
            [s for s in [str(occ.get("notes") or "")] + notes if s])
    row["comments"] = comments
    return row, notes


def _pbdb_csv_text(fields: list[str], rows: list[dict[str, Any]],
                   numeric: tuple[str, ...] = ()) -> str:
    """CSV text for *fields*, OWASP-formula-guarded (FIX-2026-09-22, item 9).

    ``exporter._sanitize_formula_cell`` prefixes a cell whose first character
    is ``= + - @`` or a tab/CR/LF with a single quote, and every other sheet of
    this project goes through it — the PBDB CSVs used to be written with a bare
    ``DictWriter``, so a model-injected ``=CMD(...)`` in a taxon name or a
    formation opened as a live formula in Excel. The numeric columns are
    exempt: ``-31.0`` is a latitude, not an attack, and prefixing it would
    turn a number into text.
    """
    def cell(value: Any, field: str) -> str:
        text = "" if value is None else str(value)
        if field in numeric:
            return text
        if text[:1] in ("=", "+", "-", "@", "\t", "\r", "\n"):
            return "'" + text
        return text

    output = io.StringIO()
    writer = csv.DictWriter(
        output, fieldnames=fields, lineterminator=chr(10),
        extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({f: cell(row.get(f, ""), f) for f in fields})
    return output.getvalue()


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
    """Write the PBDB sheets into *output_path* and return their paths.

    FIX-2026-09-22 (item 4): three files instead of two — the upload sheet
    (``pbdb_occurrences.csv``, strictly ``occurrence.schema.js``-valid), the
    local superset it was cut from (``pbdb_occurrence_extensions.csv``, the
    historical 13 columns plus the extras PBDB has no property for), and the
    collection sheet as before. ``pbdb_collections.csv`` keeps its own column
    list: it is a working locality sheet keyed by ``collection_name``, and the
    upstream collection template joins on the numbers PBDB assigns, which this
    project does not have — hence the ``collection_no`` caveat that each
    occurrence row repeats in ``comments``.
    """
    output_path = Path(output_path)
    if output_path.is_file(): output_path = output_path.parent
    occ_path = output_path / "pbdb_occurrences.csv"
    ext_path = output_path / "pbdb_occurrence_extensions.csv"
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
    upload_rows = [_pbdb_upload_projection(occ)[0] for occ in occurrences]
    _write_lf(occ_path, _pbdb_csv_text(
        PBDB_UPLOAD_OCCURRENCE_FIELDS, upload_rows))
    _write_lf(ext_path, _pbdb_csv_text(
        PBDB_OCCURRENCE_FIELDS, occurrences,
        numeric=("max_ma", "min_ma", "latitude", "longitude")))
    _write_lf(col_path, _pbdb_csv_text(
        PBDB_COLLECTION_FIELDS, collections,
        numeric=("max_ma", "min_ma", "latitude", "longitude")))
    return {
        "occurrences": occ_path,
        "extensions": ext_path,
        "collections": col_path,
    }
