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
    delete_platform_connection,
    get_platform_connection,
    grant_community_connection_access,
    resolve_community_connection,
    store_tenant_credentials,
    upsert_platform_connection,
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
        t = dal.tenant_platform_apps
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
        t = dal.tenant_platform_apps
        rows = dal((t.tenant_id == tenant_id) & (t.platform == "discord")).select()
        assert len(rows) == 1


class TestPlatformConnections:
    """Layer 2 (`platform_connections`) seam -- migration 0035 (connection-model-port)."""

    def test_upsert_then_get_round_trips_decrypted_tokens(self, bar_citizen_db: Any) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-1",
            access_token="tok_abc",
            refresh_token="ref_abc",
            installed_by_user_id=7,
        )
        assert connection_id > 0

        connection = get_platform_connection(
            dal, tenant_id=tenant_id, platform="twitch", resource_id="broadcaster-1"
        )
        assert connection is not None
        assert connection.access_token == "tok_abc"
        assert connection.refresh_token == "ref_abc"
        assert connection.status == "active"
        assert connection.installed_by_user_id == 7

    def test_upsert_is_idempotent_per_resource(self, bar_citizen_db: Any) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        first_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-1",
            access_token="tok_1",
            refresh_token="ref_1",
            installed_by_user_id=1,
        )
        second_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-1",
            access_token="tok_2",
            refresh_token="ref_2",
            installed_by_user_id=1,
        )
        assert first_id == second_id

        connection = get_platform_connection(
            dal, tenant_id=tenant_id, platform="twitch", resource_id="broadcaster-1"
        )
        assert connection is not None
        assert connection.access_token == "tok_2"

    def test_get_missing_connection_returns_none(self, bar_citizen_db: Any) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        assert (
            get_platform_connection(dal, tenant_id=tenant_id, platform="twitch", resource_id="nope")
            is None
        )

    def test_cross_tenant_isolation_same_resource_id(self, bar_citizen_db: Any) -> None:
        """Two tenants sharing the same `resource_id` never see each other's connection row."""
        dal, _community_id, tenant_a_id, _global_id = bar_citizen_db
        tenant_b_id = dal.tenants.insert(
            slug="other-tenant", display_name="Other Tenant", is_active=True, is_global=False
        )
        dal.commit()

        upsert_platform_connection(
            dal,
            tenant_id=tenant_a_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="shared-resource",
            access_token="tok_a",
            refresh_token="ref_a",
            installed_by_user_id=1,
        )
        upsert_platform_connection(
            dal,
            tenant_id=tenant_b_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="shared-resource",
            access_token="tok_b",
            refresh_token="ref_b",
            installed_by_user_id=2,
        )

        conn_a = get_platform_connection(
            dal, tenant_id=tenant_a_id, platform="twitch", resource_id="shared-resource"
        )
        conn_b = get_platform_connection(
            dal, tenant_id=tenant_b_id, platform="twitch", resource_id="shared-resource"
        )
        assert conn_a is not None and conn_a.access_token == "tok_a"
        assert conn_b is not None and conn_b.access_token == "tok_b"
        assert conn_a.id != conn_b.id

    def test_delete_platform_connection_cascades_access_grant(self, bar_citizen_db: Any) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-1",
            access_token="tok_1",
            refresh_token="ref_1",
            installed_by_user_id=1,
        )
        grant_community_connection_access(
            dal, community_id=community_id, connection_id=connection_id, status="approved"
        )

        delete_platform_connection(dal, connection_id=connection_id)

        assert (
            dal(dal.community_connection_access.connection_id == connection_id).select().first()
            is None
        )


class TestCommunityConnectionAccess:
    """Layer 3 (`community_connection_access`) seam -- grant-only, no tokens."""

    def test_resolve_requires_approved_status(self, bar_citizen_db: Any) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-1",
            access_token="tok_1",
            refresh_token="ref_1",
            installed_by_user_id=1,
        )
        grant_community_connection_access(
            dal,
            community_id=community_id,
            connection_id=connection_id,
            status="pending",
            requested_by_user_id=3,
        )

        assert (
            resolve_community_connection(dal, community_id=community_id, platform="twitch") is None
        )

        grant_community_connection_access(
            dal,
            community_id=community_id,
            connection_id=connection_id,
            status="approved",
            approved_by_user_id=9,
        )

        resolved = resolve_community_connection(dal, community_id=community_id, platform="twitch")
        assert resolved is not None
        assert resolved.access_token == "tok_1"
        assert resolved.id == connection_id

    def test_revoked_connection_access_never_resolves(self, bar_citizen_db: Any) -> None:
        dal, community_id, tenant_id, _global_id = bar_citizen_db
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-1",
            access_token="tok_1",
            refresh_token="ref_1",
            installed_by_user_id=1,
        )
        grant_community_connection_access(
            dal, community_id=community_id, connection_id=connection_id, status="approved"
        )
        grant_community_connection_access(
            dal, community_id=community_id, connection_id=connection_id, status="revoked"
        )

        assert (
            resolve_community_connection(dal, community_id=community_id, platform="twitch") is None
        )

    def test_grant_rejects_cross_tenant_connection(self, bar_citizen_db: Any) -> None:
        """A community can never be granted access to a connection installed under another tenant.

        `grant_community_connection_access()`'s own tenant-match check is
        `resolve_community_connection()`'s actual isolation guarantee --
        this proves the guard is enforced at grant time, not merely
        assumed at resolve time.
        """
        dal, community_id, _tenant_a_id, _global_id = bar_citizen_db
        tenant_b_id = dal.tenants.insert(
            slug="other-tenant-2", display_name="Other Tenant 2", is_active=True, is_global=False
        )
        dal.commit()

        other_connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_b_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="tenant-b-broadcaster",
            access_token="tok_b",
            refresh_token="ref_b",
            installed_by_user_id=1,
        )

        # `community_id` belongs to tenant A (seeded by `bar_citizen_db`) -- granting it access
        # to tenant B's connection must be rejected outright, never silently stored.
        with pytest.raises(ApiError) as excinfo:
            grant_community_connection_access(
                dal,
                community_id=community_id,
                connection_id=other_connection_id,
                status="approved",
            )
        assert excinfo.value.status_code == 400

        # No grant row was created -- resolution stays None, not just "unapproved".
        assert (
            resolve_community_connection(dal, community_id=community_id, platform="twitch") is None
        )
