"""P0-6 regression tests: ICZN open-nomenclature qualifiers extended
to 10 common markers.

Patterns added: ex gr., s.l., s.str., nom., comb. nov., stat. nov.,
subsp., var. (alongside existing sp., spp., cf., aff., ?).

REVIEW-2026-07-25 P0-6.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.aggregate import _extract_qualifiers, merge_results


class TestIcznQualifiersExtended:
    """Test _extract_qualifiers with the extended 10-marker set."""

    def test_ex_gr(self):
        """ex gr. (ex grege = from the group/cluster) normalizes to 'ex gr.'."""
        assert _extract_qualifiers("Genus ex gr. species") == frozenset({"ex gr."})
        assert _extract_qualifiers("Genus ex gr species") == frozenset({"ex gr."})
        assert _extract_qualifiers("Genus ex group. species") == frozenset({"ex gr."})
        assert _extract_qualifiers("Genus ex GROUP species") == frozenset({"ex gr."})

    def test_sensu_lato(self):
        """s.l. (sensu lato = in the broad sense) normalizes to 's.l.'."""
        assert _extract_qualifiers("Genus s.l. species") == frozenset({"s.l."})
        assert _extract_qualifiers("Genus s.l species") == frozenset({"s.l."})
        assert _extract_qualifiers("Genus s. l. species") == frozenset({"s.l."})

    def test_sensu_stricto(self):
        """s.str. (sensu stricto = in the strict sense) normalizes to 's.str.'."""
        assert _extract_qualifiers("Genus s.str. species") == frozenset({"s.str."})
        assert _extract_qualifiers("Genus s.str species") == frozenset({"s.str."})
        assert _extract_qualifiers("Genus s. str. species") == frozenset({"s.str."})

    def test_sp_qualifier(self):
        """sp. (species indeterminata) normalizes to 'sp.'."""
        assert _extract_qualifiers("Genus sp.") == frozenset({"sp."})
        assert _extract_qualifiers("Genus sp") == frozenset({"sp."})
        # Case insensitive
        assert _extract_qualifiers("Genus SP.") == frozenset({"sp."})

    def test_spp_qualifier(self):
        """spp. (species plural indeterminatae) normalizes to 'spp.'."""
        assert _extract_qualifiers("Genus spp.") == frozenset({"spp."})
        assert _extract_qualifiers("Genus spp") == frozenset({"spp."})
        assert _extract_qualifiers("Genus SPP.") == frozenset({"spp."})

    def test_cf_qualifier(self):
        """cf. (confer = compare to) normalizes to 'cf.'."""
        assert _extract_qualifiers("Genus cf. species") == frozenset({"cf."})
        assert _extract_qualifiers("Genus cf species") == frozenset({"cf."})
        assert _extract_qualifiers("Genus CF. species") == frozenset({"cf."})

    def test_aff_qualifier(self):
        """aff. (affinis = related to) normalizes to 'aff.'."""
        assert _extract_qualifiers("Genus aff. species") == frozenset({"aff."})
        assert _extract_qualifiers("Genus aff species") == frozenset({"aff."})
        assert _extract_qualifiers("Genus AFF. species") == frozenset({"aff."})

    def test_uncertain_marker(self):
        """"?" at end normalizes to '?'."""
        assert _extract_qualifiers("Genus sp. ?") == frozenset({"sp.", "?"})
        assert _extract_qualifiers("Genus ?") == frozenset({"?"})
        # "Genus sp?" has sp + ? as separate tokens (no space): both fire
        assert _extract_qualifiers("Genus sp?") == frozenset({"sp.", "?"})

    def test_nom_markers(self):
        """nom. (nomen) with sub-types: dub/nud/nov/cons/obl/rej/van."""
        assert _extract_qualifiers("Genus nom. dub.") == frozenset({"nom. dub"})
        assert _extract_qualifiers("Genus nom. nud.") == frozenset({"nom. nud"})
        assert _extract_qualifiers("Genus nom. nov.") == frozenset({"nom. nov"})
        assert _extract_qualifiers("Genus nom. cons.") == frozenset({"nom. cons"})
        assert _extract_qualifiers("Genus nom. obl.") == frozenset({"nom. obl"})
        assert _extract_qualifiers("Genus nom. rej.") == frozenset({"nom. rej"})
        assert _extract_qualifiers("Genus nom. van.") == frozenset({"nom. van"})
        # Without sub-type, nom. alone is not captured by the pattern
        assert "nom." not in _extract_qualifiers("Genus nom. species")

    def test_comb_nov(self):
        """comb. nov. (combinatio nova = new combination) normalizes to 'comb. nov.'."""
        assert _extract_qualifiers("Genus comb. nov. species") == frozenset({"comb. nov."})
        assert _extract_qualifiers("Genus comb nov. species") == frozenset({"comb. nov."})
        assert _extract_qualifiers("Genus comb. nov species") == frozenset({"comb. nov."})

    def test_stat_nov(self):
        """stat. nov. (status novus = new status) normalizes to 'stat. nov.'."""
        assert _extract_qualifiers("Genus stat. nov. species") == frozenset({"stat. nov."})
        assert _extract_qualifiers("Genus stat nov. species") == frozenset({"stat. nov."})
        assert _extract_qualifiers("Genus stat. nov species") == frozenset({"stat. nov."})

    def test_subsp_qualifier(self):
        """subsp. (subspecies) normalizes to 'subsp.'."""
        assert _extract_qualifiers("Genus subsp. subspecies") == frozenset({"subsp."})
        assert _extract_qualifiers("Genus subsp subspecies") == frozenset({"subsp."})

    def test_var_qualifier(self):
        """var. (varietas/variety) normalizes to 'var.'."""
        assert _extract_qualifiers("Genus var. variety") == frozenset({"var."})
        assert _extract_qualifiers("Genus var variety") == frozenset({"var."})

    def test_multiple_qualifiers(self):
        """A name can have multiple qualifiers simultaneously."""
        quals = _extract_qualifiers("Genus ex gr. cf. species")
        assert "ex gr." in quals
        assert "cf." in quals

    def test_no_qualifier_bare_name(self):
        """Bare genus/species with no qualifier returns empty set."""
        assert _extract_qualifiers("Genus species") == frozenset()
        assert _extract_qualifiers("Genus") == frozenset()

    def test_merge_ex_gr_runs_dedup(self):
        """Two runs of same species with ex gr. qualifier should dedup."""
        results = [
            {"sections": [], "species_ranges": [{
                "species": "Genus ex gr. species", "section": "A",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
            {"sections": [], "species_ranges": [{
                "species": "Genus ex gr. species", "section": "A",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
        ]
        merged = merge_results(results, total_runs=2)
        sp = merged.get("species_ranges", [])
        assert len(sp) == 1, f"ex gr. runs must merge; got {len(sp)}: {sp}"
        assert sp[0]["species"] == "Genus ex gr. species"

    def test_merge_s_l_and_bare_dont_merge(self):
        """Species with s.l. qualifier must NOT merge with bare species."""
        results = [
            {"sections": [], "species_ranges": [{
                "species": "Genus s.l. species", "section": "A",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
            {"sections": [], "species_ranges": [{
                "species": "Genus species", "section": "A",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
        ]
        merged = merge_results(results, total_runs=2)
        sp = merged.get("species_ranges", [])
        # s.l. and bare genus have different qualifiers → different dedup keys
        assert len(sp) == 2, (
            f"s.l. qualifier must prevent merge with bare species; got {len(sp)}"
        )
