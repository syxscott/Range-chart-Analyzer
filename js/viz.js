/**
 * js/viz.js — FE-BORROW-2026-09-20 (域V: 结果联动画布 / result-linked canvas).
 *
 * Turns the species_ranges rows of an extraction result into a horizontal
 * Gantt (range chart) drawn on the `#viz-host` mount point that index.html has
 * carried as a hidden placeholder since the first roadmap commit. This round
 * activates it: DOM table stays the SEMANTIC TRUTH SOURCE, the canvas is a
 * read-only linked view (hover -> row_index callback, focus/fade, locate).
 *
 * Two layers of code, deliberately separated:
 *
 *   1. PURE LAYOUT  (node-testable, no DOM): rcaVizLayout / rcaVizFocusPass /
 *      rcaVizHitTest / rcaVizSummary + the small parsers they use. Loaded and
 *      asserted by tests_viz.js (readFileSync + vm, like tests_frontend.js).
 *      No ES module syntax, no top-level browser access.
 *   2. RENDER       (browser only): the `rcaViz` namespace object. Every entry
 *      point guards `typeof document` / a missing host element and returns
 *      false/null instead of throwing, so requiring this file under node is
 *      harmless.
 *
 * ---------------------------------------------------------------------------
 * COORDINATE CONTRACT (WebPlotDigitizer graphicsWidget.js discipline)
 * ---------------------------------------------------------------------------
 * WPD keeps one image, three spaces, and writes the conversion down:
 *     image px  --(zoom/pan + origin)-->  canvas px  --(css)-->  screen px
 * This module cuts the same chain one step EARLIER, so nothing in the layout
 * layer ever needs the file's intrinsic pixel size:
 *
 *     (u, v) in [0,1] x [0,1]          "normalised image px"
 *        u = x / plotted-figure width   (0 = left edge of the plotted frame)
 *        v = y / plotted-figure height  (0 = TOP edge — canvas convention, NOT
 *                                        math convention; the stratigraphic
 *                                        "0 = bottom/oldest" rule lives in the
 *                                        `t` scale below, never in v)
 *
 *     canvas device px = u * canvas.width        (canvas.width = cssW * dpr)
 *     css / screen px  = u * (canvas.width / dpr)
 *     image px         = u * img.naturalWidth    (only needed to convert a WPD
 *                                                 style pixel pick INTO (u,v);
 *                                                 the overlay never reads back)
 *
 * Because the image layer is drawn with `object-fit: fill` into a stage whose
 * size the renderer owns, (u,v) maps linearly onto the visible figure: a
 * `pos_0_999` bar lands on the same relative spot the model measured. When the
 * species count forces the stage taller than the image's natural aspect the
 * v axis is stretched by `stage.rca_viz_stretch` (logged on the stage element
 * as a data attribute) — the honest, documented cost of a scrollable overlay.
 * A future calibrated pass (rca_core/geometry.py two-point + deskew) can pin
 * the registration exactly by handing the frame rect to
 * `rcaVizLayout(result, {frame: {x0,y0,x1,y1}})`; every bar then lands inside
 * that rect instead of the full stage.
 *
 * Stratigraphic scale `t` (separate from v, always 0 = oldest/bottom):
 *   * geometry path — `t = pos_0_999 / 999`, i.e. the model's own pixel read of
 *     the plotted frame (PlotLift contract, rca_core/extractor.py:POS_SUFFIX).
 *     Preferred whenever present; `axis_calibration` only changes the TICK
 *     LABELS, never the position (anti-self-echo rule).
 *   * semantic path — bed index or stage/age in Ma, linearly normalised over
 *     the domain observed across the rows on display.
 *   * neither      — the row is NOT invented away: it lands in
 *     `axis.unplaceable` and renders as a grey hatched "unplaceable" strip.
 *   `opts.strict_axis = true` forbids mixing the two paths on one axis (rows
 *   that miss the dominant source become unplaceable instead).
 *
 * ---------------------------------------------------------------------------
 * FOCUS SEMANTICS (uPlot focus/setSeries discipline)
 * ---------------------------------------------------------------------------
 * `VIZ.focus` is the ONE place focus state lives; nothing else may cache it.
 * The rule is expressed as a pure function (rcaVizFocusPass) so the data side
 * is testable without a canvas: the focused bar keeps full opacity + an
 * outline, every other placeable bar drops to `dim` (default 0.3, uPlot's
 * value), and unplaceable placeholders are never dimmed further (they are
 * already muted, and a second multiplication would push them under the 3:1
 * WCAG non-text floor against the reference image).
 *
 * Accessibility: the canvas layers carry `aria-hidden="true"` + `role="img"`
 * and the meaning is spelled out in `.rca-viz-summary` (uPlot's
 * legend-ivi idea — the interactive surface is decorative, the text alternative
 * is the reachable one).
 *
 * Not in this round (documented on purpose): series visibility toggling
 * (uPlot `setSeries`), editing, and hit-testing against the source pixels.
 */
'use strict';

// ---------------------------------------------------------------------------
// tokens & constants
// ---------------------------------------------------------------------------

/** Geometry point fields carrying the YOUNG (top) limit, best match first. */
var RCA_VIZ_GEOM_TOP_FIELDS = [
  'range_top_pos_0_999', 'top_pos_0_999', 'age_pos_0_999', 'level_pos_0_999',
  'pos_0_999',
];
/** Geometry point fields carrying the OLD (base) limit, best match first. */
var RCA_VIZ_GEOM_BASE_FIELDS = [
  'range_base_pos_0_999', 'base_pos_0_999', 'depth_pos_0_999',
];
/** extractor.py:POS_MAX — the 0-999 frame scale. */
var RCA_VIZ_POS_MAX = 999;
/** uPlot's focus fade: non-focused series render at alpha 0.3. */
var RCA_VIZ_FOCUS_DIM = 0.3;
/** Base alpha of a greyed-out placeholder strip (never dimmed further). */
var RCA_VIZ_PLACEHOLDER_ALPHA = 0.34;
/** Below this confidence a bar's own base alpha starts to fade. */
var RCA_VIZ_CONF_FLOOR = 0.55;

var RCA_VIZ_DEFAULTS = {
  // --- layout space (all normalised, see the contract above) ---
  label_width: 0.22,   // species-label column, fraction of the stage width
  right_gutter: 0.015,
  head: 0.055,         // reserve for the strat axis ticks
  foot: 0.03,
  row_inset: 0.24,     // bar thickness = (1 - 2*inset) of the row band
  min_bar_w: 0.004,    // a point range must stay visible
  max_ticks: 7,
  direction: 'younger-right', // 'younger-left' mirrors the axis
  frame: null,         // {x0,y0,x1,y1} plotted frame, normalised image space
  axis_source: 'auto', // 'auto' | 'geometry' | 'bed' | 'age'
  strict_axis: false,  // forbid mixing geometry + semantic normalisations
  section: null,       // filter to one section name
  // --- focus state machine ---
  dim: RCA_VIZ_FOCUS_DIM,
  confidence_fade: true,
  // --- hit test padding (normalised) ---
  hit_pad_u: 0.006,
  hit_pad_v_ratio: 0.35, // fraction of the row band
};

// ---------------------------------------------------------------------------
// tiny predicates (same tolerant style as js/reason-codes.js)
// ---------------------------------------------------------------------------

function rcaVizIsDict(v) {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

function rcaVizFinite(v) {
  return typeof v === 'number' && isFinite(v);
}

function rcaVizText(v) {
  return v === null || v === undefined ? '' : String(v);
}

/**
 * Mirror of rca_core/extractor.py:normalize_pos_0_999 — a *PlotLift* position
 * is an INTEGER in [0,999]; "712" is fine, 712.5 / 1000 / -1 / True are not.
 * Deliberately unforgiving so a bad read never gets clamped into a plausible
 * position.
 */
function rcaVizNormalizePos(raw) {
  if (raw === null || raw === undefined || typeof raw === 'boolean') return null;
  var parsed = null;
  if (typeof raw === 'number') parsed = raw;
  else if (typeof raw === 'string') {
    var text = raw.trim();
    if (!text) return null;
    parsed = Number(text);
  }
  if (!rcaVizFinite(parsed)) return null;
  if (Math.floor(parsed) !== parsed) return null;
  var pos = parsed;
  if (pos < 0 || pos > RCA_VIZ_POS_MAX) return null;
  return pos;
}

/**
 * Mirror of rca_core/extractor.py:_to_float_opt — best-effort float for a
 * scientific value, non-numeric text -> null. Comma decimal separator is
 * accepted because the models emit European decimal commas.
 */
function rcaVizNum(raw) {
  if (raw === null || raw === undefined || typeof raw === 'boolean') return null;
  if (typeof raw === 'number') return isFinite(raw) ? raw : null;
  if (typeof raw !== 'string') return null;
  var text = raw.trim().replace(/,/g, '.');
  if (!text) return null;
  if (/^[+-]?\d+(\.\d+)?$/.test(text)) return Number(text);
  var m = /^([+-]?\d+(?:\.\d+)?)\s*(?:ma|m\.a\.|megaa)?$/i.exec(text);
  return m ? Number(m[1]) : null;
}

// ---------------------------------------------------------------------------
// semantic parsers: bed index / Ma / stage name
// ---------------------------------------------------------------------------

// "Bed 9", "beds 16-19" (first number wins), "#7", "Lvl 12", Chinese
// "第9层" / "9号层", and a bare "9".
var RCA_VIZ_BED_PATTERNS = [
  /(?:bed|level|lvl|horizon)\s*#?\s*(\d+(?:\.\d+)?)/i,
  /#\s*(\d+(?:\.\d+)?)/,
  /第?\s*(\d+(?:\.\d+)?)\s*[层床号]/,
];

/**
 * Bed index of a boundary string, or null. Only the FIRST number of a
 * bed-labelled string is trusted ("Bed 7 (top Talung Fm)" -> 7); a bare
 * numeric string is a bed index too, so the table's `cell-num` column and the
 * canvas agree.
 */
function rcaVizBedValue(value) {
  if (typeof value === 'number' || typeof value === 'boolean') {
    return rcaVizNum(value);
  }
  var text = rcaVizText(value).trim();
  if (!text) return null;
  for (var i = 0; i < RCA_VIZ_BED_PATTERNS.length; i++) {
    var m = RCA_VIZ_BED_PATTERNS[i].exec(text);
    if (m) return Number(m[1]);
  }
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text);
  return null;
}

/**
 * Explicit "251.902 Ma" reading, or null. Ignores bare numbers (bed indices).
 */
function rcaVizMaValue(value) {
  var text = rcaVizText(value);
  var m = /(\d+(?:\.\d+)?)\s*(?:Ma|m\.a\.|Ma\s*\()/i.exec(text);
  if (m) return Number(m[1]);
  m = /(\d+(?:\.\d+)?)\s*百万年/.exec(text);
  return m ? Number(m[1]) : null;
}

var RcaVizStageIndex = null; // memoised {keys: [...longest first], lookup: {}}

/** Build (once) the case-folded stage-name index over the bundled ICS table. */
function rcaVizStages() {
  if (RcaVizStageIndex) return RcaVizStageIndex;
  var table = typeof RCA_ICS_TABLE !== 'undefined' && RCA_ICS_TABLE
    ? RCA_ICS_TABLE : null;
  var cn = typeof RCA_ICS_CN_STAGES !== 'undefined' && RCA_ICS_CN_STAGES
    ? RCA_ICS_CN_STAGES : null;
  var lookup = {};
  var keys = [];
  function add(name, canonical) {
    var key = String(name).toLowerCase();
    if (!key || lookup[key]) return;
    lookup[key] = canonical;
    keys.push(key);
  }
  if (table) {
    for (var k in table) {
      if (Object.prototype.hasOwnProperty.call(table, k)) add(k, k);
    }
  }
  if (cn) {
    for (var c in cn) {
      if (Object.prototype.hasOwnProperty.call(cn, c)) add(c, cn[c]);
    }
  }
  // Longest name first so "Late Permian"-style superstrings cannot resolve to
  // a shorter stage that merely sits inside them.
  keys.sort(function (a, b) { return b.length - a.length; });
  RcaVizStageIndex = { keys: keys, lookup: lookup };
  return RcaVizStageIndex;
}

/** First ICS stage whose name (EN or CN alias) appears in `text`. */
function rcaVizStageOf(text) {
  var s = rcaVizText(text).toLowerCase();
  if (!s.trim()) return null;
  var idx = rcaVizStages();
  for (var i = 0; i < idx.keys.length; i++) {
    if (s.indexOf(idx.keys[i]) !== -1) return idx.lookup[idx.keys[i]];
  }
  return null;
}

/** {top_ma, base_ma} of a stage name, or null. Older = larger base_ma. */
function rcaVizStageBounds(name) {
  var table = typeof RCA_ICS_TABLE !== 'undefined' && RCA_ICS_TABLE
    ? RCA_ICS_TABLE : null;
  if (!table || !name || !rcaVizIsDict(table[name])) return null;
  var top = rcaVizNum(table[name].top_ma);
  var base = rcaVizNum(table[name].base_ma);
  if (top === null || base === null) return null;
  return { name: name, top_ma: top, base_ma: base };
}

// ---------------------------------------------------------------------------
// geometry sidecar reader
// ---------------------------------------------------------------------------

/**
 * Read a row's `geometry` sidecar (rca_core/extractor.py:geometry_from_row)
 * into a {top, base} pair of 0-999 positions.
 *
 * Accepted entry shapes: `{pos, axis, value, unit}` (what Python writes) or a
 * bare integer (defensive: a hand-edited / older payload). A field that cannot
 * normalise to an int in [0,999] is ignored — the row then degrades to the
 * semantic path instead of drawing a bar at a made-up spot.
 */
function rcaVizRowGeometry(row) {
  var out = { top: null, base: null, calibrated: false, points: 0, axes: null };
  if (!rcaVizIsDict(row)) return out;
  var geom = rcaVizIsDict(row.geometry) ? row.geometry : null;
  var points = geom && rcaVizIsDict(geom.points) ? geom.points : {};
  out.points = Object.keys(points).length;
  out.calibrated = !!(geom && geom.calibrated);
  out.axes = geom && rcaVizIsDict(geom.axes) ? geom.axes : null;
  function pick(fields) {
    var i;
    for (i = 0; i < fields.length; i++) {
      var entry = points[fields[i]];
      var pos = null;
      var value = null;
      if (rcaVizIsDict(entry)) {
        pos = rcaVizNormalizePos(entry.pos);
        value = rcaVizNum(entry.value);
      } else {
        pos = rcaVizNormalizePos(entry);
      }
      if (pos !== null) return { pos: pos, field: fields[i], value: value };
    }
    // No sidecar (or the point is not in it): accept the RAW `*pos_0_999` keys
    // on the row itself — a pre-normalisation payload, or one where the merge
    // layer dropped the block but kept the evidence.
    for (i = 0; i < fields.length; i++) {
      var raw = rcaVizNormalizePos(row[fields[i]]);
      if (raw !== null) return { pos: raw, field: fields[i], value: null };
    }
    return null;
  }
  out.top = pick(RCA_VIZ_GEOM_TOP_FIELDS);
  out.base = pick(RCA_VIZ_GEOM_BASE_FIELDS);
  if (!out.top && !out.base) return out;
  if (out.points === 0 && !out.axes) out.calibrated = false;
  return out;
}

/**
 * Vertical axis calibration (root / metadata / _extras `axis_calibration`),
 * mirrored from extractor.py:axis_domains_from — only what we need to LABEL
 * the axis; positions stay on the 0-999 frame scale.
 */
function rcaVizVerticalDomain(result) {
  var roots = [
    rcaVizIsDict(result) ? result.axis_calibration : null,
    rcaVizIsDict(result) && rcaVizIsDict(result.metadata)
      ? result.metadata.axis_calibration : null,
    rcaVizIsDict(result) && rcaVizIsDict(result._extras)
      ? result._extras.axis_calibration : null,
  ];
  for (var i = 0; i < roots.length; i++) {
    var block = roots[i];
    if (!rcaVizIsDict(block)) continue;
    var raw = block.vertical !== undefined ? block.vertical
      : (block.y !== undefined ? block.y : block.depth);
    var at0 = null;
    var at999 = null;
    var unit = '';
    if (Array.isArray(raw) && raw.length === 2) {
      at0 = rcaVizNum(raw[0]);
      at999 = rcaVizNum(raw[1]);
    } else if (rcaVizIsDict(raw)) {
      at0 = rcaVizNum(raw.at_0 !== undefined ? raw.at_0
        : (raw.bottom !== undefined ? raw.bottom : raw.base));
      at999 = rcaVizNum(raw.at_999 !== undefined ? raw.at_999
        : (raw.top !== undefined ? raw.top : raw.ceiling));
      unit = rcaVizText(raw.unit).trim();
    }
    if (at0 !== null && at999 !== null && at0 !== at999) {
      return { at_0: at0, at_999: at999, unit: unit };
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// per-row candidate resolution
// ---------------------------------------------------------------------------

/** Row label: species first, then the generic id/name the other modes use. */
function rcaVizRowLabel(row, index) {
  if (!rcaVizIsDict(row)) return '(row ' + (index + 1) + ')';
  var name = rcaVizText(row.species || row.id || row.name || row.taxon).trim();
  return name || '(unnamed #' + (index + 1) + ')';
}

/**
 * The three independent readings one boundary string can support, in the order
 * the layout prefers them. Returns
 *   { geometry: {top,base}|null, bed: {young,old}|null, age: {young,old}|null,
 *     age_stages: {young,old}|null }
 * with `age` expressed in Ma (larger = older).
 */
function rcaVizRowCandidates(row, index) {
  var out = { geometry: null, bed: null, age: null, stages: null };
  if (!rcaVizIsDict(row)) return out;

  // 1. geometry sidecar (the model's own pixel read of the plotted frame).
  var geom = rcaVizRowGeometry(row);
  if (geom.top || geom.base) {
    out.geometry = {
      top: geom.top, base: geom.base,
      calibrated: geom.calibrated, axes: geom.axes,
    };
  }

  // 2. bed index space: explicit *_idx ints first, then the printed label.
  var topIdx = rcaVizNum(row.range_top_idx);
  var baseIdx = rcaVizNum(row.range_base_idx);
  var topBed = topIdx !== null ? topIdx : rcaVizBedValue(row.range_top);
  var baseBed = baseIdx !== null ? baseIdx : rcaVizBedValue(row.range_base);
  if (topBed !== null || baseBed !== null) {
    out.bed = { young: topBed, old: baseBed };
  }

  // 3. time space: an explicit "N Ma" reading wins over a stage name; a stage
  //    name contributes its own ICS bounds (young end = top_ma).
  var topMa = rcaVizMaValue(row.range_top);
  var baseMa = rcaVizMaValue(row.range_base);
  var stageTop = topMa === null ? rcaVizStageOf(row.range_top) : null;
  var stageBase = baseMa === null ? rcaVizStageOf(row.range_base) : null;
  if (topMa === null && !stageTop) {
    // Fall back to the row's zone/age text, but only when BOTH ends are silent,
    // so a half-readable pair is never invented from the biozone.
    if (baseMa === null && !stageBase) {
      var zoneStage = rcaVizStageOf(row.biozone) || rcaVizStageOf(row.age_range);
      if (zoneStage) { stageTop = zoneStage; stageBase = zoneStage; }
    }
  }
  var young = topMa;
  var old = baseMa;
  var boundsTop = stageTop ? rcaVizStageBounds(stageTop) : null;
  var boundsBase = stageBase ? rcaVizStageBounds(stageBase) : null;
  if (young === null && boundsTop) young = boundsTop.top_ma;
  if (old === null && boundsBase) old = boundsBase.base_ma;
  // A single named stage spans the row when only one end resolved.
  if (young === null && old === null && boundsTop) {
    young = boundsTop.top_ma;
    old = boundsTop.base_ma;
  }
  if (young !== null || old !== null) {
    out.age = { young: young, old: old };
    out.stages = {
      young: boundsTop ? boundsTop.name : null,
      old: boundsBase ? boundsBase.name : null,
    };
  }
  return out;
}

// ---------------------------------------------------------------------------
// tick maths
// ---------------------------------------------------------------------------

/** Nice ascending tick values covering [lo,hi] (at most about `maxN`). */
function rcaVizNiceTicks(lo, hi, maxN) {
  if (!rcaVizFinite(lo) || !rcaVizFinite(hi) || hi <= lo) return [];
  var n = Math.max(2, maxN || 5);
  var rawStep = (hi - lo) / (n - 1);
  var mag = Math.pow(10, Math.floor(Math.log(rawStep) / Math.LN10));
  var candidates = [1, 2, 2.5, 5, 10];
  var step = mag;
  for (var i = 0; i < candidates.length; i++) {
    step = candidates[i] * mag;
    if ((hi - lo) / step + 1 <= n) break;
  }
  var out = [];
  var start = Math.ceil(lo / step) * step;
  for (var v = start; v <= hi + step * 1e-6 && out.length < n + 2; v += step) {
    out.push(Math.round(v * 1e6) / 1e6);
  }
  return out;
}

/** Integer bed ticks: whole numbers only, at most `maxN` of them. */
function rcaVizBedTicks(lo, hi, maxN) {
  var vals = rcaVizNiceTicks(lo, hi, maxN);
  var out = [];
  for (var i = 0; i < vals.length; i++) {
    var r = Math.round(vals[i]);
    if (r >= lo - 1e-9 && r <= hi + 1e-9 && out.indexOf(r) === -1) out.push(r);
  }
  if (!out.length && rcaVizFinite(lo) && rcaVizFinite(hi)) {
    out = [Math.round(lo), Math.round(hi)];
    if (out[0] === out[1]) out = [out[0]];
  }
  return out;
}

function rcaVizFmtNum(v) {
  if (!rcaVizFinite(v)) return '';
  return String(Math.round(v * 1000) / 1000);
}

/** t (0 = oldest) -> u (normalised x), honouring `opts.direction` + band. */
function rcaVizTtoU(t, band, direction) {
  var clamped = t < 0 ? 0 : (t > 1 ? 1 : t);
  return direction === 'younger-left'
    ? band.u1 - clamped * (band.u1 - band.u0)
    : band.u0 + clamped * (band.u1 - band.u0);
}

// ---------------------------------------------------------------------------
// THE LAYOUT FUNCTION (pure)
// ---------------------------------------------------------------------------

/**
 * species_ranges -> horizontal Gantt bars in normalised image space.
 *
 * Returns `{ bars, axis, meta }`:
 *   bars[i] = {
 *     row_index,       // index into result.species_ranges — the join key the
 *                      // DOM table and the onRowHover callback both use
 *     x, y, w, h,      // normalised image space, see the contract header
 *     label,           // species (canvas text; the table stays the truth)
 *     faded_ok,        // may the focus pass dim this bar? (false = placeholder)
 *     u0, u1, t0, t1,  // bar extent in plot-band u / strat t
 *     placed_by,       // 'geometry' | 'bed' | 'age' | null
 *     calibrated, confidence, response_kind, reason_codes, section,
 *     degenerate, partial, placeable, reason, value_young, value_old, unit
 *   }
 *   axis = { kind, unit, calibrated, direction, band, domain, ticks, stages,
 *            sources, mixed, rows, placed, filtered, unplaceable:
 *            [{row_index, species, reason}], row_h, degenerate_domain }
 *
 * Pure: never mutates `result`, and safe to call under node.
 */
function rcaVizLayout(result, opts) {
  var o = {};
  var k;
  for (k in RCA_VIZ_DEFAULTS) {
    if (Object.prototype.hasOwnProperty.call(RCA_VIZ_DEFAULTS, k)) {
      o[k] = RCA_VIZ_DEFAULTS[k];
    }
  }
  if (rcaVizIsDict(opts)) {
    for (k in opts) {
      if (Object.prototype.hasOwnProperty.call(opts, k)) o[k] = opts[k];
    }
  }
  // A typo must not silently produce an empty chart: unknown axis sources fall
  // back to 'auto' (the direction is normalised where it is reported).
  if (['auto', 'geometry', 'bed', 'age'].indexOf(String(o.axis_source)) === -1) {
    o.axis_source = 'auto';
  }

  var rows = rcaVizIsDict(result) && Array.isArray(result.species_ranges)
    ? result.species_ranges : [];
  var frame = rcaVizIsDict(o.frame) ? o.frame : { x0: 0, y0: 0, x1: 1, y1: 1 };
  var frameW = Math.max(1e-6, frame.x1 - frame.x0);
  var frameH = Math.max(1e-6, frame.y1 - frame.y0);
  var labelW = Math.min(o.label_width, 0.5);
  var band = {
    u0: frame.x0 + labelW * frameW,
    u1: frame.x1 - o.right_gutter * frameW,
  };
  if (band.u1 <= band.u0) band.u1 = band.u0 + 1e-6;
  var vBand = {
    v0: frame.y0 + o.head * frameH,
    v1: frame.y1 - o.foot * frameH,
  };
  if (vBand.v1 <= vBand.v0) vBand.v1 = vBand.v0 + 1e-6;

  // ---- pass 1: candidates + global domains -------------------------------
  var cands = [];
  var bedLo = null, bedHi = null;
  var maLo = null, maHi = null; // maLo = youngest (small), maHi = oldest (big)
  var stageSeen = {};
  var stageOrder = [];
  var sectionFilter = o.section === null || o.section === undefined
    ? null : String(o.section);

  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var label = rcaVizRowLabel(row, i);
    var entry = {
      index: i, label: label, row: row,
      section: rcaVizIsDict(row) ? rcaVizText(row.section) : '',
      skipped: null,
    };
    if (!rcaVizIsDict(row)) {
      entry.skipped = 'malformed_row';
      cands.push(entry);
      continue;
    }
    if (sectionFilter !== null && entry.section !== sectionFilter
        && entry.section !== '') {
      entry.skipped = 'other_section';
      cands.push(entry);
      continue;
    }
    // A row the model explicitly answered "not drawn" has no position to plot.
    var kind = rcaVizText(row.response_kind).toLowerCase();
    if (kind === 'not_drawn' || kind === 'not_drawn_or_absent') {
      entry.skipped = 'not_drawn';
      entry.c = { geometry: null, bed: null, age: null, stages: null };
      cands.push(entry);
      continue;
    }
    entry.c = rcaVizRowCandidates(row, i);
    if (entry.c.bed) {
      var bl = [entry.c.bed.young, entry.c.bed.old];
      for (var b = 0; b < 2; b++) {
        if (!rcaVizFinite(bl[b])) continue;
        bedLo = bedLo === null ? bl[b] : Math.min(bedLo, bl[b]);
        bedHi = bedHi === null ? bl[b] : Math.max(bedHi, bl[b]);
      }
    }
    if (entry.c.age) {
      var al = [entry.c.age.young, entry.c.age.old];
      for (var a = 0; a < 2; a++) {
        if (!rcaVizFinite(al[a])) continue;
        maLo = maLo === null ? al[a] : Math.min(maLo, al[a]);
        maHi = maHi === null ? al[a] : Math.max(maHi, al[a]);
      }
      if (entry.c.stages) {
        var sn = [entry.c.stages.young, entry.c.stages.old];
        for (var s = 0; s < 2; s++) {
          var name = sn[s];
          if (name && !stageSeen[name]) {
            stageSeen[name] = true;
            stageOrder.push(name);
          }
        }
      }
    }
    cands.push(entry);
  }

  // ---- dominant axis kind (drives tick labels + `axis.kind`) -------------
  var srcCount = { geometry: 0, bed: 0, age: 0, none: 0 };
  for (var p = 0; p < cands.length; p++) {
    if (cands[p].skipped) continue;
    var cc = cands[p].c;
    var best = cc.geometry && (cc.geometry.top || cc.geometry.base) ? 'geometry'
      : (cc.bed ? 'bed' : (cc.age ? 'age' : 'none'));
    cands[p].source = best;
    srcCount[best] += 1;
  }
  var dominant = o.axis_source && o.axis_source !== 'auto'
    ? String(o.axis_source)
    : (function () {
      var order = ['geometry', 'bed', 'age'];
      var pick = 'none';
      var bestN = 0;
      for (var d = 0; d < order.length; d++) {
        if (srcCount[order[d]] > bestN) { bestN = srcCount[order[d]]; pick = order[d]; }
      }
      return pick;
    })();

  // ---- pass 2: t + bar geometry ------------------------------------------
  var forceStrict = !!o.strict_axis
    || (!!o.axis_source && String(o.axis_source) !== 'auto');
  var bars = [];
  var unplaceable = [];
  var filtered = 0;
  var n = rows.length;
  var rowH = (vBand.v1 - vBand.v0) / Math.max(1, n);
  var inset = rowH * Math.max(0, Math.min(0.45, o.row_inset));

  // t is ALWAYS "0 = oldest/bottom, 1 = youngest/top"; `point` marks a range
  // that genuinely has no thickness, `partial` one whose second boundary was
  // never read (the renderer may not pretend those are the same thing).
  function tFrom(source, cand) {
    var c = cand.c || {};
    if (source === 'geometry') {
      var g = c.geometry;
      if (!g || (!g.top && !g.base)) return null;
      var youngT = g.top ? g.top.pos / RCA_VIZ_POS_MAX : null;
      var oldT = g.base ? g.base.pos / RCA_VIZ_POS_MAX : null;
      var one = youngT === null ? oldT : youngT;
      return {
        t0: oldT === null ? one : oldT,
        t1: youngT === null ? one : youngT,
        placed_by: 'geometry',
        calibrated: !!g.calibrated,
        value_old: g.base ? g.base.value : null,
        value_young: g.top ? g.top.value : null,
        point: youngT !== null && oldT !== null && youngT === oldT,
        partial: youngT === null || oldT === null,
      };
    }
    if (source === 'bed') {
      var bd = c.bed;
      if (!bd || (bd.young === null && bd.old === null)) return null;
      var span = bedHi - bedLo;
      var yB = bd.young === null ? bd.old : bd.young;
      var oB = bd.old === null ? bd.young : bd.old;
      if (span <= 1e-9) {
        return { t0: 0.5, t1: 0.5, placed_by: 'bed', calibrated: false,
          value_old: oB, value_young: yB, point: true,
          partial: bd.young === null || bd.old === null,
          degenerate_domain: true };
      }
      return { t0: (Math.min(yB, oB) - bedLo) / span,
        t1: (Math.max(yB, oB) - bedLo) / span,
        placed_by: 'bed', calibrated: false, value_old: oB, value_young: yB,
        point: yB === oB, partial: bd.young === null || bd.old === null };
    }
    if (source === 'age') {
      var ag = c.age;
      if (!ag || (ag.young === null && ag.old === null)) return null;
      var maSpan = maHi - maLo;
      var ay = ag.young === null ? ag.old : ag.young;
      var ao = ag.old === null ? ag.young : ag.old;
      if (maSpan <= 1e-9) {
        return { t0: 0.5, t1: 0.5, placed_by: 'age', calibrated: true,
          value_old: ao, value_young: ay, point: true,
          partial: ag.young === null || ag.old === null,
          degenerate_domain: true };
      }
      // Oldest (largest Ma) -> t = 0.
      return { t0: (maHi - Math.max(ay, ao)) / maSpan,
        t1: (maHi - Math.min(ay, ao)) / maSpan,
        placed_by: 'age', calibrated: true, value_old: ao, value_young: ay,
        point: ay === ao, partial: ag.young === null || ag.old === null };
    }
    return null;
  }

  for (var q = 0; q < cands.length; q++) {
    var cand = cands[q];
    var rowObj = cand.row;
    var confidence = rcaVizIsDict(rowObj) ? rcaVizNum(rowObj.confidence) : null;
    if (confidence !== null && (confidence < 0 || confidence > 1)) confidence = null;
    var bar = {
      row_index: cand.index,
      label: cand.label,
      species: cand.label,
      section: cand.section,
      confidence: confidence,
      response_kind: rcaVizIsDict(rowObj) && rowObj.response_kind
        ? rcaVizText(rowObj.response_kind) : null,
      reason_codes: rcaVizIsDict(rowObj) && Array.isArray(rowObj.reason_codes)
        ? rowObj.reason_codes.slice() : [],
      placed_by: null,
      calibrated: false,
      placeable: false,
      faded_ok: false,
      degenerate: false,
      partial: false,
      reason: null,
      unit: '',
      value_old: null,
      value_young: null,
      t0: null, t1: null, u0: null, u1: null,
      // geometry is filled below for every row so the renderer can keep the
      // row band even when the bar cannot be placed.
      x: band.u0,
      y: vBand.v0 + cand.index * rowH + inset,
      w: 0,
      h: Math.max(1e-6, rowH - 2 * inset),
    };
    if (cand.skipped) {
      bar.reason = cand.skipped;
      bar.placeable = false;
      if (cand.skipped === 'other_section') {
        // Filtered out of THIS view (multi-section result): no strip at all,
        // and it is NOT reported as unplaceable — the data is fine, the view
        // is scoped. row_index still matches the DOM table row.
        bar.w = 0;
        bar.u0 = band.u0; bar.u1 = band.u0;
        filtered += 1;
      } else {
        bar.w = band.u1 - band.u0;
        bar.u0 = band.u0; bar.u1 = band.u1;
        unplaceable.push({ row_index: bar.row_index, species: bar.label,
          reason: bar.reason });
      }
      bars.push(bar);
      continue;
    }
    var source = cand.source === 'none' ? null : cand.source;
    // An explicit `axis_source` pins the whole view to one space; `strict_axis`
    // pins it to the dominant one. Either way a row that cannot speak that
    // space is greyed out instead of being projected onto a foreign axis.
    if (forceStrict && source && source !== dominant) {
      source = null;
      bar.reason = 'axis_kind_mismatch';
    }
    var placed = source ? tFrom(source, cand) : null;
    if (!placed) {
      bar.reason = (bar.reason && bar.reason !== null) ? bar.reason : 'no_position';
      bar.w = band.u1 - band.u0;
      bar.u0 = band.u0; bar.u1 = band.u1;
      bars.push(bar);
      unplaceable.push({ row_index: bar.row_index, species: bar.label,
        reason: bar.reason });
      continue;
    }
    var t0 = Math.min(placed.t0, placed.t1);
    var t1 = Math.max(placed.t0, placed.t1);
    var u0 = rcaVizTtoU(t0, band, o.direction);
    var u1 = rcaVizTtoU(t1, band, o.direction);
    if (u1 < u0) { var tmp = u0; u0 = u1; u1 = tmp; }
    var w = u1 - u0;
    var degenerate = !!placed.point || w <= 1e-9;
    if (w < o.min_bar_w) {
      // Keep a point range visible: widen around the centre, clamped to band.
      var centre = (u0 + u1) / 2;
      u0 = Math.max(band.u0, centre - o.min_bar_w / 2);
      u1 = Math.min(band.u1, u0 + o.min_bar_w);
      u0 = Math.max(band.u0, u1 - o.min_bar_w);
      w = u1 - u0;
    }
    bar.placeable = true;
    bar.faded_ok = true;
    bar.placed_by = placed.placed_by;
    bar.calibrated = !!placed.calibrated;
    bar.degenerate = degenerate;
    bar.partial = !!placed.partial;
    bar.t0 = t0; bar.t1 = t1;
    bar.u0 = u0; bar.u1 = u1;
    bar.x = u0; bar.w = w;
    bar.value_old = placed.value_old === undefined ? null : placed.value_old;
    bar.value_young = placed.value_young === undefined ? null : placed.value_young;
    bar.unit = placed.placed_by === 'age' ? 'Ma'
      : (placed.placed_by === 'bed' ? 'bed' : '');
    bars.push(bar);
  }

  // ---- ticks -------------------------------------------------------------
  var vdomain = dominant === 'geometry' ? rcaVizVerticalDomain(result) : null;
  if (!vdomain && srcCount.geometry && rcaVizIsDict(result)) {
    vdomain = rcaVizVerticalDomain(result);
  }
  var ticks = [];
  var stages = [];
  var axisUnit = '';
  var domainOut = { lo: null, hi: null, native: null };
  if (dominant === 'geometry') {
    axisUnit = vdomain && vdomain.unit ? vdomain.unit : 'pos_0_999';
    for (var f = 0; f <= 4; f++) {
      var t = f / 4;
      var u = rcaVizTtoU(t, band, o.direction);
      var lbl;
      var val;
      if (vdomain) {
        val = vdomain.at_0 + t * (vdomain.at_999 - vdomain.at_0);
        lbl = rcaVizFmtNum(val) + (vdomain.unit ? ' ' + vdomain.unit : '');
      } else {
        val = t * RCA_VIZ_POS_MAX;
        lbl = String(Math.round(val));
      }
      ticks.push({ u: u, t: t, label: lbl, value: val, major: f % 2 === 0 });
    }
    domainOut = { lo: 0, hi: RCA_VIZ_POS_MAX, native: vdomain ? 'calibrated' : 'frame' };
  } else if (dominant === 'bed' && bedHi !== null) {
    axisUnit = 'bed';
    var bt = rcaVizBedTicks(bedLo, bedHi, o.max_ticks);
    var bspan = bedHi - bedLo;
    for (var bi = 0; bi < bt.length; bi++) {
      var btT = bspan <= 1e-9 ? 0.5 : (bt[bi] - bedLo) / bspan;
      ticks.push({ u: rcaVizTtoU(btT, band, o.direction), t: btT,
        label: 'Bed ' + bt[bi], value: bt[bi], major: true });
    }
    domainOut = { lo: bedLo, hi: bedHi, native: 'bed' };
  } else if (dominant === 'age' && maHi !== null) {
    axisUnit = 'Ma';
    var asum = maHi - maLo;
    // Stage bands when the resolution came from named stages.
    if (stageOrder.length >= 2 && stageOrder.length <= o.max_ticks + 2) {
      var resolved = [];
      for (var si = 0; si < stageOrder.length; si++) {
        var b = rcaVizStageBounds(stageOrder[si]);
        if (b) resolved.push(b);
      }
      resolved.sort(function (l, r) { return r.base_ma - l.base_ma; }); // old first
      for (var ri = 0; ri < resolved.length; ri++) {
        var st = resolved[ri];
        var tOld = asum <= 1e-9 ? 0 : (maHi - st.base_ma) / asum;
        var tYoung = asum <= 1e-9 ? 1 : (maHi - st.top_ma) / asum;
        var su0 = rcaVizTtoU(tOld, band, o.direction);
        var su1 = rcaVizTtoU(tYoung, band, o.direction);
        stages.push({ name: st.name, u0: Math.min(su0, su1), u1: Math.max(su0, su1),
          top_ma: st.top_ma, base_ma: st.base_ma });
      }
      for (var sti = 0; sti < stages.length; sti++) {
        var mid = (stages[sti].u0 + stages[sti].u1) / 2;
        ticks.push({ u: mid, t: null, label: stages[sti].name,
          value: (stages[sti].top_ma + stages[sti].base_ma) / 2, major: true });
      }
    } else {
      var mt = rcaVizNiceTicks(maLo, maHi, o.max_ticks);
      for (var mi = 0; mi < mt.length; mi++) {
        var mT = asum <= 1e-9 ? 0.5 : (maHi - mt[mi]) / asum;
        ticks.push({ u: rcaVizTtoU(mT, band, o.direction), t: mT,
          label: rcaVizFmtNum(mt[mi]) + ' Ma', value: mt[mi], major: true });
      }
    }
    domainOut = { lo: maLo, hi: maHi, native: 'Ma' };
  }

  var kinds = [];
  if (srcCount.geometry) kinds.push('geometry');
  if (srcCount.bed) kinds.push('bed');
  if (srcCount.age) kinds.push('age');

  var placedCount = 0;
  for (var pc = 0; pc < bars.length; pc++) if (bars[pc].placeable) placedCount++;

  return {
    bars: bars,
    axis: {
      kind: bars.length && dominant ? dominant : 'none',
      unit: axisUnit,
      calibrated: dominant === 'geometry' ? !!vdomain : dominant === 'age',
      direction: o.direction === 'younger-left' ? 'younger-left' : 'younger-right',
      band: { u0: band.u0, u1: band.u1, v0: vBand.v0, v1: vBand.v1 },
      row_h: rowH,
      head: o.head, foot: o.foot,
      label_width: labelW,
      domain: domainOut,
      ticks: ticks,
      stages: stages,
      sources: srcCount,
      mixed: kinds.length > 1,
      kind_order: kinds,
      rows: n,
      placed: placedCount,
      filtered: filtered,
      unplaceable: unplaceable,
      degenerate_domain: dominant === 'bed' ? (bedHi - bedLo) <= 1e-9
        : (dominant === 'age' ? (maHi - maLo) <= 1e-9 : false),
    },
    meta: {
      version: 1,
      space: 'normalised-image-px',
      pos_scale: RCA_VIZ_POS_MAX,
      section: sectionFilter,
      strict_axis: !!o.strict_axis,
    },
  };
}

// ---------------------------------------------------------------------------
// focus state machine — pure, data side (uPlot semantics)
// ---------------------------------------------------------------------------

/** Base (unfocused) alpha of a bar: confidence-tinted, placeholders muted. */
function rcaVizBarAlpha(bar, opts) {
  if (!rcaVizIsDict(bar) || !bar.placeable) return RCA_VIZ_PLACEHOLDER_ALPHA;
  var o = opts || {};
  var dim = rcaVizFinite(o.confidence_floor) ? o.confidence_floor : RCA_VIZ_CONF_FLOOR;
  if (o.confidence_fade === false || bar.confidence === null
      || bar.confidence === undefined) return 1;
  var c = Math.max(0, Math.min(1, bar.confidence));
  return Math.round((dim + (1 - dim) * c) * 1000) / 1000;
}

/**
 * uPlot focus semantics as a pure function, so the alpha maths is testable
 * without a canvas and the render layer has no private copy of the rule.
 *
 * `focus` is a row_index or null. Returns one record per bar:
 *   { row_index, alpha, base, outline, focused, faded }
 * Rules: no focus -> every bar at its base alpha; focused bar -> base alpha +
 * outline; every OTHER *placeable* bar -> base alpha * dim (default 0.3);
 * unplaceable placeholders are never multiplied further (faded_ok === false).
 */
function rcaVizFocusPass(bars, focus, opts) {
  var o = opts || {};
  var dim = rcaVizFinite(o.dim) ? o.dim : RCA_VIZ_FOCUS_DIM;
  var list = Array.isArray(bars) ? bars : [];
  var f = (typeof focus === 'number' && isFinite(focus)) ? focus : null;
  var out = [];
  for (var i = 0; i < list.length; i++) {
    var bar = list[i];
    var base = rcaVizBarAlpha(bar, o);
    var focused = f !== null && rcaVizIsDict(bar) && bar.row_index === f;
    var alpha = base;
    var faded = false;
    if (f !== null && !focused && bar && bar.faded_ok) {
      alpha = Math.round(base * dim * 1000) / 1000;
      faded = true;
    }
    out.push({
      row_index: bar && typeof bar.row_index === 'number' ? bar.row_index : i,
      alpha: alpha,
      base: base,
      outline: f !== null && focused,
      focused: focused,
      faded: faded,
      placeable: !!(bar && bar.placeable),
    });
  }
  return out;
}

// ---------------------------------------------------------------------------
// hit testing — pure
// ---------------------------------------------------------------------------

/**
 * Which bar owns the point (u,v) of normalised image space?
 * Returns the row_index or null. Bars are padded by `opts.hit_pad_u` in x and
 * by `hit_pad_v_ratio` of the row band in y so a thin bar stays reachable; the
 * padded boxes are sorted top-down and first-match wins, so overlapping bars
 * resolve to the row they were laid out in.
 */
function rcaVizHitTest(layout, u, v, opts) {
  var o = opts || {};
  if (!rcaVizIsDict(layout) || !Array.isArray(layout.bars)) return null;
  if (!rcaVizFinite(u) || !rcaVizFinite(v)) return null;
  var padU = rcaVizFinite(o.hit_pad_u) ? o.hit_pad_u : RCA_VIZ_DEFAULTS.hit_pad_u;
  var padRatio = rcaVizFinite(o.hit_pad_v_ratio)
    ? o.hit_pad_v_ratio : RCA_VIZ_DEFAULTS.hit_pad_v_ratio;
  var rowH = rcaVizIsDict(layout.axis) && rcaVizFinite(layout.axis.row_h)
    ? layout.axis.row_h : 0.02;
  var padV = rowH * padRatio;
  for (var i = 0; i < layout.bars.length; i++) {
    var bar = layout.bars[i];
    if (!rcaVizIsDict(bar)) continue;
    var x0 = bar.x - padU;
    var x1 = bar.x + bar.w + padU;
    var y0 = bar.y - padV;
    var y1 = bar.y + bar.h + padV;
    if (u >= x0 && u <= x1 && v >= y0 && v <= y1) return bar.row_index;
  }
  return null;
}

// ---------------------------------------------------------------------------
// text alternative (pure) — the accessible twin of the canvas
// ---------------------------------------------------------------------------

var RCA_VIZ_STRINGS = {
  en: {
    summary: '{placed} of {rows} range rows plotted on the {kind} axis; {unplaced} unplaceable.',
    summary_empty: 'No species range rows to plot.',
    kind_geometry: 'pixel-calibrated (0-999 frame)',
    kind_bed: 'bed',
    kind_age: 'stage/age',
    kind_none: 'unresolved',
    unplaced_names: 'Unplaceable: {names}',
    mixed: ' Mixed sources: {sources}.',
  },
  zh: {
    summary: '{rows} 行延限中 {placed} 行已按{kind}轴定位；{unplaced} 行无法定位。',
    summary_empty: '没有可绘制的延限行。',
    kind_geometry: '像素标定（0-999 图框）',
    kind_bed: '层位',
    kind_age: '阶/年龄',
    kind_none: '未定轴',
    unplaced_names: '无法定位：{names}',
    mixed: ' 混合来源：{sources}。',
  },
};

function rcaVizT(key, params) {
  var lang = typeof RCA_LANG === 'string' && RCA_LANG ? RCA_LANG : 'en';
  var dict = RCA_VIZ_STRINGS[lang] || RCA_VIZ_STRINGS.en;
  var s = dict[key] !== undefined ? dict[key] : (RCA_VIZ_STRINGS.en[key] || key);
  if (params) {
    for (var k in params) {
      if (Object.prototype.hasOwnProperty.call(params, k)) {
        s = s.split('{' + k + '}').join(String(params[k]));
      }
    }
  }
  return s;
}

/** Plain-language summary for `.rca-viz-summary` (aria-visible text twin). */
function rcaVizSummary(layout) {
  if (!rcaVizIsDict(layout) || !Array.isArray(layout.bars) || !layout.bars.length) {
    return rcaVizT('summary_empty');
  }
  var ax = layout.axis || {};
  var kindKey = 'kind_' + (ax.kind === 'geometry' ? 'geometry'
    : ax.kind === 'bed' ? 'bed' : ax.kind === 'age' ? 'age' : 'none');
  var text = rcaVizT('summary', {
    placed: ax.placed, rows: ax.rows, kind: rcaVizT(kindKey),
    unplaced: (ax.unplaceable || []).length,
  });
  if (ax.mixed && Array.isArray(ax.kind_order)) {
    text += rcaVizT('mixed', { sources: ax.kind_order.join(' + ') });
  }
  var list = (ax.unplaceable || []).slice(0, 8).map(function (e) {
    return e.species;
  });
  if (list.length) text += ' ' + rcaVizT('unplaced_names', {
    names: list.join(', ') + ((ax.unplaceable || []).length > 8 ? ' …' : ''),
  });
  return text;
}

// ---------------------------------------------------------------------------
// RENDER LAYER (browser only; every entry point guards `typeof document`)
// ---------------------------------------------------------------------------

// The ONE focus/hover state holder — mirrors uPlot keeping series state in the
// instance instead of scattered on DOM nodes.
var RCA_VIZ_STATE = {
  host: null, stage: null, img: null, barsCv: null, hoverCv: null,
  summary: null,
  layout: null, result: null, opts: null, imageSrc: null,
  focus: null,      // pinned row_index (uPlot focus)
  hover: null,      // transient row_index under the pointer
  flash: null,      // {row_index, until} driven by locateTo()
  dpr: 1,
  stretch: 1,       // v-axis stretch of the image map, see the contract header
  wired: false,
  hoverListeners: [],
  resizeTimer: null,
  flashTimer: null,
  tokens: null,
};

function rcaVizHasDom() {
  return typeof document !== 'undefined' && !!document && !!document.createElement;
}

/** Read a CSS custom property once per theme change (cached in STATE.tokens). */
function rcaVizToken(name, fallback) {
  if (typeof document === 'undefined' || !document.documentElement
      || typeof window === 'undefined' || !window.getComputedStyle) return fallback;
  if (!RCA_VIZ_STATE.tokens) {
    try {
      RCA_VIZ_STATE.tokens = window.getComputedStyle(document.documentElement);
    } catch (_e) {
      RCA_VIZ_STATE.tokens = null;
    }
  }
  if (!RCA_VIZ_STATE.tokens) return fallback;
  var raw = RCA_VIZ_STATE.tokens.getPropertyValue(name);
  var v = raw && String(raw).trim ? String(raw).trim() : '';
  return v || fallback;
}

/** Bar fill by placement source — existing design tokens, no new palette. */
function rcaVizBarColor(bar) {
  if (!bar || !bar.placeable) return rcaVizToken('--text-light', '#78716c');
  if (bar.placed_by === 'geometry') return rcaVizToken('--primary-color', '#2563eb');
  if (bar.placed_by === 'bed') return rcaVizToken('--success-color', '#10b981');
  if (bar.placed_by === 'age') return rcaVizToken('--info-color', '#6366f1');
  return rcaVizToken('--secondary-color', '#6b7280');
}

/**
 * Find the structure index.html ships, creating whatever is missing so the
 * renderer also works on an older page (and after `innerHTML` wipes).
 */
function rcaVizEnsure(hostEl) {
  var S = RCA_VIZ_STATE;
  if (S.host === hostEl && S.stage && S.barsCv) return true;
  if (typeof hostEl.querySelector !== 'function') return false;
  var stage = hostEl.querySelector('.rca-viz-stage');
  if (!stage && typeof document !== 'undefined') {
    stage = document.createElement('div');
    stage.className = 'rca-viz-stage';
    hostEl.appendChild(stage);
  }
  function ensure(sel, tag, cls) {
    var el = stage.querySelector(sel);
    if (!el) {
      el = document.createElement(tag);
      el.className = cls;
      stage.appendChild(el);
    }
    return el;
  }
  S.img = ensure('.rca-viz-image', 'img', 'rca-viz-image');
  S.barsCv = ensure('.rca-viz-bars', 'canvas', 'rca-viz-canvas rca-viz-bars');
  S.hoverCv = ensure('.rca-viz-hover', 'canvas', 'rca-viz-canvas rca-viz-hover');
  S.summary = hostEl.querySelector('.rca-viz-summary');
  if (!S.summary) {
    S.summary = document.createElement('p');
    S.summary.className = 'rca-viz-summary';
    hostEl.appendChild(S.summary);
  }
  S.stage = stage;
  S.host = hostEl;
  // WPD/aria discipline: the overlay is decorative, the table is the truth.
  S.img.setAttribute('aria-hidden', 'true');
  S.img.setAttribute('alt', '');
  S.barsCv.setAttribute('aria-hidden', 'true');
  S.barsCv.setAttribute('role', 'img');
  S.hoverCv.setAttribute('aria-hidden', 'true');
  S.hoverCv.setAttribute('role', 'img');
  if (!S.summary.getAttribute('role')) S.summary.setAttribute('role', 'status');
  return true;
}

function rcaVizCtx(canvas) {
  if (!canvas || typeof canvas.getContext !== 'function') return null;
  try {
    return canvas.getContext('2d');
  } catch (_e) {
    return null;
  }
}

/** Size a canvas layer to the stage box in device px (contract: u * width). */
function rcaVizSizeCanvas(canvas, cssW, cssH, dpr) {
  var w = Math.max(1, Math.round(cssW * dpr));
  var h = Math.max(1, Math.round(cssH * dpr));
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
}

/** Stage geometry: css width from the host, height from the row count. */
function rcaVizStageBox(layout) {
  var S = RCA_VIZ_STATE;
  var host = S.host;
  var cssW = (host.clientWidth || host.offsetWidth || 640);
  var natural = null;
  if (S.img && S.img.naturalWidth && S.img.naturalHeight) {
    natural = cssW * (S.img.naturalHeight / S.img.naturalWidth);
  }
  var rows = layout && layout.axis ? layout.axis.rows : 0;
  var rowPx = 18;
  var need = 40 + rows * rowPx;
  var cssH = Math.max(220, natural || 0, need);
  var maxHeight = 0;
  if (typeof window !== 'undefined' && window.innerHeight) maxHeight = window.innerHeight * 0.7;
  if (maxHeight && cssH > maxHeight) cssH = Math.max(220, maxHeight);
  return { w: cssW, h: cssH, stretch: natural ? cssH / natural : 1 };
}

/** Layer 1: axis + bars + labels. Repainted on every focus change. */
function rcaVizDrawBars() {
  var S = RCA_VIZ_STATE;
  var ctx = rcaVizCtx(S.barsCv);
  var layout = S.layout;
  if (!ctx || !layout) return;
  var W = S.barsCv.width, H = S.barsCv.height;
  var dpr = S.dpr || 1;
  ctx.clearRect(0, 0, W, H);
  var ax = layout.axis || {};
  var band = ax.band || { u0: 0, u1: 1, v0: 0, v1: 1 };
  var focus = rcaVizFocusPass(layout.bars, S.focus, S.opts);
  var px = function (u) { return u * W; };
  var py = function (v) { return v * H; };
  // Canvas font sizes are device px (no ctx transform is applied), so the CSS
  // px guess has to be multiplied back by dpr.
  var rowCssH = (ax.row_h || 0.02) * (H / dpr);
  var fontPx = Math.max(9, Math.min(13, rowCssH * 0.66));
  var font = Math.round(fontPx * dpr) + 'px '
    + rcaVizToken('--font-sans', 'sans-serif');
  var labelMaxW = Math.max(0, band.u0 * W - 8 * dpr);

  // alternating stage bands, when the axis resolved to named stages
  if (Array.isArray(ax.stages) && ax.stages.length) {
    for (var s = 0; s < ax.stages.length; s++) {
      var st = ax.stages[s];
      ctx.globalAlpha = 0.06;
      ctx.fillStyle = s % 2 ? rcaVizToken('--primary-color', '#2563eb')
        : rcaVizToken('--secondary-color', '#6b7280');
      ctx.fillRect(px(st.u0), py(band.v0), px(st.u1 - st.u0), py(band.v1 - band.v0));
    }
    ctx.globalAlpha = 1;
  }

  // axis line + ticks
  var axisColor = rcaVizToken('--border-strong', '#d6d3d1');
  var textColor = rcaVizToken('--text-muted', '#57534e');
  ctx.strokeStyle = axisColor;
  ctx.lineWidth = 1 * dpr;
  ctx.beginPath();
  ctx.moveTo(px(band.u0), py(band.v0 - 0.008));
  ctx.lineTo(px(band.u1), py(band.v0 - 0.008));
  ctx.stroke();
  ctx.font = font;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  ctx.fillStyle = textColor;
  var ticks = ax.ticks || [];
  for (var t = 0; t < ticks.length; t++) {
    var tk = ticks[t];
    var x = px(tk.u);
    ctx.beginPath();
    ctx.moveTo(x, py(band.v0 - 0.008));
    ctx.lineTo(x, py(band.v0 + 0.004));
    ctx.stroke();
    ctx.save();
    ctx.translate(x, py(band.v0 - 0.012));
    if (String(tk.label).length > 12) {
      ctx.rotate(-Math.PI / 6);
      ctx.textAlign = 'right';
      ctx.textBaseline = 'middle';
    }
    ctx.fillText(String(tk.label), 0, 0);
    ctx.restore();
  }

  // bars + row labels
  for (var i = 0; i < layout.bars.length; i++) {
    var bar = layout.bars[i];
    var st2 = focus[i] || { alpha: 1, outline: false };
    ctx.globalAlpha = st2.alpha;
    var x0 = px(bar.x), y0 = py(bar.y), bw = px(bar.w), bh = py(bar.h);
    if (bar.placeable) {
      ctx.fillStyle = rcaVizBarColor(bar);
      if (bar.degenerate) {
        // point range -> diamond, so a zero-thickness range is not mistaken
        // for a mis-rendered bar.
        var cx = x0 + bw / 2, cy = y0 + bh / 2, rr = Math.max(2 * dpr, bh / 2);
        ctx.beginPath();
        ctx.moveTo(cx, cy - rr);
        ctx.lineTo(cx + rr, cy);
        ctx.lineTo(cx, cy + rr);
        ctx.lineTo(cx - rr, cy);
        ctx.closePath();
        ctx.fill();
      } else {
        ctx.fillRect(x0, y0, Math.max(1, bw), bh);
      }
      if (st2.outline) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = rcaVizToken('--primary-active', '#2563eb');
        ctx.lineWidth = 2 * dpr;
        ctx.strokeRect(x0 - 1, y0 - 1, Math.max(2, bw) + 2, bh + 2);
        ctx.lineWidth = 1 * dpr;
      }
    } else {
      // unplaceable: grey hatched strip, never dimmed by the focus pass
      ctx.strokeStyle = rcaVizToken('--text-light', '#78716c');
      ctx.lineWidth = 1 * dpr;
      ctx.setLineDash([3 * dpr, 3 * dpr]);
      ctx.strokeRect(x0, y0, Math.max(1, bw), bh);
      ctx.setLineDash([]);
      if (st2.outline) {
        ctx.strokeStyle = rcaVizToken('--warning-color', '#b45309');
        ctx.strokeRect(x0 - 1, y0 - 1, bw + 2, bh + 2);
      }
    }
    // label column (canvas copy of a value the DOM table already owns)
    ctx.globalAlpha = st2.alpha;
    ctx.fillStyle = textColor;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.font = font;
    var labelX = px(band.u0) - 6 * dpr;
    ctx.fillText(rcaVizClip(ctx, String(bar.label || ''), labelMaxW), labelX, y0 + bh / 2);
  }
  ctx.globalAlpha = 1;
}

/** Ellipsise a label to fit `maxW` device px. */
function rcaVizClip(ctx, text, maxW) {
  if (!text) return '';
  if (maxW <= 0 || ctx.measureText(text).width <= maxW) return text;
  var t = text;
  while (t.length > 2 && ctx.measureText(t + '…').width > maxW) t = t.slice(0, -1);
  return t + '…';
}

/** Layer 2: transient hover highlight + tooltip. Cheap to clear. */
function rcaVizDrawHover() {
  var S = RCA_VIZ_STATE;
  var ctx = rcaVizCtx(S.hoverCv);
  if (!ctx) return;
  var W = S.hoverCv.width, H = S.hoverCv.height;
  var dpr = S.dpr || 1;
  ctx.clearRect(0, 0, W, H);
  if (!S.layout) return;
  var idx = S.hover !== null ? S.hover : (S.flash ? S.flash.row_index : null);
  if (idx === null) return;
  var bar = null;
  for (var i = 0; i < S.layout.bars.length; i++) {
    if (S.layout.bars[i].row_index === idx) { bar = S.layout.bars[i]; break; }
  }
  if (!bar) return;
  var ax = S.layout.axis || {};
  var band = ax.band || { u0: 0, u1: 1, v0: 0, v1: 1 };
  var y0 = bar.y * H, h = bar.h * H;
  // row crosshair across the whole plot band
  ctx.globalAlpha = (S.flash && S.hover === null) ? 0.9 : 0.55;
  ctx.fillStyle = rcaVizToken('--primary-soft', '#eff6ff');
  ctx.fillRect(band.u0 * W, y0 - h * 0.3, (band.u1 - band.u0) * W, h * 1.6);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = rcaVizToken('--primary-active', '#2563eb');
  ctx.lineWidth = 1.5 * dpr;
  ctx.strokeRect(bar.x * W - 1, y0 - 1, Math.max(2, bar.w * W) + 2, h + 2);
  ctx.lineWidth = 1 * dpr;

  // tooltip = the row's own text, so the canvas never says something the
  // table does not
  var tip = String(bar.label || '');
  if (bar.placeable) {
    var lo = rcaVizValueText(bar.value_old, bar.unit);
    var hi = rcaVizValueText(bar.value_young, bar.unit);
    if (lo || hi) tip += '  ' + (lo || '?') + ' → ' + (hi || '?');
    tip += '  [' + bar.placed_by + (bar.calibrated ? '/calibrated' : '') + ']';
  } else {
    tip += '  [' + (bar.reason || 'unplaceable') + ']';
  }
  ctx.font = Math.round(11 * dpr) + 'px ' + rcaVizToken('--font-mono', 'monospace');
  var tw = ctx.measureText(tip).width + 12 * dpr;
  var th = 18 * dpr;
  var tx = Math.min(W - tw - 2, Math.max(2, bar.x * W));
  var ty = y0 > th + 4 ? y0 - th - 3 : y0 + h + 3;
  ctx.fillStyle = rcaVizToken('--bg-white', '#ffffff');
  ctx.globalAlpha = 0.94;
  ctx.fillRect(tx, ty, tw, th);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = rcaVizToken('--border-strong', '#d6d3d1');
  ctx.strokeRect(tx, ty, tw, th);
  ctx.fillStyle = rcaVizToken('--text-dark', '#0c0a09');
  ctx.textAlign = 'left';
  ctx.textBaseline = 'middle';
  ctx.fillText(tip, tx + 6 * dpr, ty + th / 2);
}

function rcaVizValueText(v, unit) {
  if (v === null || v === undefined) return '';
  var s = rcaVizFinite(v) ? rcaVizFmtNum(v) : String(v);
  if (!s) return '';
  if (unit === 'Ma') return s + ' Ma';
  if (unit === 'bed') return 'Bed ' + s;
  return s;
}

/** Repaint both layers (single entry point, no half-redraws). */
function rcaVizDraw() {
  if (!rcaVizHasDom() || !RCA_VIZ_STATE.layout) return;
  rcaVizDrawBars();
  rcaVizDrawHover();
}

function rcaVizPointer(ev) {
  var S = RCA_VIZ_STATE;
  if (!S.hoverCv || typeof ev !== 'object' || !ev) return null;
  var rect = typeof S.hoverCv.getBoundingClientRect === 'function'
    ? S.hoverCv.getBoundingClientRect() : null;
  if (!rect || !rect.width || !rect.height) return null;
  var clientX = ev.clientX !== undefined ? ev.clientX : (ev.pageX || 0);
  var clientY = ev.clientY !== undefined ? ev.clientY : (ev.pageY || 0);
  return { u: (clientX - rect.left) / rect.width, v: (clientY - rect.top) / rect.height };
}

function rcaVizEmitHover(idx) {
  var list = RCA_VIZ_STATE.hoverListeners.slice();
  for (var i = 0; i < list.length; i++) {
    try { list[i](idx, RCA_VIZ_STATE.layout); } catch (_e) { /* cb must not break the paint */ }
  }
}

function rcaVizOnPointerMove(ev) {
  var S = RCA_VIZ_STATE;
  var pt = rcaVizPointer(ev);
  var idx = pt ? rcaVizHitTest(S.layout, pt.u, pt.v, S.opts) : null;
  if (idx === S.hover) return;
  S.hover = idx;
  rcaVizDrawHover();
  rcaVizEmitHover(idx);
}

function rcaVizOnPointerLeave() {
  var S = RCA_VIZ_STATE;
  if (S.hover === null) return;
  S.hover = null;
  rcaVizDrawHover();
  rcaVizEmitHover(null);
}

function rcaVizOnPointerDown(ev) {
  var S = RCA_VIZ_STATE;
  var pt = rcaVizPointer(ev);
  var idx = pt ? rcaVizHitTest(S.layout, pt.u, pt.v, S.opts) : null;
  if (idx === null) { rcaVizClearFocus(); return; }
  rcaVizFocusRow(idx === S.focus ? null : idx);
}

function rcaVizWire() {
  var S = RCA_VIZ_STATE;
  if (S.wired || !S.hoverCv || typeof S.hoverCv.addEventListener !== 'function') return;
  S.hoverCv.addEventListener('mousemove', rcaVizOnPointerMove);
  S.hoverCv.addEventListener('mouseleave', rcaVizOnPointerLeave);
  S.hoverCv.addEventListener('click', rcaVizOnPointerDown);
  S.hoverCv.addEventListener('touchstart', function (ev) {
    if (ev && ev.touches && ev.touches[0]) rcaVizOnPointerMove(ev.touches[0]);
  }, { passive: true });
  S.wired = true;
  // Esc releases the pinned focus — the hint index.html prints next to the
  // title promises exactly that, so the renderer owns the key binding.
  if (typeof document !== 'undefined' && document
      && typeof document.addEventListener === 'function') {
    document.addEventListener('keydown', function (ev) {
      if (!ev || (ev.key !== 'Escape' && ev.key !== 'Esc')) return;
      if (RCA_VIZ_STATE.focus === null) return;
      rcaVizClearFocus();
      rcaVizEmitHover(RCA_VIZ_STATE.hover);
    });
  }
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('resize', function () {
      if (S.resizeTimer && typeof clearTimeout === 'function') clearTimeout(S.resizeTimer);
      S.resizeTimer = typeof setTimeout === 'function'
        ? setTimeout(function () { rcaVizResize(); }, 120) : null;
    });
    // Theme flips change the token values the canvas reads.
    if (typeof MutationObserver === 'function' && document.documentElement) {
      try {
        new MutationObserver(function () {
          S.tokens = null;
          rcaVizDraw();
        }).observe(document.documentElement, {
          attributes: true, attributeFilter: ['data-theme', 'class'],
        });
      } catch (_e) { /* no observer, no repaint on theme flip */ }
    }
  }
}

function rcaVizApplyStageSize(box) {
  var S = RCA_VIZ_STATE;
  S.stretch = box.stretch || 1;
  if (!S.stage || !S.stage.style) return;
  S.stage.style.height = Math.round(box.h) + 'px';
  // Data attribute keeps the documented v-stretch discoverable in devtools.
  if (typeof S.stage.setAttribute === 'function') {
    S.stage.setAttribute('data-rca-viz-stretch', String(Math.round(box.stretch * 1000) / 1000));
  }
}

function rcaVizSizeLayers(box) {
  var S = RCA_VIZ_STATE;
  var dpr = (typeof window !== 'undefined' && window.devicePixelRatio)
    ? window.devicePixelRatio : 1;
  S.dpr = dpr > 0 ? dpr : 1;
  rcaVizSizeCanvas(S.barsCv, box.w, box.h, S.dpr);
  rcaVizSizeCanvas(S.hoverCv, box.w, box.h, S.dpr);
}

/** Re-measure + repaint after a resize / theme / image change. */
function rcaVizResize() {
  var S = RCA_VIZ_STATE;
  if (!rcaVizHasDom() || !S.host || !S.layout) return false;
  var box = rcaVizStageBox(S.layout);
  rcaVizApplyStageSize(box);
  rcaVizSizeLayers(box);
  rcaVizDraw();
  return true;
}

/**
 * render(hostEl, result, imageSrc, opts) — idempotent.
 * Returns true when the canvas actually got a layout.
 */
function rcaVizRender(hostEl, result, imageSrc, opts) {
  if (!rcaVizHasDom()) return false;
  if (!hostEl || typeof hostEl.appendChild !== 'function') return false;
  var S = RCA_VIZ_STATE;
  if (!rcaVizEnsure(hostEl)) return false;
  S.opts = opts || {};
  S.result = result;
  var layout = rcaVizLayout(result, opts);
  S.layout = layout;
  S.focus = null;
  S.hover = null;
  S.flash = null;
  if (imageSrc) {
    // setAttribute (not the `src` property) keeps the DOM readable both ways:
    // the clear branch below checks getAttribute('src'), and stub/no-DOM
    // element shims only mirror attributes.
    var cur = typeof S.img.getAttribute === 'function'
      ? S.img.getAttribute('src') : null;
    if (S.imageSrc !== imageSrc || cur !== imageSrc) {
      S.imageSrc = imageSrc;
      S.img.setAttribute('src', imageSrc);
    }
    // Bind the re-measure hook once per <img> node, not once per render.
    if (typeof S.img.addEventListener === 'function' && !S.img.rcaVizBound) {
      S.img.rcaVizBound = true;
      S.img.addEventListener('load', function () { rcaVizResize(); });
    }
  } else if (S.img.getAttribute('src')) {
    S.img.removeAttribute('src');
    S.imageSrc = null;
  }
  if (typeof S.summary.textContent !== 'undefined') {
    S.summary.textContent = rcaVizSummary(layout);
  }
  var box = rcaVizStageBox(layout);
  rcaVizApplyStageSize(box);
  rcaVizSizeLayers(box);
  rcaVizWire();
  rcaVizDrawBars();
  rcaVizDrawHover();
  return layout.bars.length > 0;
}

/** Pin the uPlot-style focus on `row_index` (null === clearFocus). */
function rcaVizFocusRow(row_index) {
  var S = RCA_VIZ_STATE;
  if (!S.layout) return false;
  var idx = (typeof row_index === 'number' && isFinite(row_index)) ? row_index : null;
  if (idx !== null) {
    var found = false;
    for (var i = 0; i < S.layout.bars.length; i++) {
      if (S.layout.bars[i].row_index === idx) { found = true; break; }
    }
    if (!found) return false;
  }
  S.focus = idx;
  rcaVizDraw();
  return true;
}

function rcaVizClearFocus() {
  var S = RCA_VIZ_STATE;
  if (S.focus === null) return false;
  S.focus = null;
  rcaVizDraw();
  return true;
}

/**
 * Scroll the bar into view and flash it on the hover layer.
 * Returns false when there is no layout/host or the row_index is unknown.
 */
function rcaVizLocateTo(row_index) {
  var S = RCA_VIZ_STATE;
  if (!rcaVizHasDom() || !S.host || !S.layout) return false;
  var bar = null;
  for (var i = 0; i < S.layout.bars.length; i++) {
    if (S.layout.bars[i].row_index === row_index) { bar = S.layout.bars[i]; break; }
  }
  if (!bar) return false;
  var box = rcaVizStageBox(S.layout);
  var hostH = S.host.clientHeight || box.h;
  var yPx = bar.y * box.h;
  if (typeof S.host.scrollTo === 'function') {
    try {
      S.host.scrollTo({ top: Math.max(0, yPx - hostH / 2), behavior: 'smooth' });
    } catch (_e) {
      S.host.scrollTop = Math.max(0, yPx - hostH / 2);
    }
  } else {
    S.host.scrollTop = Math.max(0, yPx - hostH / 2);
  }
  if (typeof S.host.scrollIntoView === 'function' && !S.host.scrollTop) {
    try { S.host.scrollIntoView({ block: 'nearest' }); } catch (_e2) { /* older engine */ }
  }
  S.flash = { row_index: bar.row_index, until: Date.now() + 1200 };
  var frames = 0;
  var tick = function () {
    frames += 1;
    if (!S.flash || frames > 14) { S.flash = null; rcaVizDrawHover(); return; }
    rcaVizDrawHover();
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(tick);
    else if (typeof setTimeout === 'function') setTimeout(tick, 80);
    else { S.flash = null; return; }
  };
  tick();
  return true;
}

/**
 * onRowHover(cb) — the ONE hover registry (uPlot keeps hooks in one place for
 * the same reason). cb receives `row_index` or null; returns an unsubscribe
 * function so a caller cannot leak listeners across renders.
 */
function rcaVizOnRowHover(cb) {
  if (typeof cb !== 'function') return function () { return false; };
  var list = RCA_VIZ_STATE.hoverListeners;
  list.push(cb);
  return function unsubscribe() {
    var at = list.indexOf(cb);
    if (at !== -1) list.splice(at, 1);
    return at !== -1;
  };
}

/** Drop every listener + canvas so the host can be reused by another result. */
function rcaVizClear() {
  var S = RCA_VIZ_STATE;
  S.layout = null;
  S.result = null;
  S.focus = null;
  S.hover = null;
  S.flash = null;
  if (!rcaVizHasDom()) return false;
  if (S.barsCv) {
    var c = rcaVizCtx(S.barsCv);
    if (c) c.clearRect(0, 0, S.barsCv.width, S.barsCv.height);
  }
  if (S.hoverCv) {
    var h = rcaVizCtx(S.hoverCv);
    if (h) h.clearRect(0, 0, S.hoverCv.width, S.hoverCv.height);
  }
  if (S.summary && typeof S.summary.textContent !== 'undefined') {
    S.summary.textContent = '';
  }
  return true;
}

function rcaVizDestroy() {
  var S = RCA_VIZ_STATE;
  if (S.hoverCv && typeof S.hoverCv.removeEventListener === 'function') {
    S.hoverCv.removeEventListener('mousemove', rcaVizOnPointerMove);
    S.hoverCv.removeEventListener('mouseleave', rcaVizOnPointerLeave);
    S.hoverCv.removeEventListener('click', rcaVizOnPointerDown);
  }
  S.hoverListeners = [];
  S.wired = false;
  rcaVizClear();
  S.host = null;
  S.stage = null;
  S.img = null;
  S.barsCv = null;
  S.hoverCv = null;
  S.summary = null;
  return true;
}

/** Read-only introspection for app.js wiring and for the browser console. */
function rcaVizGetState() {
  var S = RCA_VIZ_STATE;
  return {
    mounted: !!S.host,
    rows: S.layout ? S.layout.axis.rows : 0,
    placed: S.layout ? S.layout.axis.placed : 0,
    focus: S.focus,
    hover: S.hover,
    kind: S.layout ? S.layout.axis.kind : null,
    listeners: S.hoverListeners.length,
    dpr: S.dpr,
    // Documented v-stretch of the image map (1 === stage matches the image).
    stretch: S.stretch,
    unplaceable: S.layout ? (S.layout.axis.unplaceable || []).length : 0,
  };
}

// ---------------------------------------------------------------------------
// exports — plain globals (no ES module syntax anywhere in this file)
// ---------------------------------------------------------------------------

// The namespace object. `var` (not const) so the vm sandbox used by
// tests_viz.js and the browser's global scope both end up seeing the very same
// instance, and so a re-include of the file cannot double-bind it.
var rcaViz = {
  // lifecycle
  render: rcaVizRender,
  resize: rcaVizResize,
  clear: rcaVizClear,
  destroy: rcaVizDestroy,
  // focus / locate (uPlot semantics; RCA_VIZ_STATE is the only source of truth)
  focusRow: rcaVizFocusRow,
  clearFocus: rcaVizClearFocus,
  locateTo: rcaVizLocateTo,
  getFocus: function () { return RCA_VIZ_STATE.focus; },
  getLayout: function () { return RCA_VIZ_STATE.layout; },
  getState: rcaVizGetState,
  // events
  onRowHover: rcaVizOnRowHover,
  // pure layer, also exposed so the app/table wiring can reuse the maths
  layout: rcaVizLayout,
  focusPass: rcaVizFocusPass,
  hitTest: rcaVizHitTest,
  summary: rcaVizSummary,
  strings: RCA_VIZ_STRINGS,
  defaults: RCA_VIZ_DEFAULTS,
};

// Explicit globalThis/window publication: js/viz.js is a plain script (no ES
// module syntax anywhere), so `var` bindings already reach the global object
// under both the browser and the vm sandbox — this just makes the surface
// obvious and keeps the naming identical in both.
if (typeof window !== 'undefined') {
  window.rcaViz = rcaViz;
  window.rcaVizLayout = rcaVizLayout;
  window.rcaVizFocusPass = rcaVizFocusPass;
  window.rcaVizHitTest = rcaVizHitTest;
  window.rcaVizSummary = rcaVizSummary;
}
if (typeof globalThis !== 'undefined') {
  globalThis.rcaViz = rcaViz;
  globalThis.rcaVizLayout = rcaVizLayout;
  globalThis.rcaVizFocusPass = rcaVizFocusPass;
  globalThis.rcaVizHitTest = rcaVizHitTest;
  globalThis.rcaVizSummary = rcaVizSummary;
  globalThis.rcaVizNormalizePos = rcaVizNormalizePos;
  globalThis.rcaVizBedValue = rcaVizBedValue;
  globalThis.rcaVizStageOf = rcaVizStageOf;
  globalThis.RCA_VIZ_STRINGS = RCA_VIZ_STRINGS;
  globalThis.RCA_VIZ_DEFAULTS = RCA_VIZ_DEFAULTS;
}
