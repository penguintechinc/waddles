"""Kick on a REAL, fully-migrated Postgres (docker): the DB constraint, single flight, real rows.

sqlite (``test_kick_adapter``) proves the logic; only Postgres proves what migration 0082 actually
installs: the ``kick_channel`` resource type is accepted, the one-live-Kick-connection-per-community
trigger refuses a second grant (also when two connects race), revoked grants do not count, other
providers/communities are unaffected, and concurrent resolvers of one expiring token spend the
single-use Kick refresh token exactly once (``SELECT ... FOR UPDATE``). Skipped (not failed) where the
``docker`` CLI is absent, like every other real-Postgres test in this repo.
"""

# ruff: noqa: F811  (imported pytest fixtures are re-declared as test parameters)

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from typing import Any

import psycopg2
import psycopg2.errors
import pytest
from penguin_dal import AsyncDB
from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.provider_connections import PlatformKeyVault, ProviderConnectionService
from services.provider_connections.adapter import AdapterRegistry
from services.provider_connections.errors import ConnectionConflict, ConnectionNotUsable
from services.provider_connections.kick import KickAdapter
from tests.provider_connections.conftest import FakeClock
from tests.provider_connections.fake_kick import APP_CLIENT_ID, APP_CLIENT_SECRET, FakeKick
from tests.provider_connections.test_pg_integration import (  # noqa: F401 - fixtures
    Ids,
    _sql,
    ids,
    install_dal,
)

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason="docker CLI not available")

CONSTRAINT = "uq_one_kick_connection_per_community"


@pytest.fixture(scope="module")
def pg_db() -> Iterator[PgTestDatabase]:
    """Own migrated Postgres 17 container (distinct name: the sibling module keeps its own)."""
    with migrated_postgres("hub-api-kick") as db:
        yield db


@pytest.fixture(autouse=True)
def _kick_dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real = socket.getaddrinfo

    def fake(host: str, *args: Any, **kwargs: Any) -> Any:
        if host in ("api.kick.com", "kick.com", "id.kick.com"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr("services.url_guard.socket.getaddrinfo", fake)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "cd" * 32)
    yield


@pytest.fixture
def clock() -> FakeClock:
    """Shared fake clock."""
    return FakeClock()


@pytest.fixture
def kick(clock: FakeClock) -> FakeKick:
    """The fake Kick socket; its token endpoint is slow so concurrent resolvers overlap."""
    fake = FakeKick(clock)
    fake.refresh_delay = 0.4
    return fake


@pytest.fixture
def service(install_dal: AsyncDB, kick: FakeKick, clock: FakeClock) -> ProviderConnectionService:  # noqa: F811
    adapter = KickAdapter(
        transport=kick.transport,
        app_credentials=lambda: (APP_CLIENT_ID, APP_CLIENT_SECRET),
        clock=clock,
    )
    return ProviderConnectionService(
        install_dal, AdapterRegistry([adapter]), PlatformKeyVault(), clock=clock
    )


async def _create(
    service: ProviderConnectionService, ids: Ids, community: int, slug: str, tokens: tuple[str, str]
) -> Any:
    return await service.create(
        tenant_id=ids.tenant,
        community_id=community,
        actor_user_id=ids.owner,
        provider="kick",
        access_token=tokens[0],
        refresh_token=tokens[1],
        metadata={"channel_slug": slug},
        enabled=True,
    )


def _raw_kick_grant(
    pg_db: Any, ids: Ids, community: int, resource_id: str, status: str = "approved"
) -> None:
    """Insert a Kick connection + grant with plain SQL (bypassing every service-level check)."""
    (cid,) = _sql(
        pg_db,
        "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
        "access_token) VALUES (%s, 'kick', 'kick_channel', %s, 'sealed') RETURNING id",
        (ids.tenant, resource_id),
    )[0]
    _sql(
        pg_db,
        "INSERT INTO community_connection_access (community_id, connection_id, status) "
        "VALUES (%s, %s, %s)",
        (community, cid, status),
    )


class TestRealRows:
    async def test_connect_stores_ciphertext_and_the_binding_ids(
        self, service: ProviderConnectionService, kick: FakeKick, ids: Ids, pg_db: Any
    ) -> None:
        tokens = kick.mint()
        view = await _create(service, ids, ids.community, "streamer-one", tokens)
        cid = view.binding.connection.id
        ((rtype, chatroom, broadcaster, selfid, access, whole),) = _sql(
            pg_db,
            "SELECT resource_type, resource_metadata->>'chatroom_id', "
            "resource_metadata->>'broadcaster_user_id', resource_metadata->>'self_user_id', "
            "access_token, t::text FROM platform_connections t WHERE id=%s",
            (cid,),
        )
        assert (rtype, chatroom, broadcaster, selfid) == ("kick_channel", "9001", "7001", "555001")
        assert access != tokens[0] and tokens[0] not in whole and tokens[1] not in whole
        resolved = await service.resolve(community_id=ids.community, connection_id=cid)
        assert resolved.kind == 1 and resolved.access_token == tokens[0]
        assert resolved.expires_at is not None
        assert {k: resolved.metadata[k] for k in ("chatroom_id", "broadcaster_user_id")} == {
            "chatroom_id": "9001",
            "broadcaster_user_id": "7001",
        }


class TestOneConnectionPerCommunityConstraint:
    def test_database_refuses_a_second_live_kick_grant(
        self,
        ids: Ids,
        pg_db: Any,  # noqa: F811
    ) -> None:
        _raw_kick_grant(pg_db, ids, ids.community, f"rid-{ids.community}-a")
        with pytest.raises(psycopg2.errors.UniqueViolation) as caught:
            _raw_kick_grant(pg_db, ids, ids.community, f"rid-{ids.community}-b")
        assert caught.value.diag.constraint_name == CONSTRAINT
        # The refused statement left nothing behind: still exactly one live grant.
        ((count,),) = _sql(
            pg_db,
            "SELECT count(*) FROM community_connection_access a JOIN platform_connections c "
            "ON c.id = a.connection_id WHERE a.community_id=%s AND c.platform='kick' "
            "AND c.resource_id LIKE %s",
            (ids.community, f"rid-{ids.community}-%"),
        )
        assert count == 1

    def test_revoked_grants_do_not_count_and_cannot_be_resurrected_into_a_dup(
        self,
        ids: Ids,
        pg_db: Any,  # noqa: F811
    ) -> None:
        _raw_kick_grant(pg_db, ids, ids.community, f"rid-{ids.community}-old", status="revoked")
        _raw_kick_grant(pg_db, ids, ids.community, f"rid-{ids.community}-new")  # allowed
        with pytest.raises(psycopg2.errors.UniqueViolation):  # un-revoking would make two live
            _sql(
                pg_db,
                "UPDATE community_connection_access SET status='approved' WHERE community_id=%s "
                "AND connection_id=(SELECT id FROM platform_connections WHERE resource_id=%s)",
                (ids.community, f"rid-{ids.community}-old"),
            )

    def test_other_communities_and_other_providers_are_unaffected(
        self,
        ids: Ids,
        pg_db: Any,  # noqa: F811
    ) -> None:
        _raw_kick_grant(pg_db, ids, ids.community, f"rid-{ids.community}-k")
        _raw_kick_grant(pg_db, ids, ids.second, f"rid-{ids.second}-k")  # another community: ok
        (twitch,) = _sql(
            pg_db,
            "INSERT INTO platform_connections (tenant_id, platform, resource_type, resource_id, "
            "access_token) VALUES (%s, 'twitch', 'twitch_channel', %s, 'sealed') RETURNING id",
            (ids.tenant, f"tw-{ids.community}"),
        )[0]
        _sql(
            pg_db,
            "INSERT INTO community_connection_access (community_id, connection_id, status) "
            "VALUES (%s, %s, 'approved')",
            (ids.community, twitch),
        )  # a twitch grant beside the kick one: allowed

    async def test_service_refuses_with_a_409_before_the_database_is_reached(
        self,
        service: ProviderConnectionService,
        kick: FakeKick,
        ids: Ids,  # noqa: F811
    ) -> None:
        await _create(service, ids, ids.community, "streamer-one", kick.mint())
        with pytest.raises(ConnectionConflict, match="maximum number of Kick connections"):
            await _create(service, ids, ids.community, "streamer_two", kick.mint())

    async def test_racing_connects_admit_exactly_one(
        self,
        service: ProviderConnectionService,
        kick: FakeKick,
        ids: Ids,
        pg_db: Any,  # noqa: F811
    ) -> None:
        """Both pass the advisory service check; the trigger serialises them and refuses one."""
        outcomes = await asyncio.gather(
            _create(service, ids, ids.second, "streamer-one", kick.mint()),
            _create(service, ids, ids.second, "streamer_two", kick.mint()),
            return_exceptions=True,
        )
        winners = [o for o in outcomes if not isinstance(o, BaseException)]
        losers = [o for o in outcomes if isinstance(o, BaseException)]
        assert len(winners) == 1 and len(losers) == 1
        assert isinstance(losers[0], ConnectionConflict)
        ((live,),) = _sql(
            pg_db,
            "SELECT count(*) FROM community_connection_access a JOIN platform_connections c "
            "ON c.id = a.connection_id WHERE a.community_id=%s AND c.platform='kick'",
            (ids.second,),
        )
        assert live == 1


class TestRefreshOnPostgres:
    async def test_concurrent_resolvers_spend_the_rotating_refresh_token_once(
        self,
        service: ProviderConnectionService,
        kick: FakeKick,
        clock: FakeClock,
        ids: Ids,  # noqa: F811
        pg_db: Any,
    ) -> None:
        tokens = kick.mint()
        cid = (
            await _create(service, ids, ids.community, "streamer-one", tokens)
        ).binding.connection.id
        clock.advance(seconds=7200 - 60)  # inside the refresh skew, still valid on Kick's side
        results = await asyncio.gather(
            *[service.resolve(community_id=ids.community, connection_id=cid) for _ in range(6)]
        )
        assert kick.refresh_calls == 1  # single flight: one exchange for six resolvers
        assert {r.access_token for r in results} == {results[0].access_token}
        assert results[0].access_token != tokens[0]
        ((sealed_changed,),) = _sql(
            pg_db, "SELECT access_token <> %s FROM platform_connections WHERE id=%s", ("x", cid)
        )
        assert sealed_changed is True
        again = await service.resolve(community_id=ids.community, connection_id=cid)
        assert again.access_token == results[0].access_token and kick.refresh_calls == 1

    async def test_rejected_refresh_marks_the_row_expired_and_stays_refused(
        self,
        service: ProviderConnectionService,
        kick: FakeKick,
        clock: FakeClock,
        ids: Ids,  # noqa: F811
        pg_db: Any,
    ) -> None:
        cid = (
            await _create(service, ids, ids.community, "streamer-one", kick.mint())
        ).binding.connection.id
        clock.advance(seconds=7200 - 60)
        kick.refresh_status = 400
        with pytest.raises(ConnectionNotUsable) as caught:
            await service.resolve(community_id=ids.community, connection_id=cid)
        assert caught.value.reason == "refresh_rejected"
        ((status, reason),) = _sql(
            pg_db, "SELECT status, status_reason FROM platform_connections WHERE id=%s", (cid,)
        )
        assert (status, reason) == ("expired", "refresh_rejected")
        kick.refresh_status = None
        calls = kick.refresh_calls
        with pytest.raises(ConnectionNotUsable):
            await service.resolve(community_id=ids.community, connection_id=cid)
        assert kick.refresh_calls == calls  # a dead chain is never retried against Kick
