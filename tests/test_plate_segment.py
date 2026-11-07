"""Plate segmentation is tested against plates whose true crops are known.

The point of this module is that it MEASURES, so the test must not accept
"some crops came back". Every expected box below is derived from the drawing
code, not from a previous run of the segmenter.
"""
from __future__ import annotations

import base64
import io
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rca_core.plate_segment import (  # noqa: E402
    PlateSegmentError,
    detect_layout,
    segment_plate,
)

Image = pytest.importorskip("PIL.Image", reason="Pillow is an optional dependency")
ImageDraw = pytest.importorskip("PIL.ImageDraw")


CELL = 120
GAP = 20
INSET = 20
SPEC = 80  # drawn specimen size inside each cell
PAD = 2


def _cell_origin(row: int, col: int) -> tuple[int, int]:
    return GAP + col * (CELL + GAP), GAP + row * (CELL + GAP)


def _draw_grid(rows: int = 3, cols: int = 3) -> tuple[str, list[tuple[int, int]]]:
    """3x3 plate; returns (base64 png, expected tightened box origins)."""
    w = GAP + cols * (CELL + GAP)
    h = GAP + rows * (CELL + GAP)
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    expected: list[tuple[int, int]] = []
    for r in range(rows):
        for c in range(cols):
            x, y = _cell_origin(r, c)
            d.rectangle([x + INSET, y + INSET,
                         x + INSET + SPEC - 1, y + INSET + SPEC - 1], fill=0)
            expected.append((x + INSET, y + INSET))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), expected


def _draw_irregular() -> str:
    """One short specimen and one very tall one: gutter model does not fit."""
    img = Image.new("L", (200, 400), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([40, 20, 119, 59], fill=0)     # 40 px tall
    d.rectangle([40, 120, 119, 379], fill=0)   # 260 px tall
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _blank() -> str:
    img = Image.new("L", (160, 160), 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_three_by_three_plate_yields_nine_crops_with_exact_boxes():
    payload, expected = _draw_grid()
    crops = segment_plate(payload, pad=PAD)
    assert len(crops) == 9, f"expected 9 specimens, got {len(crops)}"
    got = [(c["box"]["x0"], c["box"]["y0"]) for c in crops]
    assert got == [(x - PAD, y - PAD) for x, y in expected], (
        "crop origins drifted from the drawn rectangles")


def test_crop_size_matches_the_drawn_specimen_plus_padding():
    payload, _ = _draw_grid(rows=1, cols=2)
    crops = segment_plate(payload, pad=PAD)
    assert len(crops) == 2
    for crop in crops:
        assert crop["width"] == SPEC + 2 * PAD
        assert crop["height"] == SPEC + 2 * PAD


def test_each_crop_is_a_decodable_png():
    payload, _ = _draw_grid()
    crops = segment_plate(payload, pad=PAD)
    for crop in crops:
        raw = base64.b64decode(crop["png_b64"])
        img = Image.open(io.BytesIO(raw))
        img.load()
        assert img.size == (crop["width"], crop["height"]), crop


def test_crops_are_ordered_top_to_bottom_then_left_to_right():
    payload, expected = _draw_grid()
    crops = segment_plate(payload, pad=PAD)
    order = [(c["box"]["y0"], c["box"]["x0"]) for c in crops]
    assert order == sorted(order), "crops are not in reading order"
    assert [c["index"] for c in crops] == list(range(9))


def test_blank_plate_returns_nothing():
    assert segment_plate(_blank()) == []
    assert detect_layout(_blank())["layout"] == "empty"


def test_grid_layout_is_reported_with_row_and_column_counts():
    payload, _ = _draw_grid(rows=2, cols=3)
    info = detect_layout(payload)
    assert info["layout"] == "grid", info
    assert (info["rows"], info["cols"]) == (2, 3), info


def test_irregular_plate_is_flagged_and_can_be_refused():
    payload = _draw_irregular()
    info = detect_layout(payload)
    assert info["layout"] == "irregular", info
    # Default keeps the crops (still evidence), the strict mode refuses them
    # so a classifier never sees a merged pair labelled as one specimen.
    assert segment_plate(payload) != []
    assert segment_plate(payload, drop_irregular=True) == []


def test_dust_below_the_ink_threshold_is_dropped():
    img = Image.new("L", (200, 200), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 119, 119], fill=0)      # real specimen
    d.point((180, 180), fill=0)                  # single-pixel dust
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    payload = base64.b64encode(buf.getvalue()).decode("ascii")
    crops = segment_plate(payload, min_ink_fraction=0.01)
    assert len(crops) == 1, crops
    assert crops[0]["width"] >= 100


def test_undecodable_payload_is_an_error_not_an_empty_result():
    with pytest.raises(PlateSegmentError):
        segment_plate("not an image")
    with pytest.raises(PlateSegmentError):
        detect_layout("")


def test_no_dependency_on_the_model_layer():
    """Segmentation is the measurable half; it must not reach for a classifier."""
    src = (ROOT / "rca_core" / "plate_segment.py").read_text(encoding="utf-8")
    for forbidden in ("call_llm_api", "LlmProvider", "extract(", "torch", "onnx"):
        assert forbidden not in src, (
            f"plate_segment.py references {forbidden!r}; segmentation must "
            "stay a pure measurement")
