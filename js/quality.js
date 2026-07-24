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
const _CONTENT_KEYS = [
  'species_ranges', 'abundances', 'sections', 'biozones',
  'other_fossils', 'cross_beds', 'lithology_legend',
  'fossil_legend', 'age_units',
];

// Columnar-mode marker keys — unique to columnar-section extraction.
const _COLUMNAR_MARKERS = [
  'cross_beds', 'lithology_legend', 'fossil_legend',
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
  let fadLadTotal = 0;
  let fadLadViolations = 0;
  for (const row of speciesRows) {
    if (!row || typeof row !== 'object') continue;
    const top = _parseBedN(row.range_top);
    const base = _parseBedN(row.range_base);
    if (top === null || base === null) continue;
    fadLadTotal += 1;
    if (top < base) {
      fadLadViolations += 1;
      issues.push({severity: 'warning', msg_key: 'quality.range_top_lt_base'});
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
      if (block._warning === 'index_order_swap') {
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

  if (checks === 0) return [1.0, issues];
  return [_clamp01(passed / checks), issues];
}

/**
 * consistency: intentionally lightweight.
 */
function scoreConsistency(data) {
  return [1.0, []];
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
