"""Regression tests for P1-2: ICZN-style author normalization.
"Smith, 1950", "(Smith, 1950)", "Smith 1950", "Smith,1950" must dedup
together; different years must remain distinct.

REVIEW-2026-07-25 P1-2.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.aggregate import _norm_iczn_author, _extract_qualifiers, merge_results


class TestIcznNormalization:
    def test_basic_author_year_variants_normalize(self):
        variants = ["Smith, 1950", "(Smith, 1950)", "Smith 1950", "Smith,1950"]
        norms = {_norm_iczn_author(v) for v in variants}
        assert len(norms) == 1, (
            f"ICZN variants should collapse: {norms}"
        )

    def test_different_years_stay_distinct(self):
        a = _norm_iczn_author("Smith, 1950")
        b = _norm_iczn_author("Smith, 1960")
        assert a != b, "different years must NOT collapse"

    def test_et_al_handling(self):
        variants = [
            "Smith & Jones, 1950",
            "Smith and Jones, 1950",
            "(Smith & Jones, 1950)",
        ]
        norms = {_norm_iczn_author(v) for v in variants}
        assert len(norms) == 1, (
            f"author + year variants should collapse: {norms}"
        )

    def test_empty_input_returns_empty(self):
        assert _norm_iczn_author("") == ""
        assert _norm_iczn_author(None) == ""

    def test_merge_dedups_author_year_variants(self):
        """Two runs emitting the same species under different author-year
        string forms must merge into one row."""
        def _run(species):
            return {
                "sections": [], "species_ranges": [{
                    "species": species, "section": "X",
                    "range_top": "Bed 9", "range_base": "Bed 7",
                    "biozone": "B Zone", "author_year": species.split()[-2] + " " + species.split()[-1] if False else "",
                }],
                "biozones": [], "other_fossils": [], "confidence": 0.9,
            }
        # Two runs with same species 'Genus sp.' but author_year differs in form only.
        results = [
            {"sections": [], "species_ranges": [{
                "species": "Genus sp.", "section": "X",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone",
                "author_year": "Smith, 1950",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
            {"sections": [], "species_ranges": [{
                "species": "Genus sp.", "section": "X",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone",
                "author_year": "(Smith, 1950)",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
        ]
        merged = merge_results(results, total_runs=2)
        sp = merged.get("species_ranges", [])
        assert len(sp) == 1, (
            f"ICZN-variant rows must merge; got {len(sp)}: {sp}"
        )

    # F-3 (new): spp. vs sp. qualifier normalisation
    def test_spp_qualifier_matches_sp(self):
        """spp. and sp. both map to the 'sp.' qualifier group.

        ICZN open nomenclature: 'sp.' = single indeterminate specimen,
        'spp.' = multiple indeterminate specimens — taxonomically distinct
        content, but both collapse to the 'sp.' qualifier bucket so they
        DO NOT merge with bare 'Genus' (which has no qualifier).

        P0-6: output labels use the dotted form ("sp.", "spp.") per
        the extended ICZN marker specification.
        """
        from rca_core.aggregate import _extract_qualifiers
        assert _extract_qualifiers("Genus sp.") == frozenset({"sp."})
        assert _extract_qualifiers("Genus spp.") == frozenset({"spp."})
        assert _extract_qualifiers("Genus sp") == frozenset({"sp."})
        assert _extract_qualifiers("Genus spp") == frozenset({"spp."})
        # bare name has no qualifier
        assert _extract_qualifiers("Genus") == frozenset()
        # spp. does NOT collapse with bare genus
        assert _extract_qualifiers("Genus spp.") != _extract_qualifiers("Genus")

    def test_merge_spp_and_sp_do_not_merge_together(self):
        """Two runs: one emits 'Genus spp.' (plural) and one emits
        'Genus sp.' (singular).  Both normalise qualifier='sp' but
        _norm() preserves the full species name (sp./spp.) in the
        species_norm string, so dedup keys differ and they do NOT merge.
        This is correct ICZN behaviour: 'sp.' = single indeterminate
        specimen, 'spp.' = multiple indeterminate specimens."""
        results = [
            {"sections": [], "species_ranges": [{
                "species": "Genus spp.", "section": "X",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
            {"sections": [], "species_ranges": [{
                "species": "Genus sp.", "section": "X",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
        ]
        merged = merge_results(results, total_runs=2)
        sp = merged.get("species_ranges", [])
        # sp. and spp. have different species_norm strings -> different
        # dedup keys -> they do NOT merge (correct ICZN behaviour)
        assert len(sp) == 2, (
            f"'sp.' and 'spp.' must NOT merge (different species_norm); got {len(sp)}"
        )
        species_names = {row["species"] for row in sp}
        assert species_names == {"Genus spp.", "Genus sp."}

    def test_spp_and_bare_genus_do_not_merge(self):
        """'Genus spp.' (qualifier=sp) and 'Genus' (qualifier=none)
        have different dedup keys and must NOT merge."""
        results = [
            {"sections": [], "species_ranges": [{
                "species": "Genus spp.", "section": "X",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
            {"sections": [], "species_ranges": [{
                "species": "Genus", "section": "X",
                "range_top": "Bed 9", "range_base": "Bed 7",
                "biozone": "B Zone", "author_year": "",
            }], "biozones": [], "other_fossils": [], "confidence": 0.9},
        ]
        merged = merge_results(results, total_runs=2)
        sp = merged.get("species_ranges", [])
        assert len(sp) == 2, (
            f"'spp.' and bare genus must NOT merge; got {len(sp)} rows"
        )