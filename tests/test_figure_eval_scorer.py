"""The scorer is tested, because the two ad-hoc scorers that came before it were
not, and both flattered the tool.

Each test below reproduces one of those failures as a deliberate input, so the
failure mode cannot come back silently.
"""
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "eval"))

from difflib import SequenceMatcher  # noqa: E402

from scorer import (  # noqa: E402
    FUZZY_CUTOFF,
    MAX_CONTAIN_RATIO,
    MIN_MATCH_CHARS,
    compare_names,
    find_evidence,
    _joined,
    forms,
    normalise,
    same_taxon,
    score,
)


# --------------------------------------------------------------------------
# The first scorer's bug: unbounded substring containment
# --------------------------------------------------------------------------

def test_a_short_name_does_not_match_inside_an_unrelated_line():
    """Regression: `a in b` with no length floor reported 100% supported.

    The original scorer joined every OCR line into one blob and asked
    "is the name a substring of the blob". Almost any short taxon name is a
    substring of almost any English sentence.
    """
    blob_line = "the assemblage is dominated by Neogloboquadrina pachyderma"
    assert find_evidence(normalise("Robustulus"),
                         [(blob_line, normalise(blob_line))]) is None


def test_containment_needs_both_sides_long_enough():
    """Both sides of the comparison must clear MIN_MATCH_CHARS.

    A printed name is normally almost the whole candidate string -- the crop
    that carries the taxon label contains little else -- so "Phormocyrtis
    striata" inside a label line is real evidence, while the same characters
    inside a paragraph are not.
    """
    label_line = "Phormocyrtis striata exquisita"
    assert find_evidence(normalise("Phormocyrtis striata"),
                         [(label_line, normalise(label_line))]) is not None

    # One token of that label is below the floor on its own.
    assert find_evidence(normalise("striata"),
                         [(label_line, normalise(label_line))]) is None


def test_containment_is_bounded_by_length_ratio():
    """A caption must not vouch for a short name.

    Without the ratio bound, "Sphaeroid" matches inside a 400-character
    sentence that happens to contain those letters.
    """
    caption = ("at its maximum development in this interval the assemblage is "
               "characterised by Sphaeroid pralinsi and accompanied by")
    assert len(caption) > len("Sphaeroid pralinsi") * MAX_CONTAIN_RATIO
    assert find_evidence(normalise("Sphaeroid pralinsi"),
                         [(caption, normalise(caption))]) is None


def test_containment_respects_word_boundaries():
    line = "notPraeconocaryomma anywhere"
    assert find_evidence(normalise("Praeconocaryomma"),
                         [(line, normalise(line))]) is None


# --------------------------------------------------------------------------
# The second scorer's bug: no handling of OCR line splits
# --------------------------------------------------------------------------

def test_a_name_split_over_three_ocr_lines_is_still_supported():
    """Regression: `Pseudodictyophimus / gracilipes / clevei` scored absent.

    This is the real Bjorklund case: the figure prints one name, OCR returns
    three boxes, and comparing each model name to each box found nothing.
    """
    ocr = ["Pseudodictyophimus", "gracilipes", "clevei"]
    sup, unsup = compare_names(
        ["Pseudodictyophimus gracilipes clevei"],
        ["Pseudodictyophimus gracilipes clevei"], ocr)
    assert len(sup) == 1 and not unsup, "OCR line-split broke the match"


def test_joins_do_not_invent_support_for_unrelated_lines():
    """Adjacent joins must not let two unrelated taxa corroborate each other."""
    # The truth deliberately does NOT contain the produced name, so this is a
    # real check rather than a tautology.
    ocr = ["Lithomelissa", "setosa", "Phorticium", "clevei"]
    _, unsup = compare_names(["Lithomelissa setosa"],
                             ["Cleviplegma boreale"], ocr)
    assert [v.name for v in unsup] == ["Cleviplegma boreale"], (
        "an adjacent-join of two unrelated taxa corroborated a third")


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def test_open_nomenclature_question_mark_is_not_an_error():
    assert normalise("Lychnocanoma? costata") == normalise("Lychnocanoma costata")


def test_author_strings_are_kept_because_whether_to_keep_them_is_a_decision():
    """The scorer must not silently make the authority-vs-name call.

    Round 2 saw `Phormocyrtis striata praequista Nishimura` where the figure
    prints the epithet on one line and the authority on the next. Whether that
    belongs in one field is a normaliser question; folding it away here would
    hide both the merge and the spelling error.
    """
    assert "nishimura" in normalise("Praequisita Nishimura")


# --------------------------------------------------------------------------
# unsupported is NOT fabricated
# --------------------------------------------------------------------------

def test_unsupported_is_reported_without_being_called_a_fabrication():
    sup, unsup = compare_names(["Xitus subitus"],
                               ["Xitus subtilis", "Xitus subitus"])
    assert [v.name for v in unsup] == ["Xitus subtilis"]
    assert all(v.kind == "unsupported" for v in unsup)


def test_an_ocr_hit_weakens_the_verdict_but_does_not_promote_it():
    """OCR evidence yields "variant", never "exact"."""
    sup, _ = compare_names([], ["Lithomelissa setosa"],
                           ["Lithomelissa setosa"])
    assert sup[0].kind == "variant"
    assert sup[0].evidence == "Lithomelissa setosa"


def test_every_supported_verdict_carries_its_evidence():
    """An unverifiable verdict is not evidence."""
    sup, _ = compare_names(["Buryella tetradica", "Buryella pentadica"],
                           ["Buryella tetradica"])
    assert sup[0].evidence == "Buryella tetradica"


# --------------------------------------------------------------------------
# score()
# --------------------------------------------------------------------------

def _truth():
    return {"table": "species_ranges", "name_key": "species",
            "names": ["Xitus subitus", "Buryella tetradica",
                      "Buryella pentadica"]}


def test_score_reports_the_two_populations_separately():
    out = score({"species_ranges": [{"species": "Xitus subtilis"},
                                    {"species": "Buryella tetradica"}]},
                _truth())
    assert out["n_produced"] == 2
    assert out["n_supported"] == 1
    assert out["unsupported"] == ["Xitus subtilis"]
    assert sorted(out["missed"]) == ["Buryella pentadica", "Xitus subitus"]


def test_score_never_emits_a_single_headline_number():
    """The two failure modes both showed up as ONE flattering number.

    So the return value has no field a reader could quote as "accuracy".
    """
    out = score({"species_ranges": []}, _truth())
    assert "accuracy" not in out and "score" not in out
    assert out["precision_supported"] is None      # nothing produced
    assert out["recall"] == 0.0


def test_score_is_deterministic_and_order_independent():
    rows = [{"species": "Buryella tetradica"}, {"species": "Xitus subtilis"}]
    a = score({"species_ranges": rows}, _truth())
    b = score({"species_ranges": list(reversed(rows))}, _truth())
    assert a["n_supported"] == b["n_supported"]
    assert sorted(a["unsupported"]) == sorted(b["unsupported"])


def test_empty_and_absurd_inputs_do_not_raise():
    for payload in ({}, {"species_ranges": None}, {"species_ranges": [None, 7]},
                    {"species_ranges": [{"species": None}]}):
        out = score(payload, _truth())
        assert out["n_produced"] == 0
    assert score({}, {"names": []})["recall"] is None


def test_fuzzy_cutoff_is_strict_enough_to_not_merge_two_species():
    """`subitus` vs `subtilis` must NOT be called the same name.

    This is the confirmed fabrication of round 2. A sloppy cutoff here would
    silently score it correct, which is exactly the failure the previous round
    produced in reverse.
    """
    a, b = normalise("Xitus subitus"), normalise("Xitus subtilis")
    assert a != b
    assert find_evidence(a, [(b, b)]) is None

    # The one that caught this: 0.8889 sits just above a bare 0.88 cutoff, so
    # identity must come from same_taxon(), not from the similarity number.
    assert SequenceMatcher(None, a, b).ratio() < FUZZY_CUTOFF
    c, d = normalise("Buryella pentadica"), normalise("Buryella tetradica")
    assert SequenceMatcher(None, c, d).ratio() > FUZZY_CUTOFF, (
        "if this ever drops below the cutoff the guard is untested again -- "
        "keep it above, and keep same_taxon() as the real identity rule")
    assert find_evidence(c, [(d, d)]) is None
    assert not same_taxon(c, d)
    assert same_taxon(normalise("Xitus subitus"), normalise("xitus  subitus"))


# --------------------------------------------------------------------------
# Parentheticals: a synonym in brackets is metadata, not part of the name
# --------------------------------------------------------------------------

def test_a_printed_parenthetical_synonym_still_matches_the_bare_name():
    """Bjorklund Fig. 4 prints `Lithelius (Larcospira) minor`.

    Copying that verbatim is correct, so a scorer that treated the bracketed
    genus as part of the name would score a faithful transcription wrong.
    """
    printed = "Lithelius (Larcospira) minor"
    for produced in (printed, "Lithelius minor"):
        sup, unsup = compare_names([printed], [produced])
        assert not unsup, "%r was scored as absent from the figure" % produced
        assert len(sup) == 1


def test_parenthetical_handling_does_not_merge_two_species():
    """Dropping brackets must not become dropping the epithet."""
    sup, unsup = compare_names(
        ["Lithelius (Larcospira) minor"],
        ["Lithelius (Larcospira) majus"])
    assert [v.name for v in unsup] == ["Lithelius (Larcospira) majus"]


# --------------------------------------------------------------------------
# Gaps found by mutation, then closed.
#
# Four mutations of scorer.py turned the suite GREEN. Each one is below, with
# the reason the original test missed it, because "the test exists" and "the
# test would notice" are different claims.
# --------------------------------------------------------------------------

def test_the_length_floor_is_actually_load_bearing():
    """MUTATION MISS #1: MIN_MATCH_CHARS -> 0 did not turn anything red.

    The first attempt used "spong" in "spongs", which the WORD BOUNDARY
    already rejects -- so it proved nothing about the floor, and the mutation
    stayed green. This pair is chosen so that containment would otherwise
    succeed: 6 chars vs 4, ratio 1.5 (inside the 1.6 bound), and "abcd" is a
    whole word inside "abcd e". The ONLY thing rejecting it is the floor.
    """
    want, container = "abcd", "abcd e"
    assert len(container) / len(want) < MAX_CONTAIN_RATIO, (
        "the ratio must NOT be what catches this one, or the test measures the "
        "ratio again and the floor stays untested")
    assert re.search(r"(?:^| )%s(?: |$)" % want, container), (
        "the containment regex must otherwise MATCH here")
    assert find_evidence(want, [(container, container)]) is None


def test_exact_name_still_matches():
    """The identity pass is the first and cheapest rule; it has its own test.

    Also: a mutation harness that runs `pytest -k <name>` must treat exit code
    5 ("no tests ran") as NOT CAUGHT. Reading 5 as "caught" is how a mutation
    gets reported as killed when nothing was executed.
    """
    sup, unsup = compare_names(["Buryella tetradica"], ["Buryella tetradica"])
    assert not unsup
    assert sup[0].kind == "exact"


def test_the_genus_half_of_the_identity_rule_is_load_bearing():
    """MUTATION MISS #2: dropping the genus check did not turn anything red.

    The existing test used Buryella pentadica / tetradica, where the EPITHETS
    differ, so deleting the genus comparison changed nothing. The case that
    needs it is the same epithet under a different genus -- which is exactly
    what a "normalise this into a name I know" failure looks like.
    """
    a, b = normalise("Phorticium clevei"), normalise("Cleveiplegma clevei")
    assert same_taxon(a, b) is False
    assert find_evidence(a, [(b, b)]) is None, (
        "same epithet, different genus must not count as the same taxon")


def test_the_join_n_grams_are_exactly_n_gram_length():
    """MUTATION MISS #3: replacing the length guard with `if True` was green.

    Nothing asserted the SHAPE of what `_joined` produces, only that a split
    name could be found. Over-eager joins are the dangerous direction -- they
    manufacture support -- so the output is pinned directly.
    """
    j = _joined(["Pseudodictyophimus", "gracilipes", "clevei"])
    assert sorted(j) == ["Pseudodictyophimus gracilipes",
                         "Pseudodictyophimus gracilipes clevei",
                         "gracilipes clevei"]
    assert _joined(["solo"]) == [], "a single line has no adjacent pair to join"
