// json-utils.js - lenient JSON extraction, ported from RLPE
// range_chart_extractor._extract_balanced_json_object / _safe_json_loads.
'use strict';

// Extract ALL balanced {...} JSON objects (not just the first).
function extractAllBalancedJsonObjects(text) {
  const results = [];
  let start = text.indexOf('{');
  while (start !== -1) {
    let depth = 0;
    let inString = false;
    let escape = false;
    for (let i = start; i < text.length; i++) {
      const c = text[i];
      if (inString) {
        if (escape) {
          escape = false;
          continue; // skip the escaped character, matching Python behavior
        } else if (c === '\\') {
          escape = true;
        } else if (c === '"') {
          inString = false;
        }
        continue;
      }
      if (c === '"') {
        inString = true;
      } else if (c === '{') {
        depth += 1;
      } else if (c === '}') {
        depth -= 1;
        if (depth === 0) {
          const candidate = text.slice(start, i + 1);
          // Only add if it's valid JSON
          try {
            JSON.parse(candidate);
            results.push(candidate);
          } catch (_e) {
            // skip
          }
          break;
        }
      }
    }
    start = text.indexOf('{', start + 1);
  }
  return results;
}

// Score a parsed JSON object on how payload-like it is.
const _PAYLOAD_KEYS = new Set([
  'species_ranges', 'sections', 'biozones', 'other_fossils', 'confidence',
  'format', 'schema_version', '$schema', 'required', 'properties', 'example',
]);

function _payloadScore(parsed) {
  // P1-3 fix: arrays are scored by summing dict-item scores (matching Python
  // _payload_score). Previously returned 0 for arrays, losing signal from
  // list-of-dicts payloads.
  if (!parsed || typeof parsed !== 'object') return 0;
  if (Array.isArray(parsed)) {
    let score = 0;
    for (const item of parsed) {
      if (item && typeof item === 'object' && !Array.isArray(item)) {
        score += _payloadScore(item);
      }
    }
    return score;
  }
  const keys = Object.keys(parsed);
  let score = 0;
  // Real payload keys carry positive weight
  const realHits = keys.filter(k => ['species_ranges', 'sections', 'biozones', 'other_fossils', 'confidence'].includes(k)).length;
  score += realHits * 100;
  // Schema keys carry negative weight
  const schemaHits = keys.filter(k => ['format', 'schema_version', '$schema', 'required', 'properties', 'example'].includes(k)).length;
  score -= schemaHits * 50;
  // Nesting: scan dict/list values
  for (const v of Object.values(parsed)) {
    if (v && typeof v === 'object') {
      score += Math.max(0, _payloadScore(v)) / 4;
    }
  }
  return score;
}

// Return the first balanced {...} JSON object substring of `text`, or null.
// Handles nested braces and braces inside string literals correctly.
function extractBalancedJsonObject(text) {
  let start = text.indexOf('{');
  while (start !== -1) {
    let depth = 0;
    let inString = false;
    let escape = false;
    for (let i = start; i < text.length; i++) {
      const c = text[i];
      if (inString) {
        if (escape) {
          escape = false;
          continue; // skip the escaped character, matching Python behavior
        } else if (c === '\\') {
          escape = true;
        } else if (c === '"') {
          inString = false;
        }
        continue;
      }
      if (c === '"') {
        inString = true;
      } else if (c === '{') {
        depth += 1;
      } else if (c === '}') {
        depth -= 1;
        if (depth === 0) {
          return text.slice(start, i + 1);
        }
      }
    }
    // No balanced close for this start; try the next "{".
    start = text.indexOf('{', start + 1);
  }
  return null;
}

// Return the first balanced [...] JSON array substring of `text`, or null.
// Mirrors rca_core.json_utils.extract_balanced_json_array: some models
// occasionally wrap the response in `[...]` instead of `{...}`. We surface
// it rather than failing so the caller can wrap it for diagnostics.
function extractBalancedJsonArray(text) {
  let start = text.indexOf('[');
  while (start !== -1) {
    let depth = 0;
    let inString = false;
    let escape = false;
    for (let i = start; i < text.length; i++) {
      const c = text[i];
      if (inString) {
        if (escape) {
          escape = false;
          continue; // skip the escaped character, matching Python behavior
        } else if (c === '\\') {
          escape = true;
        } else if (c === '"') {
          inString = false;
        }
        continue;
      }
      if (c === '"') {
        inString = true;
      } else if (c === '[') {
        depth += 1;
      } else if (c === ']') {
        depth -= 1;
        if (depth === 0) {
          return text.slice(start, i + 1);
        }
      }
    }
    start = text.indexOf('[', start + 1);
  }
  return null;
}

// Strip markdown code fences (```json ... ```) from a model response.
// Handles: ```json ``` / ``` ``` / leading-only / multiple fences.
// Returns the string unchanged if no fence is present.
function stripMarkdownFence(text) {
  if (!text) return text;
  let s = String(text).trim();
  // If the whole text is one fenced block, extract its inner content.
  let fenceBlock = s.match(/^```(?:json)?\s*\n?([\s\S]*?)\n?```\s*$/i);
  if (fenceBlock) return fenceBlock[1].trim();
  // FIX (fenced+prose): also handle a fenced block surrounded by prose on
  // either side, e.g. "Here:\n```json\n{...}\n```\nThanks". The stricter
  // regex above requires the fence to span the whole string; this one
  // locates the first fenced block anywhere in the text.
  fenceBlock = s.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/i);
  if (fenceBlock) return fenceBlock[1].trim();
  // Otherwise strip leading/trailing fence lines defensively.
  s = s.replace(/^```(?:json)?\s*/gmi, '');
  s = s.replace(/\s*```$/gmi, '');
  return s;
}

// Find the first JSON object or array inside prose (e.g. "Here is the
// result:\n{\"a\":1}\nLet me know."). Returns null if nothing balanced.
function extractJsonLike(text) {
  if (!text) return null;
  const s = String(text).trim();
  for (const [opener, extractor] of [['{', extractBalancedJsonObject],
                                      ['[', extractBalancedJsonArray]]) {
    const start = s.indexOf(opener);
    if (start !== -1) {
      const candidate = extractor(s.slice(start));
      if (candidate !== null) return candidate;
    }
  }
  return null;
}

// Lift a payload nested one level under a common wrapper key.
// REVIEW-2026-07-31 (M2 parity): Python's safe_json_loads gained this in
// _promote_wrapper. When the model wraps the real payload in
// {"data": {...}} — with or without sibling metadata — the inner keys are
// promoted to the top level so the normalizers see the payload.
function promoteWrapper(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return parsed;
  for (const key of ['data', 'result', 'payload', 'response', 'output']) {
    const inner = parsed[key];
    if (inner && typeof inner === 'object' && !Array.isArray(inner) && Object.keys(inner).length > 0) {
      const promoted = Object.assign({}, parsed);
      delete promoted[key];
      for (const k of Object.keys(inner)) {
        if (!(k in promoted)) promoted[k] = inner[k];
      }
      return promoted;
    }
  }
  return parsed;
}

// Sprint B (REVIEW-2026-09-04): known root keys used to decide whether a
// fenced block carries the real payload. Mirrors the Python side's
// _KNOWN_ROOT_KEYS in rca_core/json_utils.py (union of the per-mode ROOTS
// constants). `_extras` is deliberately NOT in the set on either side: it
// is an artifact the normalizers ATTACH to their output and is never a
// root key of a raw model payload. The paleomap / scatter_plot /
// chemical_stratigraphy keys are inert for JS normalization (the browser
// frontend does not expose those modes) but kept for exact two-sided
// parity of the fence-selection decision.
const RCA_KNOWN_ROOT_KEYS = new Set([
  // range_chart (RANGE_CHART_ROOTS)
  'sections', 'species_ranges', 'biozones', 'other_fossils', 'confidence',
  // columnar_section (_KNOWN_COLUMNAR_ROOT_KEYS)
  'fossil_legend', 'lithology_legend', 'cross_beds', 'overall_confidence',
  // abundance_diagram (_KNOWN_ABUNDANCE_ROOT_KEYS)
  'sites', 'abundances', 'zones',
  // chemical_stratigraphy (_KNOWN_CHEMICAL_STRAT_ROOT_KEYS)
  'data_points', 'events', 'intervals',
  // paleomap (_KNOWN_PALEOMAP_ROOT_KEYS)
  'continents', 'oceans_seas', 'tectonic_features', 'biogeographic_realms',
  'fossil_sites', 'paleolatitude_indicators',
  // scatter_plot (_KNOWN_SCATTER_PLOT_ROOT_KEYS)
  'groups', 'points', 'outliers', 'statistics',
  // phylogenetic_tree
  'metadata', 'nodes', 'root_ids', 'legend',
]);

// Collect the inner content of every complete markdown fence block, in
// order. Mirrors the fence regex strip_markdown_fence already uses
// (```json-tagged or bare); blocks that are never closed are ignored (the
// caller falls back to the old strip path, which handles truncation).
function _collectFenceBlocks(s) {
  const blocks = [];
  const re = /```(?:json)?[ \t]*\r?\n?([\s\S]*?)\r?\n?```/gi;
  let m;
  while ((m = re.exec(s)) !== null) {
    blocks.push(m[1].trim());
    if (m.index === re.lastIndex) re.lastIndex += 1; // safety against zero-width loops
  }
  return blocks;
}

// Sprint B (REVIEW-2026-09-04): multi-fence payload selection. When a model
// restates the JSON schema as one fenced example and then emits the real
// payload in a SECOND fenced block, the old "strip first fence" behavior
// fed the schema example to the parser and the real data was lost.
// Rule (kept identical to the Python json_utils implementation being landed
// in parallel): collect every fence block, strictly parse each, and return
// the FIRST block that (a) parses as a JSON object (not an array) and
// (b) contains at least one known root key. If no block qualifies, fall
// back to the FIRST block — the pre-existing behavior. Returns null when
// the text contains no complete fence block at all.
function selectPayloadFenceBlock(text) {
  const s = String(text == null ? '' : text);
  const blocks = _collectFenceBlocks(s);
  if (blocks.length === 0) return null;
  for (const b of blocks) {
    try {
      const parsed = JSON.parse(b);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const k of Object.keys(parsed)) {
          if (RCA_KNOWN_ROOT_KEYS.has(k)) return b;
        }
      }
    } catch (_e) {
      // Not strict JSON — try the next block.
    }
  }
  return blocks[0];
}

// Lenient JSON object parse with a 6-level fallback chain.
// Chain (each runs only if prior failed):
//   1. Strip markdown fences (```json ... ```)
//   2. Strip raw control chars (0x00-0x1F except \t\r\n)
//   3. Strict JSON.parse (incl. top-level arrays → wrapped)
//   4. Balanced-brace object extraction ({...})
//   5. Balanced-bracket array extraction ([...]) → wrapped
//   6. Prose-embedded JSON (extractJsonLike) — last resort
// Throws on failure.
// UI-REVIEW-2026-09-07: best-effort repair of a TRUNCATED JSON object/array.
// Mirror of rca_core/json_utils._repair_truncated_json: record every element
// boundary with its open-bracket stack, then close the stack at the longest
// boundary backwards until strict JSON.parse succeeds. Recovers completed
// rows above a max_tokens cut instead of letting Level 4 pick one stray row.
function repairTruncatedJson(text) {
  const s = String(text).trim();
  if (!s || (s[0] !== '{' && s[0] !== '[')) return null;
  const stack = [];
  const boundaries = [];
  let inString = false;
  let escape = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (escape) { escape = false; continue; }
    if (inString) {
      if (c === '\\') { escape = true; }
      else if (c === '"') {
        inString = false;
        if (stack.length === 0) return null; // outer already closed: stray prose, not truncation
        boundaries.push([i + 1, stack.slice()]);
      }
      continue;
    }
    if (c === '"') { inString = true; continue; }
    if (c === '{' || c === '[') { stack.push(c); continue; }
    if (c === '}' || c === ']') {
      if (stack.length) stack.pop();
      if (stack.length === 0) return null; // outer closed mid-text: not truncation
      boundaries.push([i + 1, stack.slice()]);
      continue;
    }
    if (c === ',') { boundaries.push([i + 1, stack.slice()]); }
  }
  for (let b = boundaries.length - 1; b >= 0; b--) {
    const [idx, openStack] = boundaries[b];
    if (openStack.length === 0) continue;
    const closers = openStack.slice().reverse()
      .map((o) => (o === '{' ? '}' : ']')).join('');
    const candidate = s.slice(0, idx).replace(/[,\s]+$/, '') + closers;
    try { JSON.parse(candidate); return candidate; } catch (_e) { /* try shorter */ }
  }
  return null;
}

function safeJsonLoads(text) {
  if (!text) throw new Error('empty text');
  // Sprint B (REVIEW-2026-09-04): multi-fence selection runs BEFORE the
  // generic strip. With 0 fence blocks it returns null and the old
  // stripMarkdownFence path (incl. truncated-fence handling) applies
  // unchanged; with >=1 blocks it returns either the first qualifying
  // payload block or the first block (old behavior).
  const fenced = selectPayloadFenceBlock(String(text));
  // Level 1: strip markdown fences.
  let s = stripMarkdownFence(fenced !== null ? fenced : String(text).trim());
  // Level 2: strip raw control chars that JSON.parse rejects.
  s = s.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '');
  // Level 3: strict parse.
  try {
    const parsed = JSON.parse(s);
    // Fix J-1: typeof null === 'object' in JS, but null is not a valid object
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && parsed !== null) {
      return promoteWrapper(parsed);
    }
    // Top-level array: wrap it so downstream code can use object semantics,
    // matching Python's {_array_root: [...]} wrapper.
    if (Array.isArray(parsed)) {
      // Fix J-2: add _note field to match Python behavior
      return { _array_root: parsed, _note: 'model returned a top-level array; wrapping for diagnostics' };
    }
  } catch (_e) {
    /* fall through to balanced-object extraction */
  }
  // Level 3.5 (UI-REVIEW-2026-09-07): truncated-payload repair. Mirrors
  // rca_core/json_utils safe_json_loads Level 3.5 — close the brackets at
  // the last complete element instead of letting Level 4 pick one stray
  // row from inside the unbalanced outer payload. Only repairs yielding a
  // recognizable payload root are accepted.
  if (s && (s[0] === '{' || s[0] === '[')) {
    const repaired = repairTruncatedJson(s);
    if (repaired !== null) {
      try {
        const parsed = JSON.parse(repaired);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)
            && [...RCA_KNOWN_ROOT_KEYS].some((k) => k in parsed)) {
          return promoteWrapper(parsed);
        }
      } catch (_e2) { /* fall through */ }
    }
  }
  // Level 4: enumerate ALL balanced {...} objects, score each, pick best.
  // This is the key parity fix with Python's safe_json_loads Level 4.
  // H8/M2 parity (REVIEW-2026-07-31): Python scores candidates on PAYLOAD
  // LIKELIHOOD ONLY and uses length as a TIEBREAKER (the old JS formula
  // `_payloadScore(p) * 10 + candidate.length` let a long schema/example
  // string outscore a small real payload, silently discarding the data in
  // the browser-only path). Python also drops candidates nested inside
  // another candidate (M2) so a wrapper object keeps its sibling fields
  // and the payload is not lost.
  // Tie-break order mirrors Python's sorted(..., reverse=True) on
  // (payload_score, length, index): highest score, then LONGEST source,
  // then LATEST occurrence.
  const allCandidates = extractAllBalancedJsonObjects(s);
  if (allCandidates.length > 0) {
    // M2: drop candidates nested inside another candidate (Python's
    // `not any(c != d and c in d for d in candidates)`).
    const topLevel = allCandidates.filter(c => !allCandidates.some(d => d !== c && d.indexOf(c) !== -1));
    let best = null;
    let bestScore = -Infinity;
    let bestLen = -1;
    let bestIdx = -1;
    for (let idx = 0; idx < topLevel.length; idx++) {
      const candidate = topLevel[idx];
      try {
        const p = JSON.parse(candidate);
        if (p && typeof p === 'object' && !Array.isArray(p)) {
          const sc = _payloadScore(p);
          if (sc > bestScore ||
              (sc === bestScore && candidate.length > bestLen) ||
              (sc === bestScore && candidate.length === bestLen && idx > bestIdx)) {
            bestScore = sc;
            best = p;
            bestLen = candidate.length;
            bestIdx = idx;
          }
        }
      } catch (_e) {
        // skip invalid
      }
    }
    if (best !== null) return promoteWrapper(best);
  }
  // Level 5: balanced-bracket array extraction → wrapped.
  const arrCandidate = extractBalancedJsonArray(s);
  if (arrCandidate !== null) {
    try {
      const parsedArr = JSON.parse(arrCandidate);
      if (Array.isArray(parsedArr)) return { _array_root: parsedArr, _note: 'model returned a top-level array; wrapping for diagnostics' };
    } catch (_e) { /* fall through */ }
  }
  // Level 6: prose-embedded JSON (last resort).
  const prose = extractJsonLike(s);
  if (prose !== null) {
    try {
      const parsed = JSON.parse(prose);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && parsed !== null) return promoteWrapper(parsed);
      if (Array.isArray(parsed)) return { _array_root: parsed, _note: 'model returned a top-level array; wrapping for diagnostics' };
    } catch (_e) { /* fall through */ }
  }
  throw new Error('no JSON object found in: ' + s.slice(0, 120));
}
