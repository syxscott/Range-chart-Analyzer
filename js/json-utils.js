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

// Lenient JSON object parse with a 6-level fallback chain.
// Chain (each runs only if prior failed):
//   1. Strip markdown fences (```json ... ```)
//   2. Strip raw control chars (0x00-0x1F except \t\r\n)
//   3. Strict JSON.parse (incl. top-level arrays → wrapped)
//   4. Balanced-brace object extraction ({...})
//   5. Balanced-bracket array extraction ([...]) → wrapped
//   6. Prose-embedded JSON (extractJsonLike) — last resort
// Throws on failure.
function safeJsonLoads(text) {
  if (!text) throw new Error('empty text');
  // Level 1: strip markdown fences.
  let s = stripMarkdownFence(String(text).trim());
  // Level 2: strip raw control chars that JSON.parse rejects.
  s = s.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '');
  // Level 3: strict parse.
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
  // Level 4: balanced-brace object extraction.
  const candidate = extractBalancedJsonObject(s);
  if (candidate !== null) {
    try { return JSON.parse(candidate); } catch (_e) { /* fall through */ }
  }
  // Level 5: balanced-bracket array extraction → wrapped.
  const arrCandidate = extractBalancedJsonArray(s);
  if (arrCandidate !== null) {
    try {
      const parsedArr = JSON.parse(arrCandidate);
      if (Array.isArray(parsedArr)) return { _array_root: parsedArr };
    } catch (_e) { /* fall through */ }
  }
  // Level 6: prose-embedded JSON (last resort).
  const prose = extractJsonLike(s);
  if (prose !== null) {
    try {
      const parsed = JSON.parse(prose);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
      if (Array.isArray(parsed)) return { _array_root: parsed };
    } catch (_e) { /* fall through */ }
  }
  throw new Error('no JSON object found in: ' + s.slice(0, 120));
}
