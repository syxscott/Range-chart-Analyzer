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
means anything for it. The ``assistant`` modes (chemical stratigraphy, scatter
plot) have no ``TABLE_CONFIGS`` branch and no js/table.js renderer, so their
only real export is JSON. UIs use this flag to disable the table tab instead of
showing — and exporting — four empty range-chart sheets.
The runtime counterpart is ``rca_core.exporter.detect_tableless_mode``, which
classifies a RESULT rather than a mode.

AUDIT-2026-09-27 [item 4.2]: ``paleomap`` was in the "no tables" group and has
been moved out. The flag encoded an assumption — that an assistant mode never
returns tables — which the recorded real responses disprove: 7 of 16 palaeomap
results carried 24 populated tables between them. ``detect_tableless_mode`` was
likewise rewritten to ask whether the payload actually CONTAINS a table rather
than whether its key names appear on a whitelist, because the old test was wrong
in both directions (dropping 24 real tables, while letting 7 genuinely empty
results through to a 4-sheet empty workbook).
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
        # AUDIT-2026-09-27 [item 4.2]: promoted to export_supported. The
        # tables were always in the extractor output — measured over 66
        # recorded real responses, 7 palaeomap results carried 24 tables
        # (9 continents, 16 fossil sites with coordinates, tectonic features,
        # biogeographic realms) — but this flag was False, so UIs disabled the
        # table tab and the data was reachable only by exporting JSON. There is
        # now a `_paleomap_tables` branch in exporter.py and a matching
        # `rcaPaleomapConfigs` in js/table.js.
        "export_supported": True,
        "notes": "Palaeogeographic maps and locality figures; six tables "
                 "(continents, oceans/seas, tectonic features, biogeographic "
                 "realms, palaeolatitude indicators, fossil sites). "
                 "Present-day geological maps are honestly refused by the "
                 "model.",
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
