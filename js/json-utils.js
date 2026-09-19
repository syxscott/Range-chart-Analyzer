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
  // Nesting: scan dict/list values.
  // REVIEW-2026-09-10: integer division, matching Python's `// 4`. The
  // float version scored 112.5 where Python scored 112, and a fractional
  // difference decides the candidate in an otherwise exact tie - the two
  // engines then picked DIFFERENT objects out of the same reply.
  for (const v of Object.values(parsed)) {
    if (v && typeof v === 'object') {
      score += Math.floor(Math.max(0, _payloadScore(v)) / 4);
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
//
// REVIEW-2026-09-20 #102 (mirror of rca_core/json_utils.strip_markdown_fence):
// the two whole-text/single-block shortcuts that used to live here returned
// blocks[0] BEFORE any ranking ran, so when a model echoed the contract in a
// ```json block and put the real payload in the prose AFTER it, Level 3
// accepted the restated example as "a dict" and the extraction was placeholders
// (the browser silently rendered "<binomial>" rows). One block now goes through
// the same ranking as many, and the chosen block is then compared against the
// text OUTSIDE the fences on the same two signals (payload-key density, then
// placeholder density); a strictly better prose makes the delimiters disappear
// and the WHOLE text get returned so the later levels score every candidate.
// Ties go to the block, so every historically-correct answer is reproduced.
function stripMarkdownFence(text) {
  if (!text) return text;
  const s = String(text).trim();
  const matches = _collectFenceMatches(s);
  const blocks = matches.map((m) => m.block);
  if (blocks.length > 0) {
    const chosen = _chooseFenceBlock(blocks);
    // PERF parity with Python: scan the prose first, and a (0, 0) rank (no
    // parseable object outside the fences — the overwhelmingly common shape)
    // skips the block scan entirely.
    const parts = _splitAroundFences(s, matches);
    const outsideRank = rcaBestPayloadRank(parts.outside);
    if (!_rankIsZero(outsideRank) && _rankGreaterThan(outsideRank, rcaBestPayloadRank(chosen))) {
      return parts.unfenced;
    }
    return chosen;
  }
  // Otherwise strip leading/trailing fence lines defensively.
  let out = s.replace(/^```(?:json)?\s*/gmi, '');
  out = out.replace(/\s*```$/gm, '');
  return out;
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
  // zonation_chart (rca_core/extractor._KNOWN_ZONATION_ROOT_KEYS).
  // REVIEW-2026-09-10: mirror of the Python fix - without these a fence
  // carrying only zonations/correlations is not recognised as a payload.
  'zonations', 'correlations',
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

// True when *parsed* is an object carrying at least one known root key.
// Mirror of rca_core/json_utils._looks_like_payload_root.
function rcaLooksLikePayloadRoot(parsed) {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return false;
  for (const k of Object.keys(parsed)) {
    if (RCA_KNOWN_ROOT_KEYS.has(k)) return true;
  }
  return false;
}

// Array counterpart, mirror of _looks_like_payload_list (REVIEW-2026-09-20
// #101): a reply cut inside a TOP-LEVEL array repairs to a list, and the
// dict-only guard of Level 3.5 threw that payload away so Level 4 rescued one
// inner row. Accepted when (a) the MAJORITY of the elements are objects — a
// [...] of bare scalars is far more likely a prose fragment than an extraction —
// and (b) either one of those objects carries a known root key (an array of
// wrapper objects) or at least one looks like a DATA ROW (>= 2 fields, which is
// what a cut row array contains).
function rcaLooksLikePayloadList(parsed) {
  if (!Array.isArray(parsed) || parsed.length === 0) return false;
  const dicts = parsed.filter(
    (item) => item && typeof item === 'object' && !Array.isArray(item));
  if (dicts.length === 0 || dicts.length * 2 < parsed.length) return false;
  for (const d of dicts) {
    if (rcaLooksLikePayloadRoot(d)) return true;
  }
  for (const d of dicts) {
    if (Object.keys(d).length >= 2) return true;
  }
  return false;
}

// Mirror of _try_parse_object: strict parse; object/array or null.
function rcaTryParseObject(s) {
  try {
    const parsed = JSON.parse(s);
    if (parsed && typeof parsed === 'object') return parsed;
    return null;
  } catch (_e) {
    return null;
  }
}

// Collect EVERY complete fenced block WITH ITS SPAN, in order. Mirrors Python's
// `re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", s, DOTALL|I)`; the whitespace
// class is kept identical (JS `\s` ≈ Python `\s` under UNICODE) so the offsets
// the prose-vs-block comparison relies on agree.
function _collectFenceMatches(s) {
  const out = [];
  const re = /```(?:json)?\s*\n?([\s\S]*?)\n?```/gi;
  let m;
  while ((m = re.exec(s)) !== null) {
    out.push({ start: m.index, end: m.index + m[0].length, block: m[1].trim() });
    if (m.index === re.lastIndex) re.lastIndex += 1; // safety against zero-width loops
  }
  return out;
}

// Inner content of every complete fence block, in order (blocks that are never
// closed are ignored, like Python's finditer).
function _collectFenceBlocks(s) {
  return _collectFenceMatches(String(s == null ? '' : s)).map((m) => m.block);
}

// Python tuple ordering for the two-element payload ranks below.
function _rankGreaterThan(a, b) {
  if (a[0] !== b[0]) return a[0] > b[0];
  return a[1] > b[1];
}

function _rankIsZero(r) { return r[0] === 0 && r[1] === 0; }

// `(payload_score, -placeholder_count)` of the best object in *text* — mirror
// of _best_payload_rank. The same two signals the multi-fence rule uses:
// payload-key density first, then the angle-bracket placeholders that mark a
// restated contract ("<binomial>"), so a fenced EXAMPLE and an unfenced payload
// built from the same schema compare by their contents instead of tying.
// (0, 0) when nothing parses, which never beats a real block.
function rcaBestPayloadRank(text) {
  let best = null;
  for (const cand of extractAllBalancedJsonObjects(String(text == null ? '' : text))) {
    const parsed = rcaTryParseObject(cand);
    if (parsed === null) continue;
    let score = _payloadScore(parsed);
    if (rcaLooksLikePayloadRoot(parsed) && score <= 0) {
      // A root-keyed object whose schema-ish keys cancelled the payload keys is
      // still a payload candidate, not prose.
      score += 100;
    }
    const rank = [score, -rcaFencePlaceholderCount(cand)];
    if (best === null || _rankGreaterThan(rank, best)) best = rank;
  }
  return best === null ? [0, 0] : best;
}

// Mirror of _split_around_fences: `outside` is the text with every complete
// fenced span REMOVED, `unfenced` the same text with only the ``` delimiters
// dropped so the blocks stay readable in place.
function _splitAroundFences(s, matches) {
  const outside = [];
  const unfenced = [];
  let prev = 0;
  for (const m of matches) {
    outside.push(s.slice(prev, m.start));
    unfenced.push(s.slice(prev, m.start));
    unfenced.push(m.block);
    prev = m.end;
  }
  outside.push(s.slice(prev));
  unfenced.push(s.slice(prev));
  return { outside: outside.join('\n'), unfenced: unfenced.join('').trim() };
}

// The block-choice half of strip_markdown_fence: prefer the block with the
// FEWEST schema-placeholder tokens, then the earliest — a model that restates
// the contract first writes the REAL root keys (so the example qualifies by key
// name) plus "<binomial>" placeholders, and ranking on density is what keeps
// the payload from being evicted. When no block carries a known root key the
// historical first-block behaviour is kept.
function _chooseFenceBlock(blocks) {
  const qualifying = [];
  for (let i = 0; i < blocks.length; i += 1) {
    if (rcaLooksLikePayloadRoot(rcaTryParseObject(blocks[i]))) {
      qualifying.push([i, blocks[i]]);
    }
  }
  if (qualifying.length === 0) return blocks[0];
  qualifying.sort((a, b) => {
    const pa = rcaFencePlaceholderCount(a[1]);
    const pb = rcaFencePlaceholderCount(b[1]);
    if (pa !== pb) return pa - pb;
    return a[0] - b[0];
  });
  return qualifying[0][1];
}

// Sprint B (REVIEW-2026-09-04): multi-fence payload selection. When a model
// restates the JSON schema as one fenced example and then emits the real
// payload in a SECOND fenced block, the old "strip first fence" behavior
// fed the schema example to the parser and the real data was lost.
// REVIEW-2026-09-20: this stays a thin wrapper around the block choice for
// callers that only want it; the complete Python rule — including the "block vs
// the prose around it" comparison — lives in stripMarkdownFence, which is what
// safeJsonLoads calls. Returns null when there is no complete fence block.
function selectPayloadFenceBlock(text) {
  const blocks = _collectFenceBlocks(String(text == null ? '' : text));
  if (blocks.length === 0) return null;
  return _chooseFenceBlock(blocks);
}

// Angle-bracket placeholders mark "fill this in" in the prompt contract and in
// a model's restatement of it ("<binomial>", "<section name>"). A fenced block
// dense with them is an example, not the extraction.
const RCA_PLACEHOLDER_RE = /<[^<>\n]{0,60}>/g;

function rcaFencePlaceholderCount(text) {
  const m = String(text).match(RCA_PLACEHOLDER_RE);
  return m ? m.length : 0;
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

// Escape raw control characters that sit INSIDE a JSON string literal.
// Structural whitespace outside strings is untouched, so a pretty-printed
// body keeps its line breaks. Mirrors _escape_control_chars_in_strings in
// rca_core/json_utils.py.
function rcaEscapeControlCharsInStrings(text) {
  const s = String(text);
  const out = [];
  let inString = false;
  let escape = false;
  let changed = false;
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (escape) { escape = false; out.push(ch); continue; }
    if (inString) {
      if (ch === '\\') { escape = true; out.push(ch); continue; }
      if (ch === '"') { inString = false; out.push(ch); continue; }
      const code = ch.charCodeAt(0);
      if (code < 0x20) {
        const named = { 8: '\\b', 9: '\\t', 10: '\\n', 12: '\\f', 13: '\\r' }[code];
        out.push(named || '\\u' + code.toString(16).padStart(4, '0'));
        changed = true;
        continue;
      }
      out.push(ch);
      continue;
    }
    if (ch === '"') inString = true;
    out.push(ch);
  }
  return changed ? out.join('') : s;
}

function safeJsonLoads(text) {
  if (!text) throw new Error('empty text');
  // Level 1: strip markdown fences.
  // REVIEW-2026-09-20: this is now the SINGLE fence entry point, exactly like
  // Python's safe_json_loads -> strip_markdown_fence. The separate
  // selectPayloadFenceBlock() pre-pass the browser used to run first duplicated
  // only half of the rule (the block choice) and dropped the "block vs the
  // prose around it" comparison, so a reply whose real payload sat in the prose
  // AFTER a restated-contract fence produced placeholders in the browser and
  // rows in Python.
  let s = stripMarkdownFence(String(text).trim());
  // Level 2: strip raw control chars that JSON.parse rejects.
  //
  // REVIEW-2026-09-20 #104 (parity note, mirrors the identical comment in
  // rca_core/json_utils.py safe_json_loads Level 2): this is DELIBERATELY still
  // a DELETE and not an escape. The class is the NON-whitespace control range
  // (0x00-0x08, 0x0b, 0x0c, 0x0e-0x1f) — binary junk from a mojibake /
  // clipboard round-trip, never meaningful text — and escaping it to "\u0000"
  // would hand a literal control character to the downstream chain, where XML
  // (openpyxl raises IllegalCharacterError) and CSV cannot carry it at all: a
  // "recovered" character would turn into a crashed export instead of one
  // dropped byte. The whitespace family (\t \r \n), which IS meaningful inside
  // a caption, is exactly what this class excludes; those survive Level 2 and
  // are escaped losslessly by Level 3.2 below. Both engines use the identical
  // delete regex.
  s = s.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '');
  // Level 3: strict parse.
  try {
    const parsed = JSON.parse(s);
    // Fix J-1: typeof null === 'object' in JS, but null is not a valid object.
    // The truthiness check already excludes null, so no separate `!== null`
    // test is needed here.
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
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
  // Level 3.2 (REVIEW-2026-09-10): literal control characters INSIDE a string
  // literal. Level 2 deliberately keeps \t \r \n, which is exactly what makes
  // JSON.parse reject a string value containing a raw newline (a caption or
  // note copied from a multi-line figure label). The reply then failed
  // Levels 3-6 and Level 4 salvaged one inner row, silently emptying the
  // extraction. Mirrors rca_core/json_utils Level 3.2.
  const escapedCtl = rcaEscapeControlCharsInStrings(s);
  if (escapedCtl !== s) {
    let parsedCtl = null;
    try { parsedCtl = JSON.parse(escapedCtl); } catch (_e) { parsedCtl = null; }
    if (parsedCtl && typeof parsedCtl === 'object' && !Array.isArray(parsedCtl)) {
      return promoteWrapper(parsedCtl);
    }
    if (Array.isArray(parsedCtl)) {
      return { _array_root: parsedCtl, _note: 'model returned a top-level array; wrapping for diagnostics' };
    }
  }
  // Level 3.5 (UI-REVIEW-2026-09-07): truncated-payload repair. Mirrors
  // rca_core/json_utils safe_json_loads Level 3.5 — close the brackets at
  // the last complete element instead of letting Level 4 pick one stray
  // row from inside the unbalanced outer payload. Only repairs yielding a
  // recognizable payload are accepted.
  //
  // REVIEW-2026-09-20 #101 (parity): the guard used to be dict-only, so a
  // reply cut inside a TOP-LEVEL array — whose repair parses to a list — was
  // thrown away here and Level 4 then rescued one inner row. Both branches
  // now mirror Python exactly: a repaired dict must satisfy
  // _looks_like_payload_root(), a repaired list must satisfy
  // _looks_like_payload_list() and is wrapped in the same `_array_root`
  // envelope the other levels use (which the normalizers unwrap).
  if (s && (s[0] === '{' || s[0] === '[')) {
    const repaired = repairTruncatedJson(s);
    if (repaired !== null) {
      let parsed = null;
      try { parsed = JSON.parse(repaired); } catch (_e2) { parsed = null; }
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)
          && rcaLooksLikePayloadRoot(parsed)) {
        return promoteWrapper(parsed);
      }
      if (Array.isArray(parsed) && rcaLooksLikePayloadList(parsed)) {
        return { _array_root: parsed, _note: 'model returned a top-level array; wrapping for diagnostics' };
      }
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
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return promoteWrapper(parsed);
      if (Array.isArray(parsed)) return { _array_root: parsed, _note: 'model returned a top-level array; wrapping for diagnostics' };
    } catch (_e) { /* fall through */ }
  }
  throw new Error('no JSON object found in: ' + s.slice(0, 120));
}
