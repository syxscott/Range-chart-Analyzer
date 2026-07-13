"""Range Chart Analyzer shared core.

Pure-stdlib (plus optional Pillow) extraction core shared by the Tkinter
GUI (gui.py), the backend server (server.py), and — via the server — the
web frontend.
"""

from __future__ import annotations

from .exporter import (
    TABLE_CONFIGS,
    build_table_export,
    get_configs_for_result,
    result_to_json,
    to_csv,
    to_tsv,
)
from .aggregate import merge_results
from .extractor import (
    DEFAULT_ENDPOINT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    ExtractResult,
    extract,
    extract_columnar_section,
    extract_range_chart,
    load_image_b64,
    normalize_columnar_result,
    normalize_result,
)
from .i18n import TRANSLATIONS, Translator
from .json_utils import extract_balanced_json_object, safe_json_loads
from .prompt import (
    CHART_LANG_HINT,
    COLUMNAR_SECTION_SYSTEM_PROMPT,
    RANGE_CHART_SYSTEM_PROMPT,
)
from .llm import (
    ApiFormat,
    LlmProvider,
    ProviderPreset,
    ProviderStore,
    PROVIDER_PRESETS,
    call_llm_api,
)

__all__ = [
    "TABLE_CONFIGS",
    "build_table_export",
    "get_configs_for_result",
    "result_to_json",
    "to_csv",
    "to_tsv",
    "merge_results",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "ExtractResult",
    "extract",
    "extract_columnar_section",
    "extract_range_chart",
    "load_image_b64",
    "normalize_columnar_result",
    "normalize_result",
    "TRANSLATIONS",
    "Translator",
    "extract_balanced_json_object",
    "safe_json_loads",
    "CHART_LANG_HINT",
    "COLUMNAR_SECTION_SYSTEM_PROMPT",
    "RANGE_CHART_SYSTEM_PROMPT",
    "ApiFormat",
    "LlmProvider",
    "ProviderPreset",
    "ProviderStore",
    "PROVIDER_PRESETS",
    "call_llm_api",
]
