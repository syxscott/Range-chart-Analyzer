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
    // M3 (REVIEW-2026-09-20): AbortController for the in-flight GBIF name
    // verification round. Its own handle because the hints fire AFTER the
    // extraction resolved — `state.abort` is already cleared by then, so
    // cancelling a live round needed an owner of its own.
    _nameAbort: null,
    // H8 (REVIEW-2026-09-20): true while the CURRENT `state.dataUrl` pixels
    // have already been through the browser's supersample + unsharp pass, so
    // the backend must not enhance them a second time. Set in handleFile,
    // cleared whenever the image (or its preview) is dropped.
    _frontendEnhanced: false,
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
  // UI-REVIEW-2026-09-07: detailed variant - {mode, matched} so the
  // caller can distinguish a POSITIVE keyword hit from the range_chart
  // default. Unmatched "auto" is forwarded to the extraction layer,
  // which classifies the image itself (vision) instead of guessing.
  // REVIEW-2026-09-20 (#105 mirror): the tables, the STEM list, the bounded
  // zone-correlation phrase regex AND the branch order below are a verbatim
  // mirror of rca_core/chart_mode.py (_ZON/_AB/_COL/_PHYLO/_RANGE/_CHEM/
  // _PALEO/_SCAT + _STEMS + _ZON_PHRASE_RES + auto_detect_chart_mode_ex).
  // Order: zon → ab → col → phylo → (only when the caption does NOT name a
  // range chart) paleo → scat → chem → explicit range chart → unmatched
  // default. Pinned keyword-by-keyword by tests/test_chart_mode_parity.py,
  // so a one-sided edit fails loudly on both engines.
  //
  // Shared helpers (both auto-detect variants below declare their own keyword
  // tables, but resolve matching through these identical rules):
  //   asciiWordBoundary — whole-word ASCII keyword (\b…\b)
  //   asciiWordStart    — STEM keyword (leading \b only, so 'phylogen'
  //                       matches "phylogenetic" and 'zonation' "zonations")
  //   cjk keys          — plain substring (CJK/Cyrillic have no boundary)
  const RCA_CHART_MODE_STEMS = ['phylogen', 'molecular phylogen', 'palyno',
    'zonation', 'range chart', 'isotop', 'chemostrat', 'paleomap', 'palaeomap',
    'paleogeograph', 'palaeogeograph', 'paleocontinent', 'palaeocontinent'];

  // \bcorrelation of … zone(s) — the split spelling ("Correlation of Triassic
  // radiolarian ZONES and subzones") no keyword tuple can express. The clause
  // stops at the first full stop, so the lithostratigraphic "Correlation of
  // the measured sections" is still refused. Mirrors _ZON_PHRASE_RES.
  function rcaChartModeAsciiWordBoundary(haystack, needle) {
    const re = new RegExp('\\b' + needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b');
    return re.test(haystack);
  }

  function rcaChartModeAsciiWordStart(haystack, needle) {
    const re = new RegExp('\\b' + needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
    return re.test(haystack);
  }

  // One keyword of one table (mirrors chart_mode.py:_match_kw).
  function rcaChartModeMatchKw(haystack, needle) {
    return RCA_CHART_MODE_STEMS.indexOf(needle) !== -1
      ? rcaChartModeAsciiWordStart(haystack, needle)
      : rcaChartModeAsciiWordBoundary(haystack, needle);
  }

  // Any keyword / CJK term / phrase of one table occurs in the lowercased
  // blob (mirrors chart_mode.py:_hit).
  function rcaChartModeHit(blob, asciiKeys, cjkKeys, phraseRes) {
    for (const k of asciiKeys) {
      if (rcaChartModeMatchKw(blob, k)) return true;
    }
    for (const k of cjkKeys) {
      if (blob.indexOf(k) !== -1) return true;
    }
    return phraseRes ? phraseRes.some((re) => re.test(blob)) : false;
  }

  function rcaAutoDetectChartModeDetailed() {
    const blob = (function () {
      const cap = ($('caption') && $('caption').value || '').toLowerCase();
      const fileName = (state.file && state.file.name || '').toLowerCase();
      return cap + ' ' + fileName;
    })();
    const zonPhraseRes = [/\bcorrelation of\b[^.\n]{0,80}?\bzones?\b/];
    const zonKeysAscii = ['zonation', 'biozonation', 'zone correlation', 'correlation chart', 'correlation of zones'];
    const zonKeysCjk = ['生物带', '化石带', '带状对比', '对比图'];
    const abKeysAscii = ['pollen', 'abundance', 'percentage diagram', 'palyno'];
    const abKeysCjk = ['孢粉', '花粉', '丰度', '百分比'];
    const colKeysAscii = ['column', 'columns', 'columnar', 'col_section', 'col_sections'];
    const colKeysCjk = ['柱状', '柱状図', '柱状图'];
    const phyloKeysAscii = ['phylogen', 'phylogram', 'cladogram', 'dendrogram', 'molecular phylogen'];
    const phyloKeysCjk = ['系统发育', '进化树', '系统树', '分子系统'];
    const rangeKeysAscii = ['range chart', 'range-chart'];
    const rangeKeysCjk = ['延限', 'карта совмещения', 'график совмещения'];
    const chemKeysAscii = ['isotop', 'chemostrat', 'chemical stratigraphy', 'chemical stratigraphic'];
    const chemKeysCjk = ['同位素', '化学地层', '化学地層', '地球化学', 'изотоп', 'геохим'];
    const paleoKeysAscii = ['paleomap', 'palaeomap', 'paleogeograph', 'palaeogeograph', 'paleocontinent', 'palaeocontinent'];
    const paleoKeysCjk = ['古地理', '古海洋', '古大陆', '板块重建', 'палеогеограф', 'палеокарт', 'палеоконтинент'];
    const scatKeysAscii = ['scatter plot', 'scatterplot', 'scatter diagram', 'biplot', 'crossplot', 'cross plot'];
    const scatKeysCjk = ['散点', '散布図', 'точечная диаграмма', 'рассеяни', 'рассеиван'];
    // Does the caption name a range chart itself? UI-REVIEW-2026-09-07
    // (E2E fig_23) + REVIEW-2026-09-20 #105 item 1: a mixed
    // "Columnar section with radiolarian range chart. Zonation of …" caption
    // routes to range_chart (the extractor's biozone fields still capture the
    // zonation columns), and a self-labelled range chart is a POSITIVE hit so
    // the vision classifier never gets to overturn it.
    const saysRangeChart = rcaChartModeHit(blob, rangeKeysAscii, rangeKeysCjk);
    if (rcaChartModeHit(blob, zonKeysAscii, zonKeysCjk, zonPhraseRes)) {
      return saysRangeChart
        ? { mode: 'range_chart', matched: true }
        : { mode: 'zonation_chart', matched: true };
    }
    if (rcaChartModeHit(blob, abKeysAscii, abKeysCjk)) {
      return { mode: 'abundance_diagram', matched: true };
    }
    if (rcaChartModeHit(blob, colKeysAscii, colKeysCjk)) {
      return { mode: 'columnar_section', matched: true };
    }
    if (rcaChartModeHit(blob, phyloKeysAscii, phyloKeysCjk)) {
      return { mode: 'phylogenetic_tree', matched: true };
    }
    // The three ASSISTANT modes: chart-type wording first (biplot / map say
    // MORE about the figure than the data it plots), then the data-content
    // table. Gated on `not saysRange_chart` — "…δ13C curve above the conodont
    // range chart" must stay a range chart.
    if (!saysRangeChart) {
      if (rcaChartModeHit(blob, paleoKeysAscii, paleoKeysCjk)) {
        return { mode: 'paleomap', matched: true };
      }
      if (rcaChartModeHit(blob, scatKeysAscii, scatKeysCjk)) {
        return { mode: 'scatter_plot', matched: true };
      }
      if (rcaChartModeHit(blob, chemKeysAscii, chemKeysCjk)) {
        return { mode: 'chemical_stratigraphy', matched: true };
      }
    }
    if (saysRangeChart) {
      return { mode: 'range_chart', matched: true };
    }
    return { mode: 'range_chart', matched: false };
  }

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
    // Legacy (mode-string-only) variant of rcaAutoDetectChartModeDetailed
    // above: SAME tables, SAME stem rules and SAME branch order — the two
    // must not drift (tests/test_chart_mode_parity.py counts each table's
    // declarations and compares them against rca_core/chart_mode.py).
    const zonPhraseRes = [/\bcorrelation of\b[^.\n]{0,80}?\bzones?\b/];
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
    // UI-REVIEW-2026-09-05: biozonation / correlation charts (radiolarian
    // biochronology). Matched BEFORE abundance (mirrors chart_mode.py).
    // 'zonation' is a stem: also matches 'zonations' / 'zonal'.
    const zonKeysAscii = ['zonation', 'biozonation', 'zone correlation', 'correlation chart', 'correlation of zones'];
    const zonKeysCjk = ['生物带', '化石带', '带状对比', '对比图'];
    // REVIEW-2026-09-20 #105: the explicit range-chart wording, and the three
    // assistant-mode tables (chemical stratigraphy / paleomap / scatter).
    const rangeKeysAscii = ['range chart', 'range-chart'];
    const rangeKeysCjk = ['延限', 'карта совмещения', 'график совмещения'];
    const chemKeysAscii = ['isotop', 'chemostrat', 'chemical stratigraphy', 'chemical stratigraphic'];
    const chemKeysCjk = ['同位素', '化学地层', '化学地層', '地球化学', 'изотоп', 'геохим'];
    const paleoKeysAscii = ['paleomap', 'palaeomap', 'paleogeograph', 'palaeogeograph', 'paleocontinent', 'palaeocontinent'];
    const paleoKeysCjk = ['古地理', '古海洋', '古大陆', '板块重建', 'палеогеограф', 'палеокарт', 'палеоконтинент'];
    const scatKeysAscii = ['scatter plot', 'scatterplot', 'scatter diagram', 'biplot', 'crossplot', 'cross plot'];
    const scatKeysCjk = ['散点', '散布図', 'точечная диаграмма', 'рассеяни', 'рассеиван'];
    const saysRangeChart = rcaChartModeHit(blob, rangeKeysAscii, rangeKeysCjk);
    if (rcaChartModeHit(blob, zonKeysAscii, zonKeysCjk, zonPhraseRes)) {
      return saysRangeChart ? 'range_chart' : 'zonation_chart';
    }
    if (rcaChartModeHit(blob, abKeysAscii, abKeysCjk)) return 'abundance_diagram';
    if (rcaChartModeHit(blob, colKeysAscii, colKeysCjk)) return 'columnar_section';
    if (rcaChartModeHit(blob, phyloKeysAscii, phyloKeysCjk)) return 'phylogenetic_tree';
    if (!saysRangeChart) {
      if (rcaChartModeHit(blob, paleoKeysAscii, paleoKeysCjk)) return 'paleomap';
      if (rcaChartModeHit(blob, scatKeysAscii, scatKeysCjk)) return 'scatter_plot';
      if (rcaChartModeHit(blob, chemKeysAscii, chemKeysCjk)) return 'chemical_stratigraphy';
    }
    return 'range_chart';
  }

  // Resolve the chart extraction mode. Manual choice wins over the
  // heuristic. UI-REVIEW-2026-09-07: when the user picks "auto" and the
  // caption heuristic matches NOTHING, forward "auto" to the extraction
  // layer - the vision classifier decides (direct: classify here;
  // backend: the server resolves before dispatching). Previously an
  // unmatched auto silently became range_chart, extracting a zonation or
  // abundance figure with the wrong prompt.
  function rcaResolveChartMode() {
    const sel = $('chart-mode');
    const choice = sel ? sel.value : 'auto';
    if (choice === 'range_chart' || choice === 'columnar_section'
        || choice === 'abundance_diagram' || choice === 'phylogenetic_tree'
        || choice === 'zonation_chart') {
      return choice;
    }
    const detected = rcaAutoDetectChartModeDetailed();
    return detected.matched ? detected.mode : 'auto';
  }

  // Map a result object's shape to an export filename prefix. Mirrors the
  // shape detection in table.js / rca_core.exporter so exported files are
  // labeled by the chart kind they actually hold.
  //
  // M4/e (REVIEW-2026-09-20): this used to be its OWN inline set of ad-hoc
  // booleans (`sections && !species_ranges` → columnar, a `zones[0]` check
  // without the dict/rank test, abundance first, no phylo branch at all) and
  // so it drifted from the tables that were actually rendered: a range-chart
  // result whose species rows all came back empty was written out as
  // `columnar_section_result.json`, and a phylogenetic tree as
  // `range_chart_*`. It now calls the SAME predicates js/table.js dispatches
  // on (which are the `_looks_*` mirrors of rca_core/exporter.py), in the
  // SAME order, so the filename can never contradict the file's contents.
  function rcaResultFilePrefix(result) {
    if (!result) return 'range_chart_';
    if (rcaLooksZonationChart(result)) return 'zonation_chart_';
    if (rcaLooksAbundance(result)) return 'abundance_diagram_';
    if (rcaLooksColumnar(result) || rcaLooksColumnarEmptySections(result)) {
      return 'columnar_section_';
    }
    if (rcaLooksPhylogeneticTree(result)) return 'phylogenetic_tree_';
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
    $('max-tokens').value = rcaStoreGet(RCA_STORE.maxTokens, String(RCA_CONFIG.defaultMaxTokens));
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

  // Sprint B (REVIEW-2026-09-04): `silent` suppresses the "settings saved"
  // toast for IMPLICIT saves. runExtraction used to call saveSettings()
  // before every extraction, so each run popped a "设置已保存" toast even
  // though the user never pressed Save. Explicit saves (the Save button)
  // keep the toast. A persistence FAILURE still toasts in both modes —
  // FR3's private-mode/quota warning is actionable and must not be muted.
  function saveSettings(silent) {
    // FR3: collect per-write results so we can warn when localStorage is
    // unavailable (private mode / quota exceeded) instead of falsely
    // claiming "settings saved".
    const writes = [
      [RCA_STORE.endpoint, rcaStoreSet(RCA_STORE.endpoint, $('endpoint').value.trim() || RCA_CONFIG.defaultEndpoint)],
      [RCA_STORE.model, rcaStoreSet(RCA_STORE.model, $('model').value.trim() || RCA_CONFIG.defaultModel)],
      [RCA_STORE.maxTokens, rcaStoreSet(RCA_STORE.maxTokens, $('max-tokens').value.trim() || String(RCA_CONFIG.defaultMaxTokens))],
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
      if (!silent) toast(t('settings.saved'));
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

  // UI-REVIEW-2026-09-05: remember how the visible alert was built so
  // switchLang() can re-translate it. Alerts are rendered once with t()
  // and are NOT covered by rcaApplyI18n's static data-i18n walk — without
  // this, switching language left the old banner in the previous language.
  // Pure t() messages store {key, params, status}; callers that pass a
  // pre-composed string store it verbatim (fmt = null).
  let _currentAlertSpec = null;

  function _alertMessage(spec) {
    if (!spec || !spec.fmt) return spec ? spec.message : '';
    let msg = t(spec.fmt.key, spec.fmt.params);
    if (spec.fmt.status) msg += ' (HTTP ' + spec.fmt.status + ')';
    return msg;
  }

  function showAlert(kind, message, rawDetail, fmt) {
    const slot = $('alert-slot');
    if (!slot) return;
    _currentAlertSpec = { kind, message, rawDetail, fmt: fmt || null };
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
    // M14 (REVIEW-2026-08-19): the CSS uses keyframes (animation), NOT
    // transitions, so `transitionend` never fires and the prior alert is
    // never removed. Listen for `animationend` instead, with a setTimeout
    // safety net so a missed event doesn't leave the alert stuck.
    const existing = slot.querySelector('.alert');
    if (existing) {
      existing.classList.add('alert-fade-out');
      existing.addEventListener('animationend', () => {
        if (existing.parentNode) existing.parentNode.removeChild(existing);
      }, { once: true });
      // Safety net: if animationend never fires (e.g. CSS engine quirk),
      // force-remove after 400ms so the alert slot never blocks a new
      // alert from appearing.
      setTimeout(() => {
        if (existing.parentNode) existing.parentNode.removeChild(existing);
      }, 400);
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
      showAlert('warning', t('err.noImage'), null, { key: 'err.noImage' });
      return;
    }
    // FIX (file-too-big): reject oversized files before the base64 read blows
    // up memory. The dropzone advertises a ~20 MB cap; enforce it here so a
    // dragged-in multi-hundred-MB TIFF can't hang the browser.
    var _maxFileBytes = (RCA_CONFIG.maxFileBytes) || (20 * 1024 * 1024);
    if (file.size > _maxFileBytes) {
      showAlert('warning', t('err.fileTooBig'), null, { key: 'err.fileTooBig' });
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
    // M3 (REVIEW-2026-09-20): stop the previous image's GBIF round too.
    // Its hints describe a result that is no longer what we are heading
    // towards, and the un-cancelled serial loop used to keep issuing
    // requests (and writing issues) long after the image changed.
    rcaCancelNameVerify();
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
      // H8 (REVIEW-2026-09-20) — double-enhancement contract.
      // rcaLoadAndMaybeResize (js/minimax.js:59-87) only runs its supersample
      // + unsharp-mask INSIDE the resize branch, i.e. the browser has
      // already pre-processed the pixels iff `enhance` was on AND a resize
      // happened. `loaded` carries no `enhanced` flag, so `resized &&
      // enhance` is the exact condition; recording it here (not at extract
      // time) is what makes a later checkbox flip unable to make us lie
      // about bytes we already produced. server.py:1671 then applies a
      // SECOND unsharp + contrast on top — which is what this flag exists to
      // prevent, matching the GUI path where load_image_b64(..., enhance=…)
      // pre-processes once and extract_range_chart never re-enhances.
      state._frontendEnhanced = !!(loaded.resized && state._enhance);
      // Sprint B (REVIEW-2026-09-04): record the decoded dimensions so a
      // language switch can still show them while a large image is still
      // decoding (img.naturalWidth reads 0 until then).
      state._previewDims = { width: loaded.width, height: loaded.height };
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
          showAlert('danger', t('err.imageRead'), null, { key: 'err.imageRead' });
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

  // UI-REVIEW-2026-09-08 (borrowed: gnfinder/GBIF): background scientific-
  // name verification of extracted species rows against the GBIF
  // species-match API (CORS-enabled, key-free). Caps at 20 unique names,
  // marks FUZZY matches >= 50 confidence and NONE matches as review
  // hints, and renders them via rcaRenderNameIssues. Fail-silent: any
  // network trouble just skips the block.
  const GBIF_MATCH_URL = 'https://api.gbif.org/v1/species/match?verbose=true&name=';
  const NAME_VERIFY_MAX = 20;
  // M3 (REVIEW-2026-09-20): the loop used to be an unbounded serial `fetch`
  // chain — no signal, no timeout, no total budget — so a stalled GBIF
  // request kept a dead round alive, and its late answers wrote into the
  // results panel of a DIFFERENT image. The two constants below mirror
  // rca_core/names.py (verify_name_gbif timeout=15.0,
  // DEFAULT_BATCH_BUDGET_SECONDS=60.0, checked BEFORE every round-trip), so
  // both transports give up on the hints at the same point and never on the
  // extraction.
  const NAME_VERIFY_TIMEOUT_MS = 15000;
  const NAME_VERIFY_BUDGET_MS = 60000;

  // Mirror of rca_core/names.py:clean_name_for_lookup - the same input
  // must produce the same GBIF query string on both transports. Two defects
  // fixed in the 2026-09-10 review:
  //   1. the qualifier / ex-gr patterns carried literal 0x08 (backspace)
  //      bytes where a word-boundary token was intended, so neither
  //      substitution ever matched ordinary chart text - "Genus cf. species"
  //      reached GBIF un-cleaned;
  //   2. the trailing "sp." strip and the leading-punctuation trim were
  //      missing, so "Genus sp." and ",Genus yini" diverged from Python.
  function rcaCleanNameForLookup(species) {
    let s = String(species || '').trim();
    if (!s) return '';
    s = s.replace(/\([^)]*\)/g, ' ');                    // author/year
    s = s.replace(/\b(cf|aff|cf\.|aff\.)\s+/gi, '');      // qualifiers
    s = s.replace(/\b(ex\s+gr\.?|gr\.?|s\.\s?l\.?|s\.\s?s\.?|sensu|near)(?![a-z])/gi, ' ');
    s = s.replace(/\bsp\.?$/i, '');                       // trailing sp.
    s = s.replace(/\?/g, '');
    s = s.replace(/(^|\s)[A-Z]\.\s*(?=[a-z])/g, '$1');  // abbrev. genus
    s = s.replace(/\s+/g, ' ').replace(/^[ .,-]+|[ .,-]+$/g, '');
    return s.length > 0 && s.length <= 100 ? s : '';
  }

  function rcaNameIssuesFromGbif(payload) {
    const mt = String((payload && payload.matchType) || 'NONE');
    const conf = parseFloat(payload && payload.confidence) || 0;
    const canonical = String((payload && payload.canonicalName) || '');
    if (mt === 'FUZZY' && conf >= 50 && canonical) {
      return [{ msg_key: 'names.fuzzy', name: payload.query || canonical,
                suggestion: canonical, confidence: conf }];
    }
    if (mt === 'NONE') {
      return [{ msg_key: 'names.unmatched',
                name: (payload && payload.query) || canonical || '' }];
    }
    return [];
  }

  // M3 (REVIEW-2026-09-20): cancel the in-flight GBIF round. Called from
  // handleFile / resetUpload / cancel-btn / runExtraction / a new verify
  // round, so a dead image can never write hints into the current results
  // panel. Safe to call when nothing is in flight.
  function rcaCancelNameVerify() {
    const ctl = state._nameAbort;
    state._nameAbort = null;
    if (ctl) {
      try { ctl.abort(); } catch (_e) { /* already aborted */ }
    }
  }

  async function rcaVerifySpeciesNamesAsync(result) {
    // M3: a fresh round owns the block now. Clear BOTH the rendered hints
    // and `state._nameIssues` up front — the previous version only wiped
    // the DOM, so the stale list survived in state and renderCurrentResult
    // (language switch, re-render) repainted the PREVIOUS image's names.
    rcaCancelNameVerify();
    state._nameIssues = [];
    if (typeof rcaRenderNameIssues === 'function') {
      try { rcaRenderNameIssues([]); } catch (_e) { /* never abort here */ }
    }
    if (!result || !Array.isArray(result.species_ranges)) return;
    const host = document.getElementById('names-verify-slot');
    if (!host) return;
    host.innerHTML = '';
    // M3: capture the token this round belongs to. Any newer file
    // selection / extraction / reset bumps state.extractToken, and this
    // round then drops its (possibly already partially collected) issues.
    const myToken = state.extractToken;
    const batch = new AbortController();
    state._nameAbort = batch;
    const deadline = Date.now() + NAME_VERIFY_BUDGET_MS;
    // Ownership test: still the newest round, not cancelled, and still
    // describing the result currently on screen.
    function stillCurrent() {
      return !batch.signal.aborted
        && state._nameAbort === batch
        && myToken === state.extractToken;
    }
    const seen = new Map();  // cleaned -> original
    for (const row of result.species_ranges.slice(0, 40)) {
      const cleaned = rcaCleanNameForLookup(row && row.species);
      if (cleaned && !seen.has(cleaned) && seen.size < NAME_VERIFY_MAX) {
        seen.set(cleaned, row.species);
      }
    }
    if (seen.size === 0) {
      if (state._nameAbort === batch) state._nameAbort = null;
      return;
    }
    const issues = [];
    for (const [cleaned, original] of seen) {
      if (!stillCurrent()) return;
      // Mirror of names.py: the batch budget is checked BEFORE every
      // round-trip (never mid-request), so a stalled GBIF can't push the
      // hint block past the shared 60 s ceiling. Fail-open: dropping the
      // rest of the names must not affect the extraction itself.
      if (Date.now() >= deadline) break;
      // Per-request controller: linked to the batch one (so a cancel
      // reaches the live fetch) and armed with the 15 s timeout.
      const req = new AbortController();
      const onBatchAbort = () => { try { req.abort(); } catch (_e) { /* noop */ } };
      batch.signal.addEventListener('abort', onBatchAbort);
      const timer = setTimeout(onBatchAbort, NAME_VERIFY_TIMEOUT_MS);
      try {
        const resp = await fetch(GBIF_MATCH_URL + encodeURIComponent(cleaned),
          { signal: req.signal });
        if (!resp.ok) continue;
        const payload = await resp.json();
        for (const iss of rcaNameIssuesFromGbif(
            Object.assign({ query: original }, payload))) {
          issues.push(iss);
        }
      } catch (_e) { /* fail-silent: timeout / abort / network — skip this name */
      } finally {
        clearTimeout(timer);
        batch.signal.removeEventListener('abort', onBatchAbort);
      }
    }
    if (state._nameAbort === batch) state._nameAbort = null;
    if (!stillCurrent()) return;
    state._nameIssues = issues;
    if (typeof rcaRenderNameIssues === 'function' && issues.length > 0) {
      rcaRenderNameIssues(issues);
    }
  }

  async function runExtraction() {
    if (state.busy) return;
    const apiKey = $('api-key').value.trim();
    if (!apiKey) {
      showAlert('warning', t('err.noKey'), null, { key: 'err.noKey' });
      return;
    }
    if (!state.dataUrl) {
      showAlert('warning', t('err.noImage'), null, { key: 'err.noImage' });
      return;
    }
    const endpoint = $('endpoint').value.trim();
    if (!endpoint) {
      showAlert('warning', t('err.noEndpoint'), null, { key: 'err.noEndpoint' });
      return;
    }
    clearAlert();
    // M42: persist any settings edits the user made since the last explicit
    // Save so the in-flight extraction always uses the values currently in
    // the form (rather than the last saved snapshot).
    // Sprint B (REVIEW-2026-09-04): silent=true — this is an IMPLICIT save
    // on the extract path; the user never pressed Save, so the success
    // toast must not fire (previously every extraction popped "设置已保存").
    saveSettings(true);
    // FR2: bump token so any concurrent Reset / new file selection
    // invalidates this in-flight request.
    state.expectedToken += 1;
    state.extractToken = state.expectedToken;
    const myToken = state.extractToken;
    // FIX-6: fresh AbortController for this extraction; cancel-btn aborts it.
    const abort = new AbortController();
    state.abort = abort;
    // M3 (REVIEW-2026-09-20): a re-run supersedes any GBIF round still
    // running for the PREVIOUS answer — kill it before it races the new
    // result's own hints.
    rcaCancelNameVerify();
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
        // UI-REVIEW-2026-09-07: direct-mode "auto" classification stage —
        // show "Detecting chart type…" while the classifier round-trip runs.
        onStage: (stage) => {
          if (stage === 'classifying') setBusyLabel({ i18nKey: 'status.classifying' });
        },
        // FIX (force-rerun): bypass server cache on explicit user request.
        force_rerun: !!state._forceRerun,
        // FIX (enhance): pre-process image to boost VLM recognition.
        // H8 (REVIEW-2026-09-20): ask the backend for the PIL enhancement
        // ONLY when the browser did not already sharpen these exact bytes
        // (see `state._frontendEnhanced` in handleFile). Direct transport
        // has no server stage at all, so there the browser pass is by
        // definition the only one — the flag still reports the user's
        // intent, it just has nothing to double up with.
        enhance: !!state._enhance && !state._frontendEnhanced,
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
        // Sprint B (REVIEW-2026-09-04): removed the dead `const km =
        // RCA_KEYMAP_BY_MODE[chartMode] || RCA_DEFAULT_KEYMAP` lookup — it
        // was never read (the merge below re-detects the keymap from the
        // actual data shape via rcaAutoDetectKeymap(okDatas)).
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
      showAlert('danger', t('err.network'), String(extractError), { key: 'err.network' });
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
      showAlert('danger', msg, res.errorBody || res.raw, { key: errKey, status: res.status || 0 });
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
    // score_range_chart). UI-REVIEW-2026-09-05: only fill the gap when the
    // payload has NO server-provided quality — the previous unconditional
    // recompute overwrote the server's verdict with the client scorer's,
    // and while the two scorers' weights drift the user could see "85% B"
    // turn into "91% A" for the same payload depending on transport.
    if (
      typeof globalThis.scoreRangeChart === 'function'
      && state.result
      && typeof state.result === 'object'
      && !state.result.quality
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
        t('results.chimera_warning', { n: chimeraWarnings.length }),
        null,
        { key: 'results.chimera_warning', params: { n: chimeraWarnings.length } }
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
        t('results.partialFailure', { pf, total, succeeded: total - pf }),
        null,
        { key: 'results.partialFailure', params: { pf, total, succeeded: total - pf } }
      );
    } else if (res.truncated) {
      showAlert('warning', t('err.truncated'), null, { key: 'err.truncated' });
    }

    renderCurrentResult();
    // UI-REVIEW-2026-09-08 (borrowed: gnfinder/GBIF): verify extracted
    // species names in the background; fuzzy suggestions surface as an
    // info block. Fire-and-forget, capped, fail-silent.
    rcaVerifySpeciesNamesAsync(state.result);
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
    // UI-REVIEW-2026-09-08: restore name-verification hints (re-rendered
    // on language switch so the wording follows the active language).
    if (state._nameIssues && typeof rcaRenderNameIssues === 'function') {
      rcaRenderNameIssues(state._nameIssues);
    }
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
    // M3 (REVIEW-2026-09-20): the results panel (and with it the name-hint
    // block) is about to be wiped — kill the GBIF round and its issue list
    // so nothing re-paints into the cleared page.
    rcaCancelNameVerify();
    state._nameIssues = [];
    state.expectedToken += 1;
    state.loadToken = state.expectedToken;
    state.extractToken = state.expectedToken;
    state.file = null;
    state.dataUrl = null;
    state.mediaType = null;
    // H8 (REVIEW-2026-09-20): no image on screen → no browser enhancement
    // to remember; the next upload recomputes the flag from scratch.
    state._frontendEnhanced = false;
    state.result = null;
    state.rawText = null;
    $('file-input').value = '';
    $('caption').value = '';
    $('preview-wrap').classList.add('hidden');
    $('preview-img').src = '';
    $('results-content').classList.add('hidden');
    // M15 (REVIEW-2026-08-19): preserve the #viz-host container so the
    // next render reuses it instead of re-creating the DOM node. The
    // previous `$('results-content').innerHTML = ''` evicted the
    // mount-point, requiring a fresh creation on every render — and any
    // in-progress ECharts/Plotly init would orphan its event listeners.
    const resultsContent = $('results-content');
    const vizHost = resultsContent.querySelector('#viz-host');
    resultsContent.innerHTML = '';
    if (vizHost) resultsContent.appendChild(vizHost);
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
    // UI-REVIEW-2026-09-05: re-translate the visible alert. It was rendered
    // once with t() at creation time, so a language switch left it in the
    // previous language (e.g. a zh chimera banner under an EN interface).
    if (_currentAlertSpec) {
      showAlert(
        _currentAlertSpec.kind,
        _alertMessage(_currentAlertSpec),
        _currentAlertSpec.rawDetail,
        _currentAlertSpec.fmt || undefined
      );
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
    // Sprint B (REVIEW-2026-09-04): img.naturalWidth reads 0 while a large
    // image is still decoding, so a language switch mid-decode previously
    // rendered "0 x 0". Fall back to the dimensions recorded at load time;
    // only show the placeholder when neither source has a value.
    const dims = state._previewDims || null;
    const w = img.naturalWidth || (dims && dims.width) || 0;
    const h = img.naturalHeight || (dims && dims.height) || 0;
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

    // M2 (REVIEW-2026-09-20): a click handler receives the MouseEvent, and
    // `saveSettings(silent)` read that object as `silent` — truthy — so the
    // "settings saved" toast NEVER appeared on an explicit Save click (only
    // the persistence-failure toast did). Pass the flag explicitly.
    $('save-settings').addEventListener('click', () => saveSettings(false));

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
        // M3 (REVIEW-2026-09-20): "Cancel" means cancel — an in-flight GBIF
        // round from the previous answer is part of the same work.
        rcaCancelNameVerify();
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
    // LOW 10 (REVIEW-2026-08-19): a `dragend` fired outside the window
    // (e.g. user drags off-screen and releases) leaves the drop overlay
    // stuck. Listen on `window` and force-hide so the UI never blocks
    // a fresh file drop.
    window.addEventListener('dragend', () => {
      dragDepth = 0;
      if (dropOverlay) dropOverlay.classList.remove('show');
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
      // F-15 / M16 (REVIEW-2026-08-19): Allow Ctrl+Enter inside <textarea>
      // (caption) — the original F-15 comment described this intent but the
      // code did the OPPOSITE (`if (tag === 'TEXTAREA') return;`), so the
      // shortcut silently failed inside the caption box. Keep the
      // <input type="text"> block (so we don't hijack newlines in field
      // inputs); drop the textarea return so Ctrl+Enter in the caption
      // triggers extraction.
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
