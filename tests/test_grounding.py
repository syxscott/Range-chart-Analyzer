"""Name grounding: does an extracted name actually appear in the figure?

The defect under test (measured on Motoyama 2019 Fig. 1): four runs on one
image produced 36 / 91 / 45 / 51 zone rows and EVERY run carried names printed
nowhere in it (``Buccinosphaera invaginata``, ``Calocycletta costata``,
``Callimitra rebus``, ...). Those are real Neogene radiolarian names, so the
output is indistinguishable from a good extraction by eye, and
``score_range_chart`` returned grade A / 1.0 / zero issues on all four.

Three properties carry the weight here, and each has its own section:

1. the checker reports ABSENCES and stays quiet when it is unsure, so a wrong
   verifier degrades to "nothing missing" rather than to invented accusations;
2. every failure path is ``not_performed`` and NEVER an empty "clean" list,
   because collapsing those two is the bug being fixed;
3. the signal cannot move the score, so a flaky second model call can never
   outrank the four offline dimensions.

No test in this file touches the network: ``llm_caller`` is injected.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from rca_core.grounding import (  # noqa: E402
    GROUNDING_MAX_TOKENS,
    GROUNDING_NO_NAMES,
    GROUNDING_NOT_PERFORMED,
    GROUNDING_ROW_FLAG,
    GROUNDING_VERIFIED,
    NAME_SOURCES,
    GroundingResult,
    build_verifier_user_text,
    check_name_grounding,
    collect_candidate_names,
    flag_unverified_rows,
)
from rca_core.llm import ApiFormat, LlmProvider  # noqa: E402
from rca_core.quality import score_range_chart  # noqa: E402


class _Stub:
    """A ``call_llm_api``-shaped stand-in. Records kwargs, returns a 5-tuple.

    The real function is keyword-only and never raises, so this mirrors both
    facts; ``raises`` exists to prove that even a caller which DOES raise
    cannot escape ``check_name_grounding``.
    """

    def __init__(self, reply=None, *, status=200, truncated=False,
                 err_body="", usage=None, raises=None):
        self.reply = reply
        self.status = status
        self.truncated = truncated
        self.err_body = err_body
        self.usage = usage
        self.raises = raises
        self.calls = 0
        self.kwargs: dict = {}

    def __call__(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        if self.raises is not None:
            raise self.raises
        return (self.reply, self.truncated, self.status, self.err_body,
                self.usage)


def _provider() -> LlmProvider:
    return LlmProvider(api_format=ApiFormat.ANTHROPIC,
                       endpoint="https://example.invalid",
                       model="stub-model")


def _zonation_result() -> dict:
    """A Motoyama-shaped payload: two printed names and one invented one."""
    return {
        "sections": [{"name": "Motoyama section"}],
        "species_ranges": [
            {"species": "Buccinosphaera invaginata"},
            {"species": "Calocycletta costata"},
            {"species": "Callimitra rebus"},
        ],
    }


def _check(data, stub, **kwargs):
    return check_name_grounding(data=data, image_b64="QUJD",
                                media_type="image/png",
                                provider=_provider(), llm_caller=stub, **kwargs)


# ---------------------------------------------------------------------------
# The name collector: one table, so a mode is covered by adding keys to it
# ---------------------------------------------------------------------------


class TestCollectCandidateNames:
    def test_every_name_bearing_key_in_the_table_is_collected(self):
        data = {
            "species_ranges": [{"species": "R1"}],
            "abundances": [{"taxon": "R2"}],
            "biozones": [{"name": "R3"}],
            "zones": [{"name": "R4"}],
            "zonations": [{"name": "R5"}],
            "occurrences": [{"taxon": "R6"}],
            "columns": [{"label": "R7"}],
            "correlations": [{"from_zone": "R8", "to_zone": "R9"}],
        }
        names, dropped = collect_candidate_names(data)
        assert dropped == 0
        assert names == ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"], (
            "a key in NAME_SOURCES produced no name -- the table and the "
            "collector have drifted apart")

    def test_both_ends_of_a_correlation_edge_are_collected(self):
        names, _ = collect_candidate_names(
            {"correlations": [{"from_zone": "Zone A", "to_zone": "Zone B"}]})
        assert names == ["Zone A", "Zone B"]

    def test_a_column_is_collected_from_either_of_its_two_keys(self):
        # The normalisers write `col.get("label") or col.get("name")`, so a
        # real row carries exactly one of the two. Both spellings must read.
        assert collect_candidate_names({"columns": [{"label": "Locality"}]})[0] \
            == ["Locality"]
        assert collect_candidate_names({"columns": [{"name": "Locality"}]})[0] \
            == ["Locality"]

    def test_the_same_name_under_two_keys_collapses(self):
        # A row whose aliases disagree would otherwise yield the entity twice,
        # and the verifier would be asked about it twice.
        names, _ = collect_candidate_names(
            {"columns": [{"label": "Zone A", "name": "Zone A"}]})
        assert names == ["Zone A"]

    def test_duplicates_collapse_case_and_whitespace_insensitively(self):
        data = {"zones": [{"name": "Zone A"}, {"name": "zone  a"},
                          {"name": "Zone A "}, {"name": "Zone B"}]}
        names, _ = collect_candidate_names(data)
        assert names == ["Zone A", "Zone B"]

    def test_a_mode_with_no_name_keys_collects_nothing(self):
        # Every table here is genuinely absent from NAME_SOURCES. An earlier
        # draft used `sections` as the example -- it no longer qualifies,
        # because AUDIT item 10 added it (range_chart prints a section NAME,
        # columnar_section a section ID) and that addition is the point.
        data = {"cross_beds": [{"from_bed_idx": 1, "to_bed_idx": 2}],
                "lithology_legend": [{"pattern": "brick", "meaning": "sandstone"}],
                "fossil_legend": [{"marker": "*", "meaning": "mollusc"}]}
        names, dropped = collect_candidate_names(data)
        assert names == [] and dropped == 0

    @pytest.mark.parametrize("junk", [None, [], "text", 42, {"zones": "not-a-list"},
                                      {"zones": [None, 7, {"name": None},
                                                 {"name": "   "}]}])
    def test_malformed_payloads_never_raise(self, junk):
        names, dropped = collect_candidate_names(junk)
        assert names == [] or all(isinstance(n, str) for n in names)
        assert dropped == 0

    def test_the_table_names_real_keys_of_the_normalisers(self):
        # Guards against a typo'd row key silently emptying one source table.
        assert ("species_ranges", ("species",)) in NAME_SOURCES
        assert ("zones", ("name",)) in NAME_SOURCES
        assert ("correlations", ("from_zone", "to_zone",
                                 "from_zonation", "to_zonation")) in NAME_SOURCES


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------


class TestAbsencesAreReported:
    def test_names_the_verifier_calls_absent_are_reported(self):
        stub = _Stub('{"absent": ["Calocycletta costata", "Callimitra rebus"]}')
        result = _check(_zonation_result(), stub)
        assert result.status == GROUNDING_VERIFIED
        # 3 species_ranges + 1 sections header; the section name became a
        # candidate in AUDIT item 10 and is genuinely printed in the figure.
        assert result.checked == 4
        assert result.unverified == ("Calocycletta costata", "Callimitra rebus")
        assert result.clean is False
        assert result.reason == ""

    def test_the_reported_names_are_the_results_own_strings(self):
        # The verifier echoes with different casing / trailing punctuation; the
        # report must carry the spelling that is in the DATA, not the echo.
        data = _zonation_result()
        stub = _Stub('{"absent": ["calocycletta  costata."]}')
        result = _check(data, stub)
        assert result.unverified == ("Calocycletta costata",)
        assert data["species_ranges"][1]["species"] == "Calocycletta costata"

    def test_a_name_the_extraction_never_produced_is_dropped(self):
        # The verifier inventing a string is not evidence about any row.
        stub = _Stub('{"absent": ["Calocycletta costata", "Nonexistentia imaginary"]}')
        result = _check(_zonation_result(), stub)
        assert result.unverified == ("Calocycletta costata",)

    def test_a_bare_json_array_reply_is_accepted(self):
        result = _check(_zonation_result(), _Stub('["Callimitra rebus"]'))
        assert result.status == GROUNDING_VERIFIED
        assert result.unverified == ("Callimitra rebus",)


class TestAllNamesPresentReportsNothing:
    def test_an_empty_absence_list_is_a_clean_verified_result(self):
        result = _check(_zonation_result(), _Stub('{"absent": []}'))
        assert result.status == GROUNDING_VERIFIED
        assert result.unverified == ()
        assert result.clean is True
        assert result.performed is True

    def test_the_candidate_list_is_sent_inline_so_absence_is_decidable(self):
        stub = _Stub('{"absent": []}')
        _check(_zonation_result(), stub)
        sent = stub.kwargs["user_text"]
        for name in ("Buccinosphaera invaginata", "Calocycletta costata",
                     "Callimitra rebus"):
            assert name in sent, "%r was never offered to the verifier" % name

    def test_the_reply_budget_is_kept_small(self):
        # The verifier is pushed to a short list on purpose; a large budget
        # invites a large answer and defeats "report absences only".
        stub = _Stub('{"absent": []}')
        _check(_zonation_result(), stub)
        assert stub.kwargs["max_tokens"] == GROUNDING_MAX_TOKENS
        assert 800 <= GROUNDING_MAX_TOKENS <= 1200


class TestFailuresDegradeToNotPerformed:
    """Never an empty "clean" list. That collapse is the bug being fixed."""

    def test_a_timeout_is_not_performed_not_clean(self):
        stub = _Stub(None, status=None, err_body="read timed out")
        result = _check(_zonation_result(), stub)
        assert result.status == GROUNDING_NOT_PERFORMED
        assert result.clean is False
        assert result.unverified == ()
        assert "timed out" in result.reason

    def test_a_non_200_is_not_performed(self):
        stub = _Stub(None, status=503, err_body="upstream busy")
        result = _check(_zonation_result(), stub)
        assert result.status == GROUNDING_NOT_PERFORMED
        assert "503" in result.reason

    def test_a_200_that_smuggled_an_error_is_not_performed(self):
        stub = _Stub(None, status=200, err_body="insufficient balance")
        result = _check(_zonation_result(), stub)
        assert result.status == GROUNDING_NOT_PERFORMED

    @pytest.mark.parametrize("garbage", [
        "I could not read the figure.",
        "",
        "```json\n{\"absent\": [\"Callimitra rebus\"\n",   # unclosed fence
        '{"absent": "Callimitra rebus"}',                 # right key, wrong type
        '{"other_key": ["Callimitra rebus"]}',            # not the agreed shape
        "not json at all",
    ])
    def test_an_unreadable_reply_is_not_performed(self, garbage):
        result = _check(_zonation_result(), _Stub(garbage))
        assert result.status == GROUNDING_NOT_PERFORMED, (
            "a reply we cannot read must never be reported as a clean check")
        assert result.unverified == ()

    def test_a_truncated_reply_is_not_performed(self):
        # The JSON may still parse, but a cut-off list is not "the whole
        # absence set" -- reporting it as one would understate the evidence.
        stub = _Stub('{"absent": ["Callimitra rebus"]}', truncated=True)
        result = _check(_zonation_result(), stub)
        assert result.status == GROUNDING_NOT_PERFORMED
        assert result.unverified == ()

    def test_a_caller_that_raises_is_swallowed(self):
        stub = _Stub(raises=RuntimeError("socket exploded"))
        result = _check(_zonation_result(), stub)
        assert result.status == GROUNDING_NOT_PERFORMED
        assert "RuntimeError" in result.reason

    def test_no_provider_means_no_call_and_not_performed(self):
        stub = _Stub('{"absent": []}')
        result = check_name_grounding(data=_zonation_result(), image_b64="QUJD",
                                      provider=None, llm_caller=stub)
        assert result.status == GROUNDING_NOT_PERFORMED
        assert stub.calls == 0, "it called out without a provider"

    def test_no_image_means_no_call_and_not_performed(self):
        stub = _Stub('{"absent": []}')
        result = check_name_grounding(data=_zonation_result(), image_b64="",
                                      provider=_provider(), llm_caller=stub)
        assert result.status == GROUNDING_NOT_PERFORMED
        assert stub.calls == 0

    def test_every_failure_keeps_the_extraction_usable(self):
        # The caller's data must come back untouched: this runs after an
        # extraction and must never damage the result it is checking.
        data = _zonation_result()
        before = [dict(r) for r in data["species_ranges"]]
        _check(data, _Stub(None, status=None, err_body="timeout"))
        assert data["species_ranges"] == before
        assert score_range_chart(data)["score"] > 0


class TestNoNamesToCheck:
    def test_a_mode_without_name_keys_says_so_instead_of_claiming_clean(self):
        # `sections` cannot be the example any more: AUDIT item 10 made it a
        # name source, so a columnar_section result IS checkable now.
        data = {"cross_beds": [{"from_bed_idx": 1, "to_bed_idx": 2}],
                "lithology_legend": [], "fossil_legend": []}
        result = _check(data, _Stub('{"absent": []}'))
        assert result.status == GROUNDING_NO_NAMES
        assert result.clean is False
        assert result.performed is False
        assert result.unverified == ()

    def test_no_llm_call_is_made_when_there_is_nothing_to_check(self):
        stub = _Stub('{"absent": []}')
        _check({"cross_beds": []}, stub)
        assert stub.calls == 0


# ---------------------------------------------------------------------------
# AUDIT item 10 -- COVERAGE. A mode that carries names must not be silently
# unchecked.
#
# Measured cost of the gap: Tomimatsu et al. 2022 Fig. 8 was classified
# ``chemical_stratigraphy``, so all 32 of its names sat in ``data_points`` /
# ``events`` / ``intervals`` -- none of which was in NAME_SOURCES -- the
# collector returned [], and the check answered ``no_names`` for a figure whose
# whole content is taxon ranges. No warning, no evidence, nothing checked.
# ---------------------------------------------------------------------------

# (table_key, rows, names_that_must_be_collected). The row keys were read off
# ``extractor._PRIMARY_ID_KEYS`` / ``_KNOWN_CHEMICAL_STRAT_*`` /
# ``normalize_columnar_result`` and the ``data_keys`` of the matching
# ``exporter.get_configs_for_result`` config.
_COVERAGE_CASES = [
    # chemical_stratigraphy -- all three tables, all three name-bearing.
    ("data_points", [{"sample_id": "Os-1", "stage": "Julian"}],
     ["Os-1", "Julian"]),
    ("data_points", [{"sample_id": "Os-2", "stage": ""}], ["Os-2"]),
    ("events", [{"name": "Initial 187Os/188Os negative excursion"}],
     ["Initial 187Os/188Os negative excursion"]),
    ("intervals", [{"name": "Julian (lower; TR4A)"}],
     ["Julian (lower; TR4A)"]),
    # abundance_diagram locality headers.
    ("sites", [{"name": "Suigetsu"}], ["Suigetsu"]),
    # range_chart sections print `name`; columnar_section sections print `id`.
    ("sections", [{"name": "Jurassic nassellarian range chart"}],
     ["Jurassic nassellarian range chart"]),
    ("sections", [{"id": "Sec A"}], ["Sec A"]),
    # range_chart free-text fossils. After normalisation these rows are PLAIN
    # STRINGS; `fossil` / `text` are the dict spellings the exporter reads.
    ("other_fossils", ["Archaeocenosphaera", "  Brachiopod  "],
     ["Archaeocenosphaera", "Brachiopod"]),
    ("other_fossils", [{"fossil": "Trilobite"}], ["Trilobite"]),
    ("other_fossils", [{"text": "Crinoid"}], ["Crinoid"]),
]


class TestEveryModeThatCarriesNamesIsChecked:
    @pytest.mark.parametrize("table,rows,expected", _COVERAGE_CASES,
                             ids=[c[0] + ":" + str(i)
                                  for i, c in enumerate(_COVERAGE_CASES)])
    def test_the_row_key_is_collected(self, table, rows, expected):
        names, _ = collect_candidate_names({table: rows})
        assert names == expected, (
            "%r is emitted by a normaliser and printed in the figure, but "
            "NAME_SOURCES does not collect it" % table)

    def test_the_coverage_table_is_registered_for_each_of_them(self):
        # The collector and the flagger both drive off NAME_SOURCES, so a
        # table that is collected but not registered would be verified and
        # then silently unflagged.
        from rca_core.grounding import BARE_STRING_ROW_TABLES
        assert BARE_STRING_ROW_TABLES == {"other_fossils"}
        for table in ("data_points", "events", "intervals", "sites",
                      "sections", "other_fossils"):
            assert table in {k for k, _ in NAME_SOURCES}, \
                "%s is collected from no NAME_SOURCES entry" % table

    def test_a_chemical_stratigraphy_result_is_checked_not_declared_empty(self):
        # The measured regression, in the shape round 2 actually produced.
        data = {
            "metadata": {"section": "Tiezzi"},
            "data_points": [{"sample_id": "Os-%d" % i, "stage": "Julian"}
                            for i in range(1, 4)],
            "events": [{"name": "Reducing condition"}],
            "intervals": [{"name": "Julian (lower; TR4A)"}],
            "confidence": 0.7,
        }
        result = _check(data, _Stub('{"absent": ["Reducing condition"]}'))
        assert result.status == GROUNDING_VERIFIED, (
            "a chemostrat figure reported no_names is the item-10 defect "
            "returning: nothing was checked")
        # Os-1..Os-3 + "Julian" (deduped across the three data_points) +
        # the event + the interval.
        assert result.checked == 6
        assert result.unverified == ("Reducing condition",)

    def test_the_names_match_the_extractor_primary_id_keys(self):
        # Cross-check against the normaliser rather than a copy of it, so a
        # rename upstream empties this table instead of quietly disabling it.
        from rca_core.extractor import _PRIMARY_ID_KEYS
        by_table = dict(NAME_SOURCES)
        for table in ("abundances", "sections", "biozones", "species_ranges",
                      "sites", "zones", "zonations", "data_points"):
            assert _PRIMARY_ID_KEYS[table] in by_table[table], (
                "%s: the extractor's primary id key is not read" % table)

    def test_a_row_with_no_name_field_still_collects_nothing(self):
        # Guards the opposite failure: reading the whole row as the name would
        # turn every numeric field into a "candidate".
        assert collect_candidate_names({"sections": [{"thickness_m": "30"}]})[0] \
            == []
        assert collect_candidate_names(
            {"data_points": [{"depth_m": "12.5"}]})[0] == []

    def test_a_bare_string_row_is_junk_in_every_other_table(self):
        # Only other_fossils is normalised to strings. A string in `sections`
        # is malformed and must not be read as a name.
        assert collect_candidate_names({"sections": ["Sec A"]})[0] == []
        assert collect_candidate_names({"zones": ["Zone A"]})[0] == []

    def test_a_bare_string_name_is_reported_but_cannot_be_flagged(self):
        # There is no dict to attach `_warning` to, so the per-row marker is
        # unavailable for other_fossils -- the name still reaches the user
        # through the unverified list. Both halves are pinned here so the
        # limitation is a recorded fact, not a surprise.
        data = {"other_fossils": ["Brachiopod", "Trilobite"]}
        result = _check(data, _Stub('{"absent": ["Trilobite"]}'))
        assert result.status == GROUNDING_VERIFIED
        assert result.unverified == ("Trilobite",)
        assert flag_unverified_rows(data, result.unverified) == {}
        assert data["other_fossils"] == ["Brachiopod", "Trilobite"], (
            "a string row must never be mutated in place")


# ---------------------------------------------------------------------------
# AUDIT item 8 -- STAGE NAMES ARE PRINTED, NOT ABSENT.
#
# Zhang Lei et al. 2018 Fig. 3: the verifier reported Kungurian / Artinskian /
# Sakmarian / Asselian "Stage" absent. All four are printed, in the rotated
# stage-header column -- the largest, boldest text in the figure. Rule 2
# already forbade reporting a name printed "in a header"; it never said that a
# name WHICH IS a header is present.
# ---------------------------------------------------------------------------


class TestPrintedStageNamesAreNotFalsePositives:
    def test_the_prompt_says_a_stage_header_counts_as_printed(self):
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT
        low = GROUNDING_SYSTEM_PROMPT.lower()
        assert "stratigraphic stage or age is printed" in low
        assert "rotated axis header" in low
        # The exact reasoning error, named so the model has to contradict it.
        assert "not a taxon row" in low

    def test_the_prompt_matches_a_stem_plus_a_rank_suffix(self):
        # Printed "Kungurian" vs candidate "Kungurian Stage": the candidate
        # wins on the stem. (Checked at 6x, the figure prints "Asselian"
        # correctly spelled -- the difference is the suffix, not a letter.)
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT
        assert ("the figure prints \"Kungurian\" and the candidate is "
                "\"Kungurian Stage\"") in GROUNDING_SYSTEM_PROMPT
        assert "RANK SUFFIX" in GROUNDING_SYSTEM_PROMPT

    def test_the_candidate_list_is_not_framed_as_taxon_names_only(self):
        # Framing the list as "taxon / biozone names" invites the very
        # inference that caused the false positives.
        text = build_verifier_user_text(["Kungurian Stage"])
        assert "taxon and zone names" in text
        assert "chronostratigraphic stage names" in text
        assert "headers included" in text
        assert "taxon / biozone names" not in text

    def test_a_stage_name_the_verifier_reports_still_reaches_the_result(self):
        # The false positives are fixed in the PROMPT. This pins that they are
        # not "fixed" by suppressing stage names from the report path, which
        # would hide a genuinely absent stage for good.
        data = {"zones": [{"name": "Kungurian Stage"},
                          {"name": "Ghostia absentia"}]}
        result = _check(data, _Stub('{"absent": ["Kungurian Stage"]}'))
        assert result.status == GROUNDING_VERIFIED
        assert result.unverified == ("Kungurian Stage",)
        assert flag_unverified_rows(data, result.unverified) == {"zones": 1}

    def test_a_promoted_prompt_would_not_silence_the_check(self):
        # Every rule added for item 8 moves names from absent to present, so
        # the risk is a prompt that reports nothing at all. The spine is
        # asserted here: absences are still demanded, still certifiable, and
        # an all-present answer is still an empty list rather than no answer.
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT as p
        low = p.lower()
        assert "report only the names that are absent" in low
        assert "only when you are certain" in low
        assert "silence is a valid" in low
        assert "unfamiliar" in low and "plausibility" in low
        assert "echo each" in low
        assert "answer with an empty" in low
        # ...and a verifier that still answers is still believed.
        assert _check({"zones": [{"name": "Z1"}]},
                      _Stub('{"absent": ["Z1"]}')).unverified == ("Z1",)


# ---------------------------------------------------------------------------
# AUDIT item 9 -- TEXT DISTRIBUTED OVER LINES AND CELLS.
#
# The claim under test was that 12 printed zone names were missed because the
# text is split across table cells and a hyphenated zone name straddles a line
# break. Reading r10_zhanglei.png directly: all 24 candidate names are printed,
# the verifier reported 4 of them, and every one of those 4 was a false
# positive -- so there was nothing to miss on that figure, and the real defect
# was item 8. The split-text robustness is still worth pinning (three of the
# names ARE wrapped: "P. scalprata m." / "rhombothoracata Zone",
# "P. lomentaria-" / "P. sakmarensis" / "Assemblage-Zone", and the GTS header
# over four lines), so these tests guard the prompt rule rather than claim a
# recall improvement that was not measured.
# ---------------------------------------------------------------------------


class TestTextSplitOverLinesAndCells:
    def test_the_prompt_requires_wrapped_text_to_be_rejoined(self):
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT
        assert ("DISTRIBUTED over two lines, two table cells or two columns is "
                "PRESENT") in GROUNDING_SYSTEM_PROMPT
        assert "LAYOUT HYPHEN" in GROUNDING_SYSTEM_PROMPT
        assert ("\"P. lomentaria-\" over \"P. sakmarensis\"") \
            in GROUNDING_SYSTEM_PROMPT

    def test_the_prompt_counts_a_numeral_inside_a_merged_cell(self):
        # Ozsvart: the band prints "15  16" above the single word "Spica", so
        # "UAZ 16 - Spica" IS printed -- round 2 called it absent.
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT
        assert "MERGED cell" in GROUNDING_SYSTEM_PROMPT
        assert "15  16" in GROUNDING_SYSTEM_PROMPT

    def test_the_split_rule_does_not_eat_the_whole_prompt(self):
        # A single fuzzy-matching rule must not displace rule 1: the verifier
        # is still asked for certified absences, not a similarity score.
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT as p
        low = p.lower()
        assert "absorb whitespace and" in low          # a match instruction...
        assert "only when you are certain" in low      # ...that still certifies
        assert low.count("report a name only when") == 1

    # NOTE, recorded so it is not re-attempted blind: a further rule was
    # TRIED and REVERTED here. Tomimatsu et al. 2022 Fig. 8 labels its
    # intervals with composed strings ("Julian (lower; TR4A)") laid over a
    # stage row reading Julian | Tuvalian, and a "judge the parts of a
    # composite label" rule was added for it. Measured over the ten round-2
    # figures it made that figure report 4 -> 25 absences in one 4-second
    # call, because "every part is printed" gave the verifier licence to
    # reason about parts and then declare the whole thing absent. With one
    # sample per figure, that is not evidence for the rule, so it is gone.
    # There is no assertion pinning the rule's absence; the honest statement
    # is that prompt rules for this class did not survive measurement.


class TestPromptShape:
    def test_the_prompt_asks_for_absences_only_and_echoes_the_list(self):
        text = build_verifier_user_text(["Zone A", "Zone B"])
        assert "Zone A" in text and "Zone B" in text
        assert '"absent"' in text

    def test_the_system_prompt_forbids_plausible_but_absent_names(self):
        from rca_core.grounding import GROUNDING_SYSTEM_PROMPT
        low = GROUNDING_SYSTEM_PROMPT.lower()
        assert "absent" in low
        # The exact failure this exists for: a real name that is simply
        # unfamiliar must not become an accusation.
        assert "unfamiliar" in low and "plausibility" in low


# ---------------------------------------------------------------------------
# Row flagging: additive, never clobbering
# ---------------------------------------------------------------------------


class TestFlagUnverifiedRows:
    def test_matching_rows_get_the_flag(self):
        data = _zonation_result()
        flagged = flag_unverified_rows(data, ["Callimitra rebus"])
        assert flagged == {"species_ranges": 1}
        assert data["species_ranges"][2]["_warning"] == GROUNDING_ROW_FLAG

    def test_an_existing_string_warning_becomes_a_list_and_survives(self):
        data = _zonation_result()
        data["species_ranges"][2]["_warning"] = "index_order_swap"
        flag_unverified_rows(data, ["Callimitra rebus"])
        assert data["species_ranges"][2]["_warning"] == [
            "index_order_swap", GROUNDING_ROW_FLAG]

    def test_an_existing_list_warning_is_appended_to_not_replaced(self):
        data = _zonation_result()
        data["species_ranges"][2]["_warning"] = ["index_order_swap",
                                                 "response_kind_conflict"]
        flag_unverified_rows(data, ["Callimitra rebus"])
        assert data["species_ranges"][2]["_warning"] == [
            "index_order_swap", "response_kind_conflict", GROUNDING_ROW_FLAG]

    def test_flagging_twice_does_not_duplicate_the_flag(self):
        data = _zonation_result()
        flag_unverified_rows(data, ["Callimitra rebus"])
        flag_unverified_rows(data, ["Callimitra rebus"])
        assert data["species_ranges"][2]["_warning"] == GROUNDING_ROW_FLAG

    def test_a_row_already_carrying_the_flag_is_not_flagged_twice(self):
        data = _zonation_result()
        data["species_ranges"][2]["_warning"] = [GROUNDING_ROW_FLAG, "other"]
        flag_unverified_rows(data, ["Callimitra rebus"])
        assert data["species_ranges"][2]["_warning"] == [
            GROUNDING_ROW_FLAG, "other"]

    def test_unflagged_rows_are_untouched(self):
        data = _zonation_result()
        flag_unverified_rows(data, ["Callimitra rebus"])
        assert "_warning" not in data["species_ranges"][0]

    def test_a_correlation_row_is_flagged_from_either_end(self):
        data = {"correlations": [{"from_zone": "Zone A", "to_zone": "Zone B"}]}
        assert flag_unverified_rows(data, ["Zone B"]) == {"correlations": 1}
        assert data["correlations"][0]["_warning"] == GROUNDING_ROW_FLAG

    def test_nothing_to_flag_touches_nothing(self):
        data = _zonation_result()
        before = [dict(r) for r in data["species_ranges"]]
        assert flag_unverified_rows(data, []) == {}
        assert flag_unverified_rows(data, None) == {}
        assert flag_unverified_rows(None, ["Zone A"]) == {}
        assert data["species_ranges"] == before


# ---------------------------------------------------------------------------
# The score is untouched. This is the constraint, not an accident.
# ---------------------------------------------------------------------------


class TestGroundingNeverMovesTheScore:
    @pytest.mark.parametrize("grounding", [
        None,
        GroundingResult(status=GROUNDING_VERIFIED, checked=3,
                        unverified=("Callimitra rebus",)),
        GroundingResult(status=GROUNDING_VERIFIED, checked=3),
        GroundingResult(status=GROUNDING_NOT_PERFORMED, reason="timed out"),
        GroundingResult(status=GROUNDING_NO_NAMES),
        {"status": GROUNDING_VERIFIED, "unverified": ["Callimitra rebus"]},
        {"status": GROUNDING_NOT_PERFORMED, "reason": "no provider"},
        {"status": "something_a_future_build_invents"},
    ], ids=["omitted", "verified_absent", "verified_clean", "not_performed",
            "no_names", "dict_absent", "dict_not_performed", "unknown_status"])
    def test_score_and_grade_are_identical_with_and_without(self, grounding):
        data = _zonation_result()
        baseline = score_range_chart(data)
        graded = score_range_chart(data, grounding=grounding)
        assert graded["score"] == baseline["score"]
        assert graded["grade"] == baseline["grade"]

    def test_the_composite_is_not_recomputed_after_the_issue_is_appended(self):
        # A pathological grounding report (huge absence list) must not move it.
        data = _zonation_result()
        baseline = score_range_chart(data)
        huge = GroundingResult(status=GROUNDING_VERIFIED, checked=3,
                               unverified=tuple("Zone %d" % i for i in range(500)))
        assert score_range_chart(data, grounding=huge)["score"] == baseline["score"]

    @pytest.mark.parametrize("data", [
        _zonation_result(),
        {},                                            # empty result
        {"species_ranges": []},                        # pure extraction miss
        {"species_ranges": [{"species": "A"}], "sections": [{"name": "S"}],
         "occurrences": [{"taxon": "T", "site": "X", "code": "A"}]},
    ])
    def test_no_shape_or_report_moves_the_score(self, data):
        baseline = score_range_chart(data)
        for status in (GROUNDING_VERIFIED, GROUNDING_NOT_PERFORMED,
                       GROUNDING_NO_NAMES):
            report = GroundingResult(status=status, checked=1,
                                     unverified=("Ghostia",),
                                     reason="r")
            assert score_range_chart(data, grounding=report)["score"] \
                == baseline["score"]


class TestGroundingIsReportedSeparately:
    def test_unverified_names_become_a_warning_listing_them(self):
        out = score_range_chart(
            _zonation_result(),
            grounding=GroundingResult(status=GROUNDING_VERIFIED, checked=3,
                                     unverified=("Callimitra rebus",
                                                 "Calocycletta costata")))
        warn = [i for i in out["issues"]
                if i.get("msg_key") == "grounding.unverified_names"]
        assert len(warn) == 1
        assert warn[0]["severity"] == "warning"
        assert warn[0]["params"]["count"] == "2"
        assert "Callimitra rebus" in warn[0]["params"]["names"]

    def test_a_long_absence_list_is_truncated_in_the_message_only(self):
        out = score_range_chart(
            _zonation_result(),
            grounding=GroundingResult(status=GROUNDING_VERIFIED, checked=3,
                                     unverified=tuple("Z%d" % i
                                                      for i in range(40))))
        issue = next(i for i in out["issues"]
                     if i.get("msg_key") == "grounding.unverified_names")
        assert issue["params"]["count"] == "40"
        assert len(issue["params"]["names"]) < 200
        assert len(out["grounding"]["unverified"]) == 40

    def test_clean_and_could_not_check_are_different_issues(self):
        def keys(out):
            return [i.get("msg_key") for i in out["issues"]]

        clean = score_range_chart(
            _zonation_result(),
            grounding=GroundingResult(status=GROUNDING_VERIFIED, checked=3))
        unknown = score_range_chart(
            _zonation_result(),
            grounding=GroundingResult(status=GROUNDING_NOT_PERFORMED,
                                      reason="timed out"))
        assert "grounding.unverified_names" not in keys(clean)
        assert "grounding.not_performed" in keys(unknown)
        assert clean["grounding"]["status"] == GROUNDING_VERIFIED
        assert unknown["grounding"]["status"] == GROUNDING_NOT_PERFORMED

    def test_no_names_is_reported_and_not_silently_clean(self):
        out = score_range_chart(
            {"sections": [{"id": "S"}], "cross_beds": []},
            grounding=GroundingResult(status=GROUNDING_NO_NAMES))
        keys = [i.get("msg_key") for i in out["issues"]]
        assert "grounding.no_names" in keys
        assert "grounding.unverified_names" not in keys

    def test_an_unrecognised_status_is_treated_as_unknown_not_clean(self):
        out = score_range_chart(_zonation_result(),
                                grounding={"status": "future_value"})
        keys = [i.get("msg_key") for i in out["issues"]]
        assert "grounding.not_performed" in keys

    def test_the_early_return_paths_still_carry_the_grounding_report(self):
        for data in ({}, {"species_ranges": []}, None, "junk"):
            out = score_range_chart(
                data,
                grounding=GroundingResult(status=GROUNDING_NOT_PERFORMED,
                                          reason="timed out"))
            assert out["grounding"]["status"] == GROUNDING_NOT_PERFORMED
            assert "grounding.not_performed" in [
                i.get("msg_key") for i in out["issues"]]

    def test_omitting_the_check_leaves_the_output_exactly_as_before(self):
        data = _zonation_result()
        assert score_range_chart(data) == score_range_chart(data, grounding=None)
        assert "grounding" not in score_range_chart(data)

    def test_the_dataclass_and_a_plain_dict_report_the_same_shape(self):
        out = score_range_chart(
            _zonation_result(),
            grounding=GroundingResult(status=GROUNDING_VERIFIED, checked=3,
                                     unverified=("Ghostia",)))
        assert set(out["grounding"]) == {"status", "checked", "unverified",
                                         "reason", "candidates_truncated"}
        assert out["grounding"]["unverified"] == ["Ghostia"]


# ---------------------------------------------------------------------------
# End to end over the stub: the Motoyama failure shape
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_the_measured_failure_shape_is_caught(self):
        # Names taken verbatim from the four hallucinated runs of Motoyama 2019
        # Fig. 1; none is printed in the figure.
        data = {"sections": [{"name": "Motoyama"}], "zonations": [
            {"name": "Motoyama 2019 radiolarian zonation"}],
            "zones": [{"name": n} for n in (
                "Buccinosphaera invaginata", "Calocycletta costata",
                "Calocycletta virginis", "Calocycletta serrata",
                "Bujtospora uviformis", "Didymocyrtis penultima",
                "Diartus petterssoni", "Stichocorys wolffii",
                "Acrospyris grupium", "Lamprocyrtis heteroporos",
                "Callimitra rebus", "Arachnocorallium pentacrinoid")]}
        # A good grader on this data still says A/1.0 -- that is the whole
        # point: only the grounding pass can see the problem.
        assert score_range_chart(data)["grade"] == "A"

        absent = [n for n in data["zones"] for n in [n["name"]]]
        result = _check(data, _Stub('{"absent": %s}'
                                    % __import__("json").dumps(absent)))
        assert result.status == GROUNDING_VERIFIED
        assert set(result.unverified) == set(absent)

        out = score_range_chart(data, grounding=result)
        assert out["grade"] == "A"      # unchanged: this signal does not grade
        issue = next(i for i in out["issues"]
                     if i.get("msg_key") == "grounding.unverified_names")
        assert issue["severity"] == "warning"
        assert issue["params"]["count"] == "12"
        # The message is capped for the UI; the field is not, and it is the
        # field a caller would act on.
        assert set(out["grounding"]["unverified"]) == set(absent)
        assert issue["params"]["names"].startswith("Buccinosphaera invaginata")

        assert flag_unverified_rows(data, result.unverified) == {"zones": 12}

    def test_a_correct_run_reports_nothing_and_flags_nothing(self):
        data = {"zones": [{"name": "Zone A"}, {"name": "Zone B"}]}
        result = _check(data, _Stub('{"absent": []}'))
        out = score_range_chart(data, grounding=result)
        assert out["grounding"]["status"] == GROUNDING_VERIFIED
        assert out["grounding"]["unverified"] == []
        assert flag_unverified_rows(data, result.unverified) == {}
        assert "_warning" not in data["zones"][0]


# ---------------------------------------------------------------------------
# The i18n contract: three keys x three languages, same placeholders
# ---------------------------------------------------------------------------


GROUNDING_KEYS = ("grounding.unverified_names",
                  "grounding.not_performed",
                  "grounding.no_names")


class TestI18n:
    def test_every_emitted_key_exists_in_all_three_languages(self):
        from rca_core.i18n import TRANSLATIONS
        for key in GROUNDING_KEYS:
            for lang in ("zh", "en", "ja"):
                text = TRANSLATIONS[lang].get(key)
                assert text and text != key, "%s/%s untranslated" % (lang, key)

    def test_placeholders_are_identical_across_languages(self):
        import re
        from rca_core.i18n import TRANSLATIONS
        pat = re.compile(r"\{(\w+)\}")
        for key in GROUNDING_KEYS:
            sets = {lang: frozenset(pat.findall(TRANSLATIONS[lang][key]))
                    for lang in ("zh", "en", "ja")}
            assert len(set(sets.values())) == 1, (
                "%s has drifting placeholders: %s" % (key, sets))

    def test_the_browser_catalogue_mirrors_the_same_keys(self):
        import re
        js = (REPO / "js" / "i18n.js").read_text(encoding="utf-8")
        for key in GROUNDING_KEYS:
            hits = re.findall(r"'%s':\s*'((?:[^'\\]|\\.)*)'"
                              % re.escape(key), js)
            assert len(hits) == 3, "%s appears %d times in js/i18n.js" % (key,
                                                                        len(hits))
