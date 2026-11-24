"""Slash-command desired state (REST) + active-context service semantics: real blueprint + verifier.

Everything between the HTTP request and the (file-backed sqlite) database is real -- guard, ES256
issuer/verifier, registry service, SQLAlchemy Core queries. Postgres-only constraints are covered in
`test_app_commands_registry_pg_integration.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from quart import Quart
from quart_schema import QuartSchema
from sqlalchemy import insert

from blueprints.v1.app_commands_internal import (
    SCOPE_APP_COMMANDS,
    app_commands_internal_bp,
)
from services import app_command_registry as registry
from services.dataplane_scope import AmbiguousScopeError, InvalidSourceError, ScopeNotFoundError

INGEST = "spiffe://penguintech.io/alpha/svc-ingest"
TENANT_SCOPED = "spiffe://penguintech.io/alpha/svc-tenant"
NO_SCOPE = "spiffe://penguintech.io/alpha/svc-action"
GUILD = "474965105759748096"
BASE = "/api/v1/internal/dataplane"
# A scope the issuer may mint but the (now only) REST routes do not accept: wrong-scope probe.
OTHER_SCOPE = "dataplane:active-context"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _identity(service_id: str, scopes: set[str], tenant: str) -> ServiceIdentity:
    return ServiceIdentity(
        service_id=service_id,
        k8s_namespace="waddlebot",
        k8s_service_account=service_id.rsplit("/", 1)[-1],
        allowed_scopes=frozenset(scopes),
        tenant=tenant,
    )


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """Real issuer: svc-ingest (both scopes), a non-system tenant identity, a scope-less one."""
    private = ec.generate_private_key(ec.SECP256R1())
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    both = {SCOPE_APP_COMMANDS, OTHER_SCOPE}
    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={
            INGEST: _identity(INGEST, both, "system"),
            TENANT_SCOPED: _identity(TENANT_SCOPED, both, "acme"),
            NO_SCOPE: _identity(NO_SCOPE, {"connections:resolve"}, "system"),
        },
        audience="waddlebot-internal",
    )


@pytest.fixture
def app(install_dal: Any, issuer: ServiceJwtIssuer) -> Quart:
    """The real blueprint wired to the sqlite install DAL and the real verifier."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(app_commands_internal_bp)
    quart_app.config["install_dal"] = install_dal
    quart_app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    return quart_app


def _auth(issuer: ServiceJwtIssuer, scope: str, who: str = INGEST) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.issue(who, scope)}"}


async def _exec(dal: Any, table: str, **values: Any) -> Any:
    """Insert one row into a reflected table; return its id."""
    t = dal.metadata.tables[table]
    async with dal.engine.begin() as conn:
        result = await conn.execute(insert(t).values(**values))
        key = result.inserted_primary_key[0]
        return int(key) if isinstance(key, int) else key


#: public_id of every seeded entity, keyed ("tenants"|"communities"|"app", internal id / app_id).
PUB: dict[tuple[str, Any], str] = {}


@pytest.fixture(autouse=True)
def _reset_pub() -> None:
    """Each test gets a fresh database, so forget the previous test's public ids."""
    PUB.clear()


async def _tenant(dal: Any, slug: str, active: bool = True) -> int:
    pub = str(uuid.uuid4())
    tid = await _exec(dal, "tenants", slug=slug, display_name=slug, is_active=active, public_id=pub)
    PUB["tenants", tid] = pub
    return tid


async def _community(dal: Any, tenant_id: int, name: str, **kw: Any) -> int:
    pub = str(uuid.uuid4())
    cid = await _exec(
        dal,
        "communities",
        tenant_id=tenant_id,
        name=name,
        is_active=True,
        public_id=pub,
        **kw,
    )
    PUB["communities", cid] = pub
    return cid


async def _app(dal: Any, app_id: str) -> str:
    """Ensure an `app_catalog` row with a public_id exists; return that public id."""
    if ("app", app_id) not in PUB:
        pub = str(uuid.uuid4())
        await _exec(
            dal,
            "app_catalog",
            app_id=app_id,
            public_id=pub,
            name=app_id,
            manifest_version="3.0.0",
            module="core",
            feature="waddles.core.test",
            provider="builtin",
            execution_model="native",
            is_default=False,
            platform_compatibility='{"tested_with": "3.0.0"}',
            status="active",
            stages="{}",
        )
        PUB["app", app_id] = pub
    return PUB["app", app_id]


async def _link(dal: Any, community_id: int, source: str = GUILD, status: str = "approved") -> None:
    await _exec(
        dal,
        "community_servers",
        community_id=community_id,
        platform="discord",
        platform_server_id=source,
        status=status,
    )


async def _cmd(
    dal: Any,
    tenant_id: int,
    community_id: int,
    app_id: str,
    command: str,
    *,
    types: int = 2,
    platforms: tuple[str, ...] = ("discord",),
    description: str | None = None,
    at: datetime = T0,
) -> None:
    await _app(dal, app_id)
    await _exec(
        dal,
        "app_commands",
        tenant_id=tenant_id,
        community_id=community_id,
        app_id=app_id,
        command=command,
        command_types=types,
        platforms=list(platforms),
        description=description,
        registered_at=at,
    )


class World:
    """Ids of a seeded two-community guild."""

    tenant = 0
    first = 0
    second = 0


@pytest.fixture
async def world(install_dal: Any) -> World:
    """Tenant with two communities paired (approved) to GUILD; `first` linked before `second`."""
    w = World()
    w.tenant = await _tenant(install_dal, "slash-t")
    w.first = await _community(install_dal, w.tenant, "Alpha Crew")
    w.second = await _community(install_dal, w.tenant, "Beta Crew")
    await _link(install_dal, w.first)
    await _link(install_dal, w.second)
    return w


class TestDesired:
    async def test_union_webhook_only_with_builtin_and_owner_description(
        self, app: Quart, install_dal: Any, issuer: ServiceJwtIssuer, world: World
    ) -> None:
        d = install_dal
        await _cmd(d, world.tenant, world.first, "app.a", "ping", description="Ping!", at=T0)
        await _cmd(
            d,
            world.tenant,
            world.second,
            "app.b",
            "ping",
            description="other",
            at=T0 + timedelta(1),
        )
        await _cmd(d, world.tenant, world.second, "app.b", "roll", at=T0 + timedelta(2))
        await _cmd(
            d, world.tenant, 0, "app.c", "tenantwide", description="TW", at=T0 + timedelta(3)
        )
        await _cmd(d, world.tenant, world.first, "app.a", "chatonly", types=1)  # standard: excluded
        await _cmd(d, world.tenant, world.first, "app.a", "*", types=3)  # wildcard: excluded
        await _cmd(d, world.tenant, world.first, "app.a", "slackonly", platforms=("slack",))
        await _cmd(
            d, world.tenant, world.first, "app.a", "anyplat", platforms=("*",), at=T0 + timedelta(4)
        )
        await _cmd(d, world.tenant, world.first, "app.x", "context", at=T0)  # reserved: ignored
        response = await app.test_client().get(
            f"{BASE}/app-commands/desired?platform=discord&source_id={GUILD}",
            headers=_auth(issuer, SCOPE_APP_COMMANDS),
        )
        body = await response.get_json()
        assert response.status_code == 200, body
        names = [c["name"] for c in body["commands"]]
        assert names == ["context", "ping", "roll", "tenantwide", "anyplat"]
        by = {c["name"]: c for c in body["commands"]}
        assert by["ping"]["description"] == "Ping!"  # earliest registration owns the text
        assert by["ping"]["appPublicIds"] == [PUB["app", "app.a"], PUB["app", "app.b"]]
        assert by["ping"]["communityPublicIds"] == [
            PUB["communities", world.first],
            PUB["communities", world.second],
        ]
        assert by["tenantwide"]["communityPublicIds"] == []  # tenant-wide: no community ref
        assert by["roll"]["description"] == "Run the roll command"
        assert by["context"]["builtin"] is True
        assert body["tenantPublicId"] == PUB["tenants", world.tenant] and body["truncated"] is False
        assert response.headers["Cache-Control"] == "private, max-age=60"

    async def test_version_is_deterministic_and_tracks_payload_only(
        self, install_dal: Any, world: World
    ) -> None:
        await _cmd(install_dal, world.tenant, world.first, "app.a", "ping", description="x")
        one = await registry.desired_commands(install_dal, "discord", GUILD)
        two = await registry.desired_commands(install_dal, "discord", GUILD)
        assert one.version == two.version and one.version.startswith("sha256:")
        # A second bundle for the same command changes appIds but not the registered payload.
        await _cmd(
            install_dal,
            world.tenant,
            world.second,
            "app.b",
            "ping",
            description="y",
            at=T0 + timedelta(1),
        )
        three = await registry.desired_commands(install_dal, "discord", GUILD)
        assert three.version == one.version
        await _cmd(install_dal, world.tenant, world.first, "app.a", "new", description="n")
        assert (
            await registry.desired_commands(install_dal, "discord", GUILD)
        ).version != one.version

    async def test_if_version_short_circuits(
        self, app: Quart, install_dal: Any, issuer: ServiceJwtIssuer, world: World
    ) -> None:
        client = app.test_client()
        url = f"{BASE}/app-commands/desired?platform=discord&source_id={GUILD}"
        first = await (await client.get(url, headers=_auth(issuer, SCOPE_APP_COMMANDS))).get_json()
        again = await client.get(
            f"{url}&if_version={first['version']}", headers=_auth(issuer, SCOPE_APP_COMMANDS)
        )
        body = await again.get_json()
        assert body["changed"] is False and body["commands"] == []
        stale = await (
            await client.get(
                f"{url}&if_version=sha256:bad", headers=_auth(issuer, SCOPE_APP_COMMANDS)
            )
        ).get_json()
        assert stale["changed"] is True and stale["commands"]

    async def test_truncates_at_cap_and_flags_it(self, install_dal: Any, world: World) -> None:
        for i in range(registry.MAX_COMMANDS + 5):
            await _cmd(
                install_dal,
                world.tenant,
                world.first,
                "app.a",
                f"c{i}",
                at=T0 + timedelta(seconds=i),
            )
        state = await registry.desired_commands(install_dal, "discord", GUILD)
        assert state.truncated is True and len(state.commands) == registry.MAX_COMMANDS
        assert state.commands[0].name == "context"

    async def test_other_tenant_commands_never_leak(self, install_dal: Any, world: World) -> None:
        other = await _tenant(install_dal, "slash-other")
        await _cmd(install_dal, other, 0, "app.z", "secret")
        state = await registry.desired_commands(install_dal, "discord", GUILD)
        assert "secret" not in [c.name for c in state.commands]

    async def test_unknown_source_404_and_no_store(
        self, app: Quart, issuer: ServiceJwtIssuer, world: World
    ) -> None:
        response = await app.test_client().get(
            f"{BASE}/app-commands/desired?platform=discord&source_id=123",
            headers=_auth(issuer, SCOPE_APP_COMMANDS),
        )
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.parametrize(
        "query", ["platform=&source_id=1", "platform=discord&source_id=bad%20id", "source_id=1"]
    )
    async def test_invalid_input_400(
        self, app: Quart, issuer: ServiceJwtIssuer, query: str
    ) -> None:
        response = await app.test_client().get(
            f"{BASE}/app-commands/desired?{query}", headers=_auth(issuer, SCOPE_APP_COMMANDS)
        )
        assert response.status_code == 400

    async def test_cross_tenant_pairing_is_409(
        self, app: Quart, install_dal: Any, issuer: ServiceJwtIssuer, world: World
    ) -> None:
        other = await _tenant(install_dal, "slash-x")
        rogue = await _community(install_dal, other, "Rogue")
        await _link(install_dal, rogue)
        response = await app.test_client().get(
            f"{BASE}/app-commands/desired?platform=discord&source_id={GUILD}",
            headers=_auth(issuer, SCOPE_APP_COMMANDS),
        )
        assert response.status_code == 409

    async def test_legacy_primary_resolves_when_no_link(self, install_dal: Any) -> None:
        tid = await _tenant(install_dal, "legacy-t")
        cid = await _community(
            install_dal, tid, "Legacy", platform="discord", platform_server_id="777"
        )
        pair = await registry.paired_source(install_dal, "discord", "777")
        assert [c.public_id for c in pair.communities] == [PUB["communities", cid]]

    async def test_verified_link_beats_legacy_claim(self, install_dal: Any, world: World) -> None:
        await _community(
            install_dal, world.tenant, "Squatter", platform="discord", platform_server_id=GUILD
        )
        pair = await registry.paired_source(install_dal, "discord", GUILD)
        assert [c.name for c in pair.communities] == ["Alpha Crew", "Beta Crew"]

    async def test_pending_link_inactive_community_and_tenant_ignored(
        self, install_dal: Any
    ) -> None:
        tid = await _tenant(install_dal, "inactive-t")
        c1 = await _community(install_dal, tid, "Pending")
        await _link(install_dal, c1, "555", status="pending")
        with pytest.raises(ScopeNotFoundError):
            await registry.paired_source(install_dal, "discord", "555")
        c2 = await _exec(install_dal, "communities", tenant_id=tid, name="Off", is_active=False)
        await _link(install_dal, c2, "556")
        with pytest.raises(ScopeNotFoundError):  # tenant-inactive is covered on real Postgres
            await registry.paired_source(install_dal, "discord", "556")

    async def test_paired_source_rejects_invalid(self, install_dal: Any) -> None:
        with pytest.raises(InvalidSourceError):
            await registry.paired_source(install_dal, "Bad Platform!", "1")


class TestSources:
    async def test_pagination_and_filter(
        self, app: Quart, install_dal: Any, issuer: ServiceJwtIssuer, world: World
    ) -> None:
        for i in range(4):
            await _link(install_dal, world.first, f"90{i}")
        await _link(install_dal, world.first, "pend", status="pending")
        await _community(
            install_dal, world.tenant, "L", platform="discord", platform_server_id="legacy1"
        )
        client = app.test_client()
        headers = _auth(issuer, SCOPE_APP_COMMANDS)
        page1 = await (
            await client.get(
                f"{BASE}/app-commands/sources?platform=discord&limit=3", headers=headers
            )
        ).get_json()
        page2 = await (
            await client.get(
                f"{BASE}/app-commands/sources?platform=discord&limit=3&offset=3", headers=headers
            )
        ).get_json()
        every = page1["items"] + page2["items"]
        assert page1["total"] == 6 and len(page1["items"]) == 3
        assert sorted(every) == sorted([GUILD, "900", "901", "902", "903", "legacy1"])
        empty = await (
            await client.get(f"{BASE}/app-commands/sources?platform=twitch", headers=headers)
        ).get_json()
        assert empty == {"success": True, "items": [], "total": 0, "limit": 100, "offset": 0}

    @pytest.mark.parametrize(
        "query", ["limit=0", "limit=501", "limit=abc", "offset=-1", "offset=x"]
    )
    async def test_bad_page_args_400(
        self, app: Quart, issuer: ServiceJwtIssuer, query: str
    ) -> None:
        response = await app.test_client().get(
            f"{BASE}/app-commands/sources?platform=discord&{query}",
            headers=_auth(issuer, SCOPE_APP_COMMANDS),
        )
        assert response.status_code == 400


class TestActiveContextService:
    """Active-context semantics at the service layer (served to the data plane over gRPC)."""

    async def test_default_set_switch_clear_roundtrip(self, install_dal: Any, world: World) -> None:
        user = str(uuid.uuid4())
        default = await registry.get_active_context(install_dal, "discord", GUILD, user)
        assert default.community_public_id == PUB["communities", world.first]
        assert default.explicit is False
        assert [c.public_id for c in default.communities] == [
            PUB["communities", world.first],
            PUB["communities", world.second],
        ]
        assert [c.name for c in default.communities] == ["Alpha Crew", "Beta Crew"]
        second, first = PUB["communities", world.second], PUB["communities", world.first]
        switched = await registry.set_active_context(install_dal, "discord", GUILD, user, second)
        assert switched.community_public_id == second
        got = await registry.get_active_context(install_dal, "discord", GUILD, user)
        assert got.community_public_id == second and got.explicit is True
        by_id = await registry.set_active_context(
            install_dal, "discord", GUILD, user, first.upper()
        )
        assert by_id.community_public_id == first  # upsert, not duplicate
        assert await registry.clear_active_context(install_dal, "discord", GUILD, user) is True
        assert await registry.clear_active_context(install_dal, "discord", GUILD, user) is False
        final = await registry.get_active_context(install_dal, "discord", GUILD, user)
        assert final.explicit is False

    async def test_users_are_isolated(self, install_dal: Any, world: World) -> None:
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        await registry.set_active_context(
            install_dal, "discord", GUILD, a, PUB["communities", world.second]
        )
        other = await registry.get_active_context(install_dal, "discord", GUILD, b)
        assert other.community_public_id == PUB["communities", world.first]
        assert other.explicit is False

    async def test_unpaired_or_foreign_community_uuid_refused(
        self, install_dal: Any, world: World
    ) -> None:
        user = str(uuid.uuid4())
        with pytest.raises(registry.ContextNotFoundError):
            await registry.set_active_context(
                install_dal, "discord", GUILD, user, str(uuid.uuid4())
            )
        other = await _tenant(install_dal, "ctx-foreign")
        foreign = await _community(install_dal, other, "Foreign")
        with pytest.raises(registry.ContextNotFoundError):
            await registry.set_active_context(
                install_dal, "discord", GUILD, user, PUB["communities", foreign]
            )

    @pytest.mark.parametrize(
        "community", ["Alpha Crew", "alpha crew", "1", "999999", "", "x" * 300, "not-a-uuid"]
    )
    async def test_names_and_numeric_ids_never_resolve(
        self, install_dal: Any, world: World, community: str
    ) -> None:
        with pytest.raises(InvalidSourceError):
            await registry.set_active_context(
                install_dal, "discord", GUILD, str(uuid.uuid4()), community
            )

    async def test_stale_choice_falls_back_to_default(self, install_dal: Any, world: World) -> None:
        user = str(uuid.uuid4())
        await registry.set_active_context(
            install_dal, "discord", GUILD, user, PUB["communities", world.second]
        )
        t = install_dal.metadata.tables["community_servers"]
        from sqlalchemy import update

        async with install_dal.engine.begin() as conn:  # unpair Beta Crew
            await conn.execute(
                update(t).where(t.c.community_id == world.second).values(status="revoked")
            )
        ctx = await registry.get_active_context(install_dal, "discord", GUILD, user)
        assert ctx.community_public_id == PUB["communities", world.first]
        assert ctx.explicit is False

    @pytest.mark.parametrize("user", ["", "not-a-uuid", "12345"])
    async def test_invalid_user_refused(self, install_dal: Any, world: World, user: str) -> None:
        with pytest.raises(InvalidSourceError):
            await registry.get_active_context(install_dal, "discord", GUILD, user)
        with pytest.raises(InvalidSourceError):
            await registry.clear_active_context(install_dal, "discord", GUILD, user)

    async def test_unknown_source_refused(self, install_dal: Any, world: World) -> None:
        with pytest.raises(ScopeNotFoundError):
            await registry.get_active_context(install_dal, "discord", "1", str(uuid.uuid4()))

    async def test_cross_tenant_source_refused(self, install_dal: Any, world: World) -> None:
        other = await _tenant(install_dal, "ctx-x")
        await _link(install_dal, await _community(install_dal, other, "Rogue"))
        with pytest.raises(AmbiguousScopeError):
            await registry.get_active_context(install_dal, "discord", GUILD, str(uuid.uuid4()))


class TestAuth:
    ROUTES = [
        ("get", f"{BASE}/app-commands/sources?platform=discord", SCOPE_APP_COMMANDS),
        ("get", f"{BASE}/app-commands/desired?platform=discord&source_id=1", SCOPE_APP_COMMANDS),
    ]

    @pytest.mark.parametrize(("method", "url", "scope"), ROUTES)
    async def test_no_token_401(self, app: Quart, method: str, url: str, scope: str) -> None:
        assert (await getattr(app.test_client(), method)(url)).status_code == 401

    @pytest.mark.parametrize(("method", "url", "scope"), ROUTES)
    async def test_wrong_scope_401(
        self, app: Quart, issuer: ServiceJwtIssuer, method: str, url: str, scope: str
    ) -> None:
        other = OTHER_SCOPE
        response = await getattr(app.test_client(), method)(url, headers=_auth(issuer, other))
        assert response.status_code == 401

    @pytest.mark.parametrize(("method", "url", "scope"), ROUTES)
    async def test_non_system_tenant_403(
        self, app: Quart, issuer: ServiceJwtIssuer, method: str, url: str, scope: str
    ) -> None:
        response = await getattr(app.test_client(), method)(
            url, headers=_auth(issuer, scope, TENANT_SCOPED)
        )
        assert response.status_code == 403
        assert response.headers["Cache-Control"] == "no-store"
