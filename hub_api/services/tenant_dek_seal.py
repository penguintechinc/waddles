"""Seal a tenant DEK to a caller's ephemeral X25519 key (HPKE-lite; the broker's only crypto glue).

Mirrors ``hpke_seal_open`` in ``core/svc_ingest/src/identity_crypto.rs`` exactly -- X25519 ECDH with
a one-time server key, HKDF-SHA256 (no salt, ``info=context``) expanded to key||nonce, AES-256-GCM
with ``aad=context`` -- using the ``cryptography`` primitives already in hub-api's requirements. No
new construction is introduced here: this only speaks the wire format the Rust client already has.
The nonce is context-derived, which is safe only because the AEAD key is unique per call (fresh
ephemeral server key every time); never reuse the server key.
"""

from __future__ import annotations

from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

#: Seal-protocol version bound into the context (the trailing ``|1``).
SEAL_VERSION: Final = 1
X25519_LEN: Final = 32
DEK_LEN: Final = 32
NONCE_LEN: Final = 12


def seal_context(service_id: str, tenant_id: int, purpose_label: str) -> bytes:
    """The exact ``service_id|tenant_id|purpose|1`` context both sides must derive."""
    return f"{service_id}|{tenant_id}|{purpose_label}|{SEAL_VERSION}".encode()


def parse_client_key(raw: bytes) -> X25519PublicKey | None:
    """Parse the caller's 32-byte X25519 public key; ``None`` if it has the wrong length."""
    if len(raw) != X25519_LEN:
        return None
    return X25519PublicKey.from_public_bytes(raw)


def seal_dek(dek: bytes, client_public: X25519PublicKey, context: bytes) -> tuple[bytes, bytes]:
    """Seal ``dek`` to ``client_public``; returns ``(enc, ciphertext)`` raw bytes.

    Raises:
        ValueError: ``client_public`` is a low-order point (all-zero shared secret) or ``dek`` is
            not 32 bytes.
    """
    if len(dek) != DEK_LEN:
        raise ValueError("DEK must be exactly 32 bytes")
    server_private = X25519PrivateKey.generate()
    shared = server_private.exchange(client_public)
    material = HKDF(
        algorithm=hashes.SHA256(), length=DEK_LEN + NONCE_LEN, salt=None, info=context
    ).derive(shared)
    ciphertext = AESGCM(material[:DEK_LEN]).encrypt(material[DEK_LEN:], dek, context)
    enc = server_private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return enc, ciphertext
