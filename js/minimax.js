// minimax.js - call MiniMax M3 vision API and normalize the result.
// Mirrors RLPE range_chart_extractor.extract_range_chart. Never throws:
// returns { ok, data, error, errorKey, raw, truncated }.
'use strict';

// Downscale an image File to a JPEG/PNG data URL whose long edge <= maxEdge.
// Returns { dataUrl, mime, width, height, resized }.
//
// REVIEW-2026-11-07 (low): optional opts.signal — when aborted, the
// FileReader is stopped and the promise rejects with Error('aborted').
// Before, a superseded file-selection let the FileReader + Image decode
// run to completion in the background (the caller's token check already
// dropped the stale result, but the CPU/memory work was wasted).
function rcaLoadAndMaybeResize(file, maxEdge, opts) {
  opts = opts || {};
  const signal = opts.signal || null;
  return new Promise((resolve, reject) => {
    let settled = false;
    const reader = new FileReader();
    const cleanup = () => { if (signal) signal.removeEventListener('abort', onAbort); };
    const fail = (msg) => { if (!settled) { settled = true; cleanup(); reject(new Error(msg)); } };
    const onAbort = () => {
      if (settled) return;
      settled = true;
      cleanup();
      try { reader.abort(); } catch (_e) { /* ignore */ }
      reject(new Error('aborted'));
    };
    reader.onerror = () => fail('imageRead');
    reader.onabort = () => fail('aborted');
    reader.onload = () => {
      const originalDataUrl = reader.result;
      const img = new Image();
      img.onerror = () => fail('imageRead');
      img.onload = () => {
        if (signal && signal.aborted) { fail('aborted'); return; }
        const w = img.naturalWidth;
        const h = img.naturalHeight;
        const longEdge = Math.max(w, h);
        if (!maxEdge || longEdge <= maxEdge) {
          resolve({
            dataUrl: originalDataUrl,
            mime: file.type || 'image/png',
            width: w,
            height: h,
            resized: false,
          });
          return;
        }
        const scale = maxEdge / longEdge;
        const nw = Math.round(w * scale);
        const nh = Math.round(h * scale);
        const canvas = document.createElement('canvas');
        canvas.width = nw;
        canvas.height = nh;
        const ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        if (opts.enhance) {
          // Front-end image enhancement for thin lines / small text.
          // 1) Supersample: draw into a ~2x temp canvas (capped so memory
          //    stays bounded), then downsample back to the target size. This
          //    recovers smoother detail than a single direct downscale.
          // 2) Light unsharp-mask (3x3 blur) on the final canvas to sharpen
          //    edges without over-exposing or ringing. Without `enhance`
          //    the path below is unchanged.
          const up = 2;
          let uw = Math.round(nw * up);
          let uh = Math.round(nh * up);
          const MAX_INTERMEDIATE = 4096;
          if (uw > MAX_INTERMEDIATE) {
            const r = MAX_INTERMEDIATE / uw; uw = MAX_INTERMEDIATE; uh = Math.round(uh * r);
          }
          if (uh > MAX_INTERMEDIATE) {
            const r = MAX_INTERMEDIATE / uh; uh = MAX_INTERMEDIATE; uw = Math.round(uw * r);
          }
          const tmp = document.createElement('canvas');
          tmp.width = uw; tmp.height = uh;
          const tctx = tmp.getContext('2d');
          tctx.imageSmoothingEnabled = true;
          tctx.imageSmoothingQuality = 'high';
          tctx.drawImage(img, 0, 0, uw, uh);
          ctx.drawImage(tmp, 0, 0, nw, nh);
          rcaUnsharpMask(ctx, nw, nh, 0.4, 1);
        } else {
          ctx.drawImage(img, 0, 0, nw, nh);
        }
        // Prefer lossless PNG for downscaled charts so the small italic
        // species names stay sharp — JPEG re-compression blurs dense chart
        // text and is a known cause of OCR misreads. Only keep JPEG when the
        // source is JPEG and the resized image is large (a lossless PNG would
        // be excessively big).
        const resizedIsLarge = (nw * nh) > (2500 * 2500);
        const outMime = (file.type === 'image/jpeg' && resizedIsLarge) ? 'image/jpeg' : 'image/png';
        const dataUrl = outMime === 'image/jpeg'
          ? canvas.toDataURL('image/jpeg', 0.95)
          : canvas.toDataURL('image/png');
        resolve({ dataUrl, mime: outMime, width: nw, height: nh, resized: true });
      };
      img.src = originalDataUrl;
    };
    if (signal) {
      if (signal.aborted) { onAbort(); return; }
      signal.addEventListener('abort', onAbort);
    }
    reader.readAsDataURL(file);
  });
}

// Light unsharp-mask sharpening applied in-place on a 2D canvas context.
// amount: strength of the high-pass signal added back (0..1, kept low so we
//   don't over-expose or ring). threshold: only sharpen pixels whose
//   blur-delta exceeds this, so flat areas don't get noise amplified.
// Wrapped in try/catch: a tainted or oversized canvas (shouldn't happen for
// a local file) silently skips sharpening rather than throwing.
function rcaUnsharpMask(ctx, w, h, amount, threshold) {
  try {
    if (w * h > 16_000_000) return; // guard against pathological sizes
    const img = ctx.getImageData(0, 0, w, h);
    const data = img.data;
    // 3x3 box blur (cheap Gaussian approximation).
    const blur = new Float32Array(w * h * 3);
    const at = (x, y, c) => {
      const cx = x < 0 ? 0 : (x >= w ? w - 1 : x);
      const cy = y < 0 ? 0 : (y >= h ? h - 1 : y);
      return data[(cy * w + cx) * 4 + c];
    };
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        for (let c = 0; c < 3; c++) {
          let s = 0;
          for (let ky = -1; ky <= 1; ky++)
            for (let kx = -1; kx <= 1; kx++)
              s += at(x + kx, y + ky, c);
          blur[(y * w + x) * 3 + c] = s / 9;
        }
      }
    }
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const oi = (y * w + x) * 4;
        for (let c = 0; c < 3; c++) {
          const orig = data[oi + c];
          const diff = orig - blur[(y * w + x) * 3 + c];
          if (Math.abs(diff) >= threshold) {
            const v = orig + amount * diff;
            data[oi + c] = v < 0 ? 0 : (v > 255 ? 255 : v);
          }
        }
      }
    }
    ctx.putImageData(img, 0, 0);
  } catch (_e) { /* sharpening is best-effort */ }
}

// Split a data URL into { mediaType, base64 }.
function rcaSplitDataUrl(dataUrl) {
  const m = /^data:([^;]+);base64,(.*)$/.exec(dataUrl);
  if (!m) return { mediaType: 'image/png', base64: '' };
  return { mediaType: m[1], base64: m[2] };
}

// H8 parity: copy any unknown keys under `_extras` so the model can emit
// extra context without it being silently dropped.
function rcaCarryExtras(item, known) {
  if (!item || typeof item !== 'object') return null;
  const out = {};
  for (const k of Object.keys(item)) {
    if (!known.includes(k)) out[k] = item[k];
  }
  return Object.keys(out).length ? out : null;
}

// Normalize the parsed JSON into the strict result shape.
// H5 (REVIEW-2026-08-19): Mode-known root keys. When parsed JSON has
// none of these and no _array_root, the model returned a structurally
// foreign payload (e.g. truncated mid-stream, schema swap, hallucinated
// shape) and the result must be flagged as truncated/unrecognized.
// Mirrors rca_core/extractor.py:_MODE_KNOWN_ROOTS.
// Sprint B (REVIEW-2026-09-04): `_extras` removed from the range_chart set
// to mirror rca_core/extractor.py:629-630 RANGE_CHART_ROOTS
// ({"sections","species_ranges","biozones","other_fossils","confidence"}).
// The extra entry made the JS foreign-payload guard accept `_extras`-only
// payloads that Python (correctly) flags as truncated_or_unrecognized.
const KNOWN_ROOTS = {
  range_chart:       new Set(['sections','species_ranges','biozones','other_fossils','confidence']),
  columnar_section:  new Set(['sections','fossil_legend','lithology_legend','cross_beds','confidence','overall_confidence','_extras']),
  abundance_diagram: new Set(['sites','abundances','zones','confidence','_extras']),
  phylogenetic_tree: new Set(['metadata','nodes','root_ids','legend','confidence']),
  // UI-REVIEW-2026-09-05: radiolarian biozonation / correlation charts.
  zonation_chart:    new Set(['zonations','zones','correlations','confidence']),
};

// H5 helper: returns a fresh warnings array with the flag if no known
// root key was found and no _array_root was set. Otherwise returns null.
function rcaTruncatedWarningIfForeign(parsed, mode) {
  if (!parsed || typeof parsed !== 'object') return ['truncated_or_unrecognized_payload'];
  const keys = Object.keys(parsed);
  const hasArrayRoot = Array.isArray(parsed._array_root);
  const roots = KNOWN_ROOTS[mode];
  const matched = roots && keys.some((k) => roots.has(k));
  if (!matched && !hasArrayRoot) {
    return ['truncated_or_unrecognized_payload'];
  }
  return null;
}

// P0-5 (REVIEW-2026-07-25): If the model returned a top-level JSON array
// (already wrapped by json-utils.extractBalancedJsonArray as {_array_root:[...]}),
// distribute items into the correct tables by structural key, mirroring
// Python rca_core/extractor.py:636-661 (the _array_root unwrap in
// normalize_range_chart).
// Sprint B (REVIEW-2026-09-04): classification parity with
// _classify_array_item (extractor.py:344-389) —
//   * bare non-empty strings go to `other_fossils` (extractor.py:640-642);
//   * dicts that classify to no bucket are kept under a top-level
//     `_unclassified` array (extractor.py:648) instead of being silently
//     dropped;
//   * key tests use KEY PRESENCE, not truthiness (Python `"species" in item`),
//     so `{species: ""}` still classifies as a species row.
function rcaUnwrapArrayRoot(parsed) {
  if (!parsed || typeof parsed !== 'object') return parsed;
  if (!Array.isArray(parsed._array_root)) return parsed;
  const dist = {
    sections: [], species_ranges: [], biozones: [], other_fossils: [],
  };
  let unclassified = null;
  for (const item of parsed._array_root) {
    // Non-dict items: bare strings land in other_fossils, everything else
    // is skipped (mirrors extractor.py:639-643).
    if (!item || typeof item !== 'object') {
      if (typeof item === 'string' && item.trim()) {
        dist.other_fossils.push(item.trim());
      }
      continue;
    }
    const key = rcaClassifyArrayItem(item);
    if (key && dist[key]) dist[key].push(item);
    else {
      // Unclassifiable dicts survive under _unclassified so nothing the
      // model emitted is silently dropped (extractor.py:645-648).
      if (!unclassified) unclassified = [];
      unclassified.push(item);
    }
  }
  // Preserve any other top-level fields the wrapper may have carried.
  const out = { ...dist };
  if (unclassified) out._unclassified = unclassified;
  for (const k of Object.keys(parsed)) {
    if (k === '_array_root') continue;
    if (k in out) continue;  // already populated from array items
    out[k] = parsed[k];
  }
  return out;
}

// Lightweight re-implementation of Python _classify_array_item
// (rca_core/extractor.py:344-389) for the JS normalizers. Used only when
// unwrapping _array_root payloads.
// P0-4: explicit zone_type wins over all heuristics.
// Sprint B (REVIEW-2026-09-04): aligned 1:1 with the Python ladder —
// species via key presence, biozone via name+age presence + zone label,
// sections via name + (age_range|formations|age) presence, then a
// name-only fallback. The previous JS ladders (truthiness checks, an
// id/thickness_m section probe, a label/species/taxon other_fossils probe)
// dropped rows Python keeps — e.g. `{name:'S1', age_range:'Cretaceous'}`
// fell through every JS branch and vanished.
function rcaClassifyArrayItem(item) {
  if (!item || typeof item !== 'object') return null;
  // P0-4: explicit zone_type wins over all heuristics.
  const zt = item.zone_type;
  if (typeof zt === 'string') {
    const ztl = zt.trim().toLowerCase();
    if (['biozone', 'zone', 'assemblage_zone', 'interval_zone', 'lineage_zone',
         'acme_zone', 'oppel_zone', 'range_zone', 'subzone', 'zonule'].includes(ztl)) {
      return 'biozones';
    }
    if (['species_range', 'taxon_range', 'fad_lad'].includes(ztl)) {
      return 'species_ranges';
    }
    if (['section', 'measured_section', 'locality'].includes(ztl)) {
      return 'sections';
    }
  }
  // species: "species" in item OR (range_top AND range_base present) —
  // key-existence test, mirroring extractor.py:365-367.
  if ('species' in item || ('range_top' in item && 'range_base' in item)) {
    return 'species_ranges';
  }
  // biozone: requires name + age present AND the name passes the zone-label
  // pattern (extractor.py:368-378, iron-rule regex).
  const biozoneText = String(item.name || item.label || '');
  const isZoneLabel = /\b(zone|zonule|assemblage|oppel|interval|lineage|range|acme)\b/i.test(biozoneText);
  if ('name' in item && 'age' in item && isZoneLabel) {
    return 'biozones';
  }
  // section: name + any age/formations signal (extractor.py:379-385).
  if ('name' in item && ('age_range' in item || 'formations' in item
      || 'age' in item)) {
    return 'sections';
  }
  // Name-only fallback: still a section (extractor.py:386-388).
  if ('name' in item) {
    return 'sections';
  }
  return null;
}

function rcaNormalizeResult(parsed) {
  // P0-5 (REVIEW-2026-07-25): unwrap top-level array wrappers.
  parsed = rcaUnwrapArrayRoot(parsed) || parsed;
  const out = {
    sections: [],
    species_ranges: [],
    biozones: [],
    other_fossils: [],
    confidence: 0,
  };
  // Sprint B (REVIEW-2026-09-04): keep unclassifiable dicts top-level on
  // the result, mirroring rca_core/extractor.py:645-648
  // (out.setdefault("_unclassified", []).append(item)). Excluded from the
  // root _extras carry below so it is not duplicated there.
  if (Array.isArray(parsed._unclassified)) {
    out._unclassified = parsed._unclassified;
  }
  // H5: flag payloads that have no range_chart root keys (and no array
  // wrapper) as truncated/unrecognized. The Python extractor then
  // converts this warning into err.parse in the extract_range_chart path.
  const warn = rcaTruncatedWarningIfForeign(parsed, 'range_chart');
  if (warn) out._warnings = warn;
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const SEC_KNOWN = ['name', 'age_range', 'formations', 'formation_thickness_m', 'coordinates'];
  // P1-6 (REVIEW-2026-07-25): align SP_KNOWN with rca_core/extractor.py
  // _KNOWN_SPECIES_KEYS (13 fields). The previous 5-field list silently
  // dropped author, year, author_year, range_top_bed, range_base_bed,
  // endpoint_kind, occurrence_mode into row._extras as a dict — which
  // would be coerced to "[object Object]" by the JS merge multi-run
  // fallback.
  // M-1 fix: replaced 'reworked' (boolean) with 'occurrence_mode' (string enum)
  // to match rca_core/extractor.py:455 and the updated Python prompt.
  const SP_KNOWN = [
    'species', 'section', 'range_top', 'range_base', 'biozone',
    'author', 'year', 'author_year',
    'range_top_bed', 'range_base_bed',
    'range_top_idx', 'range_base_idx',
    'endpoint_kind', 'occurrence_mode', 'reworked', 'confidence', 'note',
  ];
  // P1-5 parity: enum values for occurrence_mode (must match
  // rca_core/extractor.py:92-100 VALID_OCCURRENCE_MODES).
  const VALID_OCCURRENCE_MODES = new Set([
    'unknown', 'in_situ', 'reworked', 'transported', 'cavity_fill',
    'bioturbated', 'derived', 'lag_deposit',
  ]);
  const VALID_ENDPOINT_KINDS = new Set([
    'unknown', 'observed', 'projected', 'truncated',
  ]);
  const asOptionalInt = (v) => {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    const n = Number(v);
    return Number.isInteger(n) ? n : null;
  };
  const asOptionalConfidence = (v) => {
    if (v === null || v === undefined || v === '' || typeof v === 'boolean') return null;
    const n = Number(v);
    return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : null;
  };

  function _normalizeOccurrenceMode(sp) {
    const raw = sp && sp.occurrence_mode;
    if (typeof raw === 'string') {
      const normalized = raw.trim().toLowerCase();
      if (VALID_OCCURRENCE_MODES.has(normalized)) return normalized;
    }
    if (sp && typeof sp.reworked === 'boolean') {
      return sp.reworked ? 'reworked' : 'in_situ';
    }
    return 'unknown';
  }
  function _normalizeEndpointKind(value) {
    if (typeof value === 'string') {
      const normalized = value.trim().toLowerCase();
      if (VALID_ENDPOINT_KINDS.has(normalized)) return normalized;
    }
    return 'unknown';
  }
  // PARITY (extractor.py:250): biozones row includes `section` so the
  // aggregation layer can fold section into the row label and prevent two
  // biozones with the same name in different sections from collapsing into
  // a single merged row. Without this, browser runs lose the section axis
  // for biozones and exported tables diverge from the Python export shape.
  const BZ_KNOWN = ['name', 'section', 'age', 'thickness_m', 'zone_type'];
  const ROOT_KNOWN = ['sections', 'species_ranges', 'biozones', 'other_fossils', 'confidence'];

  for (const sec of Array.isArray(parsed.sections) ? parsed.sections : []) {
    if (!sec || typeof sec !== 'object') continue;
    const row = {
      name: asStr(sec.name),
      age_range: asStr(sec.age_range),
      formations: Array.isArray(sec.formations) ? sec.formations.map(asStr) : [],
      formation_thickness_m: asStr(sec.formation_thickness_m),
      coordinates: asStr(sec.coordinates),
    };
    const extras = rcaCarryExtras(sec, SEC_KNOWN);
    if (extras) row._extras = extras;
    out.sections.push(row);
  }
  for (const sp of Array.isArray(parsed.species_ranges) ? parsed.species_ranges : []) {
    if (!sp || typeof sp !== 'object') continue;
    const row = {
      species: asStr(sp.species),
      section: asStr(sp.section),
      range_top: asStr(sp.range_top),
      range_base: asStr(sp.range_base),
      biozone: asStr(sp.biozone),
      // P1-6 (REVIEW-2026-07-25): promote the 8 long-standing prompt
      // fields to first-class row keys so the JS↔Python parity test,
      // merge function, and exported CSV/JSON all see them.
      author: asStr(sp.author),
      year: asStr(sp.year),
      author_year: asStr(sp.author_year),
      range_top_bed: asStr(sp.range_top_bed),
      range_base_bed: asStr(sp.range_base_bed),
      range_top_idx: asOptionalInt(sp.range_top_idx),
      range_base_idx: asOptionalInt(sp.range_base_idx),
      endpoint_kind: _normalizeEndpointKind(sp.endpoint_kind),
      occurrence_mode: _normalizeOccurrenceMode(sp),
      confidence: asOptionalConfidence(sp.confidence),
      // P1-5 / H-8: capture the per-row uncertainty note.
      note: asStr(sp.note || ''),
    };
    const extras = rcaCarryExtras(sp, SP_KNOWN);
    if (extras) row._extras = extras;
    // P0-4: defensive — if species name looks like a zone, flag & strip.
    const spName = row.species;
    if (spName && /\b(zone|zonule|assemblage|oppel|interval|lineage|range|acme)\b/i.test(spName)) {
      row.note = ((row.note || '') + ' [zone-mislabel-warning]').trim();
    }
    out.species_ranges.push(row);
  }
  for (const bz of Array.isArray(parsed.biozones) ? parsed.biozones : []) {
    if (!bz || typeof bz !== 'object') continue;
    // P0-4: infer zone_type from name keywords when not explicitly provided.
    const bzName = asStr(bz.name).trim();
    let inferredZt = 'biozone';
    const bzNameLower = bzName.toLowerCase();
    if (bzNameLower.includes('assemblage') || bzNameLower.includes('ass.')) {
      inferredZt = 'assemblage_zone';
    } else if (bzNameLower.includes('acme')) {
      inferredZt = 'acme_zone';
    } else if (bzNameLower.includes('lineage')) {
      inferredZt = 'lineage_zone';
    } else if (/\bzonule\b/.test(bzNameLower)) {
      inferredZt = 'zonule';
    } else if (/\bsubzone\b/.test(bzNameLower)) {
      inferredZt = 'subzone';
    } else if (bzNameLower.includes('oppel')) {
      inferredZt = 'oppel_zone';
    } else if (bzNameLower.includes('interval')) {
      inferredZt = 'interval_zone';
    } else if (bzNameLower.includes('range zone') || bzNameLower.includes('taxon-range')) {
      inferredZt = 'range_zone';
    }
    const row = {
      name: bzName,
      // PARITY: section is a top-level field on the biozone row, mirroring
      // rca_core/extractor.py:371. Demoting it to _extras would hide it
      // from rcaAggNorm and break the section-folding dedup in aggregate.js.
      section: asStr(bz.section),
      age: asStr(bz.age),
      thickness_m: asStr(bz.thickness_m),
      // P0-4: enforce zone_type field.
      zone_type: asStr(bz.zone_type) || inferredZt,
    };
    const extras = rcaCarryExtras(bz, BZ_KNOWN);
    if (extras) row._extras = extras;
    out.biozones.push(row);
  }
  // M1 (REVIEW-2026-08-19): other_fossils may be a string OR a dict
  // shape (label/species/taxon). The previous asStr map silently turned
  // dicts into '[object Object]'. Lift the first available label so
  // researchers see the actual fossil name in the export.
  if (Array.isArray(parsed.other_fossils)) {
    out.other_fossils = parsed.other_fossils.map((item) => {
      if (item == null) return '';
      if (typeof item === 'string') return item.trim();
      if (typeof item === 'object') {
        return String(item.label || item.species || item.taxon || item.name || '').trim();
      }
      return '';
    }).filter(Boolean);
  }
  const conf = Number(parsed.confidence);
  out.confidence = Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0;
  const rootExtras = rcaCarryExtras(parsed || {}, ROOT_KNOWN.concat(['_unclassified']));
  if (rootExtras) out._extras = rootExtras;
  return out;
}


// Normalize the parsed columnar-section JSON into the strict result shape.
// Mirrors rca_core.extractor.normalize_columnar_result (deep nested
// normalization + _extras carry + confidence fallback to top-level
// `confidence` when `overall_confidence` is absent).
function rcaNormalizeColumnarResult(parsed) {
  // P0-5 (REVIEW-2026-07-25): unwrap top-level array wrappers. For columnar
  // we distribute only items whose structural keys look like section rows
  // (id/group/lithology_blocks/...) into sections; other items are merged
  // into the appropriate legend/cross_beds buckets.
  if (parsed && Array.isArray(parsed._array_root)) {
    const items = parsed._array_root;
    const dist = {
      sections: [], fossil_legend: [], lithology_legend: [], cross_beds: [],
    };
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      if (item.id || item.group || item.lithology_blocks || item.age_units || item.samples) {
        dist.sections.push(item);
      } else if (item.fossil || item.marker) {
        dist.fossil_legend.push(item);
      } else if (item.pattern) {
        dist.lithology_legend.push(item);
      } else if (item.from_section || item.to_section) {
        dist.cross_beds.push(item);
      }
    }
    const original = arguments[0] || {};
    parsed = { ...dist };
    // Walk the ORIGINAL (pre-rewrite) payload, not the freshly-built `dist`
    // copy, so extra top-level fields that aren't columnar buckets get
    // carried through instead of being silently dropped.
    for (const k of Object.keys(original)) {
      if (k === '_array_root' || k in dist) continue;
      parsed[k] = original[k];
    }
  }
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const asInt = (v) => {
    if (v === null || v === undefined || v === '') return null;
    const n = parseInt(v, 10);
    return Number.isFinite(n) ? n : null;
  };
  const normList = (key) => (Array.isArray(parsed[key]) ? parsed[key] : []);
  const BLOCK_KNOWN = ['pattern', 'range_top_idx', 'range_base_idx'];
  const UNIT_KNOWN = ['label', 'range_top_idx', 'range_base_idx'];
  const SAMPLE_KNOWN = ['bed_idx', 'fossil_marker', 'ref'];
  const LEGEND_KNOWN = ['marker', 'pattern', 'meaning'];
  const CROSS_KNOWN = ['from_section', 'from_bed_idx', 'to_section', 'to_bed_idx'];
  const SECTION_KNOWN = ['id', 'group', 'lithology_blocks', 'age_units', 'samples',
                         'coordinates_text', 'thickness_m', 'confidence_by_section'];

  const normBlocks = (items) => items.filter((x) => x && typeof x === 'object').map((b) => {
    const row = {
      pattern: asStr(b.pattern),
      range_top_idx: asInt(b.range_top_idx),
      range_base_idx: asInt(b.range_base_idx),
    };
    const ex = rcaCarryExtras(b, BLOCK_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normUnits = (items) => items.filter((x) => x && typeof x === 'object').map((u) => {
    const row = {
      label: asStr(u.label),
      range_top_idx: asInt(u.range_top_idx),
      range_base_idx: asInt(u.range_base_idx),
    };
    const ex = rcaCarryExtras(u, UNIT_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normSamples = (items) => items.filter((x) => x && typeof x === 'object').map((s) => {
    const row = {
      bed_idx: asInt(s.bed_idx),
      fossil_marker: asStr(s.fossil_marker),
      ref: asStr(s.ref),
    };
    const ex = rcaCarryExtras(s, SAMPLE_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normLegend = (items) => items.filter((x) => x && typeof x === 'object').map((x) => {
    const row = {
      marker: asStr(x.marker),
      pattern: asStr(x.pattern),
      meaning: asStr(x.meaning),
    };
    const ex = rcaCarryExtras(x, LEGEND_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });
  const normCross = (items) => items.filter((x) => x && typeof x === 'object').map((x) => {
    const row = {
      from_section: asStr(x.from_section),
      from_bed_idx: asInt(x.from_bed_idx),
      to_section: asStr(x.to_section),
      to_bed_idx: asInt(x.to_bed_idx),
    };
    const ex = rcaCarryExtras(x, CROSS_KNOWN);
    if (ex) row._extras = ex;
    return row;
  });

  const sections = [];
  for (const sec of normList('sections')) {
    if (!sec || typeof sec !== 'object') continue;
    const confSec = Number(sec.confidence_by_section);
    const row = {
      id: asStr(sec.id),
      group: asStr(sec.group),
      lithology_blocks: normBlocks(sec.lithology_blocks || []),
      age_units: normUnits(sec.age_units || []),
      samples: normSamples(sec.samples || []),
      coordinates_text: asStr(sec.coordinates_text),
      thickness_m: asStr(sec.thickness_m),
      confidence_by_section: Number.isFinite(confSec) ? Math.max(0, Math.min(1, confSec)) : 0,
    };
    const ex = rcaCarryExtras(sec, SECTION_KNOWN);
    if (ex) row._extras = ex;
    sections.push(row);
  }
  // Some models emit root `confidence` instead of `overall_confidence`;
  // fall back so the value isn't silently zeroed.
  const overall = Number(parsed.overall_confidence != null ? parsed.overall_confidence : parsed.confidence);
  const ROOT_KNOWN = ['sections', 'fossil_legend', 'lithology_legend', 'cross_beds',
                     'overall_confidence', 'confidence'];
  // H5: flag foreign payloads as truncated/unrecognized. Checked BEFORE
  // building `out` so we can attach the warnings array on the same object.
  const warnCol = rcaTruncatedWarningIfForeign(parsed, 'columnar_section');
  const out = {
    sections,
    fossil_legend: normLegend(normList('fossil_legend')),
    lithology_legend: normLegend(normList('lithology_legend')),
    cross_beds: normCross(normList('cross_beds')),
    confidence: Number.isFinite(overall) ? Math.max(0, Math.min(1, overall)) : 0,
  };
  const rootEx = rcaCarryExtras(parsed || {}, ROOT_KNOWN);
  if (rootEx) out._extras = rootEx;
  if (warnCol) out._warnings = warnCol;
  return out;
}

// Normalize the parsed abundance-diagram JSON into the strict result shape.
// Mirrors rca_core.extractor.normalize_abundance_result (with _extras carry).
function rcaNormalizeAbundanceResult(parsed) {
  // P0-5 (REVIEW-2026-07-25): unwrap top-level array wrappers.
  // Sprint B (REVIEW-2026-09-04): the old `parsed = { ...dist }` replaced
  // the payload wholesale, dropping top-level `confidence` (and every other
  // non-bucket field such as `_note`) whenever the model emitted a bare
  // array. Mirror the columnar unwrap above and Python
  // rca_core/extractor.py:1296-1310, which mutates `parsed` in place with
  // setdefault so `confidence` survives and unclassifiable dicts are kept
  // under `_unclassified` (they flow into the root `_extras` carry at the
  // bottom of this function, exactly like Python's top_extras).
  if (parsed && Array.isArray(parsed._array_root)) {
    const items = parsed._array_root;
    const dist = { sites: [], abundances: [], zones: [] };
    let unclassified = null;
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      if (item.site_id || item.site_name || item.location) {
        dist.sites.push(item);
      } else if (item.abundance || item.count || item.percentage) {
        dist.abundances.push(item);
      } else if (item.zone || item.assemblage) {
        dist.zones.push(item);
      } else {
        if (!unclassified) unclassified = [];
        unclassified.push(item);
      }
    }
    const original = parsed || {};
    parsed = { ...dist };
    // Walk the ORIGINAL (pre-rewrite) payload, not the freshly-built `dist`
    // copy, so top-level fields that aren't abundance buckets — most
    // importantly `confidence` — get carried through instead of being
    // silently dropped.
    for (const k of Object.keys(original)) {
      if (k === '_array_root' || k in parsed) continue;
      parsed[k] = original[k];
    }
    if (unclassified) parsed._unclassified = unclassified;
  }
  // original body follows
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const normList = (key) => (Array.isArray(parsed[key]) ? parsed[key] : []);
  const SITE_KNOWN = ['name', 'location', 'age_range', 'depth_unit'];
  const AB_KNOWN = ['taxon', 'site', 'level', 'depth', 'abundance', 'abundance_unit'];
  const ZONE_KNOWN = ['name', 'age', 'level_range'];
  const ROOT_KNOWN = ['sites', 'abundances', 'zones', 'confidence'];

  const sites = [];
  for (const s of normList('sites')) {
    if (!s || typeof s !== 'object') continue;
    const row = {
      name: asStr(s.name),
      location: asStr(s.location),
      age_range: asStr(s.age_range),
      depth_unit: asStr(s.depth_unit),
    };
    const ex = rcaCarryExtras(s, SITE_KNOWN);
    if (ex) row._extras = ex;
    sites.push(row);
  }
  const abundances = [];
  for (const a of normList('abundances')) {
    if (!a || typeof a !== 'object') continue;
    const row = {
      taxon: asStr(a.taxon),
      site: asStr(a.site),
      level: asStr(a.level),
      depth: asStr(a.depth),
      abundance: asStr(a.abundance),
      abundance_unit: asStr(a.abundance_unit),
    };
    const ex = rcaCarryExtras(a, AB_KNOWN);
    if (ex) row._extras = ex;
    abundances.push(row);
  }
  const zones = [];
  for (const z of normList('zones')) {
    if (!z || typeof z !== 'object') continue;
    const row = {
      name: asStr(z.name),
      age: asStr(z.age),
      level_range: asStr(z.level_range),
    };
    const ex = rcaCarryExtras(z, ZONE_KNOWN);
    if (ex) row._extras = ex;
    zones.push(row);
  }
  const conf = Number(parsed.confidence);
  // H5: flag foreign payloads as truncated/unrecognized.
  const warnAbd = rcaTruncatedWarningIfForeign(parsed, 'abundance_diagram');
  const out = {
    sites,
    abundances,
    zones,
    confidence: Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0,
  };
  const rootEx = rcaCarryExtras(parsed || {}, ROOT_KNOWN);
  if (rootEx) out._extras = rootEx;
  if (warnAbd) out._warnings = warnAbd;
  return out;
}

// P0-3: phylogenetic tree normalizer — mirrors Python
// rca_core.extractor._normalize_phylogenetic_tree_into().
function rcaUnwrapArrayRootPhylo(parsed) {
  if (!parsed || typeof parsed !== 'object') return parsed;
  if (!Array.isArray(parsed._array_root)) return parsed;
  for (const item of parsed._array_root) {
    if (item && typeof item === 'object' && Array.isArray(item.nodes)) {
      return item;
    }
  }
  return parsed;
}

// UI-REVIEW-2026-09-05: zonation / correlation chart normalizer —
// mirror of rca_core/extractor.py:normalize_zonation_chart_result. All
// rows string-typed; H8 extras per row under `_extras`; `_array_root`
// rescue distributes to zonations / zones / correlations with an
// `_unclassified` carry for anything else.
function rcaNormalizeZonationChartResult(parsed) {
if (parsed && Array.isArray(parsed._array_root)) {
  const items = parsed._array_root;
  const dist = { zonations: [], zones: [], correlations: [] };
  let unclassified = null;
  for (const item of items) {
    if (!item || typeof item !== 'object') continue;
    if ('from_zone' in item || 'to_zone' in item) {
      dist.correlations.push(item);
    } else if ('name' in item &&
               ('rank' in item || 'zonation' in item || 'age_span' in item ||
                'defined_by' in item || 'base_age' in item)) {
      dist.zones.push(item);
    } else if ('name' in item &&
               ('region' in item || 'framework' in item || 'reference' in item)) {
      dist.zonations.push(item);
    } else {
      if (!unclassified) unclassified = [];
      unclassified.push(item);
    }
  }
  const original = parsed || {};
  parsed = { ...dist };
  for (const k of Object.keys(original)) {
    if (k === '_array_root' || k in parsed) continue;
    parsed[k] = original[k];
  }
  if (unclassified) parsed._unclassified = unclassified;
}
const asStr = (v) => (v === null || v === undefined ? '' : String(v));
const normList = (key) => (Array.isArray(parsed[key]) ? parsed[key] : []);
const out = { zonations: [], zones: [], correlations: [], confidence: 0 };
const ZONATIONS_KNOWN = ['name', 'region', 'framework', 'reference'];
const ZONE_KNOWN = ['name', 'zonation', 'rank', 'age_span', 'base_age',
                    'top_age', 'stage', 'defined_by', 'note'];
const CORR_KNOWN = ['from_zone', 'to_zone', 'from_zonation',
                    'to_zonation', 'basis', 'note'];
const carry = (item, known, row) => {
  for (const k of Object.keys(item)) {
    if (known.indexOf(k) === -1) {
      if (!row._extras) row._extras = {};
      row._extras[k] = item[k];
    }
  }
};
for (const z of normList('zonations')) {
  if (!z || typeof z !== 'object') continue;
  const row = { name: asStr(z.name), region: asStr(z.region),
                framework: asStr(z.framework), reference: asStr(z.reference) };
  carry(z, ZONATIONS_KNOWN, row);
  out.zonations.push(row);
}
for (const z of normList('zones')) {
  if (!z || typeof z !== 'object') continue;
  const row = { name: asStr(z.name), zonation: asStr(z.zonation),
                rank: asStr(z.rank), age_span: asStr(z.age_span),
                base_age: asStr(z.base_age), top_age: asStr(z.top_age),
                stage: asStr(z.stage), defined_by: asStr(z.defined_by),
                note: asStr(z.note) };
  carry(z, ZONE_KNOWN, row);
  out.zones.push(row);
}
for (const c of normList('correlations')) {
  if (!c || typeof c !== 'object') continue;
  const row = { from_zone: asStr(c.from_zone), to_zone: asStr(c.to_zone),
                from_zonation: asStr(c.from_zonation),
                to_zonation: asStr(c.to_zonation),
                basis: asStr(c.basis), note: asStr(c.note) };
  carry(c, CORR_KNOWN, row);
  out.correlations.push(row);
}
const cf = parseFloat(parsed.confidence);
out.confidence = Number.isFinite(cf) ? Math.max(0, Math.min(1, cf)) : 0;
const knownTop = ['zonations', 'zones', 'correlations', 'confidence',
                  '_array_root', '_unclassified'];
const extras = {};
let hasExtras = false;
for (const k of Object.keys(parsed)) {
  if (knownTop.indexOf(k) === -1) { extras[k] = parsed[k]; hasExtras = true; }
}
if (parsed._unclassified) { extras._unclassified = parsed._unclassified; hasExtras = true; }
if (hasExtras) out._extras = extras;
return out;
}

function rcaNormalizePhylogeneticTreeResult(parsed) {
  parsed = rcaUnwrapArrayRootPhylo(parsed);
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const asFloat = (v) => {
    if (v === null || v === undefined) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  const nodesIn = Array.isArray(parsed.nodes) ? parsed.nodes : [];
  // Build id→node lookup and children counts.
  const idToNode = {};
  const childrenCount = {};
  for (const n of nodesIn) {
    if (!n || typeof n !== 'object') continue;
    const nid = String(n.id || '');
    if (nid) {
      idToNode[nid] = n;
      childrenCount[nid] = 0;
    }
  }
  for (const n of nodesIn) {
    if (!n || typeof n !== 'object') continue;
    const parent = n.parent;
    if (parent != null) {
      const pid = String(parent);
      if (pid in childrenCount) childrenCount[pid] = (childrenCount[pid] || 0) + 1;
    }
  }

  const rootIdsRaw = parsed.root_ids || [];
  if (!rootIdsRaw.length) throw new Error('root_ids is empty');
  for (const rid of rootIdsRaw) {
    if (!(String(rid) in idToNode)) throw new Error('root_ids contains unknown node id: ' + rid);
  }

  const nodesOut = [];
  for (const n of nodesIn) {
    if (!n || typeof n !== 'object') continue;
    const nid = String(n.id || '');
    if (!nid) continue;

    const parentVal = n.parent;
    if (rootIdsRaw.indexOf(nid) === -1) {
      // Non-root
      if (parentVal == null) throw new Error('Non-root node ' + nid + ' must have a parent');
      if (!(String(parentVal) in idToNode)) throw new Error('Node ' + nid + ' references parent not in node ids');
    } else {
      // Root
      if (parentVal != null) throw new Error('Root node ' + nid + ' must have parent == null');
    }

    const isLeafInput = Boolean(n.is_leaf);
    const actualIsLeaf = (childrenCount[nid] || 0) === 0;
    const isLeaf = isLeafInput !== actualIsLeaf ? actualIsLeaf : isLeafInput;

    const supportRaw = n.support;
    const support = asFloat(supportRaw);
    if (support !== null && (support < 0 || support > 100)) {
      throw new Error('support must be in [0, 100] or null, got ' + support);
    }

    const row = {
      id: nid,
      parent: parentVal != null ? String(parentVal) : null,
      name: asStr(n.name),
      is_leaf: isLeaf,
      branch_length: asFloat(n.branch_length),
      node_age_ma: asFloat(n.node_age_ma),
      support: support,
    };
    // Carry extras (depth_range_m, sequence_count, support_confidence, etc.)
    const KNOWN = ['id', 'parent', 'name', 'is_leaf', 'branch_length', 'node_age_ma', 'support'];
    const extras = {};
    let hasExtras = false;
    for (const k of Object.keys(n)) {
      if (!KNOWN.includes(k)) {
        extras[k] = n[k];
        hasExtras = true;
      }
    }
    if (hasExtras) row.metadata = extras;
    nodesOut.push(row);
  }

  const metaRaw = parsed.metadata || {};
  const legendRaw = parsed.legend;
  const conf = Number(parsed.confidence);
  // H5: flag foreign payloads. Phylo schema requires nodes+root_ids; if
  // those were missing we already threw above. We still set the warning
  // here so the surface matches the other 3 normalizers.
  const warnPhylo = rcaTruncatedWarningIfForeign(parsed, 'phylogenetic_tree');
  // H6 (REVIEW-2026-08-19): carry extra taxonomy/version metadata from
  // either parsed.metadata or the root-level. Mirror rca_core/extractor.py
  // _normalize_phylogenetic_tree_into metadata block.
  const lift = (k) => (metaRaw[k] != null ? metaRaw[k]
                       : (parsed[k] != null ? parsed[k] : null));
  return {
    metadata: {
      title: asStr(metaRaw.title || ''),
      extraction_timestamp: asStr(metaRaw.extraction_timestamp || ''),
      tree_type: asStr(metaRaw.tree_type || ''),
      scale: asStr(metaRaw.scale || ''),
      rooted: Boolean(metaRaw.rooted !== false),
      source: asStr(metaRaw.source || metaRaw.image_source || parsed.image_source || ''),
      // New carries (mirror rca_core/extractor.py):
      version: asStr(lift('version') || '1'),
      taxon_group: asStr(lift('taxon_group') || ''),
      root_name: asStr(lift('root_name') || ''),
      total_nodes: (function () {
        const n = Number(lift('total_nodes'));
        return Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : null;
      })(),
      image_source: asStr(lift('image_source') || ''),
    },
    root_ids: rootIdsRaw.map(String),
    nodes: nodesOut,
    legend: (legendRaw && typeof legendRaw === 'object') ? legendRaw : {},
    confidence: Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0,
    ...(warnPhylo ? { _warnings: warnPhylo } : {}),
  };
}

// Build a Newick string from a normalized phylogenetic tree.
// Mirrors Python rca_core.extractor.to_newick().
function rcaBuildNewickNode(nodeId, idToChildren, nodesDict) {
  const children = idToChildren[nodeId] || [];
  if (!children.length) {
    const n = nodesDict[nodeId] || {};
    const name = n.name || '';
    const bl = n.branch_length;
    const blStr = bl != null ? ':' + bl : '';
    const safeName = name.replace(/\(/g, '_').replace(/\)/g, '_').replace(/:/g, '_');
    return safeName + blStr;
  } else {
    const childParts = children.map((cid) => rcaBuildNewickNode(cid, idToChildren, nodesDict));
    const n = nodesDict[nodeId] || {};
    const support = n.support;
    const supportStr = support != null ? String(support) : '';
    const bl = n.branch_length;
    const blStr = bl != null ? ':' + bl : '';
    return '(' + childParts.join(',') + ')' + supportStr + blStr;
  }
}

function rcaToNewick(tree) {
  const nodes = Array.isArray(tree.nodes) ? tree.nodes : [];
  const rootIds = Array.isArray(tree.root_ids) ? tree.root_ids : [];

  const nodesDict = {};
  for (const n of nodes) {
    if (n && typeof n === 'object') {
      const nid = String(n.id || '');
      if (nid) nodesDict[nid] = n;
    }
  }

  const idToChildren = {};
  for (const rid of rootIds) idToChildren[String(rid)] = [];
  for (const n of nodes) {
    if (!n || typeof n !== 'object') continue;
    const pid = n.parent;
    if (pid != null) {
      const pidStr = String(pid);
      if (!(pidStr in idToChildren)) idToChildren[pidStr] = [];
      idToChildren[pidStr].push(String(n.id || ''));
    }
  }

  const parts = rootIds.map((rid) => rcaBuildNewickNode(String(rid), idToChildren, nodesDict));
  return '(' + parts.join(',') + ');';
}

// Backend mode: POST to the same-origin Python server, which performs the
// MiniMax call server-side and returns an already-normalized result.
//
// FIXES applied:
// 1. JSON parse failure now captures HTTP status and response text
// 2. CSRF fetch failure shows better error (CSRF_FETCH_FAILED)
// 3. Concurrent requests are serialized via a pending queue to prevent token races
async function rcaCallBackend(opts, base64) {
  let resp;
  const controller = new AbortController();
  // Allow extra time when the server runs the extraction multiple times.
  const runs = Math.max(1, Math.min(parseInt(opts.runs, 10) || 1, 5));
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs * runs + 5000);
  // FIX-6: honor a caller-supplied cancel signal (user pressed Cancel).
  const onExtAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener('abort', onExtAbort);
  }
  // Sprint B (REVIEW-2026-09-04): single cleanup path for the timer and the
  // external-abort listener. Previously only the happy path and the two
  // fetch-catch paths cleared them — the three early-error returns (aborted
  // CSRF GET, CSRF fetch failure, token-acquisition error object) leaked
  // both: the timer stayed armed and would abort an unrelated future
  // controller, and the listener kept the caller's AbortSignal alive.
  const cleanup = () => {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
  };
  try {
    // Serialize concurrent extractions through a shared pending-promise chain so
    // a fast user can fire two extractions without the second one's CSRF GET
    // racing the first's POST (the second will simply queue and execute after).
    // The promise chain (`_pending`) is reused across calls so we don't race
    // token updates when multiple extractions run concurrently.
    if (!rcaCallBackend._pending) rcaCallBackend._pending = Promise.resolve();
    const myRequest = rcaCallBackend._pending.then(async () => {
      // Fetch CSRF token before POST. Uses a persistent session token stored
      // in memory so subsequent requests reuse the same session.
      // FIX: pass `signal: controller.signal` so a user cancel or the run-aware
      // timeout fires for the CSRF GET too.
      let sessionToken = rcaCallBackend._sessionToken || '';
      let csrfFetchFailed = false;
      let csrfErrorBody = '';
      try {
        const csrfResp = await fetch('/api/extract', {
          method: 'GET',
          headers: { 'X-Session-Token': sessionToken },
          signal: controller.signal,
        });
        if (csrfResp.ok) {
          const csrfData = await csrfResp.json();
          // Update stored tokens atomically after successful fetch
          rcaCallBackend._sessionToken = csrfData.session_token;
          sessionToken = csrfData.session_token;
          rcaCallBackend._csrfToken = csrfData.csrf_token;
        } else {
          // CSRF fetch returned non-OK - capture the error for better diagnostics
          csrfFetchFailed = true;
          try {
            const errText = await csrfResp.text();
            csrfErrorBody = errText.substring(0, 500);
          } catch (_e) { /* ignore */ }
        }
      } catch (_e) {
        // Network error on CSRF fetch - capture for better error message
        csrfFetchFailed = true;
        csrfErrorBody = _e && _e.message ? _e.message : 'network error';
        // REVIEW-2026-11-07 (low): an ABORTED CSRF GET (user pressed Cancel
        // mid-flight) is not a fetch failure. Without this the catch above
        // fell through to the err.csrfFetch branch below and the UI showed
        // "CSRF token failed" for what was really a user cancel.
        if (_e && _e.name === 'AbortError' && controller.signal.aborted) {
          return {
            ok: false,
            errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout',
            status: null,
            errorBody: 'CSRF fetch aborted',
          };
        }
      }

      // If CSRF fetch failed and we have no valid token, return error early
      if (csrfFetchFailed && !rcaCallBackend._csrfToken) {
        return {
          ok: false,
          errorKey: 'err.csrfFetch',
          status: null,
          errorBody: 'Failed to obtain CSRF token: ' + csrfErrorBody,
        };
      }

      const csrfToken = rcaCallBackend._csrfToken || '';
      return { sessionToken, csrfToken };
    });

    // Wait for token acquisition (and any prior request) to complete
    let tokenResult;
    try {
      tokenResult = await myRequest;
    } catch (_e) {
      if (_e && _e.name === 'AbortError') {
        return { ok: false, errorKey: opts.signal && opts.signal.aborted ? 'err.cancelled' : 'err.timeout' };
      }
      return { ok: false, errorKey: 'err.network', errorBody: String(_e) };
    }

    // If token acquisition returned an error object, propagate it
    if (tokenResult && tokenResult.errorKey) {
      return tokenResult;
    }

    const { sessionToken, csrfToken } = tokenResult;

    try {
      resp = await fetch('/api/extract', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-CSRF-Token': csrfToken,
          'X-Session-Token': sessionToken,
        },
        body: JSON.stringify({
          api_key: opts.apiKey,
          image_b64: base64,
          media_type: opts.mediaType || 'image/png',
          caption: opts.caption || '',
          chart_lang: opts.chartLang || 'auto',
          endpoint: opts.baseUrl,
          model: opts.model,
          max_tokens: opts.maxTokens,
          mode: opts.mode || 'range_chart',
          runs: runs,
          force_rerun: !!opts.force_rerun,
          enhance: opts.enhance === true,
        }),
        signal: controller.signal,
      });
    } catch (err) {
      if (err && err.name === 'AbortError') {
        return { ok: false, errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout' };
      }
      return { ok: false, errorKey: 'err.network', errorBody: String(err) };
    }

    // FIX 1: JSON parse failure now captures HTTP status and response text
    let payload;
    let parseErrorBody = '';
    let parseErrorStatus = resp.status;
    try {
      payload = await resp.json();
    } catch (_e) {
      // Try to capture the response text for better error diagnostics
      try {
        const rawText = await resp.text();
        parseErrorBody = rawText.substring(0, 500);
      } catch (_e2) {
        parseErrorBody = 'could not read response body';
      }
      return {
        ok: false,
        errorKey: 'err.parse',
        status: parseErrorStatus,
        errorBody: parseErrorBody,
        raw: parseErrorBody,
      };
    }

    // FIX 2: Check for server-side CSRF/auth errors and auto-recover by clearing tokens.
    // REVIEW-2026-11-07 (low): naming note — 'err.forbidden' is the BACKEND
    // mode's 403 (same-origin CSRF/origin rejection, token-clearable), while
    // 'err.403' (set in the direct-mode !resp.ok branch below) is a raw
    // upstream 403 that needs no token handling. Both are intentionally
    // distinct keys; the similar names are historical, don't merge them.
    if (payload.error_key === 'err.forbidden' && payload.error_body && payload.error_body.includes('CSRF')) {
      // CSRF token was invalid/expired - clear cached tokens so next request fetches fresh ones
      rcaCallBackend._csrfToken = null;
      rcaCallBackend._sessionToken = null;
    }

    // The server mirrors the ExtractResult shape with snake_case keys.
    return {
      ok: !!payload.ok,
      data: payload.data,
      errorKey: payload.error_key,
      status: payload.status,
      raw: payload.raw || '',
      truncated: !!payload.truncated,
      errorBody: payload.error_body || '',
      partialFailures: payload.partial_failures || 0,
      usage: payload.usage || null,
      latencyMs: payload.latency_ms || 0,
      warning: payload.warning || '',
    };
  } finally {
    cleanup();
  }
}

// Main entry. opts: { apiKey, baseUrl, model, maxTokens, proxyUrl, mode,
// dataUrl, mediaType, caption, chartLang }.
// mode defaults to 'range_chart'; 'columnar_section' switches prompt and
// normalizer to the columnar-section variants.
// UI-REVIEW-2026-09-07: vision chart-type classification (auto mode).
// Mirror of rca_core/extractor.py:normalize_chart_classification —
// unknown / missing chart_type degrades to "unknown" instead of raising.
function rcaNormalizeChartClassification(parsed) {
  const KNOWN = ['range_chart', 'columnar_section', 'abundance_diagram',
                 'phylogenetic_tree', 'zonation_chart',
                 'chemical_stratigraphy', 'paleomap', 'scatter_plot'];
  if (!parsed || typeof parsed !== 'object') {
    return { chart_type: 'unknown', reason: '', confidence: 0 };
  }
  let chartType = String(parsed.chart_type || '').trim().toLowerCase();
  if (KNOWN.indexOf(chartType) === -1) chartType = 'unknown';
  const conf = parseFloat(parsed.confidence);
  return {
    chart_type: chartType,
    reason: parsed.reason == null ? '' : String(parsed.reason),
    confidence: Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0,
  };
}

async function extractRangeChart(opts) {
  // UI-REVIEW-2026-09-07: "auto" may be re-assigned from the vision
  // classifier on the direct path (backend path resolves server-side).
  let mode = (opts && opts.mode) || 'range_chart';
  const {
    apiKey,
    baseUrl,
    model,
    maxTokens,
    proxyUrl,
    dataUrl,
    mediaType,
    caption,
    chartLang,
  } = opts;

  const { base64 } = rcaSplitDataUrl(dataUrl);
  if (!base64) {
    return { ok: false, errorKey: 'err.imageRead' };
  }

  // Backend mode: when served by the Python server (http/https origin),
  // call the same-origin /api/extract so the outbound MiniMax request is
  // made server-side. This avoids the browser CORS restriction entirely.
  //
  // We dispatch on `opts.transport` (set by app.js), NOT on `opts.mode`,
  // because `mode` is the chart kind (range_chart / columnar_section).
  // Mixing them up was Bug C1 and made the backend path dead code.
  if (opts.transport === 'backend') {
    return rcaCallBackend(opts, base64);
  }

  const target = (proxyUrl && proxyUrl.trim())
    ? proxyUrl.trim().replace(/\/+$/, '')
    : String(baseUrl).replace(/\/+$/, '');
  // F-8: Enforce HTTPS for proxy URLs to prevent API key leakage via HTTP MITM
  if (target.startsWith('http://')) {
    console.error('Insecure proxy URL: HTTP is not allowed, falling back to direct connection');
    return rcaCallBackend(opts, base64);
  }
  // SSRF protection: block private/internal hostnames and cloud metadata endpoints
  let targetHostname;
  try {
    targetHostname = new URL(target).hostname.toLowerCase();
  } catch (_) {
    targetHostname = '';
  }
  const ssrfBlocked = [
    'localhost', '127.0.0.1', '0.0.0.0', '::1',
    '169.254.169.254',   // AWS / Azure metadata
    'metadata.google.internal', // GCP metadata
  ];
  const isPrivate = /^10\./.test(targetHostname) ||
    /^172\.(1[6-9]|2[0-9]|3[0-1])\./.test(targetHostname) ||
    /^192\.168\./.test(targetHostname) ||
    /^127\./.test(targetHostname) ||
    /^169\.254\./.test(targetHostname) ||
    ssrfBlocked.includes(targetHostname);
  if (isPrivate && targetHostname) {
    console.error('Insecure endpoint: private/internal URLs are not allowed in direct mode, falling back to backend');
    return rcaCallBackend(opts, base64);
  }
  const url = target + '/v1/messages';

  // UI-REVIEW-2026-09-07 (auto mode, direct transport): the caption
  // heuristic matched nothing (app.js only forwards "auto" when that is
  // the case), so classify the image itself with a cheap small-token
  // call, then continue extraction with the detected chart type.
  // Confidence < 0.5 or "unknown" falls back to range_chart. The backend
  // transport never reaches here — rcaCallBackend sent mode:"auto" and
  // the server resolved it.
  if (mode === 'auto') {
    // UI-REVIEW-2026-09-07: surface the classification stage so the busy
    // label can show "Detecting chart type…" during the extra round-trip.
    if (opts.onStage) opts.onStage('classifying');
    try {
      const clsBody = {
        model: model,
        max_tokens: 500,
        system: (typeof CHART_CLASSIFY_SYSTEM_PROMPT !== 'undefined')
          ? CHART_CLASSIFY_SYSTEM_PROMPT : '',
        messages: [{
          role: 'user',
          content: [
            { type: 'image',
              source: { type: 'base64', media_type: mediaType || 'image/png', data: base64 } },
            { type: 'text',
              text: 'Caption:\n' + (caption && caption.trim() ? caption.trim() : '(no caption)')
                    + '\n\nClassify the chart type as the strict JSON contract.' },
          ],
        }],
      };
      const clsResp = await fetch(url, {
        method: 'POST',
        headers: {
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01',
          'content-type': 'application/json',
        },
        body: JSON.stringify(clsBody),
        redirect: 'manual',
      });
      if (clsResp && clsResp.ok) {
        const clsJson = await clsResp.json();
        const clsText = clsJson && clsJson.content && clsJson.content.length
          ? clsJson.content.filter((b) => b.type === 'text').map((b) => b.text).join('')
          : '';
        const cls = rcaNormalizeChartClassification(safeJsonLoads(clsText));
        if (cls.chart_type !== 'unknown' && cls.confidence >= 0.5) {
          mode = cls.chart_type;
        }
      }
      // classification failure -> keep "auto"; the modeInstruction fallback
      // below treats any non-listed mode as range_chart, matching Python.
    } catch (_e) {
      // swallow — fall back to range_chart prompt, same as Python.
    }
    if (mode === 'auto') mode = 'range_chart';
  }

  const langHint = (CHART_LANG_HINT && CHART_LANG_HINT[chartLang]) || '';
  let modeInstruction;
  if (mode === 'columnar_section') {
    modeInstruction = 'Extract the columnar-section information as the strict JSON contract.';
  } else if (mode === 'abundance_diagram') {
    modeInstruction = 'Extract the abundance-diagram information as the strict JSON contract.';
  } else if (mode === 'phylogenetic_tree') {
    modeInstruction = 'Extract the phylogenetic-tree information as the strict JSON contract.';
  } else if (mode === 'zonation_chart') {
    modeInstruction = 'Extract the biozonation / correlation chart information as the strict JSON contract.';
  } else {
    modeInstruction = 'Extract the geological information as the strict JSON contract.';
  }
  const userPrompt =
    'Caption:\n' + (caption && caption.trim() ? caption.trim() : '(no caption)') + '\n\n' +
    langHint + modeInstruction;

  let sysPrompt = RANGE_CHART_SYSTEM_PROMPT;
  if (mode === 'columnar_section' && typeof COLUMNAR_SECTION_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = COLUMNAR_SECTION_SYSTEM_PROMPT;
  } else if (mode === 'abundance_diagram' && typeof ABUNDANCE_DIAGRAM_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = ABUNDANCE_DIAGRAM_SYSTEM_PROMPT;
  } else if (mode === 'phylogenetic_tree' && typeof PHYLOGENETIC_TREE_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = PHYLOGENETIC_TREE_SYSTEM_PROMPT;
  } else if (mode === 'zonation_chart' && typeof ZONATION_CHART_SYSTEM_PROMPT !== 'undefined') {
    sysPrompt = ZONATION_CHART_SYSTEM_PROMPT;
  }

  const body = {
    model: model,
    max_tokens: maxTokens || 4000,
    system: sysPrompt,
    messages: [
      {
        role: 'user',
        content: [
          {
            type: 'image',
            source: { type: 'base64', media_type: mediaType || 'image/png', data: base64 },
          },
          { type: 'text', text: userPrompt },
        ],
      },
    ],
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs);
  // FIX-6: honor a caller-supplied cancel signal (user pressed Cancel).
  const onExtAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener('abort', onExtAbort);
  }

  // M11 (REVIEW-2026-08-19): wrap the direct-mode fetch in retryWithBackoff
  // so transient network failures (5xx, TypeError from fetch, broker reset)
  // get up to 3 retries before failing. AbortSignal still controls
  // cancellation at the retry-loop level, so a user Cancel stops the
  // retry chain mid-flight. Err.parse / err.cancelled are surfaced by
  // downstream branches — only TRANSPORT errors are retried.
  const tryOnce = async () => {
    const r = await fetch(url, {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
      // M3 (REVIEW-2026-08-19): explicit `redirect: 'manual'` so a 3xx
      // response from the upstream doesn't auto-follow and leak the
      // `x-api-key` header to the redirect target. Browsers default to
      // `follow` which can send the same Authorization header to an
      // attacker-controlled endpoint.
      redirect: 'manual',
    });
    if (r && r.ok === false && r.status >= 500 && r.status < 600) {
      // 5xx is transient — throw so retryWithBackoff retries.
      const err = new Error('HTTP ' + r.status);
      err.status = r.status;
      throw err;
    }
    return r;
  };
  const ErrUtils = (typeof window !== 'undefined' && window.RCAErrorUtils) || null;

  let resp;
  try {
    if (ErrUtils && typeof ErrUtils.retryWithBackoff === 'function') {
      resp = await ErrUtils.retryWithBackoff(tryOnce, {
        maxRetries: 3,
        initialDelay: 0.8,
        backoffFactor: 1.6,
        maxDelay: 30.0,
        signal: opts.signal || null,
        onRetry: (attempt, delay, err) => {
          if (typeof console !== 'undefined' && console.warn) {
            console.warn('[extractRangeChart] retry', attempt, 'after', delay.toFixed(2), 's —', err && err.message);
          }
        },
      });
    } else {
      resp = await tryOnce();
    }
  } catch (err) {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);
    if (err && err.name === 'AbortError') {
      return { ok: false, errorKey: (opts.signal && opts.signal.aborted) ? 'err.cancelled' : 'err.timeout' };
    }
    // CORS/TypeError from fetch, or transient 5xx that exhausted retries.
    return { ok: false, errorKey: 'err.network' };
  }
  clearTimeout(timer);
  if (opts.signal) opts.signal.removeEventListener('abort', onExtAbort);

  if (!resp.ok) {
    let detail = '';
    try { detail = await resp.text(); } catch (_e) { /* ignore */ }
    // Phase M fix: try to surface the server's structured error_key
    // from the JSON body when the status is one of the documented
    // ones. Previously resp.text() was used unconditionally, which
    // discarded the server's `error_key: 'err.bodyTooLarge'` hint.
    let errorKey = 'err.http';
    if (resp.status === 401) errorKey = 'err.401';
    else if (resp.status === 403) errorKey = 'err.403';
    // REVIEW-2026-11-07 (low): direct-mode 403 → 'err.403' (upstream
    // rejected the key/endpoint). Distinct from backend-mode
    // 'err.forbidden' (CSRF/origin), see the comment in rcaCallBackend.
    else if (resp.status === 413) errorKey = 'err.bodyTooLarge';
    else if (resp.status === 429) errorKey = 'err.429';
    // If the body is JSON, lift the server's error_key (when it
    // matches a known key) so the i18n string is correct.
    let serverKey = null;
    try {
      const j = JSON.parse(detail);
      if (j && typeof j.error_key === 'string') serverKey = j.error_key;
    } catch (_e) { /* not JSON, ignore */ }
    if (serverKey) errorKey = serverKey;
    return { ok: false, errorKey, status: resp.status, raw: detail };
  }

  let payload;
  try {
    payload = await resp.json();
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: '' };
  }

  // Extract ALL text content blocks (Anthropic-compatible shape).
  // REVIEW-2026-07-31: Anthropic splits long replies into multiple text
  // blocks; taking only the first one dropped the tail of the JSON, so
  // multi-block replies failed to parse in the browser-only path while
  // the Python side (_read_response) concatenated them and succeeded.
  // Concatenate all text blocks in order — identical to the Python side.
  let rawText = '';
  const content = Array.isArray(payload.content) ? payload.content : [];
  for (const c of content) {
    if (c && c.type === 'text' && typeof c.text === 'string') {
      rawText += c.text;
    }
  }
  // M10: detect truncation across API shapes. Anthropic uses
  // `stop_reason: "max_tokens"`; OpenAI uses `finish_reason: "length"`;
  // Gemini uses `candidates[].finishReason: "MAX_TOKENS" / "LENGTH"`.
  let truncated = false;
  if (payload && typeof payload === 'object') {
    if (payload.stop_reason === 'max_tokens') truncated = true;
    if (Array.isArray(payload.choices) && payload.choices[0] && payload.choices[0].finish_reason === 'length') truncated = true;
    if (Array.isArray(payload.candidates) && payload.candidates[0]) {
      const fr = payload.candidates[0].finishReason;
      if (fr === 'MAX_TOKENS' || fr === 'LENGTH') truncated = true;
    }
  }

  if (!rawText) {
    return { ok: false, errorKey: 'err.empty', raw: JSON.stringify(payload).slice(0, 2000), truncated };
  }

  let parsed;
  try {
    parsed = safeJsonLoads(rawText);
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: rawText, truncated };
  }

  let data;
  // REVIEW-2026-07-31: the normalizers enforce invariants by throwing
  // (mirroring rca_core/extractor.py), but the Python caller catches the
  // exception and returns ok=False — the JS side previously let the throw
  // escape, violating the never-throws contract. Mirror the Python
  // try/except here.
  try {
    if (mode === 'columnar_section' && typeof rcaNormalizeColumnarResult === 'function') {
      data = rcaNormalizeColumnarResult(parsed);
    } else if (mode === 'abundance_diagram' && typeof rcaNormalizeAbundanceResult === 'function') {
      data = rcaNormalizeAbundanceResult(parsed);
    } else if (mode === 'phylogenetic_tree' && typeof rcaNormalizePhylogeneticTreeResult === 'function') {
      data = rcaNormalizePhylogeneticTreeResult(parsed);
    } else if (mode === 'zonation_chart' && typeof rcaNormalizeZonationChartResult === 'function') {
      data = rcaNormalizeZonationChartResult(parsed);
    } else {
      data = rcaNormalizeResult(parsed);
    }
  } catch (err) {
    const why = err && err.message ? err.message : String(err);
    return { ok: false, errorKey: 'err.extract', raw: rawText, truncated, warning: 'normalize failed: ' + why };
  }
  // H5 (REVIEW-2026-08-19): mirror rca_core/extractor.py:866-880. A
  // truncated_or_unrecognized_payload in the range_chart path flips the
  // result to ok=false with err.parse. The other 3 modes keep ok=true
  // and surface the warning on data._warnings — same as Python.
  if (data && Array.isArray(data._warnings) &&
      data._warnings.indexOf('truncated_or_unrecognized_payload') !== -1 &&
      (mode === 'range_chart' || !mode)) {
    return {
      ok: false,
      errorKey: 'err.parse',
      data,
      raw: rawText,
      truncated: !!truncated,
      warning: 'truncated_or_unrecognized_payload',
    };
  }
  return { ok: true, data, raw: rawText, truncated };
}
