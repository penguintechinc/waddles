"""Discord binding metadata (``guild_id`` + ``managed_roles``) end to end.

Real path: the HTTP routes, auth chain, DB-backed community-admin check, service, vault and store
are real; only the Discord socket is replaced (``httpx.MockTransport``) and DNS answers publicly so
the real SSRF guard still runs. Covers the validation edge cases, admin-only writes, and that the
non-secret metadata round-trips through the list / resolve endpoints the data plane uses (the
shapes core/svc_ingest ``outbound_creds_discord.rs`` parses).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from flask_core.service_jwt import ServiceJwtIssuer

from blueprints.v1.provider_connections import SERVICE_CONFIG_KEY
from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import AdapterRegistry, Credentials, validate_metadata
from services.provider_connections.discord import (
    DISCORD_SPEC,
    MAX_MANAGED_ROLES,
    DiscordAdapter,
    normalize_managed_roles,
)
from services.provider_connections.errors import (
    CredentialsRejected,
    InvalidConnectionInput,
    ProviderUnavailable,
    RefreshNotConfigured,
    ResourceUnresolvable,
)
from services.provider_connections.registry import build_default_registry
from tests.conftest import make_user_token
from tests.provider_connections.conftest import (
    SYSTEM_SERVICE_ID,
    FakeClock,
    admin_headers,
)

GUILD = "812345678901234567"
OTHER_GUILD = "812345678901234999"
ROLE_A = "912345678901234501"
ROLE_B = "912345678901234502"
BOT_TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.bot-token-do-not-log"  # noqa: S105 - fake
BOT_ID = "712345678901234567"
INTERNAL = "/api/v1/internal"


class FakeDiscord:
    """A tiny fake Discord REST API: one bot, a set of guilds it is a member of."""

    def __init__(self) -> None:
        """Start with the bot a member of :data:`GUILD` only."""
        self.guilds = {GUILD}
        self.is_bot = True
        self.status_override: int | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Serve ``/users/@me`` and ``/guilds/{id}`` for the one valid bot token."""
        self.requests.append(request)
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={"message": "x"})
        if request.headers.get("authorization") != f"Bot {BOT_TOKEN}":
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        path = request.url.path
        if path.endswith("/users/@me"):
            return httpx.Response(
                200, json={"id": BOT_ID, "username": "waddlebot", "bot": self.is_bot or None}
            )
        guild = path.rsplit("/", 1)[-1]
        if "/guilds/" in path and guild in self.guilds:
            return httpx.Response(200, json={"id": guild, "name": "g"})
        return httpx.Response(404, json={"message": "Unknown Guild"})

    @property
    def transport(self) -> httpx.MockTransport:
        """The socket replacement."""
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _discord_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    real = socket.getaddrinfo

    def fake(host: str, *args: Any, **kwargs: Any) -> Any:
        if host == "discord.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("162.159.135.232", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr("services.url_guard.socket.getaddrinfo", fake)


@pytest.fixture
def fake_discord() -> FakeDiscord:
    """The fake Discord socket."""
    return FakeDiscord()


@pytest.fixture
def discord_service(
    pc_db: Any, fake_discord: FakeDiscord, clock: FakeClock
) -> ProviderConnectionService:
    """The real service with the real Discord adapter on the fake socket."""
    return ProviderConnectionService(
        pc_db.install_dal,
        AdapterRegistry([DiscordAdapter(transport=fake_discord.transport, clock=clock)]),
        PlatformKeyVault(),
        clock=clock,
    )


@pytest.fixture
def dclient(pc_app: Any, discord_service: ProviderConnectionService) -> Any:
    """Test client whose service serves Discord."""
    pc_app.config[SERVICE_CONFIG_KEY] = discord_service
    return pc_app.test_client()


def _base(db: Any) -> str:
    return f"/api/v1/communities/{db.seed.community_id}/provider-connections"


def _create_body(metadata: dict[str, Any] | None = None, **overrides: Any) -> str:
    body: dict[str, Any] = {
        "provider": "discord",
        "credentials": {"access_token": BOT_TOKEN},
        "metadata": {"guild_id": GUILD} if metadata is None else metadata,
    }
    body.update(overrides)
    return json.dumps(body)


async def _post(client: Any, db: Any, metadata: dict[str, Any] | None = None, **kw: Any) -> Any:
    return await client.post(
        _base(db), headers=admin_headers(db), data=_create_body(metadata, **kw)
    )


def _bound(issuer: ServiceJwtIssuer, community_id: int) -> dict[str, str]:
    token, _ = issuer.issue_with_claims(
        SYSTEM_SERVICE_ID, "connections:resolve", extra_claims={"community": community_id}
    )
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ managed_roles normalization


class TestNormalizeManagedRoles:
    def test_object_becomes_sorted_canonical_wire_string(self) -> None:
        got = normalize_managed_roles("managed_roles", {"vip": ROLE_B, "mod_1": ROLE_A})
        assert got == f"mod_1:{ROLE_A},vip:{ROLE_B}"

    def test_canonical_string_round_trips(self) -> None:
        canonical = f"mod_1:{ROLE_A},vip:{ROLE_B}"
        assert normalize_managed_roles("managed_roles", canonical) == canonical

    @pytest.mark.parametrize("empty", [{}, "", "   "])
    def test_empty_means_no_roles(self, empty: Any) -> None:
        assert normalize_managed_roles("managed_roles", empty) == ""

    def test_exactly_max_entries_is_accepted_and_one_more_is_not(self) -> None:
        roles = {f"r{i}": str(10**17 + i) for i in range(MAX_MANAGED_ROLES)}
        assert normalize_managed_roles("managed_roles", roles).count(":") == MAX_MANAGED_ROLES
        roles["one_too_many"] = ROLE_A
        with pytest.raises(InvalidConnectionInput, match="too many"):
            normalize_managed_roles("managed_roles", roles)

    @pytest.mark.parametrize(
        "key",
        ["", "Mod", "has-dash", "has space", "1leading", "_lead", "ünï", "x" * 33, "a:b", "a,b"],
    )
    def test_bad_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(InvalidConnectionInput, match="invalid key"):
            normalize_managed_roles("managed_roles", {key: ROLE_A})

    @pytest.mark.parametrize(
        "role",
        [
            "",
            "abc",
            "123",
            "12345678901234567x",
            "1" * 21,
            "-912345678901234501",
            " 91234567890123450 ",
        ],
    )
    def test_non_numeric_or_missized_role_ids_are_rejected(self, role: str) -> None:
        with pytest.raises(InvalidConnectionInput, match="invalid role id"):
            normalize_managed_roles("managed_roles", {"vip": role})

    @pytest.mark.parametrize("role", [912345678901234501, 1.5, None, True, ["x"], {"a": "b"}])
    def test_non_string_role_ids_are_rejected_json_numbers_lose_precision(self, role: Any) -> None:
        with pytest.raises(InvalidConnectionInput, match="invalid role id"):
            normalize_managed_roles("managed_roles", {"vip": role})

    @pytest.mark.parametrize("raw", [42, True, ["a:1"], None])
    def test_wrong_container_type_is_rejected(self, raw: Any) -> None:
        with pytest.raises(InvalidConnectionInput, match="must be an object"):
            normalize_managed_roles("managed_roles", raw)

    @pytest.mark.parametrize("raw", ["vip", f"vip{ROLE_A}", f"vip:{ROLE_A},oops"])
    def test_malformed_string_form_is_rejected(self, raw: str) -> None:
        with pytest.raises(InvalidConnectionInput):
            normalize_managed_roles("managed_roles", raw)

    def test_duplicate_key_in_string_form_is_rejected(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="duplicate"):
            normalize_managed_roles("managed_roles", f"vip:{ROLE_A},vip:{ROLE_B}")

    def test_errors_never_echo_the_offending_value(self) -> None:
        with pytest.raises(InvalidConnectionInput) as err:
            normalize_managed_roles("managed_roles", {"SecretKey": ROLE_A})
        assert "SecretKey" not in str(err.value) and ROLE_A not in str(err.value)

    def test_every_accepted_key_satisfies_the_executors_role_key_rule(self) -> None:
        # core/svc_ingest is_role_key: [a-z][a-z0-9_-]{0,31}. Anything we accept must pass it.
        import re

        for key in ("a", "a" * 32, "mod_2", "z9_"):
            out = normalize_managed_roles("managed_roles", {key: ROLE_A})
            assert re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", out.split(":")[0])


class TestValidateMetadata:
    def test_guild_required_and_numeric(self) -> None:
        assert validate_metadata(DISCORD_SPEC, {"guild_id": GUILD}) == {"guild_id": GUILD}
        for bad in (
            {},
            {"guild_id": ""},
            {"guild_id": "not-numeric"},
            {"guild_id": "123"},
            {"guild_id": "1" * 21},
            {"guild_id": GUILD[:5] + "\n" + GUILD[5:]},
            {"guild_id": 812345678901234567},
        ):
            with pytest.raises(InvalidConnectionInput):
                validate_metadata(DISCORD_SPEC, bad)

    def test_unknown_metadata_keys_are_rejected(self) -> None:
        with pytest.raises(InvalidConnectionInput, match="unknown"):
            validate_metadata(DISCORD_SPEC, {"guild_id": GUILD, "bot_token": "x"})

    def test_roles_are_canonicalised_alongside_the_guild(self) -> None:
        got = validate_metadata(DISCORD_SPEC, {"guild_id": GUILD, "managed_roles": {"vip": ROLE_A}})
        assert got == {"guild_id": GUILD, "managed_roles": f"vip:{ROLE_A}"}

    def test_empty_roles_are_omitted(self) -> None:
        assert validate_metadata(DISCORD_SPEC, {"guild_id": GUILD, "managed_roles": {}}) == {
            "guild_id": GUILD
        }


# ------------------------------------------------------------------ adapter against the fake


class TestAdapter:
    @pytest.fixture
    def adapter(self, fake_discord: FakeDiscord) -> DiscordAdapter:
        return DiscordAdapter(transport=fake_discord.transport)

    async def test_validate_and_resolve(self, adapter: DiscordAdapter) -> None:
        creds = Credentials(BOT_TOKEN)
        account = await adapter.validate_credentials(creds)
        assert (account.account_id, account.expires_at, account.scopes) == (BOT_ID, None, ())
        resource = await adapter.resolve_resource(creds, account, {"guild_id": GUILD})
        assert resource.resource_id == GUILD
        assert resource.facts["guild_id"] == GUILD

    def test_bot_prefix_is_stripped_and_nothing_else(self, adapter: DiscordAdapter) -> None:
        assert (
            adapter.normalize_credentials(Credentials(f"Bot {BOT_TOKEN}")).access_token == BOT_TOKEN
        )
        assert adapter.normalize_credentials(Credentials(BOT_TOKEN)).access_token == BOT_TOKEN

    async def test_bad_token_is_rejected(self, adapter: DiscordAdapter) -> None:
        with pytest.raises(CredentialsRejected):
            await adapter.validate_credentials(Credentials("wrong"))

    async def test_a_user_token_is_not_a_bot(
        self, adapter: DiscordAdapter, fake_discord: FakeDiscord
    ) -> None:
        fake_discord.is_bot = False
        with pytest.raises(CredentialsRejected, match="bot user"):
            await adapter.validate_credentials(Credentials(BOT_TOKEN))

    async def test_guild_the_bot_is_not_in_is_unresolvable(self, adapter: DiscordAdapter) -> None:
        creds = Credentials(BOT_TOKEN)
        account = await adapter.validate_credentials(creds)
        with pytest.raises(ResourceUnresolvable):
            await adapter.resolve_resource(creds, account, {"guild_id": OTHER_GUILD})

    @pytest.mark.parametrize("status", [500, 502, 429])
    async def test_upstream_failures_are_unavailable(
        self, adapter: DiscordAdapter, fake_discord: FakeDiscord, status: int
    ) -> None:
        fake_discord.status_override = status
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(BOT_TOKEN))

    async def test_resolve_401_and_unexpected_status(
        self, adapter: DiscordAdapter, fake_discord: FakeDiscord
    ) -> None:
        creds = Credentials(BOT_TOKEN)
        account = await adapter.validate_credentials(creds)
        fake_discord.status_override = 401
        with pytest.raises(CredentialsRejected):
            await adapter.resolve_resource(creds, account, {"guild_id": GUILD})
        fake_discord.status_override = 500
        with pytest.raises(ProviderUnavailable):
            await adapter.resolve_resource(creds, account, {"guild_id": GUILD})

    async def test_transport_failure_is_unavailable(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        adapter = DiscordAdapter(transport=httpx.MockTransport(boom))
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(BOT_TOKEN))

    @pytest.mark.parametrize("body", [b"not json", b"[1]", b'{"id": 5, "bot": true}'])
    async def test_malformed_validate_body(self, body: bytes) -> None:
        adapter = DiscordAdapter(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
        )
        with pytest.raises(ProviderUnavailable):
            await adapter.validate_credentials(Credentials(BOT_TOKEN))

    async def test_oversized_and_wrong_guild_bodies(self, fake_discord: FakeDiscord) -> None:
        creds = Credentials(BOT_TOKEN)
        account = await DiscordAdapter(transport=fake_discord.transport).validate_credentials(creds)
        big = DiscordAdapter(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 300_000))
        )
        with pytest.raises(ProviderUnavailable, match="large"):
            await big.resolve_resource(creds, account, {"guild_id": GUILD})
        wrong = DiscordAdapter(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"id": OTHER_GUILD}))
        )
        with pytest.raises(ProviderUnavailable, match="unexpected guild"):
            await wrong.resolve_resource(creds, account, {"guild_id": GUILD})

    async def test_blocked_by_the_ssrf_guard(
        self, adapter: DiscordAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = socket.getaddrinfo

        def private(host: str, *a: Any, **k: Any) -> Any:
            if host == "discord.com":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]
            return real(host, *a, **k)

        monkeypatch.setattr("services.url_guard.socket.getaddrinfo", private)
        with pytest.raises(ProviderUnavailable, match="network guard"):
            await adapter.validate_credentials(Credentials(BOT_TOKEN))

    async def test_refresh_is_refused_loudly(self, adapter: DiscordAdapter) -> None:
        with pytest.raises(RefreshNotConfigured):
            await adapter.refresh("r", {})

    def test_capabilities(self, adapter: DiscordAdapter) -> None:
        assert adapter.granted_capabilities(()) == frozenset({"action_relay"})

    async def test_token_only_ever_travels_in_the_authorization_header(
        self, adapter: DiscordAdapter, fake_discord: FakeDiscord
    ) -> None:
        await adapter.validate_credentials(Credentials(BOT_TOKEN))
        (request,) = fake_discord.requests
        assert (
            BOT_TOKEN not in str(request.url) and BOT_TOKEN not in (request.content or b"").decode()
        )


def test_default_registry_serves_discord() -> None:
    spec = build_default_registry().get("discord").spec
    assert (spec.provider, spec.resource_type, spec.refreshable) == (
        "discord",
        "discord_guild",
        False,
    )


# ------------------------------------------------------------------ HTTP: admin-only + round trip


class TestHttpBinding:
    async def test_create_stores_and_returns_non_secret_binding_metadata(
        self, dclient: Any, pc_db: Any
    ) -> None:
        response = await _post(
            dclient, pc_db, {"guild_id": GUILD, "managed_roles": {"vip": ROLE_B, "mod": ROLE_A}}
        )
        assert response.status_code == 201
        meta = (await response.get_json())["connection"]["metadata"]
        assert meta["guild_id"] == GUILD
        assert meta["managed_roles"] == f"mod:{ROLE_A},vip:{ROLE_B}"
        assert BOT_TOKEN not in await response.get_data(as_text=True)

    @pytest.mark.parametrize(
        "metadata",
        [
            {"guild_id": "nope"},
            {"guild_id": GUILD, "managed_roles": {"Zq9Bad": ROLE_A}},
            {"guild_id": GUILD, "managed_roles": {"vip": "not-a-number"}},
            {"guild_id": GUILD, "managed_roles": {f"r{i}": ROLE_A for i in range(33)}},
            {"guild_id": GUILD, "managed_roles": 5},
            {"guild_id": GUILD, "extra": "x"},
            {},
        ],
    )
    async def test_invalid_metadata_is_a_400_and_stores_nothing(
        self, dclient: Any, pc_db: Any, metadata: dict[str, Any]
    ) -> None:
        response = await _post(dclient, pc_db, metadata)
        assert response.status_code == 400
        text = await response.get_data(as_text=True)
        assert BOT_TOKEN not in text and "Zq9Bad" not in text
        listed = await dclient.get(_base(pc_db), headers=admin_headers(pc_db))
        assert (await listed.get_json())["connections"] == []

    async def test_guild_the_bot_is_not_in_is_a_422(self, dclient: Any, pc_db: Any) -> None:
        response = await _post(dclient, pc_db, {"guild_id": OTHER_GUILD})
        assert response.status_code == 422

    async def test_only_a_community_admin_may_set_binding_metadata(
        self, dclient: Any, pc_db: Any
    ) -> None:
        # regression: discord-binding-admin-only -- scope alone is not enough
        pc_db.dal.hub_users.insert(username="member")
        pc_db.dal.commit()
        member = pc_db.dal(pc_db.dal.hub_users.username == "member").select().first().id
        pc_db.dal.community_members.insert(
            community_id=pc_db.seed.community_id, user_id=str(member), role="member", is_active=True
        )
        pc_db.dal.commit()
        body = _create_body({"guild_id": GUILD, "managed_roles": {"vip": ROLE_A}})
        headers = admin_headers(pc_db, user_id=member)
        assert (await dclient.post(_base(pc_db), headers=headers, data=body)).status_code == 403
        no_scope = admin_headers(pc_db, scope="*:read")
        assert (await dclient.post(_base(pc_db), headers=no_scope, data=body)).status_code == 403
        anon = await dclient.post(_base(pc_db), data=body)
        assert anon.status_code == 401
        # ...and an admin of a DIFFERENT tenant cannot touch this community's binding.
        other = {
            "Authorization": "Bearer "
            + make_user_token(user_id=member, scope="connections:admin", tenant="other-corp"),
            "Content-Type": "application/json",
        }
        assert (await dclient.post(_base(pc_db), headers=other, data=body)).status_code in (
            403,
            404,
        )
        listed = await dclient.get(_base(pc_db), headers=admin_headers(pc_db))
        assert (await listed.get_json())["connections"] == []

    async def test_patch_updates_roles_and_replaces_the_set(self, dclient: Any, pc_db: Any) -> None:
        created = await _post(dclient, pc_db, {"guild_id": GUILD, "managed_roles": {"vip": ROLE_A}})
        cid = (await created.get_json())["connection"]["id"]
        url = f"{_base(pc_db)}/{cid}"
        patched = await dclient.patch(
            url,
            headers=admin_headers(pc_db),
            data=json.dumps({"metadata": {"guild_id": GUILD, "managed_roles": {"mod": ROLE_B}}}),
        )
        assert patched.status_code == 200
        meta = (await patched.get_json())["connection"]["metadata"]
        assert meta["managed_roles"] == f"mod:{ROLE_B}"
        cleared = await dclient.patch(
            url,
            headers=admin_headers(pc_db),
            data=json.dumps({"metadata": {"guild_id": GUILD, "managed_roles": {}}}),
        )
        assert "managed_roles" not in (await cleared.get_json())["connection"]["metadata"]
        bad = await dclient.patch(
            url,
            headers=admin_headers(pc_db),
            data=json.dumps({"metadata": {"guild_id": GUILD, "managed_roles": {"vip": "x"}}}),
        )
        assert bad.status_code == 400
        # The failed update left the stored metadata untouched.
        again = await dclient.get(url, headers=admin_headers(pc_db))
        assert "managed_roles" not in (await again.get_json())["connection"]["metadata"]

    async def test_metadata_round_trips_through_list_and_resolve(
        self, dclient: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        created = await _post(
            dclient, pc_db, {"guild_id": GUILD, "managed_roles": {"vip": ROLE_B, "mod": ROLE_A}}
        )
        cid = (await created.get_json())["connection"]["id"]
        issuer_headers = {
            "Authorization": "Bearer " + service_issuer.issue(SYSTEM_SERVICE_ID, "connections:list")
        }
        listed = await dclient.get(
            f"{INTERNAL}/provider-connections?provider=discord", headers=issuer_headers
        )
        assert listed.status_code == 200
        (item,) = (await listed.get_json())["items"]
        expected = {"guild_id": GUILD, "managed_roles": f"mod:{ROLE_A},vip:{ROLE_B}"}
        assert item["metadata"] == expected
        assert (item["provider"], item["resourceType"], item["resourceId"]) == (
            "discord",
            "discord_guild",
            GUILD,
        )
        resolved = await dclient.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_bound(service_issuer, pc_db.seed.community_id),
        )
        assert resolved.status_code == 200
        out = await resolved.get_json()
        assert out["metadata"] == expected
        assert out["accessToken"] == BOT_TOKEN  # the bot token is the OAuthAccessToken (kind 1)
        assert out["kind"] == 1 and out["secret"] is None
        # the list never carries the token
        assert BOT_TOKEN not in await listed.get_data(as_text=True)

    async def test_a_binding_without_a_guild_is_not_listed_and_not_resolvable(
        self, dclient: Any, pc_db: Any, service_issuer: ServiceJwtIssuer
    ) -> None:
        from tests.provider_connections.support import raw_rows, set_column

        created = await _post(dclient, pc_db)
        cid = (await created.get_json())["connection"]["id"]
        (row,) = await raw_rows(pc_db, "platform_connections")
        await set_column(pc_db, "platform_connections", row["id"], resource_metadata="{}")
        headers = {
            "Authorization": "Bearer " + service_issuer.issue(SYSTEM_SERVICE_ID, "connections:list")
        }
        listed = await dclient.get(
            f"{INTERNAL}/provider-connections?provider=discord", headers=headers
        )
        assert (await listed.get_json())["items"] == []
        resolved = await dclient.post(
            f"{INTERNAL}/communities/{pc_db.seed.community_id}/provider-connections/{cid}/resolve",
            headers=_bound(service_issuer, pc_db.seed.community_id),
        )
        assert resolved.status_code == 409
        assert (await resolved.get_json())["reason"] == "metadata_incomplete"

    async def test_descriptor_advertises_the_discord_fields(self, dclient: Any, pc_db: Any) -> None:
        response = await dclient.get("/api/v1/connection-providers", headers=admin_headers(pc_db))
        (discord,) = (await response.get_json())["providers"]
        assert (discord["provider"], discord["refreshable"], discord["kind"]) == (
            "discord",
            False,
            1,
        )
        fields = {f["name"]: f for f in discord["metadataFields"]}
        assert fields["guild_id"]["required"] is True
        assert fields["managed_roles"]["required"] is False


_ = Callable  # keep the typing import explicit for readers of the fixtures above
