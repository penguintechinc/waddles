"""hub-api's REAL runtime role (`waddles_hub_api`) vs `platform_integrations`, on real Postgres.

# regression: gh-969 -- `GET /api/v1/superadmin/platform-config` 500'd with
# `42501 permission denied for table platform_integrations`: the role is denied the table BY
# DESIGN (H-3) yet the code queried it directly. It now reaches the data only through the
# SECURITY DEFINER functions of migration 0069.

Everything here connects as `waddles_hub_api` with its own migrated password -- the sqlite suites
cannot prove privileges. Skipped (never failed) where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from flask_core.database import AsyncDAL
from pydal import Field
from quart import Quart
from quart_schema import QuartSchema

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from blueprints.v1.platform_config import platform_config_bp  # noqa: E402
from config import HubAPIConfig  # noqa: E402
from services import community_connections as svc  # noqa: E402
from services.platform_integrations_crypto import connection_aad, encrypt_token  # noqa: E402
from services.provider_connections.vault import open_secret  # noqa: E402
from services.schema import bind_marketplace_module_tables, bind_platform_tables  # noqa: E402
from tests.conftest import TENANT_SLUG, make_user_token  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)

_KEY = "cd" * 32  # throwaway test key
ROLE = "waddles_hub_api"
BOT_CANARY = "CANARY-BOT-TOKEN-gh969"
CFG_CANARY = "CANARY-CFG-SECRET-gh969"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real, fully-migrated container (roles provisioned by 0055, functions by 0069)."""
    with migrated_postgres("hub-api-pi-role") as db:
        yield db


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", _KEY)


def _owner(
    db: PgTestDatabase, statement: str, params: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return list(cur.fetchall()) if cur.description else []
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean(pg_db: PgTestDatabase) -> None:
    _owner(pg_db, "TRUNCATE platform_integrations, credential_access_log RESTART IDENTITY CASCADE")


@pytest.fixture
def role_dal(pg_db: PgTestDatabase) -> Iterator[AsyncDAL]:
    """A real pydal `AsyncDAL` connected AS `waddles_hub_api` (never the owner)."""
    pw = pg_db.service_role_passwords[ROLE]
    url = f"postgres://{ROLE}:{pw}@{pg_db.host}:{pg_db.port}/{pg_db.dbname}"
    async_dal = AsyncDAL(url, pool_size=1)
    dal = async_dal.dal
    dal.define_table(
        "tenants",
        Field("slug"),
        Field("logo_url"),
        Field("is_active", "boolean"),
        Field("config", "json"),
        migrate=False,  # the harness's minimal `tenants` columns
    )
    bind_platform_tables(dal, migrate=False)
    bind_marketplace_module_tables(dal, migrate=False)
    yield async_dal
    dal.close()


def _community(db: PgTestDatabase, name: str = "pi-role") -> int:
    """A community that belongs to its own tenant (so `Scope.SYSTEM` resolves a real tenant)."""
    tid = _owner(db, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"t-{name}",))[0][0]
    return int(
        _owner(
            db,
            "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id",
            (name, tid),
        )[0][0]
    )


def _seed_bot(db: PgTestDatabase) -> None:
    _owner(
        db,
        "INSERT INTO platform_integrations (platform, integration_type, access_token, "
        "refresh_token, client_id, client_secret, config_data) VALUES "
        "('twitch','bot',%s,%s,'bot-client',%s,%s::jsonb)",
        (
            BOT_CANARY,
            BOT_CANARY,
            BOT_CANARY,
            f'{{"note": "n", "api_token": "{CFG_CANARY}", "auth_code": "{CFG_CANARY}"}}',
        ),
    )


def test_the_runtime_role_is_still_denied_the_table_directly(
    role_dal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    """The H-3 boundary survives: same DAL, raw SELECT/INSERT on the table is still 42501."""
    _seed_bot(pg_db)
    for stmt in (
        "SELECT 1 FROM platform_integrations",
        "UPDATE platform_integrations SET is_active = FALSE",
    ):
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            role_dal.dal.executesql(stmt)
        role_dal.dal.rollback()


@pytest.fixture
def client(role_dal: AsyncDAL, pg_db: PgTestDatabase) -> Any:
    _owner(
        pg_db,
        "INSERT INTO tenants (slug, is_active) VALUES (%s, TRUE) ON CONFLICT (slug) DO NOTHING",
        (TENANT_SLUG,),
    )
    quart_app = Quart(__name__)
    QuartSchema(quart_app)
    quart_app.register_blueprint(platform_config_bp)
    quart_app.config["dal"] = role_dal.dal
    quart_app.config["async_dal"] = role_dal
    quart_app.config["HUB_API_CONFIG"] = HubAPIConfig(
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
        **_extra_config(),
    )
    return quart_app.test_client()


def _extra_config() -> dict[str, Any]:
    """Remaining required `HubAPIConfig` fields, copied from the blueprint test's own helper."""
    from tests.test_v1_platform_config_blueprint import _test_config

    cfg = _test_config()
    return {
        k: getattr(cfg, k)
        for k in cfg.__dataclass_fields__
        if k
        not in {
            "module_name",
            "module_version",
            "module_port",
            "grpc_port",
            "database_url",
            "database_read_replica_url",
            "db_pool_size",
            "db_max_retries",
            "db_retry_delay",
            "secret_key",
            "jwt_algorithm",
        }
    }


def _admin() -> dict[str, str]:
    token = make_user_token(
        user_id=1,
        scope="*:read *:write *:admin *:delete settings:write users:admin",
        tenant=TENANT_SLUG,
    )
    return {"Authorization": f"Bearer {token}"}


async def test_superadmin_platform_config_is_200_under_the_real_role(
    client: Any, pg_db: PgTestDatabase
) -> None:
    """The reported 500: with the REAL runtime role it is 200, and no secret escapes."""
    _seed_bot(pg_db)
    response = await client.get("/api/v1/superadmin/platform-config", headers=_admin())
    body = await response.get_json()
    assert response.status_code == 200, body
    assert body["count"] == 1 and body["data"][0]["platform"] == "twitch"
    assert body["data"][0]["accessToken"] == "***"
    raw = str(body)
    assert BOT_CANARY not in raw and CFG_CANARY not in raw
    # filters (API filter coverage): type/platform narrow; no match -> empty, not an error
    for query, expected in (
        ("?integrationType=bot", 1),
        ("?platform=twitch", 1),
        ("?platform=discord", 0),
        ("?integrationType=community_oauth", 0),
    ):
        r = await client.get(f"/api/v1/superadmin/platform-config{query}", headers=_admin())
        assert r.status_code == 200 and (await r.get_json())["count"] == expected, query
    bad = await client.get(
        "/api/v1/superadmin/platform-config?integrationType=nope", headers=_admin()
    )
    assert bad.status_code == 400  # invalid filter value: explicit 400, never a leak or a 500


async def test_community_connection_lifecycle_through_the_definer_functions(
    role_dal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    """Upsert -> status -> decrypt -> CAS refresh -> disconnect, all as the denied role."""
    cid = _community(pg_db)
    statuses = await svc.list_connections(role_dal, cid, tenant_id=svc.SYSTEM)
    assert [s.connected for s in statuses] == [False] * len(svc.SUPPORTED_PROVIDERS)

    status = await svc.upsert_connection(
        role_dal,
        cid,
        "youtube",
        access_token="acc-1",
        refresh_token="ref-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes=["a", "b"],
        account_label="My Channel",
        actor_user_id=None,
        tenant_id=svc.SYSTEM,
    )
    assert status.connected and status.account_label == "My Channel" and status.scopes == ["a", "b"]
    stored = _owner(
        pg_db,
        "SELECT access_token, refresh_token, seal_version, is_encrypted FROM platform_integrations",
    )
    assert len(stored) == 1 and stored[0][2] == 1 and stored[0][3] is True
    assert "acc-1" not in stored[0][0]  # sealed in hub-api before it ever reached SQL
    aad = connection_aad(community_id=cid, provider="youtube")
    assert open_secret(stored[0][0], aad=aad, seal_version=1) == "acc-1"

    tokens = await svc.get_decrypted_tokens(role_dal, cid, "youtube", tenant_id=svc.SYSTEM)
    assert tokens is not None and tokens.access_token == "acc-1" and tokens.refresh_token == "ref-1"
    assert (await svc.get_connection(role_dal, cid, "youtube", tenant_id=svc.SYSTEM)) is not None
    assert (await svc.get_connection(role_dal, cid, "twitch", tenant_id=svc.SYSTEM)) is None
    assert await svc.get_decrypted_tokens(role_dal, cid, "twitch", tenant_id=svc.SYSTEM) is None

    await svc.store_refreshed_access_token(
        role_dal,
        cid,
        "youtube",
        access_token="acc-2",
        expires_at=datetime.now(UTC),
        tenant_id=svc.SYSTEM,
    )
    refreshed = await svc.get_decrypted_tokens(role_dal, cid, "youtube", tenant_id=svc.SYSTEM)
    assert refreshed is not None and refreshed.access_token == "acc-2"
    assert refreshed.refresh_token == "ref-1"  # untouched refresh token survives

    assert await svc.delete_connection(role_dal, cid, "youtube", None, tenant_id=svc.SYSTEM) is True
    assert (
        await svc.delete_connection(role_dal, cid, "youtube", None, tenant_id=svc.SYSTEM) is False
    )
    assert await svc.get_decrypted_tokens(role_dal, cid, "youtube", tenant_id=svc.SYSTEM) is None
    cleared = _owner(
        pg_db, "SELECT access_token, refresh_token, is_active FROM platform_integrations"
    )
    assert cleared == [(None, None, False)]

    actions = [r[0] for r in _owner(pg_db, "SELECT action FROM credential_access_log ORDER BY id")]
    assert "community_secret_read" in actions and "community_upsert" in actions
    assert "community_disconnect" in actions and "community_set_sealed" in actions
    users = {r[0] for r in _owner(pg_db, "SELECT DISTINCT db_user FROM credential_access_log")}
    assert users == {ROLE}


async def test_legacy_row_is_lazily_resealed_via_compare_and_set(
    role_dal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    """A pre-M4 (seal_version 0) row opens, then is upgraded in place through `set_sealed`."""
    cid = _community(pg_db, "pi-legacy")
    _owner(
        pg_db,
        "INSERT INTO platform_integrations (platform, integration_type, community_id, "
        "access_token, is_encrypted, seal_version) "
        "VALUES ('twitch','community_oauth',%s,%s,TRUE,0)",
        (cid, encrypt_token("legacy-acc")),
    )
    tokens = await svc.get_decrypted_tokens(role_dal, cid, "twitch", tenant_id=svc.SYSTEM)
    assert tokens is not None and tokens.access_token == "legacy-acc"
    assert _owner(pg_db, "SELECT seal_version FROM platform_integrations") == [(1,)]
    again = await svc.get_decrypted_tokens(role_dal, cid, "twitch", tenant_id=svc.SYSTEM)
    assert again is not None and again.access_token == "legacy-acc"


async def test_bot_lane_is_unreachable_through_the_community_functions(
    role_dal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    """Even a bot row for the same platform is invisible/untouchable via the community lane."""
    _seed_bot(pg_db)
    cid = _community(pg_db, "pi-bot")
    assert await svc.get_decrypted_tokens(role_dal, cid, "twitch", tenant_id=svc.SYSTEM) is None
    assert await svc.delete_connection(role_dal, cid, "twitch", None, tenant_id=svc.SYSTEM) is False
    await svc.upsert_connection(
        role_dal,
        cid,
        "twitch",
        access_token="c",
        refresh_token=None,
        expires_at=None,
        scopes=[],
        account_label=None,
        actor_user_id=None,
        tenant_id=svc.SYSTEM,
    )
    bot = _owner(
        pg_db,
        "SELECT access_token, is_active FROM platform_integrations WHERE integration_type='bot'",
    )
    assert bot == [(BOT_CANARY, True)]


async def test_a_foreign_tenant_is_rejected_by_the_database_and_audited(
    role_dal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    """Defence in depth: even if a foreign community_id slipped past the route, SQL refuses it."""
    from services.errors import ApiError

    ta = int(_owner(pg_db, "INSERT INTO tenants (slug) VALUES ('pi-ta') RETURNING id")[0][0])
    tb = int(_owner(pg_db, "INSERT INTO tenants (slug) VALUES ('pi-tb') RETURNING id")[0][0])
    cid = int(
        _owner(
            pg_db,
            "INSERT INTO communities (name, tenant_id) VALUES ('pi-t', %s) RETURNING id",
            (ta,),
        )[0][0]
    )
    await svc.upsert_connection(
        role_dal,
        cid,
        "youtube",
        access_token="a",
        refresh_token=None,
        expires_at=None,
        scopes=[],
        account_label=None,
        actor_user_id=7,
        tenant_id=ta,
    )
    # no end-user attribution is persisted (spoofable, unverifiable by the DB)
    assert _owner(
        pg_db, "SELECT created_by_user_id, updated_by_user_id FROM platform_integrations"
    ) == [(None, None)]
    with pytest.raises(ApiError) as exc:
        await svc.upsert_connection(
            role_dal,
            cid,
            "youtube",
            access_token="evil",
            refresh_token=None,
            expires_at=None,
            scopes=[],
            account_label=None,
            actor_user_id=7,
            tenant_id=tb,
        )
    assert exc.value.status_code == 404  # reveals nothing about other tenants
    with pytest.raises(ApiError):
        await svc.delete_connection(role_dal, cid, "youtube", 7, tenant_id=tb)
    assert await svc.get_decrypted_tokens(role_dal, cid, "youtube", tenant_id=tb) is None
    assert (await svc.get_decrypted_tokens(role_dal, cid, "youtube", tenant_id=ta)) is not None
    still = await svc.get_decrypted_tokens(role_dal, cid, "youtube", tenant_id=ta)
    assert still is not None and still.access_token == "a"  # the foreign write never landed
    details = [
        r[0]
        for r in _owner(pg_db, "SELECT detail FROM credential_access_log WHERE success = FALSE")
    ]
    assert details.count("forbidden_tenant") == 3


async def test_system_scope_for_an_unknown_community_is_refused_and_audited(
    role_dal: AsyncDAL, pg_db: PgTestDatabase
) -> None:
    """SYSTEM resolves the tenant from the community row; no row -> a tenant matching nothing."""
    from services.errors import ApiError

    assert await svc.get_decrypted_tokens(role_dal, 987654, "youtube", tenant_id=svc.SYSTEM) is None
    with pytest.raises(ApiError):
        await svc.delete_connection(role_dal, 987654, "youtube", None, tenant_id=svc.SYSTEM)
    rows = _owner(pg_db, "SELECT action, success, detail FROM credential_access_log ORDER BY id")
    assert rows == [
        ("community_secret_read", False, "forbidden_tenant"),
        ("community_disconnect", False, "forbidden_tenant"),
    ]
    with pytest.raises(TypeError):  # None is never a tenant scope
        await svc.list_connections(role_dal, 1, tenant_id=None)  # type: ignore[arg-type]
