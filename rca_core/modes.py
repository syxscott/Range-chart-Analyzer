"""The single declaration of every chart mode the app knows about.

WHY THIS FILE EXISTS
====================
Before this module, the set of chart modes was written down in nine places
that could disagree with each other:

    rca_core/capabilities.py   CAPABILITIES                 (8 modes)
    rca_core/aggregate.py      SCHEMA_BY_MODE / WIRED_MODES (5 modes)
    rca_core/extractor.py      KNOWN_CHART_TYPES            (8 modes)
    rca_core/extractor.py      _mode_root_keys              (7 modes)
    rca_core/prompt.py         PROMPT_VERSION              (9 + 1 + 1)
    server.py                  _SUPPORTED_MODES             (5 + auto)
    js/app.js                  rcaResolveChartMode allowlist
    js/prompt.js               PROMPT_VERSION
    js/minimax.js              KNOWN / RCA_MODE_ROOT_KEYS / ...

Adding a mode meant editing all of them, and — this is the defect — MOST of
the misses did not raise. ``SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)``
silently applied the range-chart merge schema, ``js/minimax.js``'s
``else -> rcaNormalizeResult`` silently normalised a non-range payload as a
range chart. A mode wired into three of four places produced plausible,
confidently wrong output.

So the mode list lives here, ONCE, and every one of those sites derives from
it. Adding a chart type becomes: add one ``ModeSpec`` + write the normalizer.

DESIGN CONSTRAINT: DATA ONLY, NO SIBLING IMPORTS
=================================================
``prompt``, ``extractor`` and ``aggregate`` all import this module, so this
module must not import them — that is a cycle. Anything that is *code* is
therefore referenced by an import path string (``normalizer_ref``) and resolved
lazily by the consumer, while anything that is *data* (versions, keywords,
capabilities, flags, key names) is written out literally here.

WHAT EACH FLAG MEANS
====================
maturity          stable | candidate | assistant. "assistant" = broad genre,
                  partial yields are expected; the UI says so.
export_supported  the mode has table configs, so the table tab / CSV / XLSX
                  export are enabled (capabilities.py gates the UI on this).
server_supported  the HTTP API accepts the mode as an explicit request.
                  Distinct from export_supported: paleomap has exporter tables
                  but is still rejected with HTTP 400 when named directly.
browser_supported the browser transport can normalise it. False modes are
                  listed in js/minimax.js RCA_BROWSER_UNSUPPORTED_MODES and
                  the browser refuses them LOUDLY instead of falling through to
                  rcaNormalizeResult.
merge_wired       a MergeSchema exists, so merge_results may accept it.
                  Modes without one are rejected, because the old
                  ``SCHEMA_BY_MODE.get(mode, RANGE_CHART_SCHEMA)`` default
                  dropped every mode-specific row while still returning
                  ok=True.

THE THREE PSEUDO-ENTRIES
========================
``chart_classify`` is not a chart type - it is the vision classifier's own
prompt, keyed in PROMPT_VERSION because it is cached like one.
``abundance`` is a legacy ALIAS of ``abundance_diagram`` kept so old cache keys
still resolve. Neither is a ModeSpec: neither may appear in KNOWN_CHART_TYPES,
the capability registry, the merge schemas or the server allowlist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ModeSpec",
    "MODES",
    "PROMPT_VERSIONS",
    "CLASSIFY_PROMPT_VERSION",
    "PROMPT_ALIASES",
    "MODE_NAMES",
    "WIRED_MODE_NAMES",
    "SERVER_MODE_NAMES",
    "BROWSER_UNSUPPORTED_MODES",
    "CLASSIFICATION_PROMPT_VERSION",
    "modes_for",
]


@dataclass(frozen=True)
class ModeSpec:
    """Everything the app knows about one chart type, in one declaration."""

    name: str

    # --- prompt / cache -------------------------------------------------
    prompt_version: str
    #: "rca_core.prompt:RANGE_CHART_SYSTEM_PROMPT", or None when the mode has
    #: no dedicated system prompt (it borrows another mode's).
    prompt_ref: str | None = None

    # --- capability / routing flags -------------------------------------
    maturity: str = "candidate"
    export_supported: bool = False
    server_supported: bool = False
    browser_supported: bool = True
    merge_wired: bool = False

    # --- payload shape --------------------------------------------------
    #: Name of the module-level tuple in rca_core/extractor.py holding this
    #: mode's documented root keys. Resolved lazily against extractor globals
    #: (REVIEW-2026-09-20 #7 moved it out of an import-time dict).
    root_keys_attr: str | None = None
    #: Used instead of root_keys_attr for the one mode whose keys are a plain
    #: inline tuple.
    root_keys_inline: tuple[str, ...] = ()
    #: The lists of objects the model is asked to emit. The exporter's generic
    #: fallback renders any populated one it does not recognise, so a mode that
    #: lists one here still renders if the model invents another.
    row_tables: tuple[str, ...] = ()

    # --- caption auto-detection ----------------------------------------
    #: Whole-word keywords. Matched with a leading AND trailing ASCII word
    #: boundary (see chart_mode._match_kw).
    keywords_ascii: tuple[str, ...] = ()
    #: Non-ASCII keywords are plain substrings - the CJK/Cyrillic renderings
    #: have no usable word boundary.
    keywords_cjk: tuple[str, ...] = ()
    #: Keywords that are word STEMS, not whole words. A stem is matched with a
    #: LEADING word boundary only, so it also hits inflected / compound
    #: continuations. This is NOT a stylistic list: a stem left out here fails
    #: SILENTLY on the very words it was chosen for. ``localit`` without this
    #: entry matches the word "localit" and never "localities".
    keywords_stems: tuple[str, ...] = ()
    #: Bounded regexes for phrasings no keyword tuple can express.
    keywords_phrase_res: tuple[re.Pattern[str], ...] = ()

    # --- code references (resolved lazily; never imported here) --------
    #: "rca_core.extractor:normalize_result"
    normalizer_ref: str | None = None

    notes: str = ""
    #: Extra per-mode data merged into the capability entry.
    extra: dict[str, Any] = field(default_factory=dict)


# ===========================================================================
# The registry.
#
# Keyword tuples are MOVED verbatim out of rca_core/chart_mode.py - do not
# reword them. "Conservative" matters there: a keyword hit SKIPS the vision
# classifier, so a loose keyword silently extracts the wrong schema
# (REVIEW-2026-09-20 #105 item 3).
# ===========================================================================

MODES: dict[str, ModeSpec] = {}


def _add(spec: ModeSpec) -> ModeSpec:
    MODES[spec.name] = spec
    return spec


_add(ModeSpec(
    name="range_chart",
    prompt_version="v5",
    prompt_ref="rca_core.prompt:RANGE_CHART_SYSTEM_PROMPT",
    maturity="stable",
    export_supported=True,
    server_supported=True,
    browser_supported=True,
    merge_wired=True,
    root_keys_attr="_KNOWN_RANGE_CHART_KEYS",
    row_tables=("species_ranges",),
    keywords_ascii=("range chart", "range-chart"),
    keywords_cjk=("延限", "карта совмещения", "график совмещения"),
    normalizer_ref="rca_core.extractor:normalize_result",
    notes="Core mode; E2E-tested on literature range charts "
          "(species x sample matrices with abundance and plates).",
))

_add(ModeSpec(
    name="columnar_section",
    prompt_version="v3",
    prompt_ref="rca_core.prompt:COLUMNAR_SECTION_SYSTEM_PROMPT",
    maturity="stable",
    export_supported=True,
    server_supported=True,
    browser_supported=True,
    merge_wired=True,
    root_keys_attr="_KNOWN_COLUMNAR_ROOT_KEYS",
    row_tables=("sections",),
    keywords_ascii=("column", "columns", "columnar", "col_section",
                    "col_sections"),
    keywords_cjk=("柱状", "柱状図", "柱状图"),
    normalizer_ref="rca_core.extractor:normalize_columnar_result",
    notes="Lithologic columns with formation / thickness / legend.",
))

_add(ModeSpec(
    name="abundance_diagram",
    prompt_version="v4",
    prompt_ref="rca_core.prompt:ABUNDANCE_DIAGRAM_SYSTEM_PROMPT",
    maturity="candidate",
    export_supported=True,
    server_supported=True,
    browser_supported=True,
    merge_wired=True,
    root_keys_attr="_KNOWN_ABUNDANCE_ROOT_KEYS",
    row_tables=("sites", "abundances", "zones"),
    keywords_ascii=("pollen", "abundance", "percentage diagram", "palyno"),
    keywords_cjk=("孢粉", "花粉", "丰度", "百分比"),
    normalizer_ref="rca_core.extractor:normalize_abundance_result",
    notes="Down-core curves and profiles work well; pie charts, heatmaps "
          "and distribution maps degrade to empty output by design.",
))

_add(ModeSpec(
    name="phylogenetic_tree",
    prompt_version="v2",
    prompt_ref="rca_core.prompt:PHYLOGENETIC_TREE_SYSTEM_PROMPT",
    maturity="candidate",
    export_supported=True,
    server_supported=True,
    browser_supported=True,
    merge_wired=True,
    # The one mode whose root keys are a plain inline tuple rather than a
    # named constant next to its normalizer.
    root_keys_inline=("metadata", "nodes", "root_ids", "legend", "confidence"),
    row_tables=("nodes",),
    keywords_ascii=("phylogen", "phylogram", "cladogram", "dendrogram",
                    "molecular phylogen"),
    keywords_cjk=("系统发育", "进化树", "系统树", "分子系统"),
    normalizer_ref="rca_core.extractor:normalize_phylogenetic_tree_result",
    notes="Newick output with parent/id structure.",
))

_add(ModeSpec(
    name="zonation_chart",
    prompt_version="v1",
    prompt_ref="rca_core.prompt:ZONATION_CHART_SYSTEM_PROMPT",
    maturity="candidate",
    export_supported=True,
    server_supported=True,
    browser_supported=True,
    merge_wired=True,
    root_keys_attr="_KNOWN_ZONATION_ROOT_KEYS",
    row_tables=("zones",),
    keywords_ascii=("zonation", "biozonation", "zone correlation",
                    "correlation chart", "correlation of zones"),
    keywords_cjk=("生物带", "化石带", "带状对比", "对比图"),
    # REVIEW-2026-09-20 #105: real zone-correlation captions separate the two
    # halves of the phrase ("Correlation of Triassic radiolarian ZONES and
    # subzones"), which no keyword tuple can express. One bounded regex keeps
    # those hits while still refusing the lithostratigraphic "Correlation of
    # the measured sections" - the sentence clause stops at the first full stop.
    keywords_phrase_res=(
        re.compile(r"\bcorrelation of\b[^.\n]{0,80}?\bzones?\b"),
    ),
    normalizer_ref="rca_core.extractor:normalize_zonation_chart_result",
    notes="Biozonation / correlation charts (radiolarian biochronology); "
          "E2E recovered 32 zones + cross-framework correlations from a "
          "Triassic zonation figure.",
))

# --- REVIEW-2026-09-20 #105 (item 3): conservative tables for the three
# modes that previously had none. "Conservative" = only words that name this
# chart family and essentially nothing else, because a keyword hit SKIPS the
# vision classifier - a false positive silently extracts the wrong schema.

_add(ModeSpec(
    name="chemical_stratigraphy",
    prompt_version="v2",
    prompt_ref="rca_core.prompt:CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT",
    maturity="assistant",
    export_supported=False,
    server_supported=False,
    browser_supported=False,
    merge_wired=False,
    root_keys_attr="_KNOWN_CHEMICAL_STRAT_ROOT_KEYS",
    row_tables=("data_points", "events", "intervals"),
    keywords_ascii=("isotop", "chemostrat", "chemical stratigraphy",
                    "chemical stratigraphic"),
    keywords_cjk=("同位素", "化学地层", "化学地層", "地球化学",
                  "изотоп", "геохим"),
    normalizer_ref="rca_core.extractor:normalize_chemical_stratigraphy_result",
    notes="Geochemical curves / age-distribution summaries; broad genre, "
          "expect partial yields.",
))

_add(ModeSpec(
    name="paleomap",
    prompt_version="v2",
    prompt_ref="rca_core.prompt:PALEOMAP_SYSTEM_PROMPT",
    maturity="assistant",
    # AUDIT-2026-09-27 [item 4.2]: promoted to export_supported. The tables
    # were always in the extractor output - measured over 66 recorded real
    # responses, 7 palaeomap results carried 24 tables (9 continents, 16
    # fossil sites with coordinates, tectonic features, biogeographic
    # realms) - but this flag was False, so UIs disabled the table tab and
    # the data was reachable only by exporting JSON. There is now a
    # `_paleomap_tables` branch in exporter.py and a matching
    # `rcaPaleomapConfigs` in js/table.js.
    export_supported=True,
    server_supported=False,
    browser_supported=False,
    merge_wired=False,
    root_keys_attr="_KNOWN_PALEOMAP_ROOT_KEYS",
    row_tables=("continents", "fossil_sites", "tectonic_features"),
    keywords_ascii=("paleomap", "palaeomap", "paleogeograph", "palaeogeograph",
                    "paleocontinent", "palaeocontinent"),
    keywords_cjk=("古地理", "古海洋", "古大陆", "板块重建",
                  "палеогеограф", "палеокарт", "палеоконтинент"),
    normalizer_ref="rca_core.extractor:normalize_paleomap_result",
    notes="Palaeogeographic maps and locality figures. Present-day "
          "geological maps are honestly refused by the model.",
))

_add(ModeSpec(
    name="scatter_plot",
    prompt_version="v2",
    prompt_ref="rca_core.prompt:SCATTER_PLOT_SYSTEM_PROMPT",
    maturity="assistant",
    export_supported=False,
    server_supported=False,
    browser_supported=False,
    merge_wired=False,
    root_keys_attr="_KNOWN_SCATTER_PLOT_ROOT_KEYS",
    row_tables=("groups", "points", "outliers"),
    keywords_ascii=("scatter plot", "scatterplot", "scatter diagram",
                    "biplot", "crossplot", "cross plot"),
    keywords_cjk=("散点", "散布図", "точечная диаграмма", "рассеяни",
                  "рассеиван"),
    normalizer_ref="rca_core.extractor:normalize_scatter_plot_result",
    notes="Scatter / biplot extraction; morphospace plots vary.",
))

# --- NEW (2026-10-15): occurrence / abundance matrices. -------------------
# Rows = taxa, COLUMNS = locality / borehole identifiers printed as rotated
# headers, each cell holding an ordinal class letter (A/R/C ...). There is no
# continuous depth or age axis, so this mode deliberately carries NO
# pos_0_999 contract and NO axis_calibration block.
#
# Calibration note that shaped the prompt: in the common A/R/C convention the
# ALPHABETICAL order of the codes coincides with their abundance order
# (abundant > common > rare). Anything that sorts codes, or assumes "the first
# letter is the most abundant", is therefore silently wrong on exactly the
# papers this mode targets. The legend is read FROM THE FIGURE per run.
_add(ModeSpec(
    name="occurrence_matrix",
    prompt_version="v1",
    prompt_ref="rca_core.prompt:OCCURRENCE_MATRIX_SYSTEM_PROMPT",
    maturity="assistant",
    export_supported=True,
    server_supported=True,
    # Phase 1 ships backend-only: the browser has no normalizer for this mode
    # yet, so js/minimax.js lists it in RCA_BROWSER_UNSUPPORTED_MODES and the
    # browser transport refuses it loudly rather than normalising the payload
    # as a range chart. Flip this to True in the same commit that adds
    # js/minimax.js:rcaNormalizeOccurrenceResult.
    browser_supported=False,
    merge_wired=True,
    root_keys_inline=("columns", "legend", "occurrences", "strat_rows",
                      "taxon_rows", "confidence", "cell_status"),
    row_tables=("occurrences",),
    keywords_ascii=("localit", "borehole", "boreholes", "wellbore",
                    "well log", "well no", "occurrence matrix",
                    "occurrence chart", "abundance matrix",
                    "distribution matrix", "a/r/c", "frequency class",
                    "abundance class", "range of occurrences"),
    keywords_cjk=("产地", "地点", "钻孔", "井", "出现矩阵", "分布矩阵",
                  "丰度矩阵"),
    # "localit" MUST be a stem or it never matches "locality"/"localities",
    # which is the word the caption actually prints.
    keywords_stems=("localit", "wellbore", "borehol"),
    normalizer_ref="rca_core.extractor:normalize_occurrence_matrix_result",
    notes="Taxon x locality occurrence matrices with ordinal class letters "
          "and NO continuous axis. Sparse by contract: one row per FILLED "
          "cell, never per blank.",
))

# --- NEW (2026-10-15): the honest fallback for an unrecognised chart genre.
# This is the DEFAULT, so it needs no per-mode registration anywhere: it is
# what the app does when nothing else matches.
#
# The defect it replaces: an unknown chart used to fall through to
# rcaNormalizeResult / RANGE_CHART_SCHEMA, which emitted a confident-looking
# range chart with invented pos_0_999 values. Wrong output that LOOKED right
# is worse than a refusal. This mode reports the structure it can see and
# says plainly when it cannot.
_add(ModeSpec(
    name="generic_table",
    prompt_version="v1",
    prompt_ref="rca_core.prompt:GENERIC_TABLE_SYSTEM_PROMPT",
    maturity="assistant",
    export_supported=True,
    server_supported=False,
    browser_supported=False,
    merge_wired=False,
    root_keys_inline=("columns", "rows", "legend", "notes", "confidence"),
    row_tables=("rows",),
    # Deliberately NO keywords: this mode is never auto-detected, it is the
    # thing everything else falls back to. A keyword here would let it
    # cannibalise genres that deserve a real mode.
    normalizer_ref="rca_core.extractor:normalize_generic_table_result",
    notes="Last-resort structural extraction for an unrecognised chart "
          "genre: report the grid that is actually visible, and refuse "
          "plainly when the figure is not a table at all.",
))


# ===========================================================================
# Derived views. Every existing call site keeps its own name; only where the
# values come from changes. That is deliberate: it keeps 3491 tests and the
# whole JS mirror working unchanged while removing the chance of a MISSED
# registration.
# ===========================================================================

MODE_NAMES: frozenset[str] = frozenset(MODES)

#: Modes that have a MergeSchema. merge_results must reject everything else.
WIRED_MODE_NAMES: frozenset[str] = frozenset(
    name for name, spec in MODES.items() if spec.merge_wired)

#: Modes the HTTP API accepts as an explicit request.
SERVER_MODE_NAMES: frozenset[str] = frozenset(
    name for name, spec in MODES.items() if spec.server_supported)

#: Modes the BROWSER transport cannot normalise. Mirrored in js/minimax.js
#: RCA_BROWSER_UNSUPPORTED_MODES - tests/test_browser_unsupported_modes.py
#: keeps the two lists equal.
BROWSER_UNSUPPORTED_MODES: frozenset[str] = frozenset(
    name for name, spec in MODES.items() if not spec.browser_supported)

#: The vision classifier's own prompt. Not a chart type.
CLASSIFICATION_PROMPT_VERSION = "v1"

#: Legacy cache-key aliases. Not modes.
PROMPT_ALIASES: dict[str, str] = {"abundance": "v4"}

#: Exactly what rca_core/prompt.py PROMPT_VERSION must contain: every mode's
#: version, plus the classifier, plus the legacy aliases.
PROMPT_VERSIONS: dict[str, str] = {
    name: spec.prompt_version for name, spec in MODES.items()
}
PROMPT_VERSIONS["chart_classify"] = CLASSIFICATION_PROMPT_VERSION
PROMPT_VERSIONS.update(PROMPT_ALIASES)


def modes_for(**flags: bool) -> tuple[ModeSpec, ...]:
    """Modes matching every given flag, e.g. ``modes_for(merge_wired=True)``.

    Used by the derive sites and by the tests that assert a flag implies a
    capability (browser_supported -> export_supported is NOT implied; see the
    per-mode notes).
    """
    out = []
    for spec in MODES.values():
        if all(getattr(spec, k) is v for k, v in flags.items()):
            out.append(spec)
    return tuple(sorted(out, key=lambda s: s.name))
