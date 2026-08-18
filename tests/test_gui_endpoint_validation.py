"""Test P0-2: SSRF validation is enforced in _call_anthropic and _call_openai.

The GUI path calls _call_anthropic/_call_openai directly without going through
server.py's validate_endpoint. This test verifies that both functions call the
SSRF guard and return an error tuple for invalid endpoints.

REVIEW-2026-07-31: the policy is now `validate_endpoint_local_ok`
(rca_core/ssrf) — the SAME policy used by the connection test and the
Gemini path. Loopback + plain-http local endpoints (Ollama at
http://127.0.0.1:11434) are legitimate and MUST pass; public http,
private IPs, and empty endpoints are still rejected. Previously the
extract paths used the strict validator while the connection test used
the loopback-permitting one, so a local endpoint passed the test but
every extraction failed.
"""
import pytest
from unittest.mock import patch, MagicMock

from rca_core.llm import _call_anthropic, _call_openai, LlmProvider, ApiFormat


class TestGuiEndpointValidation:
    """P0-2: GUI path SSRF validation."""

    def test_call_anthropic_rejects_http_public_endpoint(self):
        """_call_anthropic returns SSRF error for http (cleartext) PUBLIC
        endpoints. (Plain-http LOOPBACK endpoints remain allowed for local
        Ollama-style servers — see test_call_anthropic_allows_local_ollama.)"""
        provider = LlmProvider(
            id="test-p", name="Test Provider", api_format=ApiFormat.ANTHROPIC,
            endpoint="http://example.com/api", api_key="test-key", model="test-model",
        )
        text, truncated, status, err_body, usage = _call_anthropic(
            provider=provider, system_prompt="test", image_b64="test",
            media_type="image/png", user_text="test", max_tokens=100, timeout_sec=5,
        )
        assert text is None
        assert "endpoint rejected by SSRF guard" in err_body
        assert "https required" in err_body

    def test_call_openai_rejects_private_ip(self):
        """_call_openai returns SSRF error for private IP endpoints."""
        provider = LlmProvider(
            id="test-p", name="Test Provider", api_format=ApiFormat.OPENAI,
            endpoint="https://192.168.1.1/v1", api_key="test-key", model="gpt-4o",
        )
        text, truncated, status, err_body, usage = _call_openai(
            provider=provider, system_prompt="test", image_b64="test",
            media_type="image/png", user_text="test", max_tokens=100, timeout_sec=5,
        )
        assert text is None
        assert "endpoint rejected by SSRF guard" in err_body
        assert "non-public" in err_body.lower()

    def test_call_anthropic_rejects_empty_endpoint(self):
        """_call_anthropic returns SSRF error for empty endpoints."""
        provider = LlmProvider(
            id="test-p", name="Test Provider", api_format=ApiFormat.ANTHROPIC,
            endpoint="", api_key="test-key", model="test-model",
        )
        text, truncated, status, err_body, usage = _call_anthropic(
            provider=provider, system_prompt="test", image_b64="test",
            media_type="image/png", user_text="test", max_tokens=100, timeout_sec=5,
        )
        assert text is None
        assert "endpoint rejected by SSRF guard" in err_body
        assert "empty" in err_body.lower()

    def test_validate_endpoint_is_called_in_anthropic(self):
        """_call_anthropic calls validate_endpoint_local_ok with
        provider.endpoint (the unified policy)."""
        from rca_core import ssrf as ssrf_module
        original = ssrf_module.validate_endpoint_local_ok
        captured = []

        def capture_validator(endpoint):
            captured.append(endpoint)
            return original(endpoint)

        ssrf_module.validate_endpoint_local_ok = capture_validator
        try:
            provider = LlmProvider(
                id="test-p", name="Test Provider", api_format=ApiFormat.ANTHROPIC,
                endpoint="https://api.anthropic.com", api_key="test-key", model="test-model",
            )
            _call_anthropic(
                provider=provider, system_prompt="test", image_b64="test",
                media_type="image/png", user_text="test", max_tokens=100, timeout_sec=5,
            )
        finally:
            ssrf_module.validate_endpoint_local_ok = original
        assert len(captured) == 1
        assert captured[0] == "https://api.anthropic.com"

    def test_validate_endpoint_is_called_in_openai(self):
        """_call_openai calls validate_endpoint_local_ok with
        provider.endpoint (the unified policy)."""
        from rca_core import ssrf as ssrf_module
        original = ssrf_module.validate_endpoint_local_ok
        captured = []

        def capture_validator(endpoint):
            captured.append(endpoint)
            return original(endpoint)

        ssrf_module.validate_endpoint_local_ok = capture_validator
        try:
            provider = LlmProvider(
                id="test-p", name="Test Provider", api_format=ApiFormat.OPENAI,
                endpoint="https://api.openai.com/v1", api_key="test-key", model="gpt-4o",
            )
            _call_openai(
                provider=provider, system_prompt="test", image_b64="test",
                media_type="image/png", user_text="test", max_tokens=100, timeout_sec=5,
            )
        finally:
            ssrf_module.validate_endpoint_local_ok = original
        assert len(captured) == 1
        assert captured[0] == "https://api.openai.com/v1"

    def test_call_anthropic_allows_local_ollama(self):
        """Loopback + plain-http endpoints MUST pass the guard — the
        connection test already allowed them, and extraction must agree
        (REVIEW-2026-07-31 policy unification)."""
        provider = LlmProvider(
            id="test-p", name="Ollama", api_format=ApiFormat.ANTHROPIC,
            endpoint="http://127.0.0.1:11434", api_key="x", model="llama3",
        )
        # The guard passes; the failure must NOT be an SSRF rejection
        # (it will proceed to make a network call against a nonexistent
        # local server and fail with a network error instead).
        text, truncated, status, err_body, usage = _call_anthropic(
            provider=provider, system_prompt="test", image_b64="test",
            media_type="image/png", user_text="test", max_tokens=100, timeout_sec=1,
        )
        assert "endpoint rejected by SSRF guard" not in err_body

    def test_call_openai_rejects_localhost_without_explicit_loopback(self):
        """Private RFC1918 hosts stay rejected even with the local-ok
        policy — only loopback is whitelisted."""
        provider = LlmProvider(
            id="test-p", name="Test Provider", api_format=ApiFormat.OPENAI,
            endpoint="https://192.168.1.1/v1", api_key="test-key", model="gpt-4o",
        )
        text, truncated, status, err_body, usage = _call_openai(
            provider=provider, system_prompt="test", image_b64="test",
            media_type="image/png", user_text="test", max_tokens=100, timeout_sec=5,
        )
        assert text is None
        assert "endpoint rejected by SSRF guard" in err_body

    def test_ssrf_error_is_importable(self):
        """SSRFError should be importable from rca_core.ssrf."""
        from rca_core.ssrf import SSRFError
        err = SSRFError("test")
        assert "test" in str(err)
        assert isinstance(err, Exception)

    def test_validate_endpoint_rejects_http(self):
        """validate_endpoint should reject http scheme."""
        from rca_core.ssrf import validate_endpoint
        ok, msg = validate_endpoint("http://example.com")
        assert not ok
        assert "https" in msg.lower()

    def test_validate_endpoint_accepts_https_public(self):
        """validate_endpoint should accept https public endpoints."""
        from rca_core.ssrf import validate_endpoint
        ok, msg = validate_endpoint("https://api.anthropic.com")
        assert ok
        assert msg == ""
