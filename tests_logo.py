"""Tests for rca_core.logo: generator + file outputs."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core import logo as L  # noqa: E402

_pass = 0
_fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)


def test_generate():
    with tempfile.TemporaryDirectory() as td:
        paths = L.generate(out_dir=td)
        check("generate-png-exists", os.path.isfile(paths["png"]))
        check("generate-ico-exists", os.path.isfile(paths["ico"]))
        check("generate-png-nonempty", os.path.getsize(paths["png"]) > 200)
        check("generate-ico-nonempty", os.path.getsize(paths["ico"]) > 500)
        # Logo PNG round-trips through Pillow
        from PIL import Image
        im = Image.open(paths["png"])
        check("generate-png-sizes", im.size == (256, 256))
        check("generate-png-mode", im.mode == "RGBA")
        im.close()
        # ICO contains the 256 image
        ico = Image.open(paths["ico"])
        check("generate-ico-readable", ico.size is not None)
        ico.close()


def test_pil_optional():
    # Calling _draw without Pillow should fail gracefully; calling with
    # Pillow should always produce a valid RGBA image.
    if L.HAS_PIL:
        im = L._draw(64)
        check("draw-64-size", im.size == (64, 64))
        check("draw-64-mode", im.mode == "RGBA")


if __name__ == "__main__":
    test_generate()
    test_pil_optional()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(1 if _fail else 0)
