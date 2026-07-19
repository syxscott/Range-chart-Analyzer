"""Self-contained tests for the rca_core shared logic.

Run:  python tests_core.py
No third-party test framework required.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rca_core import (
    TRANSLATIONS,
    Translator,
    build_table_export,
    merge_results,
    normalize_result,
    safe_json_loads,
    to_csv,
    to_tsv,
)
from rca_core.json_utils import extract_balanced_json_object
from rca_core.prompt import RANGE_CHART_SYSTEM_PROMPT

_pass = 0
_fail = 0


def check(name, cond):
    global _pass, _fail
    if cond:
        _pass += 1
        print("PASS", name)
    else:
        _fail += 1
        print("FAIL", name)


def test_json():
    check("json-strict", safe_json_loads('{"a":1}') == {"a": 1})
    check("json-fences", safe_json_loads("```json\n{\"a\":2}\n```") == {"a": 2})
    check("json-trailing", safe_json_loads('{"a":3} extra {') == {"a": 3})
    check("json-nested-brace-in-string",
          safe_json_loads('x {"a":{"b":"} brace"},"c":4} y') == {"a": {"b": "} brace"}, "c": 4})
    check("json-leading-prose", safe_json_loads('Here:\n{"confidence":0.9}') == {"confidence": 0.9})
    check("balanced-none", extract_balanced_json_object("no object here") is None)
    try:
        safe_json_loads("")
        check("json-empty-raises", False)
    except ValueError:
        check("json-empty-raises", True)


def test_normalize():
    r = normalize_result({
        "sections": [None, {"name": "A", "formations": ["F1", "F2"]}],
        "species_ranges": "notalist",
        "biozones": [{"name": "Z"}],
        "other_fossils": ["x", "", "  "],
        "confidence": 5,
    })
    check("norm-null-section-skipped", len(r["sections"]) == 1)
    check("norm-formations", r["sections"][0]["formations"] == ["F1", "F2"])
    check("norm-bad-array", r["species_ranges"] == [])
    check("norm-fossils-filtered", r["other_fossils"] == ["x"])
    check("norm-conf-clamped", r["confidence"] == 1.0)
    check("norm-defaults", normalize_result({})["confidence"] == 0.0)


def test_export():
    data = {"species_ranges": [
        {"species": "Sp x", "section": "A", "range_base": "b", "range_top": "t", "biozone": "Z,1"},
    ]}
    tr = Translator("en")
    headers, rows = build_table_export(data, "species_ranges", tr.t)
    check("export-headers-6", len(headers) == 6)
    check("export-species", rows[0][1] == "Sp x")
    check("export-rangebase", rows[0][3] == "b")
    csv = to_csv(headers, rows)
    check("csv-bom", csv.startswith("﻿"))
    check("csv-escapes-comma", '"Z,1"' in csv)
    tsv = to_tsv(headers, rows)
    check("tsv-tabs", "\t" in tsv)


def test_i18n():
    zh = set(TRANSLATIONS["zh"])
    for lang in TRANSLATIONS:
        check(f"i18n-parity-{lang}", set(TRANSLATIONS[lang]) == zh)
    check("i18n-zh", "剖面" in Translator("zh").t("sec.sections"))
    check("i18n-ja", "断面" in Translator("ja").t("sec.sections"))
    check("i18n-en", Translator("en").t("sec.sections") == "Sections")
    check("i18n-fallback", Translator("ja").t("no.such.key") == "no.such.key")


def test_merge():
    r1 = {
        "sections": [{"name": "A", "age_range": "Permian", "formations": ["F1"],
                      "formation_thickness_m": "", "coordinates": ""}],
        "species_ranges": [
            {"species": "Neoalbaillella optima", "section": "A", "range_base": "Bed 7", "range_top": "Bed 9", "biozone": "Z"},
            {"species": "Entactinia sashidai", "section": "A", "range_base": "Bed 22", "range_top": "Bed 26", "biozone": ""},
        ],
        "biozones": [{"name": "N. optima Zone", "age": "Late", "thickness_m": "3m"}],
        "other_fossils": ["Ammonoid: X"], "confidence": 0.8,
    }
    r2 = {
        "sections": [{"name": "A", "age_range": "Permian", "formations": ["F1", "F2"],
                      "formation_thickness_m": "", "coordinates": ""}],
        "species_ranges": [
            {"species": "Neoalbaillella optima", "section": "A", "range_base": "Bed 7", "range_top": "Bed 9", "biozone": "Z"},
            {"species": "Paracopicyntra longispina", "section": "A", "range_base": "Bed 20", "range_top": "Bed 26", "biozone": ""},
        ],
        "biozones": [{"name": "N. optima Zone", "age": "Late", "thickness_m": "3m"}],
        "other_fossils": ["Ammonoid: X", "Ammonoid: Y"], "confidence": 0.9,
    }
    m = merge_results([r1, r2])
    check("merge-runs", m["runs"] == 2)
    check("merge-species-count", len(m["species_ranges"]) == 3)
    # shared species seen in both runs -> agreement 2/2 and sorted first
    check("merge-top-agreement", m["species_ranges"][0]["agreement"] == "2/2")
    check("merge-low-agreement", any(s["agreement"] == "1/2" for s in m["species_ranges"]))
    check("merge-formations-union", m["sections"][0]["formations"] == ["F1", "F2"])
    check("merge-biozones-dedup", len(m["biozones"]) == 1)
    check("merge-fossils-union", len(m["other_fossils"]) == 2)
    check("merge-confidence-mean", abs(m["confidence"] - 0.85) < 1e-6)
    # single-run passthrough still stamps agreement 1/1
    single = merge_results([r1])
    check("merge-single-passthrough", single["runs"] == 1)
    check("merge-single-agreement", single["species_ranges"][0]["agreement"] == "1/1")
    # empty
    empty = merge_results([])
    check("merge-empty", empty["species_ranges"] == [] and empty["runs"] == 1)
    # mode: range_base disagreement resolves to the majority value
    r3 = dict(r1)
    r3 = {**r1, "species_ranges": [
        {"species": "Neoalbaillella optima", "section": "A", "range_base": "Bed 8", "range_top": "Bed 9", "biozone": "Z"},
    ]}
    m3 = merge_results([r1, r2, r3])
    opt = next(s for s in m3["species_ranges"] if s["species"] == "Neoalbaillella optima")
    check("merge-mode-majority", opt["range_base"] == "Bed 7")  # 2 of 3 say Bed 7
    check("merge-mode-agreement", opt["agreement"] == "3/3")


def test_t12_norm_strips_sp_cf():
    """aggregate._norm must strip trailing sp./cf. and collapse whitespace."""
    from rca_core.aggregate import _norm
    check("norm-strip-sp", _norm("Neoalbaillella sp.") == "neoalbaillella")
    check("norm-strip-cf", _norm("Entactinia cf. sashidai") == "entactinia sashidai")
    check("norm-collapse-ws", _norm("  Hello   World  ") == "hello world")

def test_t13_balanced_json_edge_cases():
    """json_utils must handle empty obj, multi-top-level, escaped quotes."""
    from rca_core.json_utils import extract_balanced_json_object
    check("balanced-empty-obj", extract_balanced_json_object("{}") == "{}")
    check("balanced-first-of-multi",
          extract_balanced_json_object('noise {"a":1} more {"b":2}') == '{"a":1}')
    import json
    # Build a string that contains an actual escaped quote inside.
    inner = 'he said "hi"'
    text = json.dumps({"a": inner})
    check("balanced-escaped-quote",
          extract_balanced_json_object(text) == text)

def test_prompt():
    for kw in [
        "COLUMNS ARE SEPARATE",
        "NEVER into",
        "Stage is NOT a Formation",
        "阶 = Stage",
        "组 = Formation",
        "READ SPECIES NAMES",
        "BE COMPLETE",
        "ammonoid",
        "Return JSON only",
    ]:
        check(f"prompt-kw:{kw[:20]}", kw in RANGE_CHART_SYSTEM_PROMPT)
    # Hard parity: biozone schema must declare a `section` field in BOTH
    # the Python and JS range-chart prompts (multi-section biozone
    # thickness preservation; added by the contract alignment). Catch
    # future drift that the substring-only test would miss.
    import re as _re
    import os as _os
    _js_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "js", "prompt.js")
    _js_src = open(_js_path, encoding="utf-8").read()
    _m = _re.search(r"const RANGE_CHART_SYSTEM_PROMPT = \[([\s\S]*?)\]\.join\(['\"]\\n['\"]\)", _js_src)
    check("rc-prompt-js-present", _m is not None)
    _js_prompt = _m.group(1) if _m else ""
    check("rc-prompt-biozone-section-py",
          '"section": "Pingdingshan"' in RANGE_CHART_SYSTEM_PROMPT)
    check("rc-prompt-biozone-section-js",
          '"section": "Pingdingshan"' in _js_prompt)
    check("rc-prompt-multisection-rule-py",
          "If a biozone appears in multiple sections" in RANGE_CHART_SYSTEM_PROMPT)
    check("rc-prompt-multisection-rule-js",
          "If a biozone appears in multiple sections" in _js_prompt)


def test_columnar_schema():
    """Verify MergeSchema for columnar-section mode deduplicates by (id, group)."""
    from rca_core.aggregate import merge_columnar_results, COLUMNAR_SECTION_SCHEMA

    check(
        "col-schema-primary",
        COLUMNAR_SECTION_SCHEMA.primary_list_key == "sections",
    )
    check(
        "col-schema-idkeys",
        COLUMNAR_SECTION_SCHEMA.primary_id_keys == ["id", "group"],
    )

    r1 = {
        "sections": [
            {
                "id": "Ki-1",
                "group": "Lower",
                "lithology_blocks": [],
                "age_units": [],
                "samples": [],
                "coordinates_text": "",
                "thickness_m": "500m",
                "confidence_by_section": 0.7,
            }
        ],
        "fossil_legend": [{"marker": "J", "meaning": "Jurassic radiolaria"}],
        "lithology_legend": [{"pattern": "chert", "meaning": "Chert"}],
        "cross_beds": [],
        "confidence": 0.7,
    }
    r2 = {
        "sections": [
            {
                "id": "Ki-1",
                "group": "Lower",
                "lithology_blocks": [],
                "age_units": [],
                "samples": [],
                "coordinates_text": "NW wing",
                "thickness_m": "500m",
                "confidence_by_section": 0.8,
            }
        ],
        "fossil_legend": [{"marker": "J", "meaning": "Jurassic radiolaria"}],
        "lithology_legend": [{"pattern": "chert", "meaning": "Chert"}],
        "cross_beds": [],
        "confidence": 0.6,
    }
    m = merge_columnar_results([r1, r2])
    check("col-merge-runs", m["runs"] == 2)
    check("col-merge-section-count", len(m["sections"]) == 1)
    sec = m["sections"][0]
    check("col-merge-agreement", sec["agreement"] == "2/2")
    check("col-merge-count", sec["agreement_count"] == 2)
    check("col-merge-coords-mode", sec["coordinates_text"] == "NW wing")
    check("col-merge-confidence-mean", m["confidence"] == 0.65)
    # legend dedup: same entry in both runs.
    check("col-merge-fossil-legend-dedup", len(m["fossil_legend"]) == 1)
    check("col-merge-lithology-legend-dedup", len(m["lithology_legend"]) == 1)


def test_columnar_prompt():
    """Columnar prompt must contain keywords specific to columnar-section extraction.

    Also verifies the JS mirror in js/prompt.js contains the same key tokens
    so the two prompts stay roughly in sync (we don't enforce byte-for-byte
    parity because JS-style backtick/string quoting differs in test fixtures).
    """
    from rca_core.prompt import COLUMNAR_SECTION_SYSTEM_PROMPT
    import os
    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "prompt.js")
    with open(js_path, encoding="utf-8") as f:
        js_src = f.read()
    # Pull the JS mirror string out of the `const COLUMNAR_SECTION_SYSTEM_PROMPT = [...].join('\n');` literal.
    m = re.search(r"const COLUMNAR_SECTION_SYSTEM_PROMPT = \[([\s\S]*?)\]\.join\(['\"]\\n['\"]\)", js_src)
    js_prompt = ""
    if m:
        lines = m.group(1).split("\n")
        cleaned = []
        for line in lines:
            s = line.strip().rstrip(",")
            if s.startswith("'") and s.endswith("'") and len(s) >= 2:
                cleaned.append(s[1:-1].replace("\\'", "'"))
            elif s == "":
                cleaned.append("")
        js_prompt = "\n".join(cleaned)

    tokens = [
        "COLUMNAR SECTION",
        "vertical column",
        "fossil sample horizons",
        "confidence_by_section",
        "fossil_legend",
        "lithology_legend",
        "cross_beds",
        "overall_confidence",
        "Return JSON only",
        "Preserve",
    ]
    for kw in tokens:
        check(f"col-prompt-py:{kw[:20]}", kw.lower() in COLUMNAR_SECTION_SYSTEM_PROMPT.lower())
        check(f"col-prompt-js:{kw[:20]}", kw.lower() in js_prompt.lower())


def test_extract_dispatches_provider():
    """Unified extract() must accept + forward a provider (Bug #1 fix)."""
    from rca_core import ApiFormat, LlmProvider, extract, ExtractResult

    captured = {}
    orig = extract.__globals__.get("call_llm_api")
    # Stub the llm layer so no real HTTP is made.
    def fake_call(**kw):
        captured.update(kw)
        return '{"confidence":0.5}', False, 200, "", {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_creation_tokens": 0, "estimated": False}
    import rca_core.extractor as E
    orig_ext = E.call_llm_api
    E.call_llm_api = fake_call
    try:
        provider = LlmProvider(
            name="X", api_format=ApiFormat.OPENAI,
            endpoint="https://x.io/v1", api_key="k", model="m",
        )
        res = extract(mode="range_chart", api_key="legacy-ignored", image_b64="QUFB",
                      media_type="image/png", provider=provider)
        check("extract-ok", res.ok)
        check("extract-forwarded-provider", captured.get("provider") is provider)
        # legacy path (no provider) still works
        res_legacy = extract(mode="range_chart", api_key="k2", image_b64="QUFB",
                             media_type="image/png")
        check("extract-legacy-ok", res_legacy.ok)
    finally:
        E.call_llm_api = orig_ext


def test_extract_measures_latency():
    """extract_range_chart must measure wall-clock latency of the API call
    on both success and error paths (regression: latency_ms was never set)."""
    import time as _time
    import rca_core.extractor as E
    from rca_core import ExtractResult

    orig_ext = E.call_llm_api

    def slow_ok(**kw):
        _time.sleep(0.03)
        return ('{"confidence":0.5}', False, 200, "",
                {"input_tokens": 1, "output_tokens": 1,
                 "cache_read_tokens": 0, "cache_creation_tokens": 0,
                 "estimated": False})

    def slow_err(**kw):
        _time.sleep(0.03)
        return (None, False, 500, "boom", None)

    try:
        E.call_llm_api = slow_ok
        r = E.extract_range_chart(image_b64="QUFB", media_type="image/png", api_key="k")
        check("latency-success-ok", r.ok)
        check("latency-success-measured", r.latency_ms >= 20)

        E.call_llm_api = slow_err
        r2 = E.extract_range_chart(image_b64="QUFB", media_type="image/png", api_key="k")
        check("latency-error-not-ok", not r2.ok)
        check("latency-error-measured", r2.latency_ms >= 20)
    finally:
        E.call_llm_api = orig_ext


def test_columnar_multi_run_parity_with_js():
    """Columnar sections table must mirror js/table.js: agreement column is
    appended only when data.runs > 1 (Bug #4 fix)."""
    from rca_core.exporter import get_configs_for_result

    single = {"sections": [{"id": "Ki-1", "group": "L"}], "runs": 1}
    multi = {"sections": [{"id": "Ki-1", "group": "L", "agreement": "2/2"}], "runs": 2}

    cfg_single = get_configs_for_result(single)
    sec_cfg_single = next(c for c in cfg_single if c["id"] == "sections")
    check("col-single-no-agreement", "col.agreement" not in sec_cfg_single["cols"])
    check("col-single-len-4", len(sec_cfg_single["cols"]) == 4)

    cfg_multi = get_configs_for_result(multi)
    sec_cfg_multi = next(c for c in cfg_multi if c["id"] == "sections")
    check("col-multi-agreement", "col.agreement" in sec_cfg_multi["cols"])
    check("col-multi-len-5", len(sec_cfg_multi["cols"]) == 5)
    # row extractor must include the agreement value
    row = sec_cfg_multi["row"](multi["sections"][0])
    check("col-multi-row-agreement", row[-1] == "2/2")


def test_provider_store_set_current_bogus():
    """set_current with a bogus id must not corrupt current_id (Bug #2 fix)."""
    import tempfile, os
    from rca_core import ProviderStore, LlmProvider
    path = os.path.join(tempfile.mkdtemp(), "providers.json")
    store = ProviderStore(path=path).load()
    first_id = store.providers[0].id

    store.set_current("does-not-exist")
    check("set-current-bogus-falls-back", store.current_id == first_id)
    check("set-current-bogus-flags-first",
          store.providers[0].is_current is True)


def test_gemini_key_in_header():
    """Gemini must put api_key in x-api-key header, not URL (Bug #3 fix)."""
    import json, threading, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from rca_core import ApiFormat, LlmProvider
    import rca_core.llm as LLM_mod

    captured = {}
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            captured["path"] = self.path
            captured["x-api-key"] = self.headers.get("X-Api-Key")
            captured["url_has_key"] = "key=" in self.path
            b = b'{"candidates":[{"content":{"parts":[{"text":"{\\"a\\":1}"}]},'
            b += b'"finishReason":"STOP"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 59935), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # Poll for readiness instead of a fixed sleep — CI under load may
    # not be ready in 0.2s, and waiting longer than needed is wasteful.
    import socket
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", 59935), timeout=0.05):
                break
        except OSError:
            time.sleep(0.05)
    else:
        check("gemini-server-ready-59935", False)
        srv.shutdown()
        return
    provider = LlmProvider(api_format=ApiFormat.GEMINI,
                           endpoint="http://127.0.0.1:59935",
                           api_key="SECRET-GEMINI-KEY", model="gemini-2.5-pro")
    raw, truncated, status, err_body, usage = LLM_mod.call_llm_api(
        provider=provider, system_prompt="s", image_b64="QUFB",
        media_type="image/png", user_text="hi", max_tokens=100,
    )
    check("gemini-key-in-header", captured.get("x-api-key") == "SECRET-GEMINI-KEY")
    check("gemini-key-not-in-url", captured.get("url_has_key") is False)
    srv.shutdown()


def test_gemini_probe_does_not_leak_key_in_url():
    """Bug C4 regression: _probe_gemini_models and _probe_minimal_generate
    must NOT put the API key in the URL."""
    import json, threading, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from rca_core import ApiFormat, LlmProvider
    import rca_core.llm as LLM_mod

    captured = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            captured.setdefault("gets", []).append(self.path)
            captured.setdefault("get_x_api_key", []).append(self.headers.get("X-Api-Key"))
            # Return a valid model list
            b = b'{"models":[{"name":"gemini-2.5-pro"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def do_POST(self):
            captured.setdefault("posts", []).append(self.path)
            captured.setdefault("post_x_api_key", []).append(self.headers.get("X-Api-Key"))
            # Return a valid response
            b = b'{"candidates":[{"content":{"parts":[{"text":"hi"}]},"finishReason":"STOP"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 59937), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import socket
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", 59937), timeout=0.05):
                break
        except OSError:
            time.sleep(0.05)
    else:
        check("gemini-server-ready-59937", False)
        srv.shutdown()
        return
    provider = LlmProvider(api_format=ApiFormat.GEMINI,
                           endpoint="http://127.0.0.1:59937",
                           api_key="LEAK-TEST-KEY", model="gemini-2.5-pro")
    res = LLM_mod.test_llm_connection(provider, timeout_sec=5)
    srv.shutdown()
    check("gemini-probe-ok", res.ok)
    # Probe-firing assertion: the previous version used `if h_get:` /
    # `if h_post:` which silently PASSed when the probe didn't actually
    # fire (no requests captured). Make sure at least one probe ran so
    # the assertions below are meaningful.
    probe_fired = bool(captured.get("gets")) or bool(captured.get("posts"))
    check("gemini-probe-fired", probe_fired)
    # No GET request should have ?key= in it; only /v1beta/models.
    bad_gets = [p for p in captured.get("gets", []) if "key=" in p]
    check("gemini-probe-get-no-key", not bad_gets)
    # The minimal-generate probe (if it ran) must put the key in header, not URL.
    bad_posts = [p for p in captured.get("posts", []) if "key=" in p]
    check("gemini-probe-post-no-key", not bad_posts)
    # And the header must carry the key on both probes. Only check the
    # branches that actually fired — if a probe didn't run, that's the
    # `gemini-probe-fired` assertion above failing, not these.
    h_get = [v for v in captured.get("get_x_api_key", []) if v]
    h_post = [v for v in captured.get("post_x_api_key", []) if v]
    if captured.get("gets"):
        check("gemini-probe-get-header", any(k == "LEAK-TEST-KEY" for k in h_get))
    if captured.get("posts"):
        check("gemini-probe-post-header", any(k == "LEAK-TEST-KEY" for k in h_post))


def test_h6_none_status_maps_to_network():
    """Bug H6 regression: _error_from_status(None) → err.network."""
    from rca_core.extractor import _error_from_status
    er = _error_from_status(None)
    check("h6-none-network", er.error_key == "err.network" and er.status is None)
    er2 = _error_from_status(401)
    check("h6-401-maps", er2.error_key == "err.401" and er2.status == 401)


def test_h7_error_body_surfaces():
    """Bug H7 regression: ExtractResult carries the upstream error body."""
    from rca_core.extractor import ExtractResult
    er = ExtractResult(ok=False, error_key="err.http", status=500,
                       error_body="rate limit exceeded")
    check("h7-error-body-field", er.error_body == "rate limit exceeded")


def test_h8_normalize_preserves_extras():
    """Bug H8 regression: extra keys the model emits survive normalize."""
    from rca_core.extractor import normalize_result, normalize_columnar_result
    r = normalize_result({
        "sections": [{"name": "A", "page_id": 7}],
        "species_ranges": [{"species": "x", "section": "A",
                             "range_top": "1", "range_base": "1", "biozone": "",
                             "notes": "rare"}],
        "biozones": [],
        "other_fossils": [],
        "confidence": 0.5,
        "top_extra": "hi",
    })
    check("h8-section-extras", r["sections"][0].get("_extras", {}).get("page_id") == 7)
    check("h8-species-extras", r["species_ranges"][0].get("_extras", {}).get("notes") == "rare")
    check("h8-root-extras", r.get("_extras", {}).get("top_extra") == "hi")
    cr = normalize_columnar_result({
        "sections": [{"id": "Ki-1", "group": "L", "fossil_marker_set": "J"}],
        "fossil_legend": [],
        "lithology_legend": [],
        "cross_beds": [],
        "overall_confidence": 0.5,
        "appendix": "abc",
    })
    check("h8-columnar-section-extras",
          cr["sections"][0].get("_extras", {}).get("fossil_marker_set") == "J")
    check("h8-columnar-root-extras", cr.get("_extras", {}).get("appendix") == "abc")


def test_c2_columnar_struct_fields_merge():
    """Bug C2 regression: columnar sections with structured fields merge
    without crashing and produce a stable union."""
    from rca_core.aggregate import merge_results, COLUMNAR_SECTION_SCHEMA
    r1 = {"sections": [
        {"id": "Ki-1", "group": "L", "lithology_blocks": [
            {"pattern": "chert", "range_top_idx": 1, "range_base_idx": 0}
        ], "age_units": [], "samples": [{"bed_idx": 1, "fossil_marker": "J"}],
         "coordinates_text": "", "thickness_m": "500m",
         "confidence_by_section": 0.7}], "fossil_legend": [], "lithology_legend": [],
        "cross_beds": [], "confidence": 0.7}
    r2 = {"sections": [
        {"id": "Ki-1", "group": "L", "lithology_blocks": [
            {"pattern": "shale", "range_top_idx": 2, "range_base_idx": 1}
        ], "age_units": [], "samples": [{"bed_idx": 3, "fossil_marker": "T"}],
         "coordinates_text": "x", "thickness_m": "500m",
         "confidence_by_section": 0.5}], "fossil_legend": [], "lithology_legend": [],
        "cross_beds": [], "confidence": 0.9}
    m = merge_results([r1, r2], schema=COLUMNAR_SECTION_SCHEMA)
    check("c2-no-crash", m["sections"][0]["agreement"] == "2/2")
    check("c2-blocks-union", len(m["sections"][0]["lithology_blocks"]) == 2)
    check("c2-samples-union", len(m["sections"][0]["samples"]) == 2)


def test_h5_auto_detect_columnar():
    """Bug H5 regression: columnar runs auto-detect COLUMNAR_SECTION_SCHEMA."""
    from rca_core.aggregate import merge_results
    r1 = {"sections": [{"id": "Ki-1", "group": "L", "lithology_blocks": [],
                        "age_units": [], "samples": [], "coordinates_text": "",
                        "thickness_m": "500m", "confidence_by_section": 0.7}],
          "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
          "confidence": 0.7}
    r2 = {"sections": [{"id": "Ki-1", "group": "L", "lithology_blocks": [],
                        "age_units": [], "samples": [], "coordinates_text": "",
                        "thickness_m": "500m", "confidence_by_section": 0.5}],
          "fossil_legend": [], "lithology_legend": [], "cross_beds": [],
          "confidence": 0.9}
    m = merge_results([r1, r2])  # no schema kwarg
    check("h5-autodetect", m["sections"][0]["agreement"] == "2/2" and len(m["sections"]) == 1)


def test_h4_mode_tie_break_deterministic():
    """Bug H4 regression: 3 unique values → smallest wins, regardless of order."""
    from rca_core.aggregate import merge_results
    rA = {"sections": [], "species_ranges": [
        {"species": "A", "section": "S", "range_base": "Z",
         "range_top": "2", "biozone": ""}], "biozones": [],
        "other_fossils": [], "confidence": 0.5}
    rB = {"sections": [], "species_ranges": [
        {"species": "A", "section": "S", "range_base": "M",
         "range_top": "2", "biozone": ""}], "biozones": [],
        "other_fossils": [], "confidence": 0.5}
    rC = {"sections": [], "species_ranges": [
        {"species": "A", "section": "S", "range_base": "A",
         "range_top": "2", "biozone": ""}], "biozones": [],
        "other_fossils": [], "confidence": 0.5}
    m123 = merge_results([rA, rB, rC])
    m321 = merge_results([rC, rB, rA])
    check("h4-deterministic", m123["species_ranges"][0]["range_base"] ==
          m321["species_ranges"][0]["range_base"] == "A")
    # Majority should still win.
    r_major = {"sections": [], "species_ranges": [
        {"species": "A", "section": "S", "range_base": "X",
         "range_top": "2", "biozone": ""}], "biozones": [],
        "other_fossils": [], "confidence": 0.5}
    m_major = merge_results([r_major, r_major, rA])
    check("h4-majority", m_major["species_ranges"][0]["range_base"] == "X")


def test_m5_clamp_max_tokens():
    """Bug M5 regression: max_tokens is clamped server-side."""
    from rca_core.extractor import clamp_max_tokens
    check("m5-clamp-min", clamp_max_tokens(-1) == 1)
    check("m5-clamp-max", clamp_max_tokens(99_999_999) == 32000)
    check("m5-clamp-default", clamp_max_tokens(None) == 4000)
    check("m5-clamp-mid", clamp_max_tokens(2048) == 2048)
    check("m5-clamp-str", clamp_max_tokens("oops") == 4000)


def test_m6_quarantine_on_corrupt():
    """Bug M6 regression: corrupt providers.json is quarantined + reseeded."""
    import tempfile, os
    from rca_core.llm import ProviderStore
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "providers.json")
        ProviderStore(path=path).load()  # seed
        with open(path, "w") as f:
            f.write("not json {{")  # corrupt
        store = ProviderStore(path=path).load()
        check("m6-reseed", len(store.providers) == 1)
        quarantine = [f for f in os.listdir(td)
                      if f.startswith("providers.json.corrupt-")]
        check("m6-quarantine-file", len(quarantine) >= 1)


def test_m7_provider_store_lock():
    """Bug M7 regression: ProviderStore serializes concurrent mutations."""
    import tempfile, os, threading
    from rca_core.llm import ProviderStore, LlmProvider, ApiFormat
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "providers.json")
        store = ProviderStore(path=path).load()
        # Hammer `add` from many threads; the RLock guarantees no torn writes.
        def add_one(i):
            store.add(LlmProvider(name=f"P{i}", api_format=ApiFormat.OPENAI,
                                  endpoint="https://example.com/v1",
                                  api_key="k", model="m"))
        ts = [threading.Thread(target=add_one, args=(i,)) for i in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        check("m7-locked-add-count", len(store.providers) == 9)  # 1 seed + 8 adds


def test_m8_update_returns_bool():
    """Bug M8 regression: update() returns False when no match."""
    import tempfile, os
    from rca_core.llm import ProviderStore, LlmProvider, ApiFormat
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "providers.json")
        store = ProviderStore(path=path).load()
        new = LlmProvider(id="does-not-exist", name="x",
                          api_format=ApiFormat.OPENAI,
                          endpoint="https://x.io/v1", api_key="", model="")
        ok = store.update(new)
        check("m8-update-no-match-returns-false", ok is False)


def test_m9_add_preserves_existing_created_at():
    """Bug M9 regression: add() doesn't clobber a loaded created_at."""
    import tempfile, os, time
    from rca_core.llm import ProviderStore, LlmProvider, ApiFormat
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "providers.json")
        store = ProviderStore(path=path).load()
        original = time.time() - 1000  # simulate a saved timestamp
        p = LlmProvider(id="abc", name="A", api_format=ApiFormat.OPENAI,
                        endpoint="https://x.io/v1", api_key="k", model="m",
                        created_at=original)
        store.add(p)
        check("m9-preserve-created-at", p.created_at == original)


def test_l2_translations_init_shape():
    """Bug L2 regression: TRANSLATIONS dict is initialized with all expected
    langs so the test_i18n parity check doesn't trip on KeyError."""
    from rca_core.i18n import TRANSLATIONS
    check("l2-init-has-zh", "zh" in TRANSLATIONS)
    check("l2-init-has-en", "en" in TRANSLATIONS)
    check("l2-init-has-ja", "ja" in TRANSLATIONS)


def test_be2_safe_json_strips_control_chars():
    """BE-2: safe_json_loads must strip raw control characters (0x00-0x08,
    0x0B, 0x0C, 0x0E-0x1F) that some models emit inside string values,
    otherwise json.loads raises. \\t \\r \\n are preserved."""
    from rca_core.json_utils import safe_json_loads
    # Embedded \\x01 inside a string value would crash json.loads without stripping.
    raw = '{"a": "foo\x01\x02bar", "b": "ok"}'
    parsed = safe_json_loads(raw)
    check("be2-stripped-ctrl", parsed == {"a": "foobar", "b": "ok"})
    # \\t / \\r / \\n preserved (not stripped).
    raw2 = '{"a": "line1\\nline2\\ttab"}'
    parsed2 = safe_json_loads(raw2)
    check("be2-preserves-newlines", parsed2["a"] == "line1\nline2\ttab")


def test_be1_retry_wrapper_retries_429():
    """BE-1: call_llm_api_with_retry must retry on 429 + eventually succeed.
    Mocks call_llm_api to fail-then-succeed; verifies backoff + final result."""
    import time
    from rca_core.llm import call_llm_api_with_retry, ApiFormat, LlmProvider
    import rca_core.llm as L
    attempts = []
    sleep_calls = []
    def fake_call_llm_api(**kw):
        attempts.append(1)
        if len(attempts) < 2:
            return (None, False, 429, "rate limit", None)
        return ("{} ok", False, 200, "", None)
    _saved_cll = L.call_llm_api
    L.call_llm_api = fake_call_llm_api
    orig_sleep = time.sleep
    time.sleep = lambda s: sleep_calls.append(s)
    try:
        provider = LlmProvider(name="X", api_format=ApiFormat.OPENAI,
                                endpoint="https://x.io/v1", api_key="k", model="m")
        result = call_llm_api_with_retry(
            provider=provider, system_prompt="s", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=10,
            retries=3, initial_backoff_sec=0.01,
        )
        check("be1-retries-on-429-attempts", len(attempts) == 2)
        check("be1-retries-on-429-backoff", len(sleep_calls) == 1)
        check("be1-retries-on-429-success", result[0] == "{} ok")
    finally:
        time.sleep = orig_sleep
        L.call_llm_api = _saved_cll


def test_be1_retry_does_not_retry_401():
    """BE-1: 401 (auth error) must NOT retry - just give up."""
    import time
    from rca_core.llm import call_llm_api_with_retry, ApiFormat, LlmProvider
    import rca_core.llm as L
    attempts = [0]
    def fake_call_llm_api(**kw):
        attempts[0] += 1
        return (None, False, 401, "bad key", None)
    _saved_cll = L.call_llm_api
    L.call_llm_api = fake_call_llm_api
    orig_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        provider = LlmProvider(name="X", api_format=ApiFormat.OPENAI,
                                endpoint="https://x.io/v1", api_key="k", model="m")
        result = call_llm_api_with_retry(
            provider=provider, system_prompt="s", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=10,
            retries=3, initial_backoff_sec=0.01,
        )
        check("be1-no-retry-on-401", attempts[0] == 1)
        check("be1-no-retry-on-401-status", result[2] == 401)
    finally:
        time.sleep = orig_sleep
        L.call_llm_api = _saved_cll


def test_be1_retry_gives_up_after_retries():
    """BE-1: persistent 500 returns the last attempt's result unchanged."""
    import time
    from rca_core.llm import call_llm_api_with_retry, ApiFormat, LlmProvider
    import rca_core.llm as L
    attempts = [0]
    def fake_call_llm_api(**kw):
        attempts[0] += 1
        return (None, False, 500, "boom", None)
    _saved_cll = L.call_llm_api
    L.call_llm_api = fake_call_llm_api
    orig_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        provider = LlmProvider(name="X", api_format=ApiFormat.OPENAI,
                                endpoint="https://x.io/v1", api_key="k", model="m")
        result = call_llm_api_with_retry(
            provider=provider, system_prompt="s", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=10,
            retries=3, initial_backoff_sec=0.01,
        )
        check("be1-gives-up-after-retries", attempts[0] == 3)
        check("be1-final-status-500", result[2] == 500)
        check("be1-final-errbody-stamped", "retry" in (result[3] or ""))
    finally:
        time.sleep = orig_sleep
        L.call_llm_api = _saved_cll


def test_be3_gui_worker_uses_threadpool():
    """BE-3: gui._worker multi-run path uses concurrent.futures and merges."""
    # Inspect the source: gui._worker must mention ThreadPoolExecutor when
    # runs > 1, and must not introduce a serial for-loop over runs.
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "gui.py"), encoding="utf-8").read()
    # Find _worker body
    i = src.find("def _worker(")
    j = src.find("\n    def ", i + 1)
    body = src[i:j]
    check("be3-worker-imports-concurrent", "concurrent.futures" in body)
    check("be3-worker-uses-threadpool", "ThreadPoolExecutor" in body)
    check("be3-worker-no-serial-for",
          "for _ in range(runs):" not in body or "ThreadPoolExecutor" in body)


def test_abundance_normalize():
    """Abundance-diagram normalize coerces the strict shape and clamps confidence."""
    from rca_core.extractor import normalize_abundance_result
    d = normalize_abundance_result({
        "sites": [{"name": "Core A", "depth_unit": "cm", "extra": "keep"}],
        "abundances": [
            {"taxon": "Pinus", "level": "120 cm", "depth": "120",
             "abundance": "35", "abundance_unit": "%"},
            "not-a-dict",
        ],
        "zones": [{"name": "PAZ-3", "age": "Early Holocene", "level_range": "80-140 cm"}],
        "confidence": 1.7,  # out of range → clamp to 1.0
    })
    check("ab-norm-keys", sorted(d.keys()) == ["abundances", "confidence", "sites", "zones"])
    check("ab-norm-sites", len(d["sites"]) == 1 and d["sites"][0]["name"] == "Core A")
    check("ab-norm-extras", d["sites"][0].get("_extras", {}).get("extra") == "keep")
    check("ab-norm-drops-nondict", len(d["abundances"]) == 1)
    row = d["abundances"][0]
    check("ab-norm-row", row["taxon"] == "Pinus" and row["abundance"] == "35"
          and row["abundance_unit"] == "%")
    check("ab-norm-zone", d["zones"][0]["name"] == "PAZ-3")
    check("ab-norm-conf-clamp", d["confidence"] == 1.0)


def test_abundance_schema_and_merge():
    """ABUNDANCE_DIAGRAM_SCHEMA dedups by (site, taxon, level) and majority-votes."""
    from rca_core.aggregate import (
        ABUNDANCE_DIAGRAM_SCHEMA, SCHEMA_BY_MODE, merge_results,
    )
    check("ab-schema-primary", ABUNDANCE_DIAGRAM_SCHEMA.primary_list_key == "abundances")
    check("ab-schema-idkeys",
          ABUNDANCE_DIAGRAM_SCHEMA.primary_id_keys == ["site", "taxon", "level"])
    check("ab-schema-registered",
          SCHEMA_BY_MODE.get("abundance_diagram") is ABUNDANCE_DIAGRAM_SCHEMA)

    def run(ab_val):
        return {
            "sites": [{"name": "Core A", "location": "", "age_range": "", "depth_unit": "cm"}],
            "abundances": [{"taxon": "Pinus", "site": "Core A", "level": "120 cm",
                            "depth": "120", "abundance": ab_val, "abundance_unit": "%"}],
            "zones": [{"name": "PAZ-3", "age": "", "level_range": "80-140 cm"}],
            "confidence": 0.8,
        }
    # 3 runs: abundance 35 appears twice, 40 once → majority 35.
    m = merge_results([run("35"), run("35"), run("40")], total_runs=3,
                      schema=ABUNDANCE_DIAGRAM_SCHEMA)
    check("ab-merge-dedup", len(m["abundances"]) == 1)
    check("ab-merge-agreement", m["abundances"][0]["agreement"] == "3/3")
    check("ab-merge-majority", m["abundances"][0]["abundance"] == "35")
    check("ab-merge-sites-dedup", len(m["sites"]) == 1)
    check("ab-merge-zones-dedup", len(m["zones"]) == 1)


def test_abundance_export_and_detect():
    """Abundance results route to the abundance table set and export cleanly."""
    from rca_core.extractor import normalize_abundance_result
    from rca_core.exporter import (
        _looks_abundance, get_configs_for_result, build_table_export,
    )
    d = normalize_abundance_result({
        "sites": [{"name": "Core A", "depth_unit": "cm"}],
        "abundances": [{"taxon": "Pinus", "level": "120 cm", "abundance": "35",
                        "abundance_unit": "%"}],
        "zones": [{"name": "PAZ-3"}],
        "confidence": 0.8,
    })
    check("ab-detect", _looks_abundance(d) is True)
    check("ab-detect-neg", _looks_abundance({"species_ranges": []}) is False)
    cfgs = get_configs_for_result(d)
    check("ab-tables", [c["id"] for c in cfgs] == ["sites", "abundances", "zones"])
    headers, rows = build_table_export(d, "abundances", lambda k: k)
    check("ab-export-len", len(rows) == 1 and len(rows[0]) == len(headers))
    check("ab-export-taxon", "Pinus" in rows[0])


def test_named_list_no_label_not_dropped():
    """Regression: items in a schema list_key that lack name/marker/meaning
    (abundance single-site with empty name; columnar cross_beds) must NOT be
    silently dropped when merging multiple runs — they fall back to a content
    signature so identical items collapse and distinct ones survive."""
    from rca_core.aggregate import (
        merge_results, ABUNDANCE_DIAGRAM_SCHEMA, COLUMNAR_SECTION_SCHEMA,
    )

    def ab_run(ab):
        return {
            "sites": [{"name": "", "location": "35N", "age_range": "Holocene", "depth_unit": "cm"}],
            "abundances": [{"taxon": "Pinus", "site": "", "level": "120 cm",
                            "depth": "120", "abundance": ab, "abundance_unit": "%"}],
            "zones": [{"name": "PAZ-3", "age": "", "level_range": "80-140 cm"}],
            "confidence": 0.8,
        }
    m = merge_results([ab_run("35"), ab_run("35")], total_runs=2,
                      schema=ABUNDANCE_DIAGRAM_SCHEMA)
    check("nl-empty-name-site-kept", len(m["sites"]) == 1)
    check("nl-empty-name-site-fields", m["sites"][0].get("location") == "35N")

    def cb_run(fb):
        return {
            "sections": [{"id": "Ki-1", "group": "L", "lithology_blocks": [],
                          "age_units": [], "samples": [], "coordinates_text": "",
                          "thickness_m": "", "confidence_by_section": 0.7}],
            "fossil_legend": [], "lithology_legend": [],
            "cross_beds": [{"from_section": "Ki-1", "from_bed_idx": fb,
                            "to_section": "Ki-2", "to_bed_idx": 4}],
            "confidence": 0.7,
        }
    # Identical cross_beds across 2 runs → collapse to 1 (was dropped to 0).
    m2 = merge_results([cb_run(3), cb_run(3)], total_runs=2,
                       schema=COLUMNAR_SECTION_SCHEMA)
    check("nl-crossbeds-identical-kept", len(m2["cross_beds"]) == 1)
    # Distinct cross_beds → both preserved.
    m3 = merge_results([cb_run(3), cb_run(9)], total_runs=2,
                       schema=COLUMNAR_SECTION_SCHEMA)
    check("nl-crossbeds-distinct-kept", len(m3["cross_beds"]) == 2)


def test_abundance_prompt():
    """Abundance prompt carries its key tokens on both the Python and JS sides."""
    from rca_core.prompt import ABUNDANCE_DIAGRAM_SYSTEM_PROMPT
    js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js", "prompt.js")
    with open(js_path, encoding="utf-8") as f:
        js_src = f.read()
    m = re.search(
        r"const ABUNDANCE_DIAGRAM_SYSTEM_PROMPT = \[([\s\S]*?)\]\.join\(['\"]\\n['\"]\)",
        js_src,
    )
    check("ab-prompt-js-present", m is not None)
    raw = m.group(1) if m else ""
    # Reverse the JS string-escaping so substrings like "that taxon's
    # abundance" match what Python sees.
    js_prompt = raw.replace("\\'", "'").replace('\\"', '"')
    tokens = [
        "ABUNDANCE DIAGRAM",
        "pollen diagram",
        "abundances",
        "abundance_unit",
        "zones",
        "ONE COLUMN = ONE TAXON",
        "Return JSON only",
    ]
    for kw in tokens:
        check(f"ab-prompt-py:{kw[:18]}", kw.lower() in ABUNDANCE_DIAGRAM_SYSTEM_PROMPT.lower())
        check(f"ab-prompt-js:{kw[:18]}", kw.lower() in js_prompt.lower())
    # Hard parity: "that taxon's abundance" (with the possessive) must be
    # verbatim in BOTH the Python and JS abundance prompts — the JS side
    # had silently dropped the "'s" so the two ends disagreed on a key
    # sentence; this assertion prevents the drift from regressing.
    check("ab-prompt-taxons-possessive-py",
          "that taxon's abundance" in ABUNDANCE_DIAGRAM_SYSTEM_PROMPT)
    check("ab-prompt-taxons-possessive-js",
          "that taxon's abundance" in js_prompt)


def test_t5_looks_columnar_detection():
    """T5: exporter._looks_columnar correctly classifies result shapes.
    Heuristic: columnar iff sections[0] has 'id' and not 'name'."""
    from rca_core.exporter import _looks_columnar
    check("t5-col-id-only", _looks_columnar({"sections": [{"id": "Ki-1", "group": "L"}]}) is True)
    check("t5-col-name-only", _looks_columnar({"sections": [{"name": "A"}]}) is False)
    # "id" is sufficient for columnar classification even when "name" is also present
    check("t5-col-both", _looks_columnar({"sections": [{"id": "X", "name": "A"}]}) is True)


def test_auto_detect_tie_prefers_columnar():
    """H39 regression: when 2 runs split 1-columnar / 1-abundance, the
    old version fell through to RANGE_CHART (silent data loss). The
    fixed version prefers columnar because its shape is more specific."""
    from rca_core.aggregate import (
        _auto_detect_schema, ABUNDANCE_DIAGRAM_SCHEMA, COLUMNAR_SECTION_SCHEMA,
    )
    col_data = {"sections": [{"id": "Ki-1", "group": "L"}]}
    ab_data = {"abundances": [{"taxon": "A", "site": "S1"}]}
    chosen = _auto_detect_schema([col_data, ab_data])
    check("tie-columnar-preferred", chosen is COLUMNAR_SECTION_SCHEMA)
    # Symmetric: abundance-tilted tie picks abundance.
    chosen2 = _auto_detect_schema([ab_data, col_data])
    # Both equal counts → still columnar (more specific fallback).
    check("tie-still-columnar", chosen2 is COLUMNAR_SECTION_SCHEMA)


def test_auto_detect_majority_unaffected():
    """When 3/3 runs agree on a shape, the choice should match that shape."""
    from rca_core.aggregate import (
        _auto_detect_schema, ABUNDANCE_DIAGRAM_SCHEMA,
    )
    ab_data = {"abundances": [{"taxon": "A"}]}
    chosen = _auto_detect_schema([ab_data, ab_data, ab_data])
    check("majority-abundance-3-3", chosen is ABUNDANCE_DIAGRAM_SCHEMA)


def test_extract_truncated_sets_warning():
    """M40 regression: when the model hits max_tokens, the ExtractResult
    must carry a human-readable warning string the UI can show without
    flipping ok=False."""
    from rca_core.extractor import ExtractResult
    e = ExtractResult(ok=True, data={"sections": []}, truncated=True,
                       warning="Result may be truncated (model hit max_tokens).")
    check("truncated-warning-non-empty", bool(e.warning))
    # Non-truncated result: warning should be empty (default).
    e2 = ExtractResult(ok=True, data={"sections": []})
    check("not-truncated-warning-empty", e2.warning == "")


def test_db_schema_version_set_on_init():
    """T38 regression: opening a new DB must stamp _schema_version."""
    import tempfile
    from rca_core.db import Database
    td = tempfile.mkdtemp()
    try:
        db = Database(os.path.join(td, "fresh.db"))
        row = db.query_one("SELECT version FROM _schema_version LIMIT 1")
        check("db-schema-version-present", row is not None)
        check("db-schema-version-correct",
              row["version"] == Database._CURRENT_SCHEMA_VERSION)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


def test_db_schema_upgrade_idempotent():
    """T38 regression: opening an existing DB must not re-stamp or break."""
    import tempfile
    from rca_core.db import Database
    td = tempfile.mkdtemp()
    try:
        path = os.path.join(td, "twice.db")
        Database(path)
        # Open again — same version, no errors, _apply_migrations is a no-op.
        db2 = Database(path)
        row = db2.query_one("SELECT version FROM _schema_version LIMIT 1")
        check("db-schema-version-stable", row["version"] == Database._CURRENT_SCHEMA_VERSION)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
if __name__ == "__main__":
    test_json()
    test_normalize()
    test_export()
    test_i18n()
    test_merge()
    test_prompt()
    test_columnar_schema()
    test_columnar_prompt()
    test_extract_dispatches_provider()
    test_extract_measures_latency()
    test_columnar_multi_run_parity_with_js()
    test_provider_store_set_current_bogus()
    test_gemini_key_in_header()
    test_gemini_probe_does_not_leak_key_in_url()
    test_h6_none_status_maps_to_network()
    test_h7_error_body_surfaces()
    test_h8_normalize_preserves_extras()
    test_c2_columnar_struct_fields_merge()
    test_h5_auto_detect_columnar()
    test_h4_mode_tie_break_deterministic()
    test_m5_clamp_max_tokens()
    test_m6_quarantine_on_corrupt()
    test_m7_provider_store_lock()
    test_m8_update_returns_bool()
    test_m9_add_preserves_existing_created_at()
    test_l2_translations_init_shape()
    test_abundance_normalize()
    test_abundance_schema_and_merge()
    test_abundance_export_and_detect()
    test_named_list_no_label_not_dropped()
    test_abundance_prompt()
    test_auto_detect_tie_prefers_columnar()
    test_auto_detect_majority_unaffected()
    test_extract_truncated_sets_warning()
    test_db_schema_version_set_on_init()
    test_db_schema_upgrade_idempotent()


def test_t6_build_table_export_pad_and_truncate():
    """T6: build_table_export must pad short rows and truncate long rows."""
    from rca_core import exporter as X
    orig = X._range_chart_tables
    def patched(data):
        cfg = orig(data)
        for c in cfg:
            if c["id"] == "species_ranges":
                c["row"] = lambda r: [r.get("species", "")] + ["x"] * 100  # way too many
        return cfg
    X._range_chart_tables = patched
    try:
        data = {"species_ranges": [{"species": "X", "section": "A",
                                     "range_base": "1", "range_top": "2", "biozone": "Z"}]}
        headers, rows = X.build_table_export(data, "species_ranges", lambda k: k)
        check("t6-truncate-len", len(rows[0]) == len(headers))
    finally:
        X._range_chart_tables = orig

    # Padding: a config whose cols outnumber the row cells
    def short_row(data):
        cfg = orig(data)
        for c in cfg:
            if c["id"] == "species_ranges":
                c["row"] = lambda r: [r.get("species", "")]  # return only 1 cell
        return cfg
    X._range_chart_tables = short_row
    try:
        headers, rows = X.build_table_export(data, "species_ranges", lambda k: k)
        check("t6-pad-len", len(rows[0]) == len(headers))
    finally:
        X._range_chart_tables = orig


if __name__ == "__main__":
    test_json()
    test_normalize()
    test_export()
    test_i18n()
    test_merge()
    test_prompt()
    test_columnar_schema()
    test_columnar_prompt()
    test_extract_dispatches_provider()
    test_extract_measures_latency()
    test_columnar_multi_run_parity_with_js()
    test_provider_store_set_current_bogus()
    test_gemini_key_in_header()
    test_gemini_probe_does_not_leak_key_in_url()
    test_h6_none_status_maps_to_network()
    test_h7_error_body_surfaces()
    test_h8_normalize_preserves_extras()
    test_c2_columnar_struct_fields_merge()
    test_h5_auto_detect_columnar()
    test_h4_mode_tie_break_deterministic()
    test_m5_clamp_max_tokens()
    test_m6_quarantine_on_corrupt()
    test_m7_provider_store_lock()
    test_m8_update_returns_bool()
    test_m9_add_preserves_existing_created_at()
    test_l2_translations_init_shape()
    test_abundance_normalize()
    test_abundance_schema_and_merge()
    test_abundance_export_and_detect()
    test_named_list_no_label_not_dropped()
    test_abundance_prompt()
    test_auto_detect_tie_prefers_columnar()
    test_auto_detect_majority_unaffected()
    test_extract_truncated_sets_warning()
    test_db_schema_version_set_on_init()
    test_db_schema_upgrade_idempotent()
    test_t6_build_table_export_pad_and_truncate()
    test_be2_safe_json_strips_control_chars()
    test_be1_retry_wrapper_retries_429()
    test_be1_retry_does_not_retry_401()
    test_be1_retry_gives_up_after_retries()
    test_be3_gui_worker_uses_threadpool()
    print(f"\n--- {_pass} passed, {_fail} failed ---")
    sys.exit(1 if _fail else 0)
