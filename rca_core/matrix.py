"""Occurrence-matrix extraction in two stages: geometry locates, the model reads.

WHY THIS EXISTS
---------------
The single-pass prompt asked one model call to do two jobs at once: find which
cells of a taxon x locality matrix contain a mark, and then say what the mark
is. Measured on the reference figure (22 taxa x 18 sample columns, 93 filled
cells), it did the second job well and the first job badly:

    taxon names            20/22        legend      A/C/R, all correct
    authorities            19/19        conodont band   11/11
    sample headers         12/15        filled cells   0.355 recall, 0.579 accuracy

Injecting the measured column geometry took cell recall from 0.355 to 0.565
and left accuracy at 0.574. That split is the whole finding: the model could SEE
the cells and was failing to match them to a position, and position is exactly
what arithmetic does better.

So the division of labour here is by measurement, not by taste:

    GEOMETRY decides which cells exist, and in what order. Measured exact on the
    reference figure: 93/93 cells, per-row and per-column count multisets equal
    to the authority CSV, stable across thresholds 0.04-0.12 and insets 3/4/5 px.

    THE MODEL reads the axis labels and the per-cell codes, both of which it is
    measurably good at once it no longer has to search.

End to end that scores precision 1.000 / recall 1.000 over five consecutive runs
(n=5, minimum 1.000), against a target of 0.90.

WHY THE LETTERS ARE NOT READ BY TEMPLATE MATCHING
--------------------------------------------------
A matcher hard-coded for A/C/R would score perfectly on the reference figure and
fail on the next one, where abundance is encoded as filled and open circles or
as 1/2/3. The geometric half is general precisely because "which cells contain
ink" names no letter set; the reading half has to be general too, so it stays
with the model. What geometry buys the reader is a tile that is already
isolated, already magnified, and already labelled with its position.

THE PARAMETERS, AND WHY EACH ONE IS THE VALUE IT IS
---------------------------------------------------
INK_PAD = 4 and INK_THRESHOLD = 0.08
    ``row_spans`` and ``col_spans`` INCLUDE the 2px ruled lines, so an un-inset
    cell measures ~21% ink from its own border and the threshold separates
    nothing. Measured: exactly 93 cells, constant across insets 3/4/5 px and
    thresholds 0.04-0.12 -- a plateau, not a lucky point.

SHEET_SCALE = 8, SHEET_CHUNK = 31
    At 6x the per-cell reading is not reliable. Separating the two variables
    with the same prompt and the same splitting:

        scale 8, 31 tiles/sheet   1.000  1.000  1.000  1.000  1.000   (n=5)
        scale 6, 31 tiles/sheet   0.968  1.000  1.000                       (n=3)
        scale 6, 93 tiles/sheet   0.968  0.989  0.989   and, in an earlier
                               batch, 3 of 6 runs answered "" for 64 of 93
                               tiles and precision fell to 0.29

    Magnification is the decisive variable; the split is a cheap safeguard
    against the blank-collapse above. 31 tiles/sheet keeps a 93-cell matrix at
    three calls, which is still far cheaper than the search it replaces.

NO EMPTY ANSWERS
    The first reading prompt said "if the centre is blank write ''". That makes
    a blank the SAFE answer under uncertainty, and these glyphs are ~8 px tall,
    so the model is often uncertain -- and a prompt offering a graceful wrong
    answer collects graceful wrong answers. Every tile handed to the model has
    already been proven by the ink test to contain a mark, so "" is never
    correct and the instruction is removed. The legend codes read from call 1
    are passed in as the closed vocabulary.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .grid import INK_LEVEL, GridGeometry, detect_grid

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# measured parameters (see module docstring for the runs behind each value)
# ---------------------------------------------------------------------------

#: Inset applied to every span before the ink test. 2px rules mean an un-inset
#: cell reads ~21% ink from its own border.
INK_PAD = 4

#: Mid-plateau of the 0.04-0.12 band over which the cell count is constant.
INK_THRESHOLD = 0.08

#: How much CHROMA (max(RGB) - min(RGB), 0-255) a cell must EXCEED THE PAGE's
#: own chroma by, for the cell to count as marked.
#:
#: A cell painted a saturated colour can be brighter than the ink threshold and
#: still be a mark: a yellow cell has luminance 215, a pale pink 209, a pale
#: yellow 246 -- all above 200, so a luminance-only test calls them empty. That
#: is not a corner case: an abundance matrix may be encoded by colour rather
#: than by a letter, and then the whole table reads as empty.
#:
#: The test is RELATIVE to the page, and has to be. An absolute threshold is a
#: P1 waiting to happen: a yellowed book page rendered (255,248,205) has a
#: uniform chroma of 50, so an absolute cut at 45 declares the WHOLE page
#: marked -- every empty cell becomes a tile the reader must give a letter to,
#: and the exported table invents a code for each. Measured: a 5-mark table on
#: such a page reported 64 filled cells, 59 of them false.
#:
#: Measured against a page's own chroma, the same page reports its 5 marks, and
#: a clean white page still reports a pale yellow cell (chroma 55, well above
#: the white page's 0). A tint so close to the paper that it cannot be told
#: from it is, correctly, not reported.
CHROMA_MARGIN = 45

#: Magnification for the per-cell reading tiles. Decisive: see the table above.
SHEET_SCALE = 8

#: Tiles per sheet. The sheet SIZE is not what decides accuracy -- the
#: MAGNIFICATION is. Measured through the real path on the reference figure,
#: MiniMax-M3, two runs each:
#:
#:     31 tiles/sheet -> 3 sheets -> 4 calls -> precision 1.000
#:     62 tiles/sheet -> 2 sheets -> 3 calls -> precision 1.000
#:    124 tiles/sheet -> 1 sheet  -> 2 calls -> precision 1.000
#:
#: All three are perfect, so the sheet is made as large as the call budget
#: allows rather than fixed at the smallest size that once worked.
SHEET_CHUNK = 31

#: The largest sheet worth building. 124 tiles at 8x is a 1094x1738 PNG, and
#: measured at 1.000; beyond that the API's own image downscale starts eating
#: the magnification, which is the thing that actually matters.
SHEET_CHUNK_MAX = 124

#: How many sheet calls the reader may make. The server's single-run budget is
#: `timeout_sec * 6 + 25` s and it is derived from SIX attempts, so the
#: two-stage path has to fit inside that: one label call, this many sheet
#: calls, and one single-pass fallback.
#:
#: Without a bound the call count grew with the matrix -- 93 cells cost 4
#: calls, 186 cost 7, 400 cost 14 -- and 7 sequential calls at the default
#: 120 s each overrun the 745 s budget, so the request is abandoned with
#: `err.timeout` and NO data at all, where the single-pass path (exactly one
#: call) would have answered.
MAX_SHEET_CALLS = 3

#: Ceiling on ONE call of the two-stage path.
#:
#: The server's budget is a wall-clock guard over the whole run, and this path
#: makes several calls, so each one is capped: a hung call can then spend at
#: most its share of the budget instead of the whole of it. Measured, the calls
#: take 5-45 s (MiniMax-M3 on the label image, and on a 31-tile sheet at 8x),
#: so 90 s is a generous ceiling and a real hang is caught early.
MATRIX_CALL_TIMEOUT_SEC = 90

#: Wall-clock budget for the WHOLE two-stage path, retries included.
#:
#: REVIEW-2026-10-19: the per-call ceiling above is not enough on its own.
#: This path makes four calls (labels + up to three sheets) and each of them
#: may retry, so counting calls no longer bounds the wall clock -- and the
#: server's budget is a wall-clock guard (`timeout_sec * 6 + 25` s, whose
#: meaning is "at most six attempts' worth of seconds").
#:
#: A deadline is the right shape for this, because the failure kinds cost
#: wildly different amounts: a 429/529 comes back in about a second and may
#: be retried as often as the budget allows, while a read timeout costs the
#: whole per-call ceiling. With a budget the cheap congestion is ridden out
#: and the expensive failure is stopped exactly when it would threaten the
#: run.
#:
#: The number is a budget, not a target: the measured runs take 35-57 s, and
#: the fallback to the single-pass path still has to fit afterwards (its two
#: attempts at the full timeout are 240 s, and 420 + 240 = 660 s stays inside
#: the server's 745 s).
MATRIX_PATH_BUDGET_SEC = 420

#: How much of that budget the path may use per unit of the caller's own
#: per-request timeout. Three attempts' worth: the path makes four calls, so
#: at the default 120 s timeout it gets 360 s, and the fallback still fits.
MATRIX_PATH_BUDGET_PER_TIMEOUT = 3


def matrix_path_budget_sec(timeout_sec: int) -> int:
    """The two-stage path's wall-clock budget for a run with this timeout.

    The server's single-run budget is ``timeout_sec * 6 + 25`` s and this path
    is only one of its consumers -- the single-pass fallback still has to fit
    afterwards -- so the path's own share has to SCALE with the caller's
    timeout rather than being a fixed number. A fixed 420 s would eat a
    30 s-timeout run's entire budget; a fixed share of 3 would overrun a
    120 s-timeout run's budget once the fallback is added.

    The floor keeps a tiny timeout from starving the path of the time it
    needs for its own four calls.
    """
    try:
        t = int(timeout_sec)
    except (TypeError, ValueError):
        t = 120
    return int(max(60, min(MATRIX_PATH_BUDGET_SEC,
                           t * MATRIX_PATH_BUDGET_PER_TIMEOUT)))

#: Tiles per row on a sheet.
SHEET_PER_ROW = 8

#: The label call reads the whole figure; 2x is what took sample headers from
#: 12/15 to 18/18. Capped so a very large figure is not upscaled without bound.
LABEL_SCALE = 2
LABEL_SCALE_MAX_EDGE = 2400

#: Reported on the result. ``grid_cells`` means every emitted cell came from a
#: geometric ink test and had its own code read from its own crop.
CELL_STATUS_GRID = "grid_cells"


# ---------------------------------------------------------------------------
# stage 1 -- geometry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FilledCells:
    """Which cells carry ink, plus the geometry that decided it."""

    geometry: GridGeometry
    cells: Tuple[Tuple[int, int], ...]

    @property
    def n_rows(self) -> int:
        return self.geometry.n_rows

    @property
    def n_cols(self) -> int:
        return self.geometry.n_cols


def _inset(span: Sequence[int], pad: int) -> Tuple[int, int]:
    a, b = int(span[0]), int(span[1])
    return (a + pad, max(a + pad + 1, b - pad))


def detect_filled_cells(
    image_b64: str,
    geometry: Optional[GridGeometry] = None,
    pad: int = INK_PAD,
    threshold: float = INK_THRESHOLD,
) -> Optional[FilledCells]:
    """Locate every cell containing ink. ``None`` when there is no lattice.

    ``None`` is a meaningful answer, not an error: the caller must fall back to
    the single-pass path, because a figure with no detectable grid has no
    geometry to hand the reader.

    Pure local computation -- no network, no model -- so this is the part that
    can be unit-tested exhaustively.
    """
    try:
        import numpy as np
    except Exception:                                    # pragma: no cover
        log.info("matrix: numpy unavailable, falling back to single pass")
        return None
    geo = geometry if geometry is not None else detect_grid(image_b64)
    if geo is None:
        log.info("matrix: no lattice detected, falling back to single pass")
        return None

    gray = _gray_array(image_b64)
    if gray is None:
        return None
    # INK_LEVEL is imported from rca_core.grid rather than written out again.
    # A literal here would drift silently the first time somebody tuned the
    # threshold in grid.py, and the two halves of the same measurement would
    # then disagree about what "ink" means.
    dark = gray < INK_LEVEL
    rgb = _rgb_array(image_b64)
    colour_marked = None
    if rgb is not None:
        # A cell can be MARKED without being DARK: a colour-coded matrix paints
        # its cells, and a yellow cell is brighter than the ink threshold. The
        # test is RELATIVE to the page's own chroma, because a page with a
        # colour cast has a high chroma everywhere and an absolute threshold
        # would call the whole page marked. See CHROMA_MARGIN.
        chroma = (rgb.max(axis=2).astype(np.int16)
                  - rgb.min(axis=2).astype(np.int16))
        page_chroma = float(np.median(chroma))
        colour_marked = chroma > (page_chroma + CHROMA_MARGIN)
    cells: List[Tuple[int, int]] = []
    for r, rspan in enumerate(geo.row_spans):
        ry0, ry1 = _inset(rspan, pad)
        if ry1 <= ry0:
            continue
        for c, cspan in enumerate(geo.col_spans):
            cx0, cx1 = _inset(cspan, pad)
            if cx1 <= cx0:
                continue
            window = dark[ry0:ry1, cx0:cx1]
            if not window.size:
                continue
            # Measure the MIDDLE of the interior, not the whole of it. A ruled
            # line wider than the inset -- a 5px rule (1.25pt at 300dpi) or a
            # blurred scan -- leaves a sliver of itself at the edge of the cell,
            # and a MEAN counts that sliver as a mark: measured, 52 of 64 cells
            # on an 8x8 table with 5 real marks read as filled, and the reader
            # was then obliged to invent a letter for every empty one. The
            # sliver lives at the edge and the glyph lives in the middle, so
            # measuring the middle separates them at any rule width.
            #
            # Re-measured on the reference figure with this statistic: the
            # marked cells bottom out at 0.133 and the empty ones top out at
            # 0.000 (0.052 for the speckled variant), so the 0.08 threshold
            # sits inside the window for every variant measured.
            mid = _middle_band(window)
            if float(mid.mean()) > threshold:
                cells.append((r, c))
                continue
            if colour_marked is not None:
                colour_window = _middle_band(colour_marked[ry0:ry1, cx0:cx1])
                if colour_window.size and \
                        float(colour_window.mean()) > threshold:
                    cells.append((r, c))
    return FilledCells(geometry=geo, cells=tuple(cells))


def _middle_band(window):
    """The middle of a 2-D boolean window, with a quarter off each side.

    The horizontal quarter is dropped too, so a VERTICAL rule sliver on the
    left or right is dropped as well -- a wide rule is wide in both
    directions.
    """
    h, w = window.shape[0], window.shape[1]
    mh = int(h * 0.25)
    mw = int(w * 0.25)
    if h - 2 * mh <= 0 or w - 2 * mw <= 0:
        return window
    return window[mh:h - mh, mw:w - mw]


#: Magic numbers for the two formats this module can hand to a model. Only the
#: leading bytes are needed, so sniffing costs nothing and -- unlike opening the
#: image -- cannot fail on a truncated or corrupt upload.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

#: What `guess_media_type` returns when the bytes are not recognisable. PNG is
#: the status quo at every call site, so an unknown image keeps behaving as it
#: does today instead of becoming a new failure.
MEDIA_TYPE_FALLBACK = "image/png"


def guess_media_type(image_b64: str) -> str:
    """The media type that MATCHES these bytes, for a caller that must declare one.

    WHY THIS EXISTS
    ===============
    ``render_label_image`` returns the caller's ORIGINAL bytes on three of its
    four paths -- over ``LABEL_SCALE_MAX_EDGE``, a failed resize, and the
    PIL-missing fallback. Only the resize path re-encodes to PNG. So a figure at
    or above 2400px on its long edge (the DEFAULT_MAX_EDGE is 4000, so ordinary
    phone and scanner JPEGs qualify) leaves this module still carrying JPEG.

    The call site in ``extractor.py`` declared ``media_type="image/png"``
    unconditionally, which is a JPEG declared as a PNG. A provider that
    validates media types strictly (Anthropic) answers 400; a lenient one
    guesses. Either way the two-stage path aborts and the run silently degrades
    to single_pass, with nothing in the log but "grid_read failed".

    So: read the magic number instead of assuming. Callers should pass
    ``guess_media_type(img)``, never a literal.

    Accepts a bare base64 string or a ``data:`` URL, matching ``_gray_array``.
    Unknown bytes fall back to ``image/png`` rather than raising: this runs on
    the request path, and a wrong-but-declared type is strictly better than an
    exception that costs the whole run.
    """
    if not image_b64:
        return MEDIA_TYPE_FALLBACK
    payload = image_b64.strip()
    if payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[-1]
        # Enough for both signatures, and no more: a 4KB JPEG should not be
    # decoded in full just to look at its first three bytes. The slice must land on a
    # multiple of 4 (base64's quantum) or b64decode raises Incorrect padding and
    # a perfectly good JPEG falls through to the fallback below.
    head_b64 = payload[:24]
    pad = (-len(head_b64)) % 4
    try:
        head = base64.b64decode(head_b64 + "=" * pad, validate=False)
    except Exception:
        return MEDIA_TYPE_FALLBACK
    if head.startswith(PNG_MAGIC):
        return "image/png"
    if head.startswith(JPEG_MAGIC):
        return "image/jpeg"
    return MEDIA_TYPE_FALLBACK


def _gray_array(image_b64: str):
    """base64 image -> float array of greys, or None when it cannot be read."""
    try:
        import io as _io

        import numpy as np
        from PIL import Image
    except Exception:                                    # pragma: no cover
        return None
    payload = image_b64.strip()
    if payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[-1]
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return None
    try:
        with _io.BytesIO(raw) as fh:
            im = Image.open(fh)
            im.load()
            return np.asarray(im.convert("L"), dtype=np.uint8)
    except Exception:
        return None


def _rgb_array(image_b64: str):
    """base64 image -> RGB array, or None when it cannot be read.

    Needed because a cell can be MARKED by colour rather than by darkness, and
    because a tile handed to the reader must keep that colour: greyscaling it
    would leave the reader unable to tell a red class from a blue one.
    """
    try:
        import io as _io

        import numpy as np
        from PIL import Image
    except Exception:                                    # pragma: no cover
        return None
    payload = image_b64.strip()
    if payload.lower().startswith("data:"):
        payload = payload.split(",", 1)[-1]
    try:
        raw = base64.b64decode(payload)
    except Exception:
        return None
    try:
        with _io.BytesIO(raw) as fh:
            im = Image.open(fh)
            im.load()
            return np.asarray(im.convert("RGB"), dtype=np.uint8)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# stage 2 -- what the model is asked to read
# ---------------------------------------------------------------------------

LABEL_SYSTEM_PROMPT = """\
You are transcribing the axis labels of a published taxonomic matrix.
Return ONLY JSON, no prose, no markdown fence.

{
  "rows":    [ {"taxon": "<species exactly as printed, no authority>",
                "authority": "<authority exactly as printed>"} ],
  "columns": [ {"label": "<sample or locality id exactly as printed>"} ],
  "legend":  [ {"code": "<symbol or letter>", "class": "<what it means>"} ],
  "zones":   [ {"label": "<conodont / correlation band label>",
                "columns": [ "<sample id>", ... ]} ]
}

RULES:
- `rows`: EXACTLY {N_ROWS} entries, one per taxon row, TOP to BOTTOM, no gaps, no
  omissions, no merging of adjacent rows. Count as you go and check the total.
- A taxon row lists a GENUS or SPECIES name only. A conodont-zone strip, a
  correlation band, a formation name or any other non-taxon label is NOT a
  taxon row -- put those in `zones`. Listing one here is a wrong answer even if
  the name is correct.
- `columns`: EXACTLY {N_COLS} entries, one per column, LEFT to RIGHT, no gaps, no
  omissions. The labels are often rotated; read every one. The left band that
  holds the taxon names is NOT a data column.
- `legend`: one entry per distinct symbol or letter the figure defines. This
  must never be an empty list on a figure that shows one.
- `zones`: one entry per non-taxon comparison band (conodont zone, correlation
  strip). Copy the band label verbatim and list the COLUMN labels it spans, so
  the band can be drawn. A band that names no columns gets an empty list.
- Transcribe character by character. Do NOT correct a spelling toward what the
  name "ought" to be. If one character is truly illegible write "?" for it.
"""

LEGEND_SYSTEM_PROMPT = """\
You are reading the CLASS LEGEND of a published taxonomic matrix.

Return ONLY JSON, no prose, no markdown fence:

{"legend": [{"code": "<symbol or letter as printed>",
             "class": "<what the figure says it means>"}]}

RULES:
- One entry per distinct symbol / letter the figure defines, with the meaning
  written next to it in the figure (for example "abundant", "common", "rare").
- `code` is the printed symbol itself, exactly as printed.
- Return every legend entry you can see. An empty list is a wrong answer on any
  figure that has a legend.
"""

#: Filled in per call with the MEASURED counts, so the model is asked for the
#: number of rows that actually exist instead of being trusted to count them.
#: Without this the model returned 24 rows for a 22-row table in 1 of 3 runs,
#: and the axis-alignment guard correctly refused the result -- costing the
#: whole run, not just the bad rows.
LABEL_PROMPT_TEMPLATE = LABEL_SYSTEM_PROMPT

CELL_SYSTEM_PROMPT = """\
You are reading symbols out of a contact sheet of grid cells.

Every tile in this sheet is KNOWN to contain a printed mark: the tiles were
selected by finding ink in each cell, so an empty tile is not possible and an
empty answer is always wrong. Each tile is the interior of ONE cell,
magnified, with no border lines. Printed under each tile is its position.

Return ONLY JSON, no prose, no markdown fence:

{"cells": [{"id": "R3-C5", "letter": "A"}]}

RULES:
- One entry per tile, in the order the tiles appear.
- `letter` MUST be exactly one character from this list and nothing else:
  {CODES}
- NEVER return an empty string. If a glyph is hard to read, give your best
  single-character guess from the list. Do not skip a tile.
- Copy the `id` EXACTLY as printed. Do not renumber, reorder, or omit any tile.
"""


def cell_caption(row: int, col: int) -> str:
    """Stable identifier printed under a tile, e.g. ``R21-C5``."""
    return f"R{row}-C{col}"


def render_cell_sheets(
    image_b64: str,
    filled: FilledCells,
    chunk: int = SHEET_CHUNK,
    scale: int = SHEET_SCALE,
    per_row: int = SHEET_PER_ROW,
    pad: int = INK_PAD,
) -> Optional[List[Tuple[str, List[Tuple[str, Tuple[int, int]]]]]]:
    """Render the filled cells as labelled contact sheets.

    Returns ``[(sheet_png_b64, [(caption, (row, col)), ...]), ...]``. Each tile is
    the cell INTERIOR -- the same inset the ink test used, so a tile provably
    contains the mark and no ruled line.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:                                    # pragma: no cover
        log.info("matrix: PIL unavailable, falling back to single pass")
        return None
    gray = _gray_array(image_b64)
    if gray is None:
        return None
    try:
        rgb = Image.fromarray(gray).convert("RGB")
    except Exception:                                    # pragma: no cover
        return None

    # The tiles are cropped from the ORIGINAL colour image when one can be
    # read, not from the greyscale array. A greyscale tile leaves the reader
    # unable to tell a red class from a blue one, which is the whole content of
    # a colour-coded matrix.
    colour = _rgb_array(image_b64)
    if colour is not None:
        try:
            rgb = Image.fromarray(colour)
        except Exception:                                # pragma: no cover
            rgb = Image.fromarray(gray).convert("RGB")

    geo = filled.geometry
    tiles: List[Tuple[str, Any, Tuple[int, int]]] = []
    for (r, c) in filled.cells:
        y0, y1 = _inset(geo.row_spans[r], pad)
        x0, x1 = _inset(geo.col_spans[c], pad)
        crop = rgb.crop((x0, y0, x1, y1))
        if crop.width < 1 or crop.height < 1:
            continue
        big = crop.resize((crop.width * scale, crop.height * scale),
                          Image.LANCZOS)
        tiles.append((cell_caption(r, c), big, (r, c)))
    if not tiles:
        return []

    gap, label_h = 10, 24
    try:
        font = ImageFont.truetype("consola.ttf", 15)
    except Exception:
        font = ImageFont.load_default()
    tw = max(t.width for _cap, t, _rc in tiles)
    th = max(t.height for _cap, t, _rc in tiles)

    sheets = []
    for start in range(0, len(tiles), max(1, chunk)):
        part = tiles[start:start + max(1, chunk)]
        rows = (len(part) + per_row - 1) // per_row
        sheet = Image.new("RGB", (per_row * (tw + gap) + gap,
                                  rows * (th + label_h + gap) + gap), "white")
        draw = ImageDraw.Draw(sheet)
        meta = []
        for i, (cap, tile, rc) in enumerate(part):
            gx = gap + (i % per_row) * (tw + gap)
            gy = gap + (i // per_row) * (th + label_h + gap)
            sheet.paste(tile, (gx, gy))
            draw.text((gx + 1, gy + tile.height + 4), cap,
                      fill=(170, 0, 0), font=font)
            meta.append((cap, rc))
        buf = io.BytesIO()
        sheet.save(buf, format="PNG")
        sheets.append((base64.b64encode(buf.getvalue()).decode("ascii"), meta))
    return sheets


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _pick(row: Dict[str, Any], *names: str) -> str:
    for n in names:
        v = _text(row.get(n))
        if v:
            return v
    return ""


def _band_label(zone: Any) -> str:
    """A comparison band's label, whether the reader gave a bare string or an
    object. Older single-pass payloads use a plain string, so both are accepted
    rather than dropping whichever shape this run happened to produce."""
    if isinstance(zone, str):
        return zone.strip()
    if isinstance(zone, dict):
        return _pick(zone, "label", "name", "text")
    return ""


def _strat_band(zone: Any, cols_l: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Shape a band as ``{"label", "values"}``.

    `normalize_occurrence_matrix_result` skips any `strat_rows` entry that is
    not a dict, so a bare string here would be dropped without a word -- which
    is how an earlier version lost every conodont band while still looking
    complete. The values are the COLUMN LABELS the band spans, resolved
    against the same column list this run read, so a band that names a column
    the run never saw is left out rather than echoed into the export.
    """
    label = _band_label(zone)
    values: List[str] = []
    if isinstance(zone, dict):
        raw = zone.get("columns")
        if isinstance(raw, list):
            known = {_pick(c, "label", "name", "site"): i
                     for i, c in enumerate(cols_l)}
            for item in raw:
                v = _text(item if isinstance(item, str)
                          else (item or {}).get("label")
                          if isinstance(item, dict) else "")
                if v and v in known and v not in values:
                    values.append(v)
    return {"label": label, "values": values}


def legend_codes(legend: Sequence[Dict[str, Any]]) -> List[str]:
    """Single characters the legend defines, for the closed-vocabulary prompt.

    Falls back to a generic instruction when the legend came back without usable
    codes: one run returned three classes with a null ``code`` each.

    Non-dict entries are SKIPPED, not rejected. Both guards around this function
    already do that -- ``legend_is_plausible`` (below) and
    ``assemble_occurrence_matrix`` -- so a legend of mixed shapes sailed past the
    plausibility check and then raised ``AttributeError: 'str' object has no
    attribute 'get'`` on the next line. The caller is
    ``extract_matrix_two_stage``, whose exception ``extractor.py`` catches and
    rewrites to single_pass: a paid label call plus the whole geometry pass
    discarded, logged only as "grid_read failed". Filtering here keeps the mixed
    list working -- the dict entries still carry the usable codes -- instead of
    trading a lost run for a stricter one.
    """
    codes = []
    for item in legend or []:
        if not isinstance(item, dict):
            continue
        code = _text(item.get("code"))[:1]
        if code and code not in codes:
            codes.append(code)
    return codes


def assemble_occurrence_matrix(
    labels: Dict[str, Any],
    cells: Sequence[Dict[str, Any]],
    filled: FilledCells,
    confidence: float,
) -> Dict[str, Any]:
    """Build the occurrence-matrix result in the existing payload contract.

    The root keys are exactly the ones ``rca_core/modes.py`` declares for
    occurrence_matrix (``columns``, ``legend``, ``occurrences``, ``strat_rows``,
    ``taxon_rows``, ``confidence``, ``cell_status``). No new root key is added:
    anything extra would have to be mirrored in the browser normaliser, and a
    contract the JS side does not read is worse than no contract at all.
    """
    rows_l = [r for r in (labels.get("rows") or []) if isinstance(r, dict)]
    cols_l = [c for c in (labels.get("columns") or []) if isinstance(c, dict)]
    legend = [x for x in (labels.get("legend") or []) if isinstance(x, dict)]

    class_of = {}
    for item in legend:
        code = _text(item.get("code"))[:1]
        if code:
            class_of[code] = _pick(item, "class", "meaning", "label")

    occurrences = []
    for cell in cells:
        code = _pick(cell, "code", "letter", "symbol")[:1]
        row = int(cell["row"])
        col = int(cell["col"])
        taxon = _pick(rows_l[row], "taxon", "name") if row < len(rows_l) else ""
        authority = _pick(rows_l[row], "authority") if row < len(rows_l) else ""
        site = (_pick(cols_l[col], "label", "name", "site")
                if col < len(cols_l) else "")
        if not taxon:
            # A cell whose row label is missing cannot be named, and a nameless
            # taxon in a merge would collapse into the "" bucket.
            continue
        occurrences.append({
            "taxon": taxon,
            "authority": authority,
            "site": site,
            "code": code,
            "class": _pick(cell, "class") or class_of.get(code, ""),
            "row_kind": "taxon",
            "confidence": confidence,
        })

    # `rows`, NOT `taxon_rows`. This payload is INPUT to
    # normalize_occurrence_matrix_result, and the normalizer builds the result's
    # `taxon_rows` from `parsed["rows"]`. Emitting `taxon_rows` here looked
    # right -- it is the result's own key name -- and was silently dropped, so
    # every run reported taxon_rows=0 while looking otherwise complete.
    return {
        "columns": [{"label": _pick(c, "label", "name", "site")}
                    for c in cols_l],
        "legend": [{"code": _text(x.get("code"))[:1],
                    "class": _pick(x, "class", "meaning", "label")}
                   for x in legend],
        "occurrences": occurrences,
        "strat_rows": [_strat_band(z, cols_l) for z in (labels.get("zones") or [])
                       if _band_label(z)],
        "rows": [{"index": i, "taxon": _pick(r, "taxon", "name"),
                  "authority": _pick(r, "authority")}
                 for i, r in enumerate(rows_l)],
        "confidence": confidence,
        "cell_status": CELL_STATUS_GRID,
    }


def labels_agree_with_geometry(labels: Dict[str, Any],
                               filled: FilledCells) -> Optional[str]:
    """None when the model's axis counts match the measured lattice.

    A count mismatch means row N in the model's list is not row N of the
    figure, and every cell would be attached to the wrong taxon. That is
    exactly the class of error that looks like a working table, so the caller
    must fall back rather than guess.
    """
    n_rows = len([r for r in (labels.get("rows") or []) if isinstance(r, dict)])
    n_cols = len([c for c in (labels.get("columns") or [])
                  if isinstance(c, dict)])
    if n_rows != filled.n_rows:
        return f"rows {n_rows} != geometry {filled.n_rows}"
    if n_cols != filled.n_cols:
        return f"columns {n_cols} != geometry {filled.n_cols}"
    return None


def _norm_name(value: Any) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "", _text(value).casefold())


#: A class legend is a handful of entries. Beyond this, the list is not a legend
#: any more -- one held-out run returned 13 entries for a figure with 3 codes,
#: and shipping that as `legend` puts thirteen rows in the exported table.
LEGEND_MAX_ENTRIES = 8

#: How many duplicate names an axis may carry before the reading is treated as
#: a collapse. The allowance is a SHARE of the axis, with a floor of one, so a
#: small axis is not punished for a single legal repeat: on 4 inked columns,
#: one repeat is 25%, and a fixed ratio threshold would have rejected a
#: perfectly good figure. On 22 inked rows the same rule allows 4 repeats and
#: still rejects the measured failure, where 22 printed rows collapsed onto 4
#: names (18% distinct).
LABEL_DUPLICATE_ALLOWANCE = 0.20
LABEL_MIN_DUPLICATES_ALLOWED = 1


def _allowed_duplicates(count: int) -> int:
    return max(LABEL_MIN_DUPLICATES_ALLOWED,
               int(count * LABEL_DUPLICATE_ALLOWANCE))


def legend_is_plausible(legend: Sequence[Dict[str, Any]]) -> Optional[str]:
    """None when the legend looks like a legend, else why it does not."""
    entries = [x for x in (legend or []) if isinstance(x, dict)]
    if not entries:
        return "empty"
    if len(entries) > LEGEND_MAX_ENTRIES:
        return f"{len(entries)} entries > {LEGEND_MAX_ENTRIES}"
    codes = [_text(x.get("code"))[:1] for x in entries]
    if any(not c for c in codes):
        return "an entry has no code"
    seen = set()
    for c in codes:
        if c in seen:
            return f"duplicate code {c!r}"
        seen.add(c)
    return None


def labels_are_internally_consistent(labels: Dict[str, Any],
                                     filled: FilledCells) -> Optional[str]:
    """None when the labels cannot be a faithful transcription of THIS lattice.

    Counting rows is necessary but not sufficient. A held-out run on a
    JPEG-compressed copy returned the right NUMBER of rows -- 22, matching the
    geometry, so the count guard passed -- and then collapsed them onto four
    distinct names because the reader had degraded. The result carried 93
    occurrences, 93 codes, zero blanks, and every cell attached to the wrong
    taxon, stamped `grid_cells`.

    Two facts about the figure make that detectable without a human:

      * every row geometry found ink in must have a name, because the ink is
        what proves the row exists and therefore carries a label;
      * two DIFFERENT printed rows do not normally carry the same name, so a
        collision between two inked rows means the transcription is unreliable.

    Both hold for a real taxon axis. Refusing is strictly better than emitting a
    table that looks complete and names the wrong organisms.
    """
    rows = [r for r in (labels.get("rows") or []) if isinstance(r, dict)]
    cols = [c for c in (labels.get("columns") or []) if isinstance(c, dict)]
    inked_rows = sorted({r for (r, _c) in filled.cells})
    inked_cols = sorted({c for (_r, c) in filled.cells})

    for r in inked_rows:
        if r >= len(rows):
            return f"row {r} has ink but no label entry"
        if not _text(_pick(rows[r], "taxon", "name")):
            return f"row {r} has ink but a blank taxon name"
    for c in inked_cols:
        if c >= len(cols):
            return f"column {c} has ink but no label entry"
        if not _text(_pick(cols[c], "label", "name", "site")):
            return f"column {c} has ink but a blank label"

    # A COLLAPSE is what this has to catch, and a ratio catches it without
    # banning a legal repeat.
    #
    # The pairwise rule this replaces ("no two inked rows may share a name")
    # was too strict: a matrix may legitimately list one locality twice (two
    # sections of the same well) or print the same taxon name on two rows.
    # Measured: a figure with columns ['S00','S01','S02','S00','S04'] was
    # rejected and the run fell back to the single-pass path, which is a
    # strictly worse answer for a perfectly good figure.
    #
    # What actually distinguishes a collapse is that MANY names become FEW.
    # The observed failure was 22 printed rows collapsing onto 4 names, i.e.
    # 18% distinct. A tolerance of 20% duplicates still rejects that at 4/22
    # and still accepts a couple of legal repeats.
    if inked_rows:
        distinct_rows = {_norm_name(_pick(rows[r], "taxon", "name"))
                         for r in inked_rows}
        allowed = _allowed_duplicates(len(inked_rows))
        if len(distinct_rows) < len(inked_rows) - allowed:
            return (f"only {len(distinct_rows)} distinct taxon names for "
                    f"{len(inked_rows)} rows (at most {allowed} repeat(s) "
                    f"allowed)")
    if inked_cols:
        distinct_cols = {_norm_name(_pick(cols[c], "label", "name", "site"))
                         for c in inked_cols}
        allowed = _allowed_duplicates(len(inked_cols))
        if len(distinct_cols) < len(inked_cols) - allowed:
            return (f"only {len(distinct_cols)} distinct column labels for "
                    f"{len(inked_cols)} columns (at most {allowed} repeat(s) "
                    f"allowed)")
    return None

def render_label_image(image_b64: str,
                       scale: int = LABEL_SCALE) -> Optional[str]:
    """Upscale the whole figure for the label call, within a pixel budget.

    2x is what took sample headers from 12/15 to 18/18. The budget exists so a
    very large figure is not blown up without bound: LANCZOS on a 6000px scan
    costs time and tokens and reads no better than the capped version.
    """
    try:
        from PIL import Image
    except Exception:                                    # pragma: no cover
        return image_b64
    gray = _gray_array(image_b64)
    if gray is None:
        return None
    try:
        im = Image.fromarray(gray).convert("RGB")
    except Exception:                                    # pragma: no cover
        return None
    if max(im.width, im.height) >= LABEL_SCALE_MAX_EDGE:
        return image_b64
    try:
        im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    except Exception:                                    # pragma: no cover
        return image_b64
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

#: Signature of the injected model caller:
#:   (system_prompt, image_b64, user_text, max_tokens)
#:       -> (text, truncated, error)
#: Injected rather than imported so the whole path is testable without a network
#: or a credential, and so the transport stays the caller's choice.
ModelCall = Callable[[str, str, str, int], Tuple[Optional[str], bool, str]]


def extract_matrix_two_stage(
    image_b64: str,
    call_model: ModelCall,
    max_tokens: int = 32000,
    confidence: float = 0.9,
    sheet_chunk: int = 0,
) -> Optional[Dict[str, Any]]:
    """Two-stage extraction. ``None`` means "use the single-pass path instead".

    Named `extract_matrix_two_stage`, not `extract_occurrence_matrix`:
    `rca_core.extractor` already exports a function by that name for the
    single-pass path, and two same-named entry points in the same package is
    how the wrong one gets imported.

    Returning None is a normal outcome, not a failure: no detectable lattice,
    no ink in any cell, PIL missing, a truncated or empty reply, axis counts
    that disagree with the measured grid, or a reader that answered none of the
    tiles. Each is logged with its reason, and in every one of them the
    single-pass path still runs, so the caller never ends up with no answer.

    Exceptions raised by `call_model` are NOT swallowed: the one caller
    (`extractor.extract_occurrence_matrix`) catches them, logs them and falls
    back, and letting the transport error reach it keeps the real cause in the
    log instead of flattening it into "no geometry found".
    """
    filled = detect_filled_cells(image_b64)
    if filled is None:
        return None
    if not filled.cells:
        log.info("matrix: lattice found but no cell carries ink")
        return None

    # ---- stage 2a: the axis labels --------------------------------------
    label_img = render_label_image(image_b64)
    if not label_img:
        return None
    label_prompt = LABEL_PROMPT_TEMPLATE.replace(
        "{N_ROWS}", str(filled.n_rows)).replace(
        "{N_COLS}", str(filled.n_cols))
    text, truncated, err = call_model(
        label_prompt, label_img,
        f"Transcribe every axis label in this figure. There are exactly "
        f"{filled.n_rows} taxon rows and {filled.n_cols} columns.",
        max_tokens)
    if truncated:
        log.info("matrix: label call truncated at max_tokens=%s", max_tokens)
        return None
    if not text:
        log.info("matrix: label call returned no text (err=%s)", err)
        return None
    from .json_utils import safe_json_loads
    labels = safe_json_loads(text) or {}
    if not isinstance(labels, dict):
        return None

    # The legend is a few words in a corner of a busy figure, and it shares
    # that call with 22 taxon names and 18 rotated headers. It came back EMPTY
    # in one run of three, which silently emptied the class column of every
    # exported row; and on a downscaled copy it came back with THIRTEEN entries
    # for a three-code figure. Both are the same failure -- an implausible list
    # accepted without a check -- so the trigger is plausibility, not emptiness.
    # One focused retry, only when the first list cannot be a legend.
    if legend_is_plausible(labels.get("legend") or []) is not None:
        log.info("matrix: legend implausible (%s); asking again",
                 legend_is_plausible(labels.get("legend") or []))
        ltext, ltrunc, _lerr = call_model(
            LEGEND_SYSTEM_PROMPT, label_img,
            "Read the class legend of this figure.", max_tokens)
        if ltext and not ltrunc:
            extra = safe_json_loads(ltext) or {}
            found = [x for x in (extra.get("legend") or [])
                     if isinstance(x, dict)]
            if legend_is_plausible(found) is None:
                labels["legend"] = found
                log.info("matrix: legend recovered (%d entries)", len(found))
            else:
                log.info("matrix: legend still implausible (%d entries); "
                         "keeping the first read",
                         len(labels.get("legend") or []))
        if legend_is_plausible(labels.get("legend") or []) is not None:
            # An implausible legend is worse than none: 13 rows in the exported
            # table are 10 rows of invention. Drop it rather than ship it, and
            # say why.
            log.info("matrix: dropping an implausible legend")
            labels["legend"] = []

    mismatch = labels_agree_with_geometry(labels, filled)
    if mismatch:
        log.info("matrix: %s; falling back to single pass", mismatch)
        return None
    inconsistent = labels_are_internally_consistent(labels, filled)
    if inconsistent:
        log.info("matrix: %s; falling back to single pass", inconsistent)
        return None

    # ---- stage 2b: the cell codes ---------------------------------------
    # The sheet is made as large as the call budget allows, because the sheet
    # SIZE was measured not to affect accuracy while the number of calls it
    # costs does affect the wall clock. `sheet_chunk` of 0 means "choose".
    if not sheet_chunk:
        n_cells = len(filled.cells)
        sheet_chunk = min(SHEET_CHUNK_MAX,
                          max(SHEET_CHUNK,
                              -(-n_cells // MAX_SHEET_CALLS)))
    sheets = render_cell_sheets(image_b64, filled, chunk=sheet_chunk)
    if not sheets:
        return None
    if len(sheets) > MAX_SHEET_CALLS:
        # Too many cells to read inside the caller's wall-clock budget. Saying
        # so and letting the single-pass path answer is strictly better than
        # starting a sequence that will be cut off mid-way with nothing to
        # show for it.
        log.info("matrix: %d sheet(s) needed, over the %d allowed; "
                 "falling back to single pass",
                 len(sheets), MAX_SHEET_CALLS)
        return None
    codes = legend_codes(labels.get("legend") or [])
    cell_prompt = CELL_SYSTEM_PROMPT.replace(
        "{CODES}", ", ".join(codes) if codes else "the single character printed")

    cells: List[Dict[str, Any]] = []
    unanswered = 0
    out_of_vocab = 0
    for sheet_b64, meta in sheets:
        stext, sheet_truncated, serr = call_model(
            cell_prompt, sheet_b64,
            f"Read all {len(meta)} tiles.", max_tokens)
        # `sheet_truncated`, NOT `truncated`. The latter is the LABEL call's flag,
        # still bound from line 760 and always False by now -- so a truncated
        # sheet was never recognised as one. Measured: with two sheets and the
        # second one cut off mid-array, the run returned ok with
        # cell_status "grid_cells", 31 of 53 cells carrying a code, 22 BLANK,
        # and not one "cell sheet unusable" line in the log. The run looked
        # verified while two fifths of its cells were empty.
        if sheet_truncated or not stext:
            unanswered += len(meta)
            log.info("matrix: cell sheet unusable (truncated=%s err=%s), %d tiles",
                     sheet_truncated, serr, len(meta))
            # The tiles of an unusable sheet are still REAL cells -- geometry
            # found ink in them -- so they are recorded with an empty code
            # rather than dropped. Dropping them would turn a read failure into
            # a false absence, which is the outcome the 2026-10-17 review
            # (P1-3) already ruled out for unread tiles.
            for cap, (row, col) in meta:
                cells.append({"row": row, "col": col, "code": ""})
            continue
        payload = safe_json_loads(stext) or {}
        by_id = {}
        if isinstance(payload, dict):
            for item in (payload.get("cells") or []):
                if isinstance(item, dict):
                    cid = _text(item.get("id"))
                    if cid:
                        by_id[cid] = _pick(item, "letter", "code", "symbol")
        for cap, (row, col) in meta:
            # A tile the model did not answer is recorded with an empty code
            # rather than dropped: the CELL EXISTS, and dropping it would turn a
            # read failure into a silently wrong absence.
            code = by_id.get(cap, "")
            if cap not in by_id:
                unanswered += 1
            elif codes and code and code.upper() not in codes:
                # REVIEW-2026-10-19: the legend codes go into the PROMPT, but
                # nothing checked the answer against them, so the closed
                # vocabulary was enforced only by the model's cooperation. A
                # figure whose legend defines A/C/R could come back with "X" or
                # "?" on a tile -- and the LABEL prompt in this same file
                # TEACHES the model to write "?" for an illegible character, so
                # that answer is likely rather than exotic. Such a code reaches
                # the result with no class (the class comes from the legend)
                # and no sign that it is undefined. It is treated as unread
                # instead: the cell exists, the symbol was not read.
                out_of_vocab += 1
                code = ""
            cells.append({"row": row, "col": col, "code": code})
    if unanswered:
        log.warning("matrix: %d cell(s) got no answer from the reader", unanswered)
    if out_of_vocab:
        log.warning("matrix: %d cell(s) came back with a code outside the "
                    "legend %s; treated as unread", out_of_vocab, codes)
    if not cells or unanswered >= len(cells):
        # Every tile came back unread. Emitting that would produce a table with
        # the right shape, the right row count and no data in any of it -- the
        # confidently-wrong answer this whole path exists to avoid, and the
        # single worst thing a "verified" status could be attached to. The
        # caller still has the single-pass path, so None is strictly better.
        log.info("matrix: reader answered none of %d cells; falling back",
                 len(cells))
        return None

    return assemble_occurrence_matrix(labels, cells, filled, confidence)
