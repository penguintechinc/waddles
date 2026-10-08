"""`services/twitch_install_credentials.py` -- store + refresh-with-rotation glue.

Covers: the layer-1 (app creds) / layer-2 (channel connection) storage
split migration 0035 introduced, successful refresh persisting the
ROTATED refresh token (never the one spent) to layer 2 only, and the
reuse/compromise path -- a rejected refresh call revokes the layer-2
connection (never layer 1) and raises `TwitchRefreshRevokedError`, per
security.md's "reused refresh token = treat as compromise, revoke the
chain" rule.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import twitch_install_credentials as mod
from services.credential_resolver import TransportUnavailable, get_platform_connection
from services.twitch_install_oauth import TwitchOAuthError, TwitchTokenResult

_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


@pytest.fixture(autouse=True)
def _fetch_token_user_id(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub the Twitch `/oauth2/validate` self-lookup every `store_initial_credentials` call makes.

    Returns a fixed broadcaster id -- `platform_connections.resource_id`
    -- so these tests never hit the network.
    """
    stub = AsyncMock(return_value="broadcaster-1")
    monkeypatch.setattr(mod, "fetch_token_user_id", stub)
    return stub


def _token(*, access: str = "tok_1", refresh: str = "ref_1") -> TwitchTokenResult:
    return TwitchTokenResult(
        access_token=access,
        refresh_token=refresh,
        expires_in=14400,
        scopes=["channel:read:subscriptions", "moderation:read", "channel:manage:moderators"],
        token_type="bearer",
    )


class TestStoreInitialCredentials:
    async def test_stores_layer1_app_creds_and_layer2_connection(self, bar_citizen_db: Any) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        await mod.store_initial_credentials(
            dal,
            tenant_id=tenant_id,
            client_id="tenant-cid",
            client_secret="tenant-secret",
            token=_token(access="tok_abc", refresh="ref_abc"),
            installed_by_user_id=7,
        )

        app_row = (
            dal(
                (dal.tenant_platform_apps.tenant_id == tenant_id)
                & (dal.tenant_platform_apps.platform == "twitch")
            )
            .select()
            .first()
        )
        assert app_row is not None
        assert app_row.installed_by_user_id == 7
        assert "tenant-secret" not in app_row.credentials_ciphertext
        # Layer 1 no longer carries the channel's own tokens at all.
        assert "ref_abc" not in app_row.credentials_ciphertext
        assert "tok_abc" not in app_row.credentials_ciphertext

        connection = get_platform_connection(
            dal, tenant_id=tenant_id, platform="twitch", resource_id="broadcaster-1"
        )
        assert connection is not None
        assert connection.access_token == "tok_abc"
        assert connection.refresh_token == "ref_abc"
        assert connection.resource_type == "twitch_channel"
        assert connection.installed_by_user_id == 7

    async def test_resource_lookup_failure_raises_transport_unavailable(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The layer-1 app row is still persisted even if the channel lookup fails."""
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        monkeypatch.setattr(
            mod, "fetch_token_user_id", AsyncMock(side_effect=TwitchOAuthError("boom"))
        )

        with pytest.raises(TransportUnavailable):
            await mod.store_initial_credentials(
                dal,
                tenant_id=tenant_id,
                client_id="tenant-cid",
                client_secret="tenant-secret",
                token=_token(),
                installed_by_user_id=7,
            )

        app_row = (
            dal(
                (dal.tenant_platform_apps.tenant_id == tenant_id)
                & (dal.tenant_platform_apps.platform == "twitch")
            )
            .select()
            .first()
        )
        assert app_row is not None
        connection = get_platform_connection(
            dal, tenant_id=tenant_id, platform="twitch", resource_id="x"
        )
        assert connection is None


class TestRefreshStoredCredentials:
    async def test_no_stored_credentials_raises_transport_unavailable(
        self, bar_citizen_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        with pytest.raises(TransportUnavailable):
            await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)

    async def test_app_creds_without_connection_raises_transport_unavailable(
        self, bar_citizen_db: Any
    ) -> None:
        """Layer 1 (app creds) alone, with no layer-2 connection, is still unrefreshable."""
        from services.credential_resolver import store_tenant_credentials

        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        store_tenant_credentials(
            dal,
            tenant_id=tenant_id,
            is_global_tenant=False,
            platform="twitch",
            payload={"client_id": "tenant-cid", "client_secret": "tenant-secret"},
            installed_by_user_id=7,
        )
        with pytest.raises(TransportUnavailable):
            await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)

    async def test_successful_refresh_rotates_refresh_token(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        await mod.store_initial_credentials(
            dal,
            tenant_id=tenant_id,
            client_id="tenant-cid",
            client_secret="tenant-secret",
            token=_token(access="tok_old", refresh="ref_old"),
            installed_by_user_id=7,
        )

        new_token = _token(access="tok_new", refresh="ref_new")
        fake_refresh = AsyncMock(return_value=new_token)
        monkeypatch.setattr(mod, "refresh_access_token", fake_refresh)

        result = await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)
        assert result.access_token == "tok_new"
        fake_refresh.assert_awaited_once_with(
            client_id="tenant-cid", client_secret="tenant-secret", refresh_token="ref_old"
        )

        # Re-resolve to confirm the NEW refresh token was persisted, not the old one --
        # and that it landed in layer 2 (platform_connections), not layer 1.
        connection = get_platform_connection(
            dal, tenant_id=tenant_id, platform="twitch", resource_id="broadcaster-1"
        )
        assert connection is not None
        assert connection.refresh_token == "ref_new"
        assert connection.access_token == "tok_new"

    async def test_rejected_refresh_revokes_connection_not_app_creds(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twitch rejecting a reused/rotated refresh token revokes the layer-2 connection only."""
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        await mod.store_initial_credentials(
            dal,
            tenant_id=tenant_id,
            client_id="tenant-cid",
            client_secret="tenant-secret",
            token=_token(access="tok_old", refresh="ref_old"),
            installed_by_user_id=7,
        )

        async def _raise(**_kwargs: Any) -> TwitchTokenResult:
            raise TwitchOAuthError("twitch: token endpoint returned HTTP 400")

        monkeypatch.setattr(mod, "refresh_access_token", _raise)

        with pytest.raises(mod.TwitchRefreshRevokedError):
            await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)

        # Layer 2 connection is gone...
        assert (
            get_platform_connection(
                dal, tenant_id=tenant_id, platform="twitch", resource_id="broadcaster-1"
            )
            is None
        )
        # ...but layer 1's app credentials (client_id/secret) are untouched -- the app
        # itself was never compromised, only the per-channel grant.
        app_row = (
            dal(
                (dal.tenant_platform_apps.tenant_id == tenant_id)
                & (dal.tenant_platform_apps.platform == "twitch")
            )
            .select()
            .first()
        )
        assert app_row is not None

        # Role-sync worker must re-resolve to nothing -- forces re-authorization.
        with pytest.raises(TransportUnavailable):
            await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)
