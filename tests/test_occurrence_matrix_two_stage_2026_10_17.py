"""Tests for the two-stage occurrence-matrix path (rca_core.matrix).

Everything here runs offline. The model is stubbed, because the property worth
testing is not "the model is good" -- that is what the offline regression
against the hand-checked CSV measures -- but the properties that make a good
model's answer SAFE to trust:

  1. geometry finds exactly the cells that contain ink, and does so over a
     plateau of thresholds rather than at one lucky value;
  2. a cell the reader fails to answer is recorded as unanswered, never dropped,
     because dropping it turns a read failure into a confidently wrong absence;
  3. an axis list that disagrees with the measured lattice aborts the whole
     path instead of attaching every cell to the wrong taxon;
  4. a row whose label is missing produces no record rather than one with an
     empty taxon that would collapse into the "" bucket on merge.

The figures are SYNTHETIC and drawn here rather than checked in, because a
binary fixture in the repo cannot be diffed and cannot be regenerated on a new
architecture; the expected geometry is known by construction.
"""
from __future__ import annotations

import base64
import io
import json

import pytest

from rca_core.matrix import (
    CELL_STATUS_GRID,
    INK_PAD,
    LABEL_SYSTEM_PROMPT,
    assemble_occurrence_matrix,
    cell_caption,
    detect_filled_cells,
    extract_matrix_two_stage,
    labels_agree_with_geometry,
    legend_codes,
    render_cell_sheets,
)


# --------------------------------------------------------------------------
# synthetic figures
# --------------------------------------------------------------------------

def _ruled_matrix(n_rows=6, n_cols=5, marks=(), cell=(24, 17), rule=2,
                  pad_left=90, pad_top=40):
    """A ruled table with a label band, exactly the shape grid.py looks for.

    ``marks`` is an iterable of (row, col) to ink with a solid block that stands
    in for a printed glyph.
    """
    from PIL import Image, ImageDraw
    x0 = pad_left
    y0 = pad_top
    w = cell[0] * n_cols
    h = cell[1] * n_rows
    img = Image.new("L", (x0 + w + 30, y0 + h + 30), 255)
    d = ImageDraw.Draw(img)
    for i in range(n_cols + 1):
        x = x0 + i * cell[0]
        d.rectangle([x, y0, x + rule - 1, y0 + h], fill=0)
    for j in range(n_rows + 1):
        y = y0 + j * cell[1]
        d.rectangle([x0, y, x0 + w, y + rule - 1], fill=0)
    for (r, c) in marks:
        cx = x0 + c * cell[0]
        cy = y0 + r * cell[1]
        d.rectangle([cx + 8, cy + 5, cx + 16, cy + 12], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _b64_of(img_b64):
    return img_b64


_MARKS = [(0, 0), (0, 2), (1, 1), (2, 2), (2, 4), (5, 0), (5, 4)]


# --------------------------------------------------------------------------
# stage 1: geometry
# --------------------------------------------------------------------------

class TestFilledCells:
    def test_finds_exactly_the_marked_cells(self):
        found = detect_filled_cells(_ruled_matrix(marks=_MARKS))
        assert found is not None
        assert set(found.cells) == set(_MARKS)

    def test_an_empty_matrix_has_no_cells(self):
        found = detect_filled_cells(_ruled_matrix(marks=()))
        assert found is not None
        assert found.cells == ()

    def test_count_is_stable_across_the_threshold_plateau(self):
        """One lucky threshold is a coincidence; a plateau is a measurement.

        This is why INK_THRESHOLD sits mid-band rather than at whatever value
        happened to work first.
        """
        img = _ruled_matrix(marks=_MARKS)
        baseline = set(detect_filled_cells(img, threshold=0.08).cells)
        for thr in (0.04, 0.05, 0.06, 0.10, 0.12):
            got = set(detect_filled_cells(img, threshold=thr).cells)
            assert got == baseline, f"threshold {thr} changed the cell set"

    def test_count_is_stable_across_insets(self):
        img = _ruled_matrix(marks=_MARKS)
        baseline = set(detect_filled_cells(img, pad=INK_PAD).cells)
        for pad in (3, 4, 5):
            assert set(detect_filled_cells(img, pad=pad).cells) == baseline

    def test_cells_come_back_in_reading_order(self):
        """Row-major, so tile N of the sheet is the Nth cell of the figure.

        The column indices are NOT monotonic across the whole list -- they only
        restart at each new row. That is what reading order means here, and it
        is what lets the model be asked for one entry per tile "in the order the
        tiles appear" and have that mean something checkable.
        """
        found = detect_filled_cells(_ruled_matrix(marks=_MARKS))
        rows = [r for r, _c in found.cells]
        assert rows == sorted(rows)
        for a, b in zip(found.cells, found.cells[1:]):
            if a[0] == b[0]:
                assert b[1] > a[1], f"column went backwards inside row {a[0]}"
            else:
                # Rows with no marks are skipped entirely, so the row index may
                # jump; what must never happen is going backwards or repeating.
                assert b[0] > a[0], "row must advance"

    def test_a_figure_with_no_lattice_returns_none(self):
        """None is a real answer, and the caller must fall back on it."""
        from PIL import Image
        img = Image.new("L", (300, 200), 255)
        for i in range(0, 300, 9):                       # faint noise, no rules
            img.putpixel((i, i % 200), 250)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        assert detect_filled_cells(
            base64.b64encode(buf.getvalue()).decode("ascii")) is None

    def test_garbage_input_returns_none_rather_than_raising(self):
        assert detect_filled_cells("not base64 at all") is None
        assert detect_filled_cells("") is None


# --------------------------------------------------------------------------
# stage 2: sheets
# --------------------------------------------------------------------------

class TestCellSheets:
    def test_sheet_tiles_follow_filled_order(self):
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        sheets = render_cell_sheets(img, filled, chunk=3, scale=4, per_row=3)
        flat = [rc for _b, meta in sheets for _cap, rc in meta]
        assert flat == list(filled.cells)

    def test_chunking_covers_every_cell_exactly_once(self):
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        sheets = render_cell_sheets(img, filled, chunk=3, scale=4, per_row=3)
        caps = [cap for _b, meta in sheets for cap, _rc in meta]
        assert caps == [cell_caption(r, c) for (r, c) in filled.cells]
        assert len(caps) == len(set(caps))

    def test_no_cells_means_no_sheets(self):
        img = _ruled_matrix(marks=())
        filled = detect_filled_cells(img)
        assert render_cell_sheets(img, filled) == []

    def test_caption_format_is_stable(self):
        assert cell_caption(21, 5) == "R21-C5"
        assert cell_caption(0, 0) == "R0-C0"


# --------------------------------------------------------------------------
# axis agreement
# --------------------------------------------------------------------------

class TestAxisAgreement:
    def _filled(self):
        return detect_filled_cells(_ruled_matrix(marks=_MARKS))

    def test_matching_counts_agree(self):
        filled = self._filled()
        labels = {"rows": [{}] * filled.n_rows, "columns": [{}] * filled.n_cols}
        assert labels_agree_with_geometry(labels, filled) is None

    def test_short_row_list_is_reported(self):
        filled = self._filled()
        labels = {"rows": [{}] * (filled.n_rows - 1),
                  "columns": [{}] * filled.n_cols}
        reason = labels_agree_with_geometry(labels, filled)
        assert reason and "rows" in reason

    def test_short_column_list_is_reported(self):
        filled = self._filled()
        labels = {"rows": [{}] * filled.n_rows,
                  "columns": [{}] * (filled.n_cols - 2)}
        reason = labels_agree_with_geometry(labels, filled)
        assert reason and "columns" in reason


# --------------------------------------------------------------------------
# legend codes
# --------------------------------------------------------------------------

class TestLegendCodes:
    def test_reads_distinct_single_characters(self):
        codes = legend_codes([{"code": "A", "class": "abundant"},
                              {"code": "C", "class": "common"},
                              {"code": "A", "class": "abundant"}])
        assert codes == ["A", "C"]

    def test_a_legend_without_codes_yields_nothing(self):
        """One real run returned three classes with a null `code` each."""
        assert legend_codes([{"class": "abundant"}, {"class": "rare"}]) == []


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def _labels(n_rows, n_cols, codes=("A", "C", "R")):
    return {
        "rows": [{"taxon": f"Genus{i} species", "authority": "Author"}
                 for i in range(n_rows)],
        "columns": [{"label": f"S{i:02d}"} for i in range(n_cols)],
        "legend": [{"code": c, "class": c.lower()} for c in codes],
        "zones": [{"label": "Zone A", "columns": ["S00"]}],
    }


class TestAssembly:
    def test_maps_every_cell_to_its_row_and_column(self):
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        cells = [{"row": r, "col": c, "code": "C"} for (r, c) in filled.cells]
        out = assemble_occurrence_matrix(labels, cells, filled, 0.9)
        assert len(out["occurrences"]) == len(_MARKS)
        first = out["occurrences"][0]
        assert first["taxon"] == "Genus0 species"
        assert first["site"] == "S00"
        assert first["class"] == "c"          # from the legend
        assert out["cell_status"] == CELL_STATUS_GRID

    def test_a_cell_with_no_row_label_produces_no_record(self):
        """Otherwise it merges into the "" taxon bucket and looks like data."""
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        cells = [{"row": 99, "col": 0, "code": "C"},
                 {"row": 0, "col": 0, "code": "C"}]
        out = assemble_occurrence_matrix(labels, cells, filled, 0.9)
        assert len(out["occurrences"]) == 1

    def test_empty_code_survives_into_the_record(self):
        """An unread cell must stay visible as unread, not vanish."""
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        cells = [{"row": 0, "col": 0, "code": ""}]
        out = assemble_occurrence_matrix(labels, cells, filled, 0.9)
        assert out["occurrences"][0]["code"] == ""

    def test_root_keys_are_exactly_the_declared_contract(self):
        """A new root key has to be mirrored in the browser normaliser too."""
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        out = assemble_occurrence_matrix(
            _labels(filled.n_rows, filled.n_cols), [], filled, 0.9)
        assert set(out) == {"columns", "legend", "occurrences", "strat_rows",
                            "rows", "confidence", "cell_status"}

    def test_row_labels_survive_the_production_normalizer(self):
        """Regression: emitting `taxon_rows` here was silently dropped.

        The payload built here is INPUT to normalize_occurrence_matrix_result,
        which fills the result's `taxon_rows` from `parsed["rows"]`. Emitting
        `taxon_rows` used the result's own key name and produced a run that
        looked complete while reporting taxon_rows=0.
        """
        from rca_core.extractor import normalize_occurrence_matrix_result
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        cells = [{"row": r, "col": c, "code": "C"} for (r, c) in filled.cells]
        built = assemble_occurrence_matrix(labels, cells, filled, 0.9)
        out = normalize_occurrence_matrix_result(
            built, cell_status=CELL_STATUS_GRID)
        assert len(out["taxon_rows"]) == filled.n_rows
        assert out["taxon_rows"][0]["taxon"] == "Genus0 species"
        assert len(out["occurrences"]) == len(_MARKS)

    def test_bands_survive_the_production_normalizer(self):
        """Regression: `strat_rows` entries must be dicts or they vanish.

        The normalizer skips any entry that is not a dict, so a bare string
        band was dropped silently -- the same failure shape as taxon_rows, and
        found by running the real normalizer instead of trusting the payload.
        """
        from rca_core.extractor import normalize_occurrence_matrix_result
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        labels["zones"] = [{"label": "Conodont level", "columns": ["S00", "S01"]}]
        built = assemble_occurrence_matrix(labels, [], filled, 0.9)
        out = normalize_occurrence_matrix_result(
            built, cell_status=CELL_STATUS_GRID)
        assert out["strat_rows"] == [{"label": "Conodont level",
                                     "values": ["S00", "S01"]}]

    def test_a_bare_string_band_is_still_carried(self):
        """Older payloads use a plain string; it must not be dropped either."""
        from rca_core.extractor import normalize_occurrence_matrix_result
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        labels["zones"] = ["Conodont level (see text)"]
        built = assemble_occurrence_matrix(labels, [], filled, 0.9)
        out = normalize_occurrence_matrix_result(
            built, cell_status=CELL_STATUS_GRID)
        assert out["strat_rows"] == [
            {"label": "Conodont level (see text)", "values": []}]

    def test_a_band_naming_an_unknown_column_is_trimmed(self):
        """Echoing a column this run never read would put it in the export."""
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        labels["zones"] = [{"label": "Zone", "columns": ["S00", "S99"]}]
        built = assemble_occurrence_matrix(labels, [], filled, 0.9)
        assert built["strat_rows"][0]["values"] == ["S00"]


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def _stub(labels, answers, truncated=False, empty_text=False, bad_axis=False):
    """A model stub: labels first, then one answer per sheet.

    ``state["all"]`` counts every call, ``state["cells"]`` only the sheet
    calls. Counting only the latter is what made an earlier version of the
    happy-path test assert 2 calls for what is really 1 label + 1 sheet.
    """
    state = {"all": 0, "cells": 0}

    def call(system, image_b64, user, max_tokens):
        state["all"] += 1
        if "axis labels" in system:
            payload = labels
            if bad_axis:
                payload = dict(labels)
                payload["rows"] = list(labels["rows"])[:-1]
            return json.dumps(payload), False, ""
        idx = state["cells"]
        state["cells"] += 1
        if truncated:
            return "", True, ""
        if empty_text:
            return "", False, ""
        part = answers[idx] if idx < len(answers) else []
        return json.dumps({"cells": [{"id": cid, "letter": v}
                                    for cid, v in part]}), False, ""

    return call, state


class TestTwoStage:
    def _fixture(self):
        """7 marked cells, which is ONE sheet at the production chunk of 31."""
        img = _ruled_matrix(marks=_MARKS)
        filled = detect_filled_cells(img)
        labels = _labels(filled.n_rows, filled.n_cols)
        answers = [[(cell_caption(r, c), "C") for (r, c) in filled.cells]]
        return img, filled, labels, answers

    def test_happy_path_returns_every_cell(self):
        img, filled, labels, answers = self._fixture()
        call, state = _stub(labels, answers)
        out = extract_matrix_two_stage(img, call, max_tokens=8000)
        assert out is not None
        assert len(out["occurrences"]) == len(_MARKS)
        assert state["all"] == 2         # one label call + one sheet
        assert state["cells"] == 1
        assert out["cell_status"] == CELL_STATUS_GRID

    def test_a_truncated_reply_falls_back(self):
        img, filled, labels, answers = self._fixture()
        call, _ = _stub(labels, answers, truncated=True)
        assert extract_matrix_two_stage(img, call) is None

    def test_an_empty_reply_falls_back(self):
        img, filled, labels, answers = self._fixture()
        call, _ = _stub(labels, answers, empty_text=True)
        assert extract_matrix_two_stage(img, call) is None

    def test_axis_counts_disagreeing_with_the_grid_fall_back(self):
        """The failure this prevents looks like a working table."""
        img, filled, labels, answers = self._fixture()
        call, state = _stub(labels, answers, bad_axis=True)
        assert extract_matrix_two_stage(img, call) is None
        assert state["cells"] == 0      # stopped before spending a cell call

    def test_no_geometry_falls_back_without_calling_the_model(self):
        img = _ruled_matrix(marks=())       # lattice, but no ink at all
        calls = []

        def call(system, image_b64, user, max_tokens):
            calls.append(system)
            return "{}", False, ""

        assert extract_matrix_two_stage(img, call) is None
        assert calls == []

    def test_an_unanswered_tile_is_kept_as_unread(self):
        """The cell exists. Dropping it would be a confident false absence.

        Here only SOME tiles are unread, so the run is kept -- with the unread
        ones visible as unread rather than quietly missing.
        """
        img, filled, labels, answers = self._fixture()
        partial = [[(cap, "C") if i % 2 == 0 else (cap, "")
                    for i, (cap, _v) in enumerate(answers[0])]]
        call, _ = _stub(labels, partial)
        out = extract_matrix_two_stage(img, call)
        assert out is not None
        assert len(out["occurrences"]) == len(_MARKS)
        # 7 tiles, odd indices 1/3/5 come back unread
        assert sum(1 for o in out["occurrences"] if o["code"] == "") == 3

    def test_a_reader_that_answers_nothing_at_all_falls_back(self):
        """Right shape, right row count, no data: the worst possible output."""
        img, filled, labels, answers = self._fixture()
        call, _ = _stub(labels, [[]])
        assert extract_matrix_two_stage(img, call) is None

    def test_the_cell_prompt_forbids_an_empty_answer(self):
        """A prompt that offers a graceful wrong answer collects them.

        One earlier prompt said 'if the centre is blank write ""'; three of six
        runs answered "" for 64 of 93 tiles and precision fell to 0.29.
        """
        from rca_core.matrix import CELL_SYSTEM_PROMPT
        assert "NEVER return an empty string" in CELL_SYSTEM_PROMPT
        assert "{CODES}" in CELL_SYSTEM_PROMPT
        assert "{CODES}" not in LABEL_SYSTEM_PROMPT

    def test_the_label_prompt_carries_the_measured_counts(self):
        """The model was asked to count rows and got it wrong in 1 of 3 runs.

        It returned 24 rows for a 22-row table, the alignment guard refused the
        result, and the whole run degraded. Naming the measured counts makes it
        a transcription job instead of a counting job.
        """
        from rca_core.matrix import LABEL_PROMPT_TEMPLATE
        prompt = LABEL_PROMPT_TEMPLATE.replace(
            "{N_ROWS}", "22").replace("{N_COLS}", "18")
        assert "{N_ROWS}" not in prompt and "{N_COLS}" not in prompt
        # The prompt is hard-wrapped for readability, so any phrase can straddle
        # a newline. Compare on collapsed whitespace or the assertion tests the
        # wrapping rather than the instruction.
        flat = " ".join(prompt.split())
        assert "EXACTLY 22 entries" in flat
        assert "EXACTLY 18 entries" in flat
        assert "is NOT a taxon row" in flat

    def test_an_empty_legend_triggers_one_focused_retry(self):
        """The legend came back empty in 1 of 3 runs, emptying every class."""
        img, filled, labels, answers = self._fixture()
        no_legend = dict(labels)
        no_legend["legend"] = []
        seen = {"legend_calls": 0}

        def call(system, image_b64, user, max_tokens):
            if "CLASS LEGEND" in system:
                seen["legend_calls"] += 1
                return json.dumps({"legend": [
                    {"code": "A", "class": "abundant"},
                    {"code": "C", "class": "common"}]}), False, ""
            if "axis labels" in system:
                return json.dumps(no_legend), False, ""
            return json.dumps({"cells": [{"id": cap, "letter": v}
                                         for cap, v in answers[0]]}), False, ""

        out = extract_matrix_two_stage(img, call)
        assert seen["legend_calls"] == 1
        assert out is not None
        codes = sorted(x["code"] for x in out["legend"])
        assert codes == ["A", "C"]
        assert out["occurrences"][0]["class"] in ("abundant", "common")

    def test_a_present_legend_is_not_asked_for_twice(self):
        img, filled, labels, answers = self._fixture()
        seen = {"legend_calls": 0}

        def call(system, image_b64, user, max_tokens):
            if "CLASS LEGEND" in system:
                seen["legend_calls"] += 1
                return "{}", False, ""
            if "axis labels" in system:
                return json.dumps(labels), False, ""
            return json.dumps({"cells": [{"id": cap, "letter": v}
                                         for cap, v in answers[0]]}), False, ""

        assert extract_matrix_two_stage(img, call) is not None
        assert seen["legend_calls"] == 0

    def test_a_transport_error_reaches_the_caller(self):
        """Deliberately NOT swallowed.

        The single caller catches it, logs it and falls back. Swallowing here
        would flatten a transport fault into "no geometry found", which is the
        kind of wrong explanation that costs hours later.
        """
        img, filled, labels, answers = self._fixture()

        def boom(system, image_b64, user, max_tokens):
            raise RuntimeError("transport exploded")

        with pytest.raises(RuntimeError, match="transport exploded"):
            extract_matrix_two_stage(img, boom)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
