"""`services/twitch_install_credentials.py` -- store + refresh-with-rotation glue.

Covers: initial-credential storage shape, successful refresh persisting
the ROTATED refresh token (never the one spent), and the reuse/compromise
path -- a rejected refresh call revokes the whole stored row and raises
`TwitchRefreshRevokedError`, per security.md's "reused refresh token = treat as
compromise, revoke the chain" rule.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services import twitch_install_credentials as mod
from services.credential_resolver import TransportUnavailable
from services.twitch_install_oauth import TwitchOAuthError, TwitchTokenResult

_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


def _token(*, access: str = "tok_1", refresh: str = "ref_1") -> TwitchTokenResult:
    return TwitchTokenResult(
        access_token=access,
        refresh_token=refresh,
        expires_in=14400,
        scopes=["channel:read:subscriptions", "moderation:read", "channel:manage:moderators"],
        token_type="bearer",
    )


class TestStoreInitialCredentials:
    def test_stores_documented_payload_shape(self, bar_citizen_db: Any) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        mod.store_initial_credentials(
            dal,
            tenant_id=tenant_id,
            client_id="tenant-cid",
            client_secret="tenant-secret",
            token=_token(access="tok_abc", refresh="ref_abc"),
            installed_by_user_id=7,
        )

        row = (
            dal(
                (dal.tenant_platform_credentials.tenant_id == tenant_id)
                & (dal.tenant_platform_credentials.platform == "twitch")
            )
            .select()
            .first()
        )
        assert row is not None
        assert row.installed_by_user_id == 7
        assert "tenant-secret" not in row.credentials_ciphertext
        assert "ref_abc" not in row.credentials_ciphertext


class TestRefreshStoredCredentials:
    async def test_no_stored_credentials_raises_transport_unavailable(
        self, bar_citizen_db: Any
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        with pytest.raises(TransportUnavailable):
            await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)

    async def test_successful_refresh_rotates_refresh_token(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        mod.store_initial_credentials(
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

        # Re-resolve to confirm the NEW refresh token was persisted, not the old one.
        from services.credential_resolver import DefaultCredentialResolver

        resolved = await DefaultCredentialResolver().resolve(
            dal, tenant_id=tenant_id, is_global_tenant=False, platform="twitch"
        )
        assert resolved.payload["extra"]["refresh_token"] == "ref_new"
        assert resolved.payload["bot_token"] == "tok_new"

    async def test_rejected_refresh_revokes_and_raises(
        self, bar_citizen_db: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twitch rejecting a reused/rotated refresh token must revoke the whole stored row."""
        dal, _community_id, tenant_id, _global_id = bar_citizen_db
        mod.store_initial_credentials(
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

        row = (
            dal(
                (dal.tenant_platform_credentials.tenant_id == tenant_id)
                & (dal.tenant_platform_credentials.platform == "twitch")
            )
            .select()
            .first()
        )
        assert row is None

        # Role-sync worker must re-resolve to nothing -- forces re-install.
        with pytest.raises(TransportUnavailable):
            await mod.refresh_stored_credentials(dal, tenant_id=tenant_id)
