"""AES-256-GCM helpers for ingest-source webhook secrets at rest.

Same wire format as `services/bot_crypto.py` (12-byte IV, GCM tag
appended to ciphertext) but keyed by its own env var,
`BUNDLE_SECRET_ENCRYPTION_KEY` -- a deliberately separate key from
`RCON_ENCRYPTION_KEY` (different security domain: RCON server
credentials vs. per-tenant webhook HMAC secrets), never shared.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH = 12
_KEY_HEX_LENGTH = 64


class EncryptionKeyError(ValueError):
    """`BUNDLE_SECRET_ENCRYPTION_KEY` is missing or not a 64-character hex string."""


def _get_key() -> bytes:
    hex_key = os.environ.get("BUNDLE_SECRET_ENCRYPTION_KEY", "")
    if len(hex_key) != _KEY_HEX_LENGTH:
        raise EncryptionKeyError("BUNDLE_SECRET_ENCRYPTION_KEY must be a 64-character hex string")
    return bytes.fromhex(hex_key)


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt `plaintext`; returns `(ciphertext_with_appended_tag, iv)`."""
    key = _get_key()
    iv = os.urandom(_IV_LENGTH)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return ciphertext, iv


def decrypt(ciphertext: bytes, iv: bytes) -> str:
    """Decrypt `ciphertext` (GCM tag appended) encrypted with `encrypt()`."""
    key = _get_key()
    plaintext = AESGCM(key).decrypt(iv, bytes(ciphertext), None)
    return plaintext.decode("utf-8")
