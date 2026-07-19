// tests_aggregate.js - JS-side regression tests for rcaMergeResults.
// Run:  node tests_aggregate.js
// Cross-language parity: each test case is also covered by tests_core.py.
// We keep them textually parallel so a regression on either side is easy
// to spot. Pass = 0, Fail != 0 exit code (no test framework required).

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// Resolve aggregate.js relative to this test file. tests_aggregate.js lives
// in the project root (next to js/aggregate.js), so __dirname is the project root.
let src = fs.readFileSync(path.join(__dirname, 'js', 'aggregate.js'), 'utf8');
// Top-level `const` bindings are not exposed on the VM context object.
// Append a postamble that aliases them onto `globalThis` (== ctx in vm).
src += '\nglobalThis.RCA_DEFAULT_KEYMAP = RCA_DEFAULT_KEYMAP;\n'
  + 'globalThis.RCA_COLUMNAR_KEYMAP = RCA_COLUMNAR_KEYMAP;\n'
  + 'globalThis.RCA_ABUNDANCE_KEYMAP = RCA_ABUNDANCE_KEYMAP;\n'
  + 'globalThis.rcaMergeResults = rcaMergeResults;\n';
const ctx = {};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const {
  rcaMergeResults,
  RCA_DEFAULT_KEYMAP,
  RCA_COLUMNAR_KEYMAP,
  RCA_ABUNDANCE_KEYMAP,
} = ctx;

let pass = 0, fail = 0;
function check(name, cond) {
  if (cond) { pass++; console.log('PASS', name); }
  else { fail++; console.log('FAIL', name); }
}
function assert(name, got, want) {
  check(name, JSON.stringify(got) === JSON.stringify(want));
}

// --- range-chart backward-compat ---
{
  const r1 = {
    sections: [{name:'A',age_range:'Permian',formations:['F1'],formation_thickness_m:'',coordinates:''}],
    species_ranges: [
      {species:'Neoalbaillella optima',section:'A',range_base:'7',range_top:'9',biozone:'Z'},
      {species:'Entactinia sashidai',section:'A',range_base:'22',range_top:'26',biozone:''},
    ],
    biozones: [{name:'N. optima Zone',age:'Late',thickness_m:'3m'}],
    other_fossils: ['Ammonoid: X'],
    confidence: 0.8,
  };
  const r2 = {
    sections: [{name:'A',age_range:'Permian',formations:['F1','F2'],formation_thickness_m:'',coordinates:''}],
    species_ranges: [
      {species:'Neoalbaillella optima',section:'A',range_base:'7',range_top:'9',biozone:'Z'},
      {species:'Paracopicyntra longispina',section:'A',range_base:'20',range_top:'26',biozone:''},
    ],
    biozones: [{name:'N. optima Zone',age:'Late',thickness_m:'3m'}],
    other_fossils: ['Ammonoid: X','Ammonoid: Y'],
    confidence: 0.9,
  };
  const m = rcaMergeResults([r1, r2]);
  assert('rc-runs', m.runs, 2);
  check('rc-species-count', m.species_ranges.length === 3);
  assert('rc-top-agreement', m.species_ranges[0].agreement, '2/2');
  check('rc-low-agreement', m.species_ranges.some((s) => s.agreement === '1/2'));
  assert('rc-formations-union', m.sections[0].formations, ['F1', 'F2']);
  assert('rc-biozones-dedup', m.biozones.length, 1);
  assert('rc-fossils-union', m.other_fossils.length, 2);
  assert('rc-confidence-mean', m.confidence, 0.85);
}

// --- single run passes through with 1/1 ---
{
  const single = rcaMergeResults([{
    species_ranges: [{species:'A',section:'',range_base:'b',range_top:'t',biozone:'z'}],
    sections: [],
    biozones: [],
    other_fossils: [],
    confidence: 0.5,
  }]);
  check('rc-single-runs', single.runs === 1);
  assert('rc-single-agreement', single.species_ranges[0].agreement, '1/1');
}

// --- empty ---
{
  const empty = rcaMergeResults([]);
  assert('rc-empty-runs', empty.runs, 1);
  check('rc-empty-species', empty.species_ranges.length === 0);
}

// --- columnar-section mode (keymap) ---
{
  const r1 = {
    sections: [
      {id:'Ki-1',group:'Lower',lithology_blocks:[],age_units:[],samples:[],
       coordinates_text:'',thickness_m:'500m',confidence_by_section:0.7},
    ],
    fossil_legend: [{marker:'J',meaning:'Jurassic radiolaria'}],
    lithology_legend: [{pattern:'chert',meaning:'Chert'}],
    cross_beds: [],
    confidence: 0.7,
  };
  const r2 = {
    sections: [
      {id:'Ki-1',group:'Lower',lithology_blocks:[],age_units:[],samples:[],
       coordinates_text:'NW wing',thickness_m:'500m',confidence_by_section:0.8},
    ],
    fossil_legend: [{marker:'J',meaning:'Jurassic radiolaria'}],
    lithology_legend: [{pattern:'chert',meaning:'Chert'}],
    cross_beds: [],
    confidence: 0.6,
  };
  const m = rcaMergeResults([r1, r2], null, RCA_COLUMNAR_KEYMAP);
  assert('col-runs', m.runs, 2);
  check('col-sections-count', m.sections.length === 1);
  assert('col-section-id', m.sections[0].id, 'Ki-1');
  assert('col-section-agreement', m.sections[0].agreement, '2/2');
  assert('col-coords-mode', m.sections[0].coordinates_text, 'NW wing');
  assert('col-confidence-mean', m.confidence, 0.65);
  assert('col-fossil-legend-dedup', m.fossil_legend.length, 1);
  assert('col-lithology-legend-dedup', m.lithology_legend.length, 1);
}

// --- columnar: per-section fields survive the mode-merge ---
{
  const r = [
    {
      sections: [
        {id:'Ki-1',group:'Lower',lithology_blocks:[
          {pattern:'chert',range_top_idx:8,range_base_idx:1}],age_units:[],
          samples:[{bed_idx:5,fossil_marker:'J',ref:'Kamata, 1996'}],
          coordinates_text:'',thickness_m:'500m',confidence_by_section:0.7},
        {id:'Ki-2',group:'Lower',lithology_blocks:[],
          age_units:[{label:'Lower',range_top_idx:8,range_base_idx:4}],
          samples:[],coordinates_text:'',thickness_m:'',confidence_by_section:0.6},
      ],
      fossil_legend: [], lithology_legend: [], cross_beds: [],
      confidence: 0.65,
    },
  ];
  const m = rcaMergeResults(r, null, RCA_COLUMNAR_KEYMAP);
  check('col-multi-section-count', m.sections.length === 2);
  const ki1 = m.sections.find((s) => s.id === 'Ki-1');
  check('col-block-survives', Array.isArray(ki1.lithology_blocks) && ki1.lithology_blocks.length === 1);
  check('col-sample-survives', Array.isArray(ki1.samples) && ki1.samples.length === 1);
  const ki2 = m.sections.find((s) => s.id === 'Ki-2');
  check('col-age-unit-survives', Array.isArray(ki2.age_units) && ki2.age_units.length === 1);
}

// --- range-chart vs columnar use different default shapes ---
{
  check('keymap-defaults-differ', RCA_DEFAULT_KEYMAP !== RCA_COLUMNAR_KEYMAP);
  check('default-keymap-is-range', RCA_DEFAULT_KEYMAP.primary === 'species_ranges');
  check('columnar-keymap-is-sections', RCA_COLUMNAR_KEYMAP.primary === 'sections');
}

// --- abundance-diagram: dedup by (site, taxon, level) + majority vote ---
// Parallel to tests_core.test_abundance_schema_and_merge.
{
  check('ab-keymap-primary', RCA_ABUNDANCE_KEYMAP.primary === 'abundances');
  const run = (ab) => ({
    sites: [{name:'Core A',location:'',age_range:'',depth_unit:'cm'}],
    abundances: [{taxon:'Pinus',site:'Core A',level:'120 cm',depth:'120',abundance:ab,abundance_unit:'%'}],
    zones: [{name:'PAZ-3',age:'',level_range:'80-140 cm'}],
    confidence: 0.8,
  });
  const m = rcaMergeResults([run('35'), run('35'), run('40')], 3, RCA_ABUNDANCE_KEYMAP);
  check('ab-merge-dedup', m.abundances.length === 1);
  check('ab-merge-agreement', m.abundances[0].agreement === '3/3');
  check('ab-merge-majority', m.abundances[0].abundance === '35');
  check('ab-merge-sites-dedup', m.sites.length === 1);
  check('ab-merge-zones-dedup', m.zones.length === 1);
}

// --- named-list items lacking name/marker/meaning must not be dropped ---
// Parallel to tests_core.test_named_list_no_label_not_dropped.
{
  const abRun = (ab) => ({
    sites: [{name:'',location:'35N',age_range:'Holocene',depth_unit:'cm'}],
    abundances: [{taxon:'Pinus',site:'',level:'120 cm',depth:'120',abundance:ab,abundance_unit:'%'}],
    zones: [{name:'PAZ-3',age:'',level_range:'80-140 cm'}],
    confidence: 0.8,
  });
  const m = rcaMergeResults([abRun('35'), abRun('35')], 2, RCA_ABUNDANCE_KEYMAP);
  check('nl-empty-name-site-kept', m.sites.length === 1);
  check('nl-empty-name-site-fields', m.sites[0].location === '35N');

  const cbRun = (fb) => ({
    sections: [{id:'Ki-1',group:'L',lithology_blocks:[],age_units:[],samples:[],
                coordinates_text:'',thickness_m:'',confidence_by_section:0.7}],
    fossil_legend: [], lithology_legend: [],
    cross_beds: [{from_section:'Ki-1',from_bed_idx:fb,to_section:'Ki-2',to_bed_idx:4}],
    confidence: 0.7,
  });
  const m2 = rcaMergeResults([cbRun(3), cbRun(3)], 2, RCA_COLUMNAR_KEYMAP);
  check('nl-crossbeds-identical-kept', m2.cross_beds.length === 1);
  const m3 = rcaMergeResults([cbRun(3), cbRun(9)], 2, RCA_COLUMNAR_KEYMAP);
  check('nl-crossbeds-distinct-kept', m3.cross_beds.length === 2);
}

console.log('---', pass, 'passed,', fail, 'failed ---');
process.exit(fail ? 1 : 0);
