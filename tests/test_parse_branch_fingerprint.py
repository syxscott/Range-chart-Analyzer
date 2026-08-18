"""Regression test for image fingerprint on the JSON parse-failure branch.

REVIEW-2026-08-17 (P1-1): every extract_* function computes
``image_sha256 = compute_image_sha256_from_b64(image_b64)`` and is
expected to surface that fingerprint on every returned ``ExtractResult``,
including the error branches. The 5-year audit contract is "given any
recorded result, you can find the original image" — losing the
fingerprint on parse failure breaks that for every record whose model
reply was unparseable (which is exactly the records a researcher most
needs to inspect years later).

The previous code dropped ``image_sha256`` on the ``safe_json_loads``
``ValueError`` branch in:
  - extract_range_chart          (extractor.py ~L845)
  - extract_chemical_stratigraphy (~L2026)
  - extract_paleomap             (~L2270)
  - extract_scatter_plot         (~L2476)

while extract_columnar_section / extract_abundance_diagram /
extract_phylogenetic_tree already had it. The bug regressed silently
because the parser-failure path is rarely exercised in unit tests.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest

from rca_core.extractor import (
    extract_range_chart,
    extract_chemical_stratigraphy,
    extract_paleomap,
    extract_scatter_plot,
)
from rca_core.image_hash import compute_image_sha256_from_b64


# A 1x1 transparent PNG, base64-encoded.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


# Stub call_llm_api to return text that safe_json_loads cannot parse.
# We import safe_json_loads lazily to avoid pulling in heavy deps at
# module-import time on the test runner.
def _stub_unparseable(provider, system_prompt, image_b64, media_type,
                     user_text, max_tokens, timeout_sec,
                     capture_error_body, progress_callback=None):
    return ("not valid json { ]]]", False, 200, "", {})


EXTRACTORS = [
    ("extract_range_chart", extract_range_chart, "range_chart"),
    ("extract_chemical_stratigraphy", extract_chemical_stratigraphy,
     "chemical_stratigraphy"),
    ("extract_paleomap", extract_paleomap, "paleomap"),
    ("extract_scatter_plot", extract_scatter_plot, "scatter_plot"),
]


@pytest.mark.parametrize(
    "name,fn,mode", EXTRACTORS,
    ids=[n for n, _, _ in EXTRACTORS],
)
def test_parse_failure_branch_carries_image_sha256(name, fn, mode):
    """When the model's reply fails JSON parsing, the returned
    ``ExtractResult.image_sha256`` must still match the input image's
    SHA-256 — otherwise the audit record for the failed extraction
    cannot be traced back to the source image."""
    expected_sha = compute_image_sha256_from_b64(PNG_B64)
    with patch("rca_core.extractor.call_llm_api", side_effect=_stub_unparseable):
        # Patch the per-module lazy imports too (each mode imports its
        # own prompt constant from .prompt inside its function body).
        with patch("rca_core.prompt.RANGE_CHART_SYSTEM_PROMPT", "stub"):
            with patch("rca_core.prompt.CHEMICAL_STRATIGRAPHY_SYSTEM_PROMPT", "stub"):
                with patch("rca_core.prompt.PALEOMAP_SYSTEM_PROMPT", "stub"):
                    with patch("rca_core.prompt.SCATTER_PLOT_SYSTEM_PROMPT", "stub"):
                        result = fn(
                            api_key="k",
                            image_b64=PNG_B64,
                            media_type="image/png",
                            caption="",
                            chart_lang="auto",
                        )

    assert result.ok is False, (
        f"{name}: stubbed unparseable reply should yield ok=False"
    )
    assert result.error_key == "err.parse", (
        f"{name}: expected err.parse, got {result.error_key!r}"
    )
    assert result.image_sha256 == expected_sha, (
        f"{name}: image_sha256 lost on parse-failure branch — "
        f"got {result.image_sha256!r}, expected {expected_sha!r}"
    )
    assert len(result.image_sha256) == 64, (
        f"{name}: image_sha256 must be a 64-char hex SHA-256, "
        f"got {len(result.image_sha256)} chars"
    )