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
      italicCol: -1,
      row: (s) => [s.name, s.location, s.age_range, s.depth_unit],
    },
    {
      id: 'abundances',
      titleKey: 'sec.abundances',
      cols: abColsFinal,
      italicCol: 0,
      row: abRowFinal,
    },
    {
      id: 'zones',
      titleKey: 'sec.zones',
      cols: ['col.name', 'col.age', 'col.levelRange'],
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
      italicCol: 0,
      row: secRowFinal,
    },
    {
      id: 'lithology_blocks',
      titleKey: 'sec.lithologyBlocks',
      cols: ['col.secId', 'col.pattern', 'col.topIdx', 'col.baseIdx'],
      italicCol: -1,
      row: (r) => [r.section_id, r.pattern,
        r.top_idx == null ? '' : String(r.top_idx),
        r.base_idx == null ? '' : String(r.base_idx)],
    },
    {
      id: 'age_units',
      titleKey: 'sec.ageUnits',
      cols: ['col.secId', 'col.label', 'col.topIdx', 'col.baseIdx'],
      italicCol: -1,
      row: (r) => [r.section_id, r.label,
        r.top_idx == null ? '' : String(r.top_idx),
        r.base_idx == null ? '' : String(r.base_idx)],
    },
    {
      id: 'samples',
      titleKey: 'sec.samples',
      cols: ['col.secId', 'col.bedIdx', 'col.fossilMarker', 'col.ref'],
      italicCol: -1,
      row: (r) => [r.section_id,
        r.bed_idx == null ? '' : String(r.bed_idx),
        r.fossil_marker || '', r.ref || ''],
    },
    {
      id: 'fossil_legend',
      titleKey: 'sec.fossils', // reuse fossils title to keep the keyset small
      cols: ['col.fossilMarker', 'col.fossilMeaning'],
      italicCol: -1,
      row: (it) => [it.marker || '', it.meaning || ''],
    },
    {
      id: 'lithology_legend',
      titleKey: 'sec.columnarLithology',
      cols: ['col.lithologyPattern', 'col.lithologyMeaning'],
      italicCol: -1,
      row: (it) => [it.pattern || it.marker || '', it.meaning || ''],
    },
    {
      id: 'cross_beds',
      titleKey: 'sec.crossBeds',
      cols: ['col.crossFrom', 'col.crossFromBed', 'col.crossTo', 'col.crossToBed'],
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
  // [column label, getter] — the getter doubles as the "does ANY row
  // populate this column?" predicate, exactly like the Python tuple list.
  const speciesOpt = [
    ['col.authorYear', (r) => optText(r.author_year)],
    ['col.rangeTopBed', (r) => optText(r.range_top_bed)],
    ['col.rangeTopIdx', (r) => optText(r.range_top_idx)],
    ['col.endpointKind', (r) => (r.endpoint_kind === null || r.endpoint_kind === undefined
      || r.endpoint_kind === 'unknown' ? '' : String(r.endpoint_kind))],
    ['col.occurrenceMode', (r) => (r.occurrence_mode === null || r.occurrence_mode === undefined
      || r.occurrence_mode === 'unknown' ? '' : String(r.occurrence_mode))],
    ['col.colConfidence', (r) => optText(r.confidence)],
    ['col.note', (r) => optText(r.note)],
  ];
  // Non-dict rows are skipped for the predicates only (exporter.py:325
  // `if isinstance(r, dict)`) — the row renderers still show them.
  const speciesRowsForPred = (Array.isArray(data.species_ranges)
    ? data.species_ranges
    : []).filter((r) => r !== null && typeof r === 'object' && !Array.isArray(r));
  const speciesOptCols = [];
  const speciesOptGetters = [];
  for (const [label, getter] of speciesOpt) {
    if (speciesRowsForPred.some((r) => getter(r))) {
      speciesOptCols.push(label);
      speciesOptGetters.push(getter);
    }
  }
  const speciesColsFinal = multi
    ? speciesBaseCols.concat(speciesOptCols, ['col.agreement'])
    : speciesBaseCols.concat(speciesOptCols);
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
      italicCol: -1,
      row: (b) => [b.name, b.section, b.age, b.thickness_m],
    },
    {
      id: 'other_fossils',
      titleKey: 'sec.fossils',
      cols: ['col.fossil'],
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
  for (const sec of (data && data.sections) || []) {
    const sid = sec && sec.id ? String(sec.id) : '';
    for (const b of (sec && sec.lithology_blocks) || []) {
      lithology_blocks.push({
        section_id: sid,
        pattern: b && b.pattern ? String(b.pattern) : '',
        top_idx: b ? b.range_top_idx : null,
        base_idx: b ? b.range_base_idx : null,
      });
    }
    for (const u of (sec && sec.age_units) || []) {
      age_units.push({
        section_id: sid,
        label: u && u.label ? String(u.label) : '',
        top_idx: u ? u.range_top_idx : null,
        base_idx: u ? u.range_base_idx : null,
      });
    }
    for (const s of (sec && sec.samples) || []) {
      samples.push({
        section_id: sid,
        bed_idx: s ? s.bed_idx : null,
        fossil_marker: s && s.fossil_marker ? String(s.fossil_marker) : '',
        ref: s && s.ref ? String(s.ref) : '',
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

function rcaExportCellText(value) {
  if (value === null || value === undefined) return '';
  // Python's `str(True)` is 'True', not JS's 'true' — a boolean that reaches
  // a cell (an un-normalized model field) must read the same in the browser
  // panel, the browser CSV and the GUI grid / workbook.
  if (typeof value === 'boolean') return value ? 'True' : 'False';
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
function rcaRenderResults(data, rawText) {
  const configs = rcaTableConfigs(data);
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
    parts.push('<div class="result-section" data-table="' + safeId + '">');
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
    parts.push('<div class="table-wrap"><table class="data-table" aria-label="' + tblLabel + '"><thead><tr>');
    parts.push('<th scope="col">' + rcaEsc(t('col.index')) + '</th>');
    for (const c of cfg.cols) {
      parts.push('<th scope="col">' + rcaEsc(t(c)) + '</th>');
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
          rowCls = ' class="row-low-agreement"';
        }
      }
      parts.push('<tr' + rowCls + '>');
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
        parts.push('<td class="' + cls + '"' + titleAttr + '>' + disp + '</td>');
      });
      parts.push('</tr>');
    });
    parts.push('</tbody></table></div>');
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
