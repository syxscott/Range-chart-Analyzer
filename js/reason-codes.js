// reason-codes.js - coverage reason codes and the
// "answered / not drawn / silently missing" ledger.
//
// Browser mirror of ``rca_core/reason_codes.py`` — BORROW-2026-09-20 (A).
// 2026-09-20: added together with the js/aggregate.js + js/quality.js wiring,
// so the pure-frontend (direct/proxy) transport accounts for a cell exactly
// like the Python (GUI/backend) transport does.
//
// The borrowed insight is a bookkeeping discipline, not an algorithm: a
// digitised table cell must record *why* it holds what it holds, and three
// totally different situations must never collapse into the same empty string:
//
//   1. something is drawn and it was read              -> response_kind "extracted"
//   2. the level exists but the author drew a dash ON
//      PURPOSE ("not drawn")                           -> response_kind "not_drawn"
//   3. the model simply did not answer that cell       -> silently missing
//
// Case 2 is a POSITIVE observation ("the chart says this taxon is absent
// here") and counts as an answered cell; case 3 is a gap.  Before this module
// both looked like "" and a run that honestly reported 40 dashes scored WORSE
// on coverage than one that quietly omitted those rows.
//
// Every field is OPTIONAL and additive: all helpers tolerate their absence
// (legacy results, other modes, hand-edited rows) and ``coverage_state`` falls
// back to classifying a row from the values it does carry.
//
// Contract (kept 1:1 with the Python docstring):
//   * ``response_kind``: "extracted" | "not_drawn" | "uncertain"
//   * ``reason_codes``:  list of the 12 slugs in RCA_REASON_CODE_SLUGS
//   * merge: ``reason_codes`` union; ``response_kind`` by
//     RCA_RESPONSE_KIND_PRECEDENCE (extracted > uncertain > not_drawn — "if any
//     run saw it drawn, it is not 'not drawn'"), divergence flagged.
'use strict';

// ---------------------------------------------------------------------------
// The code catalogue
// ---------------------------------------------------------------------------

/**
 * One reason code: a stable machine slug plus a one-line English gloss.
 *
 * ``family`` groups the codes so a UI can colour them without hard-coding the
 * 12 slugs (coverage = "did it answer", geometry = "where the mark ends",
 * source = "where the claim comes from", quality = "how unsure").
 * Mirrors ``rca_core/reason_codes.py:REASON_CODES`` verbatim, order included.
 */
const RCA_REASON_CODES = [
  {
    slug: 'not_drawn',
    summary: 'The column/level exists in the chart but this taxon is deliberately not drawn there (blank cell, dash or em-dash in place of a range).',
    family: 'coverage',
  },
  {
    slug: 'uncertain',
    summary: 'Something is drawn for this cell but its boundary or value could not be read confidently.',
    family: 'coverage',
  },
  {
    slug: 'obscured',
    summary: 'The mark or its label is covered by another element, a caption, a fold or print bleed.',
    family: 'source',
  },
  {
    slug: 'inferred',
    summary: 'The value was interpolated from adjacent evidence instead of read directly off the chart.',
    family: 'source',
  },
  {
    slug: 'legend_only',
    summary: "The name appears only in the legend, caption or a species list; it is not plotted in the figure body.",
    family: 'source',
  },
  {
    slug: 'crosses_top',
    summary: 'The range or curve runs off the top (youngest) edge of the frame, so the true upper limit is unknown.',
    family: 'geometry',
  },
  {
    slug: 'crosses_base',
    summary: 'The range or curve runs off the bottom (oldest) edge of the frame, so the true lower limit is unknown.',
    family: 'geometry',
  },
  {
    slug: 'truncated',
    summary: 'The mark, its label or the column itself is cut off by the figure edge, a panel split or a page break.',
    family: 'geometry',
  },
  {
    slug: 'no_label',
    summary: 'A mark is drawn but no bed/level/tick label exists to attach a value to it.',
    family: 'quality',
  },
  {
    slug: 'abbreviated',
    summary: "The name or value is abbreviated on the chart as printed ('Gen. sp.', 'cf.', 'sp.'), so the full form is not recoverable.",
    family: 'quality',
  },
  {
    slug: 'low_confidence',
    summary: 'Best-effort value emitted at reduced confidence; see the row note.',
    family: 'quality',
  },
  {
    slug: 'out_of_scope',
    summary: "The cell was not requested (taxon/level outside the requested list) or belongs to another chart mode's table.",
    family: 'coverage',
  },
];

// Lookup maps are built with Object.create(null) ON PURPOSE: a plain literal
// inherits from Object.prototype, so a model-emitted code spelled
// "constructor" / "toString" / "__proto__" would hit an inherited property and
// be accepted as a valid slug. A Python ``dict`` has no such entries, and
// neither may this mirror.
const RCA_REASON_CODES_BY_SLUG = Object.create(null);
const _REASON_CODE_BY_IDENTITY = new Map();
for (const _entry of RCA_REASON_CODES) {
  RCA_REASON_CODES_BY_SLUG[_entry.slug] = _entry;
  _REASON_CODE_BY_IDENTITY.set(_entry, _entry);
}

/** The 12 legal slugs (Set mirror of the Python ``frozenset``). */
const RCA_REASON_CODE_SLUGS = new Set(RCA_REASON_CODES.map((c) => c.slug));

/**
 * The code the parser adds by itself when it throws away derived evidence (an
 * out-of-range 0-999 position, or one that contradicts the semantic value).
 * Never a model-facing claim — see the geometry guard in extractor.py.
 */
const RCA_LOW_CONFIDENCE_CODE = 'low_confidence';

// Slugs the model tends to spell differently. Normalisation is deliberately
// conservative: only these known variants are folded, unknown codes are DROPPED
// (a typo must not silently become a new category, and an unrecognised code is
// a model hallucination that must not inflate the ledger).
const _CODE_ALIASES = Object.assign(Object.create(null), {
  notdrawn: 'not_drawn',
  'not-drawn': 'not_drawn',
  'not drawn': 'not_drawn',
  undrawn: 'not_drawn',
  blank: 'not_drawn',
  dash: 'not_drawn',
  no_range: 'not_drawn',
  unclear: 'uncertain',
  ambiguous: 'uncertain',
  illegible: 'uncertain',
  hidden: 'obscured',
  covered: 'obscured',
  interpolated: 'inferred',
  legend: 'legend_only',
  off_top: 'crosses_top',
  exceeds_top: 'crosses_top',
  off_base: 'crosses_base',
  off_bottom: 'crosses_base',
  exceeds_base: 'crosses_base',
  cut_off: 'truncated',
  cutoff: 'truncated',
  unlabeled: 'no_label',
  unlabelled: 'no_label',
  abbreviation: 'abbreviated',
  abbr: 'abbreviated',
  'low-confidence': 'low_confidence',
  lowconf: 'low_confidence',
  'out-of-scope': 'out_of_scope',
  outside_scope: 'out_of_scope',
});

// ---------------------------------------------------------------------------
// The three-state answer
// ---------------------------------------------------------------------------

const RCA_RESPONSE_EXTRACTED = 'extracted';
const RCA_RESPONSE_NOT_DRAWN = 'not_drawn';
const RCA_RESPONSE_UNCERTAIN = 'uncertain';

/** The three states a cell can be ANSWERED in. */
const RCA_RESPONSE_KINDS = new Set(
  [RCA_RESPONSE_EXTRACTED, RCA_RESPONSE_NOT_DRAWN, RCA_RESPONSE_UNCERTAIN]);

/**
 * Cells "answered" by the model, ranked. Higher rank wins a merge conflict: a
 * single run that saw a drawn range outweighs any number of runs that reported
 * a dash ("有画就不算未画"), and "I saw something but cannot read it" outranks
 * "nothing is drawn" for the same reason.
 */
const RCA_RESPONSE_KIND_PRECEDENCE = Object.assign(Object.create(null), {
  not_drawn: 1,
  uncertain: 2,
  extracted: 3,
});

/** Synthetic state for a cell nobody answered. Never written to a row. */
const RCA_SILENT_MISSING = 'silent_missing';

// Row fields that carry an actual reading, used to classify legacy rows that
// predate ``response_kind`` (and modes that do not emit it).
const _VALUE_BEARING_KEYS = [
  'range_top', 'range_base', 'range_top_idx', 'range_base_idx',
  'abundance', 'depth', 'depth_m', 'age_ma', 'top_age', 'base_age',
  'thickness_m', 'value', 'values', 'x', 'y', 'level_range',
];

const _KIND_ALIASES = Object.assign(Object.create(null), {
  extracted: 'extracted',
  extract: 'extracted',
  read: 'extracted',
  value: 'extracted',
  drawn: 'extracted',
  observed: 'extracted',
  not_drawn: 'not_drawn',
  notdrawn: 'not_drawn',
  'not-drawn': 'not_drawn',
  'not drawn': 'not_drawn',
  undrawn: 'not_drawn',
  blank: 'not_drawn',
  dash: 'not_drawn',
  uncertain: 'uncertain',
  unsure: 'uncertain',
  unclear: 'uncertain',
  ambiguous: 'uncertain',
});

// "Column" = the horizontal identity of a cell (which taxon), "stratum" = the
// vertical identity (which section / level / site). Both are read from the
// first key that carries text, so one helper serves every mode.
const RCA_DEFAULT_COLUMN_KEYS = ['species', 'taxon', 'name'];
const RCA_DEFAULT_STRATUM_KEYS = ['section', 'site', 'zonation', 'level'];

// Above this many grid cells the cross-product is skipped (a 500-taxon x
// 200-level diagram would allocate 100k entries to answer a question nobody
// can act on). The emitted-cell statistics are always computed.
const RCA_MAX_LEDGER_CELLS = 20000;

// ---------------------------------------------------------------------------
// Shared primitives (Python semantics, deliberately explicit)
// ---------------------------------------------------------------------------

/** True for plain objects that are neither arrays nor null (Python ``dict``). */
function rcaIsMapping(value) {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Python truthiness (``bool(x)``), which the contract uses in three places —
 * ``values if v``, ``row.get("reason_codes") or row.get("reason_code")`` and
 * ``geometry.get("points")``. JS ``if (x)`` differs on NaN (Python: truthy) and
 * on empty containers, so the rule lives in one helper.
 */
function rcaContractTruthy(value) {
  if (value === null || value === undefined) return false;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return !Number.isNaN(value) && value !== 0;
  if (typeof value === 'string') return value.length > 0;
  if (typeof value === 'function') return true;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(value).length > 0;
  return true;  // bigint / symbol: Python has no equivalent, treat as truthy
}

/**
 * Python ``round(value, digits)`` — correct decimal rounding of the EXACT
 * binary value, ties-to-even. ``Math.round(x * 10000) / 10000`` is NOT that:
 * it rounds halves away from zero, so 1/32 = 0.03125 yields 0.0313 here but
 * 0.0312 in Python (a 32-cell grid is a normal chart, so this is reachable).
 * The double is decomposed exactly (mantissa * 2**e -> N / 10**d) and rounded
 * with BigInt arithmetic, which reproduces CPython's ``float.__round__``.
 */
function rcaPyRound(value, digits) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return value;
  if (value === 0) return value;
  const sign = value < 0 ? -1 : 1;
  const abs = Math.abs(value);
  const buf = new ArrayBuffer(8);
  const view = new DataView(buf);
  view.setFloat64(0, abs, false);
  const hi = view.getUint32(0, false);
  const lo = view.getUint32(4, false);
  const biased = (hi & 0x7ff00000) >>> 20;
  const frac = (hi & 0x000fffff) * 4294967296 + lo;   // < 2**53, exact
  let mantissa;
  let exponent;
  if (biased === 0) {           // subnormal: value = frac * 2**-1074
    mantissa = frac;
    exponent = -1074;
  } else {
    mantissa = frac + 4503599627370496;   // implicit leading 1 (2**52)
    exponent = biased - 1075;
  }
  if (exponent >= 0) return value;      // an integer: exact at any digits >= 0
  const decimals = -exponent;           // exact decimal expansion length
  if (decimals <= digits) return value; // already representable in `digits`
  let n = BigInt(mantissa);
  let five = 1n;
  for (let i = 0; i < decimals; i += 1) five = five * 5n;
  n = n * five;                       // value === n / 10**decimals, exactly
  const shift = decimals - digits;
  let divisor = 1n;
  for (let i = 0; i < shift; i += 1) divisor = divisor * 10n;
  let q = n / divisor;
  const r = n % divisor;
  const half = divisor / 2n;            // divisor is an even power of ten
  if (r > half) q += 1n;
  else if (r === half && (q % 2n) !== 0n) q += 1n;   // ties -> even
  // ONE division by an exact power of ten (10**d is exact for d <= 22): IEEE
  // division is correctly rounded, so this yields the double nearest the
  // rounded decimal, which is what Python's round() returns. Repeated /10
  // would round twice.
  return sign * (Number(q) / Math.pow(10, digits));
}

// ---------------------------------------------------------------------------
// Normalisers
// ---------------------------------------------------------------------------

/** True for exactly the slugs in RCA_REASON_CODE_SLUGS (case-insensitive). */
function rcaIsValidReasonCode(code) {
  return typeof code === 'string' && RCA_REASON_CODE_SLUGS.has(code.trim().toLowerCase());
}

/**
 * Canonical slug for ``value``, or ``null`` when unknown. Accepts a slug, an
 * alias, or one of the RCA_REASON_CODES catalogue records (the JS shape of the
 * Python ``ReasonCode`` dataclass — matched by IDENTITY, so a model-emitted
 * ``{"slug": "not_drawn"}`` dict is dropped exactly like Python drops it).
 */
function rcaNormalizeReasonCode(value) {
  const known = _REASON_CODE_BY_IDENTITY.get(value);
  if (known) return known.slug;
  if (typeof value !== 'string') return null;
  const text = value.trim().toLowerCase();
  if (!text) return null;
  if (RCA_REASON_CODE_SLUGS.has(text)) return text;
  const alias = _CODE_ALIASES[text];
  return alias === undefined ? null : alias;
}

/**
 * Coerce anything the model emitted into a de-duplicated list of slugs.
 * Accepts an array, a comma/semicolon separated string, a single slug, an
 * object (``{"not_drawn": true}`` -> its keys) or null. Order of first
 * appearance is preserved so the UI shows the codes in the order claimed.
 *
 * FIX-2026-09-22 (audit item 8), mirror of ``normalize_codes`` in
 * rca_core/reason_codes.py: in the object shape a boolean value is an
 * assertion, so ``{"obscured": false}`` DENIES the code and the key is
 * dropped. Only the exact ``false`` denies — ``0``, ``""`` and ``null`` are
 * not claims either way and keep the historical behaviour (``===`` matches
 * Python's ``is not False`` on JSON-parsed data).
 */
function rcaNormalizeReasonCodes(values) {
  if (values === null || values === undefined) return [];
  let items;
  if (Array.isArray(values)) {
    items = values.slice();
  } else if (typeof values === 'string') {
    const text = values.trim();
    if (!text) return [];
    items = text.split(';').join(',').split(',').filter((p) => p.trim());
  } else if (rcaIsMapping(values)) {
    items = Object.keys(values).filter((k) => values[k] !== false);
  } else {
    items = [values];
  }
  const out = [];
  for (const item of items) {
    const slug = rcaNormalizeReasonCode(item);
    if (slug && out.indexOf(slug) === -1) out.push(slug);
  }
  return out;
}

/** One-line English gloss for a slug (the slug itself when unknown). */
function rcaReasonCodeSummary(code) {
  const key = String(code || '').trim().toLowerCase();
  const entry = RCA_REASON_CODES_BY_SLUG[key];
  if (entry) return entry.summary;
  return String(code || '');
}

/** Render a code list for a report / evidence chain: ``"not_drawn, obscured"``. */
function rcaRenderReasonCodes(codes) {
  return rcaNormalizeReasonCodes(codes).join(', ');
}

/**
 * One of the three response kinds, or ``null`` when absent/unknown. An unknown
 * word is NOT guessed: a silent omission has to stay detectable as one, which
 * is the whole point of the ledger.
 */
function rcaNormalizeResponseKind(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim().toLowerCase();
  if (!text) return null;
  const kind = _KIND_ALIASES[text];
  return kind === undefined ? null : kind;
}

// ---------------------------------------------------------------------------
// The three-state answer
// ---------------------------------------------------------------------------

/** True when a row carries at least one readable value (any mode's field). */
function rcaRowHasValue(row) {
  if (!rcaIsMapping(row)) return false;
  for (const key of _VALUE_BEARING_KEYS) {
    const val = row[key];
    if (val === null || val === undefined) continue;
    if (typeof val === 'string' || typeof val === 'number'
        || typeof val === 'boolean') {
      // Python tests ``str(val).strip()``: False renders "False", so a real
      // boolean — and a 0 — are readings, not gaps.
      if (String(val).trim()) return true;
    } else if (Array.isArray(val) || rcaIsMapping(val)) {
      if (Object.keys(val).length > 0) return true;
    } else {
      return true;  // pragma: defensive, mirrors the Python else-branch
    }
  }
  return false;
}

/**
 * Classify one row as extracted / not_drawn / uncertain / silent_missing.
 * Explicit beats inferred: a row without ``response_kind`` that carries a
 * reading counts as extracted (legacy results stay usable); a row without
 * either is a genuine gap — exactly the case the ledger exists to expose.
 */
function rcaCoverageState(row) {
  if (!rcaIsMapping(row)) return RCA_SILENT_MISSING;
  const kind = rcaNormalizeResponseKind(row.response_kind);
  if (kind) return kind;
  return rcaRowHasValue(row) ? RCA_RESPONSE_EXTRACTED : RCA_SILENT_MISSING;
}

/** True when the row is an answer — including the honest ``not_drawn``. */
function rcaIsAnswered(row) {
  return RCA_RESPONSE_KINDS.has(rcaCoverageState(row));
}

// ---------------------------------------------------------------------------
// Row identity
// ---------------------------------------------------------------------------

function _rowLabel(row, keys) {
  for (const key of (keys || [])) {
    const val = row[key];
    if (typeof val === 'string' && val.trim()) return val.trim();
    if (typeof val === 'number') return String(val);
    // KNOWN residual divergence (documented, not fixable here): Python
    // ``str(3.0)`` is "3.0" while JSON.parse already collapsed the number to
    // JS 3 -> "3". A numeric column/stratum label is a model hiccup; the
    // prompts ask for names, and the same class is documented for
    // rcaStrForMerge() in js/aggregate.js.
  }
  return '';
}

/** The ledger column label of a row (taxon / species / table row name). */
function rcaRowColumn(row, keys) {
  if (!rcaIsMapping(row)) return '';
  return _rowLabel(row, keys || RCA_DEFAULT_COLUMN_KEYS);
}

/** The ledger stratum label of a row (section / site / level). */
function rcaRowStratum(row, keys) {
  if (!rcaIsMapping(row)) return '';
  return _rowLabel(row, keys || RCA_DEFAULT_STRATUM_KEYS);
}

/** Human-readable identity of a row for an evidence listing. */
function rcaRowLabel(row, keys) {
  if (!rcaIsMapping(row)) return '';
  return rcaRowColumn(row, keys || RCA_DEFAULT_COLUMN_KEYS)
    || rcaRowStratum(row, RCA_DEFAULT_STRATUM_KEYS);
}

// ---------------------------------------------------------------------------
// Ledger
// ---------------------------------------------------------------------------

function _blankCounts() {
  const out = {};
  out[RCA_RESPONSE_EXTRACTED] = 0;
  out[RCA_RESPONSE_NOT_DRAWN] = 0;
  out[RCA_RESPONSE_UNCERTAIN] = 0;
  out[RCA_SILENT_MISSING] = 0;
  return out;
}

/**
 * Validate an ``expectedUnits`` denominator — mirror of
 * ``_coerce_expected_units`` in rca_core/reason_codes.py. Anything unusable
 * (null, booleans, ``NaN``/``Infinity``, a non-integral float, a non-numeric
 * string, a count <= 0) becomes ``null`` so the ledger keeps its historical
 * shape instead of guessing at a base.
 */
function _coerceExpectedUnits(value) {
  if (value === null || value === undefined || typeof value === 'boolean') {
    return null;
  }
  let units;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || !Number.isInteger(value)) return null;
    units = value;
  } else if (typeof value === 'string' && value.trim()) {
    // Python's side of this mirror validates with ``int(text)``, which rejects
    // any decimal point; ``Number("5.0")`` would not, so drop it here too.
    if (value.indexOf('.') !== -1) return null;
    const num = Number(value.trim());
    if (!Number.isFinite(num) || !Number.isInteger(num)) return null;
    units = num;
  } else {
    return null;
  }
  return units > 0 ? units : null;
}

function _finalizeLedger(counts) {
  // Every cell lands in exactly one of the four buckets, so the cell count is
  // the bucket sum - correct for the grid view and for the emitted-only
  // fallback below the RCA_MAX_LEDGER_CELLS guard.
  //
  // FIX-2026-09-22 (audit item 3) note, mirror of rca_core/reason_codes.py:
  // ``cells`` counts the rows that SURVIVED, so an upstream filter that deletes
  // not_drawn / uncertain / silent_missing rows RAISES both coverage ratios.
  // The shape below is the frozen Python/JS parity contract and stays as
  // published, but a caller that knows how many units were actually requested
  // can pin the denominator via ``rcaCoverageLedger(rows, {expectedUnits: n})``.
  const cells = counts[RCA_RESPONSE_EXTRACTED] + counts[RCA_RESPONSE_NOT_DRAWN]
    + counts[RCA_RESPONSE_UNCERTAIN] + counts[RCA_SILENT_MISSING];
  const answered = counts[RCA_RESPONSE_EXTRACTED] + counts[RCA_RESPONSE_NOT_DRAWN]
    + counts[RCA_RESPONSE_UNCERTAIN];
  // honest_coverage counts an explicit "the chart draws nothing here" as the
  // answer it is; strict_coverage still demands a readable value.
  return {
    cells: cells,
    extracted: counts[RCA_RESPONSE_EXTRACTED],
    not_drawn: counts[RCA_RESPONSE_NOT_DRAWN],
    uncertain: counts[RCA_RESPONSE_UNCERTAIN],
    silent_missing: counts[RCA_SILENT_MISSING],
    answered: answered,
    honest_coverage: cells ? rcaPyRound(answered / cells, 4) : 0.0,
    strict_coverage: cells ? rcaPyRound(counts[RCA_RESPONSE_EXTRACTED] / cells, 4) : 0.0,
  };
}

/**
 * Summarise which cells were answered, which were declared not drawn, and
 * which were silently skipped.
 *
 * @param {Array<Object>} rows  any iterable of row dicts (data.species_ranges,
 *                              data.abundances, ...) — malformed entries are
 *                              skipped, this never throws.
 * @param {Object} [options]    ``{columnKeys, stratumKeys, crossProduct,
 *                              expectedUnits}`` — ``expectedUnits`` is the
 *                              optional row-independent coverage denominator
 *                              (see the note in the totals block below).
 * @returns {Object} JSON-friendly ledger: ``{columns, strata, totals,
 *          reason_code_counts, explicit_responses, row_count,
 *          unattributed_rows, grid_used}``.
 *
 * The grid is the cross product of the observed columns x the observed strata;
 * a grid cell with no row at all is ``silent_missing``. When the model never
 * emits a stratum (single-section charts) the stratum set is ``{""}`` and the
 * grid degenerates to the emitted rows, so no phantom gaps appear.
 */
function rcaCoverageLedger(rows, options) {
  const opts = options || {};
  const columnKeys = opts.columnKeys || RCA_DEFAULT_COLUMN_KEYS;
  const stratumKeys = opts.stratumKeys || RCA_DEFAULT_STRATUM_KEYS;
  const crossProduct = opts.crossProduct === undefined
    ? true : !!opts.crossProduct;

  // Nested Map (column -> stratum -> state): a Map, because a plain object
  // would let a taxon literally named "constructor" hit Object.prototype.
  const cellState = new Map();
  const cellOrder = [];
  const columns = [];
  const strata = [];
  const codeCounts = new Map();
  let rowCount = 0;
  let unattributed = 0;
  let explicit = 0;

  const list = Array.isArray(rows) ? rows : [];
  for (const row of list) {
    if (!rcaIsMapping(row)) continue;
    rowCount += 1;
    if (rcaNormalizeResponseKind(row.response_kind)) explicit += 1;
    for (const code of rcaNormalizeReasonCodes(row.reason_codes)) {
      codeCounts.set(code, (codeCounts.get(code) || 0) + 1);
    }
    const column = rcaRowColumn(row, columnKeys);
    if (!column) {
      unattributed += 1;
      continue;
    }
    const stratum = rcaRowStratum(row, stratumKeys);
    const state = rcaCoverageState(row);
    if (columns.indexOf(column) === -1) columns.push(column);
    if (strata.indexOf(stratum) === -1) strata.push(stratum);
    let byStratum = cellState.get(column);
    if (!byStratum) { byStratum = new Map(); cellState.set(column, byStratum); }
    const prev = byStratum.get(stratum);
    if (prev === undefined) {
      byStratum.set(stratum, state);
      cellOrder.push([column, stratum]);
    } else if ((RCA_RESPONSE_KIND_PRECEDENCE[state] || 0)
               > (RCA_RESPONSE_KIND_PRECEDENCE[prev] || 0)) {
      // Several rows for the same cell: the strongest answer wins, so a
      // duplicated "not drawn" can never mask a drawn range.
      byStratum.set(stratum, state);
    }
  }

  const emittedCounts = _blankCounts();
  const perColumn = new Map();
  const perStratum = new Map();
  const bump = (bucket, label, state) => {
    let counts = bucket.get(label);
    if (!counts) { counts = _blankCounts(); bucket.set(label, counts); }
    counts[state] += 1;
  };

  let gridUsed = false;
  let gridCells = cellOrder;
  if (crossProduct && columns.length && strata.length
      && columns.length * strata.length <= RCA_MAX_LEDGER_CELLS) {
    gridUsed = true;
    gridCells = [];
    for (const c of columns) for (const s of strata) gridCells.push([c, s]);
  }

  for (const cell of gridCells) {
    const byStratum = cellState.get(cell[0]);
    const state = (byStratum && byStratum.get(cell[1])) || RCA_SILENT_MISSING;
    emittedCounts[state] += 1;
    bump(perColumn, cell[0], state);
    bump(perStratum, cell[1], state);
  }

  const totals = _finalizeLedger(emittedCounts);
  // FIX-2026-09-22 (audit item 3), mirror of coverage_ledger(expected_units=…):
  // an OPTIONAL row-independent denominator for callers that hold provenance
  // the rows cannot carry (how many cells the request itself covered). Every
  // other number here — cells included — is derived from the rows that
  // survived, so dropping not_drawn / uncertain / silent_missing rows used to
  // make coverage look better. When ``expectedUnits`` is a positive count the
  // two ratios are recomputed over max(observed, expected) and
  // ``observed_units`` / ``expected_units`` / ``unreported_units`` /
  // ``coverage_basis`` join ``totals``. Omit it and the output stays exactly
  // the historical shape: the key set is part of the frozen parity fixtures.
  const expected = _coerceExpectedUnits(opts.expectedUnits);
  if (expected !== null) {
    const observed = totals.cells;
    const basis = Math.max(observed, expected);
    totals.observed_units = observed;
    totals.expected_units = expected;
    totals.unreported_units = basis - observed;
    totals.coverage_basis = expected >= observed ? 'expected' : 'observed';
    totals.honest_coverage = basis ? rcaPyRound(totals.answered / basis, 4) : 0.0;
    totals.strict_coverage = basis
      ? rcaPyRound(totals.extracted / basis, 4) : 0.0;
  }
  totals.explicit_responses = explicit;
  totals.row_count = rowCount;
  totals.unattributed_rows = unattributed;

  // dict(sorted(code_counts.items())) — Python sorts by code point, JS by code
  // unit; identical for the ASCII slugs these keys always are.
  const reasonCodeCounts = {};
  for (const key of [...codeCounts.keys()].sort()) {
    reasonCodeCounts[key] = codeCounts.get(key);
  }

  return {
    columns: columns.map(
      (c) => Object.assign({ column: c }, _finalizeLedger(perColumn.get(c) || _blankCounts()))),
    strata: strata.map(
      (s) => Object.assign({ stratum: s }, _finalizeLedger(perStratum.get(s) || _blankCounts()))),
    totals: totals,
    reason_code_counts: reasonCodeCounts,
    explicit_responses: explicit,
    row_count: rowCount,
    unattributed_rows: unattributed,
    grid_used: gridUsed,
  };
}

// ---------------------------------------------------------------------------
// Merge rules (used by js/aggregate.js — mirror of aggregate.py's
// _merge_contract_field callers)
// ---------------------------------------------------------------------------

/**
 * Resolve per-run ``response_kind`` votes into one value.
 *
 * @returns {{kind: (string|null), divergent: boolean}} — precedence is
 * RCA_RESPONSE_KIND_PRECEDENCE: ``extracted`` beats ``not_drawn`` because a
 * single run that saw a drawn range disproves "nothing is drawn", and the
 * divergence flag lets the merge record the disagreement as a row warning
 * instead of hiding it. ``kind`` is null when no run answered.
 */
function rcaMergeResponseKinds(values) {
  const seen = [];
  for (const value of (values || [])) {
    const kind = rcaNormalizeResponseKind(value);
    if (kind) seen.push(kind);
  }
  if (!seen.length) return { kind: null, divergent: false };
  let best = seen[0];
  for (const kind of seen) {
    if (RCA_RESPONSE_KIND_PRECEDENCE[kind] > RCA_RESPONSE_KIND_PRECEDENCE[best]) best = kind;
  }
  // Python: max() over equal ranks keeps the FIRST maximum; the loop above
  // reproduces that (strictly-greater comparison only).
  const distinct = new Set(seen);
  return { kind: best, divergent: distinct.size > 1 };
}

/** Union of the per-run ``reason_codes`` lists (first-seen order). */
function rcaMergeReasonCodes(values) {
  const out = [];
  for (const group of (values || [])) {
    for (const slug of rcaNormalizeReasonCodes(group)) {
      if (out.indexOf(slug) === -1) out.push(slug);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Reporting (mirror of reason_codes.py:reason_code_rollup, used by the audit
// report; the browser keeps it for the evidence-chain panel)
// ---------------------------------------------------------------------------

/**
 * Summarise the contract fields of ``rows`` for an audit listing.
 *
 * @returns {{contracted_rows: number, by_code: Object, by_kind: Object,
 *            entries: Array<Object>}} — ``entries`` lists ONLY rows that carry
 *            a response_kind or at least one valid reason_codes value: the
 *            evidence chain shows the decisions, not a re-transcription of the
 *            table. At most ``limit`` entries are listed; the counts cover
 *            every row.
 */
function rcaReasonCodeRollup(rows, options) {
  const opts = options || {};
  const columnKeys = opts.columnKeys || RCA_DEFAULT_COLUMN_KEYS;
  // Python: `max(0, int(limit))` — truncates toward zero. A non-numeric limit
  // is a programming error there (it raises); here it degrades to "no
  // entries", which is the same answer int() would give the counts.
  let limit = opts.limit === undefined ? 200 : Number(opts.limit);
  limit = Number.isFinite(limit) ? Math.max(0, Math.trunc(limit)) : 0;
  const byCode = new Map();
  const byKind = new Map();
  const entries = [];
  let contracted = 0;
  for (const row of (Array.isArray(rows) ? rows : [])) {
    if (!rcaIsMapping(row)) continue;
    const kind = rcaNormalizeResponseKind(row.response_kind);
    const codes = rcaNormalizeReasonCodes(
      rcaContractTruthy(row.reason_codes) ? row.reason_codes : row.reason_code);
    if (!kind && !codes.length) continue;
    contracted += 1;
    if (kind) byKind.set(kind, (byKind.get(kind) || 0) + 1);
    for (const slug of codes) byCode.set(slug, (byCode.get(slug) || 0) + 1);
    if (entries.length < limit) {
      const entry = { row: rcaRowLabel(row, columnKeys) };
      if (kind) entry.response_kind = kind;
      if (codes.length) {
        entry.reason_codes = codes;
        entry.code_summaries = codes.map((c) => rcaReasonCodeSummary(c));
      }
      const geometry = row.geometry;
      if (rcaIsMapping(geometry) && rcaContractTruthy(geometry.points)) {
        // (B) says whether the position reads were usable evidence and whether
        // anything converted them — "4 of 4 points, calibrated" is the
        // auditable statement; the numbers themselves stay on the row.
        entry.geometry = {
          points: rcaIsMapping(geometry.points) ? Object.keys(geometry.points).length : 0,
          calibrated: rcaContractTruthy(geometry.calibrated),
        };
      }
      entries.push(entry);
    }
  }
  const sortedTo = (map) => {
    const out = {};
    for (const key of [...map.keys()].sort()) out[key] = map.get(key);
    return out;
  };
  return {
    contracted_rows: contracted,
    by_code: sortedTo(byCode),
    by_kind: sortedTo(byKind),
    entries: entries,
  };
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

const RCAReasonCodes = {
  REASON_CODES: RCA_REASON_CODES,
  REASON_CODES_BY_SLUG: RCA_REASON_CODES_BY_SLUG,
  REASON_CODE_SLUGS: RCA_REASON_CODE_SLUGS,
  RESPONSE_KINDS: RCA_RESPONSE_KINDS,
  RESPONSE_EXTRACTED: RCA_RESPONSE_EXTRACTED,
  RESPONSE_NOT_DRAWN: RCA_RESPONSE_NOT_DRAWN,
  RESPONSE_UNCERTAIN: RCA_RESPONSE_UNCERTAIN,
  RESPONSE_KIND_PRECEDENCE: RCA_RESPONSE_KIND_PRECEDENCE,
  SILENT_MISSING: RCA_SILENT_MISSING,
  LOW_CONFIDENCE_CODE: RCA_LOW_CONFIDENCE_CODE,
  DEFAULT_COLUMN_KEYS: RCA_DEFAULT_COLUMN_KEYS,
  DEFAULT_STRATUM_KEYS: RCA_DEFAULT_STRATUM_KEYS,
  MAX_LEDGER_CELLS: RCA_MAX_LEDGER_CELLS,
  is_valid_code: rcaIsValidReasonCode,
  normalize_code: rcaNormalizeReasonCode,
  normalize_codes: rcaNormalizeReasonCodes,
  code_summary: rcaReasonCodeSummary,
  render_codes: rcaRenderReasonCodes,
  normalize_response_kind: rcaNormalizeResponseKind,
  merge_response_kinds: rcaMergeResponseKinds,
  merge_reason_codes: rcaMergeReasonCodes,
  has_value: rcaRowHasValue,
  coverage_state: rcaCoverageState,
  is_answered: rcaIsAnswered,
  row_column: rcaRowColumn,
  row_stratum: rcaRowStratum,
  row_label: rcaRowLabel,
  coverage_ledger: rcaCoverageLedger,
  reason_code_rollup: rcaReasonCodeRollup,
  // shared with js/aggregate.js / js/quality.js
  py_round: rcaPyRound,
  py_truthy: rcaContractTruthy,
  is_mapping: rcaIsMapping,
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = RCAReasonCodes;
}
if (typeof window !== 'undefined') {
  window.RCAReasonCodes = RCAReasonCodes;
}
if (typeof globalThis !== 'undefined' && typeof window === 'undefined') {
  globalThis.RCAReasonCodes = RCAReasonCodes;
}
