"""Tests for the image enhancement functions in rca_core.extractor."""

from __future__ import annotations

import io
import unittest

from PIL import Image

from rca_core.extractor import _enhance_image_pil


class TestEnhanceImagePil(unittest.TestCase):
    def _make_test_image(self, w=64, h=64) -> Image.Image:
        img = Image.new("RGB", (w, h), color=(200, 200, 200))
        return img

    def test_enhance_returns_image(self):
        img = self._make_test_image()
        result = _enhance_image_pil(img)
        self.assertIsInstance(result, Image.Image)

    def test_enhance_preserves_mode(self):
        img = self._make_test_image()
        result = _enhance_image_pil(img)
        self.assertEqual(result.mode, img.mode)

    def test_enhance_preserves_size_pil_path(self):
        # Pillow path does NOT upsample (that is done by max_edge later).
        img = self._make_test_image(80, 40)
        result = _enhance_image_pil(img)
        self.assertEqual(result.size, (80, 40))

    def test_enhance_on_realistic_chart(self):
        # Simulate a small chart with thin lines + small text regions.
        img = Image.new("L", (200, 300), color=240)
        # Draw thin dark lines (like range lines).
        for x in range(20, 200, 25):
            for y in range(30, 270):
                img.putpixel((x, y), 30)
                img.putpixel((x + 1, y), 30)
        result = _enhance_image_pil(img)
        self.assertIsInstance(result, Image.Image)
        self.assertEqual(result.size, img.size)


if __name__ == '__main__':
    unittest.main()
