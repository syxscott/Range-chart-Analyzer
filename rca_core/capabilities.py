"""Capability registry for the extraction modes.

Borrowed from the GitHub survey (2026-09-07, thu-digitizer's
``extractor_registry.py``): every mode declares its MATURITY so UIs and
docs can tell users honestly what is production-grade versus exploratory
("a case appears in the gallery" != "stably supported").

Maturity levels:
  stable    - exercised end-to-end (real-literature E2E 2026-09-07),
              dedicated prompt vN, full stack (merge/quality/export)
  candidate - full stack wired, works on its canonical figure genre,
              thinner real-world validation
  assistant - extraction exists and returns structured data, but the
              figure genre is broad/hard; expect partial yields

REVIEW-2026-09-20: ``export_supported`` declares whether a mode has a TABLE
representation at all — i.e. whether the CSV / TSV / XLSX / table-edit path
means anything for it. The three ``assistant`` modes (chemical stratigraphy,
palaeomap, scatter plot) have no ``TABLE_CONFIGS`` branch and no js/table.js
renderer, so their only real export is JSON. UIs use this flag to disable the
table tab instead of showing — and exporting — four empty range-chart sheets.
The runtime counterpart is ``rca_core.exporter.detect_tableless_mode``, which
classifies a RESULT rather than a mode; ``tests/test_exporter_modes.py`` keeps
the two lists in agreement.
"""

from __future__ import annotations

from typing import Any

__all__ = ["CAPABILITIES", "maturity_for", "export_supported_for"]

CAPABILITIES: dict[str, dict[str, Any]] = {
    "range_chart": {
        "maturity": "stable",
        "export_supported": True,
        "notes": "Core mode; E2E-tested on literature range charts "
                 "(species x sample matrices with abundance and plates).",
    },
    "columnar_section": {
        "maturity": "stable",
        "export_supported": True,
        "notes": "Lithologic columns with formation / thickness / legend.",
    },
    "abundance_diagram": {
        "maturity": "candidate",
        "export_supported": True,
        "notes": "Down-core curves and profiles work well; pie charts, "
                 "heatmaps and distribution maps degrade to empty output "
                 "by design.",
    },
    "phylogenetic_tree": {
        "maturity": "candidate",
        "export_supported": True,
        "notes": "Newick output with parent/id structure.",
    },
    "zonation_chart": {
        "maturity": "candidate",
        "export_supported": True,
        "notes": "Biozonation / correlation charts (radiolarian "
                 "biochronology); E2E recovered 32 zones + cross-framework "
                 "correlations from a Triassic zonation figure.",
    },
    "chemical_stratigraphy": {
        "maturity": "assistant",
        "export_supported": False,
        "notes": "Geochemical curves / age-distribution summaries; broad "
                 "genre, expect partial yields.",
    },
    "paleomap": {
        "maturity": "assistant",
        "export_supported": False,
        "notes": "Palaeogeographic maps and locality figures; present-day "
                 "geological maps are honestly refused by the model.",
    },
    "scatter_plot": {
        "maturity": "assistant",
        "export_supported": False,
        "notes": "Scatter / biplot extraction; morphospace plots vary.",
    },
}

_MATURITIES = {"stable", "candidate", "assistant"}


def maturity_for(mode: str) -> dict[str, Any]:
    """Return the registry entry for *mode* (or an unknown-mode stub)."""
    entry = CAPABILITIES.get(mode)
    if entry is None:
        return {"maturity": "assistant",
                "export_supported": False,
                "notes": "unregistered mode"}
    return entry


def export_supported_for(mode: str) -> bool:
    """Whether *mode* has tabular export/editing (JSON always works)."""
    return bool(CAPABILITIES.get(mode, {}).get("export_supported", False))
