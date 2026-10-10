"""Key handling for SSO: secret-at-rest encryption and the login-CSRF binder.

One 256-bit master key, `SSO_ENCRYPTION_KEY` (64 lowercase hex chars), is
auto-provisioned by the Helm chart (`templates/sso.yaml`: generated in
alpha/local, pre-created or ExternalSecret elsewhere -- same policy as the
other `autoProvisionedKeys`). Two independent subkeys are derived from it with
HKDF-SHA256 so a bug in one use can never leak the other:

* `enc`  -- AES-256-GCM for `sso_connections.secret_ciphertext` (the OIDC /
  Google `client_secret`). The connection's `public_id` is bound as AAD, so a
  ciphertext lifted from one row cannot be replayed into another.
* `bind` -- HMAC-SHA256 producing the browser-binding cookie value for a login
  flow's `state` token. A flow started in one browser cannot be completed in
  another (login CSRF / session fixation): the callback requires a cookie only
  the initiating browser holds.

The key is read lazily and every failure is loud: a missing or malformed key
raises `SsoConfigError`; there is no fallback key and no plaintext mode.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
from hashlib import sha256
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from services.sso_settings import ENV_ENCRYPTION_KEY
from services.sso_types import SsoConfigError

_IV_LENGTH: Final = 12
_VERSION_PREFIX: Final = "v1:"
_INFO_ENC: Final = b"waddles-sso/v1/secret-encryption"
_INFO_BIND: Final = b"waddles-sso/v1/browser-binder"


def _master_key() -> bytes:
    raw = os.environ.get(ENV_ENCRYPTION_KEY)
    if not raw:
        raise SsoConfigError(
            "sso_key_missing",
            f"{ENV_ENCRYPTION_KEY} is not set -- SSO cannot store secrets or bind logins",
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise SsoConfigError(
            "sso_key_malformed", f"{ENV_ENCRYPTION_KEY} must be 64 lowercase hex characters"
        ) from exc
    if len(key) != 32:
        raise SsoConfigError(
            "sso_key_malformed", f"{ENV_ENCRYPTION_KEY} must be 64 lowercase hex characters"
        )
    return key


def _subkey(info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(_master_key())


def encrypt_secret(plaintext: str, *, aad: str) -> str:
    """AES-256-GCM encrypt `plaintext`, binding `aad`; returns `v1:` + base64(iv || ct || tag)."""
    iv = os.urandom(_IV_LENGTH)
    ciphertext = AESGCM(_subkey(_INFO_ENC)).encrypt(iv, plaintext.encode("utf-8"), aad.encode())
    return _VERSION_PREFIX + base64.b64encode(iv + ciphertext).decode("ascii")


def decrypt_secret(token: str, *, aad: str) -> str:
    """Decrypt a value produced by `encrypt_secret` under the same `aad`; raise on any mismatch."""
    if not token.startswith(_VERSION_PREFIX):
        raise SsoConfigError("secret_format", "stored SSO secret has an unknown format")
    try:
        blob = base64.b64decode(token[len(_VERSION_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SsoConfigError("secret_format", "stored SSO secret is not valid base64") from exc
    if len(blob) <= _IV_LENGTH:
        raise SsoConfigError("secret_format", "stored SSO secret is truncated")
    iv, ciphertext = blob[:_IV_LENGTH], blob[_IV_LENGTH:]
    try:
        return AESGCM(_subkey(_INFO_ENC)).decrypt(iv, ciphertext, aad.encode()).decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as exc:
        raise SsoConfigError(
            "secret_decrypt",
            "stored SSO secret failed authentication (wrong key or tampered row)",
        ) from exc


def binder_value(state: str) -> str:
    """Return the browser-binding cookie value for `state` (URL-safe base64 HMAC-SHA256)."""
    mac = hmac.new(_subkey(_INFO_BIND), b"bind|" + state.encode("utf-8"), sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def verify_binder(state: str, presented: str | None) -> bool:
    """Constant-time check that `presented` is the binder cookie for `state`."""
    if not presented:
        return False
    return hmac.compare_digest(binder_value(state), presented)
