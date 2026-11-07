"""Mode normalizers, split out of ``rca_core/extractor.py`` (Phase 3 / P1-4).

``modes_impl._shared`` is a leaf; this package never imports ``extractor``.
``extractor.py`` re-exports every name at the end of that module.
"""
from __future__ import annotations

from .range_chart import normalize_result as normalize_result
from .columnar_section import normalize_columnar_result as normalize_columnar_result
from .abundance_diagram import normalize_abundance_result as normalize_abundance_result
from .phylogenetic_tree import normalize_phylogenetic_tree_result as normalize_phylogenetic_tree_result
from .zonation_chart import normalize_zonation_chart_result as normalize_zonation_chart_result
from .chemical_stratigraphy import normalize_chemical_stratigraphy_result as normalize_chemical_stratigraphy_result
from .paleomap import normalize_paleomap_result as normalize_paleomap_result
from .scatter_plot import normalize_scatter_plot_result as normalize_scatter_plot_result
from .occurrence_matrix import normalize_occurrence_matrix_result as normalize_occurrence_matrix_result
from .generic_table import normalize_generic_table_result as normalize_generic_table_result

from .range_chart import _normalize_section_into as _normalize_section_into
from .range_chart import _normalize_occurrence_mode as _normalize_occurrence_mode
from .range_chart import _normalize_endpoint_kind as _normalize_endpoint_kind
from .range_chart import _normalize_optional_int as _normalize_optional_int
from .range_chart import _normalize_confidence as _normalize_confidence
from .range_chart import _normalize_species_into as _normalize_species_into
from .range_chart import _normalize_biozone_into as _normalize_biozone_into
from .phylogenetic_tree import _normalize_phylogenetic_tree_into as _normalize_phylogenetic_tree_into

__all__ = [
    'normalize_result',
    'normalize_columnar_result',
    'normalize_abundance_result',
    'normalize_phylogenetic_tree_result',
    'normalize_zonation_chart_result',
    'normalize_chemical_stratigraphy_result',
    'normalize_paleomap_result',
    'normalize_scatter_plot_result',
    'normalize_occurrence_matrix_result',
    'normalize_generic_table_result',
    '_normalize_section_into',
    '_normalize_occurrence_mode',
    '_normalize_endpoint_kind',
    '_normalize_optional_int',
    '_normalize_confidence',
    '_normalize_species_into',
    '_normalize_biozone_into',
    '_normalize_phylogenetic_tree_into',
]
