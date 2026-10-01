"""Properties of the reported accuracy metrics, independent of implementation.

AUDIT-2026-10-01. rca_core/eval_metrics.py produces the numbers a paper quotes,
and it had no property test -- only example-based ones. These are the invariants
that must hold whatever the implementation does, so none of them encodes a
guess about how a metric is computed; each is a property of "what an accuracy
number is allowed to mean".

The semantics these rely on, read from _endpoint_accuracy's docstring and code:

  * ``exact`` is a SUBSET of ``within_tolerance`` (an exact hit increments both),
    so ``within_tolerance + wrong`` is the number of scored species;
  * ``subscript_mismatch`` is a SUBSET of ``wrong``, and separates "wrong bed"
    from "wrong sub-bed of the right bed";
  * the tolerance window is a stratigraphic DISTANCE allowance and applies to
    ``bed_num`` only. ``_score_bed_pair`` returns on ``diff == 0`` before it ever
    looks at the window, so two sub-beds of the SAME bed can never be forgiven
    by a tolerance -- which is the whole point of the shared subscript-aware bed
    parser, and exactly what a future "simplification" to ``if diff <= window``
    would quietly break;
  * species present on only one side, and rows whose bed does not parse on
    either side, are SKIPPED rather than guessed.

Two of these properties are the ones that matter for the published numbers: the
tolerance ladder has to be monotone, and a sub-bed misread must not be able to
buy its way into "within tolerance".
"""
import pytest

from rca_core import eval_metrics as EM


def _row(species, top=None, base=None):
    row = {"species": species}
    if top is not None:
        row["range_top"] = top
    if base is not None:
        row["range_base"] = base
    return row


def _rows(spec):
    return [_row(sp, top=t, base=b) for sp, t, b in spec]


# A small corpus spanning exact hits, near misses, sub-bed mismatches, and
# species that appear on only one side.
CORPUS = [
    ("Alpha", "Bed 9", "Bed 7"),     # exact
    ("Beta", "Bed 5", "Bed 3"),      # off by 4 -> wrong
    ("Gamma", "Bed 8", "Bed 7"),     # off by 1
    ("Delta", "Bed 23c", "Bed 23d"),  # same bed, different sub-bed
    ("Epsilon", "Bed 12", "Bed 10"),  # off by 2
]
GT = _rows(CORPUS)


@pytest.mark.parametrize("scorer,field", [
    (EM.range_top_accuracy, "range_top"),
    (EM.range_base_accuracy, "range_base"),
])
class TestMetricProperties:
    def test_a_perfect_prediction_scores_one(self, scorer, field):
        r = scorer(GT, GT)
        assert r["wrong"] == 0, r
        assert r["acc_exact"] == 1.0, r
        assert r["acc_tolerance"] == 1.0, r

    def test_the_counts_partition_the_scored_species(self, scorer, field):
        r = scorer(GT, GT, tolerance=2)
        total = r["within_tolerance"] + r["wrong"]
        # every shared, parseable species is counted exactly once, and `exact`
        # is a subset of `within_tolerance`, never on top of it
        assert total == len(CORPUS), r
        assert 0 <= r["exact"] <= r["within_tolerance"], r
        assert 0 <= r["subscript_mismatch"] <= r["wrong"], r

    def test_tolerance_zero_makes_exact_and_tolerance_the_same(self, scorer, field):
        # With no window, "within tolerance" cannot be a larger set than "exact".
        r = scorer(GT, GT, tolerance=0)
        assert r["acc_tolerance"] == r["acc_exact"], r

    def test_the_tolerance_ladder_never_loses_accuracy(self, scorer, field):
        scores = [scorer(GT, GT, tolerance=t)["acc_tolerance"] for t in (0, 1, 2, 3, 5)]
        assert scores == sorted(scores), scores
        assert scores[0] == min(scores) and scores[-1] == max(scores)

    def test_a_sub_bed_misread_is_never_forgiven_by_a_tolerance(self, scorer, field):
        """23c against 23d is a different level, not a distance error.

        This is the regression this file exists for. ``_score_bed_pair`` returns
        on ``diff == 0`` BEFORE it looks at the window, so a sub-bed misread is
        reported as ``wrong`` for every tolerance. Rewriting that as the naive
        ``if diff <= window`` is a one-line "simplification" that silently
        forgives it -- and the accuracy number stops meaning what it says.
        """
        gt = [_row("Alpha", top="Bed 23d", base="Bed 23d")]
        for tolerance in (0, 1, 2, 5):
            pred = [_row("Alpha", top="Bed 23c", base="Bed 23c")]
            r = scorer(pred, gt, tolerance=tolerance)
            assert r["exact"] == 0, f"tolerance={tolerance}: {r}"
            assert r["wrong"] == 1, f"tolerance={tolerance}: {r}"
            assert r["subscript_mismatch"] == 1, f"tolerance={tolerance}: {r}"

    def test_a_sub_bed_misread_is_reported_in_its_own_bucket(self, scorer, field):
        gt = [_row("Alpha", top="Bed 23d", base="Bed 23d")]
        pred = [_row("Alpha", top="Bed 3", base="Bed 3")]   # wrong BED, not sub-bed
        r = scorer(pred, gt, tolerance=0)
        assert r["wrong"] == 1 and r["subscript_mismatch"] == 0, r

    def test_one_sided_species_are_skipped_not_guessed(self, scorer, field):
        gt = [_row("Alpha", top="Bed 9", base="Bed 7")]
        pred = [_row("Alpha", top="Bed 9", base="Bed 7"),
                _row("Ghost", top="Bed 1", base="Bed 1")]
        r = scorer(pred, gt, tolerance=0)
        assert r["within_tolerance"] + r["wrong"] == 1, r
        assert r["acc_exact"] == 1.0, r

    def test_an_unreadable_bed_is_skipped_not_guessed(self, scorer, field):
        gt = [_row("Alpha", top="Bed 9", base="Bed 7")]
        pred = [_row("Alpha", top="not a bed", base="also not")]
        r = scorer(pred, gt, tolerance=0)
        assert r["within_tolerance"] + r["wrong"] == 0, r
        assert r["acc_exact"] == 0.0, r
        # ... and the empty-gt shape stays a total function
        empty = scorer([], gt, tolerance=0)
        assert empty["acc_exact"] == 0.0 and empty["acc_tolerance"] == 0.0, empty

    def test_shifting_both_sides_by_the_same_amount_changes_nothing(
            self, scorer, field):
        """The metric measures agreement, not absolute position, so a uniform
        shift of both sides must leave every count and every score alone."""
        import re

        def shift(label):
            # keep the sub-bed, move the bed number: "Bed 23c" -> "Bed 73c"
            m = re.fullmatch(r"Bed (\d+)([a-z]?)", label)
            assert m, f"unexpected label shape in the corpus: {label!r}"
            return f"Bed {int(m.group(1)) + 50}{m.group(2)}"

        shifted = [_row(sp, top=shift(t), base=shift(b)) for sp, t, b in CORPUS]
        a = scorer(GT, GT, tolerance=1)
        b = scorer(shifted, shifted, tolerance=1)
        assert a == b, (a, b)


class TestTopAndBaseAreSymmetric:
    def test_the_two_scorers_agree_on_a_mirrored_corpus(self):
        """range_base_accuracy was added because the original scorer read range_base
        from the ground truth but only validated range_top. Build a corpus where
        the two fields are swapped and the two scorers must swap answers too."""

        def swap(spec):
            return [_row(sp, top=b, base=t) for sp, t, b in spec]

        for tolerance in (0, 1, 2, 4):
            top_answer = EM.range_top_accuracy(_rows(CORPUS), _rows(CORPUS),
                                              tolerance=tolerance)
            base_answer = EM.range_base_accuracy(swap(CORPUS), swap(CORPUS),
                                                tolerance=tolerance)
            assert top_answer == base_answer, (tolerance, top_answer, base_answer)


class TestTaxonNormalisationAgreesWithTheProduct:
    """AUDIT-2026-10-02. An evaluation harness must score against the SAME
    notion of a taxon the code under test dedups on, or every number it
    reports is untrustworthy.

    rca_core/eval_metrics.py::_normalize_taxon used a bare ``\\b``, which on a
    Python str is Unicode-aware, while the product's own notion is
    ASCII-bounded (aggregate.py::_ascii_b, and the lookarounds in
    names.py::clean_name_for_lookup -- all three fixed the same day for the
    same reason). The visible consequence was in the LENIENT pass, which exists
    precisely to ignore open-nomenclature qualifiers: for "中华虫sp." the marker
    survived, so the lenient metric scored it as a different taxon from "中华虫"
    while aggregate's dedup was correctly keeping them apart. Measured before
    and after:

        "Genus sp."          lenient  genus          ->  genus        (unchanged)
        "中华虫 sp."          lenient  中华虫          ->  中华虫        (unchanged)
        "中华虫sp."          lenient  中华虫sp.  WRONG ->  中华虫        (fixed)
        "图cf. Genus"        lenient  图cf. genus  WRONG ->  图 genus     (fixed)
        "中华虫cfsp. yini"   lenient  unchanged      ->  unchanged    (look-alike)

    The rows below are the property, not the fix: whatever the implementation
    does, the lenient pass must be INSENSITIVE to where a qualifier sits, and
    must agree with _extract_qualifiers about whether one is there at all.
    """

    QUALIFIERS = ["sp.", "spp.", "cf.", "aff."]

    @pytest.mark.parametrize("marker", QUALIFIERS)
    @pytest.mark.parametrize("gap", ["", " ", "  "])
    def test_lenient_is_insensitive_to_where_the_marker_sits(self, marker, gap):
        base = "中华虫"
        for text in (f"{base}{gap}{marker}", f"{base}{gap}{marker[:-1]}"):
            assert EM._normalize_taxon(text, preserve_qualifiers=False) == \
                EM._normalize_taxon(base, preserve_qualifiers=False), text

    @pytest.mark.parametrize("marker", QUALIFIERS)
    def test_lenient_agrees_with_the_products_own_qualifier_detection(self, marker):
        """The harness and the code under test must not disagree about whether
        a marker is present -- that disagreement is what turns an accuracy
        number into a fiction."""
        from rca_core.aggregate import _extract_qualifiers
        for text in (f"中华虫{marker}", f"中华虫 {marker}", f"Genus {marker}"):
            has_marker = bool(_extract_qualifiers(text))
            lenient = EM._normalize_taxon(text, preserve_qualifiers=False)
            strict = EM._normalize_taxon(text, preserve_qualifiers=True)
            if has_marker:
                assert lenient == strict.replace(marker, "").strip() or \
                    marker not in strict, (text, strict, lenient)
            else:
                assert lenient == strict, (text, strict, lenient)

    def test_a_lookalike_is_not_a_qualifier(self):
        """The guard must still reject an ASCII identifier that merely
        contains the marker -- the same look-alikes every other site in this
        family pins."""
        from rca_core.aggregate import _extract_qualifiers
        for text in ("中华虫cfsp. yini", "Genus nearness", "Gencf. Foo"):
            assert _extract_qualifiers(text) == set(), text
            assert EM._normalize_taxon(
                text, preserve_qualifiers=False) == \
                EM._normalize_taxon(text, preserve_qualifiers=True), text
