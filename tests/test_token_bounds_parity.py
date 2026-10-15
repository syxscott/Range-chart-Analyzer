"""The token ceiling must be the same number in the core and in the browser.

Measured on 2026-10-15: a 22-taxon x 18-locality occurrence matrix needs
4417-5428 output tokens as one JSON record per filled cell. Both engines
shipped a 4000 default, so an entirely ordinary figure was cut off mid-answer
and the last row was never emitted at all -- with `stop_reason == "max_tokens"`
surfacing only as a warning string.

The cap is now 16000 on both sides. Nothing enforced that: there was no test
pairing DEFAULT_MAX_TOKENS with RCA_CONFIG.defaultMaxTokens (measured: zero
mentions of either name in tests/), and the two 4000s could have drifted apart
again on the next edit exactly the way they drifted into being too small.
DEFAULT_MAX_TOKENS being clamped by clamp_max_tokens(None) is not a substitute:
it only proves the core is self-consistent, which is precisely what was already
true when the figure was being truncated.

These assertions are deliberately EVALUATED, not literal: after a registry
refactor the literals disappear by definition, and a test that greps for a
number stops describing the system and starts describing its implementation.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.extractor import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    MAX_MAX_TOKENS,
    MIN_MAX_TOKENS,
    clamp_max_tokens,
)

CONFIG_JS = os.path.join(PROJECT_ROOT, "js", "config.js")

# Read RCA_CONFIG out of the real browser file in a real JS context, so the
# number under test is the one that ships -- not a copy someone kept in step.
#
# `RCA_CONFIG` is declared with `const`, which in a vm context lands in the
# script's lexical scope rather than on the context object. Evaluating an extra
# expression in the SAME script is what makes the binding visible; reading
# ctx.RCA_CONFIG instead silently yields undefined.
_DRIVER = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.env.RCA_REPO;
const ctx = { console };
ctx.globalThis = ctx; ctx.window = ctx;
vm.createContext(ctx);
const src = fs.readFileSync(path.join(ROOT, 'js/config.js'), 'utf8');
const out = vm.runInContext(src + `
;JSON.stringify({
  defaultMaxTokens: RCA_CONFIG.defaultMaxTokens,
  minMaxTokens: RCA_CONFIG.minMaxTokens,
  maxMaxTokens: RCA_CONFIG.maxMaxTokens,
})`, ctx, { filename: 'js/config.js' });
if (!out || out === '{}') {
  process.stderr.write('RCA_CONFIG not reachable in js/config.js');
  process.exit(3);
}
console.log(out);
"""


def _js_config() -> dict:
    r = subprocess.run(
        ["node", "-e", _DRIVER],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env={**os.environ, "RCA_REPO": PROJECT_ROOT},
    )
    assert r.returncode == 0, "node driver failed: %s" % (r.stderr or r.stdout)
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def js():
    return _js_config()


class TestTokenBoundsParity:
    def test_defaults_are_equal(self, js):
        # The regression this file exists for: the two 4000s were equal and
        # both wrong, so equality alone is not the assertion -- see
        # test_default_clears_a_dense_matrix on the Python side.
        assert js["defaultMaxTokens"] == DEFAULT_MAX_TOKENS, (
            "core DEFAULT_MAX_TOKENS=%d but js/config.js defaultMaxTokens=%d"
            % (DEFAULT_MAX_TOKENS, js["defaultMaxTokens"]))

    def test_max_caps_are_equal(self, js):
        assert js["maxMaxTokens"] == MAX_MAX_TOKENS

    def test_min_bounds_are_equal(self, js):
        assert js["minMaxTokens"] == MIN_MAX_TOKENS

    def test_default_sits_inside_the_bounds(self, js):
        assert MIN_MAX_TOKENS <= DEFAULT_MAX_TOKENS <= MAX_MAX_TOKENS


class TestDefaultClearsADenseMatrix:
    """The bound that actually matters, checked against a real measurement."""

    # Measured with extract_occurrence_matrix against a real 22x18 figure:
    # output_tokens came back 5428 at a 32000 ceiling and 4417 on a second
    # run. Both are above the old 4000.
    LOWEST_OBSERVED_OUTPUT_TOKENS = 4417

    def test_default_exceeds_what_the_figure_needed(self):
        assert DEFAULT_MAX_TOKENS > self.LOWEST_OBSERVED_OUTPUT_TOKENS, (
            "DEFAULT_MAX_TOKENS=%d does not clear the %d tokens this figure "
            "actually needed; results will be truncated again"
            % (DEFAULT_MAX_TOKENS, self.LOWEST_OBSERVED_OUTPUT_TOKENS))

    def test_clamp_falls_back_to_that_same_default(self):
        assert clamp_max_tokens(None) == DEFAULT_MAX_TOKENS
        assert clamp_max_tokens("not a number") == DEFAULT_MAX_TOKENS
        assert clamp_max_tokens(0) == MIN_MAX_TOKENS
        assert clamp_max_tokens(10 ** 9) == MAX_MAX_TOKENS
