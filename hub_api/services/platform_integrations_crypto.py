"""Decrypt port + write path of `credential_manager_module/services/token_crypto.py`.

SECURITY (HIGH, see that module's own docstring for the full rationale):
`platform_config_service.py::test_platform_connection()` is the one other
real reader of `platform_integrations.access_token` in this repo (every
other consumer either reads its own module-local OAuth table or never
touches the raw value) -- once `credential_manager_module`'s refresh
service starts encrypting this column, this function needs to decrypt
before sending the value to the platform's own validation API, or every
"test credential" call on a since-refreshed row would send ciphertext and
report a false `invalid token`.

Originally decrypt-only (`refresh_service.py` owned all writes to
`bot`/`user_oauth` rows). gh-320 (per-community OAuth "Connections")
adds `encrypt_token()`: `services/community_connections.py` is now a
second, hub_api-local writer -- but only ever for
`integration_type='community_oauth'` rows, a lane `refresh_service.py`
never touches, so there's no write-write overlap between the two owners.

Re-implements the same AES-256-GCM primitive/wire format rather than
importing across services (`credential_manager_module` is a separate
deployable/DB-grant -- backend-database.md Per-Service Database Accounts
-- same reasoning `core/svc_streaming/services/community_access.py`'s own
docstring documents for authz code). `CREDENTIAL_ENCRYPTION_KEY` MUST be
set identically in both services' deployments.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask_core.safe_logging import log_exc_safe

logger = logging.getLogger(__name__)

_IV_LENGTH = 12


class PlatformCredentialCryptoError(Exception):
    """Raised when `CREDENTIAL_ENCRYPTION_KEY` is missing/malformed, or decryption fails."""


class PlatformCredentialKeyError(PlatformCredentialCryptoError):
    """The key itself is unusable (unset or malformed) -- as opposed to a ciphertext that failed.

    A subclass, so every existing `except PlatformCredentialCryptoError` still catches it. Lets a
    caller tell "operator must fix configuration" (503) from "this row was tampered with"
    (`Failed to decrypt`) without parsing exception text.
    """


def _encryption_key() -> bytes:
    raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not raw:
        raise PlatformCredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY environment variable is not set"
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise PlatformCredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)"
        ) from exc
    if len(key) != 32:
        raise PlatformCredentialKeyError(
            "CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)"
        )
    return key


#: HKDF `info` label separating connection-secret sealing from every no-AAD path (tenant app
#: credentials, Redis install state, credential_manager rows), which use the raw key.
_CONNECTION_KEY_INFO = b"waddles/connection-secret/aad-v1"


def _row_bound_key(key: bytes) -> bytes:
    """Derive the key used for AAD-bound (connection-secret) ciphertexts from the platform key.

    Domain separation (M4): a no-AAD ciphertext from another path -- sealed under the raw key --
    can never authenticate as a connection token, because connection tokens use this derived key.
    The wire format is unchanged; only the key differs.
    """
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_CONNECTION_KEY_INFO).derive(
        key
    )


def connection_aad(*, community_id: int, provider: str) -> bytes:
    """AAD binding a `platform_integrations` community_oauth ciphertext to its row (M4).

    `community_id:provider` is the row's identity (one active row per pair), so a ciphertext
    copied or swapped to another row no longer authenticates.
    """
    return f"{community_id}:{provider}".encode()


def platform_connection_aad(*, public_id: UUID, platform: str) -> bytes:
    """AAD binding a `platform_connections` ciphertext to its row (M4).

    Those rows are tenant-owned and shared across communities via grants, so `community_id` is
    not a stable identity there; the immutable `public_id` is.
    """
    return f"conn:{public_id}:{platform}".encode()


def encrypt_token(plaintext: str, *, aad: bytes | None = None) -> str:
    """AES-256-GCM encrypt `plaintext`; returns base64(iv(12) || ciphertext || tag).

    Mirrors `token_crypto.encrypt_value()`'s wire format exactly (same
    primitive, same layout) so a value written here decrypts cleanly via
    either service's `decrypt_value()`, and vice versa -- see module
    docstring. `aad` (when given) selects the row-bound key (domain-separated from the raw key)
    and is authenticated but not stored: the same bytes must be supplied to decrypt.
    Callers must never log `plaintext` or the returned
    ciphertext's decrypted form.
    """
    key = _encryption_key()
    if aad is not None:
        key = _row_bound_key(key)
    iv = os.urandom(_IV_LENGTH)
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), aad)
    return base64.b64encode(iv + ciphertext_and_tag).decode("ascii")


def decrypt_value(encrypted_b64: str, *, aad: bytes | None = None) -> str:
    """Decrypt a value produced by `encrypt_token()` / `token_crypto.encrypt_value()`.

    `aad` must equal the bytes the value was sealed with (`None` for legacy no-AAD values); any
    mismatch fails authentication and raises `PlatformCredentialCryptoError`.
    """
    key = _encryption_key()
    if aad is not None:
        key = _row_bound_key(key)
    try:
        raw = base64.b64decode(encrypted_b64, validate=True)
        iv, ciphertext_and_tag = raw[:_IV_LENGTH], raw[_IV_LENGTH:]
        plaintext = AESGCM(key).decrypt(iv, ciphertext_and_tag, aad)
    except (InvalidTag, ValueError, binascii.Error) as exc:
        raise PlatformCredentialCryptoError("Failed to decrypt credential value") from exc
    return plaintext.decode("utf-8")


def decrypt_if_needed(value: str | None, *, is_encrypted: bool) -> str | None:
    """Decrypt `value` if `is_encrypted` is true; otherwise return it unchanged.

    A decrypt failure is logged and the raw value returned rather than
    raised -- callers (`test_platform_connection`) send the result
    straight to a platform's validation API, which will simply reject a
    still-ciphertext/corrupt token with its own auth error rather than
    this service crashing the whole request.
    """
    if not value or not is_encrypted:
        return value
    try:
        return decrypt_value(value)
    except PlatformCredentialCryptoError as exc:
        # Redacted: only type/category (e.g. crypto_auth_failed) -- never the message or
        # chained cause text, which on a crypto failure can echo key/ciphertext fragments.
        log_exc_safe(
            logger, logging.ERROR, "Failed to decrypt platform_integrations credential", exc
        )
        return value
