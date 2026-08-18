// Shared fetch-mock payload + injection (runs inside the page context).
// Exposes window.__setMock(mode, delayMs) for scenario switching.

const MOCK_PAYLOAD = {
  ok: true,
  status: 'ok',
  data: {
    metadata: { chart_type: 'range_chart', extraction_version: '1.0.0', synthetic: true, seed: 0 },
    sections: [
      { name: 'Section X', age_range: 'Late Jurassic', formations: ['Corallian'], formation_thickness_m: '12.5', coordinates: 'N52.45° W0.12°' },
      { name: 'Section Y', age_range: 'Middle Jurassic', formations: ['Oxford Clay'], formation_thickness_m: '28.0', coordinates: 'N51.51° W0.51°' }
    ],
    species_ranges: [
      { species: 'Gryphaea arcuata', section: 'Section Y', range_top: '13', range_base: '8', biozone: 'Koslovites Biozone', agreement_count: 3, agreement: '3/3' },
      { species: 'Dactylioceras commune', section: 'Section X', range_top: '8', range_base: '3', biozone: 'Aspidoceras Zone', agreement_count: 2, agreement: '2/3' },
      { species: 'Ammonites ? koslovensis', section: 'Section X', range_top: '13', range_base: '10', biozone: 'Aspidoceras Zone', agreement_count: 1, agreement: '1/3' },
      { species: 'Lingula sp.', section: 'Section Y', range_top: '7', range_base: '2', biozone: 'Koslovites Biozone', agreement_count: 3, agreement: '3/3' },
      { species: 'Rhynchonella loxiae', section: 'Section X', range_top: '5', range_base: '1', biozone: 'Aspidoceras Zone', agreement_count: 3, agreement: '3/3' }
    ],
    biozones: [
      { name: 'Koslovites Biozone', section: 'Section Y', age: 'Middle Jurassic', thickness_m: '15' },
      { name: 'Aspidoceras Zone', section: 'Section X', age: 'Late Jurassic', thickness_m: '10' }
    ],
    other_fossils: ['Belemnites sp.', 'Bivalve indet.'],
    confidence: 0.87,
    runs: 3,
    chimera_warnings: ['Dactylioceras commune'],
    quality: { score: 0.85, grade: 'B', issues: [] }
  },
  raw: '{"mock":true,"species_ranges":[{"species":"Gryphaea arcuata","range_top":"13","range_base":"8"}]}',
  truncated: false,
  partial_failures: 0,
  warning: '',
  usage: { input_tokens: 1823, output_tokens: 640 },
  latency_ms: 3120
};

const MOCK_EMPTY = {
  ok: true,
  status: 'ok',
  data: {
    metadata: { chart_type: 'range_chart', extraction_version: '1.0.0' },
    sections: [],
    species_ranges: [],
    biozones: [],
    other_fossils: [],
    confidence: 0.12,
    runs: 1
  },
  raw: '',
  truncated: false,
  partial_failures: 0,
  warning: ''
};

function mockInject() {
  window.__mockMode = 'ok';
  window.__mockDelay = 1200;
  window.__mockPostCount = 0;
  window.__setMock = (mode, delay) => { window.__mockMode = mode; if (delay) window.__mockDelay = delay; };
  const origFetch = window.fetch.bind(window);
  const json = (obj, status) => new Response(JSON.stringify(obj), {
    status: status || 200,
    headers: { 'content-type': 'application/json' }
  });
  window.fetch = (url, opts) => {
    const u = String(url);
    const method = (opts && opts.method) || 'GET';
    if (!u.includes('/api/extract')) return origFetch(url, opts);
    if (method === 'GET') {
      return Promise.resolve(json({ session_token: 'mock-sess', csrf_token: 'mock-csrf' }));
    }
    window.__mockPostCount += 1;
    const mode = window.__mockMode;
    const delay = window.__mockDelay;
    if (mode === 'hang') {
      // Real-fetch semantics: abort() rejects with AbortError.
      return new Promise((resolve, reject) => {
        if (opts && opts.signal) {
          if (opts.signal.aborted) reject(new DOMException('Aborted', 'AbortError'));
          opts.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        }
      });
    }
    return new Promise((resolve) => {
      setTimeout(() => {
        if (mode === 'http500') {
          resolve(json({ ok: false, error_key: 'err.http', error_body: 'Simulated upstream 500 (mock)' }, 500));
        } else if (mode === 'fail200') {
          resolve(json({ ok: false, error_key: 'err.network', status: 502, error_body: 'Mock upstream failure (bad gateway)' }, 200));
        } else if (mode === 'empty') {
          resolve(json(MOCK_EMPTY));
        } else {
          resolve(json(MOCK_PAYLOAD));
        }
      }, delay);
    });
  };
}

mockInject();
