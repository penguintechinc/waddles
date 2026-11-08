"""Kick provider: adapter + HTTP admin/data-plane API, real service/vault/store, only the socket faked.

Proves the hub-api requirements of the Kick host seams: the token is served as a kind-1
``accessToken`` with ``expiresAt`` and is refreshed BEFORE it expires; binding metadata carries
``chatroom_id`` / ``broadcaster_user_id`` / ``self_user_id`` (fetched from Kick at connect time); a
failed refresh surfaces as an explicit state (never a stale token); a second Kick connection in one
community is refused; secrets are ciphertext at rest and absent from every response and log line.
The real-Postgres twin (DB trigger, single flight, migrated schema) is ``test_kick_pg_integration``.
"""

from __future__ import annotations

import json
import logging
import socket
from datetime import timedelta
from typing import Any

import httpx
import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import (
    AdapterRegistry,
    Credentials,
    validate_metadata,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InsufficientScopes,
    InvalidConnectionInput,
    ProviderUnavailable,
    RefreshNotConfigured,
    RefreshRejected,
    ResourceUnresolvable,
)
from services.provider_connections.kick import KICK_SPEC, KickAdapter
from services.provider_connections.registry import build_default_registry
from tests.provider_connections.conftest import SYSTEM_SERVICE_ID, FakeClock, admin_headers
from tests.provider_connections.fake_kick import (
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    BOT_NAME,
    BOT_USER_ID,
    FakeKick,
)
from tests.provider_connections.support import open_row, raw_rows

INTERNAL = "/api/v1/internal"
CONNECTION_FIELDS = {
    "access",
    "capabilities",
    "createdAt",
    "enabled",
    "expiresAt",
    "hasRefreshToken",
    "id",
    "kind",
    "metadata",
    "provider",
    "publicId",
    "resourceType",
    "shared",
    "status",
    "statusReason",
    "updatedAt",
    "usable",
}
BINDING = {
    "chatroom_id": "9001",
    "broadcaster_user_id": "7001",
    "self_user_id": str(BOT_USER_ID),
}


@pytest.fixture(autouse=True)
def _kick_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.getaddrinfo

    def fake(host: str, *args: Any, **kwargs: Any) -> Any:
        if host in ("api.kick.com", "kick.com", "id.kick.com"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr("services.url_guard.socket.getaddrinfo", fake)


@pytest.fixture
def fake_kick(clock: FakeClock) -> FakeKick:
    """The fake Kick socket, anchored to the shared clock."""
    return FakeKick(clock)


def _adapter(fake: FakeKick, clock: FakeClock) -> KickAdapter:
    return KickAdapter(
        transport=fake.transport,
        app_credentials=lambda: (APP_CLIENT_ID, APP_CLIENT_SECRET),
        clock=clock,
    )


@pytest.fixture
def kclient(pc_app: Any, pc_db: Any, fake_kick: FakeKick, clock: FakeClock) -> Any:
    """Test client whose service serves Kick on the fake socket."""
    pc_app.config[SERVICE_CONFIG_KEY] = ProviderConnectionService(
        pc_db.install_dal,
        AdapterRegistry([_adapter(fake_kick, clock)]),
        PlatformKeyVault(),
        clock=clock,
    )
    return pc_app.test_client()


def _app_log(caplog: pytest.LogCaptureFixture) -> str:
    """Application/httpx log text only (the sqlite driver's own statement echo is not ours)."""
    return "\n".join(
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(("services", "blueprints", "httpx", "flask_core"))
    )


def _base(db: Any, community_id: int | None = None) -> str:
    return f"/api/v1/communities/{community_id or db.seed.community_id}/provider-connections"


def _body(tokens: tuple[str, str], slug: str = "streamer-one") -> str:
    return json.dumps(
        {
            "provider": "kick",
            "credentials": {"access_token": tokens[0], "refresh_token": tokens[1]},
            "metadata": {"channel_slug": slug},
        }
    )


async def _create(
    client: Any,
    db: Any,
    tokens: tuple[str, str],
    *,
    slug: str = "streamer-one",
    cid: int | None = None,
) -> Any:
    return await client.post(_base(db, cid), headers=admin_headers(db), data=_body(tokens, slug))


def _resolve_headers(service_issuer: ServiceJwtIssuer, community_id: int) -> dict[str, str]:
    token, _ = service_issuer.issue_with_claims(
        SYSTEM_SERVICE_ID, "connections:resolve", extra_claims={"community": community_id}
    )
    return {"Authorization": f"Bearer {token}"}


async def _resolve(
    client: Any, db: Any, service_issuer: ServiceJwtIssuer, connection_id: int
) -> Any:
    cid = db.seed.community_id
    return await client.post(
        f"{INTERNAL}/communities/{cid}/provider-connections/{connection_id}/resolve",
        headers=_resolve_headers(service_issuer, cid),
    )


class TestSpec:
    def test_descriptor_declares_one_per_community_and_the_binding_contract(self) -> None:
        assert KICK_SPEC.max_connections_per_community == 1
        assert KICK_SPEC.refreshable and KICK_SPEC.resource_type == "kick_channel"
        required = {k.wire for k in KICK_SPEC.binding_metadata if k.required}
        assert required == {"chatroom_id", "broadcaster_user_id", "self_user_id"}
        assert [f.name for f in KICK_SPEC.credential_fields] == ["access_token", "refresh_token"]
        assert all(f.required for f in KICK_SPEC.credential_fields)

    def test_default_registry_serves_kick(self) -> None:
        assert build_default_registry().get("kick").spec is KICK_SPEC

    def test_channel_slug_validation(self) -> None:
        assert validate_metadata(KICK_SPEC, {"channel_slug": " Streamer-One "}) == {
            "channel_slug": "streamer-one"
        }
        for bad in ({}, {"channel_slug": ""}, {"channel_slug": "a/b"}, {"channel_slug": "x" * 51}):
            with pytest.raises(InvalidConnectionInput):
                validate_metadata(KICK_SPEC, bad)
        # The three ids come from Kick, never from the admin.
        for forged in ("chatroom_id", "broadcaster_user_id", "self_user_id"):
            with pytest.raises(InvalidConnectionInput, match="unknown"):
                validate_metadata(KICK_SPEC, {"channel_slug": "ok", forged: "1"})

    def test_capabilities_follow_scopes(self) -> None:
        adapter = KickAdapter()
        assert adapter.granted_capabilities(["channel:read"]) == {"chat_ingest"}
        assert adapter.granted_capabilities(["chat:write"]) == {"action_relay"}
        assert adapter.granted_capabilities([]) == frozenset()


class TestAdapterConnectTimeLookup:
    async def test_validate_and_resolve_fetch_every_binding_id_from_kick(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        adapter = _adapter(fake_kick, clock)
        access, _ = fake_kick.mint()
        creds = Credentials(access)
        account = await adapter.validate_credentials(creds)
        assert (account.account_id, account.account_label) == (str(BOT_USER_ID), BOT_NAME)
        assert account.app_id == APP_CLIENT_ID and account.expires_at is not None
        assert account.expires_at == clock.now + timedelta(seconds=7200)
        resource = await adapter.resolve_resource(creds, account, {"channel_slug": "streamer-one"})
        assert resource.resource_id == "7001"
        assert resource.facts["chatroom_id"] == "9001"
        assert resource.facts["broadcaster_user_id"] == "7001"
        assert resource.facts["self_user_id"] == str(BOT_USER_ID)

    async def test_the_token_only_travels_in_headers(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        adapter = _adapter(fake_kick, clock)
        access, _ = fake_kick.mint()
        account = await adapter.validate_credentials(Credentials(access))
        await adapter.resolve_resource(
            Credentials(access), account, {"channel_slug": "streamer-one"}
        )
        assert fake_kick.requests
        for request in fake_kick.requests:
            assert access not in str(request.url) and access not in request.content.decode()
        # The unauthenticated chatroom lookup never carries the token at all.
        chat = [r for r in fake_kick.requests if r.url.host == "kick.com"]
        assert chat and all("authorization" not in r.headers for r in chat)

    async def test_inactive_token_is_rejected(self, fake_kick: FakeKick, clock: FakeClock) -> None:
        with pytest.raises(CredentialsRejected):
            await _adapter(fake_kick, clock).validate_credentials(Credentials("unknown-token-1"))

    async def test_missing_required_scope_is_refused(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        fake_kick.scopes = "user:read"
        access, _ = fake_kick.mint()
        with pytest.raises(InsufficientScopes, match="channel:read"):
            await _adapter(fake_kick, clock).validate_credentials(Credentials(access))

    async def test_app_token_without_a_user_is_rejected(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        fake_kick.app_token = True
        access, _ = fake_kick.mint()
        with pytest.raises(CredentialsRejected, match="user access token"):
            await _adapter(fake_kick, clock).validate_credentials(Credentials(access))

    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_introspect_outage_is_unavailable_not_a_pass(
        self, fake_kick: FakeKick, clock: FakeClock, status: int
    ) -> None:
        fake_kick.introspect_status = status
        access, _ = fake_kick.mint()
        with pytest.raises(ProviderUnavailable):
            await _adapter(fake_kick, clock).validate_credentials(Credentials(access))

    async def _resolve(self, fake: FakeKick, clock: FakeClock, slug: str) -> Any:
        adapter = _adapter(fake, clock)
        access, _ = fake.mint()
        account = await adapter.validate_credentials(Credentials(access))
        return await adapter.resolve_resource(Credentials(access), account, {"channel_slug": slug})

    async def test_unknown_channel(self, fake_kick: FakeKick, clock: FakeClock) -> None:
        with pytest.raises(ResourceUnresolvable, match="does not exist"):
            await self._resolve(fake_kick, clock, "nobody-here")

    async def test_channel_without_a_chatroom(self, fake_kick: FakeKick, clock: FakeClock) -> None:
        for override in (None, {}, {"id": "abc"}, {"id": 0}):
            fake_kick.chatroom_id_override = override
            with pytest.raises(ResourceUnresolvable, match="no chatroom"):
                await self._resolve(fake_kick, clock, "streamer-one")

    async def test_chatroom_404_is_unresolvable(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        fake_kick.chatroom_status = 404
        with pytest.raises(ResourceUnresolvable, match="no chatroom"):
            await self._resolve(fake_kick, clock, "streamer-one")

    async def test_inconsistent_owner_ids_are_refused(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        fake_kick.chatroom_user_id = 31337
        with pytest.raises(ResourceUnresolvable, match="inconsistent"):
            await self._resolve(fake_kick, clock, "streamer-one")

    async def test_lookup_outages_are_unavailable(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        fake_kick.channel_status = 500
        with pytest.raises(ProviderUnavailable):
            await self._resolve(fake_kick, clock, "streamer-one")
        fake_kick.channel_status = None
        fake_kick.chatroom_status = 502
        with pytest.raises(ProviderUnavailable):
            await self._resolve(fake_kick, clock, "streamer-one")

    async def test_transport_failure_and_oversize_are_unavailable(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        with pytest.raises(ProviderUnavailable):
            await KickAdapter(transport=httpx.MockTransport(boom)).validate_credentials(
                Credentials("tok-1234")
            )
        big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 300_000))
        with pytest.raises(ProviderUnavailable, match="large"):
            await KickAdapter(transport=big).validate_credentials(Credentials("tok-1234"))

    async def test_malformed_body_is_unavailable(self) -> None:
        bad = httpx.MockTransport(lambda r: httpx.Response(200, content=b"[1]"))
        with pytest.raises(ProviderUnavailable, match="malformed"):
            await KickAdapter(transport=bad).validate_credentials(Credentials("tok-1234"))

    async def test_blocked_by_the_ssrf_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "services.url_guard.socket.getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))],
        )
        with pytest.raises(ProviderUnavailable, match="network guard"):
            await KickAdapter().validate_credentials(Credentials("tok-1234"))

    def test_bearer_prefix_is_stripped(self) -> None:
        out = KickAdapter().normalize_credentials(Credentials("Bearer abc123", "r"))
        assert (out.access_token, out.refresh_token) == ("abc123", "r")


class TestAdapterRefresh:
    async def test_refresh_exchanges_and_rotates(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        _, refresh = fake_kick.mint()
        out = await _adapter(fake_kick, clock).refresh(refresh, {"client_id": APP_CLIENT_ID})
        assert out.access_token and out.refresh_token and out.refresh_token != refresh
        assert out.expires_at == clock.now + timedelta(seconds=7200)
        assert out.scopes == ("user:read", "channel:read", "chat:write")
        # Single-use: replaying the old refresh token is refused by Kick -> RefreshRejected.
        with pytest.raises(RefreshRejected):
            await _adapter(fake_kick, clock).refresh(refresh, {})

    async def test_rejected_unavailable_and_malformed_refresh(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        _, refresh = fake_kick.mint()
        adapter = _adapter(fake_kick, clock)
        for status in (400, 401):
            fake_kick.refresh_status = status
            with pytest.raises(RefreshRejected):
                await adapter.refresh(refresh, {})
        fake_kick.refresh_status = 503
        with pytest.raises(ProviderUnavailable):
            await adapter.refresh(refresh, {})
        fake_kick.refresh_status = None
        fake_kick.refresh_omit_expiry = True
        with pytest.raises(ProviderUnavailable, match="unexpected"):
            await adapter.refresh(refresh, {})

    async def test_unconfigured_or_foreign_app_is_refused_without_calling_kick(
        self, fake_kick: FakeKick, clock: FakeClock
    ) -> None:
        from services.oauth_providers import ProviderNotConfigured

        def unset() -> tuple[str, str]:
            raise ProviderNotConfigured("kick")

        none = KickAdapter(transport=fake_kick.transport, app_credentials=unset, clock=clock)
        with pytest.raises(RefreshNotConfigured):
            await none.refresh("r", {})
        with pytest.raises(RefreshNotConfigured, match="different Kick application"):
            await _adapter(fake_kick, clock).refresh("r", {"client_id": "someone-elses-app"})
        assert fake_kick.refresh_calls == 0


class TestConnectApi:
    async def test_create_returns_the_validated_dto_with_binding_metadata(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick, caplog: pytest.LogCaptureFixture
    ) -> None:
        tokens = fake_kick.mint()
        with caplog.at_level(logging.DEBUG):
            response = await _create(kclient, pc_db, tokens)
        assert response.status_code == 201
        body = await response.get_json()
        conn = body["connection"]
        assert set(conn) == CONNECTION_FIELDS  # exact field set: nothing extra can leak
        assert conn["provider"] == "kick" and conn["resourceType"] == "kick_channel"
        assert conn["usable"] is True and conn["hasRefreshToken"] is True
        assert conn["expiresAt"] is not None
        assert conn["capabilities"] == ["action_relay", "chat_ingest"]
        for key, value in BINDING.items():
            assert conn["metadata"][key] == value
        assert conn["metadata"]["channel_slug"] == "streamer-one"
        assert "client_id" not in conn["metadata"]
        text = json.dumps(body) + _app_log(caplog)
        for secret in (*tokens, APP_CLIENT_SECRET, "streamer-one-secret"):
            assert secret not in text
        assert "streamer-one" not in _app_log(caplog)  # the handle never reaches the log pipeline

    async def test_tokens_are_ciphertext_at_rest(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick
    ) -> None:
        tokens = fake_kick.mint()
        assert (await _create(kclient, pc_db, tokens)).status_code == 201
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert row["access_token"] != tokens[0] and row["refresh_token"] != tokens[1]
        assert tokens[0] not in str(row) and tokens[1] not in str(row)
        assert open_row(row) == tokens[0] and open_row(row, "refresh_token") == tokens[1]
        assert row["platform"] == "kick" and row["resource_id"] == "7001"

    async def test_tenant_comes_from_the_token_not_the_body(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick
    ) -> None:
        tokens = fake_kick.mint()
        body = json.loads(_body(tokens))
        body["tenantId"] = 999
        response = await kclient.post(
            _base(pc_db), headers=admin_headers(pc_db), data=json.dumps(body)
        )
        assert response.status_code in (201, 400)
        for row in await raw_rows(pc_db, "platform_connections"):
            assert row["tenant_id"] == pc_db.seed.tenant_id

    async def test_refresh_token_is_required(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick
    ) -> None:
        access, _ = fake_kick.mint()
        body = json.dumps(
            {
                "provider": "kick",
                "credentials": {"access_token": access},
                "metadata": {"channel_slug": "streamer-one"},
            }
        )
        response = await kclient.post(_base(pc_db), headers=admin_headers(pc_db), data=body)
        assert response.status_code == 400
        assert await raw_rows(pc_db, "platform_connections") == []

    async def test_kick_failures_surface_as_explicit_statuses(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick
    ) -> None:
        fake_kick.scopes = "user:read"
        tokens = fake_kick.mint()
        first = await _create(kclient, pc_db, tokens)
        assert first.status_code == 422
        fake_kick.scopes = "user:read channel:read"
        missing = await _create(kclient, pc_db, tokens, slug="nobody-here")
        assert missing.status_code == 422
        fake_kick.introspect_status = 503
        down = await _create(kclient, pc_db, tokens)
        assert down.status_code == 502
        assert await raw_rows(pc_db, "platform_connections") == []


class TestOnePerCommunity:
    async def test_second_connection_in_the_community_is_refused_with_409(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick
    ) -> None:
        assert (await _create(kclient, pc_db, fake_kick.mint())).status_code == 201
        calls_before = len(fake_kick.requests)
        duplicate = await _create(kclient, pc_db, fake_kick.mint(), slug="streamer_two")
        assert duplicate.status_code == 409
        assert (
            "maximum number of Kick connections" in (await duplicate.get_json())["error"]["message"]
        )
        assert len(await raw_rows(pc_db, "platform_connections")) == 1
        # The refusal is cheap: no Kick call was spent on the doomed request.
        assert len(fake_kick.requests) == calls_before

    async def test_a_different_community_may_have_its_own(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick
    ) -> None:
        assert (await _create(kclient, pc_db, fake_kick.mint())).status_code == 201
        other = await _create(
            kclient,
            pc_db,
            fake_kick.mint(),
            slug="streamer_two",
            cid=pc_db.seed.second_community_id,
        )
        assert other.status_code == 201

    async def test_descriptor_advertises_the_cap(self, kclient: Any, pc_db: Any) -> None:
        response = await kclient.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
        (kick,) = (await response.get_json())["providers"]
        assert kick["provider"] == "kick" and kick["maxPerCommunity"] == 1


class TestDataPlane:
    async def test_resolve_serves_kind1_token_expiry_and_binding_metadata(
        self, kclient: Any, pc_db: Any, fake_kick: FakeKick, service_issuer: ServiceJwtIssuer
    ) -> None:
        tokens = fake_kick.mint()
        cid = (await (await _create(kclient, pc_db, tokens)).get_json())["connection"]["id"]
        response = await _resolve(kclient, pc_db, service_issuer, cid)
        assert response.status_code == 200
        out = await response.get_json()
        assert out["kind"] == 1 and out["accessToken"] == tokens[0] and out["secret"] is None
        assert out["expiresAt"] is not None
        for key, value in BINDING.items():
            assert out["metadata"][key] == value
        assert fake_kick.refresh_calls == 0  # a fresh token is not rotated needlessly

    async def test_the_token_is_refreshed_before_it_expires(
        self,
        kclient: Any,
        pc_db: Any,
        fake_kick: FakeKick,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        tokens = fake_kick.mint()
        cid = (await (await _create(kclient, pc_db, tokens)).get_json())["connection"]["id"]
        first = await (await _resolve(kclient, pc_db, service_issuer, cid)).get_json()
        # 60 s before expiry: still unexpired on Kick's side, but inside the refresh skew.
        clock.advance(seconds=7200 - 60)
        assert clock.now < fake_kick.access[tokens[0]]
        response = await _resolve(kclient, pc_db, service_issuer, cid)
        assert response.status_code == 200
        out = await response.get_json()
        assert fake_kick.refresh_calls == 1
        assert out["accessToken"] != tokens[0] and out["accessToken"] in fake_kick.access
        assert out["expiresAt"] > first["expiresAt"]
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert open_row(row) == out["accessToken"]
        assert open_row(row, "refresh_token") in fake_kick.refresh_tokens  # rotated, re-sealed
        assert out["accessToken"] not in str(row)
        # And the rotated token is what subsequent resolves serve (no second exchange).
        again = await (await _resolve(kclient, pc_db, service_issuer, cid)).get_json()
        assert again["accessToken"] == out["accessToken"] and fake_kick.refresh_calls == 1

    async def test_a_rejected_refresh_surfaces_and_is_never_papered_over(
        self,
        kclient: Any,
        pc_db: Any,
        fake_kick: FakeKick,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tokens = fake_kick.mint()
        cid = (await (await _create(kclient, pc_db, tokens)).get_json())["connection"]["id"]
        clock.advance(seconds=7200 - 60)
        fake_kick.refresh_status = 400
        with caplog.at_level(logging.DEBUG):
            refused = await _resolve(kclient, pc_db, service_issuer, cid)
        assert refused.status_code == 409
        body = await refused.get_json()
        assert body["reason"] == "refresh_rejected"
        assert tokens[0] not in json.dumps(body) + _app_log(caplog)
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert row["status"] == "expired" and row["status_reason"] == "refresh_rejected"
        # A dead connection is refused on every later resolve; the old token is never served.
        fake_kick.refresh_status = None
        calls = fake_kick.refresh_calls
        later = await _resolve(kclient, pc_db, service_issuer, cid)
        assert later.status_code == 409
        assert fake_kick.refresh_calls == calls
        listing = await kclient.get(_base(pc_db), headers=admin_headers(pc_db))
        (item,) = (await listing.get_json())["connections"]
        assert item["status"] == "expired" and item["usable"] is False

    async def test_a_transient_refresh_failure_is_a_502_and_changes_nothing(
        self,
        kclient: Any,
        pc_db: Any,
        fake_kick: FakeKick,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
    ) -> None:
        tokens = fake_kick.mint()
        cid = (await (await _create(kclient, pc_db, tokens)).get_json())["connection"]["id"]
        clock.advance(seconds=7200 - 60)
        fake_kick.refresh_status = 503
        failed = await _resolve(kclient, pc_db, service_issuer, cid)
        assert failed.status_code == 502
        (row,) = await raw_rows(pc_db, "platform_connections")
        assert row["status"] == "active" and open_row(row) == tokens[0]
        # Kick recovers: the very next resolve refreshes and succeeds (the chain was not burnt).
        fake_kick.refresh_status = None
        ok = await _resolve(kclient, pc_db, service_issuer, cid)
        assert ok.status_code == 200 and (await ok.get_json())["accessToken"] != tokens[0]

    async def test_an_unrefreshable_platform_app_is_an_explicit_409(
        self,
        pc_app: Any,
        pc_db: Any,
        fake_kick: FakeKick,
        service_issuer: ServiceJwtIssuer,
        clock: FakeClock,
        kclient: Any,
    ) -> None:
        tokens = fake_kick.mint()
        cid = (await (await _create(kclient, pc_db, tokens)).get_json())["connection"]["id"]
        # The platform's configured Kick app changes: the stored token's app can no longer refresh.
        pc_app.config[SERVICE_CONFIG_KEY] = ProviderConnectionService(
            pc_db.install_dal,
            AdapterRegistry(
                [
                    KickAdapter(
                        transport=fake_kick.transport,
                        app_credentials=lambda: ("another-app", "secret"),
                        clock=clock,
                    )
                ]
            ),
            PlatformKeyVault(),
            clock=clock,
        )
        clock.advance(seconds=7200 - 60)
        refused = await _resolve(kclient, pc_db, service_issuer, cid)
        assert refused.status_code == 409
        assert (await refused.get_json())["reason"] == "refresh_not_configured"
        assert fake_kick.refresh_calls == 0
