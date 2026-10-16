"""occurrence_matrix: cell provenance, the empty-response gate, transposition.

2026-10-16. The trigger for all of this is one measurement on a real 22-row x
18-column occurrence matrix, scored against a hand-checked CSV of all 93
filled cells:

    labels   19/22 taxa, 19/19 authorities, 11/11 zones, legend all correct
    cells    0.43 accuracy at 0.26 coverage, and the failure was OMISSION
             (33 cells never emitted) rather than misreading (only 4)

So the result object was half trustworthy and gave its reader no way to know
which half. These tests pin the three mechanisms that fix that, and pin them
against the ways each could silently stop working:

  * ``cell_status`` records where the cells came from, and is taken from the
    CALLER's code path rather than from the model's payload.
  * A near-empty body is rejected instead of being reported as an empty
    figure.
  * A mostly-transposed table is called out by distribution, not by any single
    row.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core import extractor  # noqa: E402
from rca_core.exporter import _occurrence_matrix_tables  # noqa: E402
from rca_core.extractor import (  # noqa: E402
    DEFAULT_OCCURRENCE_CELL_STATUS,
    OCCURRENCE_CELL_STATUSES,
    OCCURRENCE_CELL_STRATEGIES,
    OCCURRENCE_DEFAULT_CELL_STRATEGY,
    OCCURRENCE_MIN_OUTPUT_TOKENS,
    OCCURRENCE_SELF_STATUS_STRATEGIES,
    OCCURRENCE_STATUS_BY_STRATEGY,
    _truncation_warning,
    _usage_int,
    extract_occurrence_matrix,
    normalize_occurrence_matrix_result,
)

# A real-shaped payload: three locality columns, two taxon rows, one filled
# cell in each. Small enough to reason about, structurally complete.
_PAYLOAD = {
    "columns": [
        {"index": 0, "label": "87 KT-A19", "kind": "locality"},
        {"index": 1, "label": "87 KT-A19A", "kind": "locality"},
        {"index": 2, "label": "CAA-A27", "kind": "locality"},
    ],
    "legend": [{"code": "A", "class": "abundant"},
               {"code": "C", "class": "common"}],
    "occurrences": [
        {"taxon": "Capnodoce", "authority": "Moser", "row_kind": "taxon",
         "site": "87 KT-A19", "col_index": 0, "code": "A", "class": "abundant"},
        {"taxon": "Capnodoce", "authority": "Moser", "row_kind": "taxon",
         "site": "CAA-A27", "col_index": 2, "code": "C", "class": "common"},
    ],
    "strat_rows": [],
    "confidence": 0.8,
}

_EMPTY_PAYLOAD = {
    "columns": [], "legend": [], "occurrences": [],
    "strat_rows": [], "confidence": 0.0,
}


def _run(monkeypatch, payload, *, out_tokens, strategy=None,
         usage_reported=True):
    """Drive extract_occurrence_matrix with the transport faked out.

    Returns the ExtractResult. ``out_tokens`` is what the provider claims it
    produced; ``usage_reported=False`` simulates a provider that omits the
    block entirely, which must disable the gate rather than default to zero.
    """
    import json

    def fake_call_llm(**kwargs):
        usage = {"input_tokens": 1200, "output_tokens": out_tokens}
        if not usage_reported:
            usage = None
        return json.dumps(payload), False, 200, "", usage

    monkeypatch.setattr(extractor, "_call_llm", fake_call_llm)
    kwargs = {"api_key": "k", "image_b64": "aGk=", "media_type": "image/png"}
    if strategy is not None:
        kwargs["cell_strategy"] = strategy
    return extract_occurrence_matrix(**kwargs)


# --- cell_status -----------------------------------------------------------

class TestCellStatus:
    def test_default_claims_only_what_it_earned(self):
        """The default must be a MEASURED path, and its status must match.

        Originally this asserted the default was ``single_pass``, so that a
        caller who says nothing gets the honest unverified label. That premise
        was correct on 2026-10-16 and is now superseded: the two-stage
        ``grid_read`` path measures precision 1.000 / recall 1.000 over five
        consecutive runs on the reference matrix, minimum 1.000 across those
        runs.

        The intent is kept, not dropped. What the test still forbids is a
        default that advertises verification it cannot back:

          * the default must be a known strategy, not an unrecognised string;
          * it must not be a path whose status is a lie -- `grid_read` is
            deliberately ABSENT from OCCURRENCE_STATUS_BY_STRATEGY, because it
            rewrites the status to whatever actually ran, and a mapping here
            would freeze `grid_cells` onto a fallback answer;
          * and the module-level default status stays the unverified one, so a
            caller that bypasses the strategy table still gets the honest label.
        """
        assert OCCURRENCE_DEFAULT_CELL_STRATEGY in OCCURRENCE_CELL_STRATEGIES
        assert OCCURRENCE_DEFAULT_CELL_STRATEGY not in \
            OCCURRENCE_STATUS_BY_STRATEGY, (
            "a strategy in the status table is stamped blindly; grid_read "
            "must decide its own status from which path actually ran")
        assert DEFAULT_OCCURRENCE_CELL_STATUS == "single_pass_unverified"

    def test_normalizer_stamps_the_status_it_is_handed(self):
        out = normalize_occurrence_matrix_result(_PAYLOAD,
                                                 cell_status="grid_cells")
        assert out["cell_status"] == "grid_cells"

    def test_normalizer_rejects_an_unknown_status(self):
        """An unrecognised value falls back rather than travelling to the
        exporter, where the title logic compares it against "grid_cells" and
        would otherwise treat a typo as merely-unverified by accident."""
        out = normalize_occurrence_matrix_result(_PAYLOAD,
                                                 cell_status="totally_verified")
        assert out["cell_status"] == DEFAULT_OCCURRENCE_CELL_STATUS

    def test_model_cannot_claim_its_own_provenance(self):
        """The payload's own cell_status must not win.

        The model has no way to know whether its cell coordinates were checked
        against the figure's geometry. Reading this from the payload would let
        a prompt that wished for "grid_cells" simply assert it.
        """
        payload = dict(_PAYLOAD)
        payload["cell_status"] = "grid_cells"
        out = normalize_occurrence_matrix_result(payload,
                                                 cell_status="single_pass_unverified")
        assert out["cell_status"] == "single_pass_unverified"
        assert "grid_cells" not in out.values()

    def test_single_pass_can_never_earn_grid_cells(self):
        """Whatever the answer looks like, the strategy caps the claim."""
        assert OCCURRENCE_STATUS_BY_STRATEGY["single_pass"] == \
            "single_pass_unverified"
        for status in OCCURRENCE_STATUS_BY_STRATEGY.values():
            assert status in OCCURRENCE_CELL_STATUSES

    def test_strategy_and_status_cover_each_other(self):
        """Every strategy either has a status or declares that it chooses one.

        Originally this asserted a flat bijection, which was the right shape
        while every strategy mapped one-to-one onto a status. `grid_read` cannot:
        it runs the two-stage path and, when that fails, degrades to
        `single_pass`. A single table entry would freeze `grid_cells` onto the
        fallback answer, advertising geometric verification for a run that had
        none.

        So the invariant becomes a partition -- nothing may be unaccounted for --
        rather than a bijection, and the uniqueness clause is checked only over
        the strategies that DO claim a fixed status, where it still means what
        it was written to mean.
        """
        fixed = set(OCCURRENCE_STATUS_BY_STRATEGY)
        self_selecting = set(OCCURRENCE_SELF_STATUS_STRATEGIES)
        all_strategies = set(OCCURRENCE_CELL_STRATEGIES)
        assert fixed | self_selecting == all_strategies, (
            "a strategy is neither mapped to a status nor declared as choosing "
            "one, so its status would be decided by accident")
        assert not (fixed & self_selecting), (
            "a strategy cannot both have a fixed status and choose its own")
        assert len(set(OCCURRENCE_STATUS_BY_STRATEGY.values())) == len(fixed), \
            "two strategies share a status, so one of them is indistinguishable"
        assert "grid_read" in self_selecting, (
            "grid_read degrades to single_pass, so it must choose its status")

    def test_result_carries_the_status_through_the_extractor(self, monkeypatch):
        res = _run(monkeypatch, _PAYLOAD, out_tokens=5000)
        assert res.ok
        assert res.data["cell_status"] == "single_pass_unverified"

    def test_an_unknown_strategy_falls_back_instead_of_crashing(self, monkeypatch):
        """A config typo must degrade to the default path, not raise.

        This is a keyword-only argument threaded from config through the GUI
        and the server, so an unvalidated string is reachable in practice.
        """
        res = _run(monkeypatch, _PAYLOAD, out_tokens=5000,
                   strategy="single-pass")
        assert res.ok
        assert res.data["cell_status"] == "single_pass_unverified"


# --- the empty-response gate ------------------------------------------------

class TestEmptyResponseGate:
    def test_the_floor_sits_below_the_smallest_good_run(self):
        """Anchored to a measurement, not chosen for convenience.

        Measured output lengths on this figure: 4190 / 4400 / 5587 / 5428 for
        normal runs, 1755 for a deliberately compacted one, and 528 / 40 for
        the two that produced nothing. The floor must clear 1755 or a correct
        terse answer gets rejected, and must sit above 528 or the failure gets
        through.
        """
        assert OCCURRENCE_MIN_OUTPUT_TOKENS == 1600
        assert 1600 < 1755
        assert 1600 > 528

    def test_a_forty_token_body_is_rejected(self, monkeypatch):
        res = _run(monkeypatch, _EMPTY_PAYLOAD, out_tokens=40)
        assert not res.ok
        assert res.error_key == "err.emptyResponse"

    def test_the_rejection_explains_itself(self, monkeypatch):
        """The warning must name the numbers, or the user cannot tell a
        transient failure from a genuine empty figure."""
        res = _run(monkeypatch, _EMPTY_PAYLOAD, out_tokens=528)
        assert not res.ok
        assert "528" in res.warning
        assert str(OCCURRENCE_MIN_OUTPUT_TOKENS) in res.warning

    def test_a_real_body_of_the_same_measured_length_is_accepted(self,
                                                                monkeypatch):
        """528 tokens AND no occurrences is the signal; either alone is not."""
        res = _run(monkeypatch, _PAYLOAD, out_tokens=528)
        assert res.ok, "a short body that carries rows is doing its job"

    def test_the_smallest_measured_good_run_passes(self, monkeypatch):
        res = _run(monkeypatch, _PAYLOAD, out_tokens=1755)
        assert res.ok

    def test_no_occurrences_above_the_floor_is_not_gated(self, monkeypatch):
        """A genuinely sparse figure is a legitimate answer in this mode.

        Gating on the absence of occurrences alone would reject a real matrix
        whose cells are all blank, which is the one case the mode's sparsity
        contract exists to express.
        """
        res = _run(monkeypatch, _EMPTY_PAYLOAD, out_tokens=5000)
        assert res.ok
        assert res.data["occurrences"] == []

    def test_a_provider_that_reports_no_usage_skips_the_gate(self, monkeypatch):
        """Nothing to judge on. Defaulting a missing block to zero would turn
        every silent provider into a permanent failure."""
        res = _run(monkeypatch, _EMPTY_PAYLOAD, out_tokens=0,
                   usage_reported=False)
        assert res.ok
        assert res.error_key != "err.emptyResponse"

    def test_zero_is_a_reporting_not_a_measurement(self):
        """A provider that reports 0 did report. It is not the same as
        omitting the field, and conflating them would make the gate fire on
        a provider that simply does not count."""
        assert _usage_int({"output_tokens": 0}, "output_tokens") == 0
        assert _usage_int({}, "output_tokens") is None
        assert _usage_int(None, "output_tokens") is None

    def test_the_completion_spelling_is_accepted(self):
        assert _usage_int({"completion_tokens": 4321}, "output_tokens") == 4321

    def test_a_junk_usage_value_is_absent_not_zero(self):
        assert _usage_int({"output_tokens": "many"}, "output_tokens") is None


# --- transposition ----------------------------------------------------------

def _payload_with_unknown_columns(total, unknown):
    rows = []
    for i in range(total):
        unknown_site = i < unknown
        rows.append({
            "taxon": "Capnodoce" if unknown_site else "Capmuchosphaera",
            "authority": "Moser", "row_kind": "taxon",
            "site": "NOT-A-COLUMN" if unknown_site else "87 KT-A19",
            "col_index": 0, "code": "A", "class": "abundant",
        })
    return {"columns": [{"index": 0, "label": "87 KT-A19", "kind": "locality"},
                        {"index": 1, "label": "CAA-A27", "kind": "locality"},
                        {"index": 2, "label": "87 B1-4C", "kind": "locality"}],
            "legend": [], "occurrences": rows, "strat_rows": [],
            "confidence": 0.5}


class TestTransposition:
    def test_a_mostly_unknown_table_is_called_out(self):
        out = normalize_occurrence_matrix_result(
            _payload_with_unknown_columns(total=10, unknown=7))
        joined = " ".join(out.get("warnings") or [])
        assert "transposed" in joined

    def test_exactly_half_does_not_fire(self):
        """`unknown * 2 > total` is strict. At the boundary the table is a
        coin flip and firing would be a coin flip too."""
        out = normalize_occurrence_matrix_result(
            _payload_with_unknown_columns(total=10, unknown=5))
        assert "transposed" not in " ".join(out.get("warnings") or [])

    def test_a_stray_misread_header_does_not_fire(self):
        """One unknown row is one bad header, already reported per-row as
        uncertain. Calling that a transposed matrix cries wolf."""
        out = normalize_occurrence_matrix_result(
            _payload_with_unknown_columns(total=10, unknown=1))
        assert "transposed" not in " ".join(out.get("warnings") or [])

    def test_a_few_taxon_matrix_is_not_called_transposed(self):
        """The check that was deliberately rejected.

        "Fewer taxa than columns" looks like an orientation signal and is not:
        a 4-taxon x 12-locality matrix is an ordinary figure. This pins that
        the shipped check is the distribution of unknown columns instead.

        The row count is 4, not 2. At 2 rows the `len(occurrences) >= 3` floor
        stands the whole check down, so a two-row payload would pass for the
        wrong reason and would not notice a replacement check that reads the
        taxon/column ratio - which is exactly what the mutation harness tried
        and initially failed to kill because of this.
        """
        taxa = ["Capnodoce", "Capmuchosphaera", "Ozsvartella",
                "Archaeocanthocircus"]
        payload = {"columns": [{"index": i, "label": f"Site {i}",
                                "kind": "locality"} for i in range(12)],
                   "legend": [],
                   "occurrences": [
                       {"taxon": name, "authority": "Moser",
                        "row_kind": "taxon", "site": f"Site {i}",
                        "col_index": i, "code": "A", "class": "abundant"}
                       for i, name in enumerate(taxa)],
                   "strat_rows": [], "confidence": 0.5}
        out = normalize_occurrence_matrix_result(payload)
        assert len(out["columns"]) == 12
        assert len(out["occurrences"]) == 4
        assert len({r["taxon"] for r in out["occurrences"]}) < \
            len(out["columns"]), "the premise of this test must hold"
        assert "transposed" not in " ".join(out.get("warnings") or [])

    def test_too_few_rows_to_diagnose(self):
        """Two rows cannot establish a distribution. Below three the check
        stands down rather than reading noise as a pattern."""
        out = normalize_occurrence_matrix_result(
            _payload_with_unknown_columns(total=2, unknown=2))
        assert "transposed" not in " ".join(out.get("warnings") or [])

    def test_the_warning_says_the_labels_are_still_usable(self):
        """The whole point is not to throw away the half that measured well.
        A warning that reads as 'this result is void' would make the user
        re-run a figure whose taxon and zone labels were 100% correct."""
        out = normalize_occurrence_matrix_result(
            _payload_with_unknown_columns(total=10, unknown=7))
        joined = " ".join(out.get("warnings") or [])
        assert "usable" in joined


# --- the truncation warning -------------------------------------------------

class TestTruncationWarning:
    def test_it_names_all_three_numbers(self):
        """Actual usage, the run's own limit, and the program's ceiling.

        Before this the sentence said "try raising max_tokens" and nothing
        else. Reading that as "the cap was hit, more will not help" is wrong:
        the 16058-token case hit DEFAULT_MAX_TOKENS=16000, and the clamp still
        allowed 32000.
        """
        msg = _truncation_warning({"output_tokens": 16058}, 16000)
        assert "16058" in msg
        assert "16000" in msg
        assert str(extractor.MAX_MAX_TOKENS) in msg

    def test_a_missing_usage_block_says_so_rather_than_showing_none(self):
        msg = _truncation_warning(None, 16000)
        assert "None" not in msg
        assert "16000" in msg

    def test_the_real_cap_is_reported_not_a_literal(self):
        """Pinning MAX_MAX_TOKENS by value would let the constant drift while
        the message kept promising the old ceiling.

        Only the CODE is scanned. The docstring quotes 32000 on purpose, as
        the worked example of the mistake this sentence exists to prevent, and
        a test that flagged that would be flagging the explanation.
        """
        import ast
        import re
        source = open(extractor.__file__, encoding="utf-8").read()
        tree = ast.parse(source)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_truncation_warning")
        body = ast.dump(fn.body[0])  # the Expr holding the docstring is body[0]
        code_only = "".join(ast.dump(stmt) for stmt in fn.body[1:])
        assert "MAX_MAX_TOKENS" in code_only
        assert "32000" not in code_only, (
            "the cap is hard-coded instead of read from MAX_MAX_TOKENS")
        assert body  # the docstring is still there and was excluded above


# --- the exported sheet -----------------------------------------------------

class TestExportTitle:
    def _title(self, data):
        for cfg in _occurrence_matrix_tables(data):
            if cfg["id"] == "occurrences":
                return cfg["title_key"]
        raise AssertionError("no occurrences config returned")

    def test_verified_cells_get_the_plain_title(self):
        assert self._title({"cell_status": "grid_cells",
                            "occurrences": [], "columns": []}) == \
            "sec.occurrences"

    def test_unverified_cells_get_the_caveated_title(self):
        assert self._title({"cell_status": "single_pass_unverified",
                            "occurrences": [], "columns": []}) == \
            "sec.occurrencesUnverified"

    def test_not_attempted_gets_the_caveated_title(self):
        assert self._title({"cell_status": "not_attempted",
                            "occurrences": [], "columns": []}) == \
            "sec.occurrencesUnverified"

    def test_an_absent_status_gets_the_caveated_title(self):
        """Merged results drop scalar root keys, and older payloads predate the
        field. Absence means unverified, not verified."""
        assert self._title({"occurrences": [], "columns": []}) == \
            "sec.occurrencesUnverified"

    def test_the_table_count_is_unchanged(self):
        """The caveat rides the title on purpose. Adding a fourth table would
        break the pinned table count that _looks_occurrence_matrix asserts."""
        assert len(_occurrence_matrix_tables(
            {"cell_status": "single_pass_unverified", "occurrences": [],
             "columns": [], "legend": [], "strat_rows": []})) == 3


# --- the strategy wiring ----------------------------------------------------

class TestStrategyPrompts:
    """Which question each strategy asks, and with what help.

    The three are genuinely different designs, not three lengths of the same
    prompt. `single_pass` asks for everything and gets no help. `gated` asks
    for the half that measured well and names the omission. `cells` asks for
    everything AND hands over locally measured pixel spans, because the
    measured failure was column IDENTITY (a -3..+3 shift scan scored zero at
    every offset), not misalignment.
    """

    def _prompt_for(self, monkeypatch, strategy, geom="__default__"):
        seen = {}

        def fake_call_llm(**kwargs):
            seen["system"] = kwargs["system_prompt"]
            seen["user"] = kwargs["user_text"]
            import json
            return json.dumps(_PAYLOAD), False, 200, "", {
                "input_tokens": 900, "output_tokens": 5000}

        monkeypatch.setattr(extractor, "_call_llm", fake_call_llm)
        if geom != "__default__":
            # `extract_occurrence_matrix` does `from .grid import detect_grid`
            # INSIDE the function body, so a name set on this module is
            # shadowed by that import and the patch would silently do nothing -
            # the test would then pass against whatever the real detector
            # returned for a 3-byte base64 string (None), which is the same
            # answer as the negative case and cannot tell them apart.
            import rca_core.grid as grid_mod
            monkeypatch.setattr(grid_mod, "detect_grid", lambda _b: geom)
        res = extract_occurrence_matrix(api_key="k", image_b64="aGk=",
                                        media_type="image/png",
                                        cell_strategy=strategy)
        return seen["system"], seen["user"], res

    def test_single_pass_keeps_the_matrix_prompt(self, monkeypatch):
        from rca_core.prompt import OCCURRENCE_MATRIX_SYSTEM_PROMPT
        sys_prompt, user, _ = self._prompt_for(monkeypatch, "single_pass")
        assert sys_prompt == OCCURRENCE_MATRIX_SYSTEM_PROMPT
        assert "one row per FILLED cell" in user
        assert "GRID GEOMETRY" not in user

    def test_gated_asks_the_shorter_question(self, monkeypatch):
        from rca_core.prompt import (OCCURRENCE_LABELS_SYSTEM_PROMPT,
                                     OCCURRENCE_MATRIX_SYSTEM_PROMPT)
        sys_prompt, user, _ = self._prompt_for(monkeypatch, "gated")
        assert sys_prompt == OCCURRENCE_LABELS_SYSTEM_PROMPT
        assert sys_prompt != OCCURRENCE_MATRIX_SYSTEM_PROMPT
        assert "Do NOT read the grid cells" in user

    def test_the_labels_prompt_is_materially_shorter(self, monkeypatch):
        """The whole premise of `gated` is subtraction. If the two prompts ever
        converge, the strategy is a rename and nothing else."""
        from rca_core.prompt import (OCCURRENCE_LABELS_SYSTEM_PROMPT,
                                     OCCURRENCE_MATRIX_SYSTEM_PROMPT)
        assert len(OCCURRENCE_LABELS_SYSTEM_PROMPT) < \
            0.75 * len(OCCURRENCE_MATRIX_SYSTEM_PROMPT)

    def test_cells_gets_the_matrix_prompt_plus_geometry(self, monkeypatch):
        from rca_core.grid import GridGeometry
        from rca_core.prompt import OCCURRENCE_MATRIX_SYSTEM_PROMPT
        geom = GridGeometry(
            n_cols=2, n_rows=2,
            col_spans=((315, 339), (339, 363)),
            row_spans=((160, 177), (177, 194)),
            header_span=(19, 160), label_span=(7, 315), confidence=0.97)
        sys_prompt, user, res = self._prompt_for(monkeypatch, "cells",
                                                 geom=geom)
        assert sys_prompt == OCCURRENCE_MATRIX_SYSTEM_PROMPT
        assert "GRID GEOMETRY" in user
        assert "[0]=x315-339" in user
        assert "[1]=y177-194" in user
        assert "x7-315" in user, "the label band must be excluded by name"
        assert "0.97" in user
        assert res.data["cell_status"] == "grid_cells", \
            "geometry was supplied, so the status may claim verification"

    def test_cells_says_it_verified_nothing_when_detection_fails(self,
                                                                  monkeypatch):
        """The honesty condition. "No grid detected" is precisely when the
        cells are LEAST trustworthy, so that is exactly when the result must
        not claim they were geometrically verified."""
        from rca_core.prompt import OCCURRENCE_MATRIX_SYSTEM_PROMPT
        sys_prompt, user, res = self._prompt_for(monkeypatch, "cells",
                                                 geom=None)
        assert sys_prompt == OCCURRENCE_MATRIX_SYSTEM_PROMPT
        assert "GRID GEOMETRY" not in user
        assert res.data["cell_status"] == "single_pass_unverified", \
            "a failed detection must NOT leave the status claiming grid_cells"

    def test_a_detector_that_raises_does_not_take_the_run_down(self,
                                                               monkeypatch):
        """Third-party image decoding can fail on inputs this mode would
        otherwise handle. The extraction must still return something."""
        import json
        import rca_core.grid as grid_mod

        def boom(_b):
            raise ValueError("cannot decode")

        monkeypatch.setattr(extractor, "_call_llm",
                            lambda **kw: (json.dumps(_PAYLOAD), False, 200, "",
                                          {"output_tokens": 5000}))
        monkeypatch.setattr(grid_mod, "detect_grid", boom)
        res = extract_occurrence_matrix(
            api_key="k", image_b64="aGk=", media_type="image/png",
            cell_strategy="cells")
        assert res.ok
        assert res.data["cell_status"] == "single_pass_unverified"


class TestGatedResult:
    def test_labels_land_in_taxon_rows_not_occurrences(self):
        """`occurrences` is sparse by contract, so a label with no code would
        be dropped as a blank cell. Folding them in would empty the very list
        that carried the readings which were good."""
        payload = {"columns": _PAYLOAD["columns"], "legend": _PAYLOAD["legend"],
                   "rows": [{"index": 0, "taxon": "Capnodoce",
                             "authority": "Moser"},
                            {"index": 1, "taxon": "Capmuchosphaera",
                             "authority": "Boteac"}],
                   "occurrences": [], "strat_rows": [], "confidence": 0.7}
        out = normalize_occurrence_matrix_result(payload,
                                                 cell_status="not_attempted")
        assert out["occurrences"] == []
        assert [r["taxon"] for r in out["taxon_rows"]] == \
            ["Capnodoce", "Capmuchosphaera"]
        assert out["taxon_rows"][0]["authority"] == "Moser"

    def test_a_stratigraphic_band_is_carried_faithfully(self):
        """The labels prompt transcribes the comparison band verbatim with an
        empty authority, and there is no reliable way to tell that from a
        genus with no named authority. It is carried, not guessed at."""
        payload = {"columns": [], "legend": [],
                   "rows": [{"index": 0, "taxon": "Conodont level (see text)",
                             "authority": ""}],
                   "occurrences": [], "strat_rows": [], "confidence": 0.6}
        out = normalize_occurrence_matrix_result(payload,
                                                 cell_status="not_attempted")
        band = out["taxon_rows"][0]
        assert band["taxon"] == "Conodont level (see text)"
        assert band["authority"] == ""
        assert "row_kind" not in band, \
            "no row_kind may be inferred; that is the documented limit"

    def test_the_row_index_survives(self):
        payload = {"columns": [], "legend": [], "occurrences": [],
                   "strat_rows": [],
                   "rows": [{"index": 7, "taxon": "Ozsvartella",
                             "authority": "Siveter"}],
                   "confidence": 0.6}
        out = normalize_occurrence_matrix_result(payload)
        assert out["taxon_rows"][0]["index"] == 7

    def test_an_empty_row_is_dropped_with_a_warning(self):
        payload = {"columns": [], "legend": [], "occurrences": [],
                   "strat_rows": [], "rows": [{"index": 0, "taxon": "  "}],
                   "confidence": 0.6}
        out = normalize_occurrence_matrix_result(payload)
        assert out["taxon_rows"] == []
        assert any("no taxon label" in w for w in out.get("warnings") or [])


class TestTokenFloorPerStrategy:
    def test_only_the_measured_strategy_has_a_floor(self):
        """Reusing the full-answer floor against a labels-only answer would
        reject correct output; guessing one from the contract would reject
        good output silently. Until measured, there is no threshold."""
        from rca_core.extractor import (
            OCCURRENCE_MIN_OUTPUT_TOKENS_BY_STRATEGY as F)
        assert F["single_pass"] == OCCURRENCE_MIN_OUTPUT_TOKENS
        assert F["gated"] is None
        assert F["cells"] is None
        assert set(F) == set(OCCURRENCE_CELL_STRATEGIES)

    def test_a_short_labels_answer_is_not_gated(self, monkeypatch):
        import json
        payload = {"columns": [], "legend": [], "occurrences": [],
                   "strat_rows": [], "rows": [], "confidence": 0.5}
        monkeypatch.setattr(extractor, "_call_llm",
                            lambda **kw: (json.dumps(payload), False, 200, "",
                                          {"output_tokens": 120}))
        res = extract_occurrence_matrix(
            api_key="k", image_b64="aGk=", media_type="image/png",
            cell_strategy="gated")
        assert res.ok
        assert res.error_key != "err.emptyResponse"


class TestTaxonRowsSheet:
    def _ids(self, data):
        return [c["id"] for c in _occurrence_matrix_tables(data)]

    def test_the_sheet_is_absent_without_the_key(self):
        """Conditional, like _strat_rows_configs, so the three-table count the
        rest of the module pins still holds for every other payload."""
        assert "taxon_rows" not in self._ids(
            {"occurrences": [], "columns": [], "legend": [], "strat_rows": []})

    def test_the_sheet_is_absent_when_empty(self):
        assert "taxon_rows" not in self._ids(
            {"taxon_rows": [], "occurrences": [], "columns": []})

    def test_the_sheet_appears_with_the_labels(self):
        assert "taxon_rows" in self._ids(
            {"taxon_rows": [{"index": 0, "taxon": "Capnodoce",
                             "authority": "Moser"}],
             "occurrences": [], "columns": []})

    def test_every_new_key_resolves_in_every_language(self):
        """The exporter's i18n keys are not optional: an unresolvable title_key
        renders as the raw key string in the workbook the user cites."""
        from rca_core.i18n import TRANSLATIONS
        for lang, table in TRANSLATIONS.items():
            for key in ("sec.taxonRows", "col.rowIndex",
                        "sec.occurrencesUnverified", "err.emptyResponse"):
                assert key in table, "%s missing from %s" % (key, lang)
                assert table[key].strip(), "%s is blank in %s" % (key, lang)
