"""Regression tests for palaeomap table support (AUDIT-2026-09-27 item 4.2).

The defect this pins was not a crash or a wrong label: the palaeomap tables
were produced correctly by the extractor and then thrown away by a
classification that asked "is one of these eight key NAMES present?" instead
of "does this payload contain a table?". Measured over the 66 recorded real
responses it lost 24 populated tables across 7 results, and — in the other
direction — let 7 genuinely empty results through to a 4-sheet workbook of
nothing, which is the very thing the function's own docstring said it existed
to prevent.

The recorded corpus is a real artifact of this repository, so these tests use
it rather than hand-built fixtures: a fixture written to match the new code
would not have caught the old bug.
"""
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REC = os.path.join(REPO, "outputs", "e2e_oa", "results")
HAS_CORPUS = os.path.isdir(REC) and bool(
    [f for f in os.listdir(REC) if f.endswith(".json")])


def _payload(name):
    rec = json.load(open(os.path.join(REC, name), encoding="utf-8"))
    d = rec["result"].get("data")
    if isinstance(d, str):
        d = json.loads(d)
    return d


def _tables(data):
    return {k: v for k, v in data.items()
            if isinstance(v, list) and v and isinstance(v[0], dict)}


def _truly_empty(data):
    """No key holds ANY non-empty list.

    Deliberately weaker than ``_tables``. A payload whose only content is
    ``other_fossils: ["a caption"]`` still HAS a table — the app renders it
    as a one-column sheet on purpose, pinned by
    tests/test_exporter_xlsx_fix.py::TestToXlsxStringOtherFossils — so it must
    not be counted as an empty result. An earlier draft of this file used
    ``_tables`` here and wrongly demanded that such a payload render nothing.
    """
    return not any(isinstance(v, list) and v for v in (data or {}).values())


@unittest.skipUnless(HAS_CORPUS, "recorded e2e corpus not present")
class TestPaleomapTablesAreNotDropped(unittest.TestCase):
    def setUp(self):
        from rca_core.exporter import (detect_tableless_mode,
                                        get_configs_for_result)
        self._detect = detect_tableless_mode
        self._configs = get_configs_for_result

    def test_real_paleomap_payload_yields_configs_for_every_table(self):
        """oa_012 carries 6 tables; all 6 must be rendered."""
        data = _payload("oa_012_f055e83721.json")
        want = _tables(data)
        self.assertGreaterEqual(len(want), 5, "fixture lost its tables")
        self.assertIsNone(self._detect(data),
                          "a payload WITH tables must never be tableless")
        cfg = self._configs(data)
        got = {c["id"] for c in cfg}
        for key in want:
            self.assertIn(key, got,
                          f"table {key!r} is in the payload but not rendered")
        # Not the four range-chart shapes any more.
        self.assertNotIn("species_ranges", got)
        self.assertNotIn("sections", got)

    def test_every_recorded_paleomap_table_is_now_reachable(self):
        """The whole corpus, not one lucky fixture."""
        import glob
        recovered = dropped = 0
        for p in sorted(glob.glob(os.path.join(REC, "oa_*.json"))):
            rec = json.load(open(p, encoding="utf-8"))["result"]
            mode = rec.get("mode_used")
            if mode != "paleomap":
                continue
            data = rec.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    continue
            if not isinstance(data, dict):
                continue
            want = set(_tables(data))
            got = {c["id"] for c in self._configs(data)}
            recovered += len(want & got)
            dropped += len(want - got)
        self.assertEqual(dropped, 0,
                         f"{dropped} real palaeomap tables are still dropped")
        self.assertGreater(recovered, 0,
                           "no palaeomap tables at all — corpus changed?")

    def test_columns_line_up_with_the_recorded_row_keys(self):
        data = _payload("oa_012_f055e83721.json")
        for cfg in self._configs(data):
            rows = data.get(cfg["id"])
            if not isinstance(rows, list) or not rows:
                continue
            self.assertEqual(
                len(cfg["row"](rows[0])), len(cfg["cols"]),
                f"{cfg['id']}: row() and cols disagree in length")
            for k in cfg["data_keys"]:
                self.assertIsInstance(k, str)


@unittest.skipUnless(HAS_CORPUS, "recorded e2e corpus not present")
class TestEmptyResultsAreBlocked(unittest.TestCase):
    """The opposite direction — the guard must still do its stated job."""

    def setUp(self):
        from rca_core.exporter import (detect_tableless_mode,
                                        get_configs_for_result)
        self._detect = detect_tableless_mode
        self._configs = get_configs_for_result

    def test_zero_table_payload_renders_no_configs(self):
        import glob
        offenders = []
        for p in sorted(glob.glob(os.path.join(REC, "oa_*.json"))):
            rec = json.load(open(p, encoding="utf-8"))["result"]
            data = rec.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    continue
            if not isinstance(data, dict) or not _truly_empty(data):
                continue
            cfg = self._configs(data)
            if cfg:
                offenders.append((os.path.basename(p), rec.get("mode_used"),
                                  [c["id"] for c in cfg]))
        self.assertEqual(offenders, [],
                         "results with no tables must render no configs: %s"
                         % offenders)

    def test_to_xlsx_refuses_instead_of_writing_an_empty_workbook(self):
        import glob
        import tempfile
        from rca_core import to_xlsx
        for p in sorted(glob.glob(os.path.join(REC, "oa_*.json"))):
            rec = json.load(open(p, encoding="utf-8"))["result"]
            data = rec.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    continue
            if not isinstance(data, dict) or not _truly_empty(data):
                continue
            dest = os.path.join(tempfile.gettempdir(), "rca_should_not_exist.xlsx")
            with self.assertRaises(ValueError, msg=os.path.basename(p)):
                to_xlsx(data, dest)
            return
        self.skipTest("no empty recorded response in the corpus")


class TestPaleomapDetection(unittest.TestCase):
    def test_detects_only_populated_paleomap_keys(self):
        from rca_core.exporter import _looks_paleomap
        self.assertTrue(_looks_paleomap({"continents": [{"name": "Laurussia"}]}))
        self.assertTrue(_looks_paleomap({"fossil_sites": [{"name": "Sieselbach"}]}))
        # Present but empty is not a table.
        self.assertFalse(_looks_paleomap({"continents": []}))
        # A range chart must not be misread as a map.
        self.assertFalse(_looks_paleomap(
            {"species_ranges": [{"species": "X", "note": "n"}]}))
        self.assertFalse(_looks_paleomap(None))
        self.assertFalse(_looks_paleomap({}))

    def test_configs_declare_only_real_i18n_keys(self):
        """Every title/column key must exist in all three locales (C8)."""
        from rca_core.exporter import _paleomap_tables
        from rca_core.i18n import TRANSLATIONS
        cfg = _paleomap_tables({"continents": [{"name": "x"}]})
        self.assertTrue(cfg)
        keys = []
        for c in cfg:
            keys.append(c["title_key"])
            keys.extend(c["cols"])
        for lang in ("zh", "en", "ja"):
            for k in keys:
                self.assertIn(k, TRANSLATIONS[lang], (lang, k))
                self.assertTrue(TRANSLATIONS[lang][k], (lang, k))

    def test_capability_flag_matches_the_new_behaviour(self):
        from rca_core.capabilities import export_supported_for
        self.assertTrue(export_supported_for("paleomap"))
        # The two still-unrendered assistant modes must stay False.
        self.assertFalse(export_supported_for("chemical_stratigraphy"))
        self.assertFalse(export_supported_for("scatter_plot"))


class TestPaleomapCoordinateNormalisation(unittest.TestCase):
    """AUDIT-2026-09-27 [item 7.1] — a silent whole-result data loss.

    ``_one_point`` called ``first_non_empty``, which was never defined in
    ``rca_core``. So every palaeomap row whose ``coordinates`` arrived as a
    dict raised NameError, and ``extract_paleomap``'s ``except Exception``
    downgraded the ENTIRE result to ``ok=False`` with a warning string. It
    escaped notice because the model often emits the other two coordinate
    shapes, which take different branches — a data-loss bug whose trigger
    depends on the model's output.
    """

    def test_dict_shaped_coordinates_normalise(self):
        from rca_core.extractor import normalize_paleomap_result
        out = normalize_paleomap_result({
            "continents": [{"name": "Laurussia", "coordinates": [
                {"lat": 30.0, "lon": 60.0}]}], "confidence": 0.5})
        self.assertEqual(out["continents"][0]["coordinates"], [[30.0, 60.0]])

    def test_scalar_lat_lon_normalises(self):
        from rca_core.extractor import normalize_paleomap_result
        out = normalize_paleomap_result({
            "continents": [{"name": "Laurussia",
                            "coordinates": {"lat": 12.0, "lon": -30.0}}],
            "confidence": 0.5})
        self.assertEqual(out["continents"][0]["coordinates"], [[12.0, -30.0]])

    def test_fossil_site_latitude_longitude_normalises(self):
        """fossil_sites.lat_lon was the one table that skipped norm_coords."""
        from rca_core.extractor import normalize_paleomap_result
        out = normalize_paleomap_result({
            "fossil_sites": [{"name": "Site X", "lat_lon": [
                {"latitude": 10.0, "longitude": 20.0}]}],
            "confidence": 0.5})
        self.assertEqual(out["fossil_sites"][0]["lat_lon"], [[10.0, 20.0]])

    def test_fossil_site_lat_lon_string_still_survives(self):
        from rca_core.extractor import normalize_paleomap_result
        out = normalize_paleomap_result({
            "fossil_sites": [{"name": "X", "lat_lon": "10N 20E"}],
            "confidence": 0.5})
        self.assertIn("10N", str(out["fossil_sites"][0]["lat_lon"]))
    def test_zero_latitude_is_not_treated_as_missing(self):
        """0.0 is the equator and the prime meridian, not "no value"."""
        from rca_core.extractor import first_non_empty
        self.assertEqual(first_non_empty((None, 0.0)), 0.0)
        self.assertEqual(first_non_empty((None, 0.0, 5.0)), 0.0)
        out = None
        try:
            from rca_core.extractor import normalize_paleomap_result
            out = normalize_paleomap_result({
                "continents": [{"name": "X",
                                "coordinates": {"lat": 0.0, "lon": 0.0}}],
                "confidence": 0.5})
        except Exception as exc:  # pragma: no cover
            self.fail("normalize_paleomap_result raised: %r" % exc)
        self.assertEqual(out["continents"][0]["coordinates"], [[0.0, 0.0]])

    def test_empty_and_missing_values_still_normalise_to_empty(self):
        from rca_core.extractor import normalize_paleomap_result
        for coords in ([], {}, None, ""):
            out = normalize_paleomap_result({
                "continents": [{"name": "X", "coordinates": coords}],
                "confidence": 0.0})
            self.assertEqual(out["continents"][0]["coordinates"], [],
                             "coords=%r should not raise nor invent data"
                             % (coords,))

    def test_a_placeholder_is_not_promoted_to_a_coordinate(self):
        """Models emit "unknown" / "not visible in chart" for a coordinate
        they could not read. Those must not land in the coordinates column
        as if they were a reading — a real coordinate text like "48.1N 12.0E"
        is kept (see the next test); a placeholder is not."""
        from rca_core.extractor import normalize_paleomap_result
        for placeholder in ("unknown", "not visible in chart", "-"):
            out = normalize_paleomap_result({
                "continents": [{"name": "X", "coordinates": placeholder}],
                "confidence": 0.0})
            self.assertEqual(out["continents"][0]["coordinates"], [],
                             "placeholder %r must not become a coordinate"
                             % placeholder)

    def test_a_real_coordinate_string_is_kept(self):
        """Unlike a placeholder, a coordinate the model wrote as text is
        real information and every paleomap table routes through this path."""
        from rca_core.extractor import normalize_paleomap_result
        out = normalize_paleomap_result({
            "fossil_sites": [{"name": "Sieselbach", "lat_lon": "48.1N 12.0E"}],
            "confidence": 0.5})
        self.assertIn("48.1N", str(out["fossil_sites"][0]["lat_lon"]))


if __name__ == "__main__":
    unittest.main()
