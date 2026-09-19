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


def _base_dir() -> str:
    """``~/.range_chart_analyzer``, created on demand with private perms.

    REVIEW-2026-09-20 (item 21): the directory used to be created with
    ``os.makedirs(..., exist_ok=True)`` and left at the process umask default
    (0777 & ~umask, i.e. usually 0755). Everything this module stores in it is
    key material, so it is now 0700 best-effort. On Windows the mode bits are
    only advisory (the real ACL comes from the profile directory), so this is
    a hardening step there rather than a guarantee.
    """
    base = os.path.join(os.path.expanduser("~"), ".range_chart_analyzer")
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
    except OSError:
        pass
    try:
        os.chmod(base, 0o700)
    except (OSError, AttributeError):
        pass
    return base


def write_private_bytes(path: str, data: bytes, *, exclusive: bool = False) -> None:
    """Write *data* to *path* so it is never briefly readable by others.

    REVIEW-2026-09-20 (item 21): the shared safe-write helper for key
    material. The pattern this replaces (``open(path,'wb')`` then
    ``os.chmod(path, 0o600)``) has two holes: the file exists with the umask
    default (0644) for the whole duration of the write, and a local attacker
    who can pre-create the path as a symlink gets our bytes — so a
    ``providers.json`` rewrite could follow a symlink out of the home
    directory. Here the bytes go to a fresh temp file created with
    ``O_CREAT | O_EXCL`` at mode 0600 (open fails rather than reuse or follow),
    are fsynced, then moved into place with the atomic ``os.replace``.

    ``exclusive=True`` additionally refuses to clobber an EXISTING *path*
    (used for the salt and the key file, where overwriting silently destroys
    every stored API key).

    Raises ``OSError`` (or ``FileExistsError``) on failure; callers must treat
    that as fatal rather than continue with unsaved key material.

    NOTE: ``rca_core/llm.py`` (providers.json writer) still uses the old
    open+chmod pattern and should call this helper — see the review report; it
    is outside this change's allowed file set.
    """
    directory = os.path.dirname(path) or "."
    if directory:
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError:
            pass
    if exclusive and os.path.lexists(path):
        raise FileExistsError(path)
    tmp = f"{path}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with open(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except (OSError, AttributeError):
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _salt_path() -> str:
    return os.path.join(_base_dir(), "secrets_salt")


def _key_path() -> str:
    """Path of the fallback Fernet key file (REVIEW-2026-09-20, item 18)."""
    return os.path.join(_base_dir(), "fernet_key.fek")


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
    #
    # REVIEW-2026-09-20 (item 18): ``uuid.getnode()`` returns a RANDOM 48-bit
    # number whenever it cannot find a real MAC — and per IEEE 802 the
    # least-significant bit of the first octet distinguishes them: 0 =
    # universally administered (a burned-in MAC), 1 = locally administered
    # (i.e. getnode()'s random fallback, also what you get from a spoofed or
    # virtualised NIC, or from MAC randomisation on a laptop). Deriving key
    # material from that value means the "fingerprint" changes on every
    # process start, so anything encrypted with it is unreadable on the next
    # launch. Such a value is REFUSED: the source becomes a constant marker, so
    # the fingerprint is at least stable (and the fallback path is a plain
    # warning away — see _derive_fernet_key, which no longer relies on this at
    # all).
    try:
        import uuid
        node = uuid.getnode()
        mac_bytes = node.to_bytes(6, "big")
        if mac_bytes[0] & 1:
            sources.append(b"mac-unstable")  # locally administered / random
        else:
            sources.append(mac_bytes)
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


def _read_private_bytes(path: str) -> "bytes | None":
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _get_or_create_salt() -> bytes:
    path = _salt_path()
    existing = _read_private_bytes(path)
    if existing is not None:
        if len(existing) >= 16:
            return existing
        # REVIEW-2026-09-10: the file EXISTS but is unusable (truncated by a
        # crash/disk-full, or zero bytes). The old code treated it as absent
        # and OVERWROTE it with a fresh random salt — which silently made
        # every stored key undecryptable, with no warning and no backup.
        # Refuse instead: a loud error the operator can act on (restore the
        # salt / re-enter keys) beats a quiet total loss.
        raise RuntimeError(
            f"secrets_store: the salt file at {path} exists but is corrupt "
            f"({len(existing)} bytes; expected >= 16). Refusing to "
            "overwrite it - overwriting would permanently lose every stored "
            "API key. Restore the file from backup, or delete it AND re-enter "
            "your keys."
        )
    if existing is None and os.path.lexists(path):
        # Present but unreadable (permissions, locked by another process):
        # do NOT create a competing salt.
        raise RuntimeError(
            f"secrets_store: the salt file at {path} exists but could not be "
            "read. Fix its permissions rather than let a new salt overwrite it."
        )
    salt = secrets.token_bytes(32)
    # REVIEW-2026-09-20 (item 19): the create step used ``os.path.exists`` as a
    # check and then a non-exclusive write, so two processes starting at once
    # (GUI + ``server.py``, or two GUI windows) could both pass the check and
    # the later writer clobber the salt the first one already used to encrypt.
    # The file is now created with O_CREAT|O_EXCL through
    # :func:`write_private_bytes`, and losing the race means RE-READING the
    # winner's salt rather than writing a second one.
    try:
        write_private_bytes(path, salt, exclusive=True)
    except FileExistsError:
        raced = _read_private_bytes(path)
        if raced is not None and len(raced) >= 16:
            return raced
        raise RuntimeError(
            f"secrets_store: another process created {path} but it is not "
            "readable/usable yet. Re-run once it settles."
        )
    except OSError as exc:
        # REVIEW-2026-09-20 (item 20): the old fallback derived the salt from
        # the machine fingerprint and RETURNED it without ever writing it, so
        # every later process (and this one, after a restart) generated a
        # different salt from a different readable state and could not decrypt
        # what this process encrypted — silent, unrecoverable key loss.
        # Same loud-failure contract as the corrupt-file branch above.
        raise RuntimeError(
            f"secrets_store: cannot create the salt file at {path}: {exc}. "
            "Continuing would encrypt API keys with a salt that no later "
            "process can read. Free up disk space / fix the HOME permissions "
            "(~/.range_chart_analyzer must be writable by you only)."
        ) from exc
    return salt


def _get_or_create_fernet_key() -> bytes:
    """Random Fernet key stored in ``~/.range_chart_analyzer/fernet_key.fek``.

    REVIEW-2026-09-20 (item 18): this is the no-keyring / no-passphrase
    fallback that used to be ``PBKDF2(machine fingerprint + readable salt)``.
    That construction is not a secret: hostname, home path and MAC are all
    readable to any local account (and ``uuid.getnode()`` even returns a
    per-process RANDOM when it cannot find a real MAC, which made the derived
    key unreproducible from one start to the next). The key is now 32 random
    bytes generated at first start, written 0600 inside a 0700 directory, and
    read back afterwards — so it is only ever as readable as ``providers.json``
    itself, instead of being recomputable from public facts.

    The old derivation is still available as
    :func:`_derive_fernet_key_legacy_fingerprint` so envelopes written before
    this change stay decryptable.
    """
    path = _key_path()
    existing = _read_private_bytes(path)
    if existing is not None:
        key = existing.strip()
        if key:
            return key
        raise RuntimeError(
            f"secrets_store: the key file at {path} exists but is empty. "
            "Restore it from backup or delete it AND re-enter your API keys - "
            "overwriting it silently would make every stored key unreadable."
        )
    if os.path.lexists(path):
        raise RuntimeError(
            f"secrets_store: the key file at {path} exists but could not be "
            "read. Fix its permissions rather than generate a new key."
        )
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    try:
        write_private_bytes(path, key, exclusive=True)
    except FileExistsError:
        raced = _read_private_bytes(path)
        if raced:
            return raced
        raise RuntimeError(
            f"secrets_store: another process created {path} but it is not "
            "readable yet. Re-run once it settles."
        )
    except OSError as exc:
        raise RuntimeError(
            f"secrets_store: cannot create the key file at {path}: {exc}. "
            "Continuing would encrypt API keys with a one-process-only key."
        ) from exc
    return key


def _derive_key() -> bytes:
    """Legacy key derivation (100k iterations) — only for decrypting old obf:v1: envelopes."""
    return _pbkdf2_cached(_machine_fingerprint(), _get_or_create_salt(),
                          _PBKDF2_ITERS_LEGACY)


def _derive_fernet_key_legacy_fingerprint() -> bytes:
    """LEGACY fingerprint-derived Fernet key (PBKDF2, 600k iters).

    Kept ONLY as a decrypt candidate for envelopes written before
    REVIEW-2026-09-20 (item 18), when no keyring and no key file existed.
    It is deliberately NOT used for new encryptions any more: a same-machine
    attacker could recompute it from the locally-readable machine fingerprint +
    salt, i.e. obfuscation rather than protection — and on a host where
    ``uuid.getnode()`` falls back to a random node id the "key" was not even
    stable across starts.
    """
    raw = _pbkdf2_cached(_machine_fingerprint(), _get_or_create_salt(),
                         _PBKDF2_ITERS_FERNET)
    return base64.urlsafe_b64encode(raw)


# Backwards-compatible alias: this name used to mean "the fallback key" and is
# still what it means for DECRYPTION. New encryptions go through
# :func:`_active_fernet_key` -> :func:`_get_or_create_fernet_key`.
_derive_fernet_key = _derive_fernet_key_legacy_fingerprint


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

    REVIEW-2026-09-20: the ``"fingerprint"`` VALUE IS KEPT even though the
    source it names is now the random key file (item 18), because
    ``gui.py`` / ``gui_fluent.py`` branch on that exact string to show their
    "at-rest protection is weak" notice — renaming it would silently drop the
    warning. See the report: a follow-up should return ``"keyfile"`` and have
    both GUIs accept ``("keyfile", "fingerprint", "plaintext")``.
    """
    if not _HAS_FERNET:
        return "plaintext"
    if _HAS_KEYRING:
        return "keyring"
    return "fingerprint"


def _warn_obfuscation_only() -> None:
    """Emit a one-time warning that the current key source is the weakest one.

    REVIEW-2026-09-20 (item 18): the wording used to describe the
    fingerprint-derived key ("recomputable from hostname + home + MAC"), which
    is no longer the fallback — the fallback is now a random 32-byte key in
    ``~/.range_chart_analyzer/fernet_key.fek``. That is real authenticated
    encryption at rest, but it is still NOT keyring-grade: anything running as
    this account can read both the key and the ciphertext.
    """
    global _warned_obfuscation
    if _warned_obfuscation:
        return
    _warned_obfuscation = True
    warnings.warn(
        "secrets_store: OS keyring unavailable; the encryption key is stored "
        "in a file under ~/.range_chart_analyzer (fernet_key.fek, 0600 on a "
        "0700 directory, best-effort). Any process running as you can read it "
        "and decrypt stored API keys. Install the 'keyring' package for "
        "OS-credential-store protection.",
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
    3. Else get-or-create the random key in ``~/.range_chart_analyzer/
       fernet_key.fek`` (0600) — REVIEW-2026-09-20 item 18, replacing the
       fingerprint-derived key that any local process could recompute — and
       warn that it is only as strong as the file permissions.
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
    # Raises RuntimeError when the key file can be neither read nor created
    # (item 20's "loud failure beats silent key loss" contract) — callers such
    # as llm.save_providers surface it to the user.
    return _get_or_create_fernet_key()


def _fernet_decrypt_candidates(passphrase: str | None = None) -> list[bytes]:
    """Keys to try when decrypting, so old envelopes remain readable after the
    keyring and (REVIEW-2026-09-20) key-file adoptions.

    Order: the key that new writes use, then the historical sources:
    active (passphrase / keyring / key file) -> legacy fingerprint key.
    """
    if passphrase:
        return [_fernet_key_from_passphrase(passphrase)]
    keys: list[bytes] = []
    try:
        keys.append(_active_fernet_key())
    except Exception:
        # e.g. the key file cannot be read/created: still try the older
        # sources so an existing installation can decrypt.
        pass
    # Legacy fingerprint-derived key: decrypt envelopes written before keyring
    # and before the key-file fallback existed.
    try:
        legacy = _derive_fernet_key_legacy_fingerprint()
        if legacy not in keys:
            keys.append(legacy)
    except Exception:
        pass
    return keys


def _providers_path() -> str:
    return os.path.join(_base_dir(), "providers.json")


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


__all__ = ["encrypt", "decrypt", "is_obfuscated", "write_private_bytes",
           "encryption_status"]
