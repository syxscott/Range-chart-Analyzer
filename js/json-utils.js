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

// Lenient JSON object parse. Strips markdown fences, tries strict parse,
// then falls back to the first balanced {...} object. Throws on failure.
function safeJsonLoads(text) {
  if (!text) throw new Error('empty text');
  let s = String(text).trim();
  // Strip leading/trailing markdown code fences.
  s = s.replace(/^```(?:json)?\s*/gm, '');
  s = s.replace(/\s*```$/gm, '');
  try {
    const parsed = JSON.parse(s);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed;
    }
  } catch (_e) {
    /* fall through to balanced-object extraction */
  }
  const candidate = extractBalancedJsonObject(s);
  if (candidate !== null) {
    return JSON.parse(candidate);
  }
  throw new Error('no JSON object found in: ' + s.slice(0, 120));
}
