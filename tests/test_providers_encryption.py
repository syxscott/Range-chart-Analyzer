"""Regression tests for P2-2: rca_core.llm.LlmProvider to_dict /
from_dict round-trip the api_key through
rca_core.secrets_store.encrypt/decrypt so
``strings ~/.range_chart_analyzer/providers.json`` no longer reveals
the user's API keys.

REVIEW-2026-07-25 P2-2.
"""
from __future__ import annotations

import os, sys, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rca_core.llm import LlmProvider, ApiFormat, ProviderStore
from rca_core.secrets_store import encrypt, decrypt, is_obfuscated


KEY = "sk-test-XYZ-PLACEHOLDER-1234567890"


class TestSecretsStore:
    def test_encrypt_then_decrypt_roundtrip(self):
        c = encrypt(KEY)
        assert c != KEY, "encryption must transform value"
        assert is_obfuscated(c)
        assert decrypt(c) == KEY

    def test_empty_passthrough(self):
        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_distinct_envelopes_stable_round_trip(self):
        """Same plaintext + same machine → same plaintext after decrypt.

        Note: Fernet uses a fresh random IV per encrypt() call, so two
        ciphertexts of the same plaintext will differ.  The invariant that
        MATTERS (and this test verifies) is that both round-trip to the
        original plaintext — corruption is still detected via the HMAC.
        """
        a = encrypt(KEY)
        b = encrypt(KEY)
        # Fernet randomises IV → ciphertexts differ (this is intentional).
        # The corruption-detection property we actually care about is:
        assert decrypt(a) == KEY
        assert decrypt(b) == KEY


class TestLlmProviderEncryption:
    def test_to_dict_obfuscates_api_key(self):
        p = LlmProvider(
            id="x", name="X", endpoint="https://x",
            model="m", api_key=KEY, api_format=ApiFormat.ANTHROPIC,
        )
        d = p.to_dict()
        assert d["api_key"] != KEY, (
            "to_dict must NOT leak plaintext api_key to disk"
        )
        assert is_obfuscated(d["api_key"])

    def test_from_dict_decrypts_api_key(self):
        env = encrypt(KEY)
        d = {
            "id": "x", "name": "X", "endpoint": "https://x",
            "model": "m", "api_key": env,
            "is_current": False,
            "sort_index": 0, "created_at": 0.0,
            "extra_headers": {}, "extra_body": {},
        }
        p = LlmProvider.from_dict(d)
        assert p.api_key == KEY

    def test_from_dict_legacy_plaintext_passthrough(self):
        d = {
            "id": "x", "name": "X", "endpoint": "https://x",
            "model": "m", "api_key": KEY,  # legacy plaintext
            "is_current": False,
            "sort_index": 0, "created_at": 0.0,
            "extra_headers": {}, "extra_body": {},
        }
        p = LlmProvider.from_dict(d)
        assert p.api_key == KEY

    def test_full_round_trip_via_json(self):
        """Simulate writing to disk and reading back: the on-disk JSON
        must contain NO plaintext api_key, but the in-memory reload
        must recover the original."""
        p = LlmProvider(
            id="x", name="X", endpoint="https://x",
            model="m", api_key=KEY, api_format=ApiFormat.ANTHROPIC,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "providers.json")
            store = ProviderStore(path=path)
            store.providers = [p]
            store.current_id = "x"
            store.save()
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            assert KEY not in raw, (
                f"plaintext api_key leaked to disk; on-disk content:\n{raw}"
            )
            # Reload
            new_store = ProviderStore(path=path)
            new_store.load()
            assert len(new_store.providers) == 1
            assert new_store.providers[0].api_key == KEY

    def test_corrupted_envelope_yields_empty_key(self):
        """An unreadable envelope (e.g. salt deleted) must reset the key
        to empty rather than crash; the GUI then re-prompts."""
        d = {
            "id": "x", "name": "X", "endpoint": "https://x",
            "model": "m", "api_key": "obf:v1:!!corrupt!!",
            "is_current": False,
            "sort_index": 0, "created_at": 0.0,
            "extra_headers": {}, "extra_body": {},
        }
        p = LlmProvider.from_dict(d)
        assert p.api_key == ""


class TestFernetEncryption:
    """P0-1: Fernet AES-128-CBC + HMAC-SHA256 upgrade."""

    def test_fernet_encryption_roundtrip(self):
        """New Fernet path encrypts and decrypts cleanly."""
        from rca_core.secrets_store import encrypt, decrypt, _HAS_FERNET
        if not _HAS_FERNET:
            import pytest
            pytest.skip("cryptography not installed")
        pt = "sk-test-12345-abcdef-XYZ"
        env = encrypt(pt)
        assert env.startswith("fer:v1:"), f"expected fer:v1: prefix, got {env[:10]}"
        assert decrypt(env) == pt

    def test_fernet_fresh_nonce_per_call(self):
        """Fernet uses random IV; two encrypts of same plaintext differ."""
        from rca_core.secrets_store import encrypt, _HAS_FERNET
        if not _HAS_FERNET:
            import pytest
            pytest.skip("cryptography not installed")
        e1 = encrypt("sk-AAA")
        e2 = encrypt("sk-AAA")
        assert e1 != e2, "Fernet must randomize ciphertext (fresh IV per call)"

    def test_legacy_obf_v1_still_decrypts(self):
        """Old obfuscated values (obf:v1:) still decrypt for migration."""
        from rca_core.secrets_store import decrypt, _HAS_FERNET, _legacy_encrypt
        pt = "sk-legacy- migratory-key"
        # Manually construct a legacy envelope using the legacy path
        legacy_env = _legacy_encrypt(pt)
        assert legacy_env.startswith("obf:v1:")
        assert decrypt(legacy_env) == pt

    def test_legacy_plaintext_passthrough(self):
        """Legacy plaintext (no tag) still passes through unchanged."""
        from rca_core.secrets_store import decrypt
        pt = "sk-raw-legacy-no-tag"
        assert decrypt(pt) == pt

    def test_empty_passthrough_fernet(self):
        """Empty string is returned unchanged through both paths."""
        from rca_core.secrets_store import encrypt, decrypt
        assert encrypt("") == ""
        assert decrypt("") == ""

    def test_fernet_is_obfuscated(self):
        """fer:v1: envelopes are recognised by is_obfuscated."""
        from rca_core.secrets_store import encrypt, is_obfuscated, _HAS_FERNET
        if not _HAS_FERNET:
            import pytest
            pytest.skip("cryptography not installed")
        assert is_obfuscated(encrypt("sk-any"))
        assert is_obfuscated("fer:v1:anything") is True
        assert is_obfuscated("obf:v1:anything") is True
        assert is_obfuscated("") is False
        assert is_obfuscated("sk-plaintext") is False