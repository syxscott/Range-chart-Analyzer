"""P0-5 regression tests: chimera detection must include section.

_is_chimeric_row now uses (range_base, range_top, biozone, section) as
the chimera key. Same species in DIFFERENT sections is NOT a chimera — it
is a legitimate multi-section observation. Without section in the key,
two runs of the same species in two different sections would look like a
chimeric consensus.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.aggregate import merge_results


def _run(ranges):
    return {
        "sections": [], "species_ranges": list(ranges), "biozones": [],
        "other_fossils": [], "confidence": 0.9,
    }


class TestChimeraSectionKey:
    def test_same_species_same_section_twice_not_chimera(self):
        """Two runs of the same (species, section) with same range/biozone —
        the merged tuple IS observed → not chimeric."""
        results = [
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
        ]
        merged = merge_results(results, total_runs=2)
        sp_rows = merged.get("species_ranges", [])
        assert len(sp_rows) == 1, f"expected 1 row, got {len(sp_rows)}: {sp_rows}"
        assert "chimera_warnings" not in merged or not merged["chimera_warnings"]

    def test_same_species_different_sections_not_chimera(self):
        """P0-5 fix: same species in section A vs section B is NOT a chimera.
        Section is now part of the chimera key, so (species, A) and
        (species, B) are independent observations — no false chimera flag."""
        results = [
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "B",
                   "range_top": "Bed 11", "range_base": "Bed 8",
                   "biozone": "C Zone"}]),
        ]
        merged = merge_results(results, total_runs=2)
        sp_rows = merged.get("species_ranges", [])
        # Both rows should survive — different sections means different
        # dedup keys, and different FAD/LAD/biozone tuples survive because
        # section is now in the chimera check.
        assert len(sp_rows) == 2, (
            f"same species in different sections must NOT be chimeras; "
            f"got {len(sp_rows)} rows: {sp_rows}"
        )
        assert "chimera_warnings" not in merged or not merged["chimera_warnings"]

    def test_same_species_different_ranges_still_chimera(self):
        """Same species + same section but disagreeing ranges IS a chimera.
        The merged (FAD/LAD/biozone) tuple was never observed in any run."""
        results = [
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 11", "range_base": "Bed 6",
                   "biozone": "C Zone"}]),
        ]
        merged = merge_results(results, total_runs=2)
        sp_rows = merged.get("species_ranges", [])
        # AUDIT-2026-09-27 P1: this used to assert `len(sp_rows) == 0`. The
        # DETECTION is unchanged (no run observed the merged tuple) but the
        # CONSEQUENCE moved: the row is kept and flagged, because `_mode`
        # breaks a 1-1 tie with sorted-first-wins, so a 2-run disagreement on
        # >= 2 fields ALWAYS looks "unobserved". Executed before the fix: two
        # runs that BOTH saw "Genus sp." merged to zero rows, so raising
        # `runs` — the documented way to make an extraction more reliable —
        # deleted the taxon, and `chimera_warnings` was read by gui_fluent.py
        # alone so server.py and the whole browser path never saw it vanish.
        assert len(sp_rows) == 1, (
            f"a recombined row must SURVIVE, flagged, not be dropped; got {sp_rows}"
        )
        assert sp_rows[0].get("_warning") == "recombined_consensus", (
            f"the surviving row must be flagged; got {sp_rows[0].get('_warning')}"
        )
        ballots = sp_rows[0].get("_recombination_ballots") or []
        assert len(ballots) == 2, f"both readings must be recorded; got {ballots}"
        warnings = merged.get("chimera_warnings", [])
        assert len(warnings) == 1, f"expected 1 chimera warning, got {warnings}"
        # The operator gets the same information the old drop-site gave the
        # Fluent GUI, and now every consumer does.
        assert len(warnings[0].get("ballots") or []) == 2, (
            f"the warning must carry the ballots; got {warnings[0]}"
        )
        assert "not dropped" in warnings[0]["reason"]

    def test_different_species_same_section_different_ranges_still_chimera(self):
        """P0-5: Two runs of the same species in the same section with
        disagreeing (FAD, LAD, biozone) tuples — the merged tuple was never
        observed by any single run, so the row is FLAGGED.
        This verifies chimera detection still fires even when section is now
        part of the key. AUDIT-2026-09-27 P1: flagged-and-kept, not deleted —
        see test_same_species_different_ranges_still_chimera for why."""
        results = [
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 9", "range_base": "Bed 7",
                   "biozone": "B Zone"}]),
            _run([{"species": "Genus sp.", "section": "A",
                   "range_top": "Bed 11", "range_base": "Bed 6",
                   "biozone": "C Zone"}]),
        ]
        merged = merge_results(results, total_runs=2)
        sp_rows = merged.get("species_ranges", [])
        # Disagreeing ranges + biozone for same (species, section) → flagged
        assert len(sp_rows) == 1, (
            f"a recombined row must survive and be flagged; got {sp_rows}"
        )
        assert sp_rows[0].get("_warning") == "recombined_consensus"
        warnings = merged.get("chimera_warnings", [])
        assert len(warnings) == 1, f"expected 1 chimera warning, got {warnings}"
