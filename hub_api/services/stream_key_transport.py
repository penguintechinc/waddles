"""Ephemeral X25519 + HKDF-SHA256 + AES-256-GCM sealing for the `ingest-stream` DEK.

Implements `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-
design.md` Sec5b -- the replacement for PR #442's original (removed)
"wrap with the platform KEK" transport, which the caller had no way to
unwrap at all. Functionally HPKE's base mode
(`DHKEM(X25519, HKDF-SHA256)` + `AES-256-GCM`) without a new `hpke`
dependency -- `cryptography>=44.0.1` (already pinned,
`hub_api/requirements.in`) provides every primitive.

Never logs or returns anything but the three wire fields
(`hub_api_ephemeral_pubkey`, `nonce`, `sealed`) -- no intermediate value
(shared secret, transport key) is ever serialized or logged.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PUBKEY_LEN = 32
_NONCE_LEN = 12
_TRANSPORT_KEY_LEN = 32


class StreamKeySealError(Exception):
    """Raised on a malformed ephemeral public key or a failed seal/open."""


@dataclass(slots=True, frozen=True)
class SealedStreamKey:
    """The three wire fields returned to the caller -- never the raw or wrapped DEK bytes."""

    hub_api_ephemeral_pubkey: bytes
    nonce: bytes
    sealed: bytes


def _hkdf(shared_secret: bytes, *, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=_TRANSPORT_KEY_LEN, salt=salt, info=info).derive(
        shared_secret
    )


def seal_stream_key(
    *, plaintext_dek: bytes, client_ephemeral_pubkey: bytes, info: bytes
) -> SealedStreamKey:
    """Seal `plaintext_dek` to `client_ephemeral_pubkey` (spec Sec5b steps 1-8).

    `info` MUST already be bound to `service_id|tenant_id|purpose|dek_version`
    (the caller, `blueprints/v1/internal_keys.py`, builds it) -- this
    function only performs the cryptographic construction, it has no
    tenant/service context of its own to bind.

    Raises `StreamKeySealError` if `client_ephemeral_pubkey` isn't a valid
    32-byte X25519 public key -- never raises the underlying
    `cryptography` exception type directly, so callers don't need to
    import it.
    """
    if len(client_ephemeral_pubkey) != _PUBKEY_LEN:
        raise StreamKeySealError(
            f"client_ephemeral_pubkey must be {_PUBKEY_LEN} bytes, got "
            f"{len(client_ephemeral_pubkey)}"
        )
    try:
        client_pub = X25519PublicKey.from_public_bytes(client_ephemeral_pubkey)
    except ValueError as exc:
        raise StreamKeySealError(f"invalid client_ephemeral_pubkey: {exc}") from exc

    hub_private = X25519PrivateKey.generate()
    hub_public_bytes = hub_private.public_key().public_bytes_raw()
    shared_secret = hub_private.exchange(client_pub)

    salt = hub_public_bytes + client_ephemeral_pubkey
    transport_key = _hkdf(shared_secret, salt=salt, info=info)

    nonce = secrets.token_bytes(_NONCE_LEN)
    sealed = AESGCM(transport_key).encrypt(nonce, plaintext_dek, info)

    return SealedStreamKey(hub_api_ephemeral_pubkey=hub_public_bytes, nonce=nonce, sealed=sealed)


def open_stream_key(
    *,
    hub_api_ephemeral_pubkey: bytes,
    nonce: bytes,
    sealed: bytes,
    client_ephemeral_private_key: X25519PrivateKey,
    info: bytes,
) -> bytes:
    """Reverse `seal_stream_key()` -- reference implementation for tests/parity checks.

    The real consumer is Rust (`svc_ingest`/`svc_process`, PR #443's
    `HubApiDekProvider`); this Python mirror exists so this module's own
    test suite proves round-trip correctness without a cross-language
    harness, and so a future Python caller (if one ever needs it) has a
    ready-made reference.
    """
    try:
        hub_pub = X25519PublicKey.from_public_bytes(hub_api_ephemeral_pubkey)
    except ValueError as exc:
        raise StreamKeySealError(f"invalid hub_api_ephemeral_pubkey: {exc}") from exc

    shared_secret = client_ephemeral_private_key.exchange(hub_pub)
    client_pub_bytes = client_ephemeral_private_key.public_key().public_bytes_raw()
    salt = hub_api_ephemeral_pubkey + client_pub_bytes
    transport_key = _hkdf(shared_secret, salt=salt, info=info)

    try:
        return AESGCM(transport_key).decrypt(nonce, sealed, info)
    except InvalidTag as exc:
        raise StreamKeySealError("failed to open sealed stream key -- tag mismatch") from exc
