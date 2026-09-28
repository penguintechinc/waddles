"""`services/stream_key_transport.py` -- spec Sec5b ephemeral-key sealing."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from services.stream_key_transport import (
    StreamKeySealError,
    open_stream_key,
    seal_stream_key,
)

_INFO = b"svc-ingest|123|ingest-stream|1"


def test_round_trip_recovers_the_dek() -> None:
    dek = b"\x11" * 32
    client_private = X25519PrivateKey.generate()
    client_public_bytes = client_private.public_key().public_bytes_raw()

    sealed = seal_stream_key(
        plaintext_dek=dek, client_ephemeral_pubkey=client_public_bytes, info=_INFO
    )
    recovered = open_stream_key(
        hub_api_ephemeral_pubkey=sealed.hub_api_ephemeral_pubkey,
        nonce=sealed.nonce,
        sealed=sealed.sealed,
        client_ephemeral_private_key=client_private,
        info=_INFO,
    )
    assert recovered == dek


def test_sealed_bytes_never_contain_the_raw_dek() -> None:
    dek = b"\x22" * 32
    client_public_bytes = X25519PrivateKey.generate().public_key().public_bytes_raw()
    sealed = seal_stream_key(
        plaintext_dek=dek, client_ephemeral_pubkey=client_public_bytes, info=_INFO
    )
    assert dek not in sealed.sealed
    assert dek not in sealed.hub_api_ephemeral_pubkey


def test_each_call_uses_a_fresh_ephemeral_keypair_and_nonce() -> None:
    """Never reused across calls, even to the same client pubkey (spec Sec5b step 1)."""
    dek = b"\x33" * 32
    client_public_bytes = X25519PrivateKey.generate().public_key().public_bytes_raw()
    first = seal_stream_key(
        plaintext_dek=dek, client_ephemeral_pubkey=client_public_bytes, info=_INFO
    )
    second = seal_stream_key(
        plaintext_dek=dek, client_ephemeral_pubkey=client_public_bytes, info=_INFO
    )
    assert first.hub_api_ephemeral_pubkey != second.hub_api_ephemeral_pubkey
    assert first.nonce != second.nonce
    assert first.sealed != second.sealed


def test_wrong_client_private_key_fails_to_open() -> None:
    """A different ephemeral private key (not the one whose pubkey was sealed to) fails."""
    dek = b"\x44" * 32
    real_client_private = X25519PrivateKey.generate()
    real_client_public_bytes = real_client_private.public_key().public_bytes_raw()
    sealed = seal_stream_key(
        plaintext_dek=dek, client_ephemeral_pubkey=real_client_public_bytes, info=_INFO
    )

    wrong_private_key = X25519PrivateKey.generate()
    with pytest.raises(StreamKeySealError):
        open_stream_key(
            hub_api_ephemeral_pubkey=sealed.hub_api_ephemeral_pubkey,
            nonce=sealed.nonce,
            sealed=sealed.sealed,
            client_ephemeral_private_key=wrong_private_key,
            info=_INFO,
        )


def test_info_mismatch_fails_to_open() -> None:
    """`info` (service_id|tenant_id|purpose|dek_version) is bound as AEAD AAD (spec Sec5b)."""
    dek = b"\x55" * 32
    client_private = X25519PrivateKey.generate()
    client_public_bytes = client_private.public_key().public_bytes_raw()
    sealed = seal_stream_key(
        plaintext_dek=dek, client_ephemeral_pubkey=client_public_bytes, info=_INFO
    )

    with pytest.raises(StreamKeySealError):
        open_stream_key(
            hub_api_ephemeral_pubkey=sealed.hub_api_ephemeral_pubkey,
            nonce=sealed.nonce,
            sealed=sealed.sealed,
            client_ephemeral_private_key=client_private,
            info=b"svc-process|123|ingest-stream|1",  # wrong service_id in the bound context
        )


def test_replayed_response_against_different_tenant_fails() -> None:
    """A sealed blob replayed with a different tenant_id in `info` must not open."""
    dek = b"\x66" * 32
    client_private = X25519PrivateKey.generate()
    client_public_bytes = client_private.public_key().public_bytes_raw()
    sealed = seal_stream_key(
        plaintext_dek=dek,
        client_ephemeral_pubkey=client_public_bytes,
        info=b"svc-ingest|123|ingest-stream|1",
    )
    with pytest.raises(StreamKeySealError):
        open_stream_key(
            hub_api_ephemeral_pubkey=sealed.hub_api_ephemeral_pubkey,
            nonce=sealed.nonce,
            sealed=sealed.sealed,
            client_ephemeral_private_key=client_private,
            info=b"svc-ingest|999|ingest-stream|1",
        )


def test_invalid_client_pubkey_length_raises() -> None:
    with pytest.raises(StreamKeySealError, match="32 bytes"):
        seal_stream_key(
            plaintext_dek=b"\x00" * 32, client_ephemeral_pubkey=b"\x01" * 16, info=_INFO
        )


def test_invalid_hub_pubkey_on_open_raises() -> None:
    with pytest.raises(StreamKeySealError):
        open_stream_key(
            hub_api_ephemeral_pubkey=b"\x01" * 16,
            nonce=b"\x00" * 12,
            sealed=b"\x00" * 48,
            client_ephemeral_private_key=X25519PrivateKey.generate(),
            info=_INFO,
        )
