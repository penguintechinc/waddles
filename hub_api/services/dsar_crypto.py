"""Field encryption + keyed hashing for DSAR requester contact data (PII at rest).

Encryption goes through the repo's ONE versioned sealing vault
(`services.provider_connections.vault.seal_secret` / `open_secret`, AES-256-GCM under
`CREDENTIAL_ENCRYPTION_KEY`), never a private copy of the primitive (`tests/test_seal_callers_guard`
ratchets that). The request's `public_id` is bound in as GCM associated data, so a ciphertext copied
onto another row fails authentication. A missing/malformed key RAISES -- no plaintext fallback.

`email_fingerprint` is a keyed BLAKE2b digest of the normalised email (key `DSAR_FINGERPRINT_KEY`,
64 hex chars) used ONLY as an opaque rate-limit / duplicate bucket key, so the raw address never
reaches Valkey keys, logs or metric labels. `hash_token` is the plain SHA-256 stored for the
single-use verification token (the token itself is 256-bit random, so no salt is needed).
"""

from __future__ import annotations

import hashlib
import os

from services.platform_integrations_crypto import PlatformCredentialCryptoError
from services.provider_connections import vault

FINGERPRINT_KEY_ENV = "DSAR_FINGERPRINT_KEY"
_KEY_HEX_LENGTH = 64

#: Re-exported so callers catch one error family for "the field could not be sealed/opened".
CryptoError = PlatformCredentialCryptoError


class EncryptionKeyError(ValueError):
    """`DSAR_FINGERPRINT_KEY` is missing or not a 64-character hex string."""


def _fingerprint_key() -> bytes:
    """Return the 32-byte fingerprint key from the environment, or raise `EncryptionKeyError`."""
    hex_key = os.environ.get(FINGERPRINT_KEY_ENV, "")
    if len(hex_key) != _KEY_HEX_LENGTH:
        raise EncryptionKeyError(f"{FINGERPRINT_KEY_ENV} must be a 64-character hex string")
    try:
        return bytes.fromhex(hex_key)
    except ValueError as exc:
        raise EncryptionKeyError(f"{FINGERPRINT_KEY_ENV} must be hex") from exc


def is_configured() -> bool:
    """True when BOTH the sealing key and the fingerprint key are usable (fail closed otherwise)."""
    try:
        _fingerprint_key()
        vault.seal_secret("probe", aad=b"dsar-probe")
    except (EncryptionKeyError, CryptoError):
        return False
    return True


def encrypt(plaintext: str, *, row_id: str) -> str:
    """Seal `plaintext` bound to `row_id`; returns the vault's opaque base64 ciphertext."""
    return vault.seal_secret(plaintext, aad=row_id.encode("ascii"))


def decrypt(ciphertext: str, *, row_id: str) -> str:
    """Open a value produced by `encrypt()` for the same `row_id` (raises `CryptoError`)."""
    return vault.open_secret(ciphertext, aad=row_id.encode("ascii"), seal_version=vault.SEAL_AAD)


def normalize_email(email: str) -> str:
    """Canonical form used for fingerprinting: stripped + lower-cased."""
    return email.strip().lower()


def email_fingerprint(email: str) -> bytes:
    """Keyed digest of the normalised email (opaque bucket key, not reversible without the key)."""
    return hashlib.blake2b(
        normalize_email(email).encode("utf-8"),
        key=_fingerprint_key(),
        digest_size=32,
        person=b"dsar-email-v1",
    ).digest()


def hash_token(token: str) -> bytes:
    """SHA-256 of a verification token -- the only form stored at rest."""
    return hashlib.sha256(token.encode("ascii", "ignore")).digest()
