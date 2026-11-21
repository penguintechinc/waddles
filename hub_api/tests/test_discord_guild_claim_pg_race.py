"""Claim vs release race on REAL Postgres: a same-tenant claim in flight survives a release.

Connection A claims (same tenant, claim already exists -> "owner == me") and has NOT yet inserted
its pairing; connection B runs the last-pairing release meanwhile. Without the per-guild advisory
lock B deletes the claim at once and A's pairing ends up with no claim row; with it B waits for A's
commit, then sees A's pairing and keeps the claim. Skipped where `docker` is unavailable.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from flask_core.database import AsyncDAL

from services import guild_pairing as svc
from tests import demo_pg_support as pg

pytestmark = pytest.mark.skipif(not pg.DOCKER_AVAILABLE, reason="docker CLI not available")
GUILD = "623456789012345678"  # gitleaks:allow - fake snowflake


@pytest.fixture(scope="module")
def pg_db() -> Iterator[pg.PgTestDatabase]:
    """One migrated Postgres container for the module."""
    with pg.migrated_postgres("guild-claim-race") as db:
        pg.prepare_database(db)
        yield db


def test_claim_in_flight_is_not_deleted_by_a_racing_release(pg_db: pg.PgTestDatabase) -> None:
    gen = pg.fresh_dal(pg_db)
    a = next(gen).dal
    pg.q(a, "TRUNCATE discord_guild_tenants")
    tid = pg.scalar(
        a,
        "INSERT INTO tenants (slug, display_name, is_global, is_active) "
        "VALUES ('race-corp', 'R', false, true) RETURNING id",
    )
    cid = pg.scalar(
        a, "INSERT INTO communities (name, tenant_id) VALUES ('c1', %s) RETURNING id", [tid]
    )
    cid2 = pg.scalar(
        a, "INSERT INTO communities (name, tenant_id) VALUES ('c2', %s) RETURNING id", [tid]
    )
    pairing = (
        "INSERT INTO guild_tenant_pairings "
        "(community_id, discord_guild_id, direction, role_name_prefix) "
        "VALUES (%s, %s, 'bidirectional', '[T]')"
    )
    pg.q(a, pairing, [cid, GUILD])
    pg.q(
        a,
        "INSERT INTO discord_guild_tenants (discord_guild_id, tenant_id) VALUES (%s, %s)",
        [GUILD, tid],
    )
    # c1's pairing is being deleted (so the release would normally free the claim) ...
    pg.q(a, "DELETE FROM guild_tenant_pairings WHERE community_id = %s", [cid])

    b_async = AsyncDAL(pg_db.dsn.replace("postgresql://", "postgres://", 1), pool_size=1)
    b = b_async.dal
    b_done = threading.Event()

    def release() -> None:
        svc.release_unused_guild_claim(b, tenant_id=int(tid), discord_guild_id=GUILD)
        b.commit()
        b_done.set()

    # ... while c2's same-tenant claim is in flight on connection A (lock held, pairing not yet in).
    assert svc.claim_guild_for_tenant(a, tenant_id=int(tid), discord_guild_id=GUILD) is False
    worker = threading.Thread(target=release)
    worker.start()
    time.sleep(1.0)
    assert not b_done.is_set(), "release must wait on the guild lock held by the in-flight claim"
    a.executesql(pairing, placeholders=[cid2, GUILD])
    a.commit()
    worker.join(timeout=15)
    assert b_done.is_set()
    assert pg.q(
        a, "SELECT tenant_id FROM discord_guild_tenants WHERE discord_guild_id = %s", [GUILD]
    ) == [(tid,)] or [
        int(r[0]) for r in pg.q(a, "SELECT tenant_id FROM discord_guild_tenants")
    ] == [int(tid)]
    b.close()
    try:
        next(gen)
    except StopIteration:
        pass
