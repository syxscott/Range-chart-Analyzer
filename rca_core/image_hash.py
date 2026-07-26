"""P0-completion (REVIEW-2026-07-27): SHA-256 image fingerprinting.

Even when source_path is missing (clipboard paste, canvas extraction,
re-uploaded image), the bytes-level fingerprint must be stored so a
5-year-from-now audit can verify "this record really came from that
image". image_sha256 is now a mandatory column in the history table
(see rca_core/db.py) and a first-class field on HistoryRecord and
ExtractResult.
"""
from __future__ import annotations

import base64
import hashlib


def compute_image_sha256(image_bytes: bytes) -> str:
    """Return hex SHA-256 of the image bytes.

    Empty input returns the SHA-256 of an empty string
    (``e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855``)
    so the field is never empty for non-clipboard sources.
    """
    return hashlib.sha256(image_bytes).hexdigest()


def compute_image_sha256_from_b64(image_b64: str) -> str:
    """Decode a base64 image (with optional ``data:`` URL prefix) and hash.

    Handles:
    - Bare base64: ``iVBORw0KGgoAAAANSUhEUg...``
    - Data URL: ``data:image/png;base64,iVBORw0KGgo...``
    - Empty string → SHA-256 of empty bytes (deterministic sentinel).

    Never raises: on decode failure we hash the raw text so a downstream
    audit still sees a fingerprint (just not a useful one). The caller can
    decide to retry or fall back.
    """
    if not image_b64:
        return compute_image_sha256(b"")
    s = image_b64
    if s.startswith("data:"):
        if "," in s:
            s = s.split(",", 1)[1]
    try:
        return compute_image_sha256(base64.b64decode(s, validate=False))
    except Exception:
        return compute_image_sha256(s.encode("utf-8", errors="replace"))


__all__ = [
    "compute_image_sha256",
    "compute_image_sha256_from_b64",
]