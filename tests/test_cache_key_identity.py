r"""The chart-classify cache key could not tell two providers apart.

Both extraction key sites in server.py pass extra_headers and extra_body. The
chart-classify key in extractor.py did not -- and its own comment said so,
without recognising it as the bug: "The sibling extraction keys already include
both fields."

That omission is a collision the project had ALREADY named, in
_stable_extra_body's docstring on the extraction side: "Two requests with
different extra_body MUST NOT share a cache entry." And in
tests_server_hardening.py: the old make_key "only hashed the keys, allowing a
user to flip Authorization header values and get a cached wrong-identity
response." That was fixed for extraction. The classify key was left behind.

It matters more on the classify side than on the extraction side. The verdict
decides which CHART TYPE the image is, and mode="auto" derives the extraction
mode from it -- so a verdict served from another identity's cache silently
selects the wrong prompt, the wrong normalizer and the wrong merge schema for
the same image. No error, no warning: different data.

The helpers were also the wrong shape for the fix. _stable_extra_headers and
_stable_extra_body were NESTED defs inside the request handler, so extractor.py
could not reuse them, and re-deriving them there would have been exactly the
duplication that let the two drift. They now live in rca_core.cache beside
make_key -- where the next key builder will look -- and server.py imports
them.

Measured, and the bound is narrow on purpose: the collision needs the same
endpoint, model and api_format. A different endpoint already separates, so
this is not "one provider's verdict serves another's" in general; it is two
identities on the same vendor differing only in credentials or vendor routing.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from rca_core.cache import ResultCache  # noqa: E402
# The module is imported, not the helper NAMES. A first draft did
# `from rca_core.cache import stable_extra_body, stable_extra_headers`, and
# the reverse-proof showed why that is wrong: withdrawing the helpers made the
# whole test file un-importable, so pytest exited 2 (a collection error) and
# took the other nine tests with it. A regression should make a test FAIL, not
# make the module uncollectable -- so the attributes are looked up inside the
# tests that need them, and the file always imports.
import rca_core.cache as cache_mod  # noqa: E402

EXTRACTOR = pathlib.Path(PROJECT_ROOT) / "rca_core" / "extractor.py"
SERVER = pathlib.Path(PROJECT_ROOT) / "server.py"


class _Fmt:
    def __init__(self, value):
        self.value = value


class Provider:
    """The three attributes a provider object exposes to the key builders."""

    def __init__(self, endpoint="https://api.example.com", model="m1",
                 fmt="anthropic", headers=None, body=None, effort=""):
        self.endpoint = endpoint
        self.model = model
        self.api_format = _Fmt(fmt)
        self.extra_headers = headers if headers is not None else {}
        self.extra_body = body if body is not None else {}
        # REVIEW-2026-10-19: the reasoning effort is part of the key too -- it
        # changes the request shape the same way extra_body does, and on
        # MiniMax's M3.1 it decides whether the call answers at all.
        self.reasoning_effort = effort


A = Provider(headers={"Authorization": "Bearer KEY-A"},
             body={"routing": "eu"})
B = Provider(headers={"Authorization": "Bearer KEY-B"},
             body={"routing": "us"})
SAME_ENDPOINT_OTHER_MODEL = Provider(model="m2",
                                    headers={"Authorization": "Bearer KEY-B"})
OTHER_ENDPOINT = Provider(endpoint="https://other.example.com",
                          headers={"Authorization": "Bearer KEY-B"})


def _classify_key_field_names():
    """The classification key's field set, declared by the factory.

    ARCH-REVIEW A2: the three hand-written make_key() call sites were
    consolidated into rca_core.cache.build_extraction_key. The old helper
    scraped extractor.py's AST for a `make_key` call with mode=chart_classify;
    that call no longer exists by name. The factory's CACHE_KEY_REQUIRED is
    now the SINGLE declaration, which is strictly better than scraping one
    call site (a new call site could never be noticed).
    """
    return set(cache_mod.CACHE_KEY_REQUIRED)


# The values the product supplies for each field. Only the names decide which
# of them reach the key.
_CLASSIFY_VALUES = {
    "endpoint": lambda p: p.endpoint,
    "model": lambda p: p.model,
    "api_format": lambda p: p.api_format.value,
    "extra_headers": lambda p: cache_mod.stable_extra_headers(p),
    "extra_body": lambda p: cache_mod.stable_extra_body(p),
    "reasoning_effort": lambda p: cache_mod.stable_reasoning_effort(p),
    "prompt_version": lambda p: "v3",
    "mode": lambda p: "chart_classify",
    "image_b64": lambda p: "A" * 400,
    "caption": lambda p: "Fig. 1",
    "chart_lang": lambda p: "auto",
}


def _classify_key(p):
    """Build the classify key the way the PRODUCT builds it (via the factory)."""
    fields = {name: _CLASSIFY_VALUES[name](p)
              for name in _classify_key_field_names()}
    return cache_mod.build_extraction_key(
        **fields, max_tokens=None, media_type=None, run_idx=None)


# --- the collision ------------------------------------------------------


def test_two_identities_get_different_classify_keys():
    assert _classify_key(A) != _classify_key(B), (
        "two providers differing only in Authorization / extra_body shared one "
        "cached classification"
    )


def test_changing_only_the_authorization_value_is_enough():
    a = _classify_key(Provider(headers={"Authorization": "Bearer A"}))
    b = _classify_key(Provider(headers={"Authorization": "Bearer B"}))
    assert a != b


def test_changing_only_extra_body_is_enough():
    a = _classify_key(Provider(body={"caching": "on"}))
    b = _classify_key(Provider(body={"caching": "off"}))
    assert a != b


def test_an_identical_provider_still_hits():
    """The other direction: adding the fields must not make every key unique."""
    assert _classify_key(A) == _classify_key(Provider(
        headers={"Authorization": "Bearer KEY-A"}, body={"routing": "eu"}))


def test_changing_only_the_reasoning_effort_is_enough():
    """A cached result is a result for ONE request, not for the image.

    The effort changes the request shape the same way extra_body does, so
    the same rule applies: two efforts must not share a cache entry.
    """
    a = _classify_key(Provider(effort="low"))
    b = _classify_key(Provider(effort="high"))
    assert a != b
    assert a != _classify_key(Provider())


# --- the bound, stated so the severity is not oversold ------------------


def test_a_different_endpoint_still_separated_them_before_the_fix():
    assert _classify_key(A) != _classify_key(OTHER_ENDPOINT)
    assert _classify_key(A) != _classify_key(SAME_ENDPOINT_OTHER_MODEL)


# --- the invariant is enforced, not just satisfied by luck --------------


def _factory_call_sites():
    """Every build_extraction_key() call, ONE ENTRY PER CALL SITE."""
    out = []
    for label, path in (("extractor", EXTRACTOR), ("server", SERVER)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "build_extraction_key"):
                out.append((label, node.lineno,
                            {k.arg for k in node.keywords if k.arg}))
    return out


def test_every_key_is_built_by_the_factory():
    """No hand-written make_key(**fields) may exist outside cache.py."""
    offenders = []
    for label, path in (("extractor", EXTRACTOR), ("server", SERVER)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "make_key"):
                offenders.append(f"{label}.py:{node.lineno}")
    assert not offenders, (
        "hand-written cache keys bypass the factory's required-field guard: "
        + ", ".join(offenders))
    assert _factory_call_sites(), "no factory call site found - AST probe stale"


def test_no_cache_key_site_omits_the_identity_fields():
    """Checked PER CALL SITE, not against a module-wide union.

    ARCH-REVIEW A2: the union version was vacuous -- a new builder omitting
    extra_headers / extra_body / reasoning_effort could never fail because
    older correct sites had already put those names in the union.
    """
    sites = _factory_call_sites()
    assert sites, "no build_extraction_key() call site found - AST probe stale"
    for label, lineno, present in sites:
        for required in cache_mod.CACHE_KEY_REQUIRED:
            assert required in present, (
                f"{label}.py:{lineno} cache key omits {required}: "
                f"{sorted(present)}"
            )


def test_factory_declares_a_complete_field_set():
    assert cache_mod.CACHE_KEY_REQUIRED <= cache_mod.CACHE_KEY_FIELDS
    assert "prompt_version" in cache_mod.CACHE_KEY_REQUIRED
    for required in ("extra_headers", "extra_body", "reasoning_effort"):
        assert required in cache_mod.CACHE_KEY_REQUIRED


def test_the_helpers_live_next_to_make_key():
    """So they cannot drift again by being unreachable from a second module.

    The original defect was possible precisely because the helpers were nested
    inside server.py's request handler: extractor.py wanted the same guarantee
    and could not have it.
    """
    assert hasattr(cache_mod, "stable_extra_headers"), (
        "stable_extra_headers must live in rca_core.cache, beside make_key"
    )
    assert hasattr(cache_mod, "stable_extra_body"), (
        "stable_extra_body must live in rca_core.cache, beside make_key"
    )
    # REVIEW-2026-10-19: the reasoning-effort helper joins them for the same
    # reason -- extractor.py and server.py both need it, and a second copy
    # would drift.
    assert hasattr(cache_mod, "stable_reasoning_effort"), (
        "stable_reasoning_effort must live in rca_core.cache, beside make_key"
    )
    src = EXTRACTOR.read_text(encoding="utf-8")
    assert "stable_extra_headers" in src, (
        "the classify key must import the shared helper, not re-derive it"
    )


def test_the_helpers_are_order_insensitive_but_value_sensitive():
    a = cache_mod.stable_extra_headers(Provider(headers={"X-A": "1", "X-B": "2"}))
    b = cache_mod.stable_extra_headers(Provider(headers={"X-B": "2", "X-A": "1"}))
    assert a == b, "dict ordering must not change the key"
    c = cache_mod.stable_extra_headers(Provider(headers={"X-A": "9", "X-B": "2"}))
    assert a != c, "header VALUES must change the key"


@pytest.mark.parametrize("prov", [None, Provider(headers=None, body=None)])
def test_a_missing_provider_is_handled(prov):
    assert cache_mod.stable_extra_headers(prov) == []
    assert cache_mod.stable_extra_body(prov) == []
