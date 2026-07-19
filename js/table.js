// table.js - render extraction result into four data tables + confidence ring.
'use strict';

// HTML-escape a value for safe insertion as text content.
function rcaEsc(value) {
  const s = value === null || value === undefined ? '' : String(value);
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
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
  const hasAbundanceShape = data && Array.isArray(data.abundances);
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
    return [
      {
        id: 'sections',
        titleKey: 'sec.sections',
        cols: secColsFinal,
        italicCol: 0,
        row: secRowFinal,
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

  // -------- range-chart mode (default) --------
  const speciesCols = ['col.species', 'col.section', 'col.rangeBase', 'col.rangeTop', 'col.biozone'];
  const speciesRow = (r) => [r.species, r.section, r.range_base, r.range_top, r.biozone];
  const speciesColsFinal = multi ? speciesCols.concat(['col.agreement']) : speciesCols;
  const speciesRowFinal = multi
    ? (r) => speciesRow(r).concat([r.agreement || ''])
    : speciesRow;
  return [
    {
      id: 'sections',
      titleKey: 'sec.sections',
      cols: ['col.name', 'col.ageRange', 'col.formations', 'col.thickness', 'col.coordinates'],
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
      cols: ['col.name', 'col.age', 'col.thickness'],
      italicCol: -1,
      row: (b) => [b.name, b.age, b.thickness_m],
    },
    {
      id: 'other_fossils',
      titleKey: 'sec.fossils',
      cols: ['col.fossil'],
      italicCol: -1,
      row: (f) => [f],
    },
  ];
}

// Render the whole result. `data` is the normalized result object.
function rcaRenderResults(data, rawText) {
  const configs = rcaTableConfigs(data);
  const parts = [];

  // Confidence ring + global actions toolbar.
  // The ring is an inline SVG: two stacked circles (track + bar) with the
  // bar's stroke-dasharray advancing toward `confPct`. The numeric label
  // inside ticks up from 0 → confPct on first paint.
  const confPct = Math.max(0, Math.min(100, Math.round((data.confidence || 0) * 100)));
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
  parts.push('</div>');
  parts.push('<div class="rt-actions">');
  parts.push('<button type="button" class="btn btn-secondary btn-small" id="btn-export-all">' + rcaEsc(t('results.exportAll')) + '</button>');
  parts.push('</div>');
  parts.push('</div>');

  for (const cfg of configs) {
    const rows = Array.isArray(data[cfg.id]) ? data[cfg.id] : [];
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
      parts.push('<div class="cell-empty" style="padding:8px 2px;">' + rcaEsc(t('results.noRows')) + '</div>');
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
        const ac = Number(item.agreement_count) || 0;
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
        const disp = val.trim() ? rcaEsc(val) : '-';
        parts.push('<td class="' + cls + '">' + disp + '</td>');
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
  const items = Array.isArray(data[tableId]) ? data[tableId] : [];
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
