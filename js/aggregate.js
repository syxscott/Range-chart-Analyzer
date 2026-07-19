// aggregate.js - merge results from multiple extraction runs.
// Mirrors rca_core/aggregate.py so the browser (direct/proxy) path and the
// Python (GUI/backend) path produce the same merged structure.
//
// Pass a keymap that describes which top-level key holds the primary
// rows and which fields form the dedup key. Defaults to the range-chart
// keymap (backward compatible).
'use strict';

// B-1 fix: removed the sp./cf./aff. strip — that info is now preserved
// by rcaExtractQualifiers and carried into the dedup key as a separate
// component so "Genus sp." and "Genus" are NOT merged together.
function rcaAggNorm(s) {
  if (!s) return '';
  let t = String(s).trim();
  t = t.replace(/\s+/g, ' ');
  return t.toLowerCase();
}

// B-1 fix: mirror of Python _extract_qualifiers (aggregate.py).
// Returns the set of open-nomenclature qualifiers present in string s.
const _QUALIFIER_RE = [
  [/\s+sp\.?$/i, 'sp'],
  [/\s+cf\.?\s+/i, 'cf'],
  [/\s+aff\.?\s+/i, 'aff'],
  [/\s+\?$/i, 'unidentified'],
];
function rcaExtractQualifiers(s) {
  if (!s) return [];
  const quals = [];
  for (const [re, name] of _QUALIFIER_RE) {
    if (re.test(s)) quals.push(name);
  }
  return quals;
}

function rcaAggMode(values) {
  const nonEmpty = values.filter((v) => v && String(v).trim());
  if (nonEmpty.length === 0) return '';
  const counts = new Map();
  for (const v of nonEmpty) counts.set(v, (counts.get(v) || 0) + 1);
  let top = 0;
  for (const c of counts.values()) if (c > top) top = c;
  // H4 parity: break ANY tie at the top count deterministically by sorted
  // order — not just the all-unique case. This mirrors Python's _mode
  // (aggregate.py), where a partial tie (e.g. [B,B,A,A]) also sorts and
  // takes the first ('A'), instead of falling back to first-seen input
  // order ('B'). Without this the browser (direct/proxy) path and the
  // Python (GUI/backend) path disagree on merged strings across sessions.
  const topVals = [];
  for (const [v, c] of counts) if (c === top) topVals.push(v);
  if (topVals.length === 1) return topVals[0];
  return topVals.slice().sort((a, b) => String(a) < String(b) ? -1 : (String(a) > String(b) ? 1 : 0))[0];
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
  // BUGFIX: was returning '' here, which let empty runs appear as a
  // legitimate "" value in the merged row. Mirror the Python side's
  // NO_MERGE sentinel so callers can drop the key when no run produced
  // any value at all.
  if (nonNull.length === 0) return NO_MERGE;
  // All non-null values are lists-of-dicts → structured merge.
  if (nonNull.every((v) => Array.isArray(v) && v.every((x) => x && typeof x === 'object' && !Array.isArray(x)))) {
    return mergeStructuredField(values);
  }
  // All non-null values are primitives → scalar merge.
  if (nonNull.every((v) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')) {
    const m = mergeScalarField(values);
    return m === NO_MERGE ? NO_MERGE : m;
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

// Keymap for abundance-diagram (pollen / percentage-diagram) results.
// Rows are all-string like range-chart, so no special-case merge is needed;
// the parallel "sites" and "zones" lists are merged as named lists.
const RCA_ABUNDANCE_KEYMAP = {
  primary: 'abundances',
  idKeys: ['site', 'taxon', 'level'],
  strModeFields: ['taxon', 'site', 'level', 'depth', 'abundance', 'abundance_unit'],
  sortKeys: [['agreement_count', 'desc'], ['taxon', 'asc']],
  listKeys: ['sites', 'zones'],
  confidence: 'confidence',
  extraSections: null,
};

const RCA_KEYMAP_BY_MODE = {
  range_chart: RCA_DEFAULT_KEYMAP,
  columnar_section: RCA_COLUMNAR_KEYMAP,
  abundance_diagram: RCA_ABUNDANCE_KEYMAP,
};

// Detect the appropriate keymap from the shape of the first result object.
// Mirrors Python _auto_detect_schema so both ends agree on the schema.
function rcaAutoDetectKeymap(results) {
  if (!results || !Array.isArray(results) || results.length === 0) {
    return RCA_DEFAULT_KEYMAP;
  }
  let abCount = 0;
  let colCount = 0;
  for (const r of results) {
    if (r && Array.isArray(r.abundances) && r.abundances.length > 0) abCount++;
    if (r && Array.isArray(r.sections) && r.sections.length > 0 &&
        r.sections[0] && typeof r.sections[0] === 'object' && 'id' in r.sections[0]) {
      colCount++;
    }
  }
  const n = results.length;
  const half = Math.floor((n + 1) / 2);
  if (colCount >= half && abCount >= half) {
    // Both detectors meet threshold — prefer the more specific one.
    return colCount >= abCount ? RCA_COLUMNAR_KEYMAP : RCA_ABUNDANCE_KEYMAP;
  }
  if (colCount >= half) return RCA_COLUMNAR_KEYMAP;
  if (abCount >= half) return RCA_ABUNDANCE_KEYMAP;
  // Neither detector hit majority.
  if (colCount > abCount) return RCA_COLUMNAR_KEYMAP;
  if (abCount > colCount) return RCA_ABUNDANCE_KEYMAP;
  return RCA_DEFAULT_KEYMAP;  // tie → default to range-chart
}

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
    // Per-run dedup: if a single run emits the same primary row twice
    // (model hiccup), count it once so agreement_count can't exceed n
    // (e.g. "3/2"), which would break consensus filters expecting
    // agreement_count <= total runs. Mirrors the Python
    // _merge_primary_list `seen_in_run` guard (aggregate.py).
    const seenInRun = new Set();
    for (const it of r[km.primary] || []) {
      if (!it || typeof it !== 'object') continue;
      // B-1 fix: include open-nomenclature qualifiers (sp./cf./aff./?)
      // in the dedup key so "Genus sp." and "Genus" stay separate.
      const parts = km.idKeys.map((k) => rcaAggNorm(it[k]));
      if (!parts.some((p) => p)) continue;
      // Extract qualifiers from the species/id fields for the key.
      const speciesRaw = it['species'] || it['id'] || '';
      const quals = rcaExtractQualifiers(speciesRaw);
      const qualsSuffix = quals.length ? '\x1f' + quals.join('|') : '';
      const key = parts.join('') + qualsSuffix;
      if (seenInRun.has(key)) continue;
      seenInRun.add(key);
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
    // B-1 fix: species/taxon mode was computed on _norm()-stripped values
    // (qualifiers lost). Restore the most-common original species/taxon string
    // that produced the mode, so "sp." / "cf." / "aff." are preserved.
    // Use the actual primary ID field (last element of km.idKeys, which is 'species'
    // for range-chart and 'taxon' for abundance-diagram).
    const primaryIdField = km.idKeys[km.idKeys.length - 1];
    const primaryMode = aggr[primaryIdField] || '';
    if (primaryMode) {
      const counter = {};
      for (const g of group) {
        const raw = (g && g[primaryIdField] || '').trim();
        if (!raw) continue;
        if (rcaAggNorm(raw) === primaryMode) {
          counter[raw] = (counter[raw] || 0) + 1;
        }
      }
      if (Object.keys(counter).length > 0) {
        const mostCommon = Object.keys(counter).reduce(
          (a, b) => counter[a] >= counter[b] ? a : b
        );
        const quals = rcaExtractQualifiers(mostCommon);
        if (quals.length > 0) {
          aggr[primaryIdField] = mostCommon;
        }
      }
    }
    // BUGFIX: previous version iterated `for (g of group) for (k of g)`
    // and used `if (aggr[k] !== undefined) continue;` — the first run
    // that produced each key "won" and later runs were silently dropped.
    // Collect (key, all-per-run-values) across the whole group first,
    // then call mergeFieldAcrossRuns once per key. This also makes the
    // NO_MERGE sentinel meaningful: a key that every run reported as
    // null is dropped from the merged row instead of appearing as "".
    const fieldsToMerge = new Map();
    for (const g of group) {
      if (!g || typeof g !== 'object') continue;
      for (const k of Object.keys(g)) {
        if (k === 'agreement_count' || k === 'agreement') continue;
        if (g[k] == null) continue;
        if (!fieldsToMerge.has(k)) fieldsToMerge.set(k, []);
        fieldsToMerge.get(k).push(g[k]);
      }
    }
    for (const [k, _vals] of fieldsToMerge) {
      if (aggr[k] !== undefined) continue;  // already filled by strModeFields
      const perRun = group.map((x) => x ? x[k] : null);
      const merged_v = mergeFieldAcrossRuns(perRun);
      if (merged_v === NO_MERGE) continue;  // drop empty key
      aggr[k] = merged_v;
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
        // B-1 fix: include qualifiers (sp./cf./aff./?) in the dedup
        // label so "N. optima Zone (cf.)" and "N. optima Zone" are not
        // silently collapsed.
        const rawLabel = it.name || it.marker || it.meaning || '';
        let label = rcaAggNorm(rawLabel);
        const quals = rcaExtractQualifiers(rawLabel);
        if (quals.length > 0) label = label + '\x1f' + quals.join('|');
        // B-1 fix (named-list path): fold section into label so the same
        // biozone name in different sections produces separate rows.
        const section = rcaAggNorm(it.section || '');
        if (section && label && !label.startsWith('__nolabel__')) {
          label = label + '\x1f\x1e' + section;
        }
        if (!label) {
          // No name/marker/meaning field (e.g. abundance-diagram single-site
          // with empty name, or columnar cross_beds which key on bed indices).
          // Fall back to a content signature so identical items across runs
          // collapse to one and distinct items are preserved, instead of
          // being silently dropped. Mirrors the Python _merge_named_lists.
          try {
            label = '__nolabel__:' + JSON.stringify(
              Object.keys(it).sort().map((k) => [k, it[k] == null ? '' : String(it[k])])
            );
          } catch (_e) {
            label = '__nolabel__:' + JSON.stringify(Object.keys(it).sort());
          }
        }
        if (!groups.has(label)) { groups.set(label, []); order.push(label); }
        groups.get(label).push(it);
      }
    }
    const merged = [];
    for (const label of order) {
      const group = groups.get(label);
      // BUGFIX: same first-iteration-wins issue as mergePrimaryList —
      // collect (k, perRunValues) across the group, then merge once.
      const rep = {};
      const fieldsToMerge = new Map();
      for (const g of group) {
        if (!g || typeof g !== 'object') continue;
        for (const k of Object.keys(g)) {
          if (g[k] == null) continue;
          if (!fieldsToMerge.has(k)) fieldsToMerge.set(k, []);
          fieldsToMerge.get(k).push(g[k]);
        }
      }
      for (const [k, _vals] of fieldsToMerge) {
        const perRun = group.map((x) => x ? x[k] : null);
        const merged_v = mergeFieldAcrossRuns(perRun);
        if (merged_v === NO_MERGE) continue;
        rep[k] = merged_v;
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
        // Mirror the species merge: include agreement_count + agreement so
        // js/table.js's low-agreement highlight (table.js:237) can fire on
        // the sections list — without these fields the highlight branch
        // is dead code.
        agreement_count: group.length,
        agreement: group.length + '/' + n,
      });
    }
    out[km.extraSections] = merged;
  }

  return out;
}
