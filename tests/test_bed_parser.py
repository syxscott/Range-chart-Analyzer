"""Regression tests for the Phase E shared bed parser.

Pre-Phase E, two independent ``_parse_bed`` implementations existed
and disagreed on subscript handling — both Bed 23c and Bed 23d were
both reduced to the integer 23 in eval_metrics, inflating accuracy.
Phase E unifies them through rca_core.bed_parser.
"""

import pytest


class TestSharedBedParser:
    def test_bed_with_prefix_and_subscript(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("Bed 23c")
        assert info == {"bed_num": 23, "bed_sub": "c", "raw": "Bed 23c"}

    def test_bed_with_prefix_no_subscript(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("Bed 23")
        assert info == {"bed_num": 23, "bed_sub": "", "raw": "Bed 23"}

    def test_bare_with_subscript(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("23c")
        assert info == {"bed_num": 23, "bed_sub": "c", "raw": "23c"}

    def test_bare_digit(self):
        from rca_core.bed_parser import parse_bed
        info = parse_bed("23")
        assert info == {"bed_num": 23, "bed_sub": "", "raw": "23"}

    def test_empty_returns_none(self):
        from rca_core.bed_parser import parse_bed
        assert parse_bed("") is None
        assert parse_bed(None) is None

    def test_unparseable_returns_none(self):
        from rca_core.bed_parser import parse_bed
        # '' without digit should not match
        assert parse_bed("Bed") is None
        assert parse_bed("abc") is None

    def test_parse_bed_int_strips_subscript(self):
        """The legacy eval_metrics signature returned an int;
        parse_bed_int preserves that contract while keeping subscript
        available via parse_bed."""
        from rca_core.bed_parser import parse_bed_int, parse_bed
        assert parse_bed_int("Bed 23c") == 23
        assert parse_bed_int("Bed 23d") == 23
        # But parse_bed preserves the disagreement so the operator
        # can see Bed 23c != Bed 23d downstream.
        assert parse_bed("Bed 23c")["bed_sub"] != parse_bed("Bed 23d")["bed_sub"]

    def test_exporter_and_eval_metrics_now_agree_on_subscript(self):
        """The original divergence — exporter preserved Bed 23c's
        subscript while eval_metrics dropped it — is gone. Both
        call sites should produce the same bed_num for identical
        inputs."""
        from rca_core.exporter import _parse_bed as exporter_parse_bed
        from rca_core.bed_parser import parse_bed as shared_parse_bed
        for s in ["Bed 23c", "Bed 23d", "Bed 23", "23c", "Bed"]:
            a = exporter_parse_bed(s)
            b = shared_parse_bed(s)
            assert a == b, f"divergence on {s!r}: {a!r} vs {b!r}"


class TestAxisOrdinalsRCA20261017:
    """RCA-2026-10-17 (P2-2): ``ordinal_kind`` / ``label_ordinal`` contradicted
    their own docstrings.

    Both are consulted by the exporter's ONLY export-blocking invariant
    (``range_base_le_range_top``, via ``label_ordinal``) and by the extractor's
    endpoint-label/index swap heuristic (via both), so a wrong verdict either
    blocks a valid result or lets an inverted one through.
    """

    # ---- ordinal_kind -------------------------------------------------

    @pytest.mark.parametrize("label", ["Bed 23", "Bed 23c", "23c", "23", "Bed 7"])
    def test_known_bed_spellings_are_number_kind(self, label):
        """"Bed 23" used to answer "letter": the r"\\b[A-Za-z]{3,}\\b" word test
        matched the word "Bed" and ran BEFORE any numeric check, so it also
        overrode the function's own documented example. A bed endpoint and a
        letter-prefixed sample endpoint landed in different families and were
        never cross-checked against each other."""
        from rca_core.bed_parser import ordinal_kind
        assert ordinal_kind(label) == "number"

    @pytest.mark.parametrize("label", ["A1", "A21", "Sample 3", "B1 4C"])
    def test_letter_prefixed_labels_stay_letter_kind(self, label):
        """The counterpart: parse_bed does not accept these, so the
        letter-prefix rule must still own them. Guards against "fixing"
        "Bed 23" by widening the number branch until it swallowed A1.

        "87 B1 4C" is deliberately NOT in this list even though it reads as a
        letter-prefixed sample: it STARTS with the section number 87, so
        ``_LETTER_PREFIX_RE`` never matched and it was already "number" before
        this fix (verified against the pre-fix module). It stays that way."""
        from rca_core.bed_parser import ordinal_kind
        assert ordinal_kind(label) == "letter"

    @pytest.mark.parametrize("label", ["607 m", "1.7 Ma", "", None])
    def test_unit_and_empty_labels_have_no_kind(self, label):
        from rca_core.bed_parser import ordinal_kind
        assert ordinal_kind(label) is None

    def test_kind_is_stable_for_a_bed_spelled_two_ways(self):
        """Both spellings of the same bed must classify identically, or the
        comparison silently depends on which one the model happened to emit."""
        from rca_core.bed_parser import ordinal_kind
        assert ordinal_kind("Bed 23") == ordinal_kind("23") == "number"

    # ---- label_ordinal: foreign semantic words ------------------------

    @pytest.mark.parametrize(
        "label",
        ["zone Z1", "zone 3", "Fig. 4", "Zone A-1", "zone-3"],
    )
    def test_foreign_semantic_words_have_no_ordinal(self, label):
        """``label_ordinal``'s docstring has always promised None for
        "zone Z1"; the implementation answered 1 because it counted any
        digits. The number in these labels belongs to something else — a zone,
        a figure — so reading it as the sample's position compares two
        unrelated counters."""
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal(label) is None, (
            f"{label!r} names a zone/figure, not a sample position"
        )

    # ---- label_ordinal: ranges ---------------------------------------

    @pytest.mark.parametrize(
        "label", ["23-25", "23 – 25", "23–25", "23—25", "Bed 7-Bed 11", "A1-A5"]
    )
    def test_ranges_have_no_single_ordinal(self, label):
        """A range has no single position, and the old "last number" rule
        answered its FAR END -- 25 for "23-25" -- so a two-endpoint check asked
        "is 25 above 23?", answered yes, and reported an inversion in a cell
        that was never inverted."""
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal(label) is None

    def test_hyphenated_labels_that_are_not_ranges_keep_their_ordinal(self):
        """The range guard must not swallow every hyphen: "B1 4C" has none and
        "A1 (607 m)" is a sample plus an annotated depth."""
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal("B1 4C") == 4
        assert label_ordinal("A1 (607 m)") == 1

    # ---- AUDIT-2026-10-17: the two guards above were too wide -----------
    #
    # The P2-2 fix was written against a handful of made-up labels and was
    # never checked against the label styles the AUDIT-2026-10-15 work was
    # actually built for. It answered None for all three of these, where the
    # PRE-FIX code read a position:
    #
    #     "87 B1-4C"        None   (was 4)  composite well/interval id
    #     "87 JED 8"        None   (was 8)  well name + sample number
    #     "CAASP-84-20/15"  None   (was 15) institute + id / sample
    #
    # The consequence was silent and total for that figure class: both
    # `label_ordinal` consumers (the exporter's only export-blocking invariant,
    # and the extractor's endpoint-swap heuristic) stop working, so inverted
    # endpoints go unreported — the exact defect AUDIT-2026-10-15 closed.

    @pytest.mark.parametrize("label,expected", [
        ("87 B1-4C", 4),        # hyphen separates, does not span
        ("87 JED 8", 8),        # JED is a well name, not a semantic word
        ("CAASP-84-20/15", None),  # 15 is a sample id inside the identifier
    ])
    def test_real_well_log_labels_are_not_mistaken_for_ranges_or_prose(
            self, label, expected):
        """The conservative direction is explicit here: ``CAASP-84-20/15``
        stays None on purpose (its trailing 15 is an id, not a position), while
        the two whose trailing number IS the sample position keep it.

        A guard that refuses every unfamiliar label looks safe and is not — it
        turns the endpoint check off for every figure drawn in that style, and
        a check that never runs reports nothing while looking clean.
        """
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal(label) == expected

    def test_an_unrecognised_word_is_an_identifier_not_a_refusal(self):
        """The foreign-word test is a CLOSED list, not "anything unfamiliar".

        Well names, institute codes and operator names are arbitrary, so an
        open-ended rule cannot enumerate them; treating each as "the number
        means something else" made real labels unreadable. The closed list
        keeps the case the fix was for ("zone Z1", "Fig. 4") and leaves
        everything else alone.
        """
        from rca_core.bed_parser import _has_foreign_semantic_word
        for identifier_word in ("JED", "CAASP", "OWO", "Brazos", "TST"):
            assert not _has_foreign_semantic_word(f"{identifier_word} 12"), (
                f"{identifier_word!r} is an identifier, not a semantic word")
        for semantic in ("zone Z1", "Fig. 4", "Section B", "plate 3"):
            assert _has_foreign_semantic_word(semantic), (
                f"{semantic!r} names something other than a sample position")

    # ---- label_ordinal: the readings that must NOT regress -----------

    @pytest.mark.parametrize(
        "label,expected",
        [
            ("A1", 1),          # Diaz & Velez Fig. 5 sample series
            ("A21", 21),
            ("Sample 3", 3),
            ("Bed 23", 23),
            ("23c", 23),
            ("87 B1 4C", 4),    # LAST number: the bed, not the section
            ("1.7", 1),         # a Ma value: monotonic, usable
        ],
    )
    def test_readable_labels_still_return_their_ordinal(self, label, expected):
        """The fix is subtractive. Every style the function existed to read
        must still read identically -- this is the behaviour the two call
        sites depend on when they decide a result is checkable at all."""
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal(label) == expected

    @pytest.mark.parametrize(
        "label", ["607 m", "1.7 Ma", "", None, "Bed 12 Ma"])
    def test_unreadable_labels_stay_none(self, label):
        """Depths and ages carry a unit and were already refused. Unchanged."""
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal(label) is None

    def test_non_string_inputs_are_unaffected(self):
        """The numeric / bool / NaN guards predate this fix and must survive
        it -- NaN reaches here through the exporter's NaN-sanitisation tests,
        so this is a live path rather than a theoretical one."""
        from rca_core.bed_parser import label_ordinal
        assert label_ordinal(3) == 3
        assert label_ordinal(0) == 0
        assert label_ordinal(float("nan")) is None
        assert label_ordinal(float("inf")) is None
        assert label_ordinal(True) is None

    # ---- the two functions must agree with each other ----------------

    @pytest.mark.parametrize("label", ["Bed 23", "23c", "A1", "Sample 3", "4"])
    def test_kind_and_ordinal_are_consistent_for_readable_labels(self, label):
        """A caller may use either verdict independently, so they must not
        disagree about whether a label is readable. This is the invariant the
        "Bed 23 -> letter" bug broke."""
        from rca_core.bed_parser import label_ordinal, ordinal_kind
        assert (label_ordinal(label) is not None) == (ordinal_kind(label) is not None)

    def test_a_ranged_endpoint_is_never_compared_as_inverted(self):
        """The end-to-end consequence, in the shape the exporter uses it: a
        base/top pair of "23-25" / "5" must produce NO ordinal verdict, so no
        inversion is reported from a cell that carries a range."""
        from rca_core.bed_parser import label_ordinal
        base, top = "23-25", "5"
        bo, to = label_ordinal(base), label_ordinal(top)
        assert bo is None, "a range must not yield a comparable ordinal"
        # The exporter only reports an inversion when BOTH sides are readable.
        assert not (bo is not None and to is not None and bo != to)
