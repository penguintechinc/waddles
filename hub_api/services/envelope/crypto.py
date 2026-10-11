"""Pure cryptographic primitives for per-tenant envelope encryption.

Stateless, I/O-free, and dependency-light (`cryptography` only), so the
security-critical byte handling is unit-testable in isolation:

- **Data subkey** -- HKDF-SHA256 over a tenant's DEK (design Sec Key
  hierarchy), domain-separated per tenant. The cache holds this subkey,
  never the raw DEK.
- **Field AEAD** -- AES-256-GCM, 96-bit random nonce, AAD
  ``"{tenant_id}|{table}|{column}|{row_uuid}|{dek_version}"`` (design
  Sec2). The AAD makes "per-tenant" a cryptographic property: a ciphertext
  copied into another tenant's / row's / column's slot fails to open.
- **Local KEK wrap** -- AES-256-GCM wrap of a DEK under the platform
  baseline KEK, with the wrap *context* bound as AAD (the same binding a
  cloud KMS applies through its encryption context), and a key-id header
  so the platform KEK can be rotated by re-wrapping.

Nothing here logs, prints, or raises with key bytes in the message.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from services.envelope.errors import EnvelopeInputError, EnvelopeIntegrityError
from services.envelope.models import IV_LENGTH

#: AES-256 key length, bytes (DEK, data subkey, KEK).
KEY_LENGTH = 32

#: Wrap-context keys. Customers can pin IAM/key-policy conditions on these
#: (e.g. ``kms:EncryptionContext:waddles_tenant_id``), so they are a stable
#: public contract -- never rename.
CONTEXT_TENANT_ID = "waddles_tenant_id"
CONTEXT_PURPOSE = "waddles_purpose"
PURPOSE_TENANT_DEK = "tenant-dek"
PURPOSE_VERIFY = "verify"

_HKDF_SALT = b"waddles-envelope-v1"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_LOCAL_WRAP_VERSION = b"\x01"
_KEK_ID_LENGTH = 8


def validate_tenant_id(tenant_id: int) -> int:
    """Return `tenant_id` if it is a positive int, else raise :class:`EnvelopeInputError`."""
    if isinstance(tenant_id, bool) or not isinstance(tenant_id, int) or tenant_id < 1:
        raise EnvelopeInputError("tenant_id must be a positive integer")
    return tenant_id


def wrap_context(tenant_id: int, *, purpose: str = PURPOSE_TENANT_DEK) -> dict[str, str]:
    """Build the KEK-wrap context binding a wrapped DEK to its tenant and purpose.

    Passed as the KMS ``EncryptionContext`` (AWS) / AAD (local wrap), so a
    wrapped DEK copied to another tenant's row fails to unwrap.
    """
    return {CONTEXT_PURPOSE: purpose, CONTEXT_TENANT_ID: str(validate_tenant_id(tenant_id))}


def context_aad(context: Mapping[str, str]) -> bytes:
    """Canonical, order-independent AAD bytes for a wrap context."""
    return json.dumps(dict(context), sort_keys=True, separators=(",", ":")).encode("utf-8")


def derive_data_subkey(dek: bytes, tenant_id: int) -> bytes:
    """Derive the AES-256-GCM data subkey from a tenant DEK (HKDF-SHA256, per-tenant info)."""
    if len(dek) != KEY_LENGTH:
        raise EnvelopeInputError("DEK must be exactly 32 bytes")
    info = f"waddles-envelope-data-v1|tenant:{validate_tenant_id(tenant_id)}".encode()
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LENGTH, salt=_HKDF_SALT, info=info).derive(
        dek
    )


def field_aad(
    tenant_id: int, table: str, column: str, row_uuid: str | uuid.UUID, dek_version: int
) -> bytes:
    """Build the field-encryption AAD ``tenant|table|column|row_uuid|dek_version``.

    Raises:
        EnvelopeInputError: any component fails validation. Table/column
            must be plain lowercase identifiers and `row_uuid` a UUID, so
            no component can contain the ``|`` delimiter and shift the
            boundary between two fields (delimiter-injection).
    """
    if not _IDENTIFIER.match(table) or not _IDENTIFIER.match(column):
        raise EnvelopeInputError("table and column must be lowercase identifiers")
    try:
        row = str(row_uuid) if isinstance(row_uuid, uuid.UUID) else str(uuid.UUID(row_uuid))
    except (ValueError, AttributeError, TypeError) as exc:
        raise EnvelopeInputError("row_uuid must be a UUID") from exc
    if isinstance(dek_version, bool) or not isinstance(dek_version, int) or dek_version < 1:
        raise EnvelopeInputError("dek_version must be a positive integer")
    return f"{validate_tenant_id(tenant_id)}|{table}|{column}|{row}|{dek_version}".encode()


def seal(subkey: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM encrypt `plaintext`; return ``(iv, ciphertext_with_tag)``.

    The 96-bit nonce is drawn fresh from the OS CSPRNG on every call --
    never a counter (a crash/restart can desynchronize a counter; design
    Sec2). Per-DEK usage is capped by the service, well inside the NIST
    SP 800-38D random-nonce bound.
    """
    iv = os.urandom(IV_LENGTH)
    return iv, AESGCM(subkey).encrypt(iv, plaintext, aad)


def open_sealed(subkey: bytes, iv: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    """AES-256-GCM decrypt; raise :class:`EnvelopeIntegrityError` on any authentication failure."""
    try:
        return AESGCM(subkey).decrypt(iv, ciphertext, aad)
    except (InvalidTag, ValueError) as exc:
        raise EnvelopeIntegrityError("envelope ciphertext failed authentication") from exc


def kek_id(kek: bytes) -> str:
    """Stable, non-reversible 16-hex-char id for a KEK (SHA-256 prefix) -- safe to store/log."""
    return hashlib.sha256(kek).digest()[:_KEK_ID_LENGTH].hex()


def local_wrap(kek: bytes, dek: bytes, context: Mapping[str, str]) -> bytes:
    """Wrap `dek` under `kek` (AES-256-GCM, `context` bound as AAD).

    Blob layout: ``0x01 || kek_id(8) || iv(12) || ciphertext+tag``. The
    embedded key id lets :func:`local_unwrap` pick the right KEK during a
    platform-KEK rotation.
    """
    if len(kek) != KEY_LENGTH or len(dek) != KEY_LENGTH:
        raise EnvelopeInputError("KEK and DEK must each be exactly 32 bytes")
    iv = os.urandom(IV_LENGTH)
    sealed = AESGCM(kek).encrypt(iv, dek, context_aad(context))
    return _LOCAL_WRAP_VERSION + bytes.fromhex(kek_id(kek)) + iv + sealed


def local_wrap_key_id(blob: bytes) -> str:
    """Return the KEK id embedded in a :func:`local_wrap` blob.

    Raises:
        EnvelopeInputError: the blob is not a recognizable local wrap.
    """
    header = 1 + _KEK_ID_LENGTH
    if len(blob) < header + IV_LENGTH + 16 or blob[:1] != _LOCAL_WRAP_VERSION:
        raise EnvelopeInputError("not a locally wrapped DEK")
    return blob[1:header].hex()


def local_unwrap(kek: bytes, blob: bytes, context: Mapping[str, str]) -> bytes:
    """Reverse :func:`local_wrap`; raise :class:`EnvelopeIntegrityError` on a wrong key/context."""
    local_wrap_key_id(blob)  # structural validation
    header = 1 + _KEK_ID_LENGTH
    iv, sealed = blob[header : header + IV_LENGTH], blob[header + IV_LENGTH :]
    try:
        dek = AESGCM(kek).decrypt(iv, sealed, context_aad(context))
    except (InvalidTag, ValueError) as exc:
        raise EnvelopeIntegrityError("wrapped DEK failed authentication") from exc
    if len(dek) != KEY_LENGTH:
        raise EnvelopeIntegrityError("wrapped DEK has an unexpected length")
    return dek
