"""The mathematics of rca_core/geometry.py: fit_linear and direction.

AUDIT-2026-10-01. The axis-calibration chain gained cross-engine coverage in the
differential fixture (68 cases), but that compares the two engines against each
other -- it cannot tell whether EITHER of them is right. This pins the
mathematics itself, as properties of least squares rather than as examples.

The implementation's own comment records a near-miss worth pinning: the
three-point path used to guard with ``sxx <= _EPS``, a SPAN-SQUARED quantity
compared against a SPAN threshold, so a legal 1e-7-pixel triple that the
two-point path accepted was rejected as "zero pixel variance". "The two- and
three-point paths now share ONE degeneracy test, the pixel-span check above" is
a factual claim about the code, and a tiny-but-legal span is exactly the input
that tells you whether it is true.

Nothing here encodes how the fit is computed. Each property is something the
result must satisfy for the answer to mean what a calibration says it means.
"""
import math

import pytest

from rca_core.geometry import CalibrationError, fit_linear, _direction_for


def _a(pixel, value):
    return {"pixel": pixel, "value": value}


def _fit(pairs):
    return fit_linear([_a(p, v) for p, v in pairs])


class TestTwoPointExactSolution:
    def test_both_anchors_are_reproduced_exactly(self):
        slope, intercept, residuals = _fit([(0.0, 1.0), (999.0, 24.0)])
        assert slope == pytest.approx(23.0 / 999.0)
        assert intercept == pytest.approx(1.0)
        assert all(abs(r) < 1e-12 for r in residuals), residuals

    def test_the_order_of_two_anchors_does_not_matter(self):
        a = _fit([(0.0, 1.0), (999.0, 24.0)])
        b = _fit([(999.0, 24.0), (0.0, 1.0)])
        assert a[0] == pytest.approx(b[0])
        assert a[1] == pytest.approx(b[1])
        # sorted LISTS, not a set: pytest.approx only takes ordered sequences.
        assert sorted(abs(r) for r in a[2]) == pytest.approx(
            sorted(abs(r) for r in b[2]))


class TestLeastSquares:
    def test_collinear_anchors_give_zero_residuals(self):
        # Truly collinear: the middle point sits ON the line through the outer
        # two, so a correct least-squares fit has nothing left over. (An earlier
        # version of this test used (500, 12.5), which is NOT on the line
        # through (0, 1) and (999, 24) -- that is 12.5115..., and the fit
        # correctly did not reproduce it.)
        pairs = [(0.0, 0.0), (500.0, 11.5), (1000.0, 23.0)]
        slope, intercept, residuals = _fit(pairs)
        assert slope == pytest.approx(0.023)
        assert intercept == pytest.approx(0.0)
        assert all(abs(r) < 1e-9 for r in residuals), residuals

    def test_residuals_are_observed_minus_fitted(self):
        pairs = [(0.0, 1.0), (300.0, 9.0), (700.0, 14.0), (999.0, 25.0)]
        slope, intercept, residuals = _fit(pairs)
        for (pixel, value), r in zip(pairs, residuals):
            assert r == pytest.approx(value - (slope * pixel + intercept)), (
                f"residual at pixel {pixel} is {r}, which is not "
                f"observed - fitted = {value - (slope * pixel + intercept)}")

    def test_the_fit_satisfies_the_normal_equations(self):
        """Sum((p - mean_p) * residual) == 0 is the stationary condition of
        least squares. Checking it is what distinguishes a least-squares fit
        from a plausible-looking line through the same points."""
        pairs = [(0.0, 1.0), (300.0, 9.0), (700.0, 14.0), (999.0, 25.0)]
        slope, intercept, residuals = _fit(pairs)
        mean_p = sum(p for p, _ in pairs) / len(pairs)
        normal = sum((p - mean_p) * r for (p, _), r in zip(pairs, residuals))
        assert abs(normal) < 1e-9, (
            f"the fit is not the least-squares solution: the normal equation "
            f"sums to {normal}")

    def test_no_nearer_line_exists(self):
        """Perturbing the slope or intercept in either direction must make the
        total squared error worse."""
        pairs = [(0.0, 1.0), (300.0, 9.0), (700.0, 14.0), (999.0, 25.0)]
        slope, intercept, residuals = _fit(pairs)

        def sse(m, b):
            return sum((v - (m * p + b)) ** 2 for p, v in pairs)

        best = sse(slope, intercept)
        for dm in (-1e-3, -1e-6, 1e-6, 1e-3):
            for db in (-1e-3, -1e-6, 1e-6, 1e-3):
                assert sse(slope + dm, intercept + db) >= best - 1e-12, (
                    f"moving the fit to ({slope + dm}, {intercept + db}) did not "
                    "worsen the total squared error, so this is not the "
                    "least-squares solution")

    def test_the_anchor_order_does_not_change_the_fit(self):
        # A model emits its anchors in whatever order it likes; an
        # order-dependent fit would make the same four anchors calibrate
        # differently depending on nothing.
        pairs = [(0.0, 1.0), (300.0, 9.0), (700.0, 14.0), (999.0, 25.0)]
        base = _fit(pairs)
        for order in ([2, 0, 3, 1], [3, 2, 1, 0], [1, 3, 0, 2]):
            other = _fit([pairs[i] for i in order])
            assert other[0] == pytest.approx(base[0]), order
            assert other[1] == pytest.approx(base[1]), order
            assert sorted(abs(r) for r in other[2]) == pytest.approx(
                sorted(abs(r) for r in base[2])), order

    def test_a_repeated_anchor_does_not_break_the_fit(self):
        pairs = [(0.0, 1.0), (500.0, 12.5), (500.0, 12.5), (999.0, 24.0)]
        slope, _intercept, residuals = _fit(pairs)
        assert math.isfinite(slope)
        assert all(math.isfinite(r) for r in residuals)


class TestTheSharedDegeneracyTest:
    """The claim under test: a TINY BUT LEGAL pixel span is accepted, and the
    two-point and three-point paths refuse the SAME degenerate sets.

    This is where the recorded unit mismatch lived -- ``sxx <= _EPS`` is a
    span-SQUARED compared against a span threshold, so a 1e-7-pixel triple was
    rejected while the two-point path took it.
    """

    def test_a_tiny_but_legal_two_point_span_is_accepted(self):
        slope, _i, residuals = _fit([(500.0, 1.0), (500.0 + 1e-7, 2.0)])
        assert math.isfinite(slope)
        assert all(abs(r) < 1e-6 for r in residuals), residuals

    def test_a_tiny_but_legal_three_point_span_is_accepted_too(self):
        # THE regression: this is the input the sxx <= _EPS guard refused.
        slope, _i, residuals = _fit([(500.0, 1.0), (500.0 + 5e-8, 1.5),
                                     (500.0 + 1e-7, 2.0)])
        assert math.isfinite(slope), "a 1e-7-pixel triple was refused"
        assert all(math.isfinite(r) for r in residuals)

    @pytest.mark.parametrize("pairs", [
        [(500.0, 1.0), (500.0, 2.0)],                 # same pixel, 2 points
        [(500.0, 1.0), (500.0, 2.0), (500.0, 3.0)],   # same pixel, 3 points
        [(0.0, 7.0), (500.0, 7.0)],                    # same value, 2 points
        [(0.0, 7.0), (500.0, 7.0), (999.0, 7.0)],    # same value, 3 points
    ])
    def test_both_paths_refuse_the_same_degenerate_sets(self, pairs):
        with pytest.raises(CalibrationError):
            _fit(pairs)

    @pytest.mark.parametrize("pairs", [[], [(0.0, 1.0)]])
    def test_fewer_than_two_anchors_is_refused(self, pairs):
        with pytest.raises(CalibrationError):
            _fit(pairs)


class TestDirection:
    """data grows with the pixel -> "direct"; it shrinks -> "reversed"."""

    @pytest.mark.parametrize("slope,expected", [
        (1.0, "direct"), (0.023, "direct"), (-0.023, "reversed"),
        (-1.0, "reversed"), (0.0, "direct"),
    ])
    def test_the_sign_of_the_slope_decides(self, slope, expected):
        assert _direction_for(slope) == expected

    def test_a_deeper_axis_comes_out_reversed(self):
        # The prompt's chemical-stratigraphy example is at_0=30.0, at_999=0.0
        # measured from the top, so depth INCREASES as the pixel coordinate
        # decreases -- the axis is reversed, and saying "direct" would invert
        # the whole figure.
        slope, _i, _r = _fit([(0.0, 30.0), (999.0, 0.0)])
        assert slope < 0
        assert _direction_for(slope) == "reversed"
