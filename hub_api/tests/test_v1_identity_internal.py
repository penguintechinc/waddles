"""`blueprints/v1/identity_internal.py` -- the ephemeral-pseudonym mint endpoint.

Security review fix to PR #429: the pseudonym must be minted INSIDE the
PII boundary (HMAC-SHA256 keyed by a per-tenant-derived secret), never
locally computable by `core/svc_process`. Covers: (1) the derivation is
not computable without the master key and differs per tenant for the
same handle, (2) the mint route enforces its own dedicated service-key
scope (not the general `SERVICE_API_KEY`) and validates `tenant_id`,
(3) a misconfigured master key fails the request closed (500), never
falling back to something guessable, and never echoes the raw handle in
any response.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydal import DAL
from quart import Quart

from blueprints.v1.identity_internal import (
    EphemeralIdentityKeyError,
    derive_pseudonym,
    ensure_ephemeral_identities_table,
    identity_internal_bp,
    is_valid_identity_service_key,
    mint_or_touch_ephemeral_identity,
)

MASTER_KEY = "a" * 64
IDENTITY_SERVICE_API_KEY = "test-identity-service-key"


@pytest.fixture(autouse=True)
def _identity_key_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("IDENTITY_SERVICE_API_KEY", IDENTITY_SERVICE_API_KEY)
    monkeypatch.setenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", MASTER_KEY)
    # A DIFFERENT value than the dedicated key -- proves the route checks
    # its own dedicated credential, not the shared one.
    monkeypatch.setenv("SERVICE_API_KEY", "some-other-shared-key")


@pytest.fixture
def identity_db(tmp_path: Any) -> Any:
    dal = DAL(f"sqlite://{tmp_path / 'identity_test.db'}", pool_size=1)
    ensure_ephemeral_identities_table(dal, migrate=True)
    yield dal
    dal.close()


@pytest.fixture
def app() -> Quart:
    quart_app = Quart(__name__)
    quart_app.register_blueprint(identity_internal_bp)
    return quart_app


@pytest.fixture
def client(app: Quart, identity_db: Any) -> Any:
    app.config["dal"] = identity_db
    return app.test_client()


class FakeRequest:
    """Minimal stand-in for `quart.request`.

    Only `.headers` is touched by `is_valid_identity_service_key`.
    """

    def __init__(self, headers: dict[str, str]) -> None:
        """Wraps a fixed headers dict."""
        self.headers = headers


# ---- derive_pseudonym: not computable without the secret ----


def test_derive_pseudonym_is_deterministic_for_the_same_identity(monkeypatch: Any) -> None:
    monkeypatch.setenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", MASTER_KEY)
    a = derive_pseudonym(1, "twitch", "999")
    b = derive_pseudonym(1, "twitch", "999")
    assert a == b


def test_derive_pseudonym_differs_across_tenants_for_the_same_handle(monkeypatch: Any) -> None:
    """The core security property.

    The same platform identity in two different tenants must mint two
    DIFFERENT pseudonyms -- proves the per-tenant secret derivation
    actually varies the output, not just a fixed public namespace with
    the tenant id folded into a plaintext identity string (which would
    still be dictionary-attackable).
    """
    monkeypatch.setenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", MASTER_KEY)
    tenant_1 = derive_pseudonym(1, "twitch", "999")
    tenant_2 = derive_pseudonym(2, "twitch", "999")
    assert tenant_1 != tenant_2


def test_derive_pseudonym_differs_from_a_different_master_key(monkeypatch: Any) -> None:
    """Not computable without the secret.

    Changing ONLY the master key (never the tenant/platform/id) must
    change every derived pseudonym -- if it didn't, the master key would
    be irrelevant to the output and the whole point of hub-api-side
    minting would be defeated.
    """
    monkeypatch.setenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", MASTER_KEY)
    with_key_a = derive_pseudonym(1, "twitch", "999")
    monkeypatch.setenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", "b" * 64)
    with_key_b = derive_pseudonym(1, "twitch", "999")
    assert with_key_a != with_key_b


def test_derive_pseudonym_fails_closed_without_a_configured_master_key(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", raising=False)
    with pytest.raises(EphemeralIdentityKeyError):
        derive_pseudonym(1, "twitch", "999")


# ---- is_valid_identity_service_key: dedicated scope, not the shared key ----


def test_dedicated_service_key_accepts_the_correct_credential() -> None:
    req = FakeRequest({"X-Service-Key": IDENTITY_SERVICE_API_KEY})
    assert is_valid_identity_service_key(req)


def test_dedicated_service_key_rejects_the_general_shared_service_api_key() -> None:
    """The dedicated scope's whole point.

    A caller holding only the general `SERVICE_API_KEY` (valid for every
    OTHER internal blueprint in this port) must NOT be able to mint
    pseudonyms.
    """
    req = FakeRequest({"X-Service-Key": "some-other-shared-key"})
    assert not is_valid_identity_service_key(req)


def test_dedicated_service_key_rejects_a_missing_header() -> None:
    assert not is_valid_identity_service_key(FakeRequest({}))


def test_dedicated_service_key_fails_closed_when_unconfigured(monkeypatch: Any) -> None:
    monkeypatch.delenv("IDENTITY_SERVICE_API_KEY", raising=False)
    req = FakeRequest({"X-Service-Key": "anything"})
    assert not is_valid_identity_service_key(req)


# ---- mint_or_touch_ephemeral_identity: upsert, stable pseudonym ----


def test_mint_or_touch_returns_the_same_pseudonym_on_a_second_call(identity_db: Any) -> None:
    first = mint_or_touch_ephemeral_identity(
        identity_db, tenant_id=1, platform="twitch", platform_user_id="999", handle="someuser"
    )
    second = mint_or_touch_ephemeral_identity(
        identity_db, tenant_id=1, platform="twitch", platform_user_id="999", handle="someuser"
    )
    assert first == second
    row = identity_db(identity_db.ephemeral_identities.platform_user_id == "999").select().first()
    assert row is not None
    assert row.tenant_id == 1


# ---- route: enforces scope and tenant, never echoes the raw handle ----


@pytest.mark.asyncio
async def test_route_rejects_a_missing_service_key(client: Any) -> None:
    resp = await client.post(
        "/api/v1/internal/identities/ephemeral",
        json={"tenant_id": 1, "platform": "twitch", "platform_user_id": "999"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_route_rejects_the_general_shared_service_key(client: Any) -> None:
    resp = await client.post(
        "/api/v1/internal/identities/ephemeral",
        headers={"X-Service-Key": "some-other-shared-key"},
        json={"tenant_id": 1, "platform": "twitch", "platform_user_id": "999"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_route_rejects_a_missing_tenant_id(client: Any) -> None:
    resp = await client.post(
        "/api/v1/internal/identities/ephemeral",
        headers={"X-Service-Key": IDENTITY_SERVICE_API_KEY},
        json={"platform": "twitch", "platform_user_id": "999"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_route_rejects_a_negative_tenant_id(client: Any) -> None:
    resp = await client.post(
        "/api/v1/internal/identities/ephemeral",
        headers={"X-Service-Key": IDENTITY_SERVICE_API_KEY},
        json={"tenant_id": -1, "platform": "twitch", "platform_user_id": "999"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_route_mints_a_pseudonym_and_never_echoes_the_raw_handle(client: Any) -> None:
    resp = await client.post(
        "/api/v1/internal/identities/ephemeral",
        headers={"X-Service-Key": IDENTITY_SERVICE_API_KEY},
        json={
            "tenant_id": 1,
            "platform": "twitch",
            "platform_user_id": "999",
            "handle": "SuperSecretHandle",
        },
    )
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["success"] is True
    pseudonym = body["data"]["pseudonym"]
    assert isinstance(pseudonym, str)
    # Never the raw handle, never the platform_user_id, anywhere in the response.
    rendered = str(body)
    assert "SuperSecretHandle" not in rendered
    assert "999" not in rendered.replace(pseudonym, "")


@pytest.mark.asyncio
async def test_route_fails_closed_when_the_master_key_is_unconfigured(
    client: Any, monkeypatch: Any
) -> None:
    """The fallback contract lives on the CALLER's side.

    (`core/svc_process::hub_identity_client`'s random-token fallback) --
    this endpoint itself must fail closed (500), never invent or leak
    anything, when it cannot mint a real pseudonym.
    """
    monkeypatch.delenv("EPHEMERAL_IDENTITY_HMAC_MASTER_KEY", raising=False)
    resp = await client.post(
        "/api/v1/internal/identities/ephemeral",
        headers={"X-Service-Key": IDENTITY_SERVICE_API_KEY},
        json={
            "tenant_id": 1,
            "platform": "twitch",
            "platform_user_id": "999",
            "handle": "SuperSecretHandle",
        },
    )
    assert resp.status_code == 500
    body = await resp.get_json()
    assert "SuperSecretHandle" not in str(body)
