"""AES-256-GCM helpers for vendor webhook secrets.

Ports `vendorSubmissionController.js`'s `encryptWebhookSecret`/
`decryptWebhookSecret`, fixed rather than faithfully reproduced. Node's
originals used `crypto.createCipheriv('aes-256-cbc', Buffer.from(process.env.
ENCRYPTION_KEY || 'default-key'), iv)` -- AES-256 requires an exact 32-byte key,
but `Buffer.from(str)` on a short/arbitrary-length string silently produces a
key of the WRONG length (Node either throws or, depending on version, truncates/
pads unpredictably), and the `'default-key'` fallback is a hardcoded credential
(security.md: "Never hardcoded credentials or configuration"). This is a case
where faithful porting would ship a real vulnerability -- ported using this
repo's own established pattern instead (`services/bot_crypto.py`: AESGCM,
random 12-byte IV per encryption, 64-hex-char key from an environment
variable, IV-then-ciphertext single hex string so it fits the existing
`webhook_secret` text column with no schema change). No hardcoded fallback
key: `MarketplaceEncryptionKeyError` fails closed if the env var is absent.
"""

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IV_LENGTH = 12
_KEY_HEX_LENGTH = 64


class MarketplaceEncryptionKeyError(ValueError):
    """`MARKETPLACE_ENCRYPTION_KEY` is missing or not a 64-character hex string."""


def _get_key() -> bytes:
    hex_key = os.environ.get("MARKETPLACE_ENCRYPTION_KEY", "")
    if len(hex_key) != _KEY_HEX_LENGTH:
        raise MarketplaceEncryptionKeyError(
            "MARKETPLACE_ENCRYPTION_KEY must be a 64-character hex string"
        )
    return bytes.fromhex(hex_key)


def encrypt_webhook_secret(plaintext: str) -> str:
    """Encrypt a vendor-submitted webhook secret for storage.

    Returns a single hex string (`iv || ciphertext_with_tag`) that fits the
    existing `webhook_secret` text column unchanged.
    """
    key = _get_key()
    iv = os.urandom(_IV_LENGTH)
    ciphertext = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return (iv + ciphertext).hex()


def decrypt_webhook_secret(stored: str) -> str:
    """Decrypt a `webhook_secret` value written by `encrypt_webhook_secret`."""
    key = _get_key()
    raw = bytes.fromhex(stored)
    iv, ciphertext = raw[:_IV_LENGTH], raw[_IV_LENGTH:]
    return AESGCM(key).decrypt(iv, ciphertext, None).decode("utf-8")


#: AAD binds a payment-details ciphertext to its column (no replay into another field).
PAYMENT_DETAILS_AAD = b"vendor_submissions.payment_details"
_PAYMENT_ENVELOPE_VERSION = 1


def is_encrypted_payment_details(value: Any) -> bool:
    """True when `value` is the `{"v": 1, "enc": <hex>}` envelope this module writes."""
    return (
        isinstance(value, dict)
        and value.get("v") == _PAYMENT_ENVELOPE_VERSION
        and isinstance(value.get("enc"), str)
        and set(value) == {"v", "enc"}
    )


def encrypt_payment_details(details: dict[str, Any]) -> dict[str, Any]:
    """Field-encrypt a vendor `payment_details` dict (paypal_email, bank_account, ...).

    AES-256-GCM, random IV, column-bound AAD. Returns a small JSON envelope so the value still
    fits the existing JSONB column with no schema change; no financial key or value stays in clear.
    """
    key = _get_key()
    iv = os.urandom(_IV_LENGTH)
    plaintext = json.dumps(details, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, PAYMENT_DETAILS_AAD)
    return {"v": _PAYMENT_ENVELOPE_VERSION, "enc": (iv + ciphertext).hex()}


def decrypt_payment_details(stored: Any) -> dict[str, Any]:
    """Decrypt a stored envelope; `None`/empty (erased) yields `{}`. Raises on tamper/wrong key."""
    if stored is None or stored == {}:
        return {}
    if not is_encrypted_payment_details(stored):
        raise ValueError("payment_details is not an encrypted envelope (legacy plaintext row?)")
    raw = bytes.fromhex(stored["enc"])
    iv, ciphertext = raw[:_IV_LENGTH], raw[_IV_LENGTH:]
    decoded = json.loads(AESGCM(_get_key()).decrypt(iv, ciphertext, PAYMENT_DETAILS_AAD))
    if not isinstance(decoded, dict):
        raise ValueError("decrypted payment_details is not an object")
    return decoded
