"""Data-plane active-context against REAL Postgres: real blueprint, verifier, migrations, SQL.

The container is migrated to alembic head (so ``communities.public_id`` and
``user_active_contexts`` come from migration 0082, not a test-side definition). Nothing between the
HTTP request and the database is mocked. Contract: ``core/svc_ingest/src/active_context.rs``.
Skipped, never failed, where the ``docker`` CLI is unavailable (this repo's real-Postgres posture).
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from flask_core.database import AsyncDAL
from flask_core.service_jwt import ServiceIdentity, ServiceJwtIssuer, SigningKey
from quart import Quart
from quart_schema import QuartSchema

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from blueprints.v1.dataplane_active_context import (  # noqa: E402
    SCOPE_DATAPLANE_ACTIVE_CONTEXT,
    dataplane_active_context_bp,
)

INGEST_ID = "spiffe://penguintech.io/alpha/svc-ingest"
OTHER_ID = "spiffe://penguintech.io/alpha/svc-action"
TENANT_SCOPED_ID = "spiffe://penguintech.io/alpha/svc-tenant"
AUDIENCE = "waddlebot-internal"
GUILD = "474965105759748096"
USER = "7d9f1c52-3b8e-4f6a-9a4d-0c2b6e1f8a11"
URL = "/api/v1/internal/dataplane/active-context"
RESPONSE_FIELDS = {"communityPublicId", "communityId", "communityName", "explicit", "communities"}
COMMUNITY_FIELDS = {"publicId", "displayName", "position"}

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")


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
    """Real issuer: svc-ingest (holds the scope), svc-action (does not), a non-system tenant."""
    private = Ed25519PrivateKey.generate()
    key = SigningKey(kid="k1", private_key=private, public_key=private.public_key())
    return ServiceJwtIssuer(
        keys={"k1": key},
        active_kid="k1",
        identities={
            INGEST_ID: _identity(
                INGEST_ID, {SCOPE_DATAPLANE_ACTIVE_CONTEXT, "dataplane:resolve-scope"}, "system"
            ),
            OTHER_ID: _identity(OTHER_ID, {"connections:resolve"}, "system"),
            TENANT_SCOPED_ID: _identity(TENANT_SCOPED_ID, {SCOPE_DATAPLANE_ACTIVE_CONTEXT}, "acme"),
        },
        audience=AUDIENCE,
    )


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (includes 0082)."""
    with migrated_postgres("hub-api-active-context") as db:
        yield db


class Seed:
    """Ids the tests assert against."""

    first = 0  # first-registered community (numeric id)
    second = 0
    inactive = 0
    foreign = 0  # another tenant's community, also linked to the guild
    first_public = ""
    second_public = ""
    foreign_public = ""


async def _one(adal: AsyncDAL, sql: str, params: list[Any]) -> Any:
    rows = await adal.executesql_async(sql, params)
    return rows[0][0]


@pytest.fixture
async def db(pg_db: PgTestDatabase) -> AsyncIterator[AsyncDAL]:
    """Clean slate: one guild linked to two active communities, one inactive, one foreign."""
    adal = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    # The harness's minimal `communities` stands in for the legacy base schema (000); give it the
    # columns/table the resolver reads, exactly as 000_create_base_schema.sql defines them.
    for ddl in (
        "ALTER TABLE communities ADD COLUMN IF NOT EXISTS display_name VARCHAR(255)",
        "ALTER TABLE communities ADD COLUMN IF NOT EXISTS platform VARCHAR(50)",
        "ALTER TABLE communities ADD COLUMN IF NOT EXISTS platform_server_id VARCHAR(255)",
        "CREATE TABLE IF NOT EXISTS community_servers ("
        "id SERIAL PRIMARY KEY, "
        "community_id INTEGER NOT NULL REFERENCES communities(id) ON DELETE CASCADE, "
        "platform VARCHAR(50) NOT NULL, platform_server_id VARCHAR(255) NOT NULL, "
        "status VARCHAR(50) DEFAULT 'pending', is_primary BOOLEAN DEFAULT false, "
        "UNIQUE(community_id, platform, platform_server_id))",
    ):
        await adal.executesql_async(ddl, [])
    await adal.executesql_async(
        "TRUNCATE user_active_contexts, community_servers, communities, tenants "
        "RESTART IDENTITY CASCADE"
    )
    seed = Seed()
    tenant = int(await _one(adal, "INSERT INTO tenants (slug) VALUES ('t1') RETURNING id", []))
    other = int(await _one(adal, "INSERT INTO tenants (slug) VALUES ('t2') RETURNING id", []))

    async def community(name: str, tenant_id: int, active: bool = True) -> int:
        cid = int(
            await _one(
                adal,
                "INSERT INTO communities (name, tenant_id, is_active) VALUES (%s, %s, %s) "
                "RETURNING id",
                [name, tenant_id, active],
            )
        )
        await adal.executesql_async(
            "INSERT INTO community_servers (community_id, platform, platform_server_id, status) "
            "VALUES (%s, 'discord', %s, 'approved')",
            [cid, GUILD],
        )
        return cid

    seed.first = await community("Alpha Squad", tenant)
    seed.second = await community("Beta Squad", tenant)
    seed.inactive = await community("Dormant", tenant, active=False)
    seed.foreign = await community("Foreign", other)
    for attr, cid in (("first", seed.first), ("second", seed.second), ("foreign", seed.foreign)):
        public = await _one(adal, "SELECT public_id::text FROM communities WHERE id = %s", [cid])
        setattr(seed, f"{attr}_public", str(public))
    adal.seed = seed  # type: ignore[attr-defined]
    yield adal


@pytest.fixture
def app(db: AsyncDAL, issuer: ServiceJwtIssuer) -> Quart:
    """The real blueprint wired to the real DAL and the real verifier."""
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(dataplane_active_context_bp)
    quart_app.config["async_dal"] = db
    quart_app.config["dal"] = db.dal
    quart_app.config["SERVICE_JWT_VERIFIER"] = issuer.as_verifier()
    return quart_app


def _auth(issuer: ServiceJwtIssuer, service_id: str = INGEST_ID, scope: str | None = None) -> dict:
    token = issuer.issue(service_id, scope or SCOPE_DATAPLANE_ACTIVE_CONTEXT)
    return {"Authorization": f"Bearer {token}"}


def _get_url(user: str = USER, source: str = GUILD) -> str:
    return f"{URL}?platform=discord&source_id={source}&user={user}"


def _put_body(community: str, user: str = USER) -> dict[str, str]:
    return {"platform": "discord", "sourceId": GUILD, "user": user, "community": community}


async def _count(db: Any) -> int:
    rows = await db.executesql_async("SELECT count(*) FROM user_active_contexts", [])
    return int(rows[0][0])


class TestActiveContext:
    async def test_default_is_first_registered_with_numeric_community_id(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        response = await app.test_client().get(_get_url(), headers=_auth(issuer))
        assert response.status_code == 200
        body = await response.get_json()
        assert body["communityId"] == db.seed.first
        assert isinstance(body["communityId"], int) and not isinstance(body["communityId"], bool)
        assert body["communityPublicId"] == db.seed.first_public
        assert body["communityName"] == "Alpha Squad"
        assert body["explicit"] is False
        assert response.headers["Cache-Control"] == "no-store"

    async def test_exact_field_set_regression(self, app: Quart, issuer: ServiceJwtIssuer) -> None:
        body = await (await app.test_client().get(_get_url(), headers=_auth(issuer))).get_json()
        assert set(body) == RESPONSE_FIELDS
        assert body["communities"], "denominator: at least one community listed"
        for entry in body["communities"]:
            assert set(entry) == COMMUNITY_FIELDS
        put = await app.test_client().put(
            URL, json=_put_body(body["communities"][0]["publicId"]), headers=_auth(issuer)
        )
        assert set(await put.get_json()) == RESPONSE_FIELDS

    async def test_set_is_ordered_tenant_pinned_and_excludes_inactive(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        body = await (await app.test_client().get(_get_url(), headers=_auth(issuer))).get_json()
        assert [c["publicId"] for c in body["communities"]] == [
            db.seed.first_public,
            db.seed.second_public,
        ]
        assert [c["position"] for c in body["communities"]] == [0, 1]

    async def test_put_switches_and_get_honours_the_choice(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        client = app.test_client()
        put = await client.put(URL, json=_put_body(db.seed.second_public), headers=_auth(issuer))
        assert put.status_code == 200
        body = await put.get_json()
        assert (body["communityId"], body["explicit"]) == (db.seed.second, True)
        got = await (await client.get(_get_url(), headers=_auth(issuer))).get_json()
        assert (got["communityId"], got["explicit"]) == (db.seed.second, True)
        other = await (
            await client.get(_get_url(user=str(uuid.uuid4())), headers=_auth(issuer))
        ).get_json()
        assert (other["communityId"], other["explicit"]) == (db.seed.first, False)
        await client.put(URL, json=_put_body(db.seed.first_public), headers=_auth(issuer))
        assert await _count(db) == 1  # upsert, not a second row

    async def test_stored_choice_that_left_the_set_yields_to_default(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        client = app.test_client()
        await client.put(URL, json=_put_body(db.seed.second_public), headers=_auth(issuer))
        await db.executesql_async(
            "UPDATE communities SET is_active = FALSE WHERE id = %s", [db.seed.second]
        )
        got = await (await client.get(_get_url(), headers=_auth(issuer))).get_json()
        assert (got["communityId"], got["explicit"]) == (db.seed.first, False)

    @pytest.mark.parametrize("which", ["foreign_public", "unknown"])
    async def test_put_refuses_a_community_outside_the_set(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer, which: str
    ) -> None:
        target = str(uuid.uuid4()) if which == "unknown" else getattr(db.seed, which)
        response = await app.test_client().put(URL, json=_put_body(target), headers=_auth(issuer))
        assert response.status_code == 404
        assert await _count(db) == 0

    async def test_unlinked_source_is_404_no_store(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        response = await app.test_client().get(
            _get_url(source="999000111222333444"), headers=_auth(issuer)
        )
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.parametrize(
        "query",
        [
            f"platform=discord&source_id={GUILD}&user=not-a-uuid",
            f"platform=discord&source_id={GUILD}",
            f"platform=community&source_id=1&user={USER}",
            f"platform=Bad%20Platform&source_id={GUILD}&user={USER}",
        ],
    )
    async def test_invalid_parameters_are_400(
        self, app: Quart, issuer: ServiceJwtIssuer, query: str
    ) -> None:
        response = await app.test_client().get(f"{URL}?{query}", headers=_auth(issuer))
        assert response.status_code == 400

    async def test_put_rejects_malformed_community_and_body(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        client = app.test_client()
        bad = await client.put(URL, json=_put_body("Alpha Squad"), headers=_auth(issuer))
        assert bad.status_code == 400
        missing = await client.put(URL, json={"platform": "discord"}, headers=_auth(issuer))
        assert missing.status_code >= 400


class TestScopeRefusal:
    async def test_no_credential_is_401(self, app: Quart) -> None:
        client = app.test_client()
        assert (await client.get(_get_url())).status_code == 401
        assert (await client.put(URL, json=_put_body(str(uuid.uuid4())))).status_code == 401

    async def test_wrong_scope_is_refused_on_both_verbs(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        headers = _auth(issuer, OTHER_ID, "connections:resolve")
        client = app.test_client()
        assert (await client.get(_get_url(), headers=headers)).status_code == 401
        put = await client.put(URL, json=_put_body(str(uuid.uuid4())), headers=headers)
        assert put.status_code == 401

    async def test_resolve_scope_token_cannot_read_active_context(
        self, app: Quart, issuer: ServiceJwtIssuer
    ) -> None:
        response = await app.test_client().get(
            _get_url(), headers=_auth(issuer, INGEST_ID, "dataplane:resolve-scope")
        )
        assert response.status_code == 401

    async def test_non_system_tenant_is_403(self, app: Quart, issuer: ServiceJwtIssuer) -> None:
        response = await app.test_client().get(_get_url(), headers=_auth(issuer, TENANT_SCOPED_ID))
        assert response.status_code == 403

    async def test_unconfigured_verifier_is_503_never_open(self, app: Quart) -> None:
        app.config["SERVICE_JWT_VERIFIER"] = None
        assert (await app.test_client().get(_get_url())).status_code == 503


class TestMigration:
    async def test_every_community_has_a_unique_public_id(self, db: Any) -> None:
        rows = await db.executesql_async(
            "SELECT count(*), count(DISTINCT public_id) FROM communities", []
        )
        assert rows[0][0] >= 4 and rows[0][0] == rows[0][1]

    async def test_deleting_a_community_cascades_its_contexts(
        self, app: Quart, db: Any, issuer: ServiceJwtIssuer
    ) -> None:
        await app.test_client().put(
            URL, json=_put_body(db.seed.second_public), headers=_auth(issuer)
        )
        assert await _count(db) == 1
        await db.executesql_async("DELETE FROM communities WHERE id = %s", [db.seed.second])
        assert await _count(db) == 0
