/**
 * tests_diff_frontend_parity.js — browser mirrors vs the recorded Python oracle.
 *
 * REVIEW-2026-09-20 (frontend parity task, item 14's "generate a differential
 * fixture" suggestion, generalized over the whole extraction domain).
 *
 * The expected answers in tests/fixtures/frontend_parity_2026_09_20.json come
 * from rca_core itself — regenerate them with
 *   python tests/gen_frontend_parity_fixtures.py
 * whenever the Python side intentionally changes, and keep
 *   python -m pytest tests/test_frontend_parity_fixtures_2026_09_20.py
 * honest (it fails when the committed fixture no longer matches Python).
 *
 * Run:  node tests_diff_frontend_parity.js            (summary)
 *       node tests_diff_frontend_parity.js --verbose  (full diffs)
 *       node tests_diff_frontend_parity.js rc_dict_shaped_sections   (one case)
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = __dirname;
const FIXTURE = path.join(ROOT, 'tests', 'fixtures', 'frontend_parity_2026_09_20.json');
const VERBOSE = process.argv.includes('--verbose');
const ONLY = process.argv.slice(2).filter((a) => !a.startsWith('--'));

const SCRIPTS = [
  'js/config.js',
  'js/json-utils.js',
  'js/ics_table.js',
  // BORROW-2026-09-20: mirror of rca_core/reason_codes.py; quality.js and
  // aggregate.js consult it at CALL time (typeof-guarded), so loading it here
  // is what activates the contract groups below.
  'js/reason-codes.js',
  'js/quality.js',
  'js/aggregate.js',
  'js/minimax.js',
  // AUDIT-2026-09-30: js/table.js was MISSING here, so this harness was not
  // the environment the app runs in. It surfaced as a hard failure the moment
  // minimax.js's duplicate `rcaPyFloatStr` stub was deleted: the to_newick
  // group threw "rcaPyFloatStr is not defined", because the real 50-line
  // definition lives in table.js and only the stub was being loaded.
  //
  // Two things were wrong with that, and only the second is visible in a test
  // count. (1) rcaPyFloatStr, whose name promises Python's repr(), resolved
  // to a 3-line stub that spelled NaN as "NaN" -- while index.html loads
  // table.js AFTER minimax.js and therefore got the correct one. The harness
  // was measuring a function the app never runs. (2) js/aggregate.js consults
  // table.js's exporter predicates through a `typeof` guard, so with the file
  // absent that entire branch was inert here and live in the app.
  //
  // Order follows index.html so the context matches the product.
  'js/table.js',
];

function buildContext() {
  const store = new Map();
  const ctx = {
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    TextEncoder, TextDecoder, URL,
    fetch: async () => { throw new Error('network disabled in differential test'); },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    document: {
      documentElement: {
        style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        setAttribute() {}, removeAttribute() {}, getAttribute: () => null,
      },
      addEventListener() {}, removeEventListener() {}, dispatchEvent: () => true,
      querySelector: () => null, querySelectorAll: () => [],
      getElementById: () => null, createElement: () => ({ style: {}, appendChild() {} }),
      body: { appendChild() {}, removeChild() {}, classList: { add() {}, remove() {} } },
    },
    alert() {}, prompt: () => '', confirm: () => false,
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    navigator: { language: 'en' },
  };
  ctx.window = ctx;
  ctx.self = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  for (const rel of SCRIPTS) {
    const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    vm.runInContext(src, ctx, { filename: rel });
  }
  // `function` declarations land on the context automatically; export helpers
  // declared with const/let that the differential cases need.
  vm.runInContext(`
    globalThis.__exp = {
      safeJsonLoads: typeof safeJsonLoads !== 'undefined' ? safeJsonLoads : null,
      normalizeResult: typeof rcaNormalizeResult !== 'undefined' ? rcaNormalizeResult : null,
      normalizeColumnar: typeof rcaNormalizeColumnarResult !== 'undefined' ? rcaNormalizeColumnarResult : null,
      normalizeAbundance: typeof rcaNormalizeAbundanceResult !== 'undefined' ? rcaNormalizeAbundanceResult : null,
      normalizeZonation: typeof rcaNormalizeZonationChartResult !== 'undefined' ? rcaNormalizeZonationChartResult : null,
      normalizePhylo: typeof rcaNormalizePhylogeneticTreeResult !== 'undefined' ? rcaNormalizePhylogeneticTreeResult : null,
      normalizeClassification: typeof rcaNormalizeChartClassification !== 'undefined' ? rcaNormalizeChartClassification : null,
      toNewick: typeof rcaToNewick !== 'undefined' ? rcaToNewick : null,
      resolveAgeBound: typeof _resolveAgeBound !== 'undefined' ? _resolveAgeBound : null,
      // BORROW-2026-09-20 contract groups: RCAReasonCodes / RCA_KEYMAP_BY_MODE
      // / scoreRangeChart are const-declarations (they never attach to the
      // context object), so they must be handed out from inside a script.
      RC: typeof RCAReasonCodes !== 'undefined' ? RCAReasonCodes : null,
      mergeResults: typeof rcaMergeResults !== 'undefined' ? rcaMergeResults : null,
      keymaps: typeof RCA_KEYMAP_BY_MODE !== 'undefined' ? RCA_KEYMAP_BY_MODE : null,
      scoreRangeChart: typeof scoreRangeChart !== 'undefined' ? scoreRangeChart : null,
      // AUDIT-2026-10-01: the table editor's edit payload, for the editable
      // group. These are function DECLARATIONS in js/table.js, so they land on
      // the context on their own -- but buildContext() RETURNS ctx.__exp, not
      // ctx, so anything a runner needs has to be named here. (No backticks in
      // this comment: it lives inside a template literal.)
      rcaCaptureEdits: typeof rcaCaptureEdits !== 'undefined' ? rcaCaptureEdits : null,
      rcaApplyEdits: typeof rcaApplyEdits !== 'undefined' ? rcaApplyEdits : null,
      // AUDIT-2026-10-01: the axis-calibration mirrors in js/minimax.js.
      rcaAxisDomainsFrom: typeof rcaAxisDomainsFrom !== 'undefined' ? rcaAxisDomainsFrom : null,
      rcaPosToAxisValue: typeof rcaPosToAxisValue !== 'undefined' ? rcaPosToAxisValue : null,
      RCA_EDIT_LIST_KEYS: typeof RCA_EDIT_LIST_KEYS !== 'undefined' ? RCA_EDIT_LIST_KEYS : null,
      // AUDIT-2026-10-02: the aggregate LEAF functions, for the aggregate
      // group. mergeResults was reachable before (the merge group) but nothing
      // it calls was, and the decisions live in the leaves: what counts as the
      // same species, what counts as missing, how a tie breaks. NO_MERGE is a
      // Symbol, so it has to be handed out too for the sentinel mapping.
      NO_MERGE: typeof NO_MERGE !== 'undefined' ? NO_MERGE : null,
      rcaAggNorm: typeof rcaAggNorm !== 'undefined' ? rcaAggNorm : null,
      rcaNormIcbnAuthor: typeof rcaNormIcbnAuthor !== 'undefined' ? rcaNormIcbnAuthor : null,
      rcaExtractQualifiers: typeof rcaExtractQualifiers !== 'undefined' ? rcaExtractQualifiers : null,
      rcaStrForMerge: typeof rcaStrForMerge !== 'undefined' ? rcaStrForMerge : null,
      rcaAggMode: typeof rcaAggMode !== 'undefined' ? rcaAggMode : null,
      mergeScalarField: typeof mergeScalarField !== 'undefined' ? mergeScalarField : null,
      mergeTypedInteger: typeof mergeTypedInteger !== 'undefined' ? mergeTypedInteger : null,
      mergeConfidenceField: typeof mergeConfidenceField !== 'undefined' ? mergeConfidenceField : null,
      // Second wave: the dispatchers and the mutating mergers. The three
      // mutators are called on a COPY and the copy returned, so the JSON
      // comparison sees the mutation on both sides rather than a bare true.
      mergeMappingField: typeof mergeMappingField !== 'undefined' ? mergeMappingField : null,
      mergeStructuredField: typeof mergeStructuredField !== 'undefined' ? mergeStructuredField : null,
      mergeFieldAcrossRuns: typeof mergeFieldAcrossRuns !== 'undefined' ? mergeFieldAcrossRuns : null,
      rcaMergeRowWarnings: typeof rcaMergeRowWarnings !== 'undefined' ? rcaMergeRowWarnings : null,
      rcaAddRowWarning: typeof rcaAddRowWarning !== 'undefined' ? rcaAddRowWarning : null,
      rcaMergeContractField: typeof rcaMergeContractField !== 'undefined' ? rcaMergeContractField : null,
      rcaIsChimericRow: typeof rcaIsChimericRow !== 'undefined' ? rcaIsChimericRow : null,
      rcaRecombinationBallots: typeof rcaRecombinationBallots !== 'undefined' ? rcaRecombinationBallots : null,
      // Third wave: the float() mirror the schema sort keys are built on.
      rcaPyFloat: typeof rcaPyFloat !== 'undefined' ? rcaPyFloat : null,
      // Fourth wave: schema auto-detection, projected to the primary key on
      // both sides (a MergeSchema and a keymap object are not field-for-field
      // comparable, and that field is what decides the merged document's
      // shape). No backticks in this comment: it lives inside a template
      // literal.
      rcaAutoDetectKeymap: typeof rcaAutoDetectKeymap !== 'undefined' ? rcaAutoDetectKeymap : null,
    };
  `, ctx);
  return ctx.__exp;
}

const RUNNERS = {
  range_chart: (f, c) => f.normalizeResult(c.payload),
  columnar_section: (f, c) => f.normalizeColumnar(c.payload),
  abundance_diagram: (f, c) => f.normalizeAbundance(c.payload),
  zonation_chart: (f, c) => f.normalizeZonation(c.payload),
  phylogenetic_tree: (f, c) => f.normalizePhylo(c.payload),
  chart_classification: (f, c) => f.normalizeClassification(c.payload),
  to_newick: (f, c) => f.toNewick(f.normalizePhylo(c.payload)),
  safe_json_loads: (f, c) => f.safeJsonLoads(c.payload),
  age_bound: (f, c) => {
    const r = f.resolveAgeBound(c.payload, c.extra);
    return r === null || r === undefined ? null : { name: r.name === undefined ? null : r.name, ma: r.ma === undefined ? null : r.ma };
  },
  // BORROW-2026-09-20 (js-data-layer mirror). The reason_codes payloads carry
  // {"op", "args", "kwargs"}; the generator dispatches the same-named public
  // function of rca_core/reason_codes.py, so the replay dispatches the same
  // name on RCAReasonCodes. merge_response_kinds is wrapped into
  // {"kind","divergent"} on BOTH sides (Python returns a tuple); coverage_
  // ledger / reason_code_rollup take keyword-only options, whose JSON keys
  // (snake_case) map onto the JS option object's camelCase fields.
  reason_codes: (f, c) => {
    const RC = f.RC;
    const op = c.payload.op;
    const args = c.payload.args || [];
    const kw = c.payload.kwargs || {};
    if (!(op in RC) || typeof RC[op] !== 'function') {
      throw new Error('RCAReasonCodes has no op ' + op);
    }
    if (op === 'merge_response_kinds') {
      const r = RC.merge_response_kinds(args[0]);
      return { kind: r.kind === undefined ? null : r.kind, divergent: r.divergent };
    }
    if (op === 'coverage_ledger') {
      return RC.coverage_ledger(args[0], {
        columnKeys: kw.column_keys === undefined ? undefined : kw.column_keys,
        stratumKeys: kw.stratum_keys === undefined ? undefined : kw.stratum_keys,
        crossProduct: kw.cross_product,
      });
    }
    if (op === 'reason_code_rollup') {
      return RC.reason_code_rollup(args[0], {
        columnKeys: kw.column_keys === undefined ? undefined : kw.column_keys,
        limit: kw.limit,
      });
    }
    return RC[op].apply(null, args);
  },
  // Explicit mode -> explicit keymap: the generator calls merge_results with
  // an explicit schema=, so the replay must not auto-detect either.
  merge: (f, c) => f.mergeResults(
    c.payload.results,
    c.payload.total_runs === null ? undefined : c.payload.total_runs,
    f.keymaps[c.payload.mode || 'range_chart']),
  quality_coverage: (f, c) => f.scoreRangeChart(c.payload),
  // AUDIT-2026-10-02: the aggregate LEAF functions. The merge group above
  // drives rcaMergeResults end to end, but every function it CALLS was
  // untested across engines -- and a divergence in _norm_iczn_author or
  // _extract_qualifiers silently changes which rows the desktop and the
  // browser consider the same species, which is not visible until two runs
  // get merged.
  //
  // The alias table is the mirror of _AGG_PY_OPS in
  // tests/gen_frontend_parity_fixtures.py; the two must stay in step or the
  // group stops comparing anything. Op names are aliases because the two
  // implementations spell these differently (rcaNormIcbnAuthor -- "Icbn" is
  // a typo that is in the product).
  //
  // Two normalisations, both applied on the PYTHON side identically:
  //   * qualifiers: Python returns a frozenset, JS an array -> both sorted,
  //     so the comparison is set equality rather than insertion order.
  //   * the NO_MERGE sentinel -> null on both sides. A bare object() and a
  //     Symbol are not expressible in JSON, and normalising on one side only
  //     is what produced 751 phantom rows once already.
  aggregate: (f, c) => {
    const p = c.payload;
    const a = p.args || [];
    const sentinel = (v) => (v === f.NO_MERGE ? null : v);
    switch (p.op) {
      case 'norm': return f.rcaAggNorm(a[0]);
      case 'iczn': return f.rcaNormIcbnAuthor(a[0]);
      case 'qualifiers': return f.rcaExtractQualifiers(a[0]).slice().sort();
      case 'str_merge': return f.rcaStrForMerge(a[0]);
      case 'mode': return f.rcaAggMode(a[0]);
      case 'merge_scalar': return sentinel(f.mergeScalarField(a[0]));
      // mergeTypedInteger takes ONE argument; the Python
      // _stable_typed_mode takes (values, expected_type). args[1] carries the
      // type so both runners read the same payload -- it is simply unused
      // here, and that asymmetry is the only one in the table.
      case 'typed_mode': return sentinel(f.mergeTypedInteger(a[0]));
      case 'confidence': return sentinel(f.mergeConfidenceField(a[0]));
      // Second wave. See the generator's block header for why the mutators
      // are called on a copy and why contract_field returns {handled, target}.
      case 'merge_mapping': return sentinel(f.mergeMappingField(a[0]));
      case 'merge_structured': return f.mergeStructuredField(a[0]);
      case 'merge_across': return sentinel(f.mergeFieldAcrossRuns(a[0]));
      case 'row_warnings': {
        const t = JSON.parse(JSON.stringify(a[0]));
        f.rcaMergeRowWarnings(t, JSON.parse(JSON.stringify(a[1])));
        return t;
      }
      case 'add_row_warning': {
        const t = JSON.parse(JSON.stringify(a[0]));
        f.rcaAddRowWarning(t, a[1]);
        return t;
      }
      case 'contract_field': {
        const t = JSON.parse(JSON.stringify(a[2]));
        const handled = f.rcaMergeContractField(a[0], JSON.parse(JSON.stringify(a[1])), t);
        return { handled: handled === true, target: t };
      }
      case 'chimeric': return f.rcaIsChimericRow(a[0], a[1], a[2]) === true;
      case 'ballots': return f.rcaRecombinationBallots(a[0], a[1]);
      // Python float() on the same token vocabulary the generator uses; see
      // the third-wave header there for why NaN/inf/raise are mapped rather
      // than recorded raw.
      case 'py_float': {
        const r = f.rcaPyFloat(a[0]);
        if (r === null || r === undefined) return 'raise';
        if (typeof r === 'number' && Number.isNaN(r)) return 'nan';
        if (r === Infinity) return 'inf';
        if (r === -Infinity) return '-inf';
        return r;
      }
      case 'auto_detect': {
        const km = f.rcaAutoDetectKeymap(a[0]);
        return km ? km.primary : null;
      }
      default: throw new Error('no aggregate op ' + p.op);
    }
  },
  // AUDIT-2026-10-01: the axis-calibration chain. The normalisation pass only
  // HOISTS `axis_calibration` verbatim, so the fit is not in that path -- these
  // four functions are, and they are what turn the model's 0-999 position into
  // the number a figure is drawn from. "axis_calibration" appeared ZERO times
  // in this fixture before, while the prompt asks for the block in five modes
  // and js/minimax.js's own comment mentions it having "diverged between
  // transports". Each side derives the DOMAIN ITSELF, because that is where a
  // divergence would actually live.
  axis: (f, c) => {
    const p = c.payload;
    const a = p.args || [];
    if (p.op === 'axis_domains_from') return f.rcaAxisDomainsFrom(a[0]);
    if (p.op === 'pos_to_axis_value') {
      const axes = f.rcaAxisDomainsFrom(a[1]);
      return { axes: axes, v: f.rcaPosToAxisValue(a[0], axes[a[3]] || null) };
    }
    throw new Error('no axis op ' + p.op);
  },
  // AUDIT-2026-10-01: rca_core/editable.py vs js/table.js — the payload the
  // table editor produces and replays. The REPLAY is compared too, not just the
  // diff: a payload that looks right and replays onto the wrong table is worse
  // than one that is visibly wrong, and the editor is how a researcher corrects
  // a model mistake, so this is the path where a silent divergence would change
  // the numbers they publish.
  //
  // An absent key is normalised to null, because JS answers undefined and
  // Python's dict.get answers None and both mean "not there"; comparing them
  // raw reported one spurious difference on a missing-key case.
  editable: (f, c) => {
    const p = c.payload;
    if (p.op === 'list_keys') return f.RCA_EDIT_LIST_KEYS;
    if (p.op !== 'capture_all') throw new Error('no editable op ' + p.op);
    const edits = f.rcaCaptureEdits(p.args[0], p.args[1]);
    let replay = null;
    if (edits && typeof edits === 'object' && Object.keys(edits).length) {
      try {
        const after = f.rcaApplyEdits(JSON.parse(JSON.stringify(p.args[0])),
                                      JSON.parse(JSON.stringify(edits)));
        const out = {};
        for (const k of Object.keys(edits)) {
          out[k] = (after && Object.prototype.hasOwnProperty.call(after, k))
            ? after[k] : null;
        }
        replay = out;
      } catch (err) {
        replay = '__raised__ ' + String(err && err.message || err);
      }
    }
    return { edits: edits, replay: replay };
  },
};

// Numbers: Python 1.0 vs JS 1 (and float noise) are the same value.
function canon(value) {
  if (Array.isArray(value)) return value.map(canon);
  if (value && typeof value === 'object') {
    const out = {};
    for (const k of Object.keys(value)) out[k] = canon(value[k]);
    return out;
  }
  if (typeof value === 'number') return Number.isInteger(value) ? value : Number(value.toFixed(9));
  return value;
}

// AUDIT-2026-10-01: the diff caps at 12 LINES, but a pathological VALUE used to
// arrive as one unbounded line -- sj_nest_array_1200 (a 1200-level nested
// array, added to pin the two parsers' nesting limits) printed every bracket and
// buried the run's other output. Truncate the rendered value, and say how long
// it was, so the line stays readable without hiding that something big is
// there. 240 is well above any value a real extraction emits.
const _DIFF_VALUE_MAX = 240;
function _show(value) {
  let text;
  try { text = JSON.stringify(value); } catch (e) { text = String(value); }
  if (text === undefined) text = String(value);
  return text.length > _DIFF_VALUE_MAX
    ? `${text.slice(0, _DIFF_VALUE_MAX)}...<${text.length} chars>`
    : text;
}

// Same cap for a MESSAGE, which is where sj_nest_array_1200's real payload used
// to land: safe_json_loads quotes the whole string it failed on, so a 1200-level
// array became a 2400-character exception message in the log.
function _showText(text) {
  const s = text === undefined || text === null ? String(text) : String(text);
  return s.length > _DIFF_VALUE_MAX
    ? `${s.slice(0, _DIFF_VALUE_MAX)}...<${s.length} chars>`
    : s;
}

function diff(a, b, trail, out) {
  if (out.length > 12) return;
  if (a === b) return;
  if (typeof a === 'number' && typeof b === 'number'
      && Math.abs(a - b) < 1e-9) return;
  const ta = a === null ? 'null' : Array.isArray(a) ? 'array' : typeof a;
  const tb = b === null ? 'null' : Array.isArray(b) ? 'array' : typeof b;
  if (ta !== tb) { out.push(`${trail}: py=${ta}(${_show(a)}) js=${tb}(${_show(b)})`); return; }
  if (ta === 'array') {
    if (a.length !== b.length) {
      out.push(`${trail}: length py=${a.length} js=${b.length}`);
    }
    for (let i = 0; i < Math.min(a.length, b.length); i++) diff(a[i], b[i], `${trail}[${i}]`, out);
    return;
  }
  if (ta === 'object') {
    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    for (const k of [...keys].sort()) {
      if (!(k in a)) { out.push(`${trail}.${k}: MISSING in python (js=${_show(b[k])})`); continue; }
      if (!(k in b)) { out.push(`${trail}.${k}: MISSING in js (py=${_show(a[k])})`); continue; }
      diff(a[k], b[k], `${trail}.${k}`, out);
    }
    return;
  }
  out.push(`${trail}: py=${_show(a)} js=${_show(b)}`);
}

//  * ag_34 / ag_35 — non-ASCII decimal digits in an age label. Python's `\d`
//    and `float()` accept every Unicode decimal digit; ECMAScript's `\d` is
//    `[0-9]` and nothing else, so rca_core/standards/ics.py resolves
//    "26٠ Ma" (Arabic-Indic zero) and "２６０ Ma" (full-width) to an
//    absolute age while js/quality.js#_resolveAgeBound calls them
//    unresolvable. Same label, different DwC/PBDB age, decided by which
//    engine read the figure. Found by difffuzz_ics.py, which fuzzes the
//    shapes the 33 well-formed age fixtures never covered. Aligning them is
//    a behaviour change on BOTH engines (fold Unicode digits in JS, or
//    tighten the Python regex to ASCII), so it needs an owner decision;
//    until then it is tracked here rather than left invisible.
//  * rc_root_confidence_nan / rc_row_confidence_nan / cc_root_confidence_nan —
//    RESOLVED 2026-09-30, no longer parked. These were the literal "NaN" as a
//    confidence: both engines accept it as a float, then clamped it with the
//    same intent and opposite results, because Python's min/max do not
//    propagate NaN (min(1.0, nan) is 1.0) while JS's Math.min does. One model
//    output became a PERFECT 1.0 in the desktop app and a 0 in the browser.
//    js/minimax.js already had the right answer in two shared helpers
//    (rcaConfidenceClamped -> 0.0 for a root value, rcaOptionalConfidence ->
//    null for a row value); rca_core had 13 hand-copied unguarded clamps across
//    all 10 normalizers instead of one gate. Python now routes every site
//    through rca_core.extractor._confidence_clamped, the fixture records 0.0 /
//    null, and these three cases are ordinary matches. Kept here as a record
//    because the parked-divergence mechanism is what made the fix findable.
//    NOTE the guard is deliberately `isnan`, NOT `isfinite`: +Infinity clamps to
//    1.0 and -Infinity to 0.0 on BOTH engines, so a blanket isfinite->0.0
//    would have introduced a fresh divergence on +Infinity.
// Three cases stay diverged ON PURPOSE. All are recorded here instead of being
// papered over, so the suite still exits 0 and a future drift shows up as a NEW
// failure rather than as noise that everyone learned to skim.
//
//  * rc_dict_shaped_sections — ROW ORDER only. The payload wraps its sections
//    in a dict whose keys are "Pingdingshan", "unclear", "0". ECMAScript
//    enumerates an ordinary object's ARRAY-INDEX-like keys ("0") BEFORE the
//    string keys and in ascending numeric order, whatever the document order
//    was — and JSON.parse already lost the order, so no normalizer code can
//    recover it (only a custom order-preserving parser could). Python dicts
//    keep insertion order, so the two engines emit the same three rows in a
//    different sequence. Values, `wrapper_key` stamps and warnings all match;
//    only the row sequence differs, and only for a numeric-looking wrapper key.
//  * rc_section_formations_string — RESOLVED, removed 2026-09-30. This entry
//    was the second stale one in this table: it described rca_core/extractor.py
//    as still calling a BARE `str(v)` in six local `s()` closures "at lines
//    902/986/1052/1108/1572/3143", leaking a Python repr like "{'lat': 1}"
//    into a scientific field. Measured rather than believed: normalize_result's
//    `s()` now delegates to `_stringify_scalar` and contains zero bare
//    `str(v)` calls, and the case agrees --
//        node tests_diff_frontend_parity.js rc_section_formations_string
//        -> 1 matched, 0 documented, 0 diverged
//    The fix landed at some point and the table entry did not. An agreeing
//    case in EXPECTED_DIVERGENCES is harmless to the exit code, but it is
//    exactly the kind of stale claim this file is supposed to be the record
//    against, and the next reader would go looking for a bug that is gone.
//
//    The other two survivors were re-measured the same way and ARE still
//    diverged, so they stay: rc_dict_shaped_sections (row ORDER only) and
//    ag_34 / ag_35 (Unicode-vs-ASCII digits).
const EXPECTED_DIVERGENCES = {
  rc_dict_shaped_sections: 'JS enumerates integer-like object keys first (ECMAScript ordinary-object order); row ORDER only',
  ag_34: 'Python \\d/float() accept Unicode decimal digits, JS \\d is [0-9] only; Arabic-Indic digit age label resolves in rca_core and not in the browser',
  ag_35: 'Same Unicode-vs-ASCII digit split, full-width digits: resolves in rca_core/standards/ics.py, unresolvable in js/quality.js',
  // AUDIT-2026-09-30: rc_root_confidence_nan, rc_row_confidence_nan and
  // cc_root_confidence_nan were REMOVED from this table. They are ordinary
  // matches now: rca_core.extractor._confidence_clamped is the single gate for
  // every root confidence, and _normalize_confidence returns None for a NaN
  // row value, which is what js/minimax.js rcaConfidenceClamped (0.0) and
  // rcaOptionalConfidence (null) already did. If a NaN divergence ever comes
  // back it will now FAIL the build instead of being absorbed here, which is
  // the whole point of the table being checked rather than trusted.
  //
  // AUDIT-2026-10-01: rc_float_integral and rc_float_exponent_big were found by
  // an adversarial pass over the range-chart group, and they are the same class
  // as the three above: a property of the RUNTIME, not a defect in the mirror.
  // Python's json.loads keeps the int/float distinction ('3.0' -> 3.0, a float),
  // so rca_core/extractor.py::_stringify_scalar writes "3.0". JSON.parse has one
  // number type: JSON.parse('3.0') is the number 3 and Number.isInteger(3) is
  // true, so the browser cannot know the model wrote a float, and String(3) is
  // "3". rc_float_exponent_big is the same wall seen from the other side, and it
  // is what corrected the first classification: the discriminator is not "does
  // this value need exponent form" but "is this value INTEGRAL". The browser
  // holds 10000000000000000 whether the model wrote 1e16 or the digits, while
  // Python answers '1e+16' and '10000000000000000' respectively.
  // The NON-integral half of the family is deliberately NOT listed, because it
  // is fixable and was fixed: rcaStringifyScalar routes a non-integral float
  // through js/table.js#rcaPyFloatStr, so str(1e-7) == "1e-07" on both engines
  // (rc_float_exponent_small / _negative). rc_float_int is the control that has
  // to keep matching: an integer payload still renders "3" on both sides.
  rc_float_integral: 'JSON.parse collapses 3.0 to the integer 3, so the browser cannot know a float was written; Python json.loads keeps it (py="3.0" js="3")',
  rc_float_exponent_big: 'Same JSON.parse wall on an integral value: 1e16 and 10000000000000000 are one JS number, but Python str() answers "1e+16" and "10000000000000000" respectively',
  // AUDIT-2026-10-01: the parsers' nesting limits are recorded in the comment
  // on the expressibility guard in tests/gen_frontend_parity_fixtures.py, NOT
  // as an entry here. A case was tried and removed: the browser parses a
  // 1200-level nested array at any depth, but WHERE CPython refuses it moves
  // with the interpreter (3.10: raises at >= 1000 = sys.getrecursionlimit();
  // 3.12.14, the CI interpreter: parses 1200), so the case's recorded Python
  // answer was not the same on both of the versions this project tests. It
  // passed on 3.10 and failed on 3.12. The same measurement also covers the
  // object form, where 3.10 parses but returns a structure truncated at depth
  // 992 while reporting success, and the browser returns it whole.
  //
  // The mechanism that entry would have needed is still worth having, so it is
  // now reached from the python_error branch as well as the value-diff branch:
  // a legitimate raise-here / return-there divergence used to have nowhere to
  // be recorded and would have failed the build forever.
  //
  // AUDIT-2026-10-02: four leaf divergences in _str_for_merge / rcaStrForMerge,
  // found by the new `aggregate` group. ALL FOUR ARE LATENT -- none has a
  // reachable effect in the current call graph, and each is recorded with its
  // reason rather than waved through, so the next reader can tell a decision
  // from an oversight.
  //
  //   * None / [1, 2] / {"a": 1}: BOTH callers filter those types before the
  //     stringifier runs -- rcaAggMode skips `typeof v === 'object'` and
  //     _mode skips isinstance(v, (dict, list, set, tuple)); mergeScalarField
  //     accepts only string|number and _merge_scalar_field only
  //     (str, int, float). So no language repr can reach the vote pool today.
  //     They stay pinned because these are the dangerous shapes: String([1]) is
  //     "1", indistinguishable from the code for 1, and the filters are the
  //     only thing standing between that and a merged field.
  //   * 1e-7: the float-repr residual js/aggregate.js already documents
  //     ("str(1e-7) == 1e-07" vs "1e-7"). Not observable, because the pool KEY
  //     differs but the REPRESENTATIVE is the original value, so both engines
  //     still return 1e-7; a divergence would need two distinct values that
  //     collide on one engine and not the other, and non-integral floats have
  //     unique reprs on both sides. The JS comment calls this "not fixable in
  //     JS", which is now STALE -- rcaPyFloatStr exists and does render
  //     Python's exponent form -- but routing through it was not done, because
  //     there is no observable defect and the pool is not a user-visible
  //     string.
  agstr_229: 'LATENT: _str_for_merge(None) is "None" vs rcaStrForMerge(null) "null"; both callers filter null out before the stringifier runs, so no repr can reach the vote pool',
  agstr_231: 'LATENT, already documented in js/aggregate.js: Python renders 1e-7 exponent-style ("1e-07"), String() gives "1e-7". The pool key differs but the representative is the original value, so both engines return 1e-7 and nothing user-visible moves',
  agstr_237: 'LATENT: String([1, 2]) is "1,2" and is indistinguishable from a real value, while Python str() is "[1, 2]". Unreachable today because _mode/rcaAggMode skip containers first. Pinned as a trap, not a live defect',
  agstr_238: 'LATENT: same container filter, opposite direction -- a Python dict repr leaking into a scientific field. The mirror of agstr_237',
};

function main() {
  const fixture = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  const fns = buildContext();
  let passed = 0;
  let documented = 0;
  const failures = [];
  for (const c of fixture.cases) {
    if (ONLY.length && !ONLY.includes(c.id) && !ONLY.includes(c.group)) continue;
    const runner = RUNNERS[c.group];
    if (!runner) { failures.push(`${c.id}: no JS runner for group ${c.group}`); continue; }
    const notes = [];
    let got; let gotError = null;
    const input = c.payload === null || c.payload === undefined
      ? c.payload : JSON.parse(JSON.stringify(c.payload));
    try {
      got = runner(fns, { payload: input, extra: c.extra });
    } catch (err) {
      gotError = err && err.message ? String(err.message) : String(err);
    }
    if (c.python_error) {
      if (gotError === null) {
        // AUDIT-2026-10-01: EXPECTED_DIVERGENCES used to be consulted ONLY on the
        // value-diff path below, so a legitimately documented
        // raise-here / return-there divergence had nowhere to be recorded and
        // would fail the build forever. sj_nest_array_1200 is the first real
        // one: Python's json recursion limit refuses a 1200-level nested array
        // and the browser parses it. The mechanism now covers both shapes of
        // disagreement, and a STALE entry (the case starts agreeing again)
        // still shows up as an ordinary pass, exactly as before.
        const why = EXPECTED_DIVERGENCES[c.id];
        const detail = `${c.id} [${c.group}]: python raised ${c.python_error}`
          + `(${_showText(c.python_message)}), js returned ${_show(got)}`;
        if (why) {
          documented += 1;
          console.log(`SKIP ${c.id} [${c.group}] — ${why}`);
          if (VERBOSE) console.log('     ' + detail);
        } else {
          failures.push(detail);
        }
        continue;
      }
      notes.push(`both raise (py:${c.python_error})`);
      passed += 1;
      if (VERBOSE) console.log(`PASS ${c.id} ${notes.join(' ')}`);
      continue;
    }
    if (gotError !== null) {
      failures.push(`${c.id} [${c.group}]: js threw ${gotError}`);
      continue;
    }
    const a = canon(c.python);
    const b = canon(got);
    const d = [];
    diff(a, b, '', d);
    if (d.length) {
      if (EXPECTED_DIVERGENCES[c.id]) {
        documented += 1;
        console.log(`SKIP ${c.id} [${c.group}] — ${EXPECTED_DIVERGENCES[c.id]}`);
        for (const line of d) console.log('     ' + line);
      } else {
        failures.push(`${c.id} [${c.group}]\n    ${d.join('\n    ')}`);
      }
    } else {
      passed += 1;
      if (VERBOSE) console.log(`PASS ${c.id}`);
    }
  }
  console.log(`\n--- differential parity: ${passed} matched, ${documented} documented, `
              + `${failures.length} diverged ---`);
  for (const f of failures) console.log('FAIL ' + f);
  process.exit(failures.length ? 1 : 0);
}

main();
