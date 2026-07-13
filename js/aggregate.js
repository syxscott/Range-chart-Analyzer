// aggregate.js - merge results from multiple extraction runs.
// Mirrors rca_core/aggregate.py so the browser (direct/proxy) path and the
// Python (GUI/backend) path produce the same merged structure.
//
// Pass a keymap that describes which top-level key holds the primary
// rows and which fields form the dedup key. Defaults to the range-chart
// keymap (backward compatible).
'use strict';

function rcaAggNorm(s) {
  if (!s) return '';
  let t = String(s).trim();
  t = t.replace(/\s+sp\.?$/i, '').trim();
  t = t.replace(/\s+cf\.?\s+/i, ' ').trim();
  t = t.replace(/\s+/g, ' ');
  return t.toLowerCase();
}

function rcaAggMode(values) {
  const nonEmpty = values.filter((v) => v && String(v).trim());
  if (nonEmpty.length === 0) return '';
  const counts = new Map();
  for (const v of nonEmpty) counts.set(v, (counts.get(v) || 0) + 1);
  let top = 0;
  for (const c of counts.values()) if (c > top) top = c;
  // H4 parity: when all values are unique (top == 1) tie-break
  // deterministically by sorted order so input order doesn't silently
  // decide the merged string across sessions.
  if (top === 1 && counts.size === nonEmpty.length) {
    const sorted = nonEmpty.slice().sort();
    return sorted[0];
  }
  for (const v of nonEmpty) if (counts.get(v) === top) return v;
  return nonEmpty[0];
}

// Sentinel returned by mergeFieldAcrossRuns to signal the key carries
// no consensus content and should be dropped from the merged row.
const NO_MERGE = Symbol('rca.no_merge');

function mergeScalarField(values) {
  const coerced = [];
  for (const v of values) {
    if (v == null) continue;
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
      const s = String(v).trim();
      if (s) coerced.push(s);
    }
  }
  if (coerced.length === 0) return NO_MERGE;
  return rcaAggMode(coerced);
}

function mergeStructuredField(values) {
  // Union of dict items by stable signature. Mirrors the Python side's
  // _merge_structured_field — first-seen wins on duplicates.
  const seen = new Set();
  const out = [];
  for (const v of values) {
    if (v == null) continue;
    if (!Array.isArray(v)) continue;
    for (const item of v) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      let sig;
      try {
        sig = JSON.stringify(
          Object.keys(item).sort().map((k) => [k, item[k]])
        );
      } catch (_e) {
        continue;
      }
      if (seen.has(sig)) continue;
      seen.add(sig);
      out.push(item);
    }
  }
  return out;
}

// Dispatch across scalar / structured / mixed shapes.
// Mirrors Python `_merge_field_across_runs` so both ends behave identically.
function mergeFieldAcrossRuns(values) {
  const nonNull = values.filter((v) => v != null);
  if (nonNull.length === 0) return '';
  // All non-null values are lists-of-dicts → structured merge.
  if (nonNull.every((v) => Array.isArray(v) && v.every((x) => x && typeof x === 'object' && !Array.isArray(x)))) {
    return mergeStructuredField(values);
  }
  // All non-null values are primitives → scalar merge.
  if (nonNull.every((v) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')) {
    const m = mergeScalarField(values);
    return m === NO_MERGE ? '' : m;
  }
  // Mixed / unknown → fall back to plain string mode.
  return rcaAggMode(nonNull.map(String));
}

// Default keymap for range-chart (backward compat).
const RCA_DEFAULT_KEYMAP = {
  primary: 'species_ranges',
  idKeys: ['section', 'species'],
  strModeFields: ['species', 'section', 'range_base', 'range_top', 'biozone'],
  sortKeys: [['agreement_count', 'desc'], ['species', 'asc']],
  listKeys: ['biozones', 'other_fossils'],
  confidence: 'confidence',
  // Range-chart also has a parallel "sections" list that should be merged
  // (preserved across runs even though it's not the "primary").
  extraSections: 'sections',
};

// Keymap for columnar-section results.
const RCA_COLUMNAR_KEYMAP = {
  primary: 'sections',
  idKeys: ['id', 'group'],
  strModeFields: ['id', 'group', 'coordinates_text', 'thickness_m'],
  listKeys: ['fossil_legend', 'lithology_legend', 'cross_beds'],
  confidence: 'confidence',
  extraSections: null,
};

const RCA_KEYMAP_BY_MODE = {
  range_chart: RCA_DEFAULT_KEYMAP,
  columnar_section: RCA_COLUMNAR_KEYMAP,
};

function emptyFor(km, n) {
  const out = { [km.primary]: [], runs: n };
  for (const k of km.listKeys) out[k] = [];
  out[km.confidence] = 0;
  return out;
}

function mergePrimaryList(runs, km, n) {
  const groups = new Map();
  const order = [];
  for (const r of runs) {
    for (const it of r[km.primary] || []) {
      if (!it || typeof it !== 'object') continue;
      const parts = km.idKeys.map((k) => rcaAggNorm(it[k]));
      if (!parts.some((p) => p)) continue;
      const key = parts.join('');
      if (!groups.has(key)) { groups.set(key, []); order.push(key); }
      groups.get(key).push(it);
    }
  }
  const merged = [];
  for (const key of order) {
    const group = groups.get(key);
    const aggr = { agreement_count: group.length, agreement: group.length + '/' + n };
    // First pass: mode-merge the declared string fields.
    for (const f of km.strModeFields) {
      aggr[f] = rcaAggMode(group.map((g) => g[f]));
    }
    // Second pass: pick up any remaining keys not in strModeFields. For
    // scalars we still call rcaAggMode; for structured fields (lists of
    // dicts, e.g. columnar `lithology_blocks` / `samples` / `age_units`)
    // we union by signature. Calling rcaAggMode on objects would coerce them
    // to '[object Object]' and silently drop every run past the first — that
    // was bug C3.
    for (const g of group) {
      for (const k of Object.keys(g)) {
        if (aggr[k] !== undefined) continue;
        if (k === 'agreement_count' || k === 'agreement') continue;
        const v = g[k];
        if (v == null) continue;
        aggr[k] = mergeFieldAcrossRuns(group.map((x) => x[k]));
      }
    }
    merged.push(aggr);
  }
  // Apply schema sortKeys if defined (e.g. agreement_count desc, species asc).
  const kmSortKeys = km.sortKeys;
  merged.sort((a, b) => {
    if (kmSortKeys && kmSortKeys.length) {
      for (const [field, direction] of kmSortKeys) {
        const av = a[field];
        const bv = b[field];
        let cmp;
        const an = Number(av);
        const bn = Number(bv);
        if (av != null && bv != null && Number.isFinite(an) && Number.isFinite(bn)) {
          cmp = an - bn;
        } else {
          const as = (av != null ? String(av) : '').toLowerCase();
          const bs = (bv != null ? String(bv) : '').toLowerCase();
          cmp = as.localeCompare(bs);
        }
        if (cmp !== 0) return direction === 'desc' ? -cmp : cmp;
      }
      return 0;
    }
    // Fall back to alphabetical on id/species.
    const aKey = (a.id != null ? String(a.id) : (a.species || '')).toLowerCase();
    const bKey = (b.id != null ? String(b.id) : (b.species || '')).toLowerCase();
    return aKey.localeCompare(bKey);
  });
  return merged;
}

function mergeNamedLists(runs, km) {
  const out = {};
  for (const key of km.listKeys) {
    // Detect whether items are plain strings or dicts.
    const allItems = [];
    for (const r of runs) for (const it of r[key] || []) allItems.push(it);
    if (allItems.length > 0 && !allItems.some((x) => x && typeof x === 'object')) {
      // String list: dedup by lowercased value, preserve first-seen order.
      const seen = new Set();
      const merged = [];
      for (const s of allItems) {
        const t = String(s).trim();
        if (t && !seen.has(t.toLowerCase())) {
          seen.add(t.toLowerCase());
          merged.push(t);
        }
      }
      out[key] = merged;
      continue;
    }
    const groups = new Map();
    const order = [];
    for (const r of runs) {
      for (const it of r[key] || []) {
        if (!it || typeof it !== 'object') continue;
        const label = rcaAggNorm(it.name) || rcaAggNorm(it.marker) || rcaAggNorm(it.meaning);
        if (!label) continue;
        if (!groups.has(label)) { groups.set(label, []); order.push(label); }
        groups.get(label).push(it);
      }
    }
    const merged = [];
    for (const label of order) {
      const group = groups.get(label);
      const rep = {};
      for (const g of group) {
        for (const k of Object.keys(g)) {
          if (rep[k] !== undefined) continue;
          const v = g[k];
          if (v == null) continue;
          rep[k] = mergeFieldAcrossRuns(group.map((x) => x[k]));
        }
      }
      if (Object.keys(rep).length === 0 && group[0]) Object.assign(rep, group[0]);
      merged.push(rep);
    }
    out[key] = merged;
  }
  return out;
}

// Merge an array of normalized result objects into one.
// `keymap` is optional — defaults to RCA_DEFAULT_KEYMAP (range-chart).
function rcaMergeResults(results, totalRuns, keymap) {
  const km = keymap || RCA_DEFAULT_KEYMAP;
  const runs = (results || []).filter((r) => r && typeof r === 'object');
  let n = (totalRuns === undefined || totalRuns === null) ? runs.length : totalRuns;
  if (n <= 0) n = 1;

  if (runs.length === 0) {
    // Backward-compat empty shape for range-chart mode.
    if (km === RCA_DEFAULT_KEYMAP || !km) {
      return {
        sections: [], species_ranges: [], biozones: [], other_fossils: [],
        confidence: 0, runs: n,
      };
    }
    return emptyFor(km, n);
  }

  // Single-run passthrough.
  if (runs.length === 1 && (totalRuns === undefined || totalRuns === null || totalRuns === 1)) {
    const single = Object.assign({}, runs[0]);
    const primary = single[km.primary] || [];
    single[km.primary] = primary.map((it) => Object.assign({}, it, { agreement_count: 1, agreement: '1/1' }));
    if (single[km.confidence] === undefined) single[km.confidence] = 0;
    single.runs = 1;
    return single;
  }

  const out = { runs: n };
  out[km.primary] = mergePrimaryList(runs, km, n);
  Object.assign(out, mergeNamedLists(runs, km));

  // Mean confidence.
  let sum = 0, cnt = 0;
  for (const r of runs) {
    const c = Number(r[km.confidence]);
    if (Number.isFinite(c)) { sum += c; cnt++; }
  }
  out[km.confidence] = cnt ? Math.round((sum / cnt) * 10000) / 10000 : 0;

  // Range-chart-only: also merge the parallel "sections" list of measured
  // sections so the original four-table shape is preserved.
  if (km.extraSections && km.extraSections !== km.primary) {
    const secGroups = new Map();
    const order = [];
    for (const r of runs) {
      for (const sec of r[km.extraSections] || []) {
        if (!sec || typeof sec !== 'object') continue;
        const key = rcaAggNorm(sec.name) || 'section';
        if (!secGroups.has(key)) { secGroups.set(key, []); order.push(key); }
        secGroups.get(key).push(sec);
      }
    }
    const merged = [];
    for (const key of order) {
      const group = secGroups.get(key);
      const forms = [];
      for (const g of group) for (const f of g.formations || []) if (f && forms.indexOf(f) < 0) forms.push(f);
      merged.push({
        name: rcaAggMode(group.map((x) => x.name || '')),
        age_range: rcaAggMode(group.map((x) => x.age_range || '')),
        formations: forms,
        formation_thickness_m: rcaAggMode(group.map((x) => x.formation_thickness_m || '')),
        coordinates: rcaAggMode(group.map((x) => x.coordinates || '')),
      });
    }
    out[km.extraSections] = merged;
  }

  return out;
}
