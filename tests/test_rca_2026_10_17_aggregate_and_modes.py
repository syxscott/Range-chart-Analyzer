"""RCA-2026-10-17: merge-engine robustness + module-surface honesty.

Four defects, all reproduced before being fixed and all pinned here:

* P2-1``rca_core/modes.py`` exported names in ``__all__`` that do not exist on
  the module. A ``from rca_core.modes import *`` raises ``AttributeError`` on
  the missing name, so the dead entry breaks the one thing ``__all__`` is for.
  ``modes.py`` is owned by another workstream, so this file only ASSERTS the
  property -- the test goes green the moment that file is corrected, and until
  then it is the executable statement of what is wrong with it.
* P3-1 ``_extract_qualifiers`` raised ``TypeError`` on a non-string, aborting
  the whole merge. js/aggregate.js used ``re.exec(String(s))``, so the browser
  survived input that crashed the desktop build.
* P3-2 ``sections`` counted one run's duplicate section twice, producing
  ``agreement="3/2"`` -- more agreement than there were runs.
* P3-3 a field that was numeric in every run came back as TEXT, because
  ``_merge_scalar_field`` returned its vote key instead of the value.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# P2-1: modes.__all__ must only name things that exist
# ---------------------------------------------------------------------------

class TestModesAllNamesExist:
    """``__all__`` is a contract, and a contract that lies is worse than none:
    ``from rca_core.modes import *`` raises ``AttributeError`` naming the
    missing symbol, which reads like a typo in the CALLER rather than a dead
    entry in the module."""

    def test_every_name_in_dunder_all_resolves(self):
        import rca_core.modes as m

        missing = [n for n in m.__all__ if not hasattr(m, n)]
        assert not missing, (
            "rca_core/modes.__all__ advertises names that do not exist on the "
            f"module: {missing}. Either define them or drop them from __all__ "
            "(a `from rca_core.modes import *` raises AttributeError on each)."
        )

    def test_classification_prompt_version_is_exported(self):
        """The specific dead name reported in RCA-2026-10-17. Asserted
        separately from the sweep above so that fixing one entry does not
        quietly satisfy the other assertion."""
        import rca_core.modes as m

        assert hasattr(m, "CLASSIFICATION_PROMPT_VERSION"), (
            "rca_core.modes.CLASSIFICATION_PROMPT_VERSION is referenced by "
            "callers and listed in __all__ but is not defined on the module"
        )

    def test_all_is_not_empty_and_is_a_sequence_of_strings(self):
        import rca_core.modes as m

        assert isinstance(m.__all__, (list, tuple))
        assert m.__all__, "an empty __all__ would silently export nothing"
        assert all(isinstance(n, str) for n in m.__all__)


# ---------------------------------------------------------------------------
# P3-1: non-string values must not crash the merge
# ---------------------------------------------------------------------------

class TestNonStringSpeciesDoesNotAbortMerge:
    """A model can emit a number, a bool or an object where a taxon NAME is
    expected. That is malformed input, and the correct response is "merge the
    rest of the payload", not "raise". Before the fix
    ``pattern.search(123)`` raised ``TypeError: expected string or bytes-like
    object`` from inside the dedup key, taking the entire merge with it --
    while js/aggregate.js, which guards with ``re.exec(String(s))``, returned a
    result for the same input."""

    def test_integer_species_merges_instead_of_raising(self):
        from rca_core.aggregate import merge_results
        runs = [{"species_ranges": [{"species": 123, "section": "S1"}]},
                {"species_ranges": [{"species": 123, "section": "S1"}]}]
        merged = merge_results(runs)
        rows = merged["species_ranges"]
        assert len(rows) == 1
        assert rows[0]["species"] == 123

    def test_dict_species_merges_instead_of_raising(self):
        """A container is still not a usable name, but it must not take the
        merge down: the payload around it is still worth returning."""
        from rca_core.aggregate import merge_results
        runs = [{"species_ranges": [{"species": {"nested": "x"}, "section": "S1"}]},
                {"species_ranges": [{"species": {"nested": "x"}, "section": "S1"}]}]
        merged = merge_results(runs)
        assert isinstance(merged, dict)

    def test_extract_qualifiers_accepts_non_strings_directly(self):
        from rca_core.aggregate import _extract_qualifiers
        for value in (123, 1.5, True, ["a", "b"], {"a": 1}, (1, 2), object()):
            assert _extract_qualifiers(value) == frozenset()

    def test_qualifiers_are_still_found_in_a_string_that_looks_numeric(self):
        """The str() must not become a blanket "give up": a stringified
        payload that DOES carry a marker still contributes it, or the dedup
        key would collapse "Genus sp." into "Genus"."""
        from rca_core.aggregate import _extract_qualifiers
        assert _extract_qualifiers("Genus sp.") == frozenset({"sp."})
        assert _extract_qualifiers("Genus spp.") == frozenset({"spp."})
        assert _extract_qualifiers("Genus cf. beta") == frozenset({"cf."})

    def test_mixed_string_and_non_string_runs_do_not_raise(self):
        from rca_core.aggregate import merge_results
        runs = [{"species_ranges": [{"species": "Genus", "section": "S1"}]},
                {"species_ranges": [{"species": 7, "section": "S1"}]}]
        assert isinstance(merge_results(runs), dict)

    def test_a_row_with_no_usable_name_is_still_dropped(self):
        """The existing guard must survive the str() coercion: a row whose
        identity fields are all empty must not survive on the strength of an
        ``author_year``, which is not a name."""
        from rca_core.aggregate import RANGE_CHART_SCHEMA, merge_results
        runs = [{"species_ranges": [{
            "species": "", "section": "",
            "author_year": "Smith 1950",
            "range_base": "Bed 7", "range_top": "Bed 9"}]}] * 2
        merged = merge_results(runs, None, RANGE_CHART_SCHEMA)
        assert merged["species_ranges"] == []


# ---------------------------------------------------------------------------
# P3-2: agreement can never exceed the number of runs
# ---------------------------------------------------------------------------

class TestSectionAgreementCountsRunsNotRows:
    """``_merge_primary_list`` has always deduplicated within a run for exactly
    this reason (see its ``seen_in_run`` comment: "count it once so
    agreement_count can't exceed n"). The ``sections`` path had no such guard,
    so one run listing a section twice produced ``agreement="3/2"`` from two
    runs -- a self-contradictory string that any consumer filtering on
    ``agreement_count <= runs`` rejects, while species rows merged in the same
    call could never produce one."""

    def _sections(self, runs):
        from rca_core.aggregate import merge_results
        return merge_results(runs)["sections"]

    def test_duplicate_section_in_one_run_counts_once(self):
        secs = self._sections([
            {"sections": [{"name": "S1"}, {"name": "S1"}]},
            {"sections": [{"name": "S1"}]},
        ])
        assert len(secs) == 1
        assert secs[0]["agreement_count"] == 2
        assert secs[0]["agreement"] == "2/2"

    @pytest.mark.parametrize("duplicate_count", [2, 3, 5])
    def test_agreement_never_exceeds_runs(self, duplicate_count):
        runs = [{"sections": [{"name": "S1"}] * duplicate_count} for _ in range(3)]
        merged_secs = self._sections(runs)
        runs_n = 3
        for sec in merged_secs:
            assert sec["agreement_count"] <= runs_n, (
                f"agreement_count {sec['agreement_count']} exceeds {runs_n} runs"
            )
            count, total = sec["agreement"].split("/")
            assert int(count) <= int(total) == runs_n

    def test_a_section_reported_by_every_run_still_counts_each_run(self):
        """Dedup is per RUN, not global: three runs reporting S1 must still
        show 3/3, or genuine cross-run agreement would be erased."""
        runs = [{"sections": [{"name": "S1"}]} for _ in range(3)]
        secs = self._sections(runs)
        assert secs[0]["agreement_count"] == 3
        assert secs[0]["agreement"] == "3/3"

    def test_duplicate_rows_do_not_lose_their_formations(self):
        """The count is about RUNS; the DATA is about observations. Two rows
        for one section in one run carrying different formations must still
        union both -- otherwise the fix would trade a wrong number for silent
        data loss."""
        secs = self._sections([
            {"sections": [{"name": "S1", "formations": ["Fm A"]},
                          {"name": "S1", "formations": ["Fm B"]}]},
            {"sections": [{"name": "S1"}]},
        ])
        assert secs[0]["formations"] == ["Fm A", "Fm B"]
        assert secs[0]["agreement_count"] == 2

    def test_distinct_sections_are_not_collapsed(self):
        runs = [{"sections": [{"name": "S1"}, {"name": "S2"}]},
                {"sections": [{"name": "S1"}, {"name": "S2"}]}]
        secs = self._sections(runs)
        assert sorted(s["name"] for s in secs) == ["S1", "S2"]

    def test_species_and_sections_agree_on_the_same_invariant(self):
        """The two paths live in one function and must obey one rule."""
        from rca_core.aggregate import merge_results
        runs = [
            {"species_ranges": [{"species": "Genus", "section": "S1"}],
             "sections": [{"name": "S1"}, {"name": "S1"}]},
            {"species_ranges": [{"species": "Genus", "section": "S1"}],
             "sections": [{"name": "S1"}]},
        ]
        merged = merge_results(runs)
        n = merged["runs"]
        for row in merged["species_ranges"] + merged["sections"]:
            assert row["agreement_count"] <= n


# ---------------------------------------------------------------------------
# P3-3: a consistent type stays that type
# ---------------------------------------------------------------------------

class TestScalarMergePreservesType:
    """``_merge_scalar_field`` builds its vote pool out of ``_str_for_merge``
    strings -- that is correct, and is what makes ``1`` and ``1.0`` ONE
    candidate rather than two. But it was RETURNING the key, so any field
    numeric in every run came back as text: occurrence ``columns[].index``
    merged ``0``/``1`` into ``"0"``/``"1"``, which is invisible in the JSON and
    surfaces as a text cell in the exported XLSX."""

    def test_integer_field_stays_an_integer(self):
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([1, 1]) == 1
        assert isinstance(_merge_scalar_field([1, 1]), int)

    def test_zero_is_preserved_as_a_number_not_dropped(self):
        """0 is a legitimate observation (0 % abundance, an empty part of the
        diagram), so it must survive as a NUMBER."""
        from rca_core.aggregate import _merge_scalar_field
        result = _merge_scalar_field([0, 0])
        assert result == 0
        assert isinstance(result, int)

    def test_integral_float_keeps_its_float_type(self):
        from rca_core.aggregate import _merge_scalar_field
        result = _merge_scalar_field([3.0, 3.0])
        assert result == 3.0
        assert isinstance(result, float)

    def test_fractional_float_is_preserved(self):
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([0.5, 0.7]) == 0.5

    def test_string_field_stays_a_string(self):
        from rca_core.aggregate import _merge_scalar_field
        result = _merge_scalar_field(["a", "a"])
        assert result == "a"

    def test_type_disagreement_degrades_to_string(self):
        """With one run emitting ``8`` and another ``"8"`` there is no single
        honest type, and text is the only representation both agree on. This
        is the ONE case that is knowingly one-sided until js/aggregate.js is
        updated in step (its mergeScalarField coerces unconditionally)."""
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([3, "3"]) == "3"

    def test_int_and_float_of_the_same_value_are_one_vote(self):
        """The vote-key behaviour that was always intended, and the reason the
        bug was easy to miss: 1 and 1.0 must not become a 2-vote "consensus"
        that hides a genuine cross-run split."""
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([1.0, 1]) == 1.0

    def test_bool_pool_still_preserves_bools(self):
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([True, False]) is False
        assert _merge_scalar_field([False, True]) is False

    def test_across_runs_preserves_type(self):
        from rca_core.aggregate import _merge_field_across_runs
        assert _merge_field_across_runs([3.0, 3.0]) == 3.0
        assert _merge_field_across_runs(["a", "a"]) == "a"

    def test_end_to_end_species_row_keeps_numeric_index(self):
        from rca_core.aggregate import merge_results
        runs = [{"species_ranges": [
            {"species": "Genus", "section": "S1", "index": 0},
            {"species": "Genus", "section": "S2", "index": 1},
        ]} for _ in range(2)]
        rows = merge_results(runs)["species_ranges"]
        by_section = {r["section"]: r.get("index") for r in rows}
        assert by_section == {"S1": 0, "S2": 1}
        for value in by_section.values():
            assert isinstance(value, int), "a numeric column became text on merge"

    def test_mapping_field_keeps_numeric_values(self):
        """``_extras`` reaches the scalar merger through
        ``_merge_mapping_field``, so a figure/page number preserved in every
        run must not become text."""
        from rca_core.aggregate import _merge_mapping_field
        result = _merge_mapping_field([{"page": 2}, {"page": 2}])
        assert result["page"] == 2
        assert isinstance(result["page"], int)

    def test_bool_votes_still_ignore_bools_when_a_scalar_is_present(self):
        from rca_core.aggregate import _merge_scalar_field
        assert _merge_scalar_field([True, True, "maybe"]) == "maybe"

    def test_all_none_still_yields_no_merge(self):
        from rca_core.aggregate import _merge_field_across_runs, _NO_MERGE
        assert _merge_field_across_runs([None, None]) is _NO_MERGE
