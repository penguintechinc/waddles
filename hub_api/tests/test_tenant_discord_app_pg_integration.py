"""Tenant-owned Discord app (install -> sealed connection row -> resolver) on REAL Postgres.

Our WaddleBot Discord app serves only the default tenant; a private tenant brings its own.
Proves: the install writes a sealed (not plaintext) `platform_connections` row in the shape the
data plane reads; a tenant with no app fails loud with `TENANT_DISCORD_APP_MISSING`; env creds are
never served to a non-default tenant; one tenant's token never reaches another.

Original harness note (connection-model service layer, same fixtures):

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
from uuid import uuid4

import pytest
from pydal import DAL, Field

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

# `pg_docker` is reached via the sys.path insert above, not a package mypy
# resolves statically -- hence the import-not-found ignore below.
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services import credential_resolver as cr  # noqa: E402
from services import discord_install_service as install_module  # noqa: E402
from services import tenant_discord_install_state as state_module  # noqa: E402
from services.credential_resolver import (  # noqa: E402
    TENANT_DISCORD_APP_MISSING,
    DefaultCredentialResolver,
    TenantDiscordAppMissing,
    get_platform_connection,
)
from services.discord_install_service import complete_install  # noqa: E402
from services.schema import bind_guild_pairing_tables  # noqa: E402
from services.tenant_discord_app import get_install_status  # noqa: E402

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
    with migrated_postgres("hub-api-tenant-discord-own") as db:
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
    # `bind_auth_tables()` (called by `bind_guild_pairing_tables()`) assumes `tenants`/
    # `communities` are already bound by the caller -- same precondition
    # `tests/conftest.py::guild_pairing_db` satisfies for the sqlite fixture; mirrored here
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
    bind_guild_pairing_tables(connection, migrate=False)
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


GUILD_A = "123456789012345678"
GUILD_B = "223456789012345678"
NONCE = "browser-nonce-0123456789"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _install_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real DB + real sealing; only Discord's network + the state Redis are doubled."""
    redis = _FakeRedis()
    monkeypatch.setattr(state_module, "_redis_client", lambda: redis)
    monkeypatch.setattr(install_module, "_validate_bot_token", AsyncMock(return_value=None))


def _seed_admin(dal_: Any) -> int:
    """A real `hub_users` row: the install's `installed_by_user_id` FK is enforced by Postgres."""
    row = dal_.executesql(
        "INSERT INTO hub_users (username) VALUES (%s) RETURNING id",
        placeholders=[f"admin-{uuid4().hex}"],
    )
    dal_.commit()
    return int(row[0][0])


async def _install(dal: Any, tenant_id: int, *, app: str, token: str, guild: str) -> Any:
    state = await state_module.create_state(
        binding_nonce=NONCE,
        tenant_id=tenant_id,
        admin_user_id=_seed_admin(dal),
        application_id=app,
        client_secret=f"secret-{app}",
        bot_token=token,
        redirect_uri="https://hub.example/cb",
    )
    from unittest.mock import patch

    with patch.object(
        install_module,
        "_exchange_code",
        new=AsyncMock(return_value={"access_token": "t", "guild": {"id": guild}}),
    ):
        return await complete_install(dal, code="c", state=state, binding_nonce=NONCE)


@requires_docker
class TestTenantOwnedDiscordApp:
    async def test_install_writes_sealed_connection_in_data_plane_shape(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="dc-own-1")
        community_id = _seed_community(dal, name="c1", tenant_id=tenant_id)
        result = await _install(
            dal, tenant_id, app="111111111111111111", token="BOT-SECRET-1", guild=GUILD_A
        )

        assert result.guild_id == GUILD_A and result.community_granted is True
        row = dal(dal.platform_connections.id == result.connection_id).select().first()
        assert (row.tenant_id, row.platform, row.resource_type, row.resource_id) == (
            tenant_id,
            "discord",
            "discord_guild",
            GUILD_A,
        )
        assert row.status == "active"
        assert "BOT-SECRET-1" not in row.access_token  # sealed, never plaintext at rest
        assert row.resource_metadata["guild_id"] == GUILD_A
        assert row.resource_metadata["application_id"] == "111111111111111111"
        conn = get_platform_connection(
            dal, tenant_id=tenant_id, platform="discord", resource_id=GUILD_A
        )
        assert conn is not None and conn.access_token == "BOT-SECRET-1"
        grants = dal(dal.community_connection_access.connection_id == row.id).select()
        assert [(g.community_id, g.status, g.enabled) for g in grants] == [
            (community_id, "approved", True)
        ]

    async def test_reinstall_is_idempotent_and_rotates_token(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="dc-own-2")
        _seed_community(dal, name="c2", tenant_id=tenant_id)
        first = await _install(
            dal, tenant_id, app="111111111111111111", token="OLD-TOKEN", guild=GUILD_A
        )
        second = await _install(
            dal, tenant_id, app="111111111111111111", token="NEW-TOKEN", guild=GUILD_A
        )
        assert first.connection_id == second.connection_id
        rows = dal(
            (dal.platform_connections.tenant_id == tenant_id)
            & (dal.platform_connections.platform == "discord")
        ).count()
        assert rows == 1
        conn = get_platform_connection(
            dal, tenant_id=tenant_id, platform="discord", resource_id=GUILD_A
        )
        assert conn is not None and conn.access_token == "NEW-TOKEN"

    async def test_no_app_fails_loud_with_stable_code_and_counter(
        self, dal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tenant_id = _seed_tenant(dal, slug="dc-own-3")
        counter = MagicMock()
        monkeypatch.setattr(cr, "tenant_discord_app_missing_counter", counter)
        with pytest.raises(TenantDiscordAppMissing) as exc_info:
            await DefaultCredentialResolver().resolve(
                dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
            )
        assert exc_info.value.code == TENANT_DISCORD_APP_MISSING == "TENANT_DISCORD_APP_MISSING"
        counter.add.assert_called_once()
        assert "tenant" in counter.add.call_args.args[1]

    async def test_env_credentials_never_served_to_non_default_tenant(
        self, dal: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_CLIENT_ID", "env-client")
        monkeypatch.setenv("DISCORD_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "ENV-BOT-TOKEN")
        tenant_id = _seed_tenant(dal, slug="dc-own-4")
        with pytest.raises(TenantDiscordAppMissing):
            await DefaultCredentialResolver().resolve(
                dal, tenant_id=tenant_id, is_global_tenant=False, platform="discord"
            )
        global_id = _seed_tenant(dal, slug="dc-own-4-global", is_global=True)
        saas = await DefaultCredentialResolver().resolve(
            dal, tenant_id=global_id, is_global_tenant=True, platform="discord"
        )
        assert saas.payload["bot_token"] == "ENV-BOT-TOKEN" and saas.source == "saas"

    async def test_tenant_a_token_never_returned_for_tenant_b(self, dal: Any) -> None:
        tenant_a = _seed_tenant(dal, slug="dc-own-5a")
        tenant_b = _seed_tenant(dal, slug="dc-own-5b")
        await _install(dal, tenant_a, app="111111111111111111", token="TOKEN-OF-A", guild=GUILD_A)
        assert (
            get_platform_connection(
                dal, tenant_id=tenant_b, platform="discord", resource_id=GUILD_A
            )
            is None
        )
        with pytest.raises(TenantDiscordAppMissing):
            await DefaultCredentialResolver().resolve(
                dal, tenant_id=tenant_b, is_global_tenant=False, platform="discord"
            )
        got = await DefaultCredentialResolver().resolve(
            dal, tenant_id=tenant_a, is_global_tenant=False, platform="discord"
        )
        assert got.payload["bot_token"] == "TOKEN-OF-A"

    async def test_status_reports_install_without_secrets(self, dal: Any) -> None:
        tenant_id = _seed_tenant(dal, slug="dc-own-6")
        assert get_install_status(dal, tenant_id=tenant_id).installed is False
        await _install(
            dal, tenant_id, app="111111111111111111", token="BOT-SECRET-6", guild=GUILD_B
        )
        status = get_install_status(dal, tenant_id=tenant_id)
        assert status.installed is True
        assert status.application_id == "111111111111111111"
        assert status.connection_status == "awaiting_community_grant"  # no community yet
        assert status.updated_at is not None
        assert "BOT-SECRET-6" not in repr(status) and "secret-" not in repr(status)
