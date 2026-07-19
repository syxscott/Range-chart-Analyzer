"""Tests for rca_core.cache.ResultCache."""

from __future__ import annotations

import os
import tempfile
import unittest

from rca_core.cache import ResultCache


class TestResultCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite",
                                                 delete=False)
        self._tmp.close()
        self.cache = ResultCache(db_path=self._tmp.name, max_entries=5)

    def tearDown(self):
        self.cache.close()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_make_key_stable(self):
        k1 = ResultCache.make_key(a=1, b=2, image_b64="x" * 100)
        k2 = ResultCache.make_key(b=2, a=1, image_b64="x" * 100)
        self.assertEqual(k1, k2, "key must be order-independent")

    def test_make_key_hashes_images(self):
        # image_b64 is hashed, so key is short regardless of image size.
        k = ResultCache.make_key(image_b64="A" * 100000)
        self.assertLess(len(k), 80)

    def test_miss_then_hit(self):
        key = ResultCache.make_key(img="abc", model="m3")
        self.assertIsNone(self.cache.get(key))
        self.cache.put(key, {"species_ranges": [{"species": "Trex"}]})
        result = self.cache.get(key)
        self.assertIsNotNone(result)
        self.assertEqual(result["species_ranges"][0]["species"], "Trex")

    def test_lru_eviction(self):
        for i in range(7):
            self.cache.put(
                ResultCache.make_key(img=f"img_{i}"),
                {"i": i})
        self.assertLessEqual(self.cache.size(), 5)

    def test_lru_touch(self):
        # Insert 5 entries, then "touch" the oldest via get, then add 2 more.
        keys = [ResultCache.make_key(img=f"img_{i}") for i in range(5)]
        for k in keys:
            self.cache.put(k, {"v": k})
        # Touch the first key.
        self.cache.get(keys[0])
        # Add 2 more — the oldest *untouched* (keys[1]) should be evicted.
        self.cache.put(ResultCache.make_key(img="new1"), {"v": 1})
        self.cache.put(ResultCache.make_key(img="new2"), {"v": 2})
        self.assertIsNotNone(self.cache.get(keys[0]), "touched key survives")
        self.assertIsNone(self.cache.get(keys[1]), "untouched key evicted")

    def test_put_replace(self):
        key = ResultCache.make_key(img="x")
        self.cache.put(key, {"v": 1})
        self.cache.put(key, {"v": 2})
        self.assertEqual(self.cache.get(key)["v"], 2)
        self.assertEqual(self.cache.size(), 1)

    def test_clear(self):
        for i in range(3):
            self.cache.put(ResultCache.make_key(img=f"x{i}"), {"i": i})
        n = self.cache.clear()
        self.assertEqual(n, 3)
        self.assertEqual(self.cache.size(), 0)

    def test_different_images_different_keys(self):
        k1 = ResultCache.make_key(image_b64="A" * 100)
        k2 = ResultCache.make_key(image_b64="B" * 100)
        self.assertNotEqual(k1, k2)


if __name__ == '__main__':
    unittest.main()
