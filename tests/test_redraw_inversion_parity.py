"""The redraw, the quality scorer and the exporter must agree on "inverted".

AUDIT-2026-10-01. Three modules independently decide whether a species range is
inverted, and their docstrings all claim the same convention:

  * rca_core/redraw.py      "inverted follows the project-wide convention (the
                             exporter's range_base_le_range_top constraint,
                             quality.py's FAD/LAD check)"
  * rca_core/exporter.py    range_base_le_range_top, evaluated on the LABEL via
                             the shared subscript-aware bed parser
  * rca_core/quality.py     the same test in the accuracy and consistency passes

They did not agree. rca_core/redraw.py resolves a bed label through
parse_bed_int, which is right for DRAWING -- the redraw is a plot on the
extracted integer grid, and 23b and 23 really are one plotted point -- but the
inverted flag was computed from those plotted floats, so the subscript was gone
by the time the comparison happened. Measured, on ONE row
(range_base="Bed 23b", range_top="Bed 23"):

    redraw    -> not inverted (drawn as a normal range)
    quality   -> inverted
    exporter  -> REFUSES the export

rca_core/bed_parser.py's docstring had already named this exact cost of the
lossy integer: "code that sorts or compares on that number cannot tell 23a from
23b -- so it either forgave a real inversion or invented one. Both happened." It
forgave one.

The fix keeps the DRAWING untouched (the plotted values and their sources are
unchanged, so no figure changes) and consults the label for the comparison, so
all three modules now answer alike. This file is the guard for that: the
exporter is the oracle, because it is the one that can refuse the user's file.
"""
import pytest

from rca_core import exporter as EX
from rca_core import quality as Q
from rca_core.redraw import SOURCE_INDEX, resolve_species_range_rows

# (id, range_base, range_top) -- plain, sub-bed, mixed, and the bare forms.
CASES = [
    ("plain_fine", "Bed 7", "Bed 9"),
    ("plain_inverted", "Bed 9", "Bed 7"),
    ("plain_same", "Bed 7", "Bed 7"),
    ("subbed_b_over_bare", "Bed 23b", "Bed 23"),
    ("subbed_bare_over_b", "Bed 23", "Bed 23b"),
    ("subbed_c_over_b", "Bed 23c", "Bed 23b"),
    ("subbed_b_over_c", "Bed 23b", "Bed 23c"),
    ("subbed_rise", "Bed 23", "Bed 23a"),
    ("bare_and_sub", "23a", "23"),
    ("sub_and_bare", "23", "23a"),
    ("metres_rejected", "23 m", "23"),
    ("ages_rejected", "253 Ma", "251 Ma"),
    ("unreadable_base", "not a bed", "Bed 9"),
    ("unreadable_top", "Bed 7", "nonsense"),
]


def _result(base, top):
    return {
        "sections": [{"name": "S1"}],
        "species_ranges": [{"species": "A", "section": "S1",
                            "range_base": base, "range_top": top}],
        "biozones": [], "other_fossils": [],
    }


def _redraw_inverted(base, top):
    resolved, _summary = resolve_species_range_rows(
        [{"species": "A", "section": "S1", "range_base": base, "range_top": top}])
    return bool(resolved and resolved[0].get("inverted"))


def _quality_inverted(base, top):
    _score, issues = Q._score_accuracy(_result(base, top))
    return any(i.get("msg_key") in ("quality.range_top_lt_base", "quality.fad_lt_lad")
               for i in issues)


def _exporter_accepts(base, top):
    ok, _issues, _warnings = EX.validate_export_invariants(_result(base, top))
    return ok


class TestThreeModulesAgree:
    @pytest.mark.parametrize("cid,base,top", CASES, ids=[c[0] for c in CASES])
    def test_redraw_agrees_with_the_exporter_and_with_quality(self, cid, base, top):
        redraw = _redraw_inverted(base, top)
        quality = _quality_inverted(base, top)
        accepts = _exporter_accepts(base, top)
        assert redraw == (not accepts), (
            f"{cid}: base={base!r} top={top!r} -- the redraw says inverted="
            f"{redraw} while the exporter "
            f"{'accepts' if accepts else 'REFUSES the export'}. A row the "
            "exporter will not write must not be drawn as a normal range, and "
            "the caption count the user reads comes from the redraw.")
        assert redraw == quality, (
            f"{cid}: base={base!r} top={top!r} -- redraw inverted={redraw}, "
            f"quality inverted={quality}")

    def test_the_cases_actually_exercise_disagreement(self):
        """A matrix where nothing is ever inverted proves nothing, so at least
        one plain and one sub-bed inversion must be in it -- and the exporter
        must be the one that refuses them."""
        assert not _exporter_accepts("Bed 9", "Bed 7"), "plain inversion control"
        assert not _exporter_accepts("Bed 23b", "Bed 23"), "sub-bed inversion control"
        assert _exporter_accepts("Bed 7", "Bed 9"), "plain fine control"
        assert _exporter_accepts("Bed 23", "Bed 23a"), "sub-bed fine control"

    def test_the_drawing_is_unchanged_by_the_ordering_fix(self):
        """The fix must move the FLAG only. If the plotted positions changed,
        every existing figure would change with it."""
        resolved, _ = resolve_species_range_rows([
            {"species": "A", "range_base": "Bed 23b", "range_top": "Bed 23"},
            {"species": "B", "range_base": "Bed 7", "range_top": "Bed 9"},
        ])
        by_species = {e["species"]: e for e in resolved}
        # Both still plot at integer grid positions, as parse_bed_int means.
        assert by_species["A"]["base"] == 23.0
        assert by_species["A"]["top"] == 23.0
        assert by_species["B"]["base"] == 7.0
        assert by_species["B"]["top"] == 9.0
        # ... while only the flag differs.
        assert by_species["A"]["inverted"] is True
        assert by_species["B"]["inverted"] is False

    def test_a_row_with_no_readable_endpoint_is_unresolved_not_inverted(self):
        resolved, summary = resolve_species_range_rows(
            [{"species": "A", "range_base": "not a bed", "range_top": "Bed 9"}])
        assert resolved[0]["unresolved"] is True
        assert resolved[0]["inverted"] is False
        assert summary["inverted"] == 0
        assert summary["unresolved"] == 1


class TestTheOneDocumentedException:
    """Where the extractor's indices CONFLICT with the labels, the redraw
    follows the indices -- on purpose, and the disagreement is left standing.

    tests/test_review_2026_09_20_redraw_eval_names_usage.py::
    test_index_fields_win_over_labels pins it: "Priority matters for the
    verdict too: the labels read inverted, the indices do not, and the indices
    are what the row claims." A first version of the subscript fix consulted
    the label unconditionally and was caught by that test; the fix was narrowed
    to label-sourced endpoints only.

    So for these rows the three modules answer differently, deliberately:
    the redraw honours the structured index, quality and the exporter read the
    label. That is a real user-visible consequence -- a row can be drawn as
    fine, flagged by the quality panel, and refused by the export -- so it is
    pinned here rather than left to be rediscovered. Whether the EXPORTER
    should also prefer a present index is a product decision and is not made
    here.
    """

    @pytest.mark.parametrize("base,top,base_idx,top_idx", [
        ("Bed 26", "Bed 24", 4, 9),      # labels inverted, indices not
        ("Bed 23b", "Bed 23", 23, 23),   # sub-bed label, integer indices
    ])
    def test_redraw_follows_the_index_and_says_so(self, base, top, base_idx, top_idx):
        resolved, _ = resolve_species_range_rows([{
            "species": "A", "section": "S1",
            "range_base": base, "range_top": top,
            "range_base_idx": base_idx, "range_top_idx": top_idx,
        }])
        entry = resolved[0]
        # plotted from, and verdicted from, the index
        assert entry["base_source"] == entry["top_source"] == SOURCE_INDEX
        assert entry["inverted"] is False
        # ... while the label-reading modules disagree, which is the point
        assert _quality_inverted(base, top) is True
        assert _exporter_accepts(base, top) is False

    def test_the_exception_is_confined_to_conflicting_rows(self):
        """When the indices and the label agree, so do the three modules."""
        resolved, _ = resolve_species_range_rows([{
            "species": "A", "section": "S1",
            "range_base": "Bed 7", "range_top": "Bed 9",
            "range_base_idx": 7, "range_top_idx": 9,
        }])
        assert resolved[0]["inverted"] is False
        assert _quality_inverted("Bed 7", "Bed 9") is False
        assert _exporter_accepts("Bed 7", "Bed 9") is True
