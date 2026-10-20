"""The model list the endpoint serves, and the button that fetches it.

A wrong model name is the commonest configuration mistake, and it fails
LATE: the settings look fine and the first extraction comes back 404.
cc-switch's answer is to make the endpoint list its own models
("自动获取模型") rather than making the user copy a slug off a web page.

The plumbing was already here -- the connection test GETs ``/v1/models`` and
puts the ids in ``ConnectionResult.models_sample`` -- but the WIZARD never
offered them, so the user had to go and read the vendor's documentation.
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rca_core.llm as L                                     # noqa: E402


def _provider(fmt=L.ApiFormat.OPENAI, endpoint="https://api.example.com/v1"):
    return L.LlmProvider(name="p", api_format=fmt, endpoint=endpoint,
                          api_key="sk-test", model="m")


def _listing(ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode("utf-8")


class _AllowEndpoint:
    """Neutralise the SSRF policy so the test is about PARSING.

    The guard resolves the hostname, and in a test environment
    ``api.example.com`` can resolve to a non-public address -- which would
    make the test assert the network's mood rather than the code.
    """

    def __enter__(self):
        import rca_core.ssrf as ssrf
        self._orig = ssrf.validate_endpoint_local_ok
        ssrf.validate_endpoint_local_ok = lambda endpoint: (True, "")
        return self

    def __exit__(self, *exc):
        import rca_core.ssrf as ssrf
        ssrf.validate_endpoint_local_ok = self._orig
        return False


def test_the_openai_listing_is_read():
    with _AllowEndpoint(), \
            patch.object(L, "_get_json",
                         return_value=(_listing(["a", "b"]), 200, b"")):
        assert L.list_models(_provider(), timeout_sec=5) == ["a", "b"]


def test_the_anthropic_route_is_tried_too():
    """MiniMax's own endpoint answers the listing on BOTH routes."""
    with _AllowEndpoint(), \
            patch.object(L, "_get_json", return_value=(_listing(["m1"]), 200, b"")):
        models = L.list_models(
            _provider(L.ApiFormat.ANTHROPIC,
                      "https://api.minimaxi.com/anthropic"), timeout_sec=5)
    assert models == ["m1"]


def test_the_gemini_listing_shape_is_read():
    payload = json.dumps({"models": [{"name": "g1"}]}).encode("utf-8")
    with _AllowEndpoint(), \
            patch.object(L, "_get_json", return_value=(payload, 200, b"")):
        assert L.list_models(
            _provider(L.ApiFormat.GEMINI, "https://x.example.com"), timeout_sec=5
        ) == ["g1"]


def test_a_listing_that_fails_is_not_an_error():
    """Many relay services have no listing route; the user types the name."""
    with _AllowEndpoint(), \
            patch.object(L, "_get_json", return_value=(None, 404, b"")):
        assert L.list_models(_provider(), timeout_sec=5) == []
    with _AllowEndpoint(), \
            patch.object(L, "_get_json", side_effect=RuntimeError("boom")):
        assert L.list_models(_provider(), timeout_sec=5) == []


def test_a_malformed_body_is_not_an_error():
    with _AllowEndpoint(), \
            patch.object(L, "_get_json", return_value=(b"not json", 200, b"")):
        assert L.list_models(_provider(), timeout_sec=5) == []


def test_a_private_endpoint_is_never_dialled():
    """The key must not go out to an internal host.

    Loopback is deliberately ALLOWED -- a local Ollama server is a
    legitimate desktop use case, and the whole app uses that one policy
    (``validate_endpoint_local_ok``). A private address that is NOT
    loopback is refused, and nothing is dialled at all.
    """
    called = {}

    def fake(url, headers, timeout_sec):
        called["url"] = url
        return _listing(["x"]), 200, b""

    with patch.object(L, "_get_json", side_effect=fake):
        models = L.list_models(
            _provider(endpoint="https://10.0.0.5/v1"), timeout_sec=5)
    assert models == [], "a private endpoint must not be dialled with the key"
    assert not called, "no request may be made at all"
