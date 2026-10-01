"""The differential fixture cannot express some Python results, and the
generator now refuses them instead of failing confusingly. This pins that
refusal.

AUDIT-2026-10-01. Two classes used to break in ways that pointed nowhere near
the cause:

  * a non-finite float, because ``json.dumps`` writes it as the bare literal
    ``Infinity`` / ``NaN`` and the harness reads the fixture with ``JSON.parse``,
    which rejects both -- the failure surfaced as a ``SyntaxError`` inside
    ``tests_diff_frontend_parity.js``, pointing at a payload that looked fine;
  * a structure nested deeper than the serialisers can carry, because
    ``json.dumps`` is recursive and raised ``RecursionError`` inside the
    generator.

The guard is what makes those failures actionable, and a guard nobody has ever
seen refuse anything is indistinguishable from no guard at all -- so it is
exercised here, on both classes, plus the cases it must NOT touch.

Nothing here depends on where a particular interpreter's parser gives up. That
boundary moves with the CPython version (3.10 refuses a 1200-level nested
array, 3.12.14 parses it), which is precisely why it lives in a comment and not
in a fixture case: a committed fixture has to be byte-identical whichever
interpreter regenerates it.
"""
import importlib.util
import math
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_GEN = REPO / "tests" / "gen_frontend_parity_fixtures.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_parity_fixture", _GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()


def _nest(depth):
    value = 1
    for _ in range(depth):
        value = {"a": value}
    return value


class TestExpressibilityGuard:
    def test_refuses_a_non_finite_float(self):
        for bad in (math.inf, -math.inf, math.nan):
            with pytest.raises(ValueError, match="non-finite"):
                gen._assert_expressible({"a": bad}, "probe")

    def test_the_non_finite_message_names_the_case_and_the_cause(self):
        # A guard that fires with an unhelpful message trades a confusing
        # failure for a different confusing failure.
        with pytest.raises(ValueError) as excinfo:
            gen._assert_expressible({"a": math.inf}, "my_case_id")
        text = str(excinfo.value)
        assert "my_case_id" in text
        assert "JSON.parse" in text          # why the harness cannot read it
        assert "cannot be expressed" in text  # and what to do instead

    def test_refuses_a_structure_too_deep_to_serialise(self):
        too_deep = gen._MAX_FIXTURE_DEPTH + 1
        with pytest.raises(ValueError, match="nests deeper than"):
            gen._assert_expressible(_nest(too_deep), "probe")

    def test_refuses_a_deep_value_hidden_inside_an_ordinary_result(self):
        # The realistic shape is a normal payload with one deep corner, not a
        # deep payload outright.
        with pytest.raises(ValueError, match="nests deeper than"):
            gen._assert_expressible(
                {"sections": [{"name": "A"}], "meta": _nest(gen._MAX_FIXTURE_DEPTH + 1)},
                "probe")

    def test_accepts_every_committed_case(self):
        # The real corpus, through the real build path. If the limit were set
        # too low this is what would catch it.
        fixture = gen.build_fixture()
        assert len(fixture["cases"]) == len(gen.CASES)

    def test_committed_results_are_far_below_the_limit(self):
        # Guards the limit against a future case creeping up to it: measured
        # maximum of the committed corpus is 7 levels, the limit is 400.
        # The computed value is added by build_fixture(), so read it from
        # there rather than from the case list.
        fixture = gen.build_fixture()
        depths = [_depth(c["python"]) for c in fixture["cases"] if "python" in c]
        assert depths, "no committed case carries a Python value"
        assert max(depths) * 10 < gen._MAX_FIXTURE_DEPTH, (
            f"deepest committed result is {max(depths)} levels against a limit of "
            f"{gen._MAX_FIXTURE_DEPTH}; the limit is no longer comfortably above "
            "the corpus and will start rejecting legitimate cases")

    def test_the_boundary_is_where_the_measurement_says_it_is(self):
        # Located by measuring rather than by assuming an off-by-one: a bare
        # scalar already counts as level 1, so _nest(k) is k + 1 deep. Both
        # sides of the boundary are checked so the guard is pinned as a
        # boundary and not merely as "it raises".
        limit = gen._MAX_FIXTURE_DEPTH
        deepest_ok = max(k for k in range(limit + 5)
                         if _depth(_nest(k)) <= limit)
        assert _depth(_nest(deepest_ok)) == limit
        gen._assert_expressible(_nest(deepest_ok), "at the limit")
        with pytest.raises(ValueError, match="nests deeper than"):
            gen._assert_expressible(_nest(deepest_ok + 1), "one past the limit")

    def test_ordinary_values_pass(self):
        gen._assert_expressible({"sections": [{"name": "A"}]}, "ok")
        gen._assert_expressible({"a": 1.5, "b": None, "c": [], "d": {}}, "ok")
        gen._assert_expressible({"a": 0.0, "b": -0.0}, "finite floats are fine")


def _depth(value, _best=0):
    """Iterative structural depth: a recursive walk of a deep value would blow
    the interpreter stack, which is the very thing this file is about."""
    best = 0
    stack = [(value, 1)]
    while stack:
        current, level = stack.pop()
        if level > best:
            best = level
        if isinstance(current, dict):
            stack.extend((v, level + 1) for v in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((v, level + 1) for v in current)
    return best
