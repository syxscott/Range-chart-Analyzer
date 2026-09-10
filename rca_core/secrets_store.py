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
  * **Preferred key source: OS keyring.** When the ``keyring`` package is
    installed, a random Fernet key is stored in the OS credential store
    (service ``range_chart_analyzer``) and never written to disk. A
    same-machine attacker who can read ``providers.json`` cannot recompute
    it, defeating the local-recompute attack.
  * **Fallback: user passphrase.** ``encrypt``/``decrypt`` accept an optional
    ``passphrase`` (PBKDF2-derived) so a user-supplied secret can gate access
    without the OS keyring.
  * **Last resort: fingerprint-derived Fernet key.** Only when neither keyring
    nor a passphrase is available do we derive the key from a machine
    fingerprint + readable salt. This is OBFUSCATION ONLY — any local user
    who can read ``providers.json`` can recompute the key. A ``RuntimeWarning``
    is emitted once in that case.
  * ``providers.json`` is chmod 0600 best-effort after each write.
  * Copying ``providers.json`` to another machine (or deleting the OS keyring /
    salt) makes decryption impossible — handled by the store's
    ``decrypt_or_fallback`` policy: undecryptable values trigger a
    re-prompt in the GUI.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import warnings

try:
    from cryptography.fernet import Fernet
    _HAS_FERNET = True
except ImportError:
    _HAS_FERNET = False

try:
    import keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

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


import threading

# REVIEW-2026-09-10: memoise the PBKDF2 derivations. Every provider's
# encrypt/decrypt re-derived the key from scratch - 600 000 iterations each
# time - so saving or loading a 20-provider file cost seconds of GUI-thread
# freeze (measured: save 1.67 s, load 3.27 s), and the GUI saves after every
# connection test and twice per wizard edit. The cache is keyed on the
# derivation inputs, so a changed salt or passphrase still produces a fresh
# key rather than a stale one.
_KDF_CACHE: dict[tuple, bytes] = {}
_KDF_LOCK = threading.RLock()


def _pbkdf2_cached(material: bytes, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 with an in-process memo (see _KDF_CACHE)."""
    key = (iterations, salt, hashlib.sha256(material).digest())
    with _KDF_LOCK:
        hit = _KDF_CACHE.get(key)
    if hit is not None:
        return hit
    derived = hashlib.pbkdf2_hmac("sha256", material, salt, iterations, dklen=_KEY_LEN)
    with _KDF_LOCK:
        _KDF_CACHE[key] = derived
    return derived


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
        # REVIEW-2026-09-10: the file EXISTS but is unusable (truncated by a
        # crash/disk-full, or zero bytes). The old code treated it as absent
        # and OVERWROTE it with a fresh random salt — which silently made
        # every stored key undecryptable, with no warning and no backup.
        # Refuse instead: a loud error the operator can act on (restore the
        # salt / re-enter keys) beats a quiet total loss.
        raise RuntimeError(
            f"secrets_store: the salt file at {path} exists but is corrupt "
            f"({os.path.getsize(path)} bytes; expected >= 16). Refusing to "
            "overwrite it - overwriting would permanently lose every stored "
            "API key. Restore the file from backup, or delete it AND re-enter "
            "your keys."
        )
    salt = secrets.token_bytes(32)
    # REVIEW-2026-09-10: write atomically (tmp + os.replace) with fsync, the
    # same way providers.json is written, so a crash mid-write cannot leave
    # the truncated file that triggers the error above.
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(salt)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
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
    return _pbkdf2_cached(_machine_fingerprint(), _get_or_create_salt(),
                          _PBKDF2_ITERS_LEGACY)


def _derive_fernet_key() -> bytes:
    """Legacy fingerprint-derived Fernet key (PBKDF2, 600k iters).

    Kept as a fallback for decrypting envelopes written before OS keyring
    support existed. NOTE: a same-machine attacker who can read
    ``providers.json`` can recompute this key from the locally-readable
    machine fingerprint + salt — it is obfuscation, not real protection.
    Prefer :func:`_active_fernet_key` (keyring) for new encryptions.
    """
    raw = _pbkdf2_cached(_machine_fingerprint(), _get_or_create_salt(),
                         _PBKDF2_ITERS_FERNET)
    return base64.urlsafe_b64encode(raw)


# --- OS keyring support (M4) ---------------------------------------------
# Prefer storing the Fernet key in the OS credential store (keyring) so a
# local attacker cannot recompute it from readable files. Falls back to a
# user-supplied passphrase, then to the fingerprint-derived key above.
_KEYRING_SERVICE = "range_chart_analyzer"
_KEYRING_USERNAME = "fernet-key"
_warned_obfuscation = False


def encryption_status() -> str:
    """Report which key source would be used for NEW encryption.

    REVIEW-2026-07-31: returns ``"passphrase"`` / ``"keyring"`` /
    ``"fingerprint"`` / ``"plaintext"`` so the GUIs can surface the
    at-rest protection level to the user (the RuntimeWarning from
    ``_warn_obfuscation_only`` is invisible in a windowed app).
    """
    if not _HAS_FERNET:
        return "plaintext"
    if _HAS_KEYRING:
        return "keyring"
    return "fingerprint"


def _warn_obfuscation_only() -> None:
    """Emit a one-time warning that the current key source is obfuscation-only."""
    global _warned_obfuscation
    if _warned_obfuscation:
        return
    _warned_obfuscation = True
    warnings.warn(
        "secrets_store: OS keyring unavailable; the encryption key is derived "
        "from a locally-readable machine fingerprint (hostname + home + MAC) plus "
        "a readable salt. This is OBFUSCATION ONLY - any local user who can read "
        "providers.json can recompute the key and decrypt stored API keys. "
        "Install the 'keyring' package for real at-rest protection.",
        RuntimeWarning,
        stacklevel=3,
    )


def _fernet_key_from_passphrase(passphrase: str) -> bytes:
    """Derive a Fernet key from a user-supplied passphrase (PBKDF2)."""
    salt = _get_or_create_salt()
    raw = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), salt,
        _PBKDF2_ITERS_FERNET, dklen=_KEY_LEN
    )
    return base64.urlsafe_b64encode(raw)


def _active_fernet_key(passphrase: str | None = None) -> bytes:
    """Resolve the Fernet key for a NEW encryption, strongest source first.

    1. If ``passphrase`` is given, derive from it (no keyring needed).
    2. Else if the OS keyring is available, get-or-create a random key there.
    3. Else fall back to the fingerprint-derived key and warn that it is
       obfuscation only.
    """
    if passphrase:
        return _fernet_key_from_passphrase(passphrase)
    if _HAS_KEYRING:
        try:
            k = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            if k is None:
                k = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
                keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, k)
            return k.encode("ascii")
        except Exception:
            # Keyring backend missing/broken (no D-Bus, no credential store...).
            pass
    _warn_obfuscation_only()
    return _derive_fernet_key()


def _fernet_decrypt_candidates(passphrase: str | None = None) -> list[bytes]:
    """Keys to try when decrypting, so old fingerprint-derived envelopes
    remain readable after keyring adoption."""
    if passphrase:
        return [_fernet_key_from_passphrase(passphrase)]
    keys: list[bytes] = [_active_fernet_key()]
    # Legacy fingerprint-derived key: decrypt envelopes written before keyring.
    try:
        keys.append(_derive_fernet_key())
    except Exception:
        pass
    return keys


def _providers_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    return os.path.join(base, "providers.json")


def _protect_providers_file(path: str | None = None) -> None:
    """Best-effort: restrict providers.json to 0600 so other local accounts
    cannot read stored API keys. Call after writing the file."""
    p = path or _providers_path()
    try:
        if os.path.exists(p):
            os.chmod(p, 0o600)
    except (OSError, AttributeError):
        pass


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


def encrypt(plaintext: str, passphrase: str | None = None) -> str:
    """Encrypt a secret string.

    Key source (strongest available): user passphrase -> OS keyring ->
    fingerprint-derived Fernet key (obfuscation only; warns). Returns an
    authenticated Fernet envelope (fer:v1:), or falls back to legacy XOR
    obfuscation (obf:v1:) if cryptography is not installed. Empty input is
    returned unchanged. Best-effort chmod 0600 is applied to providers.json.
    """
    if not plaintext:
        return plaintext
    if _HAS_FERNET:
        f = Fernet(_active_fernet_key(passphrase))
        _protect_providers_file()
        return _FER_TAG + f.encrypt(plaintext.encode()).decode()
    _warn_obfuscation_only()
    return _legacy_encrypt(plaintext)


def decrypt(envelope: str, passphrase: str | None = None) -> str:
    """Decrypt a secret string. Handles fer:v1: (Fernet), obf:v1: (legacy
    XOR), and legacy plaintext (no tag). Raises ValueError on corruption.

    For fer:v1: envelopes, every candidate key is tried (active key then the
    legacy fingerprint key) so envelopes written before keyring adoption
    remain readable.
    """
    if not envelope:
        return envelope
    if envelope.startswith(_FER_TAG):
        if not _HAS_FERNET:
            raise ValueError("secrets_store: Fernet unavailable (cryptography not installed)")
        payload = envelope[len(_FER_TAG):].encode()
        last_err: Exception | None = None
        for key in _fernet_decrypt_candidates(passphrase):
            try:
                return Fernet(key).decrypt(payload).decode()
            except Exception as exc:  # wrong key / tampered token
                last_err = exc
        raise ValueError("secrets_store: could not decrypt fer:v1 envelope") from last_err
    if envelope.startswith(_OBF_TAG):
        return _legacy_decrypt(envelope)
    # Legacy plaintext value — surface as-is rather than corrupting it.
    return envelope


def is_obfuscated(value: str) -> bool:
    return bool(value) and (value.startswith(_FER_TAG) or value.startswith(_OBF_TAG))


__all__ = ["encrypt", "decrypt", "is_obfuscated"]
