"""The single declaration of "what shape is this result payload?".

WHY THIS EXISTS (ARCH-REVIEW-2026-11-02, P1-3 / A7)
===================================================
These detectors were written THREE times: rca_core/aggregate.py (4),
rca_core/exporter.py (6), and js/table.js (7). A payload that one module
called `columnar` and another called `range_chart` produced silent data
loss, and the repo has the scars:

  * `normalize_generic_table_result` also emits `columns`. When the
    occurrence-matrix detector keyed on `columns`, every generic_table
    result was classified as a matrix; the exporter returned matrix table
    configs with no `rows`, so the WHOLE DATA SET vanished from CSV/XLSX
    while still being visible in the browser.
  * The `gated` cell strategy empties `occurrences` BY DESIGN, so a gated
    matrix was invisible until `taxon_rows` became a second signal.
  * Empty `abundances: []` is a placeholder in freshly-initialized objects;
    accepting it misrouted plain range charts to the abundance renderer.

The bodies below were extracted VERBATIM from rca_core/exporter.py by AST,
so unifying them does not change a single decision aggregate.py used to
make. Both consumers now import these names under their old private
aliases, keeping every existing call site intact.

DESIGN CONSTRAINT: ZERO rca_core IMPORTS
=======================================
`exporter` already depends on `bed_parser` and `editable` depends on
`exporter`. Importing either from here would create a cycle -- the AST
guard in tests/test_rca_core_acyclic.py would catch it. This module is
stdlib-only, like rca_core/modes.py.

The JS mirrors in js/table.js are pinned against the Python side by
tests/js/tests_detector_parity.js (A11); cross-language code cannot be
shared, only locked.
"""
from __future__ import annotations

from typing import Any

__all__ = ["looks_columnar", "looks_abundance", "looks_phylogenetic_tree",
           "looks_zonation_chart", "looks_paleomap", "looks_occurrence_matrix",
           "MODE_FOR_SHAPE"]

def looks_columnar(data: dict[str, Any] | None) -> bool:
    """Heuristic: if data.sections is present and its first item has an 'id'
    field (column-label) rather than a 'name' (measured-section), it's a
    columnar-section result. Mirrors js/table.js.
    """
    if not data:
        return False
    sects = data.get("sections")
    if not isinstance(sects, list):
        return False
    if not sects:
        return False
    first = sects[0]
    # "id" is the definitive columnar marker (it holds the column-label
    # value). Even if "name" is also present (LLM being verbose), the
    # presence of "id" is sufficient to classify as columnar. Range-chart
    # sections use "name" (measured-section name) and never have "id".
    return isinstance(first, dict) and ("id" in first)

def looks_abundance(data: dict[str, Any] | None) -> bool:
    """Heuristic: an abundance-diagram result carries an ``abundances`` list
    (the per-(taxon, level) rows). Mirrors js/table.js.

    An empty list (`abundances: []`) is NOT enough to call this an
    abundance result — every freshly-extracted object schema includes an
    empty `abundances` placeholder before the model fills it in, so
    accepting empty lists misroutes pure range-chart results to the
    abundance renderer. Require at least one entry plus a non-empty
    `abundances` list to disambiguate.
    """
    if not data:
        return False
    ab = data.get("abundances")
    return isinstance(ab, list) and len(ab) > 0

def looks_phylogenetic_tree(data: dict[str, Any] | None) -> bool:
    """Heuristic: a phylogenetic-tree result carries a ``nodes`` list
    with parent/id structure and optionally ``root_ids``."""
    if not data:
        return False
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    first = nodes[0]
    return isinstance(first, dict) and "id" in first and "parent" in first

def looks_zonation_chart(data: dict[str, Any] | None) -> bool:
    """Heuristic: a zonation / correlation chart result carries a
    non-empty ``correlations`` list, or a non-empty ``zones`` list of
    zone-rank rows (``rank`` / ``zonation`` markers) that no other mode
    emits. UI-REVIEW-2026-09-05 (radiolarian biochronology charts)."""
    if not data:
        return False
    corr = data.get("correlations")
    if isinstance(corr, list) and len(corr) > 0:
        return True
    zones = data.get("zones")
    if isinstance(zones, list) and len(zones) > 0:
        first = zones[0]
        if isinstance(first, dict) and ("rank" in first or "zonation" in first):
            return True
    # A payload whose zonation column descriptors are populated is a
    # zonation chart even when every zone row was unreadable.
    zns = data.get("zonations")
    if isinstance(zns, list) and len(zns) > 0:
        return True
    return False

def looks_paleomap(data: dict[str, Any] | None) -> bool:
    """True when the payload carries paleomap-shaped tables.

    AUDIT-2026-09-27 [item 4.2]. Mirrors js/table.js.

    The trigger is any paleomap-exclusive key that actually holds rows. The
    exclusivity matters: `metadata` / `confidence` overlap every mode, and
    a range chart may legitimately carry a `note`. Being strict on the key
    NAME but not on its CONTENT is what made the old behaviour wrong in both
    directions -- it classified 7 real responses with 24 populated tables as
    "no tables at all", and let 7 genuinely empty range charts through as
    tabular.
    """
    if not isinstance(data, dict) or not data:
        return False
    for k in ("continents", "oceans_seas", "tectonic_features",
              "biogeographic_realms", "paleolatitude_indicators", "fossil_sites"):
        v = data.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return True
    return False

def looks_occurrence_matrix(data: dict[str, Any] | None) -> bool:
    """A taxon x locality matrix carries a populated ``occurrences`` list.

    The discriminator is ``occurrences``, NOT ``columns``. This looked like a
    harmless choice until a cross-mode collision test caught what it costs:
    ``normalize_generic_table_result`` ALSO emits ``columns``, so keying on it
    classified every generic_table result as an occurrence matrix. The exporter
    then returned THIS mode's three table configs, which do not include
    ``rows`` — so the generic table's entire data set vanished from the export
    while remaining visible in the browser (js/table.js falls through to its
    own generic renderer). Data present on screen, absent from CSV/XLSX, and
    `export_supported=True` in the registry asserting the opposite.

    ``occurrences`` is genuinely unique: no other normalizer in the app emits
    that key. An occurrence_matrix whose cells were not read falls through to
    `_generic_tables`, which renders its ``columns`` and ``legend`` anyway —
    degraded but not lost, which is the correct trade.
    """
    if not isinstance(data, dict):
        return False
    occ = data.get("occurrences")
    if isinstance(occ, list) and occ and isinstance(occ[0], dict):
        return True
    # 2026-10-16: `taxon_rows` is accepted as a SECOND signal, and without it
    # the `gated` cell strategy is invisible to the export. That strategy
    # returns headers, taxon labels and the legend and deliberately no cell
    # reading, so `occurrences` is empty BY DESIGN - keying on it alone sent
    # every gated result to `_generic_tables`, which renders `columns` and
    # `legend` and drops the taxon names. So the half of the figure the model
    # reads best (measured 20/22 taxa, 7/8 authorities) would be the half the
    # exported workbook cannot show.
    #
    # The key is as unique as `occurrences`: declared only in this mode's
    # normalizer (`normalize_occurrence_matrix_result`), which is exactly the
    # property `occurrences` was chosen for when `columns` proved to collide.
    taxa = data.get("taxon_rows")
    return bool(isinstance(taxa, list) and taxa and isinstance(taxa[0], dict))

# Which schema name each detector maps to. Kept as data so new shapes
# cannot be added to one side of the auto-detect chain without the other.
MODE_FOR_SHAPE = {
    "phylogenetic_tree": "looks_phylogenetic_tree",
    "columnar_section": "looks_columnar",
    "occurrence_matrix": "looks_occurrence_matrix",
    "abundance_diagram": "looks_abundance",
    "zonation_chart": "looks_zonation_chart",
    "paleomap": "looks_paleomap",
}
