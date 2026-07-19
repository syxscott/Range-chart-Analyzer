// json-utils.js - lenient JSON extraction, ported from RLPE
// range_chart_extractor._extract_balanced_json_object / _safe_json_loads.
'use strict';

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

// Lenient JSON object parse. Strips markdown fences, strips control chars
// (mirroring rca_core.json_utils.safe_json_loads so both ends survive
// raw 0x01 bytes in model string values), tries strict parse, then falls
// back to a top-level array wrapper or the first balanced {...} object.
// Throws on failure.
function safeJsonLoads(text) {
  if (!text) throw new Error('empty text');
  let s = String(text).trim();
  // Strip leading/trailing markdown code fences.
  s = s.replace(/^```(?:json)?\s*/gm, '');
  s = s.replace(/\s*```$/gm, '');
  // Strip the same control chars Python's safe_json_loads strips. Some
  // model providers emit raw 0x01 bytes inside string values; without
  // this JSON.parse throws on otherwise-valid JSON.
  s = s.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '');
  try {
    const parsed = JSON.parse(s);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed;
    }
    // Top-level array: wrap it so downstream code can use object semantics,
    // matching Python's {_array_root: [...]} wrapper.
    if (Array.isArray(parsed)) {
      return { _array_root: parsed };
    }
  } catch (_e) {
    /* fall through to balanced-object extraction */
  }
  const candidate = extractBalancedJsonObject(s);
  if (candidate !== null) {
    return JSON.parse(candidate);
  }
  // Mirror Python's array fallback: recover a top-level array wrapped in
  // the synthetic {_array_root} shape so downstream code (which assumes an
  // object) can introspect it instead of throwing.
  const arrCandidate = extractBalancedJsonArray(s);
  if (arrCandidate !== null) {
    const parsedArr = JSON.parse(arrCandidate);
    if (Array.isArray(parsedArr)) {
      return { _array_root: parsedArr };
    }
  }
  throw new Error('no JSON object found in: ' + s.slice(0, 120));
}
