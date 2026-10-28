"""AES-256-GCM helpers for one-time secret message bodies at rest (feature #684).

Own key (`ONE_TIME_SECRET_ENCRYPTION_KEY`, 64 hex chars) -- a separate
security domain from `BUNDLE_SECRET_ENCRYPTION_KEY`/`RCON_ENCRYPTION_KEY`,
never shared. The row id is bound in as GCM associated data so a
ciphertext copied onto another row fails authentication. A missing or
malformed key raises -- there is no plaintext fallback.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH = 12
_KEY_HEX_LENGTH = 64


class EncryptionKeyError(ValueError):
    """`ONE_TIME_SECRET_ENCRYPTION_KEY` is missing or not a 64-character hex string."""


def _get_key() -> bytes:
    hex_key = os.environ.get("ONE_TIME_SECRET_ENCRYPTION_KEY", "")
    if len(hex_key) != _KEY_HEX_LENGTH:
        raise EncryptionKeyError("ONE_TIME_SECRET_ENCRYPTION_KEY must be a 64-character hex string")
    try:
        return bytes.fromhex(hex_key)
    except ValueError as exc:
        raise EncryptionKeyError("ONE_TIME_SECRET_ENCRYPTION_KEY must be hex") from exc


def encrypt(plaintext: str, *, row_id: str) -> tuple[bytes, bytes]:
    """Encrypt `plaintext` bound to `row_id`; returns `(ciphertext_with_tag, iv)`."""
    key = _get_key()
    iv = os.urandom(_IV_LENGTH)
    return AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), row_id.encode("ascii")), iv


def decrypt(ciphertext: bytes, iv: bytes, *, row_id: str) -> str:
    """Decrypt a value produced by `encrypt()` for the same `row_id`."""
    key = _get_key()
    plain = AESGCM(key).decrypt(bytes(iv), bytes(ciphertext), row_id.encode("ascii"))
    return plain.decode("utf-8")
