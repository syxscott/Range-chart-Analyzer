// export.js - copy TSV to clipboard, download CSV / JSON.
'use strict';

// Sanitize one CSV/TSV cell against formula injection (OWASP): prefix a
// single quote when the cell starts with a formula trigger (= + - @) so
// Excel/LibreOffice treats it as text instead of executing =CMD(...) etc.
function rcaFormulaSafe(value) {
  const s = value === null || value === undefined ? '' : String(value);
  if (s && (s[0] === '=' || s[0] === '+' || s[0] === '-' || s[0] === '@')) {
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
  const blob = new Blob(parts, { type: (mime || 'text/plain') + ';charset=utf-8' });
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
