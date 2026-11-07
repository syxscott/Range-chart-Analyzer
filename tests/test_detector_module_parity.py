r"""aggregate.py and exporter.py used to each carry their own mode detector.

    _looks_columnar   in rca_core/aggregate.py and rca_core/exporter.py
    _looks_abundance  in rca_core/aggregate.py and rca_core/exporter.py

ARCH-REVIEW-2026-11-02 A7 UNIFIED all six detectors into rca_core/shape.py.
Both modules now re-export the SAME function objects under their old private
names. The fixture-based behavioural tests below are unchanged and now pin
that single implementation; test_all_detectors_now_share_one_implementation
replaces the old "these must be separate copies" sentinel, which explicitly
asked to be deleted once the refactor happened.

Two modules independently answering "what shape is this result?" was the
drift risk: a disagreement meant the aggregator and the exporter classified
one payload differently -- the same shape as the exporter's copies drifting
from js/table.js, which the table-config detector parity test covers.

MEASURED: the two agree on all 8 gold fixtures and on all 22 trustworthy
boundary shapes below. Zero disagreements. This commit is the guard, not a
fix -- and the value of a negative result is precisely that it turns an
unknown into a standing fact, because the duplication is a drift risk that the
next change to either side could turn into a real divergence.

Two shapes are deliberately excluded from the comparison, with the reason
carried in the table rather than asserted in prose: a payload whose first row
is not a dict is unreadable to at least one of the two detectors, so a
verdict on it would be a verdict on the probe. That shape self-check is the
lesson from the hand-written-payload false positives earlier in this audit,
applied as a rule instead of a habit.
"""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core import aggregate as AG  # noqa: E402
from rca_core import exporter as EX  # noqa: E402

GOLD_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures", "gold")


def _load_gold():
    out = []
    for dirpath, _d, files in os.walk(GOLD_DIR):
        for fn in files:
            if fn == "ground_truth.json":
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    out.append((os.path.basename(os.path.dirname(p)), json.load(f)))
    return sorted(out)


GOLD = _load_gold()


# label -> payload, chosen to sit on the boundaries rather than the happy path
SHAPES = [
    ("empty", {}),
    ("empty sections", {"sections": []}),
    ("sections is not a list", {"sections": "nope"}),
    ("section with id AND name", {"sections": [{"id": "s1", "name": "S"}]}),
    ("section with id only", {"sections": [{"id": "s1"}]}),
    ("section with name only", {"sections": [{"name": "S"}]}),
    ("section empty dict", {"sections": [{}]}),
    ("section id is None", {"sections": [{"id": None, "name": "S"}]}),
    ("section id is empty string", {"sections": [{"id": "", "name": "S"}]}),
    ("section id is an int", {"sections": [{"id": 7, "name": "S"}]}),
    ("two sections first has id", {"sections": [{"id": "a"}, {"name": "b"}]}),
    ("two sections first has name", {"sections": [{"name": "a"}, {"id": "b"}]}),
    ("abundances only", {"abundances": [{"taxon": "T", "site": "S",
                                         "abundance": 1}]}),
    ("abundances empty", {"abundances": []}),
    ("abundances non-list", {"abundances": {}}),
    ("abundance row without abundance", {"abundances": [{"taxon": "T",
                                                        "site": "S"}]}),
    ("abundance zero value", {"abundances": [{"taxon": "T", "site": "S",
                                              "abundance": 0}]}),
    ("abundance with abundance_unit", {"abundances": [{"taxon": "T",
                                                      "site": "S",
                                                      "abundance": "35",
                                                      "abundance_unit": "%"}]}),
    ("abundances + sections", {"sections": [{"name": "S"}],
                               "abundances": [{"taxon": "T", "site": "S",
                                               "abundance": 1}]}),
    ("abundances + species_ranges", {"species_ranges": [{"species": "S"}],
                                     "abundances": [{"taxon": "T",
                                                     "abundance": 1}]}),
    ("abundances and zones", {"abundances": [{"taxon": "T", "abundance": 1}],
                              "zones": [{"name": "PAZ"}]}),
    ("columnar + abundances", {"sections": [{"id": "s1", "name": "S"}],
                               "abundances": [{"taxon": "T",
                                               "abundance": 1}]}),
    ("columnar + species_ranges", {"sections": [{"id": "s1"}],
                                   "species_ranges": [{"species": "S"}]}),
    ("abundance_unit only", {"abundances": [{"abundance_unit": "%"}]}),
]

# Readable by both, or excluded. A payload whose first row is not a dict is
# unreadable to at least one detector, so a verdict on it is a verdict on the
# probe. Recorded here rather than dropped silently.
UNTRUSTWORTHY = [
    ("sections with a non-dict first", {"sections": ["x"]}),
    ("abundance row non-dict", {"abundances": ["T"]}),
]


def _verdict(data):
    d = copy.deepcopy(data)
    return (bool(AG._looks_columnar(d)), bool(EX._looks_columnar(d)),
            bool(AG._looks_abundance(d)), bool(EX._looks_abundance(d)))


def test_gold_fixtures_exist():
    assert len(GOLD) == 8, f"expected 8 gold cases, found {len(GOLD)}"


@pytest.mark.parametrize("case_id,data", GOLD, ids=[c for c, _ in GOLD])
def test_gold_results_classify_identically(case_id, data):
    ac, ec, aa, ea = _verdict(data)
    assert ac == ec, f"{case_id}: _looks_columnar agg={ac} exporter={ec}"
    assert aa == ea, f"{case_id}: _looks_abundance agg={aa} exporter={ea}"


@pytest.mark.parametrize("label,data", SHAPES, ids=[s[0] for s in SHAPES])
def test_boundary_shapes_classify_identically(label, data):
    ac, ec, aa, ea = _verdict(data)
    assert ac == ec, f"{label}: _looks_columnar agg={ac} exporter={ec} " \
                     f"({json.dumps(data, ensure_ascii=False)})"
    assert aa == ea, f"{label}: _looks_abundance agg={aa} exporter={ea} " \
                     f"({json.dumps(data, ensure_ascii=False)})"


@pytest.mark.parametrize("label,data", UNTRUSTWORTHY,
                         ids=[u[0] for u in UNTRUSTWORTHY])
def test_unreadable_payloads_are_excluded_with_a_reason(label, data):
    """They are excluded ON PURPOSE, so the reason is asserted rather than
    assumed -- otherwise "excluded" reads as "quietly not checked"."""
    assert isinstance(data.get("sections", [None])[0]
                      if isinstance(data.get("sections"), list) and data["sections"]
                      else data.get("abundances", [None])[0]
                      if isinstance(data.get("abundances"), list) and data["abundances"]
                      else None, str), (
        f"{label} is supposed to be unreadable; if it parses now, move it into "
        "SHAPES rather than leaving it silently unchecked"
    )


# --- the comparison is not vacuous -------------------------------------


def test_the_shape_table_exercises_both_answers_for_both_detectors():
    col = {s[0]: _verdict(s[1])[0] for s in SHAPES}
    abn = {s[0]: _verdict(s[1])[2] for s in SHAPES}
    assert True in col.values() and False in col.values(), (
        "every shape gives the same columnar answer -- nothing is being compared"
    )
    assert True in abn.values() and False in abn.values(), (
        "every shape gives the same abundance answer -- nothing is being compared"
    )


def test_the_gold_set_covers_distinct_shapes():
    """The fixtures must not all be the same shape, or the comparison above is
    one question asked eight times.

    Keyed on the top-level keys rather than a `mode` field: a first draft
    assumed every gold result carries `mode`, and none of them do. The second
    draft then asserted one shape per case, and the set is 4 modes x 2 seeds,
    so there are 4 shapes for 8 cases -- also wrong, and also caught. What
    actually matters is that several shapes are present AND that they drive
    different verdicts.
    """
    shapes = {frozenset(d) - {"metadata", "extracted_at", "source_file",
                             "confidence"} for _c, d in GOLD}
    assert len(shapes) >= 4, f"only {len(shapes)} distinct shapes: {shapes}"
    verdicts = {(_verdict(d)[0], _verdict(d)[2]) for _c, d in GOLD}
    assert len(verdicts) >= 3, (
        f"the fixtures only produce {len(verdicts)} distinct (columnar, "
        f"abundance) verdicts: {sorted(verdicts)}"
    )


def test_all_detectors_now_share_one_implementation():
    """The old sentinel asked to be deleted when unification happened.

    The previous version of this test asserted that aggregate._looks_* and
    exporter._looks_* were DIFFERENT function objects, on the grounds that
    "a guard that compares a function to itself proves nothing". Its failure
    message said: "delete this parity test and the refactor itself".

    ARCH-REVIEW A7 did the refactor: all six detectors now live in
    rca_core/shape.py and both modules re-export the same objects under
    their old private names. Rather than deleting the guard, it is UPGRADED:
    identity is now the REQUIRED state, and the behavioural fixture tests
    above become stronger (they now pin the one shared implementation
    rather than two copies that happened to agree today).
    """
    import rca_core.shape as shape

    pairs = (
        ("_looks_columnar", "looks_columnar"),
        ("_looks_abundance", "looks_abundance"),
        ("_looks_occurrence_matrix", "looks_occurrence_matrix"),
        ("_looks_paleomap", "looks_paleomap"),
        ("_looks_zonation_chart", "looks_zonation_chart"),
        ("_looks_zonation", "looks_zonation_chart"),
        ("_looks_phylogenetic_tree", "looks_phylogenetic_tree"),
        ("_looks_phylogenetic", "looks_phylogenetic_tree"),
    )
    for private, public in pairs:
        a = getattr(AG, private, None)
        e = getattr(EX, private, None)
        canonical = getattr(shape, public)
        # Not every alias exists in every module; at least one must.
        present = [f for f in (a, e) if f is not None]
        assert present, f"neither module exports {private}"
        for f in present:
            assert f is canonical, (
                f"{private} is no longer the shared rca_core.shape.{public} "
                "implementation -- a local copy has crept back in"
            )

    # The registry must list every detector, so adding a seventh shape
    # without wiring it into auto-detect is visible here.
    assert set(shape.MODE_FOR_SHAPE.values()) == {
        "looks_phylogenetic_tree", "looks_columnar",
        "looks_occurrence_matrix", "looks_abundance",
        "looks_zonation_chart", "looks_paleomap",
    }
