// aggregate.js - merge results from multiple extraction runs.
// Mirrors rca_core/aggregate.py so the browser (direct/proxy) path and the
// Python (GUI/backend) path produce the same merged structure.
//
// BORROW-2026-09-20 (A+B): the coverage contract (response_kind /
// reason_codes) and the geometry sidecar are merged by their own rules, which
// live in js/reason-codes.js — load that file BEFORE this one (index.html).
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

// F-10 fix: mirror of Python _norm_iczn_author (aggregate.py:54).
// Normalizes "Smith, 1950", "(Smith, 1950)", "Smith 1950", "Smith,1950"
// to the same canonical form so they dedup together; year is preserved
// as a separate suffix so "Smith, 1950" and "Smith, 1960" stay distinct.
function rcaNormIcbnAuthor(s) {
  if (!s) return '';
  let t = String(s).trim().toLowerCase();
  // Strip the year part first so we can normalize the author separately.
  const yearMatch = t.match(/(\d{4})/);
  const year = yearMatch ? yearMatch[1] : '';
  // Remove the year from the working string.
  let author = t.replace(/\d{4}/g, '');
  // H-7 parity (REVIEW-2026-07-31): em-dash (— or --) is an author
  // separator per ICZN Art. 51.2 — "Smith—Jones" must dedup with
  // "Smith, Jones".
  author = author.replace(/—+/g, ' ');
  author = author.replace(/--+/g, ' ');
  // H-7 parity: "Smith ex Jones" / "Smith in Jones" cite Jones as the
  // original; only the primary author(s) are retained for dedup.
  author = author.replace(/\s+ex\s+\S+(\s+\S+)*/g, '');
  author = author.replace(/\s+in\s+\S+(\s+\S+)*/g, '');
  // Strip ICZN-style punctuation: commas, parentheses, ampersands,
  // multiple spaces, "et", "al.", "&".
  let norm = author.replace(/&/g, ' ').replace(/\band\b/g, ' ');
  norm = norm.replace(/[(),.;:'"`]/g, ' ');
  norm = norm.replace(/\bet\.?\s+al\.?\b/g, '');  // "et al."
  norm = norm.replace(/\s+/g, ' ').trim();
  return year ? norm + '|' + year : norm;
}

// F-13 fix: Python repr()-compatible serialization for structured dedup keys.
// Python repr produces: True/False (not true/false), None (not null),
// quoted strings, bare numbers. JSON.stringify produces different strings
// for the same logical content, causing duplicate detection to fail.
function rcaStructDedupKey(item) {
  // Keys sorted, every value coerced through the same str()-style encoder so
  // {"a": 8} and {"a": "8"} collapse exactly as they do in Python.
  //
  // REVIEW-2026-09-10: the value encoder used to be `String(v)` for objects,
  // which yields the constant "[object Object]" - nested content was
  // invisible to the key, so two runs whose lithology_blocks differed only by
  // an _extras value (e.g. a different note) collapsed into ONE block and the
  // second was silently discarded.
  const entries = Object.keys(item).sort().map(
    (k) => rcaStructDedupScalar(k) + ': ' + rcaStructDedupScalar(item[k]));
  return entries.join(', ');
}

// Python's _struct_dedup_key builds
//     repr(sorted(((k, str(val)) for k, val in item.items())))
// i.e. the item's own keys are SORTED, every value is coerced with str()
// (so {"a": 8} and {"a": "8"} collapse - the P1-11 fix), and a NESTED dict or
// list renders as its Python repr with insertion order preserved. This helper
// reproduces that value coercion for JS; the caller sorts the top-level keys.
function rcaStructDedupScalar(v) {
  if (Array.isArray(v)) return '[' + v.map(rcaStructDedupScalar).join(', ') + ']';
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v).map(
      (k) => rcaStructDedupScalar(k) + ': ' + rcaStructDedupScalar(v[k])).join(', ') + '}';
  }
  if (typeof v === 'string') return v;          // str("x") == "x"
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  if (v === null || v === undefined) return 'None';
  return String(v);
}

// P0-6: mirror of Python _QUALIFIER_PATTERNS (aggregate.py).
// 10 common ICZN open-nomenclature markers. Long patterns before short ones.
const _QUALIFIER_RE = [
  [/\bex\s+gr(oup)?\.?\b/i, 'ex gr.'],
  [/\bs\.?\s*l\.?\b/i, 's.l.'],
  [/\bs\.?\s*str\.?\b/i, 's.str.'],
  [/\bsp\.?\b/i, 'sp.'],
  [/\bspp\.?\b/i, 'spp.'],
  // REVIEW-2026-09-20: cf./aff. used to require TRAILING WHITESPACE
  // (`cf\.?\s+`), so the very common trailing-suffix forms "Genus cf." /
  // "Genus aff." — nothing after the marker — never matched and the specimen
  // was deduped together with the identified "Genus". `\b` after the optional
  // dot also matches at the end of the string, and still rejects look-alikes
  // ("coffee", "affinis"). Mirrors rca_core/aggregate.py:_QUALIFIER_PATTERNS.
  [/\bcf\.?\b/i, 'cf.'],
  [/\baff\.?\b/i, 'aff.'],
  [/\?\s*$/i, '?'],
  // The label carries a Python-style `\1` backreference placeholder;
  // rcaExtractQualifiers resolves it from match group 1 so "nom. dub" is
  // emitted, not the literal "nom. $1" the old JS produced.
  [/\bnom\.?\s+(dub|nud|nov|cons|obl|rej|van)\b/i, 'nom. \\1'],
  [/\bcomb\.?\s+nov\.?\b/i, 'comb. nov.'],
  [/\bstat\.?\s+nov\.?\b/i, 'stat. nov.'],
  [/\bsubsp\.?\b/i, 'subsp.'],
  [/\bvar\.?\b/i, 'var.'],
];
function rcaExtractQualifiers(s) {
  if (!s) return [];
  // Mirrors Python _extract_qualifiers: at most one label per pattern, and the
  // result behaves like a frozenset (duplicates folded out).
  const quals = [];
  for (const [re, name] of _QUALIFIER_RE) {
    const m = re.exec(String(s));
    if (!m) continue;
    const label = name.indexOf('\\1') !== -1
      ? name.split('\\1').join(m[1] === undefined ? '' : m[1])
      : name;
    if (quals.indexOf(label) === -1) quals.push(label);
  }
  return quals;
}

// Mirror of rca_core/aggregate.py:_str_for_merge. JSON has no int/float
// distinction, so JS ``String(1.0)`` already renders "1" exactly like the
// Python side's integral-float branch; booleans keep the Python spelling so
// "True"/"False" never collide with a real string in the vote pool.
// KNOWN residual divergences (documented, not fixable in JS):
//   * Python renders huge/small floats exponent-style (str(1e20) == "1e+20",
//     str(1e-7) == "1e-07") while String() gives "100000000000000000000" /
//     "1e-7"; such ages never occur in a chart payload.
//   * Python renders float('nan')/"inf" as "nan"/"inf", JS as "NaN"/"Infinity".
function rcaStrForMerge(v) {
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  return String(v);
}

// Mirror of rca_core/aggregate.py:_mode.
// REVIEW-2026-09-20 semantics, all three of them:
//   1. container values (Python dict/list/set/tuple ↔ JS object/array) are
//      skipped outright; they are not scalars and must not abort or pollute a
//      vote;
//   2. only None/null and blank-whitespace strings count as missing, so 0, 0.0
//      and false are REAL observations again (the old `v && String(v).trim()`
//      filter dropped them: votes [0, 0, 12] merged to 12);
//   3. votes are bucketed on the SAME normalized string pool
//      ``_merge_scalar_field`` uses (``rcaStrForMerge``), so 1 and 1.0 — and in
//      JSON, the number 1 and the string "1" — are one candidate instead of two
//      Map keys whose relative order depends on the input. Python folds True
//      and 1 together only because ``True == 1`` is a dict-key collision; the
//      string pool it votes on now separates them ("True" vs "1"), which this
//      mirror reproduces for free by keying on the rendered string.
// The representative keeps the field's ORIGINAL type/value (int 0 stays 0, not
// "0"), which is what the Python side explicitly documents.
function rcaAggMode(values) {
  const counts = new Map();        // string key -> votes
  const representative = new Map();  // string key -> first original value
  for (const v of values) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'object') continue;  // dict / list mirror of the skip
    if (!String(v).trim()) continue;      // blank == missing
    const key = rcaStrForMerge(v);
    counts.set(key, (counts.get(key) || 0) + 1);
    if (!representative.has(key)) representative.set(key, v);
  }
  if (counts.size === 0) return '';
  let top = 0;
  for (const c of counts.values()) if (c > top) top = c;
  // H4 parity: break ANY tie at the top count deterministically by sorted
  // order — not just the all-unique case. This mirrors Python's _mode
  // (aggregate.py), where a partial tie (e.g. [B,B,A,A]) also sorts and
  // takes the first ('A'), instead of falling back to first-seen input
  // order. Ties are broken on the rendered KEY (Python sorts the same
  // string pool), then the original value of that key is returned.
  const topKeys = [];
  for (const [k, c] of counts) if (c === top) topKeys.push(k);
  if (topKeys.length === 1) return representative.get(topKeys[0]);
  // Python sorted() orders by code point; Array#sort orders by UTF-16 code
  // unit. The two agree on everything in the basic multilingual plane, which
  // covers every payload string here (astral chars such as emoji would be the
  // only divergence).
  topKeys.sort();
  return representative.get(topKeys[0]);
}

// Sentinel returned by mergeFieldAcrossRuns to signal the key carries
// no consensus content and should be dropped from the merged row.
const NO_MERGE = Symbol('rca.no_merge');

function mergeScalarField(values) {
  // H7 (REVIEW-2026-08-19): detect all-bool inputs and preserve the type
  // instead of coercing to "true"/"false" strings. Mirrors
  // rca_core/aggregate.py:_merge_scalar_field. Without this, boolean
  // fields like `reworked` (or any future boolean column) lose their
  // type across runs and downstream code that checks `=== true` fails.
  const nonNull = values.filter((v) => v != null);
  if (nonNull.length && nonNull.every((v) => typeof v === 'boolean')) {
    const counts = new Map();
    for (const v of nonNull) counts.set(v, (counts.get(v) || 0) + 1);
    let topVal = null;
    let topC = 0;
    for (const [v, c] of counts) {
      if (c > topC) { topVal = v; topC = c; }
    }
    // Tie-break: deterministic, `false` (False < True) wins.
    // REVIEW-2026-09-10: Python's bool path used to break ties by insertion
    // order (Counter.most_common), so the two engines disagreed for
    // [True, False]. Python now sorts the tied values the same way this does
    // — see rca_core/aggregate.py:_merge_scalar_field.
    const tied = Array.from(counts.entries()).filter(([, c]) => c === topC);
    if (tied.length > 1) {
      // Sort entries by value (false < true). Use the first.
      tied.sort((a, b) => (a[0] === b[0] ? 0 : (a[0] ? 1 : -1)));
      topVal = tied[0][0];
    }
    return topVal;
  }
  // Mixed input (some non-boolean scalar present): the booleans are IGNORED
  // and the mode is taken over the string/number values only.
  //
  // REVIEW-2026-09-10: this loop used to stringify booleans into the vote, so
  // [true, true, "maybe"] merged to "true" while Python's _merge_scalar_field
  // (which keeps bools out of the string pool - see its docstring) returned
  // "maybe". Same runs, different merged value on the two endpoints.
  const coerced = [];
  for (const v of values) {
    if (v == null) continue;
    if (typeof v === 'string' || typeof v === 'number') {
      const s = String(v).trim();
      if (s) coerced.push(s);
    }
  }
  if (coerced.length === 0) return NO_MERGE;
  return rcaAggMode(coerced);
}

function mergeTypedInteger(values) {
  const valid = values.filter((v) => typeof v === 'number' && Number.isInteger(v));
  if (valid.length === 0) return NO_MERGE;
  const counts = new Map();
  for (const value of valid) counts.set(value, (counts.get(value) || 0) + 1);
  // Iterate rather than Math.max(...counts.values()): spreading a large
  // values() iterable passes one argument per distinct integer, which
  // overflows the engine's argument limit ("Maximum call stack size
  // exceeded") once a chart yields tens of thousands of distinct bed
  // indices. Python's max() has no such limit, so the loop also keeps the
  // two implementations behaving identically at any input size.
  let top = 0;
  for (const count of counts.values()) {
    if (count > top) top = count;
  }
  return Array.from(counts.entries())
    .filter(([, count]) => count === top)
    .map(([value]) => value)
    .sort((a, b) => a - b)[0];
}

function mergeConfidenceField(values) {
  const valid = [];
  for (const value of values) {
    if (value == null || value === '' || typeof value === 'boolean') continue;
    const n = Number(value);
    if (Number.isFinite(n)) valid.push(Math.max(0, Math.min(1, n)));
  }
  if (valid.length === 0) return NO_MERGE;
  return Math.round((valid.reduce((a, b) => a + b, 0) / valid.length) * 10000) / 10000;
}

function mergeMappingField(values) {
  const mappings = values.filter((v) => v && typeof v === 'object' && !Array.isArray(v));
  if (mappings.length === 0) return NO_MERGE;
  const keys = Array.from(new Set(mappings.flatMap((mapping) => Object.keys(mapping)))).sort();
  const out = {};
  for (const key of keys) {
    const merged = mergeFieldAcrossRuns(mappings.map((mapping) => mapping[key]));
    if (merged !== NO_MERGE) out[key] = merged;
  }
  return out;
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
        sig = rcaStructDedupKey(item);
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
  // Dictionaries (notably _extras) stay structured. If a malformed run
  // mixes objects with another shape, retain the structured observations.
  if (nonNull.some((v) => v && typeof v === 'object' && !Array.isArray(v))) {
    return mergeMappingField(values);
  }
  // All non-null values are primitives → scalar merge.
  if (nonNull.every((v) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')) {
    const m = mergeScalarField(values);
    return m === NO_MERGE ? NO_MERGE : m;
  }
  // Unsupported mixed structures are omitted instead of stringified.
  return NO_MERGE;
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
// REVIEW-2026-09-20: sortKeys was missing here, so the browser emitted columnar
// sections in first-seen (merge) order while Python ordered them by `id`
// ascending (rca_core/aggregate.py:COLUMNAR_SECTION_SCHEMA
// sort_keys=[("id","asc")]) — a different section order in the same export.
const RCA_COLUMNAR_KEYMAP = {
  primary: 'sections',
  idKeys: ['id', 'group'],
  strModeFields: ['id', 'group', 'coordinates_text', 'thickness_m'],
  sortKeys: [['id', 'asc']],
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

// Keymap for phylogenetic-tree results. Nodes are the primary rows
// (deduped by ``id``); species names collapse by majority across runs.
// ``metadata`` and ``legend`` are single dicts (not lists) and are NOT
// in listKeys — the merger would otherwise extend their values into
// a list, destroying the dict shape. ``root_ids`` is a real list and
// is safe to merge. Mirrors rca_core/aggregate.PHYLOGENETIC_TREE_SCHEMA.
const RCA_PHYLO_KEYMAP = {
  primary: 'nodes',
  idKeys: ['id'],
  strModeFields: ['name'],
  sortKeys: [['agreement_count', 'desc'], ['name', 'asc']],
  listKeys: ['root_ids'],
  confidence: 'confidence',
  extraSections: null,
};

// Keymap for zonation / correlation chart results
// (UI-REVIEW-2026-09-05). Mirrors rca_core/aggregate.ZONATION_CHART_SCHEMA:
// zone rows deduped by their column (zonation) + name; correlations and
// zonation column descriptors merge as named lists.
const RCA_ZONATION_KEYMAP = {
  primary: 'zones',
  idKeys: ['zonation', 'name'],
  strModeFields: ['name', 'zonation', 'rank', 'age_span', 'base_age',
                  'top_age', 'stage', 'defined_by', 'note'],
  sortKeys: [['agreement_count', 'desc'], ['name', 'asc']],
  listKeys: ['zonations', 'correlations'],
  // UI-REVIEW-2026-09-07: correlations have no "name" — group them by
  // their business identity (both endpoints + both columns) so the same
  // edge across runs merges into one row (majority note/basis) instead of
  // duplicating via the content-signature fallback. Mirrors
  // rca_core/aggregate.MergeSchema.list_key_id_fields.
  listKeyIdFields: { correlations: ['from_zonation', 'from_zone', 'to_zonation', 'to_zone'] },
  confidence: 'confidence',
  extraSections: null,
};

const RCA_KEYMAP_BY_MODE = {
  range_chart: RCA_DEFAULT_KEYMAP,
  columnar_section: RCA_COLUMNAR_KEYMAP,
  abundance_diagram: RCA_ABUNDANCE_KEYMAP,
  phylogenetic_tree: RCA_PHYLO_KEYMAP,
  zonation_chart: RCA_ZONATION_KEYMAP,
};

// Deep clone helper (REVIEW-2026-08-17 P1-5): used by the single-run
// passthrough to keep merged-result mutations from leaking back into the
// source run. Mirrors Python ``copy.deepcopy``. ``structuredClone`` is
// available in Node 17+ and all evergreen browsers since 2022; the
// JSON round-trip is a portable fallback that handles the JSON-shaped
// values the merger actually produces (no Date, RegExp, Map/Set, etc.).
function deepClone(value) {
  if (typeof structuredClone === 'function') {
    try { return structuredClone(value); } catch (_) { /* fall through */ }
  }
  return JSON.parse(JSON.stringify(value));
}

// Detect the appropriate keymap from the shape of the first result object.
// Mirrors Python _auto_detect_schema so both ends agree on the schema.
function rcaAutoDetectKeymap(results) {
  if (!results || !Array.isArray(results) || results.length === 0) {
    return RCA_DEFAULT_KEYMAP;
  }
  let abCount = 0;
  let colCount = 0;
  let phyCount = 0;
  for (const r of results) {
    if (!r || typeof r !== 'object') continue;
    if (Array.isArray(r.abundances) && r.abundances.length > 0) abCount++;
    if (Array.isArray(r.sections) && r.sections.length > 0 &&
        r.sections[0] && typeof r.sections[0] === 'object' && 'id' in r.sections[0]) {
      colCount++;
    }
    // REVIEW-2026-08-17 (P2): phylo detection. The phylo extractor emits a
    // ``nodes`` list (primary rows deduped by id) plus a single ``metadata``
    // dict and a ``root_ids`` list. Without this branch, phylo data was
    // silently merged as range_chart, destroying the primary row key.
    // M2 fix (REVIEW-2026-11-07): require BOTH id and parent so the JS
    // detector agrees with Python _looks_phylogenetic and
    // exporter._looks_phylogenetic_tree. The normalizer always writes both
    // keys (parent=null for roots), so normalized data is unaffected.
    if (Array.isArray(r.nodes) && r.nodes.length > 0 &&
        r.nodes[0] && typeof r.nodes[0] === 'object' &&
        'id' in r.nodes[0] && 'parent' in r.nodes[0]) {
      phyCount++;
    }
  }
  const n = results.length;
  const half = Math.floor((n + 1) / 2);
  // Phylo is the most specific shape — it wins over columnar/abundance when
  // multiple detectors meet the threshold simultaneously.
  if (phyCount >= half) return RCA_PHYLO_KEYMAP;
  if (colCount >= half && abCount >= half) {
    // Both detectors meet threshold — prefer the more specific one.
    return colCount >= abCount ? RCA_COLUMNAR_KEYMAP : RCA_ABUNDANCE_KEYMAP;
  }
  if (colCount >= half) return RCA_COLUMNAR_KEYMAP;
  if (abCount >= half) return RCA_ABUNDANCE_KEYMAP;
  // Phylo beats the others when neither of them meets majority but phylo
  // still has at least one detection (handles the 1-run / 2-run edge case).
  if (phyCount > colCount && phyCount > abCount) return RCA_PHYLO_KEYMAP;
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

// M-1 fix: chimera detection — mirror of rca_core/aggregate.py:388-419
// (_is_chimeric_row). Returns true if the merged row's
// (range_base, range_top, biozone, section) tuple never appears in any
// single source run — i.e. the per-field mode vote produced a
// recombination that no individual run ever observed.
//
// Scientific meaning: emitting such a row as if it were a real
// consensus is misleading. The merged tuple may be internally
// consistent (base ≤ top, plausible biozone), but it is NOT what the
// chart showed. Mark it so the caller can drop or surface it.
//
// P0-5: include section so the same species across different sections
// are NOT flagged as chimeras (they are legitimate multi-section obs).
function rcaIsChimericRow(group, merged) {
  if (!Array.isArray(group) || group.length < 2) return false;
  const keys = ['range_base', 'range_top', 'biozone', 'section'];
  const mergedTuple = keys.map((k) => rcaAggNorm(merged ? merged[k] : ''));
  if (!mergedTuple.some((v) => v)) return false;  // no scientific content
  for (const g of group) {
    if (!g || typeof g !== 'object') continue;
    const itemTuple = keys.map((k) => rcaAggNorm(g[k]));
    if (itemTuple.length === mergedTuple.length
        && itemTuple.every((v, i) => v === mergedTuple[i])) {
      return false;  // at least one source run observed this tuple
    }
  }
  return true;
}

// Python `str(row.get(a) or row.get(b) or "")` — the `or` chain treats 0, "",
// false and None as absent, which is NOT the same as a null check.
function rcaOrChainStr(row, k) {
  const v = row ? row[k] : null;
  if (v === null || v === undefined || v === false || v === 0 || v === '') return '';
  return String(v);
}

// Python `float(v)` semantics — deliberately NOT `Number(v)`:
//   * Number("") / Number("  ") are 0, float("") raises ValueError;
//   * Number("0x10") is 16, float("0x10") raises;
//   * Python accepts "inf"/"infinity"/"nan" (case-insensitive) and the
//     underscore digit separator ("1_000" -> 1000.0), which Number() rejects.
// Returns null where Python would raise TypeError/ValueError.
const _PY_FLOAT_RE = /^[+-]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?$/;
function rcaPyFloat(v) {
  if (typeof v === 'number') return Number.isNaN(v) ? NaN : v;
  if (typeof v === 'boolean') return v ? 1 : 0;   // float(True) == 1.0
  if (typeof v !== 'string') return null;         // dict / list -> TypeError
  const s = v.trim();
  if (!s) return null;
  const low = s.toLowerCase();
  if (low === 'inf' || low === '+inf' || low === 'infinity' || low === '+infinity') return Infinity;
  if (low === '-inf' || low === '-infinity') return -Infinity;
  if (low === 'nan' || low === '+nan' || low === '-nan') return NaN;
  if (!_PY_FLOAT_RE.test(s)) return null;
  const n = Number(s.replace(/_/g, ''));
  return Number.isFinite(n) || n === Infinity || n === -Infinity ? n : null;
}

// Mirror of Python _sort_tuple: (rank, number, lowered, raw) — rank 0 keeps
// every numeric BEFORE every string, so a mixed column still has one order on
// both engines; `lowered` then `raw` reproduce Python's
// (str(v).lower(), str(v)) secondary/tertiary keys.
function rcaSortTuple(row, field) {
  const v = row ? row[field] : null;
  const num = v === null || v === undefined ? null : rcaPyFloat(v);
  if (num !== null && !(typeof v === 'object')) {
    return [0, num, '', ''];
  }
  const s = v === null || v === undefined ? '' : String(v);
  return [1, 0.0, s.toLowerCase(), s];
}

// Python tuple ordering: first differing element decides, `<` semantics.
// NaN reproduces Python's behaviour exactly (nan != nan -> the pair
// "differs", nan < nan -> false -> +1), which is what cmp_to_key gets there.
function rcaCompareSortTuples(ta, tb) {
  for (let i = 0; i < ta.length; i += 1) {
    if (ta[i] !== tb[i]) return ta[i] < tb[i] ? -1 : 1;
  }
  return 0;
}

// Mirror of Python _compare: walk the effective keys, negate for "desc"
// REGARDLESS of the value type (the old JS only negated the numeric branch, so
// a declared string sort with direction "desc" silently sorted ascending).
// REVIEW-2026-09-10 note retained: comparison stays code-point based (no
// localeCompare), so host locale cannot reorder rows; `toLowerCase()` matches
// Python's `str.lower()` for the ASCII names these fields carry.
function rcaSortCompare(a, b, effectiveKeys) {
  for (const [field, direction] of effectiveKeys) {
    const ta = rcaSortTuple(a, field);
    const tb = rcaSortTuple(b, field);
    const c = rcaCompareSortTuples(ta, tb);
    if (c !== 0) return direction === 'desc' ? -c : c;
  }
  return 0;
}

// Mirror of rca_core/aggregate.py:_add_row_warning — the extractor writes
// row._warning as a STRING for one flag and a LIST for several; merged rows
// must not invent a third shape.
function rcaAddRowWarning(row, flag) {
  const existing = row._warning;
  if (existing === null || existing === undefined || existing === '') {
    row._warning = flag;
    return;
  }
  const flags = Array.isArray(existing) ? existing.slice() : [existing];
  if (flags.indexOf(flag) === -1) flags.push(flag);
  row._warning = flags.length === 1 ? flags[0] : flags;
}

/**
 * BORROW-2026-09-20 (A+B) — mirror of rca_core/aggregate.py:_merge_contract_field.
 *
 * Merge one coverage/geometry field across runs into ``target``. Returns true
 * when ``key`` belongs to the contract and was handled here — the callers then
 * skip their generic field merger, which would otherwise drop a list of
 * strings (NO_MERGE) or stringify the integers inside ``geometry``.
 *
 * Contract semantics, shared by every merge path so the two can't drift
 * (rules live in js/reason-codes.js, i.e. in ONE place, exactly like the
 * Python side importing them from reason_codes.py):
 *
 *   * ``reason_codes`` — UNION, in first-seen order. Two runs each spotting a
 *     different defect means both defects are true.
 *   * ``response_kind`` — precedence vote, extracted > uncertain > not_drawn:
 *     one run that saw the range drawn outranks a run that called it a dash.
 *     Divergence is recorded as a warning plus the raw ballot, never hidden.
 *   * ``geometry`` — first well-formed block wins, deep-copied. Averaging two
 *     runs' pixel reads would invent a position neither run observed.
 *
 * When js/reason-codes.js has not been loaded the merger reports "not a
 * contract key", which reproduces the pre-contract generic path instead of
 * throwing — the same degradation the Python module documents for results
 * that never adopted the contract.
 */
function rcaMergeContractField(key, values, target) {
  if (typeof rcaMergeReasonCodes !== 'function'
      || typeof rcaMergeResponseKinds !== 'function'
      || typeof rcaContractTruthy !== 'function') {
    return false;
  }
  const list = Array.isArray(values) ? values : [];
  if (key === 'reason_codes') {
    const union = rcaMergeReasonCodes(list);
    if (union.length) target[key] = union;
    return true;
  }
  if (key === 'response_kind') {
    const vote = rcaMergeResponseKinds(list);
    if (vote.kind) target[key] = vote.kind;
    if (vote.divergent) {
      rcaAddRowWarning(target, 'response_kind_divergent');
      // The RAW ballot, Python's `[v for v in values if v]` — unknown spellings
      // are kept so an auditor can see what the runs actually said.
      target.response_kind_votes = list.filter((v) => rcaContractTruthy(v));
    }
    return true;
  }
  if (key === 'geometry') {
    let block = null;
    for (const v of list) {
      if (v && typeof v === 'object' && !Array.isArray(v)
          && rcaContractTruthy(v.points)) {
        block = v;
        break;
      }
    }
    if (block !== null) target[key] = deepClone(block);
    return true;
  }
  return false;
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
      // F-10 fix: also include ICZN-normalized author_year for range-chart
      // so "Smith, 1950", "(Smith, 1950)", "Smith 1950" dedup together.
      const parts = km.idKeys.map((k) => rcaAggNorm(it[k]));
      if (!parts.some((p) => p)) continue;
      // F-10: range-chart uses ICZN author_year normalization in dedup key.
      const icznSuffix = km.primary === 'species_ranges'
        ? '\x1e' + rcaNormIcbnAuthor(it['author_year'])
        : '';
      // Extract qualifiers from the species/id fields for the key.
      const speciesRaw = it['species'] || it['id'] || '';
      const quals = rcaExtractQualifiers(speciesRaw);
      const qualsSuffix = quals.length ? '\x1f' + quals.join('|') : '';
      // Sprint B (REVIEW-2026-09-04): join with an explicit NUL ('\u0000')
      // delimiter. The review flagged `parts.join('')` as collision-prone —
      // the source actually carried an INVISIBLE '\x01' byte there (same
      // collision class: a control char nobody could see or lint), so it is
      // now a visible escape. ("ab","c") vs ("a","bc") no longer merge;
      // Python never had the problem because it keys on a tuple
      // (aggregate.py:521). The iczn / qualifier suffixes below keep their
      // own \x1e / \x1f delimiters and are unaffected.
      const key = parts.join('\u0000') + icznSuffix + qualsSuffix;
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
      // REVIEW-2026-09-10: both sides of this comparison must be NORMALISED.
      // The check used `rcaAggNorm(raw) === primaryMode`, i.e. a normalised
      // value against the RAW mode, so whenever the mode string was not
      // already canonical (which is exactly the case this block exists for)
      // nothing matched, `counter` stayed empty and the qualifier restore
      // silently no-op'd. Python was fixed for this in Sprint B
      // (aggregate.py: species_mode_norm = _norm(species_mode)); the JS
      // mirror kept the defect, so case/whitespace jitter in the species
      // string produced a different merged taxon name in the browser than on
      // the server - and the species name feeds name verification + exports.
      const primaryModeNorm = rcaAggNorm(primaryMode);
      for (const g of group) {
        const raw = (g && g[primaryIdField] || '').trim();
        if (!raw) continue;
        if (rcaAggNorm(raw) === primaryModeNorm) {
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
      // BORROW-2026-09-20 (A+B): coverage contract + geometry sidecar are
      // merged by their own rules, never by the generic field merger.
      if (rcaMergeContractField(k, perRun, aggr)) continue;
      let merged_v;
      if (km.primary === 'species_ranges' && (k === 'range_top_idx' || k === 'range_base_idx')) {
        merged_v = mergeTypedInteger(perRun);
      } else if (km.primary === 'species_ranges' && k === 'confidence') {
        merged_v = mergeConfidenceField(perRun);
      } else {
        merged_v = mergeFieldAcrossRuns(perRun);
      }
      if (merged_v === NO_MERGE) continue;  // drop empty key
      aggr[k] = merged_v;
    }
    // REVIEW-2026-09-20 (mirror of aggregate.py:784-799): the two bed indices
    // are voted INDEPENDENTLY, so a run-A base and a run-B top can recombine
    // into an inverted pair even though every source row was sane. An inverted
    // pair exports as a species that dies before it appears, and the string
    // endpoints are what quality.js reads, so nothing else catches it. Repair
    // the order and flag it with the same `index_order_swap` marker the
    // extractor uses.
    if (km.primary === 'species_ranges') {
      const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
      const topIdx = aggr.range_top_idx;
      const baseIdx = aggr.range_base_idx;
      if (isNum(topIdx) && isNum(baseIdx) && baseIdx > topIdx) {
        aggr.range_top_idx = baseIdx;
        aggr.range_base_idx = topIdx;
        rcaAddRowWarning(aggr, 'index_order_swap');
      }
    }
    // M-1 fix: chimera detection. If every contributing run produced a
    // DIFFERENT (range_base, range_top, biozone, section) tuple for this
    // species — i.e. no run ever observed the merged tuple — flag with
    // _chimera_dropped so the caller can filter (and so score_consistency
    // in quality.js can surface chimera_warnings).
    if (rcaIsChimericRow(group, aggr)) {
      aggr._chimera_dropped = true;
    }
    merged.push(aggr);
  }
  // Apply schema sortKeys if defined (e.g. agreement_count desc, species asc).
  // REVIEW-2026-09-20: mirrors rca_core/aggregate.py:_merge_primary_list's
  // sort block 1:1 — see rcaSortCompare for the two behaviours that were
  // missing here (desc on a string key, and deterministic tiebreaking).
  const kmSortKeys = km.sortKeys;
  if (kmSortKeys && kmSortKeys.length) {
    // Python appends the schema's primary id fields as ascending tiebreakers
    // so rows that tie on every declared key no longer keep first-seen-run
    // order (the same set of runs produced different row orders across
    // sessions before).
    const declared = {};
    for (const [f] of kmSortKeys) declared[f] = true;
    const effectiveKeys = kmSortKeys.slice();
    for (const k of km.idKeys || []) {
      if (!Object.prototype.hasOwnProperty.call(declared, k)) {
        effectiveKeys.push([k, 'asc']);
      }
    }
    merged.sort((a, b) => rcaSortCompare(a, b, effectiveKeys));
  } else {
    // No schema order declared: Python sorts on
    // ``str(row.get("id") or row.get("species") or "")`` — an `or` chain, so an
    // id of 0 / "" / false falls through to species, and the comparison is
    // CASE-SENSITIVE (no lowercasing on either side).
    merged.sort((a, b) => {
      const aKey = rcaOrChainStr(a, 'id') || rcaOrChainStr(a, 'species');
      const bKey = rcaOrChainStr(b, 'id') || rcaOrChainStr(b, 'species');
      return aKey < bKey ? -1 : (aKey > bKey ? 1 : 0);
    });
  }
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
        // UI-REVIEW-2026-09-07: business-identity fields for keys whose
        // items have no natural name (zonation correlations) — group by
        // those so the same edge across runs merges into one row. Mirrors
        // the id_fields branch in rca_core.aggregate._merge_named_lists.
        const idFields = km.listKeyIdFields && km.listKeyIdFields[key];
        if (idFields) {
          const parts = idFields.map((f) => rcaAggNorm(it[f] || ''));
          if (parts.every((s) => s)) {
            const idLabel = parts.join('');
            if (!groups.has(idLabel)) {
              groups.set(idLabel, []);
              order.push(idLabel);
            }
            groups.get(idLabel).push(it);
            continue;
          }
        }
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
          // F-13 fix: use Python-repr-compatible serialization (True/False/None)
          // so boolean and null values produce the same key across JS/Python.
          try {
            label = '__nolabel__:' + rcaStructDedupKey(it);
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
        // BORROW-2026-09-20 (A+B): same contract rules as the primary path, so
        // a biozone / site / point row merges identically.
        if (rcaMergeContractField(k, perRun, rep)) continue;
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
    // REVIEW-2026-08-17 (P1-5): deep copy, not shallow. The Python side
    // (``copy.deepcopy(runs[0])``) already deep-copies to keep the source
    // run unaliased from the merged result. A shallow ``Object.assign``
    // made in-place mutations of nested structures (e.g.
    // ``merged.sections[0].formations.push(...)``) silently rewrite the
    // source run's nested arrays/objects, breaking audit integrity and
    // the Python↔JS parity contract.
    const single = deepClone(runs[0]);
    const primary = single[km.primary] || [];
    single[km.primary] = primary.map((it) => Object.assign({}, it, { agreement_count: 1, agreement: '1/1' }));
    if (single[km.confidence] === undefined) single[km.confidence] = 0;
    single.runs = 1;
    return single;
  }

  const out = { runs: n };
  out[km.primary] = mergePrimaryList(runs, km, n);
  Object.assign(out, mergeNamedLists(runs, km));

  // REVIEW-2026-09-10: root-level _extras / _warnings carry figure-level data
  // the extractor deliberately preserves (captions, notes, degradation
  // warnings). The single-run passthrough keeps them, but this N-run path
  // built `out` only from the primary list + listKeys + confidence, so
  // raising the run count silently dropped them. Mirrors rca_core/aggregate.py.
  for (const key of ['_extras', '_warnings']) {
    for (const run of runs) {
      if (run && typeof run === 'object' && run[key]) {
        out[key] = deepClone(run[key]);
        break;
      }
    }
  }

  // REVIEW-2026-08-17 (P2 follow-up): phylogenetic-tree ``metadata`` and
  // ``legend`` are single dicts (not lists). They are identical across
  // runs for the same image, so preserve them from the first run rather
  // than letting the merger drop them. Mirrors
  // rca_core/aggregate.py:790-796 so the Python↔JS multi-run merge
  // produces the same shape (otherwise pure-frontend phylo merge lost
  // these fields while Python preserved them — silent data loss).
  if (km.primary === 'nodes') {
    for (const key of ['metadata', 'legend']) {
      if (runs && runs[0] && typeof runs[0] === 'object' && key in runs[0]) {
        // Deep-copy so downstream mutations on the merged result do not
        // taint the source run (consistent with the single-run
        // passthrough's deepClone behavior).
        out[key] = deepClone(runs[0][key]);
      }
    }
  }

  // Weighted-mean confidence (P0-3 fix, REVIEW-2026-08-17).
  // Each run is weighted by its own ``runs`` field — a run that already
  // aggregated 3 sub-attempts counts 3x in the average, since it
  // represents 3 LLM calls. This matches rca_core.aggregate.merge_results
  // (the previous simple-average JS implementation silently produced
  // different values whenever runs reported different ``runs`` counts,
  // breaking the Python↔JS merge parity for multi-run extractions).
  // Runs with no ``runs`` field (or a non-positive value) fall back to
  // weight 1, mirroring Python's ``int(r.get("runs") or 1)`` + ``w < 1 → 1``.
  let wSum = 0, wN = 0;
  for (const r of runs) {
    const c = Number(r[km.confidence]);
    if (!Number.isFinite(c)) continue;
    let w = Number(r.runs);
    if (!Number.isFinite(w) || w < 1) w = 1;
    wSum += c * w;
    wN += w;
  }
  out[km.confidence] = wN > 0 ? Math.round((wSum / wN) * 10000) / 10000 : 0;

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

  // M-1 fix: filter chimeric rows from the primary list. Dropped rows
  // are surfaced via `chimera_warnings` so consumers and the UI can
  // tell the operator "we dropped X rows because no run ever observed
  // the merged (FAD/LAD/biozone) tuple together". Mirrors
  // rca_core/aggregate.py:784-807.
  const primaryList = out[km.primary];
  if (Array.isArray(primaryList) && primaryList.length > 0) {
    const chimeras = primaryList.filter((r) => r && r._chimera_dropped);
    if (chimeras.length > 0) {
      const surviving = primaryList.filter((r) => r && !r._chimera_dropped);
      for (const r of surviving) delete r._chimera_dropped;
      out[km.primary] = surviving;
      out.chimera_warnings = [];
      for (const c of chimeras) {
        const row = {
          species: c.species, section: c.section, biozone: c.biozone,
          range_top: c.range_top, range_base: c.range_base,
        };
        out.chimera_warnings.push({
          table: km.primary,
          row: row,
          reason: 'no single run observed the merged (FAD/LAD/biozone) tuple',
        });
        delete c._chimera_dropped;
      }
    }
  }

  return out;
}
