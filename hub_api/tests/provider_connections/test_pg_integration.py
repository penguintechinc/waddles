"""The framework against a REAL, fully-migrated Postgres (docker): schema parity, locks, rollback.

sqlite proves the logic; only Postgres proves what Postgres does -- the migration's actual columns,
real FK / unique / CHECK enforcement, JSONB and timestamptz round-trips, and above all the
``SELECT ... FOR UPDATE`` that stops concurrent resolvers from spending a single-use refresh token
twice (which Twitch treats as compromise). Skipped (not failed) where the ``docker`` CLI is absent,
same convention as every other real-Postgres test in this repo.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg2
import pytest
from penguin_dal import AsyncDB

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.provider_connections import PlatformKeyVault, ProviderConnectionService  # noqa: E402
from services.provider_connections.adapter import AdapterRegistry  # noqa: E402
from services.provider_connections.errors import (  # noqa: E402
    ConnectionConflict,
    ConnectionNotFound,
    ConnectionNotUsable,
    ProviderUnavailable,
)
from services.provider_connections.twitch import TwitchAdapter  # noqa: E402
from tests.provider_connections.conftest import (  # noqa: E402
    BOT,
    CHANNEL_ID,
    CHANNEL_LOGIN,
    OTHER_CHANNEL_LOGIN,
    FakeClock,
    build_pc_metadata,
)
from tests.provider_connections.fake_twitch import (  # noqa: E402
    APP_CLIENT_ID,
    APP_CLIENT_SECRET,
    FakeTwitch,
)

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """One real Postgres 17 container migrated to head (Alembic chain incl. 0035 + 0058)."""
    if not DOCKER_AVAILABLE:
        pytest.skip("docker CLI not available in this environment")
    with migrated_postgres("hub-api-provider-connections") as db:
        yield db


def _sql(db: PgTestDatabase, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return list(cur.fetchall()) if cur.description else []
    finally:
        conn.close()


class Ids:
    """Seeded ids for one test."""

    def __init__(
        self, tenant: int, community: int, second: int, other_tenant: int, other_comm: int
    ) -> None:
        """Hold the ids."""
        self.tenant, self.community, self.second = tenant, community, second
        self.other_tenant, self.other_community = other_tenant, other_comm


@pytest.fixture
def ids(pg_db: PgTestDatabase) -> Ids:
    """Two tenants, three communities, freshly inserted per test (unique slugs)."""
    _sql(pg_db, "ALTER TABLE communities ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
    suffix = str(len(_sql(pg_db, "SELECT id FROM tenants")))
    t1 = _sql(pg_db, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"pc-a-{suffix}",))[0][
        0
    ]
    t2 = _sql(pg_db, "INSERT INTO tenants (slug) VALUES (%s) RETURNING id", (f"pc-b-{suffix}",))[0][
        0
    ]
    insert = "INSERT INTO communities (name, tenant_id) VALUES (%s, %s) RETURNING id"
    c1 = _sql(pg_db, insert, (f"pc-a1-{suffix}", t1))[0][0]
    c2 = _sql(pg_db, insert, (f"pc-a2-{suffix}", t1))[0][0]
    c3 = _sql(pg_db, insert, (f"pc-b1-{suffix}", t2))[0][0]
    return Ids(t1, c1, c2, t2, c3)


@pytest.fixture
async def install_dal(pg_db: PgTestDatabase) -> AsyncIterator[AsyncDB]:
    """penguin-dal over the real database with a pool big enough for real concurrency."""
    dal = AsyncDB(pg_db.dsn, pool_size=12)
    await dal.reflect()
    yield dal
    await dal.close()


class SlowTwitch(FakeTwitch):
    """A fake whose token endpoint takes a moment, so concurrent resolvers genuinely overlap."""

    @property
    def transport(self) -> httpx.MockTransport:  # type: ignore[override]
        """Async handler: the refresh endpoint sleeps before answering."""

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth2/token":
                await asyncio.sleep(0.4)
            return self.handler(request)

        return httpx.MockTransport(handler)


@pytest.fixture
def slow_twitch() -> SlowTwitch:
    twitch = SlowTwitch()
    twitch.channels = {CHANNEL_LOGIN: CHANNEL_ID, OTHER_CHANNEL_LOGIN: "7002"}
    return twitch


@pytest.fixture
def service(
    install_dal: AsyncDB, slow_twitch: SlowTwitch, clock: FakeClock
) -> ProviderConnectionService:
    registry = AdapterRegistry(
        [
            TwitchAdapter(
                transport=slow_twitch.transport,
                app_credentials=lambda: (APP_CLIENT_ID, APP_CLIENT_SECRET),
                clock=clock,
            )
        ]
    )
    return ProviderConnectionService(install_dal, registry, PlatformKeyVault(), clock=clock)


async def _create(
    service: ProviderConnectionService, ids: Ids, tokens: tuple[str, str | None], **kw: Any
) -> Any:
    params: dict[str, Any] = {
        "tenant_id": ids.tenant,
        "community_id": ids.community,
        "actor_user_id": None,
        "provider": "twitch",
        "access_token": tokens[0],
        "refresh_token": tokens[1],
        "metadata": {"channel_login": CHANNEL_LOGIN},
        "enabled": True,
    }
    params.update(kw)
    return await service.create(**params)


class TestSchemaParity:
    def test_the_sqlite_test_mirror_matches_the_real_migrated_schema(
        self, pg_db: PgTestDatabase
    ) -> None:
        """If a migration changes these tables and the mirror is not updated, this fails."""
        mirror = build_pc_metadata()
        for table in ("platform_connections", "community_connection_access"):
            real = {
                name: nullable == "YES"
                for name, nullable in _sql(
                    pg_db,
                    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name=%s",
                    (table,),
                )
            }
            local = {c.name: bool(c.nullable) for c in mirror.tables[table].columns}
            assert real, table  # the comparison must have examined a real table
            assert set(real) == set(local), (table, set(real) ^ set(local))
            assert real == local, table


class TestLifecycleOnPostgres:
    async def test_create_read_update_delete_and_rows_are_ciphertext(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        pg_db: PgTestDatabase,
    ) -> None:
        tokens = slow_twitch.mint(account=BOT)
        view = await _create(service, ids, tokens)
        cid = view.binding.connection.id
        assert view.binding.connection.expires_at is not None
        assert view.binding.connection.expires_at.tzinfo is not None  # timestamptz round-trip

        (row,) = _sql(
            pg_db,
            "SELECT access_token, refresh_token, resource_metadata->>'channel_id', "
            "(SELECT enabled FROM community_connection_access WHERE connection_id=%s), "
            "t::text FROM platform_connections t WHERE id=%s",
            (cid, cid),
        )
        access_ct, refresh_ct, channel_id, enabled, whole_row = row
        assert channel_id == CHANNEL_ID and enabled is True
        for plaintext, ciphertext in ((tokens[0], access_ct), (tokens[1], refresh_ct)):
            assert plaintext and plaintext not in whole_row  # not even in the row's text form
            assert ciphertext != plaintext
            assert PlatformKeyVault().open(ciphertext) == plaintext

        updated = await service.update(
            tenant_id=ids.tenant,
            community_id=ids.community,
            actor_user_id=None,
            connection_id=cid,
            enabled=False,
            metadata=None,
            access_token=None,
            refresh_token=None,
        )
        assert updated.binding.grant.enabled is False
        retarget = await service.update(
            tenant_id=ids.tenant,
            community_id=ids.community,
            actor_user_id=None,
            connection_id=cid,
            enabled=None,
            metadata={"channel_login": OTHER_CHANNEL_LOGIN},
            access_token=None,
            refresh_token=None,
        )
        assert retarget.binding.connection.resource_id == "7002"

        assert (
            await service.delete(
                tenant_id=ids.tenant, community_id=ids.community, connection_id=cid
            )
            is True
        )
        assert _sql(pg_db, "SELECT count(*) FROM platform_connections WHERE id=%s", (cid,)) == [
            (0,)
        ]
        assert _sql(
            pg_db, "SELECT count(*) FROM community_connection_access WHERE connection_id=%s", (cid,)
        ) == [(0,)]

    async def test_the_database_enforces_the_unique_resource_even_under_a_race(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        pg_db: PgTestDatabase,
    ) -> None:
        results = await asyncio.gather(
            *[_create(service, ids, slow_twitch.mint(account=BOT)) for _ in range(4)],
            return_exceptions=True,
        )
        created = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, ConnectionConflict)]
        assert len(created) == 1 and len(conflicts) == 3, results
        assert _sql(
            pg_db, "SELECT count(*) FROM platform_connections WHERE tenant_id=%s", (ids.tenant,)
        ) == [(1,)]

    async def test_a_foreign_key_violation_is_reported_as_a_conflict_and_leaves_nothing_behind(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        pg_db: PgTestDatabase,
    ) -> None:
        with pytest.raises(ConnectionConflict, match="could not be stored"):
            await _create(service, ids, slow_twitch.mint(account=BOT), actor_user_id=987654321)
        assert _sql(
            pg_db, "SELECT count(*) FROM platform_connections WHERE tenant_id=%s", (ids.tenant,)
        ) == [(0,)]  # the half-written link was rolled back with its grant

    async def test_deleting_a_link_cascades_to_every_grant(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        pg_db: PgTestDatabase,
    ) -> None:
        view = await _create(service, ids, slow_twitch.mint(account=BOT))
        cid = view.binding.connection.id
        _sql(
            pg_db,
            "INSERT INTO community_connection_access (community_id, connection_id, status) VALUES (%s,%s,'approved')",
            (ids.second, cid),
        )
        _sql(pg_db, "DELETE FROM platform_connections WHERE id=%s", (cid,))
        assert _sql(
            pg_db, "SELECT count(*) FROM community_connection_access WHERE connection_id=%s", (cid,)
        ) == [(0,)]

    async def test_tenant_and_community_isolation_hold_on_postgres(
        self, service: ProviderConnectionService, slow_twitch: SlowTwitch, ids: Ids
    ) -> None:
        view = await _create(service, ids, slow_twitch.mint(account=BOT))
        cid = view.binding.connection.id
        assert (
            await service.list(tenant_id=ids.other_tenant, community_id=ids.other_community) == []
        )
        for scope in (
            {"tenant_id": ids.other_tenant, "community_id": ids.other_community},
            {"tenant_id": ids.other_tenant, "community_id": ids.community},
            {"tenant_id": ids.tenant, "community_id": ids.second},
        ):
            with pytest.raises(ConnectionNotFound):
                await service.get(connection_id=cid, **scope)
        with pytest.raises(ConnectionNotFound):
            await _create(
                service, ids, slow_twitch.mint(account=BOT), community_id=ids.other_community
            )

    async def test_the_data_plane_list_reads_real_jsonb(
        self, service: ProviderConnectionService, slow_twitch: SlowTwitch, ids: Ids
    ) -> None:
        await _create(service, ids, slow_twitch.mint(account=BOT, scopes=["chat:read"]))
        page = await service.list_for_dataplane(
            provider="twitch", capability="chat_ingest", after=0, limit=1000
        )
        mine = [i for i in page.items if i.tenant_id == ids.tenant]
        assert len(mine) == 1 and mine[0].metadata["channel_login"] == CHANNEL_LOGIN
        relay = await service.list_for_dataplane(
            provider="twitch", capability="action_relay", after=0, limit=1000
        )
        assert [i for i in relay.items if i.tenant_id == ids.tenant] == []


class TestRefreshLock:
    async def test_concurrent_resolvers_spend_the_single_use_refresh_token_exactly_once(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        clock: FakeClock,
        pg_db: PgTestDatabase,
    ) -> None:
        view = await _create(service, ids, slow_twitch.mint(account=BOT))
        cid = view.binding.connection.id
        clock.advance(seconds=14000)  # every caller now sees a stale token

        results = await asyncio.gather(
            *[service.resolve(community_id=ids.community, connection_id=cid) for _ in range(8)],
            return_exceptions=True,
        )
        assert all(not isinstance(r, BaseException) for r in results), results
        assert slow_twitch.refresh_calls == 1  # Twitch saw ONE refresh, not eight
        assert len({r.access_token for r in results}) == 1  # everyone got the same rotated token
        (status,) = _sql(pg_db, "SELECT status FROM platform_connections WHERE id=%s", (cid,))
        assert status == ("active",)

    async def test_concurrent_resolvers_after_a_rejected_refresh_all_get_the_same_refusal(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        clock: FakeClock,
        pg_db: PgTestDatabase,
    ) -> None:
        view = await _create(service, ids, slow_twitch.mint(account=BOT))
        cid = view.binding.connection.id
        slow_twitch.refresh_tokens.clear()
        clock.advance(seconds=14000)
        results = await asyncio.gather(
            *[service.resolve(community_id=ids.community, connection_id=cid) for _ in range(5)],
            return_exceptions=True,
        )
        assert all(isinstance(r, ConnectionNotUsable) for r in results), results
        assert {r.reason for r in results if isinstance(r, ConnectionNotUsable)} == {
            "refresh_rejected"
        }
        assert slow_twitch.refresh_calls == 1  # the dead token was tried once, not five times
        assert _sql(
            pg_db, "SELECT status, status_reason FROM platform_connections WHERE id=%s", (cid,)
        ) == [("expired", "refresh_rejected")]

    async def test_an_outage_mid_refresh_rolls_back_and_releases_the_lock(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        clock: FakeClock,
        pg_db: PgTestDatabase,
    ) -> None:
        view = await _create(service, ids, slow_twitch.mint(account=BOT))
        cid = view.binding.connection.id
        before = _sql(
            pg_db,
            "SELECT access_token, refresh_token, status, expires_at FROM platform_connections WHERE id=%s",
            (cid,),
        )
        clock.advance(seconds=14000)
        slow_twitch.status_override["token"] = 503
        with pytest.raises(ProviderUnavailable):
            await service.resolve(community_id=ids.community, connection_id=cid)
        assert (
            _sql(
                pg_db,
                "SELECT access_token, refresh_token, status, expires_at FROM platform_connections WHERE id=%s",
                (cid,),
            )
            == before
        )
        # The row lock was released by the rollback: the next resolver proceeds immediately.
        slow_twitch.status_override.clear()
        ok = await asyncio.wait_for(
            service.resolve(community_id=ids.community, connection_id=cid), timeout=10
        )
        assert ok.expires_at == clock.now + timedelta(seconds=14400)

    async def test_an_unrelated_connection_is_not_blocked_by_a_refresh_in_flight(
        self,
        service: ProviderConnectionService,
        slow_twitch: SlowTwitch,
        ids: Ids,
        pg_db: PgTestDatabase,
        clock: FakeClock,
    ) -> None:
        """The lock is per connection row, not per table: one slow refresh must not stall the rest."""
        a = await _create(service, ids, slow_twitch.mint(account=BOT))
        b = await _create(
            service,
            ids,
            slow_twitch.mint(account=BOT),
            metadata={"channel_login": OTHER_CHANNEL_LOGIN},
        )
        # Make only ``a`` stale by aging its stored expiry; ``b`` stays comfortably valid.
        _sql(
            pg_db,
            "UPDATE platform_connections SET expires_at = %s WHERE id=%s",
            (clock.now - timedelta(minutes=1), a.binding.connection.id),
        )
        slow = asyncio.create_task(
            service.resolve(community_id=ids.community, connection_id=a.binding.connection.id)
        )
        await asyncio.sleep(0.1)  # ``a``'s refresh is now in flight, holding its row lock
        started = asyncio.get_running_loop().time()
        fresh = await service.resolve(
            community_id=ids.community, connection_id=b.binding.connection.id
        )
        assert (
            asyncio.get_running_loop().time() - started < 0.3
        )  # not queued behind a's 0.4 s refresh
        assert fresh.access_token
        assert (await slow).access_token  # and ``a`` itself completed its refresh
        assert slow_twitch.refresh_calls == 1
