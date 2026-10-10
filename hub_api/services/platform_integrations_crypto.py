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

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask_core.safe_logging import log_exc_safe

logger = logging.getLogger(__name__)

_IV_LENGTH = 12


class PlatformCredentialCryptoError(Exception):
    """Raised when `CREDENTIAL_ENCRYPTION_KEY` is missing/malformed, or decryption fails."""


def _encryption_key() -> bytes:
    raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not raw:
        raise PlatformCredentialCryptoError(
            "CREDENTIAL_ENCRYPTION_KEY environment variable is not set"
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise PlatformCredentialCryptoError(
            "CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)"
        ) from exc
    if len(key) != 32:
        raise PlatformCredentialCryptoError(
            "CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)"
        )
    return key


def encrypt_token(plaintext: str) -> str:
    """AES-256-GCM encrypt `plaintext`; returns base64(iv(12) || ciphertext || tag).

    Mirrors `token_crypto.encrypt_value()`'s wire format exactly (same
    primitive, same layout) so a value written here decrypts cleanly via
    either service's `decrypt_value()`, and vice versa -- see module
    docstring. Callers must never log `plaintext` or the returned
    ciphertext's decrypted form.
    """
    key = _encryption_key()
    iv = os.urandom(_IV_LENGTH)
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv + ciphertext_and_tag).decode("ascii")


def decrypt_value(encrypted_b64: str) -> str:
    """Decrypt a value produced by `token_crypto.encrypt_value()`."""
    key = _encryption_key()
    try:
        raw = base64.b64decode(encrypted_b64, validate=True)
        iv, ciphertext_and_tag = raw[:_IV_LENGTH], raw[_IV_LENGTH:]
        plaintext = AESGCM(key).decrypt(iv, ciphertext_and_tag, None)
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
