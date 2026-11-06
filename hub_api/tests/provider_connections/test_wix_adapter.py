"""Wix provider adapter: structural public-key validation, instance metadata, registry wiring.

No network is involved (the adapter never calls Wix); keys are generated per run, never committed.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from services.provider_connections.adapter import (
    Credentials,
    validate_credentials_shape,
    validate_metadata,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    RefreshNotConfigured,
)
from services.provider_connections.kinds import ConnectionKind
from services.provider_connections.registry import build_default_registry
from services.provider_connections.wix import WIX_SPEC, WixAdapter

INSTANCE = "0b9e1a52-4c3d-4e6f-8a7b-1234567890ab"


def _spki_b64(key: object) -> str:
    der = key.public_key().public_bytes(  # type: ignore[attr-defined]
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode()


@pytest.fixture(scope="module")
def rsa_key_b64() -> str:
    return _spki_b64(rsa.generate_private_key(public_exponent=65537, key_size=2048))


async def test_valid_rsa_public_key_is_accepted(rsa_key_b64: str) -> None:
    adapter = WixAdapter()
    account = await adapter.validate_credentials(Credentials(access_token=rsa_key_b64))
    assert account.account_label == "Wix app public key"
    assert account.scopes == () and account.expires_at is None


async def test_small_rsa_ec_and_garbage_keys_are_rejected() -> None:
    adapter = WixAdapter()
    small = _spki_b64(rsa.generate_private_key(public_exponent=65537, key_size=1024))  # noqa: S505 - deliberately weak
    ec_key = _spki_b64(ec.generate_private_key(ec.SECP256R1()))
    for bad in (small, ec_key, "!!!notbase64!!!", base64.b64encode(b"not a key").decode(), "QUJD"):
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials(access_token=bad))


def test_pem_armor_is_refused_by_the_shape_check(rsa_key_b64: str) -> None:
    pem = f"-----BEGIN PUBLIC KEY-----\n{rsa_key_b64}\n-----END PUBLIC KEY-----"
    with pytest.raises(InvalidConnectionInput):
        validate_credentials_shape(WIX_SPEC, pem, None)
    assert validate_credentials_shape(WIX_SPEC, rsa_key_b64, None).access_token == rsa_key_b64


def test_instance_id_metadata_validation() -> None:
    assert validate_metadata(WIX_SPEC, {"instance_id": INSTANCE}) == {"instance_id": INSTANCE}
    for bad in (
        {},
        {"instance_id": ""},
        {"instance_id": "short"},
        {"instance_id": "a b c d e f g h"},
        {"instance_id": INSTANCE + "\nx"},
        {"instance_id": "x" * 65},
        {"instance_id": INSTANCE, "extra": "1"},
    ):
        with pytest.raises(InvalidConnectionInput):
            validate_metadata(WIX_SPEC, bad)


async def test_resolve_resource_uses_the_instance_id(rsa_key_b64: str) -> None:
    adapter = WixAdapter()
    creds = Credentials(access_token=rsa_key_b64)
    account = await adapter.validate_credentials(creds)
    resource = await adapter.resolve_resource(creds, account, {"instance_id": INSTANCE})
    assert resource.resource_id == INSTANCE and dict(resource.facts) == {"instance_id": INSTANCE}


async def test_not_refreshable_and_capabilities() -> None:
    adapter = WixAdapter()
    with pytest.raises(RefreshNotConfigured):
        await adapter.refresh("x", {})
    assert adapter.granted_capabilities(()) == frozenset({"webhook_ingest"})
    assert adapter.normalize_credentials(Credentials(access_token="abc")).access_token == "abc"


def test_spec_shape_and_registry_wiring() -> None:
    assert WIX_SPEC.kind is ConnectionKind.PLATFORM_OAUTH and not WIX_SPEC.refreshable
    assert WIX_SPEC.runtime_metadata_keys == frozenset({"instance_id"})
    assert [b.wire for b in WIX_SPEC.binding_metadata] == ["instance_id"]
    assert build_default_registry().get("wix").spec is WIX_SPEC
