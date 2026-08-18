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
* order of checks (abundance → columnar → phylo → range_chart default)
  is the same as the JS frontend.
"""

from __future__ import annotations

import re

# ASCII keywords, in detection order. Mirrors js/app.js.
_AB_ASCII = ("pollen", "abundance", "percentage diagram", "palyno")
_AB_CJK = ("孢粉", "花粉", "丰度", "百分比")
_COL_ASCII = ("column", "columns", "columnar", "col_section", "col_sections")
_COL_CJK = ("柱状", "柱状図", "柱状图")
_PHYLO_ASCII = ("phylogen", "phylogram", "cladogram", "dendrogram",
                "molecular phylogen")
_PHYLO_CJK = ("系统发育", "进化树", "系统树", "分子系统")

# Keywords that are word STEMS, not whole words: they must also match
# inflected / compound continuations, so only a LEADING word boundary is
# required (see module docstring).
_STEMS = ("phylogen", "molecular phylogen", "palyno")


def _match_kw(text: str, needle: str) -> bool:
    """Match one keyword against ``text`` (already lowercased)."""
    if any(ord(c) > 127 for c in needle):
        # CJK: plain substring — no word boundaries in CJK text.
        return needle in text
    pat = r"\b" + re.escape(needle) + ("" if needle in _STEMS else r"\b")
    return re.search(pat, text) is not None


def auto_detect_chart_mode(text: str) -> str:
    """Return the extraction mode suggested by caption / file-name text.

    One of ``"abundance_diagram"``, ``"columnar_section"``,
    ``"phylogenetic_tree"``, or ``"range_chart"`` (the default).
    """
    t = (text or "").lower()
    for k in _AB_ASCII:
        if _match_kw(t, k):
            return "abundance_diagram"
    for k in _AB_CJK:
        if k in t:
            return "abundance_diagram"
    for k in _COL_ASCII:
        if _match_kw(t, k):
            return "columnar_section"
    for k in _COL_CJK:
        if k in t:
            return "columnar_section"
    for k in _PHYLO_ASCII:
        if _match_kw(t, k):
            return "phylogenetic_tree"
    for k in _PHYLO_CJK:
        if k in t:
            return "phylogenetic_tree"
    return "range_chart"
