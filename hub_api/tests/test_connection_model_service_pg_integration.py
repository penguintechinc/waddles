"""Service-layer (`credential_resolver.py`) round-trip against a REAL, fully-migrated Postgres.

`alembic/tests/test_0035_connection_model_layers.py` already proves the
raw SQL schema (constraints, cascades, the compat view) against a real
Postgres container; this module proves the PYTHON service layer this PR
ported -- `store_tenant_credentials()`/`resolve()` (layer 1, now against
`tenant_platform_apps` directly, not the compat view) and the new layer
2/3 primitives (`upsert_platform_connection()`/`get_platform_connection()`/
`grant_community_connection_access()`/`resolve_community_connection()`) --
actually work against that same real schema via pydal, not sqlite's
looser type/constraint behavior, and that cross-tenant isolation holds
end-to-end (service layer + real DB), not just in the sqlite unit tests.

Reuses `alembic/tests/pg_docker.py`'s `migrated_postgres()` harness (one
real Postgres 17 container, migrated to `head` via the actual Alembic
chain) -- `alembic/tests` is added to `sys.path` directly since this
module lives under `hub_api/tests/`, a separate pytest rootdir with its
own `conftest.py`. Skipped (not failed) where the `docker` CLI is
unavailable, same convention every other real-Postgres test in this repo
uses (see `pg_docker.py`'s own module docstring).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydal import DAL, Field

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

# `pg_docker` is reached via the sys.path insert above, not a package mypy
# resolves statically -- hence the import-not-found ignore below.
from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.credential_resolver import (  # noqa: E402
    DefaultCredentialResolver,
    TransportUnavailable,
    get_platform_connection,
    grant_community_connection_access,
    resolve_community_connection,
    store_tenant_credentials,
    upsert_platform_connection,
)
from services.errors import ApiError  # noqa: E402
from services.schema import bind_bar_citizen_tables  # noqa: E402

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_KEY = "d4f9317783becee1a4415c1a1229b9258e7a90b768d72a9e2c7dc891af661df6"  # gitleaks:allow


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container, migrated to `head`, shared by every test below."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-connection-model-service") as db:
        yield db


@pytest.fixture
def dal(pg_db: PgTestDatabase) -> Iterator[Any]:
    """A real pydal `DAL` bound against the migrated container, `migrate=False` (schema is real).

    pydal's own URI scheme is `postgres://`, not `postgresql://` (`pg_db.
    dsn` is the latter -- the form `psycopg2.connect()`/`DATABASE_URL`
    expect) -- translated here, at the one seam that needs it.
    """
    pydal_uri = pg_db.dsn.replace("postgresql://", "postgres://", 1)
    connection = DAL(pydal_uri, pool_size=1)
    # `bind_auth_tables()` (called by `bind_bar_citizen_tables()`) assumes `tenants`/
    # `communities` are already bound by the caller -- same precondition
    # `tests/conftest.py::bar_citizen_db` satisfies for the sqlite fixture; mirrored here
    # against the real, already-migrated columns (`alembic/tests/pg_docker.py`'s own
    # bootstrap SQL -- see that module's docstring).
    connection.define_table(
        "tenants",
        Field("slug", "string", unique=True),
        Field("display_name", "string"),
        Field("logo_url", "string"),
        Field("is_global", "boolean", default=False),
        Field("is_active", "boolean", default=True),
        Field("config", "json"),
        migrate=False,
    )
    # `pg_docker.py`'s own bootstrap SQL only creates `communities(id, name)` --
    # the minimal columns migrations 0020+ reference by FK (see that module's own
    # docstring) -- not `bind_auth_tables()`'s full production column list. Add just
    # `tenant_id`, the one column this module's `grant_community_connection_access()`
    # tenant-match guard actually reads; `_seed_community()` below inserts via raw SQL
    # rather than `dal.communities.insert()`, which would otherwise try to populate
    # every OTHER defaulted column `bind_auth_tables()` defines for `communities`
    # (a much larger production column set, out of scope for this minimal schema).
    connection.executesql("ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
    bind_bar_citizen_tables(connection, migrate=False)
    yield connection
    connection.close()


def _seed_tenant(dal_: Any, *, slug: str, is_global: bool = False) -> int:
    tenant_id = int(dal_.tenants.insert(slug=slug, is_active=True, is_global=is_global))
    dal_.commit()
    return tenant_id


def _seed_community(dal_: Any, *, name: str, tenant_id: int) -> int:
    """Raw-SQL insert against the minimal bootstrap `communities` table.

    `dal_.communities.insert()` would try to populate every OTHER
    defaulted column `bind_auth_tables()` defines in production, which
    this minimal schema (`dal` fixture's own docstring) doesn't have.
    """
    row = dal_.executesql(
        "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
        placeholders=[name, tenant_id],
    )
    dal_.commit()
    return int(row[0][0])


@requires_docker
class TestLayer1AppCredentialsAgainstRealPostgres:
    """`store_tenant_credentials()`/`resolve()` against `tenant_platform_apps` directly."""

    async def test_store_then_resolve_round_trip(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="pg-svc-l1-1")
        store_tenant_credentials(
            dal,
            tenant_id=tenant_id,
            is_global_tenant=False,
            platform="discord",
            payload={"client_id": "cid-pg", "client_secret": "secret-pg", "bot_token": "bot-pg"},
            installed_by_user_id=None,
        )

        resolved = await DefaultCredentialResolver().resolve(
            dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
        )
        assert resolved.payload == {
            "client_id": "cid-pg",
            "client_secret": "secret-pg",
            "bot_token": "bot-pg",
        }

        # Ciphertext at rest never contains the plaintext secret.
        row = (
            dal(
                (dal.tenant_platform_apps.tenant_id == tenant_id)
                & (dal.tenant_platform_apps.platform == "discord")
            )
            .select()
            .first()
        )
        assert "secret-pg" not in row.credentials_ciphertext

    async def test_global_tenant_rejected_at_service_layer(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="pg-svc-l1-global", is_global=True)
        with pytest.raises(ApiError):
            store_tenant_credentials(
                dal,
                tenant_id=tenant_id,
                is_global_tenant=True,
                platform="discord",
                payload={"client_id": "x", "client_secret": "y"},
                installed_by_user_id=None,
            )


@requires_docker
class TestLayer2ConnectionsAgainstRealPostgres:
    """`upsert_platform_connection()`/`get_platform_connection()` against `platform_connections`."""

    def test_round_trip_decrypts_real_row(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="pg-svc-l2-1")
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="broadcaster-pg-1",
            access_token="tok-pg",
            refresh_token="ref-pg",
            installed_by_user_id=None,
        )
        assert connection_id > 0

        connection = get_platform_connection(
            dal, tenant_id=tenant_id, platform="twitch", resource_id="broadcaster-pg-1"
        )
        assert connection is not None
        assert connection.access_token == "tok-pg"
        assert connection.refresh_token == "ref-pg"

        row = dal(dal.platform_connections.id == connection_id).select().first()
        assert "tok-pg" not in row.access_token
        assert "ref-pg" not in row.refresh_token

    def test_cross_tenant_isolation_real_unique_constraint(self, dal: Any) -> None:
        """Two tenants sharing the same `resource_id` never see each other's row.

        Exercises Postgres's own `UNIQUE (tenant_id, platform, resource_id)`.
        """
        tenant_a = _seed_tenant(dal, slug="pg-svc-l2-iso-a")
        tenant_b = _seed_tenant(dal, slug="pg-svc-l2-iso-b")

        upsert_platform_connection(
            dal,
            tenant_id=tenant_a,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="shared-pg",
            access_token="tok-a",
            refresh_token="ref-a",
            installed_by_user_id=None,
        )
        upsert_platform_connection(
            dal,
            tenant_id=tenant_b,
            platform="twitch",
            resource_type="twitch_channel",
            resource_id="shared-pg",
            access_token="tok-b",
            refresh_token="ref-b",
            installed_by_user_id=None,
        )

        conn_a = get_platform_connection(
            dal, tenant_id=tenant_a, platform="twitch", resource_id="shared-pg"
        )
        conn_b = get_platform_connection(
            dal, tenant_id=tenant_b, platform="twitch", resource_id="shared-pg"
        )
        assert conn_a is not None and conn_a.access_token == "tok-a"
        assert conn_b is not None and conn_b.access_token == "tok-b"
        assert conn_a.id != conn_b.id


@requires_docker
class TestLayer3AccessGrantAgainstRealPostgres:
    """`grant_community_connection_access()`/`resolve_community_connection()` end-to-end."""

    def test_approve_then_resolve_round_trip(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="pg-svc-l3-1")
        community_id = _seed_community(dal, name="pg-community-1", tenant_id=tenant_id)
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="discord",
            resource_type="discord_guild",
            resource_id="guild-pg-1",
            access_token="guild-tok",
            refresh_token=None,
            installed_by_user_id=None,
        )

        grant_community_connection_access(
            dal, community_id=community_id, connection_id=connection_id, status="approved"
        )

        resolved = resolve_community_connection(dal, community_id=community_id, platform="discord")
        assert resolved is not None
        assert resolved.id == connection_id
        assert resolved.access_token == "guild-tok"

    def test_grant_rejects_cross_tenant_connection_real_db(self, dal: Any) -> None:
        """A community cannot be granted access to a connection under a different tenant.

        The application-layer tenant-match guard holds against the real schema too.
        """
        tenant_a = _seed_tenant(dal, slug="pg-svc-l3-cross-a")
        tenant_b = _seed_tenant(dal, slug="pg-svc-l3-cross-b")
        community_id = _seed_community(dal, name="pg-community-cross", tenant_id=tenant_a)
        other_connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_b,
            platform="discord",
            resource_type="discord_guild",
            resource_id="guild-pg-cross",
            access_token="guild-tok-b",
            refresh_token=None,
            installed_by_user_id=None,
        )

        with pytest.raises(ApiError):
            grant_community_connection_access(
                dal,
                community_id=community_id,
                connection_id=other_connection_id,
                status="approved",
            )

        assert (
            resolve_community_connection(dal, community_id=community_id, platform="discord") is None
        )

    def test_unapproved_grant_never_resolves(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="pg-svc-l3-pending")
        community_id = _seed_community(dal, name="pg-community-pending", tenant_id=tenant_id)
        connection_id = upsert_platform_connection(
            dal,
            tenant_id=tenant_id,
            platform="discord",
            resource_type="discord_guild",
            resource_id="guild-pg-pending",
            access_token="guild-tok-pending",
            refresh_token=None,
            installed_by_user_id=None,
        )
        grant_community_connection_access(
            dal, community_id=community_id, connection_id=connection_id, status="pending"
        )

        assert (
            resolve_community_connection(dal, community_id=community_id, platform="discord") is None
        )


@requires_docker
async def test_missing_app_credentials_raise_transport_unavailable_real_db(dal: Any) -> None:
    tenant_id = _seed_tenant(dal, slug="pg-svc-transport-unavailable")
    with pytest.raises(TransportUnavailable):
        await DefaultCredentialResolver().resolve(
            dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
        )
