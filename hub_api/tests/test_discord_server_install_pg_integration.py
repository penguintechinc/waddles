"""Add-Discord-server flow + manual "pair by ID" on REAL Postgres (demo blocker).

Drives the real routes end to end against a migrated Postgres: authorize mints a browser-bound
state, the shared install callback consumes it, records the bot install in the existing
connection model (`platform_connections` + `community_connection_access`) and creates the guild
pairing with `direction='discord_to_twitch'`. Rejections: forged, replayed, foreign-browser
state; a `guild_id` query param that disagrees with Discord's token response. Manual pairing:
success, bot-not-in-guild, already-paired, wrong scope.

Only the Discord HTTP edge (token exchange, `GET /guilds/{id}`) and Redis are faked.
Skipped (never failed) where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from flask_core.database import AsyncDAL
from quart import Quart
from quart_schema import QuartSchema

import blueprints.v1.guild_pairing as pairing_bp_module
from blueprints.v1.guild_pairing import guild_pairing_bp
from blueprints.v1.tenant_discord_install import tenant_discord_install_bp
from config import HubAPIConfig
from services import discord_guild_roles as roles_svc
from services import discord_server_install as svc
from services import guild_ownership as ownership_svc
from services.credential_resolver import (
    PlatformCredentials,
    grant_community_connection_access,
    upsert_platform_connection,
)
from services.errors import ApiError
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(
    not pg.DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

WRITE = "community.guild_pairing:write"
GUILD = "323456789012345678"  # gitleaks:allow - fake snowflake
OTHER_GUILD = "423456789012345678"  # gitleaks:allow - fake snowflake
FRONTEND = "http://localhost:5173"
#: The installing admin's linked Discord account: owner of every guild the fake Discord reports.
ADMIN_DISCORD_ID = "523456789012345678"  # gitleaks:allow - fake snowflake


class FakeRedis:
    """Minimal async Redis (`set`/`get`/`getdel`) -- single-use semantics are what matter."""

    def __init__(self) -> None:
        """Empty store."""
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store `value`."""
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        """Read `key`."""
        return self.store.get(key)

    async def getdel(self, key: str) -> str | None:
        """Atomically read + delete `key`."""
        return self.store.pop(key, None)


class FakeResolver:
    """Resolves fixed (fake) Discord app credentials for any tenant."""

    async def resolve(self, dal: Any, **kwargs: Any) -> PlatformCredentials:
        """Return the fake app."""
        return PlatformCredentials(
            tenant_id=int(kwargs["tenant_id"]),
            platform="discord",
            payload={
                "client_id": "111",
                "client_secret": "fake-secret",  # gitleaks:allow - test value
                "bot_token": "fake-bot",  # gitleaks:allow - test value
            },
            source="tenant",
        )


def _config() -> HubAPIConfig:
    return HubAPIConfig(
        module_name="hub-api-test",
        module_version="0.0.0-test",
        module_port=8204,
        grpc_port=50204,
        database_url="sqlite:memory",
        database_read_replica_url=None,
        db_pool_size=1,
        db_max_retries=1,
        db_retry_delay=1,
        secret_key="change-me-in-production",
        jwt_algorithm="HS256",
        default_tenant_slug="default",
        posthog_api_key=None,
        posthog_host="https://license.penguintech.io",
        license_server_url="https://license.penguintech.io",
        identity_callback_base_url="http://localhost:8204",
        frontend_origin=FRONTEND,
        log_level="INFO",
    )


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres container for the module."""
    if not pg.DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with pg.migrated_postgres("hub-api-server-install-pg") as db:
        pg.prepare_database(db)
        yield db


@pytest.fixture
def adal(pg_db: pg.PgTestDatabase) -> Iterator[AsyncDAL]:
    """Per-test truncated DAL (connection-model tables included)."""
    gen = pg.fresh_dal(pg_db)
    async_dal = next(gen)
    async_dal.dal.executesql(
        "TRUNCATE community_connection_access, platform_connections RESTART IDENTITY CASCADE"
    )
    async_dal.dal.commit()
    yield async_dal
    try:
        next(gen)
    except StopIteration:
        pass


@pytest.fixture
def community_id(adal: AsyncDAL) -> int:
    """`acme-corp` (the `auth_headers` default tenant) + one community + the installing user."""
    dal = adal.dal
    tid = pg.scalar(
        dal,
        "INSERT INTO tenants (slug, display_name, is_global, is_active) "
        "VALUES ('acme-corp', 'Acme', false, true) RETURNING id",
    )
    pg.q(dal, "INSERT INTO hub_users (id, username) VALUES (1, 'admin1')")
    pg.q(
        dal,
        "INSERT INTO hub_user_identities (hub_user_id, platform, platform_user_id) "
        "VALUES (1, 'discord', %s)",
        [ADMIN_DISCORD_ID],
    )
    return int(
        pg.scalar(
            dal, "INSERT INTO communities (name, tenant_id) VALUES ('c1', %s) RETURNING id", [tid]
        )
    )


@pytest.fixture
def discord_edge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake Redis + resolver + Discord HTTP; `edge["guild"]` is what the token response reports."""
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
    edge: dict[str, Any] = {"guild": GUILD, "member_of": {GUILD, OTHER_GUILD}, "redis": FakeRedis()}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/token"):
            body = parse_qs(request.content.decode())
            assert body["client_secret"] == ["fake-secret"]
            return httpx.Response(
                200,
                json={
                    "access_token": "user-access",  # gitleaks:allow - test value
                    "refresh_token": "user-refresh",  # gitleaks:allow - test value
                    "guild": {"id": edge["guild"], "name": "Server B"},
                },
            )
        match = re.search(r"/guilds/(\d+)$", request.url.path)
        if match:
            if match.group(1) in edge["member_of"]:
                return httpx.Response(
                    200, json={"id": match.group(1), "owner_id": ADMIN_DISCORD_ID}
                )
            return httpx.Response(403, json={"code": 50001, "message": "Missing Access"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(**_kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=transport)

    fake_httpx = SimpleNamespace(
        AsyncClient=client_factory, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
    )
    monkeypatch.setattr(svc, "httpx", fake_httpx)
    monkeypatch.setattr(roles_svc, "httpx", fake_httpx)
    monkeypatch.setattr(svc, "validate_outbound_url", AsyncMock(return_value=None))
    monkeypatch.setattr(svc, "_install_redis_client", lambda: edge["redis"])
    monkeypatch.setattr(svc, "DefaultCredentialResolver", FakeResolver)
    monkeypatch.setattr(roles_svc, "DefaultCredentialResolver", FakeResolver)
    # Manual pairing also proves the actor administers the guild (role-sync S3,
    # `guild_ownership.verify_guild_control`): same fake tenant app + fake Discord HTTP, real code.
    monkeypatch.setattr(ownership_svc, "httpx", fake_httpx)
    monkeypatch.setattr(ownership_svc, "DefaultCredentialResolver", FakeResolver)
    return edge


@pytest.fixture
def client(adal: AsyncDAL, monkeypatch: pytest.MonkeyPatch, discord_edge: Any) -> Any:
    monkeypatch.setattr(pairing_bp_module, "feature_enabled", AsyncMock(return_value=True))
    app = Quart(__name__)
    QuartSchema(app)
    app.register_blueprint(guild_pairing_bp)
    app.register_blueprint(tenant_discord_install_bp)
    app.config["dal"] = adal.dal
    app.config["HUB_API_CONFIG"] = _config()
    return app.test_client()


async def _authorize(
    client: Any, auth_headers: Any, community_id: int, sync_enabled: bool = True
) -> tuple[str, str]:
    """Start the flow; returns `(state, binding nonce)`."""
    response = await client.post(
        f"/api/v1/communities/{community_id}/discord/server-install/authorize",
        headers={**auth_headers(scope=WRITE, user_id="1"), "Content-Type": "application/json"},
        data=json.dumps({"sync_enabled": sync_enabled}),
    )
    assert response.status_code == 200, await response.get_data(as_text=True)
    url = (await response.get_json())["authorize_url"]
    params = parse_qs(urlparse(url).query)
    assert params["client_id"] == ["111"]
    assert params["redirect_uri"] == ["http://localhost:8204/api/v1/discord/install/callback"]
    cookie = response.headers.get("Set-Cookie", "")
    nonce = re.search(r"__Host-dc_server_install=([^;]+)", cookie)
    assert nonce is not None
    return params["state"][0], nonce.group(1)


async def _callback(client: Any, state: str, nonce: str | None, **extra: str) -> Any:
    query = {"code": "abc", "state": state, **extra}
    headers = {"Cookie": f"__Host-dc_server_install={nonce}"} if nonce else {}
    qs = "&".join(f"{k}={v}" for k, v in query.items())
    return await client.get(f"/api/v1/discord/install/callback?{qs}", headers=headers)


def _grant(
    dal: Any, tenant_id: int, community_id: int, guild: str, status: str = "approved"
) -> int:
    """Seed an install record + access grant, exactly as the OAuth callback writes them."""
    conn = upsert_platform_connection(
        dal,
        tenant_id=tenant_id,
        platform="discord",
        resource_type="discord_guild",
        resource_id=guild,
        access_token="seeded",  # gitleaks:allow - test value
        refresh_token=None,
        installed_by_user_id=1,
    )
    grant_community_connection_access(
        dal, community_id=community_id, connection_id=conn, status=status
    )
    return conn


def _pairings(dal: Any, community_id: int) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in pg.q(
            dal,
            "SELECT discord_guild_id, direction, sync_enabled, created_by_user_id "
            "FROM guild_tenant_pairings WHERE community_id=%s ORDER BY id",
            [community_id],
        )
    ]


class TestServerInstallCallback:
    async def test_records_install_and_pairs_guild(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        dal = adal.dal
        state, nonce = await _authorize(client, auth_headers, community_id, sync_enabled=True)
        response = await _callback(client, state, nonce, guild_id=GUILD)
        assert response.status_code == 302
        location = response.headers["Location"]
        assert location.startswith(f"{FRONTEND}/admin/{community_id}/role-sync?")
        assert "discord_server=paired" in location and f"guild_id={GUILD}" in location

        install = pg.q(
            dal,
            "SELECT tenant_id, resource_type, resource_id, installed_by_user_id, created_at, "
            "access_token FROM platform_connections",
        )
        assert len(install) == 1
        tenant_id = pg.scalar(dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        assert install[0][:4] == (tenant_id, "discord_guild", GUILD, 1)
        assert install[0][4] is not None
        assert "user-access" not in str(install[0][5])  # sealed, never plaintext
        access = pg.q(dal, "SELECT community_id, status FROM community_connection_access")
        assert [tuple(r) for r in access] == [(community_id, "approved")]
        assert _pairings(dal, community_id) == [(GUILD, "discord_to_twitch", True, 1)]

    async def test_sync_disabled_choice_is_respected(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        state, nonce = await _authorize(client, auth_headers, community_id, sync_enabled=False)
        assert (await _callback(client, state, nonce)).status_code == 302
        assert _pairings(adal.dal, community_id) == [(GUILD, "discord_to_twitch", False, 1)]

    async def test_replayed_state_rejected(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        state, nonce = await _authorize(client, auth_headers, community_id)
        assert "paired" in (await _callback(client, state, nonce)).headers["Location"]
        replay = await _callback(client, state, nonce)
        assert replay.status_code == 302
        assert "reason=invalid_or_expired_state" in replay.headers["Location"]
        assert len(_pairings(adal.dal, community_id)) == 1

    async def test_forged_state_rejected_nothing_written(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        await _authorize(client, auth_headers, community_id)
        response = await _callback(client, "srv.forged-token", "whatever")
        assert "reason=invalid_or_expired_state" in response.headers["Location"]
        assert pg.scalar(adal.dal, "SELECT count(*) FROM platform_connections") == 0
        assert _pairings(adal.dal, community_id) == []

    async def test_foreign_browser_rejected_and_state_not_burned(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        state, nonce = await _authorize(client, auth_headers, community_id)
        foreign = await _callback(client, state, "attacker-nonce")
        assert "reason=state_binding_mismatch" in foreign.headers["Location"]
        assert _pairings(adal.dal, community_id) == []
        # The real admin's browser can still finish.
        assert "paired" in (await _callback(client, state, nonce)).headers["Location"]

    async def test_guild_param_mismatch_rejected(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        state, nonce = await _authorize(client, auth_headers, community_id)
        response = await _callback(client, state, nonce, guild_id=OTHER_GUILD)
        assert f"/admin/{community_id}/role-sync" in response.headers["Location"]
        assert "reason=install_failed" in response.headers["Location"]
        assert _pairings(adal.dal, community_id) == []

    async def test_cancelled_on_discord(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        state, nonce = await _authorize(client, auth_headers, community_id)
        response = await _callback(client, state, nonce, error="access_denied")
        assert "reason=cancelled" in response.headers["Location"]
        assert f"/admin/{community_id}/role-sync" in response.headers["Location"]
        assert _pairings(adal.dal, community_id) == []

    async def test_readding_paired_server_is_idempotent(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        for _ in range(2):
            state, nonce = await _authorize(client, auth_headers, community_id)
            assert "paired" in (await _callback(client, state, nonce)).headers["Location"]
        assert len(_pairings(adal.dal, community_id)) == 1
        assert pg.scalar(adal.dal, "SELECT count(*) FROM platform_connections") == 1

    async def test_authorize_requires_write_scope(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await client.post(
            f"/api/v1/communities/{community_id}/discord/server-install/authorize",
            headers={**auth_headers(scope="community.guild_pairing:read", user_id="1")},
            data="{}",
        )
        assert response.status_code == 403

    async def test_authorize_unconfigured_is_503(
        self, client: Any, auth_headers: Any, community_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Broken:
            async def resolve(self, dal: Any, **kwargs: Any) -> PlatformCredentials:
                from services.credential_resolver import TransportUnavailable

                raise TransportUnavailable("none")

        monkeypatch.setattr(svc, "DefaultCredentialResolver", Broken)
        response = await client.post(
            f"/api/v1/communities/{community_id}/discord/server-install/authorize",
            headers={**auth_headers(scope=WRITE, user_id="1"), "Content-Type": "application/json"},
            data="{}",
        )
        assert response.status_code == 503
        assert (await response.get_json())["error"]["code"] == "DISCORD_NOT_CONFIGURED"


class TestManualPairById:
    async def _post(
        self, client: Any, auth_headers: Any, cid: int, guild: str, scope: str = WRITE
    ) -> Any:
        return await client.post(
            f"/api/v1/communities/{cid}/guild-pairings",
            headers={**auth_headers(scope=scope, user_id="1"), "Content-Type": "application/json"},
            data=json.dumps({"discord_guild_id": guild, "sync_enabled": True}),
        )

    async def test_foreign_guild_bot_present_no_grant_is_403(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        response = await self._post(client, auth_headers, community_id, OTHER_GUILD)
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == "DISCORD_INSTALL_REQUIRED"
        assert _pairings(adal.dal, community_id) == []

    async def test_other_tenants_install_is_403(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        dal = adal.dal
        other_tid = pg.scalar(
            dal,
            "INSERT INTO tenants (slug, display_name, is_global, is_active) "
            "VALUES ('other-co', 'Other', false, true) RETURNING id",
        )
        other_cid = pg.scalar(
            dal,
            "INSERT INTO communities (name, tenant_id) VALUES ('c2', %s) RETURNING id",
            [other_tid],
        )
        _grant(dal, int(other_tid), int(other_cid), OTHER_GUILD)
        response = await self._post(client, auth_headers, community_id, OTHER_GUILD)
        assert response.status_code == 403
        assert _pairings(dal, community_id) == []

    async def test_pending_grant_is_403(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        tid = pg.scalar(adal.dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        _grant(adal.dal, int(tid), community_id, OTHER_GUILD, status="pending")
        response = await self._post(client, auth_headers, community_id, OTHER_GUILD)
        assert response.status_code == 403

    async def test_second_community_with_own_grant_is_ok(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        dal = adal.dal
        tid = int(pg.scalar(dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id]))
        second = int(
            pg.scalar(
                dal,
                "INSERT INTO communities (name, tenant_id) VALUES ('c3', %s) RETURNING id",
                [tid],
            )
        )
        conn = _grant(dal, tid, community_id, OTHER_GUILD)
        assert (
            await self._post(client, auth_headers, community_id, OTHER_GUILD)
        ).status_code == 201
        # Same guild, second community, no grant yet -> 403; with its own grant -> 201 (N:M).
        assert (await self._post(client, auth_headers, second, OTHER_GUILD)).status_code == 403
        grant_community_connection_access(
            dal, community_id=second, connection_id=conn, status="approved"
        )
        assert (await self._post(client, auth_headers, second, OTHER_GUILD)).status_code == 201

    async def test_pairs_when_granted(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        tid = pg.scalar(adal.dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        _grant(adal.dal, int(tid), community_id, OTHER_GUILD)
        response = await self._post(client, auth_headers, community_id, OTHER_GUILD)
        assert response.status_code == 201
        assert _pairings(adal.dal, community_id) == [(OTHER_GUILD, "discord_to_twitch", True, 1)]

    async def test_pairing_records_verified_ownership(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        tid = pg.scalar(adal.dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        _grant(adal.dal, int(tid), community_id, OTHER_GUILD)
        assert (
            await self._post(client, auth_headers, community_id, OTHER_GUILD)
        ).status_code == 201
        verified = pg.scalar(
            adal.dal,
            "SELECT ownership_verified_at IS NOT NULL FROM guild_tenant_pairings "
            "WHERE community_id=%s",
            [community_id],
        )
        assert verified is True

    async def test_actor_without_linked_discord_account_is_403(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        """An install grant alone is not enough: the actor must prove they administer the guild."""
        tid = pg.scalar(adal.dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        _grant(adal.dal, int(tid), community_id, OTHER_GUILD)
        pg.q(adal.dal, "DELETE FROM hub_user_identities WHERE hub_user_id=1")
        response = await self._post(client, auth_headers, community_id, OTHER_GUILD)
        assert response.status_code == 403
        assert (await response.get_json())["error"]["code"] == "guild_control_unlinked"
        assert _pairings(adal.dal, community_id) == []

    async def test_bot_not_in_guild(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int, discord_edge: Any
    ) -> None:
        discord_edge["member_of"] = set()
        tid = pg.scalar(adal.dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        _grant(adal.dal, int(tid), community_id, OTHER_GUILD)
        response = await self._post(client, auth_headers, community_id, OTHER_GUILD)
        assert response.status_code == 409
        assert (await response.get_json())["error"]["code"] == "DISCORD_BOT_NOT_IN_GUILD"
        assert _pairings(adal.dal, community_id) == []

    async def test_already_paired(
        self, client: Any, auth_headers: Any, adal: AsyncDAL, community_id: int
    ) -> None:
        tid = pg.scalar(adal.dal, "SELECT tenant_id FROM communities WHERE id=%s", [community_id])
        _grant(adal.dal, int(tid), community_id, GUILD)
        assert (await self._post(client, auth_headers, community_id, GUILD)).status_code == 201
        again = await self._post(client, auth_headers, community_id, GUILD)
        assert again.status_code == 409
        assert (await again.get_json())["error"]["code"] == "CONFLICT"

    async def test_permission_denied(
        self, client: Any, auth_headers: Any, community_id: int
    ) -> None:
        response = await self._post(
            client, auth_headers, community_id, GUILD, scope="community.guild_pairing:read"
        )
        assert response.status_code == 403

    async def test_bad_guild_id(self, client: Any, auth_headers: Any, community_id: int) -> None:
        assert (await self._post(client, auth_headers, community_id, "abc")).status_code == 400


def test_server_install_error_carries_community() -> None:
    err = svc.ServerInstallError("x", 400, "install_failed", 7)
    assert isinstance(err, ApiError) and err.community_id == 7
