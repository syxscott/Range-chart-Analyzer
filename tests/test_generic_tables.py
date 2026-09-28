"""Generalisation guards: nothing that came back from the model may vanish.

AUDIT-2026-09-27 [item 6.1]. A real 48-figure corpus run
(``tests_real_corpus.py``, MiniMax-M3, 862 rows across 47 payloads) found
that 4 results carrying 131 rows rendered ZERO tables, because their keys
(`data_points` / `events` / `intervals` / `groups` / `points` / `outliers`)
belonged to modes with no table-config branch. The same class of defect had
already shipped once for ``paleomap`` (24 rows over 7 results).

The fix is a generic fallback rather than a branch per mode, because the
model can return a key nobody anticipated. These tests pin the fallback, and
— importantly — pin that it did NOT break the honest-failure paths, which is
what a naive "render anything" implementation would do.
"""
import glob
import json
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(REPO, "tests", "fixtures", "real_payloads")
HAS_REAL = os.path.isdir(RAW) and bool(glob.glob(os.path.join(RAW, "*.json")))


class TestGenericTableFallback(unittest.TestCase):
    """A populated table nobody claimed must still reach the user."""

    def _rendered(self, data):
        from rca_core.exporter import get_configs_for_result
        return {c["id"] for c in get_configs_for_result(data)}

    def test_a_key_nobody_anticipated_still_renders(self):
        data = {"some_future_mode_key": [
            {"alpha": "1", "beta": "two", "gamma": ""},
            {"alpha": "3", "beta": "four", "gamma": "x"},
        ]}
        self.assertIn("some_future_mode_key", self._rendered(data))

    def test_the_real_offending_modes_are_covered(self):
        """The exact keys measured as dropped in the corpus run."""
        chem = {"data_points": [{"depth_m": "1", "value": "2"}],
                "events": [{"name": "e1", "age": "x"}],
                "intervals": [{"name": "i1", "top_depth_m": "9"}]}
        scat = {"groups": [{"name": "g1", "n": "3"}],
                "points": [{"x": "1", "y": "2"}],
                "outliers": [{"x": "9", "y": "9", "label": "o"}]}
        self.assertEqual({"data_points", "events", "intervals"},
                         self._rendered(chem) & {"data_points", "events",
                                                 "intervals"})
        self.assertEqual({"groups", "points", "outliers"},
                         self._rendered(scat) & {"groups", "points",
                                                 "outliers"})

    def test_generic_columns_are_usable_and_labelled(self):
        from rca_core.exporter import _col_label, _generic_tables, _title_label
        cfg = _generic_tables({"odd_key": [{"weird_field": "v"}]}, set())
        self.assertEqual(len(cfg), 1)
        c = cfg[0]
        self.assertEqual(c["row"]({"weird_field": "v"}), ["v"])
        # No index column: both renderers prepend it, and `edit` is
        # index-aligned with `cols`.
        self.assertEqual(len(c["cols"]), len(c["data_keys"]))
        # Known keys reuse the real translation; unknown ones are readable.
        self.assertEqual(_col_label("name"), "col.name")
        self.assertEqual(_col_label("made_up_field"), "Made Up Field")
        # A key that HAS a translation wins over the humanised literal — the
        # paleomap keys added for item 4.2 are the live example.
        self.assertEqual(_title_label("fossil_sites"), "sec.fossilSites")
        self.assertEqual(_title_label("totally_made_up"), "Totally Made Up")

    def test_a_populated_string_list_keeps_its_existing_behaviour(self):
        """`other_fossils` is a STRING list and is deliberately exported as a
        one-column sheet (tests/test_exporter_xlsx_fix.py::
        TestToXlsxStringOtherFossils pins that). The generic fallback must not
        reclassify it, and must not try to derive columns from strings."""
        from rca_core.exporter import get_configs_for_result
        cfg = get_configs_for_result({"other_fossils": ["a caption line"]})
        of = [c for c in cfg if c["id"] == "other_fossils"]
        self.assertEqual(len(of), 1)
        self.assertFalse(of[0].get("generic"),
                         "other_fossils must keep its own config, not a "
                         "generic one built from a string")

    def test_honest_refusal_still_renders_nothing(self):
        """The fallback must not resurrect the 'four empty sheets' failure."""
        refusal = {"data_points": [], "events": [], "intervals": [],
                   "confidence": 0.0}
        self.assertEqual(self._rendered(refusal), set())

    def test_empty_result_refuses_to_export(self):
        """What actually matters for an empty payload: no workbook, ever."""
        import tempfile
        from rca_core import to_xlsx
        for data in ({}, {"confidence": 0.0, "sections": []}, None):
            dest = os.path.join(tempfile.gettempdir(), "rca_must_not_exist.xlsx")
            with self.assertRaises(ValueError):
                to_xlsx(data, dest)

    def test_specific_tables_still_win_over_generic_ones(self):
        """A range chart must keep its curated columns, not a generic
        re-derivation of the same keys."""
        from rca_core.exporter import get_configs_for_result
        cfg = get_configs_for_result({
            "sections": [{"name": "S1", "age_range": "A"}],
            "species_ranges": [{"species": "X", "section": "S1"}],
            "stray_extra": [{"q": "1"}],
        })
        by_id = {c["id"]: c for c in cfg}
        self.assertIn("species_ranges", by_id)
        self.assertFalse(by_id["species_ranges"].get("generic"))
        # The unclaimed key is still shown, generically.
        self.assertIn("stray_extra", by_id)
        self.assertTrue(by_id["stray_extra"].get("generic"))

    def test_to_xlsx_now_exports_a_previously_lost_mode(self):
        import tempfile
        from rca_core import to_xlsx
        data = {"groups": [{"name": "g1", "n": "3"}],
                "points": [{"x": "1", "y": "2"}]}
        dest = os.path.join(tempfile.gettempdir(), "rca_generic_fallback.xlsx")
        to_xlsx(data, dest)
        self.assertGreater(os.path.getsize(dest), 0,
                           "a result that used to be refused must now export")


@unittest.skipUnless(HAS_REAL, "real payload fixtures not present")
class TestRealPayloadsNeverLoseData(unittest.TestCase):
    """Replay genuine model responses recorded by tests_real_corpus.py."""

    def test_every_populated_table_renders(self):
        from rca_core.exporter import get_configs_for_result
        lost = []
        for p in sorted(glob.glob(os.path.join(RAW, "*.json"))):
            data = json.load(open(p, encoding="utf-8"))
            tables = {k for k, v in data.items()
                      if isinstance(v, list) and v and isinstance(v[0], dict)}
            if not tables:
                continue
            rendered = {c["id"] for c in get_configs_for_result(data)}
            for k in tables - rendered:
                lost.append("%s: %s" % (os.path.basename(p), k))
        self.assertEqual(lost, [],
                         "these tables exist in a real response but are not "
                         "renderable: %s" % lost)


if __name__ == "__main__":
    unittest.main()
