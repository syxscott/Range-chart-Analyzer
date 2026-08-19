// app.js - orchestration: settings, upload, extract, render, export, i18n.
'use strict';

(function () {

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
    // FIX-6: AbortController for the in-flight extraction so the user can
    // cancel a long-running (possibly multi-run) request.
    abort: null,
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
    // FIX: split keys into ASCII (word-boundary matched) vs CJK (substring).
    // Previously a single indexOf(k) loop over ASCII keys like 'pollen' or
    // 'col' would fire on substrings ('pollenate', 'colour') and silently
    // misclassify an ordinary range chart as an abundance / columnar chart.
    // CJK tokens have no whitespace boundaries, so a substring match there
    // is the correct behavior.
    const abKeysAscii = ['pollen', 'abundance', 'percentage diagram', 'palyno'];
    const abKeysCjk = ['孢粉', '花粉', '丰度', '百分比'];
    const colKeysAscii = ['column', 'columns', 'columnar', 'col_section', 'col_sections'];
    const colKeysCjk = ['柱状', '柱状図', '柱状图'];
    // UI fix (2026-08-07): phylogenetic-tree detection was missing entirely
    // from the auto heuristic (and from the manual mode list), so the web
    // frontend could never extract a tree. Keywords are deliberately
    // specific to avoid misclassifying range charts (e.g. a caption that
    // merely mentions "tree ring data").
    const phyloKeysAscii = ['phylogen', 'phylogram', 'cladogram', 'dendrogram', 'molecular phylogen'];
    const phyloKeysCjk = ['系统发育', '进化树', '系统树', '分子系统'];
    const asciiWordBoundary = (haystack, needle) => {
      // Use \b word boundaries around ASCII tokens so 'col' won't match
      // 'colour' but 'col_section' (with underscore) still does. The
      // 'word boundary' semantics treat _ as a word character.
      const re = new RegExp('\\b' + needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b');
      return re.test(haystack);
    };
    // REVIEW-2026-11-07 (low): leading-boundary matcher for STEM keywords.
    // \bphylogen\b NEVER matched "phylogenetic" / "phylogenies" (the most
    // common caption form — 'e'/'i' are word chars, no boundary after
    // 'n'), so auto-detect silently fell back to range_chart. Stems match
    // on a leading boundary only; 'palyno' matches "palynology" the same
    // way. Mirrors rca_core/chart_mode.py (_STEMS) so all UIs agree.
    const asciiWordStart = (haystack, needle) => {
      const re = new RegExp('\\b' + needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
      return re.test(haystack);
    };
    const stemMatch = (blob, k) =>
      k === 'phylogen' || k === 'molecular phylogen' || k === 'palyno'
        ? asciiWordStart(blob, k)
        : asciiWordBoundary(blob, k);
    for (const k of abKeysAscii) {
      if (stemMatch(blob, k)) return 'abundance_diagram';
    }
    for (const k of abKeysCjk) {
      if (blob.indexOf(k) !== -1) return 'abundance_diagram';
    }
    for (const k of colKeysAscii) {
      if (asciiWordBoundary(blob, k)) return 'columnar_section';
    }
    for (const k of colKeysCjk) {
      if (blob.indexOf(k) !== -1) return 'columnar_section';
    }
    for (const k of phyloKeysAscii) {
      if (stemMatch(blob, k)) return 'phylogenetic_tree';
    }
    for (const k of phyloKeysCjk) {
      if (blob.indexOf(k) !== -1) return 'phylogenetic_tree';
    }
    return 'range_chart';
  }

  // Resolve the chart extraction mode. Manual choice wins over the
  // heuristic; 'auto' falls back to rcaAutoDetectChartMode().
  function rcaResolveChartMode() {
    const sel = $('chart-mode');
    const choice = sel ? sel.value : 'auto';
    if (choice === 'range_chart' || choice === 'columnar_section'
        || choice === 'abundance_diagram' || choice === 'phylogenetic_tree') {
      return choice;
    }
    return rcaAutoDetectChartMode();
  }

  // Map a result object's shape to an export filename prefix. Mirrors the
  // shape detection in table.js / rca_core.exporter so exported files are
  // labeled by the chart kind they actually hold.
  function rcaResultFilePrefix(result) {
    if (result && Array.isArray(result.abundances)) return 'abundance_diagram_';
    if (result && Array.isArray(result.sections) && !Array.isArray(result.species_ranges)) {
      return 'columnar_section_';
    }
    return 'range_chart_';
  }

  function toast(msg) {
    const el = $('toast');
    if (!el) return;
    // Clear first so an identical message still re-announces to screen
    // readers (aria-live only fires on actual text changes).
    el.textContent = '';
    // Force a reflow so the empty->text change is observed as two mutations.
    void el.offsetWidth;
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
    // Phase L fix: also restore chart_lang / chart_type / enhance so
    // the JS frontend matches the Python GUI's persistence behavior.
    // Defaults mirror rca_core defaults (auto / range_chart / off).
    if ($('chart-lang')) {
      $('chart-lang').value = rcaStoreGet(RCA_STORE.chartLang, 'auto');
    }
    if ($('chart-mode')) {
      $('chart-mode').value = rcaStoreGet(RCA_STORE.chartType, 'auto');
    }
    if ($('enhance')) {
      $('enhance').checked = rcaStoreGet(RCA_STORE.enhance, '') === '1';
    }
    const remember = rcaStoreGet(RCA_STORE.rememberKey, '') === '1';
    const rk0 = $('remember-key');
    if (rk0) rk0.setAttribute('aria-checked', remember ? 'true' : 'false');
    if (remember) {
      $('api-key').value = rcaStoreGet(RCA_STORE.apiKey, '');
    }
    // Phase C: sync segmented + range outputs to loaded values.
    syncSegmentedFromSelect();
    syncRangeOutputs();
    syncFooterModel();
  }

  // FIX-5: footer should reflect the actually-configured model, not a
  // hardcoded product name.
  function syncFooterModel() {
    const el = $('footer-model');
    if (!el) return;
    const m = ($('model') && $('model').value.trim()) || RCA_CONFIG.defaultModel;
    el.textContent = m;
  }

  // Footer should reflect whether extraction actually runs in-browser or
  // via the same-origin backend — "100% client-side" is misleading when the
  // user is in backend transport mode.
  function syncFooterRuntime() {
    const el = $('footer-runtime');
    if (!el) return;
    const mode = rcaResolveMode();
    const i18nKey = mode === 'backend' ? 'footer.backend' : 'footer.clientSide';
    // Use the data-i18n attribute so rcaApplyI18n re-translates it on
    // language switch instead of getting frozen in the current language.
    el.setAttribute('data-i18n', i18nKey);
    el.textContent = t(i18nKey);
  }

  function saveSettings() {
    // FR3: collect per-write results so we can warn when localStorage is
    // unavailable (private mode / quota exceeded) instead of falsely
    // claiming "settings saved".
    const writes = [
      [RCA_STORE.endpoint, rcaStoreSet(RCA_STORE.endpoint, $('endpoint').value.trim() || RCA_CONFIG.defaultEndpoint)],
      [RCA_STORE.model, rcaStoreSet(RCA_STORE.model, $('model').value.trim() || RCA_CONFIG.defaultModel)],
      ['rca.maxTokens', rcaStoreSet('rca.maxTokens', $('max-tokens').value.trim() || String(RCA_CONFIG.defaultMaxTokens))],
      [RCA_STORE.proxy, rcaStoreSet(RCA_STORE.proxy, $('proxy').value.trim())],
      [RCA_STORE.mode, rcaStoreSet(RCA_STORE.mode, $('conn-mode').value)],
      [RCA_STORE.maxEdge, rcaStoreSet(RCA_STORE.maxEdge, $('max-edge').value.trim() || String(RCA_CONFIG.maxImageEdge))],
      [RCA_STORE.runs, rcaStoreSet(RCA_STORE.runs, $('runs').value.trim() || '1')],
    ];
    // Phase L fix: persist chart_lang / chart_type / enhance so the
    // JS settings match what the Python GUI stores in its JSON file.
    if ($('chart-lang')) {
      writes.push([RCA_STORE.chartLang, rcaStoreSet(RCA_STORE.chartLang, $('chart-lang').value || 'auto')]);
    }
    if ($('chart-mode')) {
      writes.push([RCA_STORE.chartType, rcaStoreSet(RCA_STORE.chartType, $('chart-mode').value || 'auto')]);
    }
    if ($('enhance')) {
      writes.push([RCA_STORE.enhance, rcaStoreSet(RCA_STORE.enhance, $('enhance').checked ? '1' : '')]);
    }
    // Phase C: remember-key is a switch button; read aria-checked.
    // SECURITY MODEL (F-22 fix): RCA_STORE.apiKey is ALWAYS stored in
    // sessionStorage (cleared on tab close) — the remember checkbox does NOT
    // control whether it persists to localStorage.  The checkbox only
    // controls whether the apiKey field is pre-filled on page load.
    // On shared computers: close the tab (or the whole browser) when done.
    const rk = $('remember-key');
    const remember = rk ? rk.getAttribute('aria-checked') === 'true' : false;
    writes.push([RCA_STORE.rememberKey, rcaStoreSet(RCA_STORE.rememberKey, remember ? '1' : '')]);
    if (remember) {
      writes.push([RCA_STORE.apiKey, rcaStoreSet(RCA_STORE.apiKey, $('api-key').value.trim())]);
    } else {
      writes.push([RCA_STORE.apiKey, rcaStoreSet(RCA_STORE.apiKey, '')]);
    }
    // FR3: if ANY write failed (private mode, quota, blocked storage)
    // — surface a real warning. M2: surface which specific key failed so
    // the user can act (e.g. disable a browser extension that's blocking
    // storage). The previous version only said "save failed" with no
    // detail.
    const failedKeys = writes.filter(([_, status]) => status === false).map(([k]) => k);
    if (failedKeys.length === 0) {
      toast(t('settings.saved'));
      syncFooterModel();
      syncFooterRuntime();
    } else {
      // Keep the user-facing message stable (i18n key unchanged) but add
      // the specific failing key(s) in parens for diagnostic value.
      const detail = failedKeys.join(', ');
      toast(`${t('settings.saveFailed')} (${detail})`);
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
    // Mirror the enhance checkbox into state so runExtraction reads it.
    const enh = $('enhance');
    if (enh) {
      enh.addEventListener('change', () => { state._enhance = enh.checked; });
      state._enhance = !!enh.checked;
    }
  }

  // ---- alerts ----
  function clearAlert() {
    const slot = $('alert-slot');
    if (!slot) return;
    slot.innerHTML = '';
  }

  function showAlert(kind, message, rawDetail) {
    const slot = $('alert-slot');
    if (!slot) return;
    // M3: error-level alerts need role="alert" so screen readers announce
    // them immediately (aria-live=polite on the parent slot only gets the
    // "next opportunity" announcement). Keep role="status" for warnings —
    // they're informational and shouldn't interrupt the user.
    const role = (kind === 'danger') ? 'alert' : 'status';
    const div = document.createElement('div');
    div.className = 'alert alert-' + kind;
    div.setAttribute('role', role);
    const inner = document.createElement('div');
    inner.textContent = message;
    div.appendChild(inner);
    if (rawDetail) {
      const pre = document.createElement('pre');
      pre.textContent = String(rawDetail).slice(0, 4000);
      div.appendChild(pre);
    }

    // UI polish: fade transition for alert appearance.
    // Fade out any existing alert first, then add new one with fade-in.
    const existing = slot.querySelector('.alert');
    if (existing) {
      existing.classList.add('alert-fade-out');
      existing.addEventListener('transitionend', () => existing.remove(), { once: true });
    } else {
      slot.innerHTML = '';
    }

    // Append new alert with fade-in
    slot.appendChild(div);
    // Force reflow so the browser registers the opacity change for animation
    void div.offsetWidth;
    div.classList.add('alert-fade-in');
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
    // FIX (file-too-big): reject oversized files before the base64 read blows
    // up memory. The dropzone advertises a ~20 MB cap; enforce it here so a
    // dragged-in multi-hundred-MB TIFF can't hang the browser.
    var _maxFileBytes = (RCA_CONFIG.maxFileBytes) || (20 * 1024 * 1024);
    if (file.size > _maxFileBytes) {
      showAlert('warning', t('err.fileTooBig'));
      return;
    }
    // H8 (REVIEW-2026-08-19): defer `state.file = file` until AFTER the
    // async load promise resolves. The previous synchronous assignment
    // exposed the NEW filename while the preview was still rendering the
    // OLD image — a race where export filenames + footer labels drifted
    // from the displayed preview. Downstream code is null-checked, so
    // the brief window between `state.dataUrl` set and `state.file` set
    // is safe.
    // FIX (handlefile-abort): when the user picks a NEW file mid-extraction,
    // abort the in-flight request so we don't waste API calls on the
    // previous image. The token bump below already prevents the stale
    // result from rendering; aborting just stops the underlying network
    // work (otherwise a 2-of-3 multi-run still pays for all 3).
    if (state.abort) state.abort.abort();
    // REVIEW-2026-11-07 (low): abort the PREVIOUS file's image load as
    // well — before, its FileReader/Image decode ran to completion in the
    // background after a newer selection (token check dropped the result,
    // but not the work).
    if (state._loadAbort) state._loadAbort.abort();
    const loadAbort = new AbortController();
    state._loadAbort = loadAbort;
    // FR1: claim a load token. If the user picks another file before
    // this one's load promise resolves, the new call bumps the token
    // and our async continuation will see a mismatch and silently drop.
    // Also bump extractToken + expectedToken: an in-flight extraction
    // from the PREVIOUS file must NOT write its result into this file's
    // preview (the previous version only bumped loadToken, which left
    // extractToken stale and caused old extraction results to render
    // into the new image).
    state.expectedToken += 1;
    state.loadToken = state.expectedToken;
    state.extractToken = state.expectedToken;
    const myToken = state.expectedToken;
    clearAlert();
    try {
      const edgeEl = $('max-edge');
      let maxEdge = parseInt(edgeEl && edgeEl.value, 10);
      if (!Number.isFinite(maxEdge) || maxEdge < 0) maxEdge = RCA_CONFIG.maxImageEdge;
      const loaded = await rcaLoadAndMaybeResize(file, maxEdge, {
        enhance: !!state._enhance,
        signal: loadAbort.signal,
      });
      if (myToken !== state.loadToken) {
        // A newer file-selection has superseded us; drop this result.
        return;
      }
      state.file = file;
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
        // REVIEW-2026-11-07 (low): an aborted load means a newer file
        // selection superseded this one — showing "image read failed"
        // would be misleading, so stay silent.
        if (err && err.message === 'aborted') {
          /* superseded load, nothing to report */
        } else {
          showAlert('danger', t('err.imageRead'));
        }
      }
    }
  }

  // ---- extraction flow ----
  // UI-REVIEW-2026-08-01 (H1): the force-rerun button visibility depends
  // on state.result, but setBusy(false) ran in the `finally` BEFORE
  // `state.result = res.data` was assigned — so after the FIRST successful
  // extraction the button never appeared (nothing re-evaluated it). The
  // visibility logic is now a separate function called both from setBusy
  // and after a result is stored.
  function updateActionButtons() {
    const busy = state.busy;
    const btn = $('extract-btn');
    if (btn) {
      btn.disabled = busy;
      if (busy) {
        // M4: keep the data-i18n hook on the inner span so a language
        // switch mid-extraction still re-translates the spinner label.
        btn.innerHTML = '<span class="spinner"></span><span data-i18n="upload.extract">' + rcaEsc(t('upload.extract')) + '</span>';
      } else {
        // Keep the data-i18n hook so a later language switch re-translates it.
        btn.innerHTML = '<span id="extract-btn-label" data-i18n="upload.extract">' + rcaEsc(t('upload.extract')) + '</span>';
      }
    }
    $('loading-slot').classList.toggle('hidden', !busy);
    // FIX-6: show the Cancel button only while an extraction is in flight.
    const cancelBtn = $('cancel-btn');
    if (cancelBtn) cancelBtn.classList.toggle('hidden', !busy);
    // FIX (force-rerun): show "Force rerun" when a result is present so the
    // user can re-extract (bypassing cache) without resetting the image.
    const rerunBtn = $('force-rerun-btn');
    if (rerunBtn) rerunBtn.classList.toggle('hidden', !(!!state.result && !busy));
    if (busy) {
      $('results-empty').classList.add('hidden');
      $('results-content').classList.add('hidden');
    }
  }

  function setBusy(busy) {
    state.busy = busy;
    updateActionButtons();
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
    const endpoint = $('endpoint').value.trim();
    if (!endpoint) {
      showAlert('warning', t('err.noEndpoint'));
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
    // FIX-6: fresh AbortController for this extraction; cancel-btn aborts it.
    const abort = new AbortController();
    state.abort = abort;
    setBusy(true);

    // FR4: wrap the body in try/catch/finally so an unexpected exception
    // (e.g. a provider throwing synchronously) can never leave the UI
    // stuck in the busy state.
    let res;
    let extractError = null;
    // Nit3: hoisted so the post-try error handler can pick the
    // transport-appropriate message.
    let connMode = 'backend';
    try {
      const maxTokens = rcaClampMaxTokens($('max-tokens').value);
      connMode = rcaResolveMode();  // 'backend' | 'direct' (transport)
      const chartMode = rcaResolveChartMode();  // manual choice or auto-detect heuristic
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
        signal: abort.signal,
        // FIX (force-rerun): bypass server cache on explicit user request.
        force_rerun: !!state._forceRerun,
        // FIX (enhance): pre-process image to boost VLM recognition.
        enhance: state._enhance || false,
      };

      // Phase 1: analyze (LLM call). For single-run use 'analyzing';
      // multi-run gets a per-run counter below. Use the structured form
      // so a language switch mid-extraction re-translates the prefix.
      setBusyLabel(
        runs > 1
          ? { i18nKey: 'loading.analyzing', params: { done: 0, total: runs } }
          : { i18nKey: 'loading.analyzing' }
      );

      if (connMode === 'backend') {
        // The server performs the N runs and merges; pass runs through.
        res = await extractRangeChart(Object.assign({}, baseOpts, { runs }));
      } else if (runs > 1) {
        // Direct/proxy mode: fire N requests CONCURRENTLY via Promise.all
        // so total wait is ~one latency rather than N. Each promise resolves
        // independently; a failure in one run doesn't cancel the others.
        setBusyLabel({ i18nKey: 'loading.analyzing', params: { done: 0, total: runs } });
        const km = (typeof RCA_KEYMAP_BY_MODE !== 'undefined' && RCA_KEYMAP_BY_MODE[chartMode])
          || RCA_DEFAULT_KEYMAP;
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
          setBusyLabel({ i18nKey: 'loading.analyzing', params: { done: completed, total: runs } });
          return r;
        }));
        const results = await Promise.all(tracked);
        // Phase 2: aggregating.
        setBusyLabel({ i18nKey: 'loading.aggregating' });
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
          // Auto-detect the keymap from data shape so the merge uses the
          // correct schema even when the user's mode selection was wrong.
          const detectedKm = rcaAutoDetectKeymap(okDatas);
          res = {
            ok: true,
            data: rcaMergeResults(okDatas, runs, detectedKm),
            raw: raws.join('\n---RUN---\n').slice(0, 8000),
            truncated: anyTruncated,
            partialFailures: partialFails,
          };
        }
      } else {
        res = await extractRangeChart(baseOpts);
      }
    } catch (exc) {
      // P1-4 fix: catch synchronous exceptions from extractRangeChart or any
      // other call in the try block. Show a danger alert instead of silently
      // continuing (which would leave the result blank).
      extractError = exc;
      res = { ok: false, errorKey: 'err.network', raw: String(exc) };
    } finally {
      // FR4: always restore the UI — even on synchronous throw inside the
      // try block above or an unhandled rejection from extractRangeChart.
      setBusy(false);
      // FIX-6: only clear the shared handle if it's still ours (a newer
      // extraction may have already replaced it).
      if (state.abort === abort) state.abort = null;
    }

    // P1-4 fix: if extractError was thrown, handle it after the finally block
    // restores the UI. Check myToken first so a superseded extraction doesn't
    // show a stale error.
    if (extractError !== null) {
      // FR2: superseded by a newer extraction — silent drop.
      if (myToken !== state.extractToken) return;
      // FIX-6: user cancelled — no error alert.
      if (abort.signal.aborted) return;
      showAlert('danger', t('err.network'), String(extractError));
      return;
    }

    // FR2: any concurrent Reset / new file selection bumps state.extractToken
    // past our captured myToken; in that case drop the result silently so
    // we don't override the user's current intent.
    if (myToken !== state.extractToken) {
      return;
    }

    // FIX-6: user cancelled — show a lightweight toast, no scary error alert.
    if (abort.signal.aborted) {
      toast(t('upload.cancelled'));
      return;
    }

    if (!res.ok) {
      // Nit3 (UI-REVIEW-2026-08-01): in backend transport the request is
      // same-origin, so the CORS/proxy guidance in err.network is
      // misleading — use the backend-specific message instead.
      let errKey = res.errorKey || 'err.http';
      if (errKey === 'err.network' && connMode === 'backend') {
        errKey = 'err.networkBackend';
      }
      const msg = t(errKey) +
        (res.status ? ' (HTTP ' + res.status + ')' : '');
      // H7: prefer upstream error body for 5xx debugging.
      showAlert('danger', msg, res.errorBody || res.raw);
      // FIX (results-empty): setBusy(true) hid both #results-empty and
      // #results-content. On failure there is nothing to render, so restore
      // the empty-state placeholder — otherwise the previous result (if any)
      // stays hidden and the page looks blank apart from the alert. If a
      // prior successful result exists, re-render it so the user keeps their
      // data instead of losing it on a failed re-extraction.
      if (state.result) {
        renderCurrentResult();
      } else {
        $('results-empty').classList.remove('hidden');
      }
      return;
    }

    state.result = res.data;
    state.rawText = res.raw;
    // Phase K fix: invoke the quality scorer on direct-mode results.
    // Backend mode already attaches `data.quality` server-side (via
    // score_range_chart). Without this call the pure-frontend mode
    // silently drops the quality badge — the entire quality module
    // was effectively dead code in direct-mode extractions.
    if (
      typeof globalThis.scoreRangeChart === 'function'
      && state.result
      && typeof state.result === 'object'
    ) {
      try {
        const q = globalThis.scoreRangeChart(state.result);
        state.result.quality = q;
      } catch (_e) {
        // Quality scoring is best-effort; never abort the rendering.
      }
    }

    // Step 0 (8.0→9.5): surface chimera_warnings so the operator knows
    // when a merged row was not observed in any single run.
    const chimeraWarnings = res.data && res.data.chimera_warnings;
    if (chimeraWarnings && chimeraWarnings.length > 0) {
      showAlert(
        'warning',
        t('results.chimera_warning', { n: chimeraWarnings.length })
      );
    }

    if (res.partialFailures && res.partialFailures > 0) {
      // M2: explicit partial-failure notice is more actionable than the
      // generic truncation alert when some runs failed and others succeeded.
      const pf = res.partialFailures;
      const total = (res.data && res.data.runs) || pf + 1;
      // FIX: route through t() + {pf,total,succeeded} placeholders so the
      // alert is localized in zh/en/ja. The previous string-literal was a
      // hardcoded English sentence that ignored the active language.
      showAlert(
        'warning',
        t('results.partialFailure', { pf, total, succeeded: total - pf })
      );
    } else if (res.truncated) {
      showAlert('warning', t('err.truncated'));
    }

    renderCurrentResult();
    // H1 fix: refresh action-button visibility now that state.result is
    // stored (setBusy(false) ran before the assignment).
    updateActionButtons();
  }

  // Update just the loading-slot text (used to show per-run progress).
  // Accepts either a plain string (legacy callers) or an { i18nKey, ...params }
  // object. The structured form is preferred because the i18n key gets
  // re-translated live on every call — so when the user switches language
  // mid-extraction, the in-progress label picks up the new translation
  // immediately.
  const _busyLabelFmt = { i18nKey: null, params: null };
  function setBusyLabel(arg) {
    const slot = $('loading-slot');
    if (!slot) return;
    const p = slot.querySelector('p');
    if (!p) return;
    if (typeof arg === 'string') {
      _busyLabelFmt.i18nKey = null;
      _busyLabelFmt.params = null;
      p.textContent = arg;
      return;
    }
    if (arg && typeof arg === 'object' && arg.i18nKey) {
      _busyLabelFmt.i18nKey = arg.i18nKey;
      _busyLabelFmt.params = arg.params || {};
      p.textContent = formatBusyLabel(arg.i18nKey, arg.params || {});
    }
  }

  // Build the loading-slot text from the i18n key + params. Mirrors the
  // template "{base} ({done}/{total})" used for multi-run progress.
  function formatBusyLabel(i18nKey, params) {
    const base = t(i18nKey);
    if (params && params.done != null && params.total != null) {
      return base + ' (' + params.done + '/' + params.total + ')';
    }
    return base;
  }

  function renderCurrentResult() {
    if (!state.result) return;
    const content = $('results-content');
    // FIX (viz-host-preserve): capture any existing #viz-host before we
    // overwrite innerHTML, then re-append it after rendering so the
    // future-ECharts mount point survives every render. The previous
    // behavior wiped #viz-host on every render and forced future code
    // to recreate the container each time — and any cached state inside
    // an initialized chart instance would be lost.
    const vizHost = content.querySelector('#viz-host');
    content.innerHTML = rcaRenderResults(state.result, state.rawText);
    if (vizHost) {
      // Re-append the original element (preserving any DOM state inside
      // it — handlers, child nodes, attribute changes — that a fresh
      // <div id="viz-host" hidden></div> clone would lose).
      content.appendChild(vizHost);
    }
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
      const prefersReducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (prefersReducedMotion || target <= 0) {
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
        const filename = rcaResultFilePrefix(state.result) + 'result.json';
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
        const prefix = rcaResultFilePrefix(state.result);
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
    //
    // BUGFIX: previous version only bumped `expectedToken` but did NOT
    // update `state.extractToken` / `state.loadToken`. The in-flight
    // extraction's `myToken === state.extractToken` check therefore
    // passed and the late result overwrote the freshly-cleared state.
    // Mirror `loadToken` / `extractToken` to the new expectedToken so
    // the comments below become truth.
    //
    // FIX (reset-abort): also abort the in-flight extraction so its network
    // request is cancelled instead of running to completion and wasting an
    // API call (or N calls for multi-run). The token bump already prevents
    // the stale result from rendering; aborting just stops the underlying
    // work. Safe to call when nothing is in flight (abort is null/no-op).
    if (state.abort) state.abort.abort();
    state.expectedToken += 1;
    state.loadToken = state.expectedToken;
    state.extractToken = state.expectedToken;
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
    // syncFooterRuntime() writes the textContent directly (and also updates
    // the data-i18n attribute), so it bypasses rcaApplyI18n's static-DOM
    // walk — we must call it explicitly to pick up the new translation.
    syncFooterRuntime();
    // Re-render results so column headers follow the new language.
    if (state.result) renderCurrentResult();
    // Re-apply preview meta labels if a file is loaded.
    if (state.file && state.dataUrl) {
      // meta is language-dependent; rebuild it cheaply.
      handleFileMetaRefresh();
    }
    // BUGFIX: the loading-slot label is a plain <p> (not data-i18n), so
    // rcaApplyI18n doesn't refresh it. Re-render it from the cached
    // (i18nKey, params) so a language switch mid-extraction picks up
    // the new translation instead of staying frozen in the old one.
    if (state.busy && _busyLabelFmt.i18nKey) {
      setBusyLabel({ i18nKey: _busyLabelFmt.i18nKey, params: _busyLabelFmt.params });
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
    // language: stored preference wins; default is 简体中文 (zh-CN). The
    // previous version auto-detected from navigator.language, which made
    // English browsers default to English — the project's default language
    // is Simplified Chinese, so we now fall back straight to 'zh'.
    let lang = rcaStoreGet(RCA_STORE.lang, '') || 'zh';
    rcaSetLang(lang);
    rcaApplyI18n(document);
    applyLangButtons();
    applyDocLangAttr();

    loadSettings();
    syncFooterRuntime();

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
    if (!dz || !fileInput) return;
    dz.addEventListener('click', () => fileInput.click());
    dz.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); }
    });
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      // FIX (drop double-trigger): stop the event bubbling to the window-level
      // drop handler (line ~897), which would otherwise fire handleFile() a
      // second time for the same file. The token mechanism in handleFile
      // prevents a duplicate preview, but blocking propagation avoids the
      // wasted second image decode (notable for large charts).
      e.stopPropagation();
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
      // e.clipboardData can be null in Firefox/Safari Private Mode and some
      // embedded contexts — guard before reading .items.
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
    // UX improvement: confirm before reset to prevent accidental data loss
    $('reset-btn').addEventListener('click', () => {
      // Only confirm if there's actual data to lose
      if (state.result || state.dataUrl) {
        if (!confirm(t('confirm.reset'))) return;
      }
      resetUpload();
    });
    // FIX-6: cancel the in-flight extraction on demand.
    const cancelBtn = $('cancel-btn');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', () => {
        if (state.abort) state.abort.abort();
      });
    }
    // FIX (force-rerun): re-extract bypassing the server-side cache so the
    // user can force a fresh VLM call even for identical inputs.
    state._forceRerun = false;
    const rerunBtn = $('force-rerun-btn');
    if (rerunBtn) {
      rerunBtn.addEventListener('click', () => {
        state._forceRerun = true;
        runExtraction();
        state._forceRerun = false;
      });
    }

    // Phase D: caption character counter. Soft warn at 300, hard warn at 500.
    // BUGFIX: textarea has maxlength=2000 but the counter displayed "X / 500",
    // so a 2000-char caption would never reach 500 from the user's view even
    // though the input would silently cap. Read the actual maxlength off the
    // element and use it as the denominator, with a sensible default if the
    // attribute is missing.
    const captionEl = $('caption');
    const counterEl = $('caption-counter');
    if (captionEl && counterEl) {
      const capMax = parseInt(captionEl.getAttribute('maxlength'), 10) || 500;
      const softWarn = Math.max(60, Math.round(capMax * 0.6));
      const hardWarn = capMax;
      const updateCounter = () => {
        const n = captionEl.value.length;
        counterEl.textContent = n + ' / ' + capMax;
        counterEl.classList.toggle('warn', n >= softWarn && n < hardWarn);
        counterEl.classList.toggle('over', n >= hardWarn);
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
      // UI-Demo stagger: each card has its own transition delay set inline
// via --card-stagger-i so they fade in sequentially (0ms, 90ms, 180ms
// ...) the moment the page paints. The IO observer keeps working but
// the CSS delay does the sequencing regardless of exactly when each
// card fires its IO callback.
cards.forEach((c, i) => {
        c.classList.add('io-init');
        c.style.setProperty('--card-stagger-i', (i * 90) + 'ms');
        // Force reflow so the initial 'io-init' state paints before we
        // add 'in-view' — without this the browser batches both changes
        // together and the transition never fires.
        void c.offsetWidth;
        c.classList.add('in-view');
      });
      // Keep the IO observer for safety (covers edge cases where a card
      // might not be in the initial paint region), but it's redundant
      // for the stagger effect.
      const io = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('in-view');
            io.unobserve(entry.target);
          }
        });
      }, { threshold: 0.05, rootMargin: '0px 0px -5% 0px' });
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

    // Phase C: iOS-style toggle for remember-key. The aria-checked attribute
    // is the source of truth — saveSettings() reads it back directly.
    const rk = $('remember-key');
    if (rk && rk.tagName === 'BUTTON') {
      rk.addEventListener('click', () => {
        const next = rk.getAttribute('aria-checked') !== 'true';
        rk.setAttribute('aria-checked', next ? 'true' : 'false');
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
        // Footer copy depends on the resolved transport (backend vs direct);
        // refresh it immediately so the user sees the new label.
        syncFooterRuntime();
      });
      // keep segmented in sync with external programmatic changes
      seg.addEventListener('keydown', (e) => {
        const order = ['auto', 'backend', 'direct'];
        if (e.key === 'Home') {
          seg.querySelector(`button[data-value="${order[0]}"]`).click();
          e.preventDefault();
          return;
        }
        if (e.key === 'End') {
          seg.querySelector(`button[data-value="${order[order.length - 1]}"]`).click();
          e.preventDefault();
          return;
        }
        if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
        const i = order.indexOf(sel.value);
        const next = order[(i + (e.key === 'ArrowRight' ? 1 : -1) + 3) % 3];
        seg.querySelector(`button[data-value="${next}"]`).click();
        e.preventDefault();
      });
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
      // F-15: Allow Ctrl+Enter inside <textarea> (caption) and contenteditable;
      // only block when the user is in an <input type="text"> typing (so we don't
      // hijack newlines) — the caption textarea already wants extraction via
      // this combo.
      if (tag === 'TEXTAREA') return;
      if (tag === 'INPUT' && t.type !== 'button') return;
      // Guard: skip extraction when already busy to prevent race conditions.
      if (state.busy) return;
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
