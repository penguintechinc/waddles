"""AES-256-GCM helpers for per-tenant Discord app credentials at rest (#500/#501 follow-on).

Same wire format as `services/bot_crypto.py`/`services/bundle_secret_crypto.py`
(12-byte IV, GCM tag appended to ciphertext) but keyed by its own env var,
`TENANT_DISCORD_CREDENTIAL_ENCRYPTION_KEY` -- a deliberately separate key
from every other domain (RCON creds, webhook HMAC secrets): a tenant's
Discord application client secret and bot token are the highest-blast-
-radius secret this schema holds (control of a customer's own Discord
bot), so they get their own key rather than sharing one already used
elsewhere.

**Migration seam to PR #442 (per-tenant DEK broker), documented per the
schema contract (`docs/superpowers/specs/2026-09-29-guild-binding-contract.md`
Sec2):** `tenant_platform_credentials.key_ref` records which scheme
encrypted a given row. Today every row is written with
`key_ref=STATIC_ENV_KEY_REF` (this module's single static env-var key,
matching migration 0038's own documented placeholder). Once PR #442
merges and exposes a tenant DEK broker, a follow-up migration should:
  1. add a `decrypt_static_env()`/`encrypt_with_tenant_dek()` pair here
     (or a sibling module) using the broker's per-tenant key lookup,
  2. re-encrypt every row whose `key_ref == STATIC_ENV_KEY_REF` under its
     owning tenant's DEK, updating `key_ref` to the broker's key id,
  3. flip `encrypt()`'s default to the broker path once no
     `STATIC_ENV_KEY_REF` rows remain.
This module intentionally exposes `key_ref` alongside `encrypt()`'s
output so callers persist it per-row from day one, making that follow-up
a data migration only -- no schema or call-site change required.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH = 12
_KEY_HEX_LENGTH = 64

#: Placeholder `key_ref` value for the interim static-env-key scheme --
#: see module docstring's PR #442 migration seam.
STATIC_ENV_KEY_REF = "static-env:TENANT_DISCORD_CREDENTIAL_ENCRYPTION_KEY"


class EncryptionKeyError(ValueError):
    """`TENANT_DISCORD_CREDENTIAL_ENCRYPTION_KEY` is missing or not a 64-char hex string."""


def _get_key() -> bytes:
    hex_key = os.environ.get("TENANT_DISCORD_CREDENTIAL_ENCRYPTION_KEY", "")
    if len(hex_key) != _KEY_HEX_LENGTH:
        raise EncryptionKeyError(
            "TENANT_DISCORD_CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string"
        )
    return bytes.fromhex(hex_key)


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt `plaintext`; returns `(ciphertext_with_appended_tag, iv)`.

    Pair with `STATIC_ENV_KEY_REF` when persisting -- see module docstring.
    """
    key = _get_key()
    iv = os.urandom(_IV_LENGTH)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return ciphertext, iv


def decrypt(ciphertext: bytes, iv: bytes, *, key_ref: str) -> str:
    """Decrypt `ciphertext` (GCM tag appended) encrypted with `encrypt()`.

    Raises `EncryptionKeyError` if `key_ref` names a scheme this module
    doesn't (yet) implement -- fail closed rather than silently decrypt
    with the wrong key once PR #442's broker scheme exists alongside this
    one.
    """
    if key_ref != STATIC_ENV_KEY_REF:
        raise EncryptionKeyError(f"unsupported key_ref {key_ref!r}")
    key = _get_key()
    plaintext = AESGCM(key).decrypt(iv, bytes(ciphertext), None)
    return plaintext.decode("utf-8")


def mask_secret(plaintext_hint_source: str) -> str:
    """Return a display-only mask (`...1234`) for a GET response -- never the real value."""
    tail = plaintext_hint_source[-4:] if len(plaintext_hint_source) >= 4 else plaintext_hint_source
    return f"...{tail}"
