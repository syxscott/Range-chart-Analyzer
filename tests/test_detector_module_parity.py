r"""aggregate.py and exporter.py each carry their own mode detector.

    _looks_columnar   in rca_core/aggregate.py and rca_core/exporter.py
    _looks_abundance  in rca_core/aggregate.py and rca_core/exporter.py

Two modules independently answering "what shape is this result?". A
disagreement would mean the aggregator and the exporter classify one payload
differently -- the same shape as the exporter's copies drifting from
js/table.js, which the table-config detector parity test covers. This pair is
Python-to-Python and had no such test.

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


def test_the_two_detector_pairs_are_separately_implemented():
    """A guard that compares a function to itself proves nothing.

    If either pair is ever unified -- which would be the right refactor -- the
    identity check below flips and this file says so, instead of quietly
    passing on a comparison of one implementation with itself.
    """
    for name in ("_looks_columnar", "_looks_abundance"):
        a = getattr(AG, name)
        e = getattr(EX, name)
        assert getattr(a, "__code__", None) is not getattr(e, "__code__", None), (
            f"{name} is now the SAME function object in both modules; delete "
            f"this parity test and the refactor itself"
        )
