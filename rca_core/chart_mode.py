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
# --- Keyword tables DERIVED from rca_core/modes.py (2026-10-15) -------------
#
# These were hand-written here AND mirrored again in js/app.js - which has two
# copy-pasted autodetect functions, so a keyword edited in one copy and not the
# other silently re-routed a whole genre into a different extraction pipeline.
# rca_core/modes.py holds the single copy; the names below are kept so the
# detection cascade further down stays reviewable as a cascade.
from .modes import MODES as _MODE_SPECS


def _kw(mode: str, attr: str):
    return getattr(_MODE_SPECS[mode], attr)


_ZON_ASCII = _kw("zonation_chart", "keywords_ascii")
_ZON_CJK = _kw("zonation_chart", "keywords_cjk")
# REVIEW-2026-09-20 #105: real zone-correlation captions separate the two
# halves of the phrase ("Correlation of Triassic radiolarian ZONES and
# subzones"), which no keyword tuple can express. One bounded regex keeps
# those hits while still refusing the lithostratigraphic "Correlation of the
# measured sections" — the sentence clause stops at the first full stop.
_ZON_PHRASE_RES = _kw("zonation_chart", "keywords_phrase_res")

_AB_ASCII = _kw("abundance_diagram", "keywords_ascii")
_AB_CJK = _kw("abundance_diagram", "keywords_cjk")
_COL_ASCII = _kw("columnar_section", "keywords_ascii")
_COL_CJK = _kw("columnar_section", "keywords_cjk")
_PHYLO_ASCII = _kw("phylogenetic_tree", "keywords_ascii")
_PHYLO_CJK = _kw("phylogenetic_tree", "keywords_cjk")

# Explicit RANGE-CHART wording (延限表 / 延限図 / range chart). Used for the
# two decisions the caption is allowed to settle on its own: the mixed
# zonation+range-chart caption, and the pre-default ``matched=True`` return
# that keeps the vision classifier from overturning a self-labelled figure.
# Deliberately narrow: the bare word "range" ("depth range", "age range") is
# NOT an indicator, and no generic synonym ("stratigraphic range") is either,
# so this table cannot change what the four established branches return.
_RANGE_ASCII = _kw("range_chart", "keywords_ascii")
# Non-ASCII terms are plain substrings — 延限 already covers 延限表 / 延限図 /
# 延限图, and the Russian renderings are the two phrases used for a species
# range chart in biostratigraphic literature.
_RANGE_CJK = _kw("range_chart", "keywords_cjk")

# --- REVIEW-2026-09-20 #105 (item 3): conservative tables for the three
# modes that previously had none. "Conservative" = only words that name this
# chart family and essentially nothing else, because a keyword hit SKIPS the
# vision classifier — a false positive silently extracts the wrong schema.
#
# Chemical stratigraphy: isotope / element curves against depth or age.
_CHEM_ASCII = _kw("chemical_stratigraphy", "keywords_ascii")
_CHEM_CJK = _kw("chemical_stratigraphy", "keywords_cjk")

# Paleogeographic maps: continents / coastlines / localities on a projection.
_PALEO_ASCII = _kw("paleomap", "keywords_ascii")
_PALEO_CJK = _kw("paleomap", "keywords_cjk")

# Scatter / biplot: discrete x-y point clouds (the one chart-type wording
# that no other mode claims).
_SCAT_ASCII = _kw("scatter_plot", "keywords_ascii")
_SCAT_CJK = _kw("scatter_plot", "keywords_cjk")

# Keywords that are word STEMS, not whole words: they must also match
# inflected / compound continuations, so only a LEADING word boundary is
# required (see module docstring).
#
# REVIEW-2026-09-20 #105: "zonation" joins the stems — js/app.js has always
# matched it with a LEADING boundary only ('zonation' ? asciiWordStart :
# stemMatch), so \bzonation\b on this side silently missed the plural
# "Zonations of ..." that the web frontend classified correctly. "range
# chart" likewise has to cover "range charts".
# The stems that predate the registry. Kept verbatim rather than derived
# from the per-mode keyword tuples on purpose: promoting a whole-word
# keyword to a stem changes which captions match, so it must be a
# reviewed per-mode decision (ModeSpec.keywords_stems) and never a side
# effect of adding a word to a keyword table.
_LEGACY_STEMS = frozenset((
    "phylogen", "molecular phylogen", "palyno", "zonation",
    "range chart", "isotop", "chemostrat", "paleomap", "palaeomap",
    "paleogeograph", "palaeogeograph", "paleocontinent",
    "palaeocontinent",
))
_STEMS = _LEGACY_STEMS | frozenset(
    k for spec in _MODE_SPECS.values() for k in spec.keywords_stems)


def _match_kw(text: str, needle: str) -> bool:
    """Match one keyword against ``text`` (already lowercased).

    AUDIT-2026-09-28: ``re.ASCII`` is load-bearing. Both sides of this mirror
    write ``\\b``, but they do not mean the same thing by it: Python's ``\\w`` is
    UNICODE-aware for str patterns, while a JavaScript RegExp without the ``u``
    flag treats ``\\w`` as ASCII-only. So for a caption that runs an ASCII
    keyword straight into CJK or Cyrillic, the boundary exists in the browser
    and does not exist here:

        "pollen丰度图"   python: no match -> range_chart (default)
                         js:     match     -> abundance_diagram

    That is precisely the divergence this module exists to prevent -- its
    docstring promises the web frontend and both desktop GUIs "classify the
    same caption identically" -- and the branch order means the two engines
    then run DIFFERENT EXTRACTION PIPELINES on the same figure. ``re.ASCII``
    makes Python's ``\\b`` mean what JavaScript's means. Pure-ASCII captions
    are unaffected, because both notions of word character agree there.
    """
    if any(ord(c) > 127 for c in needle):
        # CJK: plain substring — no word boundaries in CJK text.
        return needle in text
    pat = r"\b" + re.escape(needle) + ("" if needle in _STEMS else r"\b")
    return re.search(pat, text, re.ASCII) is not None


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
        # occurrence_matrix (2026-10-15) is LAST on purpose. Its keywords are
        # the least specific in the whole table - "localit" / "wellbore" name a
        # data-column kind, not a chart genre - and a palaeomap caption that
        # says "fossil localities" must keep reaching paleomap above. Placing
        # it before those three would let one generic word hijack a genre that
        # has a dedicated prompt. Ungated by `says_range_chart` for the same
        # reason zonation is: an explicit "range chart" caption still wins, and
        # that check is already made once at the top of the function.
        if _hit(t, _kw("occurrence_matrix", "keywords_ascii"),
                _kw("occurrence_matrix", "keywords_cjk")):
            return "occurrence_matrix", True
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
