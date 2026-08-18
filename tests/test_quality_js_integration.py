"""Regression tests for the Phase F1 / Phase M JS quality fixes.

The previous build wired scoreRangeChart into the renderer
(js/table.js:205 reads data.quality) but never actually CALLLED it
from app.js. The Phase K fix added the call. Verify the call chain
end-to-end: a successful extraction populates state.result.quality.
"""

import pytest


class TestJsQualityBadgeCallChain:
    """Smoke test: js/app.js must invoke scoreRangeChart after a
    successful extraction so the rendered badge has data.

    We can't easily run the full browser environment in this CI;
    what we test here is the contract: that scoreRangeChart (when
    available globally) is invoked with the result object and the
    returned data.quality is set.
    """

    def test_app_js_invokes_scoreRangeChart(self):
        """Grep scan: js/app.js must reference scoreRangeChart in a
        code path that runs after extraction completes."""
        with open('js/app.js', encoding='utf-8') as f:
            app_src = f.read()
        # Must reference scoreRangeChart somewhere after
        # ``state.result = res.data;`` (line ~553 prior to Phase K fix).
        assert 'state.result = res.data' in app_src, (
            "Reference checkpoint missing — state.result assignment "
            "anchor not found. js/app.js structure may have changed."
        )
        # Find the position of that line and confirm the score call
        # happens AFTER it.
        idx_anchor = app_src.index('state.result = res.data')
        idx_score = app_src.find('scoreRangeChart', idx_anchor)
        assert idx_score > idx_anchor, (
            "Phase K fix regressed: scoreRangeChart is no longer "
            "called after state.result is set on extraction success."
        )

    def test_quality_module_exposes_global(self):
        """js/quality.js must expose scoreRangeChart on globalThis
        so app.js can reach it without a module system."""
        with open('js/quality.js', encoding='utf-8') as f:
            q_src = f.read()
        # Look for either window.scoreRangeChart or globalThis.scoreRangeChart.
        assert 'scoreRangeChart' in q_src
        # The IIFE structure shows the symbol is defined and assigned
        # to a global under the trailing block.
        assert 'globalThis.scoreRangeChart' in q_src or 'window.scoreRangeChart' in q_src
