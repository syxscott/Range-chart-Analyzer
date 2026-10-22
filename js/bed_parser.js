// js/bed_parser.js - mirror of rca_core/bed_parser.py.
//
// AUDIT-2026-10-22: the endpoint-label/index contradiction repair
// (rca_core/extractor.py:2345-2358) needs a LABEL ORDINAL, and
// rca_core/bed_parser.py is the only place that knows how to read one. This
// module is the JS side of it, so the two engines can answer the same
// question about the same label.
//
// It used to live nowhere in JS. The consequence was not a missing feature:
// rca_core/extractor.py SWAPPED a species row's range_top / range_base when
// the printed labels and the model-assigned indices disagreed, and
// js/minimax.js did not - so the desktop app published the corrected
// endpoints and the browser published the inverted ones, for the same model
// response. The browser did not notice either: js/quality.js's FAD/LAD check
// needs a NUMBER out of the label, "A5 (606 m)" yields null, and the row was
// never checked at all.
//
// This file is now the SINGLE JS authority for bed-label parsing: the
// strict parser moved here from js/quality.js (which delegates to it), so
// there is one implementation rather than two that can drift.
//
// The patterns are transcribed from rca_core/bed_parser.py. Two
// Python/JS dialect differences are handled explicitly:
//   * Python's `\d` is Unicode-aware, JS's is ASCII-only, so the digit
//     classes are [\p{Nd}] and the digits are folded to ASCII before
//     parseInt (a match alone is not enough: parseInt("９") is NaN).
//   * Python's `int(float(x))` truncates toward zero; Math.trunc does too.
//
// tests/test_bed_parser_parity.py drives both sides over the same label set
// and fails if any answer differs.
'use strict';

// --- patterns (transcribed from rca_core/bed_parser.py) --------------------
const RCA_BED_DIGITS = '[\\p{Nd}]';
const RCA_BED_FULL_RE = new RegExp('^Bed\\s*(' + RCA_BED_DIGITS + '+)\\s*([a-zA-Z]*)', 'iu');
const RCA_BARE_BED_RE = new RegExp('^(' + RCA_BED_DIGITS + '+)\\s*([a-zA-Z]*)', 'u');
const RCA_BED_UNIT_RE = /^(ma|myr|mya|m\.y\.|m\.y\.?|ka|kyr|ga|gyr|yr|cm|mm|km|ft|a)$/i;
const RCA_BED_SUB_RE = /^[a-ln-z]$/i;

const RCA_LABEL_TRAILING_UNIT_RE = /(?:ma|myr|ka|kyr|m\b|cm|mm|ft|in)\s*$/i;
const RCA_ANY_NUM_RE = new RegExp(RCA_BED_DIGITS + '+(?:\\.' + RCA_BED_DIGITS + '+)?', 'gu');
const RCA_BARE_DECIMAL_RE = new RegExp('^' + RCA_BED_DIGITS + '\\.' + RCA_BED_DIGITS + '$', 'u');
const RCA_LETTER_PREFIX_RE = /^[A-Za-z]{1,4}[-_. ]?\d/;
const RCA_DASHED_NUMERIC_RANGE_RE =
  new RegExp(RCA_BED_DIGITS + '+(?:\\.' + RCA_BED_DIGITS + '+)?\\s*[–—]\\s*'
    + RCA_BED_DIGITS + '+(?:\\.' + RCA_BED_DIGITS + '+)?', 'u');
const RCA_ASCII_RANGE_RE = new RegExp(
  '\\b' + RCA_BED_DIGITS + '+(?:\\.' + RCA_BED_DIGITS + '+)?\\s*-\\s*'
  + '(?:bed|beds|sample|specimen)?\\s*' + RCA_BED_DIGITS + '+(?:\\.' + RCA_BED_DIGITS + '+)?\\b', 'iu');
const RCA_LETTERED_RANGE_RE = /\b([A-Za-z])-?(\d+)\s*-\s*\1-?(\d+)\b/;
const RCA_WORD_RE = /[A-Za-z]{2,}/g;

//: Words that assert the number belongs to something OTHER than the sample's
//: position. rca_core/bed_parser.py:_NON_POSITION_WORDS.
const RCA_NON_POSITION_WORDS = new Set([
  'zone', 'zones', 'zonation', 'biozone', 'biozones',
  'fig', 'figs', 'figure', 'table', 'plate', 'plates',
  'section', 'sections', 'sect',
  'formation', 'formations', 'fm', 'fmts',
  'stage', 'stages',
]);

// --- helpers ---------------------------------------------------------------

/**
 * Fold a run of Unicode decimal digits to ASCII, so parseInt can read it.
 *
 * The bed regexes match the same digits Python's \d does, and that is only
 * half of it -- parseInt("９") is NaN, so a match alone would produce
 * bed_num: NaN. NFKC covers the compatibility digits (full-width ９ -> 9, the
 * case that matters for a Chinese-language figure); the remaining Nd scripts
 * (Arabic-Indic, Devanagari, ...) are not compatibility-equivalent and are
 * folded by subtracting their block's zero code point, found by probing
 * rather than from a table.
 */
function rcaAsciiDigits(s) {
  const nfkc = String(s).normalize('NFKC');
  let out = '';
  for (const ch of nfkc) {
    const cp = ch.codePointAt(0);
    if (cp >= 0x30 && cp <= 0x39) { out += ch; continue; }
    if (!/\p{Nd}/u.test(ch)) { out += ch; continue; }
    // Find z such that z..z+9 are all Nd and ch sits inside it: the block zero.
    let folded = ch;
    for (let z = cp; z > cp - 10; z -= 1) {
      let all = true;
      for (let d = 0; d <= 9; d += 1) {
        if (!/\p{Nd}/u.test(String.fromCodePoint(z + d))) { all = false; break; }
      }
      if (all) { folded = String.fromCodePoint(0x30 + (cp - z)); break; }
    }
    out += folded;
  }
  return out;
}

function _rcaSubIsBedLabel(sub, hadBedWord) {
  if (!sub) return true;                       // bare number after an explicit "Bed"
  if (sub.toLowerCase() === 'm') return false; // metres, not a subscript
  if (sub.length > 1 && RCA_BED_UNIT_RE.test(sub)) return false;
  if (hadBedWord) return true;
  return RCA_BED_SUB_RE.test(sub);
}

function _rcaBedStrayResidue(s, end) {
  const rest = s.slice(end).trim();
  return Boolean(rest) && rest.charAt(0) !== '(' && rest.charAt(0) !== '[';
}

function _rcaHasForeignSemanticWord(s) {
  const words = s.match(RCA_WORD_RE) || [];
  return words.some(function (w) { return RCA_NON_POSITION_WORDS.has(w.toLowerCase()); });
}

function _rcaIsRangeLabel(s) {
  return RCA_DASHED_NUMERIC_RANGE_RE.test(s)
    || RCA_ASCII_RANGE_RE.test(s)
    || RCA_LETTERED_RANGE_RE.test(s);
}

function _rcaAllNums(s) {
  return s.match(RCA_ANY_NUM_RE) || [];
}

function _rcaTruncFloat(text) {
  const f = parseFloat(rcaAsciiDigits(text));
  if (Number.isNaN(f) || !Number.isFinite(f)) return null;
  return Math.trunc(f);
}

// --- parse_bed (mirror of rca_core/bed_parser.py:parse_bed) ---------------

function rcaParseBedCanonical(value) {
  if (value === undefined || value === null || value === '') return null;
  if (typeof value === 'boolean') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return { bed_num: Math.trunc(value), bed_sub: '', raw: String(value) };
  }
  const s = String(value).trim();
  if (!s) return null;
  let m = RCA_BED_FULL_RE.exec(s);
  if (m) {
    const sub = m[2];
    if (!_rcaSubIsBedLabel(sub, true)) return null;
    if (_rcaBedStrayResidue(s, m[0].length)) return null;
    return { bed_num: parseInt(rcaAsciiDigits(m[1]), 10), bed_sub: sub.toLowerCase(), raw: s };
  }
  m = RCA_BARE_BED_RE.exec(s);
  if (m) {
    const sub = m[2];
    if (!_rcaSubIsBedLabel(sub, false)) return null;
    if (_rcaBedStrayResidue(s, m[0].length)) return null;
    return { bed_num: parseInt(rcaAsciiDigits(m[1]), 10), bed_sub: sub.toLowerCase(), raw: s };
  }
  return null;
}

// --- label_ordinal (mirror of rca_core/bed_parser.py:label_ordinal) --------

// The position number a sample/bed label carries, or null.
//
//     "A1" / "A21"      -> 1 / 21   letter-prefixed sample
//     "Sample 3"       -> 3        word-prefixed
//     "87 B1 4C"       -> 4        LAST number: the bed, not the section
//     "Bed 23" / "23c" -> 23       what parse_bed already handles
//     "1.7"            -> 1        a Ma value: monotonic, usable
//     "607 m"          -> null     a DEPTH, not an ordinal
//     "1.7 Ma"         -> null     an AGE carries a unit
//     "" / "zone Z1"   -> null     nothing to read
//     "23-25"          -> null     a RANGE has no single position
//
// Callers MUST read null as "cannot check", never as "not inverted".
function rcaLabelOrdinal(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'boolean') return null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return Math.trunc(value);
  }
  const s = String(value).trim();
  if (!s) return null;

  const info = rcaParseBedCanonical(s);
  if (info !== null) return info.bed_num;

  if (RCA_LABEL_TRAILING_UNIT_RE.test(s)) return null;
  if (_rcaHasForeignSemanticWord(s)) return null;
  if (_rcaIsRangeLabel(s)) return null;
  if (RCA_BARE_DECIMAL_RE.test(s)) return _rcaTruncFloat(s);

  // A parenthesised tail is an ANNOTATION: "A1 (607 m)" is sample A1 with
  // its depth, so the ordinal is whatever precedes the bracket.
  if (s.indexOf('(') !== -1) {
    const head = s.split('(')[0];
    const nums = _rcaAllNums(head);
    return nums.length ? _rcaTruncFloat(nums[0]) : null;
  }

  const nums = _rcaAllNums(s);
  if (!nums.length) return null;
  return _rcaTruncFloat(nums[nums.length - 1]);
}

// --- ordinal_kind (mirror of rca_core/bed_parser.py:ordinal_kind) ----------

// Coarse family of an axis label: "letter" (A1, Sample 3), "number"
// (Bed 23, 23c, 4) or null. Two endpoints are only compared when they could
// plausibly sit on the same axis.
function rcaOrdinalKind(value) {
  if (value === undefined || value === null) return null;
  const s = String(value).trim();
  if (!s) return null;
  if (RCA_LABEL_TRAILING_UNIT_RE.test(s)) return null;
  if (rcaParseBedCanonical(s) !== null) return 'number';
  if (RCA_LETTER_PREFIX_RE.test(s) || /\b[A-Za-z]{3,}\b/.test(s)) return 'letter';
  if (/\d/.test(s)) return 'number';
  return null;
}

globalThis.RCA_BED_PARSER = {
  parseBed: rcaParseBedCanonical,
  labelOrdinal: rcaLabelOrdinal,
  ordinalKind: rcaOrdinalKind,
  asciiDigits: rcaAsciiDigits,
};
if (typeof window !== 'undefined') {
  window.RCA_BED_PARSER = globalThis.RCA_BED_PARSER;
}
