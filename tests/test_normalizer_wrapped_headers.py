"""Two range-chart normaliser defects measured on real published figures.

D1 - a column header printed VERTICALLY over two lines arrives as two rows.
D4 - one biozone stamped on every row of a whole figure.

Both regressions are built from recorded payloads of the shipped
``rca_core.extractor.extract(mode="auto", ...)`` run over real plates:

  * Li Xin et al. 2018a Fig. 4  -> ``_figtest/r10_lixin_col_result.json``
  * Diaz & Velez 2018 Fig. 5     -> ``_figtest/diaz_fig5_result.json``

Shrinkage (what was kept and what was cut, and why)
--------------------------------------------------
``_LIXIN_COL`` keeps 6 of the payload's 20 ``species_ranges`` rows: the FOUR
rows the two wrapped-header columns produced (``Bekoma? cf. demissa`` /
``robustus Nishimura`` and ``Bekoma? cf. demissa`` / ``demissa Foreman``) plus
two controls -- ``Orbula sp.`` (a genus-level ``sp.`` label, the shape most
likely to be over-eagerly repaired) and ``Lychnocanium carinatatum Ehrenberg``
(a complete name carrying a trailing authority). The other 14 rows are
untouched by both defects and prove nothing extra. All three ``biozones`` rows
are kept whole, because the D4 check needs the figure's declared zone list to
compare a row's zone against. Cut: ``sections``, ``other_fossils``,
``confidence``, ``axis_calibration``, the per-row ``geometry`` sidecars and
every ``range_*_idx`` -- none participates in either defect (``geometry`` is
recomputed identically by ``geometry_from_row``, and the ``_idx`` pair drives
the separate, already-fixed ``endpoint_label_index_swap`` path). The
``range_top``/``range_base`` TEXT is kept on all 6 rows because "the repair
invents no endpoints" is part of the assertion.

``_DIAZ_FIG5`` keeps all 28 of the payload's real species names, the single
biozone string they all carry, and the two declared biozone rows. Cut: every
endpoint, bed and geometry field. Keeping all 28 names is what makes the
"28 in, 28 out, same order" assertion a real zero-fabrication / zero-loss
regression over the measured figure rather than a 3-row toy.

No network: both fixtures are inlined, so nothing here reads ``_figtest/``,
which is untracked and may be absent.
"""

from __future__ import annotations

import unittest

from rca_core.extractor import (
    _is_bare_authority,
    _is_bare_epithet,
    _is_wrappable_header_base,
    _is_wrapped_header_fragment,
    normalize_result,
)

_SECTION = "ZN1 (Zhuangjiaping?) measured section"

# Li Xin et al. 2018a Fig. 4, rows 0-3 verbatim, plus rows 7 and 6 as
# controls. ``robustus Nishimura`` and ``demissa Foreman`` are the second
# PRINTED LINE of a header whose first line is already a row: neither string
# is a column header on the plate.
_LIXIN_COL = {
    "species_ranges": [
        {"species": "Bekoma? cf. demissa", "section": _SECTION,
         "range_top": "ZN1-20 (Dengang Fm)", "range_base": "ZN1-16 (Dengang Fm)",
         "biozone": "RP6 Zone"},
        {"species": "robustus Nishimura", "section": _SECTION,
         "range_top": "ZN1-21 (Dengang Fm top)",
         "range_base": "ZN1-20 (Dengang Fm)", "biozone": "RP6 Zone"},
        {"species": "Bekoma? cf. demissa", "section": _SECTION,
         "range_top": "ZN1-15 (Dengang Fm)", "range_base": "ZN1-15 (Dengang Fm)",
         "biozone": "RP6a Subzone"},
        {"species": "demissa Foreman", "section": _SECTION,
         "range_top": "ZN1-16 (Dengang Fm)", "range_base": "ZN1-15 (Dengang Fm)",
         "biozone": "RP6a Subzone"},
        {"species": "Orbula sp.", "section": _SECTION,
         "range_top": "ZN1-21 (Dengang Fm top)",
         "range_base": "ZN1-21 (Dengang Fm top)", "biozone": "RP6 Zone"},
        {"species": "Lychnocanium carinatatum Ehrenberg", "section": _SECTION,
         "range_top": "ZN1-21 (Dengang Fm top)",
         "range_base": "ZN1-15 (Dengang Fm)",
         "biozone": "RP6 Zone / RP6a Subzone"},
    ],
    "biozones": [
        {"name": "RP6 Zone", "section": _SECTION, "age": "late paleocene"},
        {"name": "RP6a Subzone", "section": _SECTION, "age": "late paleocene"},
        {"name": "Turbocapsula Zone, Costata Subzone", "section": _SECTION,
         "age": "Early Cretaceous"},
    ],
    "confidence": 0.78,
}

# The two fragments of D1, and the two printed stems they hang off.
_FRAGMENTS = ["robustus Nishimura", "demissa Foreman"]

# Diaz & Velez 2018 Fig. 5: all 28 species names in payload order, verbatim.
_DIAZ_NAMES = [
    "Haplophragmoides spp.", "Haplophragmoides collyra",
    "Saccammina alexanderi", "Bolivina sp.", "Neobulimina sp.",
    "Pseudobolvina rollaensis", "Dorothia smokvensis", "Bathysiphon vitta",
    "Dorothia glabrata", "Reophax sp.", "Reophax spp.", "Marsonella oxycona",
    "Reophax deckeri", "Dictyomitra multicostata", "Dictyocephalus cf. cayeuxi",
    "Dactyliodiscus cf. lenticulatus", "Cryptamphorella conara", "Xitus sp.",
    "Pessagnobrachia cf. fabianii", "Crucella cf. messinae", "Paronaella sp.",
    "Orbiculiforma sp.", "Praeconocaryomma sp. (medullaria?)",
    "Spongodiscus? multus", "Gyroidina sp.", "Fursenkoina tegulata",
    "Gavelinella kansensis", "Haplophragmoides kirki",
]
# Every one of the 28 rows carries this, including the radiolarians drawn
# outside the pink foraminiferal block.
_DIAZ_BIOZONE = "Trochammina sp. Zone, Marsonella oxycona Subzone"


def _diaz_payload() -> dict:
    rows = []
    for name in _DIAZ_NAMES:
        row = {"species": name, "section": "Apache 141 Halton"}
        # Row 1 was read off the legend with no range drawn, so the payload
        # carries no biozone for it either; the tolerance for that blank is
        # part of what the uniform-biozone check has to get right.
        if name != "Haplophragmoides collyra":
            row["biozone"] = _DIAZ_BIOZONE
        rows.append(row)
    return {
        "species_ranges": rows,
        "biozones": [
            {"name": "Trochammina sp. Zone", "section": "Apache 141 Halton"},
            {"name": "Marsonella oxycona Subzone",
             "section": "Apache 141 Halton"},
        ],
        "confidence": 0.8,
    }


class TestWrappedHeaderGuard(unittest.TestCase):
    """D1: a two-line vertical column header must not become two taxa."""

    def test_wrapped_header_fragments_are_dropped_and_preserved(self):
        """The measured figure: 6 rows in, 4 out, both fragments removed."""
        r = normalize_result(_LIXIN_COL)
        names = [sp["species"] for sp in r["species_ranges"]]
        self.assertEqual(len(r["species_ranges"]), 4)
        for fragment in _FRAGMENTS:
            self.assertNotIn(fragment, names)
        # Nothing is silently lost: the dropped text is kept where the
        # operator can still read it.
        self.assertEqual(r["_extras"]["dropped_species_fragments"],
                         _FRAGMENTS)
        self.assertIn("wrapped_header_fragment_dropped", r["_warnings"])

    def test_surviving_rows_keep_their_own_printed_endpoints(self):
        """No endpoint is invented, merged or dropped while removing a row."""
        r = normalize_result(_LIXIN_COL)
        bekoma = [sp for sp in r["species_ranges"]
                  if sp["species"] == "Bekoma? cf. demissa"]
        self.assertEqual(len(bekoma), 2)
        self.assertEqual([(sp["range_top"], sp["range_base"]) for sp in bekoma],
                         [("ZN1-20 (Dengang Fm)", "ZN1-16 (Dengang Fm)"),
                          ("ZN1-15 (Dengang Fm)", "ZN1-15 (Dengang Fm)")])
        # The controls pass through byte-for-byte.
        self.assertIn("Orbula sp.", [sp["species"] for sp in r["species_ranges"]])
        self.assertIn("Lychnocanium carinatatum Ehrenberg",
                      [sp["species"] for sp in r["species_ranges"]])

    def test_no_row_is_flagged_when_nothing_was_split(self):
        """A clean payload gains no _warning and no _extras key at all.

        This is the "no behaviour change for figures that are fine" guard:
        every other consumer reads these two keys, so a normaliser that
        started emitting them unconditionally would change every result.
        """
        r = normalize_result({"species_ranges": [
            {"species": "Neoalbaillella optima", "range_top": "Bed 3"},
            {"species": "Orbula sp.", "range_top": "Bed 1"},
        ]})
        self.assertNotIn("_warnings", r)
        self.assertNotIn("_extras", r)
        self.assertEqual(len(r["species_ranges"]), 2)

    def test_provable_repair_rejoins_a_genus_and_its_epithet(self):
        """A genus and nothing else can only be completed by what follows."""
        r = normalize_result({"species_ranges": [
            {"species": "Bekoma?", "range_top": "ZN1-16"},
            {"species": "robusta", "range_top": "ZN1-16"},
            {"species": "Turbocapsula costata", "range_top": "ZN1-5"},
        ]})
        names = [sp["species"] for sp in r["species_ranges"]]
        self.assertEqual(names, ["Bekoma? robusta", "Turbocapsula costata"])
        self.assertEqual(r["species_ranges"][0]["_warning"],
                         "wrapped_header_joined")
        self.assertIn("wrapped_header_row_joined", r["_warnings"])
        # A repaired tail is still on the plate, in the row that now holds it,
        # so it is not reported as dropped.
        self.assertNotIn("dropped_species_fragments", r.get("_extras", {}))

    def test_repair_refuses_a_genus_level_open_nomenclature_base(self):
        """"Orbula sp." is complete on purpose -- nothing may be appended."""
        r = normalize_result({"species_ranges": [
            {"species": "Orbula sp.", "range_top": "ZN1-21"},
            {"species": "robusta Nishimura", "range_top": "ZN1-21"},
        ]})
        names = [sp["species"] for sp in r["species_ranges"]]
        self.assertEqual(names, ["Orbula sp."])
        self.assertNotIn("wrapped_header_row_joined", r.get("_warnings", []))

    def test_repair_refuses_an_ambiguous_bare_authority_base(self):
        """A lone capitalised word may be an authority, so never build on it."""
        self.assertTrue(_is_bare_authority("Foreman"))
        # But a genus WITH an authority is not itself a bare authority -- it is
        # the printed "genus + authority" form, and refusing it as a repair base
        # would make the repair unreachable for every authority-bearing column.
        self.assertFalse(_is_bare_authority("Bekoma Foreman"))
        r = normalize_result({"species_ranges": [
            {"species": "Foreman", "range_top": "ZN1-16"},
            {"species": "robusta Nishimura", "range_top": "ZN1-16"},
        ]})
        names = [sp["species"] for sp in r["species_ranges"]]
        self.assertEqual(names, ["Foreman"])
        self.assertNotIn("wrapped_header_row_joined", r.get("_warnings", []))

    def test_lone_lowercase_word_without_an_authority_is_left_alone(self):
        """The boundary the committed parity fixture rc_iron_rule_boundaries
        pins: "zozone" / "zoness" / "zone?" are NOT zone labels and MUST
        survive as rows. So an epithet alone is not evidence of a wrap."""
        for word in ("zozone", "zoness", "zone?"):
            with self.subTest(word=word):
                self.assertFalse(_is_wrapped_header_fragment(word))
                self.assertTrue(_is_bare_epithet(word))
        r = normalize_result({"species_ranges": [
            {"species": w, "range_top": "Bed 1"} for w in
            ("zozone", "zoness", "zone?", "zonule")
        ]})
        self.assertEqual([sp["species"] for sp in r["species_ranges"]],
                         ["zozone", "zoness", "zone?", "zonule"])
        self.assertNotIn("wrapped_header_fragment_dropped",
                         r.get("_warnings", []))

    def test_row_the_iron_rule_rejected_is_not_treated_as_a_fragment(self):
        """The two rules answer different questions; the pinned one wins.

        "Postouwia Zone" reads as genus-plus-authority to the name parser, so
        without the deferral it becomes a repair TARGET and swallows the
        fragment after it ("Postouwia Zone robustus Nishimura").
        """
        r = normalize_result({"species_ranges": [
            {"species": "Postouwia Zone", "range_top": "5"},
            {"species": "robustus Nishimura", "range_top": "4"},
        ]})
        names = [sp["species"] for sp in r["species_ranges"]]
        self.assertEqual(names, ["Postouwia Zone"])
        self.assertEqual(r["species_ranges"][0]["_warning"],
                         "iron_rule_zone_label")

    def test_iron_rule_rejected_tail_is_not_folded_into_its_base(self):
        """The same deferral on the TAIL side.

        "robusta Zone" is a SYNTHETIC shape: the name parser reads it as
        epithet-plus-authority (so it passes the fragment test) while the iron
        rule reads "Zone" as a zone label (so it is not a taxon at all).
        Neither reference figure contains it -- lixin_col's fragments are
        "robustus Nishimura" / "demissa Foreman" -- but the two rules
        colliding is reachable whenever a columnar track carries both taxon
        headers and zone labels in one list.
        """
        self.assertTrue(_is_wrapped_header_fragment("robusta Zone"))
        r = normalize_result({"species_ranges": [
            {"species": "Bekoma?", "range_top": "ZN1-16"},
            {"species": "robusta Zone", "range_top": "ZN1-16"},
        ]})
        names = [sp["species"] for sp in r["species_ranges"]]
        self.assertEqual(names, ["Bekoma?", "robusta Zone"])
        self.assertNotIn("wrapped_header_row_joined", r.get("_warnings", []))

    def test_non_latin_name_is_never_read_as_a_bare_epithet(self):
        """A CJK / Greek letter has no case, so `islower` calls it "not a
        capital" and the whole of this repair would fire on Chinese genus
        names -- the case this software exists for."""
        for name in ("图zone", "生物带zone", "αacme", "αrobustus Nishimura"):
            with self.subTest(name=name):
                self.assertFalse(_is_wrapped_header_fragment(name))
        r = normalize_result({"species_ranges": [
            {"species": "图robustus Nishimura", "range_top": "Bed 1"},
            {"species": "三叶虫", "range_top": "Bed 2"},
        ]})
        self.assertEqual(len(r["species_ranges"]), 2)
        self.assertNotIn("wrapped_header_fragment_dropped",
                         r.get("_warnings", []))

    def test_genus_level_labels_are_never_wrongly_treated_as_fragments(self):
        for name in ("Orbula sp.", "Reophax spp.", "Reophax", "Bekoma?"):
            with self.subTest(name=name):
                self.assertFalse(_is_wrapped_header_fragment(name))
        # "Orbula sp." is NOT a wrappable base either -- see the open
        # nomenclature rule in _is_wrappable_header_base.
        self.assertTrue(_is_wrappable_header_base("Bekoma?"))
        self.assertFalse(_is_wrappable_header_base("Orbula sp."))
        self.assertFalse(_is_wrappable_header_base("Bekoma? cf. demissa"))


class TestBiozoneUniformityGuard(unittest.TestCase):
    """D4: one biozone stamped on every row of a whole figure."""

    def test_uniform_biozone_raises_both_root_warnings(self):
        r = normalize_result(_diaz_payload())
        self.assertIn("biozone_uniform_across_figure", r["_warnings"])
        self.assertIn("biozone_spans_whole_figure", r["_warnings"])

    def test_spans_whole_figure_requires_the_exact_join(self):
        """A row carrying ONE real declared zone is uniform, but it is not
        claiming the whole figure's zone list -- the two warnings are
        different diagnoses and must not be merged."""
        payload = _diaz_payload()
        for row in payload["species_ranges"]:
            row["biozone"] = "Trochammina sp. Zone"
        r = normalize_result(payload)
        self.assertIn("biozone_uniform_across_figure", r["_warnings"])
        self.assertNotIn("biozone_spans_whole_figure", r.get("_warnings", []))

    def test_figure_with_several_zones_raises_neither_warning(self):
        """Li Xin et al. 2018a Fig. 4 spreads four zone labels over 20 rows."""
        r = normalize_result(_LIXIN_COL)
        for tag in ("biozone_uniform_across_figure",
                    "biozone_spans_whole_figure"):
            self.assertNotIn(tag, r.get("_warnings", []))

    def test_two_rows_are_too_few_to_call_a_figure_uniform(self):
        """The threshold exists so a two-row result that genuinely has one
        zone is not reported as a figure-wide stamp."""
        r = normalize_result({"species_ranges": [
            {"species": "Reophax sp.", "biozone": "Zone A"},
            {"species": "Bolivina sp.", "biozone": "Zone A"},
        ]})
        self.assertNotIn("biozone_uniform_across_figure",
                         r.get("_warnings", []))

    def test_diaz_fig5_row_set_is_untouched(self):
        """Zero fabrication, zero loss, on the measured figure.

        All 28 rows are plain names: none is a bare epithet, so the D1 guard
        must not touch this figure at all -- which is what makes the D4
        warnings safe to raise here.
        """
        r = normalize_result(_diaz_payload())
        self.assertEqual([sp["species"] for sp in r["species_ranges"]],
                         _DIAZ_NAMES)
        self.assertEqual(len(r["species_ranges"]), 28)
        self.assertNotIn("dropped_species_fragments", r.get("_extras", {}))
        # No row is mutated: the warning vocabulary is root-level precisely
        # so the row shape the aggregator votes on is unchanged.
        self.assertFalse([sp for sp in r["species_ranges"]
                          if sp.get("_warning")])

    def test_missing_diaz_labels_are_not_recoverable_from_the_payload(self):
        """D3, measured: the four bottom-of-figure labels the model never
        emitted are absent from every level of the payload, so no normaliser
        work can recover them -- and inventing them is forbidden. The only
        honest normaliser-side guarantee is that it neither drops nor adds a
        row the model did send."""
        for label in ("Reophax clavulina", "Muricohedbergella planispira",
                      "Neobulimina albertensis", "Planoheterohelix globulosa",
                      "Haplophragmoides linki"):
            self.assertNotIn(label, _DIAZ_NAMES)
        payload = _diaz_payload()
        r = normalize_result(payload)
        self.assertEqual(len(r["species_ranges"]), len(payload["species_ranges"]))


if __name__ == "__main__":
    unittest.main()
