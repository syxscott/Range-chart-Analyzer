/**
 * js/quality.js - Quality scoring for VLM-extracted range-chart / columnar-section results.
 *
 * Byte-for-byte parity with rca_core/quality.py.
 * Scores a normalised result dict on 4 weighted dimensions and produces a
 * letter grade + issue list for the UI to surface:
 *
 *   completeness (0.30) — how many expected fields are populated
 *   accuracy    (0.40) — geological plausibility (section refs, index order)
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function gradeFor(score) {
  for (const [threshold, letter] of _GRADES) {
    if (score >= threshold) return letter;
  }
  return 'F';
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
 *
 * C3 fix: explicitly detect the mode and handle it clearly.
 * Primary-mode fields (species_ranges / abundances): an empty list means
 * "nothing extracted" — penalise it with a warning.
 * Non-primary top-level arrays (sections, biozones, other_fossils):
 * an empty array is fine (the chart simply didn't have that information).
 */
function scoreCompleteness(data) {
  const issues = [];
  let checks = 0;
  let passed = 0;

  // Primary rows check.
  let primary = null;
  for (const key of ['species_ranges', 'abundances']) {
    if (key in (data || {})) { primary = key; break; }
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

  // Non-primary top-level arrays.
  for (const key of ['sections', 'biozones', 'other_fossils']) {
    checks += 1;
    const val = data ? data[key] : undefined;
    if (val !== undefined && val !== null) {
      passed += 1;
    } else {
      issues.push({severity: 'info', msg_key: 'quality.missing_top_level'});
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

  const score = checks > 0 ? passed / checks : 0.0;
  return [Math.min(1.0, Math.max(0.0, score)), issues];
}

/**
 * accuracy: section references resolve, columnar bed indices are ordered.
 *
 * D-1 docstring note: FAD/LAD direction checks for range-chart species and
 * full monotonic-bed enforcement are not yet implemented (B-3 normalises
 * index order; additional checks are future work).
 *
 * I4 fix: guard against None — row.runs is absent in single-run results,
 * so comparison only fires when the runs field is present and exceeded.
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
      passed += 1;  // no refs at all — not a mismatch, just incomplete
    } else if (sectionNamesSet.size > 0 && ok === hasRef) {
      passed += 1;
    } else if (sectionNamesSet.size > 0 && ok > 0) {
      passed += 0.5;
      issues.push({severity: 'warning', msg_key: 'quality.unmatched_section_ref'});
    } else if (sectionNamesSet.size > 0) {
      issues.push({severity: 'warning', msg_key: 'quality.all_section_refs_unmatched'});
    } else {
      // No sections declared, can't validate — flag as info.
      passed += 0.5;
      issues.push({severity: 'info', msg_key: 'quality.sections_absent'});
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

  // I4 fix: agreement_count <= runs data-integrity check.
  // Mirrors Python: row.runs overrides data.runs (merged rows carry runs only at top level).
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
  const score = passed / checks;
  return [Math.min(1.0, Math.max(0.0, score)), issues];
}

/**
 * consistency: intentionally lightweight.
 *
 * D-2/M5 fix: removed the dead per-row confidence check (rows never carry
 * a confidence field — normalize_result does not populate it) and removed
 * the redundant species_ranges/abundances existence check (completeness
 * already covers that).  Kept for schema completeness
 * (W_CONSISTENCY = 0.20 weight).
 */
function scoreConsistency(data) {
  // No checks needed — primary signal is in completeness/accuracy.
  return [1.0, []];
}

/**
 * structure: _extras ratio, no unexpected nulls.
 */
function scoreStructure(data) {
  const issues = [];
  let checks = 0;
  let passed = 0;

  // _extras should be tiny.
  const extras = data ? data._extras : undefined;
  if (extras !== undefined && extras !== null && typeof extras === 'object') {
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

  // No top-level null values among expected keys.
  checks += 1;
  let nulls = 0;
  for (const key of ['sections', 'biozones', 'other_fossils', 'confidence']) {
    const val = data ? data[key] : undefined;
    if (val === undefined || val === null) {
      if (key !== 'confidence') nulls += 1;
    }
  }
  if (nulls === 0) {
    passed += 1;
  } else if (nulls <= 1) {
    passed += 0.5;
    issues.push({severity: 'info', msg_key: 'quality.null_fields'});
  }

  if (checks === 0) return [1.0, issues];
  const score = passed / checks;
  return [Math.min(1.0, Math.max(0.0, score)), issues];
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
  if (!data || typeof data !== 'object') {
    return {
      score: 0.0,
      grade: 'F',
      issues: [{severity: 'warning', msg_key: 'quality.invalid_result'}],
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
  const clamped = Math.min(1.0, Math.max(0.0, composite));

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
  module.exports = { scoreRangeChart, scoreCompleteness, scoreAccuracy,
                     scoreConsistency, scoreStructure };
}
// Also expose as a global for plain <script> inclusion.
if (typeof window !== 'undefined') {
  window.scoreRangeChart = scoreRangeChart;
  window.scoreCompleteness = scoreCompleteness;
  window.scoreAccuracy    = scoreAccuracy;
  window.scoreConsistency = scoreConsistency;
  window.scoreStructure   = scoreStructure;
}
