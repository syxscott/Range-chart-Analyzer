/**
 * js/quality.js - Quality scoring for VLM-extracted range-chart / columnar-section results.
 *
 * Byte-for-byte parity with rca_core/quality.py.
 * Scores a normalised result dict on 4 weighted dimensions and produces a
 * letter grade + issue list for the UI to surface:
 *
 *   completeness (0.30) — how many expected fields are populated
 *   accuracy    (0.40) — geological plausibility (FAD<LAD, bed order)
 *   consistency (0.20) — cross-field agreement (section name references)
 *   structure  (0.10) — _extras ratio, array lengths, null fields
 *
 * Pure function — no I/O, no LLM. Drop-in callable from the browser
 * (direct/proxy) path so the UI can display a quality badge without
 * waiting for a server round-trip.
 *
 * Output shape:
 *   {
 *     score: 0.87,        // 0.0 – 1.0 weighted composite
 *     grade: "B",         // A / B / C / D / F
 *     issues: [
 *       {severity: "warning", msg_key: "quality.unmatched_section_ref"},
 *       ...
 *     ]
 *   }
 *
 * msg_key entries map to i18n tables in js/i18n.js (quality.* keys).
 */

'use strict';

// ---------------------------------------------------------------------------
// Constants — must stay in sync with rca_core/quality.py
// ---------------------------------------------------------------------------

const W_COMPLETENESS = 0.30;
const W_ACCURACY    = 0.40;
const W_CONSISTENCY = 0.20;
const W_STRUCTURE   = 0.10;

const _GRADES = [
  [0.90, 'A'],
  [0.75, 'B'],
  [0.60, 'C'],
  [0.40, 'D'],
];

// Top-level arrays that count as primary "content" for the extraction result.
// Presence-but-empty across all of these = "pure extraction miss" -> F.
// UI-REVIEW-2026-09-07: zonation / correlation chart content keys included so
// _isPureExtractionMiss also covers that mode. Mirrors the Python tuple
// rca_core/quality.py:_CONTENT_KEYS exactly.
const _CONTENT_KEYS = [
  'species_ranges', 'abundances', 'sections', 'biozones',
  'other_fossils', 'cross_beds', 'lithology_legend',
  'fossil_legend', 'age_units',
  'zones', 'correlations', 'zonations',
];

// Columnar-mode marker keys — unique to columnar-section extraction.
const _COLUMNAR_MARKERS = [
  'cross_beds', 'lithology_legend', 'fossil_legend',
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Normalise a row's `_warning` field to an array of flag names.
// REVIEW-2026-09-10: the normalizers write a bare string for a single flag
// and an array when several fire together, so consumers must accept both.
// Mirrors _warning_flags in rca_core/quality.py.
function rcaWarningFlags(value) {
  if (value === null || value === undefined) return [];
  if (typeof value === 'string') return value ? [value] : [];
  if (Array.isArray(value)) return value.filter(Boolean).map(String);
  return [String(value)];
}

function gradeFor(score) {
  for (const [threshold, letter] of _GRADES) {
    if (score >= threshold) return letter;
  }
  return 'F';
}

function _detectMode(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    return 'range_chart';
  }
  for (const key of _COLUMNAR_MARKERS) {
    const v = data[key];
    if (v === undefined || v === null) continue;
    if (Array.isArray(v) && v.length > 0) return 'columnar';
    if (typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length > 0) return 'columnar';
    if (typeof v === 'string' && v.trim()) return 'columnar';
  }
  if ('abundances' in data && !('species_ranges' in data)) return 'abundance';
  return 'range_chart';
}

function _isPureExtractionMiss(data) {
  let seenAny = false;
  for (const key of _CONTENT_KEYS) {
    const v = data ? data[key] : undefined;
    if (v === undefined || v === null) continue;
    seenAny = true;
    if (Array.isArray(v) && v.length > 0) return false;
    if (typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length > 0) return false;
    if (typeof v === 'string' && v.trim()) return false;
  }
  return seenAny;
}

function _parseBedN(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'boolean') return null;
  if (typeof value === 'number') {
    if (Number.isNaN(value)) return null;
    return Math.trunc(value);
  }
  const s = String(value).trim();
  if (!s) return null;
  const m = s.match(/-?\d+/);
  return m ? parseInt(m[0], 10) : null;
}

function _clamp01(x) { return Math.min(1.0, Math.max(0.0, x)); }

// ---------------------------------------------------------------------------
// REVIEW-2026-07-31: age-bound resolution mirroring
// rca_core/standards/ics.py:ics_resolve_age_bound, so the FAD<LAD Ma
// branch behaves identically in the browser-only path.
// ---------------------------------------------------------------------------

const _AGE_UNIT_RE = /(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b/i;
const _AGE_RANGE_RE = /(?<![\w.])([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:[-–—]|\bto\b)\s*([+]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:Ma|Myr|Mya|m\.\s*y\.?|million\s+years?(?:\s+ago)?)\b/i;

function _explicitMaValues(text) {
  const out = [];
  const s = String(text);
  let m;
  const re1 = new RegExp(_AGE_UNIT_RE.source, 'gi');
  while ((m = re1.exec(s)) !== null) out.push(parseFloat(m[1]));
  const re2 = new RegExp(_AGE_RANGE_RE.source, 'gi');
  while ((m = re2.exec(s)) !== null) { out.push(parseFloat(m[1])); out.push(parseFloat(m[2])); }
  return out;
}

// True when the value carries an explicit numeric age unit ("260 Ma",
// "255.5Ma") — NOT when a word merely contains "ma" ("Madison 3" must
// stay a bed label).
function _looksLikeAge(v) {
  const s = String(v || '');
  return new RegExp(_AGE_UNIT_RE.source, 'i').test(s) || new RegExp(_AGE_RANGE_RE.source, 'i').test(s);
}

function _regExpEscape(s) { return s.replace(/[-\/\\^$*+?.()|[\]{}]/g, '\\$&'); }

// REVIEW-2026-09-20: the bundled table carries the Cambrian intervals that
// have no ratified name yet as "Stage 2" … "Stage 10", and TWO of them share
// their span with the ratified name adopted since:
//   Wuliuan == Stage 5 (504.5-506.5 Ma), Jiangshanian == Stage 9 (491.0-494.2).
// Which of the two a lookup returned therefore depended purely on key order
// (505 Ma resolved to "Stage 5" here but "Wuliuan" in Python, whose JSON
// inserts the rows in a different order). Mirrors ics.py:_INFORMAL_STAGE_RE /
// _prefer_formal: within each of the three match groups the RATIFIED name
// wins over the informal "Stage N" synonym covering the same interval.
const _INFORMAL_STAGE_RE = /^\s*(?:unnumbered|unnamed|stage)\s+[\dxvi]+\s*$/i;

function _isInformalStageName(name) {
  return _INFORMAL_STAGE_RE.test(String(name === null || name === undefined ? '' : name));
}

// First formal (ratified) name in the list, else the first entry, else null.
function _preferFormal(names) {
  for (const name of names) {
    if (!_isInformalStageName(name)) return name;
  }
  return names.length ? names[0] : null;
}

// Deterministic stage lookup for a Ma value, mirroring
// rca_core/standards/ics.py:ics_stage_from_age. Python semantics reproduced:
//   1. a strict interior hit (top < ma < base) wins — interior matches are
//      unique in a gapless table EXCEPT for the duplicate Cambrian rows, so
//      every group is collected and filtered through _preferFormal;
//   2. at an exact boundary, the boundary belongs to the YOUNGER stage whose
//      BASE it defines (259.51 -> Wuchiapingian, 254.14 -> Changhsingian);
//   3. a value equal to a stage's top (0.0 -> Holocene) is the last fallback.
// Python's if/elif means one row can only be in ONE of the two boundary
// groups; the same is reproduced below.
// Returns null when no stage matches (same as the Python None).
function _icsStageFromAge(stages, ma) {
  const interior = [];
  const baseMatch = [];
  const topMatch = [];
  for (const name of Object.keys(stages)) {
    const info = stages[name] || {};
    const top = typeof info.top_ma === 'number' ? info.top_ma : 0;
    const base = typeof info.base_ma === 'number' ? info.base_ma : 0;
    if (top < ma && ma < base) { interior.push(name); continue; }
    if (Math.abs(base - ma) <= 1e-9) baseMatch.push(name);
    else if (Math.abs(top - ma) <= 1e-9) topMatch.push(name);
  }
  if (interior.length) return _preferFormal(interior);
  if (baseMatch.length) return _preferFormal(baseMatch);
  if (topMatch.length) return _preferFormal(topMatch);
  return null;
}

// Mirror of ics.py:ics_parse_age_range — every stage name of the table found
// in the text, ordered by first appearance, REPEATS FOLDED OUT
// (REVIEW-2026-09-20): "Wuchiapingian to Changhsingian, see Wuchiapingian"
// used to yield three hits, and every consumer walking consecutive pairs
// then judged a stage against itself.
function _icsParseAgeRange(stages, text) {
  const s = String(text === null || text === undefined ? '' : text);
  if (!s) return [];
  const matches = [];
  for (const stageName of Object.keys(stages)) {
    const re = new RegExp('\\b' + _regExpEscape(stageName) + '\\b', 'gi');
    let m;
    while ((m = re.exec(s)) !== null) {
      matches.push({ idx: m.index, name: stageName });
      if (m.index === re.lastIndex) re.lastIndex += 1;  // empty-match guard
    }
  }
  // Python sorts by position only — a STABLE sort, so two names starting at
  // the same offset keep their table order. Array#sort is stable in ES2019+.
  matches.sort((a, b) => a.idx - b.idx);
  const seen = new Set();
  const ordered = [];
  for (const hit of matches) {
    if (seen.has(hit.name)) continue;
    seen.add(hit.name);
    ordered.push(hit.name);
  }
  return ordered;
}

// Mirror of ics.py:_resolve_prefer (REVIEW-2026-09-20). ``prefer`` used to be
// compared with === "younger" at every branch, so any other spelling — "Older",
// " TOP", "youngest", "bottom", an absent/None value from a JSON payload —
// silently took the INVERSE branch and exported the wrong end of the interval.
// Empty/None keeps the documented default ("older"); any other unknown value
// raises instead of quietly inverting a FAD/LAD export.
const _PREFER_OLDER = ['older', 'oldest', 'old', 'base', 'bottom'];
const _PREFER_YOUNGER = ['younger', 'youngest', 'young', 'top', 'upper'];

function _resolvePrefer(prefer) {
  const key = String(prefer === null || prefer === undefined ? '' : prefer)
    .trim().toLowerCase();
  if (!key || _PREFER_OLDER.indexOf(key) !== -1) return true;
  if (_PREFER_YOUNGER.indexOf(key) !== -1) return false;
  const known = _PREFER_OLDER.concat(_PREFER_YOUNGER).filter(
    (v, i, arr) => arr.indexOf(v) === i).sort();
  throw new Error('prefer must be an \'older\'/\'younger\' spelling (got '
    + JSON.stringify(prefer) + '); known values: ' + known.join(', '));
}

// Mirror of ics.py:_stage_bound — base_ma is the OLDER (larger) number,
// top_ma the YOUNGER (smaller) one.
function _stageBound(info, wantOlder) {
  const base = typeof info.base_ma === 'number' ? info.base_ma : 0;
  const top = typeof info.top_ma === 'number' ? info.top_ma : 0;
  return wantOlder ? (base || 0) : (top || 0);
}

// Mirror of ics.py:_series_bounds_for: the explicit override wins when the
// label carries one (Pleistocene's named stages end at 0.129 but the epoch
// runs to 0.0117), otherwise first stage's base / last stage's top.
function _seriesBoundsFor(stages, ent) {
  if (ent.bounds) return [ent.bounds[0], ent.bounds[1]];
  const first = stages[ent.stages[0]];
  const last = stages[ent.stages[ent.stages.length - 1]];
  if (!first || !last) return [null, null];
  return [(first.base_ma || 0), (last.top_ma || 0)];
}

// Resolve a bound label to {name, ma} or null. Mirrors the FULL decision tree
// of rca_core/standards/ics.py:ics_resolve_age_bound, in its order:
//   1. explicit numeric Ma literal (single value or range);
//   2. Chinese stage-name alias;
//   3. ICS stage name(s) — BEFORE series (REVIEW-2026-09-10), so
//      "Late Permian (Wuchiapingian)" resolves to the precise stage named in
//      the label instead of silently discarding it for the Lopingian series;
//   4. series/epoch label (English then Chinese);
//   5. period name (English then Chinese).
function _resolveAgeBound(text, prefer) {
  const wantOlder = _resolvePrefer(prefer);
  const s = String(text === null || text === undefined ? '' : text).trim();
  const stages = (typeof globalThis !== 'undefined' && globalThis.RCA_ICS_TABLE) ? globalThis.RCA_ICS_TABLE : null;
  if (!s || !stages || Object.keys(stages).length === 0) return null;

  // 1. Explicit numeric ages (range-aware): older = max, younger = min.
  const vals = _explicitMaValues(s);
  if (vals.length > 0) {
    let ma = vals[0];
    for (const v of vals) {
      if (wantOlder ? v > ma : v < ma) ma = v;
    }
    return { name: _icsStageFromAge(stages, ma), ma };
  }

  // 2. Chinese stage-name alias. REVIEW-2026-09-20: the alias resolves to that
  // very stage, so it takes the SAME bound rule as step 3 (base/top by
  // prefer), not the old midpoint.
  const cnStages = globalThis.RCA_ICS_CN_STAGES || {};
  for (const alias of Object.keys(cnStages)) {
    if (s.indexOf(alias) !== -1) {
      const st = cnStages[alias];
      if (stages[st]) return { name: st, ma: _stageBound(stages[st], wantOlder) };
    }
  }

  // 3. ICS stage name(s) -> order-independent range bounds.
  const found = _icsParseAgeRange(stages, s);
  if (found.length === 1) {
    // REVIEW-2026-09-20: a single named stage is one ENDPOINT of the caller's
    // range, not a point in time. The midpoint made prefer="older" and
    // prefer="younger" return the SAME number, so a species whose FAD and LAD
    // both read "Wuchiapingian" exported a zero-duration range
    // (FAD == LAD == 256.8). Take the stage's own bound instead.
    const info = stages[found[0]];
    if (info) return { name: found[0], ma: _stageBound(info, wantOlder) };
  } else if (found.length > 1) {
    // Stage range: max-of-bases / min-of-tops, honoring ``prefer``. Python's
    // max()/min() return the FIRST extremal element, so the comparison below
    // is strictly greater/less — never >= / <=.
    const bases = [];
    const tops = [];
    for (const st of found) {
      if (!stages[st]) continue;
      bases.push([st, stages[st].base_ma || 0]);
      tops.push([st, stages[st].top_ma || 0]);
    }
    if (bases.length && tops.length) {
      let pick = wantOlder ? bases[0] : tops[0];
      for (const cand of (wantOlder ? bases : tops)) {
        if (wantOlder ? cand[1] > pick[1] : cand[1] < pick[1]) pick = cand;
      }
      return { name: pick[0], ma: pick[1] };
    }
  }

  // 4. Series/epoch labels (English then Chinese).
  const series = globalThis.RCA_ICS_SERIES || {};
  const norm = s.toLowerCase();
  for (const label of Object.keys(series)) {
    if (!new RegExp('\\b' + _regExpEscape(label) + '\\b').test(norm)) continue;
    const bounds = _seriesBoundsFor(stages, series[label]);
    if (bounds[0] !== null && bounds[0] !== undefined) {
      return {
        name: series[label].name,
        ma: wantOlder ? bounds[0] : bounds[1],
      };
    }
  }
  const cnSeries = globalThis.RCA_ICS_CN_SERIES || {};
  for (const alias of Object.keys(cnSeries)) {
    if (s.indexOf(alias) === -1) continue;
    const ent = series[cnSeries[alias]];
    if (!ent) continue;
    const bounds = _seriesBoundsFor(stages, ent);
    if (bounds[0] !== null && bounds[0] !== undefined) {
      return { name: ent.name, ma: wantOlder ? bounds[0] : bounds[1] };
    }
  }

  // 5. Period-level fallback (English then Chinese). Python returns the
  // CANONICAL period name ("Permian"), not the matched label.
  const periods = globalThis.RCA_ICS_PERIODS || {};
  const periodNames = globalThis.RCA_ICS_PERIOD_NAMES || {};
  for (const label of Object.keys(periods)) {
    if (!new RegExp('\\b' + _regExpEscape(label) + '\\b').test(norm)) continue;
    const b = periods[label];
    if (b && b[0] !== null && b[0] !== undefined) {
      return { name: periodNames[label] || label, ma: wantOlder ? b[0] : b[1] };
    }
  }
  const cnPeriods = globalThis.RCA_ICS_CN_PERIODS || {};
  const cnPeriodNames = globalThis.RCA_ICS_CN_PERIOD_NAMES || {};
  for (const alias of Object.keys(cnPeriods)) {
    if (s.indexOf(alias) === -1) continue;
    const b = cnPeriods[alias];
    if (b && b[0] !== null && b[0] !== undefined) {
      return { name: cnPeriodNames[alias] || alias, ma: wantOlder ? b[0] : b[1] };
    }
  }
  return null;
}

function sectionNames(data) {
  const names = new Set();
  const sects = data && data.sections;
  if (!Array.isArray(sects)) return names;
  for (const sec of sects) {
    if (!sec || typeof sec !== 'object') continue;
    const n = sec.name || sec.id;
    if (n && String(n).trim()) names.add(String(n).trim());
  }
  return names;
}

// ---------------------------------------------------------------------------
// Dimension scorers
// ---------------------------------------------------------------------------

/**
 * completeness: how many expected fields are populated vs. empty.
 * Mode-aware: columnar / abundance / range-chart each have their own set
 * of expected fields.
 */
function scoreCompleteness(data) {
  const issues = [];
  let checks = 0;
  let passed = 0;

  const mode = _detectMode(data);

  let primary = null;
  if (mode === 'abundance') {
    primary = 'abundances';
  } else if (mode === 'columnar') {
    primary = null;
  } else {
    primary = ('species_ranges' in data) ? 'species_ranges' : null;
  }

  if (primary) {
    checks += 1;
    const rows = data[primary];
    if (Array.isArray(rows) && rows.length > 0) {
      passed += 1;
    } else {
      issues.push({severity: 'warning', msg_key: 'quality.empty_primary_rows'});
    }
  }

  // Mode-appropriate non-primary keys.
  let relevant;
  if (mode === 'columnar') {
    relevant = ['sections', 'cross_beds', 'lithology_legend', 'fossil_legend'];
  } else if (mode === 'abundance') {
    relevant = ['abundances', 'sections'];
  } else {
    relevant = ['sections', 'biozones', 'other_fossils'];
  }

  // The chart has mode-signal if at least one relevant key is non-empty.
  const hasModeSignal = relevant.some(k => {
    const v = data ? data[k] : undefined;
    return Array.isArray(v) && v.length > 0;
  });

  for (const key of relevant) {
    checks += 1;
    const val = data ? data[key] : undefined;
    if (val !== undefined && val !== null) {
      passed += 1;
    } else {
      if (!hasModeSignal) {
        issues.push({severity: 'info', msg_key: 'quality.missing_top_level'});
      }
      passed += 1;  // don't drop score for absent optional fields
    }
  }

  // Confidence field presence.
  checks += 1;
  const conf = data ? data.confidence : undefined;
  if (conf !== undefined && conf !== null && typeof conf === 'number') {
    passed += 1;
  } else {
    issues.push({severity: 'info', msg_key: 'quality.missing_confidence'});
  }

  return [checks > 0 ? _clamp01(passed / checks) : 0.0, issues];
}

/**
 * accuracy: section references resolve, FAD<LAD respected (range-chart),
 * bed indices ordered top >= base (columnar).
 */
function scoreAccuracy(data) {
  const issues = [];
  let checks = 0;
  let passed = 0;
  const sectionNamesSet = sectionNames(data);

  const speciesRows = (data && Array.isArray(data.species_ranges)) ? data.species_ranges : [];

  if (speciesRows.length > 0) {
    checks += 1;
    let ok = 0;
    let hasRef = 0;
    for (const row of speciesRows) {
      if (!row || typeof row !== 'object') continue;
      const sec = String(row.section || '').trim();
      if (!sec) continue;
      hasRef += 1;
      if (sectionNamesSet.has(sec)) ok += 1;
    }
    if (hasRef === 0) {
      passed += 1;
    } else if (sectionNamesSet.size > 0 && ok === hasRef) {
      passed += 1;
    } else if (sectionNamesSet.size > 0 && ok > 0) {
      passed += 0.5;
      issues.push({severity: 'warning', msg_key: 'quality.unmatched_section_ref'});
    } else if (sectionNamesSet.size > 0) {
      issues.push({severity: 'warning', msg_key: 'quality.all_section_refs_unmatched'});
    } else {
      passed += 0.5;
      issues.push({severity: 'info', msg_key: 'quality.sections_absent'});
    }
  }

  // FAD<LAD check for range-chart species.
  // REVIEW-2026-07-31: mirror rca_core/quality.py's two-branch check.
  //  * Bed branch: beds are 1-indexed from the bottom, so a younger bed
  //    has the LARGER index; FAD (base) must have the smaller index than
  //    LAD (top). The age branch is only entered when a bound carries an
  //    explicit age unit — previously "300 Ma"/"250 Ma" rows fell into the
  //    bed branch, _parseBedN read the leading integers and every valid
  //    age range was flagged as a violation.
  //  * Age branch: range_base = FAD = OLDER = LARGER Ma; range_top = LAD =
  //    YOUNGER = SMALLER Ma. base_ma < top_ma is a violation.
  let fadLadTotal = 0;
  let fadLadViolations = 0;
  for (const row of speciesRows) {
    if (!row || typeof row !== 'object') continue;
    const topRaw = row.range_top;
    const baseRaw = row.range_base;
    const top = _parseBedN(topRaw);
    const base = _parseBedN(baseRaw);
    const looksLikeAge = _looksLikeAge(topRaw) || _looksLikeAge(baseRaw);
    if (top !== null && base !== null && !looksLikeAge) {
      fadLadTotal += 1;
      if (top < base) {
        fadLadViolations += 1;
        // Parity: the bed-index branch reports range_top_lt_base — mirrors
        // rca_core/quality.py (both the bed branch and the ICS-age branch
        // below use "quality.range_top_lt_base", while the consistency
        // check's bed inversion uses "quality.fad_lt_lad").
        issues.push({severity: 'warning', msg_key: 'quality.range_top_lt_base'});
      }
    } else {
      const topR = _resolveAgeBound(topRaw, 'younger');
      const baseR = _resolveAgeBound(baseRaw, 'older');
      if (topR && baseR && topR.ma !== null && baseR.ma !== null) {
        fadLadTotal += 1;
        if (baseR.ma < topR.ma) {
          fadLadViolations += 1;
          issues.push({severity: 'warning', msg_key: 'quality.range_top_lt_base'});
        }
      }
    }
  }
  if (fadLadTotal > 0) {
    checks += 1;
    if (fadLadViolations === 0) {
      passed += 1;
    } else {
      passed += Math.max(0.0, 1.0 - fadLadViolations / fadLadTotal);
    }
  }

  // Columnar-section bed-order check.
  let bedTotal = 0;
  let bedViolations = 0;
  const sects = data && Array.isArray(data.sections) ? data.sections : [];
  for (const sec of sects) {
    if (!sec || typeof sec !== 'object') continue;
    const blocks = []
      .concat(Array.isArray(sec.lithology_blocks) ? sec.lithology_blocks : [])
      .concat(Array.isArray(sec.age_units) ? sec.age_units : []);
    for (const block of blocks) {
      if (!block || typeof block !== 'object') continue;
      const top = _parseBedN(block.range_top_idx);
      const base = _parseBedN(block.range_base_idx);
      if (top === null || base === null) continue;
      bedTotal += 1;
      if (top < base) {
        bedViolations += 1;
        issues.push({severity: 'warning', msg_key: 'quality.bed_index_order_invalid'});
      }
      // REVIEW-2026-09-10: `_warning` is a bare string for one flag and a
      // LIST when several fire together (e.g. ["range_top_idx_truncated",
      // "index_order_swap"]); the strict === missed the swap in exactly that
      // combination. Mirrors _warning_flags in rca_core/quality.py.
      if (rcaWarningFlags(block._warning).indexOf('index_order_swap') !== -1) {
        issues.push({severity: 'info', msg_key: 'quality.bed_index_order_swapped'});
      }
    }
  }
  if (bedTotal > 0) {
    checks += 1;
    if (bedViolations === 0) {
      passed += 1;
    } else {
      passed += Math.max(0.0, 1.0 - bedViolations / bedTotal);
    }
  }

  // Biozone section refs.
  if (Array.isArray(data && data.biozones)) {
    for (const bz of data.biozones) {
      if (!bz || typeof bz !== 'object') continue;
      const sec = String(bz.section || '').trim();
      if (sec && sectionNamesSet.size > 0 && !sectionNamesSet.has(sec)) {
        issues.push({severity: 'info', msg_key: 'quality.biozone_section_mismatch'});
        break;
      }
    }
  }

  // agreement_count <= runs.
  for (const row of speciesRows) {
    if (!row || typeof row !== 'object') continue;
    const ac = row.agreement_count;
    if (ac === undefined || ac === null) continue;
    const rowRun = row.runs;
    const n = (rowRun !== undefined && rowRun !== null) ? rowRun
             : (data && data.runs !== undefined ? data.runs : null);
    if (n === undefined || n === null) continue;
    try {
      if (parseInt(ac, 10) > parseInt(n, 10)) {
        issues.push({severity: 'warning', msg_key: 'quality.agreement_exceeds_runs'});
        break;
      }
    } catch (_) { /* non-numeric — skip */ }
  }

  // M-1 fix: cross-era consistency check.
  // Any section containing blocks from more than one era (Paleozoic /
  // Mesozoic / Cenozoic) gets a warning (NOT a hard error — boundary
  // sections such as P/T or K/Pg legitimately span eras).
  const paleozoicRe2 = /\b(cambrian|ordovician|silurian|devonian|carboniferous|pennsylvanian|mississippian|permian)\b/i;
  const mesozoicRe2 = /\b(triassic|jurassic|cretaceous)\b/i;
  const cenozoicRe2 = /\b(paleocene|paleogene|neogene|quaternary|pleistocene|holocene|eocene|oligocene|miocene|pliocene)\b/i;
  const sects2 = data && Array.isArray(data.sections) ? data.sections : [];
  const erasBySection = {};
  for (const sec of sects2) {
    if (!sec || typeof sec !== 'object') continue;
    const secName = String(sec.name || sec.id || '').trim();
    if (!secName) continue;
    const blocks = []
      .concat(Array.isArray(sec.lithology_blocks) ? sec.lithology_blocks : [])
      .concat(Array.isArray(sec.age_units) ? sec.age_units : []);
    const eras = new Set();
    for (const b of blocks) {
      if (!b || typeof b !== 'object') continue;
      const t = String(b.age || '');
      if (paleozoicRe2.test(t)) eras.add('Paleozoic');
      if (mesozoicRe2.test(t)) eras.add('Mesozoic');
      if (cenozoicRe2.test(t)) eras.add('Cenozoic');
    }
    if (eras.size > 0) erasBySection[secName] = eras;
  }
  let crossEraCount2 = 0;
  for (const k of Object.keys(erasBySection)) {
    if (erasBySection[k].size > 1) {
      crossEraCount2 += 1;
      issues.push({severity: 'warning', msg_key: 'quality.ages_inconsistent', params: {count: String(crossEraCount2)}});
    }
  }
  if (crossEraCount2 > 0) {
    checks += 1;
    // REVIEW-2026-07-31: mirror rca_core/quality.py — proportional
    // penalty (1 - 0.5 per violating section), NOT a flat 0. Boundary
    // sections (K/Pg, P/Tr) legitimately span eras; grading them F was a
    // false penalty the Python side never applied.
    passed += Math.max(0.0, 1.0 - 0.5 * crossEraCount2);
  }

  // M-1 fix: ICS-based stage-order check (parity with
  // rca_core/quality.py:_score_cross_era_accuracy). Bundle a small
  // ICS table on globalThis (RCA_ICS_TABLE) so this check works in
  // pure-frontend mode. If a section's age_range contains 2+ known
  // stages, check that older stages aren't listed AFTER younger ones.
  const stages = (typeof globalThis !== 'undefined' && globalThis.RCA_ICS_TABLE)
    ? globalThis.RCA_ICS_TABLE
    : null;
  if (stages) {
    // Mirror of rca_core/standards/ics.py:ics_age_compare — the Python oracle
    // compares the MIDPOINT of each stage's span, not its base, so a pair of
    // stages whose bases coincide with a third stage's top must order the same
    // way on both engines.
    const icsAgeCompare = (s1, s2) => {
      if (!stages[s1] || !stages[s2]) return null;
      const mid1 = ((stages[s1].base_ma || 0) + (stages[s1].top_ma || 0)) / 2;
      const mid2 = ((stages[s2].base_ma || 0) + (stages[s2].top_ma || 0)) / 2;
      if (mid1 > mid2) return -1;  // stage1 is OLDER (higher Ma)
      if (mid1 < mid2) return 1;   // stage1 is YOUNGER
      return 0;
    };
    for (const sec of sects2) {
      if (!sec || typeof sec !== 'object') continue;
      const secName = String(sec.name || sec.id || '').trim();
      if (!secName) continue;
      const ageRange = String(sec.age_range || '');
      // REVIEW-2026-09-20: go through the shared _icsParseAgeRange mirror so
      // repeated mentions of one stage ("Wuchiapingian to Changhsingian, see
      // Wuchiapingian") fold out exactly like Python's ics_parse_age_range.
      // Without the dedup the trailing repeat made the pair walker judge a
      // stage against itself and against a stage the text never juxtaposed.
      const found = _icsParseAgeRange(stages, ageRange);
      if (found.length < 2) continue;
      // M6 (REVIEW-2026-08-19): Proportional accuracy — every adjacent
      // pair contributes one check, and `passed` reflects the fraction
      // of valid pairs. The previous all-or-nothing binary ("0 or 1")
      // under-counted a section with 2 inversions in a 4-stage sequence
      // (would still read 0/1). Mirror rca_core/quality.py:
      // _score_cross_era_accuracy.
      let stageViolations = 0;
      let stageChecks = 0;
      for (let i = 0; i < found.length - 1; i += 1) {
        const cmp = icsAgeCompare(found[i], found[i + 1]);
        if (cmp === null) continue;
        stageChecks += 1;
        if (cmp > 0) {
          stageViolations += 1;
          issues.push({
            severity: 'warning', msg_key: 'quality.stage_order_reversed',
            params: {section: secName, detail: found[i] + ' above ' + found[i + 1]}
          });
        }
      }
      if (stageChecks > 0) {
        checks += 1;
        passed += stageViolations > 0 ? Math.max(0.0, 1.0 - stageViolations / stageChecks) : 1.0;
      }
    }
  }

  // M-1 fix: abundance-diagram SUM-TO-100 check (parity with
  // rca_core/quality.py:_score_abundance_sum). For each (sample) level,
  // the sum of all %-unit abundances should equal 100±5. Violations
  // deduct 0.05 each, capped at 0.3.
  const abSamples = (data && Array.isArray(data.abundances)) ? data.abundances : [];
  let sumViolCount = 0;
  if (abSamples.length > 0) {
    const levelSums = {};
    const levelIds = {};
    for (const e of abSamples) {
      if (!e || typeof e !== 'object') continue;
      const unit = String(e.abundance_unit || '').trim().toLowerCase();
      if (unit !== '%') continue;
      const p = Number(e.abundance);
      if (!Number.isFinite(p)) continue;
      const lvl = String(e.level || '');
      if (!lvl) continue;
      levelSums[lvl] = (levelSums[lvl] || 0) + p;
      if (!levelIds[lvl]) levelIds[lvl] = lvl;
    }
    const violations = [];
    for (const lvl of Object.keys(levelSums)) {
      const s = levelSums[lvl];
      if (s < 95 || s > 105) violations.push({sample: levelIds[lvl], sum: s});
    }
    if (violations.length > 0) {
      const deduction = Math.min(0.3, 0.05 * violations.length);
      // Degrade the accuracy score by deduction.
      passed = Math.max(0.0, passed - deduction);
      sumViolCount = violations.length;
      for (const v of violations.slice(0, 5)) {
        issues.push({
          severity: 'warning', msg_key: 'quality.abundance_sum_violation',
          params: {sample: v.sample, sum: String(Math.round(v.sum * 10) / 10)}
        });
      }
      issues.push({
        severity: 'info', msg_key: 'quality.abundance_sum_violation_count',
        params: {count: String(violations.length)}
      });
    }
  }

  if (checks === 0) return [1.0, issues];
  return [_clamp01(passed / checks), issues];
}

/**
 * consistency: cross-field / per-row invariants not covered by the other
 * three dimensions:
 *   (1) chimera_warnings from merge (aggregator sets this)
 *   (2) FAD <= LAD on every species_ranges row
 *   (3) agreement_count <= total_runs per row
 *   (4) every species_ranges row has a non-empty biozone label
 */
function scoreConsistency(data) {
  const issues = [];
  let score = 1.0;

  // (1) Chimera warnings from the merge.
  const chimeraWarnings = data && data.chimera_warnings;
  if (Array.isArray(chimeraWarnings) && chimeraWarnings.length > 0) {
    score -= Math.min(0.5, 0.1 * chimeraWarnings.length);
    for (const w of chimeraWarnings.slice(0, 5)) {
      if (w && typeof w === 'object') {
        issues.push({
          severity: 'warning',
          msg_key: 'quality.chimera_dropped',
          params: { row: JSON.stringify(w.row || {}).slice(0, 200) },
        });
      }
    }
  }

  const species = (data && Array.isArray(data.species_ranges)) ? data.species_ranges : [];

  if (species.length > 0) {
    // (2) FAD <= LAD per row (range_top >= range_base).
    // REVIEW-2026-07-31: age/stage bounds are validated numerically in
    // scoreAccuracy — skip them here so a valid age range like
    // base="300 Ma" / top="250 Ma" is not flagged as inverted by the
    // bed-index parse (parity with rca_core/quality.py).
    let fadViolations = 0;
    for (const sp of species) {
      if (!sp || typeof sp !== 'object') continue;
      const topRaw = sp.range_top;
      const baseRaw = sp.range_base;
      if (_looksLikeAge(topRaw) || _looksLikeAge(baseRaw)) continue;
      const top = _parseBedN(topRaw);
      const base = _parseBedN(baseRaw);
      if (top === null || base === null) continue;
      if (top < base) {
        fadViolations += 1;
      }
    }
    if (fadViolations > 0) {
      score -= Math.min(0.3, 0.1 * fadViolations);
      issues.push({
        severity: 'warning',
        msg_key: 'quality.fad_lt_lad',
        params: { count: String(fadViolations) },
      });
    }

    // (3) agreement_count <= total_runs per row.
    const totalRuns = (data && data.runs !== undefined && data.runs !== null) ? data.runs : null;
    let overAgreed = 0;
    if (Number.isInteger(totalRuns) && totalRuns > 0) {
      for (const sp of species) {
        if (!sp || typeof sp !== 'object') continue;
        const ac = sp.agreement_count;
        if (typeof ac === 'number' && ac > totalRuns) {
          overAgreed += 1;
        }
      }
    }
    if (overAgreed > 0) {
      score -= Math.min(0.3, 0.2 * overAgreed);
      issues.push({
        severity: 'warning',
        msg_key: 'quality.agreement_overflow',
        params: { count: String(overAgreed) },
      });
    }

    // (4) Every species row has a non-empty biozone label.
    let missingBiozone = 0;
    for (const sp of species) {
      if (!sp || typeof sp !== 'object') continue;
      const bz = sp.biozone;
      if (!bz || (typeof bz === 'string' && !bz.trim())) {
        missingBiozone += 1;
      }
    }
    if (missingBiozone > 0) {
      score -= Math.min(0.2, 0.05 * missingBiozone);
      issues.push({
        severity: 'warning',
        msg_key: 'quality.missing_biozone',
        params: { count: String(missingBiozone) },
      });
    }

    // M-1 fix: Steno's-Law biozone-order check (parity with
    // rca_core/quality.py:_score_biozone_order). For each section,
    // compare two species that carry biozone labels. If both labels
    // resolve to a stage, use ics_age_compare on base_ma as the real
    // stratigraphic order (NOT lexicographic).
    // REVIEW-2026-07-31: (a) mirror Python's _BIOZONE_STAGE_MAP so real
    // conodont/ammonite zone names ("N. optima Zone", "Clarkina
    // orientalis Zone") actually resolve; (b) fix the inverted
    // comparison — the LOWER-positioned species carrying a YOUNGER
    // biozone than the higher-positioned one is the violation, not the
    // other way around; (c) exclude rows without a bed position (the
    // Python side filters them too).
    const stages2 = (typeof globalThis !== 'undefined' && globalThis.RCA_ICS_TABLE)
      ? globalThis.RCA_ICS_TABLE : null;
    if (stages2) {
      const icsAgeCompare2 = (s1, s2) => {
        const a = stages2[s1]; const b = stages2[s2];
        if (!a || !b) return null;
        // Same oracle as Python's ics_age_compare: midpoint of the span.
        const midA = ((a.base_ma || 0) + (a.top_ma || 0)) / 2;
        const midB = ((b.base_ma || 0) + (b.top_ma || 0)) / 2;
        if (midA > midB) return -1;
        if (midA < midB) return 1;
        return 0;
      };
      // Same curated zone->stage map as rca_core/quality.py (species-
      // level keys; a bare "clarkina" key mis-assigns Wuchiapingian
      // zones to the Changhsingian).
      const biozoneStageMap = {
        'clarkina orientalis': 'Wuchiapingian',
        'clarkina leveni': 'Wuchiapingian',
        'clarkina subcarinata': 'Wuchiapingian',
        'clarkina guangyuanensis': 'Wuchiapingian',
        'clarkina transcaucasica': 'Wuchiapingian',
        'clarkina changxingensis': 'Changhsingian',
        'clarkina yini': 'Changhsingian',
        'clarkina meishanensis': 'Changhsingian',
        'clarkina optima': 'Changhsingian',
        'neogondolella changxingensis': 'Changhsingian',
        'neogondolella optima': 'Changhsingian',
        'n. optima': 'Changhsingian',
        'otoceras': 'Induan',
        'ophiceras': 'Induan',
        'griesbachian': 'Induan',
        'anasirabites': 'Olenekian',
        'subcolumbites': 'Olenekian',
      };
      const resolveBiozoneStage = (txt) => {
        const low = String(txt).toLowerCase();
        for (const key of Object.keys(biozoneStageMap)) {
          if (low.indexOf(key) !== -1 && stages2[biozoneStageMap[key]]) return biozoneStageMap[key];
        }
        for (const k of Object.keys(stages2)) {
          if (new RegExp('\\b' + _regExpEscape(k) + '\\b', 'i').test(low)) return k;
        }
        return null;
      };
      // Group species by section.name (only those that name a section
      // present in the result).
      const sectsForBio = (data && Array.isArray(data.sections)) ? data.sections : [];
      const sectionNames = new Set();
      for (const s of sectsForBio) if (s && s.name) sectionNames.add(String(s.name).trim());
      const bySection = new Map();
      for (const sp of species) {
        if (!sp || typeof sp !== 'object') continue;
        const secName = String(sp.section || '').trim();
        if (!sectionNames.has(secName)) continue;
        if (!bySection.has(secName)) bySection.set(secName, []);
        bySection.get(secName).push(sp);
      }
      let biozoneViol = 0;
      for (const [secName, sps] of bySection) {
        // M7 (REVIEW-2026-08-19): skip sections that have no age_range
        // anchor — without it the Steno's-law check is ungrounded and
        // synthesizes false violations. Mirror rca_core/quality.py
        // _score_steno_biozone.
        const secRow = sectsForBio.find((s) => s && s.name && String(s.name).trim() === secName);
        if (!secRow || !String(secRow.age_range || '').trim()) continue;
        // Only rows with an actual bed position participate (Python
        // excludes unpositioned rows rather than inventing an order).
        const positioned = sps
          .map(sp => ({ bed: _parseBedN(sp.range_top), sp }))
          .filter(x => x.bed !== null)
          .sort((a, b) => a.bed - b.bed)
          .map(x => x.sp);
        if (positioned.length < 2) continue;
        for (let i = 0; i < positioned.length - 1; i += 1) {
          // positioned is sorted by range_top ASC: positioned[i] sits
          // LOWER in the section (older stratigraphic position).
          const lowerSp = positioned[i];
          const upperSp = positioned[i + 1];
          const lowerBz = String(lowerSp.biozone || '').trim();
          const upperBz = String(upperSp.biozone || '').trim();
          if (!lowerBz || !upperBz) continue;
          const lowerStage = resolveBiozoneStage(lowerBz);
          const upperStage = resolveBiozoneStage(upperBz);
          if (!lowerStage || !upperStage) continue;
          const cmp2 = icsAgeCompare2(lowerStage, upperStage);
          if (cmp2 === null) continue;
          // Steno's Law: the LOWER-positioned species must NOT sit in a
          // YOUNGER biozone than the upper one. icsAgeCompare2 returns 1
          // when stage1 (lowerStage) is younger -> violation.
          if (cmp2 > 0) {
            biozoneViol += 1;
            issues.push({
              severity: 'warning',
              msg_key: 'quality.biozone_order_violation',
              params: {
                species: String(lowerSp.species || ''),
                younger_biozone: lowerBz,
                older_biozone: upperBz,
              },
            });
          }
        }
      }
      if (biozoneViol > 0) {
        score -= Math.min(0.3, 0.1 * biozoneViol);
      }
    }
  }

  score = Math.min(1.0, Math.max(0.0, score));
  return [score, issues];
}

/**
 * structure: _extras ratio, no unexpected nulls.  Mode-aware.
 */
function scoreStructure(data) {
  const issues = [];
  let checks = 0;
  let passed = 0;

  const extras = data ? data._extras : undefined;
  if (extras !== undefined && extras !== null && typeof extras === 'object' && !Array.isArray(extras)) {
    checks += 1;
    const nExtras = Object.keys(extras).length;
    if (nExtras <= 3) {
      passed += 1;
    } else {
      issues.push({severity: 'info', msg_key: 'quality.many_extras'});
    }
  } else {
    checks += 1;
    passed += 1;
  }

  const mode = _detectMode(data);
  let nullCheckKeys;
  if (mode === 'columnar') {
    nullCheckKeys = ['sections', 'cross_beds', 'lithology_legend', 'fossil_legend', 'confidence'];
  } else if (mode === 'abundance') {
    nullCheckKeys = ['sections', 'abundances', 'confidence'];
  } else {
    nullCheckKeys = ['sections', 'biozones', 'other_fossils', 'confidence'];
  }

  checks += 1;
  let nulls = 0;
  for (const key of nullCheckKeys) {
    const val = data ? data[key] : undefined;
    if ((val === undefined || val === null) && key !== 'confidence') {
      nulls += 1;
    }
  }
  if (nulls === 0) {
    passed += 1;
  } else if (nulls <= 1) {
    passed += 0.5;
    issues.push({severity: 'info', msg_key: 'quality.null_fields'});
  }

  if (checks === 0) return [1.0, issues];
  return [_clamp01(passed / checks), issues];
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Score a normalised result dict.
 *
 * @param {Object|null} data  — the data field of an ExtractResult, or
 *                                the merged result from js/aggregate.js
 * @returns {{score: number, grade: string, issues: Array}}
 */
function scoreRangeChart(data) {
  // Mirrors Python: `if not data or not isinstance(data, dict)` — but in JS
  // `!{}` is FALSE for an empty object, so we explicitly check for null /
  // non-object / array / empty-object (arrays are "objects" in JS but not
  // in Python).
  if (data === null || data === undefined
      || typeof data !== 'object'
      || Array.isArray(data)
      || Object.keys(data).length === 0) {
    return {
      score: 0.0,
      grade: 'F',
      issues: [{severity: 'warning', msg_key: 'quality.invalid_result'}],
    };
  }

  // C5 fix (parity with Python): pure-extraction-miss -> 0.0 / F.
  if (_isPureExtractionMiss(data)) {
    return {
      score: 0.0,
      grade: 'F',
      issues: [{severity: 'warning', msg_key: 'quality.empty_result'}],
    };
  }

  const [cScore, cIssues] = scoreCompleteness(data);
  const [aScore, aIssues] = scoreAccuracy(data);
  const [kScore, kIssues] = scoreConsistency(data);
  const [sScore, sIssues] = scoreStructure(data);

  const composite = (
    W_COMPLETENESS * cScore
    + W_ACCURACY    * aScore
    + W_CONSISTENCY * kScore
    + W_STRUCTURE   * sScore
  );
  const clamped = _clamp01(composite);

  const allIssues = [...cIssues, ...aIssues, ...kIssues, ...sIssues];
  return {
    score: Math.round(clamped * 10000) / 10000,
    grade: gradeFor(clamped),
    issues: allIssues,
  };
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

// CommonJS / ES module compatibility.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    scoreRangeChart,
    scoreCompleteness,
    scoreAccuracy,
    scoreConsistency,
    scoreStructure,
    gradeFor,
  };
}
// Also expose as a global for plain <script> inclusion.
if (typeof window !== 'undefined') {
  window.scoreRangeChart = scoreRangeChart;
  window.scoreCompleteness = scoreCompleteness;
  window.scoreAccuracy    = scoreAccuracy;
  window.scoreConsistency = scoreConsistency;
  window.scoreStructure   = scoreStructure;
  window.gradeFor         = gradeFor;
}
