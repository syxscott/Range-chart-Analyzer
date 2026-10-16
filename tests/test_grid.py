"""Deterministic grid geometry, measured against a real ruled range chart.

THE FIGURE
==========
``_figtest/r11_ozsvart_table.png`` - Ozsvart et al., a 777x543 range chart with
22 taxon rows and 18 sample columns. Every number pinned below was MEASURED on
that file, not chosen:

    image                 777 x 543
    ink bounding box      x 0..743, y 0..542   (the box includes the caption
                                              above and below the table)
    horizontal rules      25, first at y=19; every one scores an ink fraction
                          of 0.991 across the table width, against ~0.30 for
                          the densest line of italic species text
    vertical rules        20, from x=7 to x=742
    label column          x 7..315, 308 px wide
    sample columns        x 315..742, 18 columns, 23-26 px each
    header band           y 19..160  (rotated sample names, then the two-line
                                    "Miscellaneous localities" / "Conodont
                                    level (see text)" caption)
    taxon rows            y 160..540, 22 rows, 17-18 px each

The row count is the one worth stating explicitly, because it is easy to get
off by one and the two candidate answers look equally plausible from the rule
positions alone: 23 rules lie between y=160 and y=540, and 22 rows is what
those 23 rules ENCLOSE. Counting the rules instead would say 22 rows only by
accident, because it would then have to call the first taxon a header row.
The two are separated by reading the labels: the row above y=160 is the
two-line caption, and the row immediately below it is the first taxon,
"Archaeoacanthocircus rectangularis K,M& Ozsvart".

WHY SO MANY ASSERTIONS ON ONE IMAGE
===================================
The detector's whole job is to be off by exactly nothing. A count-only test
("18 columns, 22 rows") would pass on a detector that returned 18 columns of
the wrong width and 22 rows of the wrong height. So every structural claim is
pinned to POSITION as well: the first and last span of each axis, the two
ends meeting in the middle, and the header/label spans, which are the two
places where a plausible-looking off-by-one hides.

WHY THE MODULE IS LOADED BY FILE PATH
=====================================
``grid.py`` has no intra-package imports, so loading it directly is identical
to importing it - and it keeps these tests independent of whatever
``rca_core/__init__.py`` happens to be re-exporting while other work is in
flight.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GRID_PY = ROOT / "rca_core" / "grid.py"
FIGURE = ROOT / "_figtest" / "r11_ozsvart_table.png"

# Measured on the figure; the synthetic fallback below redraws exactly these.
ROW_RULE_Y = (
    19, 137, 160, 177, 195, 212, 229, 246, 264, 281, 298, 315, 332, 350,
    367, 384, 401, 419, 436, 453, 470, 487, 505, 522, 539,
)
COL_RULE_X = (
    7, 315, 339, 362, 386, 410, 433, 457, 481, 504, 528, 551, 577, 600,
    624, 647, 671, 695, 718, 742,
)


def _load_grid_module():
    spec = importlib.util.spec_from_file_location("_rca_grid_under_test", GRID_PY)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules, so register first.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


grid = _load_grid_module()

pytest.importorskip("PIL", reason="Pillow is needed to build the fixtures")
from PIL import Image  # noqa: E402


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _b64(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _ruled_table(row_ys, col_xs, width=777, height=543) -> Image.Image:
    """Draw a 2px black ruling at the given rule positions."""
    img = Image.new("L", (width, height), 255)
    px = img.load()
    for y in row_ys:
        for dy in (0, 1):
            for x in range(col_xs[0], col_xs[-1] + 3):
                px[x, y + dy] = 0
    for x in col_xs:
        for dx in (0, 1):
            for y in range(row_ys[0], row_ys[-1] + 3):
                px[x + dx, y] = 0
    return img


def _synthetic_table() -> Image.Image:
    """Redraw the figure's measured rule lattice as a 2px black ruling.

    Used only when the untracked ``_figtest/`` directory is absent (it is in
    .gitignore, so a clean checkout has no copy). Same rule positions, so the
    same geometry is expected.
    """
    return _ruled_table(ROW_RULE_Y, COL_RULE_X)


@pytest.fixture(scope="session")
def table_b64() -> str:
    if FIGURE.exists():
        return base64.b64encode(FIGURE.read_bytes()).decode("ascii")
    return _b64(_synthetic_table())


@pytest.fixture(scope="session")
def geometry(table_b64):
    geo = grid.detect_grid(table_b64)
    assert geo is not None, "the ruled source table must produce a geometry"
    return geo


def _cv(values):
    """Independent stdlib coefficient of variation (not the module's)."""
    mean = sum(values) / len(values)
    if mean <= 0:
        return float("inf")
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)) / mean


# --------------------------------------------------------------------------
# the measured geometry
# --------------------------------------------------------------------------


def test_sample_columns_are_the_eighteen_intervals_right_of_the_label(geometry):
    """Pinned to position, not just count.

    Mutation used: ``col_rules[widest + 1:]`` -> ``col_rules[widest:]`` in
    ``_split_label`` (the classic off-by-one that keeps the label column as a
    data column). n_cols then reads 19 and col_spans[0] reads (7, 315).
    """
    assert geometry.n_cols == 18
    assert geometry.col_spans[0] == (315, 339)
    assert geometry.col_spans[1] == (339, 362)
    assert geometry.col_spans[17] == (718, 742)
    assert len(geometry.col_spans) == geometry.n_cols
    # every column is one of the measured data-cell widths
    assert sorted({b - a for a, b in geometry.col_spans}) == [23, 24, 26]


def test_taxon_rows_are_the_twentytwo_intervals_below_the_header(geometry):
    """The row count the whole downstream matrix depends on.

    Mutation used: ``HEADER_PITCH_TOL = 0.15`` -> ``0.5``, which stops the
    header peel after one rule instead of two and folds the 23px caption
    sub-band in as a taxon row (n_rows 23, row_spans[0] (137, 160)).
    """
    assert geometry.n_rows == 22
    assert geometry.row_spans[0] == (160, 177)
    assert geometry.row_spans[1] == (177, 195)
    assert geometry.row_spans[21] == (522, 539)
    assert len(geometry.row_spans) == geometry.n_rows
    # a printed table has a constant row pitch: 17-18 px here, nothing else
    assert sorted({b - a for a, b in geometry.row_spans}) == [17, 18]


def test_header_band_stops_above_the_first_taxon(geometry):
    """header bottom (160) is the top of the first taxon row, not its middle.

    Mutation used: ``_split_header`` returning ``row_rules[k + 1]`` instead of
    ``row_rules[k]``, which slides the header edge down one rule to 177 and
    silently drops the first taxon from the matrix.
    """
    assert geometry.header_span == (19, 160)
    assert geometry.row_spans[0][0] == geometry.header_span[1] == 160
    # the top rule is the image-side edge of the rotated sample-name band
    assert geometry.header_span[0] == 19
    # ... and the caption sub-band is inside the header, not a taxon
    assert geometry.header_span[0] < 137 < geometry.header_span[1] < 177


def test_label_column_is_the_single_wide_interval(geometry):
    """The 308px label column is found by width, and abuts the data columns.

    Mutation used: ``LABEL_COL_RATIO = 2.0`` -> ``20.0``, which fails to
    recognise the label column at all (label_span None, 19 data columns).
    """
    assert geometry.label_span == (7, 315)
    assert geometry.col_spans[0][0] == geometry.label_span[1] == 315
    x0, x1 = geometry.label_span
    assert (x1 - x0) == 308
    # far wider than any data cell, which is the only reason it was picked
    assert (x1 - x0) >= 2 * max(b - a for a, b in geometry.col_spans)


def test_spans_tile_the_table_with_no_gap_and_no_overlap(geometry):
    """Adjacent spans must share an edge, or every cell is shifted.

    Mutation used: ``_spans`` built from ``positions[i], positions[i + 2]``,
    which drops every other rule and opens a one-cell gap between columns.
    """
    for spans in (geometry.col_spans, geometry.row_spans):
        assert len(spans) > 1
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            assert a1 == b0, f"gap/overlap between {a1} and {b0}"
            assert a0 < a1, "every span is non-empty"
    # and the tiling starts and ends on measured rule coordinates
    assert geometry.row_spans[0][0] == 160
    assert geometry.row_spans[-1][1] == 539
    assert geometry.col_spans[0][0] == 315
    assert geometry.col_spans[-1][1] == 742


def test_confidence_is_derived_from_measured_spacing_uniformity(geometry):
    """A hard-coded confidence would satisfy a range check and nothing else.

    Mutation used: ``confidence = 1.0 - 0.5 * (...)`` -> ``confidence = 0.99``.
    """
    row_cv = _cv([b - a for a, b in geometry.row_spans])
    col_cv = _cv([b - a for a, b in geometry.col_spans])
    assert geometry.confidence == pytest.approx(1.0 - 0.5 * (row_cv + col_cv), abs=1e-12)
    assert 0.0 < geometry.confidence <= 1.0
    # regression anchor on the measured figure
    assert round(geometry.confidence, 4) == 0.9724
    # an irregular lattice must actually lower it - feed the formula a lumpy one
    lumpy = _cv([10, 10, 10, 30, 10, 10])
    assert lumpy > row_cv


def test_rule_lines_are_measured_at_25_horizontal_and_20_vertical(table_b64):
    """The intermediate measurement, pinned to position as well as count.

    Mutation used: ``_rule_positions`` returning each run's END (``e``)
    instead of its START (``s``) - the count survives, every coordinate moves
    1-2 px, which is why this test asserts the positions too.
    """
    import numpy as np

    arr = np.asarray(grid._decode_gray(table_b64), dtype=np.float32)
    mask = arr < grid.INK_LEVEL
    ys, xs = np.where(mask)
    box = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    rows = [int(ys.min()) + v for v in grid._rule_positions(box, axis=0, frac=grid.ROW_RULE_FRAC)]
    cols = [int(xs.min()) + v for v in grid._rule_positions(box, axis=1, frac=grid.COL_RULE_FRAC)]
    assert len(rows) == 25
    assert rows[:4] == [19, 137, 160, 177]
    assert rows[-1] == 539
    assert len(cols) == 20
    assert cols[:3] == [7, 315, 339]
    assert cols[-1] == 742


# --------------------------------------------------------------------------
# refusal: no lattice means None, never a guess
# --------------------------------------------------------------------------


def test_irregular_row_pitch_is_refused():
    """The "never guess" guarantee, tested AT the boundary.

    A table whose rows are 12 px except one 30 px row is a real ruled table by
    every ink criterion - solid rules, clean columns - and the ONLY thing that
    says "this is not an occurrence matrix" is that a printed row pitch does
    not jump by 2.5x mid-table. Its spacing CV is ~0.37, past MAX_SPACING_CV.

    Mutation used: ``MAX_SPACING_CV = 0.25`` -> ``0.5``, which admits it.
    """
    col_xs = COL_RULE_X[2:]  # drop the 308px label column, keep 18 data cells
    row_ys = [10]
    for height in (12, 12, 12, 12, 12, 12, 12, 12, 30, 12, 12, 12):
        row_ys.append(row_ys[-1] + height)
    rows = [12, 12, 12, 12, 12, 12, 12, 12, 30, 12, 12, 12]
    assert _cv(rows) == pytest.approx(0.374, abs=0.01)
    assert _cv(rows) > grid.MAX_SPACING_CV
    img = _ruled_table(row_ys, col_xs)
    assert grid.detect_grid(_b64(img)) is None


def test_too_few_rules_to_be_a_table():
    """Ruled, but not a lattice: 3 row rules and 8 column rules is a fragment.

    Mutation used: ``MIN_ROW_RULES = 6`` -> ``2`` together with
    ``MIN_COL_RULES = 4`` -> ``2``. Note that NEITHER alone flips this: every
    minimum-count gate here is individually sufficient, because the structure
    split re-checks the same counts afterwards (a fabricated 7x2 "table"
    appears only once two of them are relaxed together). That redundancy is
    deliberate - it is why the negative tests below need pair mutations.
    """
    import numpy as np

    arr = np.full((300, 400), 255, np.uint8)
    for y in (20, 100, 200):
        arr[y:y + 2, :] = 0
    for x in range(20, 390, 48):
        arr[:, x:x + 2] = 0
    assert grid.detect_grid(_b64(Image.fromarray(arr))) is None


@pytest.mark.parametrize("level,label", [(255, "white"), (0, "black"), (128, "grey")])
def test_solid_colour_image_has_no_grid(level, label):
    """A blank page yields ``None``. Contract, not logic.

    HONEST LIMITATION: this assertion cannot be made RED by a mutation of the
    current implementation, and it would be dishonest to claim otherwise. A
    uniform image contains no rule lines, so the rule-count gate refuses it no
    matter what - fabricating a geometry would first require inventing rules
    that are not in the pixels. What the test does buy is a tripwire against a
    future change that gives a refused input a DEFAULT geometry, which is the
    one way this could actually regress.
    """
    assert grid.detect_grid(_b64(Image.new("L", (400, 300), level))) is None, label


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_random_noise_has_no_grid(seed):
    """The adversarial case: noise's per-row ink fraction sits near 0.5, so a
    stray run CAN clear a fixed brightness threshold.

    This refusal is deliberately over-determined - SIX independent gates have
    to give way before noise produces anything, which is why the mutation
    listed here is not a single constant. With the rule thresholds, the merge
    gap, both minimum-rule counts and the uniformity gate all relaxed,
    ``detect_grid`` returns a fabricated 399x299 lattice of 1px cells on a
    400x300 noise field (verified, 16/16 seeds). Any one of them alone holds.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    for channels in (1, 3):
        shape = (300, 400) if channels == 1 else (300, 400, 3)
        arr = rng.integers(0, 256, shape, dtype=np.uint8)
        assert grid.detect_grid(_b64(Image.fromarray(arr))) is None, f"seed {seed} c{channels}"


def test_bar_chart_has_no_grid():
    """A solid bar chart is regular and dark, but is not a lattice.

    Mutation used: ``ROW_RULE_FRAC = 0.80`` -> ``0.30`` (rule detection stops
    discriminating ink from darkness, which is the original failed approach).
    """
    import numpy as np

    arr = np.full((300, 400), 255, np.uint8)
    for x in range(20, 380, 40):
        arr[100:280, x:x + 25] = 0
    assert grid.detect_grid(_b64(Image.fromarray(arr))) is None


def test_prose_block_has_no_grid():
    """Text lines are ink but they never run the full width.

    Mutation used: ``ROW_RULE_FRAC = 0.80`` -> ``0.15``.
    """
    import numpy as np

    arr = np.full((300, 400), 255, np.uint8)
    for y in range(20, 280, 17):
        arr[y:y + 2, 20:380:5] = 0
    assert grid.detect_grid(_b64(Image.fromarray(arr))) is None


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "   ",
        "data:image/png;base64,",
        "data:image/png;base64",
        "not base64 at all !!",
        "////",
        base64.b64encode(b"this is not an image").decode(),
        None,
        12345,
        b"raw-bytes-not-str",
    ],
)
def test_unusable_payload_returns_none(payload):
    """decode failures are refusals, not exceptions.

    HONEST LIMITATION: the blanket ``except Exception: return None`` in
    ``detect_grid`` cannot be shown to be load-bearing either - every decode
    failure is already caught at its source inside ``_decode_gray``, so
    turning the backstop into a re-raise changes nothing observable. It is
    kept because the brief requires "never raises" and a defence-in-depth
    backstop is the cheap way to keep that true. The decode path ITSELF is
    proven load-bearing by the PNG-only mutation in
    ``test_jpeg_input_carries_the_same_geometry``.
    """
    assert grid.detect_grid(payload) is None


# --------------------------------------------------------------------------
# input handling the caller depends on
# --------------------------------------------------------------------------


def test_data_url_prefix_and_bare_base64_agree(table_b64, geometry):
    """The caller may hand us either; they must not disagree.

    Mutation used: the ``if payload.lower().startswith("data:")`` branch
    removed from ``_decode_gray`` - the data-URL form then decodes to None.
    """
    prefixed = "data:image/png;base64," + table_b64
    assert grid.detect_grid(prefixed) == geometry


def test_jpeg_input_carries_the_same_geometry(table_b64, geometry):
    """JPEG is a supported media type, and must survive re-compression.

    Mutation used: ``_decode_gray`` rejecting any payload not starting with
    the PNG magic bytes - the classic "works on the PNG path only" regression.
    """
    raw = base64.b64decode(table_b64)
    buf = io.BytesIO()
    Image.open(io.BytesIO(raw)).convert("RGB").save(buf, format="JPEG", quality=95)
    jpeg_geo = grid.detect_grid(base64.b64encode(buf.getvalue()).decode("ascii"))
    assert jpeg_geo is not None
    assert (jpeg_geo.n_cols, jpeg_geo.n_rows) == (18, 22)
    assert jpeg_geo.col_spans == geometry.col_spans
    assert jpeg_geo.row_spans == geometry.row_spans
    assert jpeg_geo.header_span == geometry.header_span
    assert jpeg_geo.label_span == geometry.label_span


def test_geometry_is_immutable(geometry):
    """frozen: downstream code may hold one across an edit cycle.

    Mutation used: ``@dataclass(frozen=True)`` -> ``@dataclass``.
    """
    import dataclasses

    assert dataclasses.is_dataclass(geometry)
    with pytest.raises(dataclasses.FrozenInstanceError):
        geometry.n_rows = 99  # type: ignore[misc]
