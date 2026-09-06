"""Regression tests for the vision chart-type classifier and the upgraded
"auto" mode (UI-REVIEW-2026-09-07).

Auto pipeline: caption keyword heuristic first; when it matches nothing,
classify the image itself with a cheap vision call; range_chart as the
final fallback. The resolved type is stamped on ExtractResult.mode_used.
"""

import os

import pytest

from rca_core import extractor
from rca_core.chart_mode import auto_detect_chart_mode_ex
from rca_core.extractor import (
    ExtractResult,
    extract,
    normalize_chart_classification,
    resolve_auto_mode,
)


# ---------------------------------------------------------------------------
# normalize_chart_classification
# ---------------------------------------------------------------------------

def test_normalize_valid_classification():
    out = normalize_chart_classification(
        {"chart_type": "zonation_chart", "reason": "zones columns", "confidence": 0.9}
    )
    assert out == {"chart_type": "zonation_chart",
                   "reason": "zones columns", "confidence": 0.9}


def test_normalize_unknown_type_degrades():
    out = normalize_chart_classification({"chart_type": "banana", "confidence": 0.9})
    assert out["chart_type"] == "unknown"


def test_normalize_missing_fields_degrade():
    out = normalize_chart_classification({})
    assert out["chart_type"] == "unknown" and out["confidence"] == 0.0


def test_normalize_non_dict_degrades():
    assert normalize_chart_classification("garbage")["chart_type"] == "unknown"


def test_normalize_confidence_clamped():
    out = normalize_chart_classification({"chart_type": "range_chart",
                                          "confidence": "5"})
    assert out["confidence"] == 1.0


# ---------------------------------------------------------------------------
# auto_detect_chart_mode_ex (matched flag)
# ---------------------------------------------------------------------------

def test_ex_positive_hits_report_matched():
    assert auto_detect_chart_mode_ex("pollen diagram") == ("abundance_diagram", True)
    assert auto_detect_chart_mode_ex("Correlation of Triassic zones") == ("zonation_chart", True)


def test_ex_unmatched_reports_false():
    assert auto_detect_chart_mode_ex("totally unrelated caption") == ("range_chart", False)


def test_legacy_wrapper_still_works():
    from rca_core.chart_mode import auto_detect_chart_mode
    assert auto_detect_chart_mode("pollen diagram") == "abundance_diagram"


# ---------------------------------------------------------------------------
# resolve_auto_mode (mocked vision call)
# ---------------------------------------------------------------------------

class _FakeCls:
    def __init__(self, chart_type, conf, ok=True):
        self.ok = ok
        self.data = {"chart_type": chart_type, "reason": "r", "confidence": conf}


def test_resolve_auto_mode_text_hit_skips_vision(monkeypatch):
    called = {"n": 0}

    def _should_not_be_called(**kw):
        called["n"] += 1
        raise AssertionError("vision classifier must not run on a text hit")

    monkeypatch.setattr(extractor, "classify_chart_image", _should_not_be_called)
    mode, cls = resolve_auto_mode(caption="pollen percentage diagram",
                                  filename="", image_b64="x", media_type="image/png")
    assert mode == "abundance_diagram" and cls is None
    assert called["n"] == 0


def test_resolve_auto_mode_vision_hit(monkeypatch):
    monkeypatch.setattr(extractor, "classify_chart_image",
                        lambda **kw: _FakeCls("zonation_chart", 0.9))
    mode, cls = resolve_auto_mode(caption="", filename="", image_b64="x",
                                  media_type="image/png")
    assert mode == "zonation_chart"
    assert cls is not None and cls.data["chart_type"] == "zonation_chart"


def test_resolve_auto_mode_unknown_falls_back(monkeypatch):
    monkeypatch.setattr(extractor, "classify_chart_image",
                        lambda **kw: _FakeCls("unknown", 0.1))
    mode, cls = resolve_auto_mode(caption="", filename="", image_b64="x",
                                  media_type="image/png")
    assert mode == "range_chart" and cls is not None


def test_resolve_auto_mode_low_confidence_falls_back(monkeypatch):
    monkeypatch.setattr(extractor, "classify_chart_image",
                        lambda **kw: _FakeCls("paleomap", 0.3))
    mode, _cls = resolve_auto_mode(caption="", filename="", image_b64="x",
                                   media_type="image/png")
    assert mode == "range_chart"


def test_resolve_auto_mode_classifier_exception_falls_back(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(extractor, "classify_chart_image", _boom)
    mode, cls = resolve_auto_mode(caption="", filename="", image_b64="x",
                                  media_type="image/png")
    assert mode == "range_chart"
    # UI-REVIEW-2026-09-07 fix: a vision failure keeps its provenance —
    # the synthetic failed result (ok=False) means callers label the mode
    # source "vision" (attempted), never "text".
    assert cls is not None and cls.ok is False
    assert cls.data["chart_type"] == "unknown"


# ---------------------------------------------------------------------------
# extract() auto end-to-end (mocked dispatch + classifier)
# ---------------------------------------------------------------------------

def test_extract_auto_text_path_stamps_mode(monkeypatch):
    captured = {}

    def fake_dispatch(**kw):
        captured.update(kw)
        return ExtractResult(ok=True, data={"species_ranges": []})

    monkeypatch.setitem(extractor._MODE_DISPATCH, "abundance_diagram", fake_dispatch)
    r = extract(mode="auto", image_b64="abc", media_type="image/png",
                caption="Fig.1 pollen percentage diagram")
    assert r.mode_used == "abundance_diagram" and r.mode_source == "text"


def test_extract_auto_vision_path_stamps_mode(monkeypatch):
    monkeypatch.setattr(extractor, "classify_chart_image",
                        lambda **kw: _FakeCls("zonation_chart", 0.9))
    seen = {}
    monkeypatch.setitem(extractor._MODE_DISPATCH, "zonation_chart",
                        lambda **kw: seen.setdefault("mode", kw.get("caption")) or
                        ExtractResult(ok=True, data={}))
    r = extract(mode="auto", image_b64="abc", media_type="image/png", caption="")
    assert r.mode_used == "zonation_chart" and r.mode_source == "vision"


def test_extract_explicit_mode_has_no_auto_stamp(monkeypatch):
    def fake_dispatch(**kw):
        return ExtractResult(ok=True, data={})

    monkeypatch.setitem(extractor._MODE_DISPATCH, "range_chart", fake_dispatch)
    r = extract(mode="range_chart", image_b64="abc", media_type="image/png")
    assert r.mode_used == "" and r.mode_source == ""


# ---------------------------------------------------------------------------
# server whitelist
# ---------------------------------------------------------------------------

def test_server_source_whitelists_auto():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "server.py"), encoding="utf-8") as f:
        src = f.read()
    assert "'auto'" in src
    assert "resolve_auto_mode" in src


def test_mixed_caption_range_chart_wins_over_zonation():
    # E2E fig_23: "Columnar section with radiolarian range chart ...
    # Zonation of Early Cretaceous radiolarians" - the range-chart body is
    # the primary content; zonation columns are captured as biozone fields.
    mode, matched = auto_detect_chart_mode_ex(
        "Figure 4. Columnar section with radiolarian range chart "
        "(scale bar is 50 um). Zonation of Early Cretaceous radiolarians")
    assert (mode, matched) == ("range_chart", True)


def test_pure_zonation_still_routes_to_zonation():
    mode, matched = auto_detect_chart_mode_ex(
        "Fig. 1. An example of high-resolution radiolarian zonation")
    assert (mode, matched) == ("zonation_chart", True)


# ---------------------------------------------------------------------------
# Truncation repair (Level 3.5) — E2E fig_19: a max_tokens cut inside the
# abundances array used to make safe_json_loads Level 4 pick ONE stray row
# object while discarding 60+ completed rows above the cut.
# ---------------------------------------------------------------------------

def _fig19_raw() -> str:
    import json as _json
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tests", "fixtures", "truncation", "fig19_truncated_raw.json")
    with open(p, encoding="utf-8") as f:
        return _json.load(f)["raw"]


def test_truncation_repair_recovers_completed_rows():
    from rca_core.json_utils import safe_json_loads
    parsed = safe_json_loads(_fig19_raw())
    assert len(parsed.get("sites") or []) == 3
    assert len(parsed.get("abundances") or []) == 61


def test_truncation_repair_synthetic_case():
    from rca_core.json_utils import safe_json_loads
    raw = ('{"sites": [{"name": "S1"}], '
           '"abundances": [{"taxon": "A", "abundance": "10"}, '
           '{"taxon": "B", "abun')
    parsed = safe_json_loads(raw)
    assert len(parsed.get("sites") or []) == 1
    # The partial last row is kept too: repair closes at the last complete
    # FIELD ("taxon": "B"), so B survives with abundance missing — more
    # useful for an operator than discarding the row entirely.
    assert len(parsed.get("abundances") or []) == 2
    assert parsed["abundances"][0]["taxon"] == "A"
    assert parsed["abundances"][1]["taxon"] == "B"
    assert parsed["abundances"][1].get("abundance", "") == ""


def test_truncation_repair_full_chain_normalize():
    from rca_core.extractor import normalize_abundance_result
    from rca_core.json_utils import safe_json_loads
    data = normalize_abundance_result(safe_json_loads(_fig19_raw()))
    assert len(data["abundances"]) == 61
    assert data["abundances"][0]["taxon"] == "Amphimelissa setosa"
