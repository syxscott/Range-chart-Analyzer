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
from rca_core.extractor import axis_domains_from, pos_to_axis_value  # noqa: E402
from rca_core.aggregate import SCHEMA_BY_MODE, merge_results  # noqa: E402
import rca_core.aggregate as AGG  # noqa: E402
from rca_core.quality import score_range_chart  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "frontend_parity_2026_09_20.json"

GROUPS = (
    "range_chart", "columnar_section", "abundance_diagram",
    "zonation_chart", "phylogenetic_tree", "chart_classification",
    "to_newick", "safe_json_loads", "age_bound",
    "reason_codes", "merge", "aggregate", "quality_coverage",
    "editable", "axis",
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


# --- axis calibration: rca_core/extractor.py vs js/minimax.js ---------------
# AUDIT-2026-10-01. The normalisation pass only hoists the block verbatim (12
# adversarial shapes matched), so the FIT is not in that path -- these functions
# are, and they are what turn the model's 0-999 position into the number a
# figure is drawn from. "axis_calibration" had appeared ZERO times in this
# fixture, while the prompt asks for the block in five modes and js/minimax.js's
# own comment mentions it having "diverged between transports". Measured over the
# shapes below: 180 comparisons, 0 divergences. Recorded as a group so it cannot
# start drifting silently.
_AXIS_CALS = {
    "normal": {"vertical": {"at_0": 1, "at_999": 24, "unit": "bed"}},
    "reversed": {"vertical": {"at_0": 24, "at_999": 1, "unit": "bed"}},
    "fractional": {"vertical": {"at_0": 0.5, "at_999": 99.5, "unit": "bed"}},
    "negative": {"vertical": {"at_0": -4.0, "at_999": 4.0, "unit": "PC1"}},
    "zero_span": {"vertical": {"at_0": 7, "at_999": 7, "unit": "bed"}},
    "float_ends": {"vertical": {"at_0": 0.0, "at_999": 30.0, "unit": "m"}},
    "two_axes": {"x": {"at_0": -4.0, "at_999": 4.0, "unit": "PC1"},
                 "y": {"at_0": -3.0, "at_999": 3.0, "unit": "PC2"}},
    "unknown_axis": {"depth": {"at_0": 0, "at_999": 100, "unit": "cm"}},
    "no_unit": {"vertical": {"at_0": 1, "at_999": 24}},
    "anchors_shape": {"vertical": {
        "anchors": [{"position": 0, "value": 1},
                    {"position": 999, "value": 24}], "unit": "bed"}},
    "not_a_dict": "nonsense",
    "empty": {},
}
# Positions chosen to include both ends, the interior, out-of-range values, a
# fractional one, a non-numeric one, and a missing one.
_AXIS_POSITIONS = [0, 1, 200, 333, 500, 666, 800, 998, 999, -1, 1000, 12.5, None, "500"]
# The calibrations whose every position is compared; the other shapes are
# compared through axis_domains_from alone, which is where a shape that cannot
# form a domain is caught.
_AXIS_FULL_SHAPES = ("normal", "reversed", "zero_span", "two_axes")


def _axis_payload(cal: Any) -> dict:
    return {
        "sections": [{"name": "S1"}],
        "species_ranges": [
            {"species": "A", "section": "S1", "range_top": "Bed 9",
             "range_base": "Bed 7", "top_pos_0_999": 800,
             "base_pos_0_999": 200},
        ],
        "biozones": [], "other_fossils": [], "confidence": 0.8,
        "axis_calibration": cal,
    }


def _axis_python(payload: dict) -> Any:
    op = payload.get("op")
    args = payload.get("args") or []
    if op == "axis_domains_from":
        return axis_domains_from(copy.deepcopy(args[0]))
    if op == "pos_to_axis_value":
        axes = axis_domains_from(copy.deepcopy(args[1]))
        return {"axes": axes,
                "v": pos_to_axis_value(args[0], axes.get(args[3]))}
    raise KeyError(op)


# (the axis cases are added below, after _add is defined)


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
_add(*[
    _case("axis", f"axdom_{name}", {"op": "axis_domains_from",
                                     "args": [_axis_payload(cal)]})
    for name, cal in _AXIS_CALS.items()
])
_add(*[
    _case("axis", f"axpos_{name}_{pos}",
          {"op": "pos_to_axis_value",
           "args": [pos, _axis_payload(_AXIS_CALS[name]), None, "vertical"]})
    for name in _AXIS_FULL_SHAPES
    for pos in _AXIS_POSITIONS
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
    # AUDIT-2026-10-02: two runs spelling the SAME authorship differently.
    # The leaf-level `aggregate` group found the mechanism (the "and" case in
    # the iczn_ cases); these two are the end-to-end proof that it is
    # REACHABLE, because _norm_iczn_author feeds the species dedup key
    # (aggregate.py:878) and the key decides whether the rows are one species
    # or two. Before the fix the desktop produced two rows here and the
    # browser one -- i.e. a merged range chart could carry a duplicate taxon
    # that the same merge collapses on the other endpoint.
    _mg("mrg_author_and_spaced_vs_bracketed", [
        _mrun({"species": "Pseudoschagerina sp.", "section": "S1",
               "author_year": "Smith and Jones, 1950",
               "range_top": "9", "range_base": "7"}),
        _mrun({"species": "Pseudoschagerina sp.", "section": "S1",
               "author_year": "Smith (and) Jones, 1950",
               "range_top": "9", "range_base": "7"}),
    ]),
    # The hyphenated spelling. It passed before the fix AND after, for a
    # reason that is worth stating: the two spellings produce DIFFERENT keys
    # on both engines ("smith and-jones" vs "smith -jones" before, two rows
    # either way), so the row count agrees and the end-to-end comparison sees
    # nothing. The string-level difference is pinned by agiczn_166 instead.
    # Note that neither engine strips the hyphen, and that is correct: "x-y"
    # is a compound surname under ICZN Art. 51.2, not a separator.
    _mg("mrg_author_and_hyphenated", [
        _mrun({"species": "Pseudoschagerina sp.", "section": "S1",
               "author_year": "Smith and-Jones, 1950",
               "range_top": "9", "range_base": "7"}),
        _mrun({"species": "Pseudoschagerina sp.", "section": "S1",
               "author_year": "Smith & Jones, 1950",
               "range_top": "9", "range_base": "7"}),
    ]),
    # Same surname, different year must stay TWO species -- the property the
    # author suffix exists to protect, and the reason the "and" fix above
    # cannot be "just strip more punctuation".
    _mg("mrg_author_year_still_splits", [
        _mrun({"species": "Pseudoschagerina sp.", "section": "S1",
               "author_year": "Smith and Jones, 1950",
               "range_top": "9", "range_base": "7"}),
        _mrun({"species": "Pseudoschagerina sp.", "section": "S1",
               "author_year": "Smith and Jones, 1960",
               "range_top": "9", "range_base": "7"}),
    ]),
    # AUDIT-2026-10-02: a CONTROL, not a bug guard, and the reason is worth
    # recording. The obvious way to expose the CJK "\b" divergence end to end
    # is two runs writing "中华虫属" and "中华虫属sp." and expecting two rows.
    # That case CANNOT fail: the dedup key is (id_norm, quals) and id_norm
    # already contains _norm(species) of the same string, and _norm
    # deliberately keeps the marker, so the two rows separate on id_norm
    # before the qualifier is ever consulted. It was written, watched pass
    # against the unfixed code, and deleted rather than left in place with a
    # misleading name.
    #
    # What is left is the spaced control below: the ordinary path, which
    # worked before and must keep working. If the ASCII-boundary rewrite ever
    # breaks THIS, the rewrite is wrong -- and unlike the deleted case it can
    # actually fail.
    _mg("mrg_qualifier_cjk_with_space", [
        _mrun({"species": "中华虫属", "section": "S1",
               "range_top": "9", "range_base": "7"}),
        _mrun({"species": "中华虫属 sp.", "section": "S1",
               "range_top": "9", "range_base": "7"}),
    ]),
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


# --- aggregate leaf functions (rca_core/aggregate.py vs js/aggregate.js) ------
# AUDIT-2026-10-02. The `merge` group above drives rcaMergeResults end to end,
# which is the only aggregate coverage the differential harness had. Everything
# rcaMergeResults CALLS was untested across engines: _norm / _norm_iczn_author
# / _extract_qualifiers / _str_for_merge / _mode / _merge_scalar_field /
# _stable_typed_mode / _merge_confidence. Those are the leaves where the
# decisions live (what counts as the same species, what counts as a missing
# value, how a tie breaks), and a divergence there silently changes which rows
# the two engines consider the same taxon.
#
# Payload shape mirrors `reason_codes`: {"op", "args"}. The op names are SHORT
# ALIASES on purpose -- the two implementations name these functions
# differently (rcaNormIcbnAuthor, and the "Icbn" is a typo that is in the
# product, not a test artifact), so a shared spelling would have to paper over
# it. The alias table lives in the runner on BOTH sides and must stay in step.
#
# The `_NO_MERGE` / `NO_MERGE` sentinels are normalized to null on BOTH sides
# inside the runners -- a bare object() and a Symbol are not expressible in
# JSON, and normalizing on one side only is what produced 751 phantom
# divergences once already.
#
# Result of the first run: 9 divergences in two families, and the two families
# had OPPOSITE culprits, which is the reason this group was worth building.
#
#   1. _norm_iczn_author removed the conjunction as the LITERAL " and ", which
#      matches only that exact spelling; js/aggregate.js has always used
#      /\band\b/. Python was wrong. REACHABLE and fixed: the return value is
#      spliced into the species dedup key (aggregate.py:878), so two runs
#      spelling one authorship "Smith and Jones, 1950" and
#      "Smith (and) Jones, 1950" produced on the desktop
#          .species_ranges: length py=2 js=1
#          .species_ranges[0].agreement: py="1/2" js="2/2"
#      -- a duplicate species in the merged chart AND an agreement score
#      reporting a disagreement the user never had. Pinned end to end by
#      mrg_author_and_spaced_vs_bracketed.
#
#   2. \b in the qualifier patterns and in "and"/"et al." is Unicode-aware on
#      Python str and ASCII-only in JavaScript. JS was wrong on the leaf, but
#      NO reachable effect exists: all three _extract_qualifiers call sites
#      pair the qualifier with _norm() of the same string, and _norm keeps the
#      markers, so the qualifier is a redundant key component. Fixed anyway
#      (it removes a class, not an instance) and pinned at the leaf. The
#      obvious end-to-end case CANNOT fail and was deleted rather than left
#      behind under a name that implied otherwise -- see
#      mrg_qualifier_cjk_with_space.
#
# Also measured, NOT a cross-engine divergence, and left alone: the "nom."
# label splices a captured group in with the input's CASE, so
# "Genus nom. dub." and "GENUS NOM. DUB." produce different qualifier sets
# ("nom. dub" vs "nom. DUB") and therefore separate in the dedup key even
# though their _norm values are equal. Both engines do this identically (the
# uppercase corpus rows all match), so it is an engine-internal inconsistency
# about a case-insensitive marker, not a transport one. Changing it would
# alter desktop dedup with no cross-engine reason to, so it is reported rather
# than fixed.
#
# SECOND WAVE -- a clean result, recorded so nobody re-derives it. The block
# below adds the dispatchers and the mutating mergers (mergeFieldAcrossRuns,
# mergeMappingField, mergeStructuredField, rcaMergeRowWarnings,
# rcaAddRowWarning, rcaMergeContractField, rcaIsChimericRow,
# rcaRecombinationBallots) over 500 more cases: 0 divergences. Those eight
# have been through several parity rounds and are aligned; the chimera
# detector in particular survives every key-count in the corpus, which is the
# shape AUDIT-2026-10-01 [item 9.13] made configurable after it had been
# hardcoded to the four range-chart field names.

# Harvested from the committed real extraction payloads
# (tests/fixtures/real_payloads/*.json, 8 files) -- the taxon-shaped strings
# among their name/species/label/form/genus fields. Real output beats a
# hand-written corpus, so these are the primary inputs; the hand-written rows
# below are only the edge shapes real payloads happened not to contain.
_AGG_REAL = [
    "A.? irregularis", "Bathylagus sp.", "C. robusta", "E. antarctica",
    "E. spinosum", "G. braueri", "G. nicholsi", "G. opisthopterus",
    "L. conica", "S. rad", "Rhizaria sp.", "Acantharia", "Actinommidea",
    "Acanthodesmoidea", "Aulacanthidae", "Aulosphaeridae, Sagosphaeridae",
    "Cercozoa (root)", "Collodaria clade", "Nassellaria clade",
    "Radiolaria clade", "Spumellaria clade", "Phaeodaria clade",
    "Whaingaroan", "Cache Creek Terrane", "Clade H", "Clade K",
    "inner node (Rhizaria sp.+Phacodinidae+Clade K)",
    "inner node (Coelodendridae+Conchariidae)",
    "inner node (Tuscaroridae+Aulosphaeridae/Sagosphaeridae)",
    "inner node (Hexalonchidae+Hexastylidae)",
    "inner node (Theopiliidae+Plagiacanthoidea+Acanthodesmoidea+Sphaerozoidae"
    "+Collophidiidae+Collospheeridae+Orosphaeridae)",
    "Late Permian (Wuchiapingian)", "Early Cretaceous — Albian (Alb.)",
    "Ratburi Group – Um Luk Formation (uppermost Lower–Middle Permian)",
    "Kaeng Krachan Group – Khao Chao Formation (Lower Permian)",
    "Chert radiolarian age (Albian–Cenomanian)",
    "negative δ30Si excursion", "positive δ30Si peak",
    "Principal 238U-206Pb age peak (Conglomerate, n=95)",
    "YSG and YC1σ age clusters (Santonian–Coniacian)",
    "Varicolored shale interval (age uncertain)",
    "Ko He Formation (?)", "other Cercozoa",
]

# The real payloads carry no taxonomic authorship at all (no "Smith, 1950"),
# so this matrix is HAND-WRITTEN and is the one part of the group that is.
# It is laid out along the axes the implementation actually branches on:
# comma / no comma, parens, em-dash vs double-dash, "ex" / "in", "&" / "and",
# "et al.", and the boundary shapes of the word "and".
_AGG_ICZN = [
    "Smith, 1950", "(Smith, 1950)", "Smith 1950", "Smith,1950",
    "SMITH, 1950", "  Smith,  1950  ", "Smith, 1960", "Smith, 50",
    "Smith", "", "   ", "Smith, 1950a", "Smith, 1950, 1951",
    "J. Smith, 1950", "K. Smith, 1950", "J. Smith & K. Smith, 1950",
    "Smith and Jones, 1950", "Smith & Jones, 1950",
    "Smith, 1950 and Jones, 1951",
    # The boundary shapes of "and": Python uses replace(" and ", " ") while the
    # JS mirror uses /\band\b/g. Every one of these has a word boundary
    # around "and" but NOT a space on both sides.
    "Smith (and) Jones, 1950", "Smith and(Jones), 1950",
    "Smith and-Jones, 1950", "Smith and/Jones, 1950",
    "Smith, and Jones, 1950", "Smith, 1950 and, Jones",
    "Sanderson, 1950", "Anderson, 1950", "Alexander, 1950",
    "Brand, 1950", "andersonia, 1950",
    "Smith—Jones, 1950", "Smith--Jones, 1950", "Smith----Jones, 1950",
    "Smith ex Jones, 1950", "Smith in Jones, 1950",
    "Smith ex Jones and Brown, 1950", "Smith et al., 1950",
    "Smith et al. 1950", "Smith et al, 1950", "Smith et. al., 1950",
    "Smith,  and  Jones, 1950",
    "(Smith, 1950) & (Jones, 1951)", "Smith, 9999", "1950",
    "δ13C, 1950", "O. Smith, 1950",
    # The \b family again, on the "and" pattern specifically.
    "中文and文", "和and和", "Smithand和Jones, 1950",
    "中文et al.文", "和et. al.和",
]

_AGG_NORM_EDGE = [
    "", "   ", "\t\n ", "Genus  sp.", "  Genus   sp.  ",
    "GENUS SP.", "Genus\tsp.", "中文 名称", "ＡＢＣ",
    "Zoological  Name", "A.B", "A. B.  C",
]

_AGG_QUAL_EDGE = [
    "", "Genus cf.", "Genus aff.", "Genus sp.", "Genus spp.",
    "Genus s.l.", "Genus s. l.", "Genus s.str.", "Genus s. str.",
    "Genus ex gr.", "Genus ex gr", "Genus ex groupe.",
    "Genus nom. dub.", "Genus nom. nud.", "Genus nom. nov.",
    "Genus nom. cons.", "Genus nom. obl.", "Genus nom. van.",
    "Genus comb. nov.", "Genus stat. nov.", "Genus subsp.",
    "Genus var.", "A.?", "A.?", "A. ?", "A.?\n", "A. ? ",
    "Genus cf", "Genus aff", "Genus sp", "Genus spp",
    "coffee", "affinis", "Genus sp. cf. aff.",
    "Genus cf. aff. ex gr. s.l. ?",
    # AUDIT-2026-10-02: the \b family. Python's \b is Unicode-aware on str
    # (a CJK character counts as a word character); JS's is ASCII-only, so a
    # CJK character counts as NON-word and manufactures a boundary. A Chinese
    # author writing a taxon with no space before the marker -- "中华虫属sp." --
    # is an ordinary thing to produce, not a contrived one.
    "中华虫属sp.", "中华虫属 sp.", "和sp.和", "和cf.和", "中aff.中",
    "sp.中华", "cf.中华", "sp.1", "sp.1a", "1sp.", "sp-", "sp_",
    "A.sp.", "sp. sp.", "Genus sp.",
    # AUDIT-2026-10-02: the label for the nom./comb. nov. family is built by
    # splicing a CAPTURED group into a template, so it can carry the input's
    # case. These pin whether the two engines agree about that.
    "Genus nom. dub.", "GENUS NOM. DUB.", "Genus NOM. Dub.",
    "  Genus nom.  dub.", "Genus nom. nov.", "GENUS COMB. NOV.",
    "Genus comb. nov.", "GENUS STAT. NOV.", "Genus stat. nov.",
]

_AGG_STRMERGE = [
    "abc", "", 0, 1, -1, 1.0, 1.5, True, False, None,
    1e16, 1e-7, 3.0, "1", "1.0", "True", "0",
    [1, 2], {"a": 1}, 0.1, 1 / 3,
]

_AGG_MODE = [
    [], [None, None], ["", "  "], [0, 0, 12], [0, 0], [0, 0.0],
    ["A", "A", "B"], ["B", "A"], ["A", "B", "C"], ["B", "B", "A", "A"],
    ["b", "B"], ["B", "b", "C", "c"], [True, True, False],
    [True, False], [False, True], [True, 1], [1, "1"], [1, 1.0],
    [1.0, 1], ["0", 0], [0, "0"], [None, "x", "x"],
    [{"a": 1}, "x", "x"], [[1], "y", "y"],
    [2, 10, 2, 10, 2], ["10", "2", "10", "2"],
    ["Zebra", "apple", "Apple"], ["ä", "z", "Z"],
]

_AGG_SCALAR = [
    [], [None], ["", " "], ["a", "a", "b"], ["b", "a"],
    [True, True, False], [True, False], [False, True],
    [True, "maybe"], [True, False, "maybe"], [0, False], [0, True],
    [1, "1"], ["1", 1], [1.0, 1], [1.5, 1.5, 2],
    [None, None, "x"], [{"a": 1}, "x"], [[1], "y"],
    ["", "", "z"], [0, 0, "a"], ["10", 2, "10", 2],
]

_AGG_TYPED = [
    [], [None], [3, 3, 5], [3, 3.0, 5], [3.0, 3.0], [3.5, 3.5],
    [True, True, 3], [True, 3], ["3", 3], [3, "3", 3.0],
    [5, 3, 5, 3], [7], [7.0], [-0.0, 0.0], [1e16, 1e16],
    [3, 3, 3.0, 3.0, 4, 4], [0, 0, 0],
]

_AGG_CONF = [
    [], [None], ["", "0.5"], [0.5], [0.5, 0.5], [0.1, 0.2],
    [0.01005], [0.01015], [0.03125], [1 / 32], [2 / 32], [3 / 32],
    [-0.5], [1.5], [0], [1], [True, 0.5], ["0.5", 0.5],
    [0.1, 0.2, 0.3, 0.4], [1 / 3, 1 / 3], [0.33333333],
]


def _agg(cid: str, op: str, args: list) -> dict:
    return _case("aggregate", cid, {"op": op, "args": args})


_agg_cases: list[dict] = []
for _s in _AGG_REAL + _AGG_NORM_EDGE:
    _agg_cases.append(_agg("agnorm_%d" % len(_agg_cases), "norm", [_s]))
    _agg_cases.append(_agg("agqual_%d" % len(_agg_cases), "qualifiers", [_s]))
for _s in _AGG_QUAL_EDGE:
    _agg_cases.append(_agg("agqual_%d" % len(_agg_cases), "qualifiers", [_s]))
for _s in _AGG_ICZN:
    _agg_cases.append(_agg("agiczn_%d" % len(_agg_cases), "iczn", [_s]))
for _v in _AGG_STRMERGE:
    _agg_cases.append(_agg("agstr_%d" % len(_agg_cases), "str_merge", [_v]))
for _vs in _AGG_MODE:
    _agg_cases.append(_agg("agmode_%d" % len(_agg_cases), "mode", [_vs]))
for _vs in _AGG_SCALAR:
    _agg_cases.append(_agg("agscal_%d" % len(_agg_cases), "merge_scalar", [_vs]))
for _vs in _AGG_TYPED:
    # The type argument is a PYTHON-only parameter; the JS mirror
    # (mergeTypedInteger) has exactly one. Both runners read args[1] and both
    # must arrive at the same values, so the type is carried in the payload
    # rather than hard-coded on one side.
    _agg_cases.append(_agg("agtyped_%d" % len(_agg_cases), "typed_mode", [_vs, "int"]))
for _vs in _AGG_CONF:
    _agg_cases.append(_agg("agconf_%d" % len(_agg_cases), "confidence", [_vs]))
_add(*_agg_cases)


# --- aggregate, second wave: the dispatchers and the mutating mergers --------
# AUDIT-2026-10-02. The first block covered the eight leaves whose OUTPUT is
# a scalar. These are the ones that dispatch, recurse, or mutate, which is
# where the shapes get unusual rather than the values. None of them had any
# cross-engine coverage before this.
#
# Real structured items, taken from the committed payloads so the shape is one
# the product actually emits (tests/fixtures/real_payloads/
# Bole_et_al_2020_...json intervals[0] and
# Shimura_Yusuke_et_al_2020_...json intervals[0]):
_AGG_STRUCT_REAL = [
    {"name": "Late Permian (Wuchiapingian)", "top_depth_m": "",
     "base_depth_m": "", "top_age_ma": "260", "base_age_ma": "252",
     "lithology": "", "geometry": {
         "version": 1, "scale": "pos_0_999", "calibrated": True,
         "points": {"top_pos_0_999": {"pos": 0, "axis": "vertical",
                                      "value": 260.0, "unit": "Ma"}}}},
    {"name": "Late Cretaceous – Coniacian (Con.)", "top_depth_m": "",
     "base_depth_m": "", "top_age_ma": "86.3", "base_age_ma": "89.8",
     "lithology": "Varicolored shale (uncertain)",
     "reason_codes": ["low_confidence"]},
]
# The P1-11 collapse the docstring claims: {"a": 8} and {"a": "8"} must share
# a signature. The next four pairs are the shapes that could break it -- a
# bool, a float and a string that HAPPENS to spell a number or a Python repr.
_AGG_STRUCT_EDGE = [
    {"a": 8}, {"a": "8"}, {"a": 8, "b": 1}, {"b": 1, "a": 8},
    {"a": True}, {"a": "True"}, {"a": False}, {"a": "False"},
    {"a": 1}, {"a": True}, {"a": 1.0}, {"a": "1.0"}, {"a": 0.0}, {"a": -0.0},
    {"a": None}, {"a": "None"}, {"a": "null"},
    {"a": []}, {"a": [1]}, {"a": [1, 2]}, {"a": "1,2"}, {"a": "[1, 2]"},
    {"a": {}}, {"a": {"b": 1}}, {"a": '{"b": 1}'},
    {"a": ""}, {"a": " "}, {"a": 0}, {"a": "0"}, {"a": ""},
    {"a": "x", "b": "y"}, {"b": "y", "a": "x"},
    {"a": 1e16}, {"a": 1e-7}, {"a": "1e+16"}, {"a": "1e-16"},
]

_AGG_MAPPING = [
    [], [None], [{}, {}], [{"a": 1}], [{"a": 1}, {"a": 2}],
    [{"a": 1}, {"b": 2}], [{"a": 1}, {"a": None}], [{"a": None}, {"a": 1}],
    [{"a": {"b": 1}}, {"a": {"b": 2}}], [{"a": {"b": 1}}, {"a": {"c": 1}}],
    [{"a": [{"x": 1}]}, {"a": [{"x": 1}, {"y": 2}]}],
    [{"a": "x"}, {"a": "y"}, {"a": "x"}],
    [{"a": True, "b": True}, {"a": True, "b": False}],
    [{"z": 1, "a": 2}, {"a": 3, "z": 4}],
    [{}, {"a": 1}],
    [{"a": 1}, "scalar"], ["scalar", {"a": 1}],
    [{"a": 1}, [{"x": 1}]],
]

_AGG_ACROSS = [
    [], [None], [None, None], ["a"], ["a", "a", "b"],
    [[], []], [[{"a": 1}], [{"a": 1}]], [[{"a": 1}], [{"a": 2}]],
    [[{"a": 1}], []], [[], [{"a": 1}]],
    [{}, {}], [{"a": 1}, {"a": 1}], [{"a": 1}, {}],
    [0, 0, False], [0, False], [1, "1"],
    [[1], [2]], [["a"], ["b"]], [[{"a": 1}], ["scalar"]],
    [{"a": 1}, [1]], [1, {"a": 1}],
    [0.5, 0.7], [True, 1],
]

_AGG_WARN_TARGETS = [
    {}, {"_warning": ""}, {"_warning": None},
    {"_warning": "one"}, {"_warning": ["a", "b"]},
    {"_warning": ["a", "a", "b"]}, {"_warning": "a", "species": "X"},
    {"_warning": 0}, {"_warning": False}, {"_warning": ["", "a"]},
    {"_warning": [1, "a"]}, {"_warning": [[], "a"]},
]
_AGG_WARN_VALUES = [
    [], [None], [""], ["one"], [["a", "b"]], [["b", "c"]],
    ["one", "two"], [["a"], "b"], [None, "a", ""], ["a", "a"],
    [0], [False], [1], [[]], [[[]]],
]

_AGG_CONTRACT_KEYS = ["reason_codes", "response_kind", "geometry", "species", ""]
_AGG_CONTRACT_VALUES = [
    [], [None], [["low_confidence"]], [["low_confidence"], ["inferred"]],
    [[], ["low_confidence"]], ["extracted"], ["not_drawn"],
    ["extracted", "not_drawn"], ["uncertain"], ["garbage"],
    ["extracted", "extracted", "not_drawn"],
    [None, "extracted"], ["", "extracted"],
    [{"version": 1, "points": {"p": 1}}], [{"version": 1}], [{}],
    [{"points": {}}, {"points": {"q": 1}}], [None, {"points": {"z": 1}}],
]

_AGG_CHIMERIC_ROWS = [
    [],
    [{"range_base": "9", "range_top": "7", "biozone": "Z1", "section": "S1"}],
    [{"range_base": "7", "range_top": "9", "biozone": "Z1", "section": "S1"},
     {"range_base": "7", "range_top": "9", "biozone": "Z1", "section": "S1"}],
    [{"range_base": "7", "range_top": "9", "biozone": "Z1", "section": "S1"},
     {"range_base": "7", "range_top": "9", "biozone": "Z2", "section": "S1"}],
    [{"range_base": "7", "range_top": "9"},
     {"range_base": "7", "range_top": "9"}],
    [{"range_base": "9", "range_top": "9"}],
    [{"range_base": "", "range_top": ""}],
    [{}, {}],
    [{"range_base": "9a", "range_top": "9"}, {"range_base": "9", "range_top": "9a"}],
]
_AGG_CHIMERIC_KEYS = [
    ["range_base", "range_top", "biozone", "section"],
    ["range_base", "range_top"],
    ["species"],
    [],
]

_AGG_BALLOT_ROWS = [
    [],
    [{"range_base": "7", "range_top": "9"}],
    [{"range_base": "7", "range_top": "9"},
     {"range_base": "7", "range_top": "9"},
     {"range_base": "8", "range_top": "9"}],
    [{"range_base": "9", "range_top": "7"}],
    [{"range_base": "9a", "range_top": "9"}, {"range_base": "9", "range_top": "9a"}],
    [{}, {}],
    [{"range_base": " 7 ", "range_top": "9"}, {"range_base": "7", "range_top": "9"}],
    [{"range_base": "B", "range_top": "9"}, {"range_base": "A", "range_top": "9"}],
    [{"range_base": "bed 9", "range_top": "9"},
     {"range_base": "bed 9 (rp13)", "range_top": "9"}],
]
_AGG_BALLOT_KEYS = [
    ["range_base", "range_top", "biozone", "section"],
    ["range_base", "range_top"],
    # No `None` entry on purpose. js/aggregate.js writes
    # `keys || RCA_RECOMBINATION_KEYS`, so a null would silently fall back to
    # a default there and raise TypeError in Python (`for k in None`). That
    # asymmetry is unreachable -- Python's parameter is a required positional,
    # so every call site supplies it -- and putting it in the corpus would
    # record a calling-convention difference as if it were a behaviour one.
    ["species", "section"],
]

_agg2: list[dict] = []
for _v in _AGG_MAPPING:
    _agg2.append(_agg("agmap_%d" % len(_agg2), "merge_mapping", [_v]))
for _v in _AGG_ACROSS:
    _agg2.append(_agg("agacr_%d" % len(_agg2), "merge_across", [_v]))
for _i, _v in enumerate(_AGG_STRUCT_REAL + _AGG_STRUCT_EDGE):
    _agg2.append(_agg("agstr2_%d" % len(_agg2), "merge_structured", [[_v]]))
    _agg2.append(_agg("agstr2b_%d" % len(_agg2), "merge_structured",
                      [[_v, _v]]))
_agg2.append(_agg("agstr2_pair_8", "merge_structured",
                  [[{"a": 8}, {"a": "8"}]]))
_agg2.append(_agg("agstr2_pair_true", "merge_structured",
                  [[{"a": True}, {"a": "True"}]]))
_agg2.append(_agg("agstr2_pair_1", "merge_structured",
                  [[{"a": 1}, {"a": 1.0}]]))
_agg2.append(_agg("agstr2_real_pair", "merge_structured",
                  [[_AGG_STRUCT_REAL[0], _AGG_STRUCT_REAL[1],
                    _AGG_STRUCT_REAL[0]]]))
_agg2.append(_agg("agstr2_mixed", "merge_structured",
                  [[{"a": 1}, "scalar", None, [1], [{"b": 2}]]]))
for _t in _AGG_WARN_TARGETS:
    for _v in _AGG_WARN_VALUES:
        _agg2.append(_agg("agwarn_%d" % len(_agg2), "row_warnings",
                          [copy.deepcopy(_t), copy.deepcopy(_v)]))
for _r in _AGG_WARN_TARGETS:
    for _flag in ("response_kind_divergent", "", None, "already"):
        _agg2.append(_agg("agwarnadd_%d" % len(_agg2), "add_row_warning",
                          [copy.deepcopy(_r), _flag]))
for _k in _AGG_CONTRACT_KEYS:
    for _v in _AGG_CONTRACT_VALUES:
        _agg2.append(_agg("agcon_%d" % len(_agg2), "contract_field",
                          [_k, copy.deepcopy(_v), {}]))
for _g in _AGG_CHIMERIC_ROWS:
    for _keys in _AGG_CHIMERIC_KEYS:
        _merged = {"range_base": "9", "range_top": "7", "biozone": "Z1",
                   "section": "S1"}
        _agg2.append(_agg("agchim_%d" % len(_agg2), "chimeric",
                          [copy.deepcopy(_g), copy.deepcopy(_merged),
                           _keys]))
for _g in _AGG_BALLOT_ROWS:
    for _keys in _AGG_BALLOT_KEYS:
        _agg2.append(_agg("agball_%d" % len(_agg2), "ballots",
                          [copy.deepcopy(_g), _keys]))
_add(*_agg2)


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


# Alias table for the `aggregate` group. Kept as an explicit mapping rather than
# getattr()/string dispatch: the two engines name these differently on purpose
# (rcaNormIcbnAuthor), and a getattr would happily resolve a typo to nothing.
# The JS runner carries the same table with the JS names; the two must stay in
# step or the group silently stops comparing anything.
_AGG_PY_OPS = {
    "norm": lambda a: AGG._norm(a[0]),
    "iczn": lambda a: AGG._norm_iczn_author(a[0]),
    "qualifiers": lambda a: sorted(AGG._extract_qualifiers(a[0])),
    "str_merge": lambda a: AGG._str_for_merge(a[0]),
    "mode": lambda a: AGG._mode(a[0]),
    "merge_scalar": lambda a: _agg_sentinel(AGG._merge_scalar_field(a[0])),
    "typed_mode": lambda a: _agg_sentinel(
        AGG._stable_typed_mode(a[0], int if a[1] == "int" else str)),
    "confidence": lambda a: _agg_sentinel(AGG._merge_confidence(a[0])),
    # Second wave. The three mutating mergers are called on a COPY and the
    # copy is returned, so the JSON comparison sees the mutation on both
    # sides instead of comparing a None that means "it happened in place".
    "merge_mapping": lambda a: _agg_sentinel(AGG._merge_mapping_field(a[0])),
    "merge_structured": lambda a: AGG._merge_structured_field(a[0]),
    "merge_across": lambda a: _agg_sentinel(AGG._merge_field_across_runs(a[0])),
    "row_warnings": lambda a: _agg_mutate(AGG._merge_row_warnings, a),
    "add_row_warning": lambda a: _agg_mutate(AGG._add_row_warning, a),
    # _merge_contract_field returns a bool AND mutates the target, so the
    # payload carries the "returns True" half explicitly -- otherwise a
    # JS runner that returned only the target would silently agree with a
    # Python runner that returned only True.
    "contract_field": lambda a: _agg_contract_py(a),
    "chimeric": lambda a: bool(AGG._is_chimeric_row(a[0], a[1], tuple(a[2]))),
    # ballots takes (group, keys) -- TWO arguments, so keys is args[1].
    # Writing args[2] here is the same arity mistake the JS runner would
    # have made in mirror image; both read [1] and the payload carries two.
    "ballots": lambda a: AGG._recombination_ballots(a[0], tuple(a[1])),
}


def _agg_mutate(fn, a):
    """Call an in-place merger on a copy and return the mutated copy."""
    target = copy.deepcopy(a[0])
    if fn is AGG._add_row_warning:
        fn(target, a[1])
    else:
        fn(target, copy.deepcopy(a[1]))
    return target


def _agg_contract_py(a) -> Any:
    key, values, target = a[0], a[1], copy.deepcopy(a[2])
    handled = AGG._merge_contract_field(key, values, target)
    return {"handled": bool(handled), "target": target}


def _agg_sentinel(value: Any) -> Any:
    """``_NO_MERGE`` -> None. The JS runner maps its NO_MERGE Symbol to null in
    the same place; doing it on one side only is what turned a real finding
    into a few hundred phantom rows once already."""
    return None if value is AGG._NO_MERGE else value


def _aggregate_python(payload: dict) -> Any:
    op = payload["op"]
    if op not in _AGG_PY_OPS:
        raise KeyError(op)
    return _AGG_PY_OPS[op](list(payload.get("args") or []))


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
    if group == "aggregate":
        return _aggregate_python(copy.deepcopy(payload))
    if group == "quality_coverage":
        return score_range_chart(copy.deepcopy(payload))
    if group == "editable":
        return _editable_python(payload)
    if group == "axis":
        return _axis_python(payload)
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
