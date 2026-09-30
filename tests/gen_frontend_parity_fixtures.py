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
from rca_core.editable import apply_edits, capture_edits  # noqa: E402
from rca_core.aggregate import SCHEMA_BY_MODE, merge_results  # noqa: E402
from rca_core.quality import score_range_chart  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "frontend_parity_2026_09_20.json"

GROUPS = (
    "range_chart", "columnar_section", "abundance_diagram",
    "zonation_chart", "phylogenetic_tree", "chart_classification",
    "to_newick", "safe_json_loads", "age_bound",
    "reason_codes", "merge", "quality_coverage", "editable",
)


def _case(group: str, cid: str, payload: Any, extra: Any = None) -> dict:
    return {"group": group, "id": cid, "payload": payload, "extra": extra}


# --- the table editor's edit payload: rca_core/editable.py vs js/table.js ----
# AUDIT-2026-10-01. The editor is how a researcher corrects a model mistake, and
# the payload it produces is what "Save edits" / "Apply" sends, so the two
# engines have to agree on the WHOLE thing: not only the diff, but the round
# trip, because a payload that looks right and replays to the wrong table is
# worse than one that is visibly wrong.
#
# Reading the two implementations side by side suggests a divergence that
# measurement does not support: capture_edits aligns rows by IDENTITY first
# (rca_core/editable.py:_align_rows, "so an insertion or a re-sort cannot slide
# one taxon's edit onto its neighbour") while rcaCaptureListEdits pairs purely by
# position. Running the branches settles it -- 25 cases including mid-table
# insert, head insert, re-sort with an edit, scalar-list insert/change/delete,
# dict<->scalar row changes, both deletion positions, cleared cells, added
# fields, numeric-vs-string, unicode and _extras all agree, diff AND replay.
# Recorded here because "the two implementations look different" is exactly the
# kind of claim that should not be repeated, and because the group was missing:
# the editor had no cross-engine guard at all.
def _ed_replay(before: Any, edits: Any) -> Any:
    """apply_edits(before, edits) restricted to the keys the edits mention."""
    if not isinstance(edits, dict) or not edits:
        return None
    try:
        after = apply_edits(copy.deepcopy(before), copy.deepcopy(edits))
    except Exception as exc:  # noqa: BLE001 - a raise is a finding
        return "__raised__ %s" % type(exc).__name__
    # A key the replay did not create is None, which is what the JS side answers
    # for an absent key too; comparing raw would report one spurious difference.
    return {k: (after.get(k) if isinstance(after, dict) else None) for k in edits}


def _editable_python(payload: dict) -> Any:
    args = payload.get("args") or []
    if payload.get("op") == "capture_all":
        edits = capture_edits(copy.deepcopy(args[0]), copy.deepcopy(args[1]))
        return {"edits": edits, "replay": _ed_replay(args[0], edits)}
    raise KeyError(payload.get("op"))


def _ed_row(name: str, **kw: Any) -> dict:
    d = {"species": name, "section": "S1"}
    d.update(kw)
    return d


def _ed_res(*rows: Any, **kw: Any) -> dict:
    d = {"sections": [{"name": "S1"}], "species_ranges": list(rows),
         "biozones": [], "other_fossils": []}
    d.update(kw)
    return d


_ED_R3 = [_ed_row("A", range_top="9"), _ed_row("B", range_top="8"),
          _ed_row("C", range_top="7")]
_ED_PAIRS = [
    # cell-level edits
    ("cell_change", _ed_res(*_ED_R3),
     _ed_res(_ed_row("A", range_top="1"), _ED_R3[1], _ED_R3[2])),
    ("cell_clear", _ed_res(*_ED_R3),
     _ed_res(_ed_row("A"), _ED_R3[1], _ED_R3[2])),
    ("cell_add_field", _ed_res(*_ED_R3),
     _ed_res(_ed_row("A", range_top="9", range_base="2"), _ED_R3[1], _ED_R3[2])),
    ("cell_numeric_vs_string", _ed_res(*_ED_R3),
     _ed_res(_ed_row("A", range_top=9), _ED_R3[1], _ED_R3[2])),
    ("cell_unicode", _ed_res(*_ED_R3),
     _ed_res(_ed_row("A", range_top="中华虫"), _ED_R3[1], _ED_R3[2])),
    ("cell_extras_ignored", _ed_res(*_ED_R3),
     _ed_res(_ed_row("A", range_top="9", _extras={"x": 1}), _ED_R3[1], _ED_R3[2])),
    # insertions: the branches the identity alignment exists for
    ("append_trailing", _ed_res(*_ED_R3),
     _ed_res(_ED_R3[0], _ED_R3[1], _ED_R3[2], _ed_row("D", range_top="6"))),
    ("insert_middle", _ed_res(*_ED_R3),
     _ed_res(_ED_R3[0], _ed_row("D", range_top="6"), _ED_R3[1], _ED_R3[2])),
    ("insert_head", _ed_res(*_ED_R3),
     _ed_res(_ed_row("D", range_top="6"), _ED_R3[0], _ED_R3[1], _ED_R3[2])),
    # deletions: both positions, and the shrink -> _replaced path
    ("delete_tail", _ed_res(*_ED_R3), _ed_res(_ED_R3[0], _ED_R3[1])),
    ("delete_middle", _ed_res(*_ED_R3), _ed_res(_ED_R3[0], _ED_R3[2])),
    # re-sort: same rows, different order, and with an edit riding on it
    ("resort_only", _ed_res(*_ED_R3), _ed_res(_ED_R3[2], _ED_R3[1], _ED_R3[0])),
    ("resort_with_edit", _ed_res(*_ED_R3),
     _ed_res(_ed_row("C", range_top="99"), _ED_R3[1], _ED_R3[0])),
    # scalar lists (other_fossils)
    ("scalar_append", _ed_res(*_ED_R3, other_fossils=["F1"]),
     _ed_res(*_ED_R3, other_fossils=["F1", "F2"])),
    ("scalar_change", _ed_res(*_ED_R3, other_fossils=["F1", "F2"]),
     _ed_res(*_ED_R3, other_fossils=["F1", "F9"])),
    ("scalar_delete", _ed_res(*_ED_R3, other_fossils=["F1", "F2"]),
     _ed_res(*_ED_R3, other_fossils=["F1"])),
    # row-type changes
    ("dict_to_scalar", _ed_res(*_ED_R3, other_fossils=[{"name": "A"}]),
     _ed_res(*_ED_R3, other_fossils=["A"])),
    ("scalar_to_dict", _ed_res(*_ED_R3, other_fossils=["A"]),
     _ed_res(*_ED_R3, other_fossils=[{"name": "A"}])),
    # no change and degenerate inputs
    ("identical", _ed_res(*_ED_R3), _ed_res(*_ED_R3)),
    ("empty_rows", _ed_res(), _ed_res()),
    ("before_not_dict", "nope", _ed_res()),
    ("after_not_dict", _ed_res(), 5),
    # rows with no usable identity: the positional fallback
    ("no_identity_rows", _ed_res({"range_top": "1"}, {"range_top": "2"}),
     _ed_res({"range_top": "9"}, {"range_top": "2"})),
    # other list keys
    ("sections_edit", _ed_res(*_ED_R3),
     _ed_res(*_ED_R3, sections=[{"name": "S1", "age_range": "Permian"}])),
    ("cross_beds_new", _ed_res(*_ED_R3),
     _ed_res(*_ED_R3, cross_beds=[{"from": "S1", "to": "S2"}])),
]
# (the _add(*[...]) call for these lives just below, after _add is defined)


CASES: list[dict] = []


def _add(*cases: dict) -> None:
    CASES.extend(cases)


_add(*[
    _case("editable", f"ed_{cid}", {"op": "capture_all", "args": [before, after]})
    for cid, before, after in _ED_PAIRS
])


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
    # AUDIT-2026-09-27: the literal "NaN" as a confidence. Both engines accept
    # it as a float -- Python's float("NaN") and JS's Number("NaN") both give
    # NaN -- and then they clamp it with the SAME INTENT and the OPPOSITE
    # result, because the languages disagree: Python's min(1.0, nan) returns
    # 1.0 (it does not propagate NaN), while JS's Math.min(1, NaN) is NaN and
    # stays NaN until the falsy fallback turns it into 0. So one model's
    # `"confidence": "NaN"` becomes a PERFECT 1.0 in the desktop app and a 0 in
    # the browser. Found by difffuzz_normalize.py. Every other unparseable
    # value ("bogus", "[]") agrees, so this is the NaN path alone -- which is
    # why no hand-written confidence fixture ever hit it.
    _case("range_chart", "rc_root_confidence_nan", {
        "sections": [], "confidence": "NaN",
    }),
    _case("range_chart", "rc_row_confidence_nan", {
        "sections": [], "confidence": 1,
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9", "confidence": "NaN"}],
    }),
)

# --- columnar_section ------------------------------------------------------
_add(
    _case("columnar_section", "col_empty", {}),
    # AUDIT-2026-09-27: a container the model put in an UNRECOGNISED field.
    # Python's _stringify_scalar renders it as text ("(1, 2)"); the browser's
    # _extras carry-over keeps the real array. Neither loses the information,
    # and the repo's stated policy is that the browser is "lossless-or-empty,
    # never a fabricated Python repr" (rcaStringifyScalar's docstring), so
    # teaching JS to emit "(1, 2)" is the one option that is definitely wrong.
    # Tracked rather than fixed. Found by difffuzz_normalize.py.
    _case("columnar_section", "col_extras_container", {
        "sections": [{"id": "Ki-1", "group": "G", "lithology": [1, 2]}],
        "confidence": 1,
    }),
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
    # AUDIT-2026-09-30: `metadata` and `legend` are the two OPTIONAL mappings
    # on a tree, and a model can put a string in either. `raw.get("metadata") or
    # {}` only rejects FALSY non-mappings, so a truthy string reached
    # `metadata_raw.get("title", "")` and raised AttributeError on the Python
    # side, while js/minimax.js's `rcaPyOr` let it through to `Object.keys`,
    # which yielded ["0","1","2"] and emitted {"0": "s", "1": "t", "2": "r"} --
    # a string's character positions treated as metadata keys. Found by
    # difffuzz_normalize.py, whose phylogenetic_tree generator had been raising
    # on 400/400 cases and scoring that as agreement. Both sides now treat a
    # non-mapping as absent, matching the `legend` line that was already
    # guarded. `ph_non_dict_legend` pins that sibling so the two cannot drift
    # apart again.
    _case("phylogenetic_tree", "ph_non_dict_metadata", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "metadata": "str",
        "confidence": 0.9,
    }),
    _case("phylogenetic_tree", "ph_non_dict_legend", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "legend": ["a", "b"],
    }),
    _case("phylogenetic_tree", "ph_metadata_list", {
        "root_ids": ["r"],
        "nodes": [{"id": "r", "parent": None, "name": "A"}],
        "metadata": [{"title": "T"}],
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
    # AUDIT-2026-09-30: the same NaN-clamp split rc_root_confidence_nan
    # records, in a mode that had no case for it. The root/row NaN fixtures
    # were both added under range_chart, so nothing exercised the identical
    # `confidence` field on the classification path -- and measured here it
    # diverges the same way (Python min()/max() do not propagate NaN, so
    # "NaN" clamps to 1.0; JS Math.min/max do, so it becomes 0). Found by
    # driving rcaNormalizeChartClassification directly, a function
    # difffuzz_normalize never called because its MODES table lists only the
    # five modes whose normaliser exists on BOTH engines.
    _case("chart_classification", "cc_root_confidence_nan", {
        "chart_type": "range_chart", "confidence": "NaN", "reason": "r"}),
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
    # AUDIT-2026-09-27: a leading UTF-8 BOM. JS trim() removes U+FEFF (it is in
    # the ECMAScript WhiteSpace production as the historical ZWNBSP) and
    # Python's str.strip() does not, so a BOM-prefixed reply parsed in the
    # browser and failed on the desktop/backend. Found by difffuzz_json.py.
    _case("safe_json_loads", "sj_bom_object", "\ufeff{\"sections\": [{\"name\": \"A\"}]}"),
    _case("safe_json_loads", "sj_bom_array",
          "\ufeff[{\"species\": \"A\", \"section\": \"S\"}]"),
    _case("safe_json_loads", "sj_bom_with_prose",
          "\ufeffSure! Here it is:\n{\"sections\": [{\"name\": \"A\"}]}\nDone."),
    # AUDIT-2026-10-01: the two parsers' NESTING limits were measured on a
    # depth ladder (the numbers are in the expressibility guard's comment
    # further down) and there is deliberately NO fixture case for them. The
    # array form is the half that could have been expressed -- Python refuses
    # it, so there is no value to serialise -- and adding it anyway turned out
    # to be wrong for a reason worth recording: WHERE Python refuses is
    # interpreter-dependent, so the case is not stable across the versions this
    # project tests. Measured:
    #   CPython 3.10 (sys.getrecursionlimit() == 1000): a 1200-level array
    #     raises, so the case records python_error.
    #   CPython 3.12.14 (the CI interpreter): the same 1200-level array PARSES,
    #     so the case would record a 1200-deep value -- which the guard below
    #     then refused, and tests/test_frontend_parity_fixtures_2026_09_20.py::
    #     test_fixture_matches_python failed on 3.12 while passing on 3.10.
    # A committed fixture has to be byte-identical whichever interpreter
    # regenerates it, so a case whose Python answer moves with the version
    # cannot live here. The finding is recorded where it stays true: as the
    # measurement in the guard's comment, and as a guard on the guard
    # (tests/test_parity_fixture_expressibility.py).
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
    # AUDIT-2026-09-27: non-ASCII decimal digits. Python's `\d` and `float()`
    # accept every Unicode decimal digit; ECMAScript's `\d` is `[0-9]` and
    # nothing else. So the desktop/backend path resolves "26<U+0660> Ma" to an
    # absolute age and the browser path calls it unresolvable — the same label
    # produces different DwC/PBDB ages depending on which engine read the
    # figure. Full-width digits are written with \u escapes so this file stays
    # pure ASCII. Parked in EXPECTED_DIVERGENCES in tests_diff_frontend_parity.js
    # until someone decides which way the contract should go; it is a behaviour
    # change on BOTH engines, not a bug fix.
    ("26٠ Ma", "older"),
    ("２６０ Ma", "older"),
    # AUDIT-2026-10-01: the \b half of the same Unicode-awareness family, and
    # it fails in the OPPOSITE direction, so the two \d cases above could never
    # have found it. Python's \b on a str pattern is Unicode-aware (\w is
    # str.isalnum() plus "_"), so "Hirnantian" + an Arabic-Indic digit has no
    # word boundary and rca_core returns (None, None); ECMAScript's \w is
    # ASCII-only, so js/quality.js saw a boundary, matched, and assigned
    # Hirnantian 445.2 Ma. The browser was therefore assigning a FAD/LAD age
    # the desktop refused to assign, from a label the model can emit.
    # js/quality.js now builds these patterns through _wordBoundaryRe, whose
    # lookarounds cover the same classes Python's \b uses.
    # The two ASCII-digit controls are here because a fix that simply refused
    # any label containing a digit would satisfy the cases above.
    ("Hirnantian٣", "older"),
    ("Hirnantian٣", "younger"),
    ("Wuchiapingian٣", "older"),
    ("Induan۳", "older"),
    ("Hirnantian 3", "older"),
    ("Hirnantian3", "older"),
    ("Hirnantian", "older"),
    # AUDIT-2026-10-01: `prefer` as a single-element ARRAY. Python stringifies
    # with str() and JS with String(), and they disagree where it hurts:
    # str(["younger"]) is "['younger']" (unknown -> ValueError) while
    # String(["younger"]) is "younger" (known -> the browser silently resolved
    # the YOUNGER end), and [] / [""] defaulted to older instead of raising.
    # That is the quiet FAD/LAD inversion ics.py's own comment says an unknown
    # value must not cause. Every production caller passes a literal today
    # (pbdb / darwin_core / exporter / quality, and 'older' / 'younger' in
    # js/quality.js), so this is a latent hole; these cases keep it from
    # reopening. The Python side raises for all of them, so they are recorded
    # as python_error and the harness requires the mirror to raise too.
    ("Wuchiapingian", ["younger"]),
    ("Wuchiapingian", ["older"]),
    ("Wuchiapingian", []),
    ("Wuchiapingian", [""]),
    ("Wuchiapingian", ["older", "younger"]),
    ("Wuchiapingian", "younger"),
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
    # AUDIT-2026-09-27 [P2]: the ballots of a recombined row are ORDERED
    # (votes desc, then the tuple). The tie-break compares the VALUES, and a
    # value that is a PREFIX of another is the case that separates a tuple
    # comparison from a string comparison of its encoding: the JSON separator
    # ',' (0x2C) sorts after the space (0x20) inside a value, so "bed 1 (rp13)"
    # used to compare LESS than "bed 1" and the two engines listed the same
    # disagreement in opposite order. No prior case had two ballot values in a
    # prefix relationship, which is why this survived.
    _mg("mrg_ballot_prefix_order", [
        _mrun({"species": "A", "section": "S1", "range_base": "bed 1",
               "range_top": "bed 9", "biozone": "zone c"}),
        _mrun({"species": "A", "section": "S1", "range_base": "bed 1 (rp13)",
               "range_top": "bed 9", "biozone": "zone b"}),
    ]),
    # ...and one where a 2-vote reading must outrank two 1-vote readings, so
    # the primary sort key is covered and not just the tie-break.
    _mg("mrg_ballot_majority_first", [
        _mrun({"species": "A", "section": "S1", "range_base": "bed 9",
               "range_top": "bed 9", "biozone": "zone b"}),
        _mrun({"species": "A", "section": "S1", "range_base": "bed 9",
               "range_top": "bed 9", "biozone": "zone b"}),
        _mrun({"species": "A", "section": "S1", "range_base": "bed 3",
               "range_top": "bed 8", "biozone": "zone a"}),
        _mrun({"species": "A", "section": "S1", "range_base": "bed 4",
               "range_top": "bed 8", "biozone": "zone a"}),
    ], total=4),
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
    # AUDIT-2026-09-30: the merge group had 13 cases -- twelve range_chart and
    # one abundance_diagram -- so rca_core/aggregate.py's mode dispatch
    # (schema.primary_list_key) was never replayed for the other three
    # schemas, on either engine. Both sides already carry all five keymaps
    # (SCHEMA_BY_MODE / RCA_KEYMAP_BY_MODE); only the CASES were missing, which
    # is why every structural check on the harness passed. difffuzz_aggregate.py
    # does drive all five modes at 400 cases each, but it is deliberately not
    # in CI, so this is the CI-visible coverage.
    #
    # Each case uses the divergent-vote shape (one run extracted, one
    # not_drawn) because that is the path through the mode-specific row
    # grouping, not the single-run passthrough.
    _mg("mrg_columnar_contract", [
        {"sections": [{"id": "s1", "group": "g1",
                       "lithology_blocks": [{"name": "sand", "top_depth_m": "10"}],
                       "age_units": [{"name": "U1", "top_depth_m": "5"}],
                       "response_kind": "extracted",
                       "reason_codes": ["inferred"]}],
         "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
         "confidence": 0.9},
        {"sections": [{"id": "s1", "group": "g1",
                       "lithology_blocks": [{"name": "sand", "top_depth_m": "10"}],
                       "age_units": [{"name": "U1", "top_depth_m": "5"}],
                       "response_kind": "not_drawn",
                       "reason_codes": ["not_drawn"]}],
         "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
         "confidence": 0.9},
    ], mode="columnar_section"),
    _mg("mrg_zonation_contract", [
        {"zones": [{"name": "Z1", "age": "290-280 Ma", "level_range": "1-2",
                    "response_kind": "extracted",
                    "reason_codes": ["inferred"]}],
         "zonations": [{"name": "bed 7"}],
         "correlations": [{"from_zone": "Z1", "to_zone": "Z2"}],
         "confidence": 0.8},
        {"zones": [{"name": "Z1", "age": "290-280 Ma", "level_range": "1-2",
                    "response_kind": "not_drawn",
                    "reason_codes": ["not_drawn"]}],
         "zonations": [{"name": "bed 7"}],
         "correlations": [{"from_zone": "Z1", "to_zone": "Z2"}],
         "confidence": 0.8},
    ], mode="zonation_chart"),
    _mg("mrg_phylo_contract", [
        {"nodes": [{"id": "n1", "parent": None, "name": "root",
                    "response_kind": "extracted"}],
         "root_ids": ["n1"], "metadata": {}, "legend": {}, "confidence": 0.7},
        {"nodes": [{"id": "n1", "parent": None, "name": "root",
                    "response_kind": "not_drawn",
                    "reason_codes": ["not_drawn"]}],
         "root_ids": ["n1"], "metadata": {}, "legend": {}, "confidence": 0.7},
    ], mode="phylogenetic_tree"),
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
    # AUDIT-2026-10-01 [item 26]: a scatter table whose points carry NO `label`
    # (the table's own primary key) but DO carry `group` -- the taxon.  Before
    # the ladder learned `group` this produced an empty grid (cells 0,
    # coverage 0.0) for rows that all answered; both engines must agree on the
    # 2 columns / 3 cells this now yields.
    _case("quality_coverage", "qc_group_column", {
        "sections": [{"name": "S1"}],
        "points": [
            {"group": "Bathylagus sp.", "x": 1, "y": 2, "label": "",
             "response_kind": "extracted"},
            {"group": "Bathylagus sp.", "x": 3, "y": 4, "label": "",
             "response_kind": "uncertain"},
            {"group": "E. antarctica", "x": 5, "y": 6, "label": "",
             "response_kind": "extracted"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_empty", {}),
    # AUDIT-2026-09-30: NO quality_coverage payload carried abundance_unit "%"
    # before this one, so the sum-to-100 check (P1-8) -- a named scientific
    # rule with its own weight that emits a warning the operator reads -- had
    # ZERO differential coverage while the group itself looked fully populated.
    # The sums here are ties at the first decimal on purpose: the browser
    # formatted them with Math.round(v.sum * 10) / 10, which rounds halves away
    # from zero, while rca_core formats with round(total, 1), which rounds them
    # to even. At one decimal those ties are common rather than exotic --
    # measured 8 of 16 tie-shaped sums disagreed -- so a single non-tie case
    # would have hidden it.
    _case("quality_coverage", "qc_abundance_sum_violation", {
        "sections": [{"name": "S1", "response_kind": "extracted"}],
        "abundances": [
            {"taxon": "A", "site": "S1", "level": "L1", "abundance": "1.25",
             "abundance_unit": "%", "response_kind": "extracted"},
            {"taxon": "B", "site": "S1", "level": "L1", "abundance": "1.0",
             "abundance_unit": "%", "response_kind": "extracted"},
            {"taxon": "C", "site": "S1", "level": "L2", "abundance": "100.25",
             "abundance_unit": "%", "response_kind": "extracted"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    # AUDIT-2026-09-30: every case above resolved to range_chart. The group
    # existed, its runner existed, and it had eight cases -- so every structural
    # consistency check on the harness (GROUPS == RUNNERS == groups-with-cases)
    # passed, while the group was missing two modes outright. That blind spot
    # is where the real bug lived: js/quality.js's scoreStructure had no
    # 'zonation' branch, so a zonation result was null-checked against
    # range-chart keys, lost 0.5 on the structure dimension, and the browser
    # graded a real published figure 0.89/B where rca_core said 0.94/A.
    #
    # The lesson is that "the group exists" is not "the group covers the
    # modes", so these two cases exist to make the mode coverage explicit.
    _case("quality_coverage", "qc_zonation", {
        "zones": [{"name": "Z1", "age": "290-280 Ma", "level_range": "1-2",
                   "response_kind": "extracted"}],
        "correlations": [{"from_zone": "Z1", "to_zone": "Z2",
                          "response_kind": "extracted"}],
        "zonations": [{"name": "bed 7", "response_kind": "extracted"}],
        "confidence": 0.8}),
    _case("quality_coverage", "qc_columnar", {
        "sections": [{"name": "S1", "age_range": "300-290 Ma",
                      "response_kind": "extracted"}],
        "cross_beds": [{"from": "S1", "to": "S2",
                        "response_kind": "extracted"}],
        "fossil_legend": [{"label": "A", "response_kind": "extracted"}],
        "confidence": 0.8}),
    # AUDIT-2026-10-01: SUB-BED RANGES had no differential coverage at all.
    # rca_core/quality.py::_subbed_inverted treats a subscript letter as
    # ascending and an EMPTY subscript as sorting below every letter, so
    # base="9a" / top="9" is an inverted range even though _parse_bed_n reads
    # both as the integer 9. js/quality.js had no sub-bed branch anywhere, so
    # the browser scored those rows 0.87/B with no warning while rca_core
    # scored them 0.65/C -- and, unlike rca_core, the browser's exporter has
    # no range_base_le_range_top validation either, so the impossible range
    # reached the output file. The three "valid" cases are here on purpose:
    # they are what a fix that simply flagged every subscripted pair would
    # break, so they keep the rule one-directional.
    _case("quality_coverage", "qc_subbed_inverted_bare_over_letter", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9", "range_base": "9a",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_subbed_inverted_letters", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9a", "range_base": "9b",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_subbed_inverted_uppercase", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9", "range_base": "9A",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_subbed_inverted_high_number", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "23", "range_base": "23a",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_subbed_valid_letters_ascending", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "9b", "range_base": "9a",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_subbed_valid_letter_over_bare", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "23a", "range_base": "23",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    _case("quality_coverage", "qc_subbed_valid_different_beds", {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_top": "10", "range_base": "9a",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    # AUDIT-2026-10-01: the {detail} placeholder of quality.stage_order_reversed.
    # The template is "Stage order reversed in section {section}: {detail}", and
    # rca_core/quality.py passed _score_cross_era_accuracy's violation text --
    # which already begins "Stage order reversed: " -- as {detail}, so the
    # rendered badge said the phrase twice, and said it in ENGLISH inside the
    # zh and ja sentences. No quality_coverage payload carried a multi-stage
    # age_range before this one, so the branch had no differential coverage.
    _case("quality_coverage", "qc_stage_order_detail_placeholder", {
        "sections": [{"name": "S1", "age_range": "Hirnantian - Sandbian",
                      "response_kind": "extracted"}],
        "species_ranges": [{"species": "A", "section": "S1", "range_top": "9",
                            "response_kind": "extracted"}],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    # AUDIT-2026-10-01: a sum that is a WHOLE number. rca_core renders
    # str(round(total, 1)), which always keeps one decimal ("3.0"); the browser
    # rendered String(3) == "3". The existing qc_abundance_sum_violation cases
    # are all tie-shaped decimals, so none of them could see a difference that
    # only exists when the rounded value has no fractional part.
    _case("quality_coverage", "qc_abundance_sum_whole", {
        "sections": [{"name": "S1"}],
        "abundances": [
            {"taxon": "A", "site": "S1", "level": "L1", "abundance": 1,
             "abundance_unit": "%", "response_kind": "extracted"},
            {"taxon": "A", "site": "S1", "level": "L1", "abundance": 1,
             "abundance_unit": "%", "response_kind": "extracted"},
            {"taxon": "A", "site": "S1", "level": "L1", "abundance": 1,
             "abundance_unit": "%", "response_kind": "extracted"},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8}),
    # AUDIT-2026-10-01: float repr in a string field, in three parts. The
    # mirror's note (js/minimax.js) recorded this as a known divergence and
    # gave the wrong reason -- "the aggregate layer re-normalizes both
    # spellings", which only covers MERGING, not the exported cell. Measuring
    # the PARSER instead of the mirror split it by a line I had guessed wrong:
    # the discriminator is not "does this value need exponent form", it is
    # "is this value INTEGRAL".
    #   * NON-INTEGRAL floats keep the information, so the browser can spell
    #     them the Python way: str(1e-7) is '1e-07' where String(1e-7) is
    #     '1e-7'. Fixable, and the export path already has the grammar
    #     (js/table.js#rcaPyFloatStr, verified against Python str() on 16
    #     values including 1e+16 / 1e-05 / 9.99e-05 / 5e-324 / -0.0). Fixed.
    #   * INTEGRAL values do NOT, and 1e16 is the case that proves it. The
    #     browser sees the number 10000000000000000 either way, but Python
    #     answers '1e+16' for a model that wrote 1e16 and
    #     '10000000000000000' for one that wrote the digits -- so the gap is
    #     in the PARSER, not in the number. rc_float_exponent_big and
    #     rc_float_integral are both pinned in the harness's
    #     EXPECTED_DIVERGENCES with that reason, next to rc_dict_shaped_sections
    #     and ag_34 / ag_35, which are the same class of runtime fact.
    # rc_float_int is the control: an integer payload must keep the integer
    # spelling on both sides, or the fix has broken the common case.
    _case("range_chart", "rc_float_exponent_big", {
        "species_ranges": [{"species": "A", "range_top": 1e16}]}),
    _case("range_chart", "rc_float_exponent_small", {
        "species_ranges": [{"species": "A", "range_top": 1e-7}]}),
    _case("range_chart", "rc_float_exponent_negative", {
        "species_ranges": [{"species": "A", "range_top": -2.5e-5}]}),
    _case("range_chart", "rc_float_integral", {
        "species_ranges": [{"species": "A", "range_top": 3.0}]}),
    _case("range_chart", "rc_float_int", {
        "species_ranges": [{"species": "A", "range_top": 3}]}),
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
    if group == "editable":
        return _editable_python(payload)
    payload = copy.deepcopy(payload)  # the normalizers mutate in place
    fn = _PY_RUNNERS[group]
    if group == "to_newick":
        # Normalize FIRST: the UI feeds a NORMALIZED tree to to_newick, and the
        # JS mirror under test (rcaToNewick) is only compared on that pipeline.
        return to_newick(_normalize_phylogenetic_tree_into(payload))
    return fn(payload)


def _iter_values(value: Any) -> Any:
    """Yield every value in a JSON-shaped structure, ITERATIVELY.

    A recursive walk is not an option here: the depth guard below exists
    precisely because these structures can be thousands of levels deep, and
    recursing over one blows the interpreter stack before the guard can report
    anything useful.
    """
    stack = [value]
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)


# AUDIT-2026-10-01: two classes of Python result CANNOT be written into this
# fixture, and both failed in ways that pointed nowhere near the cause.
#
#   1. A non-finite float. json.dumps writes it as the bare literal Infinity /
#      NaN, and the harness reads the fixture with JSON.parse, which rejects
#      both. The symptom was a SyntaxError at JSON.parse in
#      tests_diff_frontend_parity.js pointing at a payload that looked fine.
#      Measured reachable: safe_json_loads('{"a": 1e400}') returns {'a': inf}.
#   2. A structure nested deeper than the serialisers can carry. json.dumps is
#      recursive, so writing one raises RecursionError inside this generator.
#
# The nesting boundary was measured on both product functions
# (rca_core.json_utils.safe_json_loads vs js/json-utils.js#safeJsonLoads), and
# it is NOT the same number everywhere -- which is why the measurement lives in
# a comment and not in a case:
#   * array form, depth <  1000   both engines parse
#   * array form, depth >= 1000   CPython 3.10 refuses (sys.getrecursionlimit()
#                                 == 1000); the browser parses to at least 10000
#   * array form, depth == 1200   CPython 3.12.14 -- the CI interpreter --
#                                 PARSES it. So "where Python refuses" moves
#                                 with the interpreter, and a fixture case
#                                 built on it cannot stay byte-identical across
#                                 the versions this project tests. That is
#                                 exactly how it was caught: the case passed on
#                                 3.10 and failed on 3.12.
#   * object form, depth >= 1000  both parse, but CPython 3.10 returns a
#                                 structure truncated at depth 992 and reports
#                                 SUCCESS, while the browser returns it whole
# The object row is the dangerous half and is worth knowing about: a silently
# truncated payload that looks like a success is the same shape as the
# refused-extraction gap, one level up. Neither half is fixed here -- changing
# either engine's limit is a product decision, and no realistic range-chart
# reply nests a thousand deep.
#
# The limits below are the serialisers', not the product's: they only decide
# what this harness is able to compare. The guard itself is pinned by
# tests/test_parity_fixture_expressibility.py, because a guard that has never
# been seen to refuse anything is not a guard.
_MAX_FIXTURE_DEPTH = 400


def _assert_expressible(value: Any, case_id: str) -> None:
    stack = [(value, 1)]
    while stack:
        cur, depth = stack.pop()
        if isinstance(cur, float) and (
                cur != cur or cur in (float("inf"), float("-inf"))):
            raise ValueError(
                f"case {case_id!r} produced the non-finite float {cur!r}. Python's "
                "json writes that as a bare Infinity / NaN literal and the "
                "harness reads the fixture with JSON.parse, which rejects it, so "
                "the case cannot be expressed as a differential case. Compare it "
                "with a direct probe instead (see docs/ or the audit notes), or "
                "change the case so the value is finite.")
        if depth > _MAX_FIXTURE_DEPTH:
            raise ValueError(
                f"case {case_id!r} nests deeper than {_MAX_FIXTURE_DEPTH} levels "
                f"(measured {depth}). json.dumps is recursive and would raise "
                "RecursionError here, so the case cannot be expressed as a "
                "differential case. Note that the two product parsers ALSO "
                "disagree past depth 1000 -- see the table in the comment above.")
        if isinstance(cur, dict):
            stack.extend((x, depth + 1) for x in cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend((x, depth + 1) for x in cur)


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
                _assert_expressible(result, str(case.get("id")))
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
