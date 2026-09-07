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

// M12: HTML-escape for attribute contexts (single-quoted with `&apos;`
// flipping so we wrap data-* attributes consistently). Currently `cfg.id`
// is one of a small hardcoded set of strings, but a future contributor
// should not be able to inject attribute-breaking values.
function rcaEscAttr(value) {
  return rcaEsc(value).replace(/'/g, '&#39;');
}

// Table definitions: key on the result object, i18n title, columns, and a
// row-extractor producing an array of cell values in column order.
// `italic` marks the species column for styling. These configs are shared
// with the export path so CSV/TSV columns match the rendered table exactly.
// When `data.runs > 1` (multi-run merge), the species table gains an
// "agreement" column showing how many runs produced each row.
function rcaTableConfigs(data) {
  const multi = data && Number(data.runs) > 1;
  // Sprint B (REVIEW-2026-09-04): require at least one abundance row, not
  // just `Array.isArray(data.abundances)`. Mirrors rca_core/exporter.py
  // _looks_abundance (exporter.py:186-191): every freshly-normalized result
  // carries an empty `abundances: []` placeholder, so accepting empty lists
  // misroutes pure range-chart results to the abundance renderer/exporter.
  const hasAbundanceShape = data && Array.isArray(data.abundances)
    && data.abundances.length > 0;
  const hasColumnarShape = data
    && Array.isArray(data.sections)
    && !Array.isArray(data.species_ranges)
    && !hasAbundanceShape;

  // -------- abundance-diagram (pollen / percentage) mode --------
  if (hasAbundanceShape) {
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

  // -------- columnar-section mode --------
  if (hasColumnarShape) {
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
  // Mirrors rca_core.exporter._looks_phylogenetic_tree + _phylogenetic_tree_tables.
  // Without this the web frontend could select the mode but rendered four
  // empty range-chart tables for a tree payload (no nodes table existed).
  const hasPhyloShape = data
    && Array.isArray(data.nodes)
    && data.nodes.length > 0
    && data.nodes[0]
    && typeof data.nodes[0] === 'object'
    && 'id' in data.nodes[0]
    && 'parent' in data.nodes[0];

  // -------- phylogenetic-tree mode --------
  if (hasPhyloShape) {
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
  // Mirrors rca_core.exporter._looks_zonation_chart + _zonation_chart_tables.
  const hasZonationShape = data
    && ((Array.isArray(data.correlations) && data.correlations.length > 0)
        || (Array.isArray(data.zonations) && data.zonations.length > 0)
        || (Array.isArray(data.zones) && data.zones.length > 0
            && data.zones[0] && typeof data.zones[0] === 'object'
            && ('rank' in data.zones[0] || 'zonation' in data.zones[0])));

  // -------- zonation-chart mode --------
  if (hasZonationShape) {
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
  // columns based on which optional fields are populated. Mirrors the
  // conditional-column logic in rca_core/exporter.py so a researcher
  // gets `author_year` / `note` / `confidence` columns in their CSV
  // only when the model actually emitted them — not blank CSV columns
  // for every result.
  const speciesBaseCols = ['col.species', 'col.section', 'col.rangeBase', 'col.rangeTop', 'col.biozone'];
  const speciesBaseRow = (r) => [r.species, r.section, r.range_base, r.range_top, r.biozone];
  // (label, predicate) — predicate returns truthy iff AT LEAST ONE row
  // has a meaningful value for this column.
  const speciesOptLabels = ['col.authorYear', 'col.rangeTopBed', 'col.rangeTopIdx',
                            'col.endpointKind', 'col.occurrenceMode',
                            'col.colConfidence', 'col.note'];
  const speciesRowsForPred = Array.isArray(data.species_ranges) ? data.species_ranges : [];
  const speciesOptPreds = [
    (r) => r.author_year,
    (r) => r.range_top_bed,
    (r) => r.range_top_idx != null,
    (r) => r.endpoint_kind && r.endpoint_kind !== 'unknown',
    (r) => r.occurrence_mode && r.occurrence_mode !== 'unknown',
    (r) => r.confidence != null,
    (r) => r.note,
  ];
  const speciesOptCols = [];
  for (let i = 0; i < speciesOptLabels.length; i += 1) {
    const pred = speciesOptPreds[i];
    if (speciesRowsForPred.some(pred)) speciesOptCols.push(speciesOptLabels[i]);
  }
  const speciesOptGetters = {
    'col.authorYear':     (r) => r.author_year || '',
    'col.rangeTopBed':    (r) => r.range_top_bed || '',
    'col.rangeTopIdx':    (r) => r.range_top_idx == null ? '' : String(r.range_top_idx),
    'col.endpointKind':   (r) => r.endpoint_kind || '',
    'col.occurrenceMode': (r) => r.occurrence_mode || '',
    'col.colConfidence':  (r) => r.confidence == null ? '' : String(r.confidence),
    'col.note':           (r) => r.note || '',
  };
  const speciesColsFinal = multi
    ? speciesBaseCols.concat(speciesOptCols, ['col.agreement'])
    : speciesBaseCols.concat(speciesOptCols);
  const speciesRowFinal = (r) => {
    const row = speciesBaseRow(r);
    for (const c of speciesOptCols) row.push(speciesOptGetters[c](r));
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
      // M1 (REVIEW-2026-08-19): other_fossils rows may be a string or
      // a dict (label/species/taxon/name). Normalize to a string before
      // export so CSV / JSON don't carry dict-shaped objects.
      row: (f) => [typeof f === 'object' && f
        ? String(f.label || f.species || f.taxon || f.name || '')
        : String(f || '')],
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
function rcaRowsForTable(data, tableId) {
  if (tableId === 'lithology_blocks' || tableId === 'age_units'
      || tableId === 'samples') {
    return rcaColumnarSubTableRows(data)[tableId];
  }
  return Array.isArray(data && data[tableId]) ? data[tableId] : [];
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
      let rowCls = '';
      if ((cfg.id === 'species_ranges' || cfg.id === 'sections') && data && Number(data.runs) > 1) {
        // UI-REVIEW-2026-09-05: fall back to parsing the "n/m" agreement
        // string when agreement_count is absent. Previously a row carrying
        // agreement "3/3" without agreement_count was treated as count 0
        // and flagged low — a cream warning row right next to a green 3/3
        // pill (contradictory review signals).
        let ac = Number(item.agreement_count);
        if (!Number.isFinite(ac)) {
          const m = String(item.agreement || '').trim().match(/^(\d+)\s*\/\s*(\d+)$/);
          ac = m ? Number(m[1]) : 0;
        }
        const half = Number(data.runs) / 2;
        if (ac <= half) rowCls = ' class="row-low-agreement"';
      }
      parts.push('<tr' + rowCls + '>');
      parts.push('<th class="cell-empty" scope="row">' + (idx + 1) + '</th>');
      const cells = cfg.row(item);
      cells.forEach((cell, ci) => {
        const val = cell === null || cell === undefined ? '' : String(cell);
        const colKey = cfg.cols[ci];
        // Phase C: agreement cell -> colored pill (good/mid/low).
        if (colKey === 'col.agreement' && val.trim()) {
          const m = val.trim().match(/^(\d+)\s*\/\s*(\d+)$/);
          let pillClass = 'pill-low';
          if (m) {
            const k = parseInt(m[1], 10);
            const n = parseInt(m[2], 10);
            // Integer math avoids float rounding: 2/3 = 0.666... < 0.667.
            // good: k >= 2n/3  (k*3 >= n*2)
            // mid:  k >  n/3  (k*3 >  n)   && k < 2n/3
            // low:  k <= n/3  (k*3 <= n)
            if (k * 3 >= n * 2) pillClass = 'pill-good';
            else if (k * 3 > n) pillClass = 'pill-mid';
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
function rcaBuildTableExport(data, tableId) {
  const cfg = rcaTableConfigs(data).find((c) => c.id === tableId);
  if (!cfg) return { headers: [], rows: [] };
  const headers = [t('col.index')].concat(cfg.cols.map((c) => t(c)));
  const nCols = cfg.cols.length;
  // Sprint B (REVIEW-2026-09-04): same row source as the renderer
  // (rcaRowsForTable). The old stash-based lookup
  // (data._lithology_blocks_rows / ...) only worked when rcaTableConfigs
  // had mutated `data` beforehand and leaked internal keys into JSON export.
  const items = rcaRowsForTable(data, tableId);
  // M11: pad/truncate each row to cfg.cols.length so a future custom row
  // extractor can't silently misalign columns between headers and rows on
  // a CSV / Excel paste.
  const rows = items.map((item, idx) => {
    const raw = cfg.row(item).map((v) => (v === null || v === undefined ? '' : String(v)));
    const padded = raw.slice(0, nCols);
    while (padded.length < nCols) padded.push('');
    return [String(idx + 1)].concat(padded);
  });
  return { headers, rows };
}


// UI-Polish Phase 5: viz mount-point hook. Currently a no-op.
// Reserved for a future horizontal-Gantt visualization of species ranges
// (each species_ranges row -> { name, value: [sectionIdx, range_base, range_top], biozone };
//  data.sections becomes the Y-axis categories).
// When implementing, export this function on globalThis and call from
// rcaRenderResults() after the table HTML is built. The host element
// already exists in index.html but is `hidden` until first invocation.
function rcaRenderViz(data) {
  if (!data) return;
  var host = (typeof document !== "undefined")
    ? document.getElementById("viz-host")
    : null;
  if (!host) return;
  // No-op: future ECharts/Plotly init goes here.
  host.textContent = "";
}
if (typeof globalThis !== "undefined") globalThis.rcaRenderViz = rcaRenderViz;
