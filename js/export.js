// export.js - copy TSV to clipboard, download CSV / JSON.
'use strict';

// Escape one CSV cell: wrap in quotes if it contains comma, quote, or newline.
function rcaCsvCell(value) {
  const s = value === null || value === undefined ? '' : String(value);
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
  const clean = (v) => (v === null || v === undefined ? '' : String(v).replace(/[\t\r\n]+/g, ' '));
  const lines = [headers.map(clean).join('\t')];
  for (const row of rows) {
    lines.push(row.map(clean).join('\t'));
  }
  return lines.join('\n');
}

// Trigger a file download from a text blob.
function rcaDownload(filename, text, mime) {
  // Prepend a UTF-8 BOM for CSV so Excel reads CJK/Cyrillic correctly.
  const needsBom = (mime || '').indexOf('csv') !== -1;
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
