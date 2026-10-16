"""The grid_read dispatch: success, and every way it degrades.

`rca_core.matrix` is tested directly elsewhere. What this covers is the part
only the real entry point exercises -- that `extract_occurrence_matrix` tries
the two-stage path first, and that when it cannot, the run STILL PRODUCES AN
ANSWER under the status that answer actually earned.

That last part is the one worth a test. `grid_read` is deliberately absent from
OCCURRENCE_STATUS_BY_STRATEGY, so its status comes from whichever path ran.
The failure this guards is the quiet one: a fallback that emits `grid_cells`
because the requested strategy name survived into the result.

The model is stubbed, so none of this touches the network.
"""
from __future__ import annotations

import base64
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core import extractor as E                       # noqa: E402


# --------------------------------------------------------------------------
# synthetic figures
# --------------------------------------------------------------------------

def _ruled_matrix(n_rows=6, n_cols=5, marks=((0, 0), (1, 2), (5, 4)),
                  cell=(24, 17), rule=2, pad_left=90, pad_top=40):
    """At least 6 row rules and 4 column rules.

    `detect_grid` rejects anything with fewer (MIN_ROW_RULES = 6), so a 4-row
    toy figure is not "a figure without a lattice" -- it is a figure the detector
    correctly refuses, and asserting a fallback on one would pass for the wrong
    reason.
    """
    from PIL import Image, ImageDraw
    x0, y0 = pad_left, pad_top
    w, h = cell[0] * n_cols, cell[1] * n_rows
    img = Image.new("L", (x0 + w + 30, y0 + h + 30), 255)
    d = ImageDraw.Draw(img)
    for i in range(n_cols + 1):
        px = x0 + i * cell[0]
        d.rectangle([px, y0, px + rule - 1, y0 + h], fill=0)
    for j in range(n_rows + 1):
        py = y0 + j * cell[1]
        d.rectangle([x0, py, x0 + w, py + rule - 1], fill=0)
    for (r, c) in marks:
        cx, cy = x0 + c * cell[0], y0 + r * cell[1]
        d.rectangle([cx + 8, cy + 5, cx + 16, cy + 12], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _blank():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (300, 200), 255).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _labels(n_rows, n_cols):
    return {
        "rows": [{"taxon": f"Genus{i} sp", "authority": "A"}
                 for i in range(n_rows)],
        "columns": [{"label": f"S{i:02d}"} for i in range(n_cols)],
        "legend": [{"code": "C", "class": "common"}],
        "zones": [],
    }


_MATRIX_PAYLOAD = {
    "columns": [{"label": "S00"}],
    "legend": [{"code": "C", "class": "common"}],
    "occurrences": [{"taxon": "Genus0 sp", "authority": "A", "site": "S00",
                     "code": "C", "class": "common", "row_kind": "taxon",
                     "confidence": 0.9}],
    "strat_rows": [],
    "rows": [{"index": 0, "taxon": "Genus0 sp", "authority": "A"}],
    "confidence": 0.9,
    "cell_status": "grid_cells",
}

_SINGLE_PASS_PAYLOAD = {
    "columns": [{"label": "S00"}],
    "legend": [{"code": "C", "class": "common"}],
    "occurrences": [{"taxon": "Genus0 sp", "site": "S00", "code": "C",
                     "class": "common"}],
}


def _install(monkeypatch, handler):
    """Replace the one transport call the two-stage path makes."""
    calls = []

    def fake(system_prompt, image_b64, user_text, max_tokens,
             provider=None, media_type=None, timeout_sec=None,
             capture_error_body=None, progress_callback=None):
        calls.append(system_prompt)
        return handler(system_prompt)

    monkeypatch.setattr(E, "_call_llm", fake)
    return calls


def _run(image, **kw):
    return E.extract_occurrence_matrix(
        api_key="k", image_b64=image, media_type="image/png",
        chart_lang="en", **kw)


class TestGridReadDispatch:
    def test_default_is_grid_read(self):
        assert E.OCCURRENCE_DEFAULT_CELL_STRATEGY == "grid_read"

    def test_the_registered_dispatch_entry_is_the_one_calling_it(self):
        assert E._MODE_DISPATCH["occurrence_matrix"] is \
            E.extract_occurrence_matrix

    def test_the_two_stage_path_really_runs_on_a_lattice(self, monkeypatch):
        """Guards the guard: the other fallback tests would pass even if the
        lattice were undetectable, so prove the two-stage path IS reachable."""
        from rca_core.matrix import detect_filled_cells
        found = detect_filled_cells(_ruled_matrix())
        assert found is not None and found.cells

    def test_success_reports_grid_cells(self, monkeypatch):
        img = _ruled_matrix()
        import rca_core.matrix as M
        monkeypatch.setattr(M, "extract_matrix_two_stage",
                            lambda *a, **k: _MATRIX_PAYLOAD)
        monkeypatch.setattr(E, "_call_llm", lambda **kw: (
            _dumps(_SINGLE_PASS_PAYLOAD), False, 200, "", {"output_tokens": 3000}))
        res = _run(img)
        assert res.ok
        assert res.data["cell_status"] == "grid_cells"
        assert res.data["occurrences"]

    def test_no_lattice_falls_back_and_rewrites_the_status(self, monkeypatch):
        """A blank figure has no grid, so nothing was verified."""
        monkeypatch.setattr(E, "_call_llm", lambda **kw: (
            _dumps(_SINGLE_PASS_PAYLOAD), False, 200, "", {"output_tokens": 3000}))
        res = _run(_blank())
        assert res.ok, "a figure with no lattice must still produce an answer"
        assert res.data["cell_status"] == "single_pass_unverified", \
            "the fallback must not inherit the requested strategy's claim"
        assert res.data["occurrences"]

    def test_a_transport_error_falls_back_instead_of_raising(self, monkeypatch):
        """The handler used to raise NameError on `log`; this is its test."""
        boom = {"n": 0}

        def fake(**kw):
            boom["n"] += 1
            if boom["n"] == 1:            # the two-stage label call
                raise RuntimeError("transport exploded")
            return (_dumps(_SINGLE_PASS_PAYLOAD), False, 200, "",
                    {"output_tokens": 3000})

        monkeypatch.setattr(E, "_call_llm", fake)
        res = _run(_ruled_matrix())
        assert res.ok
        assert res.data["cell_status"] == "single_pass_unverified"

    def test_explicit_single_pass_never_touches_the_two_stage_path(
            self, monkeypatch):
        import rca_core.matrix as M

        def explode(*a, **k):             # pragma: no cover - must not run
            raise AssertionError("grid_read ran when single_pass was requested")

        monkeypatch.setattr(M, "extract_matrix_two_stage", explode)
        monkeypatch.setattr(E, "_call_llm", lambda **kw: (
            _dumps(_SINGLE_PASS_PAYLOAD), False, 200, "", {"output_tokens": 3000}))
        res = _run(_ruled_matrix(), cell_strategy="single_pass")
        assert res.ok
        assert res.data["cell_status"] == "single_pass_unverified"

    def test_an_unknown_strategy_is_normalised_not_crashed(self, monkeypatch):
        monkeypatch.setattr(E, "_call_llm", lambda **kw: (
            _dumps(_SINGLE_PASS_PAYLOAD), False, 200, "", {"output_tokens": 3000}))
        res = _run(_ruled_matrix(), cell_strategy="not-a-strategy")
        assert res.ok
        assert res.data["cell_status"] in E.OCCURRENCE_CELL_STATUSES


def _dumps(obj):
    import json
    return json.dumps(obj)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
