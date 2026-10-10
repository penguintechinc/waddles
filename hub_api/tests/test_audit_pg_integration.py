"""AuditService end-to-end on REAL Postgres 17 (migration 0048 applied, asyncpg driver).

The sqlite tests prove the logic; this proves the parts only a real server can: native `UUID` /
`timestamptz` / `JSONB` round-trip *bit-exactly* through the canonical hash, the per-chain advisory
lock serialises concurrent writers into one gapless chain, the append-only triggers hold against
the application role, and a privileged operator who disables the trigger and rewrites history is
caught by verification. Skipped, never failed, where the `docker` CLI is unavailable.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from penguin_dal import AsyncDB

_ALEMBIC_TESTS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "tests"
if str(_ALEMBIC_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_ALEMBIC_TESTS_DIR))

from pg_docker import (  # noqa: E402  # type: ignore[import-not-found]
    DOCKER_AVAILABLE,
    PgTestDatabase,
    migrated_postgres,
)

from services.audit_chain import ChainBreakReason, ChainStatus  # noqa: E402
from services.audit_events import (  # noqa: E402
    ActorKind,
    AuditCategory,
    AuditEvent,
)
from services.audit_service import AuditService, AuditWriteError, ListFilters  # noqa: E402
from tests.audit_support import FakeGate  # noqa: E402

pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="docker CLI not available in this environment"
)


@pytest.fixture(scope="module")
def pg() -> Iterator[PgTestDatabase]:
    with migrated_postgres(f"audit-svc-{os.getpid()}") as db:
        yield db


def _sql(db: PgTestDatabase, sql: str, params: object = None) -> list[tuple[Any, ...]]:
    conn = psycopg2.connect(db.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded(pg: PgTestDatabase) -> dict[str, Any]:
    """One tenant and two users (their `uuid` comes from migration 0043's column default)."""
    tenant_id = _sql(
        pg, "INSERT INTO tenants (slug, is_active) VALUES ('pg-acme', TRUE) RETURNING id"
    )[0][0]
    users = [
        _sql(pg, "INSERT INTO hub_users (username) VALUES (%s) RETURNING id, uuid", (name,))[0]
        for name in ("pg-user-a", "pg-user-b")
    ]
    # The legacy-baseline `audit_log` (000_create_base_schema.sql) is not in pg_docker's minimal
    # bootstrap; production has it, and `bundle_audit.record` writes it.
    _sql(
        pg,
        "CREATE TABLE IF NOT EXISTS audit_log (id SERIAL PRIMARY KEY, "
        "user_id INTEGER REFERENCES hub_users(id) ON DELETE SET NULL, "
        "action VARCHAR(100) NOT NULL, target_type VARCHAR(50), target_id VARCHAR(255), "
        "details JSONB DEFAULT '{}', ip_address VARCHAR(45), user_agent TEXT, "
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
    )
    return {"tenant_id": tenant_id, "users": users}


@pytest.fixture
async def service(pg: PgTestDatabase, seeded: dict[str, Any]) -> AsyncIterator[AuditService]:
    dal = AsyncDB(pg.dsn, pool_size=10)
    await dal.reflect()
    yield AuditService(dal, gate=FakeGate())
    await dal.close()


def _event(seeded: dict[str, Any], i: int = 0, *, user: int = 0) -> AuditEvent:
    return AuditEvent(
        category=AuditCategory.ADMIN,
        action=f"admin.pg_step_{i}",
        actor_kind=ActorKind.USER,
        actor_user_id=seeded["users"][user][0],
        tenant_id=seeded["tenant_id"],
        target_type="http_route",
        target_id=f"rule:{i}",
        details={"i": i, "scopes": ["tenant:admin", "tenant:read"], "ok": True, "none": None},
    )


async def test_native_types_round_trip_and_the_chain_verifies(
    service: AuditService, seeded: dict[str, Any]
) -> None:
    chain = f"tenant:{seeded['tenant_id']}"
    records = [await service.record(_event(seeded, i)) for i in range(5)]
    assert all(r is not None for r in records)
    report = await service.verify(chain)
    assert report.verification.status is ChainStatus.INTACT
    assert report.verification.examined >= 5
    stored, _ = await service.list_events(chain, filters=ListFilters(), page=1, limit=5)
    # The DB hands back a real UUID / timestamptz / JSONB: the recomputed hashes still match.
    assert str(stored[0].actor_uuid) == str(seeded["users"][0][1])
    assert stored[0].details["scopes"] == ["tenant:admin", "tenant:read"]
    assert stored[0].occurred_at.endswith("Z") and len(stored[0].occurred_at) == 27


async def test_actor_uuid_is_the_hub_users_uuid_from_the_real_table(
    service: AuditService, seeded: dict[str, Any]
) -> None:
    record = await service.record(_event(seeded, 99, user=1))
    assert record is not None
    assert record.actor_uuid == str(seeded["users"][1][1])


async def test_concurrent_writers_produce_one_gapless_chain_via_the_advisory_lock(
    service: AuditService, seeded: dict[str, Any]
) -> None:
    chain = "tenant:" + str(seeded["tenant_id"])
    before = (await service.head(chain)) or None
    start = before.seq if before else 0
    results = await asyncio.gather(*(service.record(_event(seeded, i)) for i in range(40)))
    seqs = sorted(r.seq for r in results if r is not None)
    assert seqs == list(range(start + 1, start + 41))
    assert (await service.verify(chain)).verification.ok


async def test_two_independent_services_racing_the_same_chain_never_fork(
    pg: PgTestDatabase, seeded: dict[str, Any]
) -> None:
    """Separate pools stand in for separate hub-api replicas -- only the DB serialises them."""
    dals = [AsyncDB(pg.dsn, pool_size=5) for _ in range(2)]
    try:
        for dal in dals:
            await dal.reflect()
        services = [AuditService(dal, gate=FakeGate()) for dal in dals]
        await asyncio.gather(*(services[i % 2].record(_event(seeded, i)) for i in range(30)))
        chain = "tenant:" + str(seeded["tenant_id"])
        assert (await services[0].verify(chain)).verification.ok
        rows = _sql(
            pg,
            "SELECT count(*), count(DISTINCT prev_hash) FROM audit_events WHERE chain_id = %s",
            (chain,),
        )
        assert rows[0][0] == rows[0][1]  # every record has a distinct parent: no fork
    finally:
        for dal in dals:
            await dal.close()


async def test_platform_chain_is_independent(service: AuditService) -> None:
    record = await service.record(
        AuditEvent(
            category=AuditCategory.LICENSE,
            action="license.provider_event",
            actor_kind=ActorKind.EXTERNAL,
        )
    )
    assert record is not None and record.chain_id == "platform"
    assert (await service.verify("platform")).verification.ok


async def test_the_application_cannot_rewrite_history_but_a_privileged_tamper_is_detected(
    pg: PgTestDatabase, service: AuditService, seeded: dict[str, Any]
) -> None:
    chain = f"tenant:{seeded['tenant_id']}"
    for i in range(3):
        await service.record(_event(seeded, 200 + i))
    head = await service.head(chain)
    assert head is not None

    # 1. Ordinary SQL (any role, owner included) is refused by the append-only trigger.
    with pytest.raises(psycopg2.Error, match="append-only"):
        _sql(pg, "UPDATE audit_events SET outcome = 'failure' WHERE chain_id = %s", (chain,))
    assert (await service.verify(chain)).verification.ok

    # 2. A privileged operator disables the trigger and edits a record (the case the hash chain
    #    exists for): the edit succeeds at the SQL level but verification pinpoints it.
    _sql(pg, "ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
    try:
        _sql(
            pg,
            "UPDATE audit_events SET outcome = 'failure' WHERE chain_id = %s AND seq = %s",
            (chain, head.seq - 1),
        )
    finally:
        _sql(pg, "ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
    report = await service.verify(chain)
    assert report.verification.status is ChainStatus.BROKEN
    assert report.verification.break_ is not None
    assert report.verification.break_.seq == head.seq - 1
    assert report.verification.break_.reason is ChainBreakReason.HASH_MISMATCH


async def test_unknown_tenant_is_a_loud_error_on_postgres(service: AuditService) -> None:
    with pytest.raises(AuditWriteError, match="tenant that does not exist"):
        await service.record(
            AuditEvent(
                category=AuditCategory.ADMIN,
                action="admin.action",
                actor_kind=ActorKind.SYSTEM,
                tenant_id=987654321,
            )
        )


async def test_bundle_audit_writes_the_legacy_row_and_the_chain_on_real_postgres(
    pg: PgTestDatabase,
    service: AuditService,
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: legacy bundle audit rows must actually be written on real Postgres.

    The legacy insert used a tz-aware value for a naive TIMESTAMP column, which asyncpg rejects, so
    every legacy bundle audit row failed -- silently, for as long as the old `except: pass` existed.
    Now the row is written and the chain gets the event too.
    """
    import services.audit_service as audit_module
    from services import bundle_audit

    audit_module.reset_audit_services()
    monkeypatch.setattr(audit_module, "default_gate", FakeGate())
    try:
        await bundle_audit.record(
            service._dal,
            actor_id=seeded["users"][0][0],
            action="app_installed_globally",
            target_type="app_global_installs",
            target_id="waddles.core.ping@1.0.0",
            details={"tenant_id": seeded["tenant_id"], "install_source": "system:core-seeder"},
        )
    finally:
        audit_module.reset_audit_services()
    legacy = _sql(
        pg,
        "SELECT user_id, action, created_at IS NOT NULL FROM audit_log "
        "WHERE action = 'app_installed_globally' AND target_id = 'waddles.core.ping@1.0.0'",
    )
    assert legacy == [(seeded["users"][0][0], "app_installed_globally", True)]
    chain = _sql(
        pg,
        "SELECT category, action, actor_uuid::text FROM audit_events "
        "WHERE chain_id = %s AND action = 'app_installed_globally'",
        (f"tenant:{seeded['tenant_id']}",),
    )
    assert chain == [("bundle", "app_installed_globally", str(seeded["users"][0][1]))]
