// minimax.js - call MiniMax M3 vision API and normalize the result.
// Mirrors RLPE range_chart_extractor.extract_range_chart. Never throws:
// returns { ok, data, error, errorKey, raw, truncated }.
'use strict';

// Downscale an image File to a JPEG/PNG data URL whose long edge <= maxEdge.
// Returns { dataUrl, mime, width, height, resized }.
function rcaLoadAndMaybeResize(file, maxEdge) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('imageRead'));
    reader.onload = () => {
      const originalDataUrl = reader.result;
      const img = new Image();
      img.onerror = () => reject(new Error('imageRead'));
      img.onload = () => {
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
        ctx.drawImage(img, 0, 0, nw, nh);
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
    reader.readAsDataURL(file);
  });
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
function rcaNormalizeResult(parsed) {
  const out = {
    sections: [],
    species_ranges: [],
    biozones: [],
    other_fossils: [],
    confidence: 0,
  };
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const SEC_KNOWN = ['name', 'age_range', 'formations', 'formation_thickness_m', 'coordinates'];
  const SP_KNOWN = ['species', 'section', 'range_top', 'range_base', 'biozone'];
  const BZ_KNOWN = ['name', 'age', 'thickness_m'];
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
    };
    const extras = rcaCarryExtras(sp, SP_KNOWN);
    if (extras) row._extras = extras;
    out.species_ranges.push(row);
  }
  for (const bz of Array.isArray(parsed.biozones) ? parsed.biozones : []) {
    if (!bz || typeof bz !== 'object') continue;
    const row = {
      name: asStr(bz.name),
      age: asStr(bz.age),
      thickness_m: asStr(bz.thickness_m),
    };
    const extras = rcaCarryExtras(bz, BZ_KNOWN);
    if (extras) row._extras = extras;
    out.biozones.push(row);
  }
  if (Array.isArray(parsed.other_fossils)) {
    out.other_fossils = parsed.other_fossils.map(asStr).filter((x) => x.trim());
  }
  const conf = Number(parsed.confidence);
  out.confidence = Number.isFinite(conf) ? Math.max(0, Math.min(1, conf)) : 0;
  const rootExtras = rcaCarryExtras(parsed || {}, ROOT_KNOWN);
  if (rootExtras) out._extras = rootExtras;
  return out;
}


// Normalize the parsed columnar-section JSON into the strict result shape.
// Mirrors rca_core.extractor.normalize_columnar_result.
function rcaNormalizeColumnarResult(parsed) {
  const asStr = (v) => (v === null || v === undefined ? '' : String(v));
  const norm = (key) => (Array.isArray(parsed[key]) ? parsed[key] : []);
  const sections = [];
  for (const sec of norm('sections')) {
    if (!sec || typeof sec !== 'object') continue;
    sections.push({
      id: asStr(sec.id),
      group: asStr(sec.group),
      lithology_blocks: Array.isArray(sec.lithology_blocks) ? sec.lithology_blocks : [],
      age_units: Array.isArray(sec.age_units) ? sec.age_units : [],
      samples: Array.isArray(sec.samples) ? sec.samples : [],
      coordinates_text: asStr(sec.coordinates_text),
      thickness_m: asStr(sec.thickness_m),
      confidence_by_section: Math.max(0, Math.min(1, Number(sec.confidence_by_section) || 0)),
    });
  }
  const overall = Number(parsed.overall_confidence);
  return {
    sections,
    fossil_legend: norm('fossil_legend').filter((x) => x && typeof x === 'object'),
    lithology_legend: norm('lithology_legend').filter((x) => x && typeof x === 'object'),
    cross_beds: norm('cross_beds').filter((x) => x && typeof x === 'object'),
    confidence: Number.isFinite(overall) ? Math.max(0, Math.min(1, overall)) : 0,
  };
}

// Backend mode: POST to the same-origin Python server, which performs the
// MiniMax call server-side and returns an already-normalized result.
async function rcaCallBackend(opts, base64) {
  let resp;
  const controller = new AbortController();
  // Allow extra time when the server runs the extraction multiple times.
  const runs = Math.max(1, Math.min(parseInt(opts.runs, 10) || 1, 5));
  const timer = setTimeout(() => controller.abort(), RCA_CONFIG.requestTimeoutMs * runs + 5000);
  try {
    resp = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        api_key: opts.apiKey,
        image_b64: base64,
        media_type: opts.mediaType || 'image/png',
        caption: opts.caption || '',
        chart_lang: opts.chartLang || 'auto',
        endpoint: opts.baseUrl,
        model: opts.model,
        max_tokens: opts.maxTokens,
        runs: runs,
      }),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === 'AbortError') return { ok: false, errorKey: 'err.timeout' };
    return { ok: false, errorKey: 'err.network' };
  }
  clearTimeout(timer);
  let payload;
  try {
    payload = await resp.json();
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse' };
  }
  // The server mirrors the ExtractResult shape with snake_case keys.
  return {
    ok: !!payload.ok,
    data: payload.data,
    errorKey: payload.error_key,
    status: payload.status,
    raw: payload.raw || '',
    truncated: !!payload.truncated,
    // H7: upstream error body so the UI can show 5xx reasons.
    errorBody: payload.error_body || '',
    // M2: how many of the requested runs failed.
    partialFailures: payload.partial_failures || 0,
  };
}

// Main entry. opts: { apiKey, baseUrl, model, maxTokens, proxyUrl, mode,
// dataUrl, mediaType, caption, chartLang }.
// mode defaults to 'range_chart'; 'columnar_section' switches prompt and
// normalizer to the columnar-section variants.
async function extractRangeChart(opts) {
  const mode = (opts && opts.mode) || 'range_chart';
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
  const url = target + '/v1/messages';

  const langHint = (CHART_LANG_HINT && CHART_LANG_HINT[chartLang]) || '';
  const userPrompt =
    'Caption:\n' + (caption && caption.trim() ? caption.trim() : '(no caption)') + '\n\n' +
    langHint +
    (mode === 'columnar_section'
      ? 'Extract the columnar-section information as the strict JSON contract.'
      : 'Extract the geological information as the strict JSON contract.');

  const sysPrompt = mode === 'columnar_section'
    ? (typeof COLUMNAR_SECTION_SYSTEM_PROMPT !== 'undefined' ? COLUMNAR_SECTION_SYSTEM_PROMPT : RANGE_CHART_SYSTEM_PROMPT)
    : RANGE_CHART_SYSTEM_PROMPT;

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

  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === 'AbortError') {
      return { ok: false, errorKey: 'err.timeout' };
    }
    // TypeError from fetch usually means a network/CORS failure.
    return { ok: false, errorKey: 'err.network' };
  }
  clearTimeout(timer);

  if (!resp.ok) {
    let detail = '';
    try { detail = await resp.text(); } catch (_e) { /* ignore */ }
    let errorKey = 'err.http';
    if (resp.status === 401) errorKey = 'err.401';
    else if (resp.status === 403) errorKey = 'err.403';
    else if (resp.status === 429) errorKey = 'err.429';
    return { ok: false, errorKey, status: resp.status, raw: detail };
  }

  let payload;
  try {
    payload = await resp.json();
  } catch (_e) {
    return { ok: false, errorKey: 'err.parse', raw: '' };
  }

  // Extract the first text content block (Anthropic-compatible shape).
  let rawText = '';
  const content = Array.isArray(payload.content) ? payload.content : [];
  for (const c of content) {
    if (c && c.type === 'text') {
      rawText = c.text || '';
      break;
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

  const data = (mode === 'columnar_section' && typeof rcaNormalizeColumnarResult === 'function')
    ? rcaNormalizeColumnarResult(parsed)
    : rcaNormalizeResult(parsed);
  return { ok: true, data, raw: rawText, truncated };
}
