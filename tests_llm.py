"""Tests for rca_core.llm: ProviderStore, ApiFormat, call_llm_api dispatch."""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rca_core.llm as L  # noqa: E402

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


# --- ApiFormat ---
check("api-format-values", L.ApiFormat.ANTHROPIC.value == "anthropic")
check("api-format-from-str", L.ApiFormat("openai") == L.ApiFormat.OPENAI)


# --- LlmProvider round-trip ---
p = L.LlmProvider(
    id="x1", name="Test", api_format=L.ApiFormat.OPENAI,
    endpoint="https://example.com/v1", api_key="abc", model="gpt-4o",
    extra_headers={"X-Custom": "yes"}, extra_body={"temperature": 0.1},
)
d = p.to_dict()
check("provider-to-dict-fmt", d["api_format"] == "openai")
check("provider-to-dict-key", d["api_key"] == "abc")
p2 = L.LlmProvider.from_dict(d)
check("provider-roundtrip-fmt", p2.api_format == L.ApiFormat.OPENAI)
check("provider-roundtrip-model", p2.model == "gpt-4o")
check("provider-roundtrip-headers", p2.extra_headers == {"X-Custom": "yes"})
check("provider-roundtrip-body", p2.extra_body == {"temperature": 0.1})
check("provider-auto-id", bool(L.LlmProvider().id))
check("provider-display", "Test" in p.display_label)


# --- ProviderStore ---
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "providers.json")
    store = L.ProviderStore(path=path)
    store.load()
    check("store-seeds-default", len(store.providers) == 1)
    check("store-default-current", store.providers[0].is_current is True)
    check("store-default-format", store.providers[0].api_format == L.ApiFormat.ANTHROPIC)

    # add
    new = L.LlmProvider(
        name="OpenAI", api_format=L.ApiFormat.OPENAI,
        endpoint="https://api.openai.com/v1", api_key="sk-foo", model="gpt-4o",
    )
    store.add(new)
    check("store-add", len(store.providers) == 2)

    # set_current
    store.set_current(new.id)
    check("store-set-current", store.get_current().id == new.id)
    check("store-set-current-flags", all(
        (p.is_current == (p.id == new.id)) for p in store.providers
    ))

    # reload from disk
    store2 = L.ProviderStore(path=path).load()
    check("store-persist", len(store2.providers) == 2)
    check("store-persist-current", store2.get_current().id == new.id)
    check("store-persist-format", store2.get_current().api_format == L.ApiFormat.OPENAI)

    # remove current
    store2.remove(new.id)
    check("store-remove", len(store2.providers) == 1)
    check("store-remove-current-fallback", store2.get_current().id == store2.providers[0].id)

    # legacy config
    leg = store2.to_legacy_config()
    check("store-legacy-endpoint", "endpoint" in leg)


# --- ProviderStore atomic write ---
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "providers.json")
    store = L.ProviderStore(path=path)
    store.load()
    store.save()
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    check("store-file-version", raw["version"] == 1)
    check("store-file-current-id", "current_id" in raw)
    check("store-file-providers", isinstance(raw["providers"], list))


# --- PROVIDER_PRESETS completeness ---
check("presets-non-empty", len(L.PROVIDER_PRESETS) >= 10)
names = {p.name for p in L.PROVIDER_PRESETS}
check("presets-has-anthropic", "Claude Official" in names)
check("presets-has-openai", "OpenAI Official" in names)
check("presets-has-gemini", "Google Gemini" in names)
check("presets-has-minimax", "MiniMax M3" in names)
check("presets-has-deepseek", "DeepSeek" in names)
preset_fmt = {(p.name, p.api_format) for p in L.PROVIDER_PRESETS}
check("preset-anthropic-is-anthropic-format", ("Claude Official", L.ApiFormat.ANTHROPIC) in preset_fmt)
check("preset-openai-is-openai-format", ("OpenAI Official", L.ApiFormat.OPENAI) in preset_fmt)
check("preset-gemini-is-gemini-format", ("Google Gemini", L.ApiFormat.GEMINI) in preset_fmt)
check("presets-all-have-endpoint", all(bool(p.endpoint) for p in L.PROVIDER_PRESETS))
check("presets-count-100+", len(L.PROVIDER_PRESETS) >= 100)


# --- call_llm_api dispatches on format (fake HTTP via monkeypatch) ---
class FakeResponse:
    def __init__(self, data: bytes, status=200):
        self._data = data
        self.status = status
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass


def make_capture_post(expected_fmt: str):
    captured = {}
    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        # urllib.request Request.headers is a case-insensitive dict; use
        # exact keys as set by our code (lowercase) for assertions.
        captured["headers"] = {k: v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data)
        if expected_fmt == "anthropic":
            resp = {"content": [{"type": "text", "text": "{\"a\":1}"}], "stop_reason": "end_turn"}
        elif expected_fmt == "openai":
            resp = {"choices": [{"message": {"content": "{\"a\":1}"}, "finish_reason": "stop"}]}
        else:  # gemini
            resp = {"candidates": [{"content": {"parts": [{"text": "{\"a\":1}"}]}, "finishReason": "STOP"}]}
        return FakeResponse(json.dumps(resp).encode())
    return captured, fake_urlopen


for fmt, label in [
    (L.ApiFormat.ANTHROPIC, "dispatch-anthropic"),
    (L.ApiFormat.OPENAI, "dispatch-openai"),
    (L.ApiFormat.GEMINI, "dispatch-gemini"),
]:
    provider = L.LlmProvider(
        name="X", api_format=fmt, endpoint="https://example.com",
        api_key="key", model="m",
    )
    captured, fake_urlopen = make_capture_post(fmt.value)
    import urllib.request as _ur
    orig = _ur.urlopen
    _ur.urlopen = fake_urlopen
    try:
        raw, truncated, status, err_body, usage = L.call_llm_api(
            provider=provider, system_prompt="sys", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=100,
        )
    finally:
        _ur.urlopen = orig
    check(f"{label}-ok", raw == '{"a":1}')
    check(f"{label}-not-truncated", truncated is False)
    if fmt == L.ApiFormat.ANTHROPIC:
        # HTTPMessage.title-cases the segment before each dash; the code sets
        # 'x-api-key', which urllib presents as 'X-api-key'.
        check(f"{label}-x-api-key", captured["headers"].get("X-api-key") == "key")
        check(f"{label}-path", captured["url"].endswith("/v1/messages"))
    elif fmt == L.ApiFormat.OPENAI:
        check(f"{label}-bearer", captured["headers"].get("Authorization") == "Bearer key")
        check(f"{label}-path", captured["url"].endswith("/v1/chat/completions"))
    elif fmt == L.ApiFormat.GEMINI:
        # Gemini must put the key in x-api-key header, not in the URL
        # (leak-safe contract). See Bug #3 fix.
        # HTTPMessage normalizes the first letter of the first segment to
        # uppercase, so 'x-api-key' comes back as 'X-api-key'.
        check(f"{label}-key-in-header", captured["headers"].get("X-api-key") == "key")
        check(f"{label}-key-not-in-url", "key=" not in captured["url"])
        check(f"{label}-path", captured["url"].endswith(":generateContent"))


# --- call_llm_api truncates on max_tokens finish reason ---
class FakeMaxtokResponse:
    def __init__(self, fmt):
        self.status = 200
        if fmt == L.ApiFormat.ANTHROPIC:
            resp = {"content": [{"type": "text", "text": "{\"a\":1}"}], "stop_reason": "max_tokens"}
        elif fmt == L.ApiFormat.OPENAI:
            resp = {"choices": [{"message": {"content": "{\"a\":1}"}, "finish_reason": "length"}]}
        else:
            resp = {"candidates": [{"content": {"parts": [{"text": "{\"a\":1}"}]}, "finishReason": "MAX_TOKENS"}]}
        self._data = json.dumps(resp).encode()
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass


for fmt in (L.ApiFormat.ANTHROPIC, L.ApiFormat.OPENAI, L.ApiFormat.GEMINI):
    provider = L.LlmProvider(name="X", api_format=fmt, endpoint="https://example.com",
                             api_key="key", model="m")
    import urllib.request as _ur
    orig = _ur.urlopen
    _ur.urlopen = lambda req, timeout=None: FakeMaxtokResponse(fmt)
    try:
        raw, truncated, status, err_body, usage = L.call_llm_api(
            provider=provider, system_prompt="s", image_b64="QUFB",
            media_type="image/png", user_text="hi", max_tokens=100,
        )
    finally:
        _ur.urlopen = orig
    check(f"truncated-{fmt.value}", truncated is True)


# --- call_llm_api returns None on HTTPError ---
import urllib.error as _ue


def raise_http_error(req, timeout=None):
    raise _ue.HTTPError(
        url="https://example.com", code=401, msg="Unauthorized",
        hdrs=None, fp=None,
    )


orig = _ur.urlopen
_ur.urlopen = raise_http_error
try:
    raw, truncated, status, err_body, usage = L.call_llm_api(
        provider=L.LlmProvider(name="X", api_format=L.ApiFormat.ANTHROPIC,
                               endpoint="https://example.com", api_key="k", model="m"),
        system_prompt="s", image_b64="QUFB", media_type="image/png", user_text="hi",
        max_tokens=100,
    )
finally:
    _ur.urlopen = orig
check("http-error-code", status == 401)
check("http-error-raw-none", raw is None)


# --- new extractor provider-aware path ---
# Verify the legacy signature still works (no regression) and that passing a
# provider object routes through call_llm_api.
import rca_core.extractor as E

# Build a legacy-style call with a fake call_llm_api injected.
captured = {}
orig_call = E.call_llm_api
def fake_call(**kw):
    captured.update(kw)
    return ('{"confidence":0.5}', False, 200, "",
            {"input_tokens": 10, "output_tokens": 5,
             "cache_read_tokens": 0, "cache_creation_tokens": 0,
             "estimated": False})

E.call_llm_api = fake_call
try:
    res = E.extract_range_chart(
        api_key="legacy-key", image_b64="QUFB", media_type="image/png",
        base_url="https://legacy.example.com", model="legacy-model",
    )
finally:
    E.call_llm_api = orig_call
check("extractor-legacy-ok", res.ok)
check("extractor-legacy-data", res.data is not None)


# With a provider object - should use provider fields.
E.call_llm_api = fake_call
try:
    provider = L.LlmProvider(
        name="OpenAI", api_format=L.ApiFormat.OPENAI,
        endpoint="https://api.openai.com/v1", api_key="sk-bar", model="gpt-4o",
    )
    res2 = E.extract_range_chart(
        api_key="ignored", image_b64="QUFB", media_type="image/png",
        base_url="https://ignored.example.com", model="ignored",
        provider=provider,
    )
finally:
    E.call_llm_api = orig_call
check("extractor-provider-used", captured.get("provider") is provider)

# ---- appended below ----

def t7_cases():
    import time
    import rca_core.llm as L
    from rca_core import ApiFormat, LlmProvider
    from rca_core.llm import call_llm_api_with_retry
    cases = [
        ([429, 200], 2, True),
        ([503, 200], 2, True),
        ([408, 200], 2, True),
        ([425, 200], 2, True),
        ([401], 1, False),
        ([403], 1, False),
        ([500, 500, 500], 3, False),
    ]
    prov = LlmProvider(api_format=ApiFormat.ANTHROPIC,
                       endpoint="https://x.io", api_key="k", model="m")
    for seq, expected_calls, expected_success in cases:
        attempts = []
        def make_fake(seq=seq):
            def fake(**kw):
                idx = len(attempts)
                attempts.append(1)
                status = seq[idx] if idx < len(seq) else 200
                if status == 200:
                    return ('{"ok":true}', False, status, "", None)
                return (None, False, status, "", None)
            return fake
        orig_sleep = time.sleep
        time.sleep = lambda *_a, **_kw: None
        orig_call = L.call_llm_api
        L.call_llm_api = make_fake()
        try:
            r = call_llm_api_with_retry(
                provider=prov, system_prompt="s", image_b64="QUFB",
                media_type="image/png", user_text="hi", max_tokens=100,
                retries=3)
            # H8: bare `assert` doesn't go through the check() helper, so
            # failures here would be invisible to the test runner. Route
            # every assertion through check() so they count.
            check("t7-seq-%s-attempts" % seq,
                  len(attempts) == expected_calls)
            check("t7-seq-%s-success" % seq,
                  (r[0] is not None) == expected_success)
            # r is a 5-tuple now.
            check("t7-seq-%s-shape" % seq, len(r) == 5)
        finally:
            time.sleep = orig_sleep
            L.call_llm_api = orig_call


t7_cases()


import rca_core.llm as L
def _fake_with_body(**kw):
    return None, False, 502, "rate limit exceeded", None
L.call_llm_api = _fake_with_body
from rca_core import ApiFormat, LlmProvider
prov = LlmProvider(api_format=ApiFormat.ANTHROPIC,
                   endpoint="https://x.io", api_key="k", model="m")
res = L.call_llm_api_with_retry(
    provider=prov, system_prompt="s", image_b64="QUFB",
    media_type="image/png", user_text="hi", max_tokens=100)
del L.call_llm_api
if res[2] == 502 and "rate limit exceeded" in (res[3] or ""):
    globals()['_pass'] += 1
    print("PASS", "h7-error-body-attached")
else:
    globals()['_fail'] += 1
    print("FAIL", "h7-error-body-attached", repr(res))

print("--- %d passed, %d failed ---" % (_pass, _fail))
sys.exit(1 if _fail else 0)
