"""Tests for rca_core.editable (capture / apply edits on result dicts)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core.editable import (
    apply_edits, capture_edits, is_dirty, new_row_template,
)

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


def test_capture_no_changes():
    before = {"species_ranges": [{"species": "A", "section": "S1"}]}
    after = {"species_ranges": [{"species": "A", "section": "S1"}]}
    check("cap-clean", capture_edits(before, after) == {})


def test_capture_single_cell():
    before = {"species_ranges": [{"species": "A", "section": "S1"}]}
    after = {"species_ranges": [{"species": "A", "section": "S1", "range_base": "Bed 1"}]}
    e = capture_edits(before, after)
    check("cap-one-row", 0 in e["species_ranges"])
    check("cap-one-col", e["species_ranges"][0].get("range_base") == "Bed 1")


def test_capture_multiple_rows():
    before = {"species_ranges": [
        {"species": "A", "section": "S1"},
        {"species": "B", "section": "S2"},
    ]}
    after = {"species_ranges": [
        {"species": "A*", "section": "S1"},
        {"species": "B", "section": "S2*"},
    ]}
    e = capture_edits(before, after)
    check("cap-row-0", e["species_ranges"][0] == {"species": "A*"})
    check("cap-row-1", e["species_ranges"][1] == {"section": "S2*"})


def test_capture_new_row():
    before = {"species_ranges": [{"species": "A"}]}
    after = {"species_ranges": [{"species": "A"}, {"species": "B"}]}
    e = capture_edits(before, after)
    check("cap-new-row-key", "new_1" in e["species_ranges"])
    check("cap-new-row-shape", e["species_ranges"]["new_1"]["species"] == "B")


def test_capture_strip_whitespace():
    before = {"species_ranges": [{"species": "A", "section": "S1"}]}
    after = {"species_ranges": [{"species": "  A  ", "section": " S1"}]}
    check("cap-whitespace-equal", capture_edits(before, after) == {})


def test_apply_existing():
    result = {"species_ranges": [{"species": "A", "section": "S1"}]}
    apply_edits(result, {"species_ranges": {0: {"species": "A*"}}})
    check("apply-cell", result["species_ranges"][0]["species"] == "A*")


def test_apply_new_row():
    result = {"species_ranges": [{"species": "A"}]}
    apply_edits(result, {"species_ranges": {"new_1": {"species": "B"}}})
    check("apply-new-len", len(result["species_ranges"]) == 2)
    check("apply-new-val", result["species_ranges"][1]["species"] == "B")


def test_apply_out_of_range():
    result = {"species_ranges": [{"species": "A"}]}
    apply_edits(result, {"species_ranges": {99: {"species": "B"}}})
    check("apply-oor-len", len(result["species_ranges"]) == 1)


def test_apply_ignores_extras():
    result = {"species_ranges": [{"species": "A", "_extras": {"k": "v"}}]}
    apply_edits(result, {"species_ranges": {0: {"_extras": {"k": "X"}}}})
    # The _extras key on the dict object is NOT replaced by the edit;
    # edits.target._extras are silently ignored.
    check("apply-extras-unchanged", result["species_ranges"][0]["_extras"] == {"k": "v"})


def test_is_dirty():
    a = {"species_ranges": [{"species": "A"}]}
    b = {"species_ranges": [{"species": "A*"}]}
    check("dirty-true", is_dirty(a, b))
    check("dirty-false", not is_dirty(a, a))


def test_template_keys():
    sp = new_row_template("species_ranges")
    check("tpl-species-key", "species" in sp)
    check("tpl-section-key", "section" in sp)
    bz = new_row_template("biozones")
    check("tpl-biozone-key", "name" in bz)


def test_apply_table_edits_basic():
    """End-to-end: table-style rows -> result dict."""
    from rca_core.exporter import apply_table_edits
    data = {
        'sections': [{'name': 'A', 'age_range': 'P', 'formations': ['F1'],
                      'formation_thickness_m': '', 'coordinates': ''}],
        'species_ranges': [], 'biozones': [], 'other_fossils': [],
    }
    rows = [
        ['1', 'Sec A2', 'Permian', 'F2;F3', '10m', '31N'],
        ['2', 'Sec B', 'Triassic', 'G1', '', ''],
    ]
    apply_table_edits(data, 'sections', rows)
    check('ate-2rows', len(data['sections']) == 2)
    check('ate-name', data['sections'][0]['name'] == 'Sec A2')
    check('ate-formations-list', data['sections'][0]['formations'] == ['F2', 'F3'])
    check('ate-empty-formation', data['sections'][1]['formations'] == ['G1'])
    check('ate-thickness', data['sections'][0]['formation_thickness_m'] == '10m')


def test_apply_table_edits_cross_beds_int():
    from rca_core.exporter import apply_table_edits
    data = {'sections': [], 'species_ranges': [], 'biozones': [],
            'fossil_legend': [], 'lithology_legend': [],
            'cross_beds': [{'from_section': 'A', 'from_bed_idx': 3,
                            'to_section': 'B', 'to_bed_idx': 4}]}
    rows = [['1', 'A', '5', 'B', '7']]
    apply_table_edits(data, 'cross_beds', rows)
    check('ate-cb-from-bed', data['cross_beds'][0]['from_bed_idx'] == 5)
    check('ate-cb-to-bed', data['cross_beds'][0]['to_bed_idx'] == 7)


def test_apply_table_edits_other_fossils_strings():
    from rca_core.exporter import apply_table_edits
    data = {'sections': [], 'species_ranges': [], 'biozones': [],
            'fossil_legend': [], 'lithology_legend': [], 'cross_beds': [],
            'other_fossils': []}
    rows = [['1', 'Ammonite: X'], ['2', 'Conodont: Y']]
    apply_table_edits(data, 'other_fossils', rows)
    check('ate-of-type', isinstance(data['other_fossils'], list))
    check('ate-of-str', all(isinstance(x, str) for x in data['other_fossils']))
    check('ate-of-len', len(data['other_fossils']) == 2)


def test_apply_table_edits_skip_placeholder():
    from rca_core.exporter import apply_table_edits
    data = {'sections': [{'name': 'A', 'age_range': 'P', 'formations': [],
                          'formation_thickness_m': '', 'coordinates': ''}],
            'species_ranges': [], 'biozones': [], 'other_fossils': []}
    rows = [['1', 'A', 'P', '', '', ''], ['2', '', '', '', '', '']]
    apply_table_edits(data, 'sections', rows)
    check('ate-skip-empty', len(data['sections']) == 1)


def test_apply_table_edits_ignores_agreement_col():
    """The 'agreement' column is auto-computed at merge time and must
    not overwrite a manually-typed value. ``apply_table_edits`` is the
    Edit path; it should not touch agreement at all."""
    from rca_core.exporter import apply_table_edits
    data = {
        'sections': [], 'species_ranges': [
            {'species': 'X', 'section': 'S', 'range_base': 'b',
             'range_top': 't', 'biozone': 'Z', 'agreement': '3/3'},
        ],
        'biozones': [],
    }
    rows = [['1', 'X*', 'S', 'b', 't', 'Z', 'USER-TYPED']]
    apply_table_edits(data, 'species_ranges', rows)
    # Edit path doesn't preserve the pre-existing agreement — that's
    # re-computed at the next merge. The important thing is that the
    # user's edits to other cells land correctly.
    check('ate-species-edited', data['species_ranges'][0]['species'] == 'X*')
    check('ate-section-kept', data['species_ranges'][0]['section'] == 'S')
    check('ate-biozone-kept', data['species_ranges'][0]['biozone'] == 'Z')


test_capture_no_changes()
test_capture_single_cell()
test_capture_multiple_rows()
test_capture_new_row()
test_capture_strip_whitespace()
test_apply_existing()
test_apply_new_row()
test_apply_out_of_range()
test_apply_ignores_extras()
test_is_dirty()
test_template_keys()
test_apply_table_edits_basic()
test_apply_table_edits_cross_beds_int()
test_apply_table_edits_other_fossils_strings()
test_apply_table_edits_skip_placeholder()
test_apply_table_edits_ignores_agreement_col()
print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)


