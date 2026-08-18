// Shared helpers for the UI-review scripts.
const path = require('path');
const OUT = 'D:/GIthub/Range-chart Analyzer/outputs/ui-review';
const IMG = 'D:/GIthub/Range-chart Analyzer/tests/fixtures/gold/range_chart/rc_synth_001/image.png';
const INJECT = require('fs').readFileSync(path.join(OUT, 'mock-inject.js'), 'utf8');

// Strip the Content-Security-Policy header from same-origin document responses
// so the UI can be exercised beyond the CSP bug (which is reported separately).
function stripCsp(page) {
  return page.route('**/*', async (route) => {
    const req = route.request();
    if (req.url().includes('/api/')) {
      await route.continue();
      return;
    }
    const resp = await route.fetch();
    const headers = { ...resp.headers() };
    delete headers['content-security-policy'];
    await route.fulfill({ response: resp, headers });
  });
}

module.exports = { OUT, IMG, INJECT, stripCsp };
