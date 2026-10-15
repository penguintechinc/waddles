"""Data-plane scope resolver: real blueprint, real machine-JWT verifier, real pydal tables.

Everything between the HTTP request and the (file-backed sqlite) database is real -- the guard, the
Ed25519 issuer/verifier, the resolver and the schema-bound tables. Nothing is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.database import AsyncDAL
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

from blueprints.v1.dataplane_scope import (
    SCOPE_DATAPLANE_RESOLVE_SCOPE,
    dataplane_scope_internal_bp,
)
from services import dataplane_scope as scopes
from services.schema import bind_admin_tables, bind_auth_tables, bind_community_authz_tables
from tests.conftest import make_user_token

INGEST_ID = "spiffe://penguintech.io/alpha/svc-ingest"
OTHER_ID = "spiffe://penguintech.io/alpha/svc-action"
TENANT_SCOPED_ID = "spiffe://penguintech.io/alpha/svc-tenant"
AUDIENCE = "waddlebot-internal"
GUILD = "474965105759748096"
URL = "/api/v1/internal/dataplane/resolve-scope"


def _identity(service_id: str, scopes_: set[str], tenant: str) -> ServiceIdentity:
    return ServiceIdentity(
        service_id=service_id,
        k8s_namespace="waddlebot",
        k8s_service_account=service_id.rsplit("/", 1)[-1],
        allowed_scopes=frozenset(scopes_),
        tenant=tenant,
    )


@pytest.fixture
def issuer() -> ServiceJwtIssuer:
    """Real issuer: svc-ingest (resolver scope), svc-action (other scope), a non-system tenant."""
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={
            INGEST_ID: _identity(
                INGEST_ID, {SCOPE_DATAPLANE_RESOLVE_SCOPE, "connections:resolve"}, "system"
            ),
            OTHER_ID: _identity(OTHER_ID, {"connections:resolve"}, "system"),
            TENANT_SCOPED_ID: _identity(TENANT_SCOPED_ID, {SCOPE_DATAPLANE_RESOLVE_SCOPE}, "acme"),
        },
        audience=AUDIENCE,
    )


class Seed:
    """Ids the tests assert against."""

    demo_tenant = 0
    demo_community = 0
    other_tenant = 0
    other_community = 0


@pytest.fixture
async def db(tmp_path: Any) -> AsyncIterator[Any]:
    """File-backed pydal DAL: tenants, communities, community_servers, with a demo-like seed."""
    async_dal = AsyncDAL(f"sqlite://{tmp_path / 'scope.db'}", pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug", unique=True),
        Field("display_name"),
        Field("logo_url"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
    )
    bind_auth_tables(dal, migrate=True)
    bind_community_authz_tables(dal, migrate=True)
    bind_admin_tables(dal, migrate=True)
    seed = Seed()
    seed.demo_tenant = int(dal.tenants.insert(slug="global", display_name="Global"))
    seed.other_tenant = int(dal.tenants.insert(slug="other-corp", display_name="Other"))
    seed.demo_community = int(
        dal.communities.insert(
            name="waddlebot",
            tenant_id=seed.demo_tenant,
            platform="discord",
            platform_server_id=GUILD,
        )
    )
    seed.other_community = int(
        dal.communities.insert(name="other-main", tenant_id=seed.other_tenant)
    )
    dal.community_servers.insert(
        community_id=seed.demo_community,
        platform="discord",
        platform_server_id=GUILD,
        status="approved",
        is_primary=True,
    )
    dal.community_servers.insert(
        community_id=seed.demo_community,
        platform="twitch",
        platform_server_id="penguinzplays",
        status="approved",
        is_primary=False,
    )
    dal.commit()
    for name in dal.tables:
        dal(dal[name]).count()
    async_dal.seed = seed
    yield async_dal
    dal.close()


@pytest.fixture
def app(db: Any, issuer: ServiceJwtIssuer) -> Quart:
    """The real blueprint wired to the test DAL and the real verifier."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(dataplane_scope_internal_bp)
    quart_app.config["dal"] = db.dal
    quart_app.config["async_dal"] = db
    quart_app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    return quart_app


def _auth(issuer: ServiceJwtIssuer, service_id: str = INGEST_ID, scope: str | None = None) -> dict:
    token = issuer.issue(service_id, scope or SCOPE_DATAPLANE_RESOLVE_SCOPE)
    return {"Authorization": f"Bearer {token}"}


class TestResolve:
    async def test_demo_guild_resolves_to_its_numeric_ids(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert response.status_code == 200
        body = await response.get_json()
        assert body == {
            "success": True,
            "tenantId": db.seed.demo_tenant,
            "communityId": db.seed.demo_community,
            "tenantSlug": "global",
            "communityName": "waddlebot",
        }
        assert response.headers["Cache-Control"] == "private, max-age=30"

    async def test_demo_row_is_tenant_1_community_1_on_a_fresh_db(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # The demo seed (migration 095) is the first tenant/community of a fresh install.
        assert (db.seed.demo_tenant, db.seed.demo_community) == (1, 1)
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        body = await response.get_json()
        assert (body["tenantId"], body["communityId"]) == (1, 1)

    async def test_twitch_channel_resolves_through_community_servers(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # communities.platform_server_id only holds the Discord primary; the Twitch channel is
        # reachable ONLY through community_servers, and logins are case-insensitive.
        response = await app.test_client().get(
            f"{URL}?platform=Twitch&source_id=PenguinzPlays", headers=_auth(issuer)
        )
        assert response.status_code == 200
        assert (await response.get_json())["communityId"] == db.seed.demo_community

    async def test_community_without_a_link_row_resolves_via_its_legacy_primary(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        db.dal.communities.insert(
            name="legacy",
            tenant_id=db.seed.other_tenant,
            platform="discord",
            platform_server_id="999000111222333444",
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=999000111222333444", headers=_auth(issuer)
        )
        body = await response.get_json()
        assert response.status_code == 200 and body["tenantId"] == db.seed.other_tenant

    async def test_unknown_source_is_404_and_not_cacheable(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=1", headers=_auth(issuer)
        )
        assert response.status_code == 404
        assert (await response.get_json())["error"]["code"] == "SCOPE_NOT_FOUND"
        assert response.headers["Cache-Control"] == "no-store"

    async def test_unapproved_link_does_not_resolve(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        db.dal.community_servers.insert(
            community_id=db.seed.other_community,
            platform="discord",
            platform_server_id="555",
            status="pending",
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=555", headers=_auth(issuer)
        )
        assert response.status_code == 404

    async def test_inactive_community_does_not_resolve(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        db.dal(db.dal.communities.id == db.seed.demo_community).update(is_active=False)
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert response.status_code == 404

    async def test_community_pointing_at_a_missing_tenant_never_resolves(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        db.dal.communities.insert(name="orphan", tenant_id=424242)
        orphan = db.dal(db.dal.communities.name == "orphan").select().first().id
        db.dal.community_servers.insert(
            community_id=orphan, platform="discord", platform_server_id="777", status="approved"
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=777", headers=_auth(issuer)
        )
        assert response.status_code == 404

    async def test_same_tenant_two_communities_one_primary_picks_the_primary(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # Same-tenant primary disambiguation still works (regression for the cross-tenant fix).
        sibling = int(db.dal.communities.insert(name="sibling", tenant_id=db.seed.demo_tenant))
        db.dal.community_servers.insert(
            community_id=sibling,
            platform="discord",
            platform_server_id=GUILD,
            status="approved",
            is_primary=False,
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert (await response.get_json())["communityId"] == db.seed.demo_community

    async def test_cross_tenant_verified_links_are_409_even_with_one_primary(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # Security review of 5937b709: a source claimed by two tenants fails closed; primary flags
        # never adjudicate across tenants.
        db.dal.community_servers.insert(
            community_id=db.seed.other_community,
            platform="discord",
            platform_server_id=GUILD,
            status="approved",
            is_primary=False,
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert response.status_code == 409
        assert "communityId" not in await response.get_json()

    async def test_attacker_legacy_claim_cannot_hijack_a_verified_source(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # Attacker (tenant B) sets its own communities.platform_server_id to the victim's guild.
        # Victim has an approved link on a DIFFERENT community row without legacy claim.
        victim = int(db.dal.communities.insert(name="victim", tenant_id=db.seed.demo_tenant))
        db.dal.community_servers.insert(
            community_id=victim,
            platform="discord",
            platform_server_id="123123123",
            status="approved",
            is_primary=True,
        )
        db.dal.communities.insert(
            name="attacker",
            tenant_id=db.seed.other_tenant,
            platform="discord",
            platform_server_id="123123123",
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=123123123", headers=_auth(issuer)
        )
        body = await response.get_json()
        assert response.status_code == 200
        assert (body["tenantId"], body["communityId"]) == (db.seed.demo_tenant, victim)

    async def test_attacker_legacy_claim_against_verified_legacy_owner_is_409(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # Attacker copies the guild id of the demo community (verified link + legacy primary):
        # the attacker's legacy row must not be force-marked primary nor win -- the verified owner
        # is the only verified claim, so it resolves to the owner, never the attacker.
        attacker = int(
            db.dal.communities.insert(
                name="attacker",
                tenant_id=db.seed.other_tenant,
                platform="discord",
                platform_server_id=GUILD,
            )
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        body = await response.get_json()
        assert response.status_code == 200
        assert body["communityId"] == db.seed.demo_community != attacker
        assert body["tenantId"] == db.seed.demo_tenant

    async def test_inactive_verified_owner_does_not_fall_through_to_legacy_attacker(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        # Review of #867: deactivating the verified owner must not hand the source to a legacy
        # claimant in another tenant.
        victim = int(db.dal.communities.insert(name="victim", tenant_id=db.seed.demo_tenant))
        db.dal.communities.insert(
            name="attacker",
            tenant_id=db.seed.other_tenant,
            platform="discord",
            platform_server_id="31313131",
        )
        db.dal.community_servers.insert(
            community_id=victim,
            platform="discord",
            platform_server_id="31313131",
            status="approved",
            is_primary=True,
        )
        db.dal(db.dal.communities.id == victim).update(is_active=False)
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=31313131", headers=_auth(issuer)
        )
        assert response.status_code == 404
        assert "tenantId" not in await response.get_json()

    async def test_orphaned_verified_owner_does_not_fall_through_to_legacy_attacker(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        victim = int(db.dal.communities.insert(name="orphaned", tenant_id=None))
        db.dal.communities.insert(
            name="attacker",
            tenant_id=db.seed.other_tenant,
            platform="discord",
            platform_server_id="32323232",
        )
        db.dal.community_servers.insert(
            community_id=victim,
            platform="discord",
            platform_server_id="32323232",
            status="approved",
            is_primary=True,
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=32323232", headers=_auth(issuer)
        )
        assert response.status_code == 404

    async def test_same_tenant_verified_wins_and_legacy_never_tie_breaks(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        legacy_only = int(
            db.dal.communities.insert(
                name="legacy-sib",
                tenant_id=db.seed.demo_tenant,
                platform="discord",
                platform_server_id="33333333",
            )
        )
        verified_c = int(db.dal.communities.insert(name="ver-sib", tenant_id=db.seed.demo_tenant))
        db.dal.community_servers.insert(
            community_id=verified_c,
            platform="discord",
            platform_server_id="33333333",
            status="approved",
            is_primary=False,
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=33333333", headers=_auth(issuer)
        )
        body = await response.get_json()
        assert response.status_code == 200
        assert body["communityId"] == verified_c != legacy_only

    async def test_contested_legacy_only_claims_across_tenants_are_409(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        for name, tenant in (("a", db.seed.demo_tenant), ("b", db.seed.other_tenant)):
            db.dal.communities.insert(
                name=name, tenant_id=tenant, platform="discord", platform_server_id="42424242"
            )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id=42424242", headers=_auth(issuer)
        )
        assert response.status_code == 409

    async def test_inactive_tenant_does_not_resolve(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        db.dal(db.dal.tenants.id == db.seed.demo_tenant).update(is_active=False)
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert response.status_code == 404
        assert "tenantId" not in await response.get_json()

    async def test_guild_claimed_by_two_primaries_is_409_never_a_guess(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        sibling = int(db.dal.communities.insert(name="sibling2", tenant_id=db.seed.demo_tenant))
        db.dal.community_servers.insert(
            community_id=sibling,
            platform="discord",
            platform_server_id=GUILD,
            status="approved",
            is_primary=True,
        )
        db.dal.commit()
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert response.status_code == 409
        body = await response.get_json()
        assert body["error"]["code"] == "SCOPE_AMBIGUOUS"
        assert "communityId" not in body

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "platform=discord",
            "source_id=1",
            "platform=Disc%20ord&source_id=1",
            "platform=discord&source_id=a%20b",
            "platform=discord&source_id=" + "9" * 256,
            "platform=1discord&source_id=1",
            "platform=discord&source_id=%00",
        ],
    )
    async def test_malformed_input_is_400(
        self, app: Quart, issuer: ServiceJwtIssuer, query: str
    ) -> None:
        response = await app.test_client().get(f"{URL}?{query}", headers=_auth(issuer))
        assert response.status_code == 400


class TestAuth:
    async def test_no_bearer_is_401(self, app: Quart) -> None:
        client = app.test_client()
        assert (await client.get(f"{URL}?platform=discord&source_id={GUILD}")).status_code == 401
        response = await client.get(
            f"{URL}?platform=discord&source_id={GUILD}", headers={"Authorization": "Basic abc"}
        )
        assert response.status_code == 401

    async def test_garbage_token_is_401(self, app: Quart) -> None:
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}",
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert response.status_code == 401

    async def test_identity_not_allow_listed_for_the_scope_cannot_even_mint_it(
        self, issuer: ServiceJwtIssuer
    ) -> None:
        # svc-action only holds connections:resolve; the issuer refuses the resolver scope.
        with pytest.raises(Exception):  # noqa: B017 - issuer's refusal type is its own contract
            issuer.issue(OTHER_ID, SCOPE_DATAPLANE_RESOLVE_SCOPE)

    async def test_connections_resolve_token_does_not_open_the_resolver(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        # Least privilege: the sibling scope must not satisfy this route.
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}",
            headers=_auth(issuer, INGEST_ID, "connections:resolve"),
        )
        assert response.status_code == 401

    async def test_user_jwt_with_the_scope_string_is_rejected(self, app: Quart) -> None:
        token = make_user_token(user_id=1, scope=SCOPE_DATAPLANE_RESOLVE_SCOPE)
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    async def test_non_system_tenant_identity_is_403(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer, TENANT_SCOPED_ID)
        )
        assert response.status_code == 403

    async def test_missing_verifier_fails_closed_503(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        del app.config["SERVICE_JWT_VERIFIER"]
        response = await app.test_client().get(
            f"{URL}?platform=discord&source_id={GUILD}", headers=_auth(issuer)
        )
        assert response.status_code == 503

    async def test_batch_route_is_guarded_too(self, app: Quart) -> None:
        response = await app.test_client().post(
            f"{URL}/batch", json={"items": [{"platform": "discord", "sourceId": GUILD}]}
        )
        assert response.status_code == 401


class TestBatch:
    async def test_mixed_batch_reports_each_status_in_order(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        db.dal.community_servers.insert(
            community_id=db.seed.other_community,
            platform="discord",
            platform_server_id="888",
            status="approved",
            is_primary=True,
        )
        db.dal.community_servers.insert(
            community_id=db.seed.demo_community,
            platform="discord",
            platform_server_id="888",
            status="approved",
            is_primary=True,
        )
        db.dal.commit()
        response = await app.test_client().post(
            f"{URL}/batch",
            headers=_auth(issuer),
            json={
                "items": [
                    {"platform": "discord", "sourceId": GUILD},
                    {"platform": "discord", "sourceId": "404"},
                    {"platform": "discord", "sourceId": "888"},
                    {"platform": "discord", "sourceId": "bad id"},
                ]
            },
        )
        assert response.status_code == 200
        statuses = [r["status"] for r in (await response.get_json())["results"]]
        assert statuses == ["resolved", "not_found", "ambiguous", "invalid"]
        first = (await response.get_json())["results"][0]
        assert first["tenantId"] == db.seed.demo_tenant
        assert (await response.get_json())["results"][1]["tenantId"] is None

    @pytest.mark.parametrize("count", [0, scopes.MAX_BATCH + 1])
    async def test_batch_size_is_bounded(
        self, app: Quart, issuer: ServiceJwtIssuer, count: int
    ) -> None:
        response = await app.test_client().post(
            f"{URL}/batch",
            headers=_auth(issuer),
            json={"items": [{"platform": "discord", "sourceId": "1"}] * count},
        )
        assert response.status_code == 400


class TestNormalize:
    def test_twitch_source_is_folded_but_discord_is_exact(self) -> None:
        assert scopes.normalize("TWITCH", "ChanNel") == ("twitch", "channel")
        assert scopes.normalize("discord", "AbC") == ("discord", "AbC")

    @pytest.mark.parametrize("platform", ["", " ", "a b", "9x", "x" * 51])
    def test_bad_platform_rejected(self, platform: str) -> None:
        with pytest.raises(scopes.InvalidSourceError):
            scopes.normalize(platform, "1")

    def test_non_string_inputs_are_rejected(self) -> None:
        with pytest.raises(scopes.InvalidSourceError):
            scopes.normalize(None, "1")  # type: ignore[arg-type]
        with pytest.raises(scopes.InvalidSourceError):
            scopes.normalize("discord", None)  # type: ignore[arg-type]
