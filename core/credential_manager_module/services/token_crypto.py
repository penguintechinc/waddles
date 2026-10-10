"""AES-256-GCM at-rest encryption for `platform_integrations` OAuth credentials.

SECURITY (HIGH): `refresh_service.py` previously read and re-wrote
`access_token`/`refresh_token` (and read `client_secret`) as plaintext --
a DB compromise (backup theft, replica misconfiguration, insider access)
directly exposes every connected platform account's live OAuth
credentials, no additional step required.

Same primitive and wire format as `hub_api/services/ai_routing/
byok_crypto.py` and `hub_api/services/github_sync_service.py`'s
`encrypt_token`/`decrypt_token` (`cryptography.hazmat.primitives.ciphers.
aead.AESGCM`, this module's own pinned `cryptography` dependency) -- the
"reuse the repo's existing crypto util" primitive this fix was asked to
reuse. Re-keyed under its own env var (`CREDENTIAL_ENCRYPTION_KEY`), never
shared with the BYOK or GitHub-sync keys, so a compromise of one never
compromises the others. One string column (base64 of `iv(12) ||
ciphertext || tag`), matching `byok_crypto.py`'s format exactly.

`credential_manager_module` is a separate deployable/DB-grant from
`hub_api` (backend-database.md Per-Service Database Accounts), so
`hub_api`'s crypto modules can't be imported directly -- this is a
faithful, minimal port of the same primitive, the same pattern
`core/svc_streaming/services/community_access.py`'s own docstring
documents for authz code. `hub_api/services/platform_config_service.py::
test_platform_connection()` -- the one other real reader of this table's
`access_token` -- carries its own matching decrypt-only port so both
services can read what either one writes; `CREDENTIAL_ENCRYPTION_KEY`
MUST be set identically in both services' deployments (same pattern as
the JWT `SECRET_KEY` already shared across every service in this repo).

Backward compatible with pre-existing plaintext rows: `decrypt_if_needed()`
only attempts decryption when the row's `is_encrypted` flag is true;
legacy plaintext rows pass through unchanged until their next refresh
cycle re-writes (and encrypts) them.
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
_KEY_HEX_LENGTH = 64


class TokenCryptoError(Exception):
    """Raised when `CREDENTIAL_ENCRYPTION_KEY` is missing/malformed, or decryption fails."""


def _encryption_key() -> bytes:
    """Return the 32-byte AES-256 key from `CREDENTIAL_ENCRYPTION_KEY` (64 hex chars)."""
    raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
    if not raw:
        raise TokenCryptoError("CREDENTIAL_ENCRYPTION_KEY environment variable is not set")
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise TokenCryptoError(
            "CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)"
        ) from exc
    if len(key) != 32:
        raise TokenCryptoError(
            "CREDENTIAL_ENCRYPTION_KEY must be a 64-character hex string (32 bytes)"
        )
    return key


def encrypt_value(plaintext: str) -> str:
    """AES-256-GCM encrypt `plaintext`; returns base64(iv + ciphertext + tag).

    Callers must never log the plaintext argument or the returned
    ciphertext's decrypted form.
    """
    key = _encryption_key()
    iv = os.urandom(_IV_LENGTH)
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(iv + ciphertext_and_tag).decode("ascii")


def decrypt_value(encrypted_b64: str) -> str:
    """Decrypt a value from `encrypt_value()`. Raises `TokenCryptoError` on tamper/corruption."""
    key = _encryption_key()
    try:
        raw = base64.b64decode(encrypted_b64, validate=True)
        iv, ciphertext_and_tag = raw[:_IV_LENGTH], raw[_IV_LENGTH:]
        plaintext = AESGCM(key).decrypt(iv, ciphertext_and_tag, None)
    except (InvalidTag, ValueError, binascii.Error) as exc:
        raise TokenCryptoError("Failed to decrypt credential value") from exc
    return plaintext.decode("utf-8")


def decrypt_if_needed(value: str | None, *, is_encrypted: bool) -> str | None:
    """Decrypt `value` if `is_encrypted` is true; otherwise return it unchanged.

    Backward-compatibility boundary for rows written before this fix
    (`is_encrypted` false/NULL) -- they pass through as plaintext until
    their next refresh cycle re-encrypts them. A decrypt failure on a row
    that claims to be encrypted is logged and the raw value is returned
    rather than raised -- a corrupt/un-decryptable credential should fail
    the downstream OAuth call (which will reject a still-ciphertext
    token) with a clear provider-side auth error, not crash the whole
    refresh cycle for every other integration in the same batch.
    """
    if not value or not is_encrypted:
        return value
    try:
        return decrypt_value(value)
    except TokenCryptoError as exc:
        # Redacted: only type/category (e.g. crypto_auth_failed) -- never the message or
        # chained cause text, which on a crypto failure can echo key/ciphertext fragments.
        log_exc_safe(logger, logging.ERROR, "Failed to decrypt credential value", exc)
        return value
