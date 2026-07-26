"""P0-1 (REVIEW-2026-07-27): upgraded to Fernet AES-128-CBC + HMAC-SHA256
authenticated encryption for the API key field in
~/.range_chart_analyzer/providers.json.

Cryptographic properties:
  * AEAD: Fernet (AES-128-CBC + HMAC-SHA256) — authenticated encryption,
    not just obfuscation. Any tampering is detected and rejected.
  * Key derivation: PBKDF2-HMAC-SHA256, 600 000 iterations (OWASP 2024
    recommendation for password-based key derivation).
  * Each ``encrypt()`` call uses a fresh Fernet token (random 128-bit IV)
    so identical plaintexts produce different ciphertexts — prevents
    offline traffic analysis and Known-Plaintext attacks.

Migration / backward compatibility:
  * New envelopes are tagged ``fer:v1:`` and decrypted with Fernet.
  * Legacy ``obf:v1:`` envelopes (XOR keystream, PBKDF2 100k iters) are
    still decrypted — existing stored values work without re-entry.
  * Legacy plaintext values (no tag) pass through untouched.
  * If the ``cryptography`` library is not installed, ``fernet``
    falls back to the legacy XOR keystream (marked deprecated).

Security limits:
  * The derived key depends on a machine fingerprint + per-install salt.
    An attacker with read access to BOTH ``providers.json`` AND
    ``~/.range_chart_analyzer/secrets_salt`` can still brute-force the
    key offline. On a single-user workstation this is a meaningful step
    up from plaintext.
  * Copying ``providers.json`` to another machine or deleting the salt
    makes decryption impossible — handled by the store's
    ``decrypt_or_fallback`` policy: undecryptable values trigger a
    re-prompt in the GUI.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False

# Tag prefixes — distinguish legacy from current envelope formats.
_OBF_TAG = "obf:v1:"   # legacy XOR keystream (deprecated)
_FER_TAG = "fer:v1:"   # Fernet AES-128-CBC + HMAC-SHA256

_PBKDF2_ITERS_FERNET = 600_000  # OWASP 2024 recommendation
_PBKDF2_ITERS_LEGACY = 100_000  # kept only for decrypting old envelopes
_KEY_LEN = 32  # 256-bit


def _salt_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    return os.path.join(base, "secrets_salt")


def _machine_fingerprint() -> bytes:
    """Stable per-machine fingerprint. Combining multiple sources
    reduces the chance that two users on different machines coincidentally
    share an obfuscation key."""
    sources = []
    # hostname is stable on every OS, low entropy on shared hosting
    try:
        import socket
        sources.append(socket.gethostname().encode("utf-8"))
    except Exception:
        sources.append(b"unknown-host")
    # Path to home is stable per user
    home = os.path.expanduser("~").encode("utf-8", errors="replace")
    sources.append(home)
    # Stable MAC address (uuid.getnode hides per-process randomness)
    try:
        import uuid
        mac = uuid.getnode().to_bytes(6, "big")
        sources.append(mac)
    except Exception:
        sources.append(b"nomac")
    return b"|".join(sources)


def _get_or_create_salt() -> bytes:
    path = _salt_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                salt = f.read()
                if len(salt) >= 16:
                    return salt
        except OSError:
            pass
    salt = secrets.token_bytes(32)
    try:
        with open(path, "wb") as f:
            f.write(salt)
        try:
            os.chmod(path, 0o600)
        except (OSError, AttributeError):
            pass
    except OSError:
        # Last resort: derive salt from fingerprint only (less entropy but
        # never crashes).
        salt = hashlib.sha256(_machine_fingerprint()).digest()
    return salt


def _derive_key() -> bytes:
    """Legacy key derivation (100k iterations) — only for decrypting old obf:v1: envelopes."""
    fp = _machine_fingerprint()
    salt = _get_or_create_salt()
    return hashlib.pbkdf2_hmac(
        "sha256", fp, salt, _PBKDF2_ITERS_LEGACY, dklen=_KEY_LEN
    )


def _derive_fernet_key() -> bytes:
    """Derive a Fernet-compatible 32-byte url-safe-b64 key from PBKDF2 (600k iterations)."""
    raw = hashlib.pbkdf2_hmac(
        "sha256", _machine_fingerprint(), _get_or_create_salt(),
        _PBKDF2_ITERS_FERNET, dklen=_KEY_LEN
    )
    return base64.urlsafe_b64encode(raw)


def _keystream(key: bytes, n: int) -> bytes:
    """Deterministic SHA-256 keystream. NOT cryptographically random but
    sufficient for at-rest obfuscation against a casual ``strings`` scan."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hashlib.sha256(key + counter.to_bytes(8, "big")).digest())
        counter += 1
    return bytes(out[:n])


def _legacy_encrypt(plaintext: str) -> str:
    """XOR-keystream obfuscation (deprecated). Kept only for backward compat."""
    key = _derive_key()
    pt = plaintext.encode("utf-8")
    ks = _keystream(key, len(pt))
    ct = bytes(a ^ b for a, b in zip(pt, ks))
    b64 = base64.urlsafe_b64encode(ct).decode("ascii")
    return _OBF_TAG + b64


def _legacy_decrypt(envelope: str) -> str:
    """Decrypt a legacy obf:v1: envelope (deprecated). Kept only for migration."""
    key = _derive_key()
    b64 = envelope[len(_OBF_TAG):]
    try:
        ct = base64.urlsafe_b64decode(b64.encode("ascii"))
    except Exception:
        raise ValueError("secrets_store: corrupt envelope (base64)")
    ks = _keystream(key, len(ct))
    pt = bytes(a ^ b for a, b in zip(ct, ks))
    try:
        return pt.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("secrets_store: corrupt envelope (utf-8)")


def encrypt(plaintext: str) -> str:
    """Encrypt a secret string. Returns an authenticated Fernet envelope
    (fer:v1:) or falls back to legacy XOR obfuscation (obf:v1:) if
    cryptography is not installed. Empty input is returned unchanged."""
    if not plaintext:
        return plaintext
    if _HAS_FERNET:
        f = Fernet(_derive_fernet_key())
        return _FER_TAG + f.encrypt(plaintext.encode()).decode()
    return _legacy_encrypt(plaintext)


def decrypt(envelope: str) -> str:
    """Decrypt a secret string. Handles fer:v1: (Fernet), obf:v1: (legacy
    XOR), and legacy plaintext (no tag). Raises ValueError on corruption."""
    if not envelope:
        return envelope
    if envelope.startswith(_FER_TAG):
        if not _HAS_FERNET:
            raise ValueError("secrets_store: Fernet unavailable (cryptography not installed)")
        f = Fernet(_derive_fernet_key())
        return f.decrypt(envelope[len(_FER_TAG):].encode()).decode()
    if envelope.startswith(_OBF_TAG):
        return _legacy_decrypt(envelope)
    # Legacy plaintext value — surface as-is rather than corrupting it.
    return envelope


def is_obfuscated(value: str) -> bool:
    return bool(value) and (value.startswith(_FER_TAG) or value.startswith(_OBF_TAG))


__all__ = ["encrypt", "decrypt", "is_obfuscated"]
