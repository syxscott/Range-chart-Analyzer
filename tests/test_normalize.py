"""Tests for rca_core.extractor.normalize_result edge cases.

Covers: _array_root normalization, dict-shaped arrays, _extras deduplication,
truncated VLM handling, iron rule for Zone/Zonule/assemblage species names,
classify_array_item biozone heuristics, and zone_type dead-branch handling.
"""

from __future__ import annotations

import unittest

from rca_core.extractor import normalize_result


class TestArrayRootNormalization(unittest.TestCase):
    """B-4 MEDIUM: _array_root items must be normalized like other lists."""

    def test_array_root_section_item_gets_normalized(self):
        r = normalize_result({'_array_root': [
            {'name': 'X', 'formations': 'Talung Fm'},
        ]})
        # formations must be a list, not a string. And all expected keys set.
        sec = r['sections'][0]
        self.assertEqual(sec['name'], 'X')
        self.assertEqual(sec['formations'], ['Talung Fm'])
        # Keys added by the normalizer are present.
        for key in ('name', 'age_range', 'formations', 'formation_thickness_m',
                    'coordinates'):
            self.assertIn(key, sec)

    def test_array_root_section_item_does_not_duplicate_payload_in_extras(self):
        """B-6 LOW: _extras must NOT re-include the raw _array_root payload."""
        r = normalize_result({'_array_root': [{'name': 'X'}]})
        # Top-level _extras should be empty (or absent) — not the raw list.
        self.assertFalse(r.get('_extras', {}).get('_array_root'))
        self.assertNotIn('_array_root', r.get('_extras', {}))


class TestDictShapedArrays(unittest.TestCase):
    """B-5 MEDIUM: dict-shaped (single-object) named arrays must NOT be silently
    discarded."""

    def test_sections_as_dict_keeps_record(self):
        r = normalize_result({'sections': {
            'Pingdingshan': {'age_range': 'Late Permian'},
        }})
        self.assertEqual(len(r['sections']), 1)
        # The record must survive, with the wrapping key preserved in _extras.
        sec = r['sections'][0]
        self.assertEqual(sec['age_range'], 'Late Permian')
        self.assertEqual(sec.get('_extras', {}).get('wrapper_key'),
                         'Pingdingshan')

    def test_species_ranges_as_dict_keeps_record(self):
        r = normalize_result({'species_ranges': {
            'Neoalbaillella optima': {'range_top': 'Bed 3', 'range_base': 'Bed 9'},
        }})
        self.assertEqual(len(r['species_ranges']), 1)
        sp = r['species_ranges'][0]
        self.assertEqual(sp['species'], 'Neoalbaillella optima')
        self.assertEqual(sp['range_top'], 'Bed 3')
        self.assertEqual(sp['range_base'], 'Bed 9')

    def test_biozones_as_dict_keeps_record(self):
        r = normalize_result({'biozones': {
            'Zone A': {'age': 'Late Permian'},
        }})
        self.assertEqual(len(r['biozones']), 1)
        bz = r['biozones'][0]
        self.assertEqual(bz['name'], 'Zone A')
        self.assertEqual(bz['age'], 'Late Permian')


class TestClassifyArrayItemBiozones(unittest.TestCase):
    """HIGH: biozone entries without thickness_m must still be classified as
    biozones (not silently dropped into sections)."""

    def test_biozone_with_name_and_age_classified_as_biozone(self):
        r = normalize_result({'_array_root': [
            {'name': 'Neoalbaillella optima Zone', 'age': 'Latest Changhsingian'},
        ]})
        self.assertEqual(len(r['biozones']), 1)
        self.assertEqual(r['biozones'][0]['name'], 'Neoalbaillella optima Zone')
        self.assertEqual(r['biozones'][0]['age'], 'Latest Changhsingian')
        # Must NOT leak into sections.
        self.assertEqual(r['sections'], [])

    def test_biozone_with_thickness_m_classified_as_biozone(self):
        r = normalize_result({'_array_root': [
            {'name': 'Zone A', 'age': 'Permian', 'thickness_m': '5m'},
        ]})
        self.assertEqual(len(r['biozones']), 1)

    def test_section_with_age_range_classified_as_section(self):
        r = normalize_result({'_array_root': [
            {'name': 'Pingdingshan', 'age_range': 'Late Permian'},
        ]})
        self.assertEqual(len(r['sections']), 1)
        self.assertEqual(r['biozones'], [])


class TestIronRuleSpeciesRanges(unittest.TestCase):
    """MEDIUM: species names ending in Zone / Zonule / assemblage are iron-rule
    zone labels and must NOT live in species_ranges. They are flagged + moved
    (or at minimum flagged) so they don't fabricate FAD/LAD for a fake taxon.
    """

    def test_zone_suffixed_species_flagged(self):
        r = normalize_result({'species_ranges': [{
            'species': 'Pseudodolitrites-Rotodiscoceras (assemblage)',
            'section': 'X', 'range_top': 'Bed 26', 'range_base': 'Bed 19',
        }]})
        sp = r['species_ranges'][0]
        self.assertEqual(sp.get('_warning'), 'iron_rule_zone_label')

    def test_zonule_suffixed_species_flagged(self):
        r = normalize_result({'species_ranges': [{
            'species': 'Some Genus Zonule', 'section': 'X',
            'range_top': 'Bed 1', 'range_base': 'Bed 1',
        }]})
        sp = r['species_ranges'][0]
        self.assertEqual(sp.get('_warning'), 'iron_rule_zone_label')

    def test_zone_word_suffix_flagged(self):
        r = normalize_result({'species_ranges': [{
            'species': 'Mira Zone', 'section': 'X',
            'range_top': 'Bed 1', 'range_base': 'Bed 1',
        }]})
        sp = r['species_ranges'][0]
        self.assertEqual(sp.get('_warning'), 'iron_rule_zone_label')

    def test_real_species_name_not_flagged(self):
        r = normalize_result({'species_ranges': [{
            'species': 'Neoalbaillella optima', 'section': 'X',
            'range_top': 'Bed 1', 'range_base': 'Bed 1',
        }]})
        sp = r['species_ranges'][0]
        self.assertNotIn('_warning', sp)

    def test_iron_rule_flag_collected_at_root(self):
        r = normalize_result({'species_ranges': [
            {'species': 'Neoalbaillella optima Zone', 'section': 'X',
             'range_top': 'Bed 1', 'range_base': 'Bed 1'},
        ]})
        self.assertIn('_warnings', r)
        self.assertIn('iron_rule_zone_label', r['_warnings'])


class TestZoneTypeHandling(unittest.TestCase):
    """MEDIUM: zone_type is declared by the normalizer but the prompt never
    requests it. When the model does emit it, the suffix must still be applied
    cleanly. When it doesn't, no silent corruption."""

    def test_zone_type_appended_when_emitted(self):
        r = normalize_result({'biozones': [
            {'name': 'Zone A', 'age': 'Permian', 'zone_type': 'assemblage'},
        ]})
        bz = r['biozones'][0]
        # zone_type is appended in parentheses.
        self.assertIn('assemblage', bz['name'].lower())

    def test_zone_type_absent_no_op(self):
        r = normalize_result({'biozones': [
            {'name': 'Zone A', 'age': 'Permian'},
        ]})
        bz = r['biozones'][0]
        # No spurious suffix added.
        self.assertFalse(bz['name'].endswith(')'))


class TestExtrasDedup(unittest.TestCase):
    """LOW: _extras must NOT duplicate the entire _array_root payload."""

    def test_array_root_not_kept_in_extras(self):
        r = normalize_result({'_array_root': [
            {'name': 'X', 'age_range': 'M'},
        ], '_note': 'auto-wrapped'})
        extras = r.get('_extras', {})
        self.assertNotIn('_array_root', extras)


class TestTruncatedVLMOutput(unittest.TestCase):
    """MEDIUM: truncated VLM output rescued as an inner object produces
    ok=True with empty arrays. The unrecognised-rescue path must surface a
    warning and (where unambiguous) ok=False so the user is not misled."""

    def test_rescued_inner_object_marks_warning(self):
        # The truncated text the model might emit — first balanced object is
        # an inner record; the root object never closes.
        from rca_core.json_utils import safe_json_loads
        truncated = (
            '{"sections":[{"name":"Pingdingshan","age_range":"Late Permian"}],'
            '"species_ranges":[{"species":"Neoalbaillella or'
        )
        parsed = safe_json_loads(truncated)
        # The rescued object must look small AND have no recognizable
        # range-chart roots.
        if not any(k in parsed for k in ('sections', 'species_ranges',
                                         'biozones', 'other_fossils')):
            # If safe_json_loads returned a tiny inner object, the normalizer
            # should surface a warning flag in the result.
            r = normalize_result(parsed)
            self.assertTrue(r.get('_warnings') or r.get('_extras', {}).get(
                '_truncated_rescue'))


if __name__ == '__main__':
    unittest.main()