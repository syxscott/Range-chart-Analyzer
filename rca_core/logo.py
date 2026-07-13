"""Generate the Range Chart Analyzer logo (PNG + multi-resolution ICO).

Pure stdlib + Pillow. No external assets.

Design
------
- A soft warm-white rounded square (256x256) as the icon canvas.
- Left side: a vertical stratigraphic column built from 4 stacked blocks
  in progressively warmer slate tones (youngest at top).
- Right side: two horizontal range lines (one solid, one dashed) in the
  brand emerald, representing species ranges across the chart. A small
  filled dot marks the apex of each line.
- A thin baseline ties the column to the ranges visually.

The same drawing routine is resampled for every ICO size so the icon
reads cleanly from 16 px favicon up to 256 px desktop icon.
"""

from __future__ import annotations

import io
import os
import struct
from typing import Iterable

try:
    from PIL import Image, ImageDraw  # type: ignore
    HAS_PIL = True
except Exception:
    HAS_PIL = False

# Default on-disk outputs relative to the project root.
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
DEFAULT_PNG = os.path.join(DEFAULT_OUT_DIR, "logo.png")
DEFAULT_ICO = os.path.join(DEFAULT_OUT_DIR, "logo.ico")

# Brand colors (must stay in sync with gui.py COLORS and css/style.css).
BG = (250, 250, 249)            # stone-50 off-white tile
BG_OUTLINE = (231, 229, 228)    # stone-200 card-border
PRIMARY = (79, 70, 229)         # indigo-600 --primary
COL_LIGHT = (165, 180, 252)     # indigo-300 topmost layer
COL_MID = (129, 140, 248)       # indigo-400
COL_DEEP = (99, 102, 241)       # indigo-500
COL_BASE = (79, 70, 229)        # indigo-600 bottom
ACCENT = (5, 150, 105)          # --accent emerald #059669
TEXT_DARK = (12, 10, 9)         # stone-950 --text


def _try_font(size: int):
    """Best-effort truetype font lookup; falls back to PIL default bitmap."""
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont_truetype(path, size)
            except Exception:
                continue
    from PIL import ImageFont  # type: ignore
    return ImageFont.load_default()


def ImageFont_truetype(path, size):
    from PIL import ImageFont  # type: ignore
    return ImageFont.truetype(path, size)


def _draw(canvas_size: int) -> "Image.Image":
    """Draw the icon on a square RGBA canvas of ``canvas_size`` px.

    The composition uses a 24-unit grid (left gutter 3, column 7, gap 2,
    ranges area 11, right gutter 1) so it scales cleanly.
    """
    img = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    S = canvas_size
    pad = max(2, round(S * 0.08))
    # Rounded tile background
    radius = round(S * 0.18)
    d.rounded_rectangle(
        (0, 0, S - 1, S - 1), radius=radius,
        fill=BG, outline=BG_OUTLINE, width=max(1, round(S * 0.012)),
    )

    # Stratigraphic column (left). Four stacked blocks, oldest at bottom.
    col_x0 = pad + round(S * 0.03)
    col_x1 = col_x0 + round(S * 0.30)
    col_top = pad + round(S * 0.05)
    col_bot = S - pad - round(S * 0.05)

    layer_colors = [COL_LIGHT, COL_MID, COL_DEEP, COL_BASE]
    n = len(layer_colors)
    layer_h = (col_bot - col_top) / n
    for i, c in enumerate(layer_colors):
        y0 = col_top + i * layer_h
        y1 = col_top + (i + 1) * layer_h
        d.rectangle((col_x0, y0, col_x1, y1 - 1), fill=c)
    # subtle column outline for crispness
    d.rectangle((col_x0, col_top, col_x1, col_bot), outline=PRIMARY,
                width=max(1, round(S * 0.008)))

    # Tick marks on the right edge of the column (bed boundaries).
    for i in range(n + 1):
        y = col_top + i * layer_h
        d.line((col_x1 - round(S * 0.04), y, col_x1, y),
               fill=PRIMARY, width=max(1, round(S * 0.008)))

    # Two horizontal range lines (species ranges across the chart).
    line_w = max(2, round(S * 0.022))
    dot_r = max(2, round(S * 0.034))
    ranges_start = col_x1 + round(S * 0.06)
    ranges_end = S - pad - round(S * 0.05)
    line1_y = col_top + layer_h * 1.4          # upper-mid
    line2_y = col_top + layer_h * 2.7          # lower-mid (dashed)

    # Solid range (top)
    d.line((ranges_start, line1_y, ranges_end - round(S * 0.02), line1_y),
           fill=ACCENT, width=line_w)
    d.ellipse((ranges_end - round(S * 0.06) - dot_r,
               line1_y - dot_r,
               ranges_end - round(S * 0.06) + dot_r,
               line1_y + dot_r),
              fill=ACCENT)
    # Apex dot on left of solid line
    d.ellipse((ranges_start - dot_r, line1_y - dot_r,
               ranges_start + dot_r, line1_y + dot_r),
              fill=ACCENT)

    # Dashed range (bottom)
    seg = max(4, round(S * 0.04))
    gap = max(3, round(S * 0.025))
    x = ranges_start
    while x < ranges_end - round(S * 0.02):
        x2 = min(x + seg, ranges_end - round(S * 0.02))
        if x2 > x:
            d.line((x, line2_y, x2, line2_y), fill=ACCENT, width=line_w)
        x = x2 + gap
    d.ellipse((ranges_start - dot_r, line2_y - dot_r,
               ranges_start + dot_r, line2_y + dot_r),
              fill=ACCENT)
    d.ellipse((ranges_end - round(S * 0.06) - dot_r,
               line2_y - dot_r,
               ranges_end - round(S * 0.06) + dot_r,
               line2_y + dot_r),
              fill=ACCENT)

    # Baseline connecting column to ranges (subtle dotted)
    by = col_bot + round(S * 0.015)
    bx = ranges_start
    while bx < ranges_end - round(S * 0.02):
        d.line((bx, by, bx + max(2, round(S * 0.015)), by),
               fill=BG_OUTLINE, width=max(1, round(S * 0.005)))
        bx += max(5, round(S * 0.025))

    return img


def _make_ico(images: Iterable["Image.Image"]) -> bytes:
    """Pack multiple PNG-shaped images into a single Windows .ico file."""
    sizes = []
    blobs = []
    for im in images:
        # ICO expects square >=32px images; we still pack the 16 if Pillow produced it.
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())
        sizes.append(im.size[0])

    count = len(sizes)
    # ICONDIR + ICONDIRENTRY table + ICONDIRENTRY data
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    data = b""
    for size, blob in zip(sizes, blobs):
        h = w = size if size >= 32 else 32  # OS width/height bytes (0 means 256, no smaller)
        # ICO directory entries use a single byte for size; 0 means 256.
        bw = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            bw, bw, 0, 0, 1, 32,
            len(blob), offset,
        )
        data += blob
        offset += len(blob)
    return header + entries + data


def generate(out_dir: str | None = None) -> dict[str, str]:
    """Generate the logo PNG and ICO. Returns the file paths written."""
    if not HAS_PIL:
        raise RuntimeError("Pillow is required to generate the logo")
    out_dir = out_dir or DEFAULT_OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    png_path = os.path.join(out_dir, "logo.png")
    ico_path = os.path.join(out_dir, "logo.ico")

    # Master 256x256 PNG.
    master = _draw(256)
    master.save(png_path, format="PNG", optimize=True)

    # ICO sizes — draw fresh per size so anti-aliasing looks correct.
    ico_imgs = [_draw(sz) for sz in (16, 24, 32, 48, 64, 128, 256)]
    ico_path_str = os.path.join(out_dir, "logo.ico")
    with open(ico_path_str, "wb") as f:
        f.write(_make_ico(ico_imgs))
    return {"png": png_path, "ico": ico_path_str}


if __name__ == "__main__":
    paths = generate()
    for k, v in paths.items():
        size = os.path.getsize(v)
        print(f"  {k}: {v} ({size} bytes)")
    print("logo generated.")
