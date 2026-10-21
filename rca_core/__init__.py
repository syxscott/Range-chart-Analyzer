"""Range Chart Analyzer shared core.

Pure-stdlib (plus optional Pillow) extraction core shared by the Tkinter
GUI (gui.py), the Fluent desktop GUI (gui_fluent.py), the backend
server (server.py), and — via the server — the web frontend.

Layered data flow:
* ``extractor`` + ``llm`` + ``prompt`` — produce a normalized result
  dict and an ``ExtractResult`` carrying token usage.
* ``exporter`` — render that dict as CSV / TSV / JSON / XLSX.
* ``aggregate`` — multi-run merge (with majority voting for free-text
  fields, structured-field union for list-of-dicts sub-arrays).
* ``editable`` — diff / apply user edits on the result dict.
* ``history`` + ``usage`` + ``db`` — SQLite-backed persistence of
  past extractions and per-call token usage; provider configuration
  still lives in JSON for atomic-write / portability.
* ``deskew`` + ``geometry`` — whole-page skew correction and the
  pixel ↔ data-value axis calibration built on top of it.
"""

from __future__ import annotations

__version__ = "0.1.3"
# 2026-10-16: this read "2.0.0" while the project shipped 0.1.0. It is not
# cosmetic — `exporter._package_version()` stamps it into the header of every
# CSV/TSV/JSON/XLSX exchange file, so every export claimed to come from a
# version of this software that has never existed. No test pinned it (nothing
# under tests/ mentions __version__), which is why a number that was wrong from
# the start survived the whole release history. It now tracks the CHANGELOG.

from .exporter import (
    TABLE_CONFIGS,
    apply_table_edits,
    build_table_export,
    get_configs_for_result,
    result_to_json,
    to_csv,
    to_tsv,
    to_xlsx,
    to_newick_file,
    to_wpd,
)
from .aggregate import merge_results
from .extractor import (
    DEFAULT_ENDPOINT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_EDGE,
    DEFAULT_MODEL,
    ExtractResult,
    extract,
    extract_abundance_diagram,
    extract_chemical_stratigraphy,
    extract_columnar_section,
    extract_generic_table,
    extract_occurrence_matrix,
    extract_paleomap,
    extract_range_chart,
    extract_scatter_plot,
    load_image_b64,
    normalize_abundance_result,
    normalize_chemical_stratigraphy_result,
    normalize_columnar_result,
    normalize_generic_table_result,
    normalize_occurrence_matrix_result,
    normalize_paleomap_result,
    normalize_result,
    normalize_phylogenetic_tree_result,
    normalize_scatter_plot_result,
    to_newick,
)
from .i18n import TRANSLATIONS, Translator
from .json_utils import extract_balanced_json_object, safe_json_loads
from .prompt import (
    ABUNDANCE_DIAGRAM_SYSTEM_PROMPT,
    CHART_LANG_HINT,
    CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT,
    COLUMNAR_SECTION_SYSTEM_PROMPT,
    GENERIC_TABLE_SYSTEM_PROMPT,
    OCCURRENCE_MATRIX_SYSTEM_PROMPT,
    PALEOMAP_SYSTEM_PROMPT,
    PHYLOGENETIC_TREE_SYSTEM_PROMPT,
    RANGE_CHART_SYSTEM_PROMPT,
    SCATTER_PLOT_SYSTEM_PROMPT,
)
from .llm import (
    ApiFormat,
    LlmProvider,
    ProviderPreset,
    ProviderStore,
    PROVIDER_PRESETS,
    call_llm_api,
)
from .db import Database
from .history import HistoryRecord, HistoryStore
from .usage import (
    UsageRecord,
    UsageStore,
    UsageSummary,
    estimate_tokens,
    parse_usage,
)
from .editable import (
    apply_edits,
    capture_edits,
    is_dirty,
    new_row_template,
)
# BORROW-2026-09-20 modules.  Both import on stdlib alone (Pillow / cv2 are
# lazily required only when the image helpers are actually called), so
# exporting them here keeps the "rca_core imports anywhere" contract.
# FIX-2026-09-22: the deskew + geometry public API was only reachable via
# submodule imports; surface it next to the rest of the core.
from .deskew import (
    MIN_USEFUL_ANGLE,
    DeskewError,
    deskew_image,
    estimate_skew_angle,
    hough_skew_angle,
)
from .geometry import (
    RESIDUAL_FRACTION,
    SCHEMA_VERSION,
    Anchor,
    AxisCalibration,
    Calibration,
    CalibrationError,
    fit_linear,
)

__all__ = [
    # tables / export
    "TABLE_CONFIGS",
    "apply_table_edits",
    "build_table_export",
    "get_configs_for_result",
    "result_to_json",
    "to_csv", "to_tsv", "to_xlsx", "to_newick_file",
    # aggregate / extract / i18n / json / prompt
    "merge_results",
    "DEFAULT_ENDPOINT", "DEFAULT_MAX_TOKENS", "DEFAULT_MAX_EDGE", "DEFAULT_MODEL",
    "ExtractResult",
    "extract", "extract_abundance_diagram", "extract_chemical_stratigraphy",
    "extract_columnar_section", "extract_generic_table",
    "extract_occurrence_matrix", "extract_paleomap", "extract_range_chart",
    "extract_scatter_plot",
    "load_image_b64",
    "normalize_abundance_result", "normalize_chemical_stratigraphy_result",
    "normalize_columnar_result", "normalize_generic_table_result",
    "normalize_occurrence_matrix_result", "normalize_paleomap_result",
    "normalize_result",
    "normalize_phylogenetic_tree_result", "normalize_scatter_plot_result",
    "to_newick",
    "TRANSLATIONS", "Translator",
    "extract_balanced_json_object", "safe_json_loads",
    "ABUNDANCE_DIAGRAM_SYSTEM_PROMPT",
    "CHART_LANG_HINT",
    "CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT",
    "COLUMNAR_SECTION_SYSTEM_PROMPT",
    "PALEOMAP_SYSTEM_PROMPT",
    "RANGE_CHART_SYSTEM_PROMPT",
    "SCATTER_PLOT_SYSTEM_PROMPT",
    "PHYLOGENETIC_TREE_SYSTEM_PROMPT",
    "GENERIC_TABLE_SYSTEM_PROMPT",
    "OCCURRENCE_MATRIX_SYSTEM_PROMPT",
    # providers / llm
    "ApiFormat", "LlmProvider", "ProviderPreset", "ProviderStore",
    "PROVIDER_PRESETS", "call_llm_api",
    # persistence
    "Database", "HistoryRecord", "HistoryStore",
    "UsageRecord", "UsageStore", "UsageSummary",
    "estimate_tokens", "parse_usage",
    # editing
    "apply_edits", "capture_edits", "is_dirty", "new_row_template",
    # deskew / geometry (FIX-2026-09-22: was only reachable via submodules)
    "MIN_USEFUL_ANGLE", "DeskewError", "deskew_image", "estimate_skew_angle",
    "hough_skew_angle",
    "SCHEMA_VERSION", "RESIDUAL_FRACTION", "CalibrationError", "Anchor",
    "AxisCalibration", "Calibration", "fit_linear",
]
