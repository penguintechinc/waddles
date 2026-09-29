"""Tests for the per-tenant Discord app credential store + OAuth2 bot-install flow (#500/#501).

Own, self-contained `AsyncDB` fixture (`_oauth_install_dal`) rather than
extending the shared `tests/conftest.py::install_dal` fixture -- this
slice's branch is stacked on `feature/guild-pairing-nm-binding` while a
concurrent branch (`feature/guild-pairing-api`) also adds tables under the
same migration; keeping this file's schema-mirror additive and local
avoids both branches editing the same shared fixture file.

Discord itself is never called -- `_exchange_code`/`DiscordGuildAuthorityVerifier`
network calls are patched via `unittest.mock`, matching
`services/oauth_providers.py`'s own "no real Discord calls in tests"
precedent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from penguin_dal import AsyncDB
from quart import Quart
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

from services import guild_oauth_state
from services.discord_guild_authority_verifier import DiscordGuildAuthorityVerifier
from services.errors import ApiError
from services.guild_credential_resolution import CredentialResolutionError, resolve_credentials
from services.guild_oauth_install_service import (
    OAuthInstallError,
    build_authorize_url,
    complete_install,
    revoke_pairing,
)
from services.platform_oauth_connectors import CONNECTORS
from services.tenant_platform_credentials_crypto import (
    EncryptionKeyError,
    decrypt,
    encrypt,
    mask_secret,
)
from services.tenant_platform_credentials_service import (
    SUPPORTED_PLATFORMS,
    get_masked_credentials,
    set_credentials,
)

GLOBAL_TENANT_ID = 1
TENANT_A_ID = 2
TENANT_B_ID = 3


def _create_tables(conn: Any) -> None:
    metadata = MetaData()
    Table(
        "tenants",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("slug", String(100)),
        Column("is_global", Boolean, server_default="0"),
    )
    Table(
        "tenant_platform_credentials",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("tenant_id", Integer, nullable=False),
        Column("platform", String(50), nullable=False),
        Column("application_id", String(255), nullable=False),
        Column("client_secret_ciphertext", LargeBinary, nullable=False),
        Column("client_secret_iv", LargeBinary, nullable=False),
        Column("bot_token_ciphertext", LargeBinary),
        Column("bot_token_iv", LargeBinary),
        Column("extra_secret_ciphertext", LargeBinary),
        Column("extra_secret_iv", LargeBinary),
        Column("key_ref", String(255), nullable=False),
        Column("is_active", Boolean, server_default="1"),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
        UniqueConstraint("tenant_id", "platform"),
    )
    Table(
        "guild_tenant_pairings",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("platform", String(50), nullable=False),
        Column("guild_id", String(255), nullable=False),
        Column("tenant_id", Integer, nullable=False),
        Column("status", String(20), nullable=False, server_default="pending"),
        Column("installed_by_user_id", Integer),
        Column("granted_permissions", BigInteger),
        Column("oauth_scopes", String(255)),
        Column("consent_at", DateTime),
        Column("last_verified_at", DateTime),
        Column("revoked_at", DateTime),
        Column("revoked_by", String(30)),
        Column("revoked_by_user_id", Integer),
        Column("created_at", DateTime),
        Column("updated_at", DateTime),
        UniqueConstraint("platform", "guild_id", "tenant_id"),
    )
    Table(
        "audit_log",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", Integer),
        Column("action", String(100), nullable=False),
        Column("target_type", String(50)),
        Column("target_id", String(255)),
        Column("details", JSON),
        Column("created_at", DateTime),
    )
    metadata.create_all(conn)


@pytest.fixture
async def oauth_install_dal(tmp_path: Any) -> Any:
    db_path = tmp_path / "guild_pairing_oauth_test.db"
    dal = AsyncDB(f"sqlite:///{db_path}", pool_size=1, echo=False)
    async with dal.engine.begin() as conn:
        await conn.run_sync(_create_tables)
    await dal.reflect()

    await dal.tenants.async_insert(id=GLOBAL_TENANT_ID, slug="global", is_global=True)
    await dal.tenants.async_insert(id=TENANT_A_ID, slug="tenant-a", is_global=False)
    await dal.tenants.async_insert(id=TENANT_B_ID, slug="tenant-b", is_global=False)

    yield dal
    await dal.close()


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_DISCORD_CREDENTIAL_ENCRYPTION_KEY", "a" * 64)
    monkeypatch.setenv("GUILD_OAUTH_STATE_SECRET", "test-state-secret")


@pytest.fixture
def state_app() -> Quart:
    """Minimal Quart app so `guild_oauth_state`'s Redis-lookup functions have `current_app`."""
    app = Quart(__name__)
    app.config["HUB_API_CONFIG"] = type("Cfg", (), {"valkey_url": "redis://localhost:6379/0"})()
    return app


class _FakeRedis:
    """In-memory async Redis stand-in -- `SET NX EX` semantics only, what the nonce check needs."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self._store:
            return False
        self._store[key] = value
        return True


class TestCryptoPrimitives:
    def test_mask_secret_short_value(self) -> None:
        assert mask_secret("ab") == "...ab"

    def test_decrypt_rejects_unsupported_key_ref(self) -> None:
        ciphertext, iv = encrypt("value")
        with pytest.raises(EncryptionKeyError):
            decrypt(ciphertext, iv, key_ref="some-other-scheme")


# ===== Credential storage: encryption round-trip, masking, audit =====


class TestCredentialStorage:
    async def test_set_credentials_round_trips_and_masks(self, oauth_install_dal: Any) -> None:
        result = await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=42,
            application_id="app-123",
            client_secret="super-secret-value-1234",
            bot_token="bot-token-value-5678",
        )
        assert result.client_secret_hint == "...1234"
        assert result.bot_token_hint == "...5678"
        assert result.is_active is True

    async def test_ciphertext_at_rest_never_plaintext(self, oauth_install_dal: Any) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=42,
            application_id="app-123",
            client_secret="super-secret-value-1234",
            bot_token="bot-token-value-5678",
        )
        row = (
            await oauth_install_dal(
                oauth_install_dal.tenant_platform_credentials.tenant_id == TENANT_A_ID
            ).select()
        ).first()
        assert b"super-secret-value-1234" not in bytes(row.client_secret_ciphertext)
        assert b"bot-token-value-5678" not in bytes(row.bot_token_ciphertext)

    async def test_get_masked_credentials_never_returns_raw_value(
        self, oauth_install_dal: Any
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=42,
            application_id="app-123",
            client_secret="super-secret-value-1234",
            bot_token="bot-token-value-5678",
        )
        result = await get_masked_credentials(oauth_install_dal, tenant_id=TENANT_A_ID)
        assert result.client_secret_hint == "...1234"
        assert "super-secret-value-1234" not in result.client_secret_hint

    async def test_set_credentials_writes_audit_row(self, oauth_install_dal: Any) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=42,
            application_id="app-123",
            client_secret="super-secret-value-1234",
            bot_token="bot-token-value-5678",
        )
        rows = await oauth_install_dal(oauth_install_dal.audit_log.user_id == 42).select()
        actions = [r.action for r in rows]
        assert "tenant_platform_credentials.create" in actions

    async def test_set_credentials_blocked_for_global_tenant(self, oauth_install_dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            await set_credentials(
                oauth_install_dal,
                tenant_id=GLOBAL_TENANT_ID,
                actor_id=1,
                application_id="app-x",
                client_secret="secret-x",
                bot_token="token-x",
            )
        assert exc_info.value.status_code == 409

    async def test_tenant_a_credentials_never_returned_for_tenant_b(
        self, oauth_install_dal: Any
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="app-a",
            client_secret="secret-a-1111",
            bot_token="token-a-2222",
        )
        with pytest.raises(ApiError) as exc_info:
            await get_masked_credentials(oauth_install_dal, tenant_id=TENANT_B_ID)
        assert exc_info.value.status_code == 404

    async def test_get_masked_credentials_404_when_unconfigured(
        self, oauth_install_dal: Any
    ) -> None:
        with pytest.raises(ApiError) as exc_info:
            await get_masked_credentials(oauth_install_dal, tenant_id=TENANT_A_ID)
        assert exc_info.value.status_code == 404

    async def test_rotate_with_extra_secret_round_trips(self, oauth_install_dal: Any) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="app-1",
            client_secret="secret-1",
            bot_token="token-1",
        )
        rotated = await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="app-2",
            client_secret="secret-2",
            bot_token="token-2",
            extra_secret="webhook-secret-9999",
        )
        assert rotated.extra_secret_hint == "...9999"
        fetched = await get_masked_credentials(oauth_install_dal, tenant_id=TENANT_A_ID)
        assert fetched.extra_secret_hint == "...9999"
        assert fetched.application_id == "app-2"


# ===== OAuth state: HMAC signature, expiry, single-use =====


class TestOAuthState:
    async def test_valid_state_round_trips(self, state_app: Quart) -> None:
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=7)
                verified = await guild_oauth_state.verify_and_consume_state(state)
        assert verified.tenant_id == TENANT_A_ID
        assert verified.admin_user_id == 7

    async def test_forged_state_rejected(self, state_app: Quart) -> None:
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=7)
                tampered = state[:-4] + "0000"
                with pytest.raises(guild_oauth_state.StateError):
                    await guild_oauth_state.verify_and_consume_state(tampered)

    async def test_expired_state_rejected(self, state_app: Quart) -> None:
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(
                    tenant_id=TENANT_A_ID, admin_user_id=7, ttl_seconds=-10
                )
                with pytest.raises(guild_oauth_state.StateError):
                    await guild_oauth_state.verify_and_consume_state(state)

    async def test_replayed_state_rejected(self, state_app: Quart) -> None:
        async with state_app.app_context():
            fake_redis = _FakeRedis()
            with patch.object(guild_oauth_state, "_redis_client", return_value=fake_redis):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=7)
                await guild_oauth_state.verify_and_consume_state(state)
                with pytest.raises(guild_oauth_state.StateError):
                    await guild_oauth_state.verify_and_consume_state(state)

    async def test_missing_secret_env_fails_closed(
        self, state_app: Quart, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GUILD_OAUTH_STATE_SECRET", raising=False)
        with pytest.raises(guild_oauth_state.StateError):
            guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=7)

    async def test_empty_state_rejected(self, state_app: Quart) -> None:
        async with state_app.app_context():
            with pytest.raises(guild_oauth_state.StateError):
                await guild_oauth_state.verify_and_consume_state("")

    async def test_state_with_no_dot_rejected(self, state_app: Quart) -> None:
        async with state_app.app_context():
            with pytest.raises(guild_oauth_state.StateError):
                await guild_oauth_state.verify_and_consume_state("no-dot-here")

    async def test_state_with_bad_base64_rejected(self, state_app: Quart) -> None:
        async with state_app.app_context():
            with pytest.raises(guild_oauth_state.StateError):
                await guild_oauth_state.verify_and_consume_state("not-valid-base64!!!.deadbeef")

    async def test_redis_failure_rejects_consume(self, state_app: Quart) -> None:
        class _BrokenRedis:
            async def set(self, *args: Any, **kwargs: Any) -> bool:
                raise ConnectionError("redis down")

        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_BrokenRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=7)
                with pytest.raises(guild_oauth_state.StateError):
                    await guild_oauth_state.verify_and_consume_state(state)


# ===== OAuth install flow =====


class TestOAuthInstallFlow:
    async def test_build_authorize_url_requires_configured_app(
        self, oauth_install_dal: Any
    ) -> None:
        with pytest.raises(ApiError) as exc_info:
            await build_authorize_url(
                oauth_install_dal,
                tenant_id=TENANT_A_ID,
                admin_user_id=1,
                redirect_uri="https://hub.example.com/callback",
                permissions=8,
            )
        assert exc_info.value.status_code == 404

    async def test_build_authorize_url_uses_tenants_own_app(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                url = await build_authorize_url(
                    oauth_install_dal,
                    tenant_id=TENANT_A_ID,
                    admin_user_id=1,
                    redirect_uri="https://hub.example.com/callback",
                    permissions=8,
                )
        assert "client_id=tenant-a-app-id" in url
        assert "scope=bot" in url

    async def test_complete_install_activates_pairing(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with patch(
                    "services.guild_oauth_install_service._exchange_code",
                    new=AsyncMock(return_value={"access_token": "irrelevant"}),
                ):
                    result = await complete_install(
                        oauth_install_dal,
                        state=state,
                        code="fake-code",
                        guild_id="guild-999",
                        permissions=104324673,
                        redirect_uri="https://hub.example.com/callback",
                    )
        assert result.guild_id == "guild-999"
        assert result.tenant_id == TENANT_A_ID

        row = (
            await oauth_install_dal(
                oauth_install_dal.guild_tenant_pairings.guild_id == "guild-999"
            ).select()
        ).first()
        assert row.status == "active"

    async def test_complete_install_rejects_missing_guild_id(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with pytest.raises(ApiError):
                    await complete_install(
                        oauth_install_dal,
                        state=state,
                        code="fake-code",
                        guild_id="",
                        permissions=8,
                        redirect_uri="https://hub.example.com/callback",
                    )

    async def test_complete_install_fails_closed_when_tenant_app_removed_mid_flow(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        """A state minted while configured, but the app is gone by callback time -> fail closed."""
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with pytest.raises(OAuthInstallError):
                    await complete_install(
                        oauth_install_dal,
                        state=state,
                        code="fake-code",
                        guild_id="guild-999",
                        permissions=8,
                        redirect_uri="https://hub.example.com/callback",
                    )

    async def test_exchange_code_network_error_raises(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with patch(
                    "httpx.AsyncClient.post",
                    new=AsyncMock(side_effect=__import__("httpx").ConnectError("boom")),
                ):
                    with pytest.raises(OAuthInstallError):
                        await complete_install(
                            oauth_install_dal,
                            state=state,
                            code="fake-code",
                            guild_id="guild-999",
                            permissions=8,
                            redirect_uri="https://hub.example.com/callback",
                        )

    async def test_exchange_code_non_2xx_raises(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        fake_response = AsyncMock()
        fake_response.status_code = 400
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)):
                    with pytest.raises(OAuthInstallError):
                        await complete_install(
                            oauth_install_dal,
                            state=state,
                            code="fake-code",
                            guild_id="guild-999",
                            permissions=8,
                            redirect_uri="https://hub.example.com/callback",
                        )

    async def test_exchange_code_malformed_json_raises(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: (_ for _ in ()).throw(ValueError("bad json"))
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)):
                    with pytest.raises(OAuthInstallError):
                        await complete_install(
                            oauth_install_dal,
                            state=state,
                            code="fake-code",
                            guild_id="guild-999",
                            permissions=8,
                            redirect_uri="https://hub.example.com/callback",
                        )

    async def test_complete_install_rejects_forged_state(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                with pytest.raises(OAuthInstallError):
                    await complete_install(
                        oauth_install_dal,
                        state="not-a-real-state.deadbeef",
                        code="fake-code",
                        guild_id="guild-999",
                        permissions=8,
                        redirect_uri="https://hub.example.com/callback",
                    )

    async def test_revoke_pairing_flips_status(
        self, oauth_install_dal: Any, state_app: Quart
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="tenant-a-app-id",
            client_secret="secret-a",
            bot_token="token-a",
        )
        async with state_app.app_context():
            with patch.object(guild_oauth_state, "_redis_client", return_value=_FakeRedis()):
                state = guild_oauth_state.mint_state(tenant_id=TENANT_A_ID, admin_user_id=1)
                with patch(
                    "services.guild_oauth_install_service._exchange_code",
                    new=AsyncMock(return_value={"access_token": "irrelevant"}),
                ):
                    await complete_install(
                        oauth_install_dal,
                        state=state,
                        code="fake-code",
                        guild_id="guild-999",
                        permissions=8,
                        redirect_uri="https://hub.example.com/callback",
                    )

        await revoke_pairing(
            oauth_install_dal,
            platform="discord",
            guild_id="guild-999",
            tenant_id=TENANT_A_ID,
            revoked_by="integration_removed",
            revoked_by_user_id=None,
        )
        row = (
            await oauth_install_dal(
                oauth_install_dal.guild_tenant_pairings.guild_id == "guild-999"
            ).select()
        ).first()
        assert row.status == "revoked"
        assert row.revoked_by == "integration_removed"

    async def test_revoke_pairing_404_when_no_active_pairing(self, oauth_install_dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            await revoke_pairing(
                oauth_install_dal,
                platform="discord",
                guild_id="no-such-guild",
                tenant_id=TENANT_A_ID,
                revoked_by="integration_removed",
                revoked_by_user_id=None,
            )
        assert exc_info.value.status_code == 404


# ===== Credential resolution: fail-closed, no fallback =====


class TestCredentialResolution:
    async def test_global_tenant_resolves_to_platform_bot(self, oauth_install_dal: Any) -> None:
        resolved = await resolve_credentials(oauth_install_dal, tenant_id=GLOBAL_TENANT_ID)
        assert resolved.is_platform_bot is True
        assert resolved.bot_token is None

    async def test_configured_tenant_resolves_own_token(self, oauth_install_dal: Any) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            application_id="app-a",
            client_secret="secret-a",
            bot_token="bot-token-a-value",
        )
        resolved = await resolve_credentials(oauth_install_dal, tenant_id=TENANT_A_ID)
        assert resolved.is_platform_bot is False
        assert resolved.bot_token == "bot-token-a-value"

    async def test_unconfigured_non_global_tenant_fails_closed_never_platform_bot(
        self, oauth_install_dal: Any
    ) -> None:
        """REGRESSION: a non-global tenant with no app must error, never fall back."""
        with pytest.raises(CredentialResolutionError) as exc_info:
            await resolve_credentials(oauth_install_dal, tenant_id=TENANT_B_ID)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "TENANT_APP_NOT_CONFIGURED"

    async def test_unknown_tenant_404s(self, oauth_install_dal: Any) -> None:
        with pytest.raises(CredentialResolutionError) as exc_info:
            await resolve_credentials(oauth_install_dal, tenant_id=99999)
        assert exc_info.value.status_code == 404


# ===== Cross-platform regressions (owner clarification 2026-09-29) =====
#
# Every tenant other than tenant 0 (global) ALWAYS needs its own app
# integration for EVERY platform, not only Discord. These parametrize
# across `SUPPORTED_PLATFORMS` to prove the fail-closed rule holds
# uniformly, not just for the Discord case the rest of this file exercises
# in depth.


class TestCrossPlatformRegression:
    def test_supported_platforms_match_connector_registry(self) -> None:
        assert set(CONNECTORS) == SUPPORTED_PLATFORMS

    @pytest.mark.parametrize("platform", sorted(SUPPORTED_PLATFORMS))
    async def test_non_global_tenant_without_app_fails_closed_never_platform_bot(
        self, oauth_install_dal: Any, platform: str
    ) -> None:
        """REGRESSION: for every platform, unconfigured non-global tenant errors, no fallback."""
        with pytest.raises(CredentialResolutionError) as exc_info:
            await resolve_credentials(oauth_install_dal, tenant_id=TENANT_B_ID, platform=platform)
        assert exc_info.value.status_code == 409
        assert exc_info.value.code == "TENANT_APP_NOT_CONFIGURED"

        with pytest.raises(ApiError) as get_exc_info:
            await get_masked_credentials(
                oauth_install_dal, tenant_id=TENANT_B_ID, platform=platform
            )
        assert get_exc_info.value.status_code == 404

    @pytest.mark.parametrize("platform", sorted(SUPPORTED_PLATFORMS))
    async def test_global_tenant_always_resolves_to_platform_bot(
        self, oauth_install_dal: Any, platform: str
    ) -> None:
        resolved = await resolve_credentials(
            oauth_install_dal, tenant_id=GLOBAL_TENANT_ID, platform=platform
        )
        assert resolved.is_platform_bot is True
        assert resolved.bot_token is None

    @pytest.mark.parametrize("platform", sorted(SUPPORTED_PLATFORMS))
    async def test_global_tenant_credentials_blocked_for_every_platform(
        self, oauth_install_dal: Any, platform: str
    ) -> None:
        with pytest.raises(ApiError) as exc_info:
            await set_credentials(
                oauth_install_dal,
                tenant_id=GLOBAL_TENANT_ID,
                actor_id=1,
                platform=platform,
                application_id="app-x",
                client_secret="secret-x",
            )
        assert exc_info.value.status_code == 409

    @pytest.mark.parametrize("platform", sorted(SUPPORTED_PLATFORMS))
    async def test_configured_tenant_resolves_for_every_platform(
        self, oauth_install_dal: Any, platform: str
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            platform=platform,
            application_id=f"app-{platform}",
            client_secret=f"secret-{platform}",
            bot_token=f"token-{platform}",
        )
        resolved = await resolve_credentials(
            oauth_install_dal, tenant_id=TENANT_A_ID, platform=platform
        )
        assert resolved.is_platform_bot is False
        assert resolved.bot_token == f"token-{platform}"

    async def test_unsupported_platform_rejected(self, oauth_install_dal: Any) -> None:
        with pytest.raises(ApiError) as exc_info:
            await set_credentials(
                oauth_install_dal,
                tenant_id=TENANT_A_ID,
                actor_id=1,
                platform="myspace",
                application_id="app-x",
                client_secret="secret-x",
            )
        assert exc_info.value.status_code == 400

    async def test_platform_credential_without_bot_token_is_storage_only(
        self, oauth_install_dal: Any
    ) -> None:
        """A platform whose app credential has no bot-token concept (client id/secret only)."""
        result = await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            platform="youtube",
            application_id="yt-app",
            client_secret="yt-secret",
        )
        assert result.bot_token_hint is None
        # `resolve_credentials()` is specifically the bot-token resolution
        # path (contract Sec2) -- a configured row with no bot-token
        # concept still fails closed there (never silently returns an
        # empty token); a platform with no bot token uses
        # `decrypt_client_credentials`/its own OAuth tokens instead.
        with pytest.raises(CredentialResolutionError):
            await resolve_credentials(oauth_install_dal, tenant_id=TENANT_A_ID, platform="youtube")

    async def test_tenant_a_and_b_each_get_independent_rows_per_platform(
        self, oauth_install_dal: Any
    ) -> None:
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_A_ID,
            actor_id=1,
            platform="twitch",
            application_id="a-twitch-app",
            client_secret="a-twitch-secret",
        )
        await set_credentials(
            oauth_install_dal,
            tenant_id=TENANT_B_ID,
            actor_id=1,
            platform="twitch",
            application_id="b-twitch-app",
            client_secret="b-twitch-secret",
        )
        a = await get_masked_credentials(
            oauth_install_dal, tenant_id=TENANT_A_ID, platform="twitch"
        )
        b = await get_masked_credentials(
            oauth_install_dal, tenant_id=TENANT_B_ID, platform="twitch"
        )
        assert a.application_id == "a-twitch-app"
        assert b.application_id == "b-twitch-app"


# ===== DiscordGuildAuthorityVerifier =====


class TestDiscordGuildAuthorityVerifier:
    async def test_manage_guild_bit_present_grants_authority(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: [{"id": "guild-1", "permissions": "32"}]
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            has_authority = await verifier.has_guild_authority(
                user_access_token="tok", guild_id="guild-1"
            )
        assert has_authority is True

    async def test_manage_guild_bit_absent_denies_authority(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: [{"id": "guild-1", "permissions": "0"}]
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            has_authority = await verifier.has_guild_authority(
                user_access_token="tok", guild_id="guild-1"
            )
        assert has_authority is False

    async def test_guild_not_in_list_denies_authority(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: []
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            has_authority = await verifier.has_guild_authority(
                user_access_token="tok", guild_id="guild-missing"
            )
        assert has_authority is False

    async def test_expired_token_denies_authority(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 401
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            has_authority = await verifier.has_guild_authority(
                user_access_token="expired", guild_id="guild-1"
            )
        assert has_authority is False

    async def test_empty_token_or_guild_denies_authority(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        assert await verifier.has_guild_authority(user_access_token="", guild_id="g") is False
        assert await verifier.has_guild_authority(user_access_token="t", guild_id="") is False

    async def test_network_error_raises(self) -> None:
        import httpx

        verifier = DiscordGuildAuthorityVerifier()
        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("boom"))):
            with pytest.raises(Exception):  # noqa: B017, PT011 - DiscordGuildAuthorityError
                await verifier.has_guild_authority(user_access_token="tok", guild_id="guild-1")

    async def test_non_2xx_raises(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 500
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            with pytest.raises(Exception):  # noqa: B017, PT011 - DiscordGuildAuthorityError
                await verifier.has_guild_authority(user_access_token="tok", guild_id="guild-1")

    async def test_malformed_json_raises(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: (_ for _ in ()).throw(ValueError("bad"))
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            with pytest.raises(Exception):  # noqa: B017, PT011 - DiscordGuildAuthorityError
                await verifier.has_guild_authority(user_access_token="tok", guild_id="guild-1")

    async def test_non_list_response_raises(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: {"not": "a list"}
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            with pytest.raises(Exception):  # noqa: B017, PT011 - DiscordGuildAuthorityError
                await verifier.has_guild_authority(user_access_token="tok", guild_id="guild-1")

    async def test_malformed_permissions_field_denies_authority(self) -> None:
        verifier = DiscordGuildAuthorityVerifier()
        fake_response = AsyncMock()
        fake_response.status_code = 200
        fake_response.json = lambda: [{"id": "guild-1", "permissions": "not-a-number"}]
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake_response)):
            has_authority = await verifier.has_guild_authority(
                user_access_token="tok", guild_id="guild-1"
            )
        assert has_authority is False


class TestPlatformOAuthConnectors:
    def test_get_connect_flow_status_discord_implemented(self) -> None:
        from services.platform_oauth_connectors import get_connect_flow_status

        status = get_connect_flow_status("discord")
        assert status.connect_flow_implemented is True

    def test_get_connect_flow_status_storage_only_platform(self) -> None:
        from services.platform_oauth_connectors import get_connect_flow_status

        status = get_connect_flow_status("twitch")
        assert status.storage_supported is True
        assert status.connect_flow_implemented is False

    def test_get_connect_flow_status_unknown_platform_raises(self) -> None:
        from services.platform_oauth_connectors import get_connect_flow_status

        with pytest.raises(ValueError, match="unknown platform"):
            get_connect_flow_status("myspace")
