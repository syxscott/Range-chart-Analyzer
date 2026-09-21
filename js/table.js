// table.js - render extraction result into four data tables + confidence ring.
'use strict';

// HTML-escape a value for safe insertion as text content.
function rcaEsc(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// M12: HTML-escape for attribute contexts. rcaEsc already encodes both
// quote styles (&#34; / &#39;), so the text stored here is safe inside the
// double-quoted attributes the renderer emits. Kept as a named wrapper so
// attribute call sites read distinctly from text call sites, and so a
// future contributor cannot silently reintroduce an attribute-breaking
// value.
function rcaEscAttr(value) {
  return rcaEsc(value);
}

// ---- chart-shape predicates -------------------------------------------
//
// M4 (REVIEW-2026-09-20): one-to-one mirrors of the rca_core/exporter.py
// predicates (`_looks_zonation_chart`, `_looks_abundance`, `_looks_columnar`,
// `_looks_phylogenetic_tree`). They used to be inlined as ad-hoc `has*Shape`
// booleans whose conditions drifted from Python — most visibly
// `hasColumnarShape`, which accepted ANY `sections` array (so a plain
// range-chart result whose first section was `{"name": "A"}` could be routed
// to the columnar tables) while Python keys on "the first section is a dict
// carrying an `id`". Keeping them as named functions also lets
// tests_frontend.js assert the dispatch order against
// `get_configs_for_result` instead of trusting a comment.
function rcaLooksAbundance(data) {
  // exporter.py:242-256 — an EMPTY `abundances: []` placeholder is every
  // normalized result's default, so it must not route to abundance tables.
  if (!data) return false;
  return Array.isArray(data.abundances) && data.abundances.length > 0;
}

function rcaLooksColumnar(data) {
  // exporter.py:222-239 — "id" (column label) is the definitive columnar
  // marker; range-chart sections carry "name" and never "id".
  if (!data) return false;
  const sects = data.sections;
  if (!Array.isArray(sects) || sects.length === 0) return false;
  return rcaIsDict(sects[0]) && 'id' in sects[0];
}

function rcaLooksColumnarEmptySections(data) {
  // exporter.py:664-669 (I7): the VLM produced NO columns but the payload
  // still declares the columnar schema through its columnar-only keys.
  if (!data) return false;
  if (!Array.isArray(data.sections)) return false;
  return ('fossil_legend' in data) || ('lithology_legend' in data)
    || ('cross_beds' in data);
}

function rcaLooksPhylogeneticTree(data) {
  // exporter.py:259-268 — nodes with parent/id structure.
  if (!data) return false;
  const nodes = data.nodes;
  if (!Array.isArray(nodes) || nodes.length === 0) return false;
  const first = nodes[0];
  return rcaIsDict(first) && 'id' in first && 'parent' in first;
}

function rcaLooksZonationChart(data) {
  // exporter.py:271-291 — correlations, zone-rank rows, or (I7-style)
  // populated zonation descriptors with every zone row unreadable.
  if (!data) return false;
  if (Array.isArray(data.correlations) && data.correlations.length > 0) return true;
  const zones = data.zones;
  if (Array.isArray(zones) && zones.length > 0) {
    const first = zones[0];
    if (rcaIsDict(first)
        && ('rank' in first || 'zonation' in first)) {
      return true;
    }
  }
  if (Array.isArray(data.zonations) && data.zonations.length > 0) return true;
  return false;
}

// Table definitions: key on the result object, i18n title, columns, and a
// row-extractor producing an array of cell values in column order.
// `italic` marks the species column for styling. These configs are shared
// with the export path so CSV/TSV columns match the rendered table exactly.
// When `data.runs > 1` (multi-run merge), the species table gains an
// "agreement" column showing how many runs produced each row.
//
// M4 (REVIEW-2026-09-20): the branch ORDER is the mirror of
// rca_core/exporter.py get_configs_for_result — zonation → abundance →
// columnar (incl. the empty-sections fallback) → phylogenetic tree →
// range chart (default). The JS used to test abundance first and zonation
// fourth, so a payload that carried both `abundances` rows and zonation
// descriptors rendered as an abundance diagram in the browser and as a
// zones table in the GUI/Excel export.
//
// LEFTOVER (documented, not mirrored): Python runs `detect_tableless_mode`
// FIRST and returns NO configs for the assistant modes (chemical_stratigraphy
// / paleomap / scatter_plot), so the GUI hides the table tab instead of
// "exporting" four empty range-chart sheets. js/table.js has no equivalent
// and keeps rendering empty tables; index.html also has no option to reach
// those modes from the browser.
function rcaTableConfigs(data) {
  const multi = data && Number(data.runs) > 1;
  if (rcaLooksZonationChart(data)) return rcaZonationChartConfigs(data, multi);
  if (rcaLooksAbundance(data)) return rcaAbundanceDiagramConfigs(data, multi);
  if (rcaLooksColumnar(data) || rcaLooksColumnarEmptySections(data)) {
    return rcaColumnarSectionConfigs(data, multi);
  }
  if (rcaLooksPhylogeneticTree(data)) return rcaPhylogeneticTreeConfigs(data, multi);
  return rcaRangeChartConfigs(data, multi);
}

// ---- editable column metadata (FE-BORROW-2026-09-20, 域T) ------------
//
// One `edit` entry per column, index-aligned with `cols` (same length —
// tests_edit_history.js asserts the alignment for EVERY table of EVERY chart
// mode, so a future column cannot silently arrive without an editor).
//
//   field     model key the cell writes back into (rca_core/exporter.py
//             TABLE_CONFIGS `data_keys`, NOT the i18n label)
//   type      mirrors rca_core/exporter.py COL_TYPES verbatim:
//             'str' | 'number' | 'int' | 'float' | 'list' | 'bool_yn'
//             | 'nullable_str'. It drives BOTH the text→value coercion
//             (exporter.py:_coerce_cell) and the numeric "is this cell a
//             number?" validation, so the browser and the Qt Apply-edits path
//             cannot disagree about what a typed cell means.
//   editable  false for computed columns ('agreement', mirrored from
//             editable.py:_template_from_cfg, which skips `agreement`) and for
//             identity columns the other tables reference ('id', node 'id').
//   validate  extra validator: 'stage' (ICS stage name) or a range pair
//             ('bed-pair' / 'range-pair' / 'age-pair', with `peer` giving
//             [topField, baseField]) for the inversions quality.js already
//             penalises (range_top_lt_base / bed_index_order_invalid /
//             fad_lt_lad).
//   model     nested-model key when it differs from the exported key
//             (lithology_blocks top_idx -> range_top_idx), mirroring
//             exporter.py `nested_in.key_map`.
function rcaEC(field, type, extra) {
  const spec = { field: field, type: type || 'str', editable: true, validate: null };
  if (extra && typeof extra === 'object') {
    for (const k of Object.keys(extra)) spec[k] = extra[k];
  }
  return spec;
}

// A column that exists for display only (agreement / id / reference keys).
function rcaECro(field, type) {
  return rcaEC(field, type || 'str', { editable: false });
}

// -------- abundance-diagram (pollen / percentage) mode --------
function rcaAbundanceDiagramConfigs(data, multi) {
  const abCols = ['col.taxon', 'col.site', 'col.level', 'col.depth', 'col.abundance', 'col.abundanceUnit'];
  const abRow = (r) => [r.taxon, r.site, r.level, r.depth, r.abundance, r.abundance_unit];
  const abColsFinal = multi ? abCols.concat(['col.agreement']) : abCols;
  const abRowFinal = multi ? (r) => abRow(r).concat([r.agreement || '']) : abRow;
  return [
    {
      id: 'sites',
      titleKey: 'sec.sites',
      cols: ['col.name', 'col.location', 'col.ageRange', 'col.depthUnit'],
      edit: [rcaEC('name'), rcaEC('location'), rcaEC('age_range'), rcaEC('depth_unit')],
      italicCol: -1,
      row: (s) => [s.name, s.location, s.age_range, s.depth_unit],
    },
    {
      id: 'abundances',
      titleKey: 'sec.abundances',
      cols: abColsFinal,
      // COL_TYPES['abundances'] is {} — depth / abundance stay TEXT on the
      // Python side ("35%", "common"), so no numeric validation here.
      edit: abColsFinal.map((c) => (c === 'col.agreement' ? rcaECro('agreement') : rcaEC(
        c === 'col.taxon' ? 'taxon'
          : c === 'col.site' ? 'site'
            : c === 'col.level' ? 'level'
              : c === 'col.depth' ? 'depth'
                : c === 'col.abundance' ? 'abundance' : 'abundance_unit'))),
      italicCol: 0,
      row: abRowFinal,
    },
    {
      id: 'zones',
      titleKey: 'sec.zones',
      cols: ['col.name', 'col.age', 'col.levelRange'],
      edit: [rcaEC('name'), rcaEC('age'), rcaEC('level_range')],
      italicCol: -1,
      row: (z) => [z.name, z.age, z.level_range],
    },
  ];
}

// -------- columnar-section mode (exporter.py _columnar_section_tables) --------
function rcaColumnarSectionConfigs(data, multi) {
  const secCols = [
    'col.sectionId',
    'col.sectionGroup',
    'col.thickness',
    'col.coordinates',
  ];
  const secColsFinal = multi ? secCols.concat(['col.agreement']) : secCols;
  const cols4 = (sec) => [
    sec.id || '',
    sec.group || '',
    sec.thickness_m || '',
    sec.coordinates_text || '',
  ];
  const secRowFinal = multi
    ? (sec) => cols4(sec).concat([sec.agreement || ''])
    : cols4;
  // Sprint B (REVIEW-2026-09-04): the three columnar sub-table rows now
  // come from rcaColumnarSubTableRows (below), the SINGLE row source
  // shared by rendering and export. The old code flattened them here and
  // stashed `data._lithology_blocks_rows` / `_age_units_rows` /
  // `_samples_rows` on the result — but rcaRenderResults read
  // `data[cfg.id]` (always undefined for these nested keys), so all three
  // tables rendered "no rows" and never showed copy/CSV buttons, while
  // export read the stash. The stash also leaked underscore-prefixed
  // internal keys into the "export all (JSON)" payload. Both problems are
  // gone now that render and export read the same derived arrays.
  return [
    {
      id: 'sections',
      titleKey: 'sec.sections',
      cols: secColsFinal,
      // `id` is the join key every sub-table row carries as `section_id`, so
      // editing it in place would orphan the lithology / age / sample rows —
      // read-only, like `agreement`. thickness_m is COL_TYPES 'number'.
      edit: secColsFinal.map((c) => (c === 'col.sectionId' ? rcaECro('id')
        : c === 'col.sectionGroup' ? rcaEC('group')
          : c === 'col.thickness' ? rcaEC('thickness_m', 'number')
            : c === 'col.coordinates' ? rcaEC('coordinates_text')
              : rcaECro('agreement'))),
      italicCol: 0,
      row: secRowFinal,
    },
    {
      id: 'lithology_blocks',
      titleKey: 'sec.lithologyBlocks',
      cols: ['col.secId', 'col.pattern', 'col.topIdx', 'col.baseIdx'],
      edit: [rcaECro('section_id'), rcaEC('pattern'),
        rcaEC('top_idx', 'int', { validate: 'bed-pair', peer: ['top_idx', 'base_idx'], model: 'range_top_idx' }),
        rcaEC('base_idx', 'int', { validate: 'bed-pair', peer: ['top_idx', 'base_idx'], model: 'range_base_idx' })],
      // exporter.py:511-535 `nested_in` — the model has no top-level
      // `lithology_blocks` key; the rows are flattened out of
      // ``sections[i]`` and must be written back through ``(section_index,
      // sub_key, row_index)`` (the `_src` provenance rcaColumnarSubTableRows
      // now records, exactly like exporter.py:441).
      nested: { parent: 'sections', subKey: 'lithology_blocks', derived: ['section_id'] },
      italicCol: -1,
      row: (r) => [r.section_id, r.pattern,
        r.top_idx == null ? '' : String(r.top_idx),
        r.base_idx == null ? '' : String(r.base_idx)],
    },
    {
      id: 'age_units',
      titleKey: 'sec.ageUnits',
      cols: ['col.secId', 'col.label', 'col.topIdx', 'col.baseIdx'],
      edit: [rcaECro('section_id'), rcaEC('label'),
        rcaEC('top_idx', 'int', { validate: 'bed-pair', peer: ['top_idx', 'base_idx'], model: 'range_top_idx' }),
        rcaEC('base_idx', 'int', { validate: 'bed-pair', peer: ['top_idx', 'base_idx'], model: 'range_base_idx' })],
      nested: { parent: 'sections', subKey: 'age_units', derived: ['section_id'] },
      italicCol: -1,
      row: (r) => [r.section_id, r.label,
        r.top_idx == null ? '' : String(r.top_idx),
        r.base_idx == null ? '' : String(r.base_idx)],
    },
    {
      id: 'samples',
      titleKey: 'sec.samples',
      cols: ['col.secId', 'col.bedIdx', 'col.fossilMarker', 'col.ref'],
      edit: [rcaECro('section_id'), rcaEC('bed_idx', 'int'),
        rcaEC('fossil_marker'), rcaEC('ref')],
      // samples keep their export names (exporter.py:563-570 key_map {}),
      // so no `model` override is needed on the specs above.
      nested: { parent: 'sections', subKey: 'samples', derived: ['section_id'] },
      italicCol: -1,
      row: (r) => [r.section_id,
        r.bed_idx == null ? '' : String(r.bed_idx),
        r.fossil_marker || '', r.ref || ''],
    },
    {
      id: 'fossil_legend',
      titleKey: 'sec.fossils', // reuse fossils title to keep the keyset small
      cols: ['col.fossilMarker', 'col.fossilMeaning'],
      edit: [rcaEC('marker'), rcaEC('meaning')],
      italicCol: -1,
      row: (it) => [it.marker || '', it.meaning || ''],
    },
    {
      id: 'lithology_legend',
      titleKey: 'sec.columnarLithology',
      cols: ['col.lithologyPattern', 'col.lithologyMeaning'],
      edit: [rcaEC('pattern'), rcaEC('meaning')],
      italicCol: -1,
      row: (it) => [it.pattern || it.marker || '', it.meaning || ''],
    },
    {
      id: 'cross_beds',
      titleKey: 'sec.crossBeds',
      cols: ['col.crossFrom', 'col.crossFromBed', 'col.crossTo', 'col.crossToBed'],
      edit: [rcaEC('from_section'), rcaEC('from_bed_idx', 'int'),
        rcaEC('to_section'), rcaEC('to_bed_idx', 'int')],
      italicCol: -1,
      row: (it) => [
        it.from_section || '',
        it.from_bed_idx == null ? '' : String(it.from_bed_idx),
        it.to_section || '',
        it.to_bed_idx == null ? '' : String(it.to_bed_idx),
      ],
    },
  ];
}

// -------- phylogenetic-tree mode (UI-FIX 2026-08-07) --------
// Mirror of rca_core.exporter._looks_phylogenetic_tree +
// _phylogenetic_tree_tables: without this the web frontend could select the
// mode but rendered four empty range-chart tables for a tree payload (no
// nodes table existed).
function rcaPhylogeneticTreeConfigs(data, multi) {
  return [
    {
      id: 'nodes',
      titleKey: 'sec.nodes',
      cols: ['col.nodeId', 'col.parent', 'col.name', 'col.isLeaf',
             'col.branchLength', 'col.nodeAgeMa', 'col.support'],
      // Types are COL_TYPES['nodes'] (exporter.py:1038-1053): is_leaf is the
      // Y/N display of a bool, parent is nullable_str ("" -> None so the
      // "root has parent None" invariant survives), the three numbers are
      // floats. `id` stays read-only because `parent` values reference it.
      edit: [rcaECro('id'), rcaEC('parent', 'nullable_str'), rcaEC('name'),
        rcaEC('is_leaf', 'bool_yn'), rcaEC('branch_length', 'float'),
        rcaEC('node_age_ma', 'float'), rcaEC('support', 'float')],
      italicCol: -1,
      row: (n) => [
        n.id,
        n.parent == null ? '' : n.parent,
        n.name,
        n.is_leaf ? 'Y' : 'N',
        n.branch_length == null ? '' : String(n.branch_length),
        n.node_age_ma == null ? '' : String(n.node_age_ma),
        n.support == null ? '' : String(n.support),
      ],
    },
  ];
}

// -------- zonation / correlation chart mode (UI-REVIEW-2026-09-05) --------
// Mirror of rca_core.exporter._looks_zonation_chart + _zonation_chart_tables
// (get_configs_for_result checks it FIRST, before abundance/columnar).
function rcaZonationChartConfigs(data, multi) {
  return [
    {
      id: 'zonations',
      titleKey: 'sec.zonations',
      cols: ['col.name', 'col.region', 'col.framework', 'col.reference'],
      edit: [rcaEC('name'), rcaEC('region'), rcaEC('framework'), rcaEC('reference')],
      italicCol: -1,
      row: (z) => [
        z.name || '',
        z.region || '',
        z.framework || '',
        z.reference || '',
      ],
    },
    {
      id: 'zones',
      titleKey: 'sec.zonesTable',
      cols: ['col.name', 'col.zonation', 'col.rank', 'col.ageSpan',
             'col.baseAge', 'col.topAge', 'col.stage', 'col.definedBy',
             'col.note'],
      // col.stage runs the ICS validator (RCA_ICS_TABLE + the period alias
      // maps, js/ics_table.js); the two Ma columns are a top/base pair that
      // must not cross (top_age <= base_age, younger sits higher).
      edit: [rcaEC('name'), rcaEC('zonation'), rcaEC('rank'), rcaEC('age_span'),
        rcaEC('base_age', 'float', { validate: 'age-pair', peer: ['top_age', 'base_age'] }),
        rcaEC('top_age', 'float', { validate: 'age-pair', peer: ['top_age', 'base_age'] }),
        rcaEC('stage', 'str', { validate: 'stage' }),
        rcaEC('defined_by'), rcaEC('note')],
      italicCol: 0,
      row: (r) => [
        r.name || '',
        r.zonation || '',
        r.rank || '',
        r.age_span || '',
        r.base_age || '',
        r.top_age || '',
        r.stage || '',
        r.defined_by || '',
        r.note || '',
      ],
    },
    {
      id: 'correlations',
      titleKey: 'sec.correlations',
      cols: ['col.fromZone', 'col.fromZonation', 'col.toZone',
             'col.toZonation', 'col.basis', 'col.note'],
      edit: [rcaEC('from_zone'), rcaEC('from_zonation'), rcaEC('to_zone'),
        rcaEC('to_zonation'), rcaEC('basis'), rcaEC('note')],
      italicCol: -1,
      row: (c) => [
        c.from_zone || '',
        c.from_zonation || '',
        c.to_zone || '',
        c.to_zonation || '',
        c.basis || '',
        c.note || '',
      ],
    },
  ];
}

// -------- range-chart mode (default) --------
// H3 (REVIEW-2026-08-19): dynamically expand the species_ranges CSV
// columns based on which optional fields are populated. PARITY
// (rca_core/exporter.py _range_chart_tables, REVIEW-2026-09-10): the Python
// exporter grew the same conditional columns, so a researcher gets
// `author_year` / `note` / `confidence` in their CSV only when the model
// actually emitted them — on both transports, with the same column order.
function rcaRangeChartConfigs(data, multi) {
  const speciesBaseCols = ['col.species', 'col.section', 'col.rangeBase', 'col.rangeTop', 'col.biozone'];
  const speciesBaseRow = (r) => [r.species, r.section, r.range_base, r.range_top, r.biozone];
  // exporter.py:319 `_opt_text` — "is this value present?" is a NONE test,
  // NOT `v || ''`: a legitimate 0 (range_top_idx, confidence) or an empty
  // author string must still count as "populated" and be written out.
  const optText = (v) => (v === null || v === undefined ? '' : String(v));
  // [column label, getter, editable-column spec] — the getter doubles as the
  // "does ANY row populate this column?" predicate, exactly like the Python
  // tuple list; the third slot keeps the editor metadata aligned with the
  // conditional column set (FE-BORROW-2026-09-20).
  const speciesOpt = [
    ['col.authorYear', (r) => optText(r.author_year), rcaEC('author_year')],
    ['col.rangeTopBed', (r) => optText(r.range_top_bed), rcaEC('range_top_bed')],
    ['col.rangeTopIdx', (r) => optText(r.range_top_idx), rcaEC('range_top_idx', 'number')],
    ['col.endpointKind', (r) => (r.endpoint_kind === null || r.endpoint_kind === undefined
      || r.endpoint_kind === 'unknown' ? '' : String(r.endpoint_kind)), rcaEC('endpoint_kind')],
    ['col.occurrenceMode', (r) => (r.occurrence_mode === null || r.occurrence_mode === undefined
      || r.occurrence_mode === 'unknown' ? '' : String(r.occurrence_mode)), rcaEC('occurrence_mode')],
    ['col.colConfidence', (r) => optText(r.confidence), rcaEC('confidence', 'float')],
    ['col.note', (r) => optText(r.note), rcaEC('note')],
  ];
  // Non-dict rows are skipped for the predicates only (exporter.py:325
  // `if isinstance(r, dict)`) — the row renderers still show them.
  const speciesRowsForPred = (Array.isArray(data.species_ranges)
    ? data.species_ranges
    : []).filter((r) => r !== null && typeof r === 'object' && !Array.isArray(r));
  const speciesOptCols = [];
  const speciesOptGetters = [];
  const speciesOptEdit = [];
  for (const entry of speciesOpt) {
    if (speciesRowsForPred.some((r) => entry[1](r))) {
      speciesOptCols.push(entry[0]);
      speciesOptGetters.push(entry[1]);
      speciesOptEdit.push(entry[2]);
    }
  }
  const speciesColsFinal = multi
    ? speciesBaseCols.concat(speciesOptCols, ['col.agreement'])
    : speciesBaseCols.concat(speciesOptCols);
  // Base editor specs, range_base / range_top cross-checked as a pair
  // (quality.js `range_top_lt_base` becomes a red frame instead of a
  // downstream quality penalty).
  const speciesEditFinal = [
    rcaEC('species'), rcaEC('section'),
    rcaEC('range_base', 'str', { validate: 'range-pair', peer: ['range_top', 'range_base'] }),
    rcaEC('range_top', 'str', { validate: 'range-pair', peer: ['range_top', 'range_base'] }),
    rcaEC('biozone'),
  ].concat(speciesOptEdit);
  if (multi) speciesEditFinal.push(rcaECro('agreement'));
  const speciesRowFinal = (r) => {
    const row = speciesBaseRow(r);
    for (const g of speciesOptGetters) row.push(g(r));
    if (multi) row.push(r.agreement || '');
    return row;
  };
  return [
    {
      id: 'sections',
      titleKey: 'sec.sections',
      // REVIEW-2026-07-31: col.formations was renamed to col.formation
      // (P1-9) — the stale key rendered "[?col.formations]" in every
      // section header.
      cols: ['col.name', 'col.ageRange', 'col.formation', 'col.thickness', 'col.coordinates'],
      // `col.formation` renders `formations.join('; ')` and COL_TYPES tags it
      // 'list', so the editor splits on ';' exactly like exporter._coerce_cell.
      // `formation_thickness_m` stays 'str': COL_TYPES['sections'] tags only
      // the columnar `thickness_m`, and _coerce_cell's default branch keeps
      // the operator's text untouched — coercing here would make the browser
      // write 120 where Qt writes "120".
      edit: [rcaEC('name'), rcaEC('age_range'), rcaEC('formations', 'list'),
        rcaEC('formation_thickness_m'), rcaEC('coordinates')],
      italicCol: -1,
      row: (s) => [
        s.name,
        s.age_range,
        (s.formations || []).join('; '),
        s.formation_thickness_m,
        s.coordinates,
      ],
    },
    {
      id: 'species_ranges',
      titleKey: 'sec.species',
      cols: speciesColsFinal,
      edit: speciesEditFinal,
      italicCol: 0,
      row: speciesRowFinal,
    },
    {
      id: 'biozones',
      titleKey: 'sec.biozones',
      // PARITY (exporter.py:97-99): include col.section so the browser's
      // CSV/TSV/JSON export matches the Python exporter's 4-column shape.
      // Without it the two modes exported divergent tables for the same
      // payload, even though the in-memory normalization was equivalent.
      cols: ['col.name', 'col.section', 'col.age', 'col.thickness'],
      edit: [rcaEC('name'), rcaEC('section'), rcaEC('age'),
        rcaEC('thickness_m', 'number')],
      italicCol: -1,
      row: (b) => [b.name, b.section, b.age, b.thickness_m],
    },
    {
      id: 'other_fossils',
      titleKey: 'sec.fossils',
      cols: ['col.fossil'],
      // `scalar: true` — the row itself may be a plain string, so the editor
      // writes the LIST ITEM instead of a key of it (mirrors editable.py's
      // scalar-row branch, which compares strings directly and replays the
      // whole list through `_replaced`). For a dict row the writer resolves
      // fossil/text exactly like the `row` renderer below.
      edit: [rcaEC(null, null, { scalar: true })],
      italicCol: -1,
      // M1 (REVIEW-2026-08-19): rows may be a plain string or a dict.
      // M4 (REVIEW-2026-09-20): the dict keys are now the Python ones —
      // exporter.py:395 reads `f.get("fossil", f.get("text", ""))`, i.e.
      // PRESENCE of "fossil" wins (even an empty/None value), else "text",
      // else "". The old JS chain (`label || species || taxon || name`)
      // invented keys neither transport writes, so a normalised
      // `{"text": "Ammonite sp."}` row exported as an empty CSV cell in the
      // browser while the GUI showed the text — and a row carrying
      // `{"fossil": ""}` fell through to a name that Python never reads.
      // `String(...)` keeps the "never emit [object Object]" property; a
      // None-valued key renders '' exactly like Python's str(None)→"" path
      // through _export_cell_text.
      row: (f) => {
        if (rcaIsDict(f)) {
          const v = 'fossil' in f ? f.fossil : ('text' in f ? f.text : '');
          return [v === null || v === undefined ? '' : String(v)];
        }
        return [f === null || f === undefined ? '' : String(f)];
      },
    },
  ];
}

// Sprint B (REVIEW-2026-09-04): single row source for the three columnar
// sub-tables (lithology_blocks / age_units / samples). The rows live nested
// inside each `sections[i]` entry, so they must be flattened (sections
// concatenated, in order) before rendering or export. Mirrors
// rca_core/exporter.py _columnar_section_tables. Returns an object keyed by
// table id so both consumers resolve rows identically:
//   render: rcaRenderResults  -> rcaRowsForTable(data, cfg.id)
//   export: rcaBuildTableExport -> rcaRowsForTable(data, tableId)
// Pure function — never mutates `data` (the old stash did, leaking
// underscore-prefixed internal keys into the JSON export).
function rcaColumnarSubTableRows(data) {
  const lithology_blocks = [];
  const age_units = [];
  const samples = [];
  const sections = (data && Array.isArray(data.sections)) ? data.sections : [];
  for (let si = 0; si < sections.length; si++) {
    const sec = sections[si];
    const sid = sec && sec.id ? String(sec.id) : '';
    for (let bi = 0; bi < ((sec && sec.lithology_blocks) || []).length; bi++) {
      const b = sec.lithology_blocks[bi];
      lithology_blocks.push({
        section_id: sid,
        pattern: b && b.pattern ? String(b.pattern) : '',
        top_idx: b ? b.range_top_idx : null,
        base_idx: b ? b.range_base_idx : null,
        // FE-BORROW-2026-09-20: exporter.py:441 provenance triple — the
        // editor writes back into sections[si][subKey][ri] instead of
        // inventing a top-level key nothing reads. Never reaches a cell: the
        // table's `row` extractor picks its keys explicitly.
        _src: [si, 'lithology_blocks', bi],
      });
    }
    for (let ui = 0; ui < ((sec && sec.age_units) || []).length; ui++) {
      const u = sec.age_units[ui];
      age_units.push({
        section_id: sid,
        label: u && u.label ? String(u.label) : '',
        top_idx: u ? u.range_top_idx : null,
        base_idx: u ? u.range_base_idx : null,
        _src: [si, 'age_units', ui],
      });
    }
    for (let mi = 0; mi < ((sec && sec.samples) || []).length; mi++) {
      const s = sec.samples[mi];
      samples.push({
        section_id: sid,
        bed_idx: s ? s.bed_idx : null,
        fossil_marker: s && s.fossil_marker ? String(s.fossil_marker) : '',
        ref: s && s.ref ? String(s.ref) : '',
        _src: [si, 'samples', mi],
      });
    }
  }
  return { lithology_blocks, age_units, samples };
}

// Resolve the row array for a table id. The three columnar sub-tables read
// from the flattened per-section arrays; every other table reads
// `data[tableId]` directly.
//
// M4 (REVIEW-2026-09-20): malformed rows are no longer dropped here.
// rca_core/exporter.py never filters them — `_table_items` hands every entry
// of the list to `_row_values`, which renders a non-dict row as a single
// padded cell (exporter.py:901-906). Silently dropping them made the
// browser's row count and CSV disagree with the GUI / XLSX export of the
// SAME payload (the quality report still flags the row as a `not_a_dict`
// invariant issue, so the user saw "3 rows" in the browser and "4 rows" in
// Excel). Tolerance is provided by rcaRowCellsFor (the `_row_values`
// mirror), which is what the renderer and the export path call instead of
// `cfg.row` directly — a single null can still never throw out of
// rcaRenderResults and blank the whole results panel.
function rcaRowsForTable(data, tableId) {
  if (tableId === 'lithology_blocks' || tableId === 'age_units'
      || tableId === 'samples') {
    return rcaColumnarSubTableRows(data)[tableId];
  }
  return Array.isArray(data && data[tableId]) ? data[tableId] : [];
}

// Python `isinstance(x, dict)` as close as JS gets: a non-null,
// non-array object. Used to decide whether a row may be dereferenced.
function rcaIsDict(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

// Mirror of rca_core/exporter.py:_row_values — the ONE cell builder behind
// both the rendered table and the CSV/TSV export, so a malformed row can
// never make the two transports disagree on the column count either.
// `cfg.row` is only handed non-null objects (plus every other_fossils row,
// whose extractor explicitly accepts strings); anything else becomes a
// single cell padded/truncated to the header width, exactly like Python's
// `vals = [item]` branch.
function rcaRowCellsFor(cfg, item) {
  let vals;
  if (rcaIsDict(item) || cfg.id === 'other_fossils') {
    vals = cfg.row(item);
  } else {
    vals = [item];
  }
  const nCols = (cfg.cols || []).length;
  const out = vals.slice(0, nCols);
  while (out.length < nCols) out.push('');
  return out;
}

// Mirror of rca_core/exporter.py:_export_cell_text (exporter.py:852) — the
// point where a cell value becomes TEXT, so it is also the LAST place a
// non-finite number can be caught. A producer that already str()-ed its
// value puts the literal "nan"/"inf" into the CSV/XLSX as something that
// looks like data, so those spellings are blanked here as well (Python's
// `_NONFINITE_TEXT`, exporter.py:849).
//
// Both consumers call it: the Python GUI's result grid renders through
// `build_table_export` (gui.py:2148), i.e. through `_export_cell_text`, so
// the browser panel and the CSV have to run the SAME step or the three
// surfaces disagree on a cell that reads NaN/Infinity.
const RCA_NONFINITE_TEXT = ['nan', 'inf', '-inf', '+inf', '-nan',
  'infinity', '-infinity'];

// FE-FIX-2026-09-21 (audit item 13): the GRAMMAR of Python's `str(float)`
// (exporter.py:867 `text = str(value)`). JS's `String()` and Python's
// `repr(float)` agree on the shortest round-trip DIGITS but disagree on when
// they switch to exponent form and on how the exponent is padded:
//   value          JS String()          Python str()
//   1e16           "10000000000000000"  "1e+16"
//   1e20           "100000000000000000000"  "1e+20"
//   1e-7           "1e-7"               "1e-07"
//   0.0001         "0.0001"             "0.0001"
//   2.0            "2"                  "2.0"
// so a browser CSV/TSV cell diverged from the GUI grid / workbook for the very
// same payload. Python switches to exponent form when the decimal point would
// sit at or below 1e-4 (decpt <= -4) or beyond 1e16 (decpt > 16), always keeps
// a ".0" on an integral float, and pads the exponent to two digits.
//
// LIMITATION (documented, not silently ignored): JSON.parse collapses Python's
// int/float distinction, so a payload float that happens to be integral
// (3.0) arrives as the JS number 3 and cannot be told from the int 3 any more;
// `Number.isInteger` therefore keeps the int spelling ("3", matching
// str(3)) and only non-integral values go through the float grammar.
function rcaPyFloatStr(value) {
  const num = Number(value);
  if (Number.isNaN(num)) return 'nan';
  if (num === Infinity) return 'inf';
  if (num === -Infinity) return '-inf';
  if (num === 0) return Object.is(num, -0) ? '-0.0' : '0.0';
  const sign = num < 0 ? '-' : '';
  // JS already gives the shortest round-trip digits Python's repr uses.
  const raw = String(Math.abs(num));
  let mantissa = raw;
  let exp10 = 0;
  const at = raw.indexOf('e');
  if (at !== -1) {
    mantissa = raw.slice(0, at);
    exp10 = parseInt(raw.slice(at + 1), 10) || 0;
  }
  const dot = mantissa.indexOf('.');
  let digits;
  let decpt;
  if (dot === -1) {
    digits = mantissa;
    decpt = mantissa.length + exp10;
  } else {
    digits = mantissa.slice(0, dot) + mantissa.slice(dot + 1);
    decpt = dot + exp10;
  }
  digits = digits.replace(/0+$/, '');
  if (!digits.length) digits = '0';
  let out;
  if (decpt <= -4 || decpt > 16) {
    const exp = decpt - 1;
    const pad = String(Math.abs(exp));
    out = digits.charAt(0) + (digits.length > 1 ? '.' + digits.slice(1) : '')
      + 'e' + (exp < 0 ? '-' : '+') + (pad.length < 2 ? '0' + pad : pad);
  } else if (decpt > 0) {
    const intPart = digits.length >= decpt
      ? digits.slice(0, decpt) : digits + '0'.repeat(decpt - digits.length);
    const fracPart = digits.length > decpt ? digits.slice(decpt) : '0';
    out = intPart + '.' + fracPart;
  } else {
    out = '0.' + '0'.repeat(-decpt) + digits;
  }
  return sign + out;
}

function rcaExportCellText(value) {
  if (value === null || value === undefined) return '';
  // Python's `str(True)` is 'True', not JS's 'true' — a boolean that reaches
  // a cell (an un-normalized model field) must read the same in the browser
  // panel, the browser CSV and the GUI grid / workbook.
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  // FE-FIX-2026-09-21 (audit item 13): floats render through the Python
  // grammar above; non-finite instances blank exactly like _export_cell_text.
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return '';
    if (Number.isInteger(value)) return String(value);
    return rcaPyFloatStr(value);
  }
  const s = String(value);
  return RCA_NONFINITE_TEXT.indexOf(s.trim().toLowerCase()) !== -1 ? '' : s;
}

// L2 (REVIEW-2026-09-20): the agreement band, computed in ONE place.
// Thresholds are the pill's integer tri-colour bands (see the cell renderer
// below): good k*3 >= 2n, mid k*3 > n, low k*3 <= n. Previously the row
// background used `ac <= runs/2` while the pill used `k*3 <= n`, so a 1/2 or
// 2/4 row showed a "mid" pill on a red "needs review" band — two
// contradictory review signals for the same number.
function rcaAgreementBand(k, n) {
  if (!Number.isFinite(k) || !Number.isFinite(n) || n <= 0) return 'low';
  if (k * 3 >= n * 2) return 'good';
  if (k * 3 > n) return 'mid';
  return 'low';
}

// Band of one row: `agreement_count` first (the merge writes it), then the
// "k/n" string (UI-REVIEW-2026-09-05 fallback for hand-edited / history
// rows). Returns null when the row cannot carry agreement at all (a
// malformed non-dict row) so the renderer skips the banding instead of
// reading properties off a string.
function rcaRowAgreementBand(item, runsRaw) {
  if (!rcaIsDict(item)) return null;
  const n = Number(runsRaw);
  if (!Number.isFinite(n) || n <= 0) return null;
  let k = Number(item.agreement_count);
  if (!Number.isFinite(k)) {
    const m = String(item.agreement || '').trim().match(/^(\d+)\s*\/\s*(\d+)$/);
    k = m ? Number(m[1]) : 0;
  }
  return rcaAgreementBand(k, n);
}

// Render the whole result. `data` is the normalized result object.
//
// FE-BORROW-2026-09-20 (域T) adds an OPTIONAL third argument,
// `opts = { editable: true }`. Without it the output is byte-identical to the
// old read-only markup — the Qt Fluent history dialog (gui_fluent_history_detail
// .py) reuses this renderer through QWebEngineView and must stay read-only, so
// editing is strictly opt-in and the two callers cannot drift.
function rcaRenderResults(data, rawText, opts) {
  const configs = rcaTableConfigs(data);
  const editOn = rcaEditFlag(opts);
  const parts = [];

  // Confidence ring + global actions toolbar.
  // The ring is an inline SVG: two stacked circles (track + bar) with the
  // bar's stroke-dasharray advancing toward `confPct`. The numeric label
  // inside ticks up from 0 → confPct on first paint.
  // UI-REVIEW-2026-09-05: columnar-section payloads carry the verdict in
  // `overall_confidence`, not `confidence` — without the fallback every
  // columnar extraction (web AND the Fluent history dialog, which reuses
  // this renderer via QWebEngineView) showed an empty 0% ring.
  const _confRaw = (data.confidence !== undefined && data.confidence !== null && data.confidence !== '')
    ? data.confidence : data.overall_confidence;
  const confPct = Math.max(0, Math.min(100, Math.round((_confRaw || 0) * 100)));
  let confLevel = 'low';
  if (confPct >= 70) confLevel = 'high';
  else if (confPct >= 40) confLevel = 'mid';
  const circ = 2 * Math.PI * 18;  // matches r=18 in the SVG below
  const dashLen = (circ * confPct) / 100;
  // data-conf lets tests assert the rendered percentage.
  parts.push('<div class="results-toolbar">');
  parts.push('<div class="rt-left">');
  parts.push('<span class="confidence-ring ' + confLevel + '" data-conf="' + confPct + '">');
  parts.push('<svg viewBox="0 0 44 44" aria-hidden="true">');
  parts.push('<circle class="track" cx="22" cy="22" r="18"></circle>');
  parts.push('<circle class="bar" cx="22" cy="22" r="18" '
    + 'stroke-dasharray="' + dashLen.toFixed(2) + ' ' + circ.toFixed(2) + '" '
    + 'stroke-dashoffset="0"></circle>');
  parts.push('</svg>');
  parts.push('<span class="num" data-target="' + confPct + '">0</span>');
  parts.push('</span>');
  parts.push('<span class="label">' + rcaEsc(t('results.confidence')) + '</span>');
  // UI-REVIEW-2026-08-01 (M3): the quality badge used to be a THIRD
  // sibling of rt-left / rt-actions, so `justify-content: space-between`
  // floated it alone in the middle of the toolbar (~440px away from the
  // confidence ring). Move it inside .rt-left so it sits next to the ring.
  const q = data.quality;
  if (q && typeof q.score === 'number') {
    const qpct = Math.round(q.score * 100);
    let qLevel = 'low';
    if (q.score >= 0.75) qLevel = 'high';
    else if (q.score >= 0.6) qLevel = 'mid';
    const issuesList = (q.issues || [])
      .map((iss) => {
        // REVIEW-2026-07-31: substitute {param} placeholders — issue
        // translations like "{count} section(s) span eras" rendered the
        // literal "{count}" before.
        let text = t(iss.msg_key || 'quality.invalid_result');
        if (iss.params && typeof iss.params === 'object') {
          for (const k of Object.keys(iss.params)) {
            text = text.split('{' + k + '}').join(String(iss.params[k]));
          }
        }
        return text;
      })
      .filter(Boolean)
      .join('; ');
    parts.push('<span class="quality-badge ' + qLevel + '" title="' + rcaEsc(issuesList) + '">');
    parts.push('<span class="qb-num">' + qpct + '%</span> ');
    parts.push('<span class="qb-grade">' + rcaEsc(q.grade || '-') + '</span>');
    parts.push('</span>');
  }
  // UI-REVIEW-2026-09-08 (evidence chain UI): show what the auto resolver
  // decided - which chart type and whether vision classification was used.
  // The label reuses the settings.chartType.* keys; unknown modes fall
  // back to the raw mode string.
  const rep = (data && typeof data.report === 'object') ? data.report : null;
  const repMode = rep && rep.mode ? rep.mode : null;
  const autoInfo = (data && data._auto_mode && typeof data._auto_mode === 'object')
    ? data._auto_mode : (repMode ? { mode: repMode.used, source: repMode.source } : null);
  if (autoInfo && autoInfo.mode && autoInfo.mode !== 'range_chart') {
    const CHART_MODE_LABEL_KEYS = {
      columnar_section: 'upload.chartMode.columnarSection',
      abundance_diagram: 'upload.chartMode.abundanceDiagram',
      phylogenetic_tree: 'upload.chartMode.phylogeneticTree',
      zonation_chart: 'upload.chartMode.zonationChart',
      chemical_stratigraphy: 'upload.chartMode.chemicalStratigraphy',
      paleomap: 'upload.chartMode.paleomap',
      scatter_plot: 'upload.chartMode.scatterPlot',
    };
    const modeKey = CHART_MODE_LABEL_KEYS[autoInfo.mode];
    const modeLabel = modeKey && t(modeKey).indexOf('[?') !== 0 ? t(modeKey) : String(autoInfo.mode);
    const via = autoInfo.source === 'vision'
      ? ' (' + rcaEsc(t('results.viaVision')) + ')' : '';
    parts.push('<span class="auto-mode-chip" '
      + 'title="' + rcaEsc(t('results.autoChipHint')) + '">'
      + rcaEsc(t('results.autoDetected')) + rcaEsc(modeLabel) + rcaEsc(via)
      + '</span>');
  }
  parts.push('</div>');
  parts.push('<div class="rt-actions">');
  // FE-BORROW-2026-09-20 (domain T integration): undo/redo live in the
  // toolbar so the operator can see the stack state without hunting the
  // keyboard. rcaHistoryAttachUi (js/history.js) enables/disables these
  // after every render; disabled here because a fresh result always has an
  // empty stack.
  if (editOn) {
    parts.push('<button type="button" class="btn btn-secondary btn-small" data-rca-undo="1" disabled '
      + 'title="' + rcaEscAttr(rcaEditT('edit.undo')) + '">'
      + rcaEsc(rcaEditT('edit.undo')) + '</button>');
    parts.push('<button type="button" class="btn btn-secondary btn-small" data-rca-redo="1" disabled '
      + 'title="' + rcaEscAttr(rcaEditT('edit.redo')) + '">'
      + rcaEsc(rcaEditT('edit.redo')) + '</button>');
  }
  parts.push('<button type="button" class="btn btn-secondary btn-small" id="btn-export-all">' + rcaEsc(t('results.exportAll')) + '</button>');
  parts.push('</div>');
  parts.push('</div>');
  // UI-REVIEW-2026-09-08: GBIF name-verification hints render here
  // (async, populated by app.js after extraction).
  parts.push('<div id="names-verify-slot"></div>');

  for (const cfg of configs) {
    // Sprint B (REVIEW-2026-09-04): read rows through rcaRowsForTable so
    // the three columnar sub-tables resolve the same flattened arrays the
    // export path uses (previously `data[cfg.id]` was always undefined for
    // them and every sub-table rendered empty with no copy/CSV buttons).
    const rows = rcaRowsForTable(data, cfg.id);
    const safeId = rcaEscAttr(cfg.id);
    // An empty table is still an editing target: the operator adds the first
    // row from here (Tabulator has the same "no data -> add row" affordance).
    if (editOn && rows.length === 0) {
      parts.push('<div class="result-section" data-table="' + safeId + '" data-editable="1">');
      parts.push('<div class="result-section-head">');
      parts.push('<h3>' + rcaEsc(t(cfg.titleKey)) + ' <span class="result-count">(0)</span></h3>');
      parts.push('</div>');
      parts.push('<div class="cell-empty" style="padding:8px 2px;">' + rcaEsc(t('results.noRows')) + '</div>');
      parts.push(rcaEditAddRowButton(cfg));
      parts.push('</div>');
      continue;
    }
    parts.push('<div class="result-section" data-table="' + safeId + '"' + (editOn ? ' data-editable="1"' : '') + '>');
    parts.push('<div class="result-section-head">');
    parts.push('<h3>' + rcaEsc(t(cfg.titleKey)) + ' <span class="result-count">(' + rows.length + ')</span></h3>');
    if (rows.length > 0) {
      parts.push('<div class="table-actions">');
      parts.push('<button type="button" class="btn btn-secondary btn-small" data-copy="' + safeId + '">' + rcaEsc(t('results.copyTsv')) + '</button>');
      parts.push('<button type="button" class="btn btn-secondary btn-small" data-csv="' + safeId + '">' + rcaEsc(t('results.downloadCsv')) + '</button>');
      parts.push('</div>');
    }
    parts.push('</div>');

    if (rows.length === 0) {
      // UI-REVIEW-2026-09-08: surface WHY a table is empty when the
      // evidence report says so (model note), as a hover tooltip.
      let emptyTitle = '';
      if (rep && Array.isArray(rep.empty_tables)) {
        const hit = rep.empty_tables.find((e) => e && e.key === cfg.id && e.reason);
        if (hit) emptyTitle = ' title="' + rcaEscAttr(hit.reason) + '"';
      }
      parts.push('<div class="cell-empty"' + emptyTitle + ' style="padding:8px 2px;">' + rcaEsc(t('results.noRows')) + '</div>');
      parts.push('</div>');
      continue;
    }

    // FIX-1: give each table an accessible name (section title + row count)
    // and scoped column headers so screen-reader table navigation announces
    // context correctly. Native <table> semantics already provide grid-style
    // cell navigation, so we keep the markup native rather than bolting a
    // role="grid" roving-tabindex widget onto read-only data.
    const tblLabel = rcaEscAttr(t(cfg.titleKey) + ' (' + rows.length + ')');
    // The scroll container is the J/K focus context (`tabindex="0"` only in
    // edit mode — the read-only renderer must not gain a tab stop).
    parts.push('<div class="table-wrap"'
      + (editOn ? ' tabindex="0" data-table-nav="' + safeId + '"' : '')
      + '><table class="data-table" aria-label="' + tblLabel + '"><thead><tr>');
    if (editOn) {
      parts.push('<th scope="col" class="rca-col-select"><input type="checkbox" class="rca-select-all"'
        + ' data-select-all="' + safeId + '"'
        + ' aria-label="' + rcaEscAttr(rcaEditT('edit.selectAll')) + '"></th>');
    }
    parts.push('<th scope="col">' + rcaEsc(t('col.index')) + '</th>');
    for (const c of cfg.cols) {
      parts.push('<th scope="col">' + rcaEsc(t(c)) + '</th>');
    }
    if (editOn) {
      parts.push('<th scope="col" class="rca-col-locate">'
        + rcaEsc(rcaEditT('edit.locateColumn')) + '</th>');
    }
    parts.push('</tr></thead><tbody>');
    rows.forEach((item, idx) => {
      // Flag low-agreement species rows (seen in a minority of runs) so the
      // operator knows to double-check them.
      //
      // L2 (REVIEW-2026-09-20): the band comes from rcaRowAgreementBand, the
      // SAME integer tri-colour rule the agreement pill uses below, so a row
      // can never show a red "needs review" background under a green/amber
      // pill. It also returns null for a malformed (non-dict) row, which is
      // what used to throw out of this loop.
      let rowCls = '';
      if ((cfg.id === 'species_ranges' || cfg.id === 'sections') && data && Number(data.runs) > 1) {
        if (rcaRowAgreementBand(item, data.runs) === 'low') {
          rowCls = 'row-low-agreement';
        }
      }
      // FE-BORROW-2026-09-20: the low-confidence marker is what J/K walks
      // over, so it has to be on the row already at render time (a CSS-only
      // highlight the keyboard can follow without re-querying the model).
      if (editOn && rcaTableIsLowConfidenceRow(data, cfg.id, item)) {
        rowCls = rowCls ? rowCls + ' rca-row-lowconf' : 'rca-row-lowconf';
      }
      parts.push('<tr' + (rowCls ? ' class="' + rowCls + '"' : '')
        + ' data-row="' + idx + '">');
      if (editOn) {
        parts.push('<td class="rca-cell-select"><input type="checkbox" class="rca-row-select"'
          + ' data-row-select="' + safeId + '" data-row="' + idx + '"'
          + ' aria-label="' + rcaEscAttr(rcaEditT('edit.selectRow') + ' ' + (idx + 1)) + '"></td>');
      }
      parts.push('<th class="cell-empty" scope="row">' + (idx + 1) + '</th>');
      // M4 (REVIEW-2026-09-20): rcaRowCellsFor is the `_row_values` mirror —
      // the one cell builder behind BOTH this render loop and
      // rcaBuildTableExport, so a null / primitive row can never blank the
      // results panel here nor make the browser's table and its CSV
      // disagree on the column count.
      const cells = rcaRowCellsFor(cfg, item);
      cells.forEach((cell, ci) => {
        const val = rcaExportCellText(cell);
        const colKey = cfg.cols[ci];
        // Phase C: agreement cell -> colored pill (good/mid/low).
        if (colKey === 'col.agreement' && val.trim()) {
          const m = val.trim().match(/^(\d+)\s*\/\s*(\d+)$/);
          // L2: rcaAgreementBand is the single threshold source (a missing
          // or unparsable "k/n" text stays the conservative low band, as
          // before).
          let pillClass = 'pill-low';
          if (m) {
            const k = parseInt(m[1], 10);
            const n = parseInt(m[2], 10);
            pillClass = 'pill-' + rcaAgreementBand(k, n);
          }
          parts.push('<td><span class="pill ' + pillClass + '">' + rcaEsc(val) + '</span></td>');
          return;
        }
        // Phase C: monospace numeric cells for range base/top.
        let cls = ci === cfg.italicCol ? 'cell-species' : (val.trim() ? '' : 'cell-empty');
        if (colKey === 'col.rangeBase' || colKey === 'col.rangeTop') {
          cls = val.trim() ? 'cell-num' : 'cell-empty';
        }
        // UI fix (2026-08-07): every td defaults to nowrap + ellipsis
        // (max-width 280px), but no title attribute was emitted, so the
        // truncated tail was the ONLY thing the user could see — the full
        // value was unreachable. Attach a title attribute on every non-empty
        // cell so hover shows the complete text. Also wrap the single-column
        // other_fossils table (long free-text entries read better wrapped
        // than truncated).
        if (cfg.id === 'other_fossils' && val.trim()) {
          cls += ' cell-wrapping';
        }
        const titleAttr = val.trim() ? ' title="' + rcaEscAttr(val) + '"' : '';
        const disp = val.trim() ? rcaEsc(val) : '-';
        // FE-BORROW-2026-09-20: the editable cell. `contenteditable` + the
        // data-* descriptors let js/table.js's delegated handlers resolve the
        // target column WITHOUT re-deriving it from the model (a re-render
        // mid-edit must not misattribute the value). The dirty marker comes
        // from the edits registry, so an edit survives a re-render (language
        // switch, viz refresh) instead of being painted over.
        const spec = editOn ? rcaColEditSpec(cfg, ci) : null;
        if (spec && spec.editable) {
          const dirty = rcaTableEdits.isCellEdited(cfg.id, idx, rcaEditFieldOf(spec, item));
          // FE-FIX-2026-09-21 (audit items 3 + 14): the editable branch used
          // to drop the title attribute (no hover tooltip once a cell became
          // editable) and painted the '-' placeholder INSIDE the
          // contenteditable, so a focus-out with no typing committed the
          // literal "-". Now: the cell content is empty for a blank value and
          // css/table-edit.css draws the placeholder via ::before on
          // .cell-empty; the empty class is forced on so the placeholder shows
          // even in columns whose cls never carries it (the italic species
          // column). titleAttr is included like the read-only path.
          let edCls = cls;
          if (!val.trim() && edCls.indexOf('cell-empty') === -1) {
            edCls += (edCls ? ' ' : '') + 'cell-empty';
          }
          parts.push('<td class="' + edCls + ' rca-edit-cell' + (dirty ? ' rca-cell-dirty' : '') + '"'
            + ' contenteditable="true" spellcheck="false"'
            + rcaEditDataAttrs(cfg, idx, ci, spec) + titleAttr + '>'
            + (val.trim() ? rcaEsc(val) : '') + '</td>');
          return;
        }
        parts.push('<td class="' + cls + '"' + titleAttr + '>' + disp + '</td>');
      });
      if (editOn) {
        // Row-tail 定位 button: hover / focus drive window.rcaViz (guarded),
        // and it is the anchor the viz's own row highlight scrolls to.
        parts.push('<td class="rca-cell-locate">'
          + '<button type="button" class="btn btn-secondary btn-small rca-locate-btn"'
          + ' data-rca-locate="' + safeId + ':' + idx + '"'
          + ' title="' + rcaEscAttr(rcaEditT('edit.locateHint')) + '">'
          + rcaEsc(rcaEditT('edit.locate')) + '</button></td>');
      }
      parts.push('</tr>');
    });
    parts.push('</tbody></table></div>');
    if (editOn) {
      parts.push(rcaEditAddRowButton(cfg));
      parts.push(rcaEditSelectionBar(cfg));
    }
    parts.push('</div>');
  }

  // Raw response (collapsible) for debugging.
  if (rawText) {
    parts.push('<details style="margin-top:20px;"><summary style="cursor:pointer;color:var(--text-muted);font-size:12.5px;">' + rcaEsc(t('results.rawToggle')) + '</summary>');
    parts.push('<pre style="font-family:var(--font-mono);font-size:11.5px;white-space:pre-wrap;word-break:break-word;background:var(--bg-lighter);padding:12px;border-radius:var(--radius);margin-top:8px;max-height:320px;overflow:auto;">' + rcaEsc(rawText) + '</pre>');
    parts.push('</details>');
  }

  return parts.join('');
}

// Build { headers, rows } for a table id, using current-language column labels.
//
// PARITY (M4, REVIEW-2026-09-20): this is the mirror of
// rca_core/exporter.py:_export_grid — same items (_table_items →
// rcaRowsForTable), same cell builder (_row_values → rcaRowCellsFor, which
// renders a non-dict row as one padded cell instead of raising or dropping
// it), same text step (_export_cell_text → rcaExportCellText). The row COUNT
// therefore matches the GUI / workbook export for a payload that contains
// null or primitive entries.
function rcaBuildTableExport(data, tableId) {
  const cfg = rcaTableConfigs(data).find((c) => c.id === tableId);
  if (!cfg) return { headers: [], rows: [] };
  const headers = [t('col.index')].concat(cfg.cols.map((c) => t(c)));
  // Sprint B (REVIEW-2026-09-04): same row source as the renderer
  // (rcaRowsForTable). The old stash-based lookup
  // (data._lithology_blocks_rows / ...) only worked when rcaTableConfigs
  // had mutated `data` beforehand and leaked internal keys into JSON export.
  const items = rcaRowsForTable(data, tableId);
  const rows = items.map((item, idx) => [String(idx + 1)].concat(
    rcaRowCellsFor(cfg, item).map(rcaExportCellText)
  ));
  return { headers, rows };
}


// UI-Polish Phase 5 `rcaRenderViz` (a no-op that only cleared #viz-host) was
// DEAD CODE and is gone (REVIEW-2026-09-20): nothing ever called it, so the
// "reserved for a future Gantt" hook only created the impression that the
// mount point was wired. #viz-host itself still exists in index.html and is
// still preserved across renders by app.js (renderCurrentResult /
// resetUpload) — see the report: keeping a hidden, never-populated div in the
// document is the leftover, not this function.

// UI-REVIEW-2026-09-08 (gnfinder/GBIF borrow, UI wiring): render the
// scientific-name check results as an info block under the results
// toolbar. Called by app.js after GBIF verification completes; absent
// payload renders nothing. Never throws.
function rcaRenderNameIssues(issues) {
  const host = document.getElementById('names-verify-slot');
  if (!host) return;
  // Clear previous hints. innerHTML='' is unreliable across DOM stubs,
  // so remove children explicitly (works in browsers and the test DOM).
  while (host.firstChild) host.removeChild(host.firstChild);
  if (!Array.isArray(issues) || issues.length === 0) return;
  const box = document.createElement('div');
  box.className = 'names-verify';
  for (const iss of issues) {
    const row = document.createElement('div');
    row.className = 'names-issue';
    const icon = document.createElement('span');
    icon.textContent = iss.msg_key === 'names.fuzzy' ? '\u{1F50D}' : '\u2139\uFE0F';
    const label = document.createElement('span');
    if (iss.msg_key === 'names.fuzzy') {
      label.textContent = t('names.fuzzy')
        .split('{name}').join(iss.name || '')
        .split('{suggestion}').join(iss.suggestion || '');
    } else {
      label.textContent = t('names.unmatched')
        .split('{name}').join(iss.name || '');
    }
    row.appendChild(icon);
    row.appendChild(label);
    box.appendChild(row);
  }
  host.appendChild(box);
}
if (typeof globalThis !== 'undefined') globalThis.rcaRenderNameIssues = rcaRenderNameIssues;

// ===========================================================================
// FE-BORROW-2026-09-20 (域T): IN-BROWSER TABLE EDITING
// ===========================================================================
//
// Borrowed patterns (no network needed, the shape is what matters):
//   * Tabulator Edit.js — `editTriggerEvent: "focus"`, a dirty-cell registry
//     (`setCellEdited` / `getEditedCells` / `clearCellEdited`),
//     `navigateUp/Down/Left/Right`, and `focus({preventScroll:true})` with
//     self-managed scrolling. Crucially: a cell whose validation failed KEEPS
//     focus (red frame, "lock focus") instead of silently reverting.
//   * editable-history's `undoManager` + Tabulator History's
//     `{type, ...} -> undoers[type]` dispatch table — implemented in
//     js/history.js, which only ever sees the three actions defined below
//     (cellEdit / rowDelete / rowAdd).
//
// The DIRTY MODEL is a deliberate mirror of rca_core/editable.py, which is the
// only edit engine the Qt side has. Every rule below cites the Python line it
// copies, and tests_edit_history.js replays a shared case table through both
// engines so they cannot drift silently.
//
// Editing is opt-in: `rcaRenderResults(data, raw, {editable:true})` plus
// `rcaTableEditAttach(root, data)`. The Qt Fluent history dialog keeps calling
// the two-argument form and therefore keeps rendering read-only markup.

// ---- i18n: t() with a built-in bilingual fallback -----------------------
//
// js/i18n.js has no `edit.*` cell-editor keys yet (domain U owns that file),
// and `t()` answers a missing key with "[?edit.addRow]" — a string that would
// reach the user. So every editor string goes through rcaEditT(): use the real
// key when i18n has it, otherwise this local temporary zh/en table. The
// key list is exported as RCA_EDIT_I18N_KEYS so the i18n owner can lift it and
// so tests_edit_history.js fails the moment a key lands in js/i18n.js only in
// one locale.
const RCA_EDIT_STRINGS = {
  'edit.addRow': { zh: '新增行', en: 'Add row' },
  'edit.addRowHint': { zh: '在表尾追加一行空白记录（可撤销）',
    en: 'Append a blank row at the end of the table (undoable)' },
  'edit.exportSelected': { zh: '导出所选', en: 'Export selected' },
  'edit.deleteSelected': { zh: '删除所选', en: 'Delete selected' },
  'edit.clearSelection': { zh: '清除选择', en: 'Clear selection' },
  'edit.locate': { zh: '定位', en: 'Locate' },
  'edit.locateColumn': { zh: '操作', en: 'Actions' },
  'edit.locateHint': { zh: '在图表中定位该行', en: 'Locate this row in the chart' },
  'edit.selectAll': { zh: '选择全部行', en: 'Select all rows' },
  'edit.selectRow': { zh: '选择行', en: 'Select row' },
  'edit.selectedCount': { zh: '已选 {n} 行', en: '{n} row(s) selected' },
  'edit.cellNotNumber': { zh: '此列需要数值：{value}', en: 'A number is required: {value}' },
  'edit.cellNotYesNo': { zh: '此列只接受 Y / N：{value}', en: 'Only Y / N accepted: {value}' },
  'edit.badStage': { zh: '不是 ICS 阶名：{value}', en: 'Not an ICS stage name: {value}' },
  'edit.rangeInverted': { zh: '延限倒置：顶 {top} 不得老于底 {base}',
    en: 'Range inverted: top {top} is older than base {base}' },
  'edit.ageInverted': { zh: '年龄倒置：顶 {top} Ma 应小于等于底 {base} Ma',
    en: 'Age inverted: top {top} Ma must not exceed base {base} Ma' },
  'edit.idxInverted': { zh: '层号倒置：顶 {top} 应大于等于底 {base}',
    en: 'Bed order inverted: top {top} must be >= base {base}' },
  'edit.undo': { zh: '撤销', en: 'Undo' },
  'edit.redo': { zh: '重做', en: 'Redo' },
  'edit.editsPending': { zh: '{n} 处未保存的修改', en: '{n} unsaved edit(s)' },
  'edit.rowsDeleted': { zh: '已删除 {n} 行', en: 'Deleted {n} row(s)' },
  'edit.noViz': { zh: '暂无联动图表可定位', en: 'No linked chart to locate in' },
};
const RCA_EDIT_I18N_KEYS = Object.keys(RCA_EDIT_STRINGS);

function rcaEditLang() {
  // RCA_LANG is a top-level `let` in js/i18n.js; a bare `typeof` guard keeps
  // this file loadable on its own (tests_edit_history.js loads table.js
  // without the whole app).
  return (typeof RCA_LANG !== 'undefined' && RCA_LANG) ? String(RCA_LANG) : 'zh';
}

function rcaEditT(key, params) {
  let s = null;
  if (typeof t === 'function') {
    const candidate = t(key);
    // t() answers an unknown key with "[?key]" (and warns on the console).
    if (candidate && candidate.indexOf('[?') !== 0) s = candidate;
  }
  if (s === null) {
    const entry = RCA_EDIT_STRINGS[key];
    if (!entry) return key;
    s = entry[rcaEditLang()] || entry.en || entry.zh;
  }
  if (params && typeof params === 'object') {
    for (const k of Object.keys(params)) {
      s = s.split('{' + k + '}').join(String(params[k]));
    }
  }
  return s;
}

// ---- edit mode flag ------------------------------------------------------
// opts = { editable: true } (or any object without `editable: false`).
// The two-argument call stays read-only so the Qt reuse cannot gain editors.
function rcaEditFlag(opts) {
  return !!(opts && opts.editable !== false);
}

// ---- Python-mirror primitives -------------------------------------------

// copy.deepcopy for JSON-shaped model data. Written by hand rather than via
// JSON round-trip because JSON.stringify DROPS `undefined` valued keys, and
// "key present with undefined" is exactly the null-vs-missing distinction
// editable.py's `_deleted_keys` branch turns on.
function rcaClone(value) {
  if (Array.isArray(value)) return value.map(rcaClone);
  if (value && typeof value === 'object') {
    const out = {};
    for (const k of Object.keys(value)) out[k] = rcaClone(value[k]);
    return out;
  }
  return value;
}

// editable.py:_coerce — "trim whitespace; pass through everything else".
// Non-strings (ints, lists, None) come back untouched.
function rcaCoerceVal(value) {
  return typeof value === 'string' ? value.trim() : value;
}

// Python `str(x)` as far as a model value goes: None->'None' is never needed
// here (callers test for null first), booleans follow Python's capitalised
// spelling, numbers use the shortest round-trip form.
// FE-FIX-2026-09-21 (audit item 10): renamed from the global `rcaPyStr`.
// js/minimax.js declares a DIFFERENT-semantics `rcaPyStr` and loads before
// table.js (index.html:357 vs :367), so this table.js copy silently clobbered
// minimax's own helper at runtime. A table.js-private name ends the war.
function rcaEditPyStr(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  return String(value);
}

// Python `a != b` for model values. The JS `!==` is NOT equivalent: it splits
// 1 from "1" the same way Python does, but it also splits True from 1 and
// None from undefined, both of which Python calls equal. Array / object
// compare deep (Python list/dict equality), which is what makes a nested
// columnar edit (sections[i].lithology_blocks) visible to the diff at all.
function rcaPyNotEqual(a, b) {
  return rcaPyEqual(a, b) === false;
}

function rcaPyEqual(a, b) {
  const an = (a === undefined) ? null : a;
  const bn = (b === undefined) ? null : b;
  if (an === null || bn === null) return an === bn;
  if (Array.isArray(an) || Array.isArray(bn)) {
    if (!Array.isArray(an) || !Array.isArray(bn)) return false;
    if (an.length !== bn.length) return false;
    for (let i = 0; i < an.length; i += 1) {
      if (!rcaPyEqual(an[i], bn[i])) return false;
    }
    return true;
  }
  if (rcaIsDict(an) || rcaIsDict(bn)) {
    if (!rcaIsDict(an) || !rcaIsDict(bn)) return false;
    const ak = Object.keys(an).filter((k) => an[k] !== undefined);
    const bk = Object.keys(bn).filter((k) => bn[k] !== undefined);
    if (ak.length !== bk.length) return false;
    for (const k of ak) {
      if (!(k in bn)) return false;
      if (!rcaPyEqual(an[k], bn[k])) return false;
    }
    return true;
  }
  if (typeof an === 'boolean' || typeof bn === 'boolean') {
    // Python: True == 1, False == 0, but True != "True" and True != 0.5.
    const anNum = typeof an === 'boolean' ? (an ? 1 : 0) : an;
    const bnNum = typeof bn === 'boolean' ? (bn ? 1 : 0) : bn;
    return typeof anNum === 'number' && typeof bnNum === 'number' && anNum === bnNum;
  }
  if (typeof an !== typeof bn) return false;
  if (typeof an === 'number') {
    // NaN != NaN on both engines.
    if (Number.isNaN(an) || Number.isNaN(bn)) return false;
    return an === bn;
  }
  return an === bn;
}

// rca_core/exporter.py:_coerce_cell — the ONE text -> model-value rule the Qt
// Apply-edits path uses. Mirroring it here is what makes a browser edit and a
// GUI edit of the same cell produce the same model value.
// NOTE: exporter.py deliberately KEEPS unparseable numeric text ("5m") rather
// than dropping the field; the editor additionally *warns* about it before
// anything is written (rcaValidateCell), which is stricter at input time but
// stores exactly the same value once a value does arrive from a payload.
// Python's int()/float() accept a sign, a decimal point, an exponent and the
// PEP-515 underscore separators (int("1_000") == 1000), and reject everything
// else — including JS's beloved hex ("0x10") and Infinity. Same grammar here,
// or the two engines disagree about which cells are numbers.
// FE-FIX-2026-09-21 (audit item 12): the old test STRIPPED underscores before
// matching, so "1__0", "_1" and "1_" all parsed — Python rejects every one of
// them (int("_1") and int("1__0") both raise ValueError). An underscore is
// legal only BETWEEN two digits. The regex now runs on the raw text, and it
// matches ASCII digits only: JS \d under Unicode mode happily admits
// Arabic-Indic digits (٣), which Python's int() rejects unless the locale
// says otherwise — and CPython's int() does not.
const RCA_PY_DIGITS = '[0-9](?:_?[0-9])*';
const RCA_NUMERIC_RE = new RegExp(
  '^[+-]?(?:' + RCA_PY_DIGITS + '(?:\\.(?:' + RCA_PY_DIGITS + ')?)?'
  + '|\\.(?:' + RCA_PY_DIGITS + '))(?:[eE][+-]?' + RCA_PY_DIGITS + ')?$');

function rcaPyParseNumber(text) {
  const cleaned = String(text).trim();
  if (!RCA_NUMERIC_RE.test(cleaned)) return null;
  const num = Number(cleaned.replace(/_/g, ''));
  // Deliberate deviation, flagged: Python float("inf") succeeds and would
  // store an infinity in the model; a non-finite cell value is worthless
  // downstream (rcaExportCellText blanks it), so it counts as unparsable.
  if (!Number.isFinite(num)) return null;
  return num;
}

function rcaCoerceCellValue(rawText, type) {
  const tname = type || 'str';
  let value = (rawText === null || rawText === undefined) ? '' : rawText;
  const s = typeof value === 'string' ? value : rcaEditPyStr(value);
  if (tname === 'list') {
    return (s || '').split(';').map((x) => x.trim()).filter((x) => x.length > 0);
  }
  if (tname === 'int' || tname === 'float' || tname === 'number') {
    const txt = (s || '').trim();
    if (!txt) return null;                       // an empty cell is "not filled"
    const num = rcaPyParseNumber(txt);
    if (num === null) return txt;                // keep what the operator typed
    if (tname === 'float' || tname === 'number') return num;
    // 'int': Python int("3.0") raises, then the fall-back branch does
    // int(float(s)) — i.e. truncation toward zero.
    return Math.trunc(num);
  }
  if (tname === 'bool_yn') {
    const v = (s || '').trim().toLowerCase();
    return v === 'y' || v === 'yes' || v === 'true' || v === '1' || v === 't';
  }
  if (tname === 'nullable_str') {
    const v = (s || '').trim();
    return v || null;
  }
  return s || '';
}

// "This value carries no information" — the point where a cleared cell becomes
// a KEY REMOVAL (so the diff records it under `_deleted_keys`, which is what
// normalize_result does to an emptied field and what apply_edits replays).
function rcaIsEmptyModelValue(value) {
  if (value === null || value === undefined) return true;
  if (value === '') return true;
  if (Array.isArray(value) && value.length === 0) return true;
  return false;
}

// ---- the list keys the diff walks (editable.py:_LIST_KEYS) --------------
const RCA_EDIT_LIST_KEYS = [
  'sections', 'species_ranges', 'biozones',
  'fossil_legend', 'lithology_legend', 'cross_beds',
  'other_fossils',
  'sites', 'abundances', 'zones',
  'nodes', 'zonations', 'correlations',
  'lithology_blocks', 'age_units', 'samples',
];
// editable.py:_SCALAR_LIST_KEYS — rows that are plain strings, not dicts.
const RCA_EDIT_SCALAR_LIST_KEYS = ['other_fossils'];
// editable.py:_DELETED_KEYS
const RCA_EDIT_DELETED_KEYS = '_deleted_keys';
const RCA_EDIT_EXTRAS_KEY = '_extras';

// capture_edits() for ONE list. See editable.py:133-212 for the source rules;
// each branch below cites them.
function rcaCaptureListEdits(beforeList, afterList) {
  const b = Array.isArray(beforeList) ? beforeList : [];
  const a = Array.isArray(afterList) ? afterList : [];
  // editable.py:147-154 — a deletion cannot be represented index-wise, so the
  // whole AFTER list replaces the list.
  if (a.length < b.length) return { _replaced: rcaClone(a) };
  const edits = {};
  const n = Math.min(b.length, a.length);
  let scalarChanged = false;
  for (let i = 0; i < n; i += 1) {
    const bDict = rcaIsDict(b[i]);
    const aDict = rcaIsDict(a[i]);
    // editable.py:166-170 — dict vs scalar row: no per-cell merge possible.
    if (bDict !== aDict) { scalarChanged = true; continue; }
    if (!aDict) {
      // editable.py:171-175 — both rows are scalars, compare the values.
      if (rcaPyNotEqual(rcaCoerceVal(a[i]), rcaCoerceVal(b[i]))) scalarChanged = true;
      continue;
    }
    const bi = b[i] || {};
    const ai = a[i] || {};
    const cellEdits = {};
    // editable.py:186 — the UNION of both rows' keys, before-keys first.
    const cols = Object.keys(bi);
    for (const k of Object.keys(ai)) {
      if (cols.indexOf(k) === -1) cols.push(k);
    }
    for (const col of cols) {
      if (col === RCA_EDIT_EXTRAS_KEY) continue;   // editable.py:187
      const inA = Object.prototype.hasOwnProperty.call(ai, col);
      const inB = Object.prototype.hasOwnProperty.call(bi, col);
      if (inA && inB) {
        if (rcaPyNotEqual(rcaCoerceVal(ai[col]), rcaCoerceVal(bi[col]))) {
          cellEdits[col] = ai[col];
        }
      } else if (inA) {
        // editable.py:192-196 — new key: compare against None, so an added
        // `col: ""` IS an edit ('' != None in Python).
        if (rcaPyNotEqual(rcaCoerceVal(ai[col]), rcaCoerceVal(null))) {
          cellEdits[col] = ai[col];
        }
      } else {
        // editable.py:197-199 — present before, gone after: a deletion.
        if (!Array.isArray(cellEdits[RCA_EDIT_DELETED_KEYS])) {
          cellEdits[RCA_EDIT_DELETED_KEYS] = [];
        }
        cellEdits[RCA_EDIT_DELETED_KEYS].push(col);
      }
    }
    if (Object.keys(cellEdits).length > 0) edits[i] = cellEdits;
  }
  // editable.py:202-204 — a scalar row change replaces the list (and wins on
  // replay, because apply_edits tests `_replaced` first).
  if (scalarChanged) edits._replaced = rcaClone(a);
  // editable.py:205-209 — appended rows ride as `new_<index>`; only dicts,
  // and by reference (the deep copy happens on apply).
  for (let i = n; i < a.length; i += 1) {
    if (rcaIsDict(a[i])) edits['new_' + i] = a[i];
  }
  return edits;
}

// editable.py:capture_edits for a whole result dict.
function rcaCaptureEdits(before, after) {
  if (!rcaIsDict(before) || !rcaIsDict(after)) return {};
  const out = {};
  for (const key of RCA_EDIT_LIST_KEYS) {
    const b = before[key] || [];
    const a = after[key] || [];
    if (!Array.isArray(b) || !Array.isArray(a)) continue;
    const edits = rcaCaptureListEdits(b, a);
    if (Object.keys(edits).length > 0) out[key] = edits;
  }
  return out;
}

// Python `int(x)` on a table index: only an optionally signed run of digits.
function rcaPyInt(text) {
  const s = String(text).trim();
  if (!/^[+-]?\d+$/.test(s)) return null;
  const n = parseInt(s, 10);
  return Number.isFinite(n) ? n : null;
}

// editable.py:apply_edits — replays a payload onto a result dict IN PLACE.
function rcaApplyEdits(result, edits) {
  if (!rcaIsDict(result) || !rcaIsDict(edits)) return result;
  for (const key of Object.keys(edits)) {
    if (RCA_EDIT_LIST_KEYS.indexOf(key) === -1) continue;
    const items = result[key];
    if (!Array.isArray(items)) continue;
    const rowEdits = edits[key];
    if (!rcaIsDict(rowEdits)) continue;
    if (Object.prototype.hasOwnProperty.call(rowEdits, '_replaced')) {
      result[key] = rcaClone(rowEdits._replaced);
      continue;
    }
    const modifications = [];
    const insertions = [];
    for (const idx of Object.keys(rowEdits)) {
      const cellEdits = rowEdits[idx];
      if (idx.indexOf('new_') === 0) {
        const insertAt = rcaPyInt(idx.slice(4));
        if (insertAt === null) continue;
        if (rcaIsDict(cellEdits)) insertions.push([insertAt, rcaClone(cellEdits)]);
        continue;
      }
      const i = rcaPyInt(idx);
      if (i === null) continue;
      if (rcaIsDict(cellEdits)) modifications.push([i, cellEdits]);
    }
    // editable.py:265-290 — modifications first (in-place, no reindexing).
    for (const [i, cellEdits] of modifications) {
      if (i < 0 || i >= items.length) continue;
      let item = items[i];
      if (!rcaIsDict(item)) {
        // editable.py:271-275 — promote a scalar row so it can hold fields.
        items[i] = { value: item };
        item = items[i];
      }
      for (const col of Object.keys(cellEdits)) {
        if (col === RCA_EDIT_EXTRAS_KEY) continue;
        const val = cellEdits[col];
        if (col === RCA_EDIT_DELETED_KEYS) {
          if (Array.isArray(val)) {
            for (const drop of val) {
              if (typeof drop === 'string') delete item[drop];
            }
          }
          continue;
        }
        item[col] = rcaClone(val);
      }
    }
    // editable.py:291-299 — insertions in ASCENDING index order, replayed
    // with Python list.insert() semantics.
    // FE-FIX-2026-09-21 (audit item 17): the old clamp `max(0, min(len, i))`
    // folded a NEGATIVE index to 0, but editable.py calls bare
    // `items.insert(insert_at, payload)` (editable.py:299), and CPython
    // normalises negatives as len + i (floored at 0) while large positive
    // indices simply append. A `new_-1` payload therefore landed at the front
    // in the browser and before the last item in the GUI — now mirrored.
    insertions.sort((x, y) => x[0] - y[0]);
    for (const [insertAt, payload] of insertions) {
      let at = insertAt;
      if (at < 0) at = Math.max(0, items.length + at);
      else at = Math.min(at, items.length);
      items.splice(at, 0, payload);
    }
  }
  return result;
}

// editable.py:is_dirty
function rcaIsDirtyEdits(edits) {
  return Object.keys(edits || {}).length > 0;
}

// editable.py:_default_for / new_row_template, driven off the SAME column
// metadata the editor renders from (cfg.edit) instead of a second hand-maintained
// template table. `agreement` is skipped exactly like _template_from_cfg does,
// and a scalar list key yields "" exactly like new_row_template does.
function rcaNewRowTemplate(cfg, tableId) {
  if (RCA_EDIT_SCALAR_LIST_KEYS.indexOf(tableId) !== -1) return '';
  if (!cfg || !Array.isArray(cfg.edit)) return {};
  const out = {};
  for (const spec of cfg.edit) {
    if (!spec || !spec.field) continue;
    if (spec.field === 'agreement') continue;
    const model = spec.model || spec.field;
    if (spec.type === 'list') out[model] = [];
    else if (spec.type === 'int' || spec.type === 'float' || spec.type === 'number'
      || spec.type === 'bool_yn' || spec.type === 'nullable_str') out[model] = null;
    else out[model] = '';
  }
  return out;
}

// ---- validators ----------------------------------------------------------

// quality.js:_parseBedN (private there) — "the first integer in the cell".
// Duplicated rather than reached into quality.js so table.js has no load-order
// dependency on the scorer.
function rcaParseBedN(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'boolean') return null;
  if (typeof value === 'number') return Number.isNaN(value) ? null : Math.trunc(value);
  const s = String(value).trim();
  if (!s) return null;
  const m = s.match(/-?\d+/);
  return m ? parseInt(m[0], 10) : null;
}

// quality.js:_looksLikeAge — a bed number and a Ma age must not be compared to
// each other, which is why the range pair has two branches.
const RCA_AGE_UNIT_RE = /(?:\bMa\b|\bMyr\b|\bMya\b|百万年|年前)/i;
function rcaLooksLikeAgeText(value) {
  if (value === null || value === undefined) return false;
  if (typeof value === 'number') return false;
  return RCA_AGE_UNIT_RE.test(String(value));
}

// quality.js:_INFORMAL_STAGE_RE — "unnumbered stage I" & co. are legal values
// the ICS table cannot answer for.
const RCA_INFORMAL_STAGE_RE = /^\s*(?:unnumbered|unnamed|stage)\s+[\dxvi]+\s*$/i;

// Resolve a stage / period label against the bundled ICS tables. Reuses what
// js/ics_table.js already ships instead of growing a second gazetteer:
// RCA_ICS_TABLE (stage -> ma bounds), RCA_ICS_PERIOD_NAMES +
// RCA_ICS_CN_PERIOD_NAMES (alias -> canonical period).
function rcaIcsLookupStage(name) {
  const raw = String(name === null || name === undefined ? '' : name).trim();
  if (!raw) return null;
  const lower = raw.toLowerCase();
  const table = (typeof globalThis !== 'undefined' && globalThis.RCA_ICS_TABLE)
    ? globalThis.RCA_ICS_TABLE : null;
  if (table) {
    if (Object.prototype.hasOwnProperty.call(table, raw)) return raw;
    for (const k of Object.keys(table)) {
      if (k.toLowerCase() === lower) return k;
    }
  }
  const en = (typeof globalThis !== 'undefined' && globalThis.RCA_ICS_PERIOD_NAMES)
    ? globalThis.RCA_ICS_PERIOD_NAMES : null;
  if (en && Object.prototype.hasOwnProperty.call(en, lower)) return en[lower];
  const cn = (typeof globalThis !== 'undefined' && globalThis.RCA_ICS_CN_PERIOD_NAMES)
    ? globalThis.RCA_ICS_CN_PERIOD_NAMES : null;
  if (cn && Object.prototype.hasOwnProperty.call(cn, raw)) return cn[raw];
  return null;
}

// rca_core/quality.py's bed branch: top < base is the inversion (younger end
// numerically BELOW the older end).
function rcaBedPairInverted(topVal, baseVal) {
  const top = rcaParseBedN(topVal);
  const base = rcaParseBedN(baseVal);
  if (top === null || base === null) return null;
  return top < base;
}

// The species range pair: quality.js tries the bed numbers first and only
// falls back to Ma ages when either side reads like an age.
function rcaRangePairInverted(topVal, baseVal) {
  const top = rcaParseBedN(topVal);
  const base = rcaParseBedN(baseVal);
  const agey = rcaLooksLikeAgeText(topVal) || rcaLooksLikeAgeText(baseVal);
  if (top !== null && base !== null && !agey) return top < base;
  const topMa = rcaAgeTextToMa(topVal);
  const baseMa = rcaAgeTextToMa(baseVal);
  if (topMa === null || baseMa === null) return null;   // not comparable
  return baseMa < topMa;                                // older below younger
}

function rcaAgeTextToMa(value) {
  if (value === null || value === undefined) return null;
  const num = rcaPyParseNumber(String(value).replace(/\s*(?:Ma|Myr|Mya|百万年|年前)\s*$/i, ''));
  return num;
}

// zones.top_age / base_age: both are Ma numbers, top (younger) must not exceed
// base (older).
function rcaAgePairInverted(topVal, baseVal) {
  const top = rcaAgeTextToMa(topVal);
  const base = rcaAgeTextToMa(baseVal);
  if (top === null || base === null) return null;
  return top > base;
}

// Validate one edited cell BEFORE it is written. Pure: `ref` is the plain
// descriptor ({type, validate, peer, field}), `text` the raw cell content and
// `row` the live model row (used for the pair checks' peer value).
// Returns {ok:true} or {ok:false, key, params, text} — `key` is the i18n key
// so the caller can re-render the message in another language.
function rcaValidateCell(ref, text, row) {
  const spec = ref || {};
  const s = (text === null || text === undefined) ? '' : String(text).trim();
  const type = spec.type || 'str';
  // An empty cell is always acceptable: clearing a field is a legitimate
  // correction (it travels as `_deleted_keys`, see editable.py:197).
  if (s === '' && type !== 'bool_yn') return { ok: true };
  if (type === 'int' || type === 'float' || type === 'number') {
    if (rcaPyParseNumber(s) === null) {
      return {
        ok: false, key: 'edit.cellNotNumber', params: { value: s },
        text: rcaEditT('edit.cellNotNumber', { value: s }),
      };
    }
  }
  if (type === 'bool_yn') {
    const v = s.toLowerCase();
    if (['y', 'n', 'yes', 'no', 'true', 'false', '1', '0', 't', 'f'].indexOf(v) === -1) {
      return {
        ok: false, key: 'edit.cellNotYesNo', params: { value: s },
        text: rcaEditT('edit.cellNotYesNo', { value: s }),
      };
    }
  }
  if (spec.validate === 'stage' && !RCA_INFORMAL_STAGE_RE.test(s)) {
    if (rcaIcsLookupStage(s) === null) {
      return {
        ok: false, key: 'edit.badStage', params: { value: s },
        text: rcaEditT('edit.badStage', { value: s }),
      };
    }
  }
  if (spec.validate === 'range-pair' || spec.validate === 'bed-pair'
      || spec.validate === 'age-pair') {
    const pair = Array.isArray(spec.peer) ? spec.peer : [];
    const topField = pair[0];
    const baseField = pair[1];
    if (!topField || !baseField || !rcaIsDict(row)) return { ok: true };
    // FE-FIX-2026-09-21 (audit item 7): the live row is keyed by MODEL names
    // (range_top_idx / range_base_idx), while spec.peer lists the field names
    // (top_idx / base_idx). Reading row['top_idx'] found nothing on
    // lithology_blocks / age_units, so BOTH peers were undefined and an
    // inverted bed pair was accepted. `peerModel` (mapped through cfg.edit by
    // the caller) carries the row keys; without it the field names are the
    // keys anyway (range_top/base_age columns), so the fallback is safe.
    const pm = Array.isArray(spec.peerModel) ? spec.peerModel : [];
    const topKey = pm[0] || topField;
    const baseKey = pm[1] || baseField;
    const isEditedTop = (spec.field === topField) || (spec.model === topKey);
    const topVal = isEditedTop ? s : row[topKey];
    const baseVal = !isEditedTop ? s : row[baseKey];
    const fn = spec.validate === 'bed-pair' ? rcaBedPairInverted
      : spec.validate === 'age-pair' ? rcaAgePairInverted : rcaRangePairInverted;
    const inverted = fn(topVal, baseVal);
    if (inverted === true) {
      const key = spec.validate === 'age-pair' ? 'edit.ageInverted'
        : spec.validate === 'bed-pair' ? 'edit.idxInverted' : 'edit.rangeInverted';
      return {
        ok: false, key: key,
        params: { top: rcaEditPyStr(topVal), base: rcaEditPyStr(baseVal) },
        text: rcaEditT(key, { top: rcaEditPyStr(topVal), base: rcaEditPyStr(baseVal) }),
      };
    }
  }
  return { ok: true };
}

// ---- low-confidence rows (J/K walk) --------------------------------------

// quality.js:rcaWarningFlags mirrors rca_core/quality.py::_warning_flags;
// table.js keeps its own copy so the editor loads without the scorer.
function rcaEditWarningFlags(value) {
  if (value === null || value === undefined) return [];
  if (typeof value === 'string') return value ? [value] : [];
  if (Array.isArray(value)) return value.filter(Boolean).map(String);
  return [String(value)];
}

// A row deserves a second look when ANY of the existing review signals fires.
// These are all data the pipeline already writes — nothing new is invented:
//   * per-row `confidence` below RCA_LOWCONF_THRESHOLD (species_ranges)
//   * low agreement band (multi-run merge, rcaRowAgreementBand)
//   * a `_warning` flag from the normalizers (index_order_swap, ...)
//   * the coverage contract's `not_drawn` state (js/reason-codes.js)
//   * endpoint_kind / occurrence_mode left at the "unknown" sentinel
//   * a malformed (non-dict) row in a dict table
const RCA_LOWCONF_THRESHOLD = 0.6;

function rcaTableIsLowConfidenceRow(data, tableId, item, runsOverride) {
  // other_fossils rows are plain strings BY DESIGN (editable.py:_SCALAR_LIST_KEYS),
  // so "not a dict" is not a review signal there.
  if (tableId === 'other_fossils') return false;
  if (!rcaIsDict(item)) return true;
  const runsRaw = (runsOverride === undefined && data) ? data.runs : runsOverride;
  const runs = Number(runsRaw);
  if (Number.isFinite(runs) && runs > 1 && rcaRowAgreementBand(item, runs) === 'low') return true;
  if (rcaEditWarningFlags(item._warning).length > 0) return true;
  if (typeof rcaCoverageState === 'function' && rcaCoverageState(item) === 'not_drawn') return true;
  if (item.endpoint_kind === 'unknown' || item.occurrence_mode === 'unknown') return true;
  const conf = Number(item.confidence);
  if (Number.isFinite(conf) && conf < RCA_LOWCONF_THRESHOLD) return true;
  return false;
}

// Indices of the review rows of one table, in row order.
function rcaTableLowConfidenceRows(data, tableId) {
  const out = [];
  const rows = rcaRowsForTable(data, tableId);
  rows.forEach((item, idx) => {
    if (rcaTableIsLowConfidenceRow(data, tableId, item)) out.push(idx);
  });
  return out;
}

// "上一个 / 下一个低置信行" (Tabulator's navigate semantics, but hopping between
// the rows that actually need proofreading). Returns the index, or -1 for
// "nothing further in that direction"; dir is -1 (up) or +1 (down).
function rcaNextLowConfidenceRow(indices, currentRow, dir) {
  const list = (Array.isArray(indices) ? indices : []).slice();
  if (!list.length) return -1;
  const step = dir < 0 ? -1 : 1;
  const cur = Number(currentRow);
  const base = Number.isFinite(cur) ? cur : (step > 0 ? -1 : list.length);
  if (step > 0) {
    for (const i of list) if (i > base) return i;
  } else {
    for (let k = list.length - 1; k >= 0; k -= 1) if (list[k] < base) return list[k];
  }
  return -1;
}

// ---- the dirty-cell registry (Tabulator's edit model, editable.py's diff) --
//
// One object owns the editing session:
//   attach(data)          snapshot (the `before` of the diff) + cfg lookup
//   editCell(...)         validate -> coerce -> write -> mark dirty -> action
//   capture(tableId)      editable.py:capture_edits for ONE table
//   captureAll()          ... for the whole result (what "Apply edits" sends)
//   applyEdits(res, edits)  editable.py:apply_edits
//   getEditedCells / setCellEdited / clearCellEdited / clearEdited
//   selection: toggleRow / selectAll / selectedRows / clearSelection
//
// The registry — not the DOM — decides what "dirty" means, so a re-render can
// repaint the dirty frames from data alone and the Qt payload and the browser
// agree on the same edit set.

// FE-FIX-2026-09-21 (audit item 7): rcaValidateCell's pair checks read the
// PEER value out of the live row dict, but rows are keyed by MODEL names
// (range_top_idx) while spec.peer lists field names (top_idx). Map each peer
// field to its model key through the table's own cfg.edit metadata and hand
// the validator an augmented spec (`peerModel`); the validator keeps its
// 3-arg signature and falls back to the field names, which are the keys for
// every non-columnar pair column.
function rcaEditValidateRef(edits, tableId, spec) {
  if (!spec || !Array.isArray(spec.peer) || !spec.peer.length) return spec;
  const cfg = edits.cfg(tableId);
  if (!cfg || !Array.isArray(cfg.edit)) return spec;
  const peerModel = spec.peer.map((f) => {
    for (const s2 of cfg.edit) {
      if (s2 && (s2.field === f || s2.model === f)) return s2.model || s2.field;
    }
    return f;
  });
  return Object.assign({}, spec, { peerModel: peerModel });
}

// FE-FIX-2026-09-21 (audit item 6): editable.py:290 ASSIGNS whatever the
// payload carries — `item[col] = copy.deepcopy(val)` — so a model whose
// baseline value is empty ("" / [] / None) KEEPS the key, while key removal
// only ever travels through `_deleted_keys`. The old JS write path deleted
// the key for EVERY empty value, so clearing a cell whose snapshot was
// already empty (and its undo) silently turned "keep the empty field" into
// "delete the field", and the next capture emitted a bogus _deleted_keys
// entry. This helper answers "may this empty value be written in place?" —
// yes when the snapshot itself carries that same empty under the key; the
// rowWrite implementations then assign instead of deleting.
function rcaEditKeepsEmptyKey(edits, tableId, rowIdx, spec, value) {
  if (!rcaIsEmptyModelValue(value)) return false;
  const orig = edits.originalCell(tableId, rowIdx, spec);
  if (!orig.found || !orig.hasKey) return false;
  return rcaPyEqual(rcaCoerceVal(orig.value), rcaCoerceVal(value))
    || (rcaIsEmptyModelValue(orig.value) && (value === null || value === undefined)
      && (orig.value === '' || orig.value === null || orig.value === undefined
        || (Array.isArray(orig.value) && orig.value.length === 0)));
}

const rcaTableEdits = {
  data: null,
  snapshot: null,
  _cfgs: {},
  _edited: {},        // tableId -> { 'row|field': true }
  _selection: {},     // tableId -> { row: true }
  _droppedMarks: {},  // tableId -> [{row, item, fields}] (FE-FIX item 5)

  attach(data, cfgs) {
    // FE-FIX-2026-09-21 (audit item 1): attaching DIFFERENT data drops the
    // previous session's snapshot, so every undo action on the global stack
    // addresses rows that no longer belong to this result. Clear the stack
    // (typeof-guarded: the editor must work without js/history.js loaded).
    const prevData = this.data;
    const nextData = rcaIsDict(data) ? data : null;
    this.data = nextData;
    this.snapshot = this.data ? rcaClone(this.data) : null;
    this._edited = {};
    this._selection = {};
    this._droppedMarks = {};
    this._cfgs = {};
    const list = Array.isArray(cfgs) ? cfgs : (this.data ? rcaTableConfigs(this.data) : []);
    for (const cfg of list || []) {
      if (cfg && cfg.id) this._cfgs[cfg.id] = cfg;
    }
    if (prevData && prevData !== nextData) {
      const hist = rcaEditHistoryNs();
      if (hist && typeof hist.clear === 'function') {
        try { hist.clear(); } catch (_e) { /* never block the attach */ }
      }
    }
    return this;
  },

  detach() {
    this.data = null;
    this.snapshot = null;
    this._cfgs = {};
    this._edited = {};
    this._selection = {};
  },

  isAttached() { return !!this.data; },
  live() { return this.data; },
  original() { return this.snapshot; },

  cfg(tableId) { return this._cfgs[tableId] || null; },
  setConfig(cfg) { if (cfg && cfg.id) this._cfgs[cfg.id] = cfg; return cfg; },

  // The result key the diff walks. A nested sub-table edits through its
  // PARENT list (editable.py's comment on the columnar sub-tables: those
  // changes are already captured by the `sections` row diff, because the
  // list value compares deep).
  listKey(tableId) {
    const cfg = this.cfg(tableId);
    if (cfg && cfg.nested && cfg.nested.parent) return cfg.nested.parent;
    return tableId;
  },

  spec(tableId, colIdx) {
    const cfg = this.cfg(tableId);
    return cfg ? rcaColEditSpec(cfg, colIdx) : null;
  },

  specByField(tableId, field) {
    const cfg = this.cfg(tableId);
    if (!cfg || !Array.isArray(cfg.edit)) return null;
    for (const spec of cfg.edit) {
      if (!spec) continue;
      if (rcaEditFieldOf(spec) === field) return spec;
    }
    return null;
  },

  // ---- dirty marks (Tabulator: setCellEdited / getEditedCells / clear) ----
  setCellEdited(tableId, row, field) {
    const bucket = this._edited[tableId] || (this._edited[tableId] = {});
    bucket[rcaEditDirtyKey(row, field)] = true;
    return this;
  },
  clearCellEdited(tableId, row, field) {
    const bucket = this._edited[tableId];
    if (bucket) delete bucket[rcaEditDirtyKey(row, field)];
    return this;
  },
  isCellEdited(tableId, row, field) {
    const bucket = this._edited[tableId];
    return !!(bucket && bucket[rcaEditDirtyKey(row, field)]);
  },
  // FE-FIX-2026-09-21 (audit item 5): remove ONE row's dirty marks and hand
  // back the fields they carried. deleteRow / undoAddRow stash the result by
  // (row, item) so undoDeleteRow can restore exactly what was taken — an
  // index-based re-derivation would mis-flag a re-added row (the snapshot row
  // at that index is a DIFFERENT record once an added row is inserted).
  popEditedRow(tableId, row) {
    const bucket = this._edited[tableId];
    const fields = [];
    if (!bucket) return fields;
    const kept = {};
    for (const key of Object.keys(bucket)) {
      const parts = key.split('|');
      const r = rcaPyInt(parts[0]);
      if (r === row) fields.push(parts.slice(1).join('|'));
      else kept[key] = true;
    }
    this._edited[tableId] = kept;
    return fields;
  },
  dropSelectionRow(tableId, row) {
    const bucket = this._selection[tableId];
    if (bucket) delete bucket[String(row)];
    return this;
  },
  stashDroppedMarks(tableId, row, item, fields) {
    if (!fields || !fields.length) return this;
    const stack = this._droppedMarks[tableId] || (this._droppedMarks[tableId] = []);
    stack.push({ row: row, item: rcaClone(item), fields: fields.slice() });
    while (stack.length > 100) stack.shift();   // bounded: never the memory leak
    return this;
  },
  takeStashedMarks(tableId, row, item) {
    const stack = this._droppedMarks[tableId];
    if (!Array.isArray(stack)) return null;
    for (let i = stack.length - 1; i >= 0; i -= 1) {
      if (stack[i].row === row && rcaPyEqual(stack[i].item, item)) {
        return stack.splice(i, 1)[0].fields;
      }
    }
    return null;
  },
  getEditedCells(tableId) {
    const out = [];
    const bucket = this._edited[tableId] || {};
    for (const key of Object.keys(bucket)) {
      const parts = key.split('|');
      out.push([rcaPyInt(parts[0]), parts.slice(1).join('|')]);
    }
    return out;
  },
  editedCount() {
    let n = 0;
    for (const tableId of Object.keys(this._edited)) {
      n += Object.keys(this._edited[tableId]).length;
    }
    return n;
  },
  clearEdited(tableId) {
    if (tableId === undefined) this._edited = {};
    else delete this._edited[tableId];
    return this;
  },
  // Structural edits shift row indices: keep the dirty marks attached to the
  // rows they were made on.
  shiftEdited(tableId, fromRow, delta) {
    const bucket = this._edited[tableId];
    if (!bucket) return this;
    const moved = {};
    for (const key of Object.keys(bucket)) {
      const parts = key.split('|');
      const row = rcaPyInt(parts[0]);
      const field = parts.slice(1).join('|');
      const next = (row !== null && row >= fromRow) ? row + delta : row;
      if (next === null || next < 0) continue;
      moved[rcaEditDirtyKey(next, field)] = true;
    }
    this._edited[tableId] = moved;
    return this;
  },

  // ---- the write path -----------------------------------------------------
  // One cell edit, end to end: validate -> coerce -> write into the live data
  // -> update the dirty registry -> return the history action (or null when
  // the value did not actually change, so nothing lands on the undo stack).
  editCell(tableId, rowIdx, colIdx, text) {
    const spec = this.spec(tableId, colIdx);
    if (!spec || !spec.editable) {
      return { ok: false, key: 'edit.notEditable', text: '' };
    }
    const target = this.resolveCell(tableId, rowIdx);
    if (!target.found) return { ok: false, key: 'edit.noRow', text: '' };
    const check = rcaValidateCell(rcaEditValidateRef(this, tableId, spec), text, target.row);
    if (!check.ok) return check;
    const model = spec.model || spec.field;
    const fieldKey = rcaEditFieldOf(spec);
    const before = target.cellValue(model, spec);
    const hadKey = target.hasKey(model, spec);
    const after = rcaCoerceCellValue(text, spec.type);
    if (hadKey && rcaPyEqual(rcaCoerceVal(before), rcaCoerceVal(after === null ? '' : after))
        && !rcaIsEmptyModelValue(after)) {
      // Same value: nothing is written and nothing is pushed. The frame still
      // has to be RE-DERIVED (markCell), never blindly dropped: a cell that
      // reads "A*" in a model whose snapshot says "A" is dirty, and re-typing
      // the very same "A*" (or "  A*  " — `_coerce` trims before comparing,
      // editable.py:130) must not make that review marker vanish.
      this.markCell(tableId, rowIdx, spec, fieldKey);
      return { ok: true, changed: false, action: null };
    }
    target.write(model, spec, after, rcaEditKeepsEmptyKey(this, tableId, rowIdx, spec, after));
    // FE-FIX-2026-09-21 (audit item 4): the old branch
    // `if (empty(after) && !hadKey) { clearCellEdited(); return changed:false }`
    // WIPED the dirty mark whenever a second clear-commit landed on a cell —
    // even with an edit still pending on other fields of the same key. The
    // mark is now ALWAYS re-derived from the snapshot (markCell): "dirty"
    // means "live differs from the snapshot", which only markCell answers.
    this.markCell(tableId, rowIdx, spec, fieldKey);
    // A commit that leaves the model bit-identical to what it was BEFORE the
    // write (the keyless-empty re-commit, or the snapshot's own empty kept in
    // place by rcaEditKeepsEmptyKey) changes nothing — no action on the stack.
    // A commit that WRITES the cell back to the original value does stay on
    // the stack (undo has to be able to return to the intermediate value),
    // which is why key presence + value are compared against `before`, not
    // against the snapshot. `dirty` still reflects the truth either way.
    const nowHas = target.hasKey(model, spec);
    const nowVal = target.cellValue(model, spec);
    const unwritten = (hadKey === nowHas) && (!hadKey
      || rcaPyEqual(rcaCoerceVal(before), rcaCoerceVal(nowVal)));
    if (unwritten) return { ok: true, changed: false, action: null };
    return {
      ok: true,
      changed: true,
      action: {
        type: 'cellEdit',
        tableId: tableId,
        row: rowIdx,
        col: colIdx,
        field: fieldKey,
        model: model,
        before: before,
        hadKey: hadKey,
        after: after,
        cleared: rcaIsEmptyModelValue(after),
      },
    };
  },

  // Re-apply a value WITHOUT validating or recording history — this is what
  // js/history.js's undoers call, and it mirrors what apply_edits does with a
  // payload cell (index-based, `null` / empty removes the key).
  setCellValue(tableId, rowIdx, field, value, opts) {
    const spec = (opts && opts.spec) || this.specByField(tableId, field)
      || (field === rcaEditScalarField() ? { field: null, scalar: true, type: 'str' } : null);
    const target = this.resolveCell(tableId, rowIdx, spec);
    if (!target.found) return false;
    const model = (spec && spec.model) || (spec && spec.field) || field;
    // FE-FIX-2026-09-21 (audit item 6): the old comment here claimed write()
    // already matched apply_edits by deleting the key for empties — it did
    // NOT. editable.py:290 assigns the payload value as-is (`item[col] =
    // deepcopy(val)`), so restoring an empty baseline must KEEP the key
    // (rcaEditKeepsEmptyKey), otherwise an undo of a clear on an originally-
    // empty cell leaves the key gone and capture() emits a bogus
    // _deleted_keys that would delete a field the original data had.
    target.write(model, spec, value,
      rcaEditKeepsEmptyKey(this, tableId, rowIdx, spec, value));
    const fieldKey = rcaEditFieldOf(spec, null);
    // The dirty frame is RE-DERIVED from the snapshot (markCell), so undoing
    // back to the original value clears it and undoing to a different value
    // keeps it — no blind set / clear here.
    this.markCell(tableId, rowIdx, spec, fieldKey);
    return true;
  },

  // Dirty mark = "this cell differs from the snapshot", i.e. exactly the
  // per-cell branch of editable.py:capture_edits (key presence first, then the
  // trimmed value). Re-deriving it after EVERY write — instead of setting the
  // mark on write and clearing it on empty — is what makes an undo back to the
  // original value clear the frame instead of leaving a phantom "unsaved".
  markCell(tableId, rowIdx, spec, fieldKey) {
    const model = (spec && (spec.model || spec.field)) || null;
    const live = this.resolveCell(tableId, rowIdx, spec);
    const orig = this.originalCell(tableId, rowIdx, spec);
    const liveHas = live.found && live.hasKey(model, spec);
    let dirty;
    if (!orig.found) dirty = true;                       // row appeared: structural
    else if (liveHas !== orig.hasKey) dirty = true;
    else if (!liveHas) dirty = false;                    // neither side has it
    else {
      dirty = rcaPyNotEqual(
        rcaCoerceVal(live.cellValue(model, spec)),
        rcaCoerceVal(orig.value)
      );
    }
    if (dirty) this.setCellEdited(tableId, rowIdx, fieldKey);
    else this.clearCellEdited(tableId, rowIdx, fieldKey);
    return dirty;
  },

  // The snapshot's version of one cell (the `before` of the diff). Nested
  // sub-tables resolve through their own flattened _src, exactly like
  // resolveCell does for the live data.
  originalCell(tableId, rowIdx, spec) {
    const snap = this.snapshot;
    const model = (spec && (spec.model || spec.field)) || null;
    const miss = { found: false, value: undefined, hasKey: false };
    if (!rcaIsDict(snap)) return miss;
    const cfg = this.cfg(tableId);
    let item = null;
    if (cfg && cfg.nested) {
      const flat = rcaRowsForTable(snap, tableId);
      const src = flat[rowIdx] && flat[rowIdx]._src;
      if (!Array.isArray(src)) return miss;
      const parents = snap[cfg.nested.parent];
      const parent = Array.isArray(parents) ? parents[src[0]] : null;
      const sub = rcaIsDict(parent) ? parent[cfg.nested.subKey] : null;
      if (!Array.isArray(sub) || src[2] >= sub.length) return miss;
      item = sub[src[2]];
    } else {
      const list = snap[this.listKey(tableId)];
      if (!Array.isArray(list) || rowIdx < 0 || rowIdx >= list.length) return miss;
      item = list[rowIdx];
    }
    if (item === null || item === undefined) return miss;
    if (spec && spec.scalar) return { found: true, value: item, hasKey: true };
    if (!rcaIsDict(item)) return { found: true, value: undefined, hasKey: false };
    return {
      found: true,
      value: item[model],
      hasKey: Object.prototype.hasOwnProperty.call(item, model),
    };
  },

  resolveCell(tableId, rowIdx, spec) {
    const data = this.data;
    const api = {
      found: false,
      row: null,
      list: null,
      index: rowIdx,
      hasKey(model, sp) { return api.found && api.rowHas ? api.rowHas(model, sp) : false; },
      cellValue(model, sp) { return api.found ? api.rowGet(model, sp) : undefined; },
      write(model, sp, value, keepEmpty) {
        if (api.found) api.rowWrite(model, sp, value, keepEmpty);
      },
    };
    if (!data || !rcaIsDict(data)) return api;
    const cfg = this.cfg(tableId);
    if (cfg && cfg.nested) {
      // Nested rows are only reachable through their _src provenance; index
      // based addressing matches the flattened table the user sees.
      const flat = rcaRowsForTable(data, tableId);
      const frow = flat[rowIdx];
      const src = frow && frow._src;
      if (!Array.isArray(src)) return api;
      const parents = data[cfg.nested.parent];
      const parent = Array.isArray(parents) ? parents[src[0]] : null;
      if (!rcaIsDict(parent)) return api;
      const sub = parent[cfg.nested.subKey];
      if (!Array.isArray(sub) || src[2] >= sub.length) return api;
      api.found = true;
      api.row = sub[src[2]];
      api.list = sub;
      api.index = src[2];
      api.rowHas = (model) => rcaIsDict(api.row) && Object.prototype.hasOwnProperty.call(api.row, model);
      api.rowGet = (model) => (rcaIsDict(api.row) ? api.row[model] : undefined);
      api.rowWrite = (model, sp, value, keepEmpty) => {
        if (!rcaIsDict(api.row)) return;
        // FE-FIX-2026-09-21 (audit item 6): an empty value only deletes the
        // key when it is NOT the snapshot's own empty (see
        // rcaEditKeepsEmptyKey) — mirrors editable.py:290's plain assignment.
        if (rcaIsEmptyModelValue(value) && !keepEmpty) delete api.row[model];
        else api.row[model] = (value === undefined ? null : value);
      };
      return api;
    }
    const key = this.listKey(tableId);
    const list = data[key];
    if (!Array.isArray(list) || rowIdx < 0 || rowIdx >= list.length) return api;
    api.found = true;
    api.list = list;
    api.index = rowIdx;
    api.row = list[rowIdx];
    api.rowHas = (model, sp) => {
      if (sp && sp.scalar) return true;              // the row IS the value
      if (!rcaIsDict(list[rowIdx])) return false;
      return Object.prototype.hasOwnProperty.call(list[rowIdx], model);
    };
    api.rowGet = (model, sp) => {
      const item = list[rowIdx];
      if (sp && sp.scalar) return item;
      return rcaIsDict(item) ? item[model] : undefined;
    };
    api.rowWrite = (model, sp, value, keepEmpty) => {
      const item = list[rowIdx];
      if (sp && sp.scalar) {
        list[rowIdx] = (value === null || value === undefined) ? '' : value;
        return;
      }
      if (!rcaIsDict(item)) {
        // editable.py:271-275 — promote a scalar row so it can carry fields.
        list[rowIdx] = { value: item };
      }
      // FE-FIX-2026-09-21 (audit item 6): same rule as the nested rowWrite —
      // empties only remove the key when they are not the snapshot's own
      // empty; `undefined` normalises to null (JSON has no undefined).
      if (rcaIsEmptyModelValue(value) && !keepEmpty) delete list[rowIdx][model];
      else list[rowIdx][model] = (value === undefined ? null : value);
    };
    return api;
  },

  // ---- structural edits ---------------------------------------------------
  // Row delete / add are index operations on the live list, exactly what the
  // `_replaced` / `new_<i>` branches of the payload encode. Nested sub-tables
  // are excluded: the flattened row has no stable index in a parent list.
  canDeleteRows(tableId) {
    // Same rule as the renderer (rcaTableStructuralEdits), read through the
    // ATTACHED session: no session -> no rows to mutate -> false, fail closed.
    return rcaTableStructuralEdits(this.cfg(tableId));
  },

  deleteRow(tableId, rowIdx) {
    if (!this.canDeleteRows(tableId)) return null;
    const key = this.listKey(tableId);
    const list = this.data && this.data[key];
    if (!Array.isArray(list) || rowIdx < 0 || rowIdx >= list.length) return null;
    const item = rcaClone(list[rowIdx]);
    list.splice(rowIdx, 1);
    // FE-FIX-2026-09-21 (audit item 5): the old `shiftEdited(fromRow, -1)`
    // moved the DELETED row's own marks onto the previous row (rows >= rowIdx
    // all shift, including rowIdx itself) — a phantom red frame on a row that
    // was never edited. The deleted row's marks are now REMOVED (and stashed
    // by row/identity so undoDeleteRow can put back exactly what was taken —
    // index-based re-derivation would compare a re-ADDED row against the
    // wrong snapshot row), then only rows below shift.
    this.stashDroppedMarks(tableId, rowIdx, item, this.popEditedRow(tableId, rowIdx));
    this.shiftEdited(tableId, rowIdx + 1, -1);
    this.dropSelectionRow(tableId, rowIdx);
    this.shiftSelection(tableId, rowIdx + 1, -1);
    return { type: 'rowDelete', tableId: tableId, row: rowIdx, item: item };
  },

  undoDeleteRow(tableId, rowIdx, item) {
    const key = this.listKey(tableId);
    const list = this.data && this.data[key];
    if (!Array.isArray(list)) return false;
    // FE-FIX-2026-09-21 (audit item 1): the old clamp (`min(len, rowIdx)`)
    // silently APPENDED the row at the wrong position when the index was out
    // of range while js/history.js believed the restore had succeeded. Only
    // `rowIdx === list.length` stays legal (Python list.insert appends);
    // anything beyond fails closed so the stack can classify the action.
    if (rowIdx < 0 || rowIdx > list.length) return false;
    const at = rowIdx;
    list.splice(at, 0, rcaClone(item));
    this.shiftEdited(tableId, at, +1);
    this.shiftSelection(tableId, at, +1);
    // FE-FIX-2026-09-21 (audit item 5): symmetric restore of the marks the
    // delete / add-undo took with them.
    const restored = this.takeStashedMarks(tableId, at, item);
    if (restored) {
      for (const f of restored) this.setCellEdited(tableId, at, f);
    }
    return true;
  },

  addRow(tableId, rowIdx) {
    if (!this.canDeleteRows(tableId)) return null;
    const key = this.listKey(tableId);
    const list = this.data && this.data[key];
    if (!Array.isArray(list)) return null;
    const at = (rowIdx === undefined || rowIdx === null) ? list.length
      : Math.max(0, Math.min(list.length, rowIdx));
    const template = rcaNewRowTemplate(this.cfg(tableId), tableId);
    list.splice(at, 0, template);
    this.shiftEdited(tableId, at, +1);
    return { type: 'rowAdd', tableId: tableId, row: at, item: rcaClone(template) };
  },

  undoAddRow(tableId, rowIdx) {
    const key = this.listKey(tableId);
    const list = this.data && this.data[key];
    if (!Array.isArray(list) || rowIdx < 0 || rowIdx >= list.length) return false;
    const removed = rcaClone(list[rowIdx]);
    list.splice(rowIdx, 1);
    // FE-FIX-2026-09-21 (audit item 5, same phantom-shift rule as deleteRow):
    // the removed row's own marks die with it (stashed for the redo, see
    // undoDeleteRow); only rows below shift.
    this.stashDroppedMarks(tableId, rowIdx, removed, this.popEditedRow(tableId, rowIdx));
    this.shiftEdited(tableId, rowIdx + 1, -1);
    this.dropSelectionRow(tableId, rowIdx);
    this.shiftSelection(tableId, rowIdx + 1, -1);
    return removed;
  },

  // ---- selection ----------------------------------------------------------
  toggleRow(tableId, rowIdx, on) {
    const bucket = this._selection[tableId] || (this._selection[tableId] = {});
    if (on === undefined) on = !bucket[rowIdx];
    if (on) bucket[rowIdx] = true; else delete bucket[rowIdx];
    return this;
  },
  selectAll(tableId, rows, on) {
    if (!on) { delete this._selection[tableId]; return this; }
    const bucket = this._selection[tableId] = {};
    (rows || []).forEach((item, idx) => { bucket[idx] = true; });
    return this;
  },
  selectedRows(tableId) {
    const bucket = this._selection[tableId] || {};
    return Object.keys(bucket).map((k) => rcaPyInt(k)).filter((n) => n !== null).sort((a, b) => a - b);
  },
  hasSelection(tableId) { return this.selectedRows(tableId).length > 0; },
  clearSelection(tableId) {
    if (tableId === undefined) this._selection = {};
    else delete this._selection[tableId];
    return this;
  },
  shiftSelection(tableId, fromRow, delta) {
    const bucket = this._selection[tableId];
    if (!bucket) return this;
    const moved = {};
    for (const key of Object.keys(bucket)) {
      const row = rcaPyInt(key);
      if (row === null) continue;
      const next = row >= fromRow ? row + delta : row;
      if (next < 0) continue;
      moved[next] = true;
    }
    this._selection[tableId] = moved;
    return this;
  },

  // ---- the diff -----------------------------------------------------------
  capture(tableId) {
    if (!this.data || !this.snapshot) return {};
    const key = this.listKey(tableId);
    if (RCA_EDIT_LIST_KEYS.indexOf(key) === -1) return {};
    const one = {};
    one[key] = rcaCaptureListEdits(this.snapshot[key] || [], this.data[key] || []);
    if (Object.keys(one[key]).length === 0) return {};
    return one[key];
  },

  captureAll() {
    if (!this.data || !this.snapshot) return {};
    return rcaCaptureEdits(this.snapshot, this.data);
  },

  applyEdits(result, edits) { return rcaApplyEdits(result, edits); },

  isDirty() { return rcaIsDirtyEdits(this.captureAll()); },

  // The dirty frames the DOM has to repaint after a re-render.
  dirtyTables() { return Object.keys(this._edited); },

  // Export ONLY the selected rows of a table (the selection bar's action).
  exportRows(tableId, rowIdxs) {
    if (!this.data) return { headers: [], rows: [] };
    const cfg = this.cfg(tableId);
    if (!cfg) return { headers: [], rows: [] };
    const pick = Array.isArray(rowIdxs) ? rowIdxs : this.selectedRows(tableId);
    const headers = [t('col.index')].concat(cfg.cols.map((c) => t(c)));
    const items = rcaRowsForTable(this.data, tableId);
    const rows = pick.filter((i) => i >= 0 && i < items.length).map((i) => {
      const cells = rcaRowCellsFor(cfg, items[i]).map(rcaExportCellText);
      return [String(i + 1)].concat(cells);
    });
    return { headers, rows };
  },
};

function rcaEditDirtyKey(row, field) { return String(row) + '|' + String(field); }
function rcaEditScalarField() { return '__row'; }

// The model field a spec writes. A scalar spec (other_fossils) has no field —
// its dirty mark hangs off the pseudo-field so the row itself can be flagged.
function rcaEditFieldOf(spec, item) {
  if (!spec) return null;
  if (spec.scalar) return rcaEditScalarField();
  return spec.model || spec.field || null;
}

// Column metadata accessor: `cfg.edit` is index-aligned with `cfg.cols`.
function rcaColEditSpec(cfg, colIdx) {
  if (!cfg || !Array.isArray(cfg.edit)) return null;
  return cfg.edit[colIdx] || null;
}

// The data-* descriptors an editable <td> carries. The delegated handlers read
// these back instead of re-deriving the column from the model, so a re-render
// mid-edit cannot misattribute the typed value.
function rcaEditDataAttrs(cfg, rowIdx, colIdx, spec) {
  const safeId = rcaEscAttr(cfg.id);
  let out = ' data-rca-edit="1"'
    + ' data-table="' + safeId + '"'
    + ' data-row="' + rowIdx + '"'
    + ' data-col="' + colIdx + '"';
  if (spec.scalar) out += ' data-scalar="1"';
  else out += ' data-field="' + rcaEscAttr(spec.model || spec.field) + '"';
  out += ' data-type="' + rcaEscAttr(spec.type || 'str') + '"';
  if (spec.validate) out += ' data-validate="' + rcaEscAttr(spec.validate) + '"';
  if (Array.isArray(spec.peer)) out += ' data-peer="' + rcaEscAttr(spec.peer.join(',')) + '"';
  return out;
}

// Structural editability is a property of the COLUMN CONFIG, not of the
// editing session: a nested sub-table (`lithology_blocks`, `age_units`,
// `samples`, ...) is addressed through its PARENT row, so its flattened rows
// have no stable index to insert into or splice out of.
//
// KNOWN-GAP(域T) CLOSED 2026-09-20: this used to be answered through
// `rcaTableEdits.canDeleteRows(cfg.id)`, i.e. through the registry's `_cfgs`
// map — which is only populated by `attach()`. `rcaRenderResults` is a PURE
// function (the Qt Fluent history dialog, the print template and the
// "results arrived but nothing is attached yet" first paint all call it that
// way), so every structural affordance — the 新增行 button of an EMPTY table
// above all, since an empty result is exactly the state where nothing has been
// attached yet — silently vanished outside a live editing session. Keep the
// renderer cfg-driven; the registry keeps its own guard for the WRITE path
// (`canDeleteRows` refuses to mutate a detached / nested table, which is right).
function rcaTableStructuralEdits(cfg) {
  return !!cfg && !cfg.nested;
}

function rcaEditAddRowButton(cfg) {
  if (!rcaTableStructuralEdits(cfg)) return '';
  return '<div class="rca-table-foot">'
    + '<button type="button" class="btn btn-secondary btn-small rca-addrow-btn"'
    + ' data-rca-addrow="' + rcaEscAttr(cfg.id) + '"'
    + ' title="' + rcaEscAttr(rcaEditT('edit.addRowHint')) + '">'
    + rcaEsc(rcaEditT('edit.addRow')) + '</button></div>';
}

function rcaEditSelectionBar(cfg) {
  const safeId = rcaEscAttr(cfg.id);
  const del = rcaTableStructuralEdits(cfg)
    ? '<button type="button" class="btn btn-secondary btn-small" data-rca-sel-action="delete"'
      + ' data-rca-sel-table="' + safeId + '">' + rcaEsc(rcaEditT('edit.deleteSelected')) + '</button>'
    : '';
  return '<div class="rca-selection-bar" data-selection-bar="' + safeId + '" hidden>'
    + '<span class="rsb-count" data-selection-count="' + safeId + '">0</span>'
    + '<span class="rsb-actions">'
    + '<button type="button" class="btn btn-secondary btn-small" data-rca-sel-action="export"'
    + ' data-rca-sel-table="' + safeId + '">' + rcaEsc(rcaEditT('edit.exportSelected')) + '</button>'
    + del
    + '<button type="button" class="btn btn-secondary btn-small" data-rca-sel-action="clear"'
    + ' data-rca-sel-table="' + safeId + '">' + rcaEsc(rcaEditT('edit.clearSelection')) + '</button>'
    + '</span></div>';
}

// ===========================================================================
// FE-BORROW-2026-09-20 (域T): the DOM layer
// ===========================================================================
//
// Everything above this line is data-only (Node-testable, no DOM at all).
// What follows is the thin event layer that turns the rendered markup into an
// editor, borrowed shape-for-shape from Tabulator's Edit module:
//
//   editTriggerEvent : "focus"   -> focusin starts the edit (snapshots text)
//   commit / cancel  : Enter/Esc  -> rcaEditCommitCell / rcaEditRevertCell
//   blockedEditor    : validation -> red frame, focus is KEPT (no revert)
//   navigate(Row/Col): arrows     -> rcaEditNavigateCell
//
// Handlers are DELEGATED on the table root, so replacing `root.innerHTML`
// (a structural edit re-renders) cannot orphan a listener and never needs a
// re-attach. `document` / `window` are only ever touched inside a `typeof`
// guard, which is what lets tests_edit_history.js drive this code through a
// ~60-line DOM stub.
const RCA_EDIT_DOM = {
  root: null,
  opts: null,
  rawText: '',
  editing: null,        // { tableId, row, col, cell, text0 } while a cell is focused
  invalid: null,       // the cell currently locked by a failed validation
  hoverRow: null,      // last row reported to the viz (dedupe mouseover)
  unsubHover: null,    // rcaViz.onRowHover unsubscribe
  wired: false,
  wiredRoot: null,     // the element the delegated listeners live on
  // FE-FIX-2026-09-21 (audit item 15): the listeners are added WITH the
  // opts.capture flag; addEventListener(t, fn, true) and
  // removeEventListener(t, fn) are different registrations, so detaching
  // without the flag left every handler wired (double commits after a
  // re-attach). Remembered here so attach / detach use identical options.
  capture: false,
};

// ---- tiny DOM helpers (stub-friendly) -------------------------------------

function rcaEditAttr(el, name) {
  if (!el || typeof el.getAttribute !== 'function') return null;
  const v = el.getAttribute(name);
  return v === null || v === undefined ? null : String(v);
}

function rcaEditMatches(el, selector) {
  if (!el) return false;
  if (typeof el.matches === 'function') {
    try { return !!el.matches(selector); } catch (_e) { return false; }
  }
  return false;
}

// Element.closest with a manual walk, so a stub without `closest` still works.
function rcaEditClosest(node, selector) {
  let el = node;
  while (el && el !== RCA_EDIT_DOM.root) {
    if (rcaEditMatches(el, selector)) return el;
    el = el.parentNode || (typeof el.parentElement === 'object' ? el.parentElement : null);
    if (!el || el.nodeType === 9) break;
  }
  return rcaEditMatches(el, selector) ? el : null;
}

function rcaEditText(el, value) {
  if (!el) return '';
  if (value !== undefined) {
    if (typeof el.textContent !== 'undefined') el.textContent = String(value);
    return String(value);
  }
  const raw = (typeof el.textContent !== 'undefined') ? el.textContent
    : (typeof el.innerText !== 'undefined' ? el.innerText : '');
  return raw === null || raw === undefined ? '' : String(raw);
}

// focus({preventScroll:true}) + self-managed scrolling — Tabulator's editor
// pattern: the browser's caret-into-view scroll fights the sticky first column,
// so the caller scrolls the row itself.
function rcaEditFocus(el) {
  if (!el || typeof el.focus !== 'function') return false;
  try { el.focus({ preventScroll: true }); } catch (_e) {
    try { el.focus(); } catch (_e2) { return false; }
  }
  rcaEditScrollIntoView(el);
  return true;
}

function rcaEditScrollIntoView(el) {
  if (!el) return;
  if (typeof el.scrollIntoView === 'function') {
    try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }
    catch (_e) { try { el.scrollIntoView(); } catch (_e2) { /* stub */ } }
  }
}

function rcaEditClass(el, name, on) {
  if (!el) return;
  if (el.classList && typeof el.classList.toggle === 'function') {
    el.classList.toggle(name, !!on);
    return;
  }
  const cur = rcaEditAttr(el, 'class') || '';
  const parts = cur.split(/\s+/).filter(Boolean);
  const at = parts.indexOf(name);
  if (on && at === -1) parts.push(name);
  if (!on && at !== -1) parts.splice(at, 1);
  if (typeof el.setAttribute === 'function') el.setAttribute('class', parts.join(' '));
}

function rcaEditAll(root, selector) {
  if (!root || typeof root.querySelectorAll !== 'function') return [];
  const out = root.querySelectorAll(selector);
  return out ? Array.prototype.slice.call(out) : [];
}

function rcaEditOne(root, selector) {
  if (!root || typeof root.querySelector !== 'function') return null;
  return root.querySelector(selector) || null;
}

// ---- cell addressing ------------------------------------------------------

// The <td> descriptor back into a {tableId, row, col, spec} reference. Reading
// it off the DOM (not off the model) is what keeps a mid-edit re-render from
// misattributing the typed value.
function rcaEditRefFromCell(cell) {
  if (!cell || rcaEditAttr(cell, 'data-rca-edit') !== '1') return null;
  const tableId = rcaEditAttr(cell, 'data-table');
  const row = rcaPyInt(rcaEditAttr(cell, 'data-row'));
  const col = rcaPyInt(rcaEditAttr(cell, 'data-col'));
  if (tableId === null || row === null || col === null) return null;
  const cfg = rcaTableEdits.cfg(tableId);
  const spec = cfg ? rcaColEditSpec(cfg, col) : null;
  if (!spec) return null;
  return { tableId: tableId, row: row, col: col, cell: cell, spec: spec,
    scalar: rcaEditAttr(cell, 'data-scalar') === '1' };
}

function rcaEditCellEl(tableId, row, col) {
  const root = RCA_EDIT_DOM.root;
  if (!root) return null;
  return rcaEditOne(root, '[data-rca-edit="1"][data-table="' + tableId
    + '"][data-row="' + row + '"][data-col="' + col + '"]');
}

function rcaEditRowEl(tableId, row) {
  const root = RCA_EDIT_DOM.root;
  if (!root) return null;
  const sec = rcaEditOne(root, '[data-table="' + tableId + '"]');
  return sec ? rcaEditOne(sec, 'tr[data-row="' + row + '"]') : null;
}

// The FIRST editable column of a table — where J/K and Enter-from-outside land.
function rcaEditFirstCol(cfg) {
  if (!cfg || !Array.isArray(cfg.edit)) return null;
  for (let i = 0; i < cfg.edit.length; i += 1) {
    if (cfg.edit[i] && cfg.edit[i].editable) return i;
  }
  return null;
}

// Walk to the neighbouring EDITABLE cell, skipping read-only columns
// (agreement / id) the same way Tabulator's navigate() skips non-editable ones.
function rcaEditNeighbourCell(ref, dRow, dCol) {
  const cfg = rcaTableEdits.cfg(ref.tableId);
  if (!cfg || !Array.isArray(cfg.edit)) return null;
  const nCols = cfg.edit.length;
  const nRows = rcaRowsForTable(rcaTableEdits.live(), ref.tableId).length;
  let row = ref.row;
  let col = ref.col;
  if (dRow) {
    row += dRow > 0 ? 1 : -1;
    col = ref.col;
    // FE-FIX-2026-09-21 (audit item 16): the scan for the next editable
    // column had no `col < nCols` bound — from a row whose tail columns are
    // read-only it walked past the end and threw on cfg.edit[col] (now it
    // just reports `col >= nCols` -> no neighbour, which the caller already
    // handles).
    while (row >= 0 && row < nRows && col < nCols
      && !(cfg.edit[col] && cfg.edit[col].editable)) col += 1;
    if (row < 0 || row >= nRows) return null;
    if (col >= nCols) return null;
  } else if (dCol) {
    col += dCol > 0 ? 1 : -1;
    while (col >= 0 && col < nCols && !(cfg.edit[col] && cfg.edit[col].editable)) {
      col += dCol > 0 ? 1 : -1;
    }
    if (col < 0 || col >= nCols) return null;
  }
  if (row < 0 || row >= nRows || col < 0 || col >= nCols) return null;
  const el = rcaEditCellEl(ref.tableId, row, col);
  return el || null;
}

// ---- hint / live region ---------------------------------------------------

function rcaEditAnnounce(text) {
  const root = RCA_EDIT_DOM.root;
  if (!root || !text) return;
  let live = rcaEditOne(root, '[data-rca-edit-status]');
  if (!live && typeof document !== 'undefined' && document && document.createElement) {
    live = document.createElement('div');
    if (typeof live.setAttribute === 'function') {
      live.setAttribute('class', 'rca-edit-status');
      live.setAttribute('data-rca-edit-status', '1');
      live.setAttribute('role', 'status');
      live.setAttribute('aria-live', 'polite');
    }
    if (typeof root.appendChild === 'function') root.appendChild(live);
  }
  rcaEditText(live, text);
}

// ---- the write / paint path ----------------------------------------------

function rcaEditPaintCell(cell, ref, value) {
  const text = rcaExportCellText(value);
  // FE-FIX-2026-09-21 (audit item 3): the '-' placeholder must NOT sit inside
  // the contenteditable — after a repaint the user could commit the literal
  // "-" by pressing Enter without typing. The cell content goes empty and
  // css/table-edit.css renders the placeholder via ::before on .cell-empty.
  const shown = text.trim() ? text : '';
  rcaEditText(cell, shown);
  if (typeof cell.setAttribute === 'function') {
    cell.setAttribute('title', text);
  }
  rcaEditClass(cell, 'cell-empty', !text.trim());
  rcaEditClass(cell, 'rca-cell-dirty',
    rcaTableEdits.isCellEdited(ref.tableId, ref.row, rcaEditFieldOf(ref.spec)));
}

function rcaEditMarkInvalid(cell, message) {
  rcaEditClass(cell, 'rca-cell-invalid', true);
  if (cell && typeof cell.setAttribute === 'function') {
    cell.setAttribute('aria-invalid', 'true');
    cell.setAttribute('data-rca-invalid', message || '');
  }
  RCA_EDIT_DOM.invalid = cell;
  rcaEditAnnounce(message || '');
}

function rcaEditClearInvalid(cell) {
  rcaEditClass(cell, 'rca-cell-invalid', false);
  if (cell && typeof cell.removeAttribute === 'function') {
    cell.removeAttribute('aria-invalid');
    cell.removeAttribute('data-rca-invalid');
  }
  if (RCA_EDIT_DOM.invalid === cell) RCA_EDIT_DOM.invalid = null;
}

// One commit, end to end. Returns the rcaTableEdits result so callers (and the
// tests) can branch on `ok` / `changed` / `action`.

// FE-FIX-2026-09-21 (audit item 18): a structural re-render swaps
// root.innerHTML while a cell has focus; the browser then fires `focusout`
// against the DESTROYED node. rcaEditRerender keeps no reference to old
// cells, so the honest test is "is this node still under the editor root?".
// Only the cell the editor currently tracks (RCA_EDIT_DOM.editing) can be
// such a ghost — plain out-of-DOM cells handed to the exported commit API
// (app.js / Qt bridge, and the dom-commit-api-on-plain-cell test) stay legal.
function rcaEditCellDetached(cell) {
  const root = RCA_EDIT_DOM.root;
  if (!root || !cell) return false;
  const rec = RCA_EDIT_DOM.editing;
  if (!rec || rec.cell !== cell) return false;
  if (cell === root) return false;
  if (typeof root.contains === 'function') return !root.contains(cell);
  let el = cell.parentNode
    || (typeof cell.parentElement === 'object' ? cell.parentElement : null);
  while (el) {
    if (el === root) return false;
    el = el.parentNode || (typeof el.parentElement === 'object' ? el.parentElement : null);
  }
  return true;
}

function rcaEditCommitCell(cell, textOverride) {
  if (rcaEditCellDetached(cell)) {
    // Ghost focusout: the value is stale by construction (the render that
    // removed this node re-painted the current one). Drop the edit record so
    // the next focus starts clean, and no-op the commit.
    RCA_EDIT_DOM.editing = null;
    return { ok: true, changed: false, action: null, skipped: true };
  }
  const ref = rcaEditRefFromCell(cell);
  if (!ref) return { ok: true, changed: false, action: null, skipped: true };
  const text = textOverride !== undefined ? String(textOverride) : rcaEditText(cell);
  const res = rcaTableEdits.editCell(ref.tableId, ref.row, ref.col, text);
  if (!res.ok) {
    // Tabulator's blocked-editor behaviour: the value stays on screen, the
    // cell keeps focus (red frame), and the reason is announced.
    rcaEditMarkInvalid(cell, res.text || rcaEditT('edit.cellNotNumber', { value: text }));
    rcaEditFocus(cell);
    return res;
  }
  rcaEditClearInvalid(cell);
  if (res.changed && res.action) {
    const target = rcaTableEdits.resolveCell(ref.tableId, ref.row, ref.spec);
    rcaEditPaintCell(cell, ref, target.cellValue(res.action.model, ref.spec));
    rcaEditPublishAction(res.action);
    rcaEditSyncDirtyDom();
  }
  if (RCA_EDIT_DOM.editing && RCA_EDIT_DOM.editing.cell === cell) {
    RCA_EDIT_DOM.editing = null;
  }
  return res;
}

// Esc: put the pre-focus text back and leave. Nothing reaches the model, so
// nothing reaches the undo stack either.
function rcaEditRevertCell(cell) {
  const ref = rcaEditRefFromCell(cell);
  if (!ref) return false;
  const rec = RCA_EDIT_DOM.editing;
  const original = (rec && rec.cell === cell) ? rec.text0
    : rcaExportCellText(rcaTableEdits.resolveCell(ref.tableId, ref.row, ref.spec)
      .cellValue(ref.spec.model || ref.spec.field, ref.spec));
  RCA_EDIT_DOM.reverting = true;
  rcaEditClearInvalid(cell);
  rcaEditPaintCell(cell, ref, rcaTableEdits.resolveCell(ref.tableId, ref.row, ref.spec)
    .cellValue(ref.spec.model || ref.spec.field, ref.spec));
  rcaEditText(cell, original);
  RCA_EDIT_DOM.editing = null;
  if (typeof cell.blur === 'function') cell.blur();
  RCA_EDIT_DOM.reverting = false;
  return true;
}

// Enter / arrow navigation (Tabulator navigateUp/Down/Left/Right).
function rcaEditNavigate(cell, dRow, dCol) {
  const ref = rcaEditRefFromCell(cell);
  if (!ref) return false;
  const res = rcaEditCommitCell(cell);
  if (!res.ok) return false;          // blocked: focus stays on the bad cell
  const next = rcaEditNeighbourCell(ref, dRow, dCol);
  if (!next) return false;
  return rcaEditFocus(next);
}

// ---- structural re-render -------------------------------------------------

// FE-FIX-2026-09-21 (audit item 2): root.innerHTML rewrites EVERY direct
// child, and the results root hosts nodes this renderer does not own:
//   * #viz-host       — app.js mounts the linked canvas there (rcaRenderResults
//                       never authors it), so a row delete/undo *destroyed*
//                       the chart and its listeners;
//   * #names-verify-slot — authored empty here, but app.js fills it
//                       asynchronously; the swap threw that content away.
// Capture the whitelisted nodes (id + child index) before the write, then put
// them back: a fresh authored placeholder with the same id is REPLACED by the
// captured node (its content survives), anything else is re-inserted at the
// recorded position.
const RCA_EDIT_FOREIGN_SLOT_IDS = { 'viz-host': true, 'names-verify-slot': true };

function rcaEditDomNodeId(node) {
  if (!node) return null;
  if (typeof node.id === 'string' && node.id) return node.id;
  if (typeof node.getAttribute === 'function') {
    const v = node.getAttribute('id');
    return v ? String(v) : null;
  }
  return null;
}

function rcaEditChildList(root) {
  if (!root) return [];
  const kids = (root.children && root.children.length !== undefined)
    ? root.children : root.childNodes;
  return kids ? Array.prototype.slice.call(kids) : [];
}

function rcaEditCaptureForeign(root) {
  const out = [];
  const kids = rcaEditChildList(root);
  for (let i = 0; i < kids.length; i += 1) {
    const id = rcaEditDomNodeId(kids[i]);
    if (id && RCA_EDIT_FOREIGN_SLOT_IDS[id]) out.push({ id: id, node: kids[i], index: i });
  }
  return out;
}

function rcaEditRestoreForeign(root, captured) {
  for (const f of captured) {
    if (!f || !f.node) continue;
    const kids = rcaEditChildList(root);
    let fresh = null;
    for (const k of kids) {
      if (k !== f.node && rcaEditDomNodeId(k) === f.id) { fresh = k; break; }
    }
    try {
      if (root.contains && root.contains(f.node)) continue; // survived the swap
      if (fresh && typeof root.replaceChild === 'function') {
        root.replaceChild(f.node, fresh);
      } else if (typeof root.insertBefore === 'function') {
        root.insertBefore(f.node, kids[Math.min(f.index, kids.length)] || null);
      } else if (typeof root.appendChild === 'function') {
        root.appendChild(f.node);
      }
    } catch (_e) { /* exotic DOMs: the next full render recreates the slot */ }
  }
}

function rcaEditRerender() {
  const root = RCA_EDIT_DOM.root;
  if (!root) return false;
  const opts = RCA_EDIT_DOM.opts || {};
  if (typeof opts.rerender === 'function') return opts.rerender(root, rcaTableEdits.live()) !== false;
  if (typeof rcaRenderResults !== 'function') return false;
  const foreign = rcaEditCaptureForeign(root);
  root.innerHTML = rcaRenderResults(rcaTableEdits.live(), RCA_EDIT_DOM.rawText, { editable: true });
  rcaEditRestoreForeign(root, foreign);
  rcaEditSyncSelectionDom(root);
  rcaEditSyncDirtyDom(root);
  // FE-FIX-2026-09-21 (audit item 8): the freshly rendered toolbar carries
  // the hardcoded `disabled` undo/redo buttons (a FRESH result always has an
  // empty stack), but rcaEditRerender runs on EVERY row add/delete — and the
  // stack that grew past them. history.js's rcaHistorySyncButtons is the
  // single source for the enabled state; re-run it after the swap
  // (typeof-guarded so the editor works without js/history.js).
  if (typeof globalThis !== 'undefined' && typeof globalThis.rcaHistorySyncButtons === 'function') {
    try { globalThis.rcaHistorySyncButtons(root); } catch (_e) { /* no stack: leave as rendered */ }
  }
  return true;
}

function rcaEditSyncDirtyDom(root) {
  const scope = root || RCA_EDIT_DOM.root;
  if (!scope) return 0;
  let n = 0;
  for (const tableId of rcaTableEdits.dirtyTables()) {
    for (const pair of rcaTableEdits.getEditedCells(tableId)) {
      const field = pair[1];
      const cfg = rcaTableEdits.cfg(tableId);
      if (!cfg) continue;
      const spec = rcaTableEdits.specByField(tableId, field);
      if (!spec) continue;
      const col = cfg.edit.indexOf(spec);
      const el = rcaEditCellEl(tableId, pair[0], col);
      if (el) { rcaEditClass(el, 'rca-cell-dirty', true); n += 1; }
    }
  }
  return n;
}

// ---- selection ------------------------------------------------------------

function rcaEditSelectionCount(tableId) {
  return rcaTableEdits.selectedRows(tableId).length;
}

function rcaEditUpdateSelectionBar(tableId) {
  const root = RCA_EDIT_DOM.root;
  if (!root) return;
  const bar = rcaEditOne(root, '[data-selection-bar="' + tableId + '"]');
  if (!bar) return;
  const n = rcaEditSelectionCount(tableId);
  const count = rcaEditOne(bar, '[data-selection-count="' + tableId + '"]');
  rcaEditText(count, String(n));
  if (typeof bar.setAttribute === 'function') {
    if (n > 0) bar.removeAttribute('hidden');
    else bar.setAttribute('hidden', '');
  }
  rcaEditAnnounce(rcaEditT('edit.selectedCount', { n: n }));
}

// `extraTables` names tables whose bucket is ALREADY gone. clearSelection()
// deletes the key, and the loop below only walks the tables that still have
// one — without it 清除所选 would leave the bar visible with its checkboxes
// still ticked (the delete path escapes this because it re-renders the whole
// table straight afterwards, which paints a fresh, empty bar).
function rcaEditSyncSelectionDom(root, extraTables) {
  const scope = root || RCA_EDIT_DOM.root;
  if (!scope) return;
  const ids = Object.keys(rcaTableEdits._selection);
  for (const tableId of (extraTables || [])) {
    if (tableId && ids.indexOf(tableId) === -1) ids.push(tableId);
  }
  for (const tableId of ids) {
    const picked = {};
    for (const row of rcaTableEdits.selectedRows(tableId)) picked[row] = true;
    rcaEditAll(scope, '[data-row-select="' + tableId + '"]').forEach((box) => {
      const row = rcaPyInt(rcaEditAttr(box, 'data-row'));
      box.checked = !!(row !== null && picked[row]);
    });
    // FE-FIX-2026-09-21 (audit item 19): css/table-edit.css styles
    // `tr.rca-row-selected` (and app-ux the hover), but table.js never
    // applied the class — a ticked checkbox had no row highlight until the
    // full re-render dropped even the tick. Toggle it from the same `picked`
    // map that drives the checkboxes.
    rcaEditAll(scope, '[data-table="' + tableId + '"] tr[data-row]').forEach((tr) => {
      const row = rcaPyInt(rcaEditAttr(tr, 'data-row'));
      rcaEditClass(tr, 'rca-row-selected', !!(row !== null && picked[row]));
    });
    const all = rcaEditOne(scope, '[data-select-all="' + tableId + '"]');
    if (all) {
      const nRows = rcaRowsForTable(rcaTableEdits.live(), tableId).length;
      all.checked = nRows > 0 && Object.keys(picked).length >= nRows;
    }
    rcaEditUpdateSelectionBar(tableId);
  }
}

// The bar's three actions. 删除所选 pushes ONE rowDelete action per row so a
// single Ctrl+Z walks back through them one row at a time (an undo stack that
// swallowed a 20-row delete as one step would be unreviewable).
function rcaEditSelectionAction(action, tableId) {
  const rows = rcaTableEdits.selectedRows(tableId);
  if (!rows.length) return { ok: false, reason: 'empty' };
  if (action === 'clear') {
    rcaTableEdits.clearSelection(tableId);
    rcaEditSyncSelectionDom(null, [tableId]);
    return { ok: true, action: 'clear' };
  }
  if (action === 'export') {
    const payload = rcaTableEdits.exportRows(tableId, rows);
    const opts = RCA_EDIT_DOM.opts || {};
    if (typeof opts.onExport === 'function') {
      opts.onExport(tableId, rows, payload);
    } else {
      rcaEditDownloadSelected(tableId, payload);
    }
    return { ok: true, action: 'export', payload: payload };
  }
  if (action === 'delete') {
    if (!rcaTableEdits.canDeleteRows(tableId)) return { ok: false, reason: 'nested' };
    for (let i = rows.length - 1; i >= 0; i -= 1) {
      const act = rcaTableEdits.deleteRow(tableId, rows[i]);
      if (act) rcaEditPublishAction(act);
    }
    rcaTableEdits.clearSelection(tableId);
    rcaEditAnnounce(rcaEditT('edit.rowsDeleted', { n: rows.length }));
    rcaEditRerender();
    return { ok: true, action: 'delete', count: rows.length };
  }
  return { ok: false, reason: 'unknown' };
}

function rcaEditDownloadSelected(tableId, payload) {
  if (!payload || !payload.headers) return false;
  if (typeof rcaToCsv !== 'function' || typeof rcaDownload !== 'function') return false;
  rcaDownload('rca-' + tableId + '-selected.csv',
    rcaToCsv(payload.headers, payload.rows), 'text/csv;charset=utf-8');
  return true;
}

// ---- viz guards -----------------------------------------------------------
//
// js/viz.js owns the linked canvas; when it is absent (unit tests, a build
// without the canvas, a table-only view) every call here is a silent no-op —
// the editor must never break because the chart is missing.
function rcaVizNs() {
  const ns = (typeof globalThis !== 'undefined' && globalThis.rcaViz)
    ? globalThis.rcaViz : ((typeof window !== 'undefined' && window.rcaViz) ? window.rcaViz : null);
  return ns && typeof ns === 'object' ? ns : null;
}

function rcaVizCall(method, arg) {
  const ns = rcaVizNs();
  if (!ns || typeof ns[method] !== 'function') return false;
  try { return !!ns[method](arg); } catch (_e) { return false; }
}

// FE-FIX-2026-09-21 (audit item 10): these guards were named EXACTLY like
// js/viz.js's own globals (rcaVizFocusRow / rcaVizClearFocus / rcaVizLocateTo).
// Plain global scripts — whichever file loaded last won, and callers got the
// wrong semantics (viz.js's versions touch the chart directly and can throw
// when the chart is absent; these are the never-throw namespace calls). The
// table.js copies are now table.js-private names.
function rcaEditVizFocus(idx) { return rcaVizCall('focusRow', idx); }
function rcaEditVizClearFocus() { return rcaVizCall('clearFocus', null); }
function rcaEditVizLocateTo(idx) { return rcaVizCall('locateTo', idx); }

// ---- row highlight / locate ----------------------------------------------

let RCA_TABLE_HIGHLIGHT_ROW = null;

// Public: paint the "this row is being reviewed" state. `idx` is the row index
// inside `tableId` (defaults to the table the user last touched). app.js and
// js/viz.js's own hover callback both go through here, so the table and the
// canvas can never disagree about which row is lit.
function rcaTableHighlightRow(idx, tableId) {
  const root = RCA_EDIT_DOM.root;
  const tid = tableId || RCA_EDIT_DOM.lastTable
    || (root ? (rcaTableEdits.dirtyTables()[0] || (rcaTableConfigs(rcaTableEdits.live())[0] || {}).id) : null);
  if (!root || !tid) return false;
  const prev = rcaEditOne(root, '[data-table="' + tid + '"] '
    + 'tr[data-row="' + (RCA_TABLE_HIGHLIGHT_ROW === idx ? '' : RCA_TABLE_HIGHLIGHT_ROW) + '"]');
  if (RCA_TABLE_HIGHLIGHT_ROW !== null && RCA_TABLE_HIGHLIGHT_ROW !== undefined) {
    const old = rcaEditRowEl(tid, RCA_TABLE_HIGHLIGHT_ROW);
    rcaEditClass(old, 'rca-row-active', false);
  } else if (prev) {
    rcaEditClass(prev, 'rca-row-active', false);
  }
  if (idx === null || idx === undefined) {
    RCA_TABLE_HIGHLIGHT_ROW = null;
    rcaEditVizClearFocus();
    return true;
  }
  const row = rcaEditRowEl(tid, idx);
  rcaEditClass(row, 'rca-row-active', true);
  RCA_TABLE_HIGHLIGHT_ROW = idx;
  RCA_EDIT_DOM.lastTable = tid;
  rcaEditScrollIntoView(row);
  rcaEditVizFocus(idx);
  return !!row;
}

// 定位 button / row focus: pin the focus AND scroll the canvas to that bar.
function rcaEditLocateRow(tableId, idx) {
  RCA_EDIT_DOM.lastTable = tableId;
  rcaTableHighlightRow(idx, tableId);
  const ok = rcaEditVizLocateTo(idx);
  if (!ok) rcaEditAnnounce(rcaEditT('edit.noViz'));
  return ok;
}

// ---- J / K: hop between the rows that need proofreading -------------------

function rcaEditLowConfidenceRows(tableId) {
  return rcaTableLowConfidenceRows(rcaTableEdits.live(), tableId);
}

// dir: +1 next, -1 previous. Focuses the row's first editable cell so the
// keyboard lands straight in the field that needs checking.
function rcaEditStepLowConfidence(tableId, dir) {
  const cfg = rcaTableEdits.cfg(tableId);
  if (!cfg) return false;
  const list = rcaEditLowConfidenceRows(tableId);
  const current = RCA_EDIT_DOM.reviewRow !== undefined && RCA_EDIT_DOM.reviewRow !== null
    ? RCA_EDIT_DOM.reviewRow : -1;
  const next = rcaNextLowConfidenceRow(list, current, dir);
  if (next < 0) return false;
  RCA_EDIT_DOM.reviewRow = next;
  rcaTableHighlightRow(next, tableId);
  const col = rcaEditFirstCol(cfg);
  const cell = col === null ? null : rcaEditCellEl(tableId, next, col);
  if (cell) rcaEditFocus(cell);
  return true;
}

// ---- history bridge -------------------------------------------------------

function rcaEditHistoryNs() {
  const ns = (typeof globalThis !== 'undefined' && globalThis.rcaHistory)
    ? globalThis.rcaHistory : ((typeof window !== 'undefined' && window.rcaHistory) ? window.rcaHistory : null);
  return ns && typeof ns.push === 'function' ? ns : null;
}

// An action has already been applied to the live data by rcaTableEdits; all
// that is left is to remember it. Without js/history.js the editor still
// works, it just cannot step back.
function rcaEditPublishAction(action) {
  const opts = RCA_EDIT_DOM.opts || {};
  if (typeof opts.onAction === 'function') {
    try { opts.onAction(action); } catch (_e) { /* a bad hook must not lose the edit */ }
  }
  const hist = rcaEditHistoryNs();
  if (hist) hist.push(action);
  else rcaEditAnnounce(rcaEditT('edit.editsPending', { n: rcaTableEdits.editedCount() }));
  return !!hist;
}

// ---- keyboard -------------------------------------------------------------

// Returns true when the event was consumed. Exported (and tested) as a pure
// function: the stub hands it a fake event, no browser needed.
function rcaEditHandleKey(ev) {
  if (!ev) return false;
  const target = ev.target || null;
  const key = ev.key || '';
  const cell = rcaEditRefFromCell(target) ? target : null;

  if (cell) {
    if (key === 'Enter' && !ev.shiftKey) { ev.preventDefault && ev.preventDefault(); return rcaEditNavigate(cell, +1, 0); }
    if (key === 'Enter' && ev.shiftKey) { ev.preventDefault && ev.preventDefault(); return rcaEditNavigate(cell, -1, 0); }
    if (key === 'Escape' || key === 'Esc') { ev.preventDefault && ev.preventDefault(); return rcaEditRevertCell(cell); }
    if (key === 'ArrowDown') { ev.preventDefault && ev.preventDefault(); return rcaEditNavigate(cell, +1, 0); }
    if (key === 'ArrowUp') { ev.preventDefault && ev.preventDefault(); return rcaEditNavigate(cell, -1, 0); }
    if (key === 'ArrowRight') { ev.preventDefault && ev.preventDefault(); return rcaEditNavigate(cell, 0, +1); }
    if (key === 'ArrowLeft') { ev.preventDefault && ev.preventDefault(); return rcaEditNavigate(cell, 0, -1); }
    return false;
  }

  // Outside a cell: J/K walk the review rows of the focused table container.
  const nav = rcaEditClosest(target, '[data-table-nav]');
  const tableId = nav ? rcaEditAttr(nav, 'data-table-nav') : null;
  if (tableId && (key === 'j' || key === 'J')) { ev.preventDefault && ev.preventDefault(); return rcaEditStepLowConfidence(tableId, +1); }
  if (tableId && (key === 'k' || key === 'K')) { ev.preventDefault && ev.preventDefault(); return rcaEditStepLowConfidence(tableId, -1); }
  return false;
}

// ---- delegated handlers ---------------------------------------------------

function rcaEditOnFocusIn(ev) {
  const target = ev && ev.target;
  const cell = rcaEditRefFromCell(target) ? target : null;
  if (cell) {
    if (!RCA_EDIT_DOM.editing || RCA_EDIT_DOM.editing.cell !== cell) {
      RCA_EDIT_DOM.editing = { tableId: rcaEditAttr(cell, 'data-table'),
        row: rcaPyInt(rcaEditAttr(cell, 'data-row')),
        col: rcaPyInt(rcaEditAttr(cell, 'data-col')),
        cell: cell, text0: rcaEditText(cell) };
    }
    RCA_EDIT_DOM.lastTable = RCA_EDIT_DOM.editing.tableId;
    RCA_EDIT_DOM.reviewRow = RCA_EDIT_DOM.editing.row;
    rcaTableHighlightRow(RCA_EDIT_DOM.editing.row, RCA_EDIT_DOM.editing.tableId);
    return;
  }
  const locate = rcaEditClosest(target, '.rca-locate-btn');
  if (locate) {
    const parts = (rcaEditAttr(locate, 'data-rca-locate') || '').split(':');
    const idx = rcaPyInt(parts[1]);
    if (parts[0] && idx !== null) rcaTableHighlightRow(idx, parts[0]);
  }
}

function rcaEditOnFocusOut(ev) {
  const cell = rcaEditRefFromCell(ev && ev.target) ? ev.target : null;
  if (!cell) return;
  // A failed validation LOCKS the focus (Tabulator's blocked editor): hand the
  // caret straight back and leave the red frame up.
  if (RCA_EDIT_DOM.invalid === cell) { rcaEditFocus(cell); return; }
  if (RCA_EDIT_DOM.reverting) return;
  // FE-FIX-2026-09-21 (audit item 3): the caret only VISITED this cell — the
  // text is byte-identical to the focus-in snapshot. Before the placeholder
  // fix such a blur committed the '-' that used to sit inside the
  // contenteditable; for a numeric column that read as "此列需要数值：-" and
  // re-focused the cell, an inescapable loop over untouched cells. A no-op
  // blur now just ends the edit record.
  const rec = RCA_EDIT_DOM.editing;
  if (rec && rec.cell === cell && rcaEditText(cell) === rec.text0) {
    RCA_EDIT_DOM.editing = null;
    return;
  }
  rcaEditCommitCell(cell);
}

function rcaEditOnClick(ev) {
  const target = ev && ev.target;
  if (!target) return;
  const locate = rcaEditClosest(target, '[data-rca-locate]');
  if (locate) {
    const parts = (rcaEditAttr(locate, 'data-rca-locate') || '').split(':');
    const idx = rcaPyInt(parts[1]);
    if (parts[0] && idx !== null) {
      ev.preventDefault && ev.preventDefault();
      rcaEditLocateRow(parts[0], idx);
    }
    return;
  }
  const add = rcaEditClosest(target, '[data-rca-addrow]');
  if (add) {
    ev.preventDefault && ev.preventDefault();
    const tableId = rcaEditAttr(add, 'data-rca-addrow');
    const rows = rcaRowsForTable(rcaTableEdits.live(), tableId);
    const act = rcaTableEdits.addRow(tableId, rows.length);
    if (act) { rcaEditPublishAction(act); rcaEditRerender(); }
    return;
  }
  const sel = rcaEditClosest(target, '[data-rca-sel-action]');
  if (sel) {
    ev.preventDefault && ev.preventDefault();
    const tableId = rcaEditAttr(sel, 'data-rca-sel-table');
    if (tableId) rcaEditSelectionAction(rcaEditAttr(sel, 'data-rca-sel-action'), tableId);
  }
}

function rcaEditOnChange(ev) {
  const target = ev && ev.target;
  if (!target) return;
  const rowBox = rcaEditClosest(target, '[data-row-select]');
  if (rowBox) {
    const tableId = rcaEditAttr(rowBox, 'data-row-select');
    const row = rcaPyInt(rcaEditAttr(rowBox, 'data-row'));
    if (tableId && row !== null) {
      rcaTableEdits.toggleRow(tableId, row, !!rowBox.checked);
      rcaEditUpdateSelectionBar(tableId);
      rcaEditSyncSelectionDom(null, [tableId]);
    }
    return;
  }
  const all = rcaEditClosest(target, '[data-select-all]');
  if (all) {
    const tableId = rcaEditAttr(all, 'data-select-all');
    if (tableId) {
      rcaTableEdits.selectAll(tableId, rcaRowsForTable(rcaTableEdits.live(), tableId), !!all.checked);
      rcaEditUpdateSelectionBar(tableId);
      rcaEditSyncSelectionDom(null, [tableId]);
    }
  }
}

// mouseenter / mouseleave arrive as delegated mouseover / mouseout; the row is
// only "left" when the pointer moves to a node outside it (Tabulator's
// rowMouseEnter/rowMouseLeave, minus per-row listeners).
function rcaEditOnMouseOver(ev) {
  const target = ev && ev.target;
  const rowEl = rcaEditClosest(target, 'tr[data-row]');
  const idx = rowEl ? rcaPyInt(rcaEditAttr(rowEl, 'data-row')) : null;
  if (idx !== null && idx !== RCA_EDIT_DOM.hoverRow) {
    const tableId = rcaEditAttr(rcaEditClosest(rowEl, '[data-table]') || {}, 'data-table');
    RCA_EDIT_DOM.hoverRow = idx;
    if (tableId) RCA_EDIT_DOM.lastTable = tableId;
    rcaEditVizFocus(idx);
    if (RCA_EDIT_DOM.invalid !== target) rcaEditClass(rowEl, 'rca-row-hover', true);
  }
}

function rcaEditOnMouseOut(ev) {
  const target = ev && ev.target;
  const rowEl = rcaEditClosest(target, 'tr[data-row]');
  if (!rowEl) return;
  const idx = rcaPyInt(rcaEditAttr(rowEl, 'data-row'));
  if (idx === null || idx !== RCA_EDIT_DOM.hoverRow) return;
  RCA_EDIT_DOM.hoverRow = null;
  rcaEditClass(rowEl, 'rca-row-hover', false);
  rcaEditVizClearFocus();
}

// FE-FIX-2026-09-21 (audit item 9): a contenteditable WITHOUT a paste guard
// swallows rich HTML — <b> tags, whole <table> fragments from Excel — which
// then leaks into rcaEditText() as mangled model text and into the CSV export.
// Tabulator's editors force plain text; same rule here: cancel the native
// paste and insert clipboardData's text/plain only. The fallbacks (execCommand
// -> Range insertion -> append) keep the caret behaviour in browsers and the
// behaviour testable in the DOM stub, which provides none of the first two.
function rcaEditOnPaste(ev) {
  const target = ev && ev.target;
  if (!target || rcaEditAttr(target, 'data-rca-edit') !== '1') return;
  const cd = ev.clipboardData;
  let text = '';
  try {
    text = (cd && typeof cd.getData === 'function') ? String(cd.getData('text/plain') || '') : '';
  } catch (_e) { text = ''; }
  if (typeof ev.preventDefault === 'function') ev.preventDefault();
  if (!text) return;
  if (typeof document !== 'undefined' && document
      && typeof document.execCommand === 'function') {
    try { if (document.execCommand('insertText', false, text)) return; } catch (_e) { /* fall through */ }
  }
  const win = (typeof window !== 'undefined' && window) ? window : null;
  const sel = win && typeof win.getSelection === 'function' ? win.getSelection() : null;
  if (sel && sel.rangeCount > 0 && typeof sel.getRangeAt === 'function'
      && typeof document !== 'undefined' && document
      && typeof document.createTextNode === 'function') {
    try {
      const range = sel.getRangeAt(0);
      range.deleteContents();
      range.insertNode(document.createTextNode(text));
      return;
    } catch (_e) { /* fall through */ }
  }
  rcaEditText(target, rcaEditText(target) + text);
}

const RCA_EDIT_EVENTS = [
  ['focusin', rcaEditOnFocusIn],
  ['focusout', rcaEditOnFocusOut],
  ['click', rcaEditOnClick],
  ['change', rcaEditOnChange],
  ['keydown', rcaEditHandleKey],
  ['mouseover', rcaEditOnMouseOver],
  ['mouseout', rcaEditOnMouseOut],
  // FE-FIX-2026-09-21 (audit item 9): delegated paste — contenteditable cells
  // must never receive rich HTML from the clipboard.
  ['paste', rcaEditOnPaste],
];

// ---- attach / detach -------------------------------------------------------

function rcaTableEditAttach(root, data, opts) {
  if (!root || typeof root.addEventListener !== 'function') return null;
  const o = opts || {};
  RCA_EDIT_DOM.root = root;
  RCA_EDIT_DOM.opts = o;
  RCA_EDIT_DOM.rawText = o.rawText || '';
  RCA_EDIT_DOM.editing = null;
  RCA_EDIT_DOM.invalid = null;
  RCA_EDIT_DOM.hoverRow = null;
  RCA_EDIT_DOM.reviewRow = null;
  RCA_EDIT_DOM.lastTable = o.tableId || null;
  rcaTableEdits.attach(data, o.cfgs);
  if (RCA_EDIT_DOM.wiredRoot !== root) {
    if (RCA_EDIT_DOM.wiredRoot && typeof RCA_EDIT_DOM.wiredRoot.removeEventListener === 'function') {
      for (const pair of RCA_EDIT_EVENTS) {
        // FE-FIX-2026-09-21 (audit item 15): the SAME capture flag that was
        // used to add must be used to remove — but flag of the PREVIOUS
        // wiring, so overwrite the bookkeeping only after this loop.
        RCA_EDIT_DOM.wiredRoot.removeEventListener(pair[0], pair[1],
          RCA_EDIT_DOM.capture ? true : false);
      }
    }
    for (const pair of RCA_EDIT_EVENTS) {
      root.addEventListener(pair[0], pair[1], o.capture ? true : false);
    }
    RCA_EDIT_DOM.capture = !!o.capture;
    RCA_EDIT_DOM.wiredRoot = root;
    RCA_EDIT_DOM.wired = true;
  } else if (RCA_EDIT_DOM.capture !== !!o.capture) {
    // FE-FIX-2026-09-21 (audit item 15): same root, different capture flag —
    // the old registrations are invisible to removeEventListener unless
    // replayed with the flag they were added under.
    for (const pair of RCA_EDIT_EVENTS) {
      root.removeEventListener(pair[0], pair[1], RCA_EDIT_DOM.capture ? true : false);
    }
    for (const pair of RCA_EDIT_EVENTS) {
      root.addEventListener(pair[0], pair[1], o.capture ? true : false);
    }
    RCA_EDIT_DOM.capture = !!o.capture;
  }
  // Canvas -> table direction: when the viz hovers a bar, light up its row.
  const ns = rcaVizNs();
  if (!RCA_EDIT_DOM.unsubHover && ns && typeof ns.onRowHover === 'function') {
    RCA_EDIT_DOM.unsubHover = ns.onRowHover((idx) => {
      if (idx === null || idx === undefined) {
        const old = rcaEditRowEl(RCA_EDIT_DOM.lastTable, RCA_TABLE_HIGHLIGHT_ROW);
        rcaEditClass(old, 'rca-row-active', false);
        return;
      }
      rcaTableHighlightRow(idx, RCA_EDIT_DOM.lastTable);
    });
  }
  rcaEditSyncSelectionDom(root);
  rcaEditSyncDirtyDom(root);
  return { detach: rcaTableEditDetach, root: root };
}

function rcaTableEditDetach() {
  const root = RCA_EDIT_DOM.root;
  if (root && typeof root.removeEventListener === 'function') {
    // FE-FIX-2026-09-21 (audit item 15): remove with the SAME options object
    // the listeners were added under — the old bare call never matched the
    // capture-phase registrations and left everything wired after "detach".
    for (const pair of RCA_EDIT_EVENTS) {
      root.removeEventListener(pair[0], pair[1], RCA_EDIT_DOM.capture ? true : false);
    }
  }
  if (typeof RCA_EDIT_DOM.unsubHover === 'function') RCA_EDIT_DOM.unsubHover();
  RCA_EDIT_DOM.unsubHover = null;
  RCA_EDIT_DOM.root = null;
  RCA_EDIT_DOM.opts = null;
  RCA_EDIT_DOM.editing = null;
  RCA_EDIT_DOM.invalid = null;
  RCA_EDIT_DOM.wired = false;
  RCA_EDIT_DOM.wiredRoot = null;
  RCA_EDIT_DOM.capture = false;
  rcaTableEdits.detach();
  return true;
}

// Called by js/history.js after an undo/redo re-applied something to the live
// data: repaint what the DOM cannot know about (rows that came / went back).
function rcaTableEditAfterHistory(action, direction) {
  const structural = action && (action.type === 'rowDelete' || action.type === 'rowAdd');
  if (structural) rcaEditRerender();
  else {
    const cfg = rcaTableEdits.cfg(action.tableId);
    const col = cfg && rcaPyInt(action.col);
    const cell = (cfg && col !== null) ? rcaEditCellEl(action.tableId, action.row, col) : null;
    const ref = cell ? rcaEditRefFromCell(cell) : null;
    if (cell && ref) {
      const spec = ref.spec;
      rcaEditPaintCell(cell, ref, rcaTableEdits.resolveCell(ref.tableId, ref.row, spec)
        .cellValue(spec.model || spec.field, spec));
    }
  }
  rcaEditAnnounce((direction === 'redo' ? rcaEditT('edit.redo') : rcaEditT('edit.undo'))
    + ' — ' + rcaEditT('edit.editsPending', { n: rcaTableEdits.editedCount() }));
  return true;
}

// ---- exports (plain globals; no ES-module syntax anywhere) ----------------
if (typeof globalThis !== 'undefined') {
  globalThis.rcaTableEdits = rcaTableEdits;
  globalThis.RCA_EDIT_DOM = RCA_EDIT_DOM;
  globalThis.RCA_EDIT_STRINGS = RCA_EDIT_STRINGS;
  globalThis.RCA_EDIT_I18N_KEYS = RCA_EDIT_I18N_KEYS;
  globalThis.rcaTableEditAttach = rcaTableEditAttach;
  globalThis.rcaTableEditDetach = rcaTableEditDetach;
  globalThis.rcaTableEditAfterHistory = rcaTableEditAfterHistory;
  globalThis.rcaTableHighlightRow = rcaTableHighlightRow;
  globalThis.rcaEditCommitCell = rcaEditCommitCell;
  globalThis.rcaEditHandleKey = rcaEditHandleKey;
  globalThis.rcaEditNavigate = rcaEditNavigate;
  globalThis.rcaEditRevertCell = rcaEditRevertCell;
  globalThis.rcaEditRefFromCell = rcaEditRefFromCell;
  globalThis.rcaEditSelectionAction = rcaEditSelectionAction;
  globalThis.rcaEditStepLowConfidence = rcaEditStepLowConfidence;
  globalThis.rcaEditLocateRow = rcaEditLocateRow;
  globalThis.rcaValidateCell = rcaValidateCell;
  globalThis.rcaCaptureEdits = rcaCaptureEdits;
  globalThis.rcaApplyEdits = rcaApplyEdits;
  globalThis.rcaIsDirtyEdits = rcaIsDirtyEdits;
  globalThis.rcaNewRowTemplate = rcaNewRowTemplate;
  globalThis.rcaColEditSpec = rcaColEditSpec;
  globalThis.rcaT = rcaEditT;
  globalThis.rcaTableT = rcaEditT;
  globalThis.rcaEditT = rcaEditT;
  globalThis.rcaEditLang = rcaEditLang;
  globalThis.rcaEditAnnounce = rcaEditAnnounce;
  globalThis.rcaTableRerender = rcaEditRerender;
  globalThis.rcaTableEditCellEl = rcaEditCellEl;
  globalThis.rcaTableEditRowEl = rcaEditRowEl;
  // FE-FIX-2026-09-21: explicit seams the regression tests drive directly —
  // the Python str(float) mirror (audit item 13), the renamed private
  // str() helper (item 10), the paste guard (item 9), the numeric grammar
  // (item 12) and the capture stash (items 1/5).
  globalThis.rcaPyFloatStr = rcaPyFloatStr;
  globalThis.rcaEditPyStr = rcaEditPyStr;
  globalThis.rcaEditOnPaste = rcaEditOnPaste;
  globalThis.rcaPyParseNumber = rcaPyParseNumber;
  globalThis.rcaEditForeignCapture = rcaEditCaptureForeign;
}







