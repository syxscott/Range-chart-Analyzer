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
"""

from __future__ import annotations

from typing import Any

__all__ = ["CAPABILITIES", "maturity_for"]

CAPABILITIES: dict[str, dict[str, Any]] = {
    "range_chart": {
        "maturity": "stable",
        "notes": "Core mode; E2E-tested on literature range charts "
                 "(species x sample matrices with abundance and plates).",
    },
    "columnar_section": {
        "maturity": "stable",
        "notes": "Lithologic columns with formation / thickness / legend.",
    },
    "abundance_diagram": {
        "maturity": "candidate",
        "notes": "Down-core curves and profiles work well; pie charts, "
                 "heatmaps and distribution maps degrade to empty output "
                 "by design.",
    },
    "phylogenetic_tree": {
        "maturity": "candidate",
        "notes": "Newick output with parent/id structure.",
    },
    "zonation_chart": {
        "maturity": "candidate",
        "notes": "Biozonation / correlation charts (radiolarian "
                 "biochronology); E2E recovered 32 zones + cross-framework "
                 "correlations from a Triassic zonation figure.",
    },
    "chemical_stratigraphy": {
        "maturity": "assistant",
        "notes": "Geochemical curves / age-distribution summaries; broad "
                 "genre, expect partial yields.",
    },
    "paleomap": {
        "maturity": "assistant",
        "notes": "Palaeogeographic maps and locality figures; present-day "
                 "geological maps are honestly refused by the model.",
    },
    "scatter_plot": {
        "maturity": "assistant",
        "notes": "Scatter / biplot extraction; morphospace plots vary.",
    },
}

_MATURITIES = {"stable", "candidate", "assistant"}


def maturity_for(mode: str) -> dict[str, Any]:
    """Return the registry entry for *mode* (or an unknown-mode stub)."""
    entry = CAPABILITIES.get(mode)
    if entry is None:
        return {"maturity": "assistant",
                "notes": "unregistered mode"}
    return entry
