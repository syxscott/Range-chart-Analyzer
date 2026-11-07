"""Auto-detect must refuse modes it cannot merge, and must know zonation.

ARCH-REVIEW-2026-11-02 A8. Two modes were misrouted by
aggregate._auto_detect_schema:
  * zonation_chart HAS a MergeSchema but no detector branch -> merged as
    range_chart, primary `zones` rows silently lost.
  * paleomap has NO MergeSchema -> fell through to range_chart and returned
    ok=True while dropping continents / fossil_sites / data_points.

Both are the "confidently wrong output" class the registry was built to
remove. These tests pin the new behaviour WITHOUT touching any other verdict.
"""
from __future__ import annotations

import pytest

from rca_core import aggregate as agg
from rca_core.shape import (looks_paleomap, looks_zonation_chart,
                            looks_occurrence_matrix)


def _zonation_payload():
    return {
        "zones": [{"zonation": "A", "name": "Zone 1", "rank": 1}],
        "zonations": [{"name": "A"}],
        "correlations": [],
        "confidence": 0.9,
    }


def _paleomap_payload():
    return {
        "continents": [{"name": "Pangea"}],
        "fossil_sites": [{"name": "Site 1"}],
        "confidence": 0.9,
    }


def test_zonation_is_now_auto_detected():
    assert looks_zonation_chart(_zonation_payload())
    assert agg._auto_detect_schema([_zonation_payload()]) is agg.ZONATION_CHART_SCHEMA


def test_zonation_merge_keeps_its_primary_rows():
    out = agg.merge_results([_zonation_payload()], total_runs=1)
    assert out.get("zones"), "zonation rows vanished from the merge"


def test_paleomap_auto_detect_is_refused_instead_of_silently_dropped():
    assert looks_paleomap(_paleomap_payload())
    with pytest.raises(ValueError, match="paleomap has no MergeSchema"):
        agg._auto_detect_schema([_paleomap_payload()])
    with pytest.raises(ValueError, match="paleomap has no MergeSchema"):
        agg.merge_results([_paleomap_payload()], total_runs=1)


def test_explicit_schema_still_wins_over_paleomap_refusal():
    out = agg.merge_results([_paleomap_payload()], total_runs=1,
                            schema=agg.RANGE_CHART_SCHEMA)
    assert isinstance(out, dict), "caller-provided schema must bypass auto-detect"


def test_range_chart_and_matrix_verdicts_are_unchanged():
    plain = {"species_ranges": [{"species": "A"}]}
    assert agg._auto_detect_schema([plain]) is agg.RANGE_CHART_SCHEMA
    matrix = {"occurrences": [{"taxon": "A", "locality": "L"}], "columns": []}
    assert looks_occurrence_matrix(matrix)
    assert agg._auto_detect_schema([matrix]) is agg.OCCURRENCE_MATRIX_SCHEMA
