// app.js - orchestration: settings, upload, extract, render, export, i18n.
'use strict';

(function () {
  'use strict';

  // Current in-memory state.
  const state = {
    file: null,        // original File
    dataUrl: null,     // (possibly resized) data URL sent to the API
    mediaType: null,
    result: null,      // last normalized result
    rawText: null,     // last raw model response
    busy: false,
    // FR1/FR2: monotonically increasing token bumped on every "user action"
    // (file change, reset, extract start). Any async result whose token
    // doesn't match `state.expectedToken` is dropped. This makes last-write
    // wins for async loads and prevents in-flight extraction from clobbering
    // a reset.
    expectedToken: 0,
    loadToken: 0,         // captured inside handleFile's load promise
    extractToken: 0,      // captured inside runExtraction's main promise
  };

  // ---- element helpers ----
  const $ = (id) => document.getElementById(id);

  // Resolve the connection mode. "auto" picks backend when the page is
  // served over http/https (i.e. by server.py), else direct browser call.
  function rcaResolveMode() {
    const sel = $('conn-mode');
    const choice = sel ? sel.value : 'auto';
    if (choice === 'backend') return 'backend';
    if (choice === 'direct') return 'direct';
    // auto
    const proto = location.protocol;
    return (proto === 'http:' || proto === 'https:') ? 'backend' : 'direct';
  }

  // Auto-detect the chart extraction mode from caption + filename.
  // Heuristic only — if the user knows better, they can hide the chart
  // entirely and let the model decide (range_chart is the safe fallback).
  // Mirrors the GUI's caption-based heuristic in gui.py.
  function rcaAutoDetectChartMode() {
    const cap = ($('caption') && $('caption').value || '').toLowerCase();
    const fileName = (state.file && state.file.name || '').toLowerCase();
    const blob = cap + ' ' + fileName;
    // Columnar keywords (English + Japanese / Chinese tokens).
    const keys = ['column', 'columns', 'columnar', 'col_section', 'col_sections', '柱状', '柱状図', '柱状图'];
    for (const k of keys) {
      if (blob.indexOf(k) !== -1) return 'columnar_section';
    }
    return 'range_chart';
  }

  function toast(msg) {
    const el = $('toast');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.remove('show'), 2200);
  }

  // ---- settings persistence ----
  function loadSettings() {
    $('endpoint').value = rcaStoreGet(RCA_STORE.endpoint, RCA_CONFIG.defaultEndpoint);
    $('model').value = rcaStoreGet(RCA_STORE.model, RCA_CONFIG.defaultModel);
    $('max-tokens').value = rcaStoreGet('rca.maxTokens', String(RCA_CONFIG.defaultMaxTokens));
    $('proxy').value = rcaStoreGet(RCA_STORE.proxy, '');
    $('conn-mode').value = rcaStoreGet(RCA_STORE.mode, 'auto');
    $('max-edge').value = rcaStoreGet(RCA_STORE.maxEdge, String(RCA_CONFIG.maxImageEdge));
    $('runs').value = rcaStoreGet(RCA_STORE.runs, '1');
    const remember = rcaStoreGet(RCA_STORE.rememberKey, '') === '1';
    const rk0 = $('remember-key');
    if (rk0) rk0.setAttribute('aria-checked', remember ? 'true' : 'false');
    if (remember) {
      $('api-key').value = rcaStoreGet(RCA_STORE.apiKey, '');
    }
    // Phase C: sync segmented + range outputs to loaded values.
    syncSegmentedFromSelect();
    syncRangeOutputs();
  }

  function saveSettings() {
    // FR3: collect per-write results so we can warn when localStorage is
    // unavailable (private mode / quota exceeded) instead of falsely
    // claiming "settings saved".
    const writes = [
      rcaStoreSet(RCA_STORE.endpoint, $('endpoint').value.trim() || RCA_CONFIG.defaultEndpoint),
      rcaStoreSet(RCA_STORE.model, $('model').value.trim() || RCA_CONFIG.defaultModel),
      rcaStoreSet('rca.maxTokens', $('max-tokens').value.trim() || String(RCA_CONFIG.defaultMaxTokens)),
      rcaStoreSet(RCA_STORE.proxy, $('proxy').value.trim()),
      rcaStoreSet(RCA_STORE.mode, $('conn-mode').value),
      rcaStoreSet(RCA_STORE.maxEdge, $('max-edge').value.trim() || String(RCA_CONFIG.maxImageEdge)),
      rcaStoreSet(RCA_STORE.runs, $('runs').value.trim() || '1'),
    ];
    // Phase C: remember-key is a switch button; read aria-checked.
    const rk = $('remember-key');
    const remember = rk ? rk.getAttribute('aria-checked') === 'true' : false;
    writes.push(rcaStoreSet(RCA_STORE.rememberKey, remember ? '1' : ''));
    if (remember) {
      writes.push(rcaStoreSet(RCA_STORE.apiKey, $('api-key').value.trim()));
    } else {
      writes.push(rcaStoreSet(RCA_STORE.apiKey, ''));
    }
    // FR3: if ANY write failed (private mode, quota, blocked storage)
    // — surface a real warning. Otherwise the success toast is honest.
    if (writes.every((w) => w !== false)) {
      toast(t('settings.saved'));
    } else {
      toast(t('settings.saveFailed'));
    }
  }

  // Phase C: keep the segmented control buttons in sync with the hidden
  // <select> that remains the source of truth for rcaResolveMode().
  function syncSegmentedFromSelect() {
    const seg = $('conn-mode-seg');
    const sel = $('conn-mode');
    if (!seg || !sel) return;
    seg.querySelectorAll('button[role=radio]').forEach((b) => {
      b.setAttribute('aria-checked', b.getAttribute('data-value') === sel.value ? 'true' : 'false');
    });
  }

  // Phase C: range inputs need their <output> readouts updated when loaded
  // programmatically (the input event only fires on user interaction).
  function syncRangeOutputs() {
    [['max-tokens', 'max-tokens-out'], ['max-edge', 'max-edge-out'],
     ['runs', 'runs-out']].forEach(([srcId, outId]) => {
      const inp = $(srcId), out = $(outId);
      if (inp && out) out.textContent = inp.value;
    });
  }

  // ---- alerts ----
  function clearAlert() {
    $('alert-slot').innerHTML = '';
  }

  function showAlert(kind, message, rawDetail) {
    const slot = $('alert-slot');
    let html = '<div class="alert alert-' + kind + '"><div>' + rcaEsc(message);
    if (rawDetail) {
      html += '<pre>' + rcaEsc(String(rawDetail).slice(0, 4000)) + '</pre>';
    }
    html += '</div></div>';
    slot.innerHTML = html;
  }

  // ---- file handling ----
  function humanSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
  }

  async function handleFile(file) {
    if (!file || !file.type.startsWith('image/')) {
      showAlert('warning', t('err.noImage'));
      return;
    }
    state.file = file;
    // FR1: claim a load token. If the user picks another file before
    // this one's load promise resolves, the new call bumps the token
    // and our async continuation will see a mismatch and silently drop.
    state.expectedToken += 1;
    const myToken = state.expectedToken;
    state.loadToken = myToken;
    clearAlert();
    try {
      const edgeEl = $('max-edge');
      let maxEdge = parseInt(edgeEl && edgeEl.value, 10);
      if (!Number.isFinite(maxEdge) || maxEdge < 0) maxEdge = RCA_CONFIG.maxImageEdge;
      const loaded = await rcaLoadAndMaybeResize(file, maxEdge);
      if (myToken !== state.loadToken) {
        // A newer file-selection has superseded us; drop this result.
        return;
      }
      state.dataUrl = loaded.dataUrl;
      state.mediaType = loaded.mime;
      // preview
      $('preview-img').src = loaded.dataUrl;
      const meta = [];
      meta.push('<div><strong>' + rcaEsc(file.name) + '</strong></div>');
      meta.push('<div>' + rcaEsc(t('upload.fileSize')) + ': ' + humanSize(file.size) + '</div>');
      meta.push('<div>' + rcaEsc(t('upload.fileDims')) + ': ' + loaded.width + ' x ' + loaded.height +
        (loaded.resized ? ' ' + rcaEsc(t('upload.resized')) : '') + '</div>');
      $('preview-meta').innerHTML = meta.join('');
      $('preview-wrap').classList.remove('hidden');
    } catch (err) {
      if (myToken === state.loadToken) {
        showAlert('danger', t('err.imageRead'));
      }
    }
  }

  // ---- extraction flow ----
  function setBusy(busy) {
    state.busy = busy;
    const btn = $('extract-btn');
    btn.disabled = busy;
    if (busy) {
      // M4: keep the data-i18n hook on the inner span so a language switch
      // mid-extraction still re-translates the spinner label.
      btn.innerHTML = '<span class="spinner"></span><span data-i18n="upload.extract">' + rcaEsc(t('upload.extract')) + '</span>';
    } else {
      // Keep the data-i18n hook so a later language switch re-translates it.
      btn.innerHTML = '<span id="extract-btn-label" data-i18n="upload.extract">' + rcaEsc(t('upload.extract')) + '</span>';
    }
    $('loading-slot').classList.toggle('hidden', !busy);
    if (busy) {
      $('results-empty').classList.add('hidden');
      $('results-content').classList.add('hidden');
    }
  }

  async function runExtraction() {
    if (state.busy) return;
    const apiKey = $('api-key').value.trim();
    if (!apiKey) {
      showAlert('warning', t('err.noKey'));
      return;
    }
    if (!state.dataUrl) {
      showAlert('warning', t('err.noImage'));
      return;
    }
    clearAlert();
    // M42: persist any settings edits the user made since the last explicit
    // Save so the in-flight extraction always uses the values currently in
    // the form (rather than the last saved snapshot).
    saveSettings();
    // FR2: bump token so any concurrent Reset / new file selection
    // invalidates this in-flight request.
    state.expectedToken += 1;
    state.extractToken = state.expectedToken;
    const myToken = state.extractToken;
    setBusy(true);

    // FR4: wrap the body in try/finally so an unexpected exception
    // (e.g. a provider throwing synchronously) can never leave the UI
    // stuck in the busy state.
    let res;
    try {
      const maxTokens = rcaClampMaxTokens($('max-tokens').value);
      const connMode = rcaResolveMode();  // 'backend' | 'direct' (transport)
      const chartMode = rcaAutoDetectChartMode();  // 'range_chart' | 'columnar_section' (chart type)
      const runsEl = $('runs');
      const runs = Math.max(1, Math.min(parseInt(runsEl && runsEl.value, 10) || 1, 5));
      // transport: where the LLM call actually runs (browser vs same-origin backend).
      // mode:      what kind of chart we're extracting (range_chart vs columnar_section).
      // The two are decoupled — see C1 in code-review notes for the historical collision.
      const baseOpts = {
        apiKey,
        baseUrl: $('endpoint').value.trim() || RCA_CONFIG.defaultEndpoint,
        model: $('model').value.trim() || RCA_CONFIG.defaultModel,
        maxTokens,
        proxyUrl: $('proxy').value.trim(),
        mode: chartMode,
        transport: connMode,
        dataUrl: state.dataUrl,
        mediaType: state.mediaType,
        caption: $('caption').value,
        chartLang: $('chart-lang').value,
      };

      // Phase 1: analyze (LLM call). Single-run replaces setBusyLabel
      // with the per-run counter; multi-run keeps it generic.
      setBusyLabel(runs > 1 ? t('loading.analyzing') : t('loading.analyzing'));

      if (connMode === 'backend') {
        // The server performs the N runs and merges; pass runs through.
        res = await extractRangeChart(Object.assign({}, baseOpts, { runs }));
      } else if (runs > 1) {
        // Direct/proxy mode: fire N requests CONCURRENTLY via Promise.all
        // so total wait is ~one latency rather than N. Each promise resolves
        // independently; a failure in one run doesn't cancel the others.
        setBusyLabel(t('loading.text') + ' (0/' + runs + ')');
        const km = chartMode === 'columnar_section' ? RCA_COLUMNAR_KEYMAP : RCA_DEFAULT_KEYMAP;
        const promises = [];
        for (let i = 0; i < runs; i++) {
          promises.push(
            extractRangeChart(baseOpts).catch((exc) => ({
              ok: false, errorKey: 'err.network', raw: String(exc),
            }))
          );
        }
        // Track completion count for the per-run label.
        let completed = 0;
        const tracked = promises.map((p) => p.then((r) => {
          completed += 1;
          setBusyLabel(t('loading.text') + ' (' + completed + '/' + runs + ')');
          return r;
        }));
        const results = await Promise.all(tracked);
        // Phase 2: aggregating.
        setBusyLabel(t('loading.aggregating'));
        const okDatas = [];
        let lastFail = null;
        let anyTruncated = false;
        let partialFails = 0;
        const raws = [];
        for (const r of results) {
          if (r.ok && r.data) {
            okDatas.push(r.data);
            anyTruncated = anyTruncated || !!r.truncated;
            if (r.raw) raws.push(r.raw);
          } else {
            lastFail = r;
            partialFails += 1;
          }
        }
        if (okDatas.length === 0) {
          res = lastFail || { ok: false, errorKey: 'err.empty' };
        } else {
          // M2: surface "some runs failed" via truncated flag and a dedicated
          // count so the UI can show "M of N runs failed".
          if (partialFails > 0) anyTruncated = true;
          res = {
            ok: true,
            data: rcaMergeResults(okDatas, runs, km),
            raw: raws.join('\n---RUN---\n').slice(0, 8000),
            truncated: anyTruncated,
            partialFailures: partialFails,
          };
        }
      } else {
        res = await extractRangeChart(baseOpts);
      }
    } finally {
      // FR4: always restore the UI — even on synchronous throw inside the
      // try block above or an unhandled rejection from extractRangeChart.
      setBusy(false);
    }

    // FR2: any concurrent Reset / new file selection bumps state.extractToken
    // past our captured myToken; in that case drop the result silently so
    // we don't override the user's current intent.
    if (myToken !== state.extractToken) {
      return;
    }

    if (!res.ok) {
      const msg = t(res.errorKey || 'err.http') +
        (res.status ? ' (HTTP ' + res.status + ')' : '');
      // H7: prefer upstream error body for 5xx debugging.
      showAlert('danger', msg, res.errorBody || res.raw);
      return;
    }

    state.result = res.data;
    state.rawText = res.raw;

    if (res.partialFailures && res.partialFailures > 0) {
      // M2: explicit partial-failure notice is more actionable than the
      // generic truncation alert when some runs failed and others succeeded.
      const pf = res.partialFailures;
      const total = (res.data && res.data.runs) || pf + 1;
      showAlert(
        'warning',
        `${pf} of ${total} runs failed — only ${total - pf}/${total} succeeded.`
      );
    } else if (res.truncated) {
      showAlert('warning', t('err.truncated'));
    }

    renderCurrentResult();
  }

  // Update just the loading-slot text (used to show per-run progress).
  function setBusyLabel(text) {
    const slot = $('loading-slot');
    if (!slot) return;
    const p = slot.querySelector('p');
    if (p) p.textContent = text;
  }

  function renderCurrentResult() {
    if (!state.result) return;
    const content = $('results-content');
    content.innerHTML = rcaRenderResults(state.result, state.rawText);
    content.classList.remove('hidden');
    // Phase D: cross-fade the result in (skip animation under reduced motion).
    const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduce) {
      content.classList.remove('fade-in');
      // Force reflow so the animation re-triggers on every render.
      void content.offsetWidth;
      content.classList.add('fade-in');
    }
    $('results-empty').classList.add('hidden');
    bindResultActions();
    // Animate the confidence ring's numeric label from 0 → data-target over
    // 600ms via requestAnimationFrame. Skip the tween under reduced-motion
    // (snap straight to the final value).
    const numEl = content.querySelector('.confidence-ring .num');
    if (numEl) {
      const target = parseInt(numEl.getAttribute('data-target') || '0', 10);
      const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (reduce || target <= 0) {
        numEl.textContent = String(target);
      } else {
        const start = performance.now();
        const dur = 600;
        function step(now) {
          const p = Math.min(1, (now - start) / dur);
          // ease-out cubic for a softer finish
          const eased = 1 - Math.pow(1 - p, 3);
          numEl.textContent = String(Math.round(target * eased));
          if (p < 1) requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
      }
    }
    // Bring results into view.
    $('results-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  // ---- result action buttons (event delegation) ----
  function bindResultActions() {
    const content = $('results-content');
    content.onclick = async (e) => {
      const target = e.target.closest('button');
      if (!target) return;

      if (target.id === 'btn-export-all') {
        const payload = {
          extracted_at: new Date().toISOString(),
          source_file: state.file ? state.file.name : null,
          result: state.result,
        };
        // M13: name the file after the chart kind so columnar exports don't
        // end up mislabeled.
        const isCol = state.result && Array.isArray(state.result.sections)
          && !Array.isArray(state.result.species_ranges);
        const filename = isCol ? 'columnar_section_result.json' : 'range_chart_result.json';
        rcaDownload(filename, JSON.stringify(payload, null, 2), 'application/json');
        return;
      }

      const copyId = target.getAttribute('data-copy');
      if (copyId) {
        const { headers, rows } = rcaBuildTableExport(state.result, copyId);
        const ok = await rcaCopyText(rcaToTsv(headers, rows));
        if (ok) toast(t('results.copied'));
        return;
      }

      const csvId = target.getAttribute('data-csv');
      if (csvId) {
        const { headers, rows } = rcaBuildTableExport(state.result, csvId);
        // M13: pick the file prefix based on the chart kind, same logic
        // as the all-JSON export above.
        const isCol = state.result && Array.isArray(state.result.sections)
          && !Array.isArray(state.result.species_ranges);
        const prefix = isCol ? 'columnar_section_' : 'range_chart_';
        rcaDownload(prefix + csvId + '.csv', rcaToCsv(headers, rows), 'text/csv');
        return;
      }
    };
  }

  // ---- reset ----
  function resetUpload() {
    // FR2: bump the token so any in-flight handleFile / runExtraction
    // awaiting promises drop their stale results instead of clobbering
    // the cleared UI.
    state.expectedToken += 1;
    state.file = null;
    state.dataUrl = null;
    state.mediaType = null;
    state.result = null;
    state.rawText = null;
    $('file-input').value = '';
    $('caption').value = '';
    $('preview-wrap').classList.add('hidden');
    $('preview-img').src = '';
    $('results-content').classList.add('hidden');
    $('results-content').innerHTML = '';
    $('results-empty').classList.remove('hidden');
    clearAlert();
  }

  // ---- language ----
  function applyLangButtons() {
    document.querySelectorAll('#lang-switch button').forEach((b) => {
      const isActive = b.getAttribute('data-lang') === RCA_LANG;
      b.classList.toggle('active', isActive);
      // M39 / a11y: keep aria-selected in sync so screen readers
      // announce the active language tab.
      b.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
  }

  // M39 / a11y: map app lang code to a BCP-47 value for the document
  // root attribute so screen readers pronounce correctly.
  function applyDocLangAttr() {
    const map = { zh: 'zh-CN', en: 'en', ja: 'ja' };
    document.documentElement.lang = map[RCA_LANG] || RCA_LANG;
  }

  function switchLang(lang) {
    rcaSetLang(lang);
    rcaApplyI18n(document);
    applyLangButtons();
    applyDocLangAttr();
    // Re-render results so column headers follow the new language.
    if (state.result) renderCurrentResult();
    // Re-apply preview meta labels if a file is loaded.
    if (state.file && state.dataUrl) {
      // meta is language-dependent; rebuild it cheaply.
      handleFileMetaRefresh();
    }
  }

  function handleFileMetaRefresh() {
    if (!state.file) return;
    const img = $('preview-img');
    const w = img.naturalWidth || 0;
    const h = img.naturalHeight || 0;
    const meta = [];
    meta.push('<div><strong>' + rcaEsc(state.file.name) + '</strong></div>');
    meta.push('<div>' + rcaEsc(t('upload.fileSize')) + ': ' + humanSize(state.file.size) + '</div>');
    meta.push('<div>' + rcaEsc(t('upload.fileDims')) + ': ' + w + ' x ' + h + '</div>');
    $('preview-meta').innerHTML = meta.join('');
  }

  // ---- wire up ----
  function init() {
    // language: stored -> browser -> zh
    let lang = rcaStoreGet(RCA_STORE.lang, '');
    if (!lang) {
      const nav = (navigator.language || 'zh').toLowerCase();
      if (nav.startsWith('ja')) lang = 'ja';
      else if (nav.startsWith('en')) lang = 'en';
      else lang = 'zh';
    }
    rcaSetLang(lang);
    rcaApplyI18n(document);
    applyLangButtons();
    applyDocLangAttr();

    loadSettings();

    // language switch
    $('lang-switch').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (b && b.getAttribute('data-lang')) switchLang(b.getAttribute('data-lang'));
    });

    // api key show/hide
    $('api-key-toggle').addEventListener('click', () => {
      const input = $('api-key');
      const showing = input.type === 'text';
      input.type = showing ? 'password' : 'text';
      $('api-key-toggle').textContent = showing ? t('settings.show') : t('settings.hide');
    });

    $('save-settings').addEventListener('click', saveSettings);

    // dropzone
    const dz = $('dropzone');
    const fileInput = $('file-input');
    dz.addEventListener('click', () => fileInput.click());
    dz.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
    });
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) handleFile(f);
    });
    fileInput.addEventListener('change', () => {
      const f = fileInput.files && fileInput.files[0];
      if (f) handleFile(f);
    });

    // paste image from clipboard anywhere on the page
    window.addEventListener('paste', (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      for (const it of items) {
        if (it.type && it.type.startsWith('image/')) {
          const f = it.getAsFile();
          if (f) { handleFile(f); break; }
        }
      }
    });

    $('extract-btn').addEventListener('click', runExtraction);
    $('reset-btn').addEventListener('click', resetUpload);

    // Phase D: caption character counter. Soft warn at 300, hard warn at 500.
    const captionEl = $('caption');
    const counterEl = $('caption-counter');
    if (captionEl && counterEl) {
      const updateCounter = () => {
        const n = captionEl.value.length;
        counterEl.textContent = n + ' / 500';
        counterEl.classList.toggle('warn', n >= 300 && n < 500);
        counterEl.classList.toggle('over', n >= 500);
      };
      captionEl.addEventListener('input', updateCounter);
      updateCounter();
    }

    // Phase D: IntersectionObserver card entrance animation. Cards slide up
    // + fade in as they scroll into view. Falls back to instant show when
    // IO is unavailable or the user prefers reduced motion.
    const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const cards = document.querySelectorAll('.card');
    if (reduceMotion || !('IntersectionObserver' in window)) {
      // No animation - cards are already visible by default.
    } else {
      cards.forEach((c) => c.classList.add('io-init'));
      const io = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('in-view');
            io.unobserve(entry.target);
          }
        });
      }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });
      cards.forEach((c) => io.observe(c));
    }

    // Phase C: range rows — live numeric readout as the user drags.
    [['max-tokens', 'max-tokens-out'], ['max-edge', 'max-edge-out'],
     ['runs', 'runs-out']].forEach(([srcId, outId]) => {
      const inp = $(srcId), out = $(outId);
      if (!inp || !out) return;
      const sync = () => { out.textContent = inp.value; };
      inp.addEventListener('input', sync);
      sync();
    });

    // Phase C: iOS-style toggle for remember-key. Hidden select still
    // exists for form-serialization; the switch just mirrors it.
    const rk = $('remember-key');
    if (rk && rk.tagName === 'BUTTON') {
      rk.addEventListener('click', () => {
        const next = rk.getAttribute('aria-checked') !== 'true';
        rk.setAttribute('aria-checked', next ? 'true' : 'false');
        // hidden input style: write state to reflect hidden checkbox-like value
        var_hidden_set('remember-key-state', next ? '1' : '');
      });
      // initialize from remembered state via loadSettings (which sets checked attribute already)
    }

    // Phase C: segmented control for conn-mode — keep hidden <select>
    // as source-of-truth so rcaResolveMode() keeps working unchanged.
    const seg = $('conn-mode-seg');
    const sel = $('conn-mode');
    if (seg && sel) {
      seg.addEventListener('click', (e) => {
        const btn = e.target.closest('button[role=radio]');
        if (!btn) return;
        const val = btn.getAttribute('data-value');
        seg.querySelectorAll('button[role=radio]').forEach((b) => {
          b.setAttribute('aria-checked', b === btn ? 'true' : 'false');
        });
        // reflect into hidden <select>
        sel.value = val;
        // fire change so any later watcher (and the existing logic) sees it
        sel.dispatchEvent(new Event('change'));
      });
      // keep segmented in sync with external programmatic changes
      seg.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        const order = ['auto', 'backend', 'direct'];
        const i = order.indexOf(sel.value);
        const next = order[(i + (e.key === 'ArrowRight' ? 1 : -1) + 3) % 3];
        seg.querySelector(`button[data-value="${next}"]`).click();
        e.preventDefault();
      });
    }

    // Phase C: tiny helper to update a hidden <input> the way getElementById
    // would. Used by the iOS switch above.
    function var_hidden_set(id, v) {
      // hidden element lives next to the switch button in DOM; find by id
      var el = document.getElementById(id);
      if (!el) return;
      el.value = String(v);
    }

    // Phase C: page-wide drop overlay — drop a file ANYWHERE on the page
    // (not just the dropzone box) triggers upload. Counter-based
    // dragenter/dragleave avoids flicker on child-element transitions.
    const dropOverlay = $('page-drop-overlay');
    let dragDepth = 0;
    window.addEventListener('dragenter', (e) => {
      if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
      dragDepth += 1;
      if (dropOverlay) dropOverlay.classList.add('show');
    });
    window.addEventListener('dragleave', (e) => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0 && dropOverlay) dropOverlay.classList.remove('show');
    });
    window.addEventListener('dragover', (e) => {
      if (Array.from(e.dataTransfer && e.dataTransfer.types || []).includes('Files')) {
        e.preventDefault();
      }
    });
    window.addEventListener('drop', (e) => {
      dragDepth = 0;
      if (dropOverlay) dropOverlay.classList.remove('show');
      if (!e.dataTransfer) return;
      // Only intercept when files are being dropped — let other drops fall
      // through (e.g. text drops).
      if (!Array.from(e.dataTransfer.types || []).includes('Files')) return;
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) handleFile(f);
    });

    // M42: Ctrl+Enter / Cmd+Enter anywhere in the page triggers extraction,
    // so power users don't have to mouse over to the Extract button. We
    // ignore the shortcut when the user is composing inside a normal
    // text input that isn't the caption/filename context.
    document.addEventListener('keydown', (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key !== 'Enter') return;
      const t = e.target;
      const tag = (t && t.tagName) || '';
      // Allow Ctrl+Enter inside <textarea> (caption) and contenteditable; only
      // block when the user is in an <input type="text"> typing (so we don't
      // hijack newlines) — the caption textarea already wants extraction via
      // this combo.
      if (tag === 'INPUT' && t.type !== 'button') return;
      e.preventDefault();
      runExtraction();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
