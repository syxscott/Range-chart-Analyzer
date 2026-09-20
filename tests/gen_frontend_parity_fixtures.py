"""Differential fixtures: browser JS mirrors vs the Python engine.

REVIEW-2026-09-20 (frontend parity task, item 14's suggestion generalized to
the whole extraction domain).

The browser mirrors of the extraction pipeline (js/minimax.js,
js/json-utils.js, js/quality.js, js/ics_table.js, js/aggregate.js) are only
useful while they agree with ``rca_core`` — and "agrees" historically meant a
human reading two files. This module pins the contract mechanically instead:

  1. ``CASES`` is a hand-written list of (group, id, input) triples that
     target the divergences found in the 2026-09-20 review (``_array_root``
     bucketing, dict-shaped named arrays, ``fi()`` lossy indices, the
     iron-rule / index-swap warnings, Newick quoting, ``prefer`` spelling,
     ``float()`` vs ``parseFloat()``, the fence-vs-prose ranking ...).
  2. ``compute_python_case()`` runs the CURRENT Python implementation and
     records its answer (or the fact that it raised).
  3. ``tests/fixtures/frontend_parity_2026_09_20.json`` holds inputs AND the
     Python answers; ``tests_diff_frontend_parity.js`` replays the same
     inputs through the JS mirrors in a ``vm`` context and fails on the diff.

Run ``python tests/gen_frontend_parity_fixtures.py`` after intentionally
changing the Python side to re-baseline; ``tests/test_frontend_parity_fixtures_2026_09_20.py``
fails when the committed fixture no longer matches Python, so the two
artifacts cannot drift apart silently.
"""
from __future__ import annotations

import copy
import json
import sys
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core.extractor import (  # noqa: E402
    normalize_abundance_result,
    normalize_chart_classification,
    normalize_columnar_result,
    normalize_result,
    normalize_zonation_chart_result,
    to_newick,
    _normalize_phylogenetic_tree_into,
)
from rca_core.json_utils import safe_json_loads  # noqa: E402
from rca_core.standards.ics import ics_resolve_age_bound  # noqa: E402
# BORROW-2026-09-20 (js-data-layer mirror round): the coverage-contract trio.
import rca_core.reason_codes as RC  # noqa: E402
from rca_core.aggregate import SCHEMA_BY_MODE, merge_results  # noqa: E402
from rca_core.quality import score_range_chart  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "frontend_parity_2026_09_20.json"

GROUPS = (
    "range_chart", "columnar_section", "abundance_diagram",
    "zonation_chart", "phylogenetic_tree", "chart_classification",
    "to_newick", "safe_json_loads", "age_bound",
    "reason_codes", "merge", "quality_coverage",
)


def _case(group: str, cid: str, payload: Any, extra: Any = None) -> dict:
    return {"group": group, "id": cid, "payload": payload, "extra": extra}


CASES: list[dict] = []


def _add(*cases: dict) -> None:
    CASES.extend(cases)


# --- range_chart -----------------------------------------------------------
_add(
    _case("range_chart", "rc_empty", {}),
    _case("range_chart", "rc_foreign", {"totally_unrelated": 1}),
    _case("range_chart", "rc_non_dict", ["a", "b"]),
    _case("range_chart", "rc_none", None),
    _case("range_chart", "rc_array_root_mixed", {
        "_array_root": [
            {"name": "S1", "age_range": "Cretaceous"},
            {"species": "Sp a", "range_top": "10", "range_base": "2"},
            {"name": "Postouwia Zone", "age": "Permian"},
            "bare fossil label",
            {"foo": 1},
        ],
        "confidence": 0.5,
        "other_fossils": ["brachiopod", {"label": "Ammonite"}, ""],
    }),
    _case("range_chart", "rc_array_root_string_rows", {
        "_array_root": ["   ", "Trilobite", {"name": "S", "formations": "Fm A"}],
    }),
    _case("range_chart", "rc_dict_shaped_sections", {
        "sections": {
            "Pingdingshan": {"age_range": "Triassic"},
            "unclear": {"name": "", "age_range": "X"},
            "0": {"age_range": "Y"},
            "not_a_dict": 5,
        },
    }),
    _case("range_chart", "rc_string_row_biozones", {
        "biozones": ["Clarkina postbitteri Zone", {"name": "X Zone"}],
        "sections": ["Alpha"],
        "species_ranges": ["Beta"],
    }),
    _case("range_chart", "rc_iron_rule_species", {
        "species_ranges": [
            {"species": "Postouwia Zone", "range_top": "5", "range_base": "2"},
            {"species": "Genuine taxon", "range_top": "6", "range_base": "2"},
        ],
    }),
    _case("range_chart", "rc_index_order_swap", {
        "species_ranges": [
            {"species": "A", "range_top_idx": 2, "range_base_idx": 9},
            {"species": "B", "range_top_idx": 9, "range_base_idx": 2},
            {"species": "C", "range_top_idx": "2.7", "range_base_idx": 1},
        ],
    }),
    _case("range_chart", "rc_zone_type_ladder", {
        "biozones": [
            {"name": "Interval zonule", "age": "a"},
            {"name": "Subzone oppel", "age": "b"},
            {"name": "Assemblage Zone", "age": "c"},
            {"name": "Acme of X", "age": "d"},
            {"name": "Lineage something", "age": "e"},
            {"name": "Wuchiapingian range zone", "age": "f"},
            {"name": "Plain", "age": "g", "zone_type": 0},
            {"name": "Plain2", "age": "g", "zone_type": False},
            {"name": "Plain3", "age": "g", "zone_type": "subzone"},
            {"name": "Plain4", "age": "g", "zone_type": "not-a-type"},
        ],
    }),
    _case("range_chart", "rc_other_fossils_shapes", {
        "other_fossils": {"a": {"label": "Ammonite"}, "b": "Brachiopod",
                          "c": {"x": 1}, "d": {"name": "Crinoid"}},
    }),
    _case("range_chart", "rc_other_fossils_string", {"other_fossils": "Ostracod"}),
    _case("range_chart", "rc_note_and_extras", {
        "_array_root": [{"name": "S", "age_range": "J"}],
        "_note": "model returned a top-level array",
        "_extras": {"caption": "Fig 3"},
    }),
    _case("range_chart", "rc_confidence_shapes", {
        "sections": [{"name": "S"}], "confidence": "90%"}),
    _case("range_chart", "rc_confidence_bool", {
        "sections": [{"name": "S"}], "confidence": True}),
    _case("range_chart", "rc_confidence_list", {
        "sections": [{"name": "S"}], "confidence": [3]}),
    _case("range_chart", "rc_confidence_absent", {"sections": [{"name": "S"}]}),
    _case("range_chart", "rc_species_field_types", {
        "species_ranges": [{
            "species": "A", "author_year": None, "reworked": True,
            "occurrence_mode": " REWORKED ", "endpoint_kind": "Projected",
            "confidence": "0.42", "range_top_idx": 4, "range_base_idx": 1,
            "note": "n", "formations": ["x"], "unexpected": 7,
        }],
    }),
    _case("range_chart", "rc_section_formations_string", {
        "sections": [{"name": "S", "formations": " Nanling Fm ",
                      "coordinates": {"lat": 1}, "thickness_m": 12.5}],
    }),
)

# --- columnar_section ------------------------------------------------------
_add(
    _case("columnar_section", "col_empty", {}),
    _case("columnar_section", "col_foreign", {"totally_unrelated": 1}),
    _case("columnar_section", "col_dict_shaped", {
        "sections": {
            "Ki-1": {
                "group": "G",
                "lithology_blocks": {"b1": {"pattern": "x",
                                            "range_top_idx": "8.5",
                                            "range_base_idx": 3}},
                "samples": [{"bed_idx": 2.5, "fossil_marker": "a"}],
                "age_units": [{"label": "u", "range_top_idx": 1,
                               "range_base_idx": 5}],
            },
        },
    }),
    _case("columnar_section", "col_lossy_indices", {
        "sections": [{"id": "A", "lithology_blocks": [
            {"pattern": "p", "range_top_idx": "x", "range_base_idx": 3},
            {"pattern": "q", "range_top_idx": 9.7, "range_base_idx": "2.0"},
            {"pattern": "r", "range_top_idx": True, "range_base_idx": None},
        ]}],
    }),
    _case("columnar_section", "col_index_swap_units", {
        "sections": [{"id": "A", "age_units": [
            {"label": "u", "range_top_idx": 1, "range_base_idx": 6},
        ], "cross_beds": []}],
        "cross_beds": [{"from_section": "A", "from_bed_idx": 1.5,
                        "to_section": "B", "to_bed_idx": 2.5}],
    }),
    _case("columnar_section", "col_legend_string", {
        "fossil_legend": "none", "lithology_legend": "none"}),
    _case("columnar_section", "col_legend_dict", {
        "fossil_legend": {"ammonite": {"meaning": "M", "marker": "a"}},
        "lithology_legend": {"sand": {"meaning": "sandstone", "pattern": "p"}},
    }),
    _case("columnar_section", "col_array_root", {
        "_array_root": [
            {"id": "A", "group": "G"},
            {"meaning": "m", "marker": "k"},
            {"meaning": "m2", "pattern": "p"},
            {"from_section": "a", "to_section": "b"},
            {"zz": 1},
            "bare string",
        ],
        "confidence": 0.4,
    }),
    _case("columnar_section", "col_overall_null", {
        "sections": [{"id": "A"}], "overall_confidence": None, "confidence": 0.8}),
    _case("columnar_section", "col_confidence_string", {
        "sections": [{"id": "A", "confidence_by_section": "90%"}],
        "overall_confidence": "0.75"}),
)

# --- abundance_diagram -----------------------------------------------------
_add(
    _case("abundance_diagram", "ab_empty", {}),
    _case("abundance_diagram", "ab_foreign", {"totally_unrelated": 1}),
    _case("abundance_diagram", "ab_array_root_review_inputs", {
        "_array_root": [
            {"site_id": "S1", "location": "Loc1"},
            {"abundance": "A", "count": 5},
            {"zone": "Z1", "assemblage": "ass"},
            {"foo": 1},
        ],
        "confidence": 0.7,
        "_note": "wrap",
    }),
    _case("abundance_diagram", "ab_array_root_py_buckets", {
        "_array_root": [
            {"name": "S1", "location": "Loc1"},
            {"taxon": "Pinus", "level": "3"},
            {"abundance": "20%", "level": "3"},
            {"name": "Z1", "age": "Holocene"},
            {"name": "S2", "depth_unit": "m"},
            "bare string",
            {"foo": 1},
        ],
        "confidence": "0.5",
    }),
    _case("abundance_diagram", "ab_array_root_appends", {
        "sites": [{"name": "S0"}],
        "_array_root": [{"name": "S1", "location": "L"}],
    }),
    _case("abundance_diagram", "ab_dict_shaped", {
        "sites": {"Suigetsu": {"location": "L"}},
        "abundances": {"Pinus": {"level": "1", "abundance": "30%"}},
        "zones": {"Z1": {"age": "X"}},
    }),
    _case("abundance_diagram", "ab_string_rows", {
        "sites": ["Alpha"], "abundances": ["Pinus"], "zones": ["Z"]}),
    _case("abundance_diagram", "ab_confidence_percent", {
        "sites": [{"name": "S"}], "confidence": "90%"}),
    _case("abundance_diagram", "ab_confidence_bool", {
        "sites": [{"name": "S"}], "confidence": False}),
    _case("abundance_diagram", "ab_note_extras", {
        "sites": [{"name": "S", "unexpected": 1}],
        "abundances": [{"taxon": "T", "level": "1", "extra": 2}],
        "zones": [], "confidence": 0.1, "_note": "n"}),
)

# --- zonation_chart --------------------------------------------------------
_add(
    _case("zonation_chart", "zo_empty", {}),
    _case("zonation_chart", "zo_foreign", {"unrelated": 1}),
    _case("zonation_chart", "zo_array_root_mixed", {
        "_array_root": [
            {"from_zone": "a", "to_zone": "b"},
            {"name": "n", "rank": "Zone"},
            {"name": "z", "region": "r"},
            "str",
            {"x": 1},
        ],
        "confidence": "0.7",
        "_note": "n",
    }),
    _case("zonation_chart", "zo_dict_shaped", {
        "zones": {"Z1": {"rank": "Zone"}},
        "zonations": [{"name": "N"}],
        "correlations": ["c1"],
    }),
    _case("zonation_chart", "zo_string_rows", {
        "zonations": ["Global scale"], "zones": ["X Zone"]}),
    _case("zonation_chart", "zo_confidence_percent", {
        "zones": [{"name": "n", "rank": "Zone"}], "confidence": "90%"}),
    _case("zonation_chart", "zo_root_extras", {
        "zonations": [], "zones": [], "correlations": [], "confidence": 0,
        "extra_root": 1}),
)

# --- phylogenetic_tree -----------------------------------------------------
_add(
    _case("phylogenetic_tree", "ph_basic", {
        "root_ids": ["r"],
        "nodes": [
            {"id": "r", "parent": None, "name": "Root", "is_leaf": False},
            {"id": "a", "parent": "r", "name": "Taxon A", "is_leaf": True,
             "branch_length": 0.1, "support": 95},
            {"id": "b", "parent": "r", "name": "Taxon B", "is_leaf": True,
             "branch_length": 0.2},
        ],
        "metadata": {"title": "T", "rooted": True},
        "confidence": 0.9,
    }),
    _case("phylogenetic_tree", "ph_numeric_root_ids", {
        "root_ids": [1],
        "nodes": [{"id": "1", "parent": None, "name": "A", "is_leaf": True}],
        "confidence": 0.9,
    }),
    _case("phylogenetic_tree", "ph_missing_ids", {
        "root_ids": ["r"],
        "nodes": [
            {"id": "r", "parent": None, "name": "Root"},
            {"parent": "r", "name": "Anon one"},
            {"parent": "r", "name": "Anon two", "id": "  "},
            {"id": "_anon1", "parent": "r", "name": "Clash"},
        ],
    }),
    _case("phylogenetic_tree", "ph_dict_nodes", {
        "root_ids": ["r"],
        "nodes": {
            "r": {"parent": None, "is_leaf": False},
            "a": {"parent": "r", "name": "A", "is_leaf": True},
        },
    }),
    _case("phylogenetic_tree", "ph_array_root_wrap", {
        "_array_root": [{
            "root_ids": ["r"],
            "nodes": [{"id": "r", "parent": None, "name": "A"}],
        }],
        "confidence": 0.6,
        "_note": "wrap",
        "legend": {"a": 1},
        "metadata": {"title": "outer"},
    }),
    _case("phylogenetic_tree", "ph_rooted_strings", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "metadata": {"rooted": "false"},
    }),
    _case("phylogenetic_tree", "ph_rooted_weird", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "metadata": {"rooted": "yes", "source": None, "image_source": "img"},
    }),
    _case("phylogenetic_tree", "ph_is_leaf_corrected", {
        "root_ids": ["r"],
        "nodes": [
            {"id": "r", "parent": None, "name": "R", "is_leaf": True},
            {"id": "a", "parent": "r", "name": "A", "is_leaf": False},
        ],
    }),
    _case("phylogenetic_tree", "ph_extra_metadata", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "metadata": {"taxon_group": "Ammonoidea", "root_name": "R",
                     "total_nodes": 2, "version": "2", "title": "T"},
        "confidence": 0.3, "extra_root": {"a": 1}, "_note": "n",
    }),
    _case("phylogenetic_tree", "ph_node_extras", {
        "root_ids": ["r"],
        "nodes": [
            {"id": "r", "parent": None, "name": "R",
             "depth_range_m": 12, "support_confidence": 0.4},
            {"id": "a", "parent": "r", "name": "A", "support": "88"},
        ],
    }),
    _case("phylogenetic_tree", "ph_support_out_of_range", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A", "support": 400}],
    }),
    _case("phylogenetic_tree", "ph_empty_root_ids", {
        "root_ids": [], "nodes": [{"id": "r", "parent": None}],
    }),
    _case("phylogenetic_tree", "ph_foreign", {"unrelated": 1}),
    _case("phylogenetic_tree", "ph_confidence_percent", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "confidence": "90%",
    }),
    _case("phylogenetic_tree", "ph_non_dict_nodes", {
        "root_ids": ["r"],
        "nodes": ["r"],
    }),
)

# --- chart_classification --------------------------------------------------
_add(
    _case("chart_classification", "cc_percent", {
        "chart_type": "range_chart", "confidence": "90%", "reason": "r"}),
    _case("chart_classification", "cc_bool", {
        "chart_type": "PALEOMAP", "confidence": True}),
    _case("chart_classification", "cc_unknown_type", {
        "chart_type": "bogus", "confidence": 0.4}),
    _case("chart_classification", "cc_list", {
        "chart_type": "range_chart", "confidence": [3]}),
    _case("chart_classification", "cc_none", {
        "chart_type": None, "reason": None, "confidence": None}),
    _case("chart_classification", "cc_valid_range", {
        "chart_type": "zonation_chart", "confidence": 0.87}),
    _case("chart_classification", "cc_non_dict", "range_chart"),
    _case("chart_classification", "cc_empty", {}),
)

# --- to_newick (over the NORMALIZED tree) ----------------------------------
_NW_TREES = {
    "nw_plain": {
        "root_ids": ["r"],
        "nodes": [
            {"id": "r", "parent": None, "is_leaf": False},
            {"id": "a", "parent": "r", "name": "A", "is_leaf": True,
             "branch_length": 0.1},
            {"id": "b", "parent": "r", "name": "B", "is_leaf": True},
        ],
        "confidence": 1.0,
    },
    "nw_special_labels": {
        "root_ids": ["r"],
        "nodes": [
            {"id": "r", "parent": None, "is_leaf": False, "support": 95},
            {"id": "a", "parent": "r", "name": "A(B)", "is_leaf": True},
            {"id": "b", "parent": "r", "name": "C:D", "is_leaf": True,
             "branch_length": 0.25},
            {"id": "c", "parent": "r", "name": "it's", "is_leaf": True},
            {"id": "d", "parent": "r", "name": "spaced label",
             "is_leaf": True},
            {"id": "e", "parent": "r", "name": "[square]", "is_leaf": True},
            {"id": "f", "parent": "r", "name": "", "is_leaf": True},
            {"id": "g", "parent": "r", "name": "plain", "is_leaf": True},
        ],
        "confidence": 1.0,
    },
    "nw_forest": {
        "root_ids": ["r1", "r2"],
        "nodes": [
            {"id": "r1", "parent": None, "is_leaf": True, "name": "X"},
            {"id": "r2", "parent": None, "is_leaf": False, "name": "Y"},
            {"id": "c", "parent": "r2", "name": "Z", "is_leaf": True},
        ],
    },
}
for _cid, _payload in _NW_TREES.items():
    _add(_case("to_newick", _cid, _payload))

# --- safe_json_loads -------------------------------------------------------
_SCHEMA_FENCE = ("```json\n{\"properties\": {\"sections\": {\"type\": \"array\"}},"
                 " \"required\": [\"sections\"], \"format\": \"rca-v1\"}\n```")
_PAYLOAD = ("{\"sections\": [{\"id\": \"A\", \"group\": \"G\"}],"
            " \"overall_confidence\": 0.9}")
_add(
    _case("safe_json_loads", "sj_plain", "{\"sections\": [{\"name\": \"A\"}]}"),
    _case("safe_json_loads", "sj_single_fence", "Here:\n```json\n" + _PAYLOAD + "\n```\nThanks"),
    _case("safe_json_loads", "sj_two_fences", "Schema:\n" + _SCHEMA_FENCE +
          "\nResult:\n```json\n" + _PAYLOAD + "\n```"),
    _case("safe_json_loads", "sj_fence_then_prose_payload",
          "Contract:\n" + _SCHEMA_FENCE + "\nThe answer is " + _PAYLOAD + " done"),
    _case("safe_json_loads", "sj_prose_only_payload",
          "Sure! " + "{\"confidence\": 0.5, \"sections\": [{\"name\": \"A\"}]} + tail"),
    _case("safe_json_loads", "sj_placeholder_fence",
          "```json\n{\"sections\": [{\"name\": \"<section name>\","
          " \"age_range\": \"<one of the list above>\"}]}\n```\n"
          "```json\n{\"sections\": [{\"name\": \"Ki\",\"age_range\":\"J\"}]}\n```"),
    _case("safe_json_loads", "sj_no_qualifying_block",
          "```json\n{\"properties\": {\"a\": 1}}\n```\nand\n"
          "```json\n{\"required\": [\"x\"]}\n```"),
    _case("safe_json_loads", "sj_top_level_array", "[{\"species\": \"A\", \"section\": \"S\"}]"),
    _case("safe_json_loads", "sj_truncated_array",
          "[{\"species\": \"A\", \"section\": \"S\"}, {\"spe"),
    _case("safe_json_loads", "sj_truncated_object",
          "{\"sections\": [{\"name\": \"A\"}, {\"name\": \"B\"}, {\"na"),
    _case("safe_json_loads", "sj_truncated_scalar_array",
          "[\"a\", \"b\", \"c"),
    _case("safe_json_loads", "sj_wrapper_data",
          "{\"data\": {\"sections\": [{\"name\": \"A\"}]}, \"confidence\": 0.2}"),
    _case("safe_json_loads", "sj_control_in_string",
          "{\"sections\": [{\"name\": \"A\nB\"}], \"confidence\": 0.1}"),
    _case("safe_json_loads", "sj_nul_junk",
          "{\"sections\": [{\"name\": \"A\"}]}\x00\x07"),
    _case("safe_json_loads", "sj_nan_literal", "{\"confidence\": NaN}"),
    _case("safe_json_loads", "sj_empty", ""),
    _case("safe_json_loads", "sj_no_json", "I cannot read this figure."),
    _case("safe_json_loads", "sj_nested_candidates",
          "{\"metadata\": {\"title\": \"x\"}, \"sections\": [{\"name\": \"A\"}]}"),
    _case("safe_json_loads", "sj_zonation_fence",
          "```json\n{\"zonations\": [{\"name\": \"N\"}], \"correlations\": []}\n```"),
    _case("safe_json_loads", "sj_unclosed_fence",
          "```json\n{\"sections\": [{\"name\": \"A\"}]}"),
)

# --- age bounds (quality.js / ics_table.js vs standards/ics.py) ------------
_AGE_INPUTS = [
    ("Late Permian (Wuchiapingian)", "older"),
    ("Late Permian (Wuchiapingian)", "younger"),
    ("Wuchiapingian", "older"),
    ("Wuchiapingian", "younger"),
    ("Wuchiapingian - Changhsingian", "older"),
    ("Wuchiapingian - Changhsingian", "younger"),
    ("Changhsingian - Wuchiapingian", "younger"),
    ("Changhsingian - Wuchiapingian", "older"),
    ("259.51 Ma", "older"),
    ("254.14 Ma", "older"),
    ("251.902 Ma", "older"),
    ("505 Ma", "older"),
    ("Pleistocene", "older"),
    ("Pleistocene", "younger"),
    ("Late Pennsylvanian", "older"),
    ("Permian", "older"),
    ("Permian", "younger"),
    ("吴家坪阶", "older"),
    ("吴家坪阶", "younger"),
    ("晚二叠世", "older"),
    ("Older", "Older"),
    ("Wuchiapingian", " BASE "),
    ("Wuchiapingian", "youngest"),
    ("Wuchiapingian", None),
    ("Wuchiapingian", ""),
    ("260 Ma - 250 Ma", "older"),
    ("260 Ma - 250 Ma", "younger"),
    ("0 Ma", "younger"),
    ("something unresolvable", "older"),
    ("", "older"),
    ("Wuchiapingian", "middle"),
    ("Capitanian", "older"),
    (" Guadalupian ", "younger"),
]
for _i, (_text, _prefer) in enumerate(_AGE_INPUTS):
    _add(_case("age_bound", "ag_%02d" % (_i + 1), _text, _prefer))


# --- coverage contract: reason codes / response kinds / ledger ---------------
# BORROW-2026-09-20 (js-data-layer mirror round). Every case dispatches ONE
# public function of rca_core/reason_codes.py by name; js/reason-codes.js
# exposes the same functions under the same snake_case keys on its
# ``RCAReasonCodes`` namespace, so the replay side is one lookup table.
# Constraints the payloads respect (documented residuals, not gaps in the
# mirror): no ReasonCode instances (JSON cannot carry the dataclass type),
# and int-only — never float — in numeric label positions, because Python
# ``str(3.0)`` is "3.0" while JSON.parse collapses it to JS 3 -> "3".
# ``merge_response_kinds`` returns a tuple in Python; the wrapper below turns
# it into ``{"kind", "divergent"}`` so both engines answer in one JSON shape
# (same trick as ``_age_bound_python``).

def _rz(cid: str, op: str, args: list, kwargs: dict | None = None) -> dict:
    return _case("reason_codes", cid,
                 {"op": op, "args": args, "kwargs": kwargs or {}})


#: Shared by the grid / cross_product=False replays: 3 columns x 3 strata
#: (S1, S2 and "" for the section-less row) = 9 cells, one unattributed row,
#: one explicit-but-unattributed response, and two non-dict rows.
_LEDGER_ROWS = [
    {"species": "A", "section": "S1", "range_top": "9", "range_base": "7",
     "response_kind": "extracted", "reason_codes": ["crosses_top"]},
    {"species": "A", "section": "S2", "response_kind": "not_drawn",
     "reason_codes": ["not_drawn"]},
    {"species": "B", "section": "S1", "response_kind": "uncertain",
     "reason_codes": ["unclear", "obscured"]},
    {"species": "C"},
    "junk-row",
    {"section": "S1", "response_kind": "blank"},
    None,
]

_ROLLUP_ROWS = [
    {"species": "A", "response_kind": "not_drawn", "reason_codes": ["not_drawn"],
     "geometry": {"version": 1, "scale": "pos_0_999", "calibrated": True,
                  "points": {"range_base_pos_0_999": {"pos": 210,
                                                      "axis": "vertical",
                                                      "value": 7}}}},
    {"species": "B", "reason_code": "unclear"},
    {"species": "C", "response_kind": "nope"},
    {"species": "D"},
    5,
    {"response_kind": "extracted", "reason_codes": []},
]

_add(
    _rz("rzn_valid_strip", "is_valid_code", ["  NOT_DRAWN "]),
    _rz("rzn_valid_bool", "is_valid_code", [True]),
    _rz("rzn_norm_alias", "normalize_code", [" Undrawn"]),
    _rz("rzn_norm_unknown", "normalize_code", ["constructor"]),
    _rz("rzn_norm_proto", "normalize_code", ["toString"]),
    _rz("rzn_norm_int", "normalize_code", [7]),
    _rz("rzn_norm_dict", "normalize_code",
        [{"slug": "not_drawn", "summary": "x", "family": "coverage"}]),
    _rz("rzn_norm_caps", "normalize_code", ["Out_Of_Scope"]),
    _rz("rzn_codes_list", "normalize_codes",
        [["dash", "dash", "bogus", "unclear", None, 5]]),
    _rz("rzn_codes_string", "normalize_codes", [" not_drawn ; obscured , , bogus "]),
    _rz("rzn_codes_dict", "normalize_codes",
        [{"legend_only": True, "bogus": False, "abbr": 1}]),
    _rz("rzn_codes_scalar", "normalize_codes", [3]),
    _rz("rzn_codes_none", "normalize_codes", [None]),
    _rz("rzn_codes_empty", "normalize_codes", [""]),
    _rz("rzn_kind_alias", "normalize_response_kind", [" DRAWN "]),
    _rz("rzn_kind_unknown", "normalize_response_kind", ["silent"]),
    _rz("rzn_kind_nonstr", "normalize_response_kind", [42]),
    _rz("rzn_mk_diverge", "merge_response_kinds", [["extracted", "not_drawn"]]),
    _rz("rzn_mk_fold", "merge_response_kinds", [["dash", None, "", "undrawn"]]),
    _rz("rzn_mk_garbage", "merge_response_kinds", [[None, "bogus"]]),
    _rz("rzn_mk_empty", "merge_response_kinds", [[]]),
    _rz("rzn_mk_ranks", "merge_response_kinds",
        [["not_drawn", "uncertain", "extracted", "uncertain"]]),
    _rz("rzn_codes_union", "merge_reason_codes",
        [[["obscured"], "inferred", None, ["obscured"], {"dash": 1}]]),
    _rz("rzn_summary_known", "code_summary", ["Low_Confidence"]),
    _rz("rzn_summary_unknown", "code_summary", ["bogus"]),
    _rz("rzn_summary_empty", "code_summary", [""]),
    _rz("rzn_render", "render_codes", [["dash", "dash", "no_label"]]),
    _rz("rzn_has_blank", "has_value", [{"abundance": "  "}]),
    _rz("rzn_has_zero", "has_value", [{"value": 0}]),
    _rz("rzn_has_false", "has_value", [{"x": False}]),
    _rz("rzn_has_emptylist", "has_value", [{"values": [], "depth": {}}]),
    _rz("rzn_state_explicit", "coverage_state",
        [{"response_kind": "uncertain", "range_top": "3"}]),
    _rz("rzn_state_legacy", "coverage_state", [{"range_base_idx": 0}]),
    _rz("rzn_state_gap", "coverage_state", [{"species": "A"}]),
    _rz("rzn_state_nonrow", "coverage_state", ["nope"]),
    _rz("rzn_state_bogus_kind", "coverage_state",
        [{"response_kind": "bogus", "level_range": [1, 2]}]),
    _rz("rzn_answered_nd", "is_answered", [{"response_kind": "blank"}]),
    _rz("rzn_answered_gap", "is_answered", [{"species": "A"}]),
    _rz("rzn_column_trim", "row_column", [{"species": " Sp a ", "section": "S1"}]),
    _rz("rzn_column_none", "row_column", [{"section": "S1"}]),
    _rz("rzn_stratum_keys", "row_stratum",
        [{"level": 7, "site": ""}, ["zz", "level", "site"]]),
    _rz("rzn_label_fallback", "row_label", [{"section": "S2"}, ["missing"]]),
    _rz("rzn_label_default", "row_label", [{"species": "A", "section": "S"}]),
    _rz("rzn_ledger_grid", "coverage_ledger", [_LEDGER_ROWS]),
    _rz("rzn_ledger_nocross", "coverage_ledger", [_LEDGER_ROWS],
        {"cross_product": False}),
    _rz("rzn_ledger_dup", "coverage_ledger", [[
        {"species": "A", "section": "S1", "response_kind": "not_drawn"},
        {"species": "A", "section": "S1", "range_top": "9",
         "response_kind": "extracted"},
        {"species": "A", "section": "S1", "response_kind": "undrawn"},
        {"species": "B", "section": "S1", "response_kind": "nope", "depth": "12"},
    ]]),
    _rz("rzn_ledger_unattributed", "coverage_ledger", [[
        {"species": "", "response_kind": "extracted"},
        {"response_kind": "blank"},
        "junk",
        None,
    ]]),
    _rz("rzn_ledger_custom", "coverage_ledger", [[
        {"label": "Spirocolapthus", "bed": 3, "response_kind": "not_drawn",
         "reason_codes": ["dash"]},
        {"label": "S2", "bed": 3, "range_top": "5",
         "reason_codes": ["low_confidence"]},
        {"label": "S2", "bed": 9},
    ]], {"column_keys": ["label"], "stratum_keys": ["bed"]}),
    _rz("rzn_ledger_empty", "coverage_ledger", [None]),
    # 32-cell grid with exactly one answered cell: honest_coverage is
    # round(1/32, 4) = 0.03125 -> TIES TO EVEN -> 0.0312. Math.round would
    # say 0.0313; rcaPyRound must reproduce CPython.
    _rz("rzn_ledger_tie32", "coverage_ledger", [
        ([{"species": "T00", "section": "S0", "range_top": "1"}]
         + [{"species": "T01", "section": "S1"}]
         + [{"species": "T%02d" % _k, "section": "S0"} for _k in range(2, 16)]),
    ]),
    _rz("rzn_rollup_basic", "reason_code_rollup", [_ROLLUP_ROWS], {"limit": 2}),
    _rz("rzn_rollup_all", "reason_code_rollup", [_ROLLUP_ROWS]),
    _rz("rzn_rollup_zero", "reason_code_rollup", [_ROLLUP_ROWS], {"limit": 0}),
    _rz("rzn_rollup_empty", "reason_code_rollup", [[]]),
)


# --- contract-aware multi-run merge (js/aggregate.js) -------------------------
# Mode is ALWAYS explicit: the JS rcaMergeResults does not re-run
# _auto_detect_schema when a keymap is passed, and auto-detection parity is
# pre-existing territory outside this round. Phylogenetic mode stays out (the
# metadata/legend scan divergence in js/aggregate.js is documented elsewhere).

def _mrun(row: dict, **top: Any) -> dict:
    run = {"sections": [{"name": "S1"}], "species_ranges": [row],
           "biozones": [], "other_fossils": [], "confidence": 0.8}
    run.update(top)
    return run


def _mg(cid: str, runs: list, total: Any = 2, mode: str = "range_chart") -> dict:
    return _case("merge", cid,
                 {"results": runs, "total_runs": total, "mode": mode})


def _abrun(row: dict) -> dict:
    return {"abundances": [row], "sites": [], "zones": [], "confidence": 0.9}


_add(
    _mg("mrg_divergent_vote", [
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "range_base": "7", "response_kind": "extracted",
               "reason_codes": ["crosses_top"]}),
        _mrun({"species": "A", "section": "S1", "response_kind": "not_drawn",
               "reason_codes": ["not_drawn"]}),
    ]),
    _mg("mrg_geometry_first", [
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "geometry": {"version": 1, "scale": "pos_0_999",
                            "calibrated": True,
                            "points": {"range_top_pos_0_999": {
                                "pos": 347, "axis": "vertical",
                                "value": 9}}}}),
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "geometry": {"version": 1, "scale": "pos_0_999",
                            "calibrated": True,
                            "points": {"range_top_pos_0_999": {
                                "pos": 400, "axis": "vertical",
                                "value": 10.2}}}}),
    ]),
    # An empty ``points`` block is NOT well-formed (Python: ``v.get("points")``
    # truthiness) — the second run's block must win.
    _mg("mrg_geometry_skips_empty", [
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "geometry": {"version": 1, "scale": "pos_0_999", "points": {}}}),
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "geometry": {"version": 1, "scale": "pos_0_999",
                            "calibrated": False,
                            "points": {"range_base_pos_0_999": {
                                "pos": 88, "axis": "vertical",
                                "value": 7}}}}),
    ]),
    _mg("mrg_codes_union", [
        _mrun({"species": "A", "section": "S1", "response_kind": "extracted",
               "reason_codes": ["crosses_top", "bogus"]}),
        _mrun({"species": "A", "section": "S1", "response_kind": "extracted",
               "reason_codes": "unclear"}),
    ]),
    _mg("mrg_kind_all_garbage", [
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "response_kind": "nope"}),
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "response_kind": None}),
    ]),
    # votes keep the RAW ballot minus Python-falsy entries (None / ""),
    # even the ones normalize_response_kind later drops as unknown.
    _mg("mrg_votes_falsy_dropped", [
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "response_kind": None}),
        _mrun({"species": "A", "section": "S1", "response_kind": "dash"}),
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "response_kind": "extracted"}),
    ]),
    _mg("mrg_single_run_passthrough", [
        _mrun({"species": "A", "section": "S1", "response_kind": "not_drawn",
               "reason_codes": ["not_drawn"]}),
    ], total=None),
    _mg("mrg_single_run_total2", [
        _mrun({"species": "A", "section": "S1", "response_kind": "not_drawn",
               "reason_codes": ["not_drawn"]}),
    ], total=2),
    _mg("mrg_named_list_biozones", [
        dict(_mrun({"species": "A", "section": "S1"}),
             biozones=[{"name": "X Zone", "response_kind": "not_drawn",
                        "reason_codes": ["not_drawn"]}]),
        dict(_mrun({"species": "A", "section": "S1"}),
             biozones=[{"name": "X Zone", "response_kind": "extracted",
                        "reason_codes": ["legend_only"]}]),
    ]),
    _mg("mrg_abundance_contract", [
        _abrun({"taxon": "Pinus", "site": "S1", "level": "3",
                "abundance": "12", "response_kind": "extracted",
                "reason_codes": ["inferred"],
                "geometry": {"version": 1, "scale": "pos_0_999",
                             "calibrated": False,
                             "points": {"abundance_pos_0_999": {
                                 "pos": 120, "axis": "horizontal",
                                 "value": 12}}}}),
        _abrun({"taxon": "Pinus", "site": "S1", "level": "3",
                "response_kind": "not_drawn",
                "reason_codes": ["not_drawn"]}),
    ], mode="abundance_diagram"),
    _mg("mrg_legacy_no_contract", [
        _mrun({"species": "A", "section": "S1", "range_top": "9",
               "range_base": "7"}),
        _mrun({"species": "B", "section": "S1", "range_top": "8"}),
    ]),
)


# --- quality scoring + coverage ledger (js/quality.js) ------------------------
# Exercises score_range_chart end-to-end so BOTH the additive ``coverage``
# block and the ``quality.coverage_ledger`` info issue are replayed, plus the
# table-selection rules of coverage_for (largest contracted table, strict >
# so first-in-_COVERAGE_TABLES wins ties, per-mode column keys).

_add(
    _case("quality_coverage", "qc_notdrawn", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "response_kind": "not_drawn",
                            "reason_codes": ["not_drawn"]}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_legacy", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_codes_only", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9",
                            "reason_codes": ["obscured"]}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_grid4", {
        "sections": [{"name": "S1"}, {"name": "S2"}],
        "species_ranges": [
            {"species": "A", "section": "S1", "range_top": "9",
             "range_base": "7", "response_kind": "extracted"},
            {"species": "A", "section": "S2", "response_kind": "blank",
             "reason_codes": ["dash"]},
            {"species": "B", "section": "S1", "response_kind": "unclear"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_table_choice", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9", "response_kind": "extracted"}],
        "abundances": [
            {"taxon": "Pinus", "site": "S1", "level": "3", "abundance": "12",
             "response_kind": "extracted"},
            {"taxon": "Quercus", "site": "S1", "level": "3",
             "response_kind": "not_drawn"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_tie_first_wins", {
        "sections": [{"name": "S1"}],
        "species_ranges": [
            {"species": "A", "section": "S1", "response_kind": "extracted",
             "range_top": "9"},
            {"species": "B", "section": "S1", "response_kind": "not_drawn"},
        ],
        "points": [
            {"label": "P1", "x": 3, "y": 4, "response_kind": "extracted"},
            {"label": "P2", "response_kind": "uncertain"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_datapoints", {
        "sections": [{"name": "S1"}],
        "data_points": [
            {"sample_id": "DP1", "value": 3.2, "depth": 12,
             "response_kind": "extracted"},
            {"sample_id": "DP2", "response_kind": "not_drawn"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_empty", {}),
)


# ---------------------------------------------------------------------------
# Python side of the contract
# ---------------------------------------------------------------------------

def _age_bound_python(text: Any, prefer: Any) -> Any:
    name, ma = ics_resolve_age_bound(text, prefer)
    if name is None and ma is None:
        return None
    return {"name": name, "ma": ma}


def _reason_codes_python(payload: dict) -> Any:
    """Dispatch ONE public reason_codes function; the JS replay looks the same
    name up on the RCAReasonCodes namespace."""
    op = payload["op"]
    args = list(payload.get("args") or [])
    kwargs = dict(payload.get("kwargs") or {})
    # Whitelist: only the __all__ surface is reachable, never module internals.
    if op not in RC.__all__:
        raise KeyError(op)
    if op == "merge_response_kinds":
        kind, divergent = RC.merge_response_kinds(*args, **kwargs)
        return {"kind": kind, "divergent": divergent}
    return getattr(RC, op)(*args, **kwargs)


def _merge_python(payload: dict) -> Any:
    """merge_results with an EXPLICIT schema (see the group's case header)."""
    mode = payload.get("mode") or "range_chart"
    return merge_results(payload.get("results"),
                         total_runs=payload.get("total_runs"),
                         schema=SCHEMA_BY_MODE[mode])


_PY_RUNNERS = {
    "range_chart": normalize_result,
    "columnar_section": normalize_columnar_result,
    "abundance_diagram": normalize_abundance_result,
    "zonation_chart": normalize_zonation_chart_result,
    "phylogenetic_tree": _normalize_phylogenetic_tree_into,
    "chart_classification": normalize_chart_classification,
    "to_newick": to_newick,
    "safe_json_loads": safe_json_loads,
}


def compute_python_case(case: dict) -> Any:
    group = case["group"]
    payload = case["payload"]
    if group == "age_bound":
        return _age_bound_python(payload, case["extra"])
    if group == "safe_json_loads":
        return safe_json_loads(payload)
    if group == "reason_codes":
        return _reason_codes_python(copy.deepcopy(payload))
    if group == "merge":
        return _merge_python(copy.deepcopy(payload))
    if group == "quality_coverage":
        return score_range_chart(copy.deepcopy(payload))
    payload = copy.deepcopy(payload)  # the normalizers mutate in place
    fn = _PY_RUNNERS[group]
    if group == "to_newick":
        # Normalize FIRST: the UI feeds a NORMALIZED tree to to_newick, and the
        # JS mirror under test (rcaToNewick) is only compared on that pipeline.
        return to_newick(_normalize_phylogenetic_tree_into(payload))
    return fn(payload)


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON so tuples/lists and floats are canonical."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def build_fixture() -> dict:
    out = {"generated_by": "tests/gen_frontend_parity_fixtures.py", "cases": []}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for case in CASES:
            entry = dict(case)
            try:
                result = compute_python_case(case)
            except Exception as exc:  # the JS mirror must raise too
                entry["python_error"] = type(exc).__name__
                entry["python_message"] = str(exc)
            else:
                entry["python"] = _jsonable(result)
            out["cases"].append(entry)
    return out


def main() -> int:
    fixture = build_fixture()
    # NOT sort_keys=True: the payload's KEY ORDER is semantic for the
    # dict-shaped-array repairs (`{"sections": {"Ki-1": {...}}}` yields rows in
    # document order), and Python computed `python` from the literal's own
    # order. Sorting the file would store a payload whose re-evaluation gives a
    # different row order than the recorded expectation, so the pytest drift
    # guard (tests/test_frontend_parity_fixtures_2026_09_20.py) would fail for
    # reasons that have nothing to do with the code under review.
    FIXTURE_PATH.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print("wrote %s (%d cases)" % (FIXTURE_PATH, len(fixture["cases"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
