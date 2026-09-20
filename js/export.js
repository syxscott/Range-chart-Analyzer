// export.js - copy TSV to clipboard, download CSV / JSON.
'use strict';

// Sanitize one CSV/TSV cell against formula injection (OWASP): prefix a
// single quote when the cell starts with a formula trigger (= + - @) so
// Excel/LibreOffice treats it as text instead of executing =CMD(...) etc.
// FIX: also neutralize leading TAB (\t) and CR (\r). Some spreadsheet
// engines treat TAB as a separator reset, and a leading CR can hide the
// trigger from a plain equals/plus check while still being interpreted
// as a formula when pasted into Excel's formula bar.
// M12 (REVIEW-2026-08-19): also catch leading LF (\n). Some spreadsheet
// engines (Excel for Mac in particular) treat a leading newline as a
// formula-bar prefix that lets the trigger character escape the OWASP
// guard. Prepending ' alongside \n closes that loophole.
function rcaFormulaSafe(value) {
  const s = value === null || value === undefined ? '' : String(value);
  if (!s) return s;
  const c = s[0];
  if (c === '=' || c === '+' || c === '-' || c === '@' ||
      c === '\t' || c === '\r' || c === '\n') {
    return "'" + s;
  }
  return s;
}

// Escape one CSV cell: wrap in quotes if it contains comma, quote, or newline.
function rcaCsvCell(value) {
  const s = rcaFormulaSafe(value);
  if (/[",\n\r]/.test(s)) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

// Build a CSV string from headers + rows (array of arrays).
function rcaToCsv(headers, rows) {
  const lines = [];
  lines.push(headers.map(rcaCsvCell).join(','));
  for (const row of rows) {
    lines.push(row.map(rcaCsvCell).join(','));
  }
  return lines.join('\r\n');
}

// Build a TSV string (tabs). Newlines/tabs inside cells are collapsed to spaces
// so a single record stays on one line when pasted into a spreadsheet.
function rcaToTsv(headers, rows) {
  const clean = (v) => {
    const safe = rcaFormulaSafe(v);
    return safe.replace(/[\t\r\n]+/g, ' ');
  };
  const lines = [headers.map(clean).join('\t')];
  for (const row of rows) {
    lines.push(row.map(clean).join('\t'));
  }
  return lines.join('\n');
}

// Trigger a file download from a text blob.
function rcaDownload(filename, text, mime) {
  // Prepend a UTF-8 BOM for CSV / TSV so Excel reads CJK/Cyrillic
  // correctly when the user opens the file with double-click. Without
  // the BOM, Excel interprets the file as the system code page (usually
  // Windows-1252) and turns CJK / Cyrillic characters into mojibake.
  const lowerMime = (mime || '').toLowerCase();
  const needsBom = lowerMime.indexOf('csv') !== -1 || lowerMime.indexOf('tsv') !== -1;
  const parts = needsBom ? ['﻿', text] : [text];
  // Only append the charset when the caller did not already supply one —
  // otherwise a caller passing "text/csv;charset=utf-8" produced the
  // malformed "text/csv;charset=utf-8;charset=utf-8".
  let finalMime = mime || 'text/plain';
  if (finalMime.toLowerCase().indexOf('charset') === -1) {
    finalMime += ';charset=utf-8';
  }
  const blob = new Blob(parts, { type: finalMime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke on next tick so the download has a chance to start.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Copy text to clipboard with a legacy fallback. Returns a Promise<boolean>.
async function rcaCopyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_e) {
    /* fall through to legacy path */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (_e) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// WebPlotDigitizer exchange — pure scalar helpers (BORROW-2026-09-20)
// ---------------------------------------------------------------------------
// Mirrors of the three scalar primitives of rca_core/exporter.py's to_wpd
// (format "rca-wpd/1"): _wpd_num, _wpd_num_text and _wpd_slug. The full
// rcaToWpd bundle builder is deliberately NOT mirrored here yet: the
// differential parity fixture (tests/gen_frontend_parity_fixtures.py) covers
// the extraction pipeline only, and neither index.html nor js/app.js has a
// WPD/PBDB export entry point, so there is no browser call site to keep
// honest. These helpers ARE pinned: tests_export_parity.js replays the case
// table below through them, and
// tests/test_export_js_mirror_smoke_2026_09_20.py recomputes the SAME table
// against the Python originals, so the two engines cannot drift apart before
// the builder ever lands.
//
// Determinism contract with exporter.py: integral floats collapse to an
// integer ("2.0" must serialise as "2" — JSON.stringify has no 2.0 spelling),
// non-integral values round through toFixed(6) exactly like Python's
// round(f, 6) + "%f" formatting, and nothing that Python's float() would
// reject is ever turned into a number ("common" -> "", never 0).

// Python float() semantics for a STRING input — deliberately NOT Number():
// Number("") is 0, Number("0x10") is 16, and both would invent points the
// Python export drops. Same rule as _RCA_PY_FLOAT_RE in js/minimax.js (kept
// as a local copy because export.js must load standalone in tests_export_parity.js).
const _RCA_WPD_PY_FLOAT_RE = /^[+-]?(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?$/;
const _RCA_WPD_INF_NAN_RE = /^([+-]?)(inf|infinity|nan)$/i;

function _rcaWpdPyFloat(value) {
  // Returns a number (possibly NaN/±Infinity — the caller drops non-finite
  // values just like _wpd_num's isnan/isinf guard) or null where Python's
  // float() would raise TypeError/ValueError.
  if (typeof value === 'number') return value;
  if (typeof value !== 'string') return null;   // dict / list / None -> raise
  const s = value.trim();
  if (!s) return null;                          // float("") raises
  const m = _RCA_WPD_INF_NAN_RE.exec(s);
  if (m) {
    if (m[2].toLowerCase() === 'nan') return NaN;
    return m[1] === '-' ? -Infinity : Infinity;
  }
  if (!_RCA_WPD_PY_FLOAT_RE.test(s)) return null;
  return Number(s.replace(/_/g, ''));           // "1_000" -> 1000, like float()
}

// Mirror of _wpd_num: the number that goes into wpd_axes.json, or null.
function rcaWpdNum(value) {
  if (value === null || value === undefined || typeof value === 'boolean') {
    return null;                                // bool is NOT a number here
  }
  const f = _rcaWpdPyFloat(value);
  if (f === null || Number.isNaN(f) || !Number.isFinite(f)) return null;
  if (Number.isInteger(f)) return f;            // 2.0 collapses to 2
  return Number(f.toFixed(6));                  // exporter.py: round(f, 6)
}

// Mirror of _wpd_num_text: CSV text for one point — never "2.0", never nan.
// NOTE: magnitudes >= 1e21 would print exponent-style in JS where Python's
// %f expands them; no stratigraphic value ever gets near that, and the
// case table in tests_export_parity.js pins every range that DOES occur.
function rcaWpdNumText(value) {
  const n = rcaWpdNum(value);
  if (n === null) return '';
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
}

// Mirror of _wpd_slug: filename/dataset token — letters, digits, dot, dash,
// underscore; every other run collapses to "_". (String(text || '') matches
// Python's str(text or "") for the falsy spellings the callers pass.)
function rcaWpdSlug(text, fallback) {
  const fb = fallback === undefined || fallback === null ? 'dataset' : String(fallback);
  let s = String(text || '').trim();
  s = s.replace(/[^A-Za-z0-9._\-]+/g, '_');
  s = s.replace(/^[._-]+/, '').replace(/[._-]+$/, '');
  return s || fb;
}
