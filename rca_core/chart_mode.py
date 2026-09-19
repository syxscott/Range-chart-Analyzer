"""Shared auto-detection of the chart extraction mode from text.

Single source of truth for the DESKTOP GUIs (gui.py / gui_fluent.py),
mirroring ``rcaAutoDetectChartMode`` in js/app.js so the web frontend
and both desktop GUIs classify the same caption identically.

REVIEW-2026-11-07 (low): the two desktop GUIs previously carried their
own inline copies of the heuristic using PLAIN SUBSTRING matching, while
the web frontend used ``\\b``-anchored word boundaries. The divergences
went both ways ("Pollinator..." → abundance in Python, range_chart in
JS), and the JS side's ``\\bphylogen\\b`` never matched "phylogenetic" —
the single most common caption form — silently falling back to
range_chart. All three implementations now share the same rules:

* whole-word ASCII keywords — matched on BOTH word boundaries
  (``\\bcolumn\\b``);
* STEM keywords — matched on a LEADING word boundary only, so
  ``phylogen`` matches "phylogenetic" / "phylogenies" and ``palyno``
  matches "palynology";
* CJK keywords — plain substring (CJK has no word-boundary concept);
* order of checks (zonation → abundance → columnar → phylo →
  range_chart default) is the same as the JS frontend.

REVIEW-2026-09-20 #105 (three gaps in that same contract):

1. The detailed variant promised in its own docstring that a caption
   explicitly naming a range chart "must always win over the image
   classifier", but 延限 / "range chart" was only consulted INSIDE the
   zonation branch. "Conodont range chart" therefore returned
   ``("range_chart", False)`` — the ``False`` means "nothing found", so
   ``resolve_auto_mode`` still spent a vision call on it and let the
   classifier overturn the caption. The explicit keywords are now their
   own table (``_RANGE_ASCII`` / ``_RANGE_CJK``) and are honoured right
   before the default fallback, with ``matched=True``.
2. ``correlation of`` in the zonation table was broad enough to hit
   "Correlation of the measured sections" (a plain lithostratigraphic
   cross-section figure) and — because a zonation hit returns early —
   skip the vision classifier entirely on a false positive. It is
   narrowed to the zone-specific forms.
3. chemical_stratigraphy / paleomap / scatter_plot had NO keyword table
   at all, so every such caption ("Isotope chemostratigraphy",
   "Paleogeographic map", "δ13C biplot", 古地理図, 散点图, …) fell through
   to the default and cost a vision call. Conservative tables were added
   for them; they are evaluated AFTER the established four and are
   suppressed whenever the caption also names a range chart, so they
   cannot steal the range-chart / columnar / zonation genres they overlap
   with.
"""

from __future__ import annotations

import re

# ASCII keywords, in detection order. Mirrors js/app.js.
# Zonation / correlation charts (radiolarian biochronology). "zonation"
# is a STEM so it also matches "zonations" / "zonal" via the leading-
# rule in _match_kw.
#
# REVIEW-2026-09-20 #105 (item 2 above): "correlation of" was removed from
# this table — it fired on "Correlation of the measured sections",
# "Correlation of lithofacies" and similar non-zonation captions, and a
# zonation hit short-circuits the whole heuristic (no vision fallback). The
# zone-specific spellings below keep the intended hits ("Zone correlation
# of the radiolarians", "Correlation chart of the.sections",
# "Correlation of zones") on the same cost-free path.
_ZON_ASCII = ("zonation", "biozonation", "zone correlation",
              "correlation chart", "correlation of zones")
_ZON_CJK = ("生物带", "化石带", "带状对比", "对比图")
# REVIEW-2026-09-20 #105: real zone-correlation captions separate the two
# halves of the phrase ("Correlation of Triassic radiolarian ZONES and
# subzones"), which no keyword tuple can express. One bounded regex keeps
# those hits while still refusing the lithostratigraphic "Correlation of the
# measured sections" — the sentence clause stops at the first full stop.
_ZON_PHRASE_RES = (
    re.compile(r"\bcorrelation of\b[^.\n]{0,80}?\bzones?\b"),
)

_AB_ASCII = ("pollen", "abundance", "percentage diagram", "palyno")
_AB_CJK = ("孢粉", "花粉", "丰度", "百分比")
_COL_ASCII = ("column", "columns", "columnar", "col_section", "col_sections")
_COL_CJK = ("柱状", "柱状図", "柱状图")
_PHYLO_ASCII = ("phylogen", "phylogram", "cladogram", "dendrogram",
                "molecular phylogen")
_PHYLO_CJK = ("系统发育", "进化树", "系统树", "分子系统")

# Explicit RANGE-CHART wording (延限表 / 延限図 / range chart). Used for the
# two decisions the caption is allowed to settle on its own: the mixed
# zonation+range-chart caption, and the pre-default ``matched=True`` return
# that keeps the vision classifier from overturning a self-labelled figure.
# Deliberately narrow: the bare word "range" ("depth range", "age range") is
# NOT an indicator, and no generic synonym ("stratigraphic range") is either,
# so this table cannot change what the four established branches return.
_RANGE_ASCII = ("range chart", "range-chart")
# Non-ASCII terms are plain substrings — 延限 already covers 延限表 / 延限図 /
# 延限图, and the Russian renderings are the two phrases used for a species
# range chart in biostratigraphic literature.
_RANGE_CJK = ("延限", "карта совмещения", "график совмещения")

# --- REVIEW-2026-09-20 #105 (item 3): conservative tables for the three
# modes that previously had none. "Conservative" = only words that name this
# chart family and essentially nothing else, because a keyword hit SKIPS the
# vision classifier — a false positive silently extracts the wrong schema.
#
# Chemical stratigraphy: isotope / element curves against depth or age.
_CHEM_ASCII = ("isotop", "chemostrat", "chemical stratigraphy",
               "chemical stratigraphic")
_CHEM_CJK = ("同位素", "化学地层", "化学地層", "地球化学",
             "изотоп", "геохим")

# Paleogeographic maps: continents / coastlines / localities on a projection.
_PALEO_ASCII = ("paleomap", "palaeomap", "paleogeograph", "palaeogeograph",
                "paleocontinent", "palaeocontinent")
_PALEO_CJK = ("古地理", "古海洋", "古大陆", "板块重建",
              "палеогеограф", "палеокарт", "палеоконтинент")

# Scatter / biplot: discrete x-y point clouds (the one chart-type wording
# that no other mode claims).
_SCAT_ASCII = ("scatter plot", "scatterplot", "scatter diagram", "biplot",
               "crossplot", "cross plot")
_SCAT_CJK = ("散点", "散布図", "точечная диаграмма", "рассеяни", "рассеиван")

# Keywords that are word STEMS, not whole words: they must also match
# inflected / compound continuations, so only a LEADING word boundary is
# required (see module docstring).
#
# REVIEW-2026-09-20 #105: "zonation" joins the stems — js/app.js has always
# matched it with a LEADING boundary only ('zonation' ? asciiWordStart :
# stemMatch), so \bzonation\b on this side silently missed the plural
# "Zonations of ..." that the web frontend classified correctly. "range
# chart" likewise has to cover "range charts".
_STEMS = ("phylogen", "molecular phylogen", "palyno", "zonation",
          "range chart", "isotop", "chemostrat", "paleomap", "palaeomap",
          "paleogeograph", "palaeogeograph", "paleocontinent",
          "palaeocontinent")


def _match_kw(text: str, needle: str) -> bool:
    """Match one keyword against ``text`` (already lowercased)."""
    if any(ord(c) > 127 for c in needle):
        # CJK: plain substring — no word boundaries in CJK text.
        return needle in text
    pat = r"\b" + re.escape(needle) + ("" if needle in _STEMS else r"\b")
    return re.search(pat, text) is not None


def _hit(t: str, ascii_keys: tuple[str, ...], cjk_keys: tuple[str, ...],
         phrase_res: tuple[re.Pattern[str], ...] = ()) -> bool:
    """True when any keyword (or phrase pattern) of one table occurs in the
    lowercased text."""
    return (any(_match_kw(t, k) for k in ascii_keys)
            or any(k in t for k in cjk_keys)
            or any(r.search(t) is not None for r in phrase_res))


def _range_chart_explicit(t: str) -> bool:
    """True when the caption names a range chart / 延限表 itself."""
    return _hit(t, _RANGE_ASCII, _RANGE_CJK)


def auto_detect_chart_mode_ex(text: str) -> tuple[str, bool]:
    """Detailed auto-detection: ``(mode, matched)``.

    UI-REVIEW-2026-09-07 (auto mode): ``matched`` distinguishes a POSITIVE
    keyword hit from the range_chart default. The vision-fallback in
    ``resolve_auto_mode`` only fires when the text heuristic found nothing
    — a caption that explicitly says "range chart" must always win over
    the image classifier.

    REVIEW-2026-09-20 #105: that promise is now actually kept — a caption
    naming a range chart returns ``("range_chart", True)`` from every path
    (including the no-zonation one), so no vision call is made and the
    caption cannot be overturned.
    """
    t = (text or "").lower()
    says_range_chart = _range_chart_explicit(t)
    # UI-REVIEW-2026-09-07 (E2E fig_23): captions that mix both genres
    # ("Columnar section with radiolarian range chart. Zonation of ...")
    # used to route to zonation_chart, discarding the range-chart body.
    # When the caption ALSO says "range chart" / 延限图, the range-chart
    # reading wins — zonation columns are then still captured by the
    # extractor's biozone fields.
    if _hit(t, _ZON_ASCII, _ZON_CJK, _ZON_PHRASE_RES):
        return ("range_chart", True) if says_range_chart else ("zonation_chart", True)
    for k in _AB_ASCII:
        if _match_kw(t, k):
            return "abundance_diagram", True
    for k in _AB_CJK:
        if k in t:
            return "abundance_diagram", True
    for k in _COL_ASCII:
        if _match_kw(t, k):
            return "columnar_section", True
    for k in _COL_CJK:
        if k in t:
            return "columnar_section", True
    for k in _PHYLO_ASCII:
        if _match_kw(t, k):
            return "phylogenetic_tree", True
    for k in _PHYLO_CJK:
        if k in t:
            return "phylogenetic_tree", True
    # The three ASSISTANT-only modes. Gated on ``not says_range_chart``: a
    # caption may legitimately mention "…δ13C curve above the conodont
    # range chart", and the explicitly named genre outranks a side mention.
    if not says_range_chart:
        # Chart-type wording first (biplot / scatter / map say MORE about
        # the figure than the data it plots), then the data-content tables.
        if _hit(t, _PALEO_ASCII, _PALEO_CJK):
            return "paleomap", True
        if _hit(t, _SCAT_ASCII, _SCAT_CJK):
            return "scatter_plot", True
        if _hit(t, _CHEM_ASCII, _CHEM_CJK):
            return "chemical_stratigraphy", True
    # REVIEW-2026-09-20 #105: the docstring's contract, right before the
    # "nothing matched" default — an explicit range chart caption is a POSITIVE
    # hit, not a guess, so the caller must not send the image to the
    # classifier instead.
    if says_range_chart:
        return "range_chart", True
    return "range_chart", False


def auto_detect_chart_mode(text: str) -> str:
    """Return the extraction mode suggested by caption / file-name text.

    One of ``"abundance_diagram"``, ``"columnar_section"``,
    ``"phylogenetic_tree"``, ``"zonation_chart"``, ``"chemical_stratigraphy"``,
    ``"paleomap"``, ``"scatter_plot"``, or ``"range_chart"`` (the default).

    REVIEW-2026-09-20 #106: the ~30 lines that used to follow ``return mode``
    were an unreachable copy of the pre-``_ex`` heuristic (kept from before
    the detailed variant existed) — and one of its branches referenced ``t``,
    a name that does not exist in this function's scope, so the dead code was
    also a latent NameError. The single implementation lives in
    ``auto_detect_chart_mode_ex`` now.
    """
    mode, _matched = auto_detect_chart_mode_ex(text)
    return mode
