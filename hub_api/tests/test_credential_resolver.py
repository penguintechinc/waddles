"""`services/credential_resolver.py` -- `CredentialResolver` seam tests (Bar Citizen foundation).

Covers both lanes `DefaultCredentialResolver.resolve()` takes (tenant 0 /
SaaS env vars, tenant N / decrypted DB row) and the `TransportUnavailable`
failure path for each -- the contract Units C/D/F build their own code
against.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.credential_resolver import (
    DefaultCredentialResolver,
    TransportUnavailable,
    store_tenant_credentials,
)
from services.errors import ApiError

_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


@pytest.fixture
def resolver() -> DefaultCredentialResolver:
    return DefaultCredentialResolver()


class TestGlobalTenantLane:
    async def test_resolves_from_env_vars(
        self, resolver: DefaultCredentialResolver, bar_citizen_db: Any, monkeypatch: Any
    ) -> None:
        dal, _community_id, _tenant_id, global_tenant_id = bar_citizen_db
        monkeypatch.setenv("DISCORD_CLIENT_ID", "saas-client-id")
        monkeypatch.setenv("DISCORD_CLIENT_SECRET", "saas-client-secret")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "saas-bot-token")

        creds = await resolver.resolve(
            dal, tenant_id=global_tenant_id, is_global_tenant=True, platform="discord"
        )
        assert creds.source == "saas"
        assert creds.payload == {
            "client_id": "saas-client-id",
            "client_secret": "saas-client-secret",
            "bot_token": "saas-bot-token",
        }

    async def test_missing_env_vars_raises_transport_unavailable(
        self, resolver: DefaultCredentialResolver, bar_citizen_db: Any, monkeypatch: Any
    ) -> None:
        dal, _community_id, _tenant_id, global_tenant_id = bar_citizen_db
        monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
        monkeypatch.delenv("DISCORD_CLIENT_SECRET", raising=False)

        with pytest.raises(TransportUnavailable):
            await resolver.resolve(
                dal, tenant_id=global_tenant_id, is_global_tenant=True, platform="discord"
            )


class TestTenantLane:
    async def test_resolves_stored_encrypted_credentials(
        self, resolver: DefaultCredentialResolver, bar_citizen_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        store_tenant_credentials(
            dal,
            tenant_id=tenant_id,
            is_global_tenant=False,
            platform="twitch",
            payload={"client_id": "tenant-id", "client_secret": "tenant-secret"},
            installed_by_user_id=7,
        )

        creds = await resolver.resolve(
            dal, tenant_id=tenant_id, is_global_tenant=False, platform="twitch"
        )
        assert creds.source == "tenant"
        assert creds.payload == {"client_id": "tenant-id", "client_secret": "tenant-secret"}

    async def test_missing_row_raises_transport_unavailable(
        self, resolver: DefaultCredentialResolver, bar_citizen_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        with pytest.raises(TransportUnavailable):
            await resolver.resolve(
                dal, tenant_id=tenant_id, is_global_tenant=False, platform="twitch"
            )

    async def test_corrupt_ciphertext_raises_transport_unavailable(
        self, resolver: DefaultCredentialResolver, bar_citizen_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        t = dal.tenant_platform_credentials
        t.insert(
            tenant_id=tenant_id,
            platform="discord",
            credentials_ciphertext="not-valid-base64-ciphertext",
        )
        dal.commit()

        with pytest.raises(TransportUnavailable):
            await resolver.resolve(
                dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
            )

    def test_store_rejects_global_tenant(self, bar_citizen_db: Any) -> None:
        dal, _community_id, _tenant_id, global_tenant_id = bar_citizen_db
        with pytest.raises(ApiError) as excinfo:
            store_tenant_credentials(
                dal,
                tenant_id=global_tenant_id,
                is_global_tenant=True,
                platform="discord",
                payload={"client_id": "x", "client_secret": "y"},
                installed_by_user_id=None,
            )
        assert excinfo.value.status_code == 400

    def test_store_upserts_existing_row(self, bar_citizen_db: Any) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        store_tenant_credentials(
            dal,
            tenant_id=tenant_id,
            is_global_tenant=False,
            platform="discord",
            payload={"client_id": "first", "client_secret": "s1"},
            installed_by_user_id=1,
        )
        store_tenant_credentials(
            dal,
            tenant_id=tenant_id,
            is_global_tenant=False,
            platform="discord",
            payload={"client_id": "second", "client_secret": "s2"},
            installed_by_user_id=2,
        )
        t = dal.tenant_platform_credentials
        rows = dal((t.tenant_id == tenant_id) & (t.platform == "discord")).select()
        assert len(rows) == 1
